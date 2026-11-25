"""共用通訊協定：每則訊息是一行 UTF-8 JSON，以 \\n 結尾。

座標一律使用 0~1 的比例座標，讓不同視窗大小的客戶端畫面一致。

client -> server
  {"type":"hello",  "name":str, "room":str}
  {"type":"draw",   "sid":str, "color":"#rrggbb", "width":int, "pts":[[x,y],...]}
  {"type":"cursor", "x":float, "y":float}
  {"type":"undo"}                      # 復原自己最後一筆
  {"type":"clear"}                     # 清空整個房間畫布

server -> client
  {"type":"welcome",   "name", "room", "users":[...], "strokes":[stroke,...]}
  {"type":"user_join", "name"}         {"type":"user_leave", "name"}
  {"type":"draw",      "sid", "owner", "color", "width", "pts"}
  {"type":"cursor",    "name", "x", "y"}
  {"type":"remove",    "sid"}          {"type":"clear", "by"}
  {"type":"error",     "text"}
"""
import json
import re

DEFAULT_PORT = 9100
MAX_PTS_PER_MSG = 500          # 單一 draw 訊息最多幾個點
FILE_VERSION = 1
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def encode(**obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


# ---------- 存檔 / 讀檔 (.json) ----------
def dump_board(strokes) -> dict:
    """把筆跡轉成存檔格式。"""
    return {"app": "collab-whiteboard", "version": FILE_VERSION,
            "strokes": [{"color": s["color"], "width": s["width"], "pts": s["pts"]}
                        for s in strokes]}


def parse_board(data) -> list:
    """驗證並讀取存檔內容 (接受 dump_board 格式或單純的筆跡陣列)。

    回傳 [{"color", "width", "pts"}, ...]；格式錯誤時丟出 ValueError。
    """
    if isinstance(data, dict):
        data = data.get("strokes")
    if not isinstance(data, list):
        raise ValueError("不是白板存檔格式")
    out = []
    for s in data:
        try:
            color = s["color"] if _COLOR_RE.match(str(s.get("color", ""))) else "#000000"
            width = min(60, max(1, int(s.get("width", 3))))
            pts = [[min(1.0, max(0.0, float(p[0]))), min(1.0, max(0.0, float(p[1])))]
                   for p in s["pts"]]
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue                     # 跳過壞掉的筆跡
        if pts:
            out.append({"color": color, "width": width, "pts": pts})
    if data and not out:
        raise ValueError("檔案裡沒有可讀取的筆跡")
    return out


def chunk_pts(pts, size=MAX_PTS_PER_MSG):
    """把很長的筆跡切成多段送出，每段多帶前一段最後一點讓線條連續。"""
    step = size - 1
    for i in range(0, max(1, len(pts) - 1), step):
        yield pts[i:i + size]
