#!/usr/bin/env python3
"""Exercise real dev/browser images using disposable, uniquely named resources."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from integration_s6 import check_s6_guard

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "Audit-only_42!"
HTML = """<!doctype html><meta charset="utf-8"><title>Browser acceptance</title>
<style>body{font:20px sans-serif;margin:50px}textarea{display:block;width:650px;height:160px}</style>
<h1>Browser acceptance</h1><label>Audit text<textarea id="text" aria-label="Audit text"></textarea></label>
<label>Upload file<input id="upload" aria-label="Upload file" type="file"></label>
<button onclick="document.getElementById('result').textContent='Clicked'">Confirm</button>
<p id="result">Ready</p>"""


def redact(text):
    text = text.replace(PASSWORD, "[REDACTED]")
    return re.sub(r"(?im)^(\s*(?:password|account)\s*[:=]).*$", r"\1 [REDACTED]", text)


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex[:8]
        self.user = "audit-" + self.run_id
        self.other = self.user + "-other"
        self.base = self.free_base()
        self.dev = f"ai-dev-{self.user}-{self.base}22"
        self.browser = f"ai-dev-{self.user}-{self.base}-browser"
        self.network = f"ai-dev-{self.user}-{self.base}-net"
        self.volume = f"ai-dev-{self.user}-data"
        self.other_dev = f"ai-dev-{self.other}-{self.base + 1}22"
        self.other_network = f"ai-dev-{self.other}-{self.base + 1}-net"
        self.other_volume = f"ai-dev-{self.other}-data"
        self.results = []
        self.extra_users = []
        self.extra_networks = []
        self.extra_volumes = []
        self.output = Path(args.output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.backup = self.output / ("backup with spaces-" + self.run_id)
        self.backup.mkdir()
        if os.name == "nt":
            self.bash = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
        else:
            self.bash = shutil.which("bash") or "bash"
        self.env = dict(os.environ, BROWSER_IDLE_SECONDS="6", BROWSER_MEMORY="2g", BROWSER_CPUS="2")

    @staticmethod
    def free_base():
        for base in range(410, 640, 2):
            with socket.socket() as first, socket.socket() as second:
                try:
                    first.bind(("127.0.0.1", int(f"{base}22")))
                    second.bind(("127.0.0.1", int(f"{base + 1}22")))
                    return base
                except OSError:
                    continue
        raise RuntimeError("No free acceptance SSH port pair")

    def command(self, argv, *, timeout=180, check=True, input=None):
        result = subprocess.run(argv, cwd=ROOT, env=self.env, input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(redact(f"Command failed ({result.returncode}): {argv[0]}\n"
                                      + result.stdout[-3000:] + result.stderr[-3000:]))
        return result

    def docker(self, *args, **kwargs):
        return self.command(["docker", *map(str, args)], **kwargs)

    def script(self, name, *args, **kwargs):
        return self.command([self.bash, (ROOT / "scripts" / name).as_posix(), *map(str, args)], **kwargs)

    def py(self, code, *, container=None, root=False, timeout=120):
        args = ["exec"] + (["-u", "0"] if root else [])
        return self.docker(*args, container or self.dev, "python3", "-c", code, timeout=timeout).stdout.strip()

    def cli(self, *args, timeout=150):
        result = self.docker("exec", self.dev, "ai-browser", *args, timeout=timeout)
        payload = json.loads(result.stdout)
        if payload.get("ok") is False:
            raise RuntimeError("Browser operation failed: " + json.dumps(payload))
        return payload

    def eval(self, expression):
        code = """import importlib.util,importlib.machinery,json,os
os.environ['BROWSER_CDP_HOST']=os.environ['BROWSER_HOST']
loader=importlib.machinery.SourceFileLoader('page_browser','/usr/local/bin/page-browser')
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec);loader.exec_module(module)
client=module.CdpClient(module.discover_ws_url(module.cdp_port()));client.connect()
try:
 session=module.attach_page(client)
 result=client.call('Runtime.evaluate',{'expression':EXPRESSION,'awaitPromise':True,'returnByValue':True},session_id=session)
 if result.get('exceptionDetails'): raise RuntimeError(result['exceptionDetails'])
 print(json.dumps(result.get('result',{}).get('value')))
