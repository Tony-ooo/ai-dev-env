#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  create_s6_longrun.sh --name SERVICE [options] -- COMMAND [ARG...]

Options:
  --name NAME              Service name. Required.
  --workdir DIR            Directory to cd into before exec.
  --source-dir DIR         s6-overlay source dir. Default: /etc/s6-overlay/s6-rc.d
  --bundle NAME            s6-overlay bundle name. Default: user
  --runtime-dir DIR        Active s6 scan dir. Default: /run/service
  --run-as USER            Prefix command with s6-setuidgid USER.
  --dry-run                Print actions without changing the system.
  --no-persistent          Do not write /etc/s6-overlay/s6-rc.d definitions.
  --no-runtime             Do not attach service to /run/service.
  --skip-compile           Skip s6-rc-compile validation.
  -h, --help               Show this help.

Example:
  create_s6_longrun.sh --name frpc --workdir /opt/frp -- ./frpc -c ./frpc.toml
USAGE
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '%s\n' "$*" >&2
}

quote_arg() {
  if [ "$#" -ne 1 ]; then
    die "quote_arg expects exactly one argument"
  fi
  case "$1" in
    *[!A-Za-z0-9_./:=+,%@-]*|'')
      printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
      ;;
    *)
      printf '%s' "$1"
      ;;
  esac
}

join_quoted() {
  local first=1
  local arg
  for arg in "$@"; do
    if [ "$first" -eq 0 ]; then
      printf ' '
    fi
    quote_arg "$arg"
    first=0
  done
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

root_cmd_prefix=()
dry_run=0

run_cmd() {
  if [ "$dry_run" -eq 1 ]; then
    printf '+'
    if [ "${#root_cmd_prefix[@]}" -gt 0 ]; then
      printf ' %q' "${root_cmd_prefix[@]}"
    fi
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "${root_cmd_prefix[@]}" "$@"
}

write_run_script() {
  local target="$1"
  local shebang="#!/bin/sh"
  local command_line

  if [ -x /command/with-contenv ]; then
    shebang="#!/command/with-contenv sh"
  fi

  command_line="$(join_quoted "${command_argv[@]}")"

  {
    printf '%s\n' "$shebang"
    printf 'set -eu\n'
    printf '\n'
    if [ -n "$workdir" ]; then
      printf 'cd %s\n' "$(quote_arg "$workdir")"
    fi
    if [ -n "$run_as" ]; then
      if [ -x /command/s6-setuidgid ]; then
        printf 'exec /command/s6-setuidgid %s %s\n' "$(quote_arg "$run_as")" "$command_line"
      else
        printf 'exec s6-setuidgid %s %s\n' "$(quote_arg "$run_as")" "$command_line"
      fi
    else
      printf 'exec %s\n' "$command_line"
    fi
  } >"$target"
  chmod 0755 "$target"
}

service_name=""
workdir=""
source_dir="/etc/s6-overlay/s6-rc.d"
bundle="user"
runtime_dir="/run/service"
run_as=""
write_persistent=1
attach_runtime=1
skip_compile=0
command_argv=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name)
      [ "$#" -ge 2 ] || die "--name requires a value"
      service_name="$2"
      shift 2
      ;;
    --workdir)
      [ "$#" -ge 2 ] || die "--workdir requires a value"
      workdir="$2"
      shift 2
      ;;
    --source-dir)
      [ "$#" -ge 2 ] || die "--source-dir requires a value"
      source_dir="$2"
      shift 2
      ;;
    --bundle)
      [ "$#" -ge 2 ] || die "--bundle requires a value"
      bundle="$2"
      shift 2
      ;;
    --runtime-dir)
      [ "$#" -ge 2 ] || die "--runtime-dir requires a value"
      runtime_dir="$2"
      shift 2
      ;;
    --run-as)
      [ "$#" -ge 2 ] || die "--run-as requires a value"
      run_as="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --no-persistent)
      write_persistent=0
      shift
      ;;
    --no-runtime)
      attach_runtime=0
      shift
      ;;
    --skip-compile)
      skip_compile=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      command_argv=("$@")
      break
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[ -n "$service_name" ] || die "--name is required"
[ "${#command_argv[@]}" -gt 0 ] || die "command is required after --"

