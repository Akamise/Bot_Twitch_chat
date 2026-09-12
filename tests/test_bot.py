import tempfile
import time
import unittest
from pathlib import Path

from bot import MessageScheduler, BotConfigError, load_messages, parse_bool


class FakeConnection:
    def __init__(self):
        self.messages = []

    def send_message(self, message):
        self.messages.append(message)

    @property
    def disconnected(self):
        return False

    def connect(self):
        pass

    def close(self):
        pass


class BotHelpersTests(unittest.TestCase):
    def test_parse_bool_supports_russian_and_english_values(self):
        self.assertTrue(parse_bool("ДА"))
        self.assertTrue(parse_bool("yes"))
        self.assertFalse(parse_bool("НЕТ"))
        self.assertFalse(parse_bool("0"))

    def test_parse_bool_rejects_unknown_value(self):
        with self.assertRaises(BotConfigError):
            parse_bool("sometimes")

    def test_load_messages_ignores_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.txt"
            path.write_text("# comment\n\n first \nsecond\n", encoding="utf-8")
            self.assertEqual(load_messages(path), ["first", "second"])

    def test_scheduler_waits_then_repeats_messages_in_order(self):
        connection = FakeConnection()
        scheduler = MessageScheduler(connection, ["one", "two"], 0.03)
        scheduler.start()
        time.sleep(0.045)
        scheduler.stop()
        self.assertEqual(connection.messages, ["one", "two"])


if __name__ == "__main__":
    unittest.main()