finally: client.close()
""".replace("EXPRESSION", repr(expression))
        return json.loads(self.py(code))

    def record(self, name, callback):
        start = time.monotonic()
        try:
            detail = callback()
            self.results.append({"check": name, "status": "PASS", "seconds": round(time.monotonic() - start, 2),
                                 "detail": detail or ""})
            print("PASS " + name, flush=True)
        except Exception as error:
            self.results.append({"check": name, "status": "FAIL", "seconds": round(time.monotonic() - start, 2),
                                 "detail": redact(str(error))})
            print("FAIL " + name + ": " + redact(str(error)), flush=True)
            raise
        finally:
            self.write_results()

    def write_results(self):
        payload = {"run_id": self.run_id, "dev_image": self.args.dev_image,
                   "browser_image": self.args.browser_image, "checks": self.results}
        (self.output / "integration.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    def deploy(self, user=None, base=None):
        self.script("deploy.sh", "--user", user or self.user, "--port", base or self.base,
                    "--password", PASSWORD, "--image", self.args.dev_image,
                    "--cpu", "2", "--memory", "1g", "--no-gpu")
        with socket.create_connection(("127.0.0.1", int(f"{base or self.base}22")), timeout=10) as connection:
            assert connection.recv(256).startswith(b"SSH-2.0-"), "Published SSH endpoint did not answer"

    def authenticate(self):
        code = """import os,pathlib,subprocess
path=pathlib.Path('/tmp/audit-askpass')
path.write_text('#!/bin/sh\\nprintf %s '+PASSWORD+'\\n');path.chmod(0o700)
try:
 env=dict(os.environ,DISPLAY=':audit',SSH_ASKPASS=str(path),SSH_ASKPASS_REQUIRE='force')
 result=subprocess.run(['setsid','-w','ssh','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null',
  '-o','PreferredAuthentications=password','-o','PubkeyAuthentication=no','-o','NumberOfPasswordPrompts=1',
  '-o','ConnectTimeout=5','dev@127.0.0.1','id','-u'],env=env,stdin=subprocess.DEVNULL,
  capture_output=True,text=True,timeout=20)
 if result.returncode: raise RuntimeError('SSH password authentication failed')
 assert result.stdout.strip()=='1000',result.stdout
 print('Authenticated as dev')
finally:path.unlink(missing_ok=True)
""".replace("PASSWORD", repr(PASSWORD))
        assert self.py(code) == "Authenticated as dev"

    def wait_ssh(self):
        for _ in range(50):
            result = self.docker("exec", self.dev, "bash", "-c", ": > /dev/tcp/127.0.0.1/22", check=False)
            if result.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("SSH did not recover")

    def start_browser(self):
        self.script("browser.sh", "start", "--user", self.user, "--base", self.base,
                    "--image", self.args.browser_image)
        assert self.cli("status")["state"] == "idle"
        assert self.docker("inspect", "-f", "{{.Config.StopTimeout}}", self.browser).stdout.strip() == "25"

    def prepare_files(self):
        self.py("from pathlib import Path\n"
                + f"Path('/home/dev/workspace/audit.html').write_text({HTML!r})\n"
                + "Path('/home/dev/workspace/upload space.txt').write_text('workspace upload verified')\n"
                + "Path('/home/dev/downloads/download.txt').write_text('downloads upload verified')\n")

    def duplicate_user(self):
        result = self.script("deploy.sh", "--user", self.user, "--port", self.base + 1,
                             "--image", self.args.dev_image, "--no-gpu", check=False)
        assert result.returncode != 0, "Duplicate user deployment unexpectedly succeeded"
        names = self.docker("ps", "-a", "--filter", f"label=ai-dev-env.user={self.user}", "--format", "{{.Names}}").stdout.splitlines()
        assert names == [self.dev], names

    def concurrent_user(self):
        user = self.user + "-race"
        base = self.free_base()
        self.extra_users.append(user)
        self.extra_volumes.append(f"ai-dev-{user}-data")
        self.extra_networks.extend(f"ai-dev-{user}-{port}-net" for port in (base, base + 1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(self.script, "deploy.sh", "--user", user, "--port", port,
                                   "--password", PASSWORD, "--image", self.args.dev_image,
                                   "--cpu", "1", "--memory", "512m", "--no-gpu", check=False)
                       for port in (base, base + 1)]
            results = [future.result() for future in pending]
        assert sum(result.returncode == 0 for result in results) == 1, [result.returncode for result in results]
        names = self.docker("ps", "-a", "--filter", f"label=ai-dev-env.user={user}", "--format", "{{.Names}}").stdout.splitlines()
        assert len(names) == 1 and names[0] in {f"ai-dev-{user}-{base}22", f"ai-dev-{user}-{base + 1}22"}, names
        return "Two simultaneous deployments produced exactly one live instance and no leftover operation lock"

    def network_isolation(self):
        self.deploy(self.other, self.base + 1)
        primary = json.loads(self.docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", self.dev).stdout)
        other = json.loads(self.docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", self.other_dev).stdout)
        assert set(primary) == {self.network}, primary.keys()
        assert set(other) == {self.other_network}, other.keys()
        ip = other[self.other_network]["IPAddress"]
        self.docker("exec", "-d", self.other_dev, "python3", "-m", "http.server", "18080",
                    "--bind", "0.0.0.0", "--directory", "/tmp")
        self.py("import socket,time\n"
                "for _ in range(30):\n"
                " try:\n"
                "  s=socket.create_connection(('127.0.0.1',18080),timeout=1);s.close();break\n"
                " except OSError: time.sleep(0.1)\n"
                "else: raise RuntimeError('Private fixture server did not start')", container=self.other_dev)
        code = f"""import socket
