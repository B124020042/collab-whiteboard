#!/usr/bin/env python3
"""白板啟動器：建立房間 / 輸入房間號加入。

    python3 start.py                     # 跳出小視窗：建立房間，或輸入房間號加入
    (設定了線上伺服器時 (config.json / --server / 環境變數 WHITEBOARD_SERVER)，
     預設走線上：不同網路、不同地方的人都能加入；加 --lan 則改用區網)
    python3 start.py create              # 建立房間 (在本機開伺服器)，顯示房間號
    python3 start.py join A008Q          # 輸入房間號加入
    python3 start.py join A008Q --check  # 只檢查連不連得到 (連不到會說明原因)
    python3 start.py auto                # 舊方式：自動尋找區網裡的伺服器，找不到才自己開
    (create / join / auto 都可加 --name 暱稱；create 可加 --room 房間名)

房間號是把開房那台的 IP 編碼而成 (見 roomcode.py)，加入的人解碼後直接連線，
不需要廣播或掃描，所以在會擋廣播的學校 / 公司網路也能用。

自己開的伺服器：關閉白板視窗時，如果還有其他人在使用，伺服器會繼續執行，
等所有人都離開才自動關閉 (也可以按 Ctrl+C 強制關閉)。

auto 模式的尋找順序：本機 -> UDP 廣播 -> TCP 掃描本機每張網卡所在的網段 (預設 /24)；
找不到 -> 隨機等一下再找一次 (避免兩台同時啟動各開一個伺服器) -> 在本機開伺服器。
"""
import argparse
import getpass
import ipaddress
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import roomcode
import transport
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


def run_client(server, room, name=None, code=None):
    cmd = [sys.executable, str(HERE / "client.py"), "--server", server, "--room", room]
    if name:
        cmd += ["--name", name]
    if code:
        cmd += ["--code", code]
    try:
        subprocess.run(cmd, cwd=HERE)
    except KeyboardInterrupt:
        pass


# ---------- 線上伺服器 ----------
CONFIG = HERE / "config.json"
ONLINE_CODE_LEN = 5


