# tests/test_tab_management.py
"""tab 生命周期：create_page 开的 tab 必须有配对关闭，超上限时回收空 tab。

回归背景：四个 skill 共用同一 profile 的 Chrome，此前 create_page 只开不关，
tab 永久累积，只能靠 chrome_session.py --stop 整进程清掉。
"""
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import cdp_base
from config import DEFAULTS


class _Resp:
    def __init__(self, payload=""):
        self._payload = payload.encode()

    def read(self):
        return self._payload


class FakeUrlOpen:
    """记录请求 URL；/json 返回给定 target 列表，/json/version 返回 browser ws 地址。"""

    def __init__(self, tabs=(), browser_ws="ws://browser"):
        self.tabs = list(tabs)
        self.browser_ws = browser_ws
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        self.urls.append(url)
        if url.endswith("/json/version"):
            return _Resp(json.dumps({"webSocketDebuggerUrl": self.browser_ws}))
        if "/json/close/" in url:
            tid = url.rsplit("/", 1)[-1]          # 真实 Chrome 关闭后 /json 就不再列出该 tab
            self.tabs = [t for t in self.tabs if t.get("id") != tid]
            return _Resp()
        if url.endswith("/json"):
            return _Resp(json.dumps(self.tabs))
        return _Resp()


class FakeWs:
    def __init__(self, msgs=()):
        self.msgs = list(msgs)
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        return json.dumps(self.msgs.pop(0))

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


class FakeClient:
    """create_page 返回值的替身：只需 port / target_id / close()。"""

    def __init__(self, port=9222, target_id="tab-1"):
        self.port = port
        self.target_id = target_id
        self.closed = False

    def close(self):
        self.closed = True


def _limit(monkeypatch, value):
    """只覆盖 tabs.max，其余配置走真实 get()"""
    real = cdp_base.get
    monkeypatch.setattr(cdp_base, "get", lambda k: value if k == "tabs.max" else real(k))


@pytest.fixture
def fake_urlopen(monkeypatch):
    fake = FakeUrlOpen()
    monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
    return fake


def _close_urls(fake):
    return sorted(u for u in fake.urls if "/json/close/" in u)


