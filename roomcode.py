"""房間號：把開房那台電腦的 IP (與 port、房間名) 編碼成一組短代碼。

加入的人只要輸入房間號，就能解出 IP 直接連線，不需要廣播或掃描網段。

格式：  核心碼 [":" port] ["-" 房間名]      例：A7K2Q、A7K2Q-design、A7K2Q:9200
核心碼第一個字母代表 IP 種類，後面是 IP 位元用 Crockford Base32 編碼：
  A + 4 碼  192.168.x.x     (家用 / 小型網路最常見)
  B + 4 碼  172.16~31.x.x
  C + 5 碼  10.x.x.x        (學校 / 公司常見)
  D + 7 碼  其他任何 IPv4
Crockford Base32 不使用 I L O U，輸入時 I/L 會當成 1、O 當成 0，且不分大小寫。
"""
import ipaddress
import re

from protocol import DEFAULT_PORT

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_ROOM = "lobby"
# 種類 -> (網段, 主機位元數, 編碼長度)
KINDS = {
    "A": (ipaddress.ip_network("192.168.0.0/16"), 16, 4),
    "B": (ipaddress.ip_network("172.16.0.0/12"), 20, 4),
    "C": (ipaddress.ip_network("10.0.0.0/8"), 24, 5),
    "D": (ipaddress.ip_network("0.0.0.0/0"), 32, 7),
}
CODE_RE = re.compile(r"^([^:\-]+)(?::(\d{1,5}))?(?:-(.+))?$")


def _b32(n, length):
    out = []
    for _ in range(length):
        n, r = divmod(n, 32)
        out.append(ALPHABET[r])
    return "".join(reversed(out))


def _unb32(s):
    n = 0
    for ch in s:
        n = n * 32 + ALPHABET.index(ch)
    return n


def normalize_core(core):
    core = core.upper().replace("O", "0").replace("I", "1").replace("L", "1")
    bad = [ch for ch in core if ch not in ALPHABET]
    if bad:
        raise ValueError(f"房間號裡有無效的字元：{''.join(bad)}")
    return core


def encode(ip, port=DEFAULT_PORT, room=DEFAULT_ROOM):
    """IP -> 房間號。"""
    addr = ipaddress.IPv4Address(ip)
    for kind, (net, bits, length) in KINDS.items():
        if addr in net:
            code = kind + _b32(int(addr) - int(net.network_address), length)
            break
    if port != DEFAULT_PORT:
        code += f":{port}"
    if room and room != DEFAULT_ROOM:
        code += f"-{room}"
    return code


def decode(code):
    """房間號 -> (ip, port, room)。格式錯誤時丟出 ValueError。"""
    text = re.sub(r"\s+", "", str(code))
    m = CODE_RE.match(text)
    if not m:
        raise ValueError("房間號格式不對")
    core = normalize_core(m.group(1))
    kind, body = core[:1], core[1:]
    if kind not in KINDS:
        raise ValueError("房間號格式不對 (第一個字應該是 A、B、C 或 D)")
    net, bits, length = KINDS[kind]
    if len(body) != length:
        raise ValueError(f"房間號長度不對 ({kind} 開頭的房間號應該是 {length + 1} 碼)")
    n = _unb32(body)
    if n >= 2 ** bits:
        raise ValueError("房間號不正確，請再確認一次")
    ip = str(ipaddress.IPv4Address(int(net.network_address) + n))
    port = int(m.group(2)) if m.group(2) else DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise ValueError("房間號裡的 port 不正確")
    return ip, port, (m.group(3) or DEFAULT_ROOM)[:20]
