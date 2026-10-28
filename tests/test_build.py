"""Exercise image version allocation and publishing without a real registry."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["IMAGE_TEST_LOG"], "a", encoding="utf-8") as out:
    out.write(json.dumps(args) + "\n")
if args[:2] == ["image", "inspect"]:
    if "-f" in args:
        version = args[-1].rsplit(":", 1)[-1].rsplit("-", 1)[0]
        revision = os.environ.get("IMAGE_TEST_REVISION", "")
        if args[-1] in os.environ.get("IMAGE_TEST_CONFLICT", "").split(","):
            revision = "different-commit"
        print(version + "|" + revision)
        raise SystemExit(0)
    raise SystemExit(0 if args[-1] in os.environ.get("IMAGE_TEST_LOCAL", "").split(",") else 1)
if args[:2] == ["manifest", "inspect"]:
    if os.environ.get("IMAGE_TEST_REGISTRY_ERROR"):
        print("registry connection failed", file=sys.stderr)
        raise SystemExit(1)
    if args[-1] in os.environ.get("IMAGE_TEST_REMOTE", "").split(","):
        raise SystemExit(0)
    print("no such manifest: " + args[-1], file=sys.stderr)
    raise SystemExit(1)
if args[0] == "build" and os.environ.get("IMAGE_TEST_DIRTY_AFTER_BUILD"):
    with open(os.environ["IMAGE_TEST_DIRTY_AFTER_BUILD"], "a", encoding="utf-8") as out:
        out.write("changed during build")
if args[0] == "push" and args[-1] == os.environ.get("IMAGE_TEST_FAIL_PUSH"):
    print("fixture push failed", file=sys.stderr)
    raise SystemExit(1)
if args[0] == "push" and os.environ.get("IMAGE_TEST_DIRTY_AFTER_PUSH"):
    with open(os.environ["IMAGE_TEST_DIRTY_AFTER_PUSH"], "a", encoding="utf-8") as out:
        out.write("changed during push")
raise SystemExit(0)
'''


@unittest.skipUnless(BASH and os.name == "posix", "requires Linux Bash fixtures")
class BuildVersionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="image release ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(STUB, encoding="utf-8")
        docker.chmod(0o755)
        gh = self.bin / "gh"
        gh.write_text('#!/usr/bin/env python3\nimport json, os, sys\nwith open(os.environ["IMAGE_TEST_LOG"], "a", encoding="utf-8") as out: out.write(json.dumps(["gh", *sys.argv[1:], "cwd=" + os.getcwd()]) + "\\n")\nif sys.argv[1:3] == ["workflow", "run"] and os.environ.get("IMAGE_TEST_NOTIFY_FAIL"): sys.exit(1)\n', encoding="utf-8")
        gh.chmod(0o755)
        uname = self.bin / "uname"
        uname.write_text('#!/bin/sh\nif [ "$1" = -m ]; then echo "${IMAGE_TEST_MACHINE:-' + platform.machine() + '}"; else /usr/bin/uname "$@"; fi\n', encoding="utf-8")
        uname.chmod(0o755)
        self.repo = self.folder / "repo"
        self.repo.mkdir()
        (self.repo / "scripts").mkdir()
        for name in ("build.sh", "lib.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.repo / "scripts" / name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", "scripts")
        self.git("commit", "-m", "baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "fixture.txt").write_text("first", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "first")
        remote = self.folder / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "main")
        self.state = self.folder / "state with spaces"
        self.log = self.folder / "calls.jsonl"
        self.arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        AI_DEV_VERSION_STATE_DIR=str(self.state), IMAGE_TEST_LOG=str(self.log),
                        REGISTRY="fixture/dev", BROWSER_REGISTRY="fixture/browser", SKIP_VENDOR="1",
                        RELEASE_BASE=self.base, IMAGE_TEST_REVISION=self.git("rev-parse", "HEAD").stdout.strip())
        for name in ("VERSION", "RELEASE_SERIES", "IMAGE_GIT_REMOTE"):
            self.env.pop(name, None)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True)

    def next_commit(self, push=True):
        (self.repo / "fixture.txt").write_text("second", encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "second")
        self.env["IMAGE_TEST_REVISION"] = self.git("rev-parse", "HEAD").stdout.strip()
        if push:
            self.git("push", "origin", "main")

    def run_build(self, *args, **settings):
        return subprocess.run([BASH, str(self.repo / "scripts/build.sh"), *args],
                              env=dict(self.env, **settings), capture_output=True, text=True, timeout=20)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_local_build_uses_latest_without_consuming_version(self):
        result = self.run_build("--with-browser")
        self.assertEqual(result.returncode, 0, result.stderr)
        tags = [a[a.index("-t") + 1] for a in self.calls() if a[0] == "build"]
        self.assertEqual(tags, ["fixture/dev:latest", "fixture/browser:latest"])
        self.assertFalse(self.state.exists())
        self.assertFalse(any(a[0] == "push" for a in self.calls()))

    def test_local_build_accepts_explicit_version(self):
        result = self.run_build(VERSION="0.1.7")
        self.assertEqual(result.returncode, 0, result.stderr)
        tags = [a[a.index("-t") + 1] for a in self.calls() if a[0] == "build"]
        self.assertEqual(tags, ["fixture/dev:0.1.7"])

    def test_first_commit_can_be_released_with_automatic_root(self):
        self.git("checkout", "--detach", self.base)
        self.git("push", "--force", "origin", self.base + ":main")
        self.env.pop("RELEASE_BASE")
        self.env["IMAGE_TEST_REVISION"] = self.base
        result = self.run_build("--push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("image version 0.1.1", result.stdout)
        notification = next(a for a in self.calls() if a[:3] == ["gh", "workflow", "run"])
        self.assertIn("base=@root", notification)

    def test_automatic_root_increments_for_subsequent_commit(self):
        self.env.pop("RELEASE_BASE")
        result = self.run_build("--push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("image version 0.1.2", result.stdout)

    def test_commits_increment_and_publish_matching_images_before_latest(self):
        for version in ("0.1.1", "0.1.2"):
            if version == "0.1.2":
                self.next_commit()
            result = self.run_build("--with-browser", "--push")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("image version " + version, result.stdout)
        pushes = [a[-1] for a in self.calls() if a[0] == "push"]
        expected = []
        for version in ("0.1.1", "0.1.2"):
            expected.extend([f"fixture/dev:{version}-{self.arch}", f"fixture/browser:{version}-{self.arch}",
                             f"fixture/dev:latest-{self.arch}", f"fixture/browser:latest-{self.arch}"])
        self.assertEqual(pushes, expected)
        self.assertFalse((self.state / f"image-release-{self.arch}.lock").exists())
        revision = self.git("rev-parse", "HEAD").stdout.strip()
        builds = [a for a in self.calls() if a[0] == "build"]
        self.assertIn("org.opencontainers.image.revision=" + revision, builds[-1])
        notifications = [a for a in self.calls() if a[:3] == ["gh", "workflow", "run"]]
        self.assertEqual(len(notifications), 2)
        self.assertIn("version=0.1.2", notifications[-1])
        self.assertIn("revision=" + revision, notifications[-1])
        self.assertIn("cwd=" + str(self.repo), notifications[-1])

    def test_notification_failure_reports_retry_after_push(self):
        result = self.run_build("--push", IMAGE_TEST_NOTIFY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("images were pushed", result.stderr)
        self.assertTrue(any(a[0] == "push" for a in self.calls()))

    def test_same_commit_has_same_version_across_architectures(self):
        for machine in ("x86_64", "aarch64"):
            result = self.run_build("--with-browser", "--push", IMAGE_TEST_MACHINE=machine)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("image version 0.1.1", result.stdout)
        pushes = [a[-1] for a in self.calls() if a[0] == "push"]
        for arch in ("amd64", "arm64"):
            self.assertIn(f"fixture/dev:0.1.1-{arch}", pushes)
            self.assertIn(f"fixture/browser:0.1.1-{arch}", pushes)

    def test_existing_remote_or_local_tag_is_not_overwritten(self):
        for setting in ("IMAGE_TEST_LOCAL", "IMAGE_TEST_REMOTE"):
            with self.subTest(setting=setting):
                image = f"fixture/dev:0.1.1-{self.arch}"
                result = self.run_build("--with-browser", "--push", IMAGE_TEST_CONFLICT=image, **{setting: image})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("another release", result.stderr)
                self.assertFalse(any(a[0] in ("build", "push") for a in self.calls()))

    def test_registry_failure_does_not_publish(self):
        result = self.run_build("--push", IMAGE_TEST_REGISTRY_ERROR="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot determine", result.stderr)
        self.assertFalse(any(a[0] in ("build", "push") for a in self.calls()))
        self.assertFalse((self.state / f"image-release-{self.arch}.lock").exists())

    def test_partial_push_reuses_the_published_image_on_retry(self):
        failed = f"fixture/browser:0.1.1-{self.arch}"
        result = self.run_build("--with-browser", "--push", IMAGE_TEST_FAIL_PUSH=failed)
        self.assertNotEqual(result.returncode, 0)
        pushes = [a[-1] for a in self.calls() if a[0] == "push"]
        self.assertFalse(any(":latest-" in name for name in pushes))
        existing = f"fixture/dev:0.1.1-{self.arch}"
        result = self.run_build("--with-browser", "--push", IMAGE_TEST_REMOTE=existing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using published", result.stdout)
        all_pushes = [a[-1] for a in self.calls() if a[0] == "push"]
        self.assertEqual(all_pushes.count(existing), 1)
        self.assertEqual(all_pushes[-2:], [f"fixture/dev:latest-{self.arch}",
                                          f"fixture/browser:latest-{self.arch}"])

    def test_existing_lock_stops_release(self):
        (self.state / f"image-release-{self.arch}.lock").mkdir(parents=True)
        result = self.run_build("--push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock", result.stderr)
        self.assertFalse(any(a[0] in ("build", "push") for a in self.calls()))

    def test_dirty_or_unpushed_source_stops_release(self):
        (self.repo / "fixture.txt").write_text("dirty", encoding="utf-8")
        result = self.run_build("--push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean Git working tree", result.stderr)
        self.git("checkout", "--", "fixture.txt")
        self.next_commit(push=False)
        result = self.run_build("--push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote main first-parent history", result.stderr)
        self.assertFalse(any(a[0] in ("build", "push") for a in self.calls()))

    def test_source_changed_during_build_is_not_published(self):
        result = self.run_build("--push", IMAGE_TEST_DIRTY_AFTER_BUILD=str(self.repo / "fixture.txt"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean Git working tree", result.stderr)
        self.assertTrue(any(a[0] == "build" for a in self.calls()))
        self.assertFalse(any(a[0] == "push" for a in self.calls()))

    def test_source_changed_during_push_does_not_update_latest(self):
        result = self.run_build("--push", IMAGE_TEST_DIRTY_AFTER_PUSH=str(self.repo / "fixture.txt"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean Git working tree", result.stderr)
        pushed = [a[-1] for a in self.calls() if a[0] == "push"]
        self.assertEqual(len(pushed), 1)
        self.assertTrue(pushed[0].endswith(f":0.1.1-{self.arch}"))

    def test_historical_main_commit_does_not_move_latest(self):
        first = self.git("rev-parse", "HEAD").stdout.strip()
        self.next_commit()
        self.git("checkout", "--detach", first)
        self.env["IMAGE_TEST_REVISION"] = first
        result = self.run_build("--with-browser", "--push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("image version 0.1.1", result.stdout)
        self.assertIn("latest aliases unchanged", result.stdout)
        pushes = [a[-1] for a in self.calls() if a[0] == "push"]
        self.assertEqual(pushes, [f"fixture/dev:0.1.1-{self.arch}",
                                  f"fixture/browser:0.1.1-{self.arch}"])

    def test_local_http_registry_is_checked_as_insecure(self):
        result = self.run_build("--push", REGISTRY="127.0.0.1:5000/dev")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifests = [a for a in self.calls() if a[:2] == ["manifest", "inspect"]]
        self.assertTrue(manifests)
        self.assertIn("--insecure", manifests[0])

    def test_invalid_series_and_version_override_fail_before_build(self):
        for settings in ({"RELEASE_SERIES": "0.1.2"}, {"VERSION": "0.1.2"}):
            with self.subTest(settings=settings):
                result = self.run_build("--push", **settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(a[0] in ("build", "push") for a in self.calls()))


if __name__ == "__main__":
    unittest.main()
