#!/usr/bin/env python3
"""一鍵啟動：自動找到區網裡已經開著的白板伺服器並連線；找不到才在本機開一個。

    python3 start.py                    # 自動尋找 / 自動開伺服器，然後開啟白板
    python3 start.py --name amy --room design
    python3 start.py --check            # 只列出找到的伺服器，不開白板
    python3 start.py --check --host 192.168.1.23   # 診斷連不到某台的原因
    python3 start.py --subnet 10.10.0.0/22         # 網段不是 /24 時指定要掃描的範圍

尋找順序：
  1. 本機 (127.0.0.1)
  2. UDP 廣播 (快，但有些 Wi-Fi 會擋)
  3. TCP 掃描本機每張網卡所在的網段 (預設 /24，廣播被擋時的備案)
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
    if not isinstance(info, dict):
        return None
    if info.get("app") == APP_ID:
        return info
    if info.get("type") == "error" and "hello" in str(info.get("text", "")):
        return {"app": APP_ID, "users": 0, "rooms": {}, "host": "?", "old": True}  # 舊版伺服器
    return None


def local_ips():
    """本機所有區網 IPv4 (電腦常有多張網卡：Wi-Fi、有線、VPN、虛擬機...)。"""
    ips = {local_ip()}
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def local_networks():
    return sorted({ipaddress.ip_network(ip + "/24", strict=False) for ip in local_ips()},
                  key=str)


# ---------- 探測 ----------
def probe_detail(ip, port, timeout=0.5):
    """連到 ip:port 送 ping。回傳 (伺服器資訊或 None, 失敗原因)。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(max(1.0, timeout * 2))
            s.sendall(encode(type="ping"))
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except ConnectionRefusedError:
        return None, "連線被拒絕：那台電腦沒有開伺服器，或 port 不對"
    except socket.timeout:
        return None, "連線逾時：通常是那台的防火牆擋住，或兩台不在同一個網路 (Wi-Fi 隔離)"
    except OSError as e:
        return None, f"無法連線 ({e})：IP 可能打錯，或兩台不在同一個網路"
    info = valid_pong(buf.split(b"\n", 1)[0]) if buf else None
    if not info:
        return None, "連得到，但那個 port 上不是白板伺服器"
    info.setdefault("port", port)
    return info, "ok"


def tcp_probe(ip, port, timeout=0.5):
    return probe_detail(ip, port, timeout)[0]


def udp_discover(udp_port=DISCOVERY_PORT, timeout=1.2, networks=None):
    """廣播探索封包，收集所有回應的伺服器：{ip: info}。"""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        targets = {"255.255.255.255", "127.0.0.1"}
        targets |= {str(n.broadcast_address) for n in networks or local_networks()}
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


MAX_SCAN_HOSTS = 4096


def tcp_scan(port, networks=None, timeout=0.4):
    """平行掃描網段內所有 IP (每個 /24 約 1 秒)。"""
    hosts = []
    for net in networks or local_networks():
        hosts.extend(str(h) for h in net.hosts())
    hosts = list(dict.fromkeys(hosts))[:MAX_SCAN_HOSTS]
    if not hosts:
        return {}
    with ThreadPoolExecutor(max_workers=128) as ex:
        results = ex.map(lambda h: (h, tcp_probe(h, port, timeout)), hosts)
    return {h: info for h, info in results if info}


def discover(port, udp_port, scan=True, networks=None, log=print):
    mine = set(local_ips()) | {"127.0.0.1"}
    norm = lambda ip: "127.0.0.1" if ip in mine else ip  # noqa: E731  自己這台統一成 127.0.0.1
    found = {}
    info = tcp_probe("127.0.0.1", port)
    if info:
        found["127.0.0.1"] = info
    log("  - UDP 廣播尋找中...")
    for ip, info in udp_discover(udp_port, networks=networks).items():
        found.setdefault(norm(ip), info)
    if not found and scan:
        nets = networks or local_networks()
        log(f"  - 沒有回應，改用 TCP 掃描 {', '.join(map(str, nets)) or '(無)'} ...")
        for ip, info in tcp_scan(port, nets).items():
            found.setdefault(norm(ip), info)
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
    ap.add_argument("--host", action="append", default=[],
                    help="直接連這台 (可重複)；配合 --check 可診斷連不到的原因")
    ap.add_argument("--subnet", action="append", default=[],
                    help="要掃描的網段，例如 10.10.0.0/22 (可重複)")
    ap.add_argument("--check", action="store_true", help="只列出找到的伺服器，不開白板")
    args = ap.parse_args()

    try:
        networks = [ipaddress.ip_network(n, strict=False) for n in args.subnet] or None
    except ValueError as e:
        sys.exit(f"--subnet 格式錯誤: {e}")
    print(f"本機 IP: {', '.join(local_ips()) or '127.0.0.1'}")

    if args.host:                                   # 指定 IP：直接連，連不到就說明原因
        found = {}
        for h in args.host:
            info, why = probe_detail(h, args.port, timeout=2)
            if info:
                found[h] = info
            else:
                print(f"  {h}:{args.port} -> {why}")
        if not found:
            sys.exit(1)
    else:
        print("尋找白板伺服器...")
        found = discover(args.port, args.udp_port, scan=not args.no_scan, networks=networks)
        if not found and not args.check:
            time.sleep(random.uniform(0.3, 1.5))    # 錯開時間，避免兩台同時各開一個
            found = discover(args.port, args.udp_port, scan=False, networks=networks,
                             log=lambda *a: None)

    for ip, info in sorted(found.items()):
        if info.get("old"):
            print(f"  找到 {ip}:{info.get('port')}  (舊版伺服器)")
            continue
        rooms = ", ".join(f"{r}({n})" for r, n in info.get("rooms", {}).items()) or "無"
        print(f"  找到 {ip}:{info.get('port')}  主機 {info.get('host')}  "
              f"線上 {info.get('users', 0)} 人  房間: {rooms}")
    if not found:
        print("  沒有找到任何伺服器。如果另一台確定開著伺服器，請：\n"
              "    1. 確認那台的伺服器是 git pull 之後才開的 (舊版不會回應自動探索)\n"
              "    2. 在這台執行  python3 start.py --check --host <那台的IP>  看是哪裡不通")
    if args.check:
        return

    server_proc = None
    if found:
        host, info = pick(found)
        args.port = int(info.get("port", args.port))
        print(f"連線到 {host}:{args.port}")
        if info.get("old"):
            print("  注意：那台的伺服器是舊版，文字框、縮放、移動等新功能不會同步。\n"
                  "  請在那台執行 git pull，關掉伺服器後重開。")
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
