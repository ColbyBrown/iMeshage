# Getting Started with iMeshage

This guide will walk you through setting up the iMessage to Meshtastic bridge on your Mac.

## Prerequisites

- macOS 12 (Monterey) or later
- USB-connected Meshtastic LoRa radio (or LilyGo T-Deck with serial adapter)
- Python 3.10+ installed
- Comfortable with basic terminal commands

## Step 1: Hardware Setup

### Connect Your Radio

Plug your Meshtastic-enabled USB radio into your Mac. It should appear in `/dev/cu.usbmodem*`.

Check for available devices:

```bash
ls -l /dev/cu.usbmodem*
# or
dmesg | tail -20
```

### Configure Channel Settings

1. Log into your Meshtastic radio via SSH/UART
2. Set up a private channel on slot 1 (index 1):

```python
from meshtastic.util.telemetry_setter import set_config

set_config({
    "subconfig": {
        "app": {
            "channel": "0x5A"  # Your chosen channel hash
        }
    }
})
```

## Step 2: Python Dependencies

```bash
python -m venv .venv
source .venv/bin/activate

pip install meshtastic PySerial AppKit Quartz
```

Note: On macOS, you may need `brew install python` or adjust paths.

## Step 3: Initial Configuration

Edit `config.json`:

```json
{
  "meshtastic": {
    "device_path": "/dev/cu.usbmodem*",
    "channel_index": 1,
    "channel_name": "iBridge",
    "my_node_id": "IGW0001"
  }
}
```

## Step 4: Running the Bridge

Start the bridge:

```bash
python meshtastic_bridge.py -c config.json
```

You should see output like:

```
Connected to Meshtastic node: IGW0001
Subscribed to text message channel
iMeshage bridge monitoring for new messages...
```

## Step 5: Testing

### Test Incoming Message Flow

1. Send a text message to your Mac's iMessage number from another device
2. The bridge should detect it and forward it to your LoRa network
3. On your T-Deck, you'll see: `"[Contact Name]: Your message"`

### Test Outgoing Message Flow

1. From your T-Deck, send a reply to any virtual node
2. The gateway receives it and routes via AppleScript to the correct conversation
3. Check iMessage on your Mac to verify delivery (green checkmark)

## Troubleshooting

### "No USB device found"

Make sure your radio is properly connected and configured:

```bash
ls /dev/cu.* | grep modem
```

If nothing appears, check `/System/Library/Extensions/IOUSBMassStorage.kext` permissions.

### Messages not forwarding

Verify your Meshtastic channel is active:

```bash
python3 -c "from meshtastic.client import MeshtasticClient; c = MeshtasticClient('/dev/cu.usbmodem*', connect=True); print(c.my_info)"
c.disconnect()
```

### AppleScript not working

Ensure Messages app is open and you're signed into iMessage:

```bash
osascript -e 'tell application "Messages" to return (count of chats)'
# Should return a number > 0 if connected
```

## Next Steps

- Configure ACK handling for delivery indicators
- Set up automated monitoring via systemd/cron
- Integrate with your favorite LoRa network

See `ARCHITECTURE.md` for more technical details.
