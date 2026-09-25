# 今晚挖珍珠 · Pearl Sniper Dashboard

在 **RunPod / Vast.ai / TensorDock / Salad** 上自动扫描 GPU 报价,低于你设的出价就租下、跑矿机挖 **$pearl**,持续按矿池实测算力和实时币价算每台机器**赚不赚**,不挖 / 亏钱的机器自动销毁换机——全程用一个**网页看板**查看与操作。core 纯 Python 标准库,零依赖;多账号;密码门。

> ⚠️ 会真实花钱。模板默认 **不租机**(`enabled=false`、`create_enabled=false`,1 台 / $1/h 护栏)。建议先只开「启用」不开「自动建机」看一阵日志(只观察不下单),再小额实跑。

---

## 快速开始

```bash
# 0. 依赖: Python 3.11+ 与 uv(brew install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh)
uv sync

# 1. 复制模板(真实文件已被 .gitignore 保护)
cp .env.example .env
cp configs/config.runpod.example.json configs/config.runpod.json   # 用哪个平台就复制哪个模板

# 2. 改两处
#    .env                      → 填要用的平台 API key(不用的留空会自动跳过) + 改 DASHBOARD_PASSWORD(默认 123456)
#    configs/config.runpod.json → prl_address 改成【你自己的 prl1… 钱包】(占位符不改会拒绝启动)
#    其余(启用 / 矿池 / 上限 / GPU 档)都可以起来后在看板配置页改

# 3. 起 / 停全部(已启用账号的抢卡进程 + 看板)
bash scripts/start-all.sh        # Windows: powershell -ExecutionPolicy Bypass -File scripts\start-all.ps1
bash scripts/stop-all.sh         #          scripts\stop-all.ps1

# 4. 浏览器打开 http://localhost:8787 登录
```

看板配置页顺序:「配置总览」填钱包 → 账号页 ① API Key → ② 启用 / 自动建机 → ③ 矿池(默认 Kryptex)与矿机(默认 SRBMiner)→ ④ 最多同时租 / 时租上限 → ⑤ GPU 档(下拉选型号,行下有**参考算力 / 建议出价 / 市场参考价**,点「采用推荐」回填)→ 保存 → 重启应用。

> **云服务器**:看板默认只听 `127.0.0.1`。推荐前置 Caddy/Nginx 反代 HTTPS(`reverse_proxy 127.0.0.1:8787`);或 `.env` 设 `DASHBOARD_HOST=0.0.0.0` 直连(明文公网,务必强密码)。

---

## 看板能做什么

- **总览**:在跑机器 / 矿池实测算力 / 累计租金 / 累计产出(PRL,次行给折合美元与平均每小时产币)/ 累计折合利润(次行给**预计 +$X/h · +$Y/天**,悬停看算式),可按矿池切换视图;**网络产率 · 回本线($/PRL)· 在跑产值 vs 时租**一行看全局;PRL/USDT K 线。
- **机器表**(每账号一张):单价 / 时长 / 算力 / **PRL 成本价 $/PRL**(挖到 1 PRL 花的租金,高于币价即亏) / **产值 $/h** / **利润率**(距回本线的距离:绿赚 · 黄回本线附近 · 红亏),表头下一条红色**回本线**(= 当前币价);成本价与利润率两列可点击排序,默认最贵在上;一键关闭单台。
- **自动关停**:机龄 ≥30 分钟、产值低于租金 20% 以上并持续 20 分钟才关停,并让抢租进程拉黑该宿主 6 小时;成本线附近、新机、数据过期、Salad 一律不动。`.env` 的 `AUTO_STOP_*` 或配置总览页可改。
- **数据分析**:各矿池能效对比(每 100TH 租金、累计成本 $/PRL、实测产率 vs 全网理论、矿池效率、池费)。
- **低效回收**:抢租进程按矿池实测算力逐台判定,低于该型号门槛持续超时即销毁换机(Salad 为 reallocate),不挖的机器不会一直烧钱。
- **配置页**:多账号各一页,基础设置从上到下填完即可跑;高级设置(worker 前缀 / 轮询 / 平台特定参数 / raw JSON)默认不用动;查看各进程日志、暂停/启动租用、改看板密码。

## 矿池与矿机

| 矿池 | 矿机镜像 | 说明 |
|---|---|---|
| **Kryptex**(默认推荐) | SRBMiner-MULTI `kuzigmgm/pearl-miner:srb-3.6.9-r3`;可选 KRig `krig-1.5.2` | PPS+ 按份额稳定结算(每满 1 PRL 自动付),池费 2%。SRBMiner 旧驱动宿主也能跑、起不来会快速失败换机,dev fee 2%;KRig 0% fee 但宿主需 CUDA 13(≥580 驱动),RunPod 社区机多半起不来 |
| **PearlHash** | WildRig Multi `kuzigmgm/pearl-miner:v13-wildrig` | 按小时 epoch 分配、有运气波动,池费 1%,OpenCL;作为对照或备选;TensorDock 只支持此池 |

模板默认 Kryptex + SRBMiner。每账号「新抢矿池」/「矿机」下拉切换,只影响之后新租的机器;在跑机器保持原池继续监控回收。PearlHash 是 PRL 的算法名,各卡参考算力两个池通用。

### 默认配置是什么