try:
 s=socket.create_connection(({ip!r},18080),timeout=2);s.close()
 print('reachable')
except OSError: print('isolated')
"""
        assert self.py(code) == "isolated", "Unpublished service is reachable across user networks"
        return "Live unpublished TCP service reachable locally and blocked from the other user network"

    def config_restart(self):
        self.py("from pathlib import Path\n"
                "p=Path('/home/dev/.gitconfig');p.write_text('[audit]\\n value=old\\n')\n"
                "q=p.with_suffix('.new');q.write_text('[audit]\\n value=restart\\n');q.replace(p)\n"
                "p=Path('/home/dev/.bashrc.extra');q=p.with_suffix('.new');q.write_text('');q.replace(p)\n")
        self.docker("restart", self.dev)
        self.wait_ssh()
        assert self.py("from pathlib import Path;print(Path('/home/dev/.gitconfig').read_text())") == "[audit]\n value=restart"
        assert self.py("from pathlib import Path;print(Path('/home/dev/.bashrc.extra').is_symlink(),Path('/home/dev/.bashrc.extra').stat().st_size)") == "True 0"

    def cold_tabs(self):
        tabs = self.cli("tabs")
        assert tabs["count"] == len(tabs["tabs"])
        self.cli("close", "all")

    def page_actions(self):
        self.cli("open", "file:///home/dev/workspace/audit.html")
        snapshot = self.cli("snapshot")
        assert snapshot["title"] == "Browser acceptance", snapshot
        ref = next(el["ref"] for el in snapshot["elements"] if el["name"] == "Audit text")
        result = self.cli("act", json.dumps([{"kind": "fill", "ref": ref, "text": "CDP fill verified"}]))
        assert result["completed"] == 1, result
        assert self.eval("document.getElementById('text').value") == "CDP fill verified"
        self.eval(f"localStorage.setItem('audit-profile',{json.dumps(self.run_id)});true")

    def upload(self):
        for path, expected in [("workspace/upload space.txt", "workspace upload verified"),
                               ("downloads/download.txt", "downloads upload verified")]:
            snapshot = self.cli("snapshot")
            ref = next(el["ref"] for el in snapshot["elements"] if el["name"] == "Upload file")
            result = self.cli("act", json.dumps([{"kind": "upload", "ref": ref, "paths": [path]}]))
            assert result["completed"] == 1, result
            assert self.eval("document.getElementById('upload').files[0].text()") == expected

    def long_input(self):
        self.eval("document.getElementById('text').value='';document.getElementById('text').focus();true")
        result = self.cli("type", "a" * 4096)
        assert result["completed"] == 1, result
        assert self.eval("document.getElementById('text').value") == "a" * 4096
        for text in ("中文输入 verified", "你好🙂🚀", "第一行\nsecond line\t中文", "中文输入 verified"):
            self.eval("document.getElementById('text').value='';document.getElementById('text').focus();true")
            self.cli("type", text)
            assert self.eval("document.getElementById('text').value") == text
        self.eval("document.getElementById('text').value='';document.getElementById('text').focus();true")
        self.cli("type-keys", "中文")
        assert self.eval("document.getElementById('text').value") == "中文"

        # A maximum-length Unicode paste can be combined with another action.
        self.eval("document.getElementById('text').value='';document.getElementById('text').focus();true")
        text = "a" * 4095 + "中"
        code = """import importlib.machinery,importlib.util,json
