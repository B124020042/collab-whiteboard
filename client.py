#!/usr/bin/env python3
"""協作白板 GUI 客戶端 (tkinter + socket)

執行緒模型：
  - 網路執行緒 (Net)：負責連線、自動重連、讀取伺服器訊息 -> 放進 queue
  - 主執行緒 (tkinter)：每 15ms 從 queue 取訊息更新畫面 (tkinter 只能在主執行緒操作)
效能：滑鼠拖曳時先在本機畫出，點座標累積起來每 30ms 批次送出一次。
"""
import argparse
import json
import queue
import socket
import threading
import time
import tkinter as tk
import uuid
import zlib
from tkinter import colorchooser, filedialog, messagebox, simpledialog

from protocol import DEFAULT_PORT, decode, encode

BG = "#ffffff"
PALETTE = ["#000000", "#e53935", "#fb8c00", "#fdd835", "#43a047",
           "#1e88e5", "#8e24aa", "#6d4c41"]
FLUSH_MS = 30          # 筆跡批次送出間隔
CURSOR_INTERVAL = 0.05  # 游標位置最多每 50ms 送一次
RECONNECT_SEC = 2


# ====================== 網路執行緒 ======================
class Net(threading.Thread):
    def __init__(self, host, port, name, room, inbox):
        super().__init__(daemon=True)
        self.host, self.port, self.name, self.room = host, port, name, room
        self.inbox = inbox
        self.sock = None
        self.lock = threading.Lock()
        self.running = True

    def run(self):
        while self.running:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.lock:
                    self.sock = sock
                self.send(type="hello", name=self.name, room=self.room)
                self.inbox.put({"type": "_status", "online": True})
                for line in sock.makefile("rb"):
                    try:
                        self.inbox.put(decode(line))
                    except ValueError:
                        pass
            except OSError:
                pass
            with self.lock:
                s, self.sock = self.sock, None
            if s:
                try:
                    s.close()
                except OSError:
                    pass
            if self.running:
                self.inbox.put({"type": "_status", "online": False})
                time.sleep(RECONNECT_SEC)   # 自動重連

    def send(self, **obj):
        with self.lock:
            if not self.sock:
                return False
            try:
                self.sock.sendall(encode(**obj))
                return True
            except OSError:
                return False

    def stop(self):
        self.running = False
        with self.lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def user_color(name):
    """依暱稱產生固定顏色，用於顯示他人游標。"""
    h = zlib.crc32(name.encode())
    return "#%02x%02x%02x" % (60 + h % 160, 60 + (h >> 8) % 160, 60 + (h >> 16) % 160)


