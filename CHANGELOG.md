# Changelog

本文件记录「今晚挖珍珠 · Pearl Sniper Dashboard」的重要变更。

## [建机超时修复(ISS-024) + 国家排除名单] — 2026-09-23

### Fixed — 修复
- **建机超时被 status_msg 短路(ISS-024)**:`timed_out` 原含 `and not status_msg`,宿主拉不动 Docker Hub 时 Vast 一直刷 `"<layer>: Retrying in 2 seconds"`,进度消息非空 → 永不超时;而 "Retrying" 不在 `bad_status_patterns` 里,`startup_error` 也走不到,只能等宽限 1800s + 低效 900s 共 **45 分钟**才被清掉,期间白烧租金(实测一批 10 台空转 48 分钟)。现去掉该短路,并新增两类判定,任一命中且超过 `creating_timeout`(600s)即回收拉黑:
  - `pull_stall_patterns`(retrying in / pulling fs layer / downloading / extracting / waiting)→ 原因标 `image_pull_stalled`;
  - `never_reported`(`actual_status` 与 `status_msg` 同时为空)→ 原因标 `container_never_started`。容器真起来后 Vast 必给 `actual_status='running'` 与 `status_msg='success, running <image>'`,两者皆空即容器从未创建。
  - 实测回收时间从 45 分钟降到 10.5 分钟。`tests/test_creating_timeout.py` 9 条断言,含健康实例 / 刚建机器 / 有成功消息三条不误伤用例。

### Added — 新增
- **`vast.block_countries`**(硬排除,默认空):按国家码尾段匹配(不做子串匹配,"CN" 不会命中 "Cincinnati"),优先级高于 `prefer_countries`,只影响新租,已在跑的机器不受影响。国内宿主系统性拉不动 Docker Hub,租了也不产出。配置页「平台特定参数」可编辑。`tests/test_block_countries.py` 13 条断言。
- 模板 `config.vast.example.json` 补上 `block_countries: []`(空 = 不排除)与 `max_instances_per_machine: 2`,让新用户看得到这两个开关;`test_example_configs` 加断言防止模板误带排除国家。

## [总览卡片: 产出以美元为主 + 预计每小时利润] — 2026-09-25

### Changed — 变更
- **「累计产出」卡片**:主行保持 PEARL 数量,次行给折合美元与平均每小时产币(`≈ $887.28 · 平均 5.5591 PEARL/h`);已确认/待成熟明细在主数值的悬浮提示里。
- **「累计折合利润」卡片次行换成预计每小时利润**:显示 `预计 +$1.83/h · +$43.92/天`(日单位用「天」,与看板既有的 `PRL/TH·天` 一致)(按盈亏着色,小时与日收益一眼可见),鼠标悬停显示算式 `当前算力预计产值 $9.29/h − 租金 $7.46/h = +$1.83/h; 按此速率 24h 约 +$43.92`;无在跑机器时显示"暂无在跑机器";原口径说明(产出折合 − 累计租金,以及跨期口径警告)移入主数值悬浮提示。summary 新增 `profit_usd_h`,`.tip` 悬浮样式扩展到卡片次行。

### Fixed — 修复
- **`build_summary` 的时租口径与产值不一致**:`current_hourly_usd` 此前统计全部机器,而 `value_usd_h_total` 只统计在跑机器,两者相减会被 Salad 的 `allocating`/`creating`/`downloading` 实例拉低。现两侧统一走 `_is_running`,与 `tick_spend`、`build_rentals` 一致。同一问题也影响数据分析面板的池级「理论盈亏」(非 running 机器只进租金不进产值,系统性低估 margin),一并修正。新增 `tests/test_profit_hourly.py` 锁定该口径;`test_machine_economics` 中一条断言原先锁的是旧的不一致口径,已改写。

## [机器表改「PRL 成本价」列 + Salad 租金漏算修复(ISS-025)] — 2026-09-25

### Changed — 变更
- **机器表 `$/100TH·h` 列换成「PRL 成本价 $/PRL」**:挖到 1 PRL 花的租金 = 单价 ÷ 每小时到手产币量,与币价同单位,高于币价即亏。后端 `machine_economics` 新增 `cost_usd_per_prl`(恒等 `币价 ÷ (1 + 利润率/100)`),前端不再自行换算。
- **表头可点击排序**:PRL 成本价与利润率两列均可点,默认成本价降序(最贵在上),状态存 `localStorage.mtab_sort`;无算力的机器(宽限中/未连池)始终排最前。
- **回本线红行改用币价口径**:显示 `$<币价>/PRL`,文案「成本价高于此值 = 租金超过产值」;单机标红条件同步改为高于币价或高于本账号中位数 15%。

- **口径统一**:顶部 econ 条的「回本线」由 `$/100TH·h` 改为 `$<币价>/PRL`,与机器表新列同口径(此前两处并存、单位不同容易困惑);自动关停配置页说明补上等价阈值「PRL 成本价 > 币价 ÷ (1 − 阈值)」并显示当前换算值,配置页接口新增 `coin_price_usd`。**自动关停的判定逻辑未变**——它一直是 `产值 < 单价 × (1 − 阈值)`,与 `margin_pct`、`cost_usd_per_prl` 三者等价,已加测试锁定。矿池能效面板(paPanel)的「每 100 TH/s 租金」是池级累计口径,保持不变。

### Fixed — 修复
- **Salad 租金一直没计入累计(ISS-025)**,线上实测累计租金 $623.11 中 Salad 贡献为 0。三条独立根因:
  1. `tick_spend` 把 Salad 排除出 `price×time`,只认 portal 余额下降,而余额拿不到时直接 `continue` —— portal 会话 2026-06-18 即过期(`salad_balance_prev` 恒为空字典可证),于是永远不累加。现改为退化按 `price×time` 估算,并用 `salad_estimated_usd` 记账:portal 日后恢复时先从真实扣费里抵扣已估算部分,不会重复计数;`summary.rent_has_estimate` 透出,看板「累计租金」标注「含估算」。
  2. GPU 型号识别不到 → 单价 `None` → 该机按 $0 计。现 running 但型号未知的机器按**同容器组已知单价的中位数**估算(组内无已知则退账号中位),标 `price_estimated`,单价列显示 `~$x.xxx/h`,型号识别出来后自动换回真实价。
  3. 容器组白名单 `include_container_groups` 过期(配了已删除的 `kuzi-miner-1`,漏掉新组)→ 整组静默漏算。服务器与模板均改为留空走 API 自动发现;白名单里配了却拉不到详情的组现在会告警一次(复用已有请求,不增加 API 调用)。
- **非 running 的 Salad 实例不再计费**:`allocating`/`creating`/`downloading` 此前被算进 `burn_hourly` 与当前 $/h(反向高估),现与 `value_usd_h` 统一走 `_is_running`。
- **优先级档位兜底**:`salad_inst_price_num` 在 `SALAD_GPU_PRICES` 缺该档(如 `batch`)时退到 `low` 档而非返回 `None`,避免整台机器不计费。

### Docs
- README 总览与 Salad 章节按新版重写:说明「没有 portal 会话也能记账」的完整链路(日志识别型号 → 按组优先级查价 → 型号未知取同组中位价 → portal 不可用时按 price×time 估算并标注),以及余额手填配合「约 X h 花完」作为充值提醒;看板内「工具说明」的仪表盘条目同步。
- 已知边界写入文档:整组同时冷启动的头十几分钟,所有实例都没解析出型号、中位数无从取值,这段时间不计费(金额很小,单台 reallocate 不受影响)。

### Tests
- `test_machine_economics` 补 PRL 成本价与恒等式/排序等价断言;`test_salad_rent_balance` 改写(原先把「portal 拿不到 → 不累加」当成期望行为断言,正是本 bug);新增 `test_salad_cost`(档位兜底/中位价/非 running 不计费)。67 → 68 个测试文件全绿。

## [Vast 单宿主实例上限] — 2026-09-23

### Added — 新增
- **`vast.max_instances_per_machine`(默认 2)**:放开 `min_gpu_frac` 后一台多卡主机会被连开多台(实测一台宿主被开了 4 个实例),宿主一旦出问题(驱动 / 功耗墙 / 断网)整组一起废,且同宿主实例共享 CPU / 网络会互相挤占。下单前按 `machine_id` 统计本账号同宿主在跑数量,达上限即跳过并记日志;设 0 = 不限制。存量超限实例不强制下线,上限只作用于新租。新增 `sniper.machine_instance_count()` 与 `tests/test_per_machine_cap.py`。

### Changed — 变更
- 租用成功时即写入 `machine_id`(此前只在低效销毁时才写);`reconcile_vast_instances` 每轮从实例信息回填 / 刷新 `machine_id`,存量记录自动补齐。

## [review 修复: RunPod 宽限按机器所属池 + 共享拉黑缓存分平台] — 2026-09-22

### Fixed — 修复
- **RunPod 回收宽限取错池**:`reconcile_runpod_instances` 原在循环外按账号当前新租池 `active_pool(config)` 算一次 grace;账号切池后仍在旧池挖的机器会失去旧池要求的宽限(Kryptex ≥1800s → 降到账号自设 600s),可能被提前判低效回收。现移入逐台循环、按 `rental_pool(rented)` 取,与 vast / salad 路径一致。新增 `tests/test_runpod_grace_per_pool.py`。
- **同平台共享拉黑缓存未按平台分桶**:`_sibling_bl` 原为全局单份,一个进程线程并行跑多个平台时 60s 内会互相拿到对方平台的名单;且 `ts=0` 哨兵在 `monotonic()` 初值 <60s 的环境(刚启动的容器)会把首次加载当成有效缓存跳过。现按 provider 分桶、加锁、未加载用 None 显式判断;测试补两条用例。

## [登录页偷窥入口 + 模板镜像跟随默认矿机] — 2026-09-22

### Changed — 变更
- 登录页「偷窥模式」由一行灰字改成醒目的虚线胶囊入口:👉 手指左右点动引导 + 「没有密码?点下面进访客模式」提示 + 两行文案(偷窥模式 · 仅看仪表盘 / PEEK MODE · 无需密码),支持键盘 Enter/空格,`prefers-reduced-motion` 下停止动效。
- 登录页与侧栏版本标识 v1 → **v2**。
- 模板 `config.runpod / vast / salad.example.json` 的 `image` 改为 `kuzigmgm/pearl-miner:srb-3.6.9-r3` 并显式写 `miner: srbminer`,与默认推荐(Kryptex + SRBMiner)一致(此前 image 仍是 v13-wildrig,虽被 `effective_image` 按 pool 覆盖但看着误导);`test_example_configs` 新增断言。

### Fixed — 修复
- 钱包复制图标 SVG `ry=2/>` 无引号写法被解析成 `"2/"`,控制台每次渲染报错;改为 `ry=2 />`。

## [Kryptex 设为默认推荐方案] — 2026-09-22

