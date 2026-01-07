#!/bin/bash
set -euo pipefail

# ============================================================
# 权限管理：提升到 root 执行初始化
# ============================================================
CURRENT_USER=$(whoami)

if [ "$CURRENT_USER" = "dev" ]; then
    echo "🔧 以 dev 用户启动，提升到 root 权限执行初始化..."
    exec sudo -E "$0" "$@"
fi

echo "🚀 Entrypoint starting as root..."

# ============================================================
# 第一步：环境变量与用户 UID/GID 适配
# ============================================================
HOST_UID=${HOST_UID:-1000}
HOST_GID=${HOST_GID:-1000}

CURRENT_UID=$(id -u dev)
CURRENT_GID=$(id -g dev)

echo "🔍 宿主机 UID/GID=$HOST_UID/$HOST_GID, 容器 dev UID/GID=$CURRENT_UID/$CURRENT_GID"

if [ "$HOST_UID" -gt 65535 ] || [ "$HOST_GID" -gt 65535 ]; then
    # Windows 环境
    echo "⚠️ 检测到 Windows 环境（UID/GID > 65535），跳过用户 ID 修改"
else
    # Linux/macOS 环境
    echo "🔧 Linux/macOS 环境：调整容器用户 UID/GID 以匹配宿主机..."

    # 调整 UID
    if [ "$CURRENT_UID" -ne "$HOST_UID" ]; then
        echo "   ├─ 修改 dev 用户 UID: $CURRENT_UID → $HOST_UID"
        usermod -u "$HOST_UID" dev
    fi

    # 调整 GID
    if [ "$CURRENT_GID" -ne "$HOST_GID" ]; then
        echo "   ├─ 修改 dev 用户 GID: $CURRENT_GID → $HOST_GID"
        if getent group "$HOST_GID" >/dev/null; then
            usermod -g "$HOST_GID" dev
        else
            groupmod -g "$HOST_GID" dev || true
            usermod -g "$HOST_GID" dev
        fi
    fi
fi

# ============================================================
# 第二步：设置 dev 用户密码
# ============================================================
USER_PASSWORD=${USER_PASSWORD:-}
if [ -n "$USER_PASSWORD" ]; then
    echo "dev:${USER_PASSWORD}" | chpasswd >/dev/null 2>&1
    echo "✅ dev 用户密码已设置"
else
    echo "⚠️ 未提供 USER_PASSWORD 环境变量，SSH/code-server 登录可能失败"
fi

# ============================================================
# 第三步：启动 SSH 服务
# ============================================================
if [ ! -f /etc/ssh/ssh_host_rsa_key ]; then
    echo "🔑 生成 SSH 主机密钥..."
    ssh-keygen -A
fi

SSH_PORT=${SSH_PORT:-22}
mkdir -p /run/sshd
sed -i "s/^#\?Port .*/Port $SSH_PORT/" /etc/ssh/sshd_config
/usr/sbin/sshd
echo "✅ SSH 服务已启动（端口: $SSH_PORT）"

# ============================================================
# 第四步：切换到 dev 用户启动 code-server
# ============================================================
VSCODE_PORT=${VSCODE_PORT:-8080}
ENABLE_HTTPS=${ENABLE_HTTPS:-false}
CODE_SERVER_BIN="/home/dev/.local/bin/code-server"

if [ ! -x "$CODE_SERVER_BIN" ]; then
    CODE_SERVER_BIN="$(command -v code-server || true)"
fi

if [ -z "$CODE_SERVER_BIN" ]; then
    echo "❌ 未找到 code-server，容器启动失败"
    exit 1
fi

# 确保 dev 用户目录权限正确
echo "🔧 修正 /home/dev 目录所有权..."
chown -R dev:dev /home/dev || true

echo "🚀 启动 code-server（端口: $VSCODE_PORT）..."

# 构建证书参数
CERT_ARGS=""
if [ "$ENABLE_HTTPS" = "true" ]; then
    echo "🔒 HTTPS 已启用"
    CERT_ARGS="--cert --cert-host=\"*\""
else
    echo "⚠️  HTTPS 未启用（使用 HTTP）"
fi

exec sudo -u dev bash -l <<EOF
export PASSWORD='$USER_PASSWORD'
export HOME=/home/dev
export USER=dev
cd /home/dev/workspace
exec "$CODE_SERVER_BIN" \
    --bind-addr 0.0.0.0:$VSCODE_PORT \
    --auth password \
    $CERT_ARGS \
    /home/dev/workspace
EOF
