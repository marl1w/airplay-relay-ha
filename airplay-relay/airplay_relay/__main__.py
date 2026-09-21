"""Run the receiver, and republish whatever a sender hands it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal

from .channel import Channel
from .config import Config
from .mirror import Mirror, read_upstream
from .proxy import Message, Proxy, plist_response
from .uxplay import build_command, run as run_uxplay

_LOGGER = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="airplay_relay", description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="publish this stream and skip AirPlay entirely")
    parser.add_argument("--name", help="what to call the receiver in the picker")
    parser.add_argument("--debug", action="store_true", help="log the whole AirPlay exchange")
    return parser.parse_args()


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


async def run_channel(config: Config, url: str | None) -> None:
    """Republish whatever the sender hands over, or one URL given on the command line."""
    channel = Channel(
        directory=config.channel_dir,
        port=config.channel_port,
        hls_time=config.hls_time,
        hls_list_size=config.hls_list_size,
        user_agent=config.user_agent,
        name=config.name,
        hostname=config.avahi_hostname,
        address=config.address,
    )
    channel.serve()
    stop = _stop_event()

    if url:
        try:
            await channel.play(url)
            await stop.wait()
        finally:
            await channel.stop()
            channel.close()
        return

    # A replaced session must not control the stream that superseded it.
    current_session: dict[str, str | None] = {"id": None}

    APPS = {
        "com.apple.WebKit.GPU": "Safari",
        "com.apple.WebKit": "Safari",
        "com.apple.mobilesafari": "Safari",
        "com.google.ios.youtube": "YouTube",
        "com.apple.tv": "Apple TV",
        "com.apple.podcasts": "Podcasts",
        "com.apple.Music": "Music",
        "com.netflix.Netflix": "Netflix",
        "com.vimeo": "Vimeo",
        "tv.twitch": "Twitch",
    }

    MODELS = {
        "iPhone11,2": "iPhone XS",
        "iPhone11,4": "iPhone XS Max",
        "iPhone11,6": "iPhone XS Max",
        "iPhone11,8": "iPhone XR",
        "iPhone12,1": "iPhone 11",
        "iPhone12,3": "iPhone 11 Pro",
        "iPhone12,5": "iPhone 11 Pro Max",
        "iPhone12,8": "iPhone SE",
        "iPhone13,1": "iPhone 12 mini",
        "iPhone13,2": "iPhone 12",
        "iPhone13,3": "iPhone 12 Pro",
        "iPhone13,4": "iPhone 12 Pro Max",
        "iPhone14,2": "iPhone 13 Pro",
        "iPhone14,3": "iPhone 13 Pro Max",
        "iPhone14,4": "iPhone 13 mini",
        "iPhone14,5": "iPhone 13",
        "iPhone14,6": "iPhone SE",
        "iPhone14,7": "iPhone 14",
        "iPhone14,8": "iPhone 14 Plus",
        "iPhone15,2": "iPhone 14 Pro",
        "iPhone15,3": "iPhone 14 Pro Max",
        "iPhone15,4": "iPhone 15",
        "iPhone15,5": "iPhone 15 Plus",
        "iPhone16,1": "iPhone 15 Pro",
        "iPhone16,2": "iPhone 15 Pro Max",
        "iPhone17,1": "iPhone 16 Pro",
        "iPhone17,2": "iPhone 16 Pro Max",
        "iPhone17,3": "iPhone 16",
        "iPhone17,4": "iPhone 16 Plus",
        "iPhone17,5": "iPhone 16e",
    }

    def note_sender(message: Message) -> None:
        """Accumulate the sender's details from every request, since each carries only some."""
        details = dict(channel.sender)
        if called := message.headers.get("x-apple-client-name"):
            details["device"] = called
        payload = message.plist()
        if isinstance(payload, dict):
            if name := payload.get("name"):
                details["device"] = str(name)
            if model := payload.get("model"):
                details["hardware"] = str(model)
                details["model"] = MODELS.get(str(model), str(model))
            if (system := payload.get("osName")) and (version := payload.get("osVersion")):
                details["os"] = f"{system} {version}"
            if bundle := payload.get("clientBundleID") or payload.get("clientProcName"):
                details["app"] = APPS.get(str(bundle), str(bundle))
                details["bundle"] = str(bundle)
        changed = details != channel.sender
        channel.note_sender(details)
        if changed:
            _LOGGER.info("sender: %s", ", ".join(f"{k}={v}" for k, v in details.items()))

    def session_of(message: Message) -> str | None:
        return message.headers.get("x-apple-session-id")

    def owns_channel(message: Message) -> bool:
        session = session_of(message)
        if current_session["id"] is None or session is None:
            return True
        if session == current_session["id"]:
            return True
        _LOGGER.info("ignoring %s from a session that no longer owns the channel", message.path)
        return False

    async def respond(message: Message) -> bytes | None:
        """Answer the requests the proxy keeps for us."""
        if message.path == "/play":
            payload = message.plist()
            location = payload.get("Content-Location") if isinstance(payload, dict) else None
            if location:
                current_session["id"] = session_of(message)
                _LOGGER.warning("handed a stream: %s", location)
                await channel.play(str(location))
            return None

        # The sender is only a remote: a new /play replaces the stream, /stop never ends it.
        if message.path in ("/rate", "/stop"):
            _LOGGER.info(
                "%s from the sender, ignored -- the channel stands on its own", message.path
            )
            return None

        if message.path == "/playback-info":
            # Deliberately `requested`, not `playing`: the sender's play path depends on it.
            playing = channel.requested
            position = channel.position
            # duration 0 means live; without advancing ranges the sender assumes a stall.
            ranges = [{"start": 0.0, "duration": position}] if playing else []
            return plist_response(
                message,
                {
                    "uuid": "airplay-relay",
                    "duration": 0.0,
                    "position": position,
                    "rate": 1.0 if playing else 0.0,
                    "readyToPlay": playing,
                    "playbackBufferEmpty": not playing,
                    "playbackBufferFull": playing,
                    "playbackLikelyToKeepUp": playing,
                    "loadedTimeRanges": ranges,
                    "seekableTimeRanges": ranges,
                },
            )

        return None

    command = build_command(
        config,
        hls=config.uxplay_hls,
        debug=config.uxplay_debug,
        extra=config.uxplay_extra_args,
    )

    uxplay = asyncio.create_task(run_uxplay(command))
    waiter = asyncio.create_task(stop.wait())
    try:
        upstream_port, properties = await read_upstream()
        async with Mirror(config, properties, config.port):
            server = await Proxy(config.port, upstream_port, respond, note_sender).serve()
            async with server:
                await asyncio.wait({uxplay, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if uxplay.done() and not waiter.done():
            _LOGGER.error("uxplay exited with %s", uxplay.result())
    except TimeoutError as error:
        _LOGGER.error("%s", error)
    finally:
        for task in (uxplay, waiter):
            task.cancel()
        await channel.stop()
        channel.close()


async def main() -> None:
    """Read the settings and start the receiver."""
    args = _parse_args()
    if args.name:
        os.environ["AIRPLAY_NAME"] = args.name

    config = Config()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    await run_channel(config, args.url)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
