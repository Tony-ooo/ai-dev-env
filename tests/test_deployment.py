"""Behavior tests for management scripts; Docker is replaced by a stateful stub."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

DOCKER_STUB = r'''#!/usr/bin/env python3
import fcntl, io, json, os, pathlib, subprocess, sys, tarfile, time
a = sys.argv[1:]
p = pathlib.Path(os.environ["DOCKER_TEST_STATE"])
mutex = open(str(p) + ".lock", "a+")
fcntl.flock(mutex, fcntl.LOCK_EX)
s = json.loads(p.read_text())
s["calls"].append({"args": a, "pathconv": os.environ.get("MSYS_NO_PATHCONV")})
def save():
    p.write_text(json.dumps(s))
def done(code=0, output=""):
    save()
    if output:
        print(output)
    raise SystemExit(code)
def value(flag, default=""):
    return a[a.index(flag)+1] if flag in a else default
def container(ref):
    return next(((n,c) for n,c in s["containers"].items() if n == ref or c["id"] == ref), (None,None))
cmd = a[0]
if cmd == "version":
    done(output=os.environ.get("TEST_DOCKER_VERSION", "29.0.0 29.0.0"))
if cmd == "image":
    if "-f" in a:
        label_env = "TEST_IMAGE_REVISIONS" if "revision" in value("-f") else "TEST_IMAGE_RELEASES"
        labels = json.loads(os.environ.get(label_env, "{}"))
        done(output=labels.get(a[-1], "<no value>"))
    done(1 if os.environ.get("TEST_MISSING_IMAGE") == a[-1] else 0)
if cmd == "ps":
    if os.environ.get("TEST_PAUSE_LISTING"):
        save()
        fcntl.flock(mutex, fcntl.LOCK_UN)
        root = pathlib.Path(os.environ["DOCKER_TEST_ROOT"])
        (root / "listing-paused").touch()
        deadline = time.monotonic() + 10
        while not (root / "listing-release").exists():
            if time.monotonic() > deadline:
                raise SystemExit(3)
            time.sleep(0.01)
        fcntl.flock(mutex, fcntl.LOCK_EX)
        s = json.loads(p.read_text())
    names = []
    query = value("--filter")
    for name,c in s["containers"].items():
        if "-a" not in a and not c.get("running"):
            continue
        if query.startswith("label=") and c.get("user") != query.split("=",2)[-1]:
            continue
        if query.startswith("volume=") and c.get("volume") != query.split("=",1)[-1]:
            continue
        names.append(name)
    done(output="\n".join(names))
if cmd == "volume":
    name = a[-1]
    if a[1] == "inspect":
        done(0 if name in s["volumes"] else 1)
    if a[1] == "create":
        s["volumes"].setdefault(name, str(pathlib.Path(os.environ["DOCKER_TEST_ROOT"])/name))
        pathlib.Path(s["volumes"][name]).mkdir(exist_ok=True)
        done(output=name)
if cmd == "network":
    name = a[-1] if a[1] != "connect" else a[-2]
    if a[1] == "inspect":
        done(0 if name in s["networks"] else 1)
    if a[1] == "create":
        if os.environ.get("TEST_NETWORK_CREATE_FAIL"):
            done(1)
        s["networks"].append(name)
        done(output=name)
    if a[1] == "connect":
        n,c = container(a[-1])
        if c is None:
            done(1)
        c["networks"].append(name)
        done()
if cmd == "inspect":
    n,c = container(a[-1])
    if c is None:
        done(1)
    fmt = value("-f")
    if fmt == "{{.Id}}":
        done(output=c["id"])
    if fmt == "{{.Image}}":
        done(output=c["image"])
    if fmt == "{{.Config.StopTimeout}}":
        done(output=str(c.get("stop_timeout",10)))
    if "NetworkSettings.Networks" in fmt:
        done(output="\n".join(c["networks"]))
    if "Config.Env" in fmt:
        done(output="USER_PASSWORD=fixture-password")
    if "State.Running" in fmt:
        done(output=str(c["running"]).lower())
    if "Config.Image" in fmt:
        done(output=c["image"])
    if "State.Status" in fmt:
        done(output="running" if c["running"] else "exited")
    done(output="{}")
if cmd == "cp":
    ref,path = a[1].split(":",1)
    n,c = container(ref)
    if path == "/var/lib/ai-dev-env/home-config-initialized":
        done(0 if c and c.get("marker") else 1)
    name = path.rsplit("/",1)[-1]
    if os.environ.get("TEST_COPY_FAIL_NAME") == name:
        print("Error response from daemon: fixture connection failed", file=sys.stderr)
        done(1)
    entry = c.get("home",{}).get(name) if c else None
    if entry is None:
        print("Error response from daemon: Could not find the file " + path + " in container " + ref, file=sys.stderr)
        done(1)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode="w") as archive:
        def add(name,kind,content=""):
            member = tarfile.TarInfo(name)
            member.uid, member.gid, member.mode = os.getuid(), os.getgid(), 0o700
            if kind == "symlink":
                member.type,member.linkname = tarfile.SYMTYPE,content
                archive.addfile(member)
            elif kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                content = content.encode()
                member.size = len(content)
                archive.addfile(member,io.BytesIO(content))
        add(name,entry["kind"],entry.get("content",entry.get("target","")))
        for child,content in entry.get("files",{}).items():
            add(name+"/"+child,"file",content)
    sys.stdout.buffer.write(stream.getvalue())
    done()
if cmd == "create":
    is_lock = "ai-dev-env.role=operation-lock" in a
    if is_lock and os.environ.get("TEST_LOCK_CREATE_FAIL"):
        done(1)
    if os.environ.get("TEST_CREATE_FAIL") and not is_lock:
        done(1)
    name = value("--name")
    if name in s["containers"]:
        done(1)
    labels = [a[i+1] for i,x in enumerate(a) if x == "--label"]
    user = next((x.split("=",1)[1] for x in labels if x.startswith("ai-dev-env.user=")), "")
    s["counter"] += 1
    ident = ("fixture-lock-" if is_lock else "fixture-") + str(s["counter"])
    s["containers"][name] = {"id":ident,"running":False,"image":a[-1],
        "user":user,"volume":None if is_lock else "ai-dev-"+user+"-data","networks":[value("--network","bridge")],
        "marker":True,"new":True,"stop_timeout":int(value("--stop-timeout","10"))}
    done(output=ident)
if cmd in ("start","stop","rm"):
    n,c = container(a[-1])
    if c is None:
        done(1)
    if cmd == "rm":
        del s["containers"][n]
    elif cmd == "start":
        if os.environ.get("TEST_START_FAIL") and c.get("new"):
            done(1)
        c["running"] = True
    else:
        c["running"] = False
    done(output=a[-1])
if cmd == "rename":
    if os.environ.get("TEST_RENAME_FAIL") and "-previous-" in a[-1]:
        done(1)
    n,c = container(a[-2])
    if c is None or a[-1] in s["containers"]:
        done(1)
    del s["containers"][n]
    s["containers"][a[-1]] = c
    done()
if cmd == "exec":
    offset = 3 if a[1] == "-u" else 1
    n,c = container(a[offset])
    action = a[offset+1:]
    if c is None or not c["running"]:
        done(1)
    if action[:2] == ["id","-u"]:
        done(output=os.environ.get("TEST_UID","1000"))
    if action[:2] == ["id","-g"]:
        done(output=os.environ.get("TEST_GID","1000"))
    if action[:2] == ["test","-f"]:
        done(0 if c.get("marker") else 1)
    if "--persist-home" in action:
        done(1 if os.environ.get("TEST_PERSIST_FAIL") else 0)
    if action[:3] == ["sh","-eu","-c"]:
        done(1 if os.environ.get("TEST_PERSIST_FAIL") else 0)
    fail = ((os.environ.get("TEST_NOT_READY") and c.get("new")) or
            (os.environ.get("TEST_INITIAL_NOT_READY") and not c.get("new")))
    done(1 if fail else 0)
if cmd == "run":
    if value("--entrypoint") == "/usr/bin/timeout":
        mount = dict(part.split("=",1) for part in value("--mount").split(",") if "=" in part)
        volume = mount["src"]
        root = s["volumes"].setdefault(volume,str(pathlib.Path(os.environ["DOCKER_TEST_ROOT"])/volume))
        pathlib.Path(root).mkdir(exist_ok=True)
        script = a[a.index("-c")+1].replace('DATA_ROOT = Path("/data")', 'DATA_ROOT = Path('+json.dumps(root)+')')
        arguments = a[a.index("-c")+2:]
        save()
        fcntl.flock(mutex,fcntl.LOCK_UN)
        data = sys.stdin.buffer.read()
        result = subprocess.run([sys.executable,"-c",script,*arguments],input=data)
        fcntl.flock(mutex,fcntl.LOCK_EX)
        s = json.loads(p.read_text())
        done(result.returncode)
    # Browser preparation is represented without generating or printing secrets.
    if "busybox:1" in a:
        if "cat" in a:
            done(output="dev:fixture-password" if a[-1].endswith("screen-auth") else "fixture-token")
        if "-i" in a:
            sys.stdin.read()
        done()
    mappings = {}
    for i,x in enumerate(a):
        if x != "-v":
            continue
        source,dest,*_ = a[i+1].split(":")
        mappings[dest] = s["volumes"].get(source,source)
    script = a[a.index("-c")+1]
    for dest,source in sorted(mappings.items(),key=lambda x:-len(x[0])):
        script = script.replace(dest,source)
    args = a[a.index("-c")+2:]
    save()
    result = subprocess.run(["sh","-eu","-c",script,*args])
    done(result.returncode)
done(2, "unsupported fixture command")
'''


@unittest.skipUnless(BASH and os.name == "posix", "requires Linux Bash fixtures")
class ScriptFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        self.state_file = self.folder / "state.json"
        self.state = {"containers": {}, "volumes": {}, "networks": [], "calls": [], "counter": 0}
        self.write_state()
        (self.bin / "docker").write_text(DOCKER_STUB, encoding="utf-8")
        (self.bin / "docker").chmod(0o755)
        # No waiting and no GPU access in the fixture.
        for name in ("sleep", "nvidia-smi"):
            script = "#!/bin/sh\nexit " + ("1" if name == "nvidia-smi" else "0") + "\n"
            (self.bin / name).write_text(script)
            (self.bin / name).chmod(0o755)
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.folder),
            "DOCKER_TEST_STATE": str(self.state_file),
            "DOCKER_TEST_ROOT": str(self.folder),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C.UTF-8",
        }

    def write_state(self):
        self.state_file.write_text(json.dumps(self.state))

    def read_state(self):
        self.state = json.loads(self.state_file.read_text())
        return self.state

    def add_container(self, name="ai-dev-alice-3022", *, running=True, marker=True, network="ai-dev-alice-30-net"):
        self.state["containers"][name] = {
            "id": "old-" + name, "running": running, "marker": marker,
            "networks": [network], "image": "fixture:old", "user": "alice",
            "volume": "ai-dev-alice-data",
        }
        if network not in self.state["networks"]:
            self.state["networks"].append(network)
        self.write_state()

    def run_script(self, script, args, **settings):
        env = dict(self.env, **settings)
        before = {c["id"] for c in json.loads(self.state_file.read_text())["containers"].values()}
        result = subprocess.run(
            [BASH, str(ROOT / "scripts" / script), *args],
            env=env, text=True, capture_output=True, timeout=20,
        )
        self.read_state()
        leaked_locks = [name for name,c in self.state["containers"].items()
                        if c["id"].startswith("fixture-lock-") and c["id"] not in before]
        self.assertEqual(leaked_locks, [], "operation leaked its own lock")
        return result

    def deploy(self, *extra, **settings):
        return self.run_script("deploy.sh", [
            "--user", "alice", "--port", "30", "--image", "fixture:dev",
            "--password", "fixture-password", "--no-gpu", *extra,
        ], **settings)

    def browser(self, command="start", **settings):
        return self.run_script("browser.sh", [
            command, "--user", "alice", "--base", "30", "--image", "fixture:browser",
        ], **settings)

    def mutation_calls(self):
        return [c["args"] for c in self.state["calls"] if
                (c["args"][0] in ("create","run","start","stop","rename","rm") or
                 c["args"][:2] in (["volume","create"],["network","create"],["network","connect"])) and
                "ai-dev-env.role=operation-lock" not in c["args"] and
                not (c["args"][0] == "rm" and c["args"][-1].startswith("fixture-lock-"))]


class DeploymentTest(ScriptFixture):
    def test_default_deploy_uses_latest_tag_without_light(self):
        result = self.run_script("deploy.sh", ["--user", "alice", "--port", "30",
                                                  "--password", "fixture-password", "--no-gpu"])
        self.assertEqual(result.returncode, 0, result.stderr)
        image = self.state["containers"]["ai-dev-alice-3022"]["image"]
        self.assertEqual(image, "tonyooo/ai-dev-env:latest")

    def test_version_pin_uses_version_tag_without_light(self):
        result = self.run_script("deploy.sh", ["--user", "alice", "--port", "30",
                                                  "--password", "fixture-password", "--no-gpu"], VERSION="0.1.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        image = self.state["containers"]["ai-dev-alice-3022"]["image"]
        self.assertEqual(image, "tonyooo/ai-dev-env:0.1.2")

    def test_mismatched_latest_images_are_rejected_before_mutation(self):
        releases = {"tonyooo/ai-dev-env:latest": "0.1.2",
                    "tonyooo/ai-dev-browser:latest": "0.1.1"}
        result = self.run_script("deploy.sh", ["--user", "alice", "--port", "30",
                                                  "--password", "fixture-password", "--no-gpu", "--with-browser"],
                                 TEST_IMAGE_RELEASES=json.dumps(releases))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different releases", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_browser_start_rejects_mismatched_latest_images(self):
        dev_image = "tonyooo/ai-dev-env:latest"
        browser_image = "tonyooo/ai-dev-browser:latest"
        self.add_container()
        self.state["containers"]["ai-dev-alice-3022"]["image"] = dev_image
        self.write_state()
        result = self.run_script("browser.sh", ["start", "--user", "alice", "--base", "30"],
                                 TEST_IMAGE_RELEASES=json.dumps({dev_image: "0.1.2", browser_image: "0.1.1"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different releases", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_same_version_different_revisions_are_rejected(self):
        dev = "tonyooo/ai-dev-env:0.1.2"
        browser = "tonyooo/ai-dev-browser:0.1.2"
        result = self.run_script("deploy.sh", ["--user", "alice", "--port", "30",
                                                  "--password", "fixture-password", "--no-gpu", "--with-browser"],
                                 VERSION="0.1.2", TEST_IMAGE_RELEASES=json.dumps({dev: "0.1.2", browser: "0.1.2"}),
                                 TEST_IMAGE_REVISIONS=json.dumps({dev: "commit-a", browser: "commit-b"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revision mismatch", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_existing_browser_image_is_checked_before_recreate(self):
        dev = "tonyooo/ai-dev-env:latest"
        browser = "tonyooo/ai-dev-browser:latest"
        self.add_container()
        self.add_container("ai-dev-alice-30-browser")
        self.state["containers"]["ai-dev-alice-30-browser"]["image"] = "fixture:running-browser"
        self.write_state()
        releases = {dev: "0.1.2", browser: "0.1.2", "fixture:running-browser": "0.1.1"}
        result = self.run_script("deploy.sh", ["--user", "alice", "--port", "30", "--recreate",
                                                  "--password", "fixture-password", "--no-gpu", "--with-browser"],
                                 TEST_IMAGE_RELEASES=json.dumps(releases))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different releases", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_pinned_version_must_match_image_labels(self):
        dev = "tonyooo/ai-dev-env:0.1.2"
        browser = "tonyooo/ai-dev-browser:0.1.2"
        labels = {dev: "0.1.1", browser: "0.1.1"}
        result = self.run_script("deploy.sh", ["--user", "alice", "--port", "30",
                                                  "--password", "fixture-password", "--no-gpu", "--with-browser"],
                                 VERSION="0.1.2", TEST_IMAGE_RELEASES=json.dumps(labels))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match VERSION", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_new_deploy_uses_exclusive_network(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state["containers"]["ai-dev-alice-3022"]["networks"], ["ai-dev-alice-30-net"])
        self.assertTrue(all(c["pathconv"] == "1" for c in self.state["calls"]))

    def test_recreate_recovers_network_without_browser_option(self):
        self.add_container(network="bridge")
        self.add_container("ai-dev-alice-30-browser")
        result = self.deploy("--recreate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state["containers"]["ai-dev-alice-3022"]["networks"], ["ai-dev-alice-30-net"])
        calls = [c["args"] for c in self.state["calls"]]
        persist = next(i for i,a in enumerate(calls) if "--persist-home" in a)
        stop = next(i for i,a in enumerate(calls) if a[0] == "stop")
        self.assertLess(persist, stop)
        self.assertIn("ai-dev-alice-30-browser", self.state["containers"])

    def test_second_port_rejected_before_mutation(self):
        self.add_container("ai-dev-alice-3122")
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another instance", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_existing_without_recreate_rejected_before_mutation(self):
        self.add_container()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutation_calls(), [])

    def test_persistence_failure_retains_original(self):
        self.add_container()
        result = self.deploy("--recreate", TEST_PERSIST_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["containers"]["ai-dev-alice-3022"]["image"], "fixture:old")
        self.assertFalse(any(a[0] in ("stop","rename","rm") for a in self.mutation_calls()))

    def test_uninitialized_stopped_legacy_is_not_started_or_removed(self):
        self.add_container(running=False, marker=False)
        result = self.deploy("--recreate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped legacy", result.stderr)
        self.assertFalse(self.state["containers"]["ai-dev-alice-3022"]["running"])
        self.assertFalse(any(a[0] in ("start","stop","rename","rm") for a in self.mutation_calls()))

    def legacy_home(self, **entries):
        self.add_container(running=False, marker=False)
        self.state["containers"]["ai-dev-alice-3022"]["home"] = {
            "workspace": {"kind":"symlink","target":"/home/dev/.ai-data/workspace"}, **entries,
        }
        self.write_state()

    def test_stopped_legacy_offline_recovers_files_empty_files_and_directory(self):
        self.legacy_home(**{
            ".gitconfig":{"kind":"file","content":"latest"},
            ".npmrc":{"kind":"file","content":""},
            ".config":{"kind":"directory","files":{"settings":"saved settings"}},
        })
        old_id = self.state["containers"]["ai-dev-alice-3022"]["id"]
        result = self.deploy("--recreate")
        self.assertEqual(result.returncode, 0, result.stderr)
        root = Path(self.state["volumes"]["ai-dev-alice-data"])
        self.assertEqual((root / "home-config/gitconfig").read_text(), "latest")
        self.assertEqual((root / "home-config/npmrc").read_bytes(), b"")
        self.assertEqual((root / "home-config/.config/settings").read_text(), "saved settings")
        self.assertFalse(any(c["args"] == ["start",old_id] or c["args"] == ["start","ai-dev-alice-3022"] for c in self.state["calls"]))

    def test_stopped_legacy_missing_optional_mapping_is_not_a_daemon_error(self):
        self.legacy_home()
        result = self.deploy("--recreate")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stopped_legacy_daemon_copy_error_retains_original(self):
        self.legacy_home(**{".gitconfig":{"kind":"file","content":"source retained"}})
        result = self.deploy("--recreate", TEST_COPY_FAIL_NAME=".gitconfig")
        self.assertNotEqual(result.returncode, 0)
        old = self.state["containers"]["ai-dev-alice-3022"]
        self.assertFalse(old["running"])
        self.assertEqual(old["home"][".gitconfig"]["content"], "source retained")
        self.assertFalse(any(a[0] in ("start","stop","rename","rm") for a in self.mutation_calls()))

    def test_stopped_legacy_unsafe_archive_retains_original(self):
        self.legacy_home(**{".config":{"kind":"directory","files":{"../outside":"rejected"}}})
        result = self.deploy("--recreate")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state["containers"]["ai-dev-alice-3022"]["running"])
        self.assertFalse(any(a[0] in ("start","stop","rename","rm") for a in self.mutation_calls()))

    def test_stopped_legacy_late_failure_keeps_volume_unchanged(self):
        self.legacy_home(**{
            ".config": {"kind": "directory", "files": {"settings": "new"}},
            ".gitconfig": {"kind": "file", "content": "new"},
        })
        root = self.folder / "ai-dev-alice-data"
        (root / "home-config/.config").mkdir(parents=True)
        (root / "home-config/.config/settings").write_text("old")
        result = self.deploy("--recreate", TEST_COPY_FAIL_NAME=".gitconfig")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((root / "home-config/.config/settings").read_text(), "old")
        self.assertFalse((root / ".ai-dev-recovery").exists())
        self.assertIn("ai-dev-alice-3022", self.state["containers"])

    def test_stopped_legacy_nonstandard_root_link_retains_old_container(self):
        self.legacy_home(**{".codex": {"kind": "symlink", "target": "/home/dev/custom-codex"}})
        result = self.deploy("--recreate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ai-dev-alice-3022", self.state["containers"])
        self.assertFalse(any(a[0] in ("start", "stop", "rename", "rm") for a in self.mutation_calls()))

    def test_stopped_fixed_container_can_be_recreated(self):
        self.add_container(running=False, marker=True)
        result = self.deploy("--recreate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.state["containers"]["ai-dev-alice-3022"]["running"])

    def test_failed_start_rolls_back_original(self):
        self.add_container()
        result = self.deploy("--recreate", TEST_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        old = self.state["containers"]["ai-dev-alice-3022"]
        self.assertEqual(old["image"], "fixture:old")
        self.assertTrue(old["running"])
        self.assertEqual(list(self.state["containers"]), ["ai-dev-alice-3022"])

    def test_failed_rename_restarts_original_dev(self):
        self.add_container()
        result = self.deploy("--recreate", TEST_RENAME_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.state["containers"]["ai-dev-alice-3022"]["running"])
        self.assertEqual(list(self.state["containers"]), ["ai-dev-alice-3022"])

    def test_temporarily_started_original_is_stopped_on_persist_failure(self):
        self.add_container(running=False)
        result = self.deploy("--recreate", TEST_PERSIST_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state["containers"]["ai-dev-alice-3022"]["running"])

    def test_temporarily_started_original_is_stopped_on_readiness_failure(self):
        self.add_container(running=False)
        result = self.deploy("--recreate", TEST_INITIAL_NOT_READY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state["containers"]["ai-dev-alice-3022"]["running"])

    def test_temporarily_started_original_is_stopped_on_network_failure(self):
        self.add_container(running=False, network="bridge")
        result = self.deploy("--recreate", TEST_NETWORK_CREATE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state["containers"]["ai-dev-alice-3022"]["running"])

    def test_foreign_operation_lock_is_never_removed(self):
        name = "ai-dev-alice-operation-lock"
        self.add_container(name, running=False)
        original_id = self.state["containers"][name]["id"]
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another operation", result.stderr)
        self.assertEqual(self.state["containers"][name]["id"], original_id)
        self.assertFalse(any(c["args"][0] == "rm" for c in self.state["calls"]))

    def test_lock_creation_error_does_not_claim_another_owner(self):
        result = self.deploy(TEST_LOCK_CREATE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not create operation lock", result.stderr)
        self.assertNotIn("another operation", result.stderr)
        self.assertEqual(self.state["containers"], {})

    def test_concurrent_ports_cannot_create_two_user_instances(self):
        command = [BASH, str(ROOT / "scripts/deploy.sh"), "--user", "alice", "--port", "30",
                   "--image", "fixture:dev", "--password", "fixture-password", "--no-gpu"]
        first = subprocess.Popen(command, env=dict(self.env, TEST_PAUSE_LISTING="1"),
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.folder / "listing-paused").exists():
                if first.poll() is not None or time.monotonic() > deadline:
                    self.fail("first deployment did not reach the protected listing")
                time.sleep(0.01)
            second = self.deploy("--port", "31")
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("another operation", second.stderr)
            self.assertIn("ai-dev-alice-operation-lock", self.state["containers"])
        finally:
            (self.folder / "listing-release").touch()
            stdout, stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, stderr)
        self.read_state()
        self.assertEqual(list(self.state["containers"]), ["ai-dev-alice-3022"])

    def test_deploy_timeout_is_failure(self):
        result = self.deploy(TEST_NOT_READY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not ready", result.stderr)
        self.assertNotIn("is ready", result.stdout)
        self.assertNotIn("ai-dev-alice-3022", self.state["containers"])

    def test_first_deploy_start_failure_removes_new_container(self):
        result = self.deploy(TEST_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ai-dev-alice-3022", self.state["containers"])

    def test_recreate_timeout_rolls_back_original(self):
        self.add_container()
        result = self.deploy("--recreate", TEST_NOT_READY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["containers"]["ai-dev-alice-3022"]["image"], "fixture:old")
        self.assertTrue(self.state["containers"]["ai-dev-alice-3022"]["running"])

    def test_old_browser_client_rejected_before_mutation(self):
        self.add_container()
        result = self.browser(TEST_DOCKER_VERSION="25.0.0 29.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("26+", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_old_browser_engine_rejected_before_mutation(self):
        self.add_container()
        result = self.browser(TEST_DOCKER_VERSION="29.0.0 25.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutation_calls(), [])

    def test_with_browser_preflight_precedes_dev_creation(self):
        result = self.deploy("--with-browser", TEST_DOCKER_VERSION="25.0.0 29.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("26+", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_running_legacy_container_is_persisted_before_recreate(self):
        self.add_container(marker=False)
        result = self.deploy("--recreate")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [c["args"] for c in self.state["calls"]]
        persist = next(i for i,a in enumerate(calls) if "sh" in a and "-eu" in a)
        stop = next(i for i,a in enumerate(calls) if a[0] == "stop")
        self.assertLess(persist, stop)

    def test_new_browser_uses_only_its_user_network(self):
        self.add_container()
        result = self.browser()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state["containers"]["ai-dev-alice-30-browser"]["networks"], ["ai-dev-alice-30-net"])
        helpers = [c["args"] for c in self.state["calls"] if c["args"][0] == "run"]
        self.assertTrue(helpers)
        self.assertTrue(all(a[a.index("--network")+1] == "none" for a in helpers))

    def test_browser_creation_persists_graceful_shutdown_timeout(self):
        self.add_container()
        result = self.browser()
        self.assertEqual(result.returncode, 0, result.stderr)
        inspected = subprocess.check_output(
            [str(self.bin / "docker"), "inspect", "-f", "{{.Config.StopTimeout}}", "ai-dev-alice-30-browser"],
            env=self.env, text=True, timeout=5,
        )
        self.assertEqual(inspected.strip(), "25")

    def test_browser_stop_overrides_legacy_default_timeout(self):
        self.add_container()
        self.add_container("ai-dev-alice-30-browser")
        result = self.browser("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["stop", "--timeout", "25", "ai-dev-alice-30-browser"],
                      [c["args"] for c in self.state["calls"]])
        self.assertFalse(self.state["containers"]["ai-dev-alice-30-browser"]["running"])

    def test_uid_remapping_rejected_before_mutation(self):
        self.add_container()
        result = self.browser(TEST_UID="2000")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UID/GID", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_shared_bridge_rejected_before_browser_mutation(self):
        self.add_container(network="bridge")
        result = self.browser()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shared bridge", result.stderr)
        self.assertEqual(self.mutation_calls(), [])

    def test_browser_timeout_is_failure(self):
        self.add_container()
        result = self.browser(TEST_NOT_READY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not ready", result.stderr)
        self.assertNotIn("is ready", result.stdout)
        self.assertNotIn("ai-dev-alice-30-browser", self.state["containers"])

    def test_first_browser_start_failure_removes_new_container(self):
        self.add_container()
        result = self.browser(TEST_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ai-dev-alice-30-browser", self.state["containers"])

    def test_browser_restart_failure_restores_original(self):
        self.add_container()
        self.add_container("ai-dev-alice-30-browser")
        result = self.browser("restart", TEST_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state["containers"]["ai-dev-alice-30-browser"]["image"], "fixture:old")
        self.assertTrue(self.state["containers"]["ai-dev-alice-30-browser"]["running"])
        stopped = [c["args"] for c in self.state["calls"] if c["args"][0] == "stop"]
        self.assertGreaterEqual(len(stopped), 2)
        self.assertTrue(all(a[1:3] == ["--timeout", "25"] for a in stopped))

    def test_browser_rollback_restores_stopped_state_with_graceful_timeout(self):
        self.add_container()
        self.add_container("ai-dev-alice-30-browser", running=False)
        result = self.browser("restart", TEST_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state["containers"]["ai-dev-alice-30-browser"]["running"])
        stopped = [c["args"] for c in self.state["calls"] if c["args"][0] == "stop"]
        self.assertGreaterEqual(len(stopped), 3)
        self.assertTrue(all(a[1:3] == ["--timeout", "25"] for a in stopped))

    def test_failed_rename_restarts_original_browser(self):
        self.add_container()
        self.add_container("ai-dev-alice-30-browser")
        result = self.browser("restart", TEST_RENAME_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.state["containers"]["ai-dev-alice-30-browser"]["running"])

    def test_browser_missing_image_retains_original(self):
        self.add_container()
        self.add_container("ai-dev-alice-30-browser")
        result = self.browser("restart", TEST_MISSING_IMAGE="fixture:browser")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutation_calls(), [])

    def test_invalid_port_and_missing_value_are_clear_failures(self):
        for value in ("0", "656", "0000", "abc"):
            result = self.run_script("deploy.sh", ["--user","alice","--port",value])
            self.assertNotEqual(result.returncode, 0)
        result = self.run_script("browser.sh", ["start","--user"])
        self.assertIn("requires a value", result.stderr)
        self.assertEqual(self.mutation_calls(), [])


if __name__ == "__main__":
    unittest.main()
