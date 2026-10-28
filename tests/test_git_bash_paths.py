"""Verify publishing uses a Windows-compatible Git path in Git Bash."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"


@unittest.skipUnless(os.name == "nt" and GIT_BASH.is_file(), "requires Windows Git Bash")
class GitBashBuildTest(unittest.TestCase):
    def test_publish_from_windows_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="git bash build ") as temp:
            folder = Path(temp)
            repo = folder / "project with spaces"
            (repo / "scripts").mkdir(parents=True)
            for name in ("build.sh", "lib.sh"):
                shutil.copyfile(ROOT / "scripts" / name, repo / "scripts" / name)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)

            git("init", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("add", "scripts")
            git("commit", "-m", "fixture")
            remote = folder / "origin.git"
            subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
            git("remote", "add", "origin", str(remote))
            git("push", "origin", "main")

            fake_bin = folder / "bin"
            fake_bin.mkdir()
            (fake_bin / "docker").write_text(
                '#!/usr/bin/env bash\n'
                'case "$1 $2" in\n'
                '  "image inspect") exit 1 ;;\n'
                '  "manifest inspect") echo "no such manifest: fixture" >&2; exit 1 ;;\n'
                'esac\n'
                'printf "docker %s\\n" "$*" >> "$FIXTURE_LOG"\n', encoding="utf-8", newline="\n")
            (fake_bin / "gh").write_text(
                '#!/usr/bin/env bash\nprintf "gh %s\\n" "$*" >> "$FIXTURE_LOG"\n',
                encoding="utf-8", newline="\n")
            log = folder / "calls.log"
            env = dict(os.environ, REGISTRY="fixture/dev", BROWSER_REGISTRY="fixture/browser",
                       SKIP_VENDOR="1", AI_DEV_VERSION_STATE_DIR=(folder / "state").as_posix())
            for name in ("VERSION", "RELEASE_SERIES", "RELEASE_BASE", "IMAGE_GIT_REMOTE"):
                env.pop(name, None)
            launcher = ('export PATH="$(cygpath -u "$1"):$PATH"; '
                        'export FIXTURE_LOG="$(cygpath -u "$2")"; '
                        'shift 2; exec bash "$@"')
            result = subprocess.run([str(GIT_BASH), "-c", launcher, "fixture", fake_bin.as_posix(),
                                     log.as_posix(), (repo / "scripts/build.sh").as_posix(),
                                     "--with-browser", "--push"],
                                    cwd=repo, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("image version 0.1.1", result.stdout)
            calls = log.read_text(encoding="utf-8")
            machine = subprocess.run([str(GIT_BASH), "-c", "uname -m"], capture_output=True,
                                     text=True, check=True).stdout.strip()
            arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[machine]
            self.assertIn(f"fixture/dev:0.1.1-{arch}", calls)
            self.assertIn(f"fixture/browser:0.1.1-{arch}", calls)
            self.assertIn("gh workflow run image-release.yml", calls)


if __name__ == "__main__":
    unittest.main()
