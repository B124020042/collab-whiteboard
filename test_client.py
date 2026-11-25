#!/usr/bin/env python3
"""客戶端邏輯測試 (不需要螢幕)。

用一個簡化的假 tkinter 取代真正的 GUI，讓兩個 App 透過「真的伺服器」互相協作，
驗證畫筆、文字框、選取 / 移動、複製 / 剪下 / 貼上 / 刪除、存檔讀檔等流程。

    python3 test_client.py
"""
import json
import socket
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 9188


# ====================== 假 tkinter ======================
class TclError(Exception):
    pass


class Var:
    def __init__(self, value=None):
        self.v = value

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class Widget:
    def __init__(self, master=None, **kw):
        self.master, self.kw, self.bindings = master, kw, {}
        self.root = master.root if master is not None and hasattr(master, "root") else self
        self.destroyed = False

    def pack(self, **kw): pass
    def config(self, **kw): self.kw.update(kw)
    configure = config
    def bind(self, seq, fn): self.bindings[seq] = fn
    def focus_set(self): self.root.focused = self
    def destroy(self):
        self.destroyed = True
        if self.root.focused is self:
            self.root.focused = None
            fn = self.bindings.get("<FocusOut>")
            if fn:
                fn(None)


class Tk(Widget):
    def __init__(self):
        super().__init__(None)
        self.root, self.focused, self.clipboard = self, None, None

    def title(self, t): pass
    def geometry(self, g): pass
    def protocol(self, name, fn): pass
    def after(self, ms, fn=None): return None
    def after_cancel(self, job): pass
    def focus_get(self): return self.focused
    def clipboard_clear(self): self.clipboard = ""
    def clipboard_append(self, s): self.clipboard = (self.clipboard or "") + s
    def clipboard_get(self):
        if not self.clipboard:
            raise TclError("empty")
        return self.clipboard


