#!/command/with-contenv bash
set -euo pipefail

SSH_PORT=${SSH_PORT:-22}
SSHD_PID=""

stop_sshd() {
    if [ -n "$SSHD_PID" ] && kill -0 "$SSHD_PID" 2>/dev/null; then
        kill -TERM "$SSHD_PID"
        wait "$SSHD_PID" || true
    fi
}

trap stop_sshd TERM INT QUIT

/usr/sbin/sshd -t
/usr/sbin/sshd -D &
SSHD_PID=$!

sleep 0.3
if ! kill -0 "$SSHD_PID" 2>/dev/null; then
    wait "$SSHD_PID"
fi

echo "✅ SSH 服务已启动，监听端口: ${SSH_PORT}"
wait "$SSHD_PID"
