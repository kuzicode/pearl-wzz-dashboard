# 今晚挖珍珠 · Pearl Sniper Dashboard

多平台 **GPU 自动抢租挖 $pearl** + **网页看板** 统一管理。

在 **RunPod / Vast.ai / TensorDock / Salad** 上自动扫描 GPU 价格,低于阈值就租下、跑矿机挖 **$pearl**(当前主用 **PearlHash** 矿池 + **WildRig Multi** 矿机镜像;架构支持多矿池切换/迁移,TW Pool / HeroMiners / PearlFortune 已下线),持续监控算力,对低效 / 不挖的机器自动销毁 / 换机控成本——全程用一个**网页看板**(IBM Carbon 风格 · 默认亮色可切暗色)查看与操作。

> ⚠️ 会真实花钱。模板默认 `enabled=false`、`create_enabled=false`(不租机);建议先只开「启用」不开「自动建机」跑一阵看日志/看板(只观察不下单),确认无误再开自动建机、小额实跑。

---

## 网页看板

- **总览**:钱包、在跑机器数、总算力(矿池实测)、累计租金/产出/折合利润、**挖矿成本**(每 $PRL 的电租成本,累计 + 最近 3h 两项,各对比实时币价提示盈亏),以及**按账号**列出在跑机器(单价/时长/算力/**产值 $/h**/**回本**(绿盈利 · 黄成本线附近 · 红亏损)+ 红色**回本线**行 + Salad「组」列 + 一键关闭),**自动关停**持续明显亏损的机器并留记录——**每个账号一个卡片**,卡片右上显示**账户余额**(Vast / RunPod 自动拉取;**Salad 从 portal 抓实时余额**;TensorDock 无余额 API,点余额处 ✎ 直接填一次当前余额,看板按消耗递减显示「估算余额 · 约 Yh 花完」)。
- **矿池**:默认 **PearlHash**(WildRig 矿机,池内 0% 抽水);另支持 **Kryptex**(KRig 矿机,PPS)。每账号「新抢矿池」下拉切池**只影响之后新租的机器**,在跑机器保持原池并照常监控回收;每个池自带宿主要求(如 Kryptex 需宿主 CUDA ≥ 13、Vast 可靠度 ≥ 0.98),抢租时自动按要求筛宿主,不支持该池矿机的平台会跳过建机。TW Pool / HeroMiners / PearlFortune 已下线。总览指标可**按池分别查看**,机器表显示每台在哪个池。
- **行情图表**:总览内嵌可折叠 **PRL/USDT K 线图**(Candlestick + EMA20/EMA60 + 成交量,周期 15m/1h/4h/1d,hover tooltip);实时币价自动从 SafeTrade 拉取,看板顶部显示「● 实时」。
- **Salad 真实 GPU/余额**:通过浏览器会话从 Salad portal 抓每台**真实单卡型号、单价、实时余额**(Salad 公共 API 不返回 GPU,portal 是唯一来源;一次性登录后 headless 静默续期)。**scid 缺失/过期时自动弹窗引导重登**(检测到连续抓空 → 弹有头浏览器,你过完 Turnstile/OTP 自动续上,无需手动重跑脚本;无 GUI 环境则降级为提示)。
- **逐实例低效治理**:salad 按**矿池权威算力逐实例**判定,某台低于其卡型号阈值并持续超时即自动 reallocate 换机(弹性多卡组按实例真实 GPU 取对应阈值)。
- **配置**:左侧栏「配置总览」(钱包/告警 + 各账号一览:状态 / 矿池 / 上限 / GPU 档 / 钱包)+ **按账号**列出——账号页分「**基础设置**」(① API key → ② 启用 → ③ 矿池 → ④ 最多同时租 / 时租上限 → ⑤ GPU 型号(下拉选型,附参考算力 / 建议出价 / 市场参考价,一键采用推荐)与价格/算力门槛,从上到下填完即可跑)与「**高级设置**」(worker 前缀 / 轮询 / 平台特定参数 / raw JSON,默认值通常无需改),**暂停/启动租用**,**重启应用**,以及**修改看板登录密码**。
- **多账号**:同一平台可配多个账号(如 2 个 Salad + 2 个 RunPod),各自独立监控/抢卡(见下「多账号」章节)。
- core 纯 Python 标准库(Salad GPU/余额功能需 **Playwright**,项目用 **uv** 管理);密码门保护。

---

## 快速开始

只有**一个配置文件 `.env`**(平台 API key + 看板登录都在里面),**一条命令起 / 停全部服务**。下面以只用 **RunPod** 为例(其它平台同理,复制对应模板即可):

