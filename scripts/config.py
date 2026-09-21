#!/usr/bin/env python3
"""config.py — 集中配置（对标 ieee config.js）

两层: DEFAULTS → {PREFIX}_* 环境变量覆盖
API: get(key) / set(key) / resolve_port()
"""
import os

# 环境变量前缀（xhs 用 XHS_，zhihu 用 ZHIHU_，复制到对应 skill 时改这里）
_ENV_PREFIX = "ZHIHU_"

DEFAULTS = {
    "state.dir": os.path.expandvars(r"%USERPROFILE%\.yzdpw_state"),
    "timeout.cdp": 30,                # CDP WebSocket 超时(s)
    "timeout.chrome_start": 30,       # Chrome 启动等待(s)
    "rate.limit": (8.0, 18.0),        # 请求限速范围(s)，反爬关键
    "human.scroll_rounds": (3, 6),    # 滚动轮数范围
    "human.scroll_delta": (150, 500), # 滚动步长范围(px)
    "human.pause": (0.5, 4.0),        # 滚动停顿范围(s)
    "human.mouse_prob": 0.4,          # 鼠标移动概率
    "parallel.limit": 2,              # 并行上限（默认 2）
    "tabs.max": 30,                   # tab 数上限，达到即只告警（不自动关 tab；0=不启用）
    "logs.dir": os.path.join(os.getcwd(), "logs"),  # 完整结果落盘目录（默认运行目录）
}

_OVERRIDES = {}


def _coerce(raw):
    """环境变量字符串 → 类型：'3,5'→(3,5)、数字、布尔、原样"""
    s = raw.strip()
    if "," in s:
        return tuple(_coerce(x) for x in s.split(","))
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return s


def _env_key(key):
    return _ENV_PREFIX + key.upper().replace(".", "_")


def get(key):
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    env_key = _env_key(key)
    if env_key in os.environ:
        return _coerce(os.environ[env_key])
    return DEFAULTS.get(key)


def set(key, value):
    _OVERRIDES[key] = value


def resolve_port():
    try:
        pf = os.path.join(get("state.dir"), ".cdp_port")
        with open(pf) as f:
            return int(f.read().strip())
    except Exception:
        return 9222
