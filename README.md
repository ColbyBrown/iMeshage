# iMeshage - iMessage to Meshtastic Bridge

A macOS-based bridge that connects Apple's iMessage service with the Meshtastic LoRa mesh network, allowing you to communicate with your iMessage contacts from portable devices like the LilyGo T-Deck.

## Overview

This project creates a **virtual node spoofing** system where each of your iMessage contacts appears as a distinct person in your Meshtastic network. When someone messages you via iMessage, it's forwarded to your T-Deck as if they were physically nearby on the mesh. You can reply natively from your T-Deck, and the message gets sent back through iMessage.

## Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────┐
│  iMessage       │         │  Gateway (Mac)   │         │  Meshtastic │
│  Contact        │◄──────►│  AppleScript     │◄──────►│  Python API  │
│                 │         │  + Virtual Node  │         │  LoRa USB    │
└─────────────────┘         │  Spoofing        │         └─────────────┘
                            │  Layer           │                  │
                            └──────────────────┘                  │
                                                                  ▼
                                                         ┌─────────────┐
                                                         │ LilyGo T-Deck│
                                                         │ (or other    │
                                                         │ Meshtastic   │
                                                         │ device)       │
                                                         └─────────────┘
```

## Key Features

- **Virtual Node Mapping**: Each iMessage contact gets a unique virtual Node ID
- **Native DM Interface**: Reply from T-Deck using standard Meshtastic DM threads
- **AppleScript-Based**: No external services like BlueBubbles required
- **Persistent Mapping**: Contact-to-node mappings survive gateway restarts

## Requirements

- macOS (for AppleScript iMessage integration)
- Python 3.10+
- Meshtastic USB-connected radio (gateway node)
- LilyGo T-Deck or other Meshtastic device
- Single private Meshtastic channel configured

## Project Structure

```
iMeshage/
├── README.md              # This file
├── config.json            # Configuration settings
├── node_mapping.json      # Persistent virtual node mappings
├── gateway.py             # Main bridge logic
├── applemessages.py       # AppleScript-based iMessage handler
├── meshtastic_bridge.py   # Meshtastic API integration
└── tests/                 # Test suite
```

## Getting Started

1. Configure your private channel on both the gateway radio and T-Deck
2. Update `config.json` with your settings
3. Run the gateway: `python gateway.py`

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed implementation notes.
