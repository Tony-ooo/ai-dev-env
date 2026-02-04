#!/bin/bash
set -eo pipefail

# ============================================================
# AI 云端工作站 - 容器部署脚本
# ============================================================
# 用途：为单个用户创建一个独立的开发容器环境
# 特性：自动端口映射、GPU 检测、资源限制、持久化目录
# ============================================================

# ============================================================
# 第一步：默认配置与变量初始化
# ============================================================

# 容器内固定端口（通过宿主机端口映射对外暴露）
SSH_PORT=22              # SSH 服务端口
VSCODE_PORT=8080         # code-server 服务端口
OPENCLAW_PORT=18789      # OpenClaw 服务端口

# 系统环境检测
HOST_UID=$(id -u)        # 宿主机当前用户 UID（用于文件权限映射）
HOST_GID=$(id -g)        # 宿主机当前用户 GID
GPU_ENABLED=false        # GPU 启用标志（自动检测）

# 可选资源限制（通过命令行参数设置）
CPUS=""                  # CPU 核心数限制（例如：2 或 1.5）
MEMORY=""                # 内存限制（例如：4g 或 2048m）

# ============================================================
# 第二步：命令行参数解析
# ============================================================
# 必填参数：
#   --user-name      用户名（容器命名和数据目录标识）
#   --port-base      端口基数（例如：101 表示映射 10122 和 10180）
#   --user_password  用户密码（SSH 和 code-server 登录密码）
#   --base_data_dir  宿主机数据根目录
#   --image          Docker 镜像名称
# 可选参数：
#   --cpu            CPU 限制
#   --memory         内存限制
#   --enable-https   启用 HTTPS（true/false，默认 false）
# ============================================================

while [[ $# -gt 0 ]]; do
    case $1 in
        --user-name)      USER_NAME="$2"; shift 2 ;;
        --port-base)      PORT_BASE="$2"; shift 2 ;;
        --user_password)  USER_PASSWORD="$2"; shift 2 ;;
        --base_data_dir)  BASE_DATA_DIR="$2"; shift 2 ;;
        --cpu)            CPUS="$2"; shift 2 ;;
        --memory)         MEMORY="$2"; shift 2 ;;
        --image)          IMAGE_NAME="$2"; shift 2 ;;
        --enable-https)   ENABLE_HTTPS="$2"; shift 2 ;;
        *)
            echo "❌ 错误: 未知参数 '$1'"
            echo "用法示例："
            echo "  $0 --user-name alice --port-base 101 --user_password 'secret' \\"
            echo "     --base_data_dir /data --image ai-dev:latest"
            exit 1
            ;;
    esac
done

# ============================================================
# 第三步：必填参数校验
# ============================================================
# 确保所有必需参数都已提供，否则脚本将终止
# ============================================================

: "${USER_NAME:?❌ 必须指定 --user-name（用户名）}"
: "${USER_PASSWORD:?❌ 必须指定 --user_password（登录密码）}"
: "${BASE_DATA_DIR:?❌ 必须指定 --base_data_dir（数据目录）}"
: "${IMAGE_NAME:?❌ 必须指定 --image（Docker 镜像）}"
: "${PORT_BASE:?❌ 必须指定 --port-base（端口基数）}"

# ============================================================
# 第四步：用户数据目录准备
# ============================================================
# 在宿主机上为用户创建持久化目录结构：
#   $BASE_DATA_DIR/$USER_NAME/
#   ├── workspace/          # 工作区（代码项目）
#   ├── ai-configs/         # AI 配置
#   ├── .code-server/       # Code Server 数据（开源服务器Web版）
#   ├── .vscode-server/     # VSCode Server 数据（官方服务器）
#   └── .bashrc.extra       # 用户自定义 shell 配置
# ============================================================

# 1. 检查并创建基础数据目录
if [ ! -d "$BASE_DATA_DIR" ]; then
    echo "⚠️ 基础数据目录不存在，正在创建: $BASE_DATA_DIR"
    mkdir -p "$BASE_DATA_DIR" || {
        echo "❌ 无法创建基础数据目录，请检查权限"
        echo "   建议执行: sudo mkdir -p '$BASE_DATA_DIR' && sudo chown $USER '$BASE_DATA_DIR'"
        exit 1
    }
fi