### Changed — 变更
- 看板矿池按钮 / 下拉顺序改为 **Kryptex 在前**(`POOL_DISPLAY_ORDER`);GPU 目录建议出价按当前默认矿池的池费算(Kryptex 2%)。
- 模板 `config.runpod / vast / salad.example.json` 默认 `pool: kryptex`、`prl_host: stratum+ssl://prl.kryptex.network:8048`、`monitor_pools: [kryptex]`(矿机默认 SRBMiner);TensorDock 模板仍 pearlhash(仅支持该池)。
- 工具说明 / 挖珠教程 / README:矿池与矿机表 Kryptex 置顶并标"默认推荐",说明 PPS+ 稳定结算与 SRBMiner 驱动容错;Salad 建组镜像推荐 `srb-3.6.9-r3`、2 vCPU + 2 GB。

## [同平台账号共享拉黑名单] — 2026-09-22

### Added — 新增
- `sibling_blacklist(provider)`:抢租时只读同平台其它账号的 `state.<provider>*.json` 拉黑条目(60s 缓存, 过期条目不算),`is_blacklisted` / `machine_blacklisted` 同时看本账号 + 共享名单。起因:vast-2 刚因功耗墙拉黑的 3080 宿主, vast-1 切池后第一单就租到了同一 offer。

### Changed — 变更
- 运维:vast-1 切到 Kryptex + SRBMiner,并套用 vast-2 的 GPU 档 / 过滤 / 宽限(在跑 PearlHash 机器不受影响,monitor_pools 保留 pearlhash)。

## [Kryptex 30m 均值按 worker 上线时长还原(ISS-022)] — 2026-09-22

### Fixed — 修复
- Kryptex 只给 `avg_hashrate_30m`,新 worker 不足 30 分钟时均值被窗口里的 0 拉低(pod 拉镜像 5–8 分钟才上池 → 宽限一到均值只有 240 TH,低于门槛 250 被误杀,一天 6 台好机)。新增 `kryptex_window_scale()`:按 `opened_at` 把均值放大 30/在线分钟(不足 10 分钟按 10 算,上限 ×3,≥30 分钟不变),sniper 回收判定与看板显示同口径。`tests/test_kryptex_window_scale.py`。

## [normalize_gpu 区分 Laptop 变体] — 2026-09-22

### Fixed — 修复
- `normalize_gpu` 把 "RTX 5090 Laptop GPU" 归一成 `RTX 5090 Laptop`(此前并入桌面 `RTX 5090`,Salad 移动卡会吃桌面门槛被误杀);`min_hashrate_th` / 目录可对 Laptop 单独配置。运维:Salad 各型号最低算力按参考算力 65% 填入(4090 195 / 5090 260 / 5090 Laptop 155 / 5070 Ti 110 / 4070 75 …),`default_min_hashrate_th` 40。

## [Salad 兜底价表更新为 Low Priority 实价] — 2026-09-22

### Changed — 变更
- `SALAD_GPU_PRICES` low 列按 2026-09-22 Salad 建组页 Low Priority 实价更新(4090 0.217、5090 0.333、5070 Ti 0.142、4070 0.097、4070 Ti 0.12、4070 Ti Super 0.13、3080 Ti 0.105、5080 0.187 …),并补 3070 / 3070 Ti / 3060 Ti / 4060 / 4070 Laptop / 3050 / 2080 / 2070 / 2060;medium/high 按比例估算。仅在 Salad gpu-classes 实时价取不到时兜底。

## [机器表「利润率」列 + Vast 功耗墙宿主提前回收] — 2026-09-22

### Changed — 变更
- 机器表 / 自动关停记录的「盈亏」列改名 **「利润率」**(= (产值 − 单价) ÷ 单价,即距回本线的距离,0% = 回本线);提示语同步。
- Vast 低效判定:算力来自容器日志(即时值)时,过了账号自己的 `hashrate_grace_seconds` 就开始判定,不再等矿池要求的 30 分钟宽限(仅在回退到矿池 30m 均值时才等)。实测 vast-2 两台被宿主限到 130–150 W 的 3080(28 / 43 TH/s)可提前 20 分钟回收。
- 运维:vast-2 的 RTX 3080 最低算力门槛 80 → 70(实测正常 111 TH/s,爬坡期留余量)。

## [0.2.0 上线检查: README 重写精简 + 模板与测试清理] — 2026-09-22

### Changed — 变更
- **README 重写精简**(129 → 107 行):快速开始 4 步、「看板能做什么」按功能归纳、新增「矿池与矿机」表(PearlHash+WildRig / Kryptex+SRBMiner 默认、KRig 可选)、默认配置表与必改项合并;删掉已下线矿池与「挖矿成本卡」等旧描述;**安全一节改为如实说明「偷窥模式」是只读但能看到实时数据**(此前写成"仅见演示占位页"与实际不符)。
- `pyproject.toml` 版本 0.1.0 → 0.2.0。
- 模板 `config.runpod.example.json` 删除死键 `start_ssh`;Salad 账号页镜像提示补 `srb → Kryptex`。

### Fixed — 修复
- 两个长期失败的旧测试改为当前口径:`test_full_config_pools`(Kryptex 默认镜像为 srb、下线池不在列表)、`test_salad_perf`(阈值 0.9s → 1.2s 防慢机误报)。全部 61 个测试通过;在干净 clone 上复制模板即可跑通测试,占位钱包按预期拒绝启动。

## [盈亏列改名 + Kryptex 付款跨域翻页修复 + 示例配置默认值更新 + TensorDock 限流修复] — 2026-09-22

### Fixed — 修复
- **Kryptex 已付款漏第 2 页(ISS-020 补充)**:payouts 每页 15 条,`next` 指向 `prl-api.kryptex.network`(非 `pool.kryptex.com`),旧判断停在第一页漏掉最早付款(本次 3 笔 5.26 PRL)。现按 host 后缀 `kryptex.com` / `kryptex.network` 跟随。
- **TensorDock 2s 轮询被 429 限流**导致从未租到机器:模板与真实配置轮询改 30s + `scan_backoff_base_seconds 60`。

### Changed — 变更
- 机器表 / 自动关停记录的「回本」列改名 **「盈亏」**(= (产值 − 单价) ÷ 单价);「回本线」名称不变。
- **示例配置默认值**按 2026-09 调研更新(仍默认不花钱):RunPod 仅 COMMUNITY、不限国家、容器盘 10 GB、4090 ≤0.34 / 5090 ≤0.45、最低算力 250/300、低效持续 900s;Vast 4090 ≤0.30 / 5090 ≤0.42、`min_offer 0.03` / `max_offer 0.6` / `min_reliability 0.95`;TensorDock 轮询 30s、存储 30 GB、删除死键 `seen_ttl_seconds` / `max_instance_age_starting_seconds` / `city`;Salad 最低算力 4090 240 / 5090 300。README「默认配置是什么」表同步。新增 `tests/test_example_configs.py`。
- 看板 TensorDock 高级设置移除死键 `seen_ttl_seconds`;账号页加实验性提示(裸机 v10 矿机、仅 PearlHash 池、附加费未计、轮询 ≥30s)。

## [Kryptex 池矿机变体: SRBMiner-MULTI 3.6.9 镜像(旧驱动可跑)] — 2026-09-22

### Added — 新增
- **矿机变体**:`POOLS["kryptex"]["miners"]` = `krig`(KRig 1.5.2, 需宿主 CUDA 13/≥580)/ `srbminer`(SRBMiner-MULTI 3.6.9, pearlhash 不硬性要求 CUDA 13, dev fee 2%),`default_miner=krig`。账号 config 顶层 `miner` 选变体;`effective_image()` / `pool_requires(pool, config)` 按变体取镜像与宿主要求(Vast `cuda_max_good`、RunPod `allowedCudaVersions` 随之不再强制 13.0);记账/基线仍按 kryptex 一个池。`pool_of_image` 认 `srb-*` 为 Kryptex。
- 配置页「新抢矿池」旁新增「矿机」下拉(池有变体时显示),`POST /api/set-miner`;要求提示按变体合并显示。`/api/full-config` 的 `pools[].miners / default_miner`、`platforms[].miner / image`。
- 新镜像 `kuzigmgm/pearl-miner:srb-3.6.9-r3`(源 `docker-srb/`;r1 因 SRBMiner **无 TTY 时完全不输出且秒退**而废弃, r2 用 util-linux `script` 提供伪终端 + 补 OpenCL ICD 注册;r3 加**快速失败**:矿机连续 3 次 30s 内退出即容器 rc=1 退出,让 RunPod/Vast 置为 EXITED、sniper 1 分钟内删除并拉黑宿主,不再白烧 30 分钟宽限期,`FAST_FAIL_COUNT=0` 可关):从 GitHub Release 包 COPY 二进制(`./fetch.sh <版本>` 下载);entrypoint 沿用 KRig 的短 rig 名 / PRL_ADDRESS 护栏 / DIAG 行,`PRL_HOST` 的 `stratum+ssl://` → `--pool h:p --tls true`,登录 `--wallet <addr>.<rig>`,`--api-enable` 后每 60s 从本机 API 打 `hashrate_th_s=` 结构化行(sniper 日志解析可用),`SRB_EXTRA_ARGS` 追加超频参数,退避重启循环。参数均经 3.6.9 `--help` 核对(无 `--disable-startup-monitor`)。
- 测试 `tests/test_pool_miner_variant.py`。

### Changed — 变更
- **Kryptex 默认矿机改为 SRBMiner**(`default_miner=srbminer`,`POOLS["kryptex"].image` 同步指向 `srb-3.6.9-r3`);要回 KRig 在账号 config 顶层设 `miner: "krig"`(配置页矿机下拉)。试跑通过:RunPod 4090 279 TH/s、份额 100% 接受。
- 运维:runpod-2 试跑通过后放开到 10 台 / $5/h;runpod-1 / runpod-2 的 4090 出价上限 0.30 → 0.35(RunPod 社区价 $0.34,0.30 永远租不到)。

## [单机经济性 + 自动关停亏损机 + Kryptex 已付修正 + GPU 目录推荐] — 2026-09-22

