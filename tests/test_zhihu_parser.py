# tests/test_zhihu_parser.py
from zhihu_parser import tidy_zh_result, clean_votes, html_to_text, is_zhihu_error_page

class TestCleanVotes:
    def test_comma_separated(self):
        assert clean_votes("1,234") == "1234"
    def test_plain(self):
        assert clean_votes("42") == "42"
    def test_invalid(self):
        assert clean_votes("赞同") == "0"
    def test_wan_preserved(self):
        # 知乎用"万"不用"k"：'1.2万' 原样保留
        assert clean_votes("1.2万") == "1.2万"
    def test_wan_integer(self):
        assert clean_votes("3万") == "3万"
    def test_wan_with_space_prefix(self):
        # 页面按钮原文 '赞同 8.6 万'（数字与"万"间有空格）→ 归一 '8.6万'
        assert clean_votes("赞同 8.6 万") == "8.6万"
    def test_wan_with_comment_label(self):
        # commentCount 保留原文（如 '1.2万条评论'）后同样规范化
        assert clean_votes("1.2万条评论") == "1.2万"

class TestTidyZhResult:
    def test_keeps_valid(self):
        r = tidy_zh_result({"title": "T", "link": "https://www.zhihu.com/question/1", "votes": "1,000"})
        assert r["votes"] == "1000"
        assert r["type"] == "question"
    def test_column_type_detected(self):
        r = tidy_zh_result({"title": "T", "link": "https://www.zhihu.com/p/123"})
        assert r["type"] == "column"
    def test_video_type_detected(self):
        r = tidy_zh_result({"title": "V", "link": "https://www.zhihu.com/zvideo/123"})
        assert r["type"] == "video"
    def test_missing_link_dropped(self):
        assert tidy_zh_result({"title": "T"}) is None

class TestHtmlToText:
    def test_strips_tags_and_br(self):
        assert html_to_text("<p>你好</p><br>世界<img src=\"x\">") == "你好\n世界"
    def test_block_boundaries_newline(self):
        assert html_to_text("<p>第一段</p><p>第二段</p>") == "第一段\n第二段"
    def test_entities_decoded(self):
        assert html_to_text("&amp; &lt;tag&gt;") == "& <tag>"
    def test_empty(self):
        assert html_to_text("") == ""
        assert html_to_text(None) == ""

class TestIsZhihuErrorPage:
    def test_error_page_detected(self):
        assert is_zhihu_error_page("请求错误无法访问当前页面去往首页") is True
        assert is_zhihu_error_page("无法访问当前页面") is True

    def test_normal_page_not_error(self):
        assert is_zhihu_error_page("机器学习 - 搜索结果 - 知乎") is False

    def test_empty_or_none_not_error(self):
        assert is_zhihu_error_page("") is False
        assert is_zhihu_error_page(None) is False
