# zhihu-research/tests/test_zhihu_detail_result.py
"""zhihu_detail.main() 的结果组装：失败明细必须落盘，不能只留一个计数。

背景：详情抓取是批量并发的，``--parallel > 1`` 或请求过密时会出现"部分 URL 失败"。
只有 ``failed`` 计数时，调用方（Agent/CI）无法知道是哪些 URL 挂了、为什么挂，
数据缺口会静默地污染下游结论。此处对齐 xhs_detail 的行为：
失败时在输出里附带 ``failures: [{url, error}]``，全部成功时不出现该字段。
"""
import json
from types import SimpleNamespace

import zhihu_detail


def _run_main(monkeypatch, capsys, results):
    """跑一次 zhihu_detail.main()，返回 (stdout JSON, 传给 write_log 的对象, stderr)。"""
    urls = [f"https://www.zhihu.com/question/{1000 + i}" for i in range(len(results))]
    by_url = dict(zip(urls, results))
    args = SimpleNamespace(url=urls, max_answers=15, max_comments=15, parallel=2)

    monkeypatch.setattr(zhihu_detail, "parse_args", lambda: args)
    monkeypatch.setattr(zhihu_detail, "ensure_cdp", lambda: 9222)
    monkeypatch.setattr(zhihu_detail, "_scrape_in_tab", lambda port, u, a: (u, by_url[u]))

    captured = {}

    def fake_write_log(out, name):
        # 深拷贝：main 之后还会往 out 里塞 logPath
        captured["out"] = json.loads(json.dumps(out, ensure_ascii=False))
        captured["name"] = name
        return "X:/fake/log.json"

    monkeypatch.setattr(zhihu_detail, "write_log", fake_write_log)

    zhihu_detail.main()
    io = capsys.readouterr()
    return json.loads(io.out), captured, io.err


def _column(url, title="t"):
    return {"url": url, "type": "column", "title": title, "author": "a", "content": "c"}


def _question(url, title="q"):
    return {"url": url, "type": "question", "title": title,
            "total": "1", "answers": [], "comments": []}


class TestFailuresAreRecorded:
    def test_partial_failure_keeps_url_and_error(self, monkeypatch, capsys):
        out, captured, err = _run_main(monkeypatch, capsys, [
            _column("https://zhuanlan.zhihu.com/p/1"),
            RuntimeError("专栏页加载超时"),
        ])

        assert out["count"] == 2
        assert out["succeeded"] == 1
        assert out["failed"] == 1
        assert len(out["failures"]) == 1

        f = out["failures"][0]
        assert f["url"] == "https://www.zhihu.com/question/1001"
        assert "专栏页加载超时" in f["error"]

        # 失败原因同时仍写 stderr（保持原有行为）
        assert "失败:" in err
        assert "专栏页加载超时" in err

    def test_failures_also_land_in_log_payload(self, monkeypatch, capsys):
        out, captured, _ = _run_main(monkeypatch, capsys, [
            RuntimeError("需要登录"),
            _question("https://www.zhihu.com/question/2"),
        ])

        logged = captured["out"]
        assert captured["name"] == "zhihu_detail"
        assert logged["failures"] == out["failures"]
        assert "logPath" not in logged  # 落盘的是纯结果

    def test_all_failed_has_no_questions_or_columns(self, monkeypatch, capsys):
        out, _, _ = _run_main(monkeypatch, capsys, [
            RuntimeError("触发风控/验证"),
            RuntimeError("无效 URL"),
        ])

        assert out["count"] == 2
        assert out["succeeded"] == 0
        assert out["failed"] == 2
        assert "questions" not in out
        assert "columns" not in out
        assert [f["error"] for f in out["failures"]] == ["触发风控/验证", "无效 URL"]

    def test_long_error_is_truncated(self, monkeypatch, capsys):
        out, _, _ = _run_main(monkeypatch, capsys, [RuntimeError("x" * 500)])
        assert out["failures"][0]["error"] == "x" * 200


class TestSuccessPathUnchanged:
    def test_all_success_omits_failures(self, monkeypatch, capsys):
        out, _, err = _run_main(monkeypatch, capsys, [
            _column("https://zhuanlan.zhihu.com/p/1"),
            _question("https://www.zhihu.com/question/2"),
        ])

        assert out["count"] == 2
        assert out["succeeded"] == 2
        assert out["failed"] == 0
        assert "failures" not in out
        assert len(out["columns"]) == 1
        assert len(out["questions"]) == 1
        assert "完成:" in err
