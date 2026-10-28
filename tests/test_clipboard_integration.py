"""Exercise Unicode clipboard exchange against a disposable TigerVNC desktop.

Run in a disposable container with AI_DEV_CLIPBOARD_TEST=1 and with Xtigervnc,
vncconfig and xclip installed. Never run it on a user's desktop.
"""

import os
import importlib.machinery
import importlib.util
import shutil
import socket
import struct
import subprocess
import time
import unittest
import zlib


@unittest.skipUnless(os.environ.get("AI_DEV_CLIPBOARD_TEST") == "1" and
                     all(shutil.which(x) for x in ("Xtigervnc", "vncconfig", "xclip")),
                     "requires an explicitly enabled disposable TigerVNC test container")
class ClipboardIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.display = ":87"
        self.env = dict(os.environ, DISPLAY=self.display)
        self.server = subprocess.Popen(
            ["Xtigervnc", self.display, "-geometry", "640x480", "-depth", "24",
             "-localhost", "-rfbport", "15987", "-SecurityTypes", "None", "-ac",
             "-AlwaysShared", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop_process, self.server)
        deadline = time.monotonic() + 10
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", 15987), timeout=1)
                break
            except OSError:
                if self.server.poll() is not None or time.monotonic() > deadline:
                    self.fail("isolated TigerVNC failed to start")
                time.sleep(0.1)
        self.addCleanup(self.sock.close)
        self.sock.settimeout(5)
        self.config = subprocess.Popen(["vncconfig", "-nowin"], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, self.config)
        version = self.read(12)
        self.sock.sendall(version)
        count = self.read(1)[0]
        self.assertIn(1, self.read(count))
        self.sock.sendall(b"\x01")
        self.assertEqual(self.read(4), b"\x00" * 4)
        self.sock.sendall(b"\x01")
        header = self.read(24)
        self.read(struct.unpack(">I", header[20:24])[0])
        self.sock.sendall(struct.pack(">BBHI", 2, 0, 1, 0xC0A1E5CE))
        flags, _ = self.receive()
        self.assertTrue(flags & 0x08000000, "server must support clipboard Notify")
        self.send(0x1F000001, struct.pack(">I", 0))
        time.sleep(0.5)

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def read(self, size):
        data = b""
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise RuntimeError("VNC connection closed")
            data += part
        return data

    def send(self, flags, payload=b""):
        body = struct.pack(">I", flags) + payload
        self.sock.sendall(struct.pack(">B3xi", 6, -len(body)) + body)

    def receive(self):
        kind = self.read(1)
        self.assertEqual(kind, b"\x03", "expected ServerCutText")
        length = struct.unpack(">i", self.read(7)[3:])[0]
        self.assertLess(length, 0, "expected extended clipboard")
        body = self.read(-length)
        return struct.unpack(">I", body[:4])[0], body[4:]

    def test_unicode_in_both_directions(self):
        value = "中文实时测试 Hello"
        self.send(0x08000001)
        time.sleep(0.2)
        paste = subprocess.Popen(["xclip", "-selection", "clipboard", "-o"],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_process, paste)
        flags, _ = self.receive()
        self.assertEqual(flags, 0x02000001)
        raw = value.encode("utf-8") + b"\x00"
        self.send(0x10000001, zlib.compress(struct.pack(">I", len(raw)) + raw))
        output, error = paste.communicate(timeout=5)
        self.assertEqual(paste.returncode, 0, error.decode("utf-8", errors="replace"))
        self.assertEqual(output.decode("utf-8"), value)

        reverse = "从远程复制 中文"
        subprocess.run(["xclip", "-selection", "clipboard", "-i"], env=self.env,
                       input=reverse.encode("utf-8"), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=3)
        flags, _ = self.receive()
        self.assertEqual(flags, 0x08000001)
        self.send(0x02000001)
        flags, payload = self.receive()
        self.assertEqual(flags, 0x10000001)
        decoded = zlib.decompressobj().decompress(payload)
        size = struct.unpack(">I", decoded[:4])[0]
        self.assertEqual(decoded[4:4 + size].rstrip(b"\x00").decode("utf-8"), reverse)

    def test_repeated_health_checks_do_not_blacklist_viewers(self):
        loader = importlib.machinery.SourceFileLoader("clipboard_test_hub", "/usr/local/bin/browser-hub")
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        module.VNC_PORT = 15987
        for _ in range(10):
            self.assertTrue(module.vnc_ready())
        with socket.create_connection(("127.0.0.1", 15987), timeout=3) as viewer:
            self.assertEqual(viewer.recv(12), b"RFB 003.008\n")


if __name__ == "__main__":
    unittest.main()
