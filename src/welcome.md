# 欢迎使用团队 AI 云端工作站

您正在使用基于 Docker 构建的 **AI 编码** 环境。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `/home/dev` | 用户主目录 |
| `/home/dev/workspace` | 持久化工作区（存于数据卷，重启不丢失） |
| `/home/dev/.claude`、`/home/dev/.codex` | AI 工具配置，持久化 |
| `/home/dev/.agents/skills` | 内置技能目录，持久化 |
| `/home/dev/.claude/skills` | 指向 `/home/dev/.agents/skills` 的符号链接 |
| `/etc/s6-overlay/s6-rc.d` | s6 服务定义（指向数据卷，持久化） |
| `/run/service` | s6 运行期监督目录 |

## 预装工具

| 工具 | 版本 | 说明 |
| --- | --- | --- |
| Ubuntu | 24.04 LTS | 基础镜像 |
| OpenSSH Server | 构建时系统包版本 | SSH 登录，端口 22 |
| Node.js | 24.x | NodeSource 仓库安装 |
| Python | 系统默认 | `python` / `python3` 直连系统解释器 |
| uv | 随镜像固定 | Python 包管理器，推荐用它管理依赖 |
| Claude Code / Codex | 随镜像固定 | 已全局安装于 `~/.npm-global` |
| herdr | 随镜像固定 | 终端 AI Agent 运行时，位于 `~/.local/bin` |
| git / vim / tmux / ripgrep / jq / build-essential | 构建时系统包版本 | 常用开发工具 |

实际版本见 `/opt/ai-dev-env/build-versions.txt`。

> 本镜像不包含 Conda。Python 依赖请用 `uv venv` + `uv pip install`，或系统 `python3 -m venv`。

## 容器与用户

镜像以 `dev` 用户启动，因此 `docker exec -it <container> bash` 默认就是 `dev`。
需要 root 时使用 `docker exec -u root -it <container> bash`，`dev` 用户也具备 `sudo`。

## 创建容器内部守护进程

镜像使用 s6-overlay 作为容器 1 号主进程，负责信号转发、僵尸回收与服务监督。

服务定义写在 `/etc/s6-overlay/s6-rc.d/<name>/`（该路径是指向数据卷的符号链接），容器重启后由 s6 自动拉起。运行期监督目录为 `/run/service`。

容器启动前会校验这些定义：缺少 `type`/`run`、服务名非法、或 `s6-rc-compile` 试编译失败的定义，会被移动到 `.disabled-by-guard/` 并附上 `disabled-reason.txt`，不会阻塞容器启动。

具体做法参考技能 `s6-service-manager`。

## 资源限制

CPU 与内存限制取决于管理员部署时设置的 `--cpu`、`--memory`；未设置时不限制。如需调整资源，请联系管理员。

祝你编码愉快！
