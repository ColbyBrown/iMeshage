"""
Meshtastic Bridge Layer

This module handles communication with the Meshtastic firmware via:
- Serial/USB connection to LoRa radio
- Python meshtastic client library
- Message broadcasting and reception
- Virtual node spoofing for direct message routing
"""

import os
import time
import json
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any, Union
from enum import IntEnum

# Try to import meshtastic Python client
try:
    from meshtastic.client import MeshtasticClient, MeshPacket, MeshConnectionType
    from meshtastic.protobuf.packet_pb2 import PacketIterator
    from meshtastic.protobuf.apps_pb2 import DecodedMessage
except ImportError:
    print("Warning: meshtastic package not installed. Install with: pip install meshtastic")

import serial.tools.list_ports


class PortNumber(IntEnum):
    """Meshtastic application port numbers."""
    TEXT_MESSAGE_APP = 30016
    POSITION_APP = 30020


@dataclass
class Config:
    """Configuration for Meshtastic client."""
    device_path: str = "/dev/cu.usbmodem*"
    channel_index: int = 1  # Secondary channel (slot 1)
    channel_name: str = "iBridge"
    my_node_id: str = "IGW0001"  # Gateway node ID
    tdeck_node_id: str = "TGDK0001"


@dataclass
class VirtualNodeMapping:
    """Maps a virtual node to its contact info."""
    virtual_node_id: str   # e.g., "!99999991"
    integer_id: int        # Integer representation for packet construction
    friendly_name: str     # Display name (e.g., "John Doe")
    chat_guid: Optional[str] = None  # iMessage conversation ID


