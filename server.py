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

_running = threading.Lock()


def run_once():
    # cron-job.org 30 秒就断连，而机器睡着时一轮要 90 秒以上，所以请求只负责投递
    # shortcut: 同一时刻只跑一轮（睡着的机器上每分钟触发会叠加），要并发就换队列
    if not _running.acquire(blocking=False):
        k.log("上一轮还没结束，跳过本轮")
        return
    try:
        k.main()
    except Exception as e:  # 线程里的异常不兜住就静默消失了
        k.log(f"ERROR: 未捕获异常 {e!r}")
    finally:
        _running.release()


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
        threading.Thread(target=run_once, daemon=True).start()
        self.send_response(202)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # k.log() 已经把每轮结果打出来了


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    k.log(f"listening on 0.0.0.0:{port}, endpoint GET /keepalive")
    if INTERVAL > 0:
        def loop():
            while True:
                run_once()
                time.sleep(INTERVAL)
        threading.Thread(target=loop, daemon=True).start()
        k.log(f"自循环已开启，每 {INTERVAL}s 一轮")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
