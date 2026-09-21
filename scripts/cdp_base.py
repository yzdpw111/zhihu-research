#!/usr/bin/env python3
"""
cdp_base.py — CDP 客户端 + Chrome 裸启动 + 人类行为模拟

职责:
  CdpClient      CDP WebSocket 消息循环（send/evaluate/navigate/wait_for）
  human_scroll   类人滚动（3-6 轮随机、15% 回滚、停顿 0.5-4s）
  human_mouse    40% 概率随机鼠标移动
  random_delay   随机等待
  RateLimiter    相邻请求最小间隔
  ensure_cdp     优先连接现有 CDP，否则 schtasks 裸启动 Chrome（持久 profile）
  pick_port      在 9222-9299 中挑空闲端口
"""
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request

import websocket

from config import get


def setup_stdout():
    """stdout/stderr 强制 UTF-8（CLI 入口必须调用一次）。

    控制台默认 GBK 时，结果里的 emoji 变体符（\\ufe0f、✅ 等）会让
    json.dumps 的输出抛 UnicodeEncodeError —— 抓取全部成功，用户却
    一个字都拿不到。errors="replace" 只作极端兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def write_log(data, script_name):
    """完整结果落盘到 <logs.dir>/<脚本>-<时间戳>.json，返回绝对路径。

    宿主对脚本 stdout 有大小上限，结果一多就会被截断；Agent 需要全文时
    直接读这个文件。必须在 stdout 输出之前调用：这样 stdout 崩了也不丢结果。
    """
    import datetime
    logs_dir = get("logs.dir")
    os.makedirs(logs_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(logs_dir, f"{script_name}-{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


# ── 常量（从 config 读，沿用 xhs skill 的 profile 约定）──────────
BASE = get("state.dir")
PROFILE = os.path.join(BASE, "chrome-cdp")
PORT_FILE = os.path.join(BASE, ".cdp_port")
TASK_NAME = "ChromeCDP-Shared"

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


class CdpError(Exception):
    pass


def port_busy(port):
    """探测端口是否已有 CDP 服务。

    先用 socket 做 TCP 预探测：urlopen 连未监听端口会等满 timeout，
    扫描 9222-9299 因此累计 ~200s（Chrome 离线时 ensure_cdp 卡几分钟）；
    TCP connect 对未监听端口是立刻返回的。
    """
    import socket
    s = socket.socket()
    try:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return False
    finally:
        s.close()
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return r.status == 200
    except Exception:
        return False


def port_bindable(port):
    """端口是否可被绑定（被非 CDP 进程占用时返回 False）"""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port():
    """在 9222-9299 中挑选第一个空闲且可绑定的端口"""
    for port in range(9222, 9300):
        if not port_busy(port) and port_bindable(port):
            return port
    raise CdpError("9222-9299 端口全部被占")


def find_chrome():
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    raise CdpError("Chrome 未找到")


def ensure_cdp():
    """返回可用端口。优先复用在线 CDP，否则直接 detached 启动 Chrome。"""
    # 1) 端口文件里记录的端口若在线则复用
    try:
        with open(PORT_FILE) as f:
            saved = int(f.read().strip())
        if port_busy(saved):
            return saved
    except Exception:
        pass
    # 2) 9222-9299 里已有的 CDP 直接复用（用户手动开的）
    for port in range(9222, 9300):
        if port_busy(port):
            return port
    # 3) 启动新 Chrome。必须脱离调用方进程组，否则脚本退出时 Chrome 会被一起收走。
    #    原先用 schtasks 计划任务达到同样目的，但实测本机不再触发
    #    （任务 Last Run Time 不更新、端口不监听），改为直接 Popen(DETACHED_PROCESS)。
    port = pick_port()
    exe = find_chrome()
    os.makedirs(PROFILE, exist_ok=True)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE}",
         "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check"],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True)
    deadline = time.time() + get("timeout.chrome_start")
    while time.time() < deadline:
        if port_busy(port):
            try:
                with open(PORT_FILE, "w") as f:
                    f.write(str(port))
            except Exception:
                pass
            return port
        time.sleep(1)
    raise CdpError(f"Chrome {get('timeout.chrome_start')} 秒内未就绪")


class CdpClient:
    """CDP WebSocket 客户端：同步消息循环（id 匹配 + 超时）"""

    def __init__(self, port, timeout=None, ws_url=None):
        timeout = timeout if timeout is not None else get("timeout.cdp")
        if ws_url:
            target_ws_url = ws_url
        else:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5)
            tabs = json.loads(r.read())
            target = next((t for t in tabs if t.get("type") == "page"), None)
            if not target:
                raise CdpError("没有可用的 page target")
            target_ws_url = target["webSocketDebuggerUrl"]
        # 显式声明 Origin，配合 --remote-allow-origins=http://localhost：
        # Chrome 放行本客户端，但拒绝任意页面 JS 发起的跨源连接
        try:
            self.ws = websocket.create_connection(
                target_ws_url, timeout=timeout, origin="http://localhost")
        except (websocket.WebSocketException, OSError) as e:
            raise CdpError(f"WebSocket 连接失败: {e}") from e
        self.port = port
        self.target_id = None          # create_page 填充，close_page 据此关闭 tab
        self._id = 0
        self._closed = False

    def send(self, method, params=None, timeout=None):
        timeout = timeout if timeout is not None else get("timeout.cdp")
        if self._closed:
            raise CdpError("WebSocket 已关闭")
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.close()
                raise CdpError(f"{method}: 等待响应超时（{timeout}s）")
            try:
                self.ws.settimeout(remaining)
                msg = json.loads(self.ws.recv())
            except (websocket.WebSocketTimeoutException, TimeoutError):
                self.close()
                raise CdpError(f"{method}: 等待响应超时（{timeout}s）") from None
            except (websocket.WebSocketException, OSError) as e:
                raise CdpError(f"WebSocket 通信失败: {e}") from e
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def evaluate(self, expr, await_promise=False, return_by_value=True):
        r = self.send("Runtime.evaluate", {
            "expression": expr, "awaitPromise": await_promise, "returnByValue": return_by_value})
        if "exceptionDetails" in r:
            raise CdpError(f"eval 异常: {expr[:80]}")
        return r.get("result", {}).get("value")

    def navigate(self, url):
        self.send("Page.navigate", {"url": url})

    def get_body_text(self):
        return self.evaluate("document.body ? document.body.textContent : ''") or ""

    def wait_for(self, expr, timeout=15, interval=0.5):
        """轮询直到表达式为真，超时返回 False"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(expr):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False


