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

DEFAULT_PORT = 9100


def encode(**obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)
