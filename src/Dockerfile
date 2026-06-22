FROM ubuntu:24.04

ARG TARGETARCH
ARG HOST_UID=1000
ARG HOST_GID=1000
ARG MINICONDA_VERSION=py312_26.3.2-2
ARG NODE_VERSION=24
ARG S6_OVERLAY_VERSION=3.2.3.0

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    HOME=/home/dev \
    PATH=/home/dev/.local/bin:/home/dev/.npm-global/bin:$PATH \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_VERBOSITY=1

# 更改 shell 默认行为：遇到错误立即退出，处理管道错误
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- Root 权限操作层 ---
RUN set -eux \
    # APT 抗网络抖动配置
    && printf '%s\n' \
        'Acquire::Retries "8";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        'Acquire::http::Pipeline-Depth "0";' \
        'Acquire::http::No-Cache "true";' \
        'Acquire::https::No-Cache "true";' \
        'Acquire::ForceIPv4 "true";' \
       > /etc/apt/apt.conf.d/99network-resilience \
    # 通用重试函数（指数退避，最大 5 次）
    && retry() { \
         local n=1 max=5 delay=5; \
         until "$@"; do \
           if [ "$n" -ge "$max" ]; then \
             echo "Command failed after ${n} attempts: $*"; \
             return 1; \
           fi; \
           echo "Command failed, retry ${n}/${max} in ${delay}s: $*"; \
           sleep "$delay"; \
           n=$((n + 1)); \
           delay=$((delay * 2)); \
         done; \
       } \
    && retry apt-get update \
    && retry apt-get install -y --no-install-recommends \
        curl sudo wget git gh vim nano net-tools gnupg ca-certificates file tree \
        openssh-server iputils-ping locales tzdata software-properties-common tmux build-essential ripgrep jq iproute2 xz-utils \
        python3 python3-pip python3-venv python-is-python3 \
    # 系统级时区与语言配置
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    # 配置 SSH
    && mkdir -p /run/sshd \
    && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    # 禁止 SSH 接收客户端 locale，避免本地 LANG/LC_* 污染容器
    && sed -i '/^[[:space:]]*AcceptEnv[[:space:]]\+/d' /etc/ssh/sshd_config \
    && printf '\nSetEnv LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8\n' >> /etc/ssh/sshd_config \
    # 创建用户和组 (处理 Ubuntu 24.04 默认 ubuntu 用户占用 1000:1000 的情况)
    && existing_user="$(getent passwd "$HOST_UID" | cut -d: -f1 || true)" \
    && if [ -n "$existing_user" ] && [ "$existing_user" != "dev" ]; then userdel -r "$existing_user"; fi \
    && existing_group="$(getent group "$HOST_GID" | cut -d: -f1 || true)" \
    && if [ -n "$existing_group" ] && [ "$existing_group" != "dev" ]; then groupdel "$existing_group"; fi \
    && groupadd -g "$HOST_GID" dev \
    && useradd -m -s /bin/bash -u "$HOST_UID" -g "$HOST_GID" dev \
    && usermod -aG sudo dev \
    && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && echo "set -g mouse on" > /home/dev/.tmux.conf \
    && chown dev:dev /home/dev/.tmux.conf \
    # 安装 Node.js
    && retry curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" -o /tmp/nodesource_setup.sh \
    && retry bash /tmp/nodesource_setup.sh \
    && retry apt-get update \
    && retry apt-get install -y --no-install-recommends nodejs \
    && rm -f /tmp/nodesource_setup.sh \
    # 严格清理
    && apt-get autoremove -y && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /usr/share/doc/* /usr/share/man/*

# locale 已生成后，将最终镜像环境切换到 en_US.UTF-8
ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

# --- 安装 s6-overlay 作为容器 PID 1 ---
RUN set -eux \
    && case "${TARGETARCH:-}" in \
         amd64) S6_ARCH="x86_64" ;; \
         arm64) S6_ARCH="aarch64" ;; \
         "") \
           case "$(uname -m)" in \
             x86_64) S6_ARCH="x86_64" ;; \
             aarch64) S6_ARCH="aarch64" ;; \
             *) echo "Unsupported s6-overlay arch: $(uname -m)" && exit 1 ;; \
           esac ;; \
         *) echo "Unsupported Docker target arch: ${TARGETARCH}" && exit 1 ;; \
       esac \
    && S6_BASE_URL="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}" \
    && curl -fsSLo /tmp/s6-overlay-noarch.tar.xz "${S6_BASE_URL}/s6-overlay-noarch.tar.xz" \
    && curl -fsSLo /tmp/s6-overlay-${S6_ARCH}.tar.xz "${S6_BASE_URL}/s6-overlay-${S6_ARCH}.tar.xz" \
    && tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-${S6_ARCH}.tar.xz \
    && rm -f /tmp/s6-overlay-*.tar.xz
    
# --- 用户权限操作层 ---
USER dev
WORKDIR /home/dev

# 1. 安装 uv
RUN curl -LsSf "https://astral.sh/uv/install.sh" | sh \
    && rm -rf .cache/*

# 2. 安装 Miniconda
RUN ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then \
         MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-${MINICONDA_VERSION}-Linux-x86_64.sh"; \
       elif [ "$ARCH" = "aarch64" ]; then \
         MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-${MINICONDA_VERSION}-Linux-aarch64.sh"; \
       else echo "Unsupported arch: $ARCH" && exit 1; fi \
    && curl -sSLo /tmp/miniconda.sh "$MINICONDA_URL" \
    && bash /tmp/miniconda.sh -b -p ~/miniconda3 \
    && rm /tmp/miniconda.sh \
    && ~/miniconda3/bin/conda config --set auto_activate_base false \
    && ~/miniconda3/bin/conda init bash \
    && ~/miniconda3/bin/conda clean -afy

# 3. 安装 npm 全局工具
RUN mkdir -p ~/.npm-global \
    && npm config set prefix '~/.npm-global' \
    && echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.bashrc \
    && echo '[ -s "/home/dev/.bashrc.extra" ] && . "/home/dev/.bashrc.extra"' >> ~/.bashrc \
    && npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest \
    && npm cache clean --force
    
# --- 切回 root 配置 s6 初始化脚本 ---
USER root

COPY entrypoint.sh /etc/cont-init.d/10-dev-env-init
COPY start-sshd.sh /usr/local/bin/start-sshd.sh
COPY s6-services-guard.sh /usr/local/bin/s6-services-guard.sh
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /etc/cont-init.d/10-dev-env-init \
    /usr/local/bin/start-sshd.sh \
    /usr/local/bin/s6-services-guard.sh \
    /usr/local/bin/docker-entrypoint.sh

# 暴露端口
EXPOSE 22 9321

# s6-overlay 需要以 root 作为 PID 1 启动，SSH 登录后仍进入 dev 用户
USER root
WORKDIR /home/dev/workspace

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/usr/local/bin/start-sshd.sh"]
