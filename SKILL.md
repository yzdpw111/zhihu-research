---
name: zhihu-research
description: 知乎调研 — 搜索问答/专栏，抓取回答/评论/回复，CDP Chrome 自动化（反爬）
---

# 知乎 Research

知乎搜索（问答/专栏）、问题详情（回答 + 评论 + 回复）、专栏正文。基于 CDP 裸 Chrome + 人类行为模拟规避反爬。

结果 **输出到 stdout（JSON）**，同时**落盘到文件**（stdout 有大小上限，需要全文时读落盘文件）。

## 快速开始（首次使用）

前置：**Windows** / **Google Chrome** / **Python 3.9+** / 能访问 `zhihu.com`

```powershell
# 1) 装依赖（requirements.txt 在 scripts/ 下）
pip install -r scripts/requirements.txt

# 2) 启动专用 Chrome 并登录知乎（登录态持久保存，只需做一次）
python scripts/chrome_session.py --start
#    → 在弹出的 Chrome 里扫码 / 手机号登录
python scripts/chrome_session.py --status     # 应看到: CDP 在线: ... (port 9222)

# 3) 跑第一条搜索
python scripts/zhihu_search.py --q "嵌入式" --rows 5 --parallel 1

# 4) 用完可关闭 Chrome（登录态保留在 profile 里，下次不用重登）
python scripts/chrome_session.py --stop
```

三条约定：

- **所有命令都在仓库根目录执行**（脚本路径写成 `scripts/xxx.py`）。
- **日志落在"当前工作目录"的 `logs/`**：在仓库根跑 → `<repo>/logs/`；在 `scripts/` 里跑 → `scripts/logs/`。可用 `ZHIHU_LOGS_DIR` 固定。
- `--status` 只证明 **Chrome/CDP 在线**，**不显示登录态**。确认登录最可靠的办法是跑一次搜索——返回结果即已登录，返回 `"需要登录"` 就重新执行第 2 步。

## 运行前提与约定

| 项 | 说明 |
|---|---|
| Chrome | 按 `Program Files` → `Program Files (x86)` → `%LOCALAPPDATA%` 顺序查找；都没找到会报 `Chrome 未找到` |
| profile / 端口 | profile 在 `%USERPROFILE%\.yzdpw_state\chrome-cdp`；CDP 端口 9222-9299，端口号写在同目录 `.cdp_port` |
| 四个 skill 共用 | xhs / zhihu / ieee / wanfang **共用这一个 Chrome 和 profile**，登录一次四个都能用 |
| 自动启动 | 脚本会**自动启动或复用** Chrome（不必先 `--start`）；但**登录态**必须先 `--start` 登录一次并持久保存 |
| Chrome 启动方式 | 裸启动：`--remote-debugging-port` + `--remote-allow-origins=*`，**不带** `--enable-automation` → `navigator.webdriver=false` |
| 反爬 | `--parallel 1` 保留人类行为模拟（随机滚动/停顿、请求限速 8-18s）；`--parallel >1` 会**移除串行限速**，请求变密、风控风险上升 |
| 输出编码 | 脚本已把 stdout/stderr 强制为 UTF-8；GBK 终端里 emoji 可能显示成乱码，但**内容正确** |

## 命令

### zhihu_search.py