loader=importlib.machinery.SourceFileLoader('ai_browser','/usr/local/bin/ai-browser')
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec);loader.exec_module(module)
print(json.dumps(module.request('POST','/v1/actions',{'actions':[
 {'type':'type','text':TEXT},{'type':'key','key':'Tab'}]})))
""".replace("TEXT", repr(text))
        started = time.monotonic()
        result = json.loads(self.py(code, timeout=180))
        elapsed = time.monotonic() - started
        assert result["completed"] == 2, result
        assert self.eval("document.getElementById('text').value") == text

        # Successful event dispatch does not imply that an application accepted it.
        self.eval("window.blockPaste=e=>e.preventDefault();document.getElementById('text').addEventListener('paste',window.blockPaste);document.getElementById('text').value='';document.getElementById('text').focus();true")
        self.cli("type", "中文")
        assert self.eval("document.getElementById('text').value") == ""
        self.eval("document.getElementById('text').removeEventListener('paste',window.blockPaste);true")
        self.eval("document.getElementById('text').value='Unicode and 4096 characters verified';true")
        return f"4096-character mixed paste and subsequent key completed in {elapsed:.1f}s"

    def auth(self):
        # Read only this disposable user's generated token; never print its value.
        code = f"""import base64,pathlib,socket,os,urllib.parse
address=({self.browser!r},6080)
auth=pathlib.Path('/home/dev/.ai-data/browser/state/screen-auth').read_text().strip()
for path,supplied,upgrade in (('/vnc.html','',False),('/vnc.html',auth,False),('/websockify','',True),('/websockify',auth,True)):
 with socket.create_connection(address,timeout=30) as sock:
  sock.settimeout(30)
  headers='GET '+path+' HTTP/1.1\\r\\nHost: localhost\\r\\n'
  if upgrade: headers+='Upgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Key: '+base64.b64encode(os.urandom(16)).decode()+'\\r\\nSec-WebSocket-Version: 13\\r\\n'
  if supplied: headers+='Authorization: Basic '+base64.b64encode(supplied.encode()).decode()+'\\r\\n'
  sock.sendall((headers+'\\r\\n').encode())
  data=b''
  while b'\\r\\n' not in data:
   chunk=sock.recv(1024)
   if not chunk or len(data)>4096: raise RuntimeError('Incomplete WebSocket status line')
   data+=chunk
  print(data.split(b'\\r\\n',1)[0].decode())
form=urllib.parse.urlencode(dict(zip(('username','password'),auth.split(':',1)))).encode()
with socket.create_connection(address,timeout=30) as sock:
 sock.settimeout(30)
 head=b'POST /__screen/login HTTP/1.1\\r\\nHost: localhost\\r\\nOrigin: http://localhost\\r\\nContent-Type: application/x-www-form-urlencoded\\r\\nContent-Length: '+str(len(form)).encode()+b'\\r\\n\\r\\n'
 sock.sendall(head+form); data=b''
 while b'\\r\\n\\r\\n' not in data:
  chunk=sock.recv(4096)
  if not chunk: raise RuntimeError('Incomplete login response')
  data+=chunk
 print(data.split(b'\\r\\n',1)[0].decode())
 cookie=next(line.split(b': ',1)[1].split(b';',1)[0] for line in data.split(b'\\r\\n') if line.lower().startswith(b'set-cookie: '))
for path,upgrade in (('/vnc.html',False),('/websockify',True)):
 with socket.create_connection(address,timeout=30) as sock:
  sock.settimeout(30)
  head=b'GET '+path.encode()+b' HTTP/1.1\\r\\nHost: localhost\\r\\nCookie: '+cookie+b'\\r\\n'
  if upgrade: head+=b'Upgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Key: '+base64.b64encode(os.urandom(16))+b'\\r\\nSec-WebSocket-Version: 13\\r\\n'
  sock.sendall(head+b'\\r\\n'); data=b''
  while b'\\r\\n' not in data:
   chunk=sock.recv(4096)
   if not chunk: raise RuntimeError('Incomplete session response')
   data+=chunk
  print(data.split(b'\\r\\n',1)[0].decode())
