#!/usr/bin/env python3
"""
zhihu_detail.py — 知乎问题/专栏详情 CLI

用法:
  python zhihu_detail.py --url "https://www.zhihu.com/question/613083643" --max-answers 15
  python zhihu_search.py --q 嵌入式 --rows 5 | python zhihu_detail.py --max-answers 10
输出: stdout JSON { count, succeeded, failed, questions?, columns? }

注意:
  - 提取 JS 均为参数化箭头函数，调用时用 IIFE 传参 `client.evaluate(f"({JS})(args)")`
    （不要用 % 字符串格式化注入参数——JS 内含正则 % 等会被误解析）
  - 问题页滚动加载：知乎是无限滚动，滚动到底自动加载更多回答，
    用 .List-item .RichText 计数做停止条件，计数不增长即视为到底
  - 输入支持 --url（单值）或 stdin 管道（zhihu_search.py 输出，含 BOM 容忍）
"""
import argparse
import json
import re
import sys
import time

from cdp_base import (CdpError, close_page, create_page, ensure_cdp, human_scroll,  # noqa: E402
                      setup_stdout, write_log)
from config import get
from zhihu_parser import clean_votes, html_to_text, is_zhihu_error_page

MAX_ANSWERS = 80
# 知乎页面回答流单页存在自然上限（服务端分页/懒加载，单页约数十条），
# MAX_HARD_CAP 作为滚动加载硬上限，防止无限滚动循环失控
MAX_HARD_CAP = 100
QUESTION_RE = re.compile(r"/question/(\d+)")

# ── 提取 JS（参数化，IIFE 调用）──────────────────────────
QUESTION_JS = """(max) => {
  const items = Array.from(document.querySelectorAll('.List-item')).filter(item =>
    item.querySelector('.RichText') && item.querySelector('[class*="AuthorInfo-name"]'));
  return {
    title: (document.querySelector('.QuestionHeader-title') || {}).textContent || '',
    total: items.length,
    answers: items.slice(0, max).map(item => {
      const a = item.querySelector('[class*="AuthorInfo-name"]');
      const c = item.querySelector('.RichText');
      let votes = '0';
      const vb = item.querySelector('button[aria-label*="赞同"]') || item.querySelector('[class*="VoteButton--up"]');
      if (vb) votes = (vb.textContent || '').trim();  // 保留原文（可能含"万"），Python 侧 clean_votes 规范化
      let cc = '0';
      item.querySelectorAll('button').forEach(b => {
        const t = (b.textContent || '').trim();
        if (t.includes('条评论')) cc = t;  // 保留原文，Python 侧 clean_votes 规范化
      });
      return {
        author: a ? a.textContent.trim().slice(0, 30) : '',
        content: c ? c.textContent.trim() : '',
        votes, commentCount: cc,
        answerLink: (item.querySelector('a[href*="/answer/"]') || {}).href || '',
      };
    }),
  };
}"""

COLUMN_JS = """() => ({
  title: (document.querySelector('.Post-Title, h1.Post-Title') || {}).textContent || '',
  author: (document.querySelector('[class*="AuthorInfo-name"]') || {}).textContent || '',
  content: (document.querySelector('.Post-RichTextContainer, .RichText') || {}).textContent || '',
})"""

# 展开"查看全部/阅读全文/展开"按钮（问题描述折叠 + 回答内容折叠）。
# 纯函数式：返回点击计数；已点击按钮打 dataset.expanded 标记去重，
# 防止滚动循环中重复点击把已展开内容重新收起（参照 xhs_detail.EXPAND_REPLIES_JS）
EXPAND_JS = """() => {
  let clicked = 0;
  document.querySelectorAll('button').forEach(b => {
    const t = (b.textContent || '').trim();
    if ((t.includes('查看全部') || t.includes('阅读全文') || t.includes('展开')) &&
        !b.dataset.expanded && b.offsetParent !== null) {
      b.dataset.expanded = '1';
      b.click();
      clicked++;
    }
  });
  return clicked;
}"""

