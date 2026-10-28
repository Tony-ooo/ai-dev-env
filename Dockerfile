# Pin the manifest list digest so rebuilds resolve the same base image.
FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c

ARG TARGETARCH
ARG NODE_VERSION=24
ARG CLAUDE_CODE_VERSION=2.1.312
ARG CODEX_VERSION=0.165.0
ARG S6_OVERLAY_VERSION=3.2.3.0
ARG HERDR_VERSION=0.9.5

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    HOME=/home/dev \
    PATH=/home/dev/.local/bin:/home/dev/.npm-global/bin:$PATH \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_RECEIVE_SIGNALS=1 \
    S6_VERBOSITY=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- Base system and user ---
RUN set -eux; \
    # APT resilience against network flakiness.
    printf '%s\n' \
        'Acquire::Retries "8";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        'Acquire::ForceIPv4 "true";' \
        > /etc/apt/apt.conf.d/99network-resilience; \
    retry() { \
        local n=1 max=5 d=5; \
        until "$@"; do \
            if [ "$n" -ge "$max" ]; then echo "failed after $n attempts: $*" >&2; return 1; fi; \
            sleep "$d"; n=$((n + 1)); d=$((d * 2)); \
        done; \
    }; \
    retry apt-get update; \
    retry apt-get install -y --no-install-recommends \
        ca-certificates curl wget git gh gnupg sudo \
        openssh-server tmux vim nano less \
        build-essential ripgrep jq tree file xz-utils \
        net-tools iproute2 iputils-ping \
        locales tzdata software-properties-common \
        python3 python3-venv python3-pip python-is-python3; \
    locale-gen en_US.UTF-8; \
    update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8; \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime; \
    echo "Asia/Shanghai" > /etc/timezone

# --- SSH: password login enabled, client locale pollution stripped ---
RUN set -eux; \
    mkdir -p /run/sshd; \
    # Host keys are generated at runtime into the data volume, never baked into the image.
    rm -f /etc/ssh/ssh_host_*; \
    sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; \
    sed -i '/^[[:space:]]*AcceptEnv[[:space:]]\+/d' /etc/ssh/sshd_config; \
    printf '\nSetEnv LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8\n' >> /etc/ssh/sshd_config; \
    # Ubuntu 24.04 ships ubuntu:1000; free the UID/GID before creating dev.
    deluser --remove-home ubuntu >/dev/null 2>&1 || true; \
    delgroup ubuntu >/dev/null 2>&1 || true; \
    groupadd -g 1000 dev; \
    useradd -m -s /bin/bash -u 1000 -g 1000 dev; \
    usermod -aG sudo dev; \
    printf 'dev ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/dev; \
    chmod 0440 /etc/sudoers.d/dev; \
    printf 'set -g mouse on\n' > /home/dev/.tmux.conf; \
    chown dev:dev /home/dev/.tmux.conf; \
    # Suppress the sudo lecture from /etc/bash.bashrc (dev is already in the sudo group).
    : > /home/dev/.sudo_as_admin_successful; \
    chown dev:dev /home/dev/.sudo_as_admin_successful

