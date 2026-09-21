#!/bin/bash
# Start the receiver. Everything it needs to decide is either a add-on option or
# a value we have already established works; see config.py for the full set and
# the environment variables that override them when something needs debugging.
set -euo pipefail

# UxPlay registers its service through Avahi over D-Bus and exits with status 0
# if no responder answers -- which reads like a clean shutdown rather than an
# error. It is confined to loopback on purpose: the proxy in front of it is what
# gets advertised to the network, so only one receiver is ever visible and it is
# one whose record we withdraw properly on the way out.
mkdir -p /run/dbus
rm -f /run/dbus/pid

# Loopback, always. The phone talks to the proxy and nothing else, and uxplay
# sits behind it for the FairPlay step alone -- if it advertised itself the
# phone could reach it directly and the session would be half ours.
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
# --no-rlimits because the limits avahi wants are not grantable in a container,
# and it refuses to start rather than carrying on without them.
avahi-daemon --daemonize --no-drop-root --no-rlimits || true

# Avahi failing here is the worst kind of failure: uxplay cannot register, exits
# with status 0, and everything downstream reports an empty service list as
# though nothing had been asked for. On a quick restart it can also lose a race
# for the host's 5353, so give it a few goes and say plainly if it never comes up.
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
exec python3 -m airplay_relay
