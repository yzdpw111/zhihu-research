#!/usr/bin/env python3
"""
chrome_session.py — 持久化 CDP Chrome 会话管理（登录专用）

用途:
  用专用 profile 打开小红书，登录完成后关闭。Chrome 由 schtasks 计划任务启动，
  脱离 Reasonix 进程组，不会被连带清理，可长期保持运行。

用法:
  python chrome_session.py --start [--url https://www.xiaohongshu.com]   # 打开 Chrome（持久）
  python chrome_session.py --status                                      # 查看 CDP 端口/登录状态
  python chrome_session.py --stop                                        # 关闭专用 Chrome（按 profile 精准匹配）

示例:
  python chrome_session.py --start           # 打开 Chrome，去登录小红书
  python chrome_session.py --status          # 确认已登录
  python chrome_session.py --stop            # 登录完关闭
"""
import subprocess, sys, os, json, time, urllib.request, argparse

# stdout/stderr 强制 UTF-8：控制台默认 GBK 时，输出内容里的 emoji 会抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 路径（沿用 xhs skill 的 profile 约定）─────────────────────────
BASE = os.path.expandvars(r"%USERPROFILE%\.yzdpw_state")
PROFILE = os.path.join(BASE, "chrome-cdp")
PORT_FILE = os.path.join(BASE, ".cdp_port")
TASK_NAME = "ChromeCDP-Shared"
URLS = ["https://www.xiaohongshu.com", "https://www.zhihu.com"]

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

def find_exe():
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    return None

def check_port(port):
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return json.loads(r.read())
    except Exception:
        return None

def read_port():
    try:
        with open(PORT_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None

def write_port(port):
    os.makedirs(BASE, exist_ok=True)
    with open(PORT_FILE, "w") as f:
        f.write(str(port))

def find_available_port():
    for port in range(9222, 9300):
        if not check_port(port):
            return port
    return None

def find_profile_chromes():
    """按 --user-data-dir 精准匹配使用本 profile 的 chrome 进程（不碰主 Chrome）"""
    result = []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
            f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{PROFILE}*' }} | "
            f"Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                result.append(int(line))
    except Exception as e:
        print(f"  (warn: 查找进程失败: {e})")
    return result

def open_pages(port, urls):
    """通过 CDP 打开若干 URL（第一个复用 page target，其余新建 tab）"""
    import websocket
    try:
        # browser 级 ws 端点，用于 Target.createTarget
        version = check_port(port)
        browser_ws = websocket.create_connection(version["webSocketDebuggerUrl"], timeout=15)
        seq = [0]
        def bsend(method, params):
            seq[0] += 1
            browser_ws.send(json.dumps({"id": seq[0], "method": method, "params": params}))
            while True:
                m = json.loads(browser_ws.recv())
                if m.get("id") == seq[0]:
                    return m
        for i, u in enumerate(urls):
            r = bsend("Target.createTarget", {"url": u})
            if "error" in r:
                print(f"  (warn: 打开 {u} 失败: {r['error']})")
            else:
                print(f"  已打开: {u}")
        browser_ws.close()
    except Exception as e:
        print(f"  (warn: CDP 打开页面失败: {e})")

# ── 子命令 ────────────────────────────────────────────

def cmd_start(url):
    exe = find_exe()
    if not exe:
        print("FATAL: Chrome 未找到"); sys.exit(1)

    # 该 profile 已有 CDP 在线 → 直接复用
    port = read_port()
    if port and check_port(port):
        print(f"CDP 已在线: {check_port(port).get('Browser')} (port {port})")
        return

    # 清理旧计划任务（幂等）
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True)

    # 端口分配
    port = find_available_port()
    if not port:
        print("FATAL: 9222-9299 端口全部被占"); sys.exit(1)

    # 直接 detached 启动 Chrome → 脱离 Reasonix 进程组，持久存活。
    # 原先走 schtasks 计划任务，实测本机不再触发（任务 Last Run Time 不更新、端口不监听），
    # 改用 Popen(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)。
    os.makedirs(PROFILE, exist_ok=True)
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE}",
         "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check"],
        creationflags=0x00000008 | 0x00000200,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True)

    # 等待端口就绪
    for _ in range(30):
        time.sleep(1)
        v = check_port(port)
        if v:
            write_port(port)
            break
    else:
        print("FATAL: Chrome 30 秒内未就绪"); sys.exit(1)

    # 用 CDP 打开目标页面（小红书 + 知乎）
    targets = [url] if url else URLS
    open_pages(port, targets)

    print(f"Chrome CDP 已启动: {v.get('Browser')}")
    print(f"   端口: {port}  profile: {PROFILE}")
    print(f"   请在 Chrome 窗口里登录小红书/知乎，登录完成后运行: python chrome_session.py --stop")

def cmd_status():
    port = read_port()
    if not port or not check_port(port):
        print("CDP 未在线（profile 无运行实例）")
        return
    v = check_port(port)
    print(f"CDP 在线: {v.get('Browser')} (port {port})")
    # 列出页面
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5)
        tabs = json.loads(r.read())
        for t in [x for x in tabs if x.get("type") == "page"]:
            print(f"   - {t.get('url', '')[:100]}")
    except Exception as e:
        print(f"  (warn: 列页面失败: {e})")

def cmd_stop():
    pids = find_profile_chromes()
    if not pids:
        print("没有使用该 profile 的 Chrome 实例（可能已关闭）")
    else:
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            print(f"已关闭 Chrome PID {pid}")
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True)
    print("已清理计划任务。登录态保存在 profile 中，下次 --start 无需重新登录")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="持久化 CDP Chrome 会话管理")
    ap.add_argument("--start", action="store_true", help="启动专用 Chrome（持久）")
    ap.add_argument("--stop", action="store_true", help="关闭专用 Chrome")
    ap.add_argument("--status", action="store_true", help="查看状态")
    ap.add_argument("--url", default=None, help="自定义打开 URL（--start 时）")
    args = ap.parse_args()

    if args.start:
        cmd_start(args.url)
    elif args.stop:
        cmd_stop()
    elif args.status:
        cmd_status()
    else:
        ap.print_help()
