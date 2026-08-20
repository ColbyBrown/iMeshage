# Architecture Documentation

## System Overview

The iMeshage bridge connects Apple's iMessage service with the Meshtastic LoRa mesh network through a gateway node (PC/Mac) connected to a USB-mounted LoRa radio. This document details how the virtual node spoofing approach solves the 8-channel limitation.

## Core Concepts

### Virtual Node Spoofing

Instead of creating one channel per contact (which would require up to thousands of channels), each iMessage contact is assigned a **unique virtual Node ID**. When an iMessage arrives:

1. The gateway allocates a new virtual Node ID (e.g., `!99999991`)
2. Injects a NodeInfo packet with the contact's friendly name
3. Formats the message as `"[Name]: Message"` and broadcasts over the LoRa channel
4. Your T-Deck receives it as a direct message from that virtual node

When replying on your T-Deck:

1. You compose a native DM to "John Doe" (the virtual node)
2. The packet arrives at your gateway with `to = !99999991`
3. The gateway looks up which iMessage contact corresponds to that Node ID
4. Sends it via AppleScript to the correct conversation

### Message Wrapping Format

All messages are wrapped with metadata in the LoRa payload:

```
[John Doe]: Hey, are we still meeting at 2?
```

This allows routing without requiring multiple channels. For replies:

- Virtual node ID is preserved in packet headers (`from` / `to`)
- Content is stripped of any prefix and sent directly

## Technical Design

### Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      iMeshage Gateway                             │
│  ┌──────────────┐  ┌─────────────────────────┐  ┌─────────────┐ │
│  │ AppleScript  │  │   Routing Logic         │  │ Meshtastic  │ │
│  │ Layer        │◄─┤                         ├─►│ Layer       │ │
│  │              │  │   • Message wrapping    │  │              │ │
│  └──────────────┘  │   • Virtual ID mapping  │  └─────────────┘ │
│                    │   • ACK spoofing        │                   │
│  ┌──────────────┐  │                         │     ┌───────────┐│
│  │ SQLite DB    │  │                         │     │ LoRa USB   ││
│  │ (chat.db)    │  │   Persistent mapping    │◄────┤ radio     ││
│  └──────────────┘  │                         │     └───────────┘│
│                    └─────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────┘
                                  ▼
                          ┌─────────────────┐
                          │ LoRa Network    │
                          │ (Single Private │
                          │ Channel - Slot 1)│
                          └─────────────────┘
                                  ▼
                         ┌──────────────────────┐
                         │   Portable Device    │
                         │   (LilyGo T-Deck)     │
                         └──────────────────────┘
```

### Data Flow: Incoming Messages (iMessage → Meshtastic)

1. **AppleScript Layer** monitors `/Library/Messages/chat.db` for new messages
2. When a message arrives from contact X:
   - A new virtual Node ID is allocated: `!99999991`
   - Contact name "John Doe" is stored in the mapping file
3. Message formatting: `"[John Doe]: Message content"`
4. Meshtastic API publishes packet over LoRa channel

### Data Flow: Outgoing Messages (Meshtastic → iMessage)

1. T-Deck user composes DM to "John Doe"
2. Packet arrives at gateway with virtual Node ID in headers
3. Routing layer looks up which chat_guid corresponds to `!99999991`
4. AppleScript sends message to that conversation

## Key Files

### `node_mapping.json`

Persistent mapping between iMessage conversations and Meshtastic virtual nodes:

```json
{
  "!99999991": {
    "virtual_node_id": "!99999991",
    "friendly_name": "John Doe",
    "chat_guid": "a3f2c8e9-..."
  },
  "!99999992": {
    "virtual_node_id": "!99999992",
    "friendly_name": "Jane Smith", 
    "chat_guid": "b7d4f1a2-..."
  }
}
```

### Configuration (`config.json`)

```json
{
  "meshtastic": {
    "device_path": "/dev/cu.usbmodem*",
    "channel_index": 1,
    "channel_name": "iBridge",
    "my_node_id": "IGW0001"
  },
  "virtual_nodes": {
    "start_hex": "99999991"
  }
}
```

## Technical Considerations

### Virtual Node ID Limits

- Meshtastic firmware can track ~100-200 nodes
- This is sufficient for typical user needs (50-100 contacts)
- IDs are allocated sequentially from `!99999991`

### Message MTU

- LoRa packets limited to ~237 bytes on standard config
- Long messages are automatically truncated or chunked
- Consider message length when sending large content

### State Persistence

- Mappings survive gateway restarts
- Database connection maintained for efficiency
- ACK spoofing required for delivery indicators on T-Deck

## Installation Requirements

```bash
pip install meshtastic PySerial PyQt5 AppKit Quartz kivy-ios
```

Note: On macOS, you may need to adjust permissions for USB serial devices.