class Text(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.content = ""

    def insert(self, index, s): self.content += s
    def get(self, a, b): return self.content
    def mark_set(self, *a): pass
    def type(self, s): self.content += s          # 測試用：模擬打字


class Canvas(Widget):
    """以外框 (bbox) 近似真實畫布的物件、tag、碰撞判定與堆疊順序。"""
    W, H = 1000, 600

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.items, self.order, self.next_id = {}, [], 1

    def winfo_width(self): return self.W
    def winfo_height(self): return self.H

    def _new(self, kind, coords, **kw):
        i = self.next_id
        self.next_id += 1
        tags = kw.pop("tags", ())
        tags = (tags,) if isinstance(tags, str) else tuple(tags)
        self.items[i] = {"kind": kind, "coords": list(coords), "tags": tags, "opt": kw}
        self.order.append(i)
        return i

    def create_line(self, *c, **kw): return self._new("line", c, **kw)
    def create_oval(self, *c, **kw): return self._new("oval", c, **kw)
    def create_rectangle(self, *c, **kw): return self._new("rect", c, **kw)
    def create_text(self, *c, **kw): return self._new("text", c, **kw)
    def create_window(self, *c, **kw): return self._new("window", c, **kw)

    def find(self, tag):
        if isinstance(tag, int):
            return [tag] if tag in self.items else []
        return [i for i in self.order if tag in self.items[i]["tags"]]

    def delete(self, *tags):
        for t in tags:
            for i in self.find(t):
                del self.items[i]
                self.order.remove(i)

    def coords(self, tag, *c):
        ids = self.find(tag)
        if not c:
            return list(self.items[ids[0]]["coords"]) if ids else []
        for i in ids:
            self.items[i]["coords"] = list(c)

    def move(self, tag, dx, dy):
        for i in self.find(tag):
            c = self.items[i]["coords"]
            self.items[i]["coords"] = [v + (dx if k % 2 == 0 else dy) for k, v in enumerate(c)]

    def _bbox(self, i):
        it = self.items[i]
        c, o = it["coords"], it["opt"]
        if it["kind"] == "text":
            size = o["font"][1]
            lines = o["text"].split("\n")
            w = max(len(l) for l in lines) * size * 0.6
            return c[0], c[1], c[0] + w, c[1] + size * 1.3 * len(lines)
        if it["kind"] == "window":
            return c[0], c[1], c[0] + 50, c[1] + 20
        xs, ys = c[0::2], c[1::2]
        r = o.get("width", 1) / 2
        return min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r

    def bbox(self, tag):
        ids = [i for i in self.find(tag) if self.items[i]["opt"].get("state") != "hidden"]
        if not ids:
            return None
        bbs = [self._bbox(i) for i in ids]
        return (min(b[0] for b in bbs), min(b[1] for b in bbs),
                max(b[2] for b in bbs), max(b[3] for b in bbs))

    def find_overlapping(self, x0, y0, x1, y1):
        out = []
        for i in self.order:
            if self.items[i]["opt"].get("state") == "hidden":
                continue
            a, b, c, d = self._bbox(i)
            if a <= x1 and c >= x0 and b <= y1 and d >= y0:
                out.append(i)
        return tuple(out)

    def gettags(self, i): return self.items[i]["tags"]

    def itemconfigure(self, tag, **kw):
        for i in self.find(tag):
            self.items[i]["opt"].update(kw)

    def tag_raise(self, tag):
        ids = self.find(tag)
        self.order = [i for i in self.order if i not in ids] + ids

    def postscript(self, **kw): pass

    # 測試輔助
    def visible(self, tag):
        return [i for i in self.find(tag) if self.items[i]["opt"].get("state") != "hidden"]


def install_fake_tk():
    tk = types.ModuleType("tkinter")
    for name, val in dict(Tk=Tk, Canvas=Canvas, Text=Text, Frame=Widget, Button=Widget,
                          Label=Widget, Scale=Widget, Spinbox=Widget, Entry=Widget,
                          IntVar=Var, StringVar=Var, TclError=TclError).items():
        setattr(tk, name, val)
    for c in ("LEFT", "RIGHT", "TOP", "BOTTOM", "X", "BOTH", "RAISED", "SUNKEN",
              "SOLID", "ROUND", "HORIZONTAL"):
        setattr(tk, c, c.lower())
    tk.Spinbox = type("Spinbox", (Widget,), {})
    tk.Entry = type("Entry", (Widget,), {})
    font = types.ModuleType("tkinter.font")
    font.nametofont = lambda n: types.SimpleNamespace(actual=lambda k: "Fake")
    mb = types.ModuleType("tkinter.messagebox")
    mb.answers = {}
    mb.askyesno = lambda *a: mb.answers.get("askyesno", True)
    mb.askyesnocancel = lambda *a: mb.answers.get("askyesnocancel", True)
    mb.showinfo = mb.showwarning = mb.showerror = lambda *a: None
    fd = types.ModuleType("tkinter.filedialog")
    fd.path = None
    fd.asksaveasfilename = lambda **k: fd.path
    fd.askopenfilename = lambda **k: fd.path
    others = {n: types.ModuleType("tkinter." + n) for n in ("colorchooser", "simpledialog")}
    others["colorchooser"].askcolor = lambda **k: (None, None)
    others["simpledialog"].askstring = lambda *a, **k: "guest"
    tk.font, tk.messagebox, tk.filedialog = font, mb, fd
    for n, m in others.items():
        setattr(tk, n, m)
    sys.modules.update({"tkinter": tk, "tkinter.font": font, "tkinter.messagebox": mb,
                        "tkinter.filedialog": fd,
                        **{"tkinter." + n: m for n, m in others.items()}})
    return tk


fake_tk = install_fake_tk()
sys.path.insert(0, str(HERE))
import client  # noqa: E402  (必須在安裝假 tkinter 之後才匯入)


def ev(x, y, shift=False):
    return types.SimpleNamespace(x=x, y=y, state=1 if shift else 0)


class Board:
    """一個連上伺服器的假視窗。"""

    def __init__(self, name, room):
        self.root = fake_tk.Tk()
        self.net = client.Net("127.0.0.1", PORT, name, room, client.queue.Queue())
        self.app = client.App(self.root, self.net, self.net.inbox)
        self.c = self.app.canvas
        self.net.start()
        self.wait(lambda: self.app.online and self.app.my_name == name)

    def pump(self):
        self.app.poll()

    def wait(self, cond, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            if cond():
                return
            time.sleep(0.02)
        raise AssertionError("等待逾時")

    def drag(self, pts, shift=False):
        a = self.app
        a.on_press(ev(*pts[0], shift))
        for p in pts[1:]:
            a.on_drag(ev(*p, shift))
        a.on_release(ev(*pts[-1], shift))

    def close(self):
        self.net.stop()


class ClientTest(unittest.TestCase):
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

    def setUp(self):
        self.room = self.id().split(".")[-1]
        self.a, self.b = Board("amy", self.room), Board("ben", self.room)
        self.a.wait(lambda: "ben" in self.a.app.users)

    def tearDown(self):
        self.a.close()
        self.b.close()

    def texts(self, board):
        return sorted(o["text"] for o in board.app.strokes.values() if o.get("kind") == "text")

    def test_pen_sync(self):
        self.a.drag([(100, 100), (150, 120), (200, 140)])
        self.b.wait(lambda: len(self.b.app.strokes) == 1)
        s = next(iter(self.b.app.strokes.values()))
        self.assertEqual(s["pts"][-1], [0.2, round(140 / 600, 4)])

    def test_text_create_edit_delete(self):
        A, B = self.a.app, self.b.app
        A.set_tool("text")
        A.on_press(ev(300, 200))                       # 點一下 -> 出現編輯框
        self.assertIsNotNone(A.editor)
        A.editor["w"].type("你好 hello\n第二行")
        A.commit_editor()                              # 相當於按 Enter
        self.b.wait(lambda: self.texts(self.b) == ["你好 hello\n第二行"])
        sid = next(iter(B.strokes))
        self.assertEqual(B.strokes[sid]["size"], 20)
        self.assertTrue(self.b.c.visible("s_" + sid))

        # B 雙擊編輯 A 的文字
        B.set_tool("select")
        B.on_double(ev(305, 205))
        self.assertEqual(B.editor["sid"], sid)
        self.assertFalse(self.b.c.visible("s_" + sid))  # 編輯中先隱藏原文字
        B.editor["w"].content = "改過了"
        B.commit_editor()
        self.a.wait(lambda: self.texts(self.a) == ["改過了"])

        # Esc 取消編輯不會改變內容
        A.set_tool("text")
        A.on_press(ev(305, 205))
        A.editor["w"].content = "不要存"
        A.commit_editor(cancel=True)
        self.assertEqual(self.texts(self.a), ["改過了"])

        # 內容清空 = 刪除
        A.on_press(ev(305, 205))
        A.editor["w"].content = "   "
        A.commit_editor()
        self.b.wait(lambda: not B.strokes)

    def test_select_move_delete(self):
        A, B = self.a.app, self.b.app
        self.a.drag([(100, 100), (120, 100)])
        self.a.drag([(500, 300), (520, 300)])
        self.b.wait(lambda: len(B.strokes) == 2)
        s1, s2 = list(A.strokes)

        A.set_tool("select")
        A.on_press(ev(110, 100)); A.on_release(ev(110, 100))
        self.assertEqual(A.selection, {s1})
        A.on_press(ev(510, 300, shift=True))           # Shift 加選
        self.assertEqual(A.selection, {s1, s2})

        self.a.drag([(110, 100), (210, 160)])          # 拖曳移動兩個物件
        self.b.wait(lambda: abs(B.strokes[s1]["pts"][0][0] - 0.2) < 1e-6)
        for got, want in [(B.strokes[s1]["pts"][0], [0.2, 0.2667]),
                          (B.strokes[s2]["pts"][0], [0.6, 0.6]),
                          (self.b.c.coords("s_" + s1)[:2], [200, 160])]:   # 畫面也移動了
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, delta=0.1)

        A.set_selection({s2})                          # 拖到畫布外：被限制在邊界
        self.a.drag([(610, 360), (2000, 2000)])
        self.b.wait(lambda: max(p[0] for p in B.strokes[s2]["pts"]) == 1.0)

        A.set_selection({s1})
        A.delete_selection()
        self.b.wait(lambda: list(B.strokes) == [s2])

    def test_band_select(self):
        A = self.a.app
        self.a.drag([(100, 100), (120, 110)])
        self.a.drag([(400, 400), (420, 410)])
        A.set_tool("text")
        A.on_press(ev(130, 130)); A.editor["w"].type("label"); A.commit_editor()
        A.set_tool("select")
        self.a.drag([(50, 50), (300, 300)])            # 框選左上角兩個
        self.assertEqual(len(A.selection), 2)
        self.assertFalse(A.canvas.find("band"))        # 框選矩形已移除
        A.select_all()
        self.assertEqual(len(A.selection), 3)

    def test_copy_paste_cut(self):
        A, B = self.a.app, self.b.app
        self.a.drag([(100, 100), (200, 100)])
        A.set_tool("text")
        A.on_press(ev(100, 150)); A.editor["w"].type("複製我"); A.commit_editor()
        A.select_all()
        A.copy()
        self.assertIn('"app": "collab-whiteboard"', self.a.root.clipboard)
        A.paste()
        A.paste()
        self.b.wait(lambda: len(B.strokes) == 6)
        self.assertEqual(self.texts(self.b), ["複製我"] * 3)
        xs = sorted(o["x"] for o in B.strokes.values() if o.get("kind") == "text")
        self.assertEqual(xs, [0.1, 0.12, 0.14])         # 每次貼上往右下偏移
        self.assertEqual(len(A.selection), 2)           # 貼上後自動選取新物件

        # 跨視窗：B 貼上 A 複製的內容 (模擬系統剪貼簿共用)
        self.b.root.clipboard = self.a.root.clipboard
        B.paste()
        self.a.wait(lambda: len(A.strokes) == 8)

        # 剪下 = 複製 + 刪除
        A.cut()
        self.b.wait(lambda: len(B.strokes) == 6)

        # 貼上一般文字 -> 在滑鼠位置建立文字框
        self.a.root.clipboard = "從記事本複製的字"
        A.on_move(ev(700, 450))
        A.paste()
        self.b.wait(lambda: "從記事本複製的字" in self.texts(self.b))
        o = next(o for o in B.strokes.values() if o.get("text") == "從記事本複製的字")
        self.assertEqual((o["x"], o["y"]), (0.7, 0.75))

        # 復原：移除自己最後新增的物件
        A.undo()
        self.b.wait(lambda: "從記事本複製的字" not in self.texts(self.b))

    def test_shortcuts_ignored_while_typing(self):
        A = self.a.app
        self.a.drag([(100, 100), (200, 100)])
        A.select_all()
        A.set_tool("text")
        A.on_press(ev(400, 400))
        handler = A.shortcut(A.delete_selection)
        self.assertIsNone(handler(None))               # 打字時 Delete 交給文字框
        A.commit_editor(cancel=True)
        A.set_tool("select")
        A.select_all()
        self.assertEqual(handler(None), "break")
        self.b.wait(lambda: not self.b.app.strokes)

    def test_remote_delete_while_editing_and_selected(self):
        A, B = self.a.app, self.b.app
        A.set_tool("text")
        A.on_press(ev(100, 100)); A.editor["w"].type("x"); A.commit_editor()
        self.b.wait(lambda: B.strokes)
        sid = next(iter(B.strokes))
        A.set_tool("select"); A.set_selection({sid})
        B.set_tool("text"); B.on_press(ev(102, 102))
        self.assertEqual(B.editor["sid"], sid)
        A.delete_selection()
        self.b.wait(lambda: not B.strokes)
        self.assertIsNone(B.editor)                    # 被刪掉的文字，編輯框自動關閉
        self.assertFalse(A.selection)

    def test_save_and_open(self):
        import tempfile
        A, B = self.a.app, self.b.app
        self.a.drag([(100, 100), (200, 100)])
        A.set_tool("text")
        A.on_press(ev(100, 150)); A.editor["w"].type("存起來"); A.commit_editor()
        self.b.wait(lambda: len(B.strokes) == 2)       # 確定伺服器都收到了
        path = Path(tempfile.mkdtemp()) / "board.json"
        fake_tk.filedialog.path = str(path)
        A.save()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([o.get("kind") for o in data["strokes"]], [None, "text"])
        fake_tk.messagebox.answers["askyesnocancel"] = True   # 先清除再載入
        B.open_file()
        self.a.wait(lambda: self.texts(self.a) == ["存起來"] and len(A.strokes) == 2)
        self.b.wait(lambda: len(B.strokes) == 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
