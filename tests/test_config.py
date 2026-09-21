"""Check config.yaml against the rules Supervisor enforces.

A malformed config.yaml is not skipped politely: Supervisor drops the whole app
and reports the installed copy as removed from its repository, which looks like
the repository is broken rather than one field being wrong. That cost an evening
once, so the rules it is easy to get wrong are asserted here.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml

CONFIG = Path(__file__).resolve().parent.parent / "airplay-relay" / "config.yaml"

# Supervisor's own expression for webui, which rejects a literal port number.
WEBUI = re.compile(r"^(?:https?|\[PROTO:\w+\]):\/\/\[HOST\]:\[PORT:\d+\].*$")


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def test_webui_matches_supervisors_expression(config: dict) -> None:
    if "webui" in config:
        assert WEBUI.match(config["webui"]), config["webui"]


def test_webui_port_is_declared(config: dict) -> None:
    """[PORT:n] is substituted from `ports`, and resolves to nothing without it."""
    if "webui" not in config:
        return
    port = re.search(r"\[PORT:(\d+)\]", config["webui"])
    assert port, config["webui"]
    assert f"{port.group(1)}/tcp" in (config.get("ports") or {})


def test_ingress_port_is_fixed(config: dict) -> None:
    """An app on the host's network cannot be given a port dynamically."""
    if config.get("ingress"):
        assert config.get("ingress_port"), "ingress needs a fixed ingress_port"
        if config.get("host_network"):
            assert config["ingress_port"] != 0


def test_every_option_has_a_schema_entry(config: dict) -> None:
    assert set(config["options"]) <= set(config["schema"])


def test_version_is_three_numbers(config: dict) -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(config["version"])), config["version"]


def test_version_matches_the_package(config: dict) -> None:
    init = (CONFIG.parent / "airplay_relay" / "__init__.py").read_text()
    assert f'__version__ = "{config["version"]}"' in init


def test_slug_never_changes(config: dict) -> None:
    """Changing it orphans the installed app and loses its data."""
    assert config["slug"] == "airplay_relay"


def test_version_is_newer_than_the_last_release(config: dict) -> None:
    """A fix shipped under the version already installed is a fix nobody gets.

    Supervisor decides whether an update exists by comparing version strings, so
    changing code without changing the version leaves the old build running and
    the log showing behaviour the source no longer has.
    """
    import subprocess

    previous = subprocess.run(
        ["git", "show", "HEAD:airplay-relay/config.yaml"],
        capture_output=True,
        text=True,
        cwd=CONFIG.parent.parent,
        check=False,
    )
    if previous.returncode != 0:
        pytest.skip("no previous revision to compare against")

    was = yaml.safe_load(previous.stdout)["version"]
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "airplay-relay"],
        capture_output=True,
        text=True,
        cwd=CONFIG.parent.parent,
        check=False,
    ).stdout.strip()

    if changed and changed != "airplay-relay/config.yaml":
        assert config["version"] != was, (
            f"the app changed but the version is still {was}; Supervisor will not offer it"
        )


def test_options_added_later_are_optional(config: dict) -> None:
    """A required key an installed app has never seen makes every save fail.

    Supervisor validates the options already stored against the new schema, so
    a key introduced after installation must be optional or the configuration
    page refuses to save anything at all.
    """
    for name, rule in config["schema"].items():
        if name not in config["options"]:
            continue
        if name in ("name", "channel_port", "log_level"):
            continue  # present since the first release
        assert str(rule).endswith("?"), f"{name} was added later and must be optional"