# 提取当前回答评论区顶级评论（作者/内容/日期/位置）。
# 知乎评论/回复都是 div[data-id] 结构：作者=img.Avatar 的 alt、
# 内容=.CommentContent、日期=span.css-12cl38p、位置=span.css-ntkn7q。
# filter 只保留含 .CommentContent 的评论项（嵌套层级深的回复项也可能含 .CommentContent，按任务要求不做排除）
COMMENT_JS = """(idx, max) => {
  const containers = document.querySelectorAll('.Comments-container');
  const container = containers.length > idx ? containers[idx] : document.querySelector('.Modal-content');
  if (!container) return [];
  const pick = (item) => {
    const author = (item.querySelector('img.Avatar') || {}).alt || '';
    const content = (item.querySelector('.CommentContent') || {}).textContent || '';
    const date = (item.querySelector('span.css-12cl38p') || {}).textContent || '';
    const location = (item.querySelector('span.css-ntkn7q') || {}).textContent || '';
    return { id: item.getAttribute('data-id') || '', author: author.trim(), content: content.trim(), date: date.trim(), location: location.trim() };
  };
  // 顶级评论：container 下的 [data-id]（含 .CommentContent）
  const items = Array.from(container.querySelectorAll('[data-id]'))
    .filter(it => it.querySelector('.CommentContent'))
    .slice(0, max);
  return items.map(pick);
}"""

# 点击第 i 个回答（.List-item）上可见的「x 条评论」按钮，加载其评论区
CLICK_COMMENT_JS = """(i) => {
  const item = document.querySelectorAll('.List-item')[i];
  if (!item) return 'no-item';
  const btn = Array.from(item.querySelectorAll('button')).find(b => /条评论/.test(b.textContent) && b.offsetParent !== null);
  if (btn) { btn.click(); return 'clicked'; }
  return 'no-btn';
}"""

# 展开回复折叠：「展开其他 x 条回复」（3-5 条内联展开）。
# 「查看全部 x 条回复」（>5 条弹窗）不在此点击，由 CLICK_MODAL_JS 单独处理（见 scrape_modal_replies）
EXPAND_REPLY_JS = """() => {
  let clicked = 0;
  document.querySelectorAll('.Comments-container button, .Modal-content button').forEach(b => {
    const t = (b.textContent || '').trim();
    if (t.includes('展开其他') && t.includes('回复') && b.offsetParent !== null && !b.dataset.expanded) {
      b.dataset.expanded = '1';
      b.click(); clicked++;
    }
  });
  return clicked;
}"""

# 点击第 idx 个评论区里第 k 个可见的「查看全部 x 条回复」按钮（>5 条弹窗），
# 返回其所属父评论的 data-id（无按钮时返回 null）。点击后弹窗由 MODAL_REPLIES_JS 抓取。
# 已点过的按钮用 dataset.expanded 标记去重，支持一个评论区多个弹窗逐个抓取
CLICK_MODAL_JS = """(idx, k) => {
  const containers = document.querySelectorAll('.Comments-container');
  const container = containers.length > idx ? containers[idx] : document.querySelector('.Modal-content');
  if (!container) return null;
  const btns = Array.from(container.querySelectorAll('button'))
    .filter(b => /查看全部.*回复/.test(b.textContent) && b.offsetParent !== null && !b.dataset.expanded);
  const btn = btns[k];
  if (!btn) return null;
  btn.dataset.expanded = '1';
  const parent = btn.closest('[data-id]');
  const parentId = parent ? parent.getAttribute('data-id') : '';
  btn.click();
  return parentId;
}"""

