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
# 第四步：修正 /home/dev 目录权限
# ============================================================
# 自动探测 /home/dev 下需要修复权限的路径（不再硬编码关键目录）
echo "🔧 修正 /home/dev 权限..."
CHOWN_START=$SECONDS
CHOWN_EXIT=0

if (
    chown dev:dev /home/dev 2>/dev/null || true
    CHOWN_TARGETS=()

    # 1) 自动收集挂载到 /home/dev 下的卷/绑定路径
    if [ -r /proc/self/mountinfo ]; then
        while IFS= read -r mount_target; do
            [ -n "$mount_target" ] && CHOWN_TARGETS+=("$mount_target")
        done < <(awk '$5 ~ /^\/home\/dev(\/|$)/ {print $5}' /proc/self/mountinfo 2>/dev/null | sort -u)
    fi

    # 2) 自动扫描 root 所有权路径（默认 3 层，避免全量深度扫描）
    CHOWN_SCAN_DEPTH=${CHOWN_SCAN_DEPTH:-3}
    case "$CHOWN_SCAN_DEPTH" in
        ''|*[!0-9]*) CHOWN_SCAN_DEPTH=3 ;;
    esac
    if [ "$CHOWN_SCAN_DEPTH" -le 0 ]; then
        CHOWN_SCAN_DEPTH=3
    fi

    while IFS= read -r detected_target; do
        CHOWN_TARGETS+=("$detected_target")
    done < <(find /home/dev -mindepth 1 -maxdepth "$CHOWN_SCAN_DEPTH" \( -uid 0 -o -gid 0 \) -print 2>/dev/null || true)

    declare -A CHOWN_SEEN=()
    for target in "${CHOWN_TARGETS[@]}"; do
        if [ -n "${CHOWN_SEEN[$target]+x}" ]; then
            continue
        fi
        CHOWN_SEEN["$target"]=1

        if [ -e "$target" ] || [ -L "$target" ]; then
            if [ -d "$target" ] && [ ! -L "$target" ]; then
                # 优先只修 root:root，兼容旧版本 chown 再兜底全量修复
                chown --from=0:0 -R dev:dev "$target" 2>/dev/null || chown -R dev:dev "$target" 2>/dev/null || true
            else
                chown dev:dev "$target" 2>/dev/null || true
            fi
        fi
    done
); then
    CHOWN_EXIT=0
else
    CHOWN_EXIT=$?
fi

CHOWN_COST=$((SECONDS - CHOWN_START))

if [ "$CHOWN_EXIT" -eq 0 ]; then
    echo "✅ /home/dev 权限已修正 (${CHOWN_COST}s)"
else
    echo "⚠️ /home/dev 权限修正部分失败（退出码: $CHOWN_EXIT），继续启动"
fi

echo "🔍 校验 SSH 服务配置（端口: $SSH_PORT）..."
/usr/sbin/sshd -t
echo "✅ 容器初始化完成"