class MeshtasticBridge:
    """
    Bridge to Meshtastic via serial connection.

    Handles:
    - Connecting to LoRa radio via USB/Serial
    - Listening for incoming mesh packets
    - Broadcasting outgoing messages
    - Virtual node spoofing and NodeInfo injection
    """

    def __init__(self, config: Optional[Config] = None, mapping_file: str = "node_mapping.json"):
        self.config = config or Config()
        self.mapping_file = mapping_file
        self.client: Optional[MeshtasticClient] = None
        self.is_connected = False
        self.packet_callback: Optional[Callable[[MeshPacket], None]] = None
        self.subscribed_to_text = False
        self._pending_ack: Dict[int, MeshPacket] = {}

    def connect(self) -> bool:
        """
        Connect to the Meshtastic radio device.

        Returns:
            True if connection was successful
        """
        try:
            # Find the actual device path
            available_ports = self._find_available_device()
            if not available_ports:
                print(f"Error: No Meshtastic USB device found at {self.config.device_path}")
                return False

            self.client = MeshtasticClient(self.config.device_path, connect=True)

            # Configure the channel (if needed)
            self._configure_channel()

            self.is_connected = True
            print(f"Connected to Meshtastic node: {self.client.my_info.id}")
            return True

        except Exception as e:
            print(f"Error connecting to Meshtastic: {e}")
            return False

    def disconnect(self) -> None:
        """Close the connection to the radio."""
        if self.client:
            try:
                self.client.disconnect()
            except:
                pass
            self.is_connected = False
            print("Disconnected from Meshtastic")

    def _find_available_device(self) -> List[str]:
        """Find available serial ports matching device path pattern."""
        import fnmatch
        matching_ports = []
        for port in serial.tools.list_ports.comports():
            if fnmatch.fnmatch(port.device, self.config.device_path):
                # Check if port has data (not auto-baud)
                try:
                    with open(port.device, 'rb') as f:
                        byte_val = f.read(1024)
                        if byte_val and ord(byte_val[0]) != 0:
                            matching_ports.append(port.device)
                except:
                    pass
        return matching_ports

    def _configure_channel(self) -> None:
        """
        Select the configured channel slot on the gateway radio.

        The channel itself (name + PSK) is created out-of-band, as described
        in GUIDE.md ("Configure Channel Settings"); the bridge only connects
        to the slot given by ``config.channel_index``.
        """
        slot = self.config.channel_index
        print(
            f"Using Meshtastic channel slot {slot} "
            f"(expected name: '{self.config.channel_name}')"
        )

        if not self.client or not hasattr(self.client, "channels"):
            return

        try:
            channel = self.client.channels[slot]
            if channel is None or getattr(channel, "role", 0) == 0:
                print(
                    f"Warning: Channel slot {slot} does not appear to be "
                    f"enabled on this radio. Create it first (see GUIDE.md):\n"
                    f"  meshtastic --port {self.config.device_path} "
                    f"--ch-index {slot} --ch-set name {self.config.channel_name}\n"
                    f"  meshtastic --port {self.config.device_path} "
                    f"--ch-index {slot} --ch-set psk random"
                )
        except (KeyError, IndexError, AttributeError):
            print(
                f"Warning: Could not read channel slot {slot} from the radio; "
                "verify it is configured with a matching name and PSK "
                "(see GUIDE.md)."
            )

    def subscribe_to_text_messages(self) -> bool:
        """Subscribe to TEXT_MESSAGE_APP packets (port 30016)."""
        if not self.client or not self.is_connected:
            return False

        try:
            # Subscribe to text message topic
            self.subscribed_to_text = True

            # Subscribe to mesh messages in general
            pub.subscribe(self.on_receive, "meshtastic.receive")

            print("Subscribed to text message channel")
            return True
        except Exception as e:
            print(f"Error subscribing to messages: {e}")
            return False

    def publish_text_message(
        self,
        payload: str,
        from_node: Optional[str] = None,
        to_nodes: Optional[List[str]] = None
    ) -> bool:
        """
        Broadcast a text message over the LoRa network.

        Args:
            payload: The message text to broadcast
            from_node: Source node ID (defaults to gateway's own ID)
            to_nodes: List of target node IDs (broadcast if None)

        Returns:
            True if message was published successfully
        """
        if not self.client or not self.is_connected:
            return False

        try:
            # Encode text as UTF-16LE, append checksum and length bytes
            # Meshtastic expects specific packet encoding
            encoded = payload.encode('utf-16-le')

            # Create MeshPacket with TEXT_MESSAGE_APP
            mesh_packet = self.client.create_mesh_packet(
                from_node_id=self.client.my_info.id if not from_node else from_node,
                decoded=DecodedMessage(port_number=PortNumber.TEXT_MESSAGE_APP.value),
                payload=encoded
            )

            # If targeting specific nodes (like DMs to virtual nodes)
            if to_nodes:
                self.client.send_raw(to_nodes[0], mesh_packet.payload)

            # Otherwise broadcast over the mesh channel
            else:
                self.client.sendMessage(
                    portNum=PortNumber.TEXT_MESSAGE_APP.value,
                    text=payload,
                    fromNode=self.config.my_node_id if not from_node else from_node
                )

            return True
        except Exception as e:
            print(f"Error publishing message: {e}")
            return False

    def on_receive(self, packet: MeshPacket) -> None:
        """
        Callback for incoming mesh packets.

        Args:
            packet: The received MeshPacket
        """
        if self.packet_callback:
            try:
                self.packet_callback(packet)
            except Exception as e:
                print(f"Error handling received packet: {e}")

    def broadcast_with_virtual_node(
        self,
        contact_friendly_name: str,
        message_text: str,
        my_node_id: Optional[str] = None
    ) -> bool:
        """
        Broadcast a message with sender name included.

        Args:
            contact_friendly_name: Name of the sender (e.g., "John Doe")
            message_text: Message content
            my_node_id: Override gateway's own node ID

        Returns:
            True if broadcast succeeded
        """
        # Format: "[Name]: Message"
        payload = f"{contact_friendly_name}: {message_text}"

        return self.publish_text_message(payload, from_node=my_node_id)

    def handle_incoming_virtual_node(
        self,
        packet: MeshPacket,
        mappings: Dict[str, VirtualNodeMapping]
    ) -> Optional[str]:
        """
        Handle incoming packet from a virtual node.

        Args:
            packet: Received mesh packet
            mappings: Dictionary of virtual_node_id -> VirtualNodeMapping

        Returns:
            The iMessage chat_guid if this packet is for a known contact, None otherwise
        """
        # Check if this packet is addressed to our gateway (direct message)
        if packet.to is not None and packet.to != self.client.my_info.id:
            target_id = packet.to
            if target_id in mappings:
                mapping = mappings[target_id]

                # Extract the message text
                payload_str = self._decode_packet_payload(packet)

                # Return the chat_guid for routing to iMessage
                return mapping.chat_guid, payload_str

        return None

    def _decode_packet_payload(self, packet: MeshPacket) -> str:
        """Extract and decode the message payload from a packet."""
        try:
            if hasattr(packet, 'decoded'):
                decoded = packet.decoded

                # Check for DecodedMessage protobuf
                if hasattr(decoded, 'portnum') and hasattr(decoded, 'payload'):
                    port_num = decoded.portnum

                    if port_num == PortNumber.TEXT_MESSAGE_APP.value:
                        # Text message
                        return decoded.payload.decode('utf-16-le') if decoded.payload else ""

        except Exception as e:
            print(f"Error decoding payload: {e}")

        return ""

    def inject_node_info(
        self,
        virtual_node_id: str,
        friendly_name: str,
        location: Optional[tuple] = None
    ) -> bool:
        """
        Inject NodeInfo packet to register a new virtual node.

        Args:
            virtual_node_id: The Node ID to register
            friendly_name: Display name for the node
            location: (lat, lon) tuple for GPS location

        Returns:
            True if NodeInfo was sent successfully
        """
        try:
            # Create a user info packet
            payload = f"User Short Name: {friendly_name.split()[0] if friendly_name else ''}"

            return self.publish_text_message(
                payload=f"!{virtual_node_id}: Registered",
                from_node=virtual_node_id
            )

        except Exception as e:
            print(f"Error injecting node info: {e}")
            return False

    def handle_outgoing_from_virtual_node(
        self,
        virtual_node_id: str,
        message_text: str,
        mappings: Dict[str, VirtualNodeMapping]
    ) -> Optional[Callable]:
        """
        Handle a reply from a virtual node and route it to the correct iMessage.

        Args:
            virtual_node_id: The source Node ID of the sender
            message_text: Message content from T-Deck
            mappings: Virtual node mappings

        Returns:
            Callback function to send to BlueBubbles/iMessage, or None if routing failed
        """
        # Check who this is coming from
        if virtual_node_id in mappings:
            mapping = mappings[virtual_node_id]

            # Return the chat_guid and message for iMessage routing
            return lambda: {
                'chat_guid': mapping.chat_guid,
                'message_text': message_text,
                'contact_name': mapping.friendly_name
            }

        print(f"Unknown virtual node sending message: {virtual_node_id}")
        return None

    def setup_message_handler(
        self,
        on_incoming: Callable[[MeshPacket], Optional[str]],
        on_outgoing: Callable[[VirtualNodeMapping, str], Optional[Callable]]
    ) -> None:
        """
        Set up callbacks for incoming and outgoing messages.

        Args:
            on_incoming: Callback(packet) -> chat_guid or None
            on_outgoing: Callback(mapping, text) -> send_iMessage_callback or None
        """
        self.packet_callback = lambda pkt: [
            self.handle_incoming_virtual_node(pkt, self._load_mappings()),
            self.handle_outgoing_from_virtual_node(
                pkt.to if pkt.to else "",
                self._decode_packet_payload(pkt),
                self._load_mappings()
            )
        ]

    def _load_mappings(self) -> Dict[str, VirtualNodeMapping]:
        """Load virtual node mappings from JSON file."""
        try:
            with open(self.mapping_file, 'r') as f:
                data = json.load(f)

            return {
                vni: VirtualNodeMapping(
                    virtual_node_id=vni,
                    integer_id=int(vni[1:], 16),
                    friendly_name=m['friendly_name'],
                    chat_guid=m.get('chat_guid')
                )
                for vni, m in data.items()
            }
        except (json.JSONDecodeError, IOError):
            return {}

    def send_ack_for_virtual_node(
        self,
        virtual_node_id: str,
        target_node: Optional[str] = None
    ) -> bool:
        """
        Send an ACK packet for a virtual node to simulate delivery.

        Args:
            virtual_node_id: The Node ID that sent the message
            target_node: Who we're acknowledging (defaults to our own)

        Returns:
            True if ACK was sent successfully
        """
        try:
            # Create an ACK packet spoofing the virtual node
            ack_payload = f"{virtual_node_id}: [ACK]"

            return self.publish_text_message(
                payload=ack_payload,
                from_node=virtual_node_id,
                to_nodes=[target_node] if target_node else None
            )

        except Exception as e:
            print(f"Error sending ACK: {e}")
            return False


