"""Real-tar fixtures for offline legacy home recovery; no Docker socket required."""

import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("persist_home_archive", ROOT / "scripts/persist_home_archive.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def archive_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, kind, value, *attributes in entries:
            attrs = attributes[0] if attributes else {}
            member = tarfile.TarInfo(name)
            member.uid = attrs.get("uid", os.getuid())
            member.gid = attrs.get("gid", os.getgid())
            member.mode = attrs.get("mode", 0o750 if kind == "directory" else 0o640)
            member.mtime = 1234567890
            if kind == "file":
                data = value.encode() if isinstance(value, str) else value
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
                continue
            if kind == "directory":
                member.type = tarfile.DIRTYPE
            elif kind == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, value
            elif kind == "hardlink":
                member.type, member.linkname = tarfile.LNKTYPE, value
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
            else:
                raise AssertionError("unknown fixture")
            archive.addfile(member)
    return stream.getvalue()


@unittest.skipUnless(os.name == "posix", "requires Unix ownership and symlinks")
class LegacyArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "volume"
        self.root.mkdir()

    def apply(self, source, entries, verify=False):
        data = archive_bytes(entries)
        before = bytes(data)
        recovery.apply_archive(io.BytesIO(data), source, verify=verify, root=self.root)
        self.assertEqual(data, before)

    def test_workspace_symlink_proves_initialization_without_writing(self):
        self.apply("workspace", [("workspace","symlink","/home/dev/.ai-data/workspace")], verify=True)
        self.assertEqual(list(self.root.iterdir()), [])
        for target in ("/home/dev/other", "../other"):
            with self.assertRaises(ValueError):
                self.apply("workspace", [("workspace","symlink",target)], verify=True)

    def test_workspace_directory_is_not_mistaken_for_initialized_home(self):
        with self.assertRaises(ValueError):
            self.apply("workspace", [("workspace","directory","")], verify=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_regular_and_empty_config_contents_and_modes(self):
        self.apply(".gitconfig", [(".gitconfig","file","new configuration",{"mode":0o640})])
        target = self.root / "home-config/gitconfig"
        self.assertEqual(target.read_text(), "new configuration")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.apply(".gitconfig", [(".gitconfig","file","",{"mode":0o600})])
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_directory_merge_preserves_existing_and_restores_metadata(self):
        target = self.root / "home-config/.config"
        target.mkdir(parents=True)
        (target / "unrelated").write_text("keep")
        self.apply(".config", [
            (".config","directory","",{"mode":0o750}),
            (".config/nested","directory","",{"mode":0o700}),
            (".config/nested/settings","file","new settings",{"mode":0o600}),
        ])
        self.assertEqual((target / "unrelated").read_text(), "keep")
        self.assertEqual((target / "nested/settings").read_text(), "new settings")
        self.assertEqual(stat.S_IMODE((target / "nested").stat().st_mode), 0o700)
        self.assertEqual((target / "nested/settings").stat().st_mtime, 1234567890)

    @unittest.skipUnless(os.geteuid() == 0, "requires isolated root with CHOWN/FOWNER")
    def test_uid_gid_and_ssh_key_permissions_are_preserved(self):
        self.apply(".config", [
            (".config","directory","",{"uid":1000,"gid":1000,"mode":0o750}),
            (".config/settings","file","fixture",{"uid":1000,"gid":1000,"mode":0o640}),
        ])
        for path in (self.root / "home-config/.config", self.root / "home-config/.config/settings"):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (1000,1000))
        self.apply(".ssh", [
            (".ssh","directory","",{"uid":1000,"gid":1000,"mode":0o700}),
            (".ssh/ssh_host_ed25519_key","file","fixture-key",{"uid":0,"gid":0,"mode":0o600}),
        ])
        key = self.root / "ssh/ssh_host_ed25519_key"
        self.assertEqual((key.stat().st_uid,key.stat().st_gid,stat.S_IMODE(key.stat().st_mode)), (0,0,0o600))

    def test_root_symlinks_are_not_followed(self):
        for target in ("/home/dev/.ai-data/home-config/gitconfig", ".ai-data/home-config/gitconfig"):
            self.apply(".gitconfig", [(".gitconfig","symlink",target)])
        for target in ("/outside/secret", "/home/dev/custom", ".ai-data/home-config/../gitconfig"):
            with self.assertRaises(ValueError):
                self.apply(".gitconfig", [(".gitconfig","symlink",target)])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_transaction_rejects_late_bad_member_without_changing_volume(self):
        target = self.root / "home-config/.config"
        target.mkdir(parents=True)
        (target / "setting").write_text("previous")
        recovery.begin_transaction(self.root)
        data = archive_bytes([(".config", "directory", ""),
                              (".config/setting", "file", "new"),
                              (".config/../bad", "file", "invalid")])
        with self.assertRaises(ValueError):
            recovery.stage_archive(io.BytesIO(data), ".config", self.root)
        recovery.recover_transaction(self.root)
        self.assertEqual((target / "setting").read_text(), "previous")

    def test_commit_failure_rolls_back_all_mappings(self):
        from unittest.mock import patch
        parent = self.root / "home-config"
        parent.mkdir()
        (parent / "gitconfig").write_text("old git")
        (parent / "npmrc").write_text("old npm")
        recovery.begin_transaction(self.root)
        for name in (".gitconfig", ".npmrc"):
            recovery.stage_archive(io.BytesIO(archive_bytes([(name, "file", "new")])), name, self.root)
        original_replace = os.replace
        def fail_later(src, dst):
            if str(src).endswith("stage/home-config/npmrc"):
                raise OSError("fixture commit failure")
            return original_replace(src, dst)
        with patch.object(recovery.os, "replace", side_effect=fail_later), self.assertRaises(OSError):
            recovery.commit_transaction(self.root)
        self.assertEqual((parent / "gitconfig").read_text(), "old git")
        self.assertEqual((parent / "npmrc").read_text(), "old npm")
        self.assertFalse(recovery.transaction_dir(self.root).exists())

    def test_interrupted_commit_recovers_on_next_begin(self):
        parent = self.root / "home-config"
        parent.mkdir()
        target = parent / "gitconfig"
        target.write_text("old")
        recovery.begin_transaction(self.root)
        recovery.stage_archive(io.BytesIO(archive_bytes([(".gitconfig", "file", "new")])), ".gitconfig", self.root)
        tx = recovery.transaction_dir(self.root)
        recovery.write_json(tx / "journal.json", {"items": [{"name": ".gitconfig", "existed": True}], "parents": []})
        os.replace(target, tx / "backup/.gitconfig")
        os.replace(tx / "stage/home-config/gitconfig", target)
        recovery.begin_transaction(self.root)
        self.assertEqual(target.read_text(), "old")
        recovery.recover_transaction(self.root)

    def test_staging_rejects_hardlinks_outside_the_mapping(self):
        parent = self.root / "home-config"
        parent.mkdir()
        target = parent / "gitconfig"
        target.write_text("old")
        os.link(target, self.root / "outside-link")
        recovery.begin_transaction(self.root)
        data = archive_bytes([(".gitconfig", "file", "new")])
        with self.assertRaisesRegex(ValueError, "hardlinks outside"):
            recovery.stage_archive(io.BytesIO(data), ".gitconfig", self.root)
        recovery.recover_transaction(self.root)
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(target.stat().st_ino, (self.root / "outside-link").stat().st_ino)

    def test_killed_commit_is_rolled_back_on_next_begin(self):
        parent = self.root / "home-config"
        parent.mkdir()
        target = parent / "gitconfig"
        target.write_text("old")
        recovery.begin_transaction(self.root)
        recovery.stage_archive(io.BytesIO(archive_bytes([(".gitconfig", "file", "new")])), ".gitconfig", self.root)
        code = '''
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("recovery", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
replace = module.os.replace
def interrupted(source, target):
    replace(source, target)
    if str(source).endswith("stage/home-config/gitconfig"):
        os._exit(99)
module.os.replace = interrupted
module.commit_transaction(Path(sys.argv[2]))
'''
        child = subprocess.run([sys.executable, "-B", "-c", code,
                                str(ROOT / "scripts/persist_home_archive.py"), str(self.root)], timeout=10)
        self.assertEqual(child.returncode, 99)
        self.assertEqual(target.read_text(), "new")
        recovery.begin_transaction(self.root)
        self.assertEqual(target.read_text(), "old")
        recovery.recover_transaction(self.root)

    def test_nested_relative_link_and_in_archive_hardlink(self):
        self.apply(".config", [
            (".config","directory",""),
            (".config/settings","file","value"),
            (".config/link","symlink","settings"),
            (".config/hard","hardlink",".config/settings"),
        ])
        target = self.root / "home-config/.config"
        self.assertEqual(os.readlink(target / "link"), "settings")
        self.assertEqual((target / "hard").stat().st_ino, (target / "settings").stat().st_ino)

    def test_declared_absolute_skill_link_is_preserved_without_following_it(self):
        self.apply(".claude", [
            (".claude","directory",""),
            (".claude/skills","symlink","/home/dev/.agents/skills"),
        ])
        link = self.root / ".claude/skills"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "/home/dev/.agents/skills")
        self.assertFalse((self.root / "agents").exists())

    def test_declared_relative_skill_link_keeps_its_target_after_mapping(self):
        destination = self.root / "agents/skills"
        destination.mkdir(parents=True)
        (destination / "fixture").write_text("untouched")
        self.apply(".claude", [
            (".claude","directory",""),
            (".claude/skills","symlink","../.agents/skills"),
        ])
        link = self.root / ".claude/skills"
        self.assertEqual(os.readlink(link), "../agents/skills")
        self.assertEqual(link.resolve(), destination)
        self.assertEqual((destination / "fixture").read_text(), "untouched")

    def test_cross_mapping_links_from_config_use_correct_volume_depth(self):
        self.apply(".config", [
            (".config","directory",""),
            (".config/skills","symlink","../.agents/skills"),
            (".config/git","symlink","../.gitconfig"),
        ])
        target = self.root / "home-config/.config"
        self.assertEqual(os.readlink(target / "skills"), "../../agents/skills")
        self.assertEqual(os.readlink(target / "git"), "../gitconfig")

    def test_subsequent_member_cannot_write_through_declared_skill_link(self):
        destination = self.root / "agents/skills"
        destination.mkdir(parents=True)
        (destination / "fixture").write_text("untouched")
        for link in ("/home/dev/.agents/skills", "../.agents/skills"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                self.apply(".claude", [
                    (".claude","directory",""),
                    (".claude/skills","symlink",link),
                    (".claude/skills/injected","file","rejected"),
                ])
            self.assertFalse((destination / "injected").exists())
            self.assertEqual((destination / "fixture").read_text(), "untouched")

    def test_dotdot_after_an_actual_symlink_is_rejected_without_retargeting(self):
        home = Path(self.tmp.name) / "legacy-home"
        config = home / ".config"
        workspace = home / "workspace"
        config.mkdir(parents=True)
        (workspace / "subdir").mkdir(parents=True)
        (workspace / "file").write_text("correct source target")
        (config / "file").write_text("wrong lexical target")
        (config / "alias").symlink_to("../workspace/subdir", target_is_directory=True)
        (config / "selected").symlink_to("alias/../file")
        self.assertEqual((config / "selected").resolve(), workspace / "file")
        self.assertEqual((config / "selected").read_text(), "correct source target")

        destination = self.root / "home-config/.config"
        destination.mkdir(parents=True)
        (destination / "selected").write_text("previous saved selection")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", dereference=False) as archive:
            archive.add(config, arcname=".config")
        stream.seek(0)
        with self.assertRaisesRegex(ValueError, "cannot safely map"):
            recovery.apply_archive(stream, ".config", root=self.root)
        self.assertFalse((destination / "selected").is_symlink())
        self.assertEqual((destination / "selected").read_text(), "previous saved selection")
        self.assertEqual(os.readlink(config / "selected"), "alias/../file")
        self.assertEqual((config / "selected").read_text(), "correct source target")

    def test_absolute_dotdot_after_component_is_rejected_before_replacement(self):
        target = self.root / "home-config/.config"
        target.mkdir(parents=True)
        (target / "selected").write_text("previous")
        with self.assertRaisesRegex(ValueError, "cannot safely map"):
            self.apply(".config", [
                (".config", "directory", ""),
                (".config/selected", "symlink", "/home/dev/.config/alias/../file"),
            ])
        self.assertFalse((target / "selected").is_symlink())
        self.assertEqual((target / "selected").read_text(), "previous")

    def test_archive_paths_cannot_escape_requested_mapping(self):
        for name in ("/.gitconfig", "../.gitconfig", ".gitconfig/../outside", "different"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.apply(".gitconfig", [(name,"file","rejected")])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_archive_link_cannot_target_undeclared_paths(self):
        for target in ("/outside", "/etc/passwd", "../../outside", "/home/dev/.agents-foreign/secret"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.apply(".config", [
                    (".config","directory",""),
                    (".config/link","symlink",target),
                ])

    def test_destination_parent_symlink_is_never_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "gitconfig").write_text("original")
        (self.root / "home-config").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.apply(".gitconfig", [(".gitconfig","file","new")])
        self.assertEqual((outside / "gitconfig").read_text(), "original")

    def test_destination_leaf_symlink_is_never_followed(self):
        outside = Path(self.tmp.name) / "outside-file"
        outside.write_text("original")
        parent = self.root / "home-config"
        parent.mkdir()
        (parent / "gitconfig").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.apply(".gitconfig", [(".gitconfig","file","new")])
        self.assertEqual(outside.read_text(), "original")
        self.assertTrue((parent / "gitconfig").is_symlink())

    def test_archive_symlink_cannot_become_a_write_parent(self):
        with self.assertRaises(ValueError):
            self.apply(".config", [
                (".config","directory",""),
                (".config/link","symlink","."),
                (".config/link/through-link","file","rejected"),
            ])
        self.assertFalse((self.root / "home-config/.config/through-link").exists())

    def test_special_entries_and_external_hardlinks_are_rejected(self):
        for kind,value in (("fifo",""),("hardlink","../outside"),("hardlink",".config/not-in-archive")):
            with self.subTest(kind=kind,value=value), self.assertRaises(ValueError):
                self.apply(".config", [
                    (".config","directory",""),
                    (".config/item",kind,value),
                ])

    def test_truncated_file_retains_previous_target_and_removes_temporary_file(self):
        parent = self.root / "home-config"
        parent.mkdir()
        target = parent / "gitconfig"
        target.write_text("previous")
        member = tarfile.TarInfo(".gitconfig")
        member.size = 4096
        member.uid,member.gid = os.getuid(),os.getgid()
        truncated = member.tobuf() + b"not enough bytes"
        with self.assertRaises(tarfile.TarError):
            recovery.apply_archive(io.BytesIO(truncated), ".gitconfig", root=self.root)
        self.assertEqual(target.read_text(), "previous")
        self.assertEqual(list(parent.glob(".ai-dev-recover-*")), [])


if __name__ == "__main__":
    unittest.main()
