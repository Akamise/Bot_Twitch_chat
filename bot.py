"""Twitch IRC message sender.

The bot deliberately does not parse chat commands or reply to users. It only
connects to IRC and sends the configured messages in a loop.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("twitch-chat-bot")


class BotConfigError(ValueError):
    """Raised when a required or invalid setting is found."""


def parse_bool(value: str, name: str = "boolean setting") -> bool:
    normalized = value.strip().upper()
    if normalized in {"YES", "Y", "TRUE", "1", "ДА"}:
        return True
    if normalized in {"NO", "N", "FALSE", "0", "НЕТ"}:
        return False
    raise BotConfigError(
        f"{name} must be YES/NO (or ДА/НЕТ), got {value!r}"
    )


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding real environment vars."""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise BotConfigError(f"Invalid .env line {line_number}: {raw_line!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise BotConfigError(f"Required environment variable is missing: {name}")
    return value


def _positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise BotConfigError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise BotConfigError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True)
class Settings:
    client_id: str
    oauth_token: str
    bot_username: str
    channel: str
    send_always: bool
    message_interval: float
    status_check_interval: float
    reconnect_delay: float
    messages_file: Path

    @classmethod
    def from_environment(cls, base_dir: Optional[Path] = None) -> "Settings":
        base_dir = base_dir or Path.cwd()
        token = _required_env("TWITCH_OAUTH_TOKEN")
        if token.lower().startswith("oauth:"):
            token = token[6:]
        channel = _required_env("TWITCH_CHANNEL").lstrip("#").lower()
        if not channel:
            raise BotConfigError("TWITCH_CHANNEL must not be empty")
        messages_file = Path(
            os.getenv("TWITCH_MESSAGES_FILE", "messages.txt").strip()
        )
        if not messages_file.is_absolute():
            messages_file = base_dir / messages_file
        return cls(
            client_id=_required_env("TWITCH_CLIENT_ID"),
            oauth_token=token,
            bot_username=_required_env("TWITCH_BOT_USERNAME").lower(),
            channel=channel,
            send_always=parse_bool(
                os.getenv("TWITCH_CHAT_SEND_ALWAYS", "NO"),
                "TWITCH_CHAT_SEND_ALWAYS",
            ),
            message_interval=_positive_float("TWITCH_MESSAGE_INTERVAL", 60),
            status_check_interval=_positive_float(
                "TWITCH_STATUS_CHECK_INTERVAL", 30
            ),
            reconnect_delay=_positive_float("TWITCH_RECONNECT_DELAY", 5),
            messages_file=messages_file,
        )


def load_messages(path: Path) -> list[str]:
    if not path.is_file():
        raise BotConfigError(f"Messages file does not exist: {path}")
    messages = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        message = line.strip()
        if message and not message.startswith("#"):
            if len(message) > 500:
                raise BotConfigError(
                    f"A message is longer than Twitch's 500-character limit: {message[:40]!r}"
                )
            messages.append(message)
    if not messages:
        raise BotConfigError(f"Messages file is empty: {path}")
    return messages


class MessageConnection(Protocol):
    def send_message(self, message: str) -> None:
        ...


class TwitchApiError(RuntimeError):
    """Raised when Twitch status cannot be read."""


