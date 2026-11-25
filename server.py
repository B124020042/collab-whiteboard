#!/usr/bin/env python3
"""協作白板伺服器 (TCP + asyncio)

- 每個房間各自保存筆跡，新加入者會收到完整重播
- 收到的筆跡依伺服器接收順序轉發給同房間其他人 (伺服器順序即為最終順序)
- 物件有筆跡與文字兩種；任何人都能移動 / 刪除 / 編輯文字，復原只會移除「自己」最後新增的物件
"""
import argparse
import asyncio
import logging
import re
from collections import OrderedDict

from protocol import (DEFAULT_PORT, MAX_FONT, MAX_PTS_PER_MSG, MAX_TEXT, MIN_FONT, clamp01,
                      clamp_move, clamp_scale, decode, encode, is_text, move_obj, scale_obj,
                      valid_color)

MAX_NAME = 16
MAX_ROOM = 20
MAX_STROKES = 5000          # 每房間物件上限，超過就丟掉最舊的
MAX_SIDS_PER_MSG = MAX_STROKES


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.name = None
        self.room = None
        self.addr = writer.get_extra_info("peername")


class Room:
    def __init__(self, name):
        self.name = name
        self.users = {}                    # name -> Client
        self.strokes = OrderedDict()       # sid -> stroke dict


class WhiteboardServer:
    def __init__(self):
        self.rooms = {}

    # ---------- 傳送 ----------
    async def send(self, c, data: bytes):
        try:
            c.writer.write(data)
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
        logging.info("連線: %s", c.addr)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = decode(line)
                    if not isinstance(msg, dict):
                        continue
                except ValueError:
                    continue
                await self.dispatch(c, msg)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError):
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

    async def dispatch(self, c, msg):
        t = msg.get("type")
        if t == "hello":
            await self.hello(c, msg)
            return
        if c.room is None:
            await self.send(c, encode(type="error", text="請先送出 hello"))
            return
        room = self.rooms[c.room]
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
            if not room.users and not room.strokes:
                del self.rooms[c.room]
        c.room = None


async def main():
    ap = argparse.ArgumentParser(description="協作白板伺服器")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    srv = WhiteboardServer()
    server = await asyncio.start_server(srv.handle, args.host, args.port, limit=1024 * 1024)
    logging.info("白板伺服器啟動於 %s:%d", args.host, args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
