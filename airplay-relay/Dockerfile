# Supervisor ignores build.yaml, so the base is set here. Trixie has uxplay with -hls.
FROM debian:trixie-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# uxplay exits at startup without these gstreamer plugin sets, even when headless.
# Unused OpenGL drivers are removed in the same layer, so the image actually shrinks.
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

WORKDIR /opt
COPY airplay_relay /opt/airplay_relay
COPY run.sh /run.sh
RUN chmod +x /run.sh

ENV PYTHONUNBUFFERED=1

CMD [ "/run.sh" ]
