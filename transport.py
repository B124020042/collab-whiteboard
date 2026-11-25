"""傳輸層：同一套 JSON 訊息可以走兩種連線。

  tcp://host:port   原本的「一行一則 JSON」TCP 協定 (區域網路)
  ws://  / wss://   WebSocket (RFC 6455)，每則 JSON 是一個 text frame (線上伺服器)

雲端平台 (例如 Render) 只轉發 HTTP / WebSocket，所以線上模式必須用 WebSocket。
這裡只用 Python 標準函式庫實作 WebSocket 的握手與 frame 編解碼。

伺服器端 (asyncio)：ws_accept_key / ws_frame / ws_read_message
客戶端 (blocking) ：open_connection(server) -> Conn (send / recv / close)
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
from pathlib import Path
from urllib.parse import urlsplit

from protocol import DEFAULT_PORT, encode

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 16 * 1024 * 1024     # 單則訊息上限 (復原整張畫布時可能較大)

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class ConnectionClosed(Exception):
    pass


# ====================== 共用：frame 編碼 ======================
def ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(payload: bytes, opcode=OP_TEXT, mask=False) -> bytes:
    """組一個完整 (FIN=1) 的 frame。客戶端送出的 frame 依規定必須加遮罩 (mask)。"""
    n = len(payload)
    head = bytearray([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload)) if n < 4096 \
            else _xor_mask(payload, key)
    return bytes(head) + payload


def _xor_mask(data: bytes, key: bytes) -> bytes:
    """大量資料的遮罩運算 (用整數 XOR 一次處理，比逐 byte 快很多)。"""
    n = len(data)
    k = int.from_bytes((key * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ k).to_bytes(n, "big")


def _unmask(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    return bytes(b ^ key[i % 4] for i, b in enumerate(data)) if len(data) < 4096 \
        else _xor_mask(data, key)


# ====================== 伺服器端 (asyncio) ======================
async def ws_read_message(reader, writer):
    """讀一則完整訊息 (會合併分段 frame、自動回應 ping)。對方關閉時回傳 None。"""
    parts, total = [], 0
    while True:
        b1, b2 = await reader.readexactly(2)
        fin, opcode = b1 & 0x80, b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await reader.readexactly(8))[0]
        if n > MAX_MESSAGE or total + n > MAX_MESSAGE:
            raise ValueError("訊息太大")          # 先檢查長度再讀資料，避免被拖住
        key = await reader.readexactly(4) if b2 & 0x80 else b""
        data = _unmask(await reader.readexactly(n), key)
        if opcode == OP_CLOSE:
            try:
                writer.write(ws_frame(data[:2], OP_CLOSE))
                await writer.drain()
            except Exception:
                pass
            return None
        if opcode == OP_PING:
            writer.write(ws_frame(data, OP_PONG))
            await writer.drain()
            continue
        if opcode == OP_PONG:
            continue
        parts.append(data)
        total += n
        if fin:
            return b"".join(parts)


async def read_http_headers(reader, first_line: bytes):
    """讀完 HTTP 請求的 header。回傳 (method, path, {header 小寫: 值})。"""
    try:
        method, path, _ = first_line.decode("latin-1").split(" ", 2)
    except ValueError:
        raise ValueError("bad request line")
    headers = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        if len(headers) > 100:
            raise ValueError("too many headers")
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return method, path, headers


# ====================== 客戶端 (blocking) ======================
def parse_server(server, default_port=DEFAULT_PORT):
    """'ws://..'、'wss://..'、'tcp://host:port'、'host:port' 或 'host' -> (scheme, host, port, path)。"""
    s = server.strip()
    if "://" not in s:
        s = "tcp://" + s
    u = urlsplit(s)
    scheme = u.scheme.lower()
    if scheme not in ("tcp", "ws", "wss"):
        raise ValueError(f"不支援的伺服器網址：{server}")
    if not u.hostname:
        raise ValueError(f"伺服器網址缺少主機名稱：{server}")
    port = u.port or {"tcp": default_port, "ws": 80, "wss": 443}[scheme]
    return scheme, u.hostname, port, (u.path or "/") + (f"?{u.query}" if u.query else "")


CONFIG = Path(__file__).resolve().parent / "config.json"


def configured_server(cli=None):
    """伺服器網址：參數 > 環境變數 WHITEBOARD_SERVER > config.json。沒有設定回傳 None。"""
    if cli and cli.strip():
        return cli.strip()
    if os.environ.get("WHITEBOARD_SERVER", "").strip():
        return os.environ["WHITEBOARD_SERVER"].strip()
    try:
        return (json.loads(CONFIG.read_text(encoding="utf-8")).get("server") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


def is_online(server):
    return bool(server) and server.strip().lower().startswith(("ws://", "wss://"))


class Conn:
    """一條已連線的通道 (TCP 或 WebSocket)，介面相同：send(obj) / recv() / close()。"""

    def __init__(self, sock, ws):
        self.sock, self.ws = sock, ws
        self.buf = b""
        self.lock = threading.Lock()

    # ---- 傳送 ----
    def send(self, **obj):
        data = encode(**obj)
        if self.ws:
            data = ws_frame(data[:-1], OP_TEXT, mask=True)
        with self.lock:
            self.sock.sendall(data)

    # ---- 接收 ----
    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionClosed()
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def recv_raw(self):
        """回傳下一則訊息的 bytes；連線結束丟出 ConnectionClosed。"""
        if not self.ws:
            while b"\n" not in self.buf:
                if len(self.buf) > MAX_MESSAGE:
                    raise ConnectionClosed("訊息太大")
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionClosed()
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            return line
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > MAX_MESSAGE:
                raise ConnectionClosed("訊息太大")
            key = self._read(4) if b2 & 0x80 else b""
            data = _unmask(self._read(n), key)
            if opcode == OP_CLOSE:
                raise ConnectionClosed()
            if opcode == OP_PING:
                with self.lock:
                    self.sock.sendall(ws_frame(data, OP_PONG, mask=True))
                continue
            if opcode == OP_PONG:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts)

    def recv(self):
        """回傳下一則 JSON 訊息 (dict)；略過格式錯誤的訊息。"""
        while True:
            raw = self.recv_raw()
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                return msg

    def settimeout(self, t):
        self.sock.settimeout(t)

    def shutdown(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        try:
            if self.ws:
                with self.lock:
                    self.sock.sendall(ws_frame(struct.pack("!H", 1000), OP_CLOSE, mask=True))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def ssl_context():
    ctx = ssl.create_default_context()
    try:                                       # 有安裝 certifi 就用它的憑證 (macOS 常缺憑證)
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


def open_connection(server, timeout=10):
    """連到伺服器，回傳 Conn。server 可以是 'host:port'、'tcp://..'、'ws://..'、'wss://..'。"""
    scheme, host, port, path = parse_server(server)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if scheme == "wss":
            try:
                sock = ssl_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError as e:
                raise OSError("SSL 憑證驗證失敗。macOS 請執行「應用程式 > Python 3.x > "
                              "Install Certificates.command」後再試一次") from e
        if scheme == "tcp":
            return Conn(sock, ws=False)
        key = base64.b64encode(os.urandom(16)).decode()
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host_hdr}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\nUser-Agent: collab-whiteboard\r\n\r\n").encode())
        conn = Conn(sock, ws=True)
        while b"\r\n\r\n" not in conn.buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("伺服器在 WebSocket 握手時關閉連線")
            conn.buf += chunk
            if len(conn.buf) > 65536:
                raise OSError("伺服器回應的 header 太長")
        head, conn.buf = conn.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            raise OSError(f"伺服器拒絕 WebSocket 連線 ({lines[0]})，網址可能不對")
        headers = {k.strip().lower(): v.strip()
                   for k, _, v in (l.partition(":") for l in lines[1:])}
        if headers.get("sec-websocket-accept") != ws_accept_key(key):
            raise OSError("WebSocket 握手驗證失敗")
        return conn
    except BaseException:
        sock.close()
        raise


def request(server, timeout=10, **obj):
    """送出一則訊息並等第一則回覆 (用於 ping / create_room / room_info)。"""
    conn = open_connection(server, timeout)
    try:
        conn.settimeout(timeout)
        conn.send(**obj)
        return conn.recv()
    except (socket.timeout, ConnectionClosed) as e:
        raise OSError(f"伺服器沒有回應 ({e or '連線中斷'})") from e
    finally:
        conn.close()
