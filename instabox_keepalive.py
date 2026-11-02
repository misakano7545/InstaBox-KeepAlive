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
    if isinstance(payload, dict):
        return payload.get("code") or payload.get("error") or ""
    return ""


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


# 注意：wg show interfaces 只能全局列出接口，绝不能写 wg show wg0 interfaces。
_PROBE_CMD = (
    "if sudo systemctl is-active -q sing-box; then echo SINGBOX_OK; else echo SINGBOX_DOWN; fi; "
    "if sudo wg show interfaces 2>/dev/null | grep -q '^wg0$'; then echo WG_OK; else echo WG_DOWN; fi; "
    "if ss -tln 2>/dev/null | grep -q ':2080 '; then echo PORT_OK; else echo PORT_DOWN; fi; "
    "echo \"FACT_SS=$(command -v ss || echo none) FACT_WG=$(command -v wg || echo none) "
    "UNIT=$(systemctl list-unit-files 2>/dev/null | grep -c sing-box) "
    "WGSCRIPT=$(test -f /data/home/wireguard/start-wg.sh && echo yes || echo no)\"; "
    "echo PROBE_DONE"
)


def probe_services():
    status, info = api("POST", f"/v1/machines/{MACHINE_ID}/exec",
                       {"cmd": _PROBE_CMD, "timeoutSeconds": 30}, timeout=45)
    out = info.get("stdout", "") if isinstance(info, dict) else ""
    result = {"singbox": "SINGBOX_OK" in out, "wg": "WG_OK" in out,
              "port": "PORT_OK" in out, "ran": "PROBE_DONE" in out,
              "raw": out.strip().replace("\n", " | ")[:300]}
    if status != 200 or not result["ran"]:
        result["error"] = f"exec HTTP {status}, exit={info.get('exitCode')}"
    return result


def recover_services(probe):
    parts = []
    if not probe["wg"]:
        parts.append("bash /data/home/wireguard/start-wg.sh")
    if not (probe["singbox"] and probe["port"]):
        parts.append("sudo systemctl restart sing-box")
    if not parts:
        return probe
    parts.append("sleep 3")
    cmd = "; ".join(parts)
    status, info = api("POST", f"/v1/machines/{MACHINE_ID}/exec",
                       {"cmd": cmd, "timeoutSeconds": 60}, timeout=75)
    log(f"recover exec -> HTTP {status}, exit={info.get('exitCode') if isinstance(info, dict) else '?'}")
    if isinstance(info, dict):
        log(f"recover out: {str(info.get('stdout') or '')[:200]} ")
        log(f"recover err: {str(info.get('stderr') or '')[:300]}")
    return probe_services()


def heartbeat_public_url():
    if not PUBLIC_URL:
        return None
    try:
        req = urllib.request.Request(PUBLIC_URL + "/", method="GET")
        req.add_header("User-Agent", "instabox-keepalive-actions/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(1)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"ERR:{str(e)[:80]}"


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

    probe = probe_services()
    if probe.get("error"):
        log(f"WARN: service probe failed ({probe['error']}); skipping recovery this round.")
    else:
        log(f"probe: singbox={'OK' if probe['singbox'] else 'DOWN'} "
            f"wg={'OK' if probe['wg'] else 'DOWN'} port2080={'OK' if probe['port'] else 'DOWN'}")
        if not (probe["singbox"] and probe["wg"] and probe["port"]):
            log(f"raw probe: {probe.get('raw', '')}")
            log("recovering down services...")
            probe = recover_services(probe)
            if probe.get("error"):
                log(f"ERROR: recovery probe failed ({probe['error']}).")
            else:
                log(f"after recovery: singbox={'OK' if probe['singbox'] else 'DOWN'} "
                    f"wg={'OK' if probe['wg'] else 'DOWN'} port2080={'OK' if probe['port'] else 'DOWN'}")
                if not (probe["singbox"] and probe["wg"] and probe["port"]):
                    log("ERROR: some services still down after recovery; will retry next round.")

    hb = heartbeat_public_url()
    if hb is None:
        log("public heartbeat skipped (no URL configured).")
    elif isinstance(hb, int):
        log(f"public heartbeat: HTTP {hb} (any response means the gateway path is alive).")
    else:
        log(f"WARN: public heartbeat failed ({hb}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
