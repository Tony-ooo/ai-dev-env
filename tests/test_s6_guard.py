"""Compile disposable service graphs with the actual s6 compiler."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "src" / "s6-guard.sh"
COMPILER = Path("/command/s6-rc-compile")


@unittest.skipUnless(COMPILER.exists(), "requires s6 tool image")
class ServiceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "base"
        self.source = self.root / "source"
        self.base.mkdir()
        self.source.mkdir()
        for name in ("user", "user2"):
            self.bundle(self.base, name)
        shutil.copytree(self.base, self.source, dirs_exist_ok=True)

    @staticmethod
    def bundle(root, name, contents=()):
        directory = root / name
        directory.mkdir(exist_ok=True)
        (directory / "type").write_text("bundle\n")
        (directory / "contents.d").mkdir(exist_ok=True)
        for member in contents:
            (directory / "contents.d" / member).touch()

    def service(self, name, dependencies=(), enabled=False):
        directory = self.source / name
        directory.mkdir()
        (directory / "type").write_text("longrun\n")
        (directory / "run").write_text("#!/bin/sh\nexec sleep 60\n")
        (directory / "run").chmod(0o755)
        if dependencies:
            (directory / "dependencies.d").mkdir()
            for dependency in dependencies:
                (directory / "dependencies.d" / dependency).touch()
        if enabled:
            (self.source / "user" / "contents.d" / name).touch()

    def run_guard(self):
        return subprocess.run(
            ["bash", str(GUARD)],
            env={**os.environ, "S6_RC_SOURCE_DIR": str(self.source),
                 "S6_RC_BASE_DIR": str(self.base)},
            capture_output=True, text=True, timeout=30,
        )

    def assert_compiles(self):
        output = self.root / "compiled"
        if output.exists():
            shutil.rmtree(output)
        result = subprocess.run(
            [str(COMPILER), str(output), str(self.source)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_quarantined(self, name):
        self.assertFalse((self.source / name).exists())
        copies = list((self.source / ".disabled-by-guard").glob(f"{name}.*"))
        self.assertEqual(len(copies), 1)
        self.assertTrue((copies[0] / "disabled-reason.txt").exists())

    def test_healthy_source_is_not_rewritten(self):
        self.service("healthy", enabled=True)
        before = (self.source / "healthy" / "run").read_bytes()
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.source / "healthy" / "run").read_bytes(), before)
        self.assertFalse((self.source / ".disabled-by-guard").exists())
        self.assert_compiles()

    def test_unreferenced_bad_dependency_keeps_healthy_enabled(self):
        self.service("healthy", enabled=True)
        self.service("bad", ("missing",))
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_quarantined("bad")
        self.assertTrue((self.source / "user/contents.d/healthy").exists())
        self.assertTrue((self.source / "healthy/run").exists())
        self.assert_compiles()

    def test_cycle_quarantined_while_valid_dependency_closure_survives(self):
        self.service("cycle-a", ("cycle-b",), enabled=True)
        self.service("cycle-b", ("cycle-a",))
        self.service("dependency")
        self.service("healthy", ("dependency",), enabled=True)
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_quarantined("cycle-a")
        self.assert_quarantined("cycle-b")
        self.assertTrue((self.source / "dependency/run").exists())
        self.assertTrue((self.source / "user/contents.d/healthy").exists())
        self.assert_compiles()

    def test_nested_bundle_retains_healthy_membership(self):
        self.service("worker")
        self.bundle(self.source, "group", ("worker",))
        (self.source / "user/contents.d/group").touch()
        self.service("bad", ("missing",))
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_quarantined("bad")
        self.assertTrue((self.source / "user/contents.d/group").exists())
        self.assertTrue((self.source / "group/contents.d/worker").exists())
        self.assert_compiles()

    def test_bundle_cycle_through_user_is_removed_without_losing_healthy_service(self):
        self.service("healthy", enabled=True)
        self.bundle(self.source, "cycle-group", ("user",))
        (self.source / "user/contents.d/cycle-group").touch()
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.source / "user/contents.d/healthy").exists())
        self.assertFalse((self.source / "user/contents.d/cycle-group").exists())
        self.assertTrue((self.source / "cycle-group/contents.d/user").exists())
        copies = list((self.source / ".disabled-by-guard").glob("user.*"))
        self.assertEqual(len(copies), 1)
        self.assertTrue((copies[0] / "contents.d/cycle-group").exists())
        self.assert_compiles()

    def test_legacy_user_user2_and_custom_bundle_contents_are_preserved(self):
        self.service("worker")
        self.service("worker2")
        self.bundle(self.source, "legacy-group")
        members = {"user": "legacy-group", "user2": "worker2", "legacy-group": "worker"}
        for name, member in members.items():
            directory = self.source / name
            (directory / "contents.d").rmdir()
            # The final line deliberately has no newline.
            (directory / "contents").write_text("# keep this comment\n  " + member)
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in members:
            self.assertFalse((self.source / name / "contents.d").exists())
            self.assertTrue((self.source / name / "contents").read_text().startswith("# keep"))
        self.assert_compiles()
        for name, expected in {"user": "worker", "user2": "worker2", "legacy-group": "worker"}.items():
            result = subprocess.run(
                ["/command/s6-rc-db", "-c", str(self.root / "compiled"), "contents", name],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout.splitlines())

    def test_legacy_membership_survives_recovery_for_an_unrelated_bad_service(self):
        self.service("worker")
        self.bundle(self.source, "legacy-group")
        (self.source / "legacy-group/contents.d").rmdir()
        (self.source / "legacy-group/contents").write_text("worker")
        (self.source / "user/contents.d").rmdir()
        (self.source / "user/contents").write_text("legacy-group")
        self.service("bad", ("missing",))
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.source / "user/contents.d/legacy-group").exists())
        self.assertEqual((self.source / "legacy-group/contents").read_text(), "worker")
        self.assertFalse((self.source / "legacy-group/contents.d").exists())
        self.assert_quarantined("bad")
        self.assert_compiles()

    def test_missing_run_is_preserved_in_quarantine(self):
        directory = self.source / "bad"
        directory.mkdir()
        (directory / "type").write_text("longrun\n")
        (self.source / "user/contents.d/bad").touch()
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_quarantined("bad")
        self.assertFalse((self.source / "user/contents.d/bad").exists())
        self.assert_compiles()

    def test_corrupt_base_is_restored_and_old_definition_retained(self):
        self.service("base-service")
        shutil.copytree(self.source / "base-service", self.base / "base-service")
        (self.source / "base-service/run").unlink()
        self.service("healthy", enabled=True)
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.source / "user/contents.d/healthy").exists())
        self.assertTrue((self.source / "base-service/run").exists())
        self.assertTrue(list((self.source / ".disabled-by-guard").glob("base-service.*")))
        self.assert_compiles()

    def test_valid_modified_base_survives_unrelated_bad_service(self):
        self.service("base-service")
        shutil.copytree(self.source / "base-service", self.base / "base-service")
        (self.source / "base-service/run").write_text("#!/bin/sh\nexec sleep 120\n")
        self.service("bad", ("missing",))
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sleep 120", (self.source / "base-service/run").read_text())
        self.assertFalse(list((self.source / ".disabled-by-guard").glob("base-service.*")))
        self.assert_quarantined("bad")
        self.assert_compiles()

    def test_bad_base_returns_failure_without_discarding_custom_services(self):
        self.service("healthy", enabled=True)
        self.service("bad", ("missing",))
        (self.base / "user/contents.d/missing").touch()
        result = self.run_guard()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("base definitions do not compile", result.stderr)
        self.assertTrue((self.source / "healthy/run").exists())
        self.assertTrue((self.source / "bad/run").exists())


if __name__ == "__main__":
    unittest.main()
