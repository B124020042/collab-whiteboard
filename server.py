#!/usr/bin/env python3
"""協作白板伺服器 (TCP + asyncio)

- 每個房間各自保存筆跡，新加入者會收到完整重播
- 收到的筆跡依伺服器接收順序轉發給同房間其他人 (伺服器順序即為最終順序)
- 物件有筆跡與文字兩種；任何人都能移動 / 刪除 / 編輯文字，復原只會移除「自己」最後新增的物件
- 同一個 port 同時支援三種連線 (看第一行自動判斷)：
    一行 JSON          -> 原本的 TCP 協定 (區域網路的 client.py)
    HTTP + Upgrade     -> WebSocket (線上：雲端平台只轉發 HTTP / WebSocket)
    一般 HTTP GET      -> 回傳狀態頁 (雲端平台的健康檢查)
- create_room 產生隨機房間號；--public (雲端部署用) 時 pong 不公開房間清單、空房間保留

    python3 server.py --public             # 雲端 (Render 會用這個指令，port 取自環境變數 PORT)
    python3 server.py                      # 開發 / 測試：在本機執行 (預設 port 9100)
"""
import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import socket
import time
from collections import OrderedDict

from transport import MAX_MESSAGE, read_http_headers, ws_accept_key, ws_frame, ws_read_message
from protocol import (APP_ID, DEFAULT_PORT, MAX_FONT, MAX_PTS_PER_MSG, MAX_TEXT, MIN_FONT, clamp01,
                      clamp_move, clamp_scale, clean_obj, decode, encode, is_text, move_obj,
                      scale_obj, valid_color)

MAX_NAME = 16
MAX_ROOM = 20
MAX_STROKES = 5000          # 每房間物件上限，超過就丟掉最舊的
MAX_SIDS_PER_MSG = MAX_STROKES
MAX_ROOMS = 500
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford Base32 (不含 I L O U)
CODE_LEN = 5
HTTP_METHODS = (b"GET ", b"HEAD ", b"POST ", b"PUT ", b"OPTIONS ", b"DELETE ", b"PATCH ")


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.ws = False                     # True = WebSocket 連線
        self.name = None
        self.room = None
        self.addr = writer.get_extra_info("peername")


class Room:
    def __init__(self, name):
        self.name = name
        self.users = {}                    # name -> Client
        self.strokes = OrderedDict()       # sid -> stroke dict
        self.touched = time.monotonic()    # 最後有人使用的時間 (清理閒置房間用)


