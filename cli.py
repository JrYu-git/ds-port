"""命令行入口：把分享链接直接变成一份提示词文件。

用法：
  python cli.py "https://chat.deepseek.com/share/xxxx" -o prompt.txt
  python cli.py "链接1 链接2" --merge --budget 8000
"""

import argparse
import io
import sys

import builder
import fetcher


def write_out(text, path):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return "已写入 %s（%d 字）" % (path, len(text))
    sys.stdout.write(text)
    sys.stdout.write("\n")
    return ""


def merge_messages(batches):
    """多份分享按首条时间排序拼接，接缝处的重复轮去重。"""
    batches = sorted(batches, key=lambda ms: (ms[0].get("inserted_at") or 0) if ms else 0)
    merged = []
    warnings = []
    for msgs in batches:
        if merged and msgs:
            # 接缝去重：比较 role + 文本，相邻重复的丢掉
            tail = merged[-1]
            head = msgs[0]
            if tail.get("role") == head.get("role") and \
                    (tail.get("request") or tail.get("response")) == (head.get("request") or head.get("response")):
                msgs = msgs[1:]
            if merged and msgs and merged[-1].get("role") == msgs[0].get("role") == "USER":
                warnings.append("接缝处出现连续两段「我」的发言，可能不是同一条时间线。")
        merged.extend(msgs)
    return merged, warnings


def main():
    ap = argparse.ArgumentParser(
        prog="cli.py",
        description="把 DeepSeek 分享链接变成一段可迁移的提示词。",
    )
    ap.add_argument("links", help="分享链接或裸 share id，可给多个（用空格分开）")
    ap.add_argument("-o", "--out", help="输出文件（默认打到终端）")
    ap.add_argument("--budget", type=int, default=16000,
                    help="提示词字数预算，0 表示不限（默认 16000）")
    ap.add_argument("--recent", type=int, default=12, help="逐字保留最近几轮（默认 12）")
    ap.add_argument("--samples", type=int, default=3, help="语气样本数（默认 3）")
    ap.add_argument("--merge", dest="merge", action="store_true", default=True,
                    help="多链接合并（默认开）")
    ap.add_argument("--no-merge", dest="merge", action="store_false")
    ap.add_argument("--char", default="", help="角色名（留空自动识别）")
    ap.add_argument("--user", default="", help="我的名字（留空自动识别）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新抓")
    ap.add_argument("--no-stale", action="store_true",
                    help="抓不到时不要回退到过期缓存（默认会回退）")
    args = ap.parse_args()

    ids = fetcher.extract_ids(args.links)
    if not ids:
        print("没认出链接。形如 https://chat.deepseek.com/share/xxxx", file=sys.stderr)
        return 2

    ok, errors = fetcher.fetch_many(ids, use_cache=not args.refresh,
                                    allow_stale=not args.no_stale)
    for sid, msg in errors:
        print("[跳过] %s：%s" % (sid, msg), file=sys.stderr)
    if not ok:
        return 1

    for item in ok:
        if item["stale"]:
            print("[旧缓存] %s：接口没拿到（%s），用的是 %d 小时前的缓存。"
                  % (item["id"], item["note"], item["hours"]), file=sys.stderr)

    warnings = []
    if args.merge and len(ok) > 1:
        messages, warnings = merge_messages([item["messages"] for item in ok])
    else:
        # 不合并时只用第一份
        messages = ok[0]["messages"]

    try:
        res = builder.build(
            messages,
            budget=args.budget,
            recent=args.recent,
            tone_samples=args.samples,
            char_name=args.char,
            user_name=args.user,
            direction="",
            include_think=True,
        )
    except ValueError as e:
        print("生成失败：%s" % e, file=sys.stderr)
        return 1

    st = res["stats"]
    print("轮数 %d / 原文 %d 字 / 时间线 %d 条 / 提示词 %d 字"
          % (st["rounds"], st["total_chars"], res["timeline_count"], res["chars"]),
          file=sys.stderr)
    for t in res["trimmed"]:
        print("已削减：%s" % t, file=sys.stderr)
    for w in warnings + res["warnings"]:
        print("注意：%s" % w, file=sys.stderr)

    msg = write_out(res["prompt"], args.out)
    if msg:
        print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
