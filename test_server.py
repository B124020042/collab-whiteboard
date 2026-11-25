#!/usr/bin/env python3
"""自動化測試：啟動伺服器，用多個原始 socket 客戶端驗證同步邏輯。

    python3 test_server.py
"""
import select
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

import json

from protocol import chunk_pts, decode, dump_board, encode, parse_board

HERE = Path(__file__).resolve().parent
PORT = 9177


class C:
    """測試用客戶端：自己管理接收緩衝區，可安全地做「一段時間內沒收到」的檢查。"""

    def __init__(self, name, room="lobby"):
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        self.buf = b""
        self.send(type="hello", name=name, room=room)
        self.welcome = self.until("welcome")
        self.name = self.welcome["name"]

    def send(self, **o):
        self.s.sendall(encode(**o))

    def recv(self, timeout=3):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.s], [], [], left)[0]:
                raise TimeoutError("timed out")
            data = self.s.recv(65536)
            if not data:
                raise ConnectionError("closed")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return decode(line)

    def until(self, t, pred=lambda m: True):
        for _ in range(500):
            m = self.recv()
            if m["type"] == t and pred(m):
                return m
        raise AssertionError(f"沒收到 {t}")

    def nothing(self, t, wait=0.3):
        """確認 wait 秒內沒收到 type 為 t 的訊息。"""
        end = time.monotonic() + wait
        try:
            while (left := end - time.monotonic()) > 0:
                if self.recv(left)["type"] == t:
                    return False
        except TimeoutError:
            pass
        return True

    def close(self):
        self.s.close()


class WhiteboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(PORT)],
                                 cwd=HERE, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate()
        cls.p.wait()

    def test_draw_sync_and_replay(self):
        a, b = C("alice", "r1"), C("bob", "r1")
        a.until("user_join", lambda m: m["name"] == "bob")
        a.send(type="draw", sid="s1", color="#ff0000", width=5, pts=[[0.1, 0.1], [0.2, 0.2]])
        a.send(type="draw", sid="s1", color="#ff0000", width=5, pts=[[0.2, 0.2], [0.3, 0.3]])
        m1, m2 = b.until("draw"), b.until("draw")
        self.assertEqual((m1["owner"], m1["color"], m1["width"]), ("alice", "#ff0000", 5))
        self.assertEqual(m2["pts"][-1], [0.3, 0.3])
        self.assertTrue(a.nothing("draw"))           # 不會回送給自己

        late = C("carol", "r1")                       # 後加入者拿到完整重播
        strokes = late.welcome["strokes"]
        self.assertEqual(len(strokes), 1)
        self.assertEqual(len(strokes[0]["pts"]), 4)
        self.assertEqual(sorted(late.welcome["users"]), ["alice", "bob", "carol"])
        for c in (a, b, late):
            c.close()

    def test_undo_only_own(self):
        a, b = C("alice", "r2"), C("bob", "r2")
        a.send(type="draw", sid="a1", pts=[[0.5, 0.5]])
        b.until("draw")
        b.send(type="draw", sid="b1", pts=[[0.6, 0.6]])
        a.until("draw")
        b.send(type="draw", sid="a1", pts=[[0.9, 0.9]])   # 不能接續別人的筆跡
        b.send(type="cursor", x=0, y=0)
        a.until("cursor")                                  # 確定 b 的訊息已被伺服器處理
        self.assertTrue(a.nothing("draw", 0.1))
        a.send(type="undo")
        self.assertEqual(a.until("remove")["sid"], "a1")
        self.assertEqual(b.until("remove")["sid"], "a1")
        a.send(type="undo")                                # a 已沒有筆跡 -> 不動作
        self.assertTrue(b.nothing("remove"))
        strokes = C("x", "r2").welcome["strokes"]
        self.assertEqual([s["sid"] for s in strokes], ["b1"])
        a.close(); b.close()

    def test_clear_rooms_cursor_names(self):
        a, b, other = C("alice", "r3"), C("alice", "r3"), C("dave", "r4")
        self.assertEqual(b.name, "alice_2")                # 重複暱稱自動加編號
        a.send(type="draw", sid="z", pts=[[0.1, 0.1]])
        b.until("draw")
        self.assertTrue(other.nothing("draw"))             # 房間隔離
        a.send(type="cursor", x=0.4, y=1.7)
        m = b.until("cursor")
        self.assertEqual((m["name"], m["x"], m["y"]), ("alice", 0.4, 1.0))  # 座標被限制在 0~1
        b.send(type="clear")
        self.assertEqual(a.until("clear")["by"], "alice_2")
        self.assertEqual(C("e", "r3").welcome["strokes"], [])
        b.close()
        self.assertEqual(a.until("user_leave", lambda m: m["name"] == "alice_2")["name"], "alice_2")
        a.close(); other.close()

    def test_bad_input_does_not_crash(self):
        watcher = C("watcher", "r5")
        c = C("evil", "r5")
        c.s.sendall(b"not json\n{\"type\":\"draw\"}\n[1,2]\n")
        c.send(type="draw", sid="q", color="red;rm", width=9999, pts=[["a", 1]])
        c.send(type="draw", sid="ok", color="javascript", width=9999, pts=[[2, -1]])
        watcher.until("draw")
        s = C("check", "r5").welcome["strokes"]
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["color"], s[0]["width"], s[0]["pts"]), ("#000000", 60, [[1.0, 0.0]]))
        c.close()

    def test_bot_stress(self):
        watcher = C("watcher", "botroom")
        r = subprocess.run([sys.executable, str(HERE / "bot.py"), "--port", str(PORT),
                            "--room", "botroom", "--count", "3"], cwd=HERE, timeout=30)
        self.assertEqual(r.returncode, 0)
        watcher.until("user_leave")
        self.assertEqual(len(C("v", "botroom").welcome["strokes"]), 3)
        watcher.close()

    def test_save_load_roundtrip(self):
        """模擬：A 畫圖 -> 存成 .json -> 讀檔 -> 用 client 的方式送出 -> B 看到相同畫面。"""
        a, w = C("artist", "save"), C("watcher", "save")
        long_line = [[i / 1200, 0.5] for i in range(1200)]          # 超過單訊息上限
        a.send(type="draw", sid="dot", color="#123456", width=7, pts=[[0.3, 0.3]])
        for ch in chunk_pts(long_line):
            a.send(type="draw", sid="long", color="#ff0000", width=2, pts=ch)
        for _ in range(1 + len(list(chunk_pts(long_line)))):
            w.until("draw")                                          # 確定伺服器都處理完了
        original = C("saver", "save").welcome["strokes"]
        self.assertEqual(len(original), 2)
        text = json.dumps(dump_board(original))                     # 存檔
        loaded = parse_board(json.loads(text))                       # 讀檔
        a.send(type="clear")
        a.until("clear")
        viewer = C("viewer", "save")
        for i, s in enumerate(loaded):                               # 載入 = 重新送出
            for ch in chunk_pts(s["pts"]):
                a.send(type="draw", sid=f"new{i}", color=s["color"], width=s["width"], pts=ch)
        got = [viewer.until("draw") for _ in range(1 + len(list(chunk_pts(long_line))))]
        self.assertEqual(got[0]["color"], "#123456")
        restored = C("check", "save").welcome["strokes"]
        self.assertEqual([(s["color"], s["width"]) for s in restored],
                         [(s["color"], s["width"]) for s in original])
        # 去掉分段時重複的接點後，點座標與原本完全相同
        dedup = [p for i, p in enumerate(restored[1]["pts"])
                 if i == 0 or p != restored[1]["pts"][i - 1]]
        self.assertEqual(dedup, long_line)
        a.close(); w.close(); viewer.close()


class FileFormatTest(unittest.TestCase):
    def test_parse_board(self):
        data = {"strokes": [{"color": "#00ff00", "width": 4, "pts": [[0.1, 0.2]]},
                            {"color": "evil", "width": 999, "pts": [[5, -1]]},
                            {"oops": 1}]}
        out = parse_board(data)
        self.assertEqual(out, [{"color": "#00ff00", "width": 4, "pts": [[0.1, 0.2]]},
                               {"color": "#000000", "width": 60, "pts": [[1.0, 0.0]]}])
        self.assertEqual(parse_board([]), [])                       # 空白畫布也合法
        for bad in ({"hello": 1}, "text", [{"x": 1}]):
            with self.assertRaises(ValueError):
                parse_board(bad)

    def test_chunk_pts(self):
        for n in (1, 2, 499, 500, 501, 1200):
            pts = [[i, i] for i in range(n)]
            chunks = list(chunk_pts(pts))
            self.assertTrue(all(len(c) <= 500 for c in chunks))
            self.assertEqual(chunks[0][0], pts[0])
            self.assertEqual(chunks[-1][-1], pts[-1])
            for x, y in zip(chunks, chunks[1:]):
                self.assertEqual(x[-1], y[0])                        # 段與段相接


if __name__ == "__main__":
    unittest.main(verbosity=2)
