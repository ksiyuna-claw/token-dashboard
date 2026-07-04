#!/usr/bin/env python3
"""虾厂 Token 看板 HTTP 服务"""
import http.server, json, subprocess, os, time, urllib.request, urllib.error, threading, uuid, logging, logging.handlers, gc, resource

# 🔴 内存泄漏防护 (2026-06-24): 50分钟 RSS 2GB，LRU 没拦住，加硬防线
_MEM_WATCH_INTERVAL = 60  # 秒
_MEM_GC_THRESHOLD = 400 * 1024 * 1024   # 400MB 触发 gc.collect()
_MEM_KILL_THRESHOLD = 800 * 1024 * 1024  # 800MB 自杀（launchd 会拉新进程）

def _memory_watchdog_loop():
    """后台线程：定期检查 RSS，超阈值则 gc 或自杀\n    RSS 硬限 1GB 在 plist 里设了但 launchd 可能未生效（kickstart 没重读 plist），\n    所以这里做应用层防线。"""
    while True:
        time.sleep(_MEM_WATCH_INTERVAL)
        try:
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if os.uname().sysname == 'Darwin':
                rss = rss  # macOS 单位已是 bytes
            else:
                rss *= 1024  # Linux 单位是 KB
            if rss > _MEM_KILL_THRESHOLD:
                logging.error(f'[MEM] RSS {rss//1024//1024}MB > {_MEM_KILL_THRESHOLD//1024//1024}MB, 自杀让 launchd 拉新进程')
                os._exit(1)
            elif rss > _MEM_GC_THRESHOLD:
                before = rss
                gc.collect()
                rss2 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                logging.warning(f'[MEM] GC: RSS {before//1024//1024}MB → {rss2//1024//1024}MB')
        except Exception as e:
            logging.error(f'[MEM] watchdog error: {e}')

logger = logging.getLogger()
logger.setLevel(logging.INFO)
# RotatingFileHandler 防止日志爆盘：单文件 50MB，保留 3 份
_rfh = logging.handlers.RotatingFileHandler(
    '/tmp/token_dashboard.log',
    maxBytes=50 * 1024 * 1024,
    backupCount=3,
    encoding='utf-8'
)
_rfh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(_rfh)
_console = logging.StreamHandler()
_console.setLevel(logging.WARNING)  # stderr→plist 日志只收 WARNING+，INFO 靠 rotating file handler
_console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(_console)

# WS 错误风暴防护：60 秒内同类错误只报一次
_last_ws_error_at = 0.0
def _log_ws_error(e):
    global _last_ws_error_at
    now = time.time()
    if now - _last_ws_error_at < 60:
        return
    _last_ws_error_at = now
    logging.warning(f'[WS] error (rate-limited 60s): {type(e).__name__}: {str(e)[:200]}')

# 海外 API 需要走代理
_PROXY_HANDLER = urllib.request.ProxyHandler({
    'http': 'http://127.0.0.1:7890',
    'https': 'http://127.0.0.1:7890',
})
_PROXY_OPENER = urllib.request.build_opener(_PROXY_HANDLER)

DIR = os.path.join(os.path.expanduser('~'), '.openclaw/workspace/ai_workspace/scripts')
SNAPSHOT = os.path.join(DIR, 'token_snapshot.json')
OPENCLAW_JSON = os.path.join(os.path.expanduser('~'), '.openclaw/openclaw.json')
AUTH_PROFILES = os.path.join(os.path.expanduser('~'), '.openclaw/agents/main/agent/auth-profiles.json')

# ── 缓存层：避免每次前端轮询都 spawn CLI subprocess ──────────────────────────
_CACHE_TTL = 15  # 缓存有效期（秒）
_sessions_cache = {'data': None, 'ts': 0}  # openclaw sessions --json 结果缓存
_sessions_lock = threading.Lock()

# ── WS 实时状态：Gateway WebSocket 事件维护的 agent 活动状态 ────────────────────
# 当 WS 收到 agent 相关事件时，记录 agentId → 最后活动时间戳
_agent_ws_activity = {}  # {agentId: last_activity_ms}
# Gateway sessions.changed 事件的 hasActiveRun 字段 → 精确的"模型正在思考"信号
# {agentId: True} 表示该 agent 当前有一个活跃的模型 run（从开始到回复结束）
_agent_active_runs = {}  # {agentId: True}
_ws_thread = None
_ws_connected = False

# ── Provider 额度 API 映射 ──────────────────────────────
# baseUrl 模式匹配 → (额度API URL, 类型)
# type: 'zhipu_quota' = 智谱额度API, 'deepseek_balance' = DeepSeek余额API
PROVIDER_QUOTA_API = {
    'open.bigmodel.cn': ('https://open.bigmodel.cn/api/monitor/usage/quota/limit', 'zhipu_quota'),
    'api.z.ai': ('https://api.z.ai/api/monitor/usage/quota/limit', 'zhipu_quota'),
    'api.deepseek.com': ('https://api.deepseek.com/user/balance', 'deepseek_balance'),
    'api.minimaxi.com': ('https://api.minimaxi.com/v1/token_plan/remains', 'minimax_quota'),
    'api.moonshot.cn': ('https://api.moonshot.cn/v1/users/me/balance', 'kimi_balance'),
    'api.kimi.com': ('https://api.kimi.com/coding/v1/usages', 'kimi_coding_plan'),
    # dashscope（阿里云百炼）是语音/声图模型（ASR/TTS/文生图），非 LLM 文本生成，
    # Token 看板只看 LLM 用量，故不在此注册 → _get_providers() 会跳过它，看板不展示。
    # （openclaw.json 里的 dashscope provider 必须保留，它供语音/声图调用，与本看板无关）
}

PROVIDER_LABELS = {
    'zhipu': '智谱 BigModel（国内）',
    'zai': '智谱 BigModel（海外）',
    'deepseek': 'DeepSeek',
    'minimax': 'MiniMax（编程套餐）',
    'kimi': 'Kimi（Moonshot）',
}

# 从 transcript 读实际调用模型（session 元数据的 model 是配置值，不准）
AGENTS_DIR = os.path.join(os.path.expanduser('~'), '.openclaw/agents')
# 🔄 2026-06-24 改造：从「只读最后一条模型」→「读最近 N 条 assistant 消息的模型分布 + 时间戳」
#   原因：单条快照在 glm-5.2 限流 fallback 到 deepseek 时会乱跳，看着像「当前模型不准」。
#   现在统计最近 N 条的分布，让人一眼看出「主力是谁、偶尔 fallback 几次」。
_DIST_SAMPLE_N = 10        # 采样最近 N 条 assistant 消息（可调）
_dist_cache = {}           # {session_id: (ts, payload)}
_dist_cache_lock = threading.Lock()
_DIST_CACHE_TTL = 10       # 秒（从 30s 缩短到 10s，让显示更跟手）
_DIST_CACHE_MAX = 256      # LRU 上限，防内存泄漏

