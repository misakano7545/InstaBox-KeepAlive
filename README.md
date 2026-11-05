# InstaBox-KeepAlive

用 GitHub Actions 定时给 InstaBox 免费实例续租，实例掉了就唤醒/启动，再访问一次
Public URL 确认服务活着，挂了执行一条你指定的重启命令。

```
instabox_keepalive.py            保活主脚本（纯标准库，无依赖）
server.py                        PaaS 用的 HTTP 外壳（GET /keepalive）
Dockerfile                       PaaS 容器镜像
.github/workflows/keepalive.yml  手动触发（GitHub 的 cron 定时已关掉）
```

## 配置（一次性）

仓库 → Settings → Secrets and variables → Actions，新建四个 **Repository secret**：

| Secret | 说明 |
| --- | --- |
| `INSTABOX_API_KEY` | 仪表板建的 API key，权限需含 `machines:read` + `machines:write` |
| `INSTABOX_MACHINE_ID` | 机器 ID，形如 `m_xxxxxxxx` |
| `INSTABOX_PUBLIC_URL` | 对外访问地址，从 `GET /v1/machines/{id}/ports` 的 `ports[].url` 取（可留空，跳过存活检查） |
| `INSTABOX_RESTART_CMD` | 可选。URL 挂了时在机器内执行的重启命令，如 `docker compose -f /root/app/docker-compose.yml up -d` |

仓库是公开的，所以机器 ID 和地址也走 secret，不写进代码。

`INSTABOX_PUBLIC_URL` 留空会让整条自愈链断开：脚本会直接 `public URL 未配置，跳过存活检查` 然后退出，
后面的 `INSTABOX_RESTART_CMD` 永远执行不到。两个要么都配，要么都别指望它自愈。

## 机器侧：让服务自己不依赖保活也能活

保活脚本只能在你给定的命令里救一次；进程崩了要等下一轮。把服务交给 systemd 管，
崩了 5 秒内自己回来，机器重启也自动起，保活那条命令就退化成兜底。

```bash
# 机器内执行（示例见 machine/astrbot.service，按自己的路径改）
sudo cp machine/astrbot.service /etc/systemd/system/astrbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now astrbot
```

之后 `INSTABOX_RESTART_CMD` 填：

```bash
sudo cp -n /data/home/astrbot.service /etc/systemd/system/astrbot.service \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable astrbot \
  && sudo systemctl restart astrbot \
  && sleep 12
```

四个点都是有原因的：`/etc` 在 root disk 上，机器重建后 unit 会没，所以先把
`/data`（持久盘）那份 `cp -n` 装回去（`-n` 保证不覆盖手工改过的版本）；
`daemon-reload` 让新 unit 生效；`enable` 补回开机自启的软链；`restart` 对已停和运行中都成立。
末尾那个 `sleep` 是必需的：`systemctl restart` 是异步的，0.04 秒就返回，
而脚本在命令返回后只等 5 秒就去探活，服务还没绑上端口就会被判成"重启后仍不可达"。



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
2. 续 lease（TTL 3600s）；拿不到时按错误码分流：`machine_cooldown` / `operation_conflict` /
   `host_unavailable` 都是等下一轮（机器会自己回来，动手只会再撞一次），
   `machine_not_running` 才看 state 决定 `start`（stopped）还是 `wake`，然后带 backoff 重试续租；
3. 访问一次 `INSTABOX_PUBLIC_URL`，**2xx/3xx 才算服务可用**（404/502/超时都算挂了）。
   挂了就 exec 一次 `INSTABOX_RESTART_CMD`，5 秒后再访问一次确认。

返回码：`0` 正常，`1` 服务不可达（重启后仍不可达，或没配重启命令），`2` 鉴权或 lease 失败。
`1` / `2` 都会让运行变红，GitHub 会发失败邮件。

## 本机 systemd timer（推荐，取代 Pxxl / cron-job.org）

保活不需要 PaaS：任何一台 7×24 的机器 + 一个 timer 就够。

```bash
sudo install -d -m 700 /etc/instabox
sudo install -m 600 /dev/stdin /etc/instabox/keepalive.env <<'EOF'
INSTABOX_API_KEY=...
INSTABOX_MACHINE_ID=m_...
INSTABOX_PUBLIC_URL=https://...
INSTABOX_LEASE_TTL=1800
INSTABOX_RESTART_CMD=sudo cp -n /data/home/astrbot.service /etc/systemd/system/astrbot.service && sudo systemctl daemon-reload && sudo systemctl enable astrbot && sudo systemctl restart astrbot && sleep 12
EOF
sudo install -m 644 machine/instabox-keepalive.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now instabox-keepalive.timer
```

`LEASE_TTL` 要比触发间隔长（1800s > 10min），否则两轮之间 lease 先过期。
成败看 `journalctl -u instabox-keepalive.service`，unit 本身不会因为 exit 1/2 变红。

## 部署到 PaaS（Pxxl / Railway / Render 等）—— 备用方案


一次性脚本不能直接上：PaaS 的 Web Service 要求监听 `$PORT`、绑 `0.0.0.0`、能过健康检查，
跑完就退出的进程会被判定部署失败。所以仓库里另有：

```
server.py    HTTP 外壳：GET /keepalive 投递一轮、立刻 202；成功失败看日志
Dockerfile   python:3.12-slim + CMD python3 server.py
```

