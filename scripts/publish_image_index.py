#!/usr/bin/env python3
"""Publish matching architecture images as immutable and latest indexes."""

import argparse
import json
import re
import subprocess
import sys


ARCHES = ("amd64", "arm64")
VERSION_LABEL = "org.opencontainers.image.version"
REVISION_LABEL = "org.opencontainers.image.revision"


def run(*args, missing_ok=False):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode:
        error = result.stderr.strip()
        if missing_ok and re.search(r"manifest unknown|no such manifest|not found", error, re.I):
            return None
        raise RuntimeError(f"{' '.join(args[:4])} failed: {error}")
    return result.stdout


def inspect(ref, missing_ok=False):
    text = run("docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", ref,
               missing_ok=missing_ok)
    if text is None:
        return None
    manifest = json.loads(text)
    digest = manifest.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError(f"cannot determine registry digest: {ref}")
    repo = ref.rsplit("@", 1)[0] if "@" in ref else ref.rsplit(":", 1)[0]
    pinned = f"{repo}@{digest}"
    configs = json.loads(run("docker", "buildx", "imagetools", "inspect", "--format", "{{json .Image}}", pinned))
    if "architecture" in configs:
        configs = {"single": configs}
    platforms = {}
    for config in configs.values():
        if not isinstance(config, dict) or config.get("os") != "linux" or config.get("architecture") not in ARCHES:
            continue
        arch = config["architecture"]
        if arch in platforms:
            raise RuntimeError(f"duplicate platform in {ref}: {arch}")
        labels = (config.get("config") or {}).get("Labels") or {}
        platforms[arch] = (labels.get(VERSION_LABEL), labels.get(REVISION_LABEL))
    descriptors = {}
    for item in manifest.get("manifests", []):
        platform = item.get("platform") or {}
        arch = platform.get("architecture")
        if platform.get("os") == "linux" and arch in ARCHES:
            if arch in descriptors:
                raise RuntimeError(f"duplicate manifest platform in {ref}: {arch}")
            descriptors[arch] = item.get("digest")
    if not manifest.get("manifests") and len(platforms) == 1:
        descriptors[next(iter(platforms))] = digest
    if set(descriptors) != set(platforms) or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", str(d)) for d in descriptors.values()):
        raise RuntimeError(f"inconsistent platforms in {ref}")
    return {"ref": pinned, "platforms": platforms, "descriptors": descriptors}


def main():
    parser = argparse.ArgumentParser()
    for name in ("version", "revision", "base", "series", "registry", "browser-registry"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+", args.series) or not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise RuntimeError("invalid release series or revision")
    run("git", "fetch", "origin", "main")
    history = run("git", "rev-list", "--first-parent", "origin/main").splitlines()
    if args.revision not in history:
        raise RuntimeError("release must be on main first-parent history")
    if args.base == "@root":
        offset = len(history) - history.index(args.revision)
    else:
        base = run("git", "rev-parse", "--verify", args.base + "^{commit}").strip()
        if base not in history:
            raise RuntimeError("release base must be on main first-parent history")
        offset = history.index(base) - history.index(args.revision)
    if offset < 1 or args.version != f"{args.series}.{offset}":
        raise RuntimeError("release version does not match its source commit")

    repos = (args.registry, args.browser_registry)
    if repos[0] == repos[1]:
        raise RuntimeError("dev and browser repositories must be distinct")
    candidates = {}
    for repo in repos:
        candidates[repo] = {}
        for arch in ARCHES:
            ref = f"{repo}:{args.version}-{arch}"
            image = inspect(ref, missing_ok=True)
            if image is None:
                print(f"Waiting for {ref}")
                return
            if image["platforms"] != {arch: (args.version, args.revision)}:
                raise RuntimeError(f"platform or source labels do not match: {ref}")
            candidates[repo][arch] = image

    existing = {}
    latest = {}
    newer_exists = False
    for repo in repos:
        expected = {arch: candidates[repo][arch]["descriptors"][arch] for arch in ARCHES}
        existing[repo] = inspect(f"{repo}:{args.version}", missing_ok=True)
        if existing[repo] and (existing[repo]["descriptors"] != expected or
                               existing[repo]["platforms"] != {arch: (args.version, args.revision) for arch in ARCHES}):
            raise RuntimeError(f"version tag already belongs to different images: {repo}:{args.version}")
        latest[repo] = inspect(f"{repo}:latest", missing_ok=True)
        if latest[repo]:
            values = set(latest[repo]["platforms"].values())
            if set(latest[repo]["platforms"]) != set(ARCHES) or len(values) != 1:
                raise RuntimeError(f"cannot safely compare latest: {repo}")
            _, published_revision = next(iter(values))
            if published_revision not in history:
                raise RuntimeError(f"latest revision is not on main: {repo}")
            if history.index(published_revision) < history.index(args.revision):
                newer_exists = True

    for repo in repos:
        if existing[repo] is None:
            run("docker", "buildx", "imagetools", "create", "-t", f"{repo}:{args.version}",
                *(candidates[repo][arch]["ref"] for arch in ARCHES))
            existing[repo] = inspect(f"{repo}:{args.version}")
            expected = {arch: candidates[repo][arch]["descriptors"][arch] for arch in ARCHES}
            if existing[repo]["descriptors"] != expected:
                raise RuntimeError(f"published index does not match source images: {repo}")
    if newer_exists:
        print("A newer release is already latest; version indexes published without changing latest")
        return
    for repo in repos:
        run("docker", "buildx", "imagetools", "create", "-t", f"{repo}:latest", existing[repo]["ref"])
    print(f"Published multi-architecture indexes for {args.version}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
