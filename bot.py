#!/usr/bin/env python3
"""Demo 機器人：連上白板自動畫圖案 (玫瑰線、螺旋)，方便單人展示多人協作或做壓力測試。

    python3 bot.py --name robot --count 5
"""
import argparse
import math
import random
import socket
import threading
import time
import uuid

from protocol import DEFAULT_PORT, encode

COLORS = ["#e53935", "#fb8c00", "#43a047", "#1e88e5", "#8e24aa", "#00897b"]


def shape(kind, cx, cy, r, n=240):
    pts = []
    k = random.choice([3, 4, 5, 6])
    for i in range(n + 1):
        t = 2 * math.pi * i / n
        if kind == "rose":
            rr = r * math.cos(k * t)
            x, y = cx + rr * math.cos(t), cy + rr * math.sin(t)
        else:  # spiral
            rr = r * i / n
            x, y = cx + rr * math.cos(4 * t), cy + rr * math.sin(4 * t)
        pts.append([round(min(1, max(0, x)), 4), round(min(1, max(0, y)), 4)])
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default="robot")
    ap.add_argument("--room", default="lobby")
    ap.add_argument("--count", type=int, default=5, help="要畫幾個圖案")
    ap.add_argument("--batch", type=int, default=8, help="每批送出的點數")
    args = ap.parse_args()

    sock = socket.create_connection((args.host, args.port))
    # 背景丟棄伺服器傳來的訊息，避免接收緩衝區塞滿
    threading.Thread(target=lambda: [None for _ in sock.makefile("rb")], daemon=True).start()
    sock.sendall(encode(type="hello", name=args.name, room=args.room))

    for _ in range(args.count):
        pts = shape(random.choice(["rose", "spiral"]),
                    random.uniform(0.2, 0.8), random.uniform(0.25, 0.75), random.uniform(0.08, 0.2))
        sid, color = uuid.uuid4().hex[:12], random.choice(COLORS)
        for i in range(0, len(pts), args.batch):
            chunk = pts[max(0, i - 1): i + args.batch]   # 多帶前一點讓線段連續
            sock.sendall(encode(type="draw", sid=sid, color=color, width=3, pts=chunk))
            x, y = chunk[-1]
            sock.sendall(encode(type="cursor", x=x, y=y))
            time.sleep(0.03)
        time.sleep(0.3)
    sock.close()


if __name__ == "__main__":
    main()
