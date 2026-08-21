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
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Dict, Any, Union
from enum import IntEnum
from pathlib import Path

log = logging.getLogger(__name__)

# Import the real meshtastic 2.x API.  See https://python.meshtastic.org
# and the working reference at /Users/colby/zen_sos/watch_for_sos.py.
try:
    import meshtastic
    import meshtastic.serial_interface
    from meshtastic.protobuf import portnums_pb2, mesh_pb2
    from pubsub import pub
except ImportError:
    log.warning("meshtastic package not installed. Install with: pip install meshtastic pyserial")
    meshtastic = None
    portnums_pb2 = None
    mesh_pb2 = None
    pub = None

import serial.tools.list_ports

from applemessages import AppleScriptBridge

class PortNumber(IntEnum):
    """Meshtastic application port numbers (portnums_pb2.PortNum)."""
    TEXT_MESSAGE_APP = 1
    POSITION_APP = 3


@dataclass
class Config:
    """Configuration for Meshtastic client."""
    device_path: str = "/dev/cu.usbserial*"  # CP210x/CH340 bridge; USB-native boards use /dev/cu.usbmodem*
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
        self.client: Optional[Any] = None
        self.is_connected = False
        self.my_node_id: Optional[str] = None
        self.packet_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self.subscribed_to_text = False
        self._pending_ack: Dict[int, Dict[str, Any]] = {}

    def connect(self) -> bool:
        """
        Connect to the Meshtastic radio device.

        Returns:
            True if connection was successful
        """
        try:
            if meshtastic is None:
                log.error("meshtastic package not available")
                return False

            # Find the actual device path (None = let meshtastic auto-detect)
            available_ports = self._find_available_device()
            dev_path = available_ports[0] if available_ports else None
            if not available_ports and self.config.device_path not in ("", None):
                log.error("No Meshtastic USB device found matching %s", self.config.device_path)
                return False

            log.info("Connecting to Meshtastic via %s", dev_path or "auto-detect")
            self.client = meshtastic.serial_interface.SerialInterface(devPath=dev_path)

            # Wait briefly for the radio to send its config so myInfo is populated
            self.client.waitForConfig()

            # Configure the channel (if needed)
            self._configure_channel()

            try:
                my_num = self.client.myInfo.my_node_num
                self.my_node_id = f"!{my_num:08x}"
            except Exception:
                self.my_node_id = None

            self.is_connected = True
            log.info("Connected to Meshtastic node %s", self.my_node_id)
            return True

        except Exception as e:
            log.exception("Error connecting to Meshtastic: %s", e)
            return False

    def disconnect(self) -> None:
        """Close the connection to the radio."""
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.is_connected = False
            log.info("Disconnected from Meshtastic")

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
        log.info(
            "Using Meshtastic channel slot %d (expected name: '%s')",
            slot, self.config.channel_name,
        )

        if not self.client or not hasattr(self.client, "localNode"):
            return

        try:
            channel = self.client.localNode.getChannelByChannelIndex(slot)
            if channel is None or getattr(channel, "role", 0) == 0:
                log.warning(
                    "Channel slot %d does not appear to be enabled on this "
                    "radio. Create it first (see GUIDE.md):\n"
                    "  meshtastic --port %s --ch-index %d --ch-set name %s\n"
                    "  meshtastic --port %s --ch-index %d --ch-set psk random",
                    slot, self.config.device_path, slot, self.config.channel_name,
                    self.config.device_path, slot,
                )
            else:
                ch_name = getattr(getattr(channel, "settings", None), "name", "") or ""
                log.info("Channel slot %d OK (name='%s', role=%d)", slot, ch_name, getattr(channel, "role", 0))
        except (KeyError, IndexError, AttributeError):
            log.warning(
                "Could not read channel slot %s from the radio; verify it is "
                "configured with a matching name and PSK (see GUIDE.md).", slot,
            )

    def subscribe_to_text_messages(self) -> bool:
        """Subscribe to TEXT_MESSAGE_APP packets."""
        if not self.client or not self.is_connected:
            return False

        try:
            pub.subscribe(self.on_receive, "meshtastic.receive")
            self.subscribed_to_text = True
            log.info("Subscribed to meshtastic.receive")
            return True
        except Exception as e:
            log.exception("Error subscribing to messages: %s", e)
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
            from_node: Source node ID (accepted but not honored -- see note)
            to_nodes: List of target node IDs (broadcast if None)

        Returns:
            True if message was published successfully
        """
        if not self.client or not self.is_connected:
            log.warning("publish_text_message: not connected")
            return False

        try:
            # meshtastic's sendText handles UTF-8 encoding and packet
            # construction internally.  `from_node` is accepted for API
            # compatibility but is not honored -- the sender is always the
            # gateway radio (a limitation of the simple sendText API).
            dest = to_nodes[0] if to_nodes else "^all"
            self.client.sendText(
                payload,
                destinationId=dest,
                channelIndex=self.config.channel_index,
            )
            log.info(
                "sendText -> dest=%s channel=%d payload_len=%d",
                dest, self.config.channel_index, len(payload),
            )
            log.debug("sendText payload=%r", payload)
            return True
        except Exception as e:
            log.exception("Error publishing message: %s", e)
            return False

    def on_receive(self, packet: Dict[str, Any], interface: Any = None) -> None:
        """
        Callback for incoming mesh packets.

        Args:
            packet: The received packet as a dictionary (pubsub payload).
            interface: The MeshInterface that received it (unused).
        """
        decoded = packet.get("decoded", {}) if isinstance(packet, dict) else {}
        log.info(
            "on_receive from=%s portnum=%s text=%r",
            packet.get("fromId") if isinstance(packet, dict) else None,
            decoded.get("portnum"),
            decoded.get("text"),
        )
        if self.packet_callback:
            try:
                self.packet_callback(packet)
            except Exception as e:
                log.exception("Error handling received packet: %s", e)

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
        packet: Dict[str, Any],
        mappings: Dict[str, VirtualNodeMapping]
    ) -> Optional[str]:
        """
        Handle incoming packet from a virtual node.

        Args:
            packet: Received mesh packet (dictionary from pubsub)
            mappings: Dictionary of virtual_node_id -> VirtualNodeMapping

        Returns:
            The iMessage chat_guid if this packet is for a known contact, None otherwise
        """
        # A DM addressed to a virtual node arrives with that node's id in `toId`.
        # Broadcasts have toId == "^all" (or the gateway id); for those we look at
        # the sender instead.
        to_id = packet.get("toId")
        target_id = to_id if to_id and to_id != "^all" else packet.get("fromId")

        if target_id and target_id in mappings:
            mapping = mappings[target_id]

            # Extract the message text
            payload_str = self._decode_packet_payload(packet)

            # Return the chat_guid for routing to iMessage
            return mapping.chat_guid, payload_str

        return None

    def _decode_packet_payload(self, packet: Dict[str, Any]) -> str:
        """Extract and decode the message payload from a packet."""
        try:
            decoded = packet.get("decoded", {})
            if not decoded:
                return ""

            # Text messages get a decoded "text" field populated by meshtastic's
            # _onTextReceive handler.
            text = decoded.get("text")
            if text:
                return text

            # Fall back to raw payload bytes (utf-8 for TEXT_MESSAGE_APP).
            payload = decoded.get("payload")
            if payload:
                try:
                    return payload.decode("utf-8")
                except UnicodeDecodeError:
                    return ""

        except Exception as e:
            log.exception("Error decoding payload: %s", e)

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
            log.exception("Error injecting node info: %s", e)
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

        log.warning("Unknown virtual node sending message: %s", virtual_node_id)
        return None

    def setup_message_handler(
        self,
        on_incoming: Callable[[Dict[str, Any]], Optional[str]],
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
                pkt.get("toId") or pkt.get("fromId") or "",
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
            log.exception("Error sending ACK: %s", e)
            return False


class VirtualNodeManager:
    """Manages virtual node ID allocation and mapping."""


    def __init__(self, start_hex: str = "99999991", max_nodes: int = 100, mapping_file: str = "node_mapping.json"):

        self.start_hex = start_hex
        self.max_nodes = max_nodes
        self.mapping_file = Path(mapping_file)
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
            log.warning("Approaching virtual node ID limit")

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
            log.info("Saved %d virtual node mappings", len(mappings))
        except IOError as e:
            log.error("Error saving mappings: %s", e)


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
                'device_path', '/dev/cu.usbserial-0001'
            )
            self.config.channel_index = user_config.get('meshtastic', {}).get(
                'channel_index', 2
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
            log.exception("Error initializing bridge: %s", e)
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
            log.warning("Could not subscribe to text messages")

        # Set up message routing callbacks
        self._setup_routing()

        self.running = True
        log.info("iMeshage bridge is running...")

        return True

    def stop(self) -> None:
        """Stop the bridge."""
        if self.meshtastic_bridge:
            self.meshtastic_bridge.disconnect()

        if self.applescript_bridge:
            # Keep the database connection open for monitoring
            pass

        self.running = False
        log.info("Bridge stopped")

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
            log.error("Bridge is not connected")
            return

        apple_bridge = self.applescript_bridge

        if not apple_bridge:
            log.error("AppleScript bridge not initialized")
            return

        log.info("iMeshage bridge monitoring for new messages...")

        # Monitor for incoming iMessages every 5 seconds
        while self.running:
            try:
                messages = apple_bridge.get_incoming_messages()
                mesh_bridge = self.meshtastic_bridge

                if messages:
                    log.info("poll: %d new iMessage(s)", len(messages))

                for msg in messages:
                    virtual_node_id = self.node_manager.allocate_virtual_node_id()
                    friendly_name = msg.sender or f"Contact {virtual_node_id}"

                    log.info(
                        "forwarding iMessage from %s (chat=%s): %r",
                        friendly_name, msg.chat_guid, msg.plaintext[:80],
                    )

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
                log.exception("Error during monitoring: %s", e)

            time.sleep(5.0)


# CLI entry point
def main():
    """Main entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="iMeshage - iMessage to Meshtastic Bridge")
    parser.add_argument("-c", "--config", help="Path to configuration file (JSON)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Create and start bridge
    bridge = IToMBridge(config_path=args.config)

    if not bridge.initialize():
        log.error("Failed to initialize bridge")
        return 1

    if not bridge.start():
        log.error("Failed to start bridge")
        return 1

    try:
        bridge.run()
    except KeyboardInterrupt:
        log.info("Stopping bridge...")
        bridge.stop()
        return 0

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