class TestClosePage:
    def test_closes_tab_and_disconnects_ws(self, fake_urlopen):
        fake_urlopen.tabs = [
            {"id": "tab-1", "type": "page", "url": "about:blank"},
            {"id": "other", "type": "page", "url": "https://www.xiaohongshu.com/explore/1"},
        ]
        client = FakeClient()
        assert cdp_base.close_page(client) is True
        assert _close_urls(fake_urlopen) == ["http://127.0.0.1:9222/json/close/tab-1"]
        assert client.closed is True

    def test_creates_placeholder_then_closes_last_tab(self, monkeypatch):
        """只剩一个 tab 时：先建一个 about:blank 占位再关它。

        否则要么 Chrome 整体退出，要么浏览器里留下"上次的搜索结果/详情页"。
        """
        fake = FakeUrlOpen([{"id": "only", "type": "page",
                             "url": "https://s.wanfangdata.com.cn/paper?q=x"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        sent = []

        class FakeWs:
            def send(self, payload):
                sent.append(payload)

            def recv(self):
                return json.dumps({"id": 1, "result": {"targetId": "placeholder"}})

            def settimeout(self, t):
                pass

            def close(self):
                pass

        monkeypatch.setattr(cdp_base.websocket, "create_connection",
                            lambda url, timeout=None, origin=None: FakeWs())

        client = FakeClient(target_id="only")
        assert cdp_base.close_page(client) is True
        assert any("Target.createTarget" in s for s in sent)          # 先建占位
        assert _close_urls(fake) == ["http://127.0.0.1:9222/json/close/only"]
        assert client.closed is True

    def test_placeholder_failure_keeps_last_tab(self, monkeypatch):
        """占位 tab 建不出来时，宁可留着最后一个 tab，也不能让 Chrome 退出。"""
        fake = FakeUrlOpen([{"id": "only-1", "type": "page", "url": "about:blank"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)

        class BadWs:
            def send(self, payload):
                raise OSError("boom")

            def recv(self):
                return "{}"

            def settimeout(self, t):
                pass

            def close(self):
                pass

        monkeypatch.setattr(cdp_base.websocket, "create_connection",
                            lambda url, timeout=None, origin=None: BadWs())

        client = FakeClient(target_id="only-1")
        assert cdp_base.close_page(client) is False
        assert _close_urls(fake) == []
        assert client.closed is True  # ws 仍然要断

    def test_still_disconnects_ws_when_tab_close_fails(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", boom)
        client = FakeClient()
        assert cdp_base.close_page(client) is False
        assert client.closed is True  # 关 tab 失败也必须断开 ws，不能残留连接

    def test_none_client_is_noop(self):
        assert cdp_base.close_page(None) is False


class TestCloseTarget:
    def test_without_target_id_skips_request(self, fake_urlopen):
        assert cdp_base.close_target(9222, None) is False
        assert fake_urlopen.urls == []

    def test_without_port_skips_request(self, fake_urlopen):
        assert cdp_base.close_target(None, "tab-1") is False
        assert fake_urlopen.urls == []


class TestIsBlankTarget:
    @pytest.mark.parametrize("url", ["about:blank", "about:blank#x",
                                     "chrome://newtab/", "chrome://new-tab-page/"])
    def test_blank_urls(self, url):
        assert cdp_base.is_blank_target(url) is True

    @pytest.mark.parametrize("url", ["https://www.xiaohongshu.com/explore/abc",
                                     "https://www.zhihu.com/question/1", ""])
    def test_real_urls(self, url):
        assert cdp_base.is_blank_target(url) is False


class TestTabLimitWarning:
    """tabs.max 只告警、不自动关：四个 skill 共用一个 Chrome，
    自动关"空 tab"会误伤别的 skill（甚至别的进程）正在用的 about:blank tab。"""

    def test_default_limit_configured(self):
        assert DEFAULTS["tabs.max"] >= 1

    def test_below_limit_does_nothing(self, monkeypatch):
        fake = FakeUrlOpen([{"id": "real-1", "type": "page",
                             "url": "https://www.xiaohongshu.com/explore/1"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        _limit(monkeypatch, 3)
        assert cdp_base._warn_tab_limit(9222) == (0, 0)
        assert _close_urls(fake) == []

    def test_warns_at_limit_without_closing_anything(self, monkeypatch, capsys):
        tabs = [
            {"id": "blank-1", "type": "page", "url": "about:blank"},
            {"id": "real-1", "type": "page", "url": "https://www.xiaohongshu.com/explore/1"},
            {"id": "blank-2", "type": "page", "url": "chrome://newtab/"},
            {"id": "real-2", "type": "page", "url": "https://www.xiaohongshu.com/explore/2"},
        ]
        fake = FakeUrlOpen(tabs)
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        _limit(monkeypatch, 3)
        assert cdp_base._warn_tab_limit(9222) == (4, 2)   # (总数, 其中空 tab 数)
        assert _close_urls(fake) == []                    # 一个都不关
        assert "已达上限" in capsys.readouterr().err

    def test_all_blank_also_closes_nothing(self, monkeypatch, capsys):
        """全空也一样不关（关掉最后一个 tab 会让 Chrome 整体退出）。"""
        tabs = [{"id": f"blank-{i}", "type": "page", "url": "about:blank"}
                for i in range(3)]
        fake = FakeUrlOpen(tabs)
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        _limit(monkeypatch, 3)
        assert cdp_base._warn_tab_limit(9222) == (3, 3)
        assert _close_urls(fake) == []

    def test_disabled_when_limit_zero(self, monkeypatch):
        fake = FakeUrlOpen([{"id": "blank-1", "type": "page", "url": "about:blank"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        _limit(monkeypatch, 0)
        assert cdp_base._warn_tab_limit(9222) == (0, 0)
        assert fake.urls == []  # 关闭时连 /json 都不查


class TestParallelSafety:
    """共用 Chrome 时 tabs.max 只告警不关 tab；真正关闭只走 create_page/close_page 配对。"""

    def _open_live_tab(self, monkeypatch):
        conns = []

        def fake_conn(url, timeout=None, origin=None):
            ws = FakeWs([{"id": 1, "result": {"targetId": "live-1"}}] if not conns else [])
            conns.append(ws)
            return ws

        monkeypatch.setattr(cdp_base.websocket, "create_connection", fake_conn)
        return cdp_base.create_page(9222)

    def test_never_closes_other_tabs_even_when_active(self, monkeypatch):
        """告警只统计、不动手：别的线程/别的 skill 正在用的 tab 绝不关。"""
        fake = FakeUrlOpen([{"id": "live-1", "type": "page", "url": "about:blank",
                             "webSocketDebuggerUrl": "ws://live-1"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        live = self._open_live_tab(monkeypatch)

        fake.tabs = [
            {"id": "live-1", "type": "page", "url": "about:blank"},
            {"id": "blank-1", "type": "page", "url": "about:blank"},
            {"id": "blank-2", "type": "page", "url": "chrome://newtab/"},
            {"id": "real-1", "type": "page", "url": "https://www.xiaohongshu.com/explore/1"},
        ]
        _limit(monkeypatch, 3)
        # live-1 是本进程在用的，不计入"空 tab"统计
        assert cdp_base._warn_tab_limit(9222) == (4, 2)
        assert _close_urls(fake) == []
        assert live.target_id == "live-1"

    def test_closed_tab_counted_again_after_close_page(self, monkeypatch):
        """close_page 要解除登记，否则活跃集合无限增长、统计失真。"""
        fake = FakeUrlOpen([{"id": "live-1", "type": "page", "url": "about:blank",
                             "webSocketDebuggerUrl": "ws://live-1"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        live = self._open_live_tab(monkeypatch)
        cdp_base.close_page(live)

        fake.tabs = [
            {"id": "live-1", "type": "page", "url": "about:blank"},
            {"id": "blank-1", "type": "page", "url": "about:blank"},
            {"id": "blank-2", "type": "page", "url": "chrome://newtab/"},
            {"id": "real-1", "type": "page", "url": "https://www.xiaohongshu.com/explore/1"},
        ]
        _limit(monkeypatch, 3)
        assert cdp_base._warn_tab_limit(9222) == (4, 3)


    def test_concurrent_close_never_empties_chrome(self, monkeypatch):
        """两线程同时关最后两个 tab：检查与关闭不原子就会双双放行 → 关空 → Chrome 退出。"""
        fake = FakeUrlOpen([
            {"id": "a", "type": "page", "url": "about:blank"},
            {"id": "b", "type": "page", "url": "about:blank"},
        ])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)

        barrier = threading.Barrier(2, timeout=1)
        real_list = cdp_base.list_page_targets

        def slow_list(port):
            try:
                barrier.wait()          # 逼两个线程同时进入「取当前 tab 数」这一步
            except Exception:
                pass
            return real_list(port)

        monkeypatch.setattr(cdp_base, "list_page_targets", slow_list)

        clients = [FakeClient(target_id="a"), FakeClient(target_id="b")]
        with ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(cdp_base.close_page, clients))

        closed = _close_urls(fake)
        assert len(closed) == 1, f"两个 tab 都被关掉，Chrome 会退出: {closed}"
        assert all(c.closed for c in clients)   # 两个 ws 都要断


class TestCreatePageStampsHandle:
    def test_stamps_target_id_and_port(self, monkeypatch):
        """close_page 要有关闭句柄：create_page 必须把 targetId/port 挂到 client 上。"""
        fake = FakeUrlOpen([{"id": "tab-9", "type": "page", "url": "about:blank",
                             "webSocketDebuggerUrl": "ws://page-9"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)

        conns = []

        def fake_conn(url, timeout=None, origin=None):
            ws = FakeWs([{"id": 1, "result": {"targetId": "tab-9"}}] if not conns else [])
            conns.append(ws)
            return ws

        monkeypatch.setattr(cdp_base.websocket, "create_connection", fake_conn)
        client = cdp_base.create_page(9222)
        assert client.target_id == "tab-9"
        assert client.port == 9222


class TestStdoutEncoding:
    """GBK 控制台 + 结果里的 emoji 变体符 = stdout.write 抛异常，结果全丢。"""

    def test_setup_stdout_makes_variation_selector_writable(self, monkeypatch):
        raw = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        monkeypatch.setattr(cdp_base.sys, "stdout", raw)
        monkeypatch.setattr(cdp_base.sys, "stderr",
                            io.TextIOWrapper(io.BytesIO(), encoding="gbk"))

        with pytest.raises(UnicodeEncodeError):
            raw.write("\ufe0f")        # 修复前：真实控制台下就是这样崩的

        cdp_base.setup_stdout()
        raw.write("\ufe0f")            # 修复后：不再抛，字符照原样输出
        assert raw.encoding.lower().replace("-", "") == "utf8"

    def test_setup_stdout_tolerates_stream_without_reconfigure(self, monkeypatch):
        class Bare:
            def write(self, s):
                return len(s)

        monkeypatch.setattr(cdp_base.sys, "stdout", Bare())
        monkeypatch.setattr(cdp_base.sys, "stderr", Bare())
        cdp_base.setup_stdout()        # 不抛异常即通过


class TestWriteLog:
    """完整结果落盘：stdout 有大小上限，Agent 需要能读到全文。"""

    def test_writes_full_utf8_json_to_configured_dir(self, tmp_path, monkeypatch):
        real = cdp_base.get
        monkeypatch.setattr(cdp_base, "get",
                            lambda k: str(tmp_path) if k == "logs.dir" else real(k))
        data = {"notes": [{"desc": "emoji ❗️ 与换行\n完整正文"}]}

        path = cdp_base.write_log(data, "xhs_detail")

        assert os.path.isabs(path)
        assert os.path.dirname(path) == str(tmp_path)
        assert os.path.basename(path).startswith("xhs_detail-")
        with open(path, encoding="utf-8") as f:
            assert json.load(f) == data

    def test_creates_missing_dir(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "logs"
        real = cdp_base.get
        monkeypatch.setattr(cdp_base, "get",
                            lambda k: str(target) if k == "logs.dir" else real(k))
        assert os.path.isfile(cdp_base.write_log({"a": 1}, "xhs_search"))


class TestCloseNewTargets:
    """下载时站点自己 window.open 出 tab（万方实测每次 +1，Chrome 重启还会恢复）：
    只清"本次操作期间新出现 + 域名命中白名单"的 tab。"""

    def _tabs(self):
        return [
            {"id": "old", "type": "page", "url": "https://d.wanfangdata.com.cn/periodical/x"},
            {"id": "pop-oss", "type": "page",
             "url": "https://oss.wanfangdata.com.cn/Fulltext/Download?fileId=1"},
            {"id": "pop-part", "type": "page",
             "url": "https://f.wanfangdata.com.cn/part/pc/thesis/D1"},
            {"id": "new-other", "type": "page", "url": "https://www.example.com/"},
        ]

    def test_snapshot_returns_ids(self, monkeypatch):
        fake = FakeUrlOpen(self._tabs())
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        assert cdp_base.snapshot_page_ids(9222) == {"old", "pop-oss", "pop-part", "new-other"}

    def test_closes_only_new_tabs_on_allowed_hosts(self, monkeypatch):
        fake = FakeUrlOpen(self._tabs())
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        assert cdp_base.close_new_targets(9222, {"old"}, ("wanfangdata.com.cn",)) == 2
        assert _close_urls(fake) == [
            "http://127.0.0.1:9222/json/close/pop-oss",
            "http://127.0.0.1:9222/json/close/pop-part",
        ]

    def test_skips_tabs_on_other_hosts(self, monkeypatch):
        fake = FakeUrlOpen([{"id": "x", "type": "page", "url": "https://www.example.com/"}])
        monkeypatch.setattr(cdp_base.urllib.request, "urlopen", fake)
        assert cdp_base.close_new_targets(9222, set(), ("wanfangdata.com.cn",)) == 0
        assert _close_urls(fake) == []

    def test_registrable_domain_helper(self):
        assert cdp_base._registrable("d.wanfangdata.com.cn") == "wanfangdata.com.cn"
        assert cdp_base._registrable("oss.wanfangdata.com.cn") == "wanfangdata.com.cn"
        assert cdp_base._registrable("example.com") == "example.com"
        assert cdp_base._registrable("") == ""
