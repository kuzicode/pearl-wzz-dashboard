"""矿池身份: 注册表 / 池费 / 按镜像与入口地址识别矿池 / 矿机变体 / 池专属容器 env。
sniper.py 与 dashboard.py 共用这一份(从前两边各有副本, 改一边漏一边, 见 ISS-030)。只依赖标准库与本模块自身。"""

POOL_FEE = {"pearlhash": 0.01, "kryptex": 0.03, "f2pool": 0.03}   # PearlHash 1%; Kryptex PRL PPS+ 3%, 2026-10 官网; F2Pool 未公开, 按其预估产率反推约 3%(待核实)
POOL_FEE_DEFAULT = 0.01

POOLS = {
    # platforms: 该池矿机验证可跑的平台; requires: 租宿主时的硬性要求(min_cuda→Vast cuda_max_good / RunPod allowedCudaVersions,
    # min_reliability→Vast 可靠度下限, grace_seconds_min→回收宽限下限, 矿池只给 30m 均值时防误杀); note: 配置页提示。
    "pearlhash": {"label": "PearlHash",
                  "image": "docker.io/kuzigmgm/pearl-miner:v13-wildrig",
                  "reads_prl_host": True, "default_host": "pool.pearlhash.xyz:9000",
                  "platforms": ["vast", "runpod", "tensordock", "salad", "quickpod", "clore"], "requires": {},
                  "note": "WildRig(OpenCL), 对宿主驱动容错好; 池 0% 抽水; API 给实时算力"},
    "twpool":    {"label": "TW Pool (小幣礦池)",
                  "image": "docker.io/mrkidbk/pearl-miner-twpool:v1.9.1",
                  "reads_prl_host": False},
    "herominers": {"label": "HeroMiners",
                   "image": "docker.io/mrkidbk/pearl-miner-herominers:v3.3.6",
                   "reads_prl_host": False},  # 镜像自动测速 15 节点选优, 不需 PRL_HOST
    "pearlfortune": {"label": "PearlFortune",
                     "image": "docker.io/mrkidbk/pearl-miner-pearlfortune:latest",
                     "reads_prl_host": False},  # 默认 global.pearlfortune.org:443; PRL_PROXY 可覆盖(v1 不接)
    "kryptex":   {"label": "Kryptex",
                  "image": "docker.io/kuzigmgm/pearl-miner:srb-3.7.9-r1",   # = 默认矿机变体(default_miner)的镜像, 向后兼容
                  "reads_prl_host": True, "default_host": "stratum+ssl://prl.kryptex.network:8048",
                  "platforms": ["vast", "salad", "runpod", "quickpod", "clore"],
                  "requires": {"min_reliability": 0.98, "grace_seconds_min": 1800},   # 池级要求; 矿机相关(min_cuda)在 miners[*].requires
                  # 同一池可选矿机变体(账号 config 顶层 "miner"), 记账/基线仍按 kryptex 一个池; 变体 requires 与池级合并
                  "miners": {
                      "krig":     {"label": "KRig 1.5.2", "image": "docker.io/kuzigmgm/pearl-miner:krig-1.5.2",
                                   "requires": {"min_cuda": 13.0},
                                   "note": "Kryptex 官方 miner, 0% dev fee; 宿主驱动需支持 CUDA ≥ 13(≥580)且显存独占, RunPod 社区机约半数 cuInit 失败"},
                      "srbminer": {"label": "SRBMiner-MULTI 3.7.9", "image": "docker.io/kuzigmgm/pearl-miner:srb-3.7.9-r1",
                                   "requires": {},
                                   "note": "pearlhash 不硬性要求 CUDA 13, 旧驱动宿主可跑(5000 系建议 ≥580); dev fee 2%; 无 TTY 不输出故镜像用 script 伪终端包裹, 每分钟打 hashrate_th_s= 行"},
                  },
                  "default_miner": "srbminer",   # 2026-09-22 起默认 SRBMiner(RunPod 只有它跑得起来); 账号 config 顶层 miner=krig 可回退
                  "note": "只认官方 TLS 入口; 矿池只给 30 分钟均值, 新机前 30 分钟算力偏低; 矿机默认 SRBMiner(旧驱动可跑, dev fee 2%), 可选 KRig(需 CUDA 13, 0% fee)"},
    # F2Pool: 账户名挖矿(登录 "账户名.矿工名", 账号 config 顶层 f2pool_account), 官方教程只给明文 pearl.f2pool.com:5500。
    # 镜像 = docker-srb 以 --build-arg PRL_POOL=f2pool 构建, tag 含 f2pool(pool_of_image 靠它识别)。
    "f2pool":    {"label": "F2Pool",
                  "image": "docker.io/kuzigmgm/pearl-miner:srb-f2pool-3.7.9-r1",
                  "reads_prl_host": True, "default_host": "stratum+tcp://pearl.f2pool.com:5500",
                  "platforms": ["vast", "salad", "runpod", "quickpod", "clore"],
                  "requires": {"min_reliability": 0.98},
                  "note": "账户名挖矿(需填 f2pool_account), 明文 5500 入口; 池费 3% 为推算待核实; 矿池算力/余额需 .env F2POOL_API_TOKEN(v1), 未配时 RunPod 无法判低效"},
}