def _dist_cache_put(session_id, payload, now):
    """加入缓存，超过上限时删除最旧的一半（防内存泄漏）"""
    with _dist_cache_lock:
        if len(_dist_cache) >= _DIST_CACHE_MAX:
            sorted_keys = sorted(_dist_cache.keys(), key=lambda k: _dist_cache[k][0])
            for k in sorted_keys[: _DIST_CACHE_MAX // 2]:
                _dist_cache.pop(k, None)
        _dist_cache[session_id] = (now, payload)

def _parse_msg_ts(obj, msg):
    """返回 assistant 消息的 epoch 毫秒。优先 message.timestamp（已是 ms），fallback 解析顶层 ISO timestamp。"""
    t = msg.get('timestamp')
    if isinstance(t, (int, float)) and t > 0:
        return int(t)
    iso = obj.get('timestamp')
    if isinstance(iso, str) and iso:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None

def _read_session_transcript(session_id, agent_id):
    """读单个 session transcript 的 assistant 消息，返回 [(model, provider, ts), ...]"""
    tdir = os.path.join(AGENTS_DIR, agent_id, 'sessions')
    seq = []
    try:
        if os.path.isdir(tdir):
            primary, archive = [], []
            for fn in os.listdir(tdir):
                if fn.startswith(session_id) and fn.endswith('.jsonl') and 'trajectory' not in fn:
                    primary.append(os.path.join(tdir, fn))
                elif fn.startswith(session_id) and '.jsonl.reset.' in fn:
                    archive.append(os.path.join(tdir, fn))
            primary.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            archive.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            candidates = primary or archive
            if candidates:
                with open(candidates[0]) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = obj.get('message', {})
                        if msg.get('role') == 'assistant' and msg.get('model'):
                            m = msg['model']
                            if m in ('delivery-mirror', 'gateway'):
                                continue
                            seq.append((m, msg.get('provider', ''), _parse_msg_ts(obj, msg)))
    except Exception:
        pass
    return seq

def _recent_model_dist(session_id, agent_id, n=_DIST_SAMPLE_N, extra_session_ids=None):
    """读最近 N 条 assistant 消息的实际 model/provider，返回分布 + 最后一条 + 时间戳。带 10s 缓存 + LRU 上限。
    合并主 session + extra（subagent）session 的 transcript，按时间统一排序后取最近 N 条。
    返回 dict：
      {'last': {'model','provider','ts'}, 'dist': [{'model','provider','count'}...], 'sampled': int}
    dist 按 count 降序（count 相同则最近出现的靠前），dist[0] 即「主力模型」。
    """
    empty = {'last': {'model': None, 'provider': None, 'ts': None}, 'dist': [], 'sampled': 0}
    if not session_id or not agent_id:
        return empty
    now = time.time()
    cache_key = session_id + '|subs:' + ','.join(sorted(extra_session_ids or []))
    with _dist_cache_lock:
        c = _dist_cache.get(cache_key)
        if c and now - c[0] < _DIST_CACHE_TTL:
            return c[1]
    # 读主 session transcript
    seq = _read_session_transcript(session_id, agent_id)
    # 合并 subagent session transcripts（把分身的模型调用纳入统计）
    if extra_session_ids:
        for sid in extra_session_ids:
            if sid and sid != session_id:
                seq.extend(_read_session_transcript(sid, agent_id))
    # 按时间戳排序（主+子混合后统一排序），无时间戳的排最后
    seq.sort(key=lambda x: x[2] or 0)
    recent = seq[-n:]
    payload = dict(empty)
    if recent:
        # 按 (model, provider) 统计分布；dict 在 py3.7+ 保序，记录最近出现位置用于平手排序
        counts = {}
        last_idx = {}
        for idx, (m, p, ts) in enumerate(recent):
            key = (m, p)
            counts[key] = counts.get(key, 0) + 1
            last_idx[key] = idx
        dist = [{'model': k[0], 'provider': k[1], 'count': v} for k, v in counts.items()]
        dist.sort(key=lambda x: (-x['count'], -last_idx[(x['model'], x['provider'])]))
        lm, lp, lts = recent[-1]
        payload = {
            'last': {'model': lm, 'provider': lp, 'ts': lts},
            'dist': dist,
            'sampled': len(recent),
        }
    _dist_cache_put(cache_key, payload, now)
    return payload

def _gateway_health():
    """读 Gateway 进程 RSS + uptime + 下次 04:00 重启倒计时"""
    import subprocess, time as _time, datetime
    info = {'rss': None, 'rssMB': None, 'uptimeHours': None, 'nextRestartIn': None, 'pid': None}
    try:
        out = subprocess.check_output(['ps', '-A', '-o', 'pid,rss,etime,command'], text=True)
        for line in out.splitlines():
            # 精确匹配 Gateway 进程：node + openclaw/dist/index.js + gateway
            if 'openclaw/dist/index.js' in line and ' gateway ' in line and 'grep' not in line:
                parts = line.split()
                if len(parts) >= 3:
                    info['pid'] = int(parts[0])
                    info['rss'] = int(parts[1])
                    info['rssMB'] = round(int(parts[1]) / 1024, 0)
                    # 解析 etime（格式如 02-03:34:33 或 15:30:00）
                    et = parts[2]
                    if '-' in et:
                        d, hms = et.split('-', 1)
                        h, m, s = hms.split(':')
                        info['uptimeHours'] = round(int(d) * 24 + int(h) + int(m) / 60, 1)
                    else:
                        parts2 = et.split(':')
                        if len(parts2) == 3:
                            info['uptimeHours'] = round(int(parts2[0]) + int(parts2[1]) / 60, 1)
                    break
    except Exception:
        pass
    # 计算下次 04:00 重启倒计时
    try:
        now_dt = _time.localtime()
        # 今天 04:00 或明天 04:00
        today_4 = datetime.datetime(now_dt.tm_year, now_dt.tm_mon, now_dt.tm_mday, 4, 0, 0)
        if now_dt.tm_hour >= 4:
            next_4 = today_4 + datetime.timedelta(days=1)
        else:
            next_4 = today_4
        delta_s = (next_4 - datetime.datetime.now()).total_seconds()
        h = int(delta_s // 3600)
        m = int((delta_s % 3600) // 60)
        info['nextRestartIn'] = f'{h}h{m:02d}m'
    except Exception:
        pass
    return info

def _last_actual_model(session_id, agent_id):
    """向后兼容：返回最近一条 assistant 消息的 (model, provider)。底层复用 _recent_model_dist。"""
    p = _recent_model_dist(session_id, agent_id)
    return p['last']['model'], p['last']['provider']

def _load_auth_keys():
    """从 auth-profiles.json 读取 provider → apiKey 映射"""
    try:
        with open(AUTH_PROFILES) as f:
            data = json.load(f)
        result = {}
        for pid, profile in data.get('profiles', {}).items():
            provider = pid.split(':')[0]
            key = profile.get('key', '')
            if key:
                result[provider] = key
        return result
    except Exception:
        return {}

def _get_providers():
    """从 openclaw.json + auth-profiles.json 读取 providers 配置"""
    try:
        with open(OPENCLAW_JSON) as f:
            cfg = json.load(f)
        providers_cfg = cfg.get('models', {}).get('providers', {})
        auth_keys = _load_auth_keys()
        result = []
        for pid, p in providers_cfg.items():
            base_url = p.get('baseUrl', '')
            # apiKey 优先从 openclaw.json 读，fallback 到 auth-profiles.json
            api_key = p.get('apiKey', '') or auth_keys.get(pid, '')
            # 匹配额度 API
            # 特殊处理：kimi 的 baseUrl 是 api.kimi.com/coding/v1（Coding Plan），
            # 但也有 api.moonshot.cn（开放平台余额）。优先匹配 Coding Plan。
            quota_api, quota_type = None, None
            if pid == 'kimi':
                # 优先用 Coding Plan 额度 API
                quota_api, quota_type = PROVIDER_QUOTA_API.get('api.kimi.com')
            if not quota_api:
                for host, (url, qtype) in PROVIDER_QUOTA_API.items():
                    if host in base_url:
                        quota_api, quota_type = url, qtype
                        break
            if not quota_api and quota_type != 'no_quota':
                continue  # 没有额度 API 的 provider 跳过（no_quota 类型的加占位卡片）
            if not api_key or len(api_key) < 10:
                continue  # 没有有效 apiKey 的跳过
            result.append({
                'id': pid,
                'label': PROVIDER_LABELS.get(pid, pid),
                'baseUrl': base_url,
                'apiKey': api_key,
                'quotaApi': quota_api,
                'quotaType': quota_type,
            })
        return result
    except Exception as e:
        return []

# ── 用途标注（服务/调度 → 所属项目）──────────────────────────
CRON_PURPOSE = {
    '虾厂巡检(凌晨)': '运维 · 清晨语义巡检（罗氏虾）',
    '虾厂巡检(晚)': '运维 · 傍晚综合巡检',
    'Token用量推送': '运维 · Token用量TG推送',
    'AI视频行业日报(周二)': '内容 · AI视频行业周报',
    '网站健康检查': '运维 · AI影视网站健康检查（每3h）',
    'AI影视数据采集': '内容 · AI影视每日数据采集（5am）',
    '国学运势每日推送': '国学运势 · 每日推送',
    '桐姐运势-每日素材推送': '国学运势 · 每日素材推送',
    '每周前沿Agent研究扫描': 'AI研究 · Agent前沿扫描',
    '脱友2回测流水线检查': '内容 · 脱友回测检查',
    'Git自动备份': '运维 · 虾厂Git自动备份（每天06:28）',
    'shanbei-daily-quant-lesson': '量化看板 · 扇贝每日量化课程采集',
}

# 所有 agent ID（用于遍历 cron）
ALL_AGENTS = ['main', 'jiweixia', 'caoxia', 'pipixia', 'banjiexia', 'duijiaoxia', 'shanbei', 'hailuo', 'xiaohexia', 'haixing', 'haima', 'yoooclaw']

# agent 中文名
AGENT_NAMES = {
    'main': '罗氏虾',
    'jiweixia': '基围虾',
    'caoxia': '草虾',
    'pipixia': '皮皮虾',
    'banjiexia': '斑节虾',
    'duijiaoxia': '对焦虾',
    'shanbei': '扇贝',
    'hailuo': '海螺',
    'xiaohexia': '小河虾',
    'haixing': '海星',
    'haima': '海马',
    'yoooclaw': 'YoooClaw',
}

CRONTAB_PURPOSE = {
    # 死代码已移除（2026-06-24 对焦虾审计 P1）：原空 dict 无实际用途
    # 保留结构供将来扩展，/cron 端点通过 _get_crontab() 的 if/elif 匹配
}

def _get_system_health():
    """获取系统健康指标（CPU、内存、CDP Chrome）"""
    result = {'ts': time.time()}
    try:
        # CPU 使用率（1秒采样）
        r = subprocess.run(['/bin/ps', '-A', '-o', '%cpu'], capture_output=True, text=True, timeout=5)
        total_cpu = sum(float(x) for x in r.stdout.strip().split('\n')[1:] if x.strip())
        ncpu = os.cpu_count() or 1
        result['cpu_pct'] = round(total_cpu / ncpu, 1)
        result['cpu_cores'] = ncpu

        # 内存使用率
        r = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True, timeout=5)
        mem = {}
        for line in r.stdout.strip().split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
                try:
                    mem[k.strip()] = int(v.strip().rstrip('.').replace('.', ''))
                except ValueError:
                    pass
        page_size = 16384  # ARM64 macOS default
        used = mem.get('Pages active', 0) + mem.get('Pages wired down', 0) + mem.get('Pages speculative', 0)
        free = mem.get('Pages free', 0) + mem.get('Pages inactive', 0) + mem.get('Pages purgeable', 0)
        total_mem = (used + free) * page_size
        used_mem = used * page_size
        result['mem_total_gb'] = round(total_mem / (1024**3), 1)
        result['mem_used_gb'] = round(used_mem / (1024**3), 1)
        result['mem_pct'] = round(used_mem / total_mem * 100, 1) if total_mem else 0

        # Swap 用量
        r = subprocess.run(['/usr/sbin/sysctl', 'vm.swapusage'], capture_output=True, text=True, timeout=5)
        import re as _re
        m = _re.search(r'total = (\d+).*?used = (\d+)', r.stdout)
        if m:
            result['swap_total_m'] = round(int(m.group(1)) / (1024**2), 1)
            result['swap_used_m'] = round(int(m.group(2)) / (1024**2), 1)
            result['swap_pct'] = round(int(m.group(2)) / int(m.group(1)) * 100, 1) if int(m.group(1)) else 0

        # CDP Chrome 重页面检测
        cdp_renderers = []
        r = subprocess.run(['/bin/ps', 'aux'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.split('\n'):
            if 'chrome-debug' not in line or 'renderer' not in line:
                continue
            parts = line.strip().split()
            try:
                cpu = float(parts[2])
                rss = int(parts[5])
                pid = parts[1]
                if cpu > 10:  # 超过10%才记录
                    cdp_renderers.append({'pid': pid, 'cpu': round(cpu, 1), 'rss_mb': round(rss / 1024, 1)})
            except (ValueError, IndexError):
                continue

        if cdp_renderers:
            # 尝试获取页面标题
            try:
                req = urllib.request.Request('http://127.0.0.1:18800/json', method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    pages = json.loads(resp.read())
                    titles = [p.get('title', '')[:30] for p in pages if p.get('type') == 'page']
                    result['cdp_pages'] = titles[:5]
            except:
                pass
        result['cdp_renderers'] = cdp_renderers
        result['cdp_ok'] = len(cdp_renderers) == 0

        # 磁盘空间
        r = subprocess.run(['/bin/df', '-g', '/'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split('\n')[1:]:
            parts = line.split()
            if len(parts) >= 4:
                result['disk_total_gb'] = float(parts[1])
                result['disk_used_gb'] = float(parts[2])
                result['disk_pct'] = int(parts[4].rstrip('%'))
                break

        # 系统负载
        r = subprocess.run(['/usr/bin/uptime'], capture_output=True, text=True, timeout=5)
        m = _re.search(r'load averages?: ([\d.]+)\s+([\d.]+)\s+([\d.]+)', r.stdout)
        if m:
            result['load_1m'] = float(m.group(1))
            result['load_5m'] = float(m.group(2))
            result['load_15m'] = float(m.group(3))

    except Exception as e:
        result['error'] = str(e)

    return result

LAUNCH_PURPOSE = {
    'ai.openclaw.gateway': '虾厂核心 · OpenClaw Gateway 主进程',
    'ai.hermes.gateway': 'AI基础设施 · Hermes 知识映射 Gateway',
    'com.openclaw.token-dashboard': '运维 · Token看板 HTTP服务 (18888)',
    'com.openclaw.chrome-cdp': '虾厂工具 · CDP Chrome 调试浏览器 (18800)',
    'com.openclaw.cloudflared': '量化看板 · Cloudflare Tunnel 外网穿透',
    'com.openclaw.dashboard': '量化看板 · 看板 HTTP服务',
    'com.openclaw.guoxue-bot': '国学运势 · Telegram Bot 服务',
    'com.openclaw.ai-radar': '量化看板 · AI雷达信号服务',
    'com.openclaw.onchain-dashboard': '量化看板 · 链上数据看板服务',
    'com.openclaw.managed-chrome': '虾厂工具 · CDP Chrome 调试浏览器 (18800)',
    'com.haixing.landing-page': 'AI影视 · 落地页 HTTP服务',
    'io.github.clash-verge-rev.clash-verge-rev': '网络 · Clash Verge 代理工具',
    'netdisk_service': '第三方 · 百度网盘后台服务（非虾厂）',
}

def load_snapshot():
    try:
        with open(SNAPSHOT) as f:
            return json.load(f)
    except:
        return {}

def save_snapshot(data):
    try:
        with open(SNAPSHOT, 'w') as f:
            json.dump(data, f)
    except:
        pass

# 🔴 防内存泄漏 (2026-06-23) 
_SNAPSHOT_MAX_KEYS = 500

def _trim_snapshot_data(data):
    """快照 sessions 超过上限时裁剪，保留最新的"""
    sessions = data.get('sessions', [])
    if len(sessions) > _SNAPSHOT_MAX_KEYS:
        sessions = sorted(sessions, key=lambda s: s.get('updatedAt', 0), reverse=True)[:_SNAPSHOT_MAX_KEYS]
        data['sessions'] = sessions
    return data

def merge_with_snapshot(current_data):
    """用快照填充空数据，同时用当前有效数据更新快照"""
    snap = load_snapshot()
    snap_by_key = {s.get('key',''): s for s in snap.get('sessions', [])}
    new_snap_sessions = {}

    for s in current_data.get('sessions', []):
        key = s.get('key', '')
        # 如果当前 totalTokens 有效（非 null/0），更新快照
        if s.get('totalTokens') is not None and s.get('totalTokens', 0) > 0:
            new_snap_sessions[key] = {
                'inputTokens': s.get('inputTokens', 0),
                'outputTokens': s.get('outputTokens', 0),
                'totalTokens': s.get('totalTokens', 0),
                'model': s.get('model', '-'),
                'modelProvider': s.get('modelProvider', '-'),
                'contextTokens': s.get('contextTokens', 200000),
            }
        elif key in snap_by_key:
            # 当前没数据，用快照补
            old = snap_by_key[key]
            if s.get('totalTokens') is None or s.get('totalTokens', 0) == 0:
                s['inputTokens'] = old.get('inputTokens', 0)
                s['outputTokens'] = old.get('outputTokens', 0)
                s['totalTokens'] = old.get('totalTokens', 0)
            if s.get('model', '-') == '-':
                s['model'] = old.get('model', '-')
            new_snap_sessions[key] = old

    # 保存新快照（裁剪防泄漏）
    snap_data = _trim_snapshot_data({'sessions': list(new_snap_sessions.values()), 'updatedAt': int(time.time()*1000)})
    save_snapshot(snap_data)
    return current_data

def _get_cached_sessions():
    """获取缓存的 sessions 数据，每15秒刷新一次。线程安全。"""
    now = time.time()
    with _sessions_lock:
        if _sessions_cache['data'] is not None and now - _sessions_cache['ts'] < _CACHE_TTL:
            return _sessions_cache['data']
    # 刷新缓存
    try:
        r = subprocess.run(
            ['openclaw', 'sessions', '--json', '--all-agents', '--limit', '50'],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        now_ms = int(time.time() * 1000)
        for x in data.get('sessions', []):
            if 'updatedAt' in x:
                x['ageMs'] = now_ms - x['updatedAt']
        data = merge_with_snapshot(data)
        with _sessions_lock:
            _sessions_cache['data'] = data
            _sessions_cache['ts'] = now
        return data
    except Exception as e:
        # 返回旧缓存（如果有）或空数据
        with _sessions_lock:
            if _sessions_cache['data'] is not None:
                return _sessions_cache['data']
            return {'sessions': [], 'error': str(e)}


def _parse_agent_id_from_session_key(key):
    """从 session key 中提取 agentId。key 格式: agent:<agentId>:<channel>:..."""
    if not key or not key.startswith('agent:'):
        return None
    parts = key.split(':')
    if len(parts) >= 2:
        return parts[1]
    return None


def _ws_listener_loop():
    """后台线程：连接 Gateway WebSocket，监听 agent 活动事件。

    基于 CEO 验证过的测试脚本逻辑重写（2026-06-20）。
    关键：subscribe 响应循环和事件监听循环都用 while True 跳过非 res/event 消息。
    """
    global _ws_connected
    import asyncio

    async def _ws_run():
        global _ws_connected
        import websockets

        token = _get_gateway_token()
        if not token:
            logging.error('[WS] no gateway token, aborting')
            return

        ws_url = 'ws://127.0.0.1:18789/'

        while True:  # 外层重连循环
            try:
                async with websockets.connect(ws_url) as ws:
                    # 1. 接收 challenge nonce
                    msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    challenge = json.loads(msg)
                    logging.info(f'[WS] got challenge: {challenge.get("type", "?")}')

                    # 2. 发送 connect RPC
                    connect_id = str(uuid.uuid4())
                    await ws.send(json.dumps({
                        'type': 'req',
                        'id': connect_id,
                        'method': 'connect',
                        'params': {
                            'minProtocol': 3,
                            'maxProtocol': 4,
                            'client': {
                                'id': 'gateway-client',
                                'version': '1.0.0',
                                'platform': 'darwin',
                                'mode': 'backend',
                            },
                            'caps': ['tool-events'],
                            'auth': {'token': token},
                            'role': 'operator',
                            'scopes': ['operator.read', 'operator.write', 'operator.admin'],
                        },
                    }))

                    # 循环读直到拿到 connect 的 res（跳过插队消息）
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        data = json.loads(msg)
                        if data.get('type') == 'res' and data.get('id') == connect_id:
                            if not data.get('ok'):
                                logging.error(f'[WS] connect failed: {data}')
                                await asyncio.sleep(10)
                                break  # 跳出内层 while，外层 while 会重连
                            logging.info('[WS] connect ok')
                            break
                        # 跳过非 res 消息（如插队的 health event）

                    # 3. 发送 sessions.subscribe
                    sub_id = str(uuid.uuid4())
                    await ws.send(json.dumps({
                        'type': 'req',
                        'id': sub_id,
                        'method': 'sessions.subscribe',
                        'params': {},
                    }))

                    # 循环读直到拿到 subscribe 的 res（跳过插队的 health 等事件）
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        data = json.loads(msg)
                        if data.get('type') == 'res' and data.get('id') == sub_id:
                            logging.info(f'[WS] sessions.subscribe ok={data.get("ok")}')
                            break
                        # 跳过插队的 health / event 等消息

                    _ws_connected = True
                    logging.info('[WS] connected and subscribed, listening for events...')

                    # 4. 事件监听循环
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                            data = json.loads(msg)

                            if data.get('type') != 'event':
                                continue

                            evt = data.get('event', '')
                            payload = data.get('payload', {})
                            now_ms = int(time.time() * 1000)

                            if evt == 'health':
                                continue  # 忽略心跳

                            # sessions.changed 事件携带 hasActiveRun — 这是精确的
                            # "模型正在思考/运行"信号，来自 Gateway 内部的 chatAbortControllers
                            if evt == 'sessions.changed':
                                session_key = payload.get('sessionKey', '')
                                agent_id = _parse_agent_id_from_session_key(session_key)
                                if not agent_id:
                                    agent_id = payload.get('agentId', '')
                                has_active_run = payload.get('hasActiveRun')
                                if agent_id and has_active_run is not None:
                                    if has_active_run:
                                        _agent_active_runs[agent_id] = True
                                        _agent_ws_activity[agent_id] = now_ms
                                        logging.debug(f'[WS] sessions.changed → {agent_id} hasActiveRun=True')
                                    else:
                                        # 模型 run 结束，清除活跃标记
                                        _agent_active_runs.pop(agent_id, None)
                                        logging.debug(f'[WS] sessions.changed → {agent_id} hasActiveRun=False')
                                continue

                            # 其他非 health 事件都尝试提取 agentId
                            session_key = payload.get('sessionKey', '')
                            agent_id = _parse_agent_id_from_session_key(session_key)
                            if not agent_id:
                                agent_id = payload.get('agentId', '')
                            if agent_id:
                                _agent_ws_activity[agent_id] = now_ms
                                logging.debug(f'[WS] {evt} → {agent_id} active')
                            else:
                                logging.debug(f'[WS] {evt} (no agentId)')

                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            _log_ws_error(e)
                            break  # 🔴 2026-06-24 修复：continue 导致死循环（WS死亡后 ws.recv() 立即再抛异常），改为 break 让外层重连

            except Exception as e:
                _ws_connected = False
                logging.error(f'[WS] disconnected: {e}, reconnecting in 10s...')
                await asyncio.sleep(10)

    try:
        asyncio.run(_ws_run())
    except Exception as e:
        logging.warning(f'[WS] fatal (rate-limited): {type(e).__name__}: {str(e)[:200]}')


def _get_gateway_token():
    """从 openclaw.json 读取 Gateway auth token"""
    try:
        with open(OPENCLAW_JSON) as f:
            cfg = json.load(f)
        return cfg.get('gateway', {}).get('auth', {}).get('token', '')
    except Exception:
        return ''


def _detect_active_process_agents():
    """检测哪些 agent 有活跃的 exec 进程（python/node/ffmpeg），返回 agentId set"""
    try:
        r = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split('\n')[1:]  # skip header
        active = set()
        for line in lines:
            ll = line.lower()
            # 只关注可能属于 agent exec 的进程
            if not any(kw in ll for kw in ['python3', 'node', 'ffmpeg', 'kling']):
                continue
            # 排除看板自身和 gateway
            if 'token_dashboard' in ll or 'openclaw/dist/index.js' in ll:
                continue
            if 'SimpleHTTP' in ll or 'http.server' in ll:
                continue
            # 按工作目录关键字匹配 agent
            if 'workspace-pipixia' in ll or 'workspace_pipixia' in ll:
                active.add('pipixia')
            if 'workspace-jiweixia' in ll or 'workspace_jiweixia' in ll:
                active.add('jiweixia')
            if 'workspace-xiaohexia' in ll or 'workspace_xiaohexia' in ll:
                active.add('xiaohexia')
            if 'workspace-yoooclaw' in ll or 'workspace_yoooclaw' in ll:
                active.add('yoooclaw')
            if 'workspace-banjiexia' in ll or 'workspace_banjiexia' in ll:
                active.add('banjiexia')
            if 'workspace-caoxia' in ll or 'workspace_caoxia' in ll:
                active.add('caoxia')
            if 'workspace-duijiaoxia' in ll or 'workspace_duijiaoxia' in ll:
                active.add('duijiaoxia')
            # 项目目录关键字匹配
            if '蒸馏' in line or 'distill' in ll:
                # 蒸馏任务可能属于任何虾，按进程所属区分
                for aid, kw in [('banjiexia', 'workspace-banjiexia'), ('jiweixia', 'workspace-jiweixia')]:
                    if kw in ll:
                        active.add(aid)
            # 按项目路径匹配
            for aid, patterns in {
                'pipixia': ['workspace-pipixia'],
                'jiweixia': ['workspace-jiweixia'],
                'banjiexia': ['workspace-banjiexia'],
                'caoxia': ['workspace-caoxia'],
                'duijiaoxia': ['workspace-duijiaoxia'],
                'xiaohexia': ['workspace-xiaohexia'],
                'yoooclaw': ['workspace-yoooclaw'],
            }.items():
                for p in patterns:
                    if p in ll:
                        active.add(aid)
        return active
    except Exception:
        return set()

def _get_openclaw_cron():
    """获取所有 agent 的 openclaw cron 汇总（并发遍历）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(agent_id):
        try:
            r = subprocess.run(
                ['openclaw', 'cron', 'list', '--json', '--agent', agent_id, '--all'],
                capture_output=True, text=True, timeout=15
            )
            output = r.stdout
            idx = output.find('{')
            if idx < 0:
                return []
            data = json.loads(output[idx:])
            items = data if isinstance(data, list) else data.get('jobs', data.get('crons', []))
            jobs = []
            for item in items:
                sched = item.get('schedule', {})
                state = item.get('state', {})
                expr = sched.get('expr', '')
                human = _cron_expr_to_human(expr)
                name = item.get('name', '')
                jobs.append({
                    'id': item.get('id', ''),
                    'name': name,
                    'agentId': item.get('agentId', agent_id),
                    'agentName': AGENT_NAMES.get(agent_id, agent_id),
                    'purpose': CRON_PURPOSE.get(name, ''),
                    'enabled': item.get('enabled', True),
                    'schedule': expr,
                    'scheduleHuman': human,
                    'tz': sched.get('tz', ''),
                    'lastRunStatus': state.get('lastRunStatus', '?'),
                    'lastRunAtMs': state.get('lastRunAtMs'),
                    'nextRunAtMs': state.get('nextRunAtMs'),
                    'consecutiveErrors': state.get('consecutiveErrors', 0),
                    'lastDurationMs': state.get('lastDurationMs'),
                })
            return jobs
        except Exception:
            return []

    all_jobs = []
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = {pool.submit(_fetch_one, aid): aid for aid in ALL_AGENTS}
        for f in as_completed(futures):
            all_jobs.extend(f.result())

    # 按 agent 顺序排序
    agent_order = {aid: i for i, aid in enumerate(ALL_AGENTS)}
    all_jobs.sort(key=lambda j: (agent_order.get(j.get('agentId', ''), 99), j.get('name', '')))
    return all_jobs

def _cron_expr_to_human(expr):
    """将 cron 表达式转为人类可读描述"""
    if not expr:
        return '-'
    try:
        parts = expr.split()
        if len(parts) != 5:
            return expr
        m, h, dom, mon, dow = parts
        # 处理 */x 格式
        if m.startswith('*/'):
            interval = m[2:]
            return f'每 {interval} 分钟'
        if h.startswith('*/'):
            interval = h[2:]
            return f'每 {interval} 小时'
        # 处理星期
        dow_names = {'0': '日', '1': '周一', '2': '周二', '3': '周三', '4': '周四', '5': '周五', '6': '周六', '7': '日'}
        if dow != '*':
            day_str = '/'.join(dow_names.get(d.strip(), d) for d in dow.split(','))
            return f'{day_str} {h}:{m.zfill(2)}'
        # 处理每月几号
        if dom != '*':
            return f'每月{dom}日 {h}:{m.zfill(2)}'
        # 每天
        return f'每天 {h}:{m.zfill(2)}'
    except:
        return expr

def _cron_to_chinese(schedule):
    """将 cron 表达式转换为中文描述"""
    parts = schedule.split()
    if len(parts) != 5:
        return schedule
    minute, hour, dom, month, dow = parts
    # 特殊值
    if dow != '*':
        days = {'0':'日','1':'一','2':'二','3':'三','4':'四','5':'五','6':'六','7':'日'}
        d = days.get(dow, dow)
        if minute.isdigit() and hour.isdigit():
            return f"每周{d} {int(hour):02d}:{int(minute):02d}"
        return f"每周{d}"
    if dom != '*' or month != '*':
        return schedule  # 不常见，保持原样
    if hour == '*':
        if '/' in minute:
            interval = minute.split('/')[1]
            return f"每{interval}分钟"
        if minute == '0':
            return "每小时整点"
        return f"每小时:{minute}分"
    if '/' in hour:
        interval = hour.split('/')[1]
        return f"每{interval}小时"
    if minute.startswith('*/'):
        interval = minute.split('/')[1]
        return f"每{interval}分钟"
    m = int(minute) if minute.isdigit() else minute
    h = int(hour) if hour.isdigit() else hour
    return f"每天{h:02d}:{m:02d}"

def _get_crontab():
    """获取系统 crontab，合并注释和任务行"""
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        # 第一遍：收集注释和任务
        comments = []
        entries = []
        for line in r.stdout.strip().split('\n'):
            line = line.rstrip()
            if not line:
                continue
            # 跳过环境变量行
            first = line.split(None, 1)[0] if line.split() else ''
            if '=' in first and not line.startswith('#'):
                continue
            if line.startswith('#'):
                comment_text = line[2:].strip()[:80]
                comments.append(comment_text)
                continue
            # 解析 cron 行
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            schedule = ' '.join(parts[:5])
            command = parts[5][:120]
            # 匹配用途和注释
            purpose = ''
            full_cmd = ' '.join(parts[5:]).lower()
            if 'twitter_monitor' in full_cmd:
                if 'elonmusk' in full_cmd or 'realDonald' in full_cmd:
                    purpose = '[量化看板] Twitter KOL 核心账号监控（Musk/Trump）'
                else:
                    purpose = '[量化看板] Twitter KOL 扩展账号监控（Saylor/Vitalik/Armstrong）'
            elif 'econ_monitor' in full_cmd:
                purpose = '[量化看板] 经济日历事件检查'
            elif 'price_collector' in full_cmd:
                purpose = '[量化看板] 加密货币价格采集'
            elif 'health_monitor' in full_cmd:
                purpose = '[量化看板] 信号系统健康监控'
            elif 'signal_tracker' in full_cmd:
                purpose = '[量化看板] 交易信号追踪'
            elif 'health_check.py' in full_cmd:
                purpose = '[虾厂运维] 系统健康巡检（LLM/Gateway/代理/磁盘）'
            elif 'clean_workspace_tmp' in full_cmd:
                purpose = '[虾厂运维] workspace tmp 目录自动清理'
            elif 'econ_result_analyzer' in full_cmd:
                if '13:' in line:
                    purpose = '[量化看板] 经济数据自动分析（美盘 8:30 数据）'
                else:
                    purpose = '[量化看板] 经济数据自动分析（美盘 14:00 数据）'
            elif 'daily_content' in full_cmd or 'daily_content.py' in full_cmd:
                purpose = '[国学运势] 每日运势内容生成'
            elif 'haixing_site_monitor' in full_cmd or ('healthcheck.py' in full_cmd and 'ai影视' in full_cmd):
                purpose = '[AI影视] 网站健康检查（自动检测+重启+告警）'
            elif 'data_collector.py' in full_cmd and 'ai影视' in full_cmd:
                purpose = '[AI影视] 每日数据采集+AI质检（比赛/工具/资讯）'
            elif 'email_monitor' in full_cmd:
                purpose = '[Token看板] 邮件监听（新邮件推送TG）'
            elif 'gateway_restart' in full_cmd:
                purpose = '[虾厂运维] Gateway 定时重启（每天04:00，释放V8堆碎片+session缓存）'
            # 找对应注释
            comment = ''
            for c in comments:
                if any(kw in c.lower() for kw in full_cmd.split()) if len(full_cmd) > 5 else False:
                    comment = c
                    break
            if not comment and comments:
                # 简单匹配：按顺序对应
                pass
            entries.append({
                'schedule': schedule,
                'scheduleHuman': _cron_to_chinese(schedule),
                'command': command,
                'purpose': purpose,
                'comment': comment,
                'enabled': True,
            })
            comments = []  # 用过的注释清空
        return entries
    except Exception as e:
        return [{'error': str(e)}]

def _get_heartbeats():
    """获取各虾的心跳配置"""
    try:
        r = subprocess.run(
            ['openclaw', 'config', 'get', 'agents', '--json'],
            capture_output=True, text=True, timeout=15
        )
        output = r.stdout
        idx = output.find('{')
        if idx < 0 and output.find('[') < 0:
            return []
        start = idx if idx >= 0 else output.find('[')
        data = json.loads(output[start:])

        agents = []
        if isinstance(data, list):
            agents = data
        elif isinstance(data, dict):
            agents = data.get('list', data.get('agents', []))
            if not agents and 'defaults' in data:
                # 可能是 {defaults:..., list:[...]}
                agents = data.get('list', [])

        AGENT_NAMES = {
            'main': '罗氏虾', 'jiweixia': '基围虾', 'caoxia': '草虾',
            'duijiaoxia': '对焦虾', 'pipixia': '皮皮虾', 'xiaohexia': '小河虾',
            'banjiexia': '斑节虾', 'shanbei': '扇贝', 'hailuo': '海螺',
            'haixing': '海星', 'haima': '海马', 'yoooclaw': 'YoooClaw',
        }

        result = []
        for a in agents:
            aid = a.get('id', a.get('agentId', ''))
            hb = a.get('heartbeat', {})
            every = hb.get('every', '0m')
            if not every or every == '0m' or every == '0':
                continue  # 心跳关闭的跳过
            result.append({
                'agentId': aid,
                'agentName': AGENT_NAMES.get(aid, aid),
                'every': every,
                'target': hb.get('target', 'none'),
                'enabled': hb.get('enabled', True) is not False,
            })
        return result
    except Exception as e:
        return [{'error': str(e)}]

def _get_launch_agents():
    """获取 LaunchAgent 服务信息"""
    agents = []
    patterns = [
        os.path.expanduser('~/Library/LaunchAgents/com.openclaw.*.plist'),
        os.path.expanduser('~/Library/LaunchAgents/com.haixing.*.plist'),
        os.path.expanduser('~/Library/LaunchAgents/ai.*.plist'),
        os.path.expanduser('~/Library/LaunchAgents/io.github.clash-verge-rev.*.plist'),
        os.path.expanduser('~/Library/LaunchAgents/netdisk_service.plist'),
    ]
    import glob as _glob
    plist_files = []
    for pat in patterns:
        plist_files.extend(_glob.glob(pat))

    # 获取 launchctl list 用于状态检查
    try:
        lc = subprocess.run(['launchctl', 'list'], capture_output=True, text=True, timeout=5)
        lc_output = lc.stdout
    except:
        lc_output = ''

    for pf in plist_files:
        info = {'plist': os.path.basename(pf)}
        try:
            # 读取 Label
            r = subprocess.run(['/usr/libexec/PlistBuddy', '-c', 'Print :Label', pf],
                               capture_output=True, text=True, timeout=3)
            label = r.stdout.strip()
            info['name'] = label
            info['type'] = 'LaunchAgent'

            # StartInterval
            r = subprocess.run(['/usr/libexec/PlistBuddy', '-c', 'Print :StartInterval', pf],
                               capture_output=True, text=True, timeout=3)
            info['interval'] = int(r.stdout.strip()) if r.returncode == 0 else None

            # RunAtLoad
            r = subprocess.run(['/usr/libexec/PlistBuddy', '-c', 'Print :RunAtLoad', pf],
                               capture_output=True, text=True, timeout=3)
            info['runAtLoad'] = r.stdout.strip().lower() == 'true' if r.returncode == 0 else None

            # KeepAlive
            r = subprocess.run(['/usr/libexec/PlistBuddy', '-c', 'Print :KeepAlive', pf],
                               capture_output=True, text=True, timeout=3)
            info['keepAlive'] = r.stdout.strip().lower() == 'true' if r.returncode == 0 else None

            # 用途
            info['purpose'] = LAUNCH_PURPOSE.get(label, '')

            # 运行方式（替代原始 interval）
            if info.get('keepAlive'):
                info['runMode'] = '常驻守护'
            elif info.get('interval'):
                mins = info['interval'] // 60
                hours = mins // 60
                info['runMode'] = f'定时（每{hours}时{mins%60}分）' if hours > 0 else f'定时（每{mins}分）'
            elif info.get('runAtLoad'):
                info['runMode'] = '开机启动一次'
            else:
                info['runMode'] = '手动'

            # 状态检查
            import re
            match = re.search(rf'^\s*(\d+)\s+\S+\s+{re.escape(label)}$', lc_output, re.MULTILINE)
            if match:
                info['status'] = 'running'
                info['pid'] = match.group(1)
            elif label in lc_output:
                info['status'] = 'loaded'
            else:
                info['status'] = 'stopped'

        except Exception as e:
            info['error'] = str(e)
            info['status'] = 'unknown'
        agents.append(info)
    return agents


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    def _proxy_quota(self, api_url, api_key):
        """代理请求额度 API，返回 JSON 给浏览器"""
        try:
            headers = {'Authorization': 'Bearer ' + api_key}
            # Kimi Coding Plan 需要特殊 User-Agent
            if 'kimi.com/coding' in api_url:
                headers['User-Agent'] = 'KimiCLI/1.6'
            req = urllib.request.Request(api_url, headers=headers)
            if 'z.ai' in api_url or 'deepseek' in api_url or 'minimaxi' in api_url or 'moonshot' in api_url or 'kimi.com' in api_url:
                with _PROXY_OPENER.open(req, timeout=10) as resp:
                    data = json.loads(resp.read())
            else:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def do_OPTIONS(self):
        """处理 CORS preflight"""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_GET(self):
        if self.path == '/providers':
            """返回动态 provider 列表（不含 apiKey）"""
            providers = _get_providers()
            safe = [{'id': p['id'], 'label': p['label'], 'baseUrl': p['baseUrl'],
                     'quotaType': p['quotaType']} for p in providers]
            body = json.dumps(safe).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith('/quota/'):
            """动态额度查询: /quota/<provider_id>"""
            pid = self.path.split('/')[-1]
            providers = _get_providers()
            p = next((x for x in providers if x['id'] == pid), None)
            if not p:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': f'Unknown provider: {pid}'}).encode())
                return
            self._proxy_quota(p['quotaApi'], p['apiKey'])
        elif self.path == '/sessions-json':
            # 使用缓存层，避免每次轮询都 spawn CLI
            d = _get_cached_sessions()
            body = json.dumps(d).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/agent-status':
            """各虾实时工作状态"""
            try:
                # 使用缓存数据（15秒刷新一次）
                d = _get_cached_sessions()
                now = int(time.time() * 1000)

                # === 进程检测：有活跃 exec 进程 = 确定在干活 ===
                active_agents = _detect_active_process_agents()

                # === hasActiveRun 精确信号：Gateway sessions.changed 事件 ===
                # 这是 OpenClaw 内部的 chatAbortControllers 状态，
                # 精确反映"模型正在生成回复"（即 Telegram 显示的 typing 状态）
                # 优先级最高，覆盖模型思考窗口
                # 安全 TTL：如果超过5分钟没有收到 hasActiveRun=False 事件，
                # 认为信号可能丢失，降级到其他检测方式
                active_run_agents = set()
                active_run_stale_ms = 5 * 60 * 1000  # 5分钟安全 TTL
                for aid in list(_agent_active_runs.keys()):
                    last_ts = _agent_ws_activity.get(aid, 0)
                    if now - last_ts < active_run_stale_ms:
                        active_run_agents.add(aid)
                    else:
                        # 超过5分钟没有刷新，清除可能泄漏的标记
                        _agent_active_runs.pop(aid, None)

                # === WS 实时活动检测：Gateway WS 事件更准确 ===
                # _agent_ws_activity 中有最近收到事件的 agentId → last_activity_ms
                # 90秒内的 TTL 覆盖模型思考窗口（30-90秒）
                ws_active_agents = set()
                ws_threshold = 90 * 1000  # 90秒内有事件 = 活跃
                for aid, ts in _agent_ws_activity.items():
                    if now - ts < ws_threshold:
                        ws_active_agents.add(aid)

                agent_info = {}
                for aid in ALL_AGENTS:
                    agent_info[aid] = {'updatedAt': 0, 'model': '-', 'sessionCount': 0, 'subagentCount': 0, 'subagentUpdatedAt': 0, 'subagentSessionIds': []}
                for s in d.get('sessions', []):
                    aid = s.get('agentId', 'unknown')
                    if aid not in agent_info:
                        agent_info[aid] = {'updatedAt': 0, 'model': '-', 'sessionCount': 0, 'subagentCount': 0, 'subagentUpdatedAt': 0, 'subagentSessionIds': []}
                    agent_info[aid]['sessionCount'] += 1
                    key = s.get('key', '')
                    updated = s.get('updatedAt', 0)
                    if ':subagent:' in key:
                        agent_info[aid]['subagentCount'] += 1
                        _sub_sid = s.get('sessionId', '')
                        if _sub_sid:
                            agent_info[aid]['subagentSessionIds'].append(_sub_sid)
                        if updated > agent_info[aid]['subagentUpdatedAt']:
                            agent_info[aid]['subagentUpdatedAt'] = updated
                    if updated > agent_info[aid]['updatedAt']:
                        agent_info[aid]['updatedAt'] = updated
                        agent_info[aid]['model'] = s.get('model', '-')
                        agent_info[aid]['modelProvider'] = s.get('modelProvider', '')
                        agent_info[aid]['sessionId'] = s.get('sessionId', '')
                    # 模型显示基于 direct 主会话（排除 subagent/心跳/slash/system），与推送一致，避免跳变
                    _is_direct = ('telegram:direct' in key or ':direct:' in key) and ':heartbeat' not in key and not key.endswith(':main')
                    if _is_direct and updated > agent_info[aid].get('directUpdatedAt', 0):
                        agent_info[aid]['directUpdatedAt'] = updated
                        agent_info[aid]['directSessionId'] = s.get('sessionId', '')
                        agent_info[aid]['directModel'] = s.get('model', '-')
                        agent_info[aid]['directProvider'] = s.get('modelProvider', '')
                agents = []
                for aid, info in agent_info.items():
                    now_ms = now
                    # 主 session 年龄
                    main_age = now_ms - info['updatedAt'] if info['updatedAt'] else None
                    # 分身最新活动年龄
                    sub_age = now_ms - info['subagentUpdatedAt'] if info.get('subagentUpdatedAt') else None
                    # 取最近的活动作为判断依据
                    effective_age = main_age
                    if sub_age is not None and (effective_age is None or sub_age < effective_age):
                        effective_age = sub_age
                    # 判定优先级：
                    #   1. hasActiveRun=True（Gateway 精确信号）→ working
                    #   2. 有活跃exec进程 → working（强信号）
                    #   3. WS 90秒内有事件 → working（覆盖模型思考窗口）
                    #   4. updatedAt 90秒内 → working（覆盖模型思考窗口）
                    #   5. updatedAt 90秒-10分钟 → waiting
                    #   6. 超过10分钟 → idle
                    # hasActiveRun 是最精确的信号，直接来自 Gateway 内部状态
                    if aid in active_run_agents:
                        status = 'working'
                    elif aid in active_agents:
                        status = 'working'
                    elif aid in ws_active_agents:
                        status = 'working'
                    elif effective_age is None or effective_age > 10 * 60 * 1000:
                        status = 'idle'
                    elif effective_age > 90 * 1000:
                        status = 'waiting'
                    else:
                        status = 'working'
                    # 从 transcript 读真实模型/provider，优先 direct 主会话，fallback 到 session 配置值
                    _sid = info.get('directSessionId', '') or info.get('sessionId', '')
                    # 从 transcript 读最近 N 条实际模型分布（主会话 + subagent 合并统计）
                    _sub_sids = sorted(set(info.get('subagentSessionIds', [])))  # 去重防分身双倍计数
                    _mdist = _recent_model_dist(_sid, aid, extra_session_ids=_sub_sids)
                    _am = _mdist['last']['model']
                    _ap = _mdist['last']['provider']
                    _model = _am or info.get('directModel') or info.get('model', '-')
                    _provider = _ap if _am else (info.get('directProvider') or info.get('modelProvider', ''))
                    agents.append({
                        'agentId': aid,
                        'agentName': AGENT_NAMES.get(aid, aid),
                        'status': status,
                        'ageMs': effective_age,
                        'model': _model,
                        'modelProvider': _provider,
                        'modelDist': _mdist['dist'],      # 最近 N 条分布 [{model,provider,count}...]
                        'modelLast': _mdist['last'],      # 最后一条 {model,provider,ts}
                        'sessionCount': info['sessionCount'],
                        'subagentCount': info['subagentCount'],
                    })
                agent_order = {aid: i for i, aid in enumerate(ALL_AGENTS)}
                agents.sort(key=lambda a: agent_order.get(a['agentId'], 99))
                body = json.dumps({'agents': agents, 'now': now, 'wsConnected': _ws_connected, 'activeRunAgents': list(_agent_active_runs.keys()), 'gatewayHealth': _gateway_health()}).encode()
            except Exception as e:
                body = json.dumps({'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/cron-json':
            data = {
                'openclawCron': _get_openclaw_cron(),
                'crontab': _get_crontab(),
                'launchAgents': _get_launch_agents(),
                'heartbeats': _get_heartbeats(),
                'fetchedAt': int(time.time() * 1000),
            }
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/cron':
            self.path = '/cron.html'
            super().do_GET()
        elif self.path == '/email-status':
            """邮件监听状态（由 email_monitor.py 定时写入 email_status.json）"""
            try:
                status_file = os.path.join(os.path.dirname(DIR), 'projects', 'token-dashboard', 'data', 'email_status.json')
                # email_status.json 实际在 token-dashboard 项目 data 目录下
                _td_dir = os.path.join(os.path.expanduser('~'), '.openclaw/workspace/ai_workspace/projects/token-dashboard')
                status_file = os.path.join(_td_dir, 'data', 'email_status.json')
                if os.path.exists(status_file):
                    with open(status_file) as f:
                        data = json.load(f)
                else:
                    data = {'unread_count': 0, 'new_count': 0, 'last_check': None, 'recent_new': [], 'note': '尚未运行'}
                body = json.dumps(data).encode()
            except Exception as e:
                body = json.dumps({'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/health-json':
            data = _get_system_health()
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        else:
            if self.path == '/' or self.path == '/token_dashboard.html':
                # 防止浏览器缓存旧版 HTML（导致 JS 不更新）
                html_path = os.path.join(DIR, 'token_dashboard.html')
                try:
                    with open(html_path, 'rb') as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                    self.send_header('Pragma', 'no-cache')
                    self.send_header('Expires', '0')
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode())
            else:
                super().do_GET()

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    # 启动 Gateway WebSocket 监听线程（实时 agent 活动状态）
    _ws_thread = threading.Thread(target=_ws_listener_loop, daemon=True, name='ws-listener')
    _ws_thread.start()

    # 内存泄漏防护（应用层，plist 硬限可能未生效）
    _mem_thread = threading.Thread(target=_memory_watchdog_loop, daemon=True, name='mem-watchdog')
    _mem_thread.start()

    httpd = http.server.HTTPServer(('0.0.0.0', 18888), H)
    httpd.serve_forever()
