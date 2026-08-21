# Getting Started with iMeshage

This guide will walk you through setting up the iMessage to Meshtastic bridge on your Mac.

## Prerequisites

- macOS 12 (Monterey) or later
- USB-connected Meshtastic LoRa radio (or LilyGo T-Deck with serial adapter)
- Python 3.10+ installed
- Comfortable with basic terminal commands

## Step 1: Hardware Setup

### Connect Your Radio

Plug your Meshtastic-enabled USB radio into your Mac. Depending on the board's hardware, it appears as **one of two** prefixes:

- `/dev/cu.usbserial-*` — boards with a CP210x, CH340, or FTDI USB-to-serial bridge chip
- `/dev/cu.usbmodem*` — boards with native USB (USB CDC)

Both are equally valid. List the candidates:

```bash
ls -l /dev/cu.*
```

If you're unsure which entry is the radio, unplug it, run the command again, and see which one disappears. Always use the `/dev/cu.*` entry, never its `/dev/tty.*` twin. Whichever prefix you see here (e.g. `/dev/cu.usbserial*`) is what goes into `config.json` in Step 3.

### Configure Channel Settings

Meshtastic radios do **not** have an SSH or UART login. Channels are configured over the USB serial connection using the `meshtastic` command-line tool (installed below in Step 2) or the Meshtastic mobile/desktop app.

A Meshtastic channel is defined by two values that are only valid **together**:

- a **channel name** (e.g. `iBridge`), and
- a **pre-shared key** (PSK).

Both your gateway radio *and* your T-Deck must be configured with the **same name and same PSK** on the same slot (`index 1`, the first secondary channel). If either value differs, packets remain unreadable between the radios and messages will not forward.

**Option A — Meshtastic app (easiest):**

1. Create a new private channel named `iBridge` on slot 1 of your gateway radio.
2. Change its PSK to `random` (generates a fresh key).
3. Use the app's "share channel" feature (QR code / channel URL) to load the identical channel onto your T-Deck.

**Option B — CLI:**

Substitute the device path you saw in Step 1 (e.g. `/dev/cu.usbserial-*`) anywhere you see `/dev/cu.usbmodem*` below.

1. Create the channel on slot 1 of the gateway radio:

```bash
meshtastic --port /dev/cu.usbmodem* --ch-index 1 --ch-set name iBridge
meshtastic --port /dev/cu.usbmodem* --ch-index 1 --ch-set psk random
```

2. Print the channel URL (it carries both the name and the key):

```bash
meshtastic --port /dev/cu.usbmodem* --ch-index 1 --ch-url
```

3. Connect the T-Deck and apply that URL:

```bash
meshtastic --port /dev/cu.usbserial* --ch-url "https://www.meshtastic.org/e/#...."
```

4. Verify slot 1 is enabled on both devices:

```bash
meshtastic --port /dev/cu.usbmodem* --info
```

This is a one-time, out-of-band step. The bridge does **not** create the channel itself; it simply connects to the slot set in `config.json` (`"channel_index": 1`).

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
    "device_path": "/dev/cu.usbserial*",
    "channel_index": 1,
    "channel_name": "iBridge",
    "my_node_id": "IGW0001"
  }
}
```

Set `device_path` to whichever path appeared in Step 1 (`/dev/cu.usbserial*` for CP210x/CH340/FTDI bridge chips, or `/dev/cu.usbmodem*` for USB-native boards). The `*` is a wildcard, so the trailing digits don't matter.

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
ls /dev/cu.* | grep -E 'usb(serial|modem)'
```

If nothing appears, check what USB chip macOS reports: `system_profiler SPUSBDataType | grep -i -A5 serial`. Some CH340-based boards also need a vendor driver installed.

### Messages not forwarding

First, confirm the channel name and PSK match on both devices:

```bash
meshtastic --port /dev/cu.usbmodem* --info    # gateway radio
meshtastic --port /dev/cu.usbserial* --info   # T-Deck
```

Both must show slot 1 **enabled** with an identical channel name and identical PSK. If in doubt, re-share the channel URL from the gateway radio to the T-Deck (see "Configure Channel Settings" above).

Then verify the gateway can communicate with the radio:

```bash
python3 -c "from meshtastic.client import MeshtasticClient; c = MeshtasticClient('/dev/cu.usbmodem*', connect=True); print(c.my_info); c.disconnect()"
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