搜索问答 / 专栏，支持类型 / 排序 / 时间筛选。

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--q` | 必填 | — | 搜索关键词（可重复） |
| `--rows` | 可选 | 25 | 最大结果数（1-100） |
| `--type` | 可选 | 不限 | 不限 / 回答 / 文章 |
| `--sort` | 可选 | 综合 | 综合 / 最多赞同 / 最新发布 |
| `--time` | 可选 | 不限 | 不限 / 一天内 / 一周内 / 一月内 / 三月内 / 半年内 / 一年内 |
| `--parallel` | 可选 | 2 | 并行关键词数（1-8） |

**输出：** `{ filters, count, results, logPath }`，每条 result 含 `keyword`, `total`, `items[{ title, link, type, description, votes, comments }]`；`type` 为 `question`（问题）或 `column`（专栏）。

- 无结果 → `notice: "无搜索结果"`；登录失效 → `error: "需要登录"`；风控 → `error: "触发风控/验证，请稍后再试"`
- 参数校验：`--q` 必填；筛选值非法直接退出

### zhihu_detail.py

抓问题详情（回答 + 每条回答下的评论）或专栏正文。

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--url` | 二选一 | — | 问题 / 专栏 URL（可重复）；与 stdin 管道二选一，至少给一个 |
| `--max-answers` | 可选 | 15 | 每问题最大回答数（1-80） |
| `--max-comments` | 可选 | 15 | 每条回答最大评论数 |
| `--parallel` | 可选 | 2 | 并行 URL 数（1-8） |

**输出：** `{ count, succeeded, failed, questions, columns, logPath }`

- question：`url, type, title, total, answers[{ author, content, votes, commentCount, answerLink }], comments[{ author, content, date, location }]`
- column：`url, type, title, author, content`

**用法：**

```powershell
# 直接抓单个问题
python scripts/zhihu_detail.py --url "https://www.zhihu.com/question/2052812805796017840" --max-answers 5 --max-comments 10

# 直接抓专栏
python scripts/zhihu_detail.py --url "https://zhuanlan.zhihu.com/p/2073468760233603768"

# 搜索 → 详情（管道；stdout 直接喂给下一个脚本）
python scripts/zhihu_search.py --q "嵌入式" --rows 5 --parallel 1 | python scripts/zhihu_detail.py --max-answers 10
```

**错误：** 无效 URL → `"无效 URL"`；登录失效 → `"需要登录"`；风控 → `"触发风控/验证"`

## 输出落盘

两个脚本在写 stdout 的**同时**，把**同一份完整结果**写入：

```
<ZHIHU_LOGS_DIR>/<脚本名>-<时间戳>.json      # 默认 <运行目录>/logs/
```

绝对路径会同时出现在 **stderr** 和 **stdout 的 `logPath` 字段**。

- 文件里**不含** `logPath`，是纯结果。
- **落盘发生在写 stdout 之前** → stdout 被截断或失败也不丢结果。
- 读取方式：

```powershell
$f = (Get-Content .\logs\zhihu_search-*.json | Select-Object -Last 1)   # 或直接用 logPath
Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json | Select-Object count
```

## 配置（环境变量，前缀 `ZHIHU_`）

