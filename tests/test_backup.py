"""Backup and restore behavior using real tar and fixture-only Docker mounts."""

import hashlib
import os
from pathlib import Path
import tarfile
import unittest

from test_deployment import ScriptFixture


class BackupTest(ScriptFixture):
    def setUp(self):
        super().setUp()
        self.data = self.folder / "volume"
        self.data.mkdir()
        (self.data / "payload.txt").write_text("fixture-original", encoding="utf-8")
        self.output = self.folder / "backup folder"
        self.output.mkdir()
        self.archive = self.output / "ai-dev-alice-data.tar.gz"
        self.state["volumes"]["ai-dev-alice-data"] = str(self.data)
        self.write_state()

    def backup(self, **settings):
        return self.run_script("backup.sh", [
            "--user","alice","--output",str(self.output),"--image","fixture:tar",
        ], **settings)

    def restore(self, *extra, **settings):
        return self.run_script("restore.sh", [
            "--user","alice","--input",str(self.output),"--image","fixture:tar",*extra,
        ], **settings)

    def assert_no_temporary_archives(self):
        self.assertEqual(list(self.output.glob(".*.tmp.*")), [])

    def install_failing_tar(self, mode):
        script = "#!/bin/sh\n"
        if mode == "write":
            script += '/usr/bin/tar "$@"\ncase "$*" in *-czf*) exit 2 ;; esac\n'
        elif mode == "interrupt":
            script += '/usr/bin/tar "$@"\ncase "$*" in *-czf*) kill -TERM "$PPID"; exit 143 ;; esac\n'
        elif mode == "check":
            script += 'case "$*" in *-tzf*) exit 2 ;; esac\n/usr/bin/tar "$@"\n'
        path = self.bin / "tar"
        path.write_text(script)
        path.chmod(0o755)

    def test_backup_and_restore_preserve_contents(self):
        result = self.backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.archive, "r:gz") as archive:
            self.assertEqual(archive.extractfile("./payload.txt").read(), b"fixture-original")
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.archive.stat().st_uid, os.geteuid())
        (self.data / "payload.txt").write_text("changed")
        result = self.restore("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.data / "payload.txt").read_text(), "fixture-original")
        self.assert_no_temporary_archives()

    def test_failed_write_preserves_previous_archive(self):
        self.assertEqual(self.backup().returncode, 0)
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        (self.data / "payload.txt").write_text("new-data")
        self.install_failing_tar("write")
        result = self.backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), digest)
        self.assert_no_temporary_archives()

    def test_failed_validation_preserves_previous_archive(self):
        self.assertEqual(self.backup().returncode, 0)
        old = self.archive.read_bytes()
        self.install_failing_tar("check")
        result = self.backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.archive.read_bytes(), old)
        self.assert_no_temporary_archives()

    def test_interruption_preserves_previous_archive(self):
        self.assertEqual(self.backup().returncode, 0)
        old = self.archive.read_bytes()
        self.install_failing_tar("interrupt")
        result = self.backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.archive.read_bytes(), old)
        self.assert_no_temporary_archives()

    def test_nonempty_restore_requires_force(self):
        self.assertEqual(self.backup().returncode, 0)
        result = self.restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not empty", result.stderr)
        self.assertEqual((self.data / "payload.txt").read_text(), "fixture-original")

    def test_restore_corrupt_archive_changes_nothing(self):
        self.archive.write_bytes(b"invalid gzip fixture")
        result = self.restore("--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreadable", result.stderr)
        self.assertEqual((self.data / "payload.txt").read_text(), "fixture-original")
        self.assertFalse(any(c["args"][:2] == ["volume","create"] for c in self.state["calls"]))

    def test_backup_and_restore_refuse_volume_in_use(self):
        self.add_container()
        self.archive.write_bytes(b"not reached")
        for result in (self.backup(), self.restore("--force")):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("still running", result.stderr)
        self.assertFalse(any(c["args"][0] == "run" for c in self.state["calls"]))

    def test_all_helpers_disable_network_and_path_conversion(self):
        self.assertEqual(self.backup().returncode, 0)
        calls = self.state["calls"]
        helpers = [c for c in calls if c["args"][0] == "run"]
        self.assertTrue(helpers)
        for c in helpers:
            args = c["args"]
            self.assertEqual(args[args.index("--network")+1], "none")
            self.assertEqual(c["pathconv"], "1")
        self.assertTrue(any("ai-dev-alice-data:/data:ro" in c["args"] for c in helpers))
        self.assertFalse(any(str(self.output)+":/backup" in c["args"] for c in helpers))


if __name__ == "__main__":
    unittest.main()
