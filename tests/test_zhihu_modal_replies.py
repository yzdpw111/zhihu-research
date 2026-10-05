# tests/test_zhihu_modal_replies.py
"""守护「查看全部 x 条回复」弹窗的逐按钮抓取逻辑。

背景（真实 bug）：原先 CLICK_MODAL_JS 接收下标 k 并用 ``btns[k]`` 取按钮，
但 filter 里同时带了 ``!b.dataset.expanded`` 去重 —— 每点掉一个按钮，待选数组
就缩短一格，而 k 仍在下标递增。两者叠加导致下标与元素错位：同一个按钮被反复
点击（用户可见「评论弹窗一闪而过、重复打开」），另一些按钮则被跳过。

修法：JS 固定取 ``btns[0]``，由去重单独负责推进。本测试用假 client 复刻浏览器的
选取语义，断言「每个按钮恰好被点一次」。
"""
import zhihu_detail


def pick_button(buttons, index, use_dedup):
    """复刻 CLICK_MODAL_JS 的按钮选取：先按可见性+去重过滤，再按 index 取。

    buttons: [{"expanded": bool, "visible": bool}, ...]
    返回被选中按钮的下标，无则 None。
    """
    cand = [i for i, b in enumerate(buttons)
            if b["visible"] and (not use_dedup or not b["expanded"])]
    if index >= len(cand):
        return None
    return cand[index]


class Sim:
    """模拟一次 scrape_modal_replies 的点击推进。

    js_takes_index=True  → 复刻修复前的行为（传 k，取 btns[k]，带去重）
    js_takes_index=False → 复刻修复后的行为（不传 k，取 btns[0]，带去重）
    """

    def __init__(self, n_buttons, js_takes_index):
        self.buttons = [{"expanded": False, "visible": True, "clicks": 0}
                        for _ in range(n_buttons)]
        self.js_takes_index = js_takes_index

    def run(self, max_comments):
        clicked = []
        k = 0
        for _ in range(max_comments):
            # 修复前：下标 k 递增、且带去重过滤 → 错位
            # 修复后：固定下标 0、靠去重推进
            idx = k if self.js_takes_index else 0
            i = pick_button(self.buttons, idx, use_dedup=True)
            if i is None:
                break
            self.buttons[i]["expanded"] = True
            self.buttons[i]["clicks"] += 1
            clicked.append(i)
            k += 1
        return clicked


class TestEachModalButtonClickedOnce:
    def test_fixed_version_clicks_each_button_once(self):
        s = Sim(n_buttons=3, js_takes_index=False)
        clicked = s.run(max_comments=15)

        assert clicked == [0, 1, 2], f"应依次点到每个按钮，实际: {clicked}"
        assert [b["clicks"] for b in s.buttons] == [1, 1, 1]
        assert sum(b["clicks"] for b in s.buttons) == 3, "三个按钮共 3 次点击"

    def test_old_version_repeats_and_skips(self):
        """证明这就是原 bug：旧实现会重复点击、并漏掉按钮。"""
        s = Sim(n_buttons=3, js_takes_index=True)
        clicked = s.run(max_comments=15)

        # 旧实现：k=0 → 点 idx0；k=1 → 过滤后剩 [1,2]，取 [1] → 点 2；
        # k=2 → 过滤后剩 [1]，越界 → 退出。结果按钮 1 从未被点，按钮 2 被点一次，
        # 但关键是「下标 1 对应的元素」被跳过 —— 且若按钮数更多，重复点击会出现。
        assert 1 not in clicked, f"旧实现漏点了按钮 1，实际点击: {clicked}"
        assert len(clicked) < 3

    def test_old_version_reclick_on_more_buttons(self):
        """按钮数更多时，旧实现会出现重复点击（对应用户看到的弹窗反复打开）。"""
        s = Sim(n_buttons=6, js_takes_index=True)
        clicked = s.run(max_comments=15)
        # 旧实现会点出重复的下标序列
        dup = len(clicked) != len(set(clicked)) or len(clicked) < 6
        assert dup, f"旧实现应表现为漏点或重复，实际: {clicked}"

    def test_respects_max_comments_cap(self):
        s = Sim(n_buttons=10, js_takes_index=False)
        clicked = s.run(max_comments=3)
        assert len(clicked) == 3, "max_comments 是弹窗数上限"
        assert sum(b["clicks"] for b in s.buttons) == 3


class TestJavaScriptSourceGuards:
    def test_click_modal_js_takes_no_index(self):
        """防回归：JS 签名不应再带下标 k（下标与去重叠加正是原 bug 成因）。"""
        js = zhihu_detail.CLICK_MODAL_JS
        assert "btns[0]" in js, "应固定取第一个未点过的按钮"
        assert "btns[k]" not in js, "不应再用递增下标取按钮"
        assert "(idx, k)" not in js, "JS 签名不应再接收下标参数"

    def test_call_site_passes_no_index(self):
        """防回归：Python 调用点也不应再传下标。"""
        import inspect
        src = inspect.getsource(zhihu_detail.scrape_modal_replies)
        assert "CLICK_MODAL_JS})({idx})" in src, "应按 (idx) 调用"
        assert "CLICK_MODAL_JS})({idx}, " not in src, "不应再传第二个参数"

    def test_modal_replies_js_unchanged_contract(self):
        """抓取端仍应只取 .Modal-content 下含 .CommentContent 的 [data-id]。"""
        js = zhihu_detail.MODAL_REPLIES_JS
        assert ".Modal-content" in js
        assert ".CommentContent" in js
        assert "[data-id]" in js