# ── tab 生命周期（create_page 必须配对 close_page）──────────

_BLANK_URL_PREFIXES = ("about:blank", "chrome://newtab", "chrome://new-tab-page")
_TAB_LOCK = threading.RLock()   # 可重入：create_page 持锁期间仍会调用 _enforce_tab_limit
_ACTIVE_TARGETS = set()         # 本进程正在使用的 tab，回收时必须跳过


def _register_target(target_id):
    with _TAB_LOCK:
        _ACTIVE_TARGETS.add(target_id)


def _unregister_target(target_id):
    with _TAB_LOCK:
        _ACTIVE_TARGETS.discard(target_id)


def is_blank_target(url):
    """空 tab 判定（about:blank / 新标签页）"""
    u = (url or "").strip().lower()
    return any(u.startswith(p) for p in _BLANK_URL_PREFIXES)


def list_page_targets(port):
    """当前 Chrome 的 page target 列表（含 id/url），查询失败返回 []"""
    try:
        tabs = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read())
        return [t for t in tabs if t.get("type") == "page" and t.get("id")]
    except Exception:
        return []


def close_target(port, target_id):
    """按 targetId 关闭 tab（DevTools HTTP /json/close/<id>）。失败返回 False，不抛。"""
    if not port or not target_id:
        return False
    url = f"http://127.0.0.1:{port}/json/close/{target_id}"
    for method in ("GET", "PUT"):      # 各 Chrome 版本对该端点的方法要求不一致
        try:
            urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5).read()
            return True
        except Exception:
            continue
    return False


