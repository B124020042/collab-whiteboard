#!/usr/bin/env python3
"""start.py 自動探索 / 自動開伺服器的測試 (不需要螢幕)。

    python3 test_start.py
"""
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import start
from protocol import encode

HERE = Path(__file__).resolve().parent
PORT, UDP = 9195, 9196          # 測試用伺服器
FREE, FREE_UDP = 9197, 9198     # 確定沒有伺服器的 port


def run_server(port, udp):
    p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(port),
                          "--udp-port", str(udp)], cwd=HERE, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if start.tcp_probe("127.0.0.1", port):
            return p
        time.sleep(0.1)
    raise RuntimeError("server not up")


class StartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = run_server(PORT, UDP)

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate()
        cls.p.wait()

    def test_tcp_probe(self):
        info = start.tcp_probe("127.0.0.1", PORT)
        self.assertEqual((info["app"], info["port"], info["users"]), ("collab-whiteboard", PORT, 0))
        self.assertIsNone(start.tcp_probe("127.0.0.1", FREE))       # 沒有伺服器

    def test_probe_rejects_other_services(self):
        """port 上是別的程式 (不是白板) 時不能誤判。"""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0)); srv.listen()
        port = srv.getsockname()[1]

        def fake():
            c, _ = srv.accept()
            c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            c.close()
        threading.Thread(target=fake, daemon=True).start()
        self.assertIsNone(start.tcp_probe("127.0.0.1", port))
        srv.close()

    def test_udp_discover_and_user_count(self):
        s = socket.create_connection(("127.0.0.1", PORT))
        s.sendall(encode(type="hello", name="amy", room="r1"))
        time.sleep(0.3)
        found = start.udp_discover(UDP)
        self.assertTrue(found, "UDP 探索沒有回應")
        info = next(iter(found.values()))
        self.assertEqual((info["port"], info["users"], info["rooms"]), (PORT, 1, {"r1": 1}))
        self.assertEqual(start.udp_discover(FREE_UDP, timeout=0.5), {})
        s.close()

    def test_discover_and_pick(self):
        found = start.discover(PORT, UDP, scan=False, log=lambda *a: None)
        self.assertIn("127.0.0.1", found)
        busy = {"192.168.1.9": {"users": 1}, "192.168.1.20": {"users": 3},
                "192.168.1.5": {"users": 3}, "10.0.0.1": {"users": 0}}
        self.assertEqual(start.pick(busy)[0], "192.168.1.5")       # 人最多，再比 IP

    def test_check_command(self):
        out = subprocess.run([sys.executable, "start.py", "--check", "--no-scan",
                              "--port", str(PORT), "--udp-port", str(UDP)],
                             cwd=HERE, capture_output=True, text=True, timeout=20).stdout
        self.assertIn(f"找到 127.0.0.1:{PORT}", out)
        out = subprocess.run([sys.executable, "start.py", "--check", "--no-scan",
                              "--port", str(FREE), "--udp-port", str(FREE_UDP)],
                             cwd=HERE, capture_output=True, text=True, timeout=20).stdout
        self.assertIn("沒有找到任何伺服器", out)

    def test_own_server_stays_until_everyone_leaves(self):
        proc = start.start_server(FREE, FREE_UDP)               # 找不到時自己開
        self.assertTrue(start.tcp_probe("127.0.0.1", FREE))
        guest = socket.create_connection(("127.0.0.1", FREE))  # 另一台電腦的人連進來
        guest.sendall(encode(type="hello", name="guest", room="lobby"))
        t = threading.Thread(target=start.wait_until_empty, args=(proc, FREE), daemon=True)
        t.start()
        time.sleep(2.5)
        self.assertIsNone(proc.poll(), "還有人在用，伺服器不應該關閉")
        guest.close()                                           # 最後一個人離開
        t.join(10)
        self.assertIsNotNone(proc.poll(), "所有人離開後伺服器應該關閉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
