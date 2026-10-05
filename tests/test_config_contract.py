# tests/test_config_contract.py
"""守护 config.py 的共享逻辑，防止四个 skill 之间静默漂移。

背景：xhs / zhihu / ieee / wanfang 四个 skill 各自带一份 config.py（为了可独立分发），
DEFAULTS 本来就按 skill 不同、**应该**不同；但 DEFAULTS 之外的辅助逻辑
（_coerce / _env_key / get / set / resolve_port）是**同一套实现复制了四份**。

风险：改了其中一个 skill 的 config.py 逻辑，忘了同步另外三个 → 行为静默分叉。

本测试锁定这段共享逻辑的**归一化**形态（去注释、去空行、去 docstring），
所以：
  - 纯注释/docstring/空行改动 → 不触发失败（不算逻辑变更）
  - 任何实质逻辑改动 → 失败，提示需要同步到另外三个 skill 并更新本断言

改这段逻辑时，请同步四个 skill 并更新下面的 FROZEN 值。
"""
import ast
import hashlib
import os

CONFIG_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "config.py")

# 共享逻辑的归一化 SHA-256（四份 config.py 应完全一致）
FROZEN = "eefa47c12042edcb7d5a5c2872cb71033225cc6f99d18e63b9920837e9f7404d"

# 这段共享逻辑包含的顶层定义
SHARED_NAMES = ("_OVERRIDES", "_coerce", "_env_key", "get", "set", "resolve_port")


def _shared_logic_normalized(path):
    """抽出 DEFAULTS 之外的共享逻辑并归一化。

    - 只取 SHARED_NAMES 对应的顶层节点源码，忽略顺序之外的内容
    - 去掉注释、docstring、空白差异
    """
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")

    chunks = []
    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                name = t.id
        if name not in SHARED_NAMES:
            continue
        if node.end_lineno is None:
            continue
        seg = lines[node.lineno - 1: node.end_lineno]
        # 去掉 docstring（函数体第一条 Expr 若是常量字符串）
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ds_end = body[0].end_lineno
                seg = [l for i, l in enumerate(seg, start=node.lineno) if i != ds_end]
        chunks.append("\n".join(seg))

    # 归一化：去行尾空白、去空行、去整行注释
    out = []
    for line in "\n".join(chunks).split("\n"):
        s = line.rstrip()
        if not s.strip():
            continue
        if s.strip().startswith("#"):
            continue
        out.append(s)
    return "\n".join(out)


def test_config_py_exists():
    assert os.path.isfile(CONFIG_PY), f"未找到 {CONFIG_PY}"


def test_shared_logic_shape_is_stable():
    """共享逻辑的归一化内容应与冻结值一致（防止单仓库静默改动）。"""
    norm = _shared_logic_normalized(CONFIG_PY)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()

    assert digest == FROZEN, (
        "config.py 的共享逻辑（_coerce/_env_key/get/set/resolve_port）已改动。\n"
        "这段逻辑在 xhs/zhihu/ieee/wanfang 四个 skill 里是同一份实现 —— 请同步四处，\n"
        "然后把本文件里的 FROZEN 更新为新的哈希。\n"
        f"  实际: {digest}\n"
        f"  期望: {FROZEN}\n"
        "若这是有意为之且只改了本 skill，请重新评估是否应该只改 DEFAULTS 而不是逻辑。"
    )


def test_defaults_are_skill_specific_not_required_identical():
    """反向保险：DEFAULTS 允许各 skill 不同，不应被上面的测试波及。"""
    import config
    assert isinstance(config.DEFAULTS, dict) and config.DEFAULTS
    assert hasattr(config, "_ENV_PREFIX") and config._ENV_PREFIX.endswith("_")