class VirtualNodeManager:
    """Manages virtual node ID allocation and mapping."""

    def __init__(self, start_hex: str = "99999991", max_nodes: int = 100):
        self.start_hex = start_hex
        self.max_nodes = max_nodes
        self.current_allocation = int.from_bytes(
            bytes.fromhex(start_hex), 'big'
        )

    def allocate_virtual_node_id(self) -> str:
        """Allocate a new virtual node ID."""
        hex_str = f"{self.current_allocation:08X}"
        new_id = f"!{hex_str}"

        # Check if we're at the limit
        current_int = int.from_bytes(bytes.fromhex(hex_str), 'big')
        max_int = int.from_bytes(bytes.fromhex("FFFFFFFF"), 'big')

        if current_int > max_int - self.max_nodes:
            print("Warning: Approaching virtual node ID limit")

        self.current_allocation += 1
        return new_id

    def save_mapping(self, mappings: Dict[str, VirtualNodeMapping]) -> None:
        """Save current mappings to file."""
        data = {
            vni: {
                'integer_id': hex(mapping.integer_id)[2:].upper(),
                'friendly_name': mapping.friendly_name,
                'chat_guid': mapping.chat_guid
            }
            for vni, mapping in mappings.items()
        }

        try:
            with open(self.mapping_file, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"Saved {len(mappings)} virtual node mappings")
        except IOError as e:
            print(f"Error saving mappings: {e}")


