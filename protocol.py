"""共用通訊協定：每則訊息是一行 UTF-8 JSON，以 \\n 結尾。

座標一律使用 0~1 的比例座標，讓不同視窗大小的客戶端畫面一致。
畫布上的「物件」有兩種，用同一組 sid 管理：
  筆跡 stroke: {"sid","owner","color","width","pts":[[x,y],...]}
  文字 text  : {"sid","owner","kind":"text","x","y","text","color","size"}

client -> server
  {"type":"hello",  "name":str, "room":str}
  {"type":"draw",   "sid", "color":"#rrggbb", "width":int, "pts":[[x,y],...]}  # 新增/接續筆跡
  {"type":"text",   "sid", "x", "y", "text", "color", "size"}               # 新增/修改文字
  {"type":"move",   "sids":[...], "dx", "dy"}                               # 移動物件
  {"type":"scale",  "sids":[...], "ox", "oy", "sx", "sy"}                   # 以 (ox,oy) 為基準縮放
  {"type":"delete", "sids":[...]}                                           # 刪除物件
  {"type":"cursor", "x", "y"}
  {"type":"ping"}                      # 探測伺服器 (不需先 hello)，回覆 pong
  {"type":"undo"}                      # 移除自己最後新增的物件
  {"type":"clear"}                     # 清空整個房間畫布

server -> client
  {"type":"welcome",   "name", "room", "users":[...], "strokes":[物件,...]}
  {"type":"user_join", "name"}         {"type":"user_leave", "name"}
  {"type":"draw",      "sid", "owner", "color", "width", "pts"}
  {"type":"text",      "sid", "owner", "kind", "x", "y", "text", "color", "size"}
  {"type":"move",      "sids", "dx", "dy"}
  {"type":"scale",     "sids", "ox", "oy", "sx", "sy"}
  {"type":"cursor",    "name", "x", "y"}
  {"type":"remove",    "sid"}          {"type":"clear", "by"}
  {"type":"pong",      "app", "port", "users", "rooms":{房間: 人數}, "host"}
  {"type":"error",     "text"}

自動探索 (UDP)：客戶端廣播 DISCOVER_MAGIC 到 DISCOVERY_PORT，伺服器回傳一個 pong JSON。
"""
import json
import math
import re

