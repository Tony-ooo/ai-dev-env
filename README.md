# AI Cloud Station Dev Env

为每个用户提供独立的 AI 开发容器和持久化数据卷，通过 SSH 使用。环境预装 Claude Code、Codex、Node、Python、Git 和 uv，也可以启用独立的浏览器电脑供 agent 操作。

## 开始使用

需要安装 Git、Bash、Docker CLI 和 Docker Engine。启用浏览器电脑时，Docker CLI 和 Engine 均需为 26 或更高版本。以下命令在 Docker 主机的终端运行；Windows 可使用 Git Bash。

获取源码：

```bash
git clone --branch main https://github.com/Tony-ooo/ai-dev-env.git
cd ai-dev-env
```

准备镜像。下面的命令构建开发环境和浏览器镜像；不需要浏览器时去掉 `--with-browser`：

```bash
scripts/build.sh --with-browser
```

也可以拉取已发布的镜像，部署脚本使用前需要它们存在于本机：

```bash
docker pull tonyooo/ai-dev-env
docker pull tonyooo/ai-dev-browser  # 需要浏览器时执行
```

Docker 会自动选择适合的架构。如果所需标签尚未发布，请使用上面的构建命令。

部署用户 `tony`，并启用浏览器电脑：

```bash
scripts/deploy.sh --user tony --port 30 --with-browser
```

不需要浏览器时去掉 `--with-browser`。`--port 30` 对应 SSH 端口 `3022`；未指定密码时，部署结束会显示随机生成的密码，请及时保存。

在 Docker 主机上连接：

```bash
ssh dev@127.0.0.1 -p 3022
```

SSH 默认只监听本机。需要从其他电脑连接时，部署时加入 `--bind 0.0.0.0`，配置防火墙白名单，并将连接地址改为 Docker 主机的地址。

## 浏览器电脑

浏览器电脑与开发容器独立，默认不创建。也可以在部署后单独启用：

```bash
scripts/browser.sh start  --user tony --base 30
scripts/browser.sh status --user tony --base 30
scripts/browser.sh stop   --user tony --base 30
```

查看浏览器实时画面时，先在自己电脑的终端建立 SSH 隧道。以下示例适用于本机部署；远程部署时将 `127.0.0.1` 中 `dev@` 后面的地址替换为 Docker 主机地址：

```bash
ssh -T -o ExitOnForwardFailure=yes -p 3022 -L 127.0.0.1:6080:ai-dev-tony-30-browser:6080 dev@127.0.0.1 "echo SSH connected. Open the live view to check the tunnel.; exec sleep infinity"
```

看到 `SSH connected` 后保持终端开启，在同一台电脑的浏览器打开 [实时画面](http://127.0.0.1:6080/vnc.html)。如果隧道已经打开，可直接访问该地址。若本机 6080 端口被占用，可将隧道命令中的 `127.0.0.1:6080` 和访问地址中的端口一起改为 `6081`。

在开发容器终端查看画面登录账号和密码，自行输入，不要发送到聊天中：

```bash
cat /home/dev/.ai-data/browser/state/screen-auth; printf '\n'
```

agent 可在开发容器中使用 `ai-browser` 操作浏览器：

```bash
ai-browser open https://example.com
ai-browser snapshot
ai-browser act '[{"kind":"click","ref":"e3"}]'
```

`e3` 是示例，实际操作时使用 `snapshot` 返回的元素编号。浏览器空闲 600 秒后自动休眠，下一次操作会唤醒；用户数据和下载保留。向远程桌面粘贴中文时，可先将文本填入 noVNC 侧栏的剪贴板，再粘贴到远程输入框。

## 数据、重建与备份

用户数据保存在 Docker 卷 `ai-dev-<user>-data` 中，包括工作区、常用配置、SSH 信息和浏览器数据。重建容器会保留该数据卷和原 SSH 密码；删除数据卷会永久删除其中的数据。

准备好新镜像后，重建开发容器：

```bash
scripts/deploy.sh --user tony --port 30 --recreate
```

如果使用浏览器，并且也准备了与开发容器匹配的新浏览器镜像，接着重建浏览器容器：

```bash
scripts/browser.sh restart --user tony --base 30
```

需要远程访问时，开发容器重建命令也要加 `--bind 0.0.0.0`。同一用户只能有一个开发容器实例。浏览器和开发环境使用匹配的镜像版本；脚本检测到版本不一致时会拒绝启用浏览器。

备份或恢复前，在 Docker 主机上停止该用户的浏览器和开发容器：

```bash
scripts/browser.sh stop --user tony --base 30  # 已启用浏览器时执行
docker stop ai-dev-tony-3022
```

备份数据，路径需使用 Docker 主机上的绝对路径：

```bash
scripts/backup.sh --user tony --output /backup/tony
```

需要恢复时运行：

```bash
scripts/restore.sh --user tony --input /backup/tony --force
```

恢复会覆盖卷中同名文件；`--force` 允许恢复到非空数据卷，请确认目标后使用。

备份或恢复完成后，可以重新启动容器：

```bash
docker start ai-dev-tony-3022
scripts/browser.sh start --user tony --base 30  # 已启用浏览器时执行
```

## 常用说明

- 自定义 shell 配置写入 `~/.bashrc.extra`；直接修改 `~/.bashrc` 不会在重建后保留。
- 容器内 `dev` 可以使用 sudo 安装系统包，但系统包不会随数据卷保留。
- 自定义 s6 服务定义放在数据卷的 `s6-services/` 目录。
- 浏览器容器默认上限为 2 GB 内存和 2 CPU，可通过 `BROWSER_MEMORY`、`BROWSER_CPUS` 调整。
- 查看更多部署或浏览器命令：`scripts/deploy.sh --help`、`scripts/browser.sh --help`。

## License

MIT。`browser/third_party/page_browser.py` 来自 [rakazo](https://github.com/elie222/rakazo)，以 Apache-2.0 分发，详见该目录的 `LICENSE.rakazo` 与 `NOTICE`。
