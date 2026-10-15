#!/bin/bash
set -euo pipefail

mkdir -p /run/dbus
rm -f /run/dbus/pid

# Avahi on loopback only: the proxy, not uxplay, is the receiver the network sees.
cat > /etc/avahi/avahi-daemon.conf <<'CONF'
[server]
host-name=relay
allow-interfaces=lo
disallow-other-stacks=no
use-ipv4=yes
use-ipv6=no
ratelimit-interval-usec=1000000
ratelimit-burst=1000

[publish]
publish-addresses=yes
publish-hinfo=no
publish-workstation=no
CONF

dbus-daemon --system --fork
# --no-rlimits: a container cannot grant the limits avahi asks for.
avahi-daemon --daemonize --no-drop-root --no-rlimits || true

# uxplay exits with status 0 if avahi is down, so fail loudly here instead.
for attempt in 1 2 3 4 5; do
    if avahi-daemon --check 2>/dev/null; then
        echo "avahi is running (attempt ${attempt})"
        break
    fi
    echo "avahi is not up yet; retrying (attempt ${attempt})"
    sleep 2
    avahi-daemon --daemonize --no-drop-root --no-rlimits || true
done

if ! avahi-daemon --check 2>/dev/null; then
    echo "avahi never started -- uxplay cannot register and nothing will work" >&2
    exit 1
fi

cd /opt
# Ahead of the box's other add-ons when they compete for a core: the box is mostly idle, but many small
# bursty processes (camera decoders) often hold a core for the few milliseconds a segment needs. uxplay
# and ffmpeg inherit it. Needs SYS_NICE (config.yaml); without it the relay runs at normal priority.
if nice -n -5 true 2>/dev/null; then
    echo "running at raised CPU priority (nice -5)"
    exec nice -n -5 python3 -m airplay_relay
fi
echo "cannot raise CPU priority (no SYS_NICE); running at normal priority"
exec python3 -m airplay_relay
