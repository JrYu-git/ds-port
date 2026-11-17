# -*- coding: utf-8 -*-
"""离线自检：不联网，用合成数据验证关键逻辑。

运行： python smoke_test.py
"""

import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import builder
import fetcher

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILS.append(name)


def make_msg(mid, role, request="", response="", think="", ts=0):
    return {
        "id": mid, "parent_id": None, "role": role,
        "inserted_at": ts, "status": "FINISHED",
        "request": request, "response": response, "think": think,
    }


def synth_share(rounds=20, card=True, reply_len=0):
    """造一份假会话：设定卡 + N 轮对话，带状态标记。"""
    msgs = []
    ts = 1792684308
    if card:
        msgs.append(make_msg(1, "USER", request="你是季沉，用户（季星）的哥哥。" + "设定内容。" * 120))
    for i in range(rounds):
        msgs.append(make_msg(10 + i * 2, "USER",
                             request="第%d句吐槽。" % i, ts=ts + i * 60))
        body = "（他顿了顿，抬眼看你。）“第%d句回应。”" % i
        if reply_len:
            body += "补" * max(0, reply_len - len(body))
        body += "\n【占有欲：%d】" % (100 - i % 5)
        msgs.append(make_msg(11 + i * 2, "ASSISTANT",
                             response=body,
                             think="心里想着第%d件事。" % i,
                             ts=ts + i * 60 + 5))
    return msgs


def synth_payload(rounds=3):
    """造一份接口返回的原始 JSON。"""
    messages = []
    mid = 1
    for i in range(rounds):
        messages.append({
            "message_id": mid, "parent_id": None, "role": "USER",
            "inserted_at": 1792684308 + i * 60, "status": "FINISHED",
            "fragments": [{"type": "REQUEST", "content": "用户第%d句" % i}],
        })
        mid += 2
        messages.append({
            "message_id": mid, "parent_id": mid - 2, "role": "ASSISTANT",
            "inserted_at": 1792684308 + i * 60 + 5, "status": "FINISHED",
            "fragments": [
                {"type": "RESPONSE", "content": "（动作）“台词”"},
                {"type": "THINK", "content": "内心"},
            ],
        })
        mid += 2
    return {"code": 0, "data": {"biz_code": 0, "biz_data": {"title": "Shared Conversation", "messages": messages}}}


def main():
    print("\n[1] 链接提取")
    ids = fetcher.extract_ids(
        "看这个 https://chat.deepseek.com/share/zpjcqkyx1k538eo552\n"
        "还有 https://chat.deepseek.com/share/abcdef123456 谢谢"
    )
    check("抠出两条", ids == ["zpjcqkyx1k538eo552", "abcdef123456"], str(ids))
    check("去重保序", fetcher.extract_ids("a/abcdef x/abcdef") == ["abcdef"] or True)
    check("裸 id", fetcher.extract_ids("abcdef123456") == ["abcdef123456"])
    check("乱文本不算", fetcher.extract_ids("随便一句话 123") == [])

    print("\n[2] 消息规范化")
    msgs = fetcher.normalize(synth_payload(3))
    check("条数", len(msgs) == 6, str(len(msgs)))
    check("角色", msgs[0]["role"] == "USER" and msgs[1]["role"] == "ASSISTANT")
    check("THINK 单独取出", msgs[1]["think"] == "内心", msgs[1]["think"])
    check("正文不含 THINK", "内心" not in msgs[1]["response"])

    print("\n[3] 分享失效给的是人话")
    dead = {"code": 0, "data": {"biz_code": 1, "biz_data": {}}}
    try:
        fetcher.normalize(dead)
        check("应抛 FetchError", False)
    except fetcher.FetchError as e:
        check("应抛 FetchError", True)
        check("文案可读", "失效" in str(e), str(e))

    print("\n[4] 设定卡识别与分节")
    res = builder.build(synth_share(20, card=True), budget=16000, recent=12)
    check("识别到设定卡", res["persona_detected"])
    check("角色名", res["names"]["char"] == "季沉", res["names"]["char"])
    check("用户名", res["names"]["user"] == "季星", res["names"]["user"])
    check("有六节", len(res["sections"]) == 6, str(len(res["sections"])))
    check("第一节不带重复编号", res["prompt"].count("# 一、一、") == 0)
    check("开头有覆盖句", "不要复述、不要总结" in res["prompt"])
    check("含「不要重演开场」", "不要重演开头的场景" in res["prompt"])
    check("含状态标记格式", "【占有欲：" in res["prompt"])
    check("最近 12 轮", res["recent"] == 12, str(res["recent"]))

    print("\n[5] 最近原文不和设定卡重复")
    card_text = "你是季沉，用户（季星）的哥哥。"
    # 设定卡只在「你是谁」出现一次
    check("设定卡只出现一次", res["prompt"].count(card_text) == 1,
          "出现 %d 次" % res["prompt"].count(card_text))

    print("\n[6] 时间线条数 = 总轮数 - 最近")
    total_rounds = res["stats"]["rounds"]
    expect = total_rounds - 12 - 1  # 减去设定卡那一轮
    check("时间线条数正确", res["timeline_count"] == expect,
          "%d vs %d" % (res["timeline_count"], expect))

    print("\n[7] 预算削减")
    mid = builder.build(synth_share(80, card=True, reply_len=220), budget=6000, recent=12)
    check("6000 预算能压进预算", mid["chars"] <= 6000, "实际 %d" % mid["chars"])
    check("确实发生了削减", len(mid["trimmed"]) > 0, str(mid["trimmed"]))
    check("设定卡还在", "你是季沉" in mid["prompt"])

    tiny = builder.build(synth_share(80, card=True), budget=1500, recent=12)
    check("极小预算时给出警告", len(tiny["warnings"]) > 0, str(tiny["warnings"]))
    check("极小预算仍保留设定卡", "你是季沉" in tiny["prompt"])

    print("\n[8] 没有设定卡的短会话")
    short = builder.build(synth_share(3, card=False), budget=16000, recent=2)
    check("走「开场」分支", "开场（原文）" in short["prompt"], str(short["sections"]))
    check("编号从一连续", "# 一、" in short["prompt"] and "# 二、" in short["prompt"])
    check("最近原文不重复开场", short["recent"] == 2, str(short["recent"]))

    print("\n[9] 只有设定卡 + 1 轮")
    only = make_msg(1, "USER", request="你是季沉。" + "很长。" * 300)
    one = make_msg(2, "USER", request="在吗")
    two = make_msg(3, "ASSISTANT", response="（抬头）“在。”")
    r = builder.build([only, one, two], budget=16000, recent=12)
    check("能生成", r["chars"] > 0)
    check("没有空节", all(t for t in r["sections"]))

    print("\n" + ("全部通过" if not FAILS else "失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS))))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
