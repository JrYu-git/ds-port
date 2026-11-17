"""本地服务：http.server，只监听 127.0.0.1。

路由：
  GET  /            界面
  GET  /api/health  健康检查
  POST /api/fetch   {text, refresh}          → 抓取并规范化
  POST /api/build   {messages, opts}         → 生成提示词
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import builder
import fetcher

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PREFERRED_PORT = 8899
MAX_BODY = 32 * 1024 * 1024  # 32MB，够放一份长会话

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ds-port/1.0"

    # ---------- 基础工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
            raw = body.encode("utf-8")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体太大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/health":
            self._send(200, {"ok": True, "service": "ds-port", "version": "1.0"})
            return

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        self._send(404, {"ok": False, "error": "没有这个路径"})

    def _serve_static(self, name):
        # 只允许 static 目录里的普通文件，挡掉 ../
        safe = os.path.normpath(os.path.join(STATIC, name))
        if not safe.startswith(STATIC + os.sep) and safe != STATIC:
            self._send(403, {"ok": False, "error": "拒绝访问"})
            return
        if not os.path.isfile(safe):
            self._send(404, {"ok": False, "error": "文件不存在"})
            return
        _, ext = os.path.splitext(safe)
        with open(safe, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext.lower(), "application/octet-stream"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            payload = self._json_body()
        except ValueError as e:
            self._send(400, {"ok": False, "error": str(e)})
            return

        if path == "/api/fetch":
            self._handle_fetch(payload)
        elif path == "/api/build":
            self._handle_build(payload)
        else:
            self._send(404, {"ok": False, "error": "没有这个路径"})

    # ---------- 业务 ----------
    def _handle_fetch(self, payload):
        text = (payload.get("text") or "").strip()
        refresh = bool(payload.get("refresh"))
        if not text:
            self._send(400, {"ok": False, "error": "没有收到链接"})
            return

        ids = fetcher.extract_ids(text)
        if not ids:
            self._send(400, {"ok": False,
                             "error": "没认出链接。形如 https://chat.deepseek.com/share/xxxx"})
            return

        ok, errors = fetcher.fetch_many(ids, use_cache=not refresh)
        if not ok:
            self._send(200, {"ok": False, "shares": [], "errors": errors,
                             "error": errors[0][1] if errors else "抓取失败"})
            return

        shares = []
        for sid, msgs, from_cache in ok:
            shares.append({"id": sid, "messages": msgs, "from_cache": from_cache})
        self._send(200, {"ok": True, "shares": shares, "errors": errors})

    def _handle_build(self, payload):
        messages = payload.get("messages") or []
        opts = payload.get("opts") or {}
        if not messages:
            self._send(400, {"ok": False, "error": "没有消息可生成"})
            return
        try:
            result = builder.build(
                messages,
                budget=int(opts.get("budget") or 16000),
                recent=int(opts.get("recent") or 12),
                tone_samples=int(opts.get("samples") or 3),
                char_name=opts.get("char") or "",
                user_name=opts.get("user") or "",
                direction=opts.get("direction") or "",
                include_think=bool(opts.get("think", True)),
            )
        except ValueError as e:
            self._send(200, {"ok": False, "error": str(e)})
            return
        self._send(200, {"ok": True, "result": result})


def main(argv=None):
    port = PREFERRED_PORT
    for cand in (PREFERRED_PORT, 8890, 8891, 8892):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError:
            continue
    else:
        print("8899 及备用端口都被占用了，先关掉占用的程序再试。")
        return 1

    print("ds-port 已启动： http://127.0.0.1:%d" % port)
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
