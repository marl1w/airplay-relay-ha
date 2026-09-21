# The base is named here rather than in build.yaml on purpose. Supervisor has
# deprecated build.yaml and ignores it, substituting BUILD_FROM=
# ghcr.io/home-assistant/base:latest -- an Alpine image, on which every apt-get
# below fails with 127 in a way that reads like a broken package list.
#
# Debian trixie carries uxplay 1.71.1, the first release with the -hls option
# that receives a URL instead of a mirrored screen, alongside
# python3-zeroconf, so nothing here needs pip.
FROM debian:trixie-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The gstreamer plugin sets are not optional, however tempting it is to drop
# them: uxplay checks for the 'app', 'playback', 'autodetect' and
# 'videoparsersbad' elements while starting and exits if any is missing, even
# with rendering switched off by -vs 0 -as 0. Leaving them out halves the
# download to 85MB and produces an add-on that cannot start at all.
#
# --no-install-recommends and dropping docs, manpages and locales are what is
# actually safe to trim here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-zeroconf \
        uxplay \
        ffmpeg \
        avahi-daemon \
        avahi-utils \
        dbus \
        jq \
        ca-certificates \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
    && find /usr/share/doc /usr/share/man /usr/share/locale -type f -delete \
    && rm -f /usr/lib/*/libLLVM* /usr/lib/*/libgallium* /usr/lib/*/libz3* \
    && rm -rf /usr/lib/*/dri

# Mesa and LLVM above are 177MB of OpenGL driver, pulled in by gstreamer's GL
# plugin and never used: uxplay runs with -vs 0 and this add-on has no display
# to render into. Verified that uxplay still starts with them gone. They are
# deleted in the same layer as the install because a later RUN would leave the
# files in the layer beneath and shrink nothing.

WORKDIR /opt
COPY airplay_relay /opt/airplay_relay
COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

CMD [ "/run.sh" ]
