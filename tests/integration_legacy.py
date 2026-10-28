#!/usr/bin/env python3
"""Verify migration from a real old image without touching existing deployments."""

import argparse
import tempfile

from integration import Acceptance


def run(args):
    suite = Acceptance(argparse.Namespace(dev_image=args.old_image, browser_image="unused", output=args.output))

    def replace_configs():
        suite.py("from pathlib import Path\n"
                 "p=Path('/home/dev/.gitconfig');q=p.with_suffix('.new');q.write_text('[audit]\\n value=legacy\\n');q.replace(p)\n"
                 "p=Path('/home/dev/.bashrc.extra');q=p.with_suffix('.new');q.write_text('');q.replace(p)\n"
                 "p=Path('/home/dev/.config');p.unlink();p.mkdir();(p/'legacy.txt').write_text('directory retained')\n"
                 "p=Path('/home/dev/.claude');p.unlink();p.mkdir();(p/'skills').symlink_to('/home/dev/.agents/skills')")

    def assert_configs():
        assert suite.py("from pathlib import Path;print(Path('/home/dev/.gitconfig').read_text())") == "[audit]\n value=legacy"
        assert suite.py("from pathlib import Path;print(Path('/home/dev/.bashrc.extra').stat().st_size)") == "0"
        assert suite.py("from pathlib import Path;print(Path('/home/dev/.config/legacy.txt').read_text())") == "directory retained"
        assert suite.py("from pathlib import Path;s=Path('/home/dev/.config/legacy.txt').stat();print(s.st_uid,s.st_gid,oct(s.st_mode & 0o777))") == "1000 1000 0o644"
        assert suite.py("import os;print(os.readlink('/home/dev/.claude/skills'))") == "/home/dev/.agents/skills"

    def copy_failure():
        suite.py("from pathlib import Path;p=Path('/usr/local/bin/cp');p.write_text('#!/bin/sh\\nexit 1\\n');p.chmod(0o755)", root=True)
        original = suite.docker("inspect", "-f", "{{.Id}}", suite.dev).stdout.strip()
        result = suite.script("deploy.sh", "--user", suite.user, "--port", suite.base,
                              "--image", args.new_image, "--recreate", "--no-gpu", check=False)
        assert result.returncode != 0
        assert suite.docker("inspect", "-f", "{{.Id}}", suite.dev).stdout.strip() == original
        assert suite.docker("inspect", "-f", "{{.State.Running}}", suite.dev).stdout.strip() == "true"
        assert_configs()
        suite.py("from pathlib import Path;Path('/usr/local/bin/cp').unlink()", root=True)

    def migrate():
        suite.script("deploy.sh", "--user", suite.user, "--port", suite.base,
                     "--image", args.new_image, "--recreate", "--no-gpu", "--cpu", "2", "--memory", "1g")
        assert_configs()
        assert suite.py("from pathlib import Path;print(Path('/home/dev/.gitconfig').is_symlink())") == "True"

    def stopped_fixed():
        suite.py("from pathlib import Path;p=Path('/home/dev/.gitconfig');q=p.with_suffix('.new');q.write_text('[audit]\\n value=legacy\\n');q.replace(p)")
        suite.docker("stop", suite.dev)
        suite.script("deploy.sh", "--user", suite.user, "--port", suite.base,
                     "--image", args.new_image, "--recreate", "--no-gpu", "--cpu", "2", "--memory", "1g")
        assert_configs()

    try:
        suite.record("old image real entrypoint", suite.deploy)
        suite.record("old image atomic configuration fixture", replace_configs)
        if args.stopped:
            suite.docker("stop", suite.dev)
            suite.record("old stopped image offline migration preserves files directories and owners", migrate)
        else:
            suite.record("old migration copy failure preserves original container and files", copy_failure)
            suite.record("old running image migration preserves regular empty and directory state", migrate)
        suite.record("stopped fixed container safely recreates", stopped_fixed)
    finally:
        suite.cleanup()
        suite.write_results()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-image", required=True)
    parser.add_argument("--new-image", required=True)
    parser.add_argument("--stopped", action="store_true")
    parser.add_argument("--output", default=None,
                        help="directory for the results (default: a new temp directory)")
    args = parser.parse_args()
    if args.output is None:
        args.output = tempfile.mkdtemp(prefix="ai-dev-acceptance-legacy-")
    run(args)


if __name__ == "__main__":
    main()
