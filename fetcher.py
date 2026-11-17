"""抓取层：把 DeepSeek 分享链接变成规范化的消息列表。

只用标准库（urllib / json / re / os），不装任何依赖，Python 3.9+ 可跑。

设计约束：
  * 只读公开分享接口，不登录、不带 cookie、不向 DeepSeek 写入任何东西。
  * 抓到的原文按 share_id 缓存，24 小时过期。
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://chat.deepseek.com/api/v0/share/content?share_id={}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CACHE_TTL = 24 * 3600

# /share/ 后面这一段是分享 id，只允许 URL 安全字符
SHARE_RE = re.compile(r"/share/([A-Za-z0-9_-]{6,})")
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


class FetchError(Exception):
    """抓取失败。消息文案直接给用户看，不抛栈。"""


def extract_ids(text):
    """从一段乱七八糟的粘贴文本里抠出 share id，去重保序。"""
    if not text:
        return []
    found = []
    for m in SHARE_RE.finditer(text):
        found.append(m.group(1))
    if not found:
        # 整行就是裸 id 的情况（用户只贴了一串字符）
        for line in text.splitlines():
            s = line.strip()
            if BARE_ID_RE.match(s):
                found.append(s)
    seen = set()
    out = []
    for i in found:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _cache_path(share_id):
    return os.path.join(CACHE_DIR, "%s.json" % share_id)


def _read_cache(share_id, use_cache=True):
    if not use_cache:
        return None
    path = _cache_path(share_id)
    if not os.path.isfile(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > CACHE_TTL:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(share_id, raw):
    """先写临时文件再 os.replace，避免留下半个文件。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(share_id)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # 缓存写失败不影响主流程（比如 Windows 上文件被占用）
        pass


def _request_once(share_id, timeout):
    url = API.format(urllib.parse.quote(share_id))
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise FetchError("被限流了，等一会儿再点，或者直接用缓存。")
        if 500 <= e.code < 600:
            raise FetchError("DeepSeek 服务端出错（HTTP %d），稍后再试。" % e.code)
        if e.code == 404:
            raise FetchError("这条分享不存在，检查一下链接。")
        raise FetchError("请求失败（HTTP %d）。" % e.code)
    except urllib.error.URLError as e:
        raise FetchError("网络连不上：%s" % (e.reason,))
    except OSError as e:
        raise FetchError("网络异常：%s" % (e,))

    try:
        # 必须显式按 UTF-8 解码：Windows 上默认编码是 GBK，会变乱码
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError("返回内容不是 UTF-8，可能接口变了。")

    try:
        raw = json.loads(text)
    except ValueError:
        raise FetchError("返回的不是 JSON，可能接口变了，或者被拦了。")
    return raw


def fetch_share(share_id, use_cache=True, timeout=20):
    """抓一条分享，返回原始 JSON dict。优先走缓存。"""
    cached = _read_cache(share_id, use_cache)
    if cached is not None:
        cached["_from_cache"] = True
        return cached

    delay = 1.5
    last = None
    for _ in range(3):
        try:
            raw = _request_once(share_id, timeout)
            break
        except FetchError as e:
            last = e
            msg = str(e)
            if "限流" in msg or "服务端出错" in msg:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    else:
        raise last or FetchError("抓取失败。")

    raw["_from_cache"] = False
    _write_cache(share_id, raw)
    return raw


def normalize(raw):
    """原始 JSON → 规范化消息列表。

    每条消息：
      {id, parent_id, role, inserted_at, status, request, response, think}
    其中 role 为 'USER' / 'ASSISTANT'，request 只有用户消息有。
    """
    if not isinstance(raw, dict):
        raise FetchError("返回结构不对。")

    code = raw.get("code")
    if code not in (0, None):
        raise FetchError("接口返回异常（code=%s）。" % code)

    data = raw.get("data") or {}
    biz_code = data.get("biz_code", 0)
    if biz_code not in (0, None):
        # 分享被删 / 失效 / 转私密
        raise FetchError("这条分享已经失效了：可能被删除、被设为私密，或者链接不完整。")

    biz = data.get("biz_data") or {}
    msgs = biz.get("messages")
    if not msgs:
        raise FetchError("这条分享里没有消息。")

    out = []
    for m in msgs:
        request = response = think = ""
        for frag in (m.get("fragments") or []):
            ftype = frag.get("type")
            content = frag.get("content") or ""
            if ftype == "REQUEST":
                request += content
            elif ftype == "RESPONSE":
                response += content
            elif ftype == "THINK":
                think += content
        out.append({
            "id": m.get("message_id"),
            "parent_id": m.get("parent_id"),
            "role": (m.get("role") or "").upper(),
            "inserted_at": m.get("inserted_at") or 0,
            "status": m.get("status") or "",
            "request": request.strip(),
            "response": response.strip(),
            "think": think.strip(),
        })
    return out


def fetch_many(share_ids, use_cache=True):
    """逐个抓。单个失败不拖垮整批。

    返回 (ok_list, errors)：
      ok_list: [(share_id, messages, from_cache)]
      errors:  [(share_id, 错误文案)]
    """
    ok, errors = [], []
    for sid in share_ids:
        try:
            raw = fetch_share(sid, use_cache=use_cache)
            msgs = normalize(raw)
            ok.append((sid, msgs, bool(raw.get("_from_cache"))))
        except FetchError as e:
            errors.append((sid, str(e)))
    return ok, errors