case "$service_name" in
  */*|'') die "service name must not be empty or contain /" ;;
esac
case "$service_name" in
  [A-Za-z0-9]*)
    ;;
  *)
    die "service name must start with a letter or digit"
    ;;
esac
case "$service_name" in
  *[!A-Za-z0-9_.-]*)
    die "service name may only contain letters, digits, dot, underscore, and hyphen"
    ;;
esac

if [ -n "$workdir" ]; then
  [ -d "$workdir" ] || die "workdir does not exist: $workdir"
  workdir="$(cd "$workdir" && pwd -P)"
fi

if [ "$(id -u)" -ne 0 ]; then
  if have_cmd sudo; then
    root_cmd_prefix=(sudo)
  else
    die "root privileges are required and sudo was not found"
  fi
fi

tmpdir="$(mktemp -d)"
cleanup() {
  if [ -d "$tmpdir" ]; then
    if [ "${#root_cmd_prefix[@]}" -gt 0 ]; then
      "${root_cmd_prefix[@]}" rm -rf "$tmpdir"
    else
      rm -rf "$tmpdir"
    fi
  fi
}
trap cleanup EXIT

run_file="$tmpdir/run"
type_file="$tmpdir/type"
write_run_script "$run_file"
printf 'longrun\n' >"$type_file"

log "Generated run script:"
sed 's/^/  /' "$run_file" >&2

if [ "$write_persistent" -eq 1 ]; then
  service_dir="$source_dir/$service_name"
  contents_dir="$source_dir/$bundle/contents.d"

  run_cmd install -d -m 0755 "$service_dir"
  run_cmd install -m 0755 "$run_file" "$service_dir/run"
  run_cmd install -m 0644 "$type_file" "$service_dir/type"
  run_cmd install -d -m 0755 "$contents_dir"
  run_cmd touch "$contents_dir/$service_name"
  run_cmd chmod 0644 "$contents_dir/$service_name"

  if [ "$skip_compile" -eq 0 ]; then
    if [ -x /command/s6-rc-compile ]; then
      compiled_dir="$tmpdir/compiled"
      run_cmd /command/s6-rc-compile "$compiled_dir" "$source_dir"
    else
      log "warning: /command/s6-rc-compile not found; skipped compile validation"
    fi
  fi
fi

if [ "$attach_runtime" -eq 1 ]; then
  if [ ! -d "$runtime_dir" ]; then
    die "runtime dir does not exist: $runtime_dir"
  fi

  runtime_service_dir="$runtime_dir/$service_name"
  if [ -e "$runtime_service_dir" ]; then
    run_cmd install -m 0755 "$run_file" "$runtime_service_dir/run"
    if [ -x /command/s6-svc ]; then
      run_cmd /command/s6-svc -t "$runtime_service_dir"
    else
      log "warning: /command/s6-svc not found; updated run script but did not restart"
    fi
  else
    tmp_runtime_dir="$runtime_dir/.$service_name.new.$$"
    run_cmd install -d -m 0755 "$tmp_runtime_dir"
    run_cmd install -m 0755 "$run_file" "$tmp_runtime_dir/run"
    run_cmd mv "$tmp_runtime_dir" "$runtime_service_dir"
    if [ -x /command/s6-svscanctl ]; then
      run_cmd /command/s6-svscanctl -a "$runtime_dir"
    else
      log "warning: /command/s6-svscanctl not found; service may start when svscan rescans"
    fi
  fi

  if [ -x /command/s6-svstat ]; then
    run_cmd /command/s6-svstat "$runtime_service_dir"
  else
    log "warning: /command/s6-svstat not found; skipped status check"
  fi
fi
