#!/usr/bin/env python3
"""
zhihu_search.py — 知乎搜索 CLI

用法:
  python zhihu_search.py --q "嵌入式" --rows 25
输出: stdout JSON { count, results }
"""
import argparse
import json
import sys
import time
from urllib.parse import quote

from cdp_base import (close_page, create_page, ensure_cdp, human_scroll,  # noqa: E402
                      setup_stdout, write_log)
from config import get
from zhihu_parser import tidy_zh_result, is_zhihu_error_page

MAX_ROWS = 100

# 知乎搜索筛选 URL 参数映射（实测确认）
VERTICAL_MAP = {"回答": "answer", "文章": "article"}
SORT_MAP = {"最多赞同": "upvoted_count", "最新发布": "created_time"}
TIME_MAP = {"一天内": "a_day", "一周内": "a_week", "一月内": "a_month",
            "三月内": "three_month", "半年内": "half_a_year", "一年内": "a_year"}

EXTRACT_JS = """() => Array.from(document.querySelectorAll('.List-item'))
  .filter(card => card.querySelector('a[href*="/p/"], a[href*="/question/"], a[href*="/zvideo/"]'))
  .slice(0, %d)
  .map(card => {
    const titleEl = card.querySelector('.ContentItem-title, h2')
      || card.querySelector('a[href*="/zvideo/"]');
    const linkEl = card.querySelector('a[href*="/p/"], a[href*="/question/"], a[href*="/zvideo/"]');
    const descEl = card.querySelector('.RichText');
    let votes = '0';
    const vb = card.querySelector('button[aria-label*="赞同"]') || card.querySelector('[class*="VoteButton--up"]');
    if (vb) { const m = (vb.textContent || '').trim().match(/([\\d,]+)/); if (m) votes = m[1]; }
    let comments = '0';
    card.querySelectorAll('button').forEach(b => {
      const m = (b.textContent || '').trim().match(/(\\d+)\\s*条评论/);
      if (m) comments = m[1];
    });
    return {
      title: titleEl ? titleEl.textContent.trim() : '',
      link: linkEl ? linkEl.href : '',
      description: descEl ? descEl.textContent.trim().slice(0, 300) : '',
      votes, comments,
    };
  })"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="知乎搜索")
    ap.add_argument("--q", action="append", required=True, help="搜索关键词（可重复）")
    ap.add_argument("--rows", type=int, default=25, help=f"每关键词最大结果数（默认 25，最大 {MAX_ROWS}）")
    ap.add_argument("--parallel", type=int, default=None, help="并行任务数（默认 2，最大 8）")
    ap.add_argument("--type", choices=["不限", "回答", "文章"], default="不限", help="内容类型筛选")
    ap.add_argument("--sort", choices=["综合", "最多赞同", "最新发布"], default="综合", help="排序方式")
    ap.add_argument("--time", choices=["不限", "一天内", "一周内", "一月内", "三月内", "半年内", "一年内"], default="不限", help="发布时间筛选")
    args = ap.parse_args(argv)
    args.rows = max(1, min(args.rows, MAX_ROWS))
    args.parallel = args.parallel if args.parallel is not None else get("parallel.limit")
    args.parallel = max(1, min(args.parallel, 8))
    return args


def build_url(kw, args):
    url = f"https://www.zhihu.com/search?type=content&q={quote(kw)}"
    if args.type != "不限":
        url += f"&vertical={VERTICAL_MAP[args.type]}"
    if args.sort != "综合":
        url += f"&sort={SORT_MAP[args.sort]}"
    if args.time != "不限":
        url += f"&time_interval={TIME_MAP[args.time]}"
    return url


def search_one(client, kw, args):
    url = build_url(kw, args)
    client.navigate(url)
    state = "timeout"
    for _ in range(30):
        try:
            body = client.get_body_text()[:400]
        except Exception:
            time.sleep(0.5)
            continue
        try:
            if client.evaluate("!!document.querySelector('.List-item')"):
                state = "normal"
                break
        except Exception:
            pass  # 页面跳转/连接抖动：本轮未就绪，继续轮询
        if "没有找到" in body or "暂无相关内容" in body:
            state = "noresult"
            break
        if "登录后查看" in body:
            state = "login"
            break
        if "操作频繁" in body or "安全验证" in body or "验证码" in body:
            state = "risk"
            break
        if is_zhihu_error_page(body):
            state = "error"
            break
        time.sleep(0.5)
    if state == "noresult":
        return {"keyword": kw, "total": 0, "items": [], "notice": "无搜索结果"}
    if state == "login":
        return {"keyword": kw, "total": 0, "items": [], "error": "需要登录"}
    if state == "risk":
        return {"keyword": kw, "total": 0, "items": [], "error": "触发风控/验证，请稍后再试"}
    if state == "error":
        return {"keyword": kw, "total": 0, "items": [], "error": "无效 URL"}
    if state == "timeout":
        return {"keyword": kw, "total": 0, "items": [], "error": "页面加载超时"}
    time.sleep(2)
    for _ in range(5):
        human_scroll(client, max_rounds=2)
        count = client.evaluate(
            "document.querySelectorAll('.List-item a[href*=\\\"/p/\\\"], .List-item a[href*=\\\"/question/\\\"], .List-item a[href*=\\\"/zvideo/\\\"]').length")
        if count >= args.rows + 3 or count >= 100:
            break
    raw = client.evaluate(f"({EXTRACT_JS % args.rows})()") or []
    items = [t for r in raw if (t := tidy_zh_result(r))]
    return {"keyword": kw, "total": len(items), "items": items}


def _search_in_tab(port, kw, args):
    """在独立 tab 中搜索单个关键词，返回 (kw, result_or_error)。"""
    client = None
    try:
        client = create_page(port)
        return kw, search_one(client, kw, args)
    except Exception as e:
        return kw, e
    finally:
        close_page(client)


def main():
    from concurrent.futures import ThreadPoolExecutor
    args = parse_args()
    port = ensure_cdp()
    limit = min(len(args.q), args.parallel)
    results = []
    with ThreadPoolExecutor(max_workers=limit) as ex:
        for kw, r in ex.map(_search_in_tab, [port] * len(args.q), args.q, [args] * len(args.q)):
            if isinstance(r, Exception):
                results.append({"keyword": kw, "total": 0, "items": [], "error": str(r)[:200]})
                sys.stderr.write(f"[zhihu-search] {kw} 失败: {str(r)[:120]}\n")
            else:
                results.append(r)
                sys.stderr.write(f"[zhihu-search] {kw}: {r.get('total', 0)} 条\n")
            sys.stderr.flush()
    out = {"filters": {"type": args.type, "sort": args.sort, "time": args.time},
           "count": len(results), "results": results}
    # 先落盘再写 stdout：宿主对 stdout 有大小上限，Agent 拿全文直接读 logPath
    try:
        out["logPath"] = write_log(out, "zhihu_search")
        sys.stderr.write(f"[zhihu-search] 完整结果已落盘: {out['logPath']}\n")
    except Exception as e:
        sys.stderr.write(f"[zhihu-search] 落盘失败: {str(e)[:80]}\n")
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    setup_stdout()
    main()