| 项 | 模板默认 | 说明 |
|----|------|------|
| `<平台>.enabled` / `create_enabled` | **false / false** | 不租机。`enabled=true, create_enabled=false` = 只观察不下单;两者都 true 才真租 |
| `max_active_instances` / `max_total_hourly_usd` | 1 台 / $1.0/h | **每账号独立**的花钱护栏,先小后大;最坏总花费 = 各账号上限之和 |
| `pool` / `miner` | `kryptex` / `srbminer`(TensorDock 模板为 `pearlhash`) | 镜像随池与矿机自动决定 |
| GPU 档 `thresholds` / `min_hashrate_th` | RunPod 4090 ≤$0.34 ≥250TH,5090 ≤$0.45 ≥300TH;Vast 4090 ≤$0.30,5090 ≤$0.42 | 按 2026-09 行情(RunPod 社区价 0.34,回本线 ≈$0.35/h)。行情变了用配置页的「建议出价」重算 |
| RunPod `cloud_types` / `country_codes` | `["COMMUNITY"]` / `[]` | 秘密云 4090 $0.74 永不命中;不限国家扩大宿主池 |
| Vast `min_offer_price_usd` / `max_offer_price_usd` / `min_reliability` | 0.03 / 0.6 / 0.95 | 粗筛区间与可靠度;每型号上限由 GPU 档决定 |
| TensorDock | `enabled: false`,轮询 30s | 实验性:裸机 v10 矿机、仅 PearlHash 池、存储/vCPU 附加费未计;轮询 <30s 会被 429 限流 |
| 自动关停 `AUTO_STOP_*`(.env) | 开 / 亏 20% / 机龄 30min / 持续 20min / 拉黑 6h | 见上「自动关停」 |
| `worker_prefix` | `auto` | 矿池 worker 名前缀;同钱包多人/多账号请各用不同前缀 |
| `DASHBOARD_HOST` | `127.0.0.1` | 见上「云服务器」 |

### 必须配置(否则白挖 / 跑不起来)

- `prl_address`(每份 config):**你自己的 $pearl 钱包**,占位符不改会拒绝启动。
- `.env` 里启用平台的 API key;`DASHBOARD_PASSWORD` 默认 `123456`,公网务必改。
- Salad:需在其后台预建 container group(镜像推荐 `kuzigmgm/pearl-miner:srb-3.6.9-r3`(Kryptex);PearlHash 用 `v13-wildrig`;env **必须填 `PRL_ADDRESS`**,建议 2 vCPU + 2 GB)+ `SALAD_API_KEY`;台数由 replica 决定,不计入上面两项护栏。
- TensorDock:需 SSH 密钥对 `ssh-keygen -t ed25519 -f keys/tensordock -N ""`。

---

## 多账号

同一平台可配多个账号,各自独立进程 / state / 日志 / 护栏,`start-all`、`stop-all` 与看板自动发现:

```bash
cp configs/config.salad.json configs/config.salad-2.json   # 文件名加 -N
#   改 config.salad-2.json: "api_key_env": "SALAD_API_KEY_2"(salad 另改 organization_name / project_name)
echo 'SALAD_API_KEY_2=<账号2 的 key>' >> .env
bash scripts/stop-all.sh && bash scripts/start-all.sh
```

- `config.<平台>.json` = 账号 1;`config.<平台>-<N>.json` = 账号 N;`.env` 用 `<标准名>_<N>`,由 config 的 `api_key_env` 指向。
- 账号标签默认「平台-标识」,可在 config 加 `"account_label"`;同钱包多账号请用不同 `worker_prefix`。
- 「暂停租用」按账号独立,只停下单,监控与回收照常。

## 安全

- 看板能填 key、启停真实租机,**唯一防线是密码**——务必改掉默认 `123456`,公网用 HTTPS 反代。
- `.gitignore` 已保护 `.env` / `keys/` / `secrets/` / 真实 `config.*.json` / `state.*.json` / `logs/` / `docs/`。
- 「偷窥模式」(访客)是**只读**:能看到总览与机器表的实时数据(钱包地址、算力、收益),不能改配置、不能关机。不想让外人看到收益就别把看板暴露在公网,或只走 VPN / 反代鉴权。
- 矿机镜像 `kuzigmgm/pearl-miner:*` 由本项目构建(WildRig Multi / SRBMiner-MULTI / KRig 官方二进制 + 入口脚本),使用即信任该来源与各矿机作者。

## Salad 真实 GPU / 余额(可选,需 Playwright)

Salad 公共 API 不返回 GPU 型号与余额,看板可用浏览器会话从 portal 抓取:`uv sync && uv run playwright install chromium` → `uv run python salad_login.py`(有头登录一次,会话存 `secrets/`)→ 重启看板。会话过期时看板会自动弹窗引导重登(无 GUI 环境降级为日志提示)。未装 / 未登录则整套静默跳过。

**没有 portal 会话也能记账**(2026-09 起):GPU 型号改从矿机容器日志识别,单价按容器组优先级查价表;型号还没解析出来的在跑实例按同组已知单价的中位数估算(单价列显示 `~$`)。租金累计在 portal 余额不可用时退化为「单价 × 在跑时长」,看板「累计租金」标注**含估算**;日后会话恢复,真实扣费会先抵扣这段估算量,不重复计。余额可在 Salad 卡片上**手填**(点余额处),之后按实测时租递减并给出「约 X h 花完」,用作充值提醒。

> 已知边界:整组同时冷启动的头十几分钟,所有实例都还没解析出型号,中位数无从取值,这段时间不计费(金额很小)。单台 reallocate 换机不受影响。

---

变更记录见 `CHANGELOG.md`;部署 / 调参 / 各平台细节在本地 `docs/`(不随仓库分发)。
