# Pin the manifest list digest so rebuilds resolve the same base image.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    DISPLAY=:1 \
    HOME=/home/dev \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PYTHONPATH=/usr/local/lib/ai-dev-python

# APT resilience against network flakiness (same policy as the main image).
RUN set -eux; \
    printf '%s\n' \
        'Acquire::Retries "8";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        'Acquire::ForceIPv4 "true";' \
        > /etc/apt/apt.conf.d/99network-resilience

# Desktop stack: TigerVNC + fluxbox + noVNC + Chromium.
# Debian bookworm ships chromium as a native deb; Ubuntu 24.04 offers only
# the snap, which cannot run in a container.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        xvfb x11vnc fluxbox xterm \
        xdotool wmctrl x11-utils x11-xserver-utils \
        imagemagick novnc websockify \
        python3 procps bash ca-certificates curl jq \
        chromium \
        fonts-liberation fonts-dejavu-core fonts-noto-cjk fonts-noto-color-emoji \
        locales tzdata dbus-x11 \
        xdg-desktop-portal xdg-desktop-portal-gtk xdg-utils; \
    sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen; \
    locale-gen; \
    update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8; \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime; \
    echo "Asia/Shanghai" > /etc/timezone; \
    groupadd --gid 1000 dev; \
    useradd --uid 1000 --gid dev --create-home --shell /bin/bash dev; \
    mkdir -p /etc/ai-dev/fluxbox /home/dev/.ai-data/profile /home/dev/.ai-data/downloads; \
    chown -R dev:dev /home/dev; \
    apt-get autoremove -y; apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /usr/share/doc/* /usr/share/man/*

# Install the Unicode-capable VNC server separately so existing desktop layers
# remain reusable when upgrading the screen transport.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tigervnc-standalone-server xclip; \
    apt-get purge -y xvfb x11vnc; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# COPY --chmod creates missing intermediate directories with that mode (no
# execute bit), so create them first.
RUN mkdir -p /usr/local/lib/ai-dev-python/third_party /usr/share/doc/ai-dev-browser \
    && chmod 755 /usr/local/lib/ai-dev-python /usr/share/doc/ai-dev-browser

COPY --chmod=755 browser/start.sh        /usr/local/bin/browser-computer
COPY --chmod=755 browser/hub.py          /usr/local/bin/browser-hub
COPY --chmod=755 browser/control.py      /usr/local/bin/browser-control
COPY --chmod=644 browser/auth_file_plugin.py /usr/local/lib/ai-dev-python/ai_dev_auth.py
COPY --chmod=644 browser/ai_dev_logs.py  /usr/local/lib/ai-dev-python/ai_dev_logs.py
COPY --chmod=644 browser/ai_dev_session.py /usr/local/lib/ai-dev-python/ai_dev_session.py
COPY --chmod=644 browser/third_party/page_browser.py /usr/local/lib/ai-dev-python/third_party/page_browser.py
COPY --chmod=755 browser/browser-open   /usr/local/bin/browser-open
COPY --chmod=644 browser/fluxbox/init    /etc/ai-dev/fluxbox/init
COPY --chmod=644 browser/fluxbox/apps    /etc/ai-dev/fluxbox/apps
COPY --chmod=644 browser/fluxbox/menu    /etc/ai-dev/fluxbox/menu

# License copy for the Apache-2.0 component redistributed with the binary
# (see third_party/NOTICE).
COPY --chmod=644 LICENSE /usr/share/doc/ai-dev-browser/LICENSE
COPY --chmod=644 browser/third_party/LICENSE.rakazo browser/third_party/NOTICE \
     /usr/share/doc/ai-dev-browser/

# CRLF guard: a Windows checkout leaves a stray CR in the shebang and breaks execution.
RUN sed -i 's/\r$//' /usr/local/bin/browser-computer /usr/local/bin/browser-hub \
      /usr/local/bin/browser-control \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

EXPOSE 6080 7070 9223

# Probe the hub itself so the healthcheck does not wake the desktop stack.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7070/__hub/status >/dev/null || exit 1

USER 1000:1000
WORKDIR /home/dev

ENTRYPOINT ["/usr/local/bin/browser-computer"]
