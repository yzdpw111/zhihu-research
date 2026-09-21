# python/tests/test_zhihu_search.py
from zhihu_search import build_url, parse_args


class TestBuildUrl:
    def test_default_no_filter(self):
        args = parse_args(["--q", "x"])
        assert build_url("机器学习", args) == (
            "https://www.zhihu.com/search?type=content&q=%E6%9C%BA%E5%99%A8%E5%AD%A6%E4%B9%A0")

    def test_type_filter(self):
        args = parse_args(["--q", "x", "--type", "回答"])
        assert "&vertical=answer" in build_url("机器学习", args)

    def test_sort_filter(self):
        args = parse_args(["--q", "x", "--sort", "最新发布"])
        assert "&sort=created_time" in build_url("机器学习", args)

    def test_time_filter(self):
        args = parse_args(["--q", "x", "--time", "一周内"])
        assert "&time_interval=a_week" in build_url("机器学习", args)

    def test_all_filters(self):
        args = parse_args(["--q", "x", "--type", "文章", "--sort", "最多赞同", "--time", "一年内"])
        url = build_url("机器学习", args)
        assert "&vertical=article" in url
        assert "&sort=upvoted_count" in url
        assert "&time_interval=a_year" in url


class TestZhihuSearchFilterArgs:
    def test_multi_q(self):
        args = parse_args(["--q", "x", "--q", "y"])
        assert args.q == ["x", "y"]

    def test_defaults(self):
        args = parse_args(["--q", "x"])
        assert args.type == "不限"
        assert args.sort == "综合"
        assert args.time == "不限"

    def test_invalid_type_rejected(self):
        import pytest
        with pytest.raises(SystemExit):
            parse_args(["--q", "x", "--type", "随便"])

    def test_invalid_sort_rejected(self):
        import pytest
        with pytest.raises(SystemExit):
            parse_args(["--q", "x", "--sort", "随便"])

    def test_invalid_time_rejected(self):
        import pytest
        with pytest.raises(SystemExit):
            parse_args(["--q", "x", "--time", "随便"])