`scripts/config.py` 集中配置，`ZHIHU_*` 环境变量覆盖（键名 = 前缀 + 点号转下划线大写）。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ZHIHU_RATE_LIMIT` | `8,18` | 请求限速范围(s)；调低加快、调高更安全 |
| `ZHIHU_PARALLEL_LIMIT` | `2` | `--parallel` 的默认值 |
| `ZHIHU_TABS_MAX` | `30` | tab 数上限，达到即**只告警**（不自动关 tab；`0`=不启用） |
| `ZHIHU_HUMAN_SCROLL_DELTA` | `150,500` | 滚动步长范围(px) |
| `ZHIHU_HUMAN_PAUSE` | `0.5,4` | 滚动停顿范围(s) |
| `ZHIHU_HUMAN_MOUSE_PROB` | `0.4` | 鼠标移动概率 |
| `ZHIHU_TIMEOUT_CDP` | `30` | CDP WebSocket 超时(s) |
| `ZHIHU_TIMEOUT_CHROME_START` | `30` | 自动启动 Chrome 的等待上限(s) |
| `ZHIHU_STATE_DIR` | `~/.yzdpw_state` | 状态目录（profile + 端口文件） |
| `ZHIHU_LOGS_DIR` | `<运行目录>/logs` | 结果落盘目录（可设成固定路径） |

设置方式（两种 shell 都给，别混用）：

```powershell
# PowerShell
$env:ZHIHU_RATE_LIMIT = "3,5"; python scripts/zhihu_search.py --q "嵌入式" --rows 5
```
```cmd
:: CMD
set ZHIHU_RATE_LIMIT=3,5
python scripts\zhihu_search.py --q "嵌入式" --rows 5
```

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `error: "需要登录"` | 登录态失效 → `python scripts/chrome_session.py --start` 重新登录（`--status` 看不出登录态） |
| `error: "触发风控/验证，请稍后再试"` | 请求太密 → 冷却几分钟，去掉并行（`--parallel 1`）、调高 `ZHIHU_RATE_LIMIT` |
| `Chrome 未找到` | Chrome 没装或装在别处 → 安装 Chrome，或在 `scripts/cdp_base.py` 的 `CHROME_PATHS` 里加路径 |
| `Chrome 30 秒内未就绪` | 启动超时 → 检查是否有 Chrome 弹窗/杀软拦截，重试；或调高 `ZHIHU_TIMEOUT_CHROME_START` |
| `9222-9299 端口全部被占` | 端口耗尽 → 关掉多余的调试用 Chrome |
| 报 `WebSocket 连接失败` / 连不上 CDP | Chrome 已退出 → 直接重跑脚本，会自动拉起 |
| 浏览器里留着上次的搜索/详情页 | 历史残留 tab → 正常留 1 个空白占位页；手动关掉即可 |
| 结果被截断（stdout 只看到一部分） | 宿主对 stdout 有大小上限 → 读 `logPath` 指向的文件 |
| emoji 显示成乱码/问号 | GBK 终端渲染问题 → 内容正确，用 `Get-Content -Raw -Encoding UTF8` 读文件 |
| 回答数比预期少 | `--max-answers` 默认 15（上限 80）→ 显式调大；评论区条数由 `--max-comments` 控制 |
| 专栏被当成问题解析 / 反之 | 用 `zhuanlan.zhihu.com/p/...` 抓专栏、`zhihu.com/question/...` 抓问题；`zhihu_search` 结果的 `link` 直接可用 |

## 已知限制

- **tab 管理**：每个关键词/URL 用一个 tab，用完即关（`close_page`）。tab 总数达 `ZHIHU_TABS_MAX` 时**只告警不自动关**——四个 skill 共用一个 Chrome，自动关"空 tab"会误伤其他任务刚建好、还没 navigate 的 tab。
- **最后一个 tab**：`close_page` 会先建一个 `about:blank` 占位页再关它（直接关会让整个共享 Chrome 退出，不关又会留下上次的页面）；占位建不出来时保留该 tab。
- **回答懒加载**：知乎新版问答页只渲染首屏回答，脚本改用 answers API 分页补齐；因此**回答顺序可能与页面显示不同**。
- **回复抓取有限**：弹窗回复只处理每个回答评论区里**第一个**「查看全部」的评论；若有多条评论都超过 5 条回复，其余只抓折叠前可见的回复。
- **依赖站点结构**：选择器依赖知乎当前页面结构，站点改版可能让某条路径静默失效。

## 脚本与测试

```
scripts/
  zhihu_search.py     搜索（关键词 + 类型/排序/时间筛选）
  zhihu_detail.py     详情 + 回答 + 评论/回复（问题与专栏）
  zhihu_parser.py     结果后处理纯函数
  cdp_base.py         CDP 客户端 + Chrome 启动 + 人类行为模拟 + tab 生命周期 + 落盘
  chrome_session.py   登录会话管理（--start/--status/--stop）
  config.py           集中配置（可用 ZHIHU_* 覆盖）
  requirements.txt    依赖清单

tests/                离线单测（不需要 Chrome / 网络）
```

跑测试（不需要登录）：

```powershell
pip install pytest
python -m pytest tests -q
```
