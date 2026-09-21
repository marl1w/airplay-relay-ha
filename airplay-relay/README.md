# AirPlay Relay

AirPlay a video from your phone and every screen in the house can watch it,
without the phone staying in the middle.

Safari hands an AirPlay receiver the *URL* of what you are watching and expects
the receiver to fetch it. This app takes that URL, pulls the stream once, and
republishes it on the local network. The link out of the house carries one copy
however many televisions are on, and your phone is free to go elsewhere.

Nothing is re-encoded: `ffmpeg -c copy` repackages the incoming segments into a
short HLS window held in RAM under `/dev/shm`. A stream that drops is retried
until the sender stops it.

## Watching it

Point any player on each television at:

    http://<home-assistant>:8099/channel.m3u8

VLC and Infuse both do this from the tvOS App Store; save it once as a bookmark
and tuning in is two clicks.

## How it fits together

```
iPhone ──AirPlay──▶ proxy :7000 ──┬──▶ UxPlay (loopback) : pairing and FairPlay
                                  └──▶ /play, /playback-info : answered here
                                          │
                                   ffmpeg -c copy
                                          ▼
                                  :8099/channel.m3u8 ──▶ every screen
```

UxPlay is present for one reason: it satisfies `/fp-setup`, the step where a
sender requires the receiver to be Apple-licensed hardware. Everything that
describes playback is answered by this app instead, so the phone is told the
truth about a stream UxPlay is not involved in -- and because `/play` is never
forwarded, UxPlay never fetches anything, so the stream is pulled exactly once.

UxPlay's own mDNS is confined to loopback and its record is republished here,
pointing at the proxy. That keeps one receiver visible instead of two, and it
gets withdrawn properly on shutdown rather than lingering as a ghost that the
next run collides with.

## Configuration

| Option | What it does |
| --- | --- |
| `name` | What the receiver is called in the AirPlay picker. |
| `channel_port` | Where the republished stream is served. |
| `log_level` | `debug` logs the whole AirPlay exchange, headers included. |

Everything else has a value established by testing and is set in the code. The
few that are worth changing while diagnosing something -- the HLS window, the
address advertised on a multi-homed host, the user agent the stream is fetched
with -- can be overridden through environment variables, and are listed in
`config.py`.

## If it does not appear in the picker

Discovery is multicast and does not cross a VLAN by itself. If Home Assistant
and the phone are on different VLANs, the router has to reflect `_airplay._tcp`
between them, or the app runs perfectly and is never seen.

Reflectors also tend not to pass on the "goodbye" that withdraws a service, so a
receiver that has been restarted a few times can leave stale entries behind that
resolve to nothing. If you see more than one receiver, or one that fails the
moment it is picked, restart avahi on the router to flush them.
