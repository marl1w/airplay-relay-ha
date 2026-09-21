"""Settings for the receiver.

The same module runs from a terminal on a laptop and inside a Home Assistant
add-on, which differ only in where settings come from. Both are read here: an
environment variable wins, the add-on's options file is consulted next, and a
value established by testing is the default. That ordering means a single run
can be overridden without editing the add-on's configuration.
"""

from __future__ import annotations

import json
import os
import socket

# The service type an iOS video player looks for when it populates the AirPlay
# picker. Audio-only senders use _raop._tcp, which this receiver does not offer.
SERVICE_TYPE = "_airplay._tcp.local."

# Where Supervisor writes the add-on's configured options. Absent everywhere else.
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
    """Return the local address that reaches the default route.

    A UDP connect performs no traffic; it only asks the routing table which
    source address would be used. On a multi-homed host this picks the
    internet-facing interface, which is not always the one viewers are on, so
    the advertise_ip setting overrides it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
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

        # The channel the villas watch. Segments live in RAM: a few seconds of
        # 1080p is some tens of megabytes, and writing them to disk would put
        # continuous churn on the volume that also holds backups and the
        # recorder database.
        self.channel_port = int(self._setting("channel_port", "8099"))
        self.channel_dir = self._setting("channel_dir", "/dev/shm/airplay-relay")
        # Identify as Apple's player. A CDN that serves a placeholder image to
        # clients it does not recognise is indistinguishable from a broken
        # stream: the playlist parses, and every segment comes back as a WebP
        # that ffmpeg reports as "Could not find codec parameters".
        self.user_agent = self._setting(
            "user_agent",
            "AppleCoreMedia/1.0.0.22L572 (Apple TV; U; CPU OS 18_0 like Mac OS X; en_us)",
        )
        self.hls_time = int(self._setting("hls_time", "2"))
        self.hls_list_size = int(self._setting("hls_list_size", "6"))

        # The name UxPlay's record is republished under. Its own port is not
        # set here: UxPlay picks one and the mirror reads it back from avahi.
        self.avahi_hostname = self._setting("avahi_hostname", "relay")
        self.uxplay_hls = self._setting("hls", "true").lower() == "true"
        self.uxplay_debug = self._setting("uxplay_debug", "true").lower() == "true"
        self.uxplay_extra_args = self._setting("uxplay_extra_args", "")

        self.log_level = self._setting("log_level", "INFO").upper()

    def _setting(self, key: str, default: str) -> str:
        """Return one setting, preferring the environment over the add-on option."""
        return os.getenv(f"AIRPLAY_{key.upper()}") or self._options.get(key) or default
