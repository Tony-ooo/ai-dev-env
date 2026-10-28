"""Exercise real migration functions against disposable files only."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "src" / "entrypoint.sh"
GENERATOR = ROOT / "src/skills/s6-service-manager/scripts/create_s6_longrun.sh"


@unittest.skipUnless(os.name == "posix", "requires Linux shell fixtures")
class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "home-file"
        self.target = self.root / "saved-file"

    def run_function(self, function, initialized=False, fail_copy=False):
        script = 'source "$1"; CONFIGS_INITIALIZED=$2; '
        if fail_copy:
            script += "cp() { return 1; }; "
        script += f'{function} "$3" "$4"'
        return subprocess.run(
            ["bash", "-c", script, "fixture", str(ENTRYPOINT),
             "1" if initialized else "0", str(self.source), str(self.target)],
            capture_output=True, text=True, timeout=10,
        )

    def test_failed_directory_copy_preserves_original(self):
        self.source.mkdir()
        (self.source / "important.txt").write_text("original")
        result = self.run_function("link", fail_copy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.source / "important.txt").read_text(), "original")
        self.assertFalse(self.source.is_symlink())
        self.assertNotIn("merged directory", result.stderr)

    def test_failed_file_copy_preserves_original_and_previous_target(self):
        self.source.write_text("latest")
        self.target.write_text("previous")
        result = self.run_function("link", fail_copy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.source.read_text(), "latest")
        self.assertEqual(self.target.read_text(), "previous")
        self.assertFalse(self.source.is_symlink())

    def test_directory_migration_is_repeatable(self):
        self.source.mkdir()
        (self.source / "item").write_text("content")
        self.target.mkdir()
        (self.target / "existing").write_text("keep")
        for _ in range(2):
            result = self.run_function("link")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.source.is_symlink())
            self.assertEqual((self.target / "item").read_text(), "content")
            self.assertEqual((self.target / "existing").read_text(), "keep")

    def test_first_boot_seeds_missing_config(self):
        self.source.write_text("image default")
        result = self.run_function("seed_file")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.source.is_symlink())
        self.assertEqual(self.target.read_text(), "image default")

    def test_new_container_preserves_saved_nonempty_and_empty_configs(self):
        for saved in ("custom configuration", ""):
            with self.subTest(saved=saved):
                if self.source.is_symlink():
                    self.source.unlink()
                self.source.write_text("image default")
                self.target.write_text(saved)
                result = self.run_function("seed_file")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.target.read_text(), saved)
                self.assertTrue(self.source.is_symlink())

    def test_same_container_recovers_atomic_replacement_including_empty(self):
        self.target.write_text("previous")
        for latest in ("latest configuration", ""):
            with self.subTest(latest=latest):
                if self.source.is_symlink():
                    self.source.unlink()
                self.source.write_text(latest)
                result = self.run_function("seed_file", initialized=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.target.read_text(), latest)
                self.assertTrue(self.source.is_symlink())

    def test_failed_seed_preserves_atomic_replacement(self):
        self.source.write_text("latest")
        self.target.write_text("previous")
        result = self.run_function("seed_file", initialized=True, fail_copy=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.source.read_text(), "latest")
        self.assertEqual(self.target.read_text(), "previous")

    def test_existing_symlink_keeps_saved_configuration(self):
        self.target.write_text("saved")
        self.source.symlink_to(self.target)
        result = self.run_function("seed_file", initialized=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.source.read_text(), "saved")
        self.assertTrue(self.source.is_symlink())

    def test_link_preserves_intentional_empty_file(self):
        self.source.write_text("")
        self.target.write_text("previous")
        result = self.run_function("link")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(), "")

    def test_s6_seed_keeps_legacy_bundle_members_and_adds_missing_services(self):
        template = self.root / "skeleton"
        destination = self.root / "services"
        (template / "user/contents.d").mkdir(parents=True)
        (template / "user/type").write_text("bundle\n")
        (template / "new-service").mkdir()
        (template / "new-service/type").write_text("oneshot\n")
        (template / "new-service/up").write_text("/bin/true\n")
        (destination / "user").mkdir(parents=True)
        (destination / "user/type").write_text("bundle\n")
        (destination / "user/contents").write_text("existing-service\n")
        for _ in range(2):
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; seed_s6_source "$2" "$3"',
                 "fixture", str(ENTRYPOINT), str(template), str(destination)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / "user/contents.d").exists())
            self.assertEqual((destination / "user/contents").read_text(), "existing-service\n")
            self.assertEqual((destination / "new-service/up").read_text(), "/bin/true\n")


@unittest.skipUnless(Path("/command/s6-setuidgid").exists(), "requires s6 tool image")
class ServiceGeneratorTest(unittest.TestCase):
    def test_default_generated_service_runs_as_dev(self):
        result = subprocess.run(
            ["bash", str(GENERATOR), "--name", "fixture", "--dry-run",
             "--no-persistent", "--no-runtime", "--", "/bin/echo", "a b"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exec /command/s6-setuidgid dev /bin/echo 'a b'", result.stderr)

    def test_empty_run_as_rejected(self):
        result = subprocess.run(
            ["bash", str(GENERATOR), "--name", "fixture", "--run-as", "",
             "--dry-run", "--no-persistent", "--no-runtime", "--", "/bin/true"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid --run-as", result.stderr)


@unittest.skipUnless(
    os.environ.get("AI_DEV_ACCOUNT_FIXTURE") == "1"
    and Path("/scratch/account-fixture-enabled").exists(),
    "requires the isolated /etc and /home/dev tmpfs account fixture",
)
class AccountRemapTest(unittest.TestCase):
    def setUp(self):
        # This fixture is enabled only in a container with disposable account
        # databases; it must never be enabled on a developer's normal machine.
        Path("/etc/passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\n"
            "dev:x:1000:1000:dev:/home/dev:/bin/bash\n"
        )
        Path("/etc/group").write_text("root:x:0:\ndev:x:1000:\n")
        Path("/etc/shadow").write_text("root:!:1:0:99999:7:::\ndev:!:1:0:99999:7:::\n")
        Path("/etc/gshadow").write_text("root:!::\ndev:!::\n")
        Path("/etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nshadow: files\n")
        for name in ("subuid", "subgid", "login.defs"):
            Path("/etc", name).write_text("")
        self.volume = Path("/home/dev/.ai-data")
        self.volume.mkdir(exist_ok=True)
        self.file = self.volume / "owned-file"
        self.file.write_text("data")
        self.home_file = Path("/home/dev/home-owned-file")
        self.home_file.write_text("home")
        for path in (Path("/home/dev"), self.volume, self.file, self.home_file):
            os.chown(path, 1000, 1000)

    def remap(self, uid=None, gid=None):
        env = {key: value for key, value in os.environ.items()
               if key not in ("HOST_UID", "HOST_GID")}
        if uid is not None:
            env["HOST_UID"] = str(uid)
        if gid is not None:
            env["HOST_GID"] = str(gid)
        return subprocess.run(
            ["bash", "-c", 'source "$1"; remap_dev', "fixture", str(ROOT / "src/sysinit.sh")],
            env=env, capture_output=True, text=True, timeout=10,
        )

    def assert_owner(self, uid, gid):
        for path in (self.volume, self.file, self.home_file):
            with self.subTest(path=str(path)):
                stat = path.stat()
                self.assertEqual((stat.st_uid, stat.st_gid), (uid, gid))
        self.assertEqual(subprocess.check_output(["id", "-u", "dev"], text=True).strip(), str(uid))
        self.assertEqual(subprocess.check_output(["id", "-g", "dev"], text=True).strip(), str(gid))

    def test_uid_and_gid_remap_and_repeat(self):
        for _ in range(2):
            result = self.remap(2001, 2002)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_owner(2001, 2002)

    def test_gid_only_remap(self):
        result = self.remap(gid=2002)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner(1000, 2002)

    def test_invalid_id_is_rejected_before_account_changes(self):
        for uid in ("0", "not-an-id", "99999999999999999999"):
            result = self.remap(uid=uid)
            self.assertNotEqual(result.returncode, 0)
            self.assert_owner(1000, 1000)

    def test_existing_other_group_is_not_silently_accepted(self):
        with Path("/etc/group").open("a") as handle:
            handle.write("other:x:2002:\n")
        result = self.remap(2001, 2002)
        self.assertNotEqual(result.returncode, 0)
        self.assert_owner(1000, 1000)

    def test_uid_collision_is_rejected_before_changing_an_available_gid(self):
        with Path("/etc/passwd").open("a") as handle:
            handle.write("other:x:2001:1000:other:/nonexistent:/bin/false\n")
        result = self.remap(2001, 2002)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOST_UID is already assigned", result.stdout)
        self.assert_owner(1000, 1000)
        self.assertNotIn(":2002:", Path("/etc/group").read_text())


if __name__ == "__main__":
    unittest.main()