def _raw_pool(config):
    """读取并规范化 config 中的原始 pool 字符串。"""
    return str((config or {}).get("pool") or "").strip()


def active_pool(config):
    """从 config 读 pool, 返回 POOLS 中的有效 key; 未知/未配则按 config['image'] 推断(pool_of_image), 再不行默认 'pearlhash'。"""
    p = _raw_pool(config)
    if p in POOLS:
        return p
    guess = pool_of_image((config or {}).get("image"))
    return guess if guess in POOLS else "pearlhash"


def pool_miners(pool_id):
    """该池可选矿机变体 {miner_id: {label,image,requires,note}}; 无变体的池返回 {}。"""
    return dict((POOLS.get(pool_id) or {}).get("miners") or {})


def active_miner(config, pool_id=None):
    """账号选的矿机变体(config 顶层 'miner'); 无效/未配 → 池的 default_miner; 池无变体 → None。"""
    pool_id = pool_id or active_pool(config)
    miners = pool_miners(pool_id)
    if not miners:
        return None
    m = str((config or {}).get("miner") or "").strip()
    if m in miners:
        return m
    d = (POOLS.get(pool_id) or {}).get("default_miner")
    return d if d in miners else next(iter(miners))


def pool_requires(pool_id, config=None):
    """池级 requires 与矿机变体 requires 合并(变体覆盖同名键)。不传 config 时用池的默认变体(向后兼容: kryptex 默认 KRig 仍要 CUDA 13)。"""
    req = dict((POOLS.get(pool_id) or {}).get("requires") or {})
    miners = pool_miners(pool_id)
    if miners:
        m = active_miner(config, pool_id) if config is not None else (POOLS.get(pool_id) or {}).get("default_miner")
        req.update((miners.get(m) or {}).get("requires") or {})
    return req


def pool_supports(pool_id, platform):
    """该池矿机是否在此平台验证可跑; 未声明 platforms 的池视为全平台。"""
    plats = (POOLS.get(pool_id) or {}).get("platforms")
    return True if not plats else platform in plats


def effective_grace(cfg, pool_id, default=300):
    """回收宽限 = max(账号配置, 池要求下限)。Kryptex 只给 30m 均值 → 至少 1800s。"""
    return max(int((cfg or {}).get("hashrate_grace_seconds", default)), int(pool_requires(pool_id).get("grace_seconds_min", 0)))


def effective_image(config):
    """新抢机器用的镜像: 优先按配置的 pool(及其矿机变体 miner)镜像; pool 未知/未配则回退 config['image']。"""
    p = _raw_pool(config)
    if p in POOLS:
        m = active_miner(config, p)
        if m:
            return (pool_miners(p).get(m) or {}).get("image") or POOLS[p]["image"]
        return POOLS[p]["image"]
    return (config or {}).get("image")


def pool_of_image(image):
    """按镜像判定矿池: 先认 herominers/pearlfortune; twpool/conishc→'twpool'; 其它非空→'pearlhash'; 空→None。"""
    s = str(image or "").lower()
    if not s:
        return None
    if "herominers" in s:
        return "herominers"
    if "pearlfortune" in s:
        return "pearlfortune"
    if "twpool" in s or "conishc" in s:
        return "twpool"
    if "f2pool" in s:   # srb-f2pool-*: 须在 srb 判断之前, 否则被认成 Kryptex
        return "f2pool"
    if "kryptex" in s or "krig" in s or "srb" in s:   # srb-* = SRBMiner 变体镜像, 同属 Kryptex 池
        return "kryptex"
    return "pearlhash"


def pool_of_host(host):
    """矿池入口地址归属: 按域名关键字认 f2pool / kryptex / pearlhash; 认不出(自定义代理等) → None。"""
    h = str(host or "").lower()
    for k in ("f2pool", "kryptex", "pearlhash"):
        if k in h:
            return k
    return None


def pool_host(config, pool_id):
    """容器 PRL_HOST: 账号 prl_host 属于该池(或认不出归属)时照用, 属于别的池时改用该池 default_host。
    看板切池只改 config['pool'], prl_host 仍是旧池地址; 不纠正会拿 F2Pool 账户名去登 Kryptex 入口, 矿机挖不了还一直计费。"""
    host = str((config or {}).get("prl_host") or "").strip()
    owner = pool_of_host(host)
    default = (POOLS.get(pool_id) or {}).get("default_host")
    if default and (not host or (owner and owner != pool_id)):
        return default
    return host


def pool_env_extra(config, pool_id):
    """池专属的额外容器 env(建机 make_env 与迁移 migrate_account 共用)。
    F2Pool 账户名挖矿: 镜像用 "F2POOL_ACCOUNT.worker" 登录, 缺账户名会拒绝启动。"""
    if pool_id == "f2pool":
        return {"PRL_POOL": "f2pool", "F2POOL_ACCOUNT": str(config.get("f2pool_account") or "").strip()}
    return {}


def pool_fee(pool):
    """该池费率; 未登记的池按 POOL_FEE_DEFAULT。"""
    return POOL_FEE.get(pool, POOL_FEE_DEFAULT)
