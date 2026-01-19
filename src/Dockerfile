FROM ubuntu:22.04

ARG HOST_UID=1000 \
    HOST_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/dev \
    NVM_DIR=/home/dev/.nvm \
    PATH=/home/dev/miniconda3/bin:/home/dev/.local/bin:$PATH \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai

# 系统依赖、SSH 设置与 dev 用户配置
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl sudo wget git vim nano net-tools gnupg ca-certificates file tree \
        openssh-server iputils-ping locales tzdata \
        software-properties-common \
        build-essential; \
    locale-gen zh_CN.UTF-8; \
    update-locale LANG=zh_CN.UTF-8; \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime; \
    sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; \
    groupadd -g "$HOST_GID" dev 2>/dev/null || groupmod -g "$HOST_GID" dev; \
    useradd -m -s /bin/bash -u "$HOST_UID" -g "$HOST_GID" dev; \
    usermod -aG sudo dev; \
    echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers; \
    echo 'export LANG=zh_CN.UTF-8' >> /home/dev/.bashrc; \
    echo 'export LC_ALL=zh_CN.UTF-8' >> /home/dev/.bashrc; \
    echo 'export TZ=Asia/Shanghai' >> /home/dev/.bashrc; \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# uv
RUN set -eux; \
    su - dev -c "bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'"; \
    su - dev -c "rm -rf $HOME/.cache/*"

# Miniconda
RUN set -eux; \
    ARCH=$(uname -m); \
    if [ "$ARCH" = "x86_64" ]; then \
        MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-py310_23.5.2-0-Linux-x86_64.sh"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-py310_23.5.2-0-Linux-aarch64.sh"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi; \
    curl -sSLo /tmp/miniconda.sh "$MINICONDA_URL"; \
    su - dev -c "bash /tmp/miniconda.sh -b -p $HOME/miniconda3"; \
    rm /tmp/miniconda.sh; \
    su - dev -c "$HOME/miniconda3/bin/conda clean -afy"; \
    su - dev -c "$HOME/miniconda3/bin/conda init bash"; \
    printf '%s\n' \
        'export PATH="/home/dev/miniconda3/bin:/home/dev/.local/bin:$PATH"' \
        '[ -s "/home/dev/.bashrc.extra" ] && . "/home/dev/.bashrc.extra"' \
        >> /home/dev/.bashrc; \
    su - dev -c "rm -rf $HOME/.cache/*"

# NVM & Node
RUN set -eux; \
    su - dev -c "bash -lc 'curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash'"; \
    su - dev -c "bash -lc 'export NVM_DIR=/home/dev/.nvm && \
        [ -s \"$NVM_DIR/nvm.sh\" ] && . \"$NVM_DIR/nvm.sh\" && \
        nvm install 22 && \
        nvm alias default 22 && \
        nvm use default && \
        npm install -g @anthropic-ai/claude-code@latest @openai/codex@latest @google/gemini-cli@latest && \
        npm cache clean --force'"; \
    printf '%s\n' \
        'export NVM_DIR="/home/dev/.nvm"' \
        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"' \
        '[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"' \
        'if command -v nvm >/dev/null 2>&1; then nvm use default >/dev/null 2>&1; fi' \
        >> /home/dev/.bashrc; \
    su - dev -c "rm -rf $HOME/.cache/*"

# code-server
RUN set -eux; \
    su - dev -c "bash -lc 'curl -fsSL https://code-server.dev/install.sh | PREFIX=$HOME/.local sh -s -- --method=standalone'"; \
    su - dev -c "rm -rf $HOME/.cache/*"

# 入口脚本
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

# 默认用户与工作目录
USER dev
WORKDIR /home/dev/workspace

# 暴露 SSH 与 code-server 端口
EXPOSE 22 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