# --- Node.js ---
RUN set -eux; \
    curl -fsSL --retry 8 --retry-delay 5 --retry-all-errors \
        "https://deb.nodesource.com/setup_${NODE_VERSION}.x" -o /tmp/nodesource.sh; \
    bash /tmp/nodesource.sh; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -f /tmp/nodesource.sh; \
    apt-get autoremove -y; apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /usr/share/doc/* /usr/share/man/*

# --- s6-overlay: PID 1 for signal forwarding, reaping and service supervision ---
# Payloads come from vendor/ (see scripts/vendor.sh); a verified local blob is used
# offline, and only a missing or mismatched one falls back to the network.
RUN --mount=type=bind,source=vendor,target=/vendor,ro set -eux; \
    case "${TARGETARCH:-}" in \
        amd64)  S6_ARCH=x86_64  ;; \
        arm64)  S6_ARCH=aarch64 ;; \
        "") case "$(uname -m)" in \
                x86_64)  S6_ARCH=x86_64  ;; \
                aarch64) S6_ARCH=aarch64 ;; \
                *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
            esac ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    source /vendor/fetch.sh; \
    verified_payload "s6-overlay-noarch-v${S6_OVERLAY_VERSION}.tar.xz" /tmp/s6-noarch.tar.xz; \
    verified_payload "s6-overlay-${S6_ARCH}-v${S6_OVERLAY_VERSION}.tar.xz" /tmp/s6-arch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-arch.tar.xz; \
    rm -f /tmp/s6-*.tar.xz

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

# --- User toolchain ---
USER dev
WORKDIR /home/dev

ARG UV_VERSION=0.13.5
# Local uv payload replaces the network installer entirely when verified.
RUN --mount=type=bind,source=vendor,target=/vendor,ro set -eux; \
    source /vendor/fetch.sh; \
    case "$(uname -m)" in \
        x86_64) UV_ARCH=x86_64 ;; \
        aarch64|arm64) UV_ARCH=aarch64 ;; \
        *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    BLOB="uv-${UV_ARCH}-unknown-linux-gnu-${UV_VERSION}.tar.gz"; \
    verified_payload "$BLOB" /tmp/uv.tar.gz; \
    mkdir -p "$HOME/.local/bin"; \
    tar -xzf /tmp/uv.tar.gz -C /tmp; \
    install -m 0755 "/tmp/uv-${UV_ARCH}-unknown-linux-gnu/uv" "$HOME/.local/bin/uv"; \
    install -m 0755 "/tmp/uv-${UV_ARCH}-unknown-linux-gnu/uvx" "$HOME/.local/bin/uvx"; \
    rm -rf /tmp/uv.tar.gz /tmp/uv-${UV_ARCH}-unknown-linux-gnu; \
    uv --version; \
    rm -rf ~/.cache/*

# Native binaries are required here. Fetch and verify each manifest payload,
# install from those tarballs offline, and check the resulting versions.
RUN --mount=type=bind,source=vendor,target=/vendor,ro \
    --mount=type=cache,target=/home/dev/.npm,uid=1000,gid=1000 \
    set -eux; \
    NPM_ARCH=$(node -p 'process.arch'); \
    case "$NPM_ARCH" in x64|arm64) ;; *) echo "unsupported npm arch: $NPM_ARCH" >&2; exit 1 ;; esac; \
    mkdir -p ~/.npm-global/lib/node_modules; \
    npm config set prefix "$HOME/.npm-global"; \
    source /vendor/fetch.sh; \
    CLAUDE_NATIVE="claude-code-linux-${NPM_ARCH}-${CLAUDE_CODE_VERSION}.tgz"; \
    CODEX_NATIVE="codex-${CODEX_VERSION}-linux-${NPM_ARCH}.tgz"; \
    verified_payload "claude-code-${CLAUDE_CODE_VERSION}.tgz" /tmp/claude-wrapper.tgz; \
    verified_payload "$CLAUDE_NATIVE" /tmp/claude-native.tgz; \
    verified_payload "codex-${CODEX_VERSION}.tgz" /tmp/codex-wrapper.tgz; \
    verified_payload "$CODEX_NATIVE" /tmp/codex-native.tgz; \
    npm install -g --offline --ignore-scripts --no-audit --no-fund \
        /tmp/claude-wrapper.tgz /tmp/claude-native.tgz /tmp/codex-wrapper.tgz; \
    node ~/.npm-global/lib/node_modules/@anthropic-ai/claude-code/install.cjs; \
    CODEX_ALIAS="$HOME/.npm-global/lib/node_modules/@openai/codex-linux-${NPM_ARCH}"; \
    mkdir -p "$CODEX_ALIAS"; \
    tar -xzf /tmp/codex-native.tgz -C "$CODEX_ALIAS" --strip-components=1; \
    rm -f /tmp/claude-wrapper.tgz /tmp/claude-native.tgz /tmp/codex-wrapper.tgz /tmp/codex-native.tgz; \
    [ "$(claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)" ]; \
    [ "$(codex --version)" = "codex-cli ${CODEX_VERSION}" ]; \
    rm -rf /home/dev/.codex/tmp

# herdr: single static binary
RUN --mount=type=bind,source=vendor,target=/vendor,ro set -eux; \
    case "$(uname -m)" in \
        x86_64) HERDR_ARCH=x86_64 ;; \
        aarch64|arm64) HERDR_ARCH=aarch64 ;; \
        *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    mkdir -p ~/.local/bin; \
    BLOB="herdr-linux-${HERDR_ARCH}-v${HERDR_VERSION}"; \
    source /vendor/fetch.sh; \
    verified_payload "$BLOB" /tmp/herdr; \
    install -m 0755 /tmp/herdr ~/.local/bin/herdr; \
    rm -f /tmp/herdr; \
    herdr --version

# --- Shell startup ---
RUN cat >> ~/.bashrc <<'EOF'

export PATH="$HOME/.npm-global/bin:$PATH"
# ~/.bashrc lives in the image layer and is reset on rebuild; .bashrc.extra is the
# only shell config that survives in the data volume.
if [ -s "$HOME/.bashrc.extra" ]; then
    . "$HOME/.bashrc.extra"
fi
EOF

# --- Runtime scripts ---
USER root

COPY src/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY src/sysinit.sh    /etc/cont-init.d/10-sysinit
COPY src/s6-guard.sh   /usr/local/bin/s6-guard.sh
COPY src/root-exec.c   /tmp/root-exec.c

# root-exec: setuid in-place privilege escalation; sudo would fork and break PID 1.
RUN set -eux; \
    gcc -O2 -Wall -o /usr/local/bin/root-exec /tmp/root-exec.c; \
    rm -f /tmp/root-exec.c; \
    chown root:root /usr/local/bin/root-exec; \
    chmod 4755 /usr/local/bin/root-exec; \
    chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/s6-guard.sh /etc/cont-init.d/10-sysinit

COPY --chown=dev:dev src/skills/ /opt/ai-dev-env/skills/
COPY src/welcome.md /opt/ai-dev-env/welcome.md

# Browser computer client: CLI plus the vendored CDP page helper
# (page_browser.py from rakazo, Apache-2.0; see browser/third_party/NOTICE).
COPY --chmod=755 browser/ai-browser /usr/local/bin/ai-browser
COPY --chmod=755 browser/third_party/page_browser.py /usr/local/bin/page-browser
RUN mkdir -p /usr/share/doc/ai-dev-env && chmod 755 /usr/share/doc/ai-dev-env
COPY --chmod=644 browser/third_party/LICENSE.rakazo browser/third_party/NOTICE \
     /usr/share/doc/ai-dev-env/
COPY --chmod=644 LICENSE /usr/share/doc/ai-dev-env/LICENSE

# Record the resolved toolchain and OS package versions for each built image.
RUN set -eu; { \
        uname -m; \
        node --version; python3 --version; uv --version; \
        runuser -u dev -- claude --version; runuser -u dev -- codex --version; herdr --version; \
        printf 's6-overlay %s\n' "$S6_OVERLAY_VERSION"; \
        dpkg-query -W -f='${Package} ${Version}\n'; \
    } > /opt/ai-dev-env/build-versions.txt; \
    rm -rf /home/dev/.codex/tmp

# Snapshot of the s6 service skeleton; the entrypoint idempotently seeds the volume
# from it on every boot. Owned by dev so `cp -an` inherits ownership, avoiding a
# recursive chown at startup.
RUN cp -a /etc/s6-overlay/s6-rc.d /opt/ai-dev-env/s6-skel \
    && chown -R dev:dev /opt/ai-dev-env/s6-skel

EXPOSE 22

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD bash -c ': > /dev/tcp/127.0.0.1/${SSH_PORT:-22}'

# Start as dev so `docker exec` lands in dev; the entrypoint escalates in place
# via root-exec without breaking PID 1.
USER dev
WORKDIR /home/dev/workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
