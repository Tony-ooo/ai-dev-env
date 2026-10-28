"""Exercise index publishing using a stateful registry fixture."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


path = Path(__file__).resolve().parents[1] / "scripts" / "publish_image_index.py"
spec = importlib.util.spec_from_file_location("publish_image_index", path)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ImageReleaseTest(unittest.TestCase):
    def setUp(self):
        self.base = "a" * 40
        self.first = "b" * 40
        self.second = "c" * 40
        self.history = [self.second, self.first, self.base]
        self.images = {}
        self.created = []
        self.argv = ["publish", "--version", "0.1.1", "--revision", self.first,
                     "--base", self.base, "--series", "0.1", "--registry", "test/dev",
                     "--browser-registry", "test/browser"]
        for repo in ("test/dev", "test/browser"):
            for arch in release.ARCHES:
                self.single(repo, "0.1.1", self.first, arch)

    def put(self, ref, manifest, config):
        self.images[ref] = (manifest, config)
        self.images[ref.split(":", 1)[0] + "@" + manifest["digest"]] = (manifest, config)

    def single(self, repo, version, revision, arch):
        digest = "sha256:" + hashlib.sha256((repo + version + revision + arch).encode()).hexdigest()
        config = {"os": "linux", "architecture": arch, "config": {"Labels": {
            release.VERSION_LABEL: version, release.REVISION_LABEL: revision}}}
        self.put(f"{repo}:{version}-{arch}", {"digest": digest}, config)

    def run_fake(self, *args, missing_ok=False):
        if args[:2] == ("git", "rev-parse"):
            return self.base + "\n"
        if args[:2] == ("git", "fetch"):
            return ""
        if args[:2] == ("git", "rev-list"):
            return "\n".join(self.history)
        if args[:4] == ("docker", "buildx", "imagetools", "inspect"):
            ref = args[-1]
            if ref not in self.images:
                if missing_ok:
                    return None
                raise RuntimeError("fixture image missing")
            manifest, config = self.images[ref]
            return json.dumps(manifest if ".Manifest" in args[-2] else config)
        if args[:4] == ("docker", "buildx", "imagetools", "create"):
            target = args[5]
            sources = [self.images[ref] for ref in args[6:]]
            descriptors, configs = [], {}
            for manifest, config in sources:
                if "manifests" in manifest:
                    descriptors.extend(manifest["manifests"])
                    configs.update(config)
                else:
                    arch = config["architecture"]
                    descriptors.append({"digest": manifest["digest"], "platform": {"os": "linux", "architecture": arch}})
                    configs["linux/" + arch] = config
            digest = "sha256:" + hashlib.sha256(json.dumps(descriptors, sort_keys=True).encode()).hexdigest()
            self.put(target, {"digest": digest, "manifests": descriptors}, configs)
            self.created.append(target)
            return ""
        raise AssertionError(args)

    def publish(self):
        with mock.patch.object(release, "run", side_effect=self.run_fake), mock.patch.object(sys, "argv", self.argv):
            release.main()

    def test_waits_without_publishing_until_both_images_are_ready(self):
        del self.images["test/browser:0.1.1-arm64"]
        self.publish()
        self.assertEqual(self.created, [])

    def test_publishes_once_and_retries_without_replacing_version(self):
        self.publish()
        self.assertEqual(self.created, ["test/dev:0.1.1", "test/browser:0.1.1", "test/dev:latest", "test/browser:latest"])
        self.created.clear()
        self.publish()
        self.assertEqual(self.created, ["test/dev:latest", "test/browser:latest"])

    def test_rejects_inconsistent_source_before_publishing(self):
        self.single("test/browser", "0.1.1", self.second, "arm64")
        with self.assertRaisesRegex(RuntimeError, "source labels"):
            self.publish()
        self.assertEqual(self.created, [])

    def test_old_release_does_not_replace_newer_latest(self):
        for repo in ("test/dev", "test/browser"):
            for arch in release.ARCHES:
                self.single(repo, "0.1.2", self.second, arch)
        self.argv[self.argv.index("0.1.1")] = "0.1.2"
        self.argv[self.argv.index(self.first)] = self.second
        self.publish()
        self.created.clear()
        self.argv[self.argv.index("0.1.2")] = "0.1.1"
        self.argv[self.argv.index(self.second)] = self.first
        self.publish()
        self.assertEqual(self.created, ["test/dev:0.1.1", "test/browser:0.1.1"])

    def test_existing_version_conflict_is_rejected(self):
        self.publish()
        self.created.clear()
        self.single("test/dev", "0.1.1", self.first, "amd64")
        manifest, config = self.images["test/dev:0.1.1-amd64"]
        manifest = dict(manifest, digest="sha256:" + "d" * 64)
        self.put("test/dev:0.1.1-amd64", manifest, config)
        with self.assertRaisesRegex(RuntimeError, "different images"):
            self.publish()
        self.assertEqual(self.created, [])

    def test_invalid_version_rejected_before_registry_access(self):
        self.argv[self.argv.index("0.1.1")] = "0.1.99"
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.publish()
        self.assertEqual(self.created, [])

    def test_automatic_root_numbers_commits_consistently(self):
        self.argv[self.argv.index(self.base)] = "@root"
        self.argv[self.argv.index("0.1.1")] = "0.1.2"
        for repo in ("test/dev", "test/browser"):
            for arch in release.ARCHES:
                self.single(repo, "0.1.2", self.first, arch)
        self.publish()
        self.assertIn("test/dev:0.1.2", self.created)


if __name__ == "__main__":
    unittest.main()