"""
        lines = self.py(code).splitlines()
        assert len(lines) == 7 and all(code in line for code, line in zip(("401", "200", "401", "101", "303", "200", "101"), lines)), lines
        return "Basic and form login protect the page and allow authenticated WebSocket connections"

    def crash_recovery(self):
        pid = self.docker("exec", self.browser, "pgrep", "-f", "^/usr/bin/python3 /usr/local/bin/browser-control$").stdout.strip()
        assert pid.isdigit(), pid
        self.docker("exec", self.browser, "kill", "-TERM", pid)
        for _ in range(30):
            self.docker("exec", self.dev, "ai-browser", "status", check=False)
            if self.docker("exec", self.browser, "kill", "-0", pid, check=False).returncode:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Terminated control process was not reaped")
        self.cli("tabs")
        new_pid = self.docker("exec", self.browser, "pgrep", "-f", "^/usr/bin/python3 /usr/local/bin/browser-control$").stdout.strip()
        assert new_pid.isdigit() and new_pid != pid, (pid, new_pid)
        self.assert_profile()

    def assert_profile(self):
        self.cli("open", "file:///home/dev/workspace/audit.html")
        assert self.eval("localStorage.getItem('audit-profile')") == self.run_id

    def idle_restore(self):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.cli("status")["state"] == "idle":
                break
            time.sleep(1)
        else:
            raise RuntimeError("Desktop did not enter idle")
        self.cli("tabs")
        self.cli("close", "all")
        self.cli("open", "file:///home/dev/workspace/audit.html")
        assert self.cli("snapshot")["title"] == "Browser acceptance"
        self.assert_profile()

    def browser_recreate(self):
        self.script("browser.sh", "restart", "--user", self.user, "--base", self.base,
                    "--image", self.args.browser_image)
        self.docker("exec", self.browser, "browser-open", timeout=120)
        self.assert_profile()

    def recreate(self):
        before = self.py("import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/home/dev/.ssh/ssh_host_ed25519_key.pub').read_bytes()).hexdigest())")
        self.py("from pathlib import Path;p=Path('/home/dev/.gitconfig');q=p.with_suffix('.new');q.write_text('[audit]\\n value=recreate\\n');q.replace(p)")
        self.script("deploy.sh", "--user", self.user, "--port", self.base,
                    "--image", self.args.dev_image, "--recreate", "--no-gpu", "--cpu", "2", "--memory", "1g")
        assert self.py("from pathlib import Path;print(Path('/home/dev/.gitconfig').read_text())") == "[audit]\n value=recreate"
        after = self.py("import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/home/dev/.ssh/ssh_host_ed25519_key.pub').read_bytes()).hexdigest())")
        assert before == after, "SSH host identity changed"
        assert self.py("from pathlib import Path;print(Path('/home/dev/.bashrc.extra').is_symlink(),Path('/home/dev/.bashrc.extra').stat().st_size)") == "True 0"
        self.cli("tabs")
        self.cli("open", "file:///home/dev/workspace/audit.html")
        assert self.cli("snapshot")["title"] == "Browser acceptance"
        self.assert_profile()
        self.authenticate()

    def screenshot(self):
        self.cli("shot", "-o", "/tmp/audit-screen.png")
        output = self.output / "browser-screen.png"
        self.docker("cp", f"{self.dev}:/tmp/audit-screen.png", str(output))
        assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        return {"file": output.name, "bytes": output.stat().st_size}

    def backup_restore(self):
        self.docker("stop", self.browser, self.dev)
        self.script("backup.sh", "--user", self.user, "--output", self.backup.as_posix(), "--image", self.args.dev_image)
        archive = self.backup / (self.volume + ".tar.gz")
        assert archive.is_file() and archive.stat().st_size > 0
        if os.name == "posix":
            assert archive.stat().st_uid == os.geteuid(), "Backup is not owned by the invoking user"
            assert archive.stat().st_mode & 0o777 == 0o600, "Backup permissions must be private"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        # Alter only the fixture file inside the dedicated test volume.
        self.docker("run", "--rm", "--network", "none", "--mount", f"type=volume,src={self.volume},dst=/data",
                    "--entrypoint", "python3", self.args.dev_image,
                    "-c", "from pathlib import Path;Path('/data/workspace/upload space.txt').write_text('changed')")
        self.script("restore.sh", "--user", self.user, "--input", self.backup.as_posix(), "--force", "--image", self.args.dev_image)
        value = self.docker("run", "--rm", "--network", "none", "--mount", f"type=volume,src={self.volume},dst=/data,readonly",
                            "--entrypoint", "python3", self.args.dev_image,
                            "-c", "from pathlib import Path;print(Path('/data/workspace/upload space.txt').read_text())").stdout.strip()
        assert value == "workspace upload verified", value
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
        return {"archive": archive.name, "sha256": digest}

    def cleanup(self):
        for user in [self.user, self.other, *self.extra_users]:
            names = self.docker("ps", "-a", "--filter", f"label=ai-dev-env.user={user}",
                                "--format", "{{.Names}}", check=False).stdout.splitlines()
            for name in names:
                assert name.startswith(f"ai-dev-{user}-"), name
                self.docker("rm", "-f", name, check=False)
        for network in [self.network, self.other_network, *self.extra_networks]:
            assert network.startswith("ai-dev-audit-")
            self.docker("network", "rm", network, check=False)
        for volume in [self.volume, self.other_volume, *self.extra_volumes]:
            assert volume.startswith("ai-dev-audit-")
            self.docker("volume", "rm", volume, check=False)
        # Backups contain disposable SSH/browser credentials. Retain only the
        # checksum in the evidence, not the credential-bearing test archive.
        for archive in self.backup.iterdir():
            assert archive.parent.resolve() == self.backup.resolve()
            if archive.name == self.volume + ".tar.gz" or archive.name.startswith("." + self.volume + ".tar.gz.tmp."):
                archive.unlink()
        self.backup.rmdir()

    def run(self):
        try:
            for name, check in [
                ("real dev entrypoint and SSH readiness", self.deploy),
                ("real SSH password authentication", self.authenticate),
                ("same-user duplicate deployment rejected", self.duplicate_user),
                ("cross-user network isolation", self.network_isolation),
                ("concurrent same-user deployments remain single", self.concurrent_user),
                ("atomic configuration survives restart", self.config_restart),
                ("s6 isolates invalid definitions and preserves healthy services", lambda: check_s6_guard(self)),
                ("browser entrypoint starts idle", self.start_browser),
                ("shared fixture files", self.prepare_files),
                ("tabs and close from cold start", self.cold_tabs),
                ("real page snapshot and DOM action", self.page_actions),
                ("real workspace and downloads upload", self.upload),
                ("4096-character desktop typing", self.long_input),
                ("screen authentication", self.auth),
                ("control process crash recovery", self.crash_recovery),
                ("idle and wake restores browser", self.idle_restore),
                ("browser recreation and menu launcher retain profile", self.browser_recreate),
                ("dev recreate preserves configuration network and identity", self.recreate),
                ("real desktop screenshot", self.screenshot),
                ("offline backup and restore roundtrip", self.backup_restore),
            ]:
                self.record(name, check)
        except Exception:
            for role, name in [("dev", self.dev), ("browser", self.browser)]:
                logs = self.docker("logs", "--tail", "120", name, check=False)
                (self.output / (role + "-failure.log")).write_text(
                    redact(logs.stdout + logs.stderr), encoding="utf-8", newline="\n")
            logs = self.docker("exec", self.browser, "tail", "-n", "80",
                               "/tmp/browser/control.log", "/tmp/browser/chromium.log",
                               "/tmp/browser/websockify.log", check=False)
            (self.output / "desktop-failure.log").write_text(
                redact(logs.stdout + logs.stderr), encoding="utf-8", newline="\n")
            raise
        finally:
            self.cleanup()
            self.write_results()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-image", required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--output", default=None,
                        help="directory for the results (default: a new temp directory)")
    args = parser.parse_args()
    if args.output is None:
        # Acceptance evidence is a local artifact, never a repo path: pointing at
        # the working tree would scatter logs into the source checkout.
        args.output = tempfile.mkdtemp(prefix="ai-dev-acceptance-")
    suite = Acceptance(args)
    suite.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
