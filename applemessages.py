"""
AppleScript-based iMessage Integration Layer

This module handles communication with Apple's Messages app using AppleScript (OSAX).
It provides functionality to:
- Monitor for incoming iMessages
- Send outgoing iMessages via AppleScript
- Track conversation state by chat GUID
"""

import os
import sqlite3
import json
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Dict, List, Any

import Quartz
import AppKit


# iMessage SQLite Database Location
iMESSAGE_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")


@dataclass
class ChatInfo:
    """Represents a single iMessage conversation."""
    chat_guid: str  # Unique identifier for this conversation thread
    title: str = ""  # Friendly name (contact name or group name)
    unread_count: int = 0
    last_message_time: float = 0
    participant_id: Optional[str] = None  # Apple's participant identifier


@dataclass
class Message:
    """Represents a single iMessage message."""
    guid: str
    chat_guid: str
    sender: str
    is_from_me: bool
    plaintext: str
    image_data_url: Optional[str] = None


class AppleScriptBridge:
    """
    Bridge to Apple's Messages app using OSAX/AppleScript.

    This provides a programmatic interface to send messages via iMessage
    while monitoring for incoming messages through the SQLite database.
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the AppleScript bridge.

        Args:
            config_path: Path to configuration JSON file
        """
        self.db_path = iMESSAGE_DB_PATH
        self.mapping_file: Optional[Path] = None
        self.new_messages_callback: Optional[Callable] = None
        self._connection: Optional[sqlite3.Connection] = None
        self._scroller_app_id = None

        # Cache for unread messages tracking
        self._last_unread_timestamp: float = 0

        if config_path and Path(config_path).exists():
            self.load_config(config_path)

    def load_config(self, config_path: str) -> None:
        """Load configuration from JSON file."""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            self.mapping_file = Path(config.get('mapping_file', 'node_mapping.json'))
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load mapping config: {e}")

    def _get_scroller_app(self) -> AppKit.NSRunningApplication:
        """Get the ChatScroller app (iMessage UI process)."""
        if self._scroller_app_id is None:
            # Find iMessage ChatScroller by bundle identifier
            for pid, app_bundle in Quartz.ns.Application.runningApplicationsDictionaryItems().items():
                bundle = app_bundle.valueForKey_("LSBUApplicationBundleId")
                if str(bundle) == "com.apple.ChatScroller":
                    self._scroller_app_id = pid
                    break
        if self._scroller_app_id:
            return Quartz.ns.Application.runningApplicationWithProcessID_(self._scroller_app_id)
        return AppKit.NSRunningApplication.applicationWithOptions_("NSLaunchList", {})

    def send_message(
        self,
        contact_name: str,
        message_text: str,
        is_group_chat: bool = False
    ) -> bool:
        """
        Send a message via AppleScript to an iMessage contact.

        Args:
            contact_name: Name of the contact (as shown in Messages)
            message_text: Text content of the message
            is_group_chat: Whether this is for a group conversation

        Returns:
            True if message was sent successfully, False otherwise
        """
        try:
            osascript_code = f"""
            tell application "Messages"
                -- Find the contact/service
                set foundService to false
                repeat with aService in services 1
                    try
                        if name of service aService equals "{contact_name}" then
                            set foundService to true
                            break
                        end if
                    error number -6781
                        -- Service not found, continue searching
                    end try
                end repeat

                if not foundService then
                    return false
                end if

                -- Send the message
                send message "{message_text}" via service aService of (finder 1)
            end tell
            """
            result = os.system("osascript -e '" + osascript_code.replace("'", "'\"'\"'") + "'")
            return result == 0
        except Exception as e:
            print(f"Error sending iMessage via AppleScript: {e}")
            return False

    def send_message_with_group(
        self,
        group_name: str,
        contact_in_group: str,
        message_text: str
    ) -> bool:
        """
        Send a message to a specific person within a group chat.

        Args:
            group_name: Name of the group conversation
            contact_in_group: Name of the recipient within the group
            message_text: Text content of the message

        Returns:
            True if message was sent successfully
        """
        try:
            osascript_code = f"""
            tell application "Messages"
                -- Find the group chat
                set foundGroup to false
                repeat with aChat in chats 1
                    try
                        if name of chat aChat equals "{group_name}" then
                            set foundGroup to true
                            break
                        end if
                    error number -6781
                        -- Chat not found, continue searching
                    end try
                end repeat

                if not foundGroup then
                    return false
                end if

                -- Find the service and send message
                set foundService to false
                repeat with aService in services 1
                    try
                        if name of service aService equals "{contact_in_group}" then
                            set foundService to true
                            break
                        end if
                    error number -6781
                    end try
                end repeat

                if not foundService then
                    return false
                end if

                send message "{message_text}" via service aService of chat aChat
            end tell
            """
            result = os.system("osascript -e '" + osascript_code.replace("'", "'\"'\"'") + "'")
            return result == 0
        except Exception as e:
            print(f"Error sending group iMessage via AppleScript: {e}")
            return False

    def get_incoming_messages(self) -> List[Message]:
        """
        Scan the database for new incoming messages.

        Returns:
            List of Message objects with new/unread messages
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            # Get last checked timestamp (or use creation time if first run)
            self._last_unread_timestamp = self._get_last_check_time(conn)

            # Get all chats that have messages since last check
            query = """
            SELECT DISTINCT c.guid as chat_guid,
                           c.title,
                           m.msgguid AS message_guid,
                           m.sender_name,
                           (SELECT COUNT(*) FROM messages WHERE
                            msg_id > (SELECT MAX(msg_id) FROM messages
                                     WHERE guid = m.msgguid AND is_from_me = 0)) as unread_count
            FROM messages m
            JOIN chats c ON m.guid = c.guid
            WHERE m.is_from_me = 0
              AND m.timestamp > ?
            ORDER BY m.timestamp DESC
            """

            cursor.execute(query, (self._last_unread_timestamp,))
            rows = cursor.fetchall()

            results = []
            for row in rows:
                chat_guid = row['chat_guid']

                # Get the full message text
                msg_query = "SELECT plaintext FROM messages WHERE guid = ? AND timestamp > ?"
                cursor.execute(msg_query, (row['message_guid'], self._last_unread_timestamp))
                msg_row = cursor.fetchone()

                if msg_row:
                    results.append(Message(
                        guid=row['message_guid'],
                        chat_guid=chat_guid,
                        sender=row['sender_name'] or '',
                        is_from_me=False,
                        plaintext=msg_row['plaintext']
                    ))

            conn.close()
            self._update_last_check_time()
            return results

        except sqlite3.Error as e:
            print(f"Database error: {e}")
            return []

    def _get_db_connection(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._connection is None or not self._connection:
            self._connection = sqlite3.connect(self.db_path)
        return self._connection

    def _update_last_check_time(self) -> None:
        """Update the last check timestamp in the database."""
        conn = self._get_db_connection()
        try:
            cursor = conn.cursor()
            # Create table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS _last_check_times (
                    guid TEXT PRIMARY KEY,
                    timestamp REAL
                )
            """)
            current_time = time.time()
            cursor.execute(
                "INSERT OR REPLACE INTO _last_check_times (guid, timestamp) VALUES (?, ?)",
                ("*", current_time)  # Use "*" as wildcard key
            )
            conn.commit()
        except sqlite3.Error as e:
            print(f"Error updating last check time: {e}")
        finally:
            conn.close()

    def _get_last_check_time(self, conn: sqlite3.Connection) -> float:
        """Get the last check timestamp from the database."""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM _last_check_times WHERE guid = ? ORDER BY timestamp DESC LIMIT 1", ("*",))
            row = cursor.fetchone()
            return row[0] if row else time.time() - 3600  # Default to 1 hour ago
        except sqlite3.Error:
            return time.time() - 3600

    def register_new_message_callback(self, callback: Callable[[Message], None]) -> None:
        """
        Register a callback for new incoming messages.

        Args:
            callback: Function to call with Message objects when new messages arrive
        """
        self.new_messages_callback = callback
        # Check immediately if there are pending messages
        messages = self.get_incoming_messages()
        for msg in messages:
            if self.new_messages_callback:
                self.new_messages_callback(msg)

    def start_monitoring(self, interval: float = 5.0) -> None:
        """
        Continuously monitor for new incoming messages.

        Args:
            interval: Seconds between database scans
        """
        print("Starting iMessage monitoring...")
        while True:
            try:
                messages = self.get_incoming_messages()
                for msg in messages:
                    if self.new_messages_callback:
                        self.new_messages_callback(msg)
            except Exception as e:
                print(f"Error during monitoring: {e}")
            time.sleep(interval)


