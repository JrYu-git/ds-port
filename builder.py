"""生成层：规范化消息 → 提示词。

核心判断：不能做成「总结剧情」。总结会把语气洗掉，新会话第一句就不像了。
保真靠三层：
  1. 设定卡      逐字原样
  2. 最近 N 轮   逐字原样
  3. 中间历史    一行一轮的时间线，保事件顺序，不保文采
"""

import re

# ---------------------------------------------------------------- 常量

# n-gram 过滤：以助词/代词/介词开头或结尾的都不算「惯用词」
STOP_EDGE = set("了他她你我着在把被就才又还都也很最一是不没这那的地得吗呢吧啊呀哦嗯")
STOP_ANY = set("你我他她")
STOP_PHRASES = {
    "的时候", "一下", "得很", "什么", "这个", "那个", "一个", "没有", "就是",
    "还是", "可以", "已经", "因为", "所以", "如果", "但是", "然后", "现在",
    "这样", "那样", "起来", "出来", "过来", "下去", "上来", "我们", "你们",
    "他们", "自己", "知道", "觉得", "有些", "有点", "只是", "不过", "于是",
}

ACTION_RE = re.compile(r"（([^）]{1,80})）")
QUOTE_RE = re.compile(r"“([^”]{1,200})”")
MARK_RE = re.compile(r"【([^】]{1,16})】")
PUNCT = "。！？…，、；：,.!?"


# ---------------------------------------------------------------- 小工具

def _median(nums):
    if not nums:
        return 0
    s = sorted(nums)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2


