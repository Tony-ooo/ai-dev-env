"""Verify cache and network payloads using isolated local shell fixtures."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires Linux shell tools")
class VendorFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vendor = self.root / "vendor"
        (self.vendor / "blobs").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\ncp "$FIXTURE_DOWNLOAD" "$2"\n')
        curl.chmod(0o755)
        self.payload = self.root / "download"
        self.payload.write_bytes(b"trusted fixture")
        digest = hashlib.sha256(self.payload.read_bytes()).hexdigest()
        (self.vendor / "manifest.txt").write_text(f"{digest}  fixture  https://example.invalid/payload  any\n")
        self.output = self.root / "out"

    def fetch(self):
        return subprocess.run(
            ["bash", "-c", 'set -e; source "$1"; verified_payload fixture "$2"',
             "fixture", str(ROOT / "vendor/fetch.sh"), str(self.output)],
            env={**os.environ, "VENDOR_ROOT": str(self.vendor), "FIXTURE_DOWNLOAD": str(self.payload),
                 "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]},
            capture_output=True, text=True,
        )

    def test_network_payload_is_verified(self):
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), b"trusted fixture")

    def test_corrupt_download_is_not_used(self):
        self.payload.write_bytes(b"wrong fixture")
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse(self.output.exists())

    def test_bad_cache_falls_back_to_verified_download(self):
        (self.vendor / "blobs/fixture").write_bytes(b"bad cache")
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), b"trusted fixture")

    def test_missing_manifest_entry_fails_closed(self):
        (self.vendor / "manifest.txt").write_text("")
        self.assertNotEqual(self.fetch().returncode, 0)
        self.assertFalse(self.output.exists())
