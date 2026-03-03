#!/usr/bin/env bash

set -euo pipefail

HOME_DIR="${HOME_DIR:-/home/dev}"
APP_NAME="${APP_NAME:-openclaw}"
PORT_OVERRIDE="${OPENCLAW_GATEWAY_PORT:-}"
BIND_OVERRIDE="${OPENCLAW_GATEWAY_BIND:-}"
STATE_DIR="${OPENCLAW_STATE_DIR:-${HOME_DIR}/.openclaw}"
CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${STATE_DIR}/openclaw.json}"
PROBE_RETRIES="${GATEWAY_PROBE_RETRIES:-20}"
PROBE_INTERVAL="${GATEWAY_PROBE_INTERVAL:-1}"

export PATH="${HOME_DIR}/.local/bin:${HOME_DIR}/.npm-global/bin:${HOME_DIR}/bin:${HOME_DIR}/.volta/bin:${HOME_DIR}/.asdf/shims:${HOME_DIR}/.bun/bin:${HOME_DIR}/Library/pnpm:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# 防止旧环境变量污染版本显示。
unset OPENCLAW_VERSION || true
unset OPENCLAW_SERVICE_VERSION || true
unset npm_package_version || true

export HOME="${HOME_DIR}"
export OPENCLAW_STATE_DIR="${STATE_DIR}"
export OPENCLAW_CONFIG_PATH="${CONFIG_PATH}"

if [ -n "${PORT_OVERRIDE}" ]; then
  export OPENCLAW_GATEWAY_PORT="${PORT_OVERRIDE}"
else
  unset OPENCLAW_GATEWAY_PORT || true
fi

# 代理变量透传（若有）。
for proxy_key in HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY http_proxy https_proxy no_proxy all_proxy; do
  if [ -n "${!proxy_key:-}" ]; then
    export "${proxy_key}=${!proxy_key}"
  fi
done

OPENCLAW_BIN="${OPENCLAW_BIN:-${HOME_DIR}/.npm-global/bin/openclaw}"
if [ ! -x "${OPENCLAW_BIN}" ]; then
  OPENCLAW_BIN="$(command -v openclaw 2>/dev/null || true)"
fi

if [ -z "${OPENCLAW_BIN:-}" ] || [ ! -x "${OPENCLAW_BIN}" ]; then
  echo "OpenClaw executable not found. Set OPENCLAW_BIN or ensure openclaw is in PATH."
  exit 1
fi

OPENCLAW_RUNTIME_VERSION="$("${OPENCLAW_BIN}" --version 2>/dev/null | tr -d '\r' | head -n1 | awk '{print $1}' || true)"
if [ -n "${OPENCLAW_RUNTIME_VERSION:-}" ]; then
  # 动态注入当前二进制版本，避免网关自检显示 unknown。
  export OPENCLAW_SERVICE_VERSION="${OPENCLAW_RUNTIME_VERSION}"
fi

echo "[$(date '+%F %T')] Starting Gateway cleanup and restart..."
echo "Using OpenClaw binary: ${OPENCLAW_BIN}"
echo "Detected OpenClaw version: ${OPENCLAW_RUNTIME_VERSION:-unknown}"
echo "PM2 app name: ${APP_NAME}"
if [ -n "${PORT_OVERRIDE}" ]; then
  echo "Gateway port override: ${PORT_OVERRIDE}"
else
  echo "Gateway port source: config/default"
fi
if [ -n "${BIND_OVERRIDE}" ]; then
  echo "Gateway bind override: ${BIND_OVERRIDE}"
else
  echo "Gateway bind source: config/default"
fi

# 0) 尽力优雅停止。
"${OPENCLAW_BIN}" gateway stop >/dev/null 2>&1 || true

# 1) 仅清理目标 PM2 任务。
pm2 delete "${APP_NAME}" >/dev/null 2>&1 || true

# 2) 清理残留 gateway 进程（先 TERM，再兜底 KILL）。
pkill -TERM -f "openclaw.*gateway" >/dev/null 2>&1 || true
sleep 1
pkill -KILL -f "openclaw.*gateway" >/dev/null 2>&1 || true

# 3) 清理锁文件。
rm -f "${STATE_DIR}"/*.lock "${STATE_DIR}/gateway.pid" >/dev/null 2>&1 || true
LOCK_DIR="${TMPDIR:-/tmp}/openclaw-$(id -u)"
if [ -d "${LOCK_DIR}" ]; then
  find "${LOCK_DIR}" -maxdepth 2 -type f \( -name "*gateway*lock*" -o -name "*openclaw*lock*" \) -delete >/dev/null 2>&1 || true
fi

# 4) 启动 PM2（固定本地模式参数）。
START_ARGS=(gateway --allow-unconfigured)
if [ -n "${PORT_OVERRIDE}" ]; then
  START_ARGS+=(--port "${PORT_OVERRIDE}")
fi
if [ -n "${BIND_OVERRIDE}" ]; then
  START_ARGS+=(--bind "${BIND_OVERRIDE}")
fi

pm2 start "${OPENCLAW_BIN}" \
  --name "${APP_NAME}" \
  --interpreter none \
  --time \
  -- "${START_ARGS[@]}"

pm2 save >/dev/null

resolve_probe_port() {
  if [ -n "${PORT_OVERRIDE}" ]; then
    echo "${PORT_OVERRIDE}"
    return 0
  fi

  local cfg_port
  cfg_port="$("${OPENCLAW_BIN}" config get gateway.port 2>/dev/null | tr -d '\r' | head -n1 || true)"
  if [[ "${cfg_port}" =~ ^[0-9]+$ ]] && [ "${cfg_port}" -gt 0 ]; then
    echo "${cfg_port}"
    return 0
  fi

  echo "18789"
}

PROBE_PORT="$(resolve_probe_port)"

probe_once() {
  local -a args
  args=("${OPENCLAW_BIN}" gateway health --url "ws://127.0.0.1:${PROBE_PORT}" --timeout 3000)

  if [ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
    args+=(--token "${OPENCLAW_GATEWAY_TOKEN}")
  elif [ -n "${OPENCLAW_GATEWAY_PASSWORD:-}" ]; then
    args+=(--password "${OPENCLAW_GATEWAY_PASSWORD}")
  fi

  "${args[@]}" >/dev/null 2>&1
}

# 5) 健康检查（本地优先）。
echo "Probing local gateway health..."
for i in $(seq 1 "${PROBE_RETRIES}"); do
  sleep "${PROBE_INTERVAL}"

  if probe_once; then
    echo "Gateway health passed on attempt ${i}/${PROBE_RETRIES}."
    echo "[$(date '+%F %T')] Gateway restart sequence completed."
    exit 0
  fi

  if "${OPENCLAW_BIN}" gateway probe >/dev/null 2>&1; then
    echo "Gateway probe passed on attempt ${i}/${PROBE_RETRIES}."
    echo "[$(date '+%F %T')] Gateway restart sequence completed."
    exit 0
  fi
done

echo "Gateway probe failed after restart."
echo "Detailed diagnostics:"
"${OPENCLAW_BIN}" gateway health --url "ws://127.0.0.1:${PROBE_PORT}" --timeout 3000 || true
"${OPENCLAW_BIN}" gateway probe || true
pm2 describe "${APP_NAME}" || true
pm2 logs "${APP_NAME}" --lines 80 --nostream || true
exit 1