def close_page(client):
    """关闭 create_page 开的 tab 并断开 ws —— 与 create_page 配对使用。

    关 tab 失败也必须断开 ws，且任何清理异常都不外抛（不污染抓取结果）。
    若它是最后一个 tab：先建一个 about:blank 占位再关它（直接关会让 Chrome 整体
    退出；不关又会在浏览器里留下"上次的搜索结果/详情页"）；占位建不出来时则保留它。
    """
    if client is None:
        return False
    _unregister_target(getattr(client, "target_id", None))
    port = getattr(client, "port", None)
    target_id = getattr(client, "target_id", None)
    ok = False
    if target_id:
        with _TAB_LOCK:      # 检查与关闭同锁：否则两个线程会双双放行，把 tab 关空
            tabs = list_page_targets(port)
            if len(tabs) > 1:
                ok = close_target(port, target_id)
            elif tabs and tabs[0]["id"] == target_id and _create_blank_page(port):
                ok = close_target(port, target_id)
    try:
        client.close()
    except Exception:
        pass
    return ok


_MULTI_SUFFIX = ("com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
                 "co.jp", "co.kr", "com.hk")


def _browser_ws(port):
    """连 browser 级 CDP WebSocket（用于 Target.* 操作）"""
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
    version = json.loads(r.read())
    return websocket.create_connection(version["webSocketDebuggerUrl"], timeout=15,
                                       origin="http://localhost")


def _create_blank_page(port):
    """新建一个 about:blank tab（占位用）。成功 True，失败 False，不抛。"""
    try:
        bws = _browser_ws(port)
    except Exception:
        return False
    try:
        bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                             "params": {"url": "about:blank"}}))
        deadline = time.time() + 5
        while time.time() < deadline:
            bws.settimeout(max(0.1, deadline - time.time()))
            msg = json.loads(bws.recv())
            if msg.get("id") == 1:
                return "error" not in msg
    except Exception:
        return False
    finally:
        try:
            bws.close()
        except Exception:
            pass
    return False


def _registrable(host):
    """粗略取注册域：d.wanfangdata.com.cn → wanfangdata.com.cn

    注意 .com.cn 这类二级后缀本身占两段，不能简单取末两段。
    """
    parts = (host or "").lower().split(".")
    if len(parts) < 2:
        return host or ""
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_SUFFIX:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host_of(url):
    """URL → 主机名（小写），解析失败返回 ''"""
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def snapshot_page_ids(port):
    """当前 page target id 集合（用于记录"操作前已存在"的 tab）"""
    return {t["id"] for t in list_page_targets(port)}


def close_new_targets(port, known_ids, hosts=()):
    """关闭 known_ids 之后新出现、且域名命中 hosts 的 tab，返回关闭数量。

    用途：下载时站点自己 window.open 出打包/下载页（万方实测每次 +1，
    Chrome 重启还会恢复），这些 tab 不是 create_page 创建的，close_page 管不到。
    只动"新出现 + 域名命中白名单"的 tab：不碰其它站点、不碰操作前就有的、
    也不碰本进程正在使用的 tab。
    """
    allowed = {h.lower() for h in hosts if h}
    if not allowed:
        return 0
    closed = 0
    for t in list_page_targets(port):
        tid = t["id"]
        if tid in known_ids or tid in _ACTIVE_TARGETS:
            continue
        if _registrable(_host_of(t.get("url") or "")) in allowed:
            if close_target(port, tid):
                closed += 1
    return closed


def _warn_tab_limit(port):
    """tab 总数达到 tabs.max 时只告警，不自动关闭。

    四个 skill 共用一个 Chrome：本进程无从得知别的 skill / 别的进程正在用哪些
    tab，自动关"空 tab"会误伤它们刚建好、还没 navigate 的 about:blank（并行实测
    误关过 2/6）。真正的回收靠 create_page/close_page 配对。
    返回 (page target 总数, 其中未被本进程占用的空 tab 数)；未达上限返回 (0, 0)。
    """
    limit = get("tabs.max")
    if not limit or limit <= 0:
        return (0, 0)
    with _TAB_LOCK:
        tabs = list_page_targets(port)
        if len(tabs) < limit:
            return (0, 0)
        blanks = [t for t in tabs
                  if is_blank_target(t.get("url") or "") and t["id"] not in _ACTIVE_TARGETS]
        sys.stderr.write(
            f"[cdp] tab 数 {len(tabs)} 已达上限 {limit}（其中空 tab {len(blanks)} 个）；"
            f"只告警不自动关 —— 自动关会误伤并行任务正在用的 tab\n")
        return (len(tabs), len(blanks))


