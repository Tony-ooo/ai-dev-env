---
name: s6-service-manager
description: 将任意前台进程封装为 s6 或 s6-overlay longrun 服务并接入守护进程管理。Use for creating an s6 run script, converting a nohup/background process into a supervised foreground service, adding a service to /run/service, creating /etc/s6-overlay/s6-rc.d service definitions, validating s6 status, or documenting/managing start/stop/restart commands for s6-supervise/s6-overlay.
---

# S6 Service Manager

## 目标

把用户给出的任意可前台运行命令转换为 s6 `run` 脚本，并按当前系统形态接入 s6 监督。优先做最小、可验证、可回滚的变更。

## 核心判断

先收集这些信息：

- 服务名：以字母或数字开头，只使用字母、数字、点、下划线和连字符，不能包含 `/`。
- 前台命令：必须是不会自行 daemonize 的命令，例如 `./app --config app.toml`。
- 工作目录：优先使用绝对路径。
- s6 形态：检查 `/command/s6-svc`、`/command/s6-svstat`、`/command/s6-svscanctl`、`/etc/s6-overlay/s6-rc.d`、`/run/service`。
- 权限：系统目录通常需要 root 或 sudo。

不要把 secret、token、password、private key 等敏感值写进 `run` 脚本或命令行；使用已有配置文件或 s6 envdir 时只引用路径，不读取敏感内容。

## 标准流程

1. 把启动方式改为前台执行。
   - 删除 `nohup`、后台 `&`、`daemon`、`disown`。
   - 不把 stdout/stderr 重定向到 `/dev/null`，让 s6/容器日志接管输出。
   - 最后一行使用 `exec command args...`。

2. 创建 `run` 脚本。

```sh
#!/command/with-contenv sh
set -eu

cd /absolute/workdir
exec command arg1 arg2
```

如果系统没有 `/command/with-contenv`，使用：

```sh
#!/bin/sh
set -eu

cd /absolute/workdir
exec command arg1 arg2
```

3. 创建持久化 s6-overlay v3 服务定义。

```sh
sudo install -d -m 0755 /etc/s6-overlay/s6-rc.d/<service>
sudo install -m 0755 run /etc/s6-overlay/s6-rc.d/<service>/run
printf 'longrun\n' | sudo install -m 0644 /dev/stdin /etc/s6-overlay/s6-rc.d/<service>/type
sudo install -d -m 0755 /etc/s6-overlay/s6-rc.d/user/contents.d
sudo touch /etc/s6-overlay/s6-rc.d/user/contents.d/<service>
```

编译检查：

```sh
tmpdir=$(mktemp -d)
sudo /command/s6-rc-compile "$tmpdir/compiled" /etc/s6-overlay/s6-rc.d
sudo rm -rf "$tmpdir"
```

4. 接入当前运行中的 scan 目录。

如果 `/run/service` 存在，需要当前会话立即启动服务：

```sh
sudo install -d -m 0755 /run/service/.<service>.new
sudo install -m 0755 run /run/service/.<service>.new/run
sudo mv /run/service/.<service>.new /run/service/<service>
sudo /command/s6-svscanctl -a /run/service
```

如果 `/run/service/<service>` 已存在，只更新 `run` 后用：

```sh
sudo /command/s6-svc -t /run/service/<service>
```

5. 验证。

```sh
sudo /command/s6-svstat /run/service/<service>
pgrep -a <process-name> || true
```

期望看到 `up (pid ...)`。如果命令不在 `PATH`，优先使用 `/command/s6-*` 的完整路径；这在 s6-overlay 环境中是正常现象。

## 辅助脚本

优先使用 `scripts/create_s6_longrun.sh` 执行可重复流程。先 dry-run，确认将要写入的路径和命令正确，再正式执行。

示例：

```sh
skills/s6-service-manager/scripts/create_s6_longrun.sh \
  --dry-run \
  --name frpc \
  --workdir /home/dev/workspace/Project/personal-website/frp \
  -- ./frpc -c ./frpc.toml
```

正式执行：

```sh
skills/s6-service-manager/scripts/create_s6_longrun.sh \
  --name frpc \
  --workdir /home/dev/workspace/Project/personal-website/frp \
  -- ./frpc -c ./frpc.toml
```

## 常用管理命令

```sh
sudo /command/s6-svstat /run/service/<service>
sudo /command/s6-svc -t /run/service/<service>
sudo /command/s6-svc -d /run/service/<service>
sudo /command/s6-svc -u /run/service/<service>
```

## 注意事项

- `run` 脚本必须保持主进程在前台；s6 监督的是 `run` 脚本 `exec` 出来的进程。
- 不要用 `kill` 当作常规管理方式，优先使用 `s6-svc`。
- 修改已存在服务时，先检查是否有同名服务和同名进程，避免重复拉起。
- 对 s6-overlay v3，`/etc/s6-overlay/s6-rc.d` 是下次启动的持久化来源，`/run/service` 是当前运行时监督目录。
- 如果只创建了 `/etc/s6-overlay/s6-rc.d/<service>`，通常需要重启 s6-overlay 环境才会自动进入当前监督；要立即生效还要接入 `/run/service`。
