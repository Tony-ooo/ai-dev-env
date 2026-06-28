#!/command/with-contenv bash
set -euo pipefail

# ============================================================
# s6-overlay container initialization hook
# ============================================================
echo "🚀 初始化容器环境..."

# ============================================================
# 第一步：环境变量与用户 UID/GID 适配
# ============================================================
HOST_UID=${HOST_UID:-1000}
HOST_GID=${HOST_GID:-1000}

CURRENT_UID=$(id -u dev)
CURRENT_GID=$(id -g dev)

if [ "$HOST_UID" -gt 65535 ] || [ "$HOST_GID" -gt 65535 ]; then
    # Windows 环境
    echo "⚠️ 检测到 Windows 风格 UID/GID，跳过 dev 用户 ID 适配"
else
    # Linux/macOS 环境
    # 调整 UID
    if [ "$CURRENT_UID" -ne "$HOST_UID" ]; then
        usermod -u "$HOST_UID" dev
    fi

    # 调整 GID
    if [ "$CURRENT_GID" -ne "$HOST_GID" ]; then
        if getent group "$HOST_GID" >/dev/null; then
            usermod -g "$HOST_GID" dev
        else
            groupmod -g "$HOST_GID" dev || true
            usermod -g "$HOST_GID" dev
        fi
    fi

    echo "✅ dev 用户 ID 已适配: $(id -u dev)/$(id -g dev)"
fi

touch /home/dev/.sudo_as_admin_successful
chown dev:dev /home/dev/.sudo_as_admin_successful

# ============================================================
# 第二步：设置 dev 用户密码
# ============================================================
USER_PASSWORD=${USER_PASSWORD:-}
if [ -n "$USER_PASSWORD" ]; then
    echo "dev:${USER_PASSWORD}" | chpasswd >/dev/null 2>&1
    echo "✅ dev 用户密码已设置"
else
    echo "⚠️ 未提供 USER_PASSWORD 环境变量，SSH 登录可能失败"
fi

# ============================================================
# 第三步：配置 SSH 服务
# ============================================================
if [ ! -f /etc/ssh/ssh_host_rsa_key ]; then
    echo "🔑 生成 SSH 主机密钥..."
    ssh-keygen -A
fi

SSH_PORT=${SSH_PORT:-22}
mkdir -p /run/sshd
sed -i "s/^#\?Port .*/Port $SSH_PORT/" /etc/ssh/sshd_config
echo "✅ SSH 已配置: 端口 $SSH_PORT"

# ============================================================
# 第四步：修正必要的 /home/dev 配置权限
# ============================================================
# 仅修复登录和工具配置真正需要写入的路径，避免递归扫描大型 workspace。
echo "🔧 修正 /home/dev 配置权限..."
CHOWN_START=$SECONDS
CHOWN_EXIT=0

if (
    chown dev:dev /home/dev 2>/dev/null || true

    for file_path in /home/dev/.sudo_as_admin_successful /home/dev/.bashrc.extra /home/dev/.claude.json; do
        if [ -e "$file_path" ] || [ -L "$file_path" ]; then
            chown dev:dev "$file_path" 2>/dev/null || true
        fi
    done

    for config_dir in /home/dev/.claude /home/dev/.codex; do
        if [ -d "$config_dir" ] && [ ! -L "$config_dir" ]; then
            chown --from=0:0 -R dev:dev "$config_dir" 2>/dev/null || chown -R dev:dev "$config_dir" 2>/dev/null || true
        fi
    done

    if [ -d /home/dev/.vscode-server ] && [ ! -L /home/dev/.vscode-server ] && ! sudo -u dev test -w /home/dev/.vscode-server; then
        chown --from=0:0 -R dev:dev /home/dev/.vscode-server 2>/dev/null || chown -R dev:dev /home/dev/.vscode-server 2>/dev/null || true
    fi
); then
    CHOWN_EXIT=0
else
    CHOWN_EXIT=$?
fi

CHOWN_COST=$((SECONDS - CHOWN_START))

if [ "$CHOWN_EXIT" -eq 0 ]; then
    echo "✅ /home/dev 配置权限已修正 (${CHOWN_COST}s)"
else
    echo "⚠️ /home/dev 配置权限修正部分失败（退出码: $CHOWN_EXIT），继续启动"
fi

echo "🔍 校验 SSH 服务配置（端口: $SSH_PORT）..."
/usr/sbin/sshd -t
echo "✅ 容器初始化完成"
