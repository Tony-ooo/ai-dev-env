FROM ubuntu:22.04

ARG HOST_UID=1000
ARG HOST_GID=1000
ARG MINICONDA_VERSION=py310_23.5.2-0
ARG NODE_VERSION=22

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai \
    HOME=/home/dev \
    NVM_DIR=/home/dev/.nvm \
    PATH=/home/dev/miniconda3/bin:/home/dev/.local/bin:/home/dev/.nvm/versions/node/v${NODE_VERSION}/bin:$PATH

# 更改 shell 默认行为：遇到错误立即退出，处理管道错误
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- Root 权限操作层 ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl sudo wget git gh git-filter-repo vim nano net-tools gnupg ca-certificates file tree \
        openssh-server iputils-ping locales tzdata software-properties-common tmux build-essential \
    # 配置语言和时区
    && locale-gen zh_CN.UTF-8 \
    && update-locale LANG=zh_CN.UTF-8 \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    # 配置 SSH
    && mkdir -p /run/sshd \
    && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    # 创建用户和组 (处理 ID 冲突逻辑)
    && (groupadd -g "$HOST_GID" dev || groupmod -g "$HOST_GID" dev) \
    && useradd -m -s /bin/bash -u "$HOST_UID" -g "$HOST_GID" dev \
    && usermod -aG sudo dev \
    && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    # 清理缓存减少体积
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- 用户权限操作层 ---
USER dev
WORKDIR /home/dev

# 配置 dev 用户的 shell 环境
RUN echo "export LANG=zh_CN.UTF-8" >> .bashrc \
    && echo "export LC_ALL=zh_CN.UTF-8" >> .bashrc \
    && echo "export TZ=Asia/Shanghai" >> .bashrc \
    && echo "set -g mouse on" >> .tmux.conf

# 1. 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
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
    && ~/miniconda3/bin/conda clean -afy \
    && ~/miniconda3/bin/conda init bash

# 3. 安装 NVM & Node
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash \
    && . "$NVM_DIR/nvm.sh" \
    && nvm install ${NODE_VERSION} \
    && nvm alias default ${NODE_VERSION} \
    && nvm use default \
    && npm install -g @openai/codex@latest @google/gemini-cli@latest pm2@latest \
    && npm cache clean --force \
    && echo 'export NVM_DIR="/home/dev/.nvm"' >> .bashrc \
    && echo '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"' >> .bashrc \
    && echo '[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"' >> .bashrc

# 4. Install Claude Code & code-server
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && curl -fsSL https://code-server.dev/install.sh | PREFIX=~/.local sh -s -- --method=standalone \
    && rm -rf .cache/*

# --- 切回 root 配置入口脚本权限 ---
USER root

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 暴露端口
EXPOSE 22 8080

# 切换回 dev 用户作为默认运行用户
USER dev
WORKDIR /home/dev/workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]