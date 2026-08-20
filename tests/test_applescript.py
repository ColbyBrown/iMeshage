"""
Tests for AppleScript iMessage integration.
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from applemessages import AppleScriptBridge, Message


class TestAppleScriptBridge(unittest.TestCase):
    """Test cases for the AppleScript iMessage bridge."""

    def setUp(self):
        """Set up test fixtures."""
        self.bridge = AppleScriptBridge()

    @patch("os.system")
    def test_send_message_success(self, mock_system):
        """Test sending a simple message succeeds."""
        mock_system.return_value = 0

        result = self.bridge.send_message(
            contact_name="John Doe",
            message_text="Hello world!"
        )

        self.assertTrue(result)
        self.assertEqual(mock_system.call_count, 1)

    @patch("os.system")
    def test_send_message_failure(self, mock_system):
        """Test sending a message fails when script returns error."""
        mock_system.return_value = 1

        result = self.bridge.send_message(
            contact_name="Jane Smith",
            message_text="Hi there"
        )

        self.assertFalse(result)

    @patch("os.system")
    def test_send_message_with_quotes(self, mock_system):
        """Test sending a message containing quotes."""
        mock_system.return_value = 0

        message = 'He said "hello"'
        result = self.bridge.send_message(
            contact_name="John Doe",
            message_text=message
        )

        self.assertTrue(result)

    @patch("os.system")
    def test_send_group_message(self, mock_system):
        """Test sending a message to a specific person in a group chat."""
        mock_system.return_value = 0

        result = self.bridge.send_message_with_group(
            group_name="Family Group",
            contact_in_group="Mom",
            message_text="See you soon!"
        )

        self.assertTrue(result)

    @patch("os.system")
    def test_send_group_message_failure(self, mock_system):
        """Test sending to group member fails."""
        mock_system.return_value = 1

        result = self.bridge.send_message_with_group(
            group_name="Work Chat",
            contact_in_group="Boss",
            message_text="Meeting at 3"
        )

        self.assertFalse(result)


class TestMessageParsing(unittest.TestCase):
    """Test cases for message parsing and handling."""

    def test_parse_standard_message(self):
        """Test parsing a standard iMessage."""
        msg = Message(
            guid="msg-001",
            chat_guid="chat-john-doe",
            sender="John Doe",
            is_from_me=False,
            plaintext="Hello!"
        )

        self.assertEqual(msg.sender, "John Doe")
        self.assertFalse(msg.is_from_me)
        self.assertEqual(msg.plaintext, "Hello!")

    def test_parse_own_message(self):
        """Test parsing a message sent by me."""
        msg = Message(
            guid="msg-002",
            chat_guid="chat-john-doe",
            sender="John Doe",
            is_from_me=True,
            plaintext="Hi John!"
        )

        self.assertTrue(msg.is_from_me)

    def test_parse_empty_message(self):
        """Test parsing an empty message."""
        msg = Message(
            guid="msg-003",
            chat_guid="chat-test",
            sender="",
            is_from_me=False,
            plaintext=""
        )

        self.assertEqual(msg.sender, "")


if __name__ == "__main__":
    unittest.main()