def create_page(port, timeout=None):
    """创建新 tab 并返回连到它的 CdpClient（并行抓取用）。

    通过 browser 级 CDP 的 Target.createTarget 新建 tab，
    轮询 /json 拿到新 target 的 ws url 后建立独立连接。
    用完必须 close_page(client)：断开 ws 不会关闭 tab，
    只调 client.close() 会让 tab 永久残留在共享 profile 的 Chrome 里。
    """
    _warn_tab_limit(port)
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
    version = json.loads(r.read())
    bws = websocket.create_connection(version["webSocketDebuggerUrl"], timeout=15, origin="http://localhost")
    target_id = None
    try:
        bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                             "params": {"url": "about:blank"}}))
        while True:
            msg = json.loads(bws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise CdpError(f"Target.createTarget 失败: {msg['error']}")
                target_id = msg["result"]["targetId"]
                _register_target(target_id)
                break
    finally:
        bws.close()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read())
            target = next((t for t in tabs if t.get("id") == target_id and t.get("webSocketDebuggerUrl")), None)
            if target:
                client = CdpClient(port, timeout=timeout, ws_url=target["webSocketDebuggerUrl"])
                client.target_id = target_id
                return client
        except Exception:
            pass
        time.sleep(0.3)
    _unregister_target(target_id)      # 建了 tab 却拿不到 ws：别让它永久占着活跃登记
    raise CdpError("新 tab 未就绪")


# ── 人类行为模拟 ────────────────────────────────────

_SCROLL_JS = """(maxRounds, dMin, dMax, pMin, pMax) => new Promise((resolve) => {
  const rounds = Math.min(maxRounds, 3 + Math.floor(Math.random() * 4));
  let i = 0;
  const step = () => {
    if (i >= rounds) { resolve(true); return; }
    i++;
    let delta = dMin + Math.floor(Math.random() * (dMax - dMin));
    if (Math.random() < 0.15) delta = -(Math.floor(dMin / 3) + Math.floor(Math.random() * Math.floor(dMin / 3)));
    window.scrollBy(0, delta);
    const pause = pMin + Math.random() * (pMax - pMin);
    setTimeout(step, pause);
  };
  step();
})"""


def human_scroll(client, max_rounds=None):
    """类人滚动：3-6 轮随机步长、15% 回滚、停顿 0.5-4s"""
    max_rounds = get("human.scroll_rounds")[1] if max_rounds is None else max_rounds
    d_min, d_max = get("human.scroll_delta")
    p_min, p_max = get("human.pause")
    client.evaluate(f"({_SCROLL_JS})({max_rounds}, {d_min}, {d_max}, {p_min}, {p_max})", await_promise=True)


def human_mouse(client, probability=None):
    """40% 概率随机移动鼠标（输入轨迹特征）"""
    probability = get("human.mouse_prob") if probability is None else probability
    if random.random() >= probability:
        return
    x, y = random.randint(100, 800), random.randint(100, 600)
    client.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    time.sleep(random.uniform(0.5, 1.5))


def random_delay(lo=None, hi=None):
    """请求间随机等待（防频率特征）"""
    lo, hi = (get("rate.limit")[0] if lo is None else lo), (get("rate.limit")[1] if hi is None else hi)
    time.sleep(random.uniform(lo, hi))


class RateLimiter:
    """相邻操作最小间隔（首次调用不等待）"""

    def __init__(self, lo=None, hi=None):
        lo, hi = (get("rate.limit")[0] if lo is None else lo), (get("rate.limit")[1] if hi is None else hi)
        self.lo, self.hi = lo, hi
        self._last = 0.0

    def wait(self):
        elapsed = time.time() - self._last
        need = random.uniform(self.lo, self.hi)
        if elapsed < need:
            time.sleep(need - elapsed)
        self._last = time.time()
