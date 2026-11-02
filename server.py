#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次性脚本包成 HTTP 端点：PaaS 的 cron 打 GET /keepalive 就执行一轮。

本地：PORT=8080 INSTABOX_API_KEY=... python3 server.py
"""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import instabox_keepalive as k


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/keepalive":
            self.send_response(404)
            self.end_headers()
            return
        code = k.main()
        self.send_response(200 if code == 0 else 500)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # k.log() 已经把每轮结果打出来了


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    k.log(f"listening on 0.0.0.0:{port}, endpoint GET /keepalive")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
