"""Receiver settings, from the environment, then the add-on options, then defaults."""

from __future__ import annotations

import json
import os
import socket

SERVICE_TYPE = "_airplay._tcp.local."

OPTIONS_PATH = "/data/options.json"


def addon_options() -> dict[str, str]:
    """Return the add-on's options as strings, or nothing when not an add-on."""
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: str(value) for key, value in raw.items() if value not in (None, "")}


def default_address() -> str:
    """Return the local address that reaches the default route."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # A UDP connect sends nothing; it only selects the route's source address.
        sock.connect(("192.0.2.1", 9))
        return str(sock.getsockname()[0])
    finally:
        sock.close()


class Config:
    """Receiver settings, with the defaults established by testing."""

    def __init__(self) -> None:
        """Read every setting from the environment, the add-on, or a default."""
        self._options = addon_options()

        self.name = self._setting("name", "Relay")
        self.port = int(self._setting("port", "7000"))
        self.address = self._setting("advertise_ip", "") or default_address()

        self.channel_port = int(self._setting("channel_port", "8099"))
        # In RAM, so continuous segment writes do not churn the persistent volume.
        self.channel_dir = self._setting("channel_dir", "/dev/shm/airplay-relay")
        # Some CDNs serve placeholder segments to players they do not recognise.
        self.user_agent = self._setting(
            "user_agent",
            "AppleCoreMedia/1.0.0.22L572 (Apple TV; U; CPU OS 18_0 like Mac OS X; en_us)",
        )
        self.hls_time = int(self._setting("hls_time", "2"))
        self.hls_list_size = int(self._setting("hls_list_size", "6"))

        self.avahi_hostname = self._setting("avahi_hostname", "relay")
        self.uxplay_hls = self._setting("hls", "true").lower() == "true"
        self.uxplay_debug = self._setting("uxplay_debug", "true").lower() == "true"
        self.uxplay_extra_args = self._setting("uxplay_extra_args", "")

        self.log_level = self._setting("log_level", "INFO").upper()

    def _setting(self, key: str, default: str) -> str:
        """Return one setting, preferring the environment over the add-on option."""
        return os.getenv(f"AIRPLAY_{key.upper()}") or self._options.get(key) or default
