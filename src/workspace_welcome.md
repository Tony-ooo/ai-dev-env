# 欢迎使用团队 AI 云端工作站

您正在使用基于 Docker 构建的 **AI 编码** 环境。

## 目录结构
- `/home/dev`：您的用户主目录。
- `/home/dev/workspace`：持久化工作区，会映射到宿主机。

## 预装工具 (常见版本)
| 工具 | 版本 | 说明 |
|------|------|------|
| Ubuntu | 22.04 LTS | 基础镜像 |
| Bash | 5.x | 默认 Shell |
| OpenSSH Server | 最新 | 方便远程 SSH 登录 |
| **Node.js** | 22.x | 由 NodeSource 仓库安装 |
| **Python** | 系统默认版本 | 默认使用系统自带 `python`/`python3`，Miniconda 可按需手动激活 |
| **uv** | 最新 | Rust 实现的极速 Python 包管理器 |
| Git / Vim / curl / build-essential | 最新 | 常用开发工具 |
| **Claude Code / Codex** | 最新 | 常用 AI 工具 |

> 注：版本号可能随镜像重新构建而更新，可在终端通过 `node -v`、`python --version`、`python3 --version` 等命令查看；如需使用 Conda 环境，请先执行 `conda activate base` 或激活你自己的环境。

## 创建容器内部系统守护进程

镜像使用 s6-overlay 作为容器 1 号主进程。容器初始化阶段会以 root 执行用户 UID/GID、密码、SSH 与挂载目录权限配置；通过 SSH 登录后仍进入 `dev` 用户。如需通过 `docker exec` 进入 `dev` 用户，请使用 `docker exec -u dev -it <container> bash`。

容器运行后，可以在容器内部创建 s6 管理的守护进程。服务定义保存在 `/etc/services.d/<service-name>/run`，同一个容器重启后会由 s6 自动拉起。具体设置参考技能 `skills/s6-service-manager` ([GitHub](https://github.com/Tony-ooo/ai-dev-env.git))

## 资源限制

管理员在部署脚本中为每个容器设置了 `--cpus` 与 `--memory` 参数，避免资源争用。如需更多资源，请联系管理员。

祝你编码愉快！
