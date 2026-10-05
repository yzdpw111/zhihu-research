# tests/test_doc_cli_sync.py
"""守护 SKILL.md 与脚本 --help 的一致性，防止文档漂移。

背景：SKILL.md 里手写了各脚本的参数名（表格），而参数真实来源是脚本的 argparse。
两边各写一遍 → 改了 argparse 忘了改文档（或反之）就会静默偏离。

本测试：
  1) SKILL.md 中作为 CLI 参数出现的 `--xxx`，必须真实存在于某个脚本的 --help 中
     （抓「文档写了不存在的参数」）
  2) 各脚本 --help 里的长选项，必须都至少在 SKILL.md 中出现过一次
     （抓「新增参数没写文档」）

不依赖网络 / Chrome：只用 python <script> --help 与读文件。
"""
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO, "scripts")
SKILL_MD = os.path.join(REPO, "SKILL.md")

# 这些 `--xxx` 不是本 skill 脚本的 CLI 参数，而是 Chrome 启动参数（散见于正文说明），
# 因此不要求出现在任何脚本的 --help 里。
NON_CLI_FLAGS = {
    "--enable-automation",
    "--remote-debugging-port",
    "--remote-allow-origins",
    "--user-data-dir",
    "--no-first-run",
    "--no-default-browser-check",
}


def _script_paths():
    if not os.path.isdir(SCRIPTS_DIR):
        return []
    return sorted(
        os.path.join(SCRIPTS_DIR, f)
        for f in os.listdir(SCRIPTS_DIR)
        if f.endswith(".py") and f != "config.py"
    )


def _help_flags_of(script):
    """运行 `python <script> --help`，抽出其中的长选项。

    只在脚本成功输出帮助时才抽 —— 纯模块（无 argparse）跑 --help 会报错，
    报错文本里可能含 `--xxx`（如路径或文件名），不能当成参数。
    """
    try:
        r = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=REPO)
    except subprocess.TimeoutExpired:
        return set()
    if r.returncode != 0:
        return set()
    out = (r.stdout or "")
    return set(re.findall(r"--[a-z][a-z0-9-]*", out))


def _doc_flags(path):
    """抽出 SKILL.md 中作为 CLI 参数书写的 `--xxx`（反引号包裹）。"""
    text = open(path, encoding="utf-8").read()
    return set(re.findall(r"`(--[a-z][a-z0-9-]*)`", text))


@pytest.fixture(scope="module")
def help_flags():
    flags = set()
    for s in _script_paths():
        flags |= _help_flags_of(s)
    return flags


@pytest.fixture(scope="module")
def doc_flags():
    return _doc_flags(SKILL_MD)


def test_skill_md_and_scripts_exist():
    assert os.path.isfile(SKILL_MD), f"未找到 {SKILL_MD}"
    assert _script_paths(), f"{SCRIPTS_DIR} 下没有脚本"


def test_scripts_do_not_crash_on_help():
    """每个脚本跑 --help 都不应因语法/导入错误而崩溃。

    scripts/ 下不全是 CLI：也可能是纯模块（如 ocr_images.py 只提供函数），
    这类脚本没有 argparse，跑 --help 会以非 0 退出 —— 这是正常的，不算失败。
    真正要抓的是 SyntaxError / ImportError / ModuleNotFoundError 这类崩溃。
    """
    crash_markers = ("SyntaxError", "ImportError", "ModuleNotFoundError",
                     "IndentationError", "AttributeError")
    bad = []
    for s in _script_paths():
        r = subprocess.run([sys.executable, s, "--help"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, cwd=REPO)
        err = (r.stderr or "")
        if r.returncode != 0 and any(m in err for m in crash_markers):
            bad.append((os.path.basename(s), err[-300:]))
    assert not bad, "以下脚本存在语法/导入错误：\n" + \
        "\n".join(f"  {n}: {e}" for n, e in bad)


def test_documented_flags_exist_in_help(doc_flags, help_flags):
    """SKILL.md 写到的参数必须是真实存在的（除已知的非 CLI 参数）。"""
    unknown = doc_flags - help_flags - NON_CLI_FLAGS
    assert not unknown, (
        "SKILL.md 里出现了脚本 --help 中不存在的参数（文档漂移或拼写错误）：\n  "
        + "\n  ".join(sorted(unknown))
        + "\n若这些确实不是本 skill 脚本的参数，请加入 NON_CLI_FLAGS。"
    )


def test_help_flags_are_documented(doc_flags, help_flags):
    """脚本 --help 里的参数都应至少在 SKILL.md 中出现过一次（防止新增参数不写文档）。"""
    missing = help_flags - doc_flags - {"--help"}
    assert not missing, (
        "以下参数存在于脚本 --help 中，但 SKILL.md 从未提到：\n  "
        + "\n  ".join(sorted(missing))
        + "\n请在 SKILL.md 中补充说明。"
    )