DEFAULT_PORT = 9100
DISCOVERY_PORT = 9101
DISCOVER_MAGIC = b"COLLAB_WHITEBOARD_DISCOVER"
MAX_PTS_PER_MSG = 500          # 單一 draw 訊息最多幾個點
MAX_TEXT = 1000                # 文字框最多字數
MIN_FONT, MAX_FONT = 6, 120
MAX_WIDTH = 60
MIN_SCALE, MAX_SCALE = 0.05, 20.0
FILE_VERSION = 2
APP_ID = "collab-whiteboard"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def local_ip():
    """取得本機在區域網路上的 IP (不會真的送出封包)。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def encode(**obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def clamp01(v):
    return min(1.0, max(0.0, float(v)))


def q(v):
    """座標統一取到小數 4 位並限制在 0~1 (伺服器與客戶端都用，結果一致)。"""
    return round(clamp01(v), 4)


def valid_color(c):
    return c if isinstance(c, str) and COLOR_RE.match(c) else "#000000"


def is_text(o):
    return o.get("kind") == "text"


# ---------- 移動 (伺服器與客戶端共用，確保結果一致) ----------
def objects_bbox(objs):
    """回傳物件的外框 (minx, miny, maxx, maxy)；文字以左上角錨點計算。"""
    xs, ys = [], []
    for o in objs:
        if is_text(o):
            xs.append(o["x"]); ys.append(o["y"])
        else:
            xs.extend(p[0] for p in o["pts"]); ys.extend(p[1] for p in o["pts"])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def clamp_move(objs, dx, dy):
    """限制位移量，讓物件不會被移出畫布 (0~1)。"""
    bb = objects_bbox(objs)
    if bb is None:
        return 0.0, 0.0
    minx, miny, maxx, maxy = bb
    dx = max(-minx, min(1.0 - maxx, float(dx)))
    dy = max(-miny, min(1.0 - maxy, float(dy)))
    return dx, dy


def move_obj(o, dx, dy):
    if is_text(o):
        o["x"] = q(o["x"] + dx)
        o["y"] = q(o["y"] + dy)
    else:
        o["pts"] = [[q(x + dx), q(y + dy)] for x, y in o["pts"]]


# ---------- 縮放 (同樣由伺服器與客戶端共用) ----------
def clamp_scale(objs, ox, oy, sx, sy, keep_ratio=False):
    """限制縮放倍率：不可翻轉、不可小於 MIN_SCALE，且縮放後不能超出畫布。"""
    ox, oy = clamp01(ox), clamp01(oy)
    sx = min(MAX_SCALE, max(MIN_SCALE, float(sx)))
    sy = min(MAX_SCALE, max(MIN_SCALE, float(sy)))
    bb = objects_bbox(objs)
    if bb is None:
        return ox, oy, 1.0, 1.0
    minx, miny, maxx, maxy = bb

    def limit(s, lo, hi, o):
        if hi > o:
            s = min(s, (1.0 - o) / (hi - o))
        if lo < o:
            s = min(s, o / (o - lo))
        return max(1e-3, s)

    sx, sy = limit(sx, minx, maxx, ox), limit(sy, miny, maxy, oy)
    if keep_ratio:
        sx = sy = min(sx, sy)
    return ox, oy, sx, sy


def scale_obj(o, ox, oy, sx, sy):
    """以 (ox, oy) 為基準縮放；筆跡粗細與文字字級依面積比例 sqrt(sx*sy) 調整。"""
    f = math.sqrt(sx * sy)
    if is_text(o):
        o["x"] = q(ox + (o["x"] - ox) * sx)
        o["y"] = q(oy + (o["y"] - oy) * sy)
        o["size"] = min(MAX_FONT, max(MIN_FONT, round(o["size"] * f)))
    else:
        o["pts"] = [[q(ox + (x - ox) * sx), q(oy + (y - oy) * sy)] for x, y in o["pts"]]
        o["width"] = min(MAX_WIDTH, max(1, round(o["width"] * f)))


# ---------- 存檔 / 讀檔 (.json，也用於剪貼簿) ----------
def dump_board(objs) -> dict:
    """把物件轉成存檔格式 (不含 sid / owner)。"""
    out = []
    for o in objs:
        if is_text(o):
            out.append({"kind": "text", "x": o["x"], "y": o["y"], "text": o["text"],
                        "color": o["color"], "size": o["size"]})
        else:
            out.append({"color": o["color"], "width": o["width"], "pts": o["pts"]})
    return {"app": APP_ID, "version": FILE_VERSION, "strokes": out}


def parse_board(data) -> list:
    """驗證並讀取存檔內容 (接受 dump_board 格式或單純的物件陣列)。

    回傳物件清單 (筆跡或文字)；格式錯誤時丟出 ValueError。
    """
    if isinstance(data, dict):
        data = data.get("strokes")
    if not isinstance(data, list):
        raise ValueError("不是白板存檔格式")
    out = []
    for s in data:
        try:
            color = valid_color(s.get("color"))
            if s.get("kind") == "text":
                text = str(s["text"])[:MAX_TEXT]
                if not text.strip():
                    continue
                out.append({"kind": "text", "x": clamp01(s["x"]), "y": clamp01(s["y"]),
                            "text": text, "color": color,
                            "size": min(MAX_FONT, max(MIN_FONT, int(s.get("size", 20))))})
                continue
            width = min(60, max(1, int(s.get("width", 3))))
            pts = [[clamp01(p[0]), clamp01(p[1])] for p in s["pts"]]
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue                     # 跳過壞掉的物件
        if pts:
            out.append({"color": color, "width": width, "pts": pts})
    if data and not out:
        raise ValueError("檔案裡沒有可讀取的內容")
    return out


def chunk_pts(pts, size=MAX_PTS_PER_MSG):
    """把很長的筆跡切成多段送出，每段多帶前一段最後一點讓線條連續。"""
    step = size - 1
    for i in range(0, max(1, len(pts) - 1), step):
        yield pts[i:i + size]