def configured_server(cli=None):
    """線上伺服器網址：--server > 環境變數 WHITEBOARD_SERVER > config.json。"""
    if cli:
        return cli
    if os.environ.get("WHITEBOARD_SERVER"):
        return os.environ["WHITEBOARD_SERVER"]
    try:
        return (json.loads(CONFIG.read_text(encoding="utf-8")).get("server") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


def online_error(e):
    msg = str(e) or e.__class__.__name__
    if isinstance(e, socket.gaierror):
        msg = "找不到伺服器網址 (網址打錯，或這台電腦沒有網路)"
    elif isinstance(e, (socket.timeout, TimeoutError)):
        msg = "連線逾時 (伺服器可能還在喚醒，請再試一次)"
    return RuntimeError(f"連不上線上伺服器：{msg}")


def normalize_online_code(code):
    c = roomcode.normalize_core(re.sub(r"[\s-]+", "", str(code)))
    if len(c) != ONLINE_CODE_LEN:
        raise ValueError(f"房間號應該是 {ONLINE_CODE_LEN} 碼")
    return c


def online_create(server):
    """請線上伺服器開一個新房間，回傳房間號。"""
    try:
        reply = transport.request(server, timeout=90, type="create_room")
    except (OSError, ValueError) as e:
        raise online_error(e)
    if reply.get("type") != "room_created":
        raise RuntimeError("伺服器版本太舊，不支援建立房間 (請重新部署最新版)")
    return reply["code"]


def online_join(server, code):
    """確認線上房間存在，回傳整理過的房間號。房間號錯誤丟 ValueError，其他丟 RuntimeError。"""
    code = normalize_online_code(code)
    try:
        reply = transport.request(server, timeout=90, type="room_info", room=code)
    except (OSError, ValueError) as e:
        raise online_error(e)
    if reply.get("type") != "room_info":
        raise RuntimeError("伺服器版本太舊，不支援房間號 (請重新部署最新版)")
    if not reply.get("exists"):
        raise RuntimeError(f"找不到房間 {code}，請確認房間號 (伺服器重啟過的話，請房主重新建立)")
    return code


# ---------- 建立 / 加入房間 ----------
def create_room(port=DEFAULT_PORT, udp_port=DISCOVERY_PORT, room=roomcode.DEFAULT_ROOM):
    """在本機開伺服器 (已經開著就沿用)。回傳 (自己開的伺服器 process 或 None, 房間號)。"""
    info = tcp_probe("127.0.0.1", port)
    proc = None
    if info is None:
        proc = start_server(port, udp_port)
    elif info.get("old"):
        raise RuntimeError("這台已經開著舊版的伺服器，請先關掉它 (Ctrl+C) 再建立房間")
    return proc, roomcode.encode(local_ip(), port, room)


def join_target(code):
    """解出房間號並確認連得到。回傳 (ip, port, room, 伺服器資訊)；
    房間號錯誤丟出 ValueError，連不到丟出 RuntimeError (附原因)。"""
    ip, port, room = roomcode.decode(code)
    info, why = probe_detail(ip, port, timeout=2)
    if not info and ip in local_ips():          # 房間就開在自己這台
        info, why = probe_detail("127.0.0.1", port, timeout=2)
    if not info:
        raise RuntimeError(f"連不到房間 {roomcode.encode(ip, port, room)} ({ip})：{why}")
    return ip, port, room, info


def print_code(code, port, room):
    line = "=" * 44
    print(f"\n{line}\n  房間已建立！ 房間號：  {code}\n"
          f"  其他人執行  python3 start.py  並輸入房間號即可加入\n"
          f"  (或  python3 start.py join {code})\n{line}")
    others = [ip for ip in local_ips() if ip != local_ip()]
    if others:                                  # 多張網卡：列出其他網路用的房間號
        print("  如果對方連不到，可以改用其他網卡的房間號：")
        for ip in others:
            print(f"    {roomcode.encode(ip, port, room)}   ({ip})")
    print()


def cmd_create(name, room, port, udp_port, server=None):
    if server:                                  # 線上
        try:
            print(f"連線到線上伺服器 {server} ... (伺服器休眠時第一次約需 1 分鐘)")
            code = online_create(server)
        except RuntimeError as e:
            sys.exit(str(e))
        line = "=" * 44
        print(f"\n{line}\n  房間已建立！ 房間號：  {code}\n"
              f"  其他人執行  python3 start.py  並輸入房間號即可加入\n"
              f"  (不同網路、不同地方都可以)\n{line}\n")
        run_client(server, code, name, code)
        return
    try:
        proc, code = create_room(port, udp_port, room)
    except RuntimeError as e:
        sys.exit(str(e))
    print_code(code, port, room)
    run_client(f"127.0.0.1:{port}", room, name, code)
    if proc:
        wait_until_empty(proc, port)


def cmd_join(code, name, check=False, server=None):
    if server:                                  # 線上
        try:
            code = online_join(server, code)
        except (ValueError, RuntimeError) as e:
            sys.exit(str(e))
        print(f"房間 {code} 存在 ({server})")
        if not check:
            run_client(server, code, name, code)
        return
    try:
        ip, port, room, info = join_target(code)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    norm = roomcode.encode(ip, port, room)
    if info.get("old"):
        print("注意：那台的伺服器是舊版，文字框、縮放等新功能不會同步。請那台 git pull 後重開。")
    print(f"房間 {norm} -> {ip}:{port}  線上 {info.get('users', '?')} 人")
    if not check:
        run_client(f"{ip}:{port}", room, name, norm)


def launcher_gui(server=None):
    """小視窗：建立房間，或輸入房間號加入。回傳使用者的選擇 (關閉視窗則為空)。

    server 有值時可選「線上」或「區網」；連線動作在背景執行緒進行，視窗不會卡住。
    """
    import threading
    import tkinter as tk
    root = tk.Tk()
    root.title("協作白板")
    root.resizable(False, False)
    result = {}
    f = tk.Frame(root, padx=24, pady=18)
    f.pack()
    tk.Label(f, text="協作白板", font=("TkDefaultFont", 18, "bold")).grid(
        row=0, column=0, columnspan=3, pady=(0, 12))
    tk.Label(f, text="暱稱").grid(row=1, column=0, sticky="e", padx=(0, 6))
    try:
        default_name = getpass.getuser()
    except Exception:
        default_name = "guest"
    name_var = tk.StringVar(value=default_name)
    tk.Entry(f, textvariable=name_var, width=18).grid(row=1, column=1, columnspan=2, sticky="we")
    mode_var = tk.StringVar(value="online" if server else "lan")
    modes = tk.Frame(f)
    modes.grid(row=2, column=0, columnspan=3, pady=(10, 0))
    if server:
        host = transport.parse_server(server)[1]
        tk.Radiobutton(modes, text=f"線上 ({host})", variable=mode_var,
                       value="online").pack(side=tk.LEFT)
        tk.Radiobutton(modes, text="同一個區網", variable=mode_var, value="lan").pack(side=tk.LEFT)
    else:
        tk.Label(modes, text="模式：同一個區網 (尚未設定線上伺服器，見 README)",
                 fg="#888").pack()
    status = tk.Label(f, text="", fg="#c62828", wraplength=320, justify="left")
    buttons = []

    def name():
        return name_var.get().strip() or "guest"

    def busy(text):
        status.config(text=text, fg="#555")
        for b in buttons:
            b.config(state=tk.DISABLED)

    def run_bg(work, done):
        """在背景執行 work()，完成後在主執行緒呼叫 done(結果, 例外)。"""
        box = {}

        def target():
            try:
                box["value"] = work()
            except Exception as e:          # noqa: BLE001  交給 done 顯示
                box["error"] = e
        th = threading.Thread(target=target, daemon=True)
        th.start()

        def check():
            if th.is_alive():
                root.after(100, check)
                return
            for b in buttons:
                b.config(state=tk.NORMAL)
            done(box.get("value"), box.get("error"))
        root.after(100, check)

    def fail(e):
        status.config(text=str(e), fg="#c62828")

    def on_create():
        if mode_var.get() == "lan":
            result.update(action="create", name=name())
            root.destroy()
            return
        busy("建立房間中… (線上伺服器休眠時第一次約需 1 分鐘)")

        def done(code, err):
            if err:
                return fail(err)
            result.update(action="online", code=code, name=name())
            root.destroy()
        run_bg(lambda: online_create(server), done)

    def on_join(event=None):
        code = code_var.get().strip()
        if not code:
            status.config(text="請輸入房間號", fg="#c62828")
            return
        online = mode_var.get() == "online"
        busy("連線中…" + (" (線上伺服器休眠時第一次約需 1 分鐘)" if online else ""))

        def done(value, err):
            if err:
                return fail(err)
            if online:
                result.update(action="online", code=value, name=name())
            else:
                result.update(action="join", target=value, name=name())
            root.destroy()
        run_bg((lambda: online_join(server, code)) if online else (lambda: join_target(code)),
               done)

    b = tk.Button(f, text="建立房間", width=24, command=on_create)
    b.grid(row=3, column=0, columnspan=3, pady=(14, 6))
    buttons.append(b)
    tk.Label(f, text="──────  或  ──────", fg="#999").grid(row=4, column=0, columnspan=3)
    tk.Label(f, text="房間號").grid(row=5, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
    code_var = tk.StringVar()
    entry = tk.Entry(f, textvariable=code_var, width=12, font=("TkFixedFont", 14))
    entry.grid(row=5, column=1, pady=(6, 0))
    entry.bind("<Return>", on_join)
    b = tk.Button(f, text="加入房間", command=on_join)
    b.grid(row=5, column=2, padx=(6, 0), pady=(6, 0))
    buttons.append(b)
    status.grid(row=6, column=0, columnspan=3, pady=(10, 0), sticky="w")
    entry.focus_set()
    root.mainloop()
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("--server", "--lan"):   # 啟動視窗
        ap = argparse.ArgumentParser(prog="start.py")
        ap.add_argument("--server", help="線上伺服器網址，例如 wss://xxx.onrender.com")
        ap.add_argument("--lan", action="store_true", help="不使用線上伺服器")
        a = ap.parse_args(argv)
        server = None if a.lan else configured_server(a.server)
        r = launcher_gui(server)
        if r.get("action") == "online":
            run_client(server, r["code"], r["name"], r["code"])
        elif r.get("action") == "create":
            cmd_create(r["name"], roomcode.DEFAULT_ROOM, DEFAULT_PORT, DISCOVERY_PORT)
        elif r.get("action") == "join":
            ip, port, room, info = r["target"]
            run_client(f"{ip}:{port}", room, r["name"], roomcode.encode(ip, port, room))
        return
    if argv[0] in ("create", "join"):
        ap = argparse.ArgumentParser(prog="start.py " + argv[0])
        ap.add_argument("--name", help="暱稱 (未指定會在視窗跳出詢問)")
        ap.add_argument("--server", help="線上伺服器網址 (預設讀 config.json)")
        ap.add_argument("--lan", action="store_true", help="不使用線上伺服器，改用區網")
        if argv[0] == "create":
            ap.add_argument("--room", default=roomcode.DEFAULT_ROOM, help="房間名 (預設 lobby)")
            ap.add_argument("--port", type=int, default=DEFAULT_PORT)
            ap.add_argument("--udp-port", type=int, default=DISCOVERY_PORT)
            a = ap.parse_args(argv[1:])
            cmd_create(a.name, a.room, a.port, a.udp_port,
                       None if a.lan else configured_server(a.server))
        else:
            ap.add_argument("code", help="房間號，例如 A008Q")
            ap.add_argument("--check", action="store_true", help="只檢查連不連得到，不開白板")
            a = ap.parse_args(argv[1:])
            cmd_join(a.code, a.name, a.check, None if a.lan else configured_server(a.server))
        return
    if argv[0] == "auto":
        argv = argv[1:]
    auto_main(argv)                             # 舊用法 (start.py --check 等) 仍可使用


def auto_main(argv):
    ap = argparse.ArgumentParser(prog="start.py auto",
                                 description="自動尋找區網裡的伺服器，找不到才自己開")
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
    args = ap.parse_args(argv)

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

    run_client(f"{host}:{args.port}", args.room, args.name,
               roomcode.encode(local_ip() if host == "127.0.0.1" else host, args.port, args.room))
    if server_proc:
        wait_until_empty(server_proc, args.port)


if __name__ == "__main__":
    main()
