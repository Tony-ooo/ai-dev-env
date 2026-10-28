---
name: s6-service-manager
description: Wrap any foreground process as an s6 or s6-overlay longrun service and attach it to the supervisor. Use for creating an s6 run script, converting a nohup/background process into a supervised foreground service, adding a service to /run/service, creating /etc/s6-overlay/s6-rc.d service definitions, validating s6 status, or documenting/managing start/stop/restart commands for s6-supervise/s6-overlay.
---

# S6 Service Manager

## Goal

Turn any command the user can run in the foreground into an s6 `run` script, and attach
it to the supervisor that matches the system. Prefer the smallest change that can be
verified and rolled back.

## What to determine first

- Service name: starts with a letter or digit, uses only letters, digits, dot,
  underscore and hyphen, and must not contain `/`.
- Run user: `dev` by default. Use `--run-as root` only for services that genuinely
  need privileges; running as root leaves root-owned files in the workspace.
- Foreground command: must not daemonize itself, e.g. `./app --config app.toml`.
- Working directory: an absolute path is preferred.
- s6 flavour: check `/command/s6-svc`, `/command/s6-svstat`,
  `/command/s6-svscanctl`, `/etc/s6-overlay/s6-rc.d` and `/run/service`.
- Permissions: system directories usually need root or sudo.

Never write secrets, tokens, passwords or private keys into the `run` script or its
command line. When an existing config file or an s6 envdir holds them, reference the
path only and do not read the sensitive values.

## Standard procedure

1. Convert the start command to a foreground process.
   - Remove `nohup`, background `&`, `daemon` and `disown`.
   - Do not redirect stdout/stderr to `/dev/null`; let s6 and the container log the output.
   - Make the last line `exec command args...`.

2. Create the `run` script.

```sh
#!/command/with-contenv sh
set -eu

cd /absolute/workdir
exec /command/s6-setuidgid dev command arg1 arg2
```

Running as `dev` (`s6-setuidgid dev`) is the default, so files the service writes under
`workspace` belong to `dev` and the user can edit them directly, with no root-owned
files left behind. Drop `s6-setuidgid`, or pass `--run-as root`, only when the service
really needs root (for example, binding a port below 1024).

If `/command/with-contenv` does not exist, use:

```sh
#!/bin/sh
set -eu

cd /absolute/workdir
exec s6-setuidgid dev command arg1 arg2
```

3. Create a persistent s6-overlay v3 service definition.

```sh
sudo install -d -m 0755 /etc/s6-overlay/s6-rc.d/<service>
sudo install -m 0755 run /etc/s6-overlay/s6-rc.d/<service>/run
printf 'longrun\n' | sudo install -m 0644 /dev/stdin /etc/s6-overlay/s6-rc.d/<service>/type
sudo install -d -m 0755 /etc/s6-overlay/s6-rc.d/user/contents.d
sudo touch /etc/s6-overlay/s6-rc.d/user/contents.d/<service>
```

Check that it compiles:

```sh
tmpdir=$(mktemp -d)
sudo /command/s6-rc-compile "$tmpdir/compiled" /etc/s6-overlay/s6-rc.d
sudo rm -rf "$tmpdir"
```

4. Attach it to the running scan directory.

If `/run/service` exists and the service must start in the current session:

```sh
sudo install -d -m 0755 /run/service/.<service>.new
sudo install -m 0755 run /run/service/.<service>.new/run
sudo mv /run/service/.<service>.new /run/service/<service>
sudo /command/s6-svscanctl -a /run/service
```

If `/run/service/<service>` already exists, update `run` and signal it:

```sh
sudo /command/s6-svc -t /run/service/<service>
```

5. Verify.

```sh
sudo /command/s6-svstat /run/service/<service>
pgrep -a <process-name> || true
```

Expect `up (pid ...)`. If a command is not on `PATH`, use its full `/command/s6-*`
path; that is normal in an s6-overlay environment.

## Helper script

Prefer `scripts/create_s6_longrun.sh` for the repeatable part. Dry-run first to confirm
the paths and commands it will write, then run it for real.

Dry run:

```sh
~/.agents/skills/s6-service-manager/scripts/create_s6_longrun.sh \
  --dry-run \
  --name frpc \
  --workdir /home/dev/workspace/Project/personal-website/frp \
  -- ./frpc -c ./frpc.toml
```

Real run:

```sh
~/.agents/skills/s6-service-manager/scripts/create_s6_longrun.sh \
  --name frpc \
  --workdir /home/dev/workspace/Project/personal-website/frp \
  -- ./frpc -c ./frpc.toml
```

## Common management commands

```sh
sudo /command/s6-svstat /run/service/<service>
sudo /command/s6-svc -t /run/service/<service>
sudo /command/s6-svc -d /run/service/<service>
sudo /command/s6-svc -u /run/service/<service>
```

## Notes

- The `run` script must keep its main process in the foreground; s6 supervises the
  process that `run` execs.
- Do not treat `kill` as normal management; prefer `s6-svc`.
- Before modifying an existing service, check for a service and a process with the same
  name so you do not start a duplicate.
- With s6-overlay v3, `/etc/s6-overlay/s6-rc.d` is the persistent source for the next
  boot and `/run/service` is the supervisor directory for the current run.
- In this image `/etc/s6-overlay/s6-rc.d` is a symlink to `s6-services/` in the data
  volume, so writing through it behaves like a normal directory.
- That directory is owned by `dev`, so creating or editing definitions needs no `sudo`.
  `/run/service`, `s6-svscanctl` and `s6-svc` do need root, so activating a service
  requires `sudo`.
- Definitions are validated before the container starts: one that lacks `type`/`run`,
  has an illegal name, or fails the test compile is moved to `.disabled-by-guard/` with
  a `disabled-reason.txt` instead of blocking startup.
- Creating only `/etc/s6-overlay/s6-rc.d/<service>` usually requires restarting the
  s6-overlay environment to enter supervision; attaching it to `/run/service` is what
  makes it take effect immediately.
