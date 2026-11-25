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
    def __init__(self, value=""):
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
        if self.root is not self:
            self.root.widgets.append(self)

    def pack(self, **kw): pass
    def grid(self, **kw): pass
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
        self.widgets = []
        self.scheduled = []
        self.tk = types.SimpleNamespace(call=lambda *a: "x11")

    on_mainloop = None                         # 測試用：mainloop 時要執行的動作

    def title(self, t): pass
    def resizable(self, *a): pass
    def update_idletasks(self): pass
    def mainloop(self):
        if Tk.on_mainloop:
            Tk.on_mainloop(self)
    def button(self, text):
        return next(w for w in self.widgets if w.kw.get("text") == text)
    def geometry(self, g): pass
    def protocol(self, name, fn): pass
    def after(self, ms, fn=None):
        if fn:
            self.scheduled.append(fn)

    def pump(self, cond, timeout=10):
        """測試用：執行 after() 排程的回呼，直到 cond() 成立。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            todo, self.scheduled = self.scheduled, []
            for fn in todo:
                fn()
            if cond():
                return
            time.sleep(0.02)
        raise AssertionError("等待逾時")
    def after_cancel(self, job): pass
    def focus_get(self): return self.focused
    def clipboard_clear(self): self.clipboard = ""
    def clipboard_append(self, s): self.clipboard = (self.clipboard or "") + s
    def clipboard_get(self):
        if not self.clipboard:
            raise TclError("empty")
        return self.clipboard


class Menu(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.entries, self.popped = [], None

    def delete(self, a, b=None): self.entries = []
    def add_command(self, **kw): self.entries.append(kw)
    def add_separator(self): self.entries.append({"label": "---"})
    def tk_popup(self, x, y): self.popped = (x, y)
    def grab_release(self): pass

    # 測試輔助
    def item(self, label):
        return next((e for e in self.entries if e["label"] == label), None)

    def click(self, label):
        e = self.item(label)
        assert e and e.get("state", "normal") == "normal", f"{label} 無法點選"
        e["command"]()


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
    for name, val in dict(Tk=Tk, Canvas=Canvas, Text=Text, Menu=Menu, Frame=Widget,
                          Radiobutton=Widget, Button=Widget,
                          Label=Widget, Scale=Widget, Spinbox=Widget, Entry=Widget,
                          IntVar=Var, StringVar=Var, TclError=TclError).items():
        setattr(tk, name, val)
    for c in ("LEFT", "RIGHT", "TOP", "BOTTOM", "X", "BOTH", "RAISED", "SUNKEN",
              "SOLID", "ROUND", "HORIZONTAL", "NORMAL", "DISABLED"):
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
    return types.SimpleNamespace(x=x, y=y, x_root=x + 50, y_root=y + 80, state=1 if shift else 0)


class Board:
    """一個連上伺服器的假視窗。"""

    def __init__(self, name, room, server=None):
        self.root = fake_tk.Tk()
        self.net = client.Net(server or f"127.0.0.1:{PORT}", name, room, client.queue.Queue())
        self.app = client.App(self.root, self.net, self.net.inbox)
        self.c = self.app.canvas
        self.net.start()
        self.wait(lambda: self.app.online and self.app.my_name == name)

    def pump(self):
        self.app.poll()

    def wait(self, cond, timeout=6):
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

    def handle_xy(self, board, name):
        return board.app.handle_points(board.app.sel_bbox)[name]

    def test_resize_corner_keeps_ratio(self):
        A, B = self.a.app, self.b.app
        self.a.drag([(100, 100), (200, 100), (200, 160)])   # L 形線條，寬 4
        self.b.wait(lambda: B.strokes)
        sid = next(iter(A.strokes))
        A.set_tool("select")
        A.on_press(ev(150, 100)); A.on_release(ev(150, 100))
        self.assertIsNotNone(A.sel_bbox)
        self.assertEqual(len(A.canvas.find("handle")), 8)    # 8 個控制點
        hx, hy = self.handle_xy(self.a, "se")
        ox, oy = A.sel_bbox[0], A.sel_bbox[1]
        self.a.drag([(hx, hy), (ox + (hx - ox) * 2, oy + (hy - oy) * 1.2)])  # 往右下拉
        self.b.wait(lambda: B.strokes[sid]["width"] == 8)    # 等比例放大 2 倍，粗細 4 -> 8
        pa, pb = A.strokes[sid]["pts"], B.strokes[sid]["pts"]
        self.assertEqual(pa, pb)                              # 兩邊結果完全一致
        w = (pa[1][0] - pa[0][0]) * 1000
        h = (pa[2][1] - pa[1][1]) * 600
        self.assertAlmostEqual(w / h, 100 / 60, places=2)     # 維持長寬比
        self.assertAlmostEqual(w, 200, delta=1)
        self.assertEqual(A.selection, {sid})                  # 縮放後仍保持選取

    def test_resize_shift_and_edge(self):
        A, B = self.a.app, self.b.app
        self.a.drag([(100, 100), (200, 200)])
        self.b.wait(lambda: B.strokes)
        sid = next(iter(A.strokes))
        A.set_tool("select"); A.set_selection({sid})
        x0, y0, x1, y1 = A.sel_bbox
        hx, hy = self.handle_xy(self.a, "se")
        self.a.drag([(hx, hy), (x0 + (hx - x0) * 2, hy)], shift=True)   # Shift：只拉寬
        self.b.wait(lambda: B.strokes[sid]["pts"] == A.strokes[sid]["pts"]
                    and B.strokes[sid]["pts"][1][0] > 0.25)
        (ax, ay), (bx, by) = A.strokes[sid]["pts"]
        self.assertAlmostEqual((by - ay) * 600, 100, delta=1)  # 高度不變
        self.assertGreater((bx - ax) * 1000, 190)
        hx, hy = self.handle_xy(self.a, "n")                 # 上方邊：只改高度
        self.a.drag([(hx, hy), (hx + 300, hy - 50)])
        self.b.wait(lambda: B.strokes[sid]["pts"] == A.strokes[sid]["pts"]
                    and B.strokes[sid]["pts"][0][1] < ay - 0.05)
        self.assertAlmostEqual(A.strokes[sid]["pts"][0][0], ax, places=4)  # x 不變

    def test_resize_text_and_limits(self):
        A, B = self.a.app, self.b.app
        A.set_tool("text")
        A.on_press(ev(800, 400)); A.editor["w"].type("大字"); A.commit_editor()
        self.b.wait(lambda: B.strokes)
        sid = next(iter(A.strokes))
        A.set_tool("select"); A.set_selection({sid})
        hx, hy = self.handle_xy(self.a, "se")
        self.a.drag([(hx, hy), (hx + 5000, hy + 5000)])     # 拉到超出畫布
        self.b.wait(lambda: B.strokes[sid]["size"] > 20)
        self.assertEqual(B.strokes[sid], A.strokes[sid])
        self.assertLessEqual(B.strokes[sid]["x"], 1.0)
        hx, hy = self.handle_xy(self.a, "se")
        self.a.drag([(hx, hy), (0, 0)])                      # 縮到最小，不會翻轉或消失
        self.b.wait(lambda: B.strokes[sid]["size"] < 20)
        self.assertGreaterEqual(B.strokes[sid]["size"], 6)
        self.assertEqual(B.strokes[sid], A.strokes[sid])

    def test_context_menu(self):
        A, B = self.a.app, self.b.app
        menu = A.menu
        self.a.drag([(100, 100), (200, 100)])
        A.set_tool("text")
        A.on_press(ev(400, 300)); A.editor["w"].type("右鍵"); A.commit_editor()
        self.b.wait(lambda: len(B.strokes) == 2)
        line, txt = list(A.strokes)
        self.a.root.clipboard = None
        A.clip = None

        # 空白處按右鍵：沒有選取、剪貼簿空 -> 複製/剪下/刪除/貼上都不能按
        A.set_tool("pen")
        A.on_context(ev(800, 500))
        self.assertEqual(menu.popped, (850, 580))             # 選單出現在滑鼠位置
        for label in ("複製", "剪下", "貼上", "刪除"):
            self.assertEqual(menu.item(label)["state"], "disabled")
        self.assertIsNone(menu.item("編輯文字"))

        # 在線條上按右鍵：自動切到選取工具並選取它，可以複製
        A.on_context(ev(150, 100))
        self.assertEqual((A.tool, A.selection), ("select", {line}))
        menu.click("複製")
        # 在空白處按右鍵 -> 貼上到按右鍵的位置
        A.on_context(ev(600, 450))
        self.assertFalse(A.selection)
        menu.click("貼上")
        self.b.wait(lambda: len(B.strokes) == 3)
        new = next(s for s in B.strokes if s not in (line, txt))
        self.assertEqual(B.strokes[new]["pts"][0], [0.6, 0.75])
        self.assertEqual(A.selection, {new})                  # 貼上後選取新物件

        # 文字上按右鍵 -> 有「編輯文字」；刪除
        A.on_context(ev(405, 305))
        self.assertEqual(A.selection, {txt})
        menu.click("編輯文字")
        self.assertEqual(A.editor["sid"], txt)
        A.commit_editor(cancel=True)
        A.on_context(ev(405, 305))
        menu.click("刪除")
        self.b.wait(lambda: txt not in B.strokes)

        # 多選後在選取範圍內 (物件之間的空白) 按右鍵，不會取消選取；剪下
        A.select_all()
        self.assertEqual(len(A.selection), 2)
        x0, y0, x1, y1 = A.sel_bbox
        A.on_context(ev(x0 + 1, y1 - 1))
        self.assertEqual(len(A.selection), 2)
        menu.click("剪下")
        self.b.wait(lambda: not B.strokes)
        A.on_context(ev(10, 10))
        self.assertEqual(menu.item("貼上")["state"], "normal")
        self.assertEqual(menu.item("全選")["state"], "disabled")

        # 剪貼簿是一般文字 -> 貼上成文字框，位置就是按右鍵的地方
        self.a.root.clipboard = "外面複製的字"
        A.on_context(ev(300, 120))
        menu.click("貼上")
        self.b.wait(lambda: self.texts(self.b) == ["外面複製的字"])
        o = next(iter(B.strokes.values()))
        self.assertEqual((o["x"], o["y"]), (0.3, 0.2))

    def test_room_code_shown(self):
        self.assertEqual(self.a.app.code,
                         client.roomcode.encode(client.local_ip(), PORT, self.room))
        self.a.app.copy_code()
        self.assertEqual(self.a.root.clipboard, self.a.app.code)
        self.assertTrue(any(w.kw.get("text") == self.a.app.code for w in self.a.root.widgets))

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


class RestartTest(unittest.TestCase):
    """線上伺服器重啟 (雲端休眠 / 重新部署) 後，客戶端自動重連並把畫面復原。"""

    PORT = 9195

    def start(self):
        p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(self.PORT),
                              "--public"], cwd=HERE, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.PORT), timeout=0.2).close()
                return p
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server not up")

    def test_restore_after_restart(self):
        server = f"ws://127.0.0.1:{self.PORT}"
        p = self.start()
        a, b = Board("amy", "ROOM1", server=server), Board("ben", "ROOM1", server=server)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.drag([(100, 100), (200, 150)])
        b.app.set_tool("text")
        b.app.on_press(ev(300, 300)); b.app.editor["w"].type("還在嗎"); b.app.commit_editor()
        a.wait(lambda: len(a.app.strokes) == 2)
        b.wait(lambda: len(b.app.strokes) == 2)
        before = {sid: (o.get("owner"), o.get("text")) for sid, o in a.app.strokes.items()}

        p.terminate(); p.wait()                                # 伺服器掛掉
        a.wait(lambda: not a.app.online)
        self.assertEqual(len(a.app.strokes), 2)               # 離線時畫面還在
        p = self.start()                                       # 重新啟動 (房間全部清空)
        self.addCleanup(lambda: (p.terminate(), p.wait()))
        for x in (a, b):                                       # 兩邊都自動重連
            x.wait(lambda x=x: x.app.online, timeout=10)
        time.sleep(0.5)
        for x in (a, b):
            x.pump()
        for x in (a, b):                                       # 畫面復原、沒有重複
            x.wait(lambda x=x: {sid: (o.get("owner"), o.get("text"))
                                for sid, o in x.app.strokes.items()} == before)
        c = Board("cat", "ROOM1", server=server)               # 新加入的人也看得到
        self.addCleanup(c.close)
        c.wait(lambda: len(c.app.strokes) == 2)
        b.app.undo()                                           # 作者資訊保留 -> 可復原自己的
        a.wait(lambda: len(a.app.strokes) == 1)
        self.assertNotIn("還在嗎", [o.get("text") for o in a.app.strokes.values()])


class LauncherTest(unittest.TestCase):
    """start.py 不帶參數時的小視窗：建立房間 / 輸入房間號加入。"""

    def run_launcher(self, action):
        import start
        fake_tk.Tk.on_mainloop = action
        try:
            return start.launcher_gui()
        finally:
            fake_tk.Tk.on_mainloop = None

    @staticmethod
    def entries(root):
        return [w for w in root.widgets if isinstance(w, fake_tk.Entry)]

    def test_create(self):
        def act(root):
            self.entries(root)[0].kw["textvariable"].set("  amy ")
            root.button("建立房間").kw["command"]()
        self.assertEqual(self.run_launcher(act), {"action": "create", "name": "amy"})

    def test_join(self):
        p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", "9189",
                              "--udp-port", "9190"], cwd=HERE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.terminate(), p.wait()))
        time.sleep(0.8)
        seen = {}

        def act(root):
            code_var = self.entries(root)[1].kw["textvariable"]
            status = next(w for w in root.widgets if w.kw.get("wraplength"))
            join = root.button("加入房間").kw["command"]
            settled = lambda: not status.kw["text"].startswith("連線中") or root.destroyed  # noqa
            join()
            seen["empty"] = status.kw["text"]
            for key, code in [("bad", "A0?11"),
                              ("down", client.roomcode.encode("127.0.0.1", 9191))]:  # 沒有人開
                code_var.set(code)
                join()
                root.pump(settled)
                seen[key] = status.kw["text"]
            code_var.set(client.roomcode.encode("127.0.0.1", 9189).lower())
            join()
            root.pump(lambda: root.destroyed)
        r = self.run_launcher(act)
        self.assertEqual(seen["empty"], "請輸入房間號")
        self.assertIn("無效的字元", seen["bad"])
        self.assertIn("沒有開伺服器", seen["down"])
        self.assertEqual(r["action"], "join")
        self.assertEqual(r["target"][:3], ("127.0.0.1", 9189, "lobby"))

    def test_online_mode(self):
        """設定線上伺服器時：建立房間拿到隨機房間號、輸入房間號加入、打錯會提示。"""
        p = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", "9192",
                              "--public"], cwd=HERE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.terminate(), p.wait()))
        time.sleep(0.8)
        import start
        server = "ws://127.0.0.1:9192"

        def create(root):
            root.button("建立房間").kw["command"]()
            root.pump(lambda: root.destroyed)
        fake_tk.Tk.on_mainloop = create
        try:
            r = start.launcher_gui(server)
        finally:
            fake_tk.Tk.on_mainloop = None
        self.assertEqual(r["action"], "online")
        code = r["code"]
        self.assertEqual(len(code), 5)
        seen = {}

        def join(root):
            code_var = self.entries(root)[1].kw["textvariable"]
            status = next(w for w in root.widgets if w.kw.get("wraplength"))
            btn = root.button("加入房間").kw["command"]
            code_var.set("ZZZZZ" if code != "ZZZZZ" else "00000")
            btn()
            root.pump(lambda: not status.kw["text"].startswith("連線中"))
            seen["missing"] = status.kw["text"]
            code_var.set(code.lower())
            btn()
            root.pump(lambda: root.destroyed)
        fake_tk.Tk.on_mainloop = join
        try:
            r = start.launcher_gui(server)
        finally:
            fake_tk.Tk.on_mainloop = None
        self.assertIn("找不到房間", seen["missing"])
        self.assertEqual(r, {"action": "online", "code": code, "name": r["name"]})

        # 兩個客戶端用 WebSocket 連進同一個房間協作
        a = Board("amy", code, server=server)
        b = Board("ben", code, server=server)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual(a.app.code, code)                     # 視窗上顯示線上房間號
        a.drag([(100, 100), (300, 200)])
        b.wait(lambda: len(b.app.strokes) == 1)
        a.app.set_tool("text")
        a.app.on_press(ev(400, 300)); a.app.editor["w"].type("線上你好"); a.app.commit_editor()
        b.wait(lambda: any(o.get("text") == "線上你好" for o in b.app.strokes.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
