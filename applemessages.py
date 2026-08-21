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
import logging
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional, Dict, List, Any


log = logging.getLogger(__name__)


# iMessage SQLite Database Location
iMESSAGE_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# chat.db stores timestamps as Cocoa epoch nanoseconds (seconds since
# 2001-01-01 UTC, scaled by 1e9).  Unix epoch starts 2001-01-01 + 978307200 s.
_COCOA_EPOCH_OFFSET = 978307200


def unix_to_cocoa_ns(unix_seconds: float) -> float:
    """Convert a Unix timestamp (seconds) to Cocoa nanoseconds."""
    return (unix_seconds - _COCOA_EPOCH_OFFSET) * 1e9


def cocoa_ns_to_unix(cocoa_ns: float) -> float:
    """Convert Cocoa nanoseconds to a Unix timestamp (seconds)."""
    return cocoa_ns / 1e9 + _COCOA_EPOCH_OFFSET


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
            log.warning("Could not load mapping config: %s", e)

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
            log.exception("Error sending iMessage via AppleScript: %s", e)
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
            log.exception("Error sending group iMessage via AppleScript: %s", e)
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

            # Get last checked timestamp (Cocoa nanoseconds); default to
            # 1 hour ago on first run.
            last_check_ns = self._get_last_check_time(conn)

            # chat.db uses SINGULAR table names (message, chat) and joins via
            # chat_message_join.  Columns: message.text (body), message.date
            # (Cocoa ns), handle.id (phone/email), chat.display_name.
            query = """
            SELECT m.ROWID AS msg_rowid,
                   m.guid AS message_guid,
                   c.guid AS chat_guid,
                   c.display_name AS title,
                   h.id AS sender_handle,
                   m.text AS body,
                   m.date AS msg_date
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.is_from_me = 0
              AND m.text IS NOT NULL
              AND m.date > ?
            ORDER BY m.date DESC
            """

            cursor.execute(query, (last_check_ns,))
            rows = cursor.fetchall()

            results: List[Message] = []
            seen_guids: set = set()
            max_date_seen = last_check_ns

            for row in rows:
                msg_guid = row["message_guid"]
                if msg_guid in seen_guids:
                    continue
                seen_guids.add(msg_guid)

                body = row["body"] or ""
                results.append(Message(
                    guid=msg_guid,
                    chat_guid=row["chat_guid"],
                    sender=row["sender_handle"] or "",
                    is_from_me=False,
                    plaintext=body,
                ))

                if row["msg_date"] > max_date_seen:
                    max_date_seen = row["msg_date"]

            # Advance the watermark so the next poll only sees newer msgs.
            if max_date_seen > last_check_ns:
                self._update_last_check_time(max_date_seen)

            log.debug(
                "get_incoming_messages: last_check_ns=%s found=%d",
                last_check_ns, len(results),
            )
            return results

        except sqlite3.Error as e:
            log.warning("Database error reading incoming messages: %s", e)
            return []

    def _get_db_connection(self) -> sqlite3.Connection:
        """Get or create database connection (row factory for dict access)."""
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path)
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def _update_last_check_time(self, cocoa_ns: float) -> None:
        """Persist the last-seen message timestamp (Cocoa nanoseconds)."""
        # Use a short-lived connection so we don't disturb the cached poll conn.
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS _last_check_times (
                    guid TEXT PRIMARY KEY,
                    timestamp REAL
                )
            """)
            cursor.execute(
                "INSERT OR REPLACE INTO _last_check_times (guid, timestamp) VALUES (?, ?)",
                ("*", float(cocoa_ns)),
            )
            conn.commit()
        except sqlite3.Error as e:
            log.warning("Error updating last check time: %s", e)
        finally:
            conn.close()

    def _get_last_check_time(self, conn: sqlite3.Connection) -> float:
        """Get the last check timestamp (Cocoa nanoseconds).

        Defaults to 1 hour ago on first run.
        """
        default_ns = unix_to_cocoa_ns(time.time() - 3600)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM _last_check_times WHERE guid = ? LIMIT 1",
                ("*",),
            )
            row = cursor.fetchone()
            return float(row[0]) if row else default_ns
        except sqlite3.Error:
            return default_ns

    def register_new_message_callback(self, callback: Callable[[Message], None]) -> None:
        """
        Register a callback for new incoming messages.

        Args:
            callback: Function to call with Message objects when new messages arrive
        """
        self.new_messages_callback = callback
        # Check immediately if there are pending messages
        messages = self.get_incoming_messages()
        log.info("register_new_message_callback: delivering %d pending messages", len(messages))
        for msg in messages:
            if self.new_messages_callback:
                self.new_messages_callback(msg)

    def start_monitoring(self, interval: float = 5.0) -> None:
        """
        Continuously monitor for new incoming messages.

        Args:
            interval: Seconds between database scans
        """
        log.info("Starting iMessage monitoring (interval=%.1fs)", interval)
        while True:
            try:
                messages = self.get_incoming_messages()
                if messages:
                    log.info("monitoring: %d new message(s)", len(messages))
                for msg in messages:
                    if self.new_messages_callback:
                        self.new_messages_callback(msg)
            except Exception as e:
                log.exception("Error during monitoring: %s", e)
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
