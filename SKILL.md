---
name: zhihu-research
description: 知乎调研 — 搜索问答/专栏，抓取回答/评论/回复，CDP Chrome 自动化（反爬）
---

# 知乎 Research

知乎搜索、问题/专栏详情、回答、评论与回复抓取，基于 CDP 裸 Chrome + 人类行为模拟规避反爬。输出 stdout JSON。

## 安装

1. 确认 Chrome 已安装（`C:\Program Files\Google\Chrome\Application\chrome.exe`）
2. 安装依赖：`pip install -r requirements.txt`（仅 `websocket-client`）
3. 启动 Chrome 会话：`python chrome_session.py --start`，在弹出的 Chrome 中登录知乎（扫码/手机号）；登录态持久保存，`--status` 查看、`--stop` 关闭

profile 位于 `%USERPROFILE%\.yzdpw_state\chrome-cdp`，CDP 端口记录于同目录 `.cdp_port`。Chrome 用 `--remote-debugging-port` 裸启动（不带 `--enable-automation`），`navigator.webdriver=false`。

**并行与反爬**：`--parallel N`（默认 2）并行时会移除串行限速、请求频率更高，风控风险上升；`--parallel 1` 时保留人类行为模拟（随机滚动/停顿/限速）。

## 输出落盘

两个脚本 stdout 输出完整 JSON 的同时，自动把**同一份完整结果**写入 `<ZHIHU_LOGS_DIR>/<脚本名>-<时间戳>.json`（默认 `logs/`，即**脚本启动时的工作目录**下，与 ieee/wanfang 一致），并在 stderr 与 stdout 的 `logPath` 字段给出绝对路径。

用途：宿主对 stdout 有大小上限，结果条数一多会被截断；需要全文时直接读该文件。文件内容不含 `logPath`，是纯结果；落盘发生在写 stdout **之前**，stdout 即使失败也不丢结果。

## 命令

### zhihu_search.py

搜索问答/专栏，支持类型/排序/时间筛选。

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--q` | ✅ | — | 搜索关键词（可重复） |
| `--rows` | ❌ | 25 | 最大结果数（1-100） |
| `--type` | ❌ | 不限 | 不限 / 回答 / 文章 |
| `--sort` | ❌ | 综合 | 综合 / 最多赞同 / 最新发布 |
| `--time` | ❌ | 不限 | 不限 / 一天内 / 一周内 / 一月内 / 三月内 / 半年内 / 一年内 |
| `--parallel` | ❌ | 2 | 并行关键词数（1-8） |

**输出：** `{ filters, count, results, logPath }`，每条 result 含 `keyword`, `total`, `items[{ title, link, type, description, votes, comments }]`，`type` 为 `question`（问题）或 `column`（专栏）。

- 无结果 `notice: "无搜索结果"`；登录失效 `error: "需要登录"`；风控 `error: "触发风控/验证，请稍后再试"`
- 参数校验：`--q` 必填；筛选值非法直接退出

### zhihu_detail.py

抓问题详情，含回答、回答下的评论与回复。

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--url` | ⚠️ | — | 问题/专栏 URL（可重复）；与 stdin 管道二选一，至少一个 |
| `--max-answers` | ❌ | 15 | 每问题最大回答数（1-80） |
| `--max-comments` | ❌ | 15 | 每条回答最大评论数 |
| `--parallel` | ❌ | 2 | 并行 URL 数（1-8） |

**输出：** `{ count, succeeded, failed, questions, columns, logPath }`。

- question：`url, type, title, total, answers[{ author, content, votes, commentCount, answerLink }], comments[{ author, content, date, location }]`
- column：`url, type, title, author, content`

**用法：**
```powershell
# 直接抓单个问题
python zhihu_detail.py --url "https://www.zhihu.com/question/2052812805796017840" --max-answers 5 --max-comments 10

# 搜索 → 详情（管道）
python zhihu_search.py --q "嵌入式" --rows 5 | python zhihu_detail.py --max-answers 10
```

**错误：** 无效 URL → `"无效 URL"`；登录失效 → `"需要登录"`；风控 → `"触发风控/验证"`

## 配置（环境变量，前缀 `ZHIHU_`）

脚本内置 `config.py` 集中配置，可用 `ZHIHU_*` 环境变量覆盖：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ZHIHU_RATE_LIMIT` | `8,18` | 请求限速范围(s)，逗号分隔；调低加快、调高更安全 |
| `ZHIHU_PARALLEL_LIMIT` | `2` | 并行上限 |
| `ZHIHU_HUMAN_SCROLL_DELTA` | `150,500` | 滚动步长范围(px) |
| `ZHIHU_HUMAN_PAUSE` | `0.5,4` | 滚动停顿范围(s) |
| `ZHIHU_HUMAN_MOUSE_PROB` | `0.4` | 鼠标移动概率 |
| `ZHIHU_TIMEOUT_CDP` | `30` | CDP WebSocket 超时(s) |
| `ZHIHU_STATE_DIR` | `~/.yzdpw_state` | 状态目录（profile + 端口文件） |
| `ZHIHU_TABS_MAX` | `30` | tab 数上限，达到即**只告警**（不自动关 tab；0=不启用） |
| `ZHIHU_LOGS_DIR` | `<运行目录>/logs` | 完整结果落盘目录（默认 = 脚本启动时的工作目录，可覆盖为固定路径） |

示例：`set ZHIHU_RATE_LIMIT=3,5` 后运行脚本，限速从 8-18s 调为 3-5s。

## 已知限制

- 每个关键词/URL 用一个 tab，用完即关（`close_page`）；tab 总数达 `ZHIHU_TABS_MAX` 时**只告警不自动关** —— 四个 skill 共用一个 Chrome，自动关"空 tab"会误伤其他任务刚建好、还没 navigate 的 tab
- 若只剩最后一个 tab：`close_page` 会先建一个空白页（about:blank）占位再关它 —— 直接关会让整个共享 Chrome 退出，不关又会留下上次的搜索结果/详情页
- 知乎新版问答页只渲染首屏回答，脚本用 answers API 分页补齐
- 弹窗回复只抓每个回答评论区里第一个「查看全部」的评论（评论区若有多个 >5 条回复的评论，只处理第一个的弹窗，其余只抓折叠前可见的回复）

## 脚本清单

```
scripts/
  zhihu_search.py     搜索（关键词 + 类型/排序/时间筛选）
  zhihu_detail.py     详情 + 回答 + 评论/回复
  zhihu_parser.py     结果后处理纯函数
  cdp_base.py         CDP 客户端 + Chrome 裸启动 + 人类行为模拟
  chrome_session.py   登录会话管理（--start/--status/--stop）
```
