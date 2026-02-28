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

# 自动探测 /home/dev 下需要修复权限的路径（不再硬编码关键目录）
echo "🔧 自动探测并修正 /home/dev 下 root 所有权路径..."

(
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
) &
CHOWN_PID=$!
CHOWN_START=$SECONDS
CHOWN_ESTIMATE_SEC=${CHOWN_ESTIMATE_SEC:-100}
CHOWN_WIDTH=24
CHOWN_LAST_SHOWN=-1

case "$CHOWN_ESTIMATE_SEC" in
    ''|*[!0-9]*) CHOWN_ESTIMATE_SEC=100 ;;
esac
if [ "$CHOWN_ESTIMATE_SEC" -le 0 ]; then
    CHOWN_ESTIMATE_SEC=100
fi

if [ -t 1 ]; then
    while kill -0 "$CHOWN_PID" 2>/dev/null; do
        CHOWN_COST=$((SECONDS - CHOWN_START))
        CHOWN_PERCENT=$((CHOWN_COST * 100 / CHOWN_ESTIMATE_SEC))
        if [ "$CHOWN_PERCENT" -gt 99 ]; then
            CHOWN_PERCENT=99
        fi
        CHOWN_FILLED=$((CHOWN_PERCENT * CHOWN_WIDTH / 100))
        CHOWN_EMPTY=$((CHOWN_WIDTH - CHOWN_FILLED))
        CHOWN_BAR_FILL=$(printf '%*s' "$CHOWN_FILLED" '' | tr ' ' '#')
        CHOWN_BAR_EMPTY=$(printf '%*s' "$CHOWN_EMPTY" '' | tr ' ' '-')
        if [ "$CHOWN_COST" -ne "$CHOWN_LAST_SHOWN" ]; then
            printf "\r   总进度(估算)：[%s%s] %3d%% 进行中 %ss" \
                "$CHOWN_BAR_FILL" "$CHOWN_BAR_EMPTY" "$CHOWN_PERCENT" "$CHOWN_COST"
            CHOWN_LAST_SHOWN=$CHOWN_COST
        fi
        sleep 0.2
    done
else
    while kill -0 "$CHOWN_PID" 2>/dev/null; do
        CHOWN_COST=$((SECONDS - CHOWN_START))
        CHOWN_PERCENT=$((CHOWN_COST * 100 / CHOWN_ESTIMATE_SEC))
        if [ "$CHOWN_PERCENT" -gt 99 ]; then
            CHOWN_PERCENT=99
        fi
        CHOWN_FILLED=$((CHOWN_PERCENT * CHOWN_WIDTH / 100))
        CHOWN_EMPTY=$((CHOWN_WIDTH - CHOWN_FILLED))
        CHOWN_BAR_FILL=$(printf '%*s' "$CHOWN_FILLED" '' | tr ' ' '#')
        CHOWN_BAR_EMPTY=$(printf '%*s' "$CHOWN_EMPTY" '' | tr ' ' '-')
        if [ $((CHOWN_COST % 5)) -eq 0 ] && [ "$CHOWN_COST" -ne "$CHOWN_LAST_SHOWN" ]; then
            echo "   总进度(估算)：[${CHOWN_BAR_FILL}${CHOWN_BAR_EMPTY}] ${CHOWN_PERCENT}% 进行中 ${CHOWN_COST}s"
            CHOWN_LAST_SHOWN=$CHOWN_COST
        fi
        sleep 1
    done
fi

CHOWN_EXIT=0
wait "$CHOWN_PID" || CHOWN_EXIT=$?
CHOWN_COST=$((SECONDS - CHOWN_START))
CHOWN_BAR_DONE=$(printf '%*s' "$CHOWN_WIDTH" '' | tr ' ' '#')

if [ -t 1 ]; then
    printf "\r   总进度(估算)：[%s] 100%% 完成 %ss\n" "$CHOWN_BAR_DONE" "$CHOWN_COST"
else
    echo "   总进度(估算)：[${CHOWN_BAR_DONE}] 100% 完成 ${CHOWN_COST}s"
fi

if [ "$CHOWN_EXIT" -eq 0 ]; then
    echo "✅ 关键目录所有权修正完成"
else
    echo "⚠️ 关键目录所有权修正部分失败（退出码: $CHOWN_EXIT），继续启动"
fi

GATEWAY_START_SCRIPT="/usr/local/bin/gateway-start.sh"
if [ -x "$GATEWAY_START_SCRIPT" ]; then
    echo "🚀 执行 OpenClaw Gateway 启动脚本..."
    if sudo -E -u dev "$GATEWAY_START_SCRIPT"; then
        echo "✅ OpenClaw Gateway 启动脚本执行完成"
    else
        echo "⚠️ OpenClaw Gateway 启动脚本执行失败，继续启动 code-server"
    fi
else
    echo "⚠️ 未找到 gateway-start.sh，跳过 OpenClaw Gateway 启动"
fi

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