以 Pxxl 为例（它的 Cron Jobs 是"定时打你的 HTTP 端点"，不是跑命令）：

1. Deploy → 源选 GitHub 选本仓库，或源选 Dockerfile；类型 **Web Service**
2. 启动命令 `python3 server.py`（Dockerfile 源已经写在 CMD 里）
3. Secrets（scope 选 Project）配 `INSTABOX_API_KEY`、`INSTABOX_MACHINE_ID`、
   `INSTABOX_PUBLIC_URL`（`KEEPALIVE_TOKEN` 可选，见下）
4. Cron Jobs 新建：method `GET`，URL `https://<你的项目域名>/keepalive`，cron `*/5 * * * *`
5. 在 Cron 详情页看 next run，再手动 Trigger 一次，应返回 202、日志里出现 `OK: 服务可用。`

注意：

- `KEEPALIVE_TOKEN` **不配就是不校验**（`server.py` 里 `if TOKEN and ...`），此时任何知道域名的人
  打一下都会消耗你的 API 配额。不想多配一个口令就空着，域名本身算半个密码。
- 单次最坏约 2~4 分钟（wake/start 45s + lease 重试 30s + 重启 135s），所以 `/keepalive` 只投递、立刻
  回 202，不在请求里等——调用方的超时设多短都不会把一次真实的保活判成失败。
- Pxxl 的 Cron Jobs 只在 Plus / Pro / Enterprise 档提供；免费档只能让容器自己循环。

### 没有 cron 的平台（Cloady 这类纯容器 PaaS）

Cloady（NeptoLab）只有长驻容器，没有定时任务产品，但它不按需休眠，所以不需要 cron：
把 `KEEPALIVE_INTERVAL` 设成 `300`，`server.py` 就自己每 5 分钟跑一轮，`$PORT` 照样监听
（Cloady 用它做健康检查和地址）。免费额度：一个工作区 0.5 vCPU / 1 GB / 5 GB SSD /
100 GB 流量，这个脚本几十 MB 内存，够用；但新加坡区当前售罄，其余区是"首月免费试用"，
之后要绑卡。

## 免费的定时器：cron-job.org

cron-job.org 完全免费、无限个任务、最小 1 分钟粒度、支持任意 method + 自定义 header + 请求体，
可以直接替代付费的 Cron Jobs 档位。两种用法：

**A. 敲 GitHub 的 dispatch 接口（推荐，不需要 Pxxl）**

method `POST`，URL

```
https://api.github.com/repos/misakano7545/InstaBox-KeepAlive/actions/workflows/keepalive.yml/dispatches
```

header 两行：`Authorization: Bearer <你的PAT>` 和 `Accept: application/vnd.github+json`，
body `{"ref":"main"}`，cron `*/5 * * * *`。

PAT 用 fine-grained、只授权这一个仓库的 `Actions: write`。接口立刻返回 204
（已实测），所以 cron-job.org 那 30 秒超时完全不影响它——真正跑在 Actions 里，
公开仓库不计费。

**B. 敲 Pxxl 的 `/keepalive`（当前选定的方案）**

`/keepalive` 是**投递式**的：收到请求就开一个后台线程跑那一轮，立刻返回 `202`，
不等结果。所以 cron-job.org 那 30 秒硬超时不影响它——机器睡着时那一轮要 90 秒以上，
照样不会被记成失败。

cron-job.org 里这样填：

| 字段 | 值 |
|---|---|
| URL | `https://<你的 Pxxl 项目域名>/keepalive` |
| 请求方法 | `GET` |
| Schedule | Every 5 minutes（或 Every 1 minute） |
| Headers | 留空（不配 `KEEPALIVE_TOKEN` 就不需要） |
| Timeout | 默认 30 秒即可 |

注意：

- 同一时刻只跑一轮。上一轮没结束（机器在醒来的 90 秒里）时，新请求直接跳过并在日志里写
  `上一轮还没结束，跳过本轮`，不会叠加。所以 1 分钟触发也是安全的。
- 202 只代表"收到"，真正的成败在 Pxxl 的 Logs 里（`OK: 服务可用。` / `ERROR: ...`）。
  cron-job.org 那边永远显示成功，别拿它当健康证据。
- 想知道状态就翻日志，`/keepalive` 不提供 `/status`。

## 已知限制

- **没有定时触发**：`schedule` 已注释掉。实测这个账号的 cron 会排队 5 小时以上才跑
  （同账号另两个仓库 10 天 35 次运行，全部迟到 4~7.5 小时），5 分钟粒度不可用。
  现在只能手动 Actions → Run workflow，或由外部每分钟/5 分钟 `gh workflow run` 敲门。
- `INSTABOX_LEASE_TTL` 仍是 3600，配合 5 分钟粒度的外部敲门留足余量。
- 实例连续运行 8 小时后会被平台停掉，届时由上面第 2 步的 `start` 分支拉起来，
  单个 run 最长约 2 分钟（含 wake/start 的 45 秒等待）。
- 存活判断只看 URL 根路径，如果服务的根路径本身就返回 4xx（只是 API，没有首页），
  这个检查会一直误报挂了——那就把 `public_url_status()` 里的 `PUBLIC_URL + "/"` 换成真实的健康检查路径。
- 重启命令由 `INSTABOX_RESTART_CMD` 提供，脚本不假设任何服务名或端口；不配就只报警不恢复。