# Convenience function for running the bridge
def run_iMessage_monitor(
    callback: Callable[[Message, str], None],
    config_path: Optional[str] = None,
    interval: float = 5.0
) -> AppleScriptBridge:
    """
    Run the iMessage monitoring loop.

    Args:
        callback: Function(message, virtual_node_id) to call on new messages
        config_path: Optional path to configuration file
        interval: Scan interval in seconds

    Returns:
        The bridge instance for further configuration
    """
    bridge = AppleScriptBridge(config_path)
    bridge.register_new_message_callback(lambda msg: callback(msg, _get_virtual_node_for_chat(bridge, msg.chat_guid)))
    return bridge.start_monitoring(interval)


def _get_virtual_node_for_chat(bridge: AppleScriptBridge, chat_guid: str) -> str:
    """Get or create virtual node ID for a chat."""
    if not bridge.mapping_file.exists():
        return f"!!{hash(chat_guid) & 0xFFFFFFFF:08X}"

    with open(bridge.mapping_file, 'r') as f:
        mappings = json.load(f)

    if chat_guid in mappings:
        return mappings[chat_guid]['virtual_node_id']

    # Generate new virtual node ID
    new_id = f"!!{int.from_bytes(bytes.fromhex('FF000001'), 'big'):08X}"
    mappings[chat_guid] = {
        'chat_guid': chat_guid,
        'virtual_node_id': new_id,
        'title': mappings.get(chat_guid, {}).get('title', '') if chat_guid in mappings else ''
    }

    with open(bridge.mapping_file, 'w') as f:
        json.dump(mappings, f, indent=2)

    return new_id