### Added — 新增
- **网络产率**:看板从 prlscan 最新区块(难度 + 出块奖励)算每 TH/s 每小时理论产币(`yield = 3600×reward/(difficulty×2^48/1e12)`,与 hashrate.no / Kryptex 全网算力校准;`YIELD_DIFF_SCALE` 可覆盖),10 分钟 serve-stale。`/api/summary` 新增 `yield_prl_per_th_h / yield_live / breakeven_usd_per_100th / value_usd_h_total`。
- **机器表「产值 $/h」「回本」列**:产值 = 算力 × 产率 × 币价 × (1−池费);回本 = (产值−单价)/单价,绿 盈利 / 黄 成本线附近 / 红 亏超阈值;表头下红色**回本线**行(`$/100TH·h` 高于它 = 租金超过产值),`$/100TH·h` 高于回本线也标红。总览卡片下新增一行:网络产率 · 回本线 · 在跑产值 vs 时租 · 自动关停状态。`/api/rentals` 每台机器新增 `value_usd_h / margin_pct / breakeven_usd_per_100th`。
- **自动关停亏损机**(默认开):看板每 60s 检查非-Salad 在跑机器,机龄 ≥30 分钟、产值 < 单价×(1−20%) 且持续 20 分钟才 terminate;币价/产率数据过期、账号进程没跑、成本线附近、新机一律不动;每 tick 最多关 2 台,候选超过在跑一半时暂停(防数据异常误杀)。`.env` `AUTO_STOP_ENABLED / AUTO_STOP_LOSS_PCT / AUTO_STOP_MIN_AGE_MIN / AUTO_STOP_PERSIST_MIN / AUTO_STOP_BLACKLIST_HOURS`,配置总览页「自动关停亏损机」可改(`POST /api/auto-stop-settings`,立即生效);总览新增「自动关停记录」。关停后写 `control/<账号>.blacklist-add` 交接给 sniper 拉黑该 offer/机器 6 小时(sniper 每轮 rename 后合并;拉黑条目支持 `expires_epoch` 过期)。
- **数据分析 · 矿池能效对比**(总览可折叠面板,与行情面板同款):每池一列对比 在跑/实测算力/时租/每 100TH 租金/理论产值与盈亏/累计租金·产出·利润/成本 $/PRL/每 $ 产币/累计算力小时/**实测产率**(累计产出 ÷ 算力小时)/**矿池效率**(实测 ÷ 全网理论)/池费,行内更优者标绿。`tick_spend` 新增按池累计 `th_hours_by_pool`(矿池实测算力 × 时长,重置统计时清零);`/api/summary` 新增 `pool_analysis[]`、`theory_prl_per_th_day`。回本线本身是全网理论值(两池只差池费),池间差异看「矿池效率」。
- **配置页 GPU 档下拉**:`GPU_CATALOG`(sniper.py,RTX 20/30/40/50 系 + A/H/L 系数据中心卡)+ `GET /api/gpu-catalog`:每型号参考算力(公开表;本池同型号 ≥3 台时用实测中位数)、建议出价(= 参考算力 × 产率 × 币价 × (1−池费) × (1−目标利润率),利润率 10–40% 可选)、建议最低算力(参考 × 75%)、市场参考价(RunPod 社区档 / Vast 典型 / RunPod 实时观测最低价);行下提示 + 「采用推荐」一键回填;仍可「自定义…」手填。`normalize_gpu` 补 3070/3060/20 系与 H100/A100/L40S/L4/RTX 6000 Ada/A6000/A5000/A4000/V100/T4。

### Fixed — 修复
- **Kryptex 已付款漏算(ISS-020)**:`_kryptex_view` 原硬编码 `pool_paid=None`,Kryptex 每小时自动付款后余额归零,自重置产出把已付全部漏掉(本次 14 笔 24.43 PRL,利润被低估约 $25)。现从 `/prl/api/v1/miner/payouts` 按 `next` 翻页求和(只计 FINISHED;任一页失败整体保留旧值),`kryptex_data` 进后台预热;`tick_output` 新增 `output_<pool>_paid_baseline`,旧基线首次拿到 paid 时只把重置前的付款并入基线,显示值不跳变。

### Changed — 变更
- `/api/rentals` 非-Salad 机器增加 `provider / external_id / machine_id`;`/api/full-config` 增加 `auto_stop`。
- 运维:vast-2 / salad 账号已打「暂停租用」(Kryptex 换 miner 前不再新租;在跑机器保留)。

## [KRig 升级 1.5.2] — 2026-09-21

### Changed — 变更
- Kryptex 池镜像 → `kuzigmgm/pearl-miner:krig-1.5.2`(KRig 1.5.2,入口脚本不变:短 rig 名、官方 TLS 入口、缺钱包拒启);`POOLS["kryptex"]` 与 vast-账号2 配置已指向新 tag,Salad 容器组 kuzi-miner-2-salad 同步换镜像。在跑的 Vast 机器不动,新租用 1.5.2。

## [账号备注可在仪表盘直接编辑] — 2026-09-21

### Added — 新增
- 仪表盘「账号总览」账号名旁 ✎ 按钮:改账号备注(config 顶层 `account_label`),侧栏 / 总览 / 各平台租用卡片 / 配置页标题统一显示「平台-备注」;留空恢复默认(账号N / Salad 组织名)。新增 `POST /api/account-label`;`/api/rentals` 增 `label_custom`。点账号名仍跳转到该账号配置页。

## [仪表盘: 账号总览表 + 余额栏格式 + 去 worker 明细] — 2026-09-21

### Changed — 变更
- 仪表盘顶部新增「账号总览」表(状态 / 矿池 / 在跑台数 / 账号总算力 / 时租 / 最多同时租·时租上限),点账号名进入配置;原配置总览页的账号表移除(钱包列不再显示)。
- review 修复:账号总览的「矿池」列改为按机器实际所在池汇总(多池时显示各池台数),配置里的新抢矿池只在与实际不同或无机器时灰字标注「新租 X」;Salad 无机器时显示「由容器组决定」。
- 各账号卡片余额栏改为「余额 $x ｜ $y/h ｜ 约 zh 花完」;按池筛选时另注本池时租。
- 移除「矿池在挖 WORKER」逐 worker 明细表(机器信息以账号卡片为准)。`/api/rentals` 每账号增加 pool / pool_label / max_active_instances / max_total_hourly_usd。

## [仪表盘: 机器表新增性价比列并按其降序] — 2026-09-21

### Added — 新增
- 各账号机器表新增「$/100TH·h」列(单价 ÷ 算力 × 100 = 每 100 TH/s 每小时花费),表内按此**降序**(最贵在上),无算力数据(宽限中/未连池)的排最前显示 "—";比本账号中位数贵 15% 以上标红,便于从上往下关机。

## [矿池注册表带宿主要求 + 切池不影响在跑机器 + Vast/Kryptex 纳入自动化] — 2026-09-21

### Added — 新增
- `POOLS` 每池新增 `platforms`(验证可跑的平台)/`requires`(`min_cuda`→Vast `cuda_max_good` 过滤与 RunPod `allowedCudaVersions` 自动填充、`min_reliability`→Vast 可靠度下限、`grace_seconds_min`→回收宽限下限)/`note`。Kryptex:CUDA ≥ 13、可靠度 ≥ 0.98、宽限 ≥ 1800s,平台 vast/salad/runpod(RunPod 部分宿主 cuInit 失败靠回收换机)。
- 租用记录写入 `pool`;监控池 = 配置 `monitor_pools` ∪ 活跃池 ∪ 在租机器所属池(`monitored_pools`),切池后老机器仍按原池查算力,不再被当 0 算力误杀;看板切池自动把新池并入 `monitor_pools`。
- 回收宽限按池取大(`effective_grace`);平台不支持的池不下单并每 5 分钟提示(`allow_unsupported_pool` 可强制)。
- 配置页:矿池下拉标注 "(该平台不支持)",下拉下方显示该池租用要求与说明;总览「矿池参考」列平台支持与要求;Vast/RunPod 高级设置增 宽限秒数 / 低效持续秒数。

### Fixed — 修复
- Vast 回收按 `env.PRL_WORKER`(租用时真正注入的名字)查矿池,不再按当前 config 重算(切池后命名不同查不到 → 0 算力误判)。
- 手动租的 Vast Kryptex 实例 51847099 已登记进 sniper 状态,由自动化接管(4090 门槛 220 TH,宽限 30 分钟)。

## [Kryptex 短 worker 名 + runpod-2 单机重测] — 2026-09-21

### Fixed — 修复
- Kryptex 矿池 rig 名限 32 字符(stratum 探测: 32 过 / 36 拒 "Invalid login"),而 sniper 给 RunPod 注入的 `kx-runpod-nvidia-geforce-rtx-4090-<时间戳>` 加镜像后缀超 50 字符 → 之前 CUDA 13.0 过滤后的 RunPod 宿主很可能是登录被拒而非驱动问题。新增 `make_worker_name()`:kryptex 池用 `<prefix>-<平台前2位>-<id 末8位>`(≤23 字符),其它池不变;回收按前缀匹配不受影响。

## [Kryptex 在 Salad 跑通: 算力字段校准 + 回收权威] — 2026-09-21

### Fixed — 修复
- **KRig + Kryptex 在 Salad 实测跑通**(RTX 4070 SUPER 约 70-88 TH/s, share accepted)。Kryptex workers API 实际字段是 `avg_hashrate_30m / 3h / 24h`(字符串 H/s)且无 `hashrate`,之前适配器一直读到 None;新增 `kryptex_rate()`(看板同名 `_kx_rate`),offline 视为 0。30m 均值开机前半小时偏低,回收靠 grace 兜底。
- Salad 回收把 `kryptex` 纳入"矿池权威"池(之前退回容器日志判定,KRig 日志格式不识别 → 算力恒 0)。
- Salad 容器日志解析支持 KRig 格式(`Total: N TH/s`、`GPU0 01:00.0 RTX 4070: N TH/s`),并识别 `RTX xx70 SUPER` 型号 → Salad 卡片 GPU 列与精确单价不再依赖 portal 会话。
- KRig 1.5.1 对 Pearl **拒绝明文 stratum+tcp**(r3 尝试加 7048 failover 导致 "plain TCP is not supported" 崩溃循环, 已回退);现役镜像 `krig-1.5.1-r4`(=`krig-1.5.1`):只连官方 TLS 入口,`KRIG_URL2` 可选传第二个 stratum+ssl 入口做 failover。部分 Salad 节点到 8048 的 TLS 握手失败属节点网络问题,只能靠回收换机。
- Salad 同一 tag 缓存旧 digest:镜像改动后需换新 tag(`krig-1.5.1-r2`)并在组停止、上一个版本铺完(`pending_update_in_progress` 消失)后 PATCH image 再 Start。

## [Kryptex 在 Salad 跑通前的两处修正: 官方入口 + 短 rig 名] — 2026-09-21

### Fixed — 修复
- Salad 容器日志显示 KRig 在 Salad 上 GPU 初始化正常,但连池失败:① KRig 1.5.1 拒绝区域入口 `prl-sg.kryptex.network`("not the official Kryptex PRL pool"),改回官方 `prl.kryptex.network:8048`;② 矿池对过长 rig 名返回 `Invalid login`(直连 stratum 探测:带完整 36 位机器 ID 被拒,`kx-<前 8 位>` 通过)。镜像 `krig-1.5.1`/`krig-1.5.1-r2` 的 rig 名改为机器 ID 前 8 位;sniper / 看板按机器 ID 匹配 Salad worker 时同时接受 8 位前缀。
- `POOLS["kryptex"]` 镜像 → `krig-1.5.1-r2`。

## [Kryptex/KRig 对比朋友镜像: 无 bug, 改在 Salad 试] — 2026-09-20

### Changed — 变更
- 与朋友跑通的 `conishc/pearl-miner:kryptex-krig-1.5.1` 逐项对比:krig-miner 二进制 md5 相同、启动参数相同、对方镜像为纯 ubuntu 无 CUDA 库 → 我方镜像无 bug;差异在宿主(Salad 新驱动 vs RunPod 社区机旧驱动,KRig 需支持 CUDA 13 的驱动)。
- KRig 镜像 `krig-1.5.1` 重推:入口地址 `KRIG_URL` 未设时读 sniper 注入的 `PRL_HOST`(可配区域入口如 `stratum+ssl://prl-sg.kryptex.network:8048`),去掉内置默认钱包(缺 `PRL_ADDRESS` 拒绝启动);`POOLS["kryptex"].reads_prl_host` → true。

## [review 修复: 占位 key 只校验已启用平台 / Vast 自动建机默认值统一 / start-all 缺字段不退出] — 2026-09-20

### Fixed — 修复
- 启动护栏只检查**本 config 已启用平台**的 key 占位符;旧 `.env` 里未使用平台残留的 `replace_with_…` 不再导致正常账号退出(原实现无条件查四个变量,会停掉监控与回收)。
- Vast `create_enabled` 缺省值看板与 sniper 统一为 **true**(旧配置无该键时看板曾显示关闭而后台仍在租机,保存其它参数还会把它意外关掉)。
- `start-all.sh` 在 `.env` 缺 `DASHBOARD_HOST` / `DASHBOARD_PORT` 时不再因 `set -e` + `pipefail` 异常退出,改用默认值。
- 测试:更新依赖旧镜像 / 已下线矿池 / 已移除迁移功能的断言(`test_full_config_pools`、`test_pool_registry`、`test_summary_newpools`;删除 `test_migrate*`)。

## [新人初始化流程整改: 安全默认 + 护栏 + 文档对齐] — 2026-09-20

### Changed — 变更
- **模板默认不花钱**:4 个 `config.*.example.json` 的 `enabled` / `create_enabled` 默认 **false**(Vast 新增 `create_enabled` 开关,老配置无此键默认 true),并补 `pool: pearlhash`、`monitor_pools: ["pearlhash"]`(之前缺省会去查全部 5 个池含 3 个已下线池);护栏默认 1 台 / $1.0/h。
- **`.env.example` 的 key 留空**(之前是 `replace_with_…` 占位串,导致 start-all 的"空 key 跳过"永远不触发、四个进程一起 401);新增 `DASHBOARD_HOST=127.0.0.1` 及说明。
- **WildRig 镜像 v13-wildrig 去掉内置默认钱包**(原为作者钱包,Salad 手建组漏填 PRL_ADDRESS 会挖给作者);`PRL_ADDRESS` 为空或非 prl1… 时拒绝启动。默认 worker 前缀 kuzi → miner。
- README「快速开始」按"只用 RunPod"的最小路径重写,补上 `cp configs/config.<平台>.example.json` 一步(之前遗漏)、云服务器访问看板的两种方式(反代 / `DASHBOARD_HOST=0.0.0.0`)、「默认配置是什么」表、TensorDock 密钥生成命令、Salad 组必须填钱包;去掉已下线的"一键迁移"与"dry-run 不加 --live"(start-all 无此模式)的说法。看板内「工具说明」同步(uv / 五步 / 镜像 v13 / 时租上限是**每账号**而非全局 / 去掉 image、prl_host 参数)。
- 平台文案顺序统一 RunPod / Vast / TensorDock / Salad。

### Fixed — 修复
- **钱包 / key 护栏**:sniper 启动时 `prl_address` 缺失或仍是占位符、或 `.env` 的 key 仍是 `replace_with_…` → 直接报错退出(之前会租到机器挖给无效地址)。
- 看板拉起 sniper 前自动建 `logs/`(之前不经 start-all 启动看板时「重启应用」静默失败);改用当前解释器(uv .venv)启动,与 start-all 一致;拉起失败打印原因。
- Windows `.ps1` 读 `.env` 时剥掉看板写入的引号(之前从看板保存的 key 在 Windows 上带引号导致 401)。
- `start-all.sh` / `restart-dashboard.sh` 结束语按实际监听地址提示(默认 localhost,云服务器提示反代或改 HOST)。

## [修: 暂停租用时不回收死机 / 关闭已销毁实例报 404 / 产出卡 tooltip 不显示] — 2026-09-20

### Fixed — 修复
- **暂停租用 / 关闭自动建机期间 RunPod、TensorDock 不再回收死机**:回收(reconcile)从 create 路径里挪到 cycle 顶层,始终执行;之前暂停期间 0 算力机器会一直烧钱。
- 看板「关闭」遇到平台 404(实例已被回收或平台侧已销毁)不再报失败,提示「实例已不存在」;终止操作写入看板日志。
- 累计产出卡的「已确认 · 待成熟」悬停提示改为 CSS 自绘 tooltip(页面每几秒重绘会打断浏览器原生 title 提示),大数字下加虚线提示可悬停。

## [看板体验微调: 去 GPU 列 / 去矿池分析 / 加宽 / 产出 tooltip / 按钮对齐 / 平台排序] — 2026-09-20

### Changed — 变更
- 配置总览表去掉「GPU 档」列(信息过密),GPU 档在各账号页编辑。
- 仪表盘移除「矿池分析 · 余额 / 性价比 / 挖矿成本」折叠面板(前端删除,`/api/summary` 字段保留)。
- 内容栏最大宽度 1040 → 1280px,卡片更宽;工具集固定 3 列(桌面)。
- 累计产出卡片的「已确认 · 待成熟」次行改为鼠标悬停大数字时的提示(虚线下划线示意可悬停),五张统计卡高度对齐。
- 各账号租用表最后一列(关闭按钮)固定宽度并右对齐,各账号表按钮对齐一致。
- 平台展示顺序统一为 runpod > vast > tensordock > salad(`PLATFORM_ORDER`,左栏 / 仪表盘 / 配置总览);仪表盘账号卡片不再按在跑机器数排序。工具集板块重排为 3×3:官网 · 区块浏览器 · 钱包 / 租卡平台 · 矿池 · Miner / 收益计算器 · 交易平台 · 数据源,租卡平台内 RunPod > Vast > TensorDock > Salad。

## [配置页产品化: 配置总览 + 基础/高级分层 + 迁移下线] — 2026-09-20

### Changed — 变更
- **「全局配置」→「配置总览」**:全局只保留真正共享的 **钱包地址 + 告警 URL**(`COMMON_KEYS`);新增各账号一览表(状态 / 矿池 / 最多同时租·时租上限 / GPU 档一行 / 钱包,点账号名进入编辑),并显示各账号时租上限之和(最坏每小时花费)。
- **账号配置页分层**:「基础设置」按上手顺序 ① API KEY → ② 启用/自动建机 → ③ 新抢矿池 → ④ 最多同时租 / 总时租上限(账号级,写 config 顶层 `ACCOUNT_KEYS`)→ ⑤ GPU 档(型号 / 最高出价 / 最低算力)→ 保存 → 重启;其余(worker 前缀 / 轮询 / 最低性价比 `min_th_per_usd_hour` / 平台特定参数 / raw JSON)收进「高级设置」折叠。平台特定参数补中文说明(`SPEC_LABELS`),runpod 增 `allowed_cuda_versions`、`hashrate_watch_enabled`。
- 文档教程步骤同步(配置总览填钱包 → 账号页 ①→⑤)。

### Removed — 移除
- **矿池迁移功能下线**:全局「一键全部账号迁移」与各账号「迁移现有机器到所选池」按钮、`/api/migrate` 接口、`do_migrate` 全部移除(改在跑 pod 镜像不稳)。各账号「新抢矿池」下拉保留,只影响新租机器。

## [WildRig 镜像 v13-wildrig: 修 RunPod 崩溃循环 + 设为默认] — 2026-09-20

### Fixed — 修复
- **v12-wildrig 在部分 RunPod 宿主崩溃循环**(`Failed to find devices`):entrypoint 无条件传 `--opencl-platforms nvidia`,宿主平台名不匹配时过滤为空。v13 默认**不传**平台过滤(`PRL_OPENCL_PLATFORMS` 非空才传);启动打印 OpenCL ICD / 库 / nvidia-smi 诊断;wildrig 退出后 10s→60s 退避重试、容器不退出。
- PearlHash 池默认镜像与 4 个 config 的 `image` → `docker.io/kuzigmgm/pearl-miner:v13-wildrig`(新租生效,在跑机器不动)。

## [Kryptex 池接入 + RunPod 第二账号 + CUDA 版本过滤] — 2026-09-20

### Added — 新增
- **Kryptex 矿池**:`POOLS["kryptex"]`(KRig 1.5.1 镜像 `kuzigmgm/pearl-miner:krig-1.5.1`,stratum+ssl://prl.kryptex.network:8048)+ sniper 回收适配器 `kryptex_worker_hashrates`(`/prl/api/v3/miner/workers/{addr}`)+ 看板 `POOL_MONITORS["kryptex"]`(workers / balance);`pool_of_image` 识别 kryptex 镜像;池面板链接直接带钱包地址跳到 Kryptex 个人 stats 页。Kryptex API 走 Cloudflare,请求需带浏览器 UA。
- **RunPod `allowed_cuda_versions`**(per-account 配置,runpod 块):创建 pod 时传 `allowedCudaVersions`,只租宿主驱动 CUDA 版本在列表内的机器。CUDA 原生 miner(KRig/PearlFortune)在旧驱动宿主上报 `CUDA: runtime loaded but init failed`,OpenCL 的 WildRig 不受影响;不配则不过滤。
- 多账号:`config.runpod-2.json`(gitignore)用 `api_key_env: RUNPOD_API_KEY_2`,独立 state/log,与账 1(WildRig + PearlHash)同钱包并行做 A/B 对比。

### Changed — 变更
- **全局配置收缩**:`COMMON_KEYS` 去掉 `image`、`prl_host`(镜像/矿池地址由各账号配置页按池决定,全局页只留钱包、worker 前缀、并发/预算上限、轮询、告警)。

### Fixed — 修复
- **「暂停租用」误伤同平台其它账号**:看板开关写的是平台级 `control/runpod.rent-paused`,sniper 也按平台名读 → 在 runpod-账2 上点暂停会把账 1 的 RunPod 租用一起停掉(监控照常、日志无提示)。改为按账号隔离:sniper 从 `--config` 文件名推出账号名(`ACCOUNT`),读 `control/<账号>.rent-paused`;看板开关同样按账号写。账 1 账号名即平台名,与旧文件兼容。

## [回退访客数据屏蔽] — 2026-09-20

按需求恢复:访客(偷窥模式)照常查看仪表盘实时数据,不再显示演示占位。

### Reverted — 回退
- 删除前端 `guestOverview()` 占位页与 `renderOverview` 的访客/`guest_masked` 分支;访客走正常渲染。
- 删除后端 `/api/summary`、`/api/rentals` 对访客的 `guest_masked` 屏蔽;访客拿真实数据。
- 还原 `initRole`(去掉多加的一次重渲染)与登录页文案(「偷窥模式 · 仅看仪表盘 / PEEK MODE」)。
- 访客仍为只读(所有写操作 / 配置 / 日志接口对 guest 依旧 403,与原行为一致)。

## [累计产出含待成熟 pending + 卡片拆分标注] — 2026-09-20

### Fixed — 修复
- **累计产出漏算 pending**:`tick_output` 读矿池 pending 时用了错字段 `total_pending`,而 API 实际是 `total_pending_prl` → pending 恒为 0,累计产出只反映已确认部分。改为读 `total_pending_prl`(兼容旧 `total_pending`)。修完累计产出大数字正确 = 已确认 + 待成熟(总额)。

### Added — 新增
- summary 暴露 `output_confirmed`(已确认 = 总额 − pending,clamp≥0)与 `output_pending`(待成熟 = total_pending_prl + 非-ph 池 pending_balance)。
- 累计产出卡片次行加标注:`已确认 X · 待成熟 +Y PRL`(待成熟用暖色),两者相加 = 大数字总额,与矿池 account 页一致(如 0.27 已确认 + 4.44 待成熟 = 4.71)。NOCK pending 未开放付款,暂不显示。

## [访客模式屏蔽实时数据 + 演示占位引导] — 2026-09-20

公开域名下,访客不应看到钱包/算力/收益/机器等实时数据。

### Changed — 变更
- **前端**:访客(guest)进仪表盘显示**演示占位页**(辉光珍珠 + 「访客模式 · Guest」说明「实时数据仅在你部署自己的看板后可见」+ 「查看部署说明 · 工具说明 →」按钮引导到 `doc:guide`),不再渲染真实数据。`renderOverview` 加 `ROLE!='admin'` 与 `guest_masked` 双分支;`initRole` 拿到 role 后补一次渲染,消除默认 admin 的首帧闪现。
- **后端**:`/api/summary`、`/api/rentals` 对访客返回 `{"guest_masked":true}`(不含 wallet/机器/收益),防公开域名下直接 curl 取数。
- 登录页「偷窥模式 · 仅看仪表盘」文案改为「访客预览 · 部署后见数据 / GUEST」。
- 访客侧栏仍可见 工具集 / 文档(工具说明、挖珠教程),配置工作台仍仅管理员。

## [下线失效矿池 + 钱包头微调] — 2026-09-20

### Changed — 变更
- **下线 3 个失效矿池**:TW Pool(twpool)/ HeroMiners / PearlFortune 从看板池列表(池面板按钮、矿池视图下拉、迁移选择器、配置池说明)隐藏,只保留 PearlHash。经评估:三者 API 仍响应 200,但我们只在 PearlHash 挖、其余无活动,据用户反馈已不可用。实现:`dashboard.py` 加 `OFFLINE_POOLS` 常量 + `available_pools()`,过滤两处 `pools` 列表。
- **钱包头**:「复制」由第二行文字按钮改为**地址右侧小图标**(`.copyi`,发丝方框 + hover 薄荷);地址行改 flex,图标固定、地址可横向滚动。
- **池面板按钮**去掉 📊 图标,仅文字。

### 后续微调(同日)
- **sniper 只查 pearlhash**:四个 config 设 `monitor_pools:["pearlhash"]` 并 stop-all/start-all 重启,不再查三个死池的 account API(日志死池查询 0 条)。
- **累计产出卡精简**:副文本两行合并为一行 `≈ $X · 平均 Y PEARL/h`(去掉"自重置起算/统计自.../@ 币价/实时"与单独的"自重置"行)。
- **复制图标修位**:原 `flex:1` 把图标顶到卡片最右 → 改 `flex:0 1 auto`,图标紧跟地址;换 Feather copy 双方块图标、`--tx` 深色 + 4px 圆角小框。

## [看板前端重构为 IBM Carbon 风格] — 2026-09-20

把看板从"薄荷绿 + 圆角 + 渐变/阴影/毛玻璃 + Inter"重构成 IBM Carbon Design System 风格。纯 `dashboard.py` 内嵌 CSS 改动,HTML 结构与 JS 逻辑、所有 class 名不变。

### Changed — 变更
- **字体**:Inter/Roboto Mono/JetBrains → **IBM Plex Sans + IBM Plex Mono**(Google Fonts,OFL);body 加 `letter-spacing:.16px`(Carbon 精度细节)。
- **颜色 token**:亮色改 Carbon White(白/浅灰 #f4f4f4/发丝 #e0e0e0/炭 #161616),暗色改 Carbon Gray-100(#161616/#262626/#393939)。**强调色保留原本薄荷/青绿色系**(暗 #3fe0c5 / 亮 #0b9a82),并保留原本蓝→青渐变(`--g1`/`--g2`)做层次感。
- **纯平化**:所有圆角 → 0(直角);去卡片/表格/按钮阴影、渐变背景、header/side 毛玻璃;深度改由面变化 + 1px 发丝线承载。
- **保留层次感**:主按钮 `.b-acc` 薄荷渐变、侧栏辉光珍珠 mark、品牌渐变字、激活态渐变高亮均保留(Carbon 直角 + 薄荷渐变的混合)。
- **组件**:danger `.b-bad` 实心红;输入焦点用 2px 薄荷 outline;表头去 uppercase 重 tracking。favicon 保留薄荷珍珠图标。
- **登录页**:按要求**只换字体**(→ Plex Sans/Mono)+ 微调,海洋浪花/珍珠旋转动效与玻璃卡全部保留。
- meta theme-color 与 `applyTheme` 同步改(亮 #ffffff / 暗 #161616)。

## [回收逻辑根治: Vast 查无 worker 按 0 回收 + 防 API 抖动误杀] — 2026-09-20

换 WildRig 镜像后实测暴露两处回收缺陷,一并根治(仅 `sniper.py` + vast/runpod config)。

### Fixed — 修复
- **Vast 漏杀坏机**:`reconcile_vast_hashrate` 日志读不到算力时回退矿池 API,原先 worker 不在池就保持 `None` → 跳过,坏机(如宿主驱动太旧、`Failed to start OpenCL threads` 的 5090)一直空烧钱。现改为:本轮矿池查询成功但 worker 不在池 → 按 0 算力交低效计时回收(与 RunPod `missing_worker_as_zero` 对齐,vast 默认 True)。
- **全平台误杀风险**:`merged_worker_hashrates` 吞掉每池异常、总返回字典,RunPod 的 `worker_api_failed` 几乎不触发 → pearlhash API 整体宕机时会把所有机当 0 批量销毁。新增 `merged_worker_hashrates_ex` 返回 `pool_ok`(至少一池查询成功),RunPod 改用 `worker_api_failed = not pool_ok`;全池失败时"worker 不在池"视为未知、跳过不杀。

### Added — 新增
- `merged_worker_hashrates_ex(config)` → `(merged, pool_ok)`;`resolve_hashrate_from_pool(info, pool_ok, missing_as_zero)` 纯决策函数(命中→算力 / 未命中+查询成功→0 / 否则→None)。
- `tests/test_reclaim_missing_worker.py` 覆盖 pool_ok 信号与三态决策。

### Changed — 变更
- vast config 加 `missing_worker_as_zero: true`;vast/runpod `hashrate_grace_seconds` 由首夜临时的 1200 回落到 600(覆盖镜像慢拉取又不久拖坏机),`low_efficiency_stop_seconds` 保持 300。

## [工具集页更新矿池 / Miner / 数据源导航] — 2026-09-19

服务器重装后看板恢复, 顺带把「工具集」页(`dashboard.py` 的 `LINKS` 常量)从主网早期那批外链更新到 2026-09 现状, 为重启租赁机器跑 miner 做准备。矿池格局已变(Kryptex 约占全网 50%)。

### Added — 新增
- **矿池** 分类补齐 6 个已验证官方地址:Kryptex Pool / LuckyPool / HeroMiners / K1Pool / PearlPool.cloud / f2pool。
- 新增 **Miner 下载** 分类:HydraX(1%)/ SRBMiner-MULTI(3%)/ lpminer(0%)/ BzMiner(2%)/ PRL-Today 收益悬浮窗。
- 新增 **数据源 / 调研** 分类:PearlTrack / Lord of Pearls / prlscan / MiningPoolStats / Hashrate.no / HydraX Miner 对比。

### Notes — 注意
- 纯静态 `LINKS` 数据改动, 未动 `renderLinks()` / CSS / 后端。调研数值(算力/份额/开发费)按需求**不进网页**, 仅一次性分析交付。
- URL 均经 WebSearch 查证。已部署并重启看板。

---

## [vast/runpod 算力回退日志文案纠正] — 2026-06-18

vast/runpod 拉取容器日志失败回退到矿池 worker API 时, 日志写死「PearlHash」, 但代码实际走 `merged_worker_hashrates`(按 `monitor_pools` 跨所有池, 含当前活跃池 pearlfortune)。文案误导, 让人以为 pearlfortune 出错或只查了 pearlhash。

### Changed — 变更
- `sniper.py` 三处日志文案 `PearlHash worker API/check` → `pool worker API (merged across monitor_pools, incl active pool)` / `pool worker check failed`(vast 2 处 + runpod 1 处)。纯文案, 行为不变。

### Notes — 注意
- 排查 contract 41434959(RTX 4090): vast 实例日志持续 403(该 host/上游不放日志, 非 S3 竞态可重试范畴), 回退查矿池 worker 也 found=false → 该机始终未在 pearlfortune 出算力, 已 inactive。pearlfortune 本身正常(salad 4 台在挖)。**pearlfortune 未出错**。
- 文案改动需 sniper 重启(start-all.sh / 各账号进程)才在日志生效; 已部署文件, 运行中进程下次重启自动采用。

---

## [Salad 实例列表算力/单价/ID 对应修复] — 2026-06-18

修复 salad 实例列表三处:算力恒空、RTX 4070 SUPER 显示价格区间而非单价、实例 ID 与「矿池在挖 WORKER」对不上。

### Fixed — 修复
- **算力恒空(核心 bug)**:`_salad_compute` 原写死 `pool_data()`(= pearlhash 监控)取 worker 匹配算力/GPU。但实际挖矿池常是 pearlfortune/twpool, pearlhash 无 worker → 匹配全空。改为跨所有 `POOL_MONITORS` 取归一化 worker(`name`/`th`/`gpus`), 按 salad worker 名内含的 `machine_id` 子串匹配。算力/GPU 现正常回填。
- **RTX 4070 SUPER 单价区间**:salad org 无独立「RTX 4070 Super」class(只有 4070 / 4070 Ti / 4070 Ti Super), 物理卡按 RTX 4070 计费。`SALAD_PRICE_ALIAS` 增 `rtx 4070 super → rtx 4070`(同既有 `rtx 4080 super → rtx 4080` 惯例), 现显示 `$0.070/h`。
- **ID 对应**:salad 公共 API 实例 `id`=instance_id, 而矿池 worker 名 = `<prefix>-salad-<machine_id>`, 两者本就不同。`active_rentals` 现透传 `machine_id`, 列表「实例」列改显 machine_id(= worker 后缀, 列头改「机器(worker)」, instance_id 移入 title 悬浮), 与「矿池在挖 WORKER」一一对得上。

### Notes — 注意
- 服务器 + 本地已同步;Playwright 隧道实测:5/5 实例有算力、单价 `$0.070/h`、机器列与 worker 后缀逐条对应(47c9f16f / 2b6431a0 / af240eaf …)。salad batch 优先级机器会动态进出, 实例数随时间变动属正常。

---

## [钱包卡矿池视图默认在跑池 + 链接按钮改名] — 2026-06-18

钱包卡矿池视图不再默认「合并」(合并会把 4 个矿池链接全列出, 把钱包地址挤窄), 改为默认显示当前在跑的矿池(取全局配置活跃池, 如 PearlFortune), 只显示该池一个链接按钮。

### Changed — 变更
- **默认矿池视图 = 配置活跃池**:后端 `build_summary` 新增 `_default_pool_key()`(取已启用账号 `S.active_pool` 多数, 兜底 `pearlfortune`)+ 响应字段 `default_pool`;`pool` 参数支持 `default` 哨兵(空/未知一并解析为活跃池)。前端首次进入(无 localStorage)请求 `pool=default`, 视图与下拉框随后端解析结果 `d.pool_view` 同步。「合并」仍保留为可手选项。
- **矿池链接按钮改名**:`PearlFortune →` → `📊 PearlFortune 矿池面板 →`(打开该矿池本钱包地址的矿池面板/算力收益页), 加 title 悬浮说明。

### Notes — 注意
- 服务器 + 本地 dashboard.py 已同步;Playwright 隧道实测:清 localStorage 后进入默认 `pearlfortune`、下拉同步、钱包卡仅一个算力页按钮、地址不再被挤窄;`pool=merged` 显式仍正常。

---

## [仪表盘 KPI 重排 + 矿池分析折叠] — 2026-06-18

仪表盘首行 KPI 精简为核心指标,次要的矿池分析数据收进可折叠面板,默认收起。

### Changed — 变更
- **累计折合利润移回首行**:放在累计产出右边,首行卡片为 在跑机器 / 总算力 / 累计租金 / 累计产出 / 累计折合利润。
- **新增「🪙 矿池分析」折叠面板**(复用 `.kpanel` 模式,`togglePool()`/`_poolopen`,默认收起):内含 矿池余额 / 算力性价比 / 挖矿成本 三卡 + 矿池分析数据条(待结算 / 累计收益 / 份额 / 费率 / 高度 / 爆块,原 `detBar`)。
- 面板展开态随 `renderOverview` 重建 DOM 后恢复(与 K线 / 算力趋势面板一致)。

### Notes — 注意
- 服务器 + 本地 dashboard.py 已同步部署;Playwright 经隧道实测:首行 5 卡、面板默认收起、点击向下展开后三卡 + 数据条正常。

---

## [看板手机端响应式适配] — 2026-06-17

看板 UI 由桌面固定侧栏布局适配为手机端可用:登录页 + 主页(仪表盘)在窄屏完整可用、无横向溢出。

### Added — 新增
- **移动端抽屉式侧栏**:≤760px 固定侧栏(`.side` 210px)改 off-canvas 抽屉,新增汉堡顶栏(`.mtopbar`)+ 遮罩(`.mbackdrop`),`toggleSide()`/`closeSide()` 控制开关;点任意导航项自动收起。
- **表格横向滚动容器** `.tscroll`:机器表 / Worker 表包裹后在窄屏独立横滑,不撑破页面。

### Changed — 变更
- 移动端(≤760px):卡片网格 2 列、数值字号缩小、钱包行竖排、K线头 flex-wrap、配置表单堆叠;输入框字号 16px 防 iOS 聚焦缩放。
- 登录页(≤480px):缩小珍珠动画、`#login` 可纵向滚动、表单优先;`#login` z-index 升到 50 盖住移动顶栏。

### Notes — 注意
- 桌面(>760px)布局与字号保持原样,无回归(Playwright 390/1280 双视口实测:抽屉开关、无横溢、表格独立滑动、登录页层级均正常)。

---

## [Salad scid 健康检测 + 半自动弹窗重登] — 2026-06-17

dashboard 启动/运行时检测 salad portal 会话(scid)缺失或过期,自动弹有头浏览器引导人工重登,登录完成自动续上抓取——不再需要手动盯着重跑 salad_login.py。

### Added — 新增
- **scid 健康检测**:`should_relogin`(纯函数)判定——会话文件**缺失**或**连续 2 轮抓取全空**(scid 过期)且距上次重登尝试满冷却(默认 30min)→ 触发重登。启动时(缺失账号)+ 运行中(连续空)都检测。
- **半自动重登 `auto_login`**:弹有头 Chromium 到 portal,**自动轮询 portal-api 检测登录完成**(替代旧的 `input()` 等回车,后台 nohup 进程也能用);你人工过完 Turnstile/OTP、scid 生效后自动存会话、关窗、续上抓取;超时(默认 10min)放弃。
- run_manager 每账号维护 `consecutive_empty` 计数 + `last_relogin` 冷却,过期/缺失触发 auto_login 并用新会话重建 context。

### Changed — 变更
- `start_portal_manager` 不再因 session 文件缺失而跳过账号(改按 `salad.enabled`),缺失的账号交给 run_manager 引导登录。

### Notes — 注意
- **优雅降级**:playwright 缺失 / 无 GUI 环境(ssh/服务器)弹窗失败 → 回退现状(log 提示手动 `salad_login.py`),不崩。

---

## [Salad 逐实例低效判定加固] — 2026-06-12

修复多卡弹性组下低效判定的两个边界 bug,让死机/掉队机器被可靠清理。

### Fixed — 修复
- **死机(无容器日志 + 不在矿池)被漏判**:原逻辑两数据源都没有时直接跳过判定,致 0 算力死机不被清理。改为矿池 API 正常(能确认真离线)时视为 0 算力判低效,且双无 running 实例**绕过新实例长宽限**——首次观测后满 `low_efficiency_stop_seconds`(默认 5min)即 reallocate;矿池 API 也挂时仍跳过不杀(防抖动误杀)。
- **多卡组逗号串误归一**:salad 弹性组的 gpu 字段(`RTX 4090,RTX 5090,...`)被 `normalize_gpu` 拼串后命中首个 "5090" → 误取最严阈值(300)。改为含逗号即返回空,回退组级阈值。
- tests: `test_salad_pool_authoritative.py` 加 missing/绕宽限 case + 新增 `test_normalize_gpu_multi.py`。

---

## [挖矿成本指标 + 产出口径自重置] — 2026-06-11

新增「挖矿成本」量化每个 $PRL 的电租成本并对比币价提示盈亏;产出口径改为自重置,重置统计后真正归零。

### Added — 新增
- **挖矿成本卡(两指标)**:
  - 累计挖矿成本 `cost_cumulative_usd` = 累计租金 ÷ 累计产出(按当前矿池视图)。
  - 最近 3 小时实时成本 `cost_recent3h_usd` = (当前每小时租金 × 3) ÷ 最近 3h 产出(全局口径,不随视图变)。
  - 卡片两行各与实时币价对比:低于币价绿(盈利)/ 高于红(应关机);无数据显 —。
- **产出滚动快照**:每 5 分钟记一次、裁剪保留 4h,用于算「最近 3h 产出」(运行不足 3h 显 —)。

### Changed — 变更
- **产出口径改为「自重置」**:非 PearlHash 池产出 = 自重置增量(全期值 − 重置基线,基线含 pending),口径统一为 `since_reset`,消除合并视图「口径不一」警示。**重置统计后累计产出真正归零**(此前用全期值,重置不归零)。平均每小时产出、利润口径随之统一。

### Fixed — 修复
- **salad RTX 4080 SUPER 成本显示成价格区间**(如 `$0.090–0.250/h`):salad 组为 batch 优先级且 gpu-classes 无 4080 SUPER class,定价两路都查不到 → 回退组级区间。改为别名复用 salad RTX 4080 class 的实时(batch)价,显示真实单价。

---

## [herominers + pearlfortune 双矿池 + 算力趋势图 + 池详情] — 2026-06-10

新增 herominers、pearlfortune 两个矿池(共支持 4 池),默认切到 pearlfortune;池卡片加详情条与算力趋势图。

### Added — 新增
- **两个新矿池**:herominers、pearlfortune 注册接入(POOLS 驱动,共 PearlHash / TW Pool / herominers / pearlfortune 4 池),配置页可切换/一键迁移。
- **默认矿池改为 pearlfortune**(active_pool 兜底)。
- **池卡片详情条**:待结算、累计收益、份额(good/invalid/stale)、网络信息(高度/爆块)、worker 离线标记。
  - pearlfortune 接 ledger(已付/收益)+ pending/费率;herominers 接 shares + pool_info。
- **可折叠算力趋势图**:每池每小时算力折线(canvas);pf/twpool/herominers 显示,pearlhash/合并视图隐藏。
- 待结算余额计入累计产出;每小时产出改用「总产出 ÷ 周期」口径。

### Changed — 变更
- pearlfortune 默认镜像 v1.1.1 → **v1.1.2**(修复 Salad 加壳 miner PID1 崩溃无限重启)。
- salad 低效判定按机器所在池路由:pearlfortune 池权威 TH 门槛 / herominers 退容器日志。
- `parse_latest_hashrate` 支持 pearlfortune `proof_per_sec` 日志算力。

### Fixed — 修复
- herominers 余额改读 `stats.balance`(真实数据证实,修此前取不到余额)。

---

## [Salad portal 真实 GPU/余额 + 池权威逐实例低效] — 2026-06-09

通过浏览器会话从 Salad portal 抓真实单卡型号/单价/余额;salad 低效判定改为「矿池权威、逐实例」。

### Added — 新增
- **Salad portal 抓取**(常驻 headless Playwright,持登录会话):
  - 每实例**真实单卡 GPU 型号 + 单价**(公共 API 不返回 GPU,portal 是唯一来源)。
  - **账号真实余额**(前端显示「实时余额」,优先于手填估算)。
  - 一次性有头登录 `salad_login.py` 存会话,之后 headless 静默续期。
- **salad 累计租金改用 portal 真实余额下降量**(实测扣费;非-salad 仍 price×time)。
- **累计产出卡加「平均每小时产出」**(自重置口径)。
- 多矿池框架雏形:POOL_MONITORS 注册表 + build_summary/pool_view 跨池 POOLS 驱动。

### Changed — 变更
- **salad 低效判定改为「矿池权威、逐实例」**:矿池在线算力 ≥ 门槛=健康;曾在池出现却离线(即使容器日志在挖)即 reallocate;矿池 API 挂 / 无 machine_id / 新实例宽限期则退容器日志判定(防误杀)。
- salad twpool 镜像换成 `mrkidbk/pearl-miner-twpool:v1.9.1`(每实例唯一 worker;runpod/vast 向后兼容)。
- 日志算力↔实例改按 `machine_id` 关联(salad API instance_id 偶发 None,此前致算力显示 0)。
- 项目改用 **uv** 管理(pyproject + uv.lock + Python 3.14)。

### Fixed — 修复
- `_twpool_view` 剔除矿池上报的损坏算力(单 worker > 2000 TH/s)。
- `parse_latest_hashrate` 支持 twpool 镜像日志格式。

### Notes — 注意
- 新增 `scripts/restart-dashboard.sh` 安全重启看板(杀旧 → 等端口释放 → 起新 → 验证监听)。

---

## [机器分池可视化 + 全面分池计算] — 2026-06-08

总览所有指标可按矿池分别查看,机器表显示每台在哪个池并可按池筛选。

### Added — 新增
- **每台机器矿池判定**:镜像优先(conishc/twpool→TW Pool;kuzigmgm/mrkidbk/其它→PearlHash)→ 取不到镜像用「该 worker 在哪个池报算力」兜底 → 仍判不出=未知。
  - 镜像来源:salad 从 `salad_live` 组信息透出;runpod/vast 用 `account_machine_images` live 抓取 + serve-stale 缓存 + 后台刷新(迁移后镜像即时准确)。
- **机器表加「矿池」列** + 按顶部矿池下拉筛选:合并=全部;单池=只显示该池机器;未知机器只在合并显示。各账号当前 $/h 跟随筛选(只算该池机器)。
- **在跑台数按池**:合并显示 `PearlHash N / TW Pool M / 未知 K`;单池显示该池台数。
- **成本/利润/性价比按池**(随矿池下拉切换):
  - 当前 $/h:按每台机器的池拆分(精确)。
  - 累计租金:`tick_spend` 按池累计(`cumulative_usd_by_pool`,**自本次更新部署起**;历史混合段仅进总额;unknown 仅进总额不入单池)。
  - 累计折合利润:该池产出 − 该池租金(保留口径警示,twpool 全期产出 vs 自更新租金不完全对齐)。
  - **新增「算力性价比」卡**:TH/($·h) = 该池总算力 / 该池当前 $/h。

### Notes — 注意
- 不影响 PRL/USDT 币价与 K 线、各账号平台账单余额(账号级)、hours_left。
- 镜像/缓存均后台 30s 刷新;build_summary/tick_spend 读缓存不额外打 API。

---

## [配置页矿池澄清 + 迁移下拉] — 2026-06-08

消除多矿池相关的配置困惑,迁移目标改下拉选择。

### Added / Changed
- **COMMON 全局配置加只读「矿池参考」区**:列出每个池用的镜像 + 是否读 PRL_HOST(PearlHash → kuzigmgm 读 PRL_HOST;TW Pool → conishc 不读 host)。说明「镜像由所选矿池自动决定,切池/迁移无需改 image/prl_host」。
- **image / prl_host 字段加灰字标注**:image「仅未选矿池时兜底」、prl_host「仅 PearlHash 读, TW Pool 不读」——澄清这俩是 pearlhash 兜底值,换池不用改。
- **一键全部账号迁移改池下拉**:按钮旁加矿池下拉(PearlHash / TW Pool),不再手填池名;MIGRATE 确认保留。
- `build_full_config` 的 `pools` 列表补 `image`/`reads_prl_host` 字段(供前端矿池参考区用)。

---

## [总览矿池显示切换] — 2026-06-08

总览页加矿池显示切换下拉,迁移期间混合机群可按池查看。纯显示,不影响挖矿/配置/迁移。

### Added — 新增
- **总览矿池切换下拉**(钱包卡内):`合并 / PearlHash / TW Pool` 三选一,默认**合并**,`localStorage` 记忆(键 `pool_view`)。切换即时重渲染总览。
- **后端 `twpool_data()`**:查 `api.tw-pool.com/api/worker_stats`,serve-stale 缓存 + 后台 30s 刷新(与 pearlhash `pool_data` 并列)。
- **`pool_view(which)`**:pearlhash/twpool/merged 三视图统一映射(在挖 worker 表 / 总算力 / 矿池 PRL 余额 / error);合并按 worker 名取最大、总算力与余额相加、单池故障容错。
- **`/api/summary?pool=`**:按所选池返回 总算力 / worker 表 / 矿池余额 / 累计产出。
- **产出口径标注**(累计产出卡):PearlHash =「自重置起算(统计自 …)」、TW Pool =「全期(已付+未付)」、合并 =「PearlHash 自重置 + TW Pool 全期」。`tick_output` 始终调用,pearlhash 自重置累加不中断。
- **矿池余额卡**:显示所选池的 PRL 钱包余额(twpool 直接来自 API;无数据显示 —)。

### Notes — 注意
- 各账号**平台账单余额**(vast/runpod 充值)不随池切换变;随池变的是**矿池钱包 PRL 余额**。
- 不影响 PRL/USDT 币价与 K 线行情(与矿池无关)、不影响在跑机器表每实例算力(来自 sniper 双池合并)。

---

## [多矿池可切换 + 一键迁移] — 2026-06-08

支持 PearlHash / TW Pool 多矿池:双池监控避免混合机群误杀、可配置默认抢哪个池、UI 一键把现有 vast/runpod/salad 机器迁移到目标池。架构可扩展到更多池。所有第三方迁移接口均经真机/官方源码实测确认后才实现。

### Added — 新增
- **矿池注册表 `POOLS`**(sniper.py):pearlhash(`kuzigmgm/pearl-miner:v11`,读 PRL_HOST)/ twpool(`conishc/pearl-miner:twpool-v1.9.0-auto`,池写死、读 PRL_ADDRESS/PRL_WORKER)。加新池 = 加一条 registry + 一个 `*_worker_hashrates` adapter。`active_pool()` / `effective_image()` 决定新抢机器用的镜像。
- **双池监控(安全层)**:`twpool_worker_hashrates`(实测 `api.tw-pool.com/api/worker_stats`)+ `merged_worker_hashrates`(按 worker 名合并取最大、单池故障跳过)。runpod/vast/salad reconcile 改用合并算力 → 混合机群(部分在 pearlhash、部分在 twpool)算力都查得到,**不误杀**。
- **新抢矿池可切换**:每账号配置 `pool`(顶层),create 用 `effective_image`;配置页每账号矿池下拉(`/api/set-pool`,只改新抢、不迁移)。
- **一键迁移**(`/api/migrate`,确认词 `MIGRATE` 严格校验):
  - **runpod**:`POST /v1/pods/{id}/update` 原地换 imageName+env 触发 reset(实测确认 env 整体替换)。
  - **vast**:`DELETE` 销毁,扫描循环用新池镜像重租。
  - **salad**:`PATCH containers/{group}`(merge-patch+json)改 image+env(整体替换)→ Salad 自动重建实例 + 保守显式 recreate(实测 group gpu10 迁移成功)。
  - UI:每账号「迁移现有机器到所选池」按钮 + 全局「一键全部账号迁移」按钮,均经 `MIGRATE` 确认 prompt。
- **host 兜底池感知**:twpool 不读 PRL_HOST → 切 host 无意义,迁移到 twpool 后自动禁用 host 兜底。

### Notes — 注意
- 迁移只改现有机器 + 落盘 pool;**新抢用新池需重启对应账号监控才生效**。
- salad 现役镜像为 `mrkidbk/pearl-miner:latest`(用 `WORKER_NAME`),迁移取 `salad_group_worker_name` 沿用 worker 名。

---

## [登录页改版 · 海洋玻璃主视觉(v2)] — 2026-06-05

### Changed
- **登录页按设计稿高保真重写**（`design_handoff_pearl_login/` v2）：整页**海洋→沙滩场景**(天空/海面渐变 + 阳光 + 漂移焦散光纹 + 浪花泡沫线 + 沙滩斑点),左侧自旋虹彩**珍珠**漂浮水中(纯 CSS 渐变 + box-shadow + conic/mask)+ whirl 动效(扩散水波纹 ×3 + 3 圈 + 双轨道粒子 + 彗尾扫光 + 光晕);右侧**玻璃拟态(frosted glass)登录卡**(`backdrop-filter:blur(22px)`,海面透过卡片隐约可见,顶部高光 sheen)。
- 卡片内容:eyebrow / 标题(珍珠=深蓝)/ 副标题 / 干净密码框(无图标,focus 白霜环)/ 渐变登录钮(含 inset 高光)/ 分隔线 / 偷窥模式页脚。
- Noto Sans SC + JetBrains Mono;精确还原尺寸/色值/阴影/动画;`@media(prefers-reduced-motion)` 关闭全部动画;820px 以下单列堆叠。
- 全部样式 `#login` 作用域隔离(避免与看板 `.card/.sub` 等冲突),登录逻辑(`login()`/`guestLogin()`/`#pw`/`#lerr`)不变;登录页固定海洋浅色,不随看板亮/暗主题切换。(上一版纯浅蓝背景 + 白卡 v1 已被本版取代。)
- 微调:珍珠左移(`translateX(-72px)`,移动端复位);登录卡缩小(max-width 296px + 收紧 padding);登录按钮半透明渐变(rgba .82,海面透出);**背景去掉沙滩,改纯海洋 + 双层浪花**(海延伸到底加深海色,浪花泡沫线移到底部 + 上方加一条反向慢摇的波线)。
---

## [行情图表 + 实时币价] — 2026-06-05

### Added
- **PRL/USDT K 线图面板**:总览页币价行下方可折叠行情面板(默认收起,点标题展开)。
  - 纯 Canvas 绘制(无外部库):Candlestick(绿涨/红跌)+ **EMA20(金)/EMA60(紫)** + 价格 Y 轴 + 时间 X 轴。
  - 底部独立 Canvas 成交量柱状图(颜色跟涨跌联动)。
  - 周期切换:**15m / 1h / 4h / 1d**;头部实时显示区间涨跌幅。
  - hover crosshair **OHLCV tooltip**。
  - `renderOverview` 每 10s 刷新后保持展开状态并自动重绘。
- **实时 PRL/USDT 币价**:自动从 SafeTrade REST API 拉取(`ticker.last`),后台每 30s 刷新;API 失败 fallback 到缓存旧值。看板显示「**● 实时**」标志,无需手动填写。移除了原手动「币价」输入框与「保存币价」按钮。
- 后端 `/api/kline?period=` 代理端点:serve-stale 缓存(各周期独立 TTL),API 失败返回旧缓存/空列表。

---

## [看板产出统计与体验改版] — 2026-06-05

围绕收益可视化与界面体验的一批改动（前端为主），与多账号合并在同一天完成。

### Added — 新增
- **累计产出 / 累计折合利润卡**：替换原「待结算 / 近期已结算 PEARL」两卡。产出 = 矿池正向 epoch credit（提现不计）+ 当前 pending，**自重置起算**（从 0 单调起涨，结算不跳变、提现不减）；折合利润 = 产出 × 币价 − 累计租金。
- **币价配置 + 重置统计**（仅 admin）：可填币价（默认 0.75）实时折算；一键重置租金/产出/利润从当前起算（保留币价）。
- **亮 / 暗双主题**：默认亮色，左下角透明切换钮，`localStorage` 持久化 + 防首屏闪烁。
- **文档页**：导航「文档」组 → 工具说明（含本地部署四步 + docker 拉取原理）+ 挖珠教程（小白四步：钱包 → 租卡 → 配置 → 卖币 SafeTrade）。
- **工具集**新增「交易平台」组（SafeTrade / Pearl OTC / OKX Web3）。
- 左下页脚加 **GitHub 项目链接**（与主题切换并排）。

### Changed — 变更
- 导航改名：总览 → **仪表盘**、工具链接 → **工具集**、配置 → **配置工作台**；favicon 去黑底透明、品牿字距加宽、菜单加极细分隔线、淡化选中背景；钱包卡按钮 ACCOUNT → 改 **PearlHash →**。
- 总览各平台租用情况**按在跑机器数排序**（有机器的账号在上）。

### Fixed — 修复
- **Salad 移动卡价格显示区间而非单价**：`gpu_key` 未剥结尾「 GPU」后缀（`RTX 5090 Laptop GPU`）致名字不匹配价表 → fallback 到整档 min–max 区间。
- **侧栏滚动到页面底部时 footer 上移**：侧栏 `position:sticky` 改 `fixed`，常驻左下。
- **配置侧栏账号项要进配置页才出现**：改由总览加载时即用 `/api/rentals` 填充。

## [多账号支持] — 2026-06-05

将单账号架构升级为「多平台 × 多账号」,并修复了一批 dashboard 显示缺陷。`sniper.py` 监控/抢卡核心**未改动**,改动集中在启动脚本、dashboard 与配置组织。

### Added — 新增
- **多平台多账号支持**:可为同一平台配置多个账号(`configs/config.<平台>-<N>.json` + `.env` 里的 `<KEY>_<N>`),各账号独立监控、抢卡、`state.*`/`logs/*` 隔离。**新增第 N 个账号零代码改动**——放一个 config 文件 + 在 `.env` 加对应 key + 重启即可。
- dashboard 总览页**按账号渲染**:每个账号一个卡片(独立 RUNNING/余额/机器表),顶部为合并汇总(同钱包)。
- dashboard 配置页**按账号**:左侧栏动态列出各账号,可分别编辑账号 1/2/… 的门槛、key、raw JSON。
- 所有操作(暂停租用 / 重启 / 关闭单机 / 保存配置)**按账号定位**到对应 config/进程;关闭/启动时按账号注入对应 key。
- Salad 实例表格新增「组」列,显示 Container Group 名(gpu1…gpuN)。
- 账号标签采用「平台-标识」格式(Salad 自动用组织名,如 `salad-duffett` / `salad-mrkidbk`;其它平台可在 `account_label` 自定义)。
- **Salad / TensorDock 手填余额估算**:这两个平台**无余额查询 API**(已实测 14 个候选端点全 404,官方 Python SDK 也无任何 billing/credit/balance 服务),无法像 Vast(`credit`)/ RunPod(`clientBalance`)那样自动拉取。新增 `balance_usd` 字段(看板配置页可改,保存时自动记录 `balance_asof` 时间戳),看板按当前消耗速率递减,显示「估算余额 $X · 约 Yh 花完」。不知充值/精确计费,会逐渐偏差,需偶尔回填校准。
- **总览内联编辑余额**:无余额 API 的平台(salad/tensordock)在总览卡片余额位置直接点击 ✎ 即可就地填写/修改余额(`$` 前缀输入框 + ✓/✕,回车保存 Esc 取消,编辑时暂停自动刷新),无需再去配置页;复用 `save-platform` 端点(自动盖 `balance_asof`)。Vast/RunPod 余额来自 API,不显示编辑入口。

### Fixed — 修复
- **看板每次刷新卡顿数十秒(性能)**:`salad_live` 对每个容器组**串行**打 2 次 Salad API(实测 10 组 = 22 次串行 ≈ 11.7s/账号,3 个 salad 账号一次构建 >35s),且全在 HTTP 请求线程同步执行;叠加前端每 10s 自动刷新 + 30s 缓存,冷窗口频繁、并发惊群。改为:① 每账号容器组请求用**线程池并发**拉取(11.7s→~0.6s);② 后台 daemon 线程每 30s **预热所有缓存**(salad/余额/矿池),HTTP 请求只读缓存(**serve-stale**,永不阻塞;仅缓存超兜底上限才同步重算)。实测接口 `/api/summary` ~25ms、`/api/rentals` ~0.3s(原 >35s/超时)。
- **dashboard 看不到 Salad 机器(HTTP 400)**:`salad_live` 内层循环把组名变量 `nm` 覆盖成 GPU 名,导致用「RTX 4090 (24 GB)」当组名查 `/instances` 报 400、显示「无在跑机器」。
- **GPU 列显示的是配置而非实际在跑的卡**:改为用矿池 worker 名(= 组名)匹配,显示每台实际 GPU(如 `RTX 4070 Ti SUPER` / `RTX 5070 Ti`)。
- **start-all / stop-all 的 dashboard 进程匹配错误**:`pgrep` 用 `python3 dashboard.py`,而实际进程名为 `Python dashboard.py`,导致重复启动 / 停不掉 dashboard。
- **start-all 误判 Salad「已在运行」而跳过**:旧脚本 `salad_watchdog.py` 进程命令行含 `config.salad.json` 被宽匹配命中;改为精确匹配 `sniper.py --config <文件>`。

### Changed — 变更
- `scripts/start-all.sh`:由「无条件起 4 平台」改为**扫描账号 config、按 `enabled` 启动、按 `api_key_env` 注入 per-account key**;直接调 `python3 sniper.py`(绕过会 `source .env` 覆盖注入 key 的 `run-*.sh`)。
- `scripts/stop-all.sh`:按账号 config 扫描停止(可停掉 `-N` 账号)。
- `.gitignore`:真实平台配置改为通配 `configs/config.*.json` + 保留 `*.example.json`,自动覆盖任意账号数(含钱包的真实 config 始终本地保留)。
- Salad 监控由独立旧脚本 `salad_watchdog.py` **统一迁移到当前项目的 sniper**(同一套 start-all/stop-all/dashboard 管理)。
- `dashboard.py` 后端:新增 `platform_of` / `list_accounts` / `account_label` / `key_var_for` 等 helper;`salad_live` / `platform_balance` 改为 **per-account 缓存**(消除两个同平台账号互相覆盖数据的串号风险);`build_summary/rentals/config/full_config`、`tick_spend` 遍历账号;`prl_address` 不再硬编码读 vast config。

### Configuration / Ops — 配置与运维说明
- 启动采用「做法 B」:未使用的 vast / tensordock 在各自 config 里 `enabled=false`,start-all 自动跳过(不空跑、不抢卡)。
- Salad 监控走「路径 B」(逐实例解析容器日志算力,按 `instance_id` 闭环),门槛由 `gpu_class_names`(Salad class id → 型号)+ `min_hashrate_th`(满载 × 95%)决定;**实例的 reallocate 精确到 `instance_id`,不依赖矿池命名**。
- 注意:Salad 的「暂停租用」是**平台级**(sniper 按平台名读 `control/<平台>.rent-paused`),同平台多个账号会联动。
- 护栏(`max_active_instances` / `max_total_hourly_usd` / `thresholds`)按账号独立计算,最坏总花费 = 各账号上限之和。

---

*本次升级由 Claude (Anthropic · Claude Code) 协助设计与实现 — 2026-06-05*（并非协助）

---

> 以下为 CHANGELOG 建立之前的历史里程碑，据 `docs/plan-archive.md` / `docs/issues.md` 补记，保持变更记录完整。

## [项目首日：抢租核心 + 网页看板 + 开源化 + 稳健性] — 2026-06-04

项目第一天，从零搭出整套系统（M1 抢租核心 → M2 网页看板 → M3 开源化 → M4 稳健性打磨）。

### Added — 新增
- **抢租核心 `sniper.py`**（纯标准库）：Vast.ai / RunPod / TensorDock / Salad 扫描 → 命中价格 & 算力阈值租用 → 监控算力 → 低效/不挖自动销毁拉黑；每平台独立进程 + 独立 `state.<plat>.json`/`logs/<plat>.log` 隔离（`SNIPER_STATE_PATH`/`LOG_PATH`，见 ISS-002）；`--config`/`--live`/`--once` CLI。
- **网页看板 `dashboard.py`**（纯 stdlib `http.server`，:8787）：密码门 + 无状态签名 cookie；总览（钱包/算力/累计租金/待结算·已结算 PEARL/各平台余额 + 预计花完时间）、配置页（公共 + 4 平台二级标签，表单 + raw JSON）、工具链接、后台日志 tail；暂停/恢复租用、重启、关机、改密码；暗色主题、左侧导航。
- 看板**访客(偷窥)模式**（签名 cookie 区分 admin/guest，访客免密码只读）；发光珍珠 **logo + SVG favicon**。
- **Windows(PowerShell)启停脚本** `start-all.ps1` / `stop-all.ps1`（对齐 Linux 版）。
- Salad **按 GPU 型号判健康**（逐实例按 machine_id 从矿池解析型号取 `min_hashrate_th`；`normalize_gpu` 扩 40/50 系列）。
- **开源化**：`.example` 配置模板 + README + `.gitignore`（保护 `.env`/`keys/`/真实 config/state/logs/docs），推公开仓库 `github.com/kuzicode/pearl-wzz-dashboard`（仅 `gpu-sniper-shareable/` 子目录）。

### Fixed — 修复
- Salad **坏实例(完全无算力日志)不被回收**一直烧钱 → 矿池兜底取算力、查不到按 0 计时回收（ISS-010）。
- 看板**全称 GPU key**（`NVIDIA GeForce RTX 5090`）表格空 → 保存用空值覆盖丢失（ISS-010）。
- Salad 踢出门槛「每行型号」不生效 + 24h 计时器掩盖误杀 4070（ISS-009）。
- TensorDock 无算力按 0 回收；RunPod 不挖的 dud pod 回收（ISS-005）。
- Vast 日志 S3 上传竞态 403 重试（ISS-003）；Salad 日志默认 UA 被 WAF 挡 403（ISS-004）；`pkill -f dashboard.py` 自杀（ISS-001）。

### Changed — 变更
- 配置合并到**单一 `.env`**（值单引号转义防注入）；移除 byobu 依赖，改 **nohup/setsid 一键起停**。
- Codex review 修复 5 处（成本护栏盲点 / 回收漏洞 / key 注入等）。
