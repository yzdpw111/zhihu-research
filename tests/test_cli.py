# zhihu-research/tests/test_cli.py
import json

import pytest
from zhihu_detail import parse_args as zh_detail_parse_args
from zhihu_detail import parse_pipe_input as zh_parse_pipe_input
from zhihu_detail import read_targets as zh_read_targets


class TestZhihuDetailCli:
    def test_max_answers_capped(self):
        args = zh_detail_parse_args(["--url", "http://x", "--max-answers", "999"])
        assert args.max_answers == 80

    def test_max_answers_default(self):
        args = zh_detail_parse_args(["--url", "http://x"])
        assert args.max_answers == 15

    def test_max_answers_min_clamped(self):
        args = zh_detail_parse_args(["--url", "http://x", "--max-answers", "0"])
        assert args.max_answers == 1

    def test_multi_url(self):
        args = zh_detail_parse_args(["--url", "http://x/q1", "--url", "http://x/q2"])
        assert args.url == ["http://x/q1", "http://x/q2"]

    def test_rejects_no_input_tty(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        with pytest.raises(SystemExit):
            zh_detail_parse_args([])


class TestZhParsePipeInput:
    def test_invalid_json_returns_none(self):
        assert zh_parse_pipe_input("not json") is None
        assert zh_parse_pipe_input("{broken") is None

    def test_non_dict_structure_returns_none(self):
        assert zh_parse_pipe_input("[1, 2]") is None

    def test_valid_pipe_structure_returns_links(self):
        data = json.dumps({"results": [
            {"keyword": "k1", "items": [{"link": "https://z/q1"}, {"link": "https://z/p1"}]},
            {"keyword": "k2", "items": []},
        ]})
        links = zh_parse_pipe_input(data)
        assert sorted(links) == ["https://z/p1", "https://z/q1"]

    def test_valid_pipe_dedupes(self):
        data = json.dumps({"results": [
            {"items": [{"link": "https://z/q1"}]},
            {"items": [{"link": "https://z/q1"}]},
        ]})
        assert zh_parse_pipe_input(data) == ["https://z/q1"]

    def test_empty_or_missing_results_returns_empty(self):
        assert zh_parse_pipe_input('{"results": []}') == []
        assert zh_parse_pipe_input("{}") == []

    def test_bom_prefixed_pipe_ok(self):
        # PowerShell 管道会给 stdin 注入 UTF-8 BOM，JSON 解析应容忍
        data = '\ufeff{"results": [{"items": [{"link": "https://z/q1"}]}]}'
        assert zh_parse_pipe_input(data) == ["https://z/q1"]

    def test_malformed_results_entries_skipped(self):
        # 非 dict 的 results 项（结构错乱）应跳过而非抛异常
        data = json.dumps({"results": [None, "x", {"items": [{"link": "https://z/q1"}]}]})
        assert zh_parse_pipe_input(data) == ["https://z/q1"]


class TestZhReadTargets:
    def test_urls_flag_wins(self, monkeypatch):
        args = zh_detail_parse_args(["--url", "https://z/q1", "--url", "https://z/q2"])
        assert zh_read_targets(args) == ["https://z/q1", "https://z/q2"]

    def test_invalid_pipe_exits_friendly(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdin.read", lambda: "not json")
        with pytest.raises(SystemExit) as e:
            zh_read_targets(zh_detail_parse_args([]))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "JSON" in err and "stdin" in err

    def test_valid_pipe_returns_links(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdin.read", lambda: json.dumps(
            {"results": [{"items": [{"link": "https://z/q1"}, {"link": "https://z/p1"}]}]}))
        links = zh_read_targets(zh_detail_parse_args([]))
        assert sorted(links) == ["https://z/p1", "https://z/q1"]