class TwitchApi:
    def __init__(self, client_id: str, oauth_token: str, timeout: float = 15):
        self.client_id = client_id
        self.oauth_token = oauth_token
        self.timeout = timeout

    def is_stream_online(self, channel: str) -> bool:
        query = urlencode({"user_login": channel})
        request = Request(
            f"https://api.twitch.tv/helix/streams?{query}",
            headers={
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.oauth_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise TwitchApiError(f"Could not read Twitch stream status: {exc}") from exc
        return bool(payload.get("data"))


class TwitchIrcConnection:
    HOST = "irc.chat.twitch.tv"
    PORT = 6697

    def __init__(self, username: str, oauth_token: str, channel: str):
        self.username = username
        self.oauth_token = oauth_token
        self.channel = channel
        self._socket: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._disconnected = threading.Event()

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    def connect(self) -> None:
        if self._socket is not None and not self.disconnected:
            return
        self._closed.clear()
        self._disconnected.clear()
        context = ssl.create_default_context()
        raw_socket = socket.create_connection((self.HOST, self.PORT), timeout=15)
        self._socket = context.wrap_socket(raw_socket, server_hostname=self.HOST)
        self._socket.settimeout(1.0)
        self._send_raw(f"PASS oauth:{self.oauth_token}")
        self._send_raw(f"NICK {self.username}")
        self._send_raw(f"JOIN #{self.channel}")
        self._reader = threading.Thread(
            target=self._read_loop,
            name="twitch-irc-reader",
            daemon=True,
        )
        self._reader.start()
        LOGGER.info("Connected to Twitch IRC channel #%s", self.channel)

    def _send_raw(self, line: str) -> None:
        if self._socket is None:
            raise ConnectionError("IRC socket is not connected")
        with self._send_lock:
            self._socket.sendall((line + "\r\n").encode("utf-8"))

    def send_message(self, message: str) -> None:
        if self.disconnected:
            raise ConnectionError("IRC connection is closed")
        try:
            self._send_raw(f"PRIVMSG #{self.channel} :{message}")
        except OSError as exc:
            self._disconnected.set()
            raise ConnectionError("IRC message could not be sent") from exc
        LOGGER.info("Sent message: %s", message)

    def _read_loop(self) -> None:
        buffer = b""
        try:
            while not self._closed.is_set() and self._socket is not None:
                try:
                    data = self._socket.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise ConnectionError("Twitch IRC closed the connection")
                buffer += data
                while b"\r\n" in buffer:
                    raw_line, buffer = buffer.split(b"\r\n", 1)
                    line = raw_line.decode("utf-8", errors="replace")
                    if line.startswith("PING "):
                        self._send_raw(line.replace("PING", "PONG", 1))
                    elif "Login authentication failed" in line:
                        LOGGER.error("Twitch rejected IRC authentication")
        except (ConnectionError, OSError) as exc:
            if not self._closed.is_set():
                LOGGER.warning("Twitch IRC connection lost: %s", exc)
        finally:
            self._disconnected.set()

    def close(self) -> None:
        self._closed.set()
        self._disconnected.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self._reader = None


def create_irc_connection(settings: Settings) -> TwitchIrcConnection:
    return TwitchIrcConnection(
        settings.bot_username,
        settings.oauth_token,
        settings.channel,
    )


class MessageScheduler:
    def __init__(
        self,
        connection: MessageConnection,
        messages: list[str],
        interval: float,
    ):
        self.connection = connection
        self.messages = messages
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="twitch-message-scheduler",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval):
            for message in self.messages:
                if self._stop_event.is_set():
                    return
                try:
                    self.connection.send_message(message)
                except (ConnectionError, OSError) as exc:
                    LOGGER.warning("Message was not sent: %s", exc)
                    return

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(self.interval + 1, 2))
        self._thread = None


class TwitchBot:
    def __init__(
        self,
        settings: Settings,
        messages: list[str],
        api: Optional[TwitchApi] = None,
        connection_factory: Callable[[Settings], TwitchIrcConnection] = create_irc_connection,
    ):
        self.settings = settings
        self.messages = messages
        self.api = api or TwitchApi(settings.client_id, settings.oauth_token)
        self.connection_factory = connection_factory
        self._stop_event = threading.Event()
        self._connection: Optional[TwitchIrcConnection] = None
        self._scheduler: Optional[MessageScheduler] = None

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_sending()
        self._close_connection()

    def _close_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _stop_sending(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop()
            self._scheduler = None

    def _start_sending(self) -> None:
        if self._connection is None or self._scheduler is not None:
            return
        self._scheduler = MessageScheduler(
            self._connection,
            self.messages,
            self.settings.message_interval,
        )
        self._scheduler.start()

    def _connect(self) -> bool:
        try:
            self._connection = self.connection_factory(self.settings)
            self._connection.connect()
            return True
        except (ConnectionError, OSError) as exc:
            LOGGER.warning("Could not connect to Twitch IRC: %s", exc)
            self._close_connection()
            return False

    def run(self) -> None:
        LOGGER.info(
            "Bot started for #%s; send_always=%s",
            self.settings.channel,
            self.settings.send_always,
        )
        next_status_check = 0.0
        stream_online = False
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self.settings.send_always:
                    stream_online = True
                elif now >= next_status_check:
                    try:
                        stream_online = self.api.is_stream_online(self.settings.channel)
                        LOGGER.info("Stream status: %s", "online" if stream_online else "offline")
                    except TwitchApiError as exc:
                        LOGGER.warning("%s; keeping the last known stream status", exc)
                    next_status_check = now + self.settings.status_check_interval

                if stream_online:
                    if self._connection is None or self._connection.disconnected:
                        self._stop_sending()
                        self._close_connection()
                        if not self._connect():
                            self._stop_event.wait(self.settings.reconnect_delay)
                            continue
                    self._start_sending()
                else:
                    self._stop_sending()
                    self._close_connection()

                wait_time = 1.0
                if not self.settings.send_always:
                    wait_time = min(wait_time, max(0.1, next_status_check - time.monotonic()))
                self._stop_event.wait(wait_time)
        finally:
            self.stop()
            LOGGER.info("Bot stopped")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("TWITCH_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    try:
        load_dotenv(base_dir / ".env")
        configure_logging()
        settings = Settings.from_environment(base_dir)
        messages = load_messages(settings.messages_file)
        TwitchBot(settings, messages).run()
    except BotConfigError as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.info("Stopping...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())