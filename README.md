# InstaBox-KeepAlive

用 GitHub Actions 定时给 InstaBox 免费实例续租，实例掉了就唤醒/启动，再访问一次
Public URL 确认服务活着，挂了执行一条你指定的重启命令。

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
| `INSTABOX_PUBLIC_URL` | 对外访问地址，从 `GET /v1/machines/{id}/ports` 的 `ports[].url` 取（可留空，跳过存活检查） |
| `INSTABOX_RESTART_CMD` | 可选。URL 挂了时在机器内执行的重启命令，如 `docker compose -f /root/app/docker-compose.yml up -d` |

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

**Public URL**：不要自己拼，直接问 API 要（端口发布后平台才是权威来源）：

```bash
curl -s -H "Authorization: Bearer $INSTABOX_API_KEY" \
  https://api.instabox.sh/v1/machines/$INSTABOX_MACHINE_ID/ports
```

返回里 `ports[].url` 就是可用地址，形如 `https://<port>-<machine-id>.instabox.tech`。
注意端口号是你发布时选的，不一定是 2080。留空则跳过存活检查。

配好后在 Actions → keepalive → Run workflow 手动跑一次，看日志确认：
`OK: lease active` + `public URL -> 200` + `OK: 服务可用。`

## 行为

每轮做三件事：

1. 查机器状态；
2. 续 lease（TTL 3600s）；拿不到且报 `machine_not_running` 就先 `wake`，还不行就 `start`，然后重试续租；
3. 访问一次 `INSTABOX_PUBLIC_URL`，**2xx/3xx 才算服务可用**（404/502/超时都算挂了）。
   挂了就 exec 一次 `INSTABOX_RESTART_CMD`，5 秒后再访问一次确认。

返回码：`0` 正常，`1` 服务不可达（重启后仍不可达，或没配重启命令），`2` 鉴权或 lease 失败。
`1` / `2` 都会让运行变红，GitHub 会发失败邮件。

## 已知限制

- **没有定时触发**：`schedule` 已注释掉。实测这个账号的 cron 会排队 5 小时以上才跑
  （同账号另两个仓库 10 天 35 次运行，全部迟到 4~7.5 小时），5 分钟粒度不可用。
  现在只能手动 Actions → Run workflow，或由外部每分钟/5 分钟 `gh workflow run` 敲门。
- `INSTABOX_LEASE_TTL` 仍是 3600，配合 5 分钟粒度的外部敲门留足余量。
- 实例连续运行 8 小时后会被平台停掉，届时由上面第 2 步的 `start` 分支拉起来，
  单个 run 最长约 2 分钟（含 `wait_running` 轮询）。
- 存活判断只看 URL 根路径，如果服务的根路径本身就返回 4xx（只是 API，没有首页），
  这个检查会一直误报挂了——那就把 `public_url_status()` 里的 `PUBLIC_URL + "/"` 换成真实的健康检查路径。
- 重启命令由 `INSTABOX_RESTART_CMD` 提供，脚本不假设任何服务名或端口；不配就只报警不恢复。
