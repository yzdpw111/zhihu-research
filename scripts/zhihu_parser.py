#!/usr/bin/env python3
"""zhihu_parser.py — 知乎结果后处理纯函数"""
import html
import re


def clean_votes(raw):
    """赞同数清洗：'1.2万' → '1.2万'、'1,234' → '1234'，无效回退 '0'。

    知乎按中文习惯显示计数：带"万"单位时原样保留（如 '1.2万'），
    页面上按钮文本可能是 '赞同 8.6 万'（数字与"万"间有空格），
    统一归一为无空格 '8.6万'；千分位逗号去除；空/纯文本等无效值回退 '0'。
    """
    if not raw:
        return "0"
    m = re.search(r"([\d,]+\.?\d*)\s*万?", str(raw).strip())
    if not m:
        return "0"
    num = m.group(1).replace(",", "")
    if "万" in m.group(0):
        num += "万"
    return num


def html_to_text(raw):
    """知乎回答 API 的 content 是 HTML（含 <p>/<img>/<br> 等），转为纯文本。

    - <br>、</p> 等块级边界换行
    - 去其余标签（图片丢弃，与 DOM textContent 抓取的行为一致）
    - HTML 实体解码；空/无效回退 ''
    """
    if not raw:
        return ""
    s = str(raw)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(?:p|div|li|h[1-6]|blockquote|pre|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def tidy_zh_result(raw):
    """单条搜索结果清洗；无效返回 None"""
    title = (raw.get("title") or "").strip()
    link = raw.get("link") or ""
    if not title or not link:
        return None
    return {
        "title": title,
        "link": link,
        "type": "column" if "/p/" in link else ("video" if "/zvideo/" in link else "question"),
        "description": (raw.get("description") or "").strip()[:300],
        "votes": clean_votes(raw.get("votes")),
        "comments": clean_votes(raw.get("comments")),
    }


def is_zhihu_error_page(body):
    """知乎错误页特征：请求错误 / 无法访问当前页面（无效 URL 时出现）"""
    if not body:
        return False
    return "请求错误" in body or "无法访问当前页面" in body
