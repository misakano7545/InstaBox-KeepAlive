# InstaBox-KeepAlive

用 GitHub Actions 定时给 InstaBox 免费实例续租，实例掉了就唤醒/启动，并检查机器内
`sing-box`、`wg0`、`2080` 端口，挂了自动重启。

```
instabox_keepalive.py            保活主脚本（纯标准库，无依赖）
.github/workflows/keepalive.yml  每 5 分钟跑一次 + 手动触发
```

## 配置（一次性）

仓库 → Settings → Secrets and variables → Actions，新建三个 **Repository secret**：

| Secret | 说明 |
| --- | --- |
| `INSTABOX_API_KEY` | 仪表板建的 API key，权限需含 `machines:read` + `machines:write` |
| `INSTABOX_MACHINE_ID` | 机器 ID，形如 `m_xxxxxxxx` |
| `INSTABOX_PUBLIC_URL` | 对外访问地址，如 `https://2080-xxxx.instabox.tech`（可留空，跳过心跳） |

仓库是公开的，所以机器 ID 和地址也走 secret，不写进代码。

### 这几个值怎么拿

**Machine ID** 三个途径，按省事程度排：

1. 从访问地址反推：地址形如 `https://2080-<machine-id>.instabox.tech`，
   把 `2080-` 后面的那段取出、加上 `m_` 前缀，就是 Machine ID
   （例：`2080-abc123xyz.instabox.tech` → `m_abc123xyz`）。
2. Dashboard `https://dashboard.instabox.sh/` 点进机器，详情页 URL 里带 `m_xxx`。
3. 有 key 之后一次列全（多台机器时用）：

```bash
curl -s -H "Authorization: Bearer $INSTABOX_API_KEY" \
  https://api.instabox.sh/v1/machines | python3 -m json.tool
```

**API Key**：`https://dashboard.instabox.sh/dashboard/api-keys` 新建，范围勾
`machines:read` + `machines:write`。只勾 read 会在续租时拿到 403。

**Public URL**：就是平时访问 2080 的那个域名，`https://2080-<id>.instabox.tech`。留空则跳过心跳，
其余功能不受影响。

配好后在 Actions → keepalive → Run workflow 手动跑一次，看日志确认：
`OK: lease active` + 三项 probe 都是 `OK`。

## 行为

每轮做四件事，任一步失败都会让本次运行变红（GitHub 会发失败邮件）：

1. 查机器状态；
2. 续 lease（TTL 3600s）；拿不到且报 `machine_not_running` 就先 `wake`，还不行就 `start`，然后重试续租；
3. 在机器内 `exec` 探测 `sing-box` / `wg0` / 2080 端口，缺哪个补哪个（`start-wg.sh` / `systemctl restart sing-box`），再探一次；
4. 请求一次公网地址，确认网关链路通。

返回码：`0` 正常，`1` 配置缺失，`2` 鉴权或 lease 失败。

## 已知限制

- **5 分钟是 GitHub 的下限**，高峰期 cron 可能延迟 10 分钟以上；因此 TTL 设为 1 小时，
  单次延迟不会让实例睡过去。
- **公开仓库连续 60 天没有任何提交，GitHub 会自动停掉 scheduled workflow**；
  想长期稳定，隔一两个月手动 Run 一次或提交一次。
- 实例连续运行 8 小时后会被平台停掉，届时由上面第 2 步的 `start` 分支拉起来，
  单个 run 最长约 2 分钟（含 `wait_running` 轮询）。
- 恢复命令里的 `start-wg.sh` 路径、`sing-box` 服务名、2080 端口是按当前机器写死的，
  换机器要改脚本里的 `_PROBE_CMD` / `recover_services`。
