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
from datetime import datetime, timedelta, timezone

API_BASE = os.environ.get("INSTABOX_API_BASE", "https://api.instabox.sh").rstrip("/")
MACHINE_ID = os.environ.get("INSTABOX_MACHINE_ID", "").strip()
LEASE_ID = os.environ.get("INSTABOX_LEASE_ID", "actions-keepalive")
PUBLIC_URL = os.environ.get("INSTABOX_PUBLIC_URL", "").strip()
try:
    LEASE_TTL = int(os.environ.get("INSTABOX_LEASE_TTL", "900"))
except ValueError:
    LEASE_TTL = 900
try:
    # 免费机单次运行上限 480 分钟，撞上就强制挂起并吃 120-135 分钟冷却（连撞越多次越久）。
    # 提前自己 pause/resume 换一个新窗口，冷却就不欠了，连击计数也归零。
    PAUSE_AFTER_MIN = int(os.environ.get("INSTABOX_PAUSE_AFTER_MIN", "420"))
except ValueError:
    PAUSE_AFTER_MIN = 420

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


def run_minutes(info):
    """这一轮已经跑了多少分钟（runStartedAt 缺失就当作 0）。"""
    started = info.get("runStartedAt")
    if not started:
        return 0.0
    ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60


def recycle_run():
    """主动 pause 再 resume：欠冷却是 max-run 才有的，自己结束的这轮不算。"""
    status, info = api("POST", f"/v1/machines/{MACHINE_ID}/suspend", {}, timeout=60)
    log(f"suspend -> HTTP {status} {err_code(info)}")
    if status != 200:
        return False
    for delay in (8, 12, 15):
        time.sleep(delay)
        status, info = api("POST", f"/v1/machines/{MACHINE_ID}/resume", {}, timeout=60)
        log(f"resume -> HTTP {status} {err_code(info)}")
        if status == 200:
            return True
    return False


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
        if info.get("state") == "running" and 0 < PAUSE_AFTER_MIN <= run_minutes(info):
            log(f"这一轮已跑 {run_minutes(info):.0f} 分钟（阈值 {PAUSE_AFTER_MIN}），主动换窗口，免得撞 max-run……")
            recycle_run()
    else:
        log(f"state check skipped (HTTP {status}, code={err_code(info)}); continuing to lease.")

    status, info = renew_lease()
    leased = status == 200

    if not leased and status in (401, 403):
        if status == 401:
            log("ERROR: key 无效或已过期（401）。请检查 INSTABOX_API_KEY 是否填对、key 是否被删。")
        else:
            log("ERROR: key 权限不够（403）。建 key 时范围要勾 machines:read 和 machines:write。")
        return 2

    if not leased:
        code = err_code(info)
        # 这两类是"机器会自己回来"，wake/start 只会再吃一个 operation_conflict
        if code == "machine_cooldown":
            log(f"冷却中，还需 {retry_after(info)}s；等下一轮。")
            return 0
        if code in ("operation_conflict", "host_unavailable"):
            log(f"机器正在切换状态（{code}），{retry_after(info) or '?'}s 后自己回来；等下一轮。")
            return 0
        if code != "machine_not_running":
            log(f"ERROR: lease failed, HTTP {status}, code={code}, body={str(info)[:200]}")
            return 2

        info2 = api("GET", f"/v1/machines/{MACHINE_ID}", timeout=30)[1]
        state = info2.get("state")
        # suspended 有两种：自己睡的（autoSuspendedAt 有值，访问即醒）和被手动暂停的
        # （autoSuspendedAt 为空，wake 永远拉不起来，只有 resume/start 管用）。
        if state == "stopped":
            verb = "start"
        elif state == "suspended" and not info2.get("autoSuspendedAt"):
            verb = "resume"
        else:
            verb = "wake"
        log(f"state={state}; {verb}...")
        s2, i2 = api("POST", f"/v1/machines/{MACHINE_ID}/{verb}", {}, timeout=60)
        log(f"{verb} -> HTTP {s2} {err_code(i2)}")
        # 不轮询 state：API 的 state 有约 30s 延迟，机器已经起来时它还会显示 starting。
        # lease 续上是唯一真相。
        for delay in (5, 10, 15):
            time.sleep(delay)
            status, info = renew_lease()
            if status == 200:
                break
        leased = status == 200
        if not leased:
            log(f"ERROR: {verb} 后仍无法续租，HTTP {status}, code={err_code(info)}")
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
    if sys.argv[1:] == ["--self-check"]:
        # 真实错误体（2026-11-05 从 API 抓的），err_code 回归的守卫
        cold = {"error": {"code": "machine_cooldown", "retryAfterSeconds": 5831}}
        assert err_code(cold) == "machine_cooldown"
        assert retry_after(cold) == 5831
        assert err_code({"error": {"code": "machine_not_running"}}) == "machine_not_running"
        assert err_code({"code": "plain"}) == "plain"
        assert err_code({}) == ""
        assert retry_after({}) is None

        # 错误码分流：桩掉网络层，不碰真实机器（真机 8 小时才走一次这条路径）
        real = api, time.sleep, API_KEY, MACHINE_ID, PUBLIC_URL
        API_KEY, MACHINE_ID, PUBLIC_URL = "ibk_stub", "m_stub", ""
        time.sleep = lambda s: None

        def branch(lease_seq, state, run_started=None, auto_sleep=True):
            global api
            seq, seen = list(lease_seq), []

            def fake(method, path, body=None, timeout=30):
                seen.append(f"{method} {path}")
                if "activity-leases" in path:
                    return seq.pop(0) if seq else (200, {"expiresAt": "t"})
                if method == "GET":
                    return 200, {"state": state, "runStartedAt": run_started,
                                 "autoSuspendedAt": "2026-11-06T05:30:34Z" if auto_sleep else None}
                return 200, {}

            api = fake
            try:
                return main(), seen
            finally:
                api = real[0]

        def err(code, ra=None):
            return 409, {"error": {"code": code, "retryAfterSeconds": ra}}

        assert branch([err("machine_cooldown", 60)], "suspended")[0] == 0
        assert branch([err("operation_conflict", 5)], "starting")[0] == 0
        assert branch([err("host_unavailable")], "running")[0] == 0
        assert branch([(401, {"error": {"code": "unauthorized"}})], "running")[0] == 2

        # 换窗口：只有 running 且跑满阈值才 pause+resume，没到点一根手指都不碰
        now_iso = lambda m: (datetime.now(timezone.utc) - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rc, seen = branch([(200, {"expiresAt": "t"})], "running", now_iso(PAUSE_AFTER_MIN + 5))
        assert rc == 0 and any(p.endswith("/suspend") for p in seen) and any(p.endswith("/resume") for p in seen), seen
        rc, seen = branch([(200, {"expiresAt": "t"})], "running", now_iso(5))
        assert not any(p.endswith(("/suspend", "/resume")) for p in seen), seen
        assert run_minutes({}) == 0.0
        rc, seen = branch([err("machine_not_running")], "stopped")
        assert rc == 0 and any(p.endswith("/start") for p in seen), seen
        rc, seen = branch([err("machine_not_running")], "suspended")
        assert rc == 0 and any(p.endswith("/wake") for p in seen), seen
        # 手动暂停（autoSuspendedAt 为空）wake 救不回，必须 resume
        rc, seen = branch([err("machine_not_running")], "suspended", auto_sleep=False)
        assert rc == 0 and any(p.endswith("/resume") for p in seen), seen

        api, time.sleep, API_KEY, MACHINE_ID, PUBLIC_URL = real
        print("self-check ok")
        sys.exit(0)
    sys.exit(main())