# 2. 构建目录路径
DATA_DIR="$BASE_DATA_DIR/$USER_NAME"
BASHRC_EXTRA_DIR="$DATA_DIR/.bashrc.extra"
CLAUDE_DIR="$DATA_DIR/ai-configs/.claude"
CLAUDE_JSON_DIR="$DATA_DIR/ai-configs/.claude.json"
CODEX_DIR="$DATA_DIR/ai-configs/.codex"
GEMINI_DIR="$DATA_DIR/ai-configs/.gemini"
OPENCLAW_DIR="$DATA_DIR/ai-configs/.openclaw"
VSCODE_SERVER_DIR="$DATA_DIR/.vscode-server"
CODE_SERVER_DIR="$DATA_DIR/.code-server"
WORKSPACE_DIR="$DATA_DIR/workspace"

# 3. 创建所有必要目录
mkdir -p "$WORKSPACE_DIR" "$CLAUDE_DIR" "$CODEX_DIR" "$GEMINI_DIR" "$VSCODE_SERVER_DIR" "$CODE_SERVER_DIR" "$OPENCLAW_DIR"

# 3. 创建配置文件（仅当不存在时）
# 注意：先删除可能被 Docker 自动创建的同名目录
[[ ! -f "$BASHRC_EXTRA_DIR" ]] && { [[ -d "$BASHRC_EXTRA_DIR" ]] && rm -rf "$BASHRC_EXTRA_DIR"; touch "$BASHRC_EXTRA_DIR"; }
[[ ! -f "$CLAUDE_JSON_DIR" ]] && { [[ -d "$CLAUDE_JSON_DIR" ]] && rm -rf "$CLAUDE_JSON_DIR"; echo '{"hasCompletedOnboarding": true}' > "$CLAUDE_JSON_DIR"; }

# 注意：文件权限由容器 entrypoint.sh 统一处理
# - mkdir 创建的目录自动归属当前用户，通常无需手动修正
# - 容器启动时，entrypoint.sh 会根据 HOST_UID/GID 自动调整所有挂载目录的权限

# 3. 初始化工作区欢迎文档
cat <<'EOF' > "$WORKSPACE_DIR/README.md"
# 欢迎使用团队 AI 云端工作站

您正在使用基于 Docker 构建的 **AI 编码 4.0** 环境。

## 快速提示

## 预装工具 (常见版本)
| 工具 | 版本 | 说明 |
|------|------|------|
| Ubuntu | 22.04 LTS | 基础镜像 |
| Bash | 5.x | 默认 Shell |
| OpenSSH Server | 最新 | 方便远程 SSH 登录 |
| **Node.js** | 22.x | 由 NodeSource 仓库安装 |
| **Python** | 3.10 | 由 Miniconda 提供 |
| code-server | 最新 | VS Code Web 版 |
| **uv** | 最新 | Rust 实现的极速 Python 包管理器 |
| Git / Vim / curl / build-essential | 最新 | 常用开发工具 |
| **Claude Code / Codex / Gemini / OpenClaw** | 最新 | 常用 AI 工具 |

> 注：版本号可能随镜像重新构建而更新，可在终端通过 `node -v`、`python --version` 等命令查看。

## 目录结构
- `/home/dev`：您的用户主目录。
- `/home/dev/workspace`：持久化工作区，会映射到宿主机。

## 资源限制
管理员在部署脚本中为每个容器设置了 `--cpus` 与 `--memory` 参数，避免资源争用。如需更多资源，请联系管理员。

祝你编码愉快！
EOF

# ============================================================
# 第五步：构建 Docker 运行命令
# ============================================================
# 组装完整的 docker run 命令，包括：
# - 容器命名规则：ai-dev-{用户名}-{端口基数}80
# - 资源限制（CPU/内存）
# - 目录挂载（持久化用户数据）
# - 端口映射（SSH 和 code-server）
# - GPU 自动检测与配置
# ============================================================

# 1. 基础命令（容器名称、重启策略）
DOCKER_CMD=(
    docker run -d
    --name "ai-dev-${USER_NAME}-${PORT_BASE}80"
    --restart always
    --add-host=host.docker.internal:host-gateway
)

# 2. 可选资源限制
if [[ -n "$CPUS" ]]; then
    DOCKER_CMD+=(--cpus "$CPUS")
fi

if [[ -n "$MEMORY" ]]; then
    DOCKER_CMD+=(--memory "$MEMORY")
fi

