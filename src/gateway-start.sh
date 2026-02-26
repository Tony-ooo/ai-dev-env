#!/usr/bin/env bash

set -u

# 确保环境变量加载（针对 PM2 和 Node）
export PATH="/home/dev/.local/bin:/home/dev/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

APP_NAME="openclaw"
OPENCLAW_BIN="${OPENCLAW_BIN:-/home/dev/.npm-global/bin/openclaw}"
PROBE_RETRIES="${GATEWAY_PROBE_RETRIES:-15}"
PROBE_INTERVAL="${GATEWAY_PROBE_INTERVAL:-1}"

if [ ! -x "$OPENCLAW_BIN" ]; then
  OPENCLAW_BIN="$(command -v openclaw 2>/dev/null || true)"
fi

if [ -z "${OPENCLAW_BIN:-}" ] || [ ! -x "$OPENCLAW_BIN" ]; then
  echo "OpenClaw executable not found. Set OPENCLAW_BIN or ensure openclaw is in PATH."
  exit 1
fi

echo "[$(date)] Starting Gateway cleanup and restart..."
echo "Using OpenClaw binary: $OPENCLAW_BIN"

# 1. 停止 PM2 任务（避免重复启动）
echo "Stopping existing PM2 task..."
pm2 delete "$APP_NAME" 2>/dev/null || true
pm2 kill 2>/dev/null || true

# 2. 强制清理所有相关进程
echo "Killing all gateway processes..."
pkill -9 -f "openclaw.*gateway" 2>/dev/null || true
sleep 1

# 3. 清理锁文件和 PID 文件
echo "Cleaning up lock files..."
rm -f ~/.openclaw/*.lock ~/.openclaw/gateway.pid
find /tmp -maxdepth 2 -type f \( -name "*openclaw*lock" -o -name "*gateway*lock" \) -delete 2>/dev/null || true

# 4. 启动 PM2（用可执行文件 + interpreter none，避免参数被 Node 误吞）
echo "Starting OpenClaw Gateway via PM2..."
pm2 start "$OPENCLAW_BIN" \
  --name "$APP_NAME" \
  --interpreter none \
  --time \
  -- gateway run

pm2 save

# 5. 健康检查（提高重试次数，降低冷启动误判）
echo "Probing gateway health..."
for i in $(seq 1 "$PROBE_RETRIES"); do
  sleep "$PROBE_INTERVAL"
  if "$OPENCLAW_BIN" gateway probe >/dev/null 2>&1; then
    echo "Gateway probe passed on attempt ${i}/${PROBE_RETRIES}."
    echo "[$(date)] Gateway restart sequence completed."
    exit 0
  fi
done

echo "Gateway probe failed after restart."
echo "Detailed probe output:"
"$OPENCLAW_BIN" gateway probe || true
pm2 describe "$APP_NAME" || true
pm2 logs "$APP_NAME" --lines 60 --nostream
exit 1
