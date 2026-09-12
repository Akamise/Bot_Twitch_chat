import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import (
    BotConfigError,
    MessageScheduler,
    Settings,
    TwitchBot,
    load_messages,
    parse_bool,
    update_dotenv_values,
)


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


class FakeApi:
    def __init__(self):
        self.oauth_token = "old-access"

    def refresh_access_token(self, client_secret, refresh_token):
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        return "new-access", "new-refresh"


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
        time.sleep(0.085)
        scheduler.stop()
        self.assertGreaterEqual(len(connection.messages), 2)
        self.assertEqual(connection.messages[:2], ["one", "two"])

    def test_update_dotenv_values_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# keep this comment\nTWITCH_OAUTH_TOKEN=old\nOTHER=value\n",
                encoding="utf-8",
            )
            update_dotenv_values(
                path,
                {
                    "TWITCH_OAUTH_TOKEN": "new-access",
                    "TWITCH_REFRESH_TOKEN": "new-refresh",
                },
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# keep this comment\nTWITCH_OAUTH_TOKEN=new-access\nOTHER=value\n"
                "TWITCH_REFRESH_TOKEN=new-refresh\n",
            )

    def test_refresh_updates_runtime_settings_and_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text(
                "TWITCH_OAUTH_TOKEN=old-access\nTWITCH_REFRESH_TOKEN=old-refresh\n",
                encoding="utf-8",
            )
            settings = Settings(
                client_id="client-id",
                client_secret="client-secret",
                oauth_token="old-access",
                refresh_token="old-refresh",
                bot_username="chatbot",
                channel="streamer",
                send_always=False,
                message_interval=60,
                status_check_interval=30,
                reconnect_delay=5,
                messages_file=Path(directory) / "messages.txt",
                dotenv_path=dotenv_path,
            )
            api = FakeApi()
            with patch.dict(os.environ, {}, clear=False):
                bot = TwitchBot(settings, ["message"], api=api)
                self.assertTrue(bot._refresh_tokens())
                self.assertEqual(bot.settings.oauth_token, "new-access")
                self.assertEqual(bot.settings.refresh_token, "new-refresh")
                self.assertEqual(api.oauth_token, "new-access")
                self.assertIn("TWITCH_OAUTH_TOKEN=new-access", dotenv_path.read_text())


if __name__ == "__main__":
    unittest.main()