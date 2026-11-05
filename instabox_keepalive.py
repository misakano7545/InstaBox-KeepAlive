#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""InstaBox 免费实例保活：续租 lease、必要时唤醒/启动、探测并重启机器内服务。

所有配置只从环境变量读取，密钥不落盘、不进仓库。std lib only，无需安装依赖。
本地：INSTABOX_API_KEY=xxx INSTABOX_MACHINE_ID=m_xxx python3 instabox_keepalive.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

API_BASE = os.environ.get("INSTABOX_API_BASE", "https://api.instabox.sh").rstrip("/")
MACHINE_ID = os.environ.get("INSTABOX_MACHINE_ID", "").strip()
LEASE_ID = os.environ.get("INSTABOX_LEASE_ID", "actions-keepalive")
PUBLIC_URL = os.environ.get("INSTABOX_PUBLIC_URL", "").strip()
try:
    LEASE_TTL = int(os.environ.get("INSTABOX_LEASE_TTL", "900"))
except ValueError:
    LEASE_TTL = 900

API_KEY = os.environ.get("INSTABOX_API_KEY", "").strip()
# 机器内重启服务的命令，例如 "docker compose -f /root/app/docker-compose.yml up -d"
RESTART_CMD = os.environ.get("INSTABOX_RESTART_CMD", "").strip()


def log(msg):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def api(method, path, body=None, timeout=30):
    url = API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("X-API-Key", API_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "instabox-keepalive-actions/1.0")
    if method == "POST":
        req.add_header("Idempotency-Key", uuid.uuid4().hex[:32])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:300]
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw}
        return e.code, parsed
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def err_code(payload):
    # 真实错误形如 {"error":{"code":"machine_cooldown",...}}，直接取 payload["error"] 会返回整个 dict，
    # 下面的字符串比较永远为假，唤醒/启动分支就成了死代码。
    if isinstance(payload, dict):
        e = payload.get("error")
        if isinstance(e, dict):
            return e.get("code") or ""
        return e or payload.get("code") or ""
    return ""


def retry_after(payload):
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"].get("retryAfterSeconds")
    return None


def wait_running(timeout=90):
    waited = 0
    while waited < timeout:
        status, info = api("GET", f"/v1/machines/{MACHINE_ID}", timeout=30)
        if status == 200 and info.get("state") == "running":
            return True
        time.sleep(5)
        waited += 5
    return False


def renew_lease():
    return api("PUT", f"/v1/machines/{MACHINE_ID}/activity-leases/{LEASE_ID}",
               {"ttlSeconds": LEASE_TTL}, timeout=30)


def public_url_status():
    """公网 URL 的真实响应码：2xx/3xx 才算服务活着。"""
    if not PUBLIC_URL:
        return None
    try:
        req = urllib.request.Request(PUBLIC_URL + "/", method="GET")
        req.add_header("User-Agent", "instabox-keepalive-actions/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read(64)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"ERR:{str(e)[:80]}"


def alive(st):
    return isinstance(st, int) and st < 400


def restart_service():
    status, info = api("POST", f"/v1/machines/{MACHINE_ID}/exec",
                       {"cmd": RESTART_CMD, "timeoutSeconds": 120}, timeout=135)
    log(f"restart -> HTTP {status}, exit={info.get('exitCode') if isinstance(info, dict) else '?'}")
    if isinstance(info, dict):
        out = str(info.get("stdout") or "").strip()
        err = str(info.get("stderr") or "").strip()
        if out:
            log(f"restart out: {out[:200]}")
        if err:
            log(f"restart err: {err[:300]}")
    time.sleep(5)


def main():
    if not API_KEY:
        log("ERROR: 缺少环境变量 INSTABOX_API_KEY，请在仓库 Secrets 里配置仪表板建的 key。")
        return 1
    if not MACHINE_ID:
        log("ERROR: 缺少环境变量 INSTABOX_MACHINE_ID。")
        return 1
    if not (30 <= LEASE_TTL <= 3600):
        log(f"ERROR: INSTABOX_LEASE_TTL={LEASE_TTL} 超出范围 30-3600。")
        return 1
    log(f"machine={MACHINE_ID} lease={LEASE_ID} ttl={LEASE_TTL}s")

    status, info = api("GET", f"/v1/machines/{MACHINE_ID}", timeout=30)
    if status == 200:
        log(f"state={info.get('state')} autoSuspendAfterSeconds={info.get('autoSuspendAfterSeconds')} "
            f"maxRunMinutes={info.get('maxRunMinutes')} runStartedAt={info.get('runStartedAt')}")
    else:
        log(f"state check skipped (HTTP {status}, code={err_code(info)}); continuing to lease.")

    status, info = renew_lease()
    leased = status == 200
    # max-run 暂停后平台强制冷却 5~135 分钟，这段时间 exec/wake/start/lease 全部 409。
    # 这是免费机的固有代价，不是故障：记一行就退出，别每 5 分钟刷一次红。
    if not leased and err_code(info) == "machine_cooldown":
        log(f"冷却中，还需 {retry_after(info)}s；等下一轮。")
        return 0
    if not leased and status == 409 and err_code(info) == "machine_not_running":
        log("machine not running; trying wake...")
        status, info = api("POST", f"/v1/machines/{MACHINE_ID}/wake", {}, timeout=60)
        log(f"wake -> HTTP {status}")
        if wait_running():
            log("machine running after wake.")
        status, info = renew_lease()
        leased = status == 200
    if not leased and status == 409 and err_code(info) == "machine_not_running":
        log("still not running (maybe stopped after 8h max runtime); trying start...")
        status, info = api("POST", f"/v1/machines/{MACHINE_ID}/start", {}, timeout=60)
        log(f"start -> HTTP {status}")
        if wait_running():
            log("machine running after start.")
        status, info = renew_lease()
        leased = status == 200

    if not leased:
        if status == 401:
            log("ERROR: key 无效或已过期（401）。请检查 INSTABOX_API_KEY 是否填对、key 是否被删。")
            return 2
        if status == 403:
            log("ERROR: key 权限不够（403）。建 key 时范围要勾 machines:read 和 machines:write。")
            return 2
        log(f"ERROR: lease failed, HTTP {status}, code={err_code(info)}, body={str(info)[:200]}")
        return 2
    log(f"OK: lease active, expiresAt={info.get('expiresAt')}")

    probe = public_url_status()
    if probe is None:
        log("public URL 未配置，跳过存活检查。")
        return 0
    log(f"public URL -> {probe}")
    if not alive(probe):
        if not RESTART_CMD:
            log("ERROR: 服务不可达，且未配置 INSTABOX_RESTART_CMD，无法自动恢复。")
            return 1
        log("服务不可达，执行重启命令...")
        restart_service()
        probe = public_url_status()
        log(f"after restart -> {probe}")
        if not alive(probe):
            log("ERROR: 重启后仍不可达，等下一轮重试。")
            return 1
    log("OK: 服务可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
