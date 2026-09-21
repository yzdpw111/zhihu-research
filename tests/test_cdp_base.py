# tests/test_cdp_base.py
import time

import cdp_base
from cdp_base import RateLimiter, pick_port

class TestRateLimiter:
    def test_first_call_no_wait(self):
        limiter = RateLimiter(lo=5.0, hi=5.0)
        t0 = time.time()
        limiter.wait()  # 首次调用：上一操作时间为 0，elapsed 足够大，不应 sleep
        assert time.time() - t0 < 1.0

    def test_second_call_enforces_gap(self):
        limiter = RateLimiter(lo=1.0, hi=1.0)
        limiter.wait()
        t0 = time.time()
        limiter.wait()  # 距上次 < 1s，应补足到 1s
        assert time.time() - t0 >= 0.9

class TestPickPort:
    def test_picks_port_in_range(self, monkeypatch):
        monkeypatch.setattr("cdp_base.port_busy", lambda p: False)
        assert 9222 <= pick_port() <= 9299

    def test_skips_busy_ports(self, monkeypatch):
        busy = {9222, 9223}
        monkeypatch.setattr("cdp_base.port_busy", lambda p: p in busy)
        assert pick_port() == 9224


class TestPortBusy:
    """未监听端口的探测必须"立刻"返回 False。

    实测：urlopen 直连未监听端口会等满 timeout，于是扫描 9222-9299 要 ~200s
    —— Chrome 离线时 ensure_cdp 会卡好几分钟。
    """

    def test_closed_port_returns_fast(self):
        t0 = time.time()
        assert cdp_base.port_busy(6599) is False
        assert time.time() - t0 < 0.5, f"探测未监听端口耗时 {time.time() - t0:.2f}s"


class TestEnsureCdpLaunch:
    """Chrome 启动必须脱离调用方进程组：脚本退出后 Chrome 要继续存活。

    原先走 schtasks 计划任务，实测本机不再触发（Last Run Time 不更新、
    端口不监听），改为直接 Popen(DETACHED_PROCESS)。
    """

    def _patch(self, monkeypatch, tmp_path):
        state = {"launched": False}
        calls = []

        class FakePopen:
            def __init__(self, args, **kw):
                calls.append((args, kw))
                state["launched"] = True

        monkeypatch.setattr(cdp_base, "PORT_FILE", str(tmp_path / ".cdp_port"))
        monkeypatch.setattr(cdp_base, "PROFILE", str(tmp_path / "profile"))
        monkeypatch.setattr(cdp_base, "pick_port", lambda: 9222)
        monkeypatch.setattr(cdp_base, "find_chrome", lambda: r"C:\chrome.exe")
        monkeypatch.setattr(cdp_base, "port_busy", lambda p: p == 9222 and state["launched"])
        monkeypatch.setattr(cdp_base.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(cdp_base.time, "sleep", lambda s: None)
        monkeypatch.setattr(cdp_base, "get", lambda k: 5 if k == "timeout.chrome_start" else 30)
        return calls

    def test_launches_detached_chrome(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, tmp_path)

        assert cdp_base.ensure_cdp() == 9222

        args, kw = calls[0]
        assert args[0].endswith("chrome.exe")
        assert "--remote-debugging-port=9222" in args
        assert any(a.startswith("--user-data-dir=") for a in args)
        assert kw["creationflags"] & 0x00000008        # DETACHED_PROCESS

    def test_records_port_file(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path)
        cdp_base.ensure_cdp()
        with open(tmp_path / ".cdp_port") as f:
            assert f.read().strip() == "9222"

    def test_no_schtasks_used(self, monkeypatch, tmp_path):
        """回归：不再依赖计划任务（本机实测 schtasks 路线已失效）。"""
        calls = self._patch(monkeypatch, tmp_path)
        ran = []
        monkeypatch.setattr(cdp_base.subprocess, "run", lambda *a, **kw: ran.append(a))

        cdp_base.ensure_cdp()

        assert ran == []
        assert calls