```bash
# 0. 依赖: Python 3.11+ 与 uv(core 纯标准库; 只有 Salad 余额抓取需要 playwright)
#    装 uv: brew install uv     # 或 curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                          # 建 .venv + 装依赖

# 1. 复制模板(真实文件已被 .gitignore 保护, 不会提交)
cp .env.example .env
cp configs/config.runpod.example.json configs/config.runpod.json   # 用哪个平台就复制哪个; 看板只认 config.<平台>.json

# 2. 改两处
#    ① .env : 填 RUNPOD_API_KEY(不用的平台留空即可, 会自动跳过) + 改 DASHBOARD_PASSWORD(默认 123456 务必改)
#    ② configs/config.runpod.json : prl_address 改成【你自己的 prl1… 钱包】(占位符不改会拒绝启动),
#       并把 runpod.enabled / create_enabled 改为 true(模板默认 false = 不租机)
#       —— ② 也可以先不改, 起来后在看板里点: 配置总览填钱包 → 账号页 ①API Key ②启用 ③矿池 ④上限 ⑤GPU 档 → 保存 → 重启应用

# 3. 一条命令起全部(已启用账号的抢卡进程 + 网页看板)
bash scripts/start-all.sh                                  # Linux / macOS
powershell -ExecutionPolicy Bypass -File scripts\start-all.ps1   # Windows
#    停全部: bash scripts/stop-all.sh  /  scripts\stop-all.ps1

# 4. 浏览器打开 http://localhost:8787, 登录 admin / 你设的 DASHBOARD_PASSWORD
```

> **云服务器上访问看板**:看板默认只监听 `127.0.0.1`(安全)。两种做法:① 前置 Caddy/Nginx 反代出 HTTPS 域名(推荐,Caddyfile 一行 `reverse_proxy 127.0.0.1:8787`);② 在 `.env` 设 `DASHBOARD_HOST=0.0.0.0` 后重启,直连 `http://<服务器IP>:8787`(明文暴露公网,务必强密码)。
>
> 钱包、key、密码、GPU 门槛等都能在看板**配置页**里改:「配置总览」填钱包 → 各账号页按「基础设置」①→⑤ 填完 → 保存 → 「重启应用」生效。看板里还能**查看各平台后台日志**、暂停/启动租用、一键关闭某台机器。

### 默认配置是什么

| 项 | 模板默认 | 说明 |
|----|------|------|
| `<平台>.enabled` / `create_enabled` | **false / false** | 不租机。`enabled=true, create_enabled=false` = 只观察不下单(看日志里的 observe hit 判断出价是否合理);两者都 true 才真租 |
| `pool` / `monitor_pools` | `pearlhash` | 矿池与算力监控来源;镜像 `kuzigmgm/pearl-miner:v13-wildrig` 随池自动决定 |
| `miner`(仅 Kryptex) | `krig` | 同一矿池的矿机变体:`krig`(KRig,需宿主 CUDA 13)/ `srbminer`(SRBMiner-MULTI,旧驱动可跑,dev fee 2%);配置页「矿机」下拉可切,只影响新租 |
| `max_active_instances` / `max_total_hourly_usd` | 1 台 / $1.0/h | **每账号独立**的花钱护栏,先小后大 |
| GPU 档 `thresholds` / `min_hashrate_th` | 4090 ≤$0.4 ≥220TH,5090 ≤$0.5 ≥250TH | 高于出价不租;实测算力持续低于门槛自动回收换机 |
| 自动关停 `AUTO_STOP_*`(.env) | 开 / 亏 20% / 机龄 30min / 持续 20min / 拉黑 6h | 看板每分钟按「算力 × 网络产率 × 币价」算每台产值,持续明显亏损才关停并让 sniper 拉黑;成本线附近、新机、Salad、数据过期不动。配置总览页可改 |
| `worker_prefix` | `auto` | 矿池里 worker 名前缀;多人/多账号同钱包请各自改成不同前缀 |
| `DASHBOARD_HOST` | `127.0.0.1` | 见上「云服务器上访问看板」 |

## 必须配置(否则白挖 / 跑不起来)

| 项 | 说明 |
|----|------|
| `prl_address`(每份 config)| **你自己的 $pearl 钱包**;占位符不改 sniper 会拒绝启动 |
| `.env` 的 API key | 启用平台的(RUNPOD / VAST / TENSORDOCK / SALAD),不用的留空 |
| `.env` 的 `DASHBOARD_PASSWORD` | 看板登录密码,**默认 `123456`,公网端口务必改掉** |
| `max_active_instances` / `max_total_hourly_usd` | 花钱护栏,**先设小**(注意:**每平台独立计算**,非全局——4 平台各跑独立进程/独立 state,最坏情况是 `平台数 × 上限`;Salad 受其 group replica 数管,不计入这两项)|

