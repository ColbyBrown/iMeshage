# Debugging Guide

This document covers common issues and how to troubleshoot them.

## Common Issues

### "No device found" - Serial port not detected

**Symptoms:**
```
Error: No Meshtastic USB device found at /dev/cu.usbmodem*
```

**Solutions:**

1. **Check if device is visible:**
   ```bash
   ls -l /dev/cu.* | grep modem
   # or
   system_profiler SPUSBDataType | grep "Serial"
   ```

2. **Set correct permissions:**
   ```bash
   sudo chmod 666 /dev/cu.usbmodem*
   ```

3. **Check if device is mounted:**
   ```bash
   dmesg | tail -50 | grep USB
   ```

4. **If using LilyGo T-Deck with CP210x chip:**
   Sometimes these require manual mounting. Try:
   ```bash
   sudo chmod 666 /dev/cu.usbserial*
   ```

### "Could not connect" - Serial connection issues

**Symptoms:**
```
SerialException: Failed to open port
```

**Solutions:**

1. **Check baud rate:** Your LoRa radio may need specific baud rate:
   ```python
   # In your serial config, set:
   baud_rate = 9600  # Common for Meshtastic
   ```

2. **Disable auto-baud if configured:**
   Edit your device's USB properties to disable auto-baud detection.

### iMessage not forwarding

**Symptoms:**
Messages detected in database but not appearing on T-Deck.

**Debug steps:**

1. **Check database for new messages:**
   ```bash
   sqlite3 ~/Library/Messages/chat.db "SELECT sender_name, plaintext FROM messages ORDER BY timestamp DESC LIMIT 5;"
   ```

2. **Verify AppleScript is working:**
   ```bash
   osascript -e 'tell application "Messages" to return true'
   ```

3. **Check mapping file is writable:**
   ```bash
   ls -l ~/iMeshage/node_mapping.json
   ```

### Bridge connects but no mesh messages appear

**Symptoms:**
The gateway reports "Subscribed to text message channel" but nothing shows
up on the T-Deck.

**Debug steps:**

1. **Confirm the channel name and PSK match on both radios:**
   ```bash
   meshtastic --port /dev/cu.usbmodem* --info    # gateway radio
   meshtastic --port /dev/cu.usbserial* --info   # T-Deck
   ```
   Slot 1 must be **enabled** on both, with an identical channel name and
   identical PSK. Any mismatch makes packets unreadable between devices.

2. **Re-share the channel if unsure:**
   ```bash
   # Print the URL on the gateway radio
   meshtastic --port /dev/cu.usbmodem* --ch-index 1 --ch-url
   # Apply it on the T-Deck
   meshtastic --port /dev/cu.usbserial* --ch-url "https://www.meshtastic.org/e/#...."
   ```

### ACK packets not appearing on T-Deck

**Symptoms:**
Message sent but no checkmark in Meshtastic UI.

**Solutions:**

1. **Increase ACK handling frequency:**
   Adjust the monitoring interval in your code to check for pending ACKs more frequently.

2. **Verify virtual node ID tracking:**
   Make sure the NodeInfo packet was received by T-Deck before sending the message.

### Message truncation issues

**Symptoms:**
Long messages are cut off or corrupted.

**Solutions:**

1. **Check the channel and modem configuration:**
   ```bash
   meshtastic --port /dev/cu.usbmodem* --info
   ```
   Confirm slot 1 is enabled with the expected channel name and note the
   modem preset. The ~237-byte LoRa payload limit means very long messages
   can be truncated at the radio level regardless of software timing.

2. **Implement chunking in your gateway script:**
   Break long messages into smaller chunks with indices.

## Debug Mode

Enable verbose logging:

```bash
python meshtastic_bridge.py -c config.json --verbose
```

### Enable SQLite debugging

View database queries:
```bash
export SQL_DEBUG=1
# Or modify code to add print statements before/after queries
```

### Check AppleScript output

Run commands directly:
```bash
osascript <<EOF
tell application "Messages"
    set chats to chats 1
    repeat with aChat in chats 1
        display dialog name of chat aChat
    end repeat
end tell
EOF
```

## Advanced Debugging

### Packet inspection

Monitor raw packets on serial port:
```bash
sudo tail -f /var/log/messages | grep Meshtastic
```

Or use tcpdump if your radio supports packet logging.

### Memory profiling

Check for NodeDB overflow:
```python
from meshtastic.client import MeshtasticClient
client = MeshtasticClient('/dev/cu.usbmodem*', connect=True)
print(f"Discovered nodes: {len(client.node_db.nodes)}")
for node_id, info in client.node_db.nodes.items():
    print(f"  {node_id}: {info}")
```

## Log File Locations

- Main logs: `imessage_bridge.log` (in project directory)
- Database: `~/Library/Messages/chat.db`
- Virtual node mappings: `[project_dir]/node_mapping.json`

## Performance Tuning

### For high message volume

Increase scan frequency (but watch CPU):
```python
# In applemessages.py, adjust interval parameter:
bridge = AppleScriptBridge(interval=1.0)  # Check every second
```

### For battery-conscious devices

Reduce scan frequency and batch ACK checks.

## Getting Help

If you encounter an issue not covered here:

1. Check the Meshtastic Discord for firmware-specific tips
2. Review the BlueBubbles bridge code for alternative approaches (if needed)
3. Consider using Hermes-agent's existing implementations as reference

See: https://github.com/NousResearch/hermes-agent