# ====================== GUI ======================
class App:
    def __init__(self, root, net, inbox):
        self.root, self.net, self.inbox = root, net, inbox
        self.my_name = net.name
        self.online = False
        self.users = []
        self.strokes = {}          # sid -> {"color","width","pts","owner"}
        self.cursors = {}          # name -> [dot_id, text_id, x, y]
        self.color = PALETTE[0]
        self.eraser = False
        self.cur_sid = None
        self.pending = []          # 尚未送出的點
        self.last_sent = None      # 上一批最後一點 (讓批次之間線條連續)
        self.last_px = None
        self.last_cursor_t = 0.0
        self.resize_job = None
        self.build_ui()
        self.root.after(15, self.poll)
        self.root.after(FLUSH_MS, self.flush_timer)

    # ---------- 介面 ----------
    def build_ui(self):
        self.root.title("協作白板")
        self.root.geometry("1000x680")
        bar = tk.Frame(self.root, padx=6, pady=4)
        bar.pack(side=tk.TOP, fill=tk.X)

        self.swatches = []
        for c in PALETTE:
            sw = tk.Label(bar, bg=c, width=2, height=1, relief=tk.RAISED, bd=2)
            sw.pack(side=tk.LEFT, padx=2)
            sw.bind("<Button-1>", lambda e, c=c: self.set_color(c))
            self.swatches.append((c, sw))
        tk.Button(bar, text="自訂色", command=self.pick_color).pack(side=tk.LEFT, padx=4)
        self.cur_swatch = tk.Label(bar, bg=self.color, width=3, relief=tk.SUNKEN, bd=2)
        self.cur_swatch.pack(side=tk.LEFT, padx=2)

        tk.Label(bar, text="粗細").pack(side=tk.LEFT, padx=(10, 0))
        self.width_var = tk.IntVar(value=4)
        tk.Scale(bar, from_=1, to=40, orient=tk.HORIZONTAL, variable=self.width_var,
                 length=110, showvalue=True).pack(side=tk.LEFT)

        self.eraser_btn = tk.Button(bar, text="橡皮擦", command=self.toggle_eraser)
        self.eraser_btn.pack(side=tk.LEFT, padx=4)
        tk.Button(bar, text="復原 (Ctrl+Z)", command=self.undo).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="清除畫布", command=self.clear).pack(side=tk.LEFT, padx=2)
        tk.Button(bar, text="儲存", command=self.save).pack(side=tk.LEFT, padx=2)

        self.status = tk.Label(bar, text="連線中...", fg="#888", anchor="e")
        self.status.pack(side=tk.RIGHT)

        self.users_lbl = tk.Label(self.root, text="", anchor="w", fg="#555", padx=8)
        self.users_lbl.pack(side=tk.BOTTOM, fill=tk.X)

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_move)
        self.canvas.bind("<Configure>", self.on_resize)
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Command-z>", lambda e: self.undo())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_color(self.color)

    def refresh_status(self):
        if self.online:
            self.status.config(text=f"● 線上  {self.my_name} @ {self.net.room}", fg="#2e7d32")
        else:
            self.status.config(text="● 離線，重新連線中...", fg="#c62828")
        self.users_lbl.config(text=f"線上 {len(self.users)} 人: " + ", ".join(self.users))

    def set_color(self, c):
        self.color = c
        self.eraser = False
        self.eraser_btn.config(relief=tk.RAISED)
        self.cur_swatch.config(bg=c)
        for col, sw in self.swatches:
            sw.config(relief=tk.SUNKEN if col == c else tk.RAISED)

    def pick_color(self):
        c = colorchooser.askcolor(color=self.color, title="選擇顏色")[1]
        if c:
            self.set_color(c.lower())

    def toggle_eraser(self):
        self.eraser = not self.eraser
        self.eraser_btn.config(relief=tk.SUNKEN if self.eraser else tk.RAISED)

    # ---------- 座標轉換 ----------
    def size(self):
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def to_norm(self, x, y):
        w, h = self.size()
        return [min(1.0, max(0.0, x / w)), min(1.0, max(0.0, y / h))]

    def to_px(self, p):
        w, h = self.size()
        return p[0] * w, p[1] * h

    # ---------- 繪製 ----------
    def draw_pts(self, sid, color, width, pts):
        tag = "s_" + sid               # 加前綴，避免全數字 tag 被當成 item id
        coords = []
        for p in pts:
            coords.extend(self.to_px(p))
        if len(coords) == 2:           # 單點 -> 畫圓點
            x, y = coords
            r = width / 2
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline=color,
                                    tags=(tag, "stroke"))
        elif coords:
            self.canvas.create_line(*coords, fill=color, width=width, capstyle=tk.ROUND,
                                    joinstyle=tk.ROUND, smooth=False, tags=(tag, "stroke"))
        self.canvas.tag_raise("cursor")

    def redraw_all(self):
        self.canvas.delete("stroke")
        for sid, s in self.strokes.items():
            self.draw_pts(sid, s["color"], s["width"], s["pts"])
        for name, cur in self.cursors.items():
            self.place_cursor(name, cur[2], cur[3])

    # ---------- 滑鼠事件 ----------
    def on_press(self, e):
        self.cur_sid = uuid.uuid4().hex[:12]
        p = self.to_norm(e.x, e.y)
        color = BG if self.eraser else self.color
        width = self.width_var.get() * (3 if self.eraser else 1)
        self.strokes[self.cur_sid] = {"color": color, "width": width, "pts": [p],
                                      "owner": self.my_name}
        self.pending, self.last_sent, self.last_px = [p], None, (e.x, e.y)
        self.draw_pts(self.cur_sid, color, width, [p])

    def on_drag(self, e):
        if not self.cur_sid:
            return
        s = self.strokes[self.cur_sid]
        p = self.to_norm(e.x, e.y)
        s["pts"].append(p)
        self.pending.append(p)
        lx, ly = self.last_px
        self.canvas.create_line(lx, ly, e.x, e.y, fill=s["color"], width=s["width"],
                                capstyle=tk.ROUND, tags=("s_" + self.cur_sid, "stroke"))
        self.last_px = (e.x, e.y)
        self.on_move(e)

    def on_release(self, e):
        self.flush()
        self.cur_sid = None

    def on_move(self, e):
        now = time.monotonic()
        if now - self.last_cursor_t >= CURSOR_INTERVAL:
            self.last_cursor_t = now
            x, y = self.to_norm(e.x, e.y)
            self.net.send(type="cursor", x=round(x, 4), y=round(y, 4))

    def flush(self):
        """把累積的點一次送出 (批次化，大幅減少封包數)。"""
        if not self.cur_sid or not self.pending:
            return
        s = self.strokes[self.cur_sid]
        pts = ([self.last_sent] if self.last_sent else []) + self.pending
        pts = [[round(x, 4), round(y, 4)] for x, y in pts]
        self.net.send(type="draw", sid=self.cur_sid, color=s["color"],
                      width=s["width"], pts=pts)
        self.last_sent = self.pending[-1]
        self.pending = []

    def flush_timer(self):
        self.flush()
        self.root.after(FLUSH_MS, self.flush_timer)

    def on_resize(self, e):
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(80, self.redraw_all)

    # ---------- 工具 ----------
    def undo(self):
        self.flush()
        self.net.send(type="undo")      # 伺服器會廣播 remove 給所有人 (包含自己)

    def clear(self):
        if messagebox.askyesno("清除畫布", "確定要清除所有人的畫布嗎?"):
            self.net.send(type="clear")

    def save(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".ps",
            filetypes=[("PostScript 圖檔", "*.ps"), ("筆跡資料 JSON", "*.json")])
        if not path:
            return
        if path.lower().endswith(".json"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(list(self.strokes.values()), f, ensure_ascii=False)
        else:
            self.canvas.itemconfigure("cursor", state="hidden")
            self.canvas.postscript(file=path, colormode="color")
            self.canvas.itemconfigure("cursor", state="normal")
        messagebox.showinfo("儲存", f"已儲存到\n{path}")

    # ---------- 他人游標 ----------
    def place_cursor(self, name, x, y):
        px, py = self.to_px((x, y))
        cur = self.cursors.get(name)
        if cur is None:
            col = user_color(name)
            dot = self.canvas.create_oval(0, 0, 0, 0, fill=col, outline="white",
                                          tags=("cursor",))
            txt = self.canvas.create_text(0, 0, text=name, fill=col, anchor="nw",
                                          font=("TkDefaultFont", 10, "bold"), tags=("cursor",))
            cur = self.cursors[name] = [dot, txt, x, y]
        cur[2], cur[3] = x, y
        self.canvas.coords(cur[0], px - 5, py - 5, px + 5, py + 5)
        self.canvas.coords(cur[1], px + 8, py + 6)
        self.canvas.tag_raise("cursor")

    def remove_cursor(self, name):
        cur = self.cursors.pop(name, None)
        if cur:
            self.canvas.delete(cur[0], cur[1])

    # ---------- 處理伺服器訊息 ----------
    def poll(self):
        for _ in range(2000):
            try:
                m = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle(m)
            except (KeyError, TypeError, ValueError, tk.TclError):
                pass
        self.root.after(15, self.poll)

    def handle(self, m):
        t = m.get("type")
        if t == "_status":
            self.online = m["online"]
            if not self.online:
                self.users = []
                for n in list(self.cursors):
                    self.remove_cursor(n)
            self.refresh_status()
        elif t == "welcome":              # (重新)連線 -> 以伺服器的完整狀態為準
            self.my_name = m["name"]
            self.users = m["users"]
            self.cur_sid, self.pending = None, []
            self.strokes = {s["sid"]: s for s in m["strokes"]}
            self.redraw_all()
            self.refresh_status()
        elif t == "draw":
            s = self.strokes.setdefault(m["sid"], {"color": m["color"], "width": m["width"],
                                                   "pts": [], "owner": m["owner"]})
            s["pts"].extend(m["pts"])
            self.draw_pts(m["sid"], m["color"], m["width"], m["pts"])
        elif t == "remove":
            self.strokes.pop(m["sid"], None)
            self.canvas.delete("s_" + m["sid"])
            if self.cur_sid == m["sid"]:
                self.cur_sid = None
        elif t == "clear":
            self.strokes.clear()
            self.canvas.delete("stroke")
            self.cur_sid = None
            self.status.config(text=f"{m['by']} 清除了畫布", fg="#1565c0")
            self.root.after(2000, self.refresh_status)
        elif t == "cursor":
            self.place_cursor(m["name"], m["x"], m["y"])
        elif t == "user_join":
            if m["name"] not in self.users:
                self.users = sorted(self.users + [m["name"]])
            self.refresh_status()
        elif t == "user_leave":
            self.users = [u for u in self.users if u != m["name"]]
            self.remove_cursor(m["name"])
            self.refresh_status()
        elif t == "error":
            self.status.config(text="錯誤: " + m.get("text", ""), fg="#c62828")

    def on_close(self):
        self.net.stop()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="協作白板客戶端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", help="暱稱 (未指定會詢問)")
    ap.add_argument("--room", default="lobby", help="房間名稱 (預設 lobby)")
    args = ap.parse_args()

    root = tk.Tk()
    name = args.name
    if not name:
        root.withdraw()
        name = simpledialog.askstring("協作白板", "請輸入暱稱:", parent=root) or "guest"
        root.deiconify()
    inbox = queue.Queue()
    net = Net(args.host, args.port, name, args.room, inbox)
    App(root, net, inbox)
    net.start()
    root.mainloop()


if __name__ == "__main__":
    main()
