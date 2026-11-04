#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次性脚本包成 HTTP 端点：PaaS 的 cron 打 GET /keepalive 就执行一轮。

本地：PORT=8080 INSTABOX_API_KEY=... python3 server.py
"""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import instabox_keepalive as k

# 端点挂在公网上，不设口令等于谁都能替你消耗 API 配额；留空则不校验（仅本地用）
TOKEN = os.environ.get("KEEPALIVE_TOKEN", "").strip()
# >0 就自己按秒循环，用于没有 cron 的平台（Cloady 这类纯容器 PaaS）
INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "0"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/keepalive":
            self.send_response(404)
            self.end_headers()
            return
        if TOKEN and self.headers.get("X-Keepalive-Token") != TOKEN:
            self.send_response(403)
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
    if INTERVAL > 0:
        def loop():
            while True:
                k.main()
                time.sleep(INTERVAL)
        threading.Thread(target=loop, daemon=True).start()
        k.log(f"自循环已开启，每 {INTERVAL}s 一轮")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