Salad 需在其后台预建 container group(镜像 `kuzigmgm/pearl-miner:v13-wildrig`,env **必须填 `PRL_ADDRESS`**,镜像不带默认钱包、留空会拒绝启动)+ `SALAD_API_KEY`;TensorDock 需 SSH 密钥对:`ssh-keygen -t ed25519 -f keys/tensordock -N ""`(config 里 `ssh_key_path` / `ssh_private_key_path` 指向它)。

---

## 多账号(同平台多个账号)

每个平台可配**多个账号**,各自独立监控/抢卡、`state.*`/`logs/*` 隔离、护栏各算各的。`start-all` / `stop-all` 与看板会自动发现所有账号——**加一个账号零代码改动**:

```bash
# 例: 加第 2 个 Salad 账号
cp configs/config.salad.json configs/config.salad-2.json     # 文件名加后缀 -2
#   改 config.salad-2.json:
#     "api_key_env": "SALAD_API_KEY_2"        ← 指向第 2 个 key
#     salad.organization_name / project_name  ← 改成账号 2 的
echo 'SALAD_API_KEY_2=<账号2 的 key>' >> .env                 # .env 加对应 key
bash scripts/stop-all.sh && bash scripts/start-all.sh        # 重启, 看板自动多出该账号卡片
```

- **命名约定**:`config.<平台>.json` = 账号 1;`config.<平台>-<N>.json` = 账号 N。`.env` 里对应 key 用 `<标准名>_<N>`(如 `RUNPOD_API_KEY_2`),由 config 的 `api_key_env` 字段指向。
- **账号标签**自动按「平台-标识」显示(Salad 用组织名,如 `salad-duffett` / `salad-mrkidbk`);想自定义在 config 加 `"account_label": "..."`。
- **同钱包多账号**:各账号 `prl_address` 可相同,但矿池 worker 名 / Salad 容器组名要全局不冲突(如各账号用不同 `worker_prefix`、不同组名)。
- **护栏按账号独立**:`max_active_instances` / `max_total_hourly_usd` 各账号各算,最坏总花费 = 各账号上限之和。
- 「暂停租用」按账号独立(`control/<账号>.rent-paused`);暂停只停下单,监控与低效回收照常。

---

## 安全

- 看板在 `服务器:端口` 上、能填 key + 启停真实租机,**唯一防线是密码——务必改掉 `.env` 里默认的 `DASHBOARD_PASSWORD=123456`**。
- `.gitignore` 已保护 `.env`(含 key + 看板密码)/ `keys/` / 真实 `config.*.json`(含钱包)/ `state.*.json` / `logs/` / `docs/`,不会被提交。
- 实际挖矿用矿机镜像(`kuzigmgm/pearl-miner:v13-wildrig`,基于 PearlHash 官方推荐的 **WildRig Multi**;在 PearlHash 池 0% 抽水),使用即信任该来源与 PearlHash 项目。
- **访客预览模式**:非管理员进入看板仅见演示占位页,实时数据(钱包/算力/收益/在跑机器)前后端均不下发,需部署自己的看板并登录管理员才可见——公开域名部署时保护隐私。

---

> 详细部署/调参/各平台说明在本地 `docs/`(不随仓库分发)。

## Salad GPU/单价/真实余额(可选, 需 Playwright)

salad 迁 twpool 后, 公共 API 不再提供 GPU 型号。开启后用浏览器会话从 portal-api 抓真实 `gpu_class` + 信用余额:

1. 装依赖: `uv sync && uv run playwright install chromium`
2. 一次性登录(有头, 每个 salad 账号一个隔离窗口): `uv run python salad_login.py`
   - 在弹出的窗口里登录对应账号(过 Turnstile/OTP), 完成后回终端按回车保存会话。
   - 会话存到 `secrets/salad_session_<账号>.json`(已 gitignore, 切勿提交)。
3. 重启 dashboard。常驻 headless 浏览器会自动续 Cloudflare 通行证并定时刷新 GPU/余额。
4. **scid 缺失/过期自动重登(半自动)**:dashboard 检测到某账号会话缺失、或连续 2 轮抓取全空(scid 过期)且过冷却(默认 30min)→ **自动弹有头浏览器**到该账号 portal 登录页;你人工过 Turnstile/OTP,登录后**自动检测完成并存会话、续上抓取**(无需手动重跑第 2 步)。超时(默认 10min)未登完则放弃。
   - 需本机有图形界面;**无 GUI 环境(ssh/服务器)弹窗失败 → 自动降级**为日志提示,回到手动 `salad_login.py` 流程。

未装 Playwright / 未登录时, 整套静默跳过, 不影响其它功能。