# 3. 挂载目录和环境变量
DOCKER_CMD+=(
    # 持久化目录挂载
    -v "$WORKSPACE_DIR:/home/dev/workspace"
    -v "$BASHRC_EXTRA_DIR:/home/dev/.bashrc.extra"
    -v "$CLAUDE_DIR:/home/dev/.claude"
    -v "$CLAUDE_JSON_DIR:/home/dev/.claude.json"
    -v "$CODEX_DIR:/home/dev/.codex"
    -v "$GEMINI_DIR:/home/dev/.gemini"
    -v "$VSCODE_SERVER_DIR:/home/dev/.vscode-server"
    -v "$OPENCLAW_DIR:/home/dev/.openclaw"
    -v "$CODE_SERVER_DIR:/home/dev/.local/share/code-server"

    # 环境变量（用于 entrypoint.sh 权限处理）
    -e "HOST_UID=$HOST_UID"
    -e "HOST_GID=$HOST_GID"
    -e "USER_PASSWORD=$USER_PASSWORD"
    -e "ENABLE_HTTPS=${ENABLE_HTTPS:-false}"

    # 端口映射（宿主机:容器）
    -p "${PORT_BASE}22:$SSH_PORT"      # SSH 端口
    -p "${PORT_BASE}80:$VSCODE_PORT"   # code-server 端口
    -p "${PORT_BASE}789:$OPENCLAW_PORT"   # openclaw 端口
)

# 4. GPU 支持自动检测
if nvidia-smi -L &>/dev/null; then
    echo "✅ 检测到 NVIDIA GPU，启用 GPU 支持"
    GPU_ENABLED=true
    DOCKER_CMD+=(
        --gpus all
        -e NVIDIA_VISIBLE_DEVICES=all
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
    )
else
    echo "ℹ️ 未检测到 NVIDIA GPU，使用 CPU 模式"
fi

# 5. 指定镜像
DOCKER_CMD+=("$IMAGE_NAME")

# ============================================================
# 第六步：执行容器部署
# ============================================================

echo "🚀 正在为用户 $USER_NAME 部署开发环境容器..."
echo ""

# 执行 docker run 命令
if DOCKER_OUTPUT=$("${DOCKER_CMD[@]}" 2>&1); then
    # ========================================
    # 部署成功 - 显示访问信息
    # ========================================
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🎉 用户 $USER_NAME 的开发环境已部署完成！"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # 登录凭证
    echo "🔐 登录凭证："
    echo "   用户名: dev"
    echo "   密码: $USER_PASSWORD"
    echo ""

    # 访问方式
    echo "🌐 访问方式："
    echo "   ├─ Web VS Code:   http://YOUR_SERVER_IP:${PORT_BASE}80"
    echo "   └─ SSH 终端:      ssh dev@YOUR_SERVER_IP -p ${PORT_BASE}22"
    echo "   └─ OpenClaw 端口: http://YOUR_SERVER_IP:${PORT_BASE}789"
    echo ""

    # 资源配置
    echo "📊 资源配置："
    if [[ -n "$CPUS" ]]; then
        echo "   ├─ CPU 限制: $CPUS 核"
    else
        echo "   ├─ CPU 限制: 无限制（使用宿主机全部资源）"
    fi

    if [[ -n "$MEMORY" ]]; then
        echo "   ├─ 内存限制: $MEMORY"
    else
        echo "   ├─ 内存限制: 无限制（使用宿主机全部资源）"
    fi

    if [[ "$GPU_ENABLED" == true ]]; then
        echo "   └─ GPU 分配: 已启用（all）"
    else
        echo "   └─ GPU 分配: 未启用"
    fi
    echo ""

    # 重要提示
    echo "⚠️  注意事项："
    echo "   1. 请将 'YOUR_SERVER_IP' 替换为宿主机的实际 IP 地址"
    echo "   2. 确保防火墙已开放相应端口（${PORT_BASE}22 和 ${PORT_BASE}80）"
    echo "   3. Web VS Code 和 SSH 使用相同的登录密码"
    echo "   4. 用户数据持久化到: $DATA_DIR"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

else
    # ========================================
    # 部署失败 - 显示错误信息
    # ========================================
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "❌ 容器部署失败！"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "错误信息："
    echo "$DOCKER_OUTPUT"
    echo ""
    echo "常见问题排查："
    echo "  1. 检查端口是否已被占用：lsof -i:${PORT_BASE}22 或 lsof -i:${PORT_BASE}80"
    echo "  2. 检查容器名称是否冲突：docker ps -a | grep ai-dev-${USER_NAME}"
    echo "  3. 检查镜像是否存在：docker images | grep $IMAGE_NAME"
    echo "  4. 检查 Docker 服务状态：systemctl status docker"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 2
fi
