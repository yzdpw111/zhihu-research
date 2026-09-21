# zhihu-research/tests/test_config.py
import os

import config
from config import get, set


class TestConfig:
    def test_get_default(self):
        assert get("timeout.cdp") == 30
        assert get("parallel.limit") == 2

    def test_state_dir(self):
        assert "yzdpw_state" in get("state.dir")

    def test_logs_dir_defaults_to_cwd(self):
        """默认落在运行目录（当前对话/工作目录）的 logs/，与 ieee/wanfang 一致。"""
        assert get("logs.dir") == os.path.join(os.getcwd(), "logs")

    def test_rate_limit_tuple(self):
        lo, hi = get("rate.limit")
        assert lo < hi

    def test_set_override_and_cleanup(self):
        set("timeout.cdp", 60)
        assert get("timeout.cdp") == 60
        config._OVERRIDES.pop("timeout.cdp", None)
        assert get("timeout.cdp") == 30

    def test_env_override(self, monkeypatch):
        # 动态前缀：兼容 XHS_/ZHIHU_ 环境变量
        key = config._ENV_PREFIX + "TIMEOUT_CDP"
        monkeypatch.setenv(key, "45")
        assert get("timeout.cdp") == 45

    def test_env_tuple(self, monkeypatch):
        key = config._ENV_PREFIX + "RATE_LIMIT"
        monkeypatch.setenv(key, "3,5")
        assert get("rate.limit") == (3, 5)