# Main bridge class combining both layers
class IToMBridge:
    """
    Complete iMessage to Meshtastic Bridge.

    Integrates AppleScript iMessage handling with Meshtastic LoRa networking.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config = Config()
        self.node_manager = VirtualNodeManager(start_hex="99999991")

        # Load configuration if provided
        if config_path and Path(config_path).exists():
            with open(config_path, 'r') as f:
                user_config = json.load(f)

            # Apply user overrides to default config
            self.config.device_path = user_config.get('meshtastic', {}).get(
                'device_path', '/dev/cu.usbmodem*'
            )
            self.config.channel_index = user_config.get('meshtastic', {}).get(
                'channel_index', 1
            )

        self.applescript_bridge: Optional[AppleScriptBridge] = None
        self.meshtastic_bridge: Optional[MeshtasticBridge] = None

        self.running = False
        self._last_messages: Dict[str, Dict] = {}

    def initialize(self) -> bool:
        """Initialize both iMessage and Meshtastic layers."""
        try:
            # Initialize AppleScript bridge
            mapping_file = Path(self.config.device_path).parent / 'node_mapping.json'
            self.applescript_bridge = AppleScriptBridge()

            # Load existing mappings or create new ones
            if not self.node_manager.mapping_file.exists():
                with open(self.node_manager.mapping_file, 'w') as f:
                    json.dump({}, f)

        except Exception as e:
            print(f"Error initializing bridge: {e}")
            return False

        return True

    def start(self, config_path: Optional[str] = None) -> bool:
        """
        Start the bridge and begin message routing.

        Args:
            config_path: Optional path to configuration file

        Returns:
            True if started successfully
        """
        if not self.initialize():
            return False

        # Load mapping file for node manager
        with open(self.node_manager.mapping_file, 'r') as f:
            existing_mappings = json.load(f)

        self.meshtastic_bridge = MeshtasticBridge(
            config=self.config,
            mapping_file=self.node_manager.mapping_file
        )

        if not self.meshtastic_bridge.connect():
            return False

        if not self.meshtastic_bridge.subscribe_to_text_messages():
            print("Warning: Could not subscribe to text messages")

        # Set up message routing callbacks
        self._setup_routing()

        self.running = True
        print("iMeshage bridge is running...")

        return True

    def stop(self) -> None:
        """Stop the bridge."""
        if self.meshtastic_bridge:
            self.meshtastic_bridge.disconnect()

        if self.applescript_bridge:
            # Keep the database connection open for monitoring
            pass

        self.running = False
        print("Bridge stopped")

    def _setup_routing(self) -> None:
        """Set up message routing between iMessage and Meshtastic."""
        mesh_bridge = self.meshtastic_bridge
        apple_bridge = self.applescript_bridge

        if not apple_bridge or not mesh_bridge:
            return

        # Register callback for new iMessages
        def on_new_imessage(msg: AppleScriptBridge.Message, virtual_node_id: str) -> None:
            """Handle incoming iMessage."""
            mapping_key = self.node_manager.allocate_virtual_node_id()
            friendly_name = msg.sender or f"Contact {virtual_node_id}"

            # Save the mapping
            with open(self.node_manager.mapping_file, 'w') as f:
                existing_mappings = json.load(f)
                existing_mappings[mapping_key] = {
                    'virtual_node_id': mapping_key,
                    'friendly_name': friendly_name,
                    'chat_guid': msg.chat_guid
                }
                json.dump(existing_mappings, f, indent=2)

            # Broadcast to Meshtastic with sender name
            mesh_bridge.broadcast_with_virtual_node(
                contact_friendly_name=friendly_name,
                message_text=msg.plaintext
            )

        apple_bridge.register_new_message_callback(on_new_imessage)

        # Also handle incoming messages immediately on startup
        def check_pending():
            if apple_bridge and mesh_bridge:
                for msg in apple_bridge.get_incoming_messages():
                    virtual_node_id = self.node_manager.allocate_virtual_node_id()
                    friendly_name = msg.sender or f"Contact {virtual_node_id}"

                    with open(self.node_manager.mapping_file, 'w') as f:
                        existing_mappings = json.load(f)
                        existing_mappings[virtual_node_id] = {
                            'virtual_node_id': virtual_node_id,
                            'friendly_name': friendly_name,
                            'chat_guid': msg.chat_guid
                        }
                        json.dump(existing_mappings, f, indent=2)

                    mesh_bridge.broadcast_with_virtual_node(
                        contact_friendly_name=friendly_name,
                        message_text=msg.plaintext
                    )

        check_pending()

    def run(self) -> None:
        """Run the bridge as a daemon."""
        if not self.meshtastic_bridge or not self.meshtastic_bridge.is_connected:
            print("Error: Bridge is not connected")
            return

        apple_bridge = self.applescript_bridge

        if not apple_bridge:
            print("Error: AppleScript bridge not initialized")
            return

        print("iMeshage bridge monitoring for new messages...")

        # Monitor for incoming iMessages every 5 seconds
        while self.running:
            try:
                messages = apple_bridge.get_incoming_messages()
                mesh_bridge = self.meshtastic_bridge

                for msg in messages:
                    virtual_node_id = self.node_manager.allocate_virtual_node_id()
                    friendly_name = msg.sender or f"Contact {virtual_node_id}"

                    # Save mapping
                    with open(self.node_manager.mapping_file, 'w') as f:
                        existing_mappings = json.load(f)
                        existing_mappings[virtual_node_id] = {
                            'virtual_node_id': virtual_node_id,
                            'friendly_name': friendly_name,
                            'chat_guid': msg.chat_guid
                        }
                        json.dump(existing_mappings, f, indent=2)

                    # Broadcast to mesh
                    mesh_bridge.broadcast_with_virtual_node(
                        contact_friendly_name=friendly_name,
                        message_text=msg.plaintext
                    )

            except Exception as e:
                print(f"Error during monitoring: {e}")

            time.sleep(5.0)


# CLI entry point
def main():
    """Main entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="iMeshage - iMessage to Meshtastic Bridge")
    parser.add_argument("-c", "--config", help="Path to configuration file (JSON)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    # Create and start bridge
    bridge = IToMBridge(config_path=args.config)

    if not bridge.initialize():
        print("Failed to initialize bridge")
        return 1

    if not bridge.start():
        print("Failed to start bridge")
        return 1

    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\nStopping bridge...")
        bridge.stop()
        return 0

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