# 提取「查看全部 x 条回复」弹窗（.Modal-content）里的回复（作者/内容/日期）
MODAL_REPLIES_JS = """() => {
  const modal = document.querySelector('.Modal-content');
  if (!modal) return [];
  const pick = (item) => ({
    author: (item.querySelector('img.Avatar') || {}).alt || '',
    content: (item.querySelector('.CommentContent') || {}).textContent || '',
    date: (item.querySelector('span.css-12cl38p') || {}).textContent || '',
  });
  return Array.from(modal.querySelectorAll('[data-id]'))
    .filter(it => it.querySelector('.CommentContent'))
    .map(pick);
}"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="知乎内容详情")
    ap.add_argument("--url", action="append", help="问题/专栏 URL（可重复）")
    ap.add_argument("--max-answers", type=int, default=15,
                    help=f"每问题最大回答数（默认 15，最大 {MAX_ANSWERS}）")
    ap.add_argument("--max-comments", type=int, default=15,
                    help="每条回答最大评论数（默认 15）")
    ap.add_argument("--parallel", type=int, default=None, help="并行任务数（默认 2，最大 8）")
    args = ap.parse_args(argv)
    args.max_answers = max(1, min(args.max_answers, MAX_ANSWERS))
    args.max_comments = max(1, args.max_comments)
    args.parallel = args.parallel if args.parallel is not None else get("parallel.limit")
    args.parallel = max(1, min(args.parallel, 8))
    # 无 --url 且 stdin 不是管道（tty）时直接报错退出，否则可能阻塞等待 stdin
    if not args.url and sys.stdin.isatty():
        ap.error("需要 --url 或 stdin 管道输入")
    return args


def parse_pipe_input(data):
    """解析 zhihu_search.py 的管道输出，返回去重后的 URL 列表。

    非法 JSON 或结构不符时返回 None（由 read_targets 转成友好报错）。
    """
    try:
        # PowerShell 管道会给 stdin 注入 UTF-8 BOM（\ufeff），需先剔除
        parsed = json.loads(data.lstrip("\ufeff").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    links = set()
    for g in parsed.get("results", []):
        if not isinstance(g, dict):
            continue
        items = g.get("items") or ([g] if g.get("link") else [])
        for r in items:
            if isinstance(r, dict) and r.get("link"):
                links.add(r["link"])
    return list(links)


def read_targets(args):
    if args.url:
        return args.url
    if sys.stdin.isatty():
        sys.stderr.write("需要 --url 或 stdin 管道输入\n")
        sys.exit(1)
    data = sys.stdin.read()
    links = parse_pipe_input(data)
    if links is None:
        sys.stderr.write("stdin 输入不是合法的 JSON（预期 zhihu_search.py 管道输出）\n")
        sys.exit(1)
    return links


ANSWER_API_JS = """(qid, offset) => (async () => {
  const u = `/api/v4/questions/${qid}/answers?include=data%5B*%5D.content%2Cauthor.name%2Cvoteup_count%2Ccomment_count%2Cid&limit=20&offset=${offset}&platform=desktop&sort_by=default`;
  const resp = await fetch(u, {headers: {'x-requested-with': 'fetch'}});
  const j = await resp.json();
  const arr = j.data || [];
  return {
    status: resp.status,
    isEnd: !!(j.paging && j.paging.is_end),
    total: (j.paging && j.paging.totals) || null,
    items: arr.map(d => ({
      id: d.id,
      author: d.author && d.author.name,
      content: d.content || '',
      votes: d.voteup_count,
      cc: d.comment_count,
    })),
  };
})()"""


def fetch_answers_via_api(client, question_id, max_answers):
    """通过知乎 answers API 分页抓取回答（页面上下文 fetch，复用登录 cookie）。

    知乎新版网页版问答页只渲染前几条推荐回答，滚动不再加载更多；
    回答全量数据需走 /api/v4/questions/{id}/answers 分页。逐页 offset 递增，
    直到凑满 max_answers 或 paging.is_end。失败返回 (None, None)。
    """
    answers, total = [], None
    for offset in range(0, max_answers, 20):
        try:
            # question_id 以字符串传入：长 id（如 2052812805796017840）超过 JS 安全整数
            # 上限（2^53），作数字传会丢精度导致 URL 指向错误问题而 404
            r = client.evaluate(
                f'({ANSWER_API_JS})("{question_id}", {offset})', await_promise=True) or {}
        except CdpError as e:
            sys.stderr.write(f"answers API 请求失败: {e}\n")
            return (None, total)
        if r.get("status") != 200 or not r.get("items"):
            sys.stderr.write(f"answers API 返回异常: status={r.get('status')}\n")
            break
        if r.get("total") is not None:
            total = r["total"]
        for it in r["items"]:
            answers.append({
                "author": (it.get("author") or "").strip()[:30],
                "content": html_to_text(it.get("content")),
                "votes": clean_votes(str(it.get("votes")) if it.get("votes") is not None else "0"),
                "commentCount": clean_votes(str(it.get("cc")) if it.get("cc") is not None else "0"),
                "answerLink": f"https://www.zhihu.com/question/{question_id}/answer/{it['id']}",
            })
        if r.get("isEnd") or len(answers) >= max_answers:
            break
    return (answers or None, total)


def _answer_key(answer):
    """answerLink 取 /answer/{id} 作去重键；无 id 时退化为 author+content 前缀"""
    m = re.search(r"/answer/(\d+)", answer.get("answerLink") or "")
    if m:
        return ("id", m.group(1))
    return ("txt", answer.get("author", "") + "|" + (answer.get("content") or "")[:40])


def scrape_question(client, url, max_answers, max_comments):
    m = QUESTION_RE.search(url)
    if not m:
        raise RuntimeError(f"不是有效问题 URL: {url[:80]}")
    clean_url = f"https://www.zhihu.com/question/{m.group(1)}"
    client.navigate(clean_url)
    time.sleep(2)
    for _ in range(30):
        try:
            body = client.get_body_text()[:500]
        except Exception:
            time.sleep(0.5)
            continue
        try:
            if client.evaluate("!!document.querySelector('.QuestionHeader-title')"):
                break
        except Exception:
            pass
        if "登录后查看" in body or "请先登录" in body:
            raise RuntimeError("需要登录")
        if "安全验证" in body or "验证码" in body:
            raise RuntimeError("触发风控/验证")
        if is_zhihu_error_page(body):
            raise RuntimeError("无效 URL")
        time.sleep(0.5)
    else:
        raise RuntimeError("问题页加载超时")
    time.sleep(2)

    # 滚动加载：知乎无限滚动，滚动到底自动加载更多回答；
    # 每滚动一轮后立即对本轮可见的回答执行展开点击（阅读全文/查看全部），
    # 已点按钮带 dataset.expanded 标记去重；计数达到上限或不再增长即停
    prev = 0
    for _ in range(10):
        human_scroll(client, max_rounds=2)
        time.sleep(1)
        # 展开是异步的：多轮点击直到无新增按钮
        for _ in range(3):
            n = client.evaluate(f"({EXPAND_JS})()") or 0
            if n == 0:
                break
            time.sleep(1.0)
        count = client.evaluate("document.querySelectorAll('.List-item .RichText').length")
        if count >= max_answers + 3 or count >= MAX_HARD_CAP:
            break
        if count == prev and _ > 2:
            break
        prev = count

    data = client.evaluate(f"({QUESTION_JS})({max_answers})") or {}
    # DOM 抓取的 content 也可能含知乎序列化的 <img> 标签字面量，
    # 统一过 html_to_text，与 API 补齐的纯文本输出格式一致
    dom_answers = [{
        "author": x.get("author", ""),
        "content": html_to_text(x.get("content")),
        "votes": clean_votes(x.get("votes")),
        "commentCount": clean_votes(x.get("commentCount")),
        "answerLink": x.get("answerLink", ""),
    } for x in (data.get("answers") or [])]

    # DOM 首屏抓取（可能含更全的展开全文）+ answers API 分页补齐：
    # 知乎新版问答页只渲染前几条，滚动不再加载更多 → 用 API 拿全量。
    # 按 answerLink 去重：DOM 优先（展开全文），API 只补 DOM 缺失的条目。
    answers, notice, api_total = dom_answers[:], "", None
    if len(answers) < max_answers:
        api_answers, api_total = fetch_answers_via_api(client, m.group(1), max_answers)
        if api_answers is None:
            notice = f"API 补齐失败，仅抓到首屏 {len(answers)} 条"
        else:
            seen = {_answer_key(a) for a in answers}
            for a in api_answers:
                key = _answer_key(a)
                if key not in seen:
                    seen.add(key)
                    answers.append(a)
                if len(answers) >= max_answers:
                    break

    total = api_total if api_total is not None else data.get("total", 0)
    result = {"url": clean_url, "type": "question",
              "title": data.get("title", ""), "total": total,
              "answers": answers}
    if notice:
        result["notice"] = notice
    try:
        comments = scrape_comments(client, max_answers, max_comments)
        sys.stderr.write(f"[debug] scrape_comments 完成，{len(comments)} 条\n")
    except Exception as e:
        sys.stderr.write(f"[debug] scrape_comments 异常: {str(e)[:200]}\n")
        comments = []
    if comments:
        result["comments"] = comments
    return result


def scrape_column(client, url):
    client.navigate(url)
    time.sleep(2)
    for _ in range(30):
        try:
            body = client.get_body_text()[:500]
        except Exception:
            time.sleep(0.5)
            continue
        try:
            if client.evaluate("!!document.querySelector('.Post-Title, .RichText')"):
                break
        except Exception:
            pass
        if "登录后查看" in body or "请先登录" in body:
            raise RuntimeError("需要登录")
        if "安全验证" in body or "验证码" in body:
            raise RuntimeError("触发风控/验证")
        if is_zhihu_error_page(body):
            raise RuntimeError("无效 URL")
        time.sleep(0.5)
    else:
        raise RuntimeError("专栏页加载超时")
    time.sleep(2)
    # 专栏正文懒加载：滚动几轮让全文渲染
    for _ in range(6):
        human_scroll(client, max_rounds=2)
    data = client.evaluate(f"({COLUMN_JS})()") or {}
    return {"url": url, "type": "column",
            "title": data.get("title", ""), "author": data.get("author", ""),
            "content": data.get("content", "")}


def scrape_modal_replies(client, idx, max_comments):
    """遍历第 idx 个评论区里的所有「查看全部」弹窗，逐个抓取，返回 {parent_id: replies}"""
    modal_map = {}
    for k in range(max_comments):
        parent_id = client.evaluate(f"({CLICK_MODAL_JS})({idx}, {k})")
        if not parent_id:
            break  # 没有更多弹窗按钮
        for _ in range(20):
            if client.evaluate("!!document.querySelector('.Modal-content')"):
                break
            time.sleep(0.5)
        replies = client.evaluate(f"({MODAL_REPLIES_JS})()") or []
        modal_map[parent_id] = replies
        client.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape"})
        client.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape"})
        time.sleep(0.5)
    return modal_map


def scrape_comments(client, max_answers, max_comments):
    """遍历所有回答，抓各自的评论"""
    all_comments = []
    for i in range(max_answers):
        clicked = client.evaluate(f"({CLICK_COMMENT_JS})({i})")
        if clicked == 'no-item':
            break  # 没有更多回答
        if clicked == 'no-btn':
            continue  # 这个回答没有评论，跳过
        # 轮询等评论区出现（内联 .Comments-container[i] 或弹窗 .Modal-content），且评论项加载完
        for _ in range(30):
            if client.evaluate(
                    f"(document.querySelectorAll('.Comments-container')[{i}] || document.querySelector('.Modal-content'))"
                    f" && (document.querySelectorAll('.Comments-container')[{i}] || document.querySelector('.Modal-content')).querySelectorAll('[data-id]').length > 0"):
                break
            time.sleep(0.5)
        else:
            break
        # 展开内联回复（3-5 条折叠）
        for _ in range(5):
            n = client.evaluate(f"({EXPAND_REPLY_JS})()") or 0
            if n == 0:
                break
            time.sleep(1.5)
        raw = client.evaluate(f"({COMMENT_JS})({i}, {max_comments})") or []
        # 处理该评论区的所有「查看全部」弹窗，弹窗回复合并到对应评论
        modal_map = scrape_modal_replies(client, i, max_comments)
        if modal_map:
            for c in raw:
                if c.get("id") in modal_map:
                    c["replies"] = modal_map[c["id"]]
        # 弹窗模式下，抓完评论后 ESC 关闭弹窗，避免影响下一个回答的评论展开
        if client.evaluate("!!document.querySelector('.Modal-content')"):
            client.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape"})
            client.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape"})
            time.sleep(0.5)
        all_comments.extend(raw)
    return all_comments


def _scrape_in_tab(port, u, args):
    """在独立 tab 中抓取单篇内容，返回 (u, result_or_error)。"""
    client = None
    try:
        client = create_page(port)
        if "/p/" in u:
            return u, scrape_column(client, u)
        return u, scrape_question(client, u, args.max_answers, args.max_comments)
    except Exception as e:
        return u, e
    finally:
        close_page(client)


def main():
    from concurrent.futures import ThreadPoolExecutor
    args = parse_args()
    urls = read_targets(args)
    if not urls:
        sys.stderr.write("没有可处理的 URL\n")
        sys.exit(1)
    port = ensure_cdp()
    limit = min(len(urls), args.parallel)
    questions, columns, failed = [], [], 0
    with ThreadPoolExecutor(max_workers=limit) as ex:
        for u, r in ex.map(_scrape_in_tab, [port] * len(urls), urls, [args] * len(urls)):
            if isinstance(r, Exception):
                failed += 1
                sys.stderr.write(f"失败: {u[:80]} — {str(r)[:100]}\n")
            else:
                if r.get("type") == "column":
                    columns.append(r)
                else:
                    questions.append(r)
                sys.stderr.write(f"完成: {u[:80]}\n")
    out = {"count": len(questions) + len(columns) + failed,
           "succeeded": len(questions) + len(columns), "failed": failed}
    if questions:
        out["questions"] = questions
    if columns:
        out["columns"] = columns
    # 先落盘再写 stdout：宿主对 stdout 有大小上限，Agent 拿全文直接读 logPath
    try:
        out["logPath"] = write_log(out, "zhihu_detail")
        sys.stderr.write(f"[zhihu-detail] 完整结果已落盘: {out['logPath']}\n")
    except Exception as e:
        sys.stderr.write(f"[zhihu-detail] 落盘失败: {str(e)[:80]}\n")
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    setup_stdout()
    main()