def _cut(text, limit):
    """在标点处断开，不要拦腰截。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for i in range(len(head) - 1, max(0, len(head) - 15), -1):
        if head[i] in PUNCT:
            return head[:i + 1]
    return head + "…"


def _first_action(text):
    m = ACTION_RE.search(text or "")
    return m.group(1).strip() if m else ""


def _first_quote(text):
    m = QUOTE_RE.search(text or "")
    return m.group(1).strip() if m else ""


def _strip_action(text):
    return ACTION_RE.sub("", text or "").strip()


def _text_of(msg):
    """一条消息的正文（不含 THINK）。"""
    return msg.get("response") or msg.get("request") or ""


def pair_rounds(messages):
    """把消息按 USER/ASSISTANT 配成轮。配不上的也保留，不丢内容。"""
    rounds = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m["role"] == "USER":
            a = messages[i + 1] if i + 1 < n and messages[i + 1]["role"] == "ASSISTANT" else None
            rounds.append({"u": m, "a": a})
            i += 2 if a else 1
        else:
            rounds.append({"u": None, "a": m})
            i += 1
    return rounds


def detect_names(messages, char_name="", user_name=""):
    """从设定卡里猜角色名和用户名，猜不到用兜底。"""
    char = char_name.strip()
    user = user_name.strip()
    head = (messages[0]["request"] if messages and messages[0]["role"] == "USER" else "")

    if not user:
        m = re.search(r"用户（([^）]{1,12})）", head) or re.search(r"我叫([\u4e00-\u9fa5]{1,6})", head)
        if m:
            user = m.group(1).strip()
    if not char:
        m = re.search(r"你是([\u4e00-\u9fa5]{1,6})", head)
        if m:
            char = m.group(1).strip()
    return (char or "角色", user or "你")


def is_setting_card(msg):
    """设定卡：第一条用户消息，够长，且有人设味。"""
    if not msg or msg.get("role") != "USER":
        return False
    text = msg.get("request") or ""
    if len(text) < 600:
        return False
    return bool(re.search(r"你是|设定|人设|角色|你的身份", text))


def collect_stats(rounds, char_names=(), user_names=()):
    """从原文统计写法特征——不能是套话。"""
    a_texts = [(_text_of(r["a"]) if r["a"] else "") for r in rounds]
    a_texts = [t for t in a_texts if t]
    u_texts = [(_text_of(r["u"]) if r["u"] else "") for r in rounds]
    u_texts = [t for t in u_texts if t]
    all_text = "\n".join(a_texts)

    lengths = [len(t) for t in a_texts]
    med = _median(lengths)

    with_bracket = sum(1 for t in a_texts if ACTION_RE.search(t))
    with_quote = sum(1 for t in a_texts if QUOTE_RE.search(t))

    # 状态标记：【占有欲：100】
    marks = MARK_RE.findall(all_text)
    mark_pairs = []
    for raw in marks:
        for sep in ("：", ":"):
            if sep in raw:
                k, v = raw.split(sep, 1)
                mark_pairs.append((k.strip(), v.strip()))
                break
    mark_keys = []
    for k, _ in mark_pairs:
        if k not in mark_keys:
            mark_keys.append(k)
    mark_current = dict(mark_pairs[-len(mark_keys):]) if mark_pairs else {}

    # 惯用词：2-4 字 n-gram，count*len 排序
    banned_tokens = set(mark_keys) | set(char_names) | set(user_names)
    ngram = {}
    for size in (2, 3, 4):
        for i in range(len(all_text) - size + 1):
            g = all_text[i:i + size]
            if not re.match(r"^[\u4e00-\u9fa5]+$", g):
                continue
            if g[0] in STOP_EDGE or g[-1] in STOP_EDGE:
                continue
            if any(ch in STOP_ANY for ch in g):
                continue
            if g in STOP_PHRASES:
                continue
            ngram[g] = ngram.get(g, 0) + 1
    # 去掉状态键名、角色名、以及它们的子串（占有 / 有欲 都是「占有欲」的碎片）
    for g in list(ngram):
        for tok in banned_tokens:
            if not tok:
                continue
            if g in tok or tok in g:
                del ngram[g]
                break
    scored = sorted(
        ((g, c) for g, c in ngram.items() if c >= 3),
        key=lambda kv: kv[1] * len(kv[0]),
        reverse=True,
    )
    idioms = []
    for g, _c in scored:
        if any(g in kept or kept in g for kept in idioms):
            continue
        idioms.append(g)
        if len(idioms) >= 14:
            break

    # 动作起手词：去掉主语后取到第一个标点，2-6 字
    starters = {}
    for t in a_texts:
        for act in ACTION_RE.findall(t):
            act = act.strip()
            for nm in list(char_names) + ["他", "她"]:
                if nm and act.startswith(nm):
                    act = act[len(nm):]
            head = re.split(r"[，。、；：！？,]", act)[0].strip()
            if not (2 <= len(head) <= 6):
                continue
            if any(ch in STOP_ANY for ch in head):
                continue
            if head[0] in STOP_EDGE or head[-1] in STOP_EDGE:
                continue
            if head in STOP_PHRASES:
                continue
            starters[head] = starters.get(head, 0) + 1
    starter_list = [k for k, c in sorted(starters.items(), key=lambda kv: -kv[1]) if c >= 2][:10]

    return {
        "rounds": len(rounds),
        "total_chars": sum(len(t) for t in a_texts) + sum(len(t) for t in u_texts),
        "assistant_median": med,
        "assistant_min": min(lengths) if lengths else 0,
        "assistant_max": max(lengths) if lengths else 0,
        "user_median": _median([len(t) for t in u_texts]),
        "bracket_ratio": (with_bracket / len(a_texts)) if a_texts else 0,
        "quote_ratio": (with_quote / len(a_texts)) if a_texts else 0,
        "mark_keys": mark_keys,
        "mark_current": mark_current,
        "mark_count": len(marks),
        "idioms": idioms,
        "starters": starter_list,
    }


def timeline_line(rounds, idx, names):
    """一行一轮：我的一条 ｜ 角色的一条。"""
    char, user = names
    r = rounds[idx]
    parts = []

    if r["u"]:
        ut = _text_of(r["u"])
        act = _first_action(ut)
        body = _strip_action(ut)
        body = _cut(body, 40) if body else ""
        left = ("（%s）%s" % (_cut(act, 20), body)) if act else body
        parts.append("%s：%s" % (user, left or "…"))
    if r["a"]:
        at = _text_of(r["a"])
        act = _first_action(at)
        quote = _first_quote(at)
        right = ""
        if act:
            # 动作里已经带了角色名就重复了，去掉开头的主语
            for nm in (char, "他", "她"):
                if nm and act.startswith(nm):
                    act = act[len(nm):]
            act = act.strip().rstrip("，、；：,;:")
            if act:
                right += "（%s）" % _cut(act, 34)
        if quote:
            right += "“%s”" % _cut(quote, 34)
        if not right:
            right = _cut(_strip_action(at), 40)
        parts.append("%s：%s" % (char, right))
    return "· %d) %s" % (idx + 1, "｜".join(parts))


def _date_label(msg):
    ts = (msg or {}).get("inserted_at") or 0
    if not ts:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m月%d日")


def build_timeline(rounds, names):
    """时间线，按日期分段。"""
    lines = []
    last_date = None
    for i, r in enumerate(rounds):
        label = _date_label(r["u"] or r["a"])
        if label and label != last_date:
            if lines:
                lines.append("")
            lines.append("—— %s ——" % label)
            last_date = label
        lines.append(timeline_line(rounds, i, names))
    return "\n".join(lines)


def build_writing_section(stats, rounds, tone_samples=3, include_think=True):
    """写法一节：全部来自原文统计，不许写套话。"""
    items = []

    # 这条必须放第一：压掉设定里的「开场方式」
    items.append(
        "先说明一件事：设定里若写了「开场方式」「剧情起点」「第一条回复」，"
        "那些都已经发生过了——不要重演开头的场景，不要重新自我介绍，直接从最后一段之后接着写。"
    )

    if stats["bracket_ratio"] >= 0.5 and stats["quote_ratio"] >= 0.5:
        items.append("每轮先用一个（）里的动作神态起头，再接“”里的台词；一段一轮，不要拆成多条。")
    elif stats["bracket_ratio"] >= 0.5:
        items.append("每轮先用一个（）里的动作起头，再接要说的话；一段一轮。")
    elif stats["quote_ratio"] >= 0.5:
        items.append("台词短，一句说完；停顿用动作表达，不要堆形容词。")

    lo = max(40, int(stats["assistant_median"] * 0.6) // 10 * 10)
    hi = max(lo + 20, int(stats["assistant_median"] * 1.8) // 10 * 10)
    items.append("每轮字数控制在 %d–%d 字之间（这是从原文统计出来的区间）。" % (lo, hi))

    items.append("用「你」称呼对方，不喊名字，不用第三人称；不替对方决定，不写对方的心里话。")
    items.append("不加标题、列表、旁白，不出戏，不解释自己在做什么。")

    if stats["mark_keys"]:
        fmt = "，".join("【%s：%s】" % (k, stats["mark_current"].get(k, "?"))
                       for k in stats["mark_keys"])
        items.append("每轮结尾必须附上状态标记，格式一模一样：%s" % fmt)

    if stats["idioms"]:
        items.append("惯用的说法（来自原文高频片段，可以自然使用，不要硬塞）："
                     + "、".join(stats["idioms"]))

    if stats["starters"]:
        items.append("动作常用这几个起手：" + "、".join(stats["starters"]))

    # 语气样本：沿时间轴均匀取，避免只学到结尾画风
    if tone_samples > 0:
        pool = [(i, r) for i, r in enumerate(rounds) if r["a"]]
        if pool:
            med = stats["assistant_median"] or 1
            picks = []
            used = set()
            for frac in (0.25, 0.5, 0.75)[:tone_samples]:
                pos = min(len(pool) - 1, int(len(pool) * frac))
                cand = pool[max(0, pos - 2):pos + 3]
                # 按「长度接近中位数」排序，跳过已经选过的位置
                for _idx, r in sorted(cand, key=lambda ir: abs(len(_text_of(ir[1]["a"])) - med)):
                    key = pool.index((_idx, r))
                    if key not in used:
                        used.add(key)
                        picks.append((_idx, r))
                        break
            if picks:
                body = []
                for i, (_idx, r) in enumerate(picks):
                    body.append("样本%d：%s" % (
                        i + 1, _cut(_text_of(r["a"]), 250)))
                items.append("语气样本（照这个密度和口吻来，不是让你照抄内容）：\n" + "\n".join(body))

    if include_think:
        think = ""
        for r in reversed(rounds):
            if r["a"] and r["a"].get("think"):
                think = r["a"]["think"]
                break
        if think:
            items.append("若开启思考，保持同样的密度和克制感：" + _cut(think, 200))

    return items


# ---------------------------------------------------------------- 主流程

def build(messages, budget=16000, recent=12, tone_samples=3, char_name="",
          user_name="", direction="", include_think=True):
    """组装提示词。

    返回 dict：prompt / stats / warnings / trimmed / sections
    """
    if not messages:
        raise ValueError("没有消息")

    rounds = pair_rounds(messages)
    if not rounds:
        raise ValueError("没有可用的轮次")

    names = detect_names(messages, char_name, user_name)
    warnings = []

    has_card = is_setting_card(rounds[0]["u"])
    head_round = rounds[0]
    body = rounds[1:]

    if not body:
        raise ValueError("除设定卡之外没有对话，没法生成提示词。")

    stats = collect_stats(rounds, [names[0]], [names[1]])
    trimmed = []

    def assemble(recent_n, timeline_rounds, tone_n):
        sections = []

        if has_card:
            sections.append(("你是谁（设定原文，不要改写）",
                             head_round["u"]["request"]))
        else:
            opening = []
            if head_round["u"]:
                opening.append("我：" + _text_of(head_round["u"]))
            if head_round["a"]:
                opening.append("%s：%s" % (names[0], _text_of(head_round["a"])))
            sections.append(("开场（原文）", "\n".join(opening)))

        if timeline_rounds:
            sections.append(("已经发生过的事（按顺序）",
                             build_timeline(timeline_rounds, names)))

        if stats["mark_keys"]:
            body_lines = []
            for k in stats["mark_keys"]:
                body_lines.append("%s：%s（全篇出现 %d 次）"
                                  % (k, stats["mark_current"].get(k, "?"), stats["mark_count"]))
            sections.append(("当前状态", "\n".join(body_lines)))

        writing = build_writing_section(
            stats, body[-max(recent_n, 1):] + [head_round], tone_n, include_think)
        sections.append(("写法（照这个来，这是从原文统计出来的）", "\n".join(
            ["%d. %s" % (i + 1, s) for i, s in enumerate(writing)])))

        recent_rounds = body[-recent_n:] if recent_n > 0 else []
        if recent_rounds:
            lines = []
            for r in recent_rounds:
                if r["u"]:
                    lines.append("我：" + _text_of(r["u"]))
                if r["a"]:
                    lines.append("%s：%s" % (names[0], _text_of(r["a"])))
            sections.append(("最近 %d 轮原文（逐字）" % recent_n, "\n".join(lines)))

        tail = []
        last = recent_rounds[-1] if recent_rounds else body[-1]
        if last["a"]:
            tail.append("接着上面最后一段%s的话往下写一轮。" % names[0])
        else:
            tail.append("回应上面最后那一句。")
        if direction.strip():
            tail.append("这一轮往这个方向走：" + direction.strip())
        sections.append(("现在开始", "\n".join(tail)))

        return sections

    def render(sections):
        cn = "一二三四五六七八九十"
        out = ["读完之后直接进入角色，从最后一行之后接着写。不要复述、不要总结、不要解释，不要问「准备好了吗」。", ""]
        for i, (title, text) in enumerate(sections):
            num = cn[i] if i < len(cn) else str(i + 1)
            out.append("# %s、%s" % (num, title))
            out.append("")
            out.append(text)
            out.append("")
        return "\n".join(out).strip()

    # 预算控制：按规格的顺序削
    recent_n = max(1, min(recent, len(body)))
    tone_n = max(0, tone_samples)
    timeline_source = body[:-recent_n] if recent_n < len(body) else []

    sections = assemble(recent_n, timeline_source, tone_n)
    prompt = render(sections)

    if budget and len(prompt) > budget:
        # 1) 语气样本减到 1
        if tone_n > 1:
            tone_n = 1
            sections = assemble(recent_n, timeline_source, tone_n)
            prompt = render(sections)
            trimmed.append("语气样本减到 1 条")

    while budget and len(prompt) > budget and len(timeline_source) > 60:
        # 2) 时间线从最早的开始，每次砍前 1/10
        cut = max(1, len(timeline_source) // 10)
        timeline_source = timeline_source[cut:]
        sections = assemble(recent_n, timeline_source, tone_n)
        prompt = render(sections)
        trimmed.append("时间线砍掉最早 %d 条" % cut)

    while budget and len(prompt) > budget and recent_n > 6:
        # 3) 最近原文减半（不低于 6 轮）
        recent_n = max(6, recent_n // 2)
        timeline_source = body[:-recent_n]
        sections = assemble(recent_n, timeline_source, tone_n)
        prompt = render(sections)
        trimmed.append("最近原文减到 %d 轮" % recent_n)

    if budget and len(prompt) > budget and len(timeline_source) > 60:
        # 4) 时间线压到最近 60 条
        timeline_source = timeline_source[-60:]
        sections = assemble(recent_n, timeline_source, tone_n)
        prompt = render(sections)
        trimmed.append("时间线压到最近 60 条")

    if budget and len(prompt) > budget:
        warnings.append("已按顺序削到最省，仍然超出 %d 字预算；建议放宽预算或减少轮数。" % budget)

    return {
        "prompt": prompt,
        "stats": stats,
        "warnings": warnings,
        "trimmed": trimmed,
        "sections": [t for t, _ in sections],
        "names": {"char": names[0], "user": names[1]},
        "chars": len(prompt),
        "budget": budget,
        "persona_detected": has_card,
        "timeline_count": len(timeline_source),
        "recent": recent_n,
    }
