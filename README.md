# AirPlay Relay

Pull one video stream from the internet once, and let every Apple TV and phone in
the house watch it from the local network.

You choose something on your phone and tap AirPlay. A receiver running on the
Home Assistant box takes the URL, fetches the stream, and republishes it as a
live HLS channel held in RAM. One internet pull regardless of how many people
watch, the phone free to walk away, and no transcoding — `ffmpeg -c copy` only.

The receiver lives in [`airplay-relay/`](airplay-relay/), which is both the
Home Assistant app and its build context. Its own
[README](airplay-relay/README.md) covers how the pieces fit together and what
the options do.

## Installing it as a Home Assistant app

In Home Assistant: **Settings → Apps → App Store → ⋮ → Repositories**, add
`https://github.com/marl1w/airplay-relay-ha`, then install **AirPlay Relay** and
start it.

**Discovery has to reach the phone.** mDNS is multicast and does not cross a
VLAN on its own, so if Home Assistant and the phone are on different VLANs the
router must reflect `_airplay._tcp` between them. Without that the app runs
perfectly and never appears in the picker, which is indistinguishable from the
protocol failing.

## Running it from a laptop

Only Linux: the receiver needs UxPlay on the same host, and avahi to publish
through.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd airplay-relay && sudo ../.venv/bin/python -m airplay_relay
```

Port 7000 is privileged, hence `sudo`. `--debug` logs the whole AirPlay
exchange, and `--url https://example.com/live.m3u8` publishes a stream straight
away without waiting for a sender, which is the quickest way to test the
channel and the players on their own.

## Tests

```bash
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest
```

They cover what Supervisor demands of `config.yaml` — a version that always
moves forward, a schema every option validates against — because those failures
surface as the app vanishing from the store rather than as an error.

## Layout

    repository.yaml          makes this an app repository
    airplay-relay/           the app, and Supervisor's build context
      config.yaml            options, and the version Supervisor offers
      run.sh                 brings up dbus and avahi, then starts the receiver
      airplay_relay/         the receiver
    tests/                   what Supervisor requires of the app manifest

The package lives inside the app directory because that directory is the build
context, and a Dockerfile cannot copy from above its own context.

## Licence

MIT — see [LICENSE](LICENSE).
