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

## Pointing a player at it

    http://<home-assistant>:8099/channels.m3u

is the channel list, and what an IPTV app wants. The list is generated per
request from the address it was asked for, so whatever you type is what the
channel plays from.

The receiver also answers to its own name over mDNS -- `relay.local`, or
whatever the `name` option is reduced to -- which is friendlier to type and
survives the address changing. Use it only for players on the same network
segment. mDNS is link-local: where it has to cross a VLAN through a reflector,
a lookup can take seconds, and a player that pays that on every connection
buffers continuously on a stream the plain address serves perfectly. For those,
either use the address or give the box a name in the router's own DNS.

## Tuning in before anything is playing

The channel always carries something. Between streams it publishes a still
card, on the same playlist and the same numbering the next stream carries on
from, so a television can be pointed at the channel and left there: when the
phone starts a stream the card gives way to it as an ordinary discontinuity,
with nothing to reconnect.

The card is rendered once at startup -- a logo on a dark background, five frames
a second, about 60 kB in all -- and looped, so it costs nothing to keep up.

## Choosing a rendition

Where the source offers the same programme at several bitrates, the best is
taken and kept for as long as the link carries it. If the window stops gaining
a second of media per second of real time for the best part of a minute, a step
down the ladder keeps the picture moving rather than letting every screen
stutter at once; five settled minutes earn a step back, and the rendition just
left is barred for ten, because oscillating costs more than staying a step low.

The status page names the rendition in use and how far down the ladder it is.

## Several languages

When the source offers its audio in more than one language, every track is
relayed and the player picks between them -- nothing on the status page
switches it, because the choice belongs in the same menu a viewer already uses
for subtitles. The add-on log names the tracks as it finds them:

    audio: English, 日本語, Deutsch, Español, International Feed

Only the video is pinned to one rendition: the highest the source offers, which
is what would have been taken anyway. The alternate audio costs about 160 kbps
each, so five languages are a rounding error against a 9 Mbps picture.

Whether the player shows those names or just "Track 1, Track 2" depends on
ffmpeg: it carries the languages as far as the transport stream, whose muxer
does not always write them all back out.

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
