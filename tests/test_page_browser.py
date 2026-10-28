"""Unit tests for the vendored CDP helper's endpoint configuration."""

import importlib.util
from importlib.machinery import SourceFileLoader
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


pb = load("ai_dev_page_browser", ROOT / "browser" / "third_party" / "page_browser.py")


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("BROWSER_CDP_HOST", "BROWSER_CDP_PORT")}
        os.environ.pop("BROWSER_CDP_HOST", None)
        os.environ.pop("BROWSER_CDP_PORT", None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_point_at_local_chromium(self):
        self.assertEqual(pb.cdp_host(), "127.0.0.1")
        self.assertEqual(pb.cdp_port(), 9222)

    def test_env_overrides(self):
        os.environ["BROWSER_CDP_HOST"] = "ai-dev-alice-30-browser"
        os.environ["BROWSER_CDP_PORT"] = "9223"
        self.assertEqual(pb.cdp_host(), "ai-dev-alice-30-browser")
        self.assertEqual(pb.cdp_port(), 9223)

    def test_display_num_removed(self):
        """The display-derived port was dropped when the endpoint became configurable."""
        self.assertFalse(hasattr(pb, "display_num"))


class SharedUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.workspace = self.home / ".ai-data/workspace"
        self.downloads = self.home / ".ai-data/browser/downloads"
        self.workspace.mkdir(parents=True)
        self.downloads.mkdir(parents=True)
        (self.home / "workspace").symlink_to(self.workspace, target_is_directory=True)
        (self.home / "downloads").symlink_to(self.downloads, target_is_directory=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_maps_workspace_and_download_paths(self):
        (self.workspace / "a file.txt").write_text("fixture", encoding="utf-8")
        (self.downloads / "download.txt").write_text("fixture", encoding="utf-8")
        paths = pb.resolve_upload_paths(["~/workspace/a file.txt", "downloads/download.txt"], str(self.home))
        self.assertEqual(paths, ["/home/dev/workspace/a file.txt", "/home/dev/.ai-data/downloads/download.txt"])

    def test_symlink_inside_shared_root_maps_canonical_target(self):
        (self.workspace / "target.txt").write_text("fixture", encoding="utf-8")
        (self.workspace / "alias.txt").symlink_to(self.workspace / "target.txt")
        self.assertEqual(pb.resolve_upload_paths(["workspace/alias.txt"], str(self.home)),
                         ["/home/dev/workspace/target.txt"])

    def test_unshared_files_and_escaping_symlinks_are_rejected(self):
        (self.home / "private.txt").write_text("fixture", encoding="utf-8")
        (self.workspace / "escape.txt").symlink_to(self.home / "private.txt")
        for path in ("private.txt", "workspace/escape.txt", "../outside"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                pb.resolve_upload_paths([path], str(self.home))

    def test_optional_roots_can_only_narrow_shared_paths(self):
        (self.workspace / "a.txt").write_text("fixture", encoding="utf-8")
        (self.downloads / "b.txt").write_text("fixture", encoding="utf-8")
        self.assertEqual(pb.resolve_upload_paths(["workspace/a.txt"], str(self.home), ["workspace"]),
                         ["/home/dev/workspace/a.txt"])
        for paths, roots in [(["downloads/b.txt"], ["workspace"]), (["workspace/a.txt"], ["."])]:
            with self.assertRaises(RuntimeError):
                pb.resolve_upload_paths(paths, str(self.home), roots)

    def test_optional_root_rejects_redirected_subdirectory(self):
        private = self.workspace / "private"
        private.mkdir()
        (private / "file").write_text("fixture")
        (self.workspace / "allowed").symlink_to(private, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            pb.resolve_upload_paths(["workspace/allowed/file"], str(self.home), ["workspace/allowed"])

    def test_missing_directory_and_oversize_files_are_rejected(self):
        with self.assertRaises(RuntimeError):
            pb.resolve_upload_paths(["workspace/missing"], str(self.home))
        with self.assertRaises(RuntimeError):
            pb.resolve_upload_paths(["workspace"], str(self.home))
        (self.workspace / "large.txt").write_bytes(b"a" * 20)
        from unittest.mock import patch
        with patch.dict(os.environ, {"RAKAZO_BROWSER_UPLOAD_MAX_BYTES": "10"}), self.assertRaises(RuntimeError):
            pb.resolve_upload_paths(["workspace/large.txt"], str(self.home))


if __name__ == "__main__":
    unittest.main()