class WhiteboardServer:
    def __init__(self, port=DEFAULT_PORT, public=False):
        self.rooms = {}
        self.port = port
        self.public = public

    def info(self):
        users = sum(len(r.users) for r in self.rooms.values())
        if self.public:                     # 線上伺服器不公開房間號
            return {"type": "pong", "app": APP_ID, "port": self.port, "host": "online",
                    "users": users, "rooms": {}, "public": True}
        return {"type": "pong", "app": APP_ID, "port": self.port, "host": socket.gethostname(),
                "users": users,
                "rooms": {n: len(r.users) for n, r in self.rooms.items() if r.users}}

    def new_room_code(self):
        self.purge_rooms()
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if code not in self.rooms:
                self.rooms[code] = Room(code)
                return code

    def purge_rooms(self):
        """房間太多時，移除沒有人在用、最久沒動的房間。"""
        if len(self.rooms) < MAX_ROOMS:
            return
        idle = sorted((r.touched, n) for n, r in self.rooms.items() if not r.users)
        for _, n in idle[:len(self.rooms) - MAX_ROOMS + 1]:
            del self.rooms[n]

    # ---------- 傳送 ----------
    async def send(self, c, data: bytes):
        try:
            c.writer.write(ws_frame(data[:-1]) if c.ws else data)
            await asyncio.wait_for(c.writer.drain(), timeout=5)
        except Exception:
            pass  # 斷線的連線由讀取迴圈清理

    async def broadcast(self, room, exclude=None, **obj):
        data = encode(**obj)
        await asyncio.gather(*(self.send(c, data)
                               for c in list(room.users.values()) if c is not exclude))

    # ---------- 連線 ----------
    async def handle(self, reader, writer):
        c = Client(writer)
        try:
            first = await reader.readline()
            if first.startswith(HTTP_METHODS):
                await self.handle_http(c, reader, writer, first)
                return
            logging.info("連線 (TCP): %s", c.addr)
            line = first
            while line:                             # 原本的「一行一則 JSON」協定
                await self.handle_raw(c, line)
                line = await reader.readline()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ValueError):
            pass
        except Exception:
            logging.exception("處理 %s 發生錯誤", c.addr)
        finally:
            await self.leave(c)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def handle_raw(self, c, raw: bytes):
        try:
            msg = decode(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(msg, dict):
            await self.dispatch(c, msg)

    async def handle_http(self, c, reader, writer, first):
        method, path, headers = await read_http_headers(reader, first)
        if "websocket" in headers.get("upgrade", "").lower() and "sec-websocket-key" in headers:
            accept = ws_accept_key(headers["sec-websocket-key"])
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            await writer.drain()
            c.ws = True
            logging.info("連線 (WebSocket): %s", headers.get("x-forwarded-for", c.addr))
            while True:
                data = await ws_read_message(reader, writer)
                if data is None:
                    return
                await self.handle_raw(c, data)
        # 一般 HTTP：狀態頁 / 健康檢查
        info = self.info()
        if path.split("?")[0] in ("/", "/healthz"):
            body = (f"collab-whiteboard server OK\nonline users: {info['users']}\n"
                    f"rooms: {len(self.rooms)}\n").encode()
            status = "200 OK"
        else:
            body, status = b"not found\n", "404 Not Found"
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
                     + (b"" if method == "HEAD" else body))
        await writer.drain()

    async def dispatch(self, c, msg):
        t = msg.get("type")
        if t == "hello":
            await self.hello(c, msg)
            return
        if t == "ping":
            await self.send(c, encode(**self.info()))
            return
        if t == "create_room":
            await self.send(c, encode(type="room_created", code=self.new_room_code()))
            return
        if t == "room_info":
            r = self.rooms.get(str(msg.get("room", ""))[:MAX_ROOM])
            await self.send(c, encode(type="room_info", room=msg.get("room"),
                                      exists=r is not None, users=len(r.users) if r else 0))
            return
        if c.room is None:
            await self.send(c, encode(type="error", text="請先送出 hello"))
            return
        room = self.rooms[c.room]
        room.touched = time.monotonic()
        if t == "draw":
            await self.draw(c, room, msg)
        elif t == "text":
            await self.text(c, room, msg)
        elif t == "delete":
            removed = [sid for sid in self.sid_list(msg) if room.strokes.pop(sid, None)]
            for sid in removed:
                await self.broadcast(room, type="remove", sid=sid)
        elif t == "move":
            sids = [sid for sid in self.sid_list(msg) if sid in room.strokes]
            try:
                objs = [room.strokes[sid] for sid in sids]
                dx, dy = clamp_move(objs, msg["dx"], msg["dy"])
            except (KeyError, TypeError, ValueError):
                return
            if sids and (dx or dy):
                for o in objs:
                    move_obj(o, dx, dy)
                await self.broadcast(room, exclude=c, type="move", sids=sids, dx=dx, dy=dy)
        elif t == "scale":
            sids = [sid for sid in self.sid_list(msg) if sid in room.strokes]
            try:
                objs = [room.strokes[sid] for sid in sids]
                ox, oy, sx, sy = clamp_scale(objs, msg["ox"], msg["oy"], msg["sx"], msg["sy"])
            except (KeyError, TypeError, ValueError):
                return
            if sids and (sx != 1 or sy != 1):
                for o in objs:
                    scale_obj(o, ox, oy, sx, sy)
                await self.broadcast(room, exclude=c, type="scale", sids=sids,
                                     ox=ox, oy=oy, sx=sx, sy=sy)
        elif t == "cursor":
            try:
                x, y = clamp01(msg["x"]), clamp01(msg["y"])
            except (KeyError, TypeError, ValueError):
                return
            await self.broadcast(room, exclude=c, type="cursor", name=c.name, x=x, y=y)
        elif t == "undo":
            for sid in reversed(room.strokes):
                if room.strokes[sid]["owner"] == c.name:
                    del room.strokes[sid]
                    await self.broadcast(room, type="remove", sid=sid)
                    break
        elif t == "restore":
            await self.restore(c, room, msg)
        elif t == "clear":
            room.strokes.clear()
            await self.broadcast(room, type="clear", by=c.name)

    async def hello(self, c, msg):
        if c.room is not None:
            await self.leave(c)
        base = re.sub(r"\s+", "_", str(msg.get("name", "")).strip())[:MAX_NAME] or "guest"
        room_name = str(msg.get("room", "lobby")).strip()[:MAX_ROOM] or "lobby"
        room = self.rooms.setdefault(room_name, Room(room_name))
        name, i = base, 2
        while name in room.users:           # 暱稱重複就自動加編號
            name, i = f"{base}_{i}", i + 1
        c.name, c.room = name, room_name
        room.users[name] = c
        await self.send(c, encode(type="welcome", name=name, room=room_name,
                                  users=sorted(room.users),
                                  strokes=list(room.strokes.values())))
        await self.broadcast(room, exclude=c, type="user_join", name=name)
        logging.info("%s 加入 [%s] (%d 人)", name, room_name, len(room.users))

    async def draw(self, c, room, msg):
        try:
            sid = str(msg["sid"])[:40]
            color = valid_color(msg.get("color"))
            width = int(min(60, max(1, int(msg.get("width", 3)))))
            pts = [[clamp01(p[0]), clamp01(p[1])] for p in msg["pts"][:MAX_PTS_PER_MSG]]
        except (KeyError, TypeError, ValueError, IndexError):
            return
        if not pts:
            return
        s = room.strokes.get(sid)
        if s is None:
            s = room.strokes[sid] = dict(sid=sid, owner=c.name, color=color, width=width, pts=[])
            while len(room.strokes) > MAX_STROKES:
                room.strokes.popitem(last=False)
        elif is_text(s) or s["owner"] != c.name:
            return                          # 不能接續別人的筆跡
        s["pts"].extend(pts)
        await self.broadcast(room, exclude=c, type="draw", sid=sid, owner=c.name,
                             color=s["color"], width=s["width"], pts=pts)

    async def restore(self, c, room, msg):
        """伺服器重啟後房間是空的：接受第一個重新連上的人傳回的畫面，並同步給所有人。"""
        if room.strokes or not isinstance(msg.get("strokes"), list):
            return                              # 房間已經有內容 (別人先復原了) -> 忽略
        for s in msg["strokes"][:MAX_STROKES]:
            if not isinstance(s, dict):
                continue
            o = clean_obj(s)
            sid = str(s.get("sid", ""))[:40]
            if o and sid and sid not in room.strokes:
                o["sid"] = sid
                o["owner"] = str(s.get("owner") or c.name)[:MAX_NAME + 4]
                room.strokes[sid] = o
        if room.strokes:
            logging.info("%s 復原了 [%s] 的 %d 個物件", c.name, room.name, len(room.strokes))
            await self.broadcast(room, type="reset", by=c.name, strokes=list(room.strokes.values()))

    @staticmethod
    def sid_list(msg):
        sids = msg.get("sids")
        if not isinstance(sids, list):
            return []
        return list(dict.fromkeys(str(x)[:40] for x in sids[:MAX_SIDS_PER_MSG]))

    async def text(self, c, room, msg):
        """新增文字框；sid 已存在時代表修改文字內容 / 顏色 / 字級 (位置以伺服器為準)。"""
        try:
            sid = str(msg["sid"])[:40]
            text = str(msg.get("text", ""))[:MAX_TEXT]
            size = min(MAX_FONT, max(MIN_FONT, int(msg.get("size", 20))))
            x, y = clamp01(msg["x"]), clamp01(msg["y"])
        except (KeyError, TypeError, ValueError):
            return
        if not text.strip():
            return
        color = valid_color(msg.get("color"))
        s = room.strokes.get(sid)
        if s is None:
            s = room.strokes[sid] = dict(sid=sid, owner=c.name, kind="text", x=x, y=y)
            while len(room.strokes) > MAX_STROKES:
                room.strokes.popitem(last=False)
        elif not is_text(s):
            return                          # 不能把筆跡改成文字
        s.update(text=text, color=color, size=size)
        await self.broadcast(room, exclude=c, type="text", **s)

    async def leave(self, c):
        if c.room is None:
            return
        room = self.rooms.get(c.room)
        if room and room.users.get(c.name) is c:
            del room.users[c.name]
            await self.broadcast(room, type="user_leave", name=c.name)
            logging.info("%s 離開 [%s]", c.name, c.room)
            room.touched = time.monotonic()
            if not room.users and not room.strokes and not self.public:
                del self.rooms[c.room]
        c.room = None


async def main():
    ap = argparse.ArgumentParser(description="協作白板伺服器")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                    help="TCP port (預設讀環境變數 PORT，沒有則 9100)")
    ap.add_argument("--public", action="store_true",
                    help="部署到雲端時使用 (不公開房間清單、空房間保留)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    srv = WhiteboardServer(args.port, public=args.public)
    server = await asyncio.start_server(srv.handle, args.host, args.port, limit=MAX_MESSAGE)
    logging.info("白板伺服器啟動於 port %d%s", args.port, " (public)" if args.public else "")
    if not args.public:
        logging.info("本機測試：python3 start.py --server ws://127.0.0.1:%d", args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
