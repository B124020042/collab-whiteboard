#!/usr/bin/env python3
"""一鍵啟動：自動找到區網裡已經開著的白板伺服器並連線；找不到才在本機開一個。

    python3 start.py                    # 自動尋找 / 自動開伺服器，然後開啟白板
    python3 start.py --name amy --room design
    python3 start.py --check            # 只列出找到的伺服器，不開白板

尋找順序：
  1. 本機 (127.0.0.1)
  2. UDP 廣播 (快，但有些 Wi-Fi 會擋)
  3. TCP 掃描同網段 x.x.x.1 ~ x.x.x.254 (廣播被擋時的備案)
找不到 -> 隨機等一下再找一次 (避免兩台同時啟動各開一個伺服器) -> 在本機開伺服器。

自己開的伺服器：關閉白板視窗時，如果還有其他人在使用，伺服器會繼續執行，
等所有人都離開才自動關閉 (也可以按 Ctrl+C 強制關閉)。
"""
import argparse
import ipaddress
import json
import random
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from protocol import APP_ID, DEFAULT_PORT, DISCOVER_MAGIC, DISCOVERY_PORT, encode, local_ip

HERE = Path(__file__).resolve().parent


def valid_pong(data):
    try:
        info = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return info if isinstance(info, dict) and info.get("app") == APP_ID else None


# ---------- 探測 ----------
def tcp_probe(ip, port, timeout=0.5):
    """連到 ip:port 送 ping，確認對方真的是白板伺服器。回傳 pong 內容或 None。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout * 2)
            s.sendall(encode(type="ping"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            return valid_pong(buf.split(b"\n", 1)[0])
    except OSError:
        return None


def udp_discover(udp_port=DISCOVERY_PORT, timeout=1.2):
    """廣播探索封包，收集所有回應的伺服器：{ip: info}。"""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        targets = {"255.255.255.255", "127.0.0.1"}
        ip = local_ip()
        if ip != "127.0.0.1":
            targets.add(str(ipaddress.ip_network(ip + "/24", strict=False).broadcast_address))
        for t in targets:
            try:
                s.sendto(DISCOVER_MAGIC, (t, udp_port))
            except OSError:
                pass
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                data, addr = s.recvfrom(65536)
            except (socket.timeout, OSError):
                break
            info = valid_pong(data)
            if info:
                found[addr[0]] = info
    finally:
        s.close()
    return found


def tcp_scan(port, timeout=0.4):
    """掃描同一個 /24 網段 (平行連線，約 1 秒)。"""
    ip = local_ip()
    if ip == "127.0.0.1":
        return {}
    net = ipaddress.ip_network(ip + "/24", strict=False)
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=64) as ex:
        results = ex.map(lambda h: (h, tcp_probe(h, port, timeout)), hosts)
    return {h: info for h, info in results if info}


def discover(port, udp_port, scan=True, log=print):
    me = local_ip()
    found = {}
    info = tcp_probe("127.0.0.1", port)
    if info:
        found["127.0.0.1"] = info
    log("  - UDP 廣播尋找中...")
    for ip, info in udp_discover(udp_port).items():
        found.setdefault("127.0.0.1" if ip in (me, "127.0.0.1") else ip, info)
    if not found and scan:
        log("  - 沒有回應，改用 TCP 掃描同網段 (約 1~3 秒)...")
        for ip, info in tcp_scan(port).items():
            found.setdefault("127.0.0.1" if ip == me else ip, info)
    return found


def pick(found):
    """多台伺服器時：選人最多的那台；一樣多就選 IP 最小的 (每台電腦選的結果一致)。"""
    def key(item):
        ip, info = item
        return (-int(info.get("users", 0)), ipaddress.ip_address(ip))
    return sorted(found.items(), key=key)[0]


# ---------- 啟動 ----------
def start_server(port, udp_port):
    log_path = HERE / "server.log"
    log = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--port", str(port),
                             "--udp-port", str(udp_port)],
                            cwd=HERE, stdout=log, stderr=log)
    for _ in range(50):
        if proc.poll() is not None:
            raise RuntimeError(f"伺服器啟動失敗，請查看 {log_path}")
        if tcp_probe("127.0.0.1", port):
            return proc
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"伺服器沒有回應，請查看 {log_path}")


def wait_until_empty(proc, port):
    """自己開的伺服器：還有人在用就繼續開著，所有人離開後才關閉。"""
    try:
        last = None
        time.sleep(1)                               # 等伺服器處理完自己這個視窗的離線
        while proc.poll() is None:
            info = tcp_probe("127.0.0.1", port, timeout=1)
            users = info.get("users", 0) if info else 0
            if users == 0:
                break
            if users != last:
                print(f"還有 {users} 人在使用這台的白板，伺服器繼續執行... (Ctrl+C 強制關閉)")
                last = users
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("伺服器已關閉")


def main():
    ap = argparse.ArgumentParser(description="自動連線 / 自動開伺服器的白板啟動器")
    ap.add_argument("--name", help="暱稱 (未指定會在視窗跳出詢問)")
    ap.add_argument("--room", default="lobby")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--udp-port", type=int, default=DISCOVERY_PORT)
    ap.add_argument("--no-scan", action="store_true", help="不做 TCP 網段掃描")
    ap.add_argument("--check", action="store_true", help="只列出找到的伺服器，不開白板")
    args = ap.parse_args()

    print(f"本機 IP: {local_ip()}，尋找白板伺服器...")
    found = discover(args.port, args.udp_port, scan=not args.no_scan)
    if not found and not args.check:
        time.sleep(random.uniform(0.3, 1.5))        # 錯開時間，避免兩台同時各開一個
        found = discover(args.port, args.udp_port, scan=False, log=lambda *a: None)

    for ip, info in sorted(found.items()):
        rooms = ", ".join(f"{r}({n})" for r, n in info.get("rooms", {}).items()) or "無"
        print(f"  找到 {ip}:{info.get('port')}  主機 {info.get('host')}  "
              f"線上 {info.get('users', 0)} 人  房間: {rooms}")
    if args.check:
        if not found:
            print("  沒有找到任何伺服器 (執行 start.py 會在本機開一個)")
        return

    server_proc = None
    if found:
        host, info = pick(found)
        args.port = int(info.get("port", args.port))
        print(f"連線到 {host}:{args.port}")
    else:
        print("沒有找到伺服器，在本機開一個...")
        try:
            server_proc = start_server(args.port, args.udp_port)
        except RuntimeError as e:
            print(e)
            sys.exit(1)
        host = "127.0.0.1"
        print(f"伺服器已啟動！其他電腦執行 python3 start.py 會自動連到這台 ({local_ip()})")

    cmd = [sys.executable, str(HERE / "client.py"), "--host", host, "--port", str(args.port),
           "--room", args.room]
    if args.name:
        cmd += ["--name", args.name]
    try:
        subprocess.run(cmd, cwd=HERE)
    except KeyboardInterrupt:
        pass
    if server_proc:
        wait_until_empty(server_proc, args.port)


if __name__ == "__main__":
    main()
