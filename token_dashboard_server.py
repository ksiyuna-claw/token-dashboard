#!/usr/bin/env python3
"""虾厂 Token 看板 HTTP 服务"""
import http.server, json, subprocess, os, time, urllib.request, urllib.error, threading, uuid, logging, logging.handlers, gc, resource, functools, plistlib, hmac, hashlib, base64, sqlite3  # B7(2026-09-11): sqlite3提模块级，原函数内重复import在循环调用下纯浪费

# ── Auth 配置（JWT 无状态认证）─────────
_AUTH_ENV_PATH = os.path.join(os.path.expanduser('~'), '.openclaw/workspace/ai_workspace/projects/2026-06-04-量化信息看板/.env')
_AUTH_USER = 'kuangsiyu'
_AUTH_PASS = ''
_SESSION_MAX_AGE = 30 * 24 * 3600  # 30天（秒）
_JWT_SECRET = None  # JWT签名密钥，延迟初始化
_login_attempts = {}  # ip -> [timestamps] 登录频率限制
_login_attempts_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 5  # 60秒内最多5次
_LOGIN_WINDOW = 60  # 60秒窗口

# ── 通用缓存装饰器（P0 优化）──────────────────────────
def cached(ttl_seconds):
    """通用缓存装饰器，线程安全。被装饰函数返回的数据缓存ttl_seconds秒。"""
    _store = {}
    _lock = threading.Lock()
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = str(args) + str(sorted(kwargs.items()))
            now = time.time()
            with _lock:
                c = _store.get(key)
                if c and now - c[0] < ttl_seconds:
                    return c[1]
            result = fn(*args, **kwargs)
            with _lock:
                _store[key] = (now, result)
            return result
        return wrapper
    return decorator

# ── CORS 白名单（P1 安全）──────────────────────────────
_ALLOWED_ORIGINS = {'http://127.0.0.1:18888', 'https://token-dashboard.crypto-signal.work'}
def _cors_origin(handler):
    origin = handler.headers.get('Origin', '')
    return origin if origin in _ALLOWED_ORIGINS else 'http://127.0.0.1:18888'

# ── Quota 缓存（P0，超时fallback用）────────────────────
_quota_cache = {}  # {provider_id: (timestamp, data)}
_quota_cache_lock = threading.Lock()
_QUOTA_CACHE_TTL = 60  # 60秒

def _load_auth_password():
    """从量化看板 .env 读取 DASHBOARD_PASSWORD"""
    global _AUTH_PASS
    try:
        with open(_AUTH_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith('DASHBOARD_PASSWORD='):
                    _AUTH_PASS = line.split('=', 1)[1].strip()
                    break
    except Exception as e:
        logging.warning(f'[AUTH] 读取密码失败: {e}')

_load_auth_password()

# ── JWT 无状态认证工具函数 ────────────────────────────
def _get_jwt_secret():
    """延迟初始化JWT密钥，基于认证密码派生"""
    global _JWT_SECRET
    if _JWT_SECRET is None:
        if _AUTH_PASS:
            # R2（issue-0235第2轮）：PBKDF2派生独立JWT密钥，不再用登录密码原文。副作用：密钥变更后现存JWT全部失效，用户需重新登录一次
            _JWT_SECRET = hashlib.pbkdf2_hmac('sha256', _AUTH_PASS.encode('utf-8'), b'token-dashboard-jwt', 100000)
        else:
            _JWT_SECRET = b'token-dashboard-fallback-secret'
    return _JWT_SECRET

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return base64.urlsafe_b64decode(s)

def _jwt_create(user: str, expires_in: int = 30 * 24 * 3600) -> str:
    """生成JWT token"""
    header = _b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    payload_dict = {'user': user, 'exp': int(time.time()) + expires_in}
    payload = _b64url_encode(json.dumps(payload_dict).encode('utf-8'))
    signing_input = f'{header}.{payload}'.encode('utf-8')
    signature = hmac.new(_get_jwt_secret(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)
    return f'{header}.{payload}.{sig_b64}'

def _jwt_verify(token: str) -> bool:
    """验证JWT token：签名正确 + 未过期。返回True表示有效。"""
    if not token or token.count('.') != 2:
        return False
    try:
        header, payload, sig = token.split('.')
        signing_input = f'{header}.{payload}'.encode('utf-8')
        expected_sig = hmac.new(_get_jwt_secret(), signing_input, hashlib.sha256).digest()
        provided_sig = _b64url_decode(sig)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return False
        payload_dict = json.loads(_b64url_decode(payload))
        if payload_dict.get('exp', 0) <= time.time():
            return False
        return True
    except Exception:
        return False

def _check_login_rate_limit(client_ip):
    """检查登录频率，返回True表示允许尝试"""
    now = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts.get(client_ip, [])
        # 清理60秒外的记录
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            _login_attempts[client_ip] = attempts
            return False
        attempts.append(now)
        _login_attempts[client_ip] = attempts
        return True

def _create_session(user):
    """创建JWT token（无状态，服务器不需要存储）"""
    return _jwt_create(user, _SESSION_MAX_AGE)

def _check_session(handler):
    """检查Cookie中的JWT token，返回True表示已登录"""
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return False
    for part in cookie_header.split(';'):
        part = part.strip()
        if part.startswith('td_session='):
            token = part[len('td_session='):]
            return _jwt_verify(token)
    return False

# Basic Auth 已移除（用户反馈体验差），统一用 Cookie Session Auth

_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🦐 Token 看板登录</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;display:flex;justify-content:center;align-items:center;min-height:100vh}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;width:320px}
.card h1{font-size:20px;margin-bottom:4px}
.card .sub{font-size:13px;color:#8b949e;margin-bottom:24px}
.field{margin-bottom:16px}
.field label{display:block;font-size:13px;color:#8b949e;margin-bottom:6px}
.field input{width:100%;padding:10px 12px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;font-size:14px}
.field input:focus{outline:none;border-color:#58a6ff}
.btn{width:100%;padding:10px;background:#238636;border:1px solid #238636;border-radius:6px;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{background:#2ea043}
.err{color:#f85149;font-size:13px;margin-top:12px;text-align:center}
.remember{margin-bottom:16px}
.remember label{display:flex;align-items:center;gap:6px;font-size:13px;color:#8b949e;cursor:pointer}
.remember input{width:auto}
</style></head>
<body><div class="card">
<h1>🦐 Token 看板</h1>
<div class="sub">请登录</div>
<form method="POST" action="/login">
<div class="field"><label>用户名</label><input type="text" name="username" autofocus></div>
<div class="field"><label>密码</label><input type="password" name="password"></div>
<div class="remember"><label><input type="checkbox" name="remember" value="1" checked> 记住我（30天免登录）</label></div>
<button type="submit" class="btn">登录</button>
</form>
</div></body></html>'''

def _send_login_page(handler):
    """发送登录页面"""
    body = _LOGIN_HTML.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', len(body))
    handler.end_headers()
    handler.wfile.write(body)

def _send_auth_challenge(handler):
    """未登录时跳转到登录页"""
    handler.send_response(302)
    handler.send_header('Location', '/login')
    handler.send_header('Content-Type', 'text/plain')
    handler.end_headers()


# 🔴 内存泄漏防护 (2026-06-24): 50分钟 RSS 2GB，LRU 没拦住，加硬防线
_MEM_WATCH_INTERVAL = 60  # 秒
_MEM_GC_THRESHOLD = 400 * 1024 * 1024   # 400MB 触发 gc.collect()
_MEM_KILL_THRESHOLD = 800 * 1024 * 1024  # 800MB 自杀（launchd 会拉新进程）

def _memory_watchdog_loop():
    """后台线程：定期检查 RSS，超阈值则 gc 或自杀
    RSS 硬限 1GB 在 plist 里设了但 launchd 可能未生效（kickstart 没重读 plist），
    所以这里做应用层防线。
    Y9(2026-09-11)：改用 ps 当前 RSS（复用 _ps_snapshot 共享快照），替代 ru_maxrss。
    原实现用 ru_maxrss（进程生命周期历史峰值）当当前值：峰值冲高后读数永不下降，
    >400MB 分支每次 gc 后照样报高读数刷无效 warning；一旦历史峰值超 800MB，
    看门狗会每 60s 自杀一次（launchd 拉新进程后才恢复）。"""
    while True:
        time.sleep(_MEM_WATCH_INTERVAL)
        try:
            rss = None
            _mypid = os.getpid()
            for pid, _ppid, _et, rss_kb, _cmd in _ps_snapshot():
                if pid == _mypid:
                    rss = rss_kb * 1024  # ps rss 单位 KB → bytes（与原 ru_maxrss macOS 单位一致）
                    break
            if rss is None:
                # ps 快照异常时保守回退 ru_maxrss（历史峰值，偏高不偏低，安全侧）
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if os.uname().sysname != 'Darwin':
                    rss *= 1024  # Linux 单位是 KB
            if rss > _MEM_KILL_THRESHOLD:
                logging.error(f'[MEM] RSS {rss//1024//1024}MB > {_MEM_KILL_THRESHOLD//1024//1024}MB, 自杀让 launchd 拉新进程')
                os._exit(1)
            elif rss > _MEM_GC_THRESHOLD:
                before = rss
                gc.collect()
                rss2 = None
                for pid, _ppid, _et, rss_kb, _cmd in _ps_snapshot():
                    if pid == os.getpid():
                        rss2 = rss_kb * 1024
                        break
                logging.warning(f'[MEM] GC: RSS {before//1024//1024}MB → {(rss2 or before)//1024//1024}MB (当前值口径)')
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

# WS 错误风暴防护：60 秒内同类型错误只报一次（B6 2026-09-11：按错误类型分桶限流，
# 原全局单时间戳会让不同类型错误互相抑制，风暴时只有第一条可查）
_last_ws_error_at = {}  # {error_type: last_log_ts}
def _log_ws_error(e):
    _k = type(e).__name__
    now = time.time()
    if now - _last_ws_error_at.get(_k, 0) < 60:
        return
    _last_ws_error_at[_k] = now
    logging.warning(f'[WS] error[{_k}] (rate-limited 60s/bucket): {type(e).__name__}: {str(e)[:200]}')

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
# Y3(2026-09-11)：改为 sessionKey 维度——按 agentId 存/删会把同 agent 并发 session 的
# 标记互相覆盖/误清（主会话 run 结束 pop 掉还在跑的 subagent/cron 标记），消费端聚合回 agentId
_agent_active_runs = {}  # {sessionKey: True}
_ws_thread = None
_ws_connected = False

# ── Provider 额度 API 映射 ──────────────────────────────
# baseUrl 模式匹配 → (额度API URL, 类型)
# type: 'zhipu_quota' = 智谱额度API, 'deepseek_balance' = DeepSeek余额API
PROVIDER_QUOTA_API = {
    'open.bigmodel.cn': ('https://open.bigmodel.cn/api/monitor/usage/quota/limit', 'zhipu_quota'),
    'api.z.ai': ('https://api.z.ai/api/monitor/usage/quota/limit', 'zhipu_quota'),
    'api.deepseek.com': ('https://api.deepseek.com/user/balance', 'deepseek_balance'),
    # 2026-07-23 MiniMax套餐到期下线，代码保留备用
    # 'api.minimaxi.com': ('https://api.minimaxi.com/v1/token_plan/remains', 'minimax_quota'),
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
    # 2026-07-23 MiniMax套餐到期下线，代码保留备用
    # 'minimax': 'MiniMax（编程套餐）',
    'kimi': 'Kimi（Moonshot）',
}

# ── 看板隐藏的 provider（2026-08-14 匡书记批准：方案A改良版）──────────────
# zai（智谱海外）：Coding Plan 已于 2026-08-07 到期未续费。2026-08-14 基围虾用
# `openclaw onboard --auth-choice zai-coding-cn` 重写 openclaw.json 的 zai provider，
# 实际挂的是国内端点 open.bigmodel.cn + 国内 Key（与 zhipu 完全同 Key 同端点同额度接口）。
# openclaw.json 的 zai 是全厂 default 模型 zai/glm-5.2 的路由依赖，绝不能删；
# 因此在看板侧隐藏：zai 不进 /providers 响应 → 前端无卡片、服务端不为其发起
# 任何额度查询/缓存（省掉与 zhipu 卡完全重复的额度 API 调用，纯浪费带宽）。
# 🔄 恢复方法：从下面集合中移除 'zai' 即可（代码保留以防万一，
#    PROVIDER_QUOTA_API / PROVIDER_LABELS 的 zai 条目均未动）。
HIDDEN_PROVIDERS = {'zai'}

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
    """读单个 session transcript 的 assistant 消息，返回 (seq, stale)：seq = [(model, provider, ts), ...]
    优先读 agent sqlite（beta.3+），fallback 读旧 .jsonl 文件。
    Y4(2026-09-11)：返回值增加 stale 标志——sqlite 读失败或回退旧 jsonl（8.2 后只剩 .bak/.reset
    远古快照）时 stale=True，调用方可标记「数据滞后」，不再无痕返回数周前的模型数据。"""
    seq = []
    stale = False
    # --- Phase 1: 读 sqlite transcript_events（beta.3+） ---
    db_path = os.path.join(AGENTS_DIR, agent_id, 'agent', 'openclaw-agent.sqlite')
    try:
        if os.path.isfile(db_path):
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            try:
                cur = conn.execute(
                    'SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq DESC LIMIT 30',
                    (session_id,)
                )
                rows = list(cur)
                rows.reverse()  # 恢复时间正序
                for (event_json,) in rows:
                    try:
                        obj = json.loads(event_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    msg = obj.get('message', {})
                    if msg.get('role') == 'assistant' and msg.get('model'):
                        m = msg['model']
                        if m in ('delivery-mirror', 'gateway', 'gateway-injected'):
                            continue
                        seq.append((m, msg.get('provider', ''), _parse_msg_ts(obj, msg)))
            finally:
                conn.close()
            if seq:
                return seq, False  # sqlite 有数据，直接返回
    except Exception as e:
        # Y4：不再静默回退——sqlite 读失败（如 db 短暂锁住）时必须留日志，否则排查无入口
        logging.warning(f'[TRANSCRIPT] sqlite读失败回退jsonl ({agent_id}/{str(session_id)[:8]}): {type(e).__name__}: {e}')
        stale = True
    # --- Phase 2: fallback 读旧 .jsonl 文件（兼容旧数据） ---
    tdir = os.path.join(AGENTS_DIR, agent_id, 'sessions')
    try:
        if os.path.isdir(tdir):
            primary, archive, bak = [], [], []
            for fn in os.listdir(tdir):
                if fn.startswith(session_id) and fn.endswith('.jsonl') and 'trajectory' not in fn:
                    primary.append(os.path.join(tdir, fn))
                elif fn.startswith(session_id) and '.jsonl.reset.' in fn:
                    archive.append(os.path.join(tdir, fn))
                elif fn.startswith(session_id) and '.jsonl.bak-' in fn:
                    bak.append(os.path.join(tdir, fn))
            primary.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            archive.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            bak.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            candidates = primary or archive or bak
            if candidates:
                # Y4：命中归档/bak 文件 = 数据可能是远古快照，留日志 + 标 stale
                if candidates[0] not in primary:
                    logging.warning(f'[TRANSCRIPT] 回退旧jsonl快照 ({agent_id}/{str(session_id)[:8]}): {os.path.basename(candidates[0])} 数据可能滞后')
                stale = True
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
                            if m in ('delivery-mirror', 'gateway', 'gateway-injected'):
                                continue
                            seq.append((m, msg.get('provider', ''), _parse_msg_ts(obj, msg)))
    except Exception:
        pass
    return seq, stale

def _cron_job_recent_model(job_id, agent_id):
    """cron job 最近一次运行的实际模型：从最新 run session（agent:{aid}:cron:{jobId}:run:%）
    的 transcript 提取（session 元数据的 model 是配置值，不可信）；无 run 记录时 fallback 父 session。
    独立 TTL 缓存（复用 _dist_cache_lock/_dist_cache，key 前缀区分）。返回模型字符串或 None。"""
    if not job_id or not agent_id:
        return None
    cache_key = 'cronmodel|' + agent_id + '|' + job_id
    now = time.time()
    with _dist_cache_lock:
        c = _dist_cache.get(cache_key)
        if c and now - c[0] < _DIST_CACHE_TTL:
            return c[1]
    db_path = os.path.join(AGENTS_DIR, agent_id, 'agent', 'openclaw-agent.sqlite')
    sid = None
    win_model = None
    try:
        if os.path.isfile(db_path):
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            try:
                # R2修复(2026-09-11)：8.2 升级删掉 sessions 表，旧查询必抛 no such table 被
                # 裸 except 吞掉 → lastModel 恒 None，模型列自 8.2 起一直显示配置值。换
                # session_windows 表。B1优化：直接取该表自带的 model 列（实测有真值，如
                # glm-5.3-flash/k3），省掉每 job 二次查 transcript_events；model 为空时才
                # 回退老路径（transcript_events 提取），兼容未记录 model 的旧窗口。
                cur = conn.execute(
                    'SELECT session_id, model FROM session_windows WHERE session_key LIKE ? ORDER BY updated_at DESC LIMIT 1',
                    (f'agent:{agent_id}:cron:{job_id}:run:%',))
                row = cur.fetchone()
                if not row:
                    # 从未跑过（或旧版在父 session 跑）：fallback 父 session
                    cur = conn.execute(
                        'SELECT session_id, model FROM session_windows WHERE session_key = ? LIMIT 1',
                        (f'agent:{agent_id}:cron:{job_id}',))
                    row = cur.fetchone()
                if row:
                    sid = row[0]
                    win_model = row[1] or None
            finally:
                conn.close()
    except Exception as e:
        # R2修复：失败不再静默——原裸 except 让「打开 sqlite+必败查询」每 60s 对每个 job 重复全套动作
        logging.warning(f'[CRONMODEL] sqlite查模型失败 ({agent_id}/{job_id[:8]}): {type(e).__name__}: {e}')
        return None
    if win_model:
        model = win_model  # B1：session_windows.model 列直接可用（实测真值），省二次查 transcript
    else:
        model = None
        if sid:
            # model 列为空（旧窗口未记录 model）：回退读 transcript_events 提取最后一条实际调用
            seq, _stale = _read_session_transcript(sid, agent_id)
            if seq:
                model = seq[-1][0]  # transcript 按时间正序，最后一条即最新实际调用
    with _dist_cache_lock:
        if len(_dist_cache) >= _DIST_CACHE_MAX:
            sorted_keys = sorted(_dist_cache.keys(), key=lambda k: _dist_cache[k][0])
            for k in sorted_keys[:16]:
                _dist_cache.pop(k, None)
        _dist_cache[cache_key] = (now, model)
    return model


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
    # B5(2026-09-11)：缓存 key 从「全量 subagent id 拼接」降维为「数量+集合hash」——
    # subagent 集合每变一个 id 即 miss，最坏每 10s 每 agent 开 N 个 sqlite 连接；
    # 降维后内容相同（不管 id 顺序/成员怎么变）只要集合一致就命中。
    subs = sorted(set(extra_session_ids or []))
    subs_h = hashlib.md5(('|'.join(subs)).encode()).hexdigest()[:12] if subs else ''
    cache_key = session_id + f'|s{len(subs)}:{subs_h}'
    with _dist_cache_lock:
        c = _dist_cache.get(cache_key)
        if c and now - c[0] < _DIST_CACHE_TTL:
            return c[1]
    # 读主 session transcript
    seq, seq_stale = _read_session_transcript(session_id, agent_id)
    # 合并 subagent session transcripts（把分身的模型调用纳入统计）
    if subs:
        for sid in subs:
            if sid and sid != session_id:
                _s, _st = _read_session_transcript(sid, agent_id)
                seq.extend(_s)
                seq_stale = seq_stale or _st
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
            'stale': seq_stale,  # Y4：数据来自旧 jsonl 回退时标滞后
        }
    else:
        payload['stale'] = seq_stale
    _dist_cache_put(cache_key, payload, now)
    return payload

def _is_gateway_cmd(cmd):
    """Gateway 进程命令行识别统一口径（Y7 2026-09-11）：
    必须含 openclaw/dist/index.js 且 gateway 为独立词。
    实测生产命令行为 `node .../openclaw/dist/index.js gateway --port 18789`。
    原 _find_gateway_pids 用无尾空格的 ' gateway' 子串匹配，会误匹配命令行里
    恰好引用了这两个字符串的巡检/grep 进程（审查实测误配），收紧到词级匹配。"""
    return 'openclaw/dist/index.js' in cmd and (' gateway ' in cmd or cmd.rstrip().endswith(' gateway'))

def _gateway_health():
    """读 Gateway 进程 RSS + uptime + 下次 04:00 重启倒计时
    B2(2026-09-11)：复用 _ps_snapshot 共享快照（5s TTL），替代独立跑 ps——
    原每 15s/客户端轮询各自 spawn 一次 ps 纯浪费；识别口径统一走 _is_gateway_cmd。"""
    import datetime
    info = {'rss': None, 'rssMB': None, 'uptimeHours': None, 'nextRestartIn': None, 'pid': None}
    try:
        for pid, _ppid, et_sec, rss_kb, cmd in _ps_snapshot():
            if _is_gateway_cmd(cmd) and 'grep' not in cmd:
                info['pid'] = pid
                info['rss'] = rss_kb
                info['rssMB'] = round(rss_kb / 1024, 0)
                if et_sec is not None:
                    info['uptimeHours'] = round(et_sec / 3600, 1)
                break
    except Exception:
        pass
    # 计算下次 04:00 重启倒计时
    try:
        now_dt = time.localtime()
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

def _hermes_health():
    """Hermes 网关健康监测（2026-08-18 匡书记批准新增）
    监测三层维持措施：进程存活+uptime、gateway.heartbeat 心跳年龄、watchdog 动作日志
    """
    import subprocess as _sp
    hermes_home = os.path.join(os.path.expanduser('~'), '.hermes')
    info = {
        'running': False, 'pid': None, 'uptimeHours': None,
        'hbAgeSec': None, 'hbFresh': False,
        'watchdogLast': None, 'watchdogTriggered': False,
        'note': '',
    }
    # 1. 进程存活 + uptime
    try:
        out = _sp.check_output(['ps', '-A', '-o', 'pid,etime,command'], text=True)
        for line in out.splitlines():
            if 'hermes_cli.main' in line and ' gateway' in line and 'grep' not in line:
                info['running'] = True
                parts = line.split()
                info['pid'] = int(parts[0])
                et = parts[1]
                if '-' in et:
                    d, hms = et.split('-', 1)
                    h, m, s = hms.split(':')
                    info['uptimeHours'] = round(int(d) * 24 + int(h) + int(m) / 60, 1)
                else:
                    p2 = et.split(':')
                    if len(p2) == 3:
                        # HH:MM:SS（超1小时场景，如 30:30:00 = 30.5h）
                        info['uptimeHours'] = round(int(p2[0]) + int(p2[1]) / 60, 1)
                    elif len(p2) == 2:
                        # MM:SS（不足1小时）
                        info['uptimeHours'] = round(int(p2[0]) / 60 + int(p2[1]) / 3600, 2)
                break
    except Exception:
        pass
    # 2. 心跳文件年龄（正常 30s 刷新一次；>600s = 假死信号，与 watchdog STALE_SECS 一致）
    try:
        hb_file = os.path.join(hermes_home, 'state', 'gateway.heartbeat')
        if os.path.exists(hb_file):
            age = time.time() - os.path.getmtime(hb_file)
            info['hbAgeSec'] = int(age)
            info['hbFresh'] = age <= 600
        else:
            info['note'] = '心跳文件不存在'
    except Exception:
        pass
    # 3. watchdog 最近一次动作（日志只在触发重启时写）
    try:
        wd_log = '/tmp/hermes-watchdog.log'
        if os.path.exists(wd_log):
            with open(wd_log) as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                info['watchdogLast'] = lines[-1]
                info['watchdogTriggered'] = True
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
            # 隐藏的 provider：不展示卡片、不发额度查询（原因见 HIDDEN_PROVIDERS 注释）
            if pid in HIDDEN_PROVIDERS:
                continue
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
# 2026-08-27 全量对齐 sqlite 实际 job 名（旧键名与现名不符致总览页用途列大面积"—"，匡书记报修）
CRON_PURPOSE = {
    'Git自动备份': '运维 · 虾厂Git自动备份（每天06:28，flash灰度中）',
    '凌晨巡检(06:00)': '运维 · 基围虾清晨系统巡检（quiet只报紧急异常）',
    '晚间巡检(21:15)': '运维 · 基围虾晚间综合巡检（当日全量汇报）',
    '看脸实验室-每日内容（皮皮虾）': '内容 · 看脸实验室每日内容生产（kimi/k3）',
    '看脸实验室-每周选题补充（皮皮虾）': '内容 · 看脸实验室每周选题池补充',
    '颜姐-每日内容卡片（皮皮虾）': '内容 · 颜姐日推卡片生成（v3.1换马kimi/k3）',
    '颜姐-周日复盘+选题（皮皮虾）': '内容 · 颜姐周日复盘与选题规划',
    '颜姐-周日回流（皮皮虾）': '内容 · 颜姐账号周批回流（8-28拍板周批模式，周日07:30）',
    '颜姐-每日快照（皮皮虾）': '数据 · 颜姐账号日频快照（停用待命，未来或恢复日频，issue-0157已收口）',
    '聚萤-月初财税提醒': '财税 · 聚萤月初财税提醒（每月1日）',
    '[手动停用] Token用量推送': '运维 · Token用量TG推送（历史停用，看板已替代）',
    '[手动停用] 每周前沿Agent研究扫描': 'AI研究 · Agent前沿周扫（历史停用，心跳方向3覆盖）',
    '[手动停用] AI视频行业日报(周二)': '内容 · AI视频行业周报（历史停用）',
    '[手动停用] shanbei-daily-quant-lesson': '量化看板 · 扇贝每日量化课（历史停用）',
}

def _cron_purpose_fill(name, enabled=True):
    """动态用途补全（2026-09-07 方案①）：静态字典 miss 时按 job 名前缀规则生成。
    背景：CRON_PURPOSE 静态字典在 job 新增/改名后无人同步会大面积 miss（08-27 教训），
    规则型 job（heartbeat-*/skill-collection-review-*）改用前缀规则动态生成，零维护。"""
    if name.startswith('skill-collection-review-'):
        return '系统自动 · Skill Workshop每周收藏集检查（轻量秒级，几乎零token）'
    if name.startswith('heartbeat-'):
        aid = name[len('heartbeat-'):]
        aname = AGENT_NAMES.get(aid, aid)
        if name == 'heartbeat-jiweixia':
            base = '✅ iso模式（09-07启用）：隔离会话+轻上下文，token预计降90%+'
            return base if enabled else base + ' · 已禁用'
        return f'心跳monitor job（{aname}）· {"启用" if enabled else "已禁用"}'
    if 'secrets注入探针' in name:
        return '安全 · secrets注入探针（issue-0226 历史验证用，已停用）'
    return ''

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
    # 保留结构供将来扩展，/cron 端点通过 _crontab_purpose() 的 if/elif 匹配
}

def _crontab_purpose(full_cmd, line=''):
    """crontab 命令关键词 → 用途映射（2026-09-07 从 _get_crontab 抽取：活跃行与停用注释行共用一套）"""
    import re as _re_purpose
    if 'twitter_monitor' in full_cmd:
        if 'elonmusk' in full_cmd or 'realdonald' in full_cmd:
            return '[量化看板] Twitter KOL 核心账号监控（Musk/Trump）'
        else:
            return '[量化看板] Twitter KOL 扩展账号监控（Saylor/Vitalik/Armstrong）'
    elif 'econ_monitor' in full_cmd:
        return '[量化看板] 经济日历事件检查'
    elif 'price_collector' in full_cmd:
        return '[量化看板] 加密货币价格采集'
    elif 'health_monitor' in full_cmd:
        return '[量化看板] 信号系统健康监控'
    elif 'signal_tracker' in full_cmd:
        return '[量化看板] 交易信号追踪'
    elif 'health_check.py' in full_cmd:
        return '[虾厂运维] 系统健康巡检（LLM/Gateway/代理/磁盘）'
    elif 'cron_delivery_snapshot' in full_cmd:
        return '[虾厂运维] cron投递状态快照（7天滚动日志）'
    elif 'tg_deadletter_resend' in full_cmd:
        return '[虾厂运维] TG死信队列自动重发（每分钟）'
    elif 'clean_workspace_tmp' in full_cmd:
        return '[虾厂运维] workspace tmp 目录自动清理'
    elif 'econ_result_analyzer' in full_cmd:
        # 按调度小时字段区分：13点跑=美盘8:30数据，18点跑=美盘14:00数据（修原 '13:' 死匹配）
        m_hour = _re_purpose.search(r'\s\d{1,2}\s+(\d{1,2})\s+\*\s+\*', line)
        if m_hour and m_hour.group(1) == '13':
            return '[量化看板] 经济数据自动分析（美盘 8:30 数据）'
        else:
            return '[量化看板] 经济数据自动分析（美盘 14:00 数据）'
    elif 'daily_content' in full_cmd or 'daily_content.py' in full_cmd:
        return '[国学运势] 每日运势内容生成'
    elif 'haixing_site_monitor' in full_cmd or ('healthcheck.py' in full_cmd and 'ai影视' in full_cmd):
        return '[AI影视] 网站健康检查（自动检测+重启+告警）'
    elif 'data_collector.py' in full_cmd and 'ai影视' in full_cmd:
        return '[AI影视] 每日数据采集+AI质检（比赛/工具/资讯）'
    elif 'email_monitor' in full_cmd:
        return '[Token看板] 邮件监听（新邮件推送TG）'
    elif 'gateway_restart' in full_cmd:
        return '[虾厂运维] Gateway 定时重启（每天04:00，释放V8堆碎片+session缓存）'
    elif 'cloud_monitor' in full_cmd:
        return '[聚光萤] 云端服务监控（每30分钟，异常时TG告警）'
    elif 'backup.sh' in full_cmd or 'zhitai' in full_cmd:
        return '[虾厂运维] 智泰硬盘全量备份（每天06:40，成功/失败TG推送）'
    elif 'gateway_log_rotate' in full_cmd:
        return '[虾厂运维] Gateway 日志轮转（每小时25分，防日志膨胀）'
    elif 'du_snapshot' in full_cmd:
        return '[虾厂运维] 全厂du磁盘快照（每天07:30，存储卫生治理M3，2026-08-30上线）'
    return ''

@cached(30)
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
    'ai.hermes.watchdog': 'AI基础设施 · Hermes 看门狗（每6h自愈：进程存活+心跳年龄检查）',
    'ai.hermes.cleanup-locks': 'AI基础设施 · Hermes 开机锁文件清理（防 PID 复用导致误锁）',
    'com.openclaw.token-dashboard': '运维 · Token看板 HTTP服务 (18888)',
    'com.openclaw.cloudflared': '运维 · Cloudflare Tunnel 外网穿透（token-dashboard.crypto-signal.work / quant-dashboard）',
    'com.openclaw.dashboard': '量化看板 · 看板 HTTP服务',
    'com.openclaw.guoxue-bot': '国学运势 · Telegram Bot 服务',
    'com.openclaw.ai-radar': '量化看板 · AI雷达信号服务',
    'com.openclaw.kline-radar': '量化看板 · K线雷达服务（行情信号检测）',
    'com.openclaw.onchain-dashboard': '量化看板 · 链上数据看板服务',
    'com.openclaw.joke-detector': 'AI实验 · 笑力检测仪服务（智谱API检测幽默内容）',
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
            # Y8(2026-09-11)：补存 updatedAt——_trim_snapshot_data 按 updatedAt 排序裁剪，
            # 原本 6 字段不存 → 超过 500 条时排序恒为 0，裁剪退化成按插入序，可能裁掉活跃
            # session 保留死 session（当前量级未触发，定时炸弹拆除）
            new_snap_sessions[key] = {
                'inputTokens': s.get('inputTokens', 0),
                'outputTokens': s.get('outputTokens', 0),
                'totalTokens': s.get('totalTokens', 0),
                'model': s.get('model', '-'),
                'modelProvider': s.get('modelProvider', '-'),
                'contextTokens': s.get('contextTokens', 200000),
                'updatedAt': s.get('updatedAt', 0),
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


_sqlite_warn_ts = {}  # {agentId: last_warn_ts} R1：sqlite 覆盖失败日志 per-agent 限流 5min

def _refresh_updated_at_from_sqlite(data):
    """issue-0183 修复：用 agent sqlite 的 session_windows 时间戳覆盖 CLI 的滞后 updatedAt。

    根因（2026-08-15 实测验证）：
    - `openclaw sessions --json` 对 subagent session 返回的 updatedAt ≈ spawn 观察时刻
      （transcript_observed_at），子 session 运行期间不再刷新；
    - Gateway 对 subagent run 不发 sessions.changed(hasActiveRun=True) 事件
      （WS 日志 12:10:48→12:27:17 对 jiweixia 空白 16 分钟，但 sqlite transcript
       12:10-12:31 持续有事件写入）；
    - 双信号同时失明 → 看板把正在跑子任务的 agent 判成 waiting/idle。

    sqlite 时间戳是真实写入时间（运行中实测 age<1s），按 session_key
    精确对应后取 max 覆盖 CLI 值。会话彻底结束后 sqlite 时间戳停止前进，
    不影响 idle/stale 判定。

    R1修复(2026-09-11)：8.2 升级删掉 sessions 表（实测 12/12 agent 库均无），
    旧查询每 15s 必抛 no such table 且被裸 except 静默吞掉 → 本修复自 8.2 起完全
    失效（issue-0295 9-7 复发的直接根因）。换 session_windows 表：
    - 字段选 COALESCE(transcript_updated_at, updated_at)：transcript_updated_at
      是 transcript 真实写入时间（与 0183 语义完全对口，实测运行中 age<1s）；
      可能为 NULL（刚建窗口还没写 transcript），退 updated_at（≈spawn 时刻，
      覆盖也不出错）。纯用 updated_at 会把 status 等元数据变更时刻也当写入时刻，
      语义偏差放不需要的语义进来。
    - 吞错改 warning 日志（per-agent 限流 5min，12 agent×每 15s 全打会刷屏）。
    """
    per_agent_keys = {}
    for s in data.get('sessions', []):
        aid = s.get('agentId')
        key = s.get('key', '')
        if aid and key:
            per_agent_keys.setdefault(aid, {})[key] = s
    for aid, keymap in per_agent_keys.items():
        db_path = os.path.join(AGENTS_DIR, aid, 'agent', 'openclaw-agent.sqlite')
        if not os.path.isfile(db_path):
            continue
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
            try:
                # 可接受债务（issue-0183 对焦虾第2轮R2-1）：全表扫无 LIMIT/索引。session_windows
                # 为本地小表（仅窗口元数据行，当前百行级），全表扫开销可忽略；若量级增长需加 LIMIT/索引。
                rows = conn.execute(
                    'SELECT session_key, COALESCE(transcript_updated_at, updated_at) FROM session_windows').fetchall()
            finally:
                conn.close()
        except Exception as e:
            # R1修复：不再静默吞——每 agent 限流 5 分钟一条，保留排查入口
            _last = _sqlite_warn_ts.get(aid, 0)
            if time.time() - _last > 300:
                _sqlite_warn_ts[aid] = time.time()
                logging.warning(f'[SQLITE] session_windows 覆盖失败 ({aid}): {type(e).__name__}: {e}')
            continue
        for sk, upd in rows:
            s = keymap.get(sk)
            if s is None or not upd:
                continue
            if upd > s.get('updatedAt', 0):
                s['updatedAt'] = upd
    return data

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
        data = merge_with_snapshot(data)
        data = _refresh_updated_at_from_sqlite(data)  # issue-0183：sqlite 真源覆盖 CLI 滞后 updatedAt
        # R1修复：ageMs 必须在 sqlite 覆盖后计算，否则前端直接消费的 s.ageMs 仍是 CLI 滞后值
        now_ms = int(time.time() * 1000)
        for x in data.get('sessions', []):
            if 'updatedAt' in x:
                x['ageMs'] = now_ms - x['updatedAt']
        with _sessions_lock:
            _sessions_cache['data'] = data
            _sessions_cache['ts'] = now
        return data
    except Exception as e:
        # 返回旧缓存（如果有）或空数据
        # issue-0235（2026-09-03）：静默回退曾致看板定格22:10-22:19零迹可查（模型路由v2施工期网关繁忙、
        # openclaw sessions子进程超时，except不打日志=排查无入口）。补日志：缓存年龄+异常摘要。
        # Y5(2026-09-11)：回退旧缓存时在响应体顶层加 _stale/_staleAgeSec 字段——前端可见「数据滞后」
        # 角标（0266 界面层复现路径的补口），不再无痕定格。浅拷贝顶层 dict 避免污染缓存本体。
        with _sessions_lock:
            if _sessions_cache['data'] is not None:
                stale_age = time.time() - _sessions_cache['ts']
                logging.warning(f'[CACHE] sessions CLI失败，回退旧缓存（已陈旧{stale_age:.0f}s）: {type(e).__name__}: {e}')
                _d = dict(_sessions_cache['data'])
                _d['_stale'] = True
                _d['_staleAgeSec'] = int(stale_age)
                return _d
            logging.error(f'[CACHE] sessions CLI失败且无旧缓存: {type(e).__name__}: {e}')
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

        ws_url = 'ws://127.0.0.1:18789/'

        while True:  # 外层重连循环
            # Y6(2026-09-11)：token 每次重连重读——原只在线程启动时读一次，token 轮换后
            # WS 永久失联（connect 永远 401，每 10s 空转重试，服务进程长期不重启则风险累积）。
            # 无 token 时不再 abort 线程（原 return 后 WS 永久死掉），等 60s 重试等配置出现。
            token = _get_gateway_token()
            if not token:
                logging.error('[WS] no gateway token yet, retry in 60s')
                await asyncio.sleep(60)
                continue
            try:
                async with websockets.connect(ws_url, ping_interval=None, ping_timeout=None) as ws:
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

                    # 重连后同步状态：以 Gateway 为准重建 active runs
                    sync_id = str(uuid.uuid4())
                    await ws.send(json.dumps({
                        'type': 'req',
                        'id': sync_id,
                        'method': 'sessions.list',
                        'params': {},
                    }))
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        data = json.loads(msg)
                        if data.get('type') == 'res' and data.get('id') == sync_id:
                            if not data.get('ok'):
                                # Y1(2026-09-11)：sync 失败保留旧状态——原无 ok 检查，
                                # 失败响应（result 为空）同样走到 clear → 活动状态归零。
                                # 超时异常路径本来就不 clear（跳外层重连），但失败响应路径必须拦。
                                logging.warning(f'[WS] reconnect sync not ok, keep old state: {str(data)[:200]}')
                                break
                            sessions = data.get('result', {}).get('sessions', [])
                            _agent_active_runs.clear()
                            _agent_ws_activity.clear()
                            now_ms_sync = int(time.time() * 1000)
                            for s in sessions:
                                if s.get('hasActiveRun'):
                                    sk = s.get('sessionKey', '')
                                    aid = _parse_agent_id_from_session_key(sk) or s.get('agentId', '')
                                    if sk:
                                        _agent_active_runs[sk] = True  # Y3: sessionKey 维度
                                    if aid:
                                        _agent_ws_activity[aid] = now_ms_sync
                            logging.info(f'[WS] reconnect sync ok: {len(_agent_active_runs)} active session windows')
                            break
                        # 跳过插队消息

                    # 4. 事件监听循环
                    while True:
                        # --- WS 传输层：recv + json.loads ---
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            _log_ws_error(e)
                            break  # WS 断开/死亡 → 让外层重连

                        # --- 事件处理层：解析 + 更新状态 ---
                        try:
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
                                if has_active_run is not None and (session_key or agent_id):
                                    if has_active_run:
                                        # Y3(2026-09-11)：run 标记改 sessionKey 维度——原按 agentId
                                        # 存/删，主会话 run 结束会把同 agent 其他还在跑的
                                        # session（cron/心跳/子代理）标记一并 pop → working→idle→working 抖动
                                        if session_key:
                                            _agent_active_runs[session_key] = True
                                        if agent_id:
                                            _agent_ws_activity[agent_id] = now_ms
                                        logging.info(f'[WS] sessions.changed → {session_key or agent_id} hasActiveRun={has_active_run} (processed OK)')
                                    else:
                                        # 只清本 session 的 run 标记；活动时间戳更新而非删除
                                        # （run 结束也是一次活动，30s TTL 自然过期）
                                        _agent_active_runs.pop(session_key, None)
                                        if agent_id:
                                            _agent_ws_activity[agent_id] = now_ms
                                        logging.info(f'[WS] sessions.changed → {session_key or agent_id} hasActiveRun={has_active_run} (processed OK)')
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

                        except Exception as e:
                            _log_ws_error(e)
                            continue  # 跳过这条坏事件，继续监听下一条（不 break 重连）

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


# ── 进程快照与 Gateway 进程树归属（2026-08-20 exec归因修复）──────────────
# 背景：系统 cron（twitter_monitor/巡检/邮件监控）跑在公共 workspace 路径下，
# 旧逻辑按「命令行含 workspace 路径」归因 → cron 一启动就把罗氏虾误标 working。
# 新逻辑：只有 PPID 链回溯到 Gateway 进程的才是虾的活跃 exec（进程树归属）；
# cron/launchd 起的进程 PPID 链不通到 Gateway，天然排除。
# 性能：单次 ps 快照 + 内存回溯，禁止逐级 ps 子调用；Gateway PID 每次实时解析、
# 绝不缓存（launchd 托管，重启后 PID 必变）。

_PS_SNAPSHOT_TTL = 5  # 秒；两次 ps 之间的最短间隔（agent-status 轮询与任务栏线程共享同一次快照）
_ps_snapshot_cache = {'rows': None, 'ts': 0.0}
_ps_snapshot_lock = threading.Lock()

def _ps_snapshot():
    """单次 `ps -A -o pid=,ppid=,etime=,rss=,command=` 快照（短TTL缓存，全模块共享）。
    返回 [(pid, ppid, elapsed_sec_or_None, rss_kb, cmd)]，异常时返回 []。
    2026-09-11 加 rss 列：Y9 内存看门狗改用 ps 当前 RSS + B2 _gateway_health 复用本快照。"""
    now = time.time()
    with _ps_snapshot_lock:
        if _ps_snapshot_cache['rows'] is not None and now - _ps_snapshot_cache['ts'] < _PS_SNAPSHOT_TTL:
            return _ps_snapshot_cache['rows']
    try:
        r = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,etime=,rss=,command='],
                           capture_output=True, text=True, timeout=5)
        rows = []
        for line in r.stdout.split('\n'):
            parts = line.strip().split(None, 4)
            if len(parts) == 5:
                try:
                    rows.append((int(parts[0]), int(parts[1]), _etime_to_sec(parts[2]), int(parts[3]), parts[4]))
                except ValueError:
                    continue
        with _ps_snapshot_lock:
            _ps_snapshot_cache['rows'] = rows
            _ps_snapshot_cache['ts'] = now
        return rows
    except Exception:
        return []

def _etime_to_sec(s):
    """ps etime 字段（[[dd-]hh:]mm:ss）转秒数"""
    try:
        days = 0
        if '-' in s:
            d, s = s.split('-', 1)
            days = int(d)
        sec = 0
        for p in s.split(':'):
            sec = sec * 60 + int(p)
        return days * 86400 + sec
    except Exception:
        return None

def _find_gateway_pids(rows=None):
    """从快照实时解析 Gateway PID 集合（绝不缓存结果）。
    识别：统一走 _is_gateway_cmd（Y7 2026-09-11：与 _gateway_health 同口径，词级匹配）"""
    rows = rows if rows is not None else _ps_snapshot()
    pids = set()
    for pid, _ppid, _et, _rss, cmd in rows:
        if _is_gateway_cmd(cmd):
            pids.add(pid)
    return pids

def _gateway_derived_pids(rows, gateway_pids):
    """在内存中回溯 PPID 链，返回所有 Gateway 衍生进程的 PID 集合（不含 Gateway 本身）。
    单次快照内回溯，无逐级 subprocess 调用；链断（父进程已退出）按非衍生处理（安全侧）。"""
    if not gateway_pids:
        return set()
    ppid_of = {pid: ppid for pid, ppid, _et, _rss, _cmd in rows}
    derived = set()
    for pid, ppid, _et, _rss, _cmd in rows:
        if pid in gateway_pids:
            continue
        cur, hops, seen = ppid, 0, set()
        while cur and cur > 1 and hops < 16:
            if cur in gateway_pids:
                derived.add(pid)
                break
            if cur in seen:  # 环保护
                break
            seen.add(cur)
            cur = ppid_of.get(cur)
            if cur is None:  # 快照瞬间父进程已退出 → 链断，按非衍生处理
                break
            hops += 1
    return derived

# workspace 路径 → agentId 归因标记（与旧版口径一致）
_AGENT_PATH_MARKERS = {
    'pipixia': ('workspace-pipixia', 'workspace_pipixia'),
    'jiweixia': ('workspace-jiweixia', 'workspace_jiweixia'),
    'xiaohexia': ('workspace-xiaohexia', 'workspace_xiaohexia'),
    'yoooclaw': ('workspace-yoooclaw', 'workspace_yoooclaw'),
    'banjiexia': ('workspace-banjiexia', 'workspace_banjiexia'),
    'caoxia': ('workspace-caoxia', 'workspace_caoxia'),
    'duijiaoxia': ('workspace-duijiaoxia', 'workspace_duijiaoxia'),
}

def _detect_active_process_agents():
    """检测哪些 agent 有活跃的 exec 进程，返回 agentId set。

    2026-08-20 重写为「进程树归属」（exec归因修复）：
    - 旧逻辑按「命令行含 workspace 路径」归因，系统 cron（twitter_monitor/巡检/
      邮件监控）恰好跑在公共 workspace 路径下 → cron 一启动就把罗氏虾误标 working。
    - 新逻辑：只有 PPID 链回溯到 Gateway 进程的才是虾的活跃 exec；
      cron/launchd 起的进程 PPID 链不通到 Gateway，天然排除。
    - Gateway PID 每次从 ps 快照实时解析，绝不缓存（launchd 托管，重启后 PID 必变）。
    - 性能：单次 ps 快照 + 内存回溯 PPID 链，无逐级 ps 子调用。
    - Gateway 解析不到时安全降级：返回空集（本优先级不判任何虾 working）。
    """
    try:
        rows = _ps_snapshot()
        gw_pids = _find_gateway_pids(rows)
        if not gw_pids:
            # Y2 安全降级：解析不到 Gateway（重启窗口/ps异常）→ 本优先级跳过，
            # 不算任何虾 working，交给 hasActiveRun/WS/age 三级兜底
            return set()
        derived = _gateway_derived_pids(rows, gw_pids)
        active = set()
        for pid, ppid, _et, _rss, cmd in rows:
            if pid not in derived:
                continue  # 非 Gateway 衍生（cron/launchd/常驻服务）→ 不归因任何虾
            ll = cmd.lower()
            # 只关注可能属于 agent exec 的进程类型（与旧版关键字口径一致）
            if not any(kw in ll for kw in ['python3', 'python', '/node', 'node ', 'ffmpeg', 'kling']):
                continue
            # 排除看板自身（本服务进程不归因任何虾）
            if 'token_dashboard' in ll:
                continue
            # 按命令行中的 workspace 路径归因到具体虾（与旧版路径口径一致）
            for aid, markers in _AGENT_PATH_MARKERS.items():
                if any(m in ll for m in markers):
                    active.add(aid)
            # CEO 的 workspace 是 workspace/（无后缀），单独匹配；
            # 注意：workspace-xxx 已被上面处理，这里只匹配无后缀的 workspace/
            if '/.openclaw/workspace/' in ll and 'workspace-' not in ll:
                active.add('main')
        return active
    except Exception:
        return set()

# ── 系统任务栏（2026-08-20）：非Gateway衍生的定时任务进程可见性，仅看板网页可见，不进TG推送 ──
# 语义边界：虾卡片 = LLM turn + Gateway衍生exec（token相关）；系统任务栏 = 机器上的
# cron/launchd 活动（零token）；token统计不变。
_TD_DATA_DIR = os.path.join(os.path.expanduser('~'), '.openclaw/workspace/ai_workspace/projects/token-dashboard', 'data')
_SYS_TASK_STATE_FILE = os.path.join(_TD_DATA_DIR, 'system_tasks_state.json')

# 已知任务名单（首版）；config.json 的 system_tasks 键可扩展/覆盖（config可扩展）
DEFAULT_SYSTEM_TASKS = [
    {'name': 'Twitter KOL监控', 'owner': '系统', 'period': '每20/30分钟', 'match': ['collectors.twitter_monitor']},
    {'name': '服务器巡检·健康巡检', 'owner': '基围虾/运维', 'period': '每15分钟', 'match': ['health_check.py']},
    {'name': '服务器巡检·日志轮转', 'owner': '基围虾/运维', 'period': '每小时25分', 'match': ['gateway_log_rotate.sh']},
    {'name': '服务器巡检·tmp清理', 'owner': '基围虾/运维', 'period': '每日07:00', 'match': ['clean_workspace_tmp.py']},
    {'name': '邮件监控', 'owner': '系统', 'period': '每30分钟', 'match': ['email_monitor.py']},
    # 2026-08-27 移除「Git自动备份」条目：该任务已是 OpenClaw cron（agent_id=jiweixia），
    # 由 Gateway 派生执行 git_backup.sh；本扫描器按设计边界排除一切 Gateway 派生进程
    # （见 _gateway_derived_pids 注释），此条目自上线起永远匹配不到，看板恒显「暂无运行记录」。
    # 其真实运行状态在 /cron 定时任务总览页正常展示（sqlite 实证 8-27 06:28 ok delivered）。
    # 2026-08-31 登记「国学bot服务」：launchd 常驻服务 com.openclaw.guoxue-bot（KeepAlive），
    # 每周重启后被 launchd 重新拉起的前 30 分钟会被未识别栏误抓（跑超 30 分钟才被常驻排除规则过滤）。
    # match 双关键字：中文路径段最精确；bot/bot_server.py 为 ASCII 兜底（全机器唯一，无同名冲突），
    # 防 ps 输出环境变化导致中文匹配失效。项目目录现为 桐姐玄学项目（原「国学运势顾问V3」仅剩 data 残留）。
    {'name': '国学bot服务', 'owner': '基围虾/运维', 'period': '常驻（launchd）', 'match': ['桐姐玄学项目/生产-TGBot/bot/bot_server.py', 'bot/bot_server.py']},
]

def _load_system_task_defs():
    """任务名单：config.json 的 system_tasks 键存在且非空则整体覆盖，否则用内置名单"""
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(cfg_path) as f:
            cfg = json.load(f)
        defs = cfg.get('system_tasks')
        if isinstance(defs, list) and defs:
            out = []
            for d in defs:
                if isinstance(d, dict) and d.get('name') and d.get('match'):
                    out.append({'name': str(d['name']), 'owner': str(d.get('owner', '系统')),
                                'period': str(d.get('period', '')),
                                'match': [str(m) for m in d['match'] if m]})
            if out:
                return out
    except Exception:
        pass
    return DEFAULT_SYSTEM_TASKS

_sys_task_state = {'tasks': {}}          # name → {'time','durationSec','exit'} 最近一次运行记录（持久化）
_sys_task_lock = threading.Lock()
_sys_task_latest = {'tasks': [], 'unknown': [], 'ts': 0}  # 最近一次扫描结果（供 /system-tasks 端点）
_sys_task_seen = {}                       # name → {'elapsedSec','ts','startTs'} 用于运行→结束转换时算时长
_SYS_TASK_SCAN_INTERVAL = 5               # 秒（短任务不漏拍：health_check类短巡检<10s也能捕捉）

def _sys_task_load_state():
    """看板重启后最近一次运行记录不丢（验收5）"""
    try:
        with open(_SYS_TASK_STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data.get('tasks'), dict):
            with _sys_task_lock:
                _sys_task_state['tasks'] = data['tasks']
    except Exception:
        pass

def _sys_task_save_state():
    try:
        os.makedirs(_TD_DATA_DIR, exist_ok=True)
        tmp = _SYS_TASK_STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'tasks': _sys_task_state['tasks'], 'updatedAt': time.time()}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _SYS_TASK_STATE_FILE)
    except Exception as e:
        logging.warning(f'[SysTask] 状态持久化失败: {e}')

def _sys_task_scan_once():
    """扫描一次系统进程：已知任务运行态 + 未识别python定时进程兑底。
    同时维护 运行中→结束 转换并把「最近一次」写入 data/system_tasks_state.json。"""
    rows = _ps_snapshot()
    gw_pids = _find_gateway_pids(rows)
    derived = _gateway_derived_pids(rows, gw_pids)
    now = time.time()
    defs = _load_system_task_defs()
    matched_pids = set()
    running_now = {}
    for d in defs:
        procs = []
        for pid, ppid, et, _rss, cmd in rows:
            if pid in derived or pid in matched_pids or pid in gw_pids:
                continue
            if any(m in cmd for m in d['match']):
                procs.append((pid, et, cmd))
                matched_pids.add(pid)
        if procs:
            el = max((et for _p, et, _c in procs if et is not None), default=None)
            running_now[d['name']] = {'elapsedSec': el, 'procCount': len(procs)}
    # 未识别兑底：非名单内、非Gateway衍生、含workspace路径的 python 定时进程（只显示不归因）
    # 排除常驻服务（跑超过30分钟的 python 服务不是定时任务，避免任务栏长期挂噪音）
    unknown = []
    for pid, ppid, et, _rss, cmd in rows:
        if pid in derived or pid in matched_pids or pid in gw_pids:
            continue
        ll = cmd.lower()
        if 'python' not in ll:
            continue
        if 'token_dashboard' in ll:
            continue
        if '.openclaw/workspace' not in cmd and 'ai_workspace' not in cmd:
            continue
        if et is not None and et > 1800:
            continue
        # 摘要取尾部：脚本名/参数在命令行尾部，头部都是路径前缀
        summary = cmd if len(cmd) <= 100 else '…' + cmd[-95:]
        unknown.append({'pid': pid, 'elapsedSec': et, 'cmdSummary': summary})
    # 运行→结束转换：记录最近一次（时间=开始时刻，时长=上次见到时的已跑秒数+扫描间隔，退出=正常）
    # 注：ps 拿不到退出码，「正常」=进程完整跑完后消失（中途被 kill 的场景 v1 无法区分，已知局限）
    with _sys_task_lock:
        for name, seen in list(_sys_task_seen.items()):
            if name not in running_now:
                dur = int((seen.get('elapsedSec') or 0) + (now - seen.get('ts', now)))
                _sys_task_state['tasks'][name] = {
                    'time': time.strftime('%m-%d %H:%M', time.localtime(seen.get('startTs', now))),
                    'durationSec': dur, 'exit': 'normal'}
                _sys_task_seen.pop(name, None)
                _sys_task_save_state()
        for name, info in running_now.items():
            prev = _sys_task_seen.get(name)
            start_ts = prev['startTs'] if prev else (now - (info['elapsedSec'] or 0))
            _sys_task_seen[name] = {'elapsedSec': info['elapsedSec'], 'ts': now, 'startTs': start_ts}
        # 转换处理完成后再组装输出（保证刚结束的任务立刻带上「最近一次」记录）
        tasks_out = []
        for d in defs:
            rn = running_now.get(d['name'])
            tasks_out.append({'name': d['name'], 'owner': d['owner'], 'period': d['period'],
                              'running': rn is not None,
                              'elapsedSec': rn.get('elapsedSec') if rn else None,
                              'procCount': rn.get('procCount', 0) if rn else 0,
                              'last': _sys_task_state['tasks'].get(d['name'])})
        _sys_task_latest['tasks'] = tasks_out
        _sys_task_latest['unknown'] = unknown[:5]  # 最多展示5条，防刷屏
        _sys_task_latest['ts'] = int(now * 1000)

def _sys_task_monitor_loop():
    """后台扫描线程：独立于前端轮询，保证没人开看板时也能捕捉运行→结束转换并持久化"""
    while True:
        try:
            _sys_task_scan_once()
        except Exception as e:
            logging.warning(f'[SysTask] 扫描异常: {e}')
        time.sleep(_SYS_TASK_SCAN_INTERVAL)

@cached(60)
@cached(120)
def _get_openclaw_cron():
    """获取所有 agent 的 openclaw cron 汇总（并发遍历）
    B3(2026-09-11)：加 120s 缓存——原无缓存，每次前端 cron 轮询都并发 spawn 12 个 CLI
    子进程（每个 timeout 15s）；降频后子进程量减半以上，lastModel 有独立 TTL 缓存不受影响。"""
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
                    # 用途：静态字典优先，miss 时走前缀规则动态补全（2026-09-07，覆盖25条空缺）
                    'purpose': CRON_PURPOSE.get(name, '') or _cron_purpose_fill(name, item.get('enabled', True)),
                    'enabled': item.get('enabled', True),
                    'schedule': expr,
                    'scheduleHuman': human,
                    'tz': sched.get('tz', ''),
                    'lastRunStatus': state.get('lastRunStatus', '?'),
                    'lastRunAtMs': state.get('lastRunAtMs'),
                    'nextRunAtMs': state.get('nextRunAtMs'),
                    'consecutiveErrors': state.get('consecutiveErrors', 0),
                    'lastDurationMs': state.get('lastDurationMs'),
                    # 模型两口径（2026-08-29 新增）：configModel=payload 配置值；lastModel=最近一次 run 的实际调用模型
                    'configModel': (item.get('payload') or {}).get('model') or '' if isinstance(item.get('payload'), dict) else '',
                    'lastModel': _cron_job_recent_model(item.get('id', ''), agent_id),
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
        if ',' in minute:
            # 逗号列表，如 "5,35" → 计算等间隔输出"每X分钟"
            mins = [int(x) for x in minute.split(',') if x.isdigit()]
            if len(mins) >= 2:
                intervals = [mins[i+1] - mins[i] for i in range(len(mins)-1)]
                if len(set(intervals)) == 1:
                    return f"每{intervals[0]}分钟"
            return f"每小时:{minute}分"
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

@cached(60)
def _get_crontab():
    """获取系统 crontab，合并注释和任务行。
    2026-09-07 方案B：注释行中带 [停用YYYYMMDD...]/[暂停YYYYMMDD...] 标记的原 cron 行
    解析为已停用条目（enabled=False，保留 schedule/purpose/停用日期/原因），历史可追溯。"""
    import re as _re_dis
    # 匹配：# [停用2026-08-22 余额不足520cr] */20 * * * * cmd... / # [暂停20260828 项目暂停] 0 8 * * * cmd...
    marker_re = _re_dis.compile(r'^#\s*\[(停用|暂停)\s*(\d{4})-?(\d{2})-?(\d{2})\s*([^\]]*)\]\s*(.+)$')
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
                # 方案B：停用/暂停标记行 → 已停用条目（灰条展示，不入 comments）
                m = marker_re.match(line)
                if m:
                    action, y, mo, d, reason, cron_part = m.groups()
                    parts = cron_part.split(None, 5)
                    if len(parts) >= 6:
                        schedule = ' '.join(parts[:5])
                        full_cmd = cron_part[len(schedule):].strip().lower()
                        entries.append({
                            'schedule': schedule,
                            'scheduleHuman': _cron_to_chinese(schedule),
                            'command': parts[5][:120],
                            'purpose': _crontab_purpose(full_cmd, line) or '已停用（用途未标注）',
                            'comment': '',
                            'enabled': False,
                            'disabledAction': action,
                            'disabledDate': f'{y}-{mo}-{d}',
                            'disabledReason': reason.strip(),
                        })
                    continue
                comment_text = line[2:].strip()[:80]
                comments.append(comment_text)
                continue
            # 解析 cron 行
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            schedule = ' '.join(parts[:5])
            command = parts[5][:120]
            # 匹配用途和注释（2026-09-07 抽取为 _crontab_purpose，与停用行共用）
            full_cmd = ' '.join(parts[5:]).lower()
            purpose = _crontab_purpose(full_cmd, line)
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

@cached(60)
def _get_heartbeats():
    """获取各虾的心跳配置（含已关闭的，用于看板完整展示）"""
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

        result = []
        for a in agents:
            aid = a.get('id', a.get('agentId', ''))
            hb = a.get('heartbeat', {})
            every = hb.get('every', '0m')
            # 0m/0 = 心跳关闭，但仍展示以便看板完整呈现所有虾的状态
            is_enabled = every and every not in ('0m', '0', '', '0min', '0minutes')
            result.append({
                'agentId': aid,
                'agentName': AGENT_NAMES.get(aid, aid),
                'every': every if every and every not in ('0m', '0', '') else '已关闭',
                'target': hb.get('target', 'none'),
                'enabled': is_enabled,
            })
        return result
    except Exception as e:
        return [{'error': str(e)}]

@cached(60)
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
            with open(pf, 'rb') as f:
                pl = plistlib.load(f)
            label = pl.get('Label', '')
            info['name'] = label
            info['type'] = 'LaunchAgent'

            # StartInterval
            interval = pl.get('StartInterval')
            info['interval'] = int(interval) if interval else None

            # RunAtLoad
            info['runAtLoad'] = pl.get('RunAtLoad')

            # KeepAlive
            ka = pl.get('KeepAlive')
            info['keepAlive'] = bool(ka) if ka is not None else None

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
    # protocol_version = "HTTP/1.1"  # 回滚：Python http.server 的 HTTP/1.1 实现导致线程耗尽，所有请求超时

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    def handle_one_request(self):
        """重写父类方法：统一捕获 BrokenPipe/ConnectionReset/ConnectionAborted。
        cloudflared 超时断开后，Python server 写响应会抛这些异常，
        不捕获会导致线程崩溃+stderr刷屏。静默处理即可。"""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端（cloudflared）已断开，无法发送响应——静默丢弃
            try:
                self.close_connection = True
            except Exception:
                pass

    def _send_json(self, status, data, extra_headers=None):
        """安全发送 JSON 响应。如果客户端已断开（BrokenPipe/ConnectionReset），静默处理。"""
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', _cors_origin(self))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端（cloudflared）已断开，无法发送——静默丢弃
            logging.debug('client disconnected before response (provider may have been slow)')

    def _proxy_quota(self, api_url, api_key, provider_id):
        """代理请求额度 API，返回 JSON 给浏览器。带缓存+超时fallback。"""
        # 1. 查缓存（TTL 内直接返回；过期后仍保留旧缓存用于 fallback）
        now = time.time()
        with _quota_cache_lock:
            c = _quota_cache.get(provider_id)
            if c and now - c[0] < _QUOTA_CACHE_TTL:
                self._send_json(200, c[1])
                return
            stale_cache = c  # 过期缓存也留着，超时时 fallback 用

        # 2. 调上游API（超时 10 秒，原 5 秒太短导致 kimi/智谱偶尔超时）
        try:
            headers = {'Authorization': 'Bearer ' + api_key}
            # Kimi Coding Plan 需要特殊 User-Agent
            if 'kimi.com/coding' in api_url:
                headers['User-Agent'] = 'KimiCLI/1.6'
            req = urllib.request.Request(api_url, headers=headers)
            # 2026-07-23 MiniMax套餐到期下线，去掉 'minimaxi' in api_url 条件（代码保留备用）
            if 'z.ai' in api_url or 'deepseek' in api_url or 'moonshot' in api_url or 'kimi.com' in api_url:
                with _PROXY_OPENER.open(req, timeout=10) as resp:
                    data = json.loads(resp.read())
            else:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
            # 成功 → 更新缓存
            with _quota_cache_lock:
                _quota_cache[provider_id] = (now, data)
            self._send_json(200, data)
        except (urllib.error.URLError, TimeoutError) as e:
            # 超时 → fallback 到旧缓存（即使过期），无缓存才返回 504
            if stale_cache:
                data = dict(stale_cache[1])
                data['cached'] = True
                self._send_json(200, data)
            else:
                self._send_json(504, {'error': '上游API超时', 'cached': False})
        except Exception as e:
            # 其他错误 → 也 fallback 到旧缓存
            if stale_cache:
                data = dict(stale_cache[1])
                data['cached'] = True
                self._send_json(200, data)
            else:
                self._send_json(502, {'error': str(e)})

    def do_OPTIONS(self):
        """处理 CORS preflight"""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', _cors_origin(self))
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_POST(self):
        """处理登录表单提交"""
        if self.path == '/login':
            # 登录频率限制
            client_ip = self.client_address[0]
            if not _check_login_rate_limit(client_ip):
                body = '<h1>Too many attempts</h1><p>请1分钟后再试。</p>'.encode('utf-8')
                self.send_response(429)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            # parse application/x-www-form-urlencoded
            from urllib.parse import parse_qs
            params = parse_qs(body)
            user = params.get('username', [''])[0]
            passwd = params.get('password', [''])[0]
            # R1（issue-0235第2轮）：常量时间比较，防时序侧信道
            if hmac.compare_digest(user.encode('utf-8'), _AUTH_USER.encode('utf-8')) and hmac.compare_digest(passwd.encode('utf-8'), _AUTH_PASS.encode('utf-8')):
                token = _create_session(user)
                remember = 'remember' in params
                self.send_response(302)
                self.send_header('Location', '/')
                # R3（issue-0235第2轮）：经Cloudflare Tunnel（HTTPS）访问时Cookie加Secure；本地http不加（无条件加会使127.0.0.1登录失效）
                _secure = '; Secure' if (self.headers.get('X-Forwarded-Proto', '') == 'https' or self.headers.get('CF-Connecting-IP')) else ''
                if remember:
                    self.send_header('Set-Cookie', f'td_session={token}; Max-Age={_SESSION_MAX_AGE}; Path=/; SameSite=Lax; HttpOnly{_secure}')
                else:
                    self.send_header('Set-Cookie', f'td_session={token}; Path=/; SameSite=Lax; HttpOnly{_secure}')
                self.end_headers()
            else:
                # 登录失败，返回登录页+错误提示
                body = _LOGIN_HTML.replace('</div></body>', '<div class="err">用户名或密码错误</div></div></body>')
                body = body.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def _require_auth(self):
        """检查认证（Cookie session）
        本地直接访问（127.0.0.1/localhost）免认证
        通过 Cloudflare Tunnel 的请求需要认证"""
        client_ip = self.client_address[0]
        is_direct_local = client_ip in ('127.0.0.1', 'localhost', '::1') and not self.headers.get('CF-Connecting-IP')
        if is_direct_local:
            return True
        # 检查 cookie session
        if _check_session(self):
            return True
        # 未登录，跳转登录页
        _send_auth_challenge(self)
        return False

    def do_GET(self):
        # /login 页面不需要认证
        if self.path == '/login':
            _send_login_page(self)
            return
        # /logout 清除Cookie（JWT无状态，不需要服务端清理）
        if self.path == '/logout':
            self.send_response(302)
            self.send_header('Location', '/login')
            # R3同口径：隧道访问时清除Cookie也带Secure
            _secure = '; Secure' if (self.headers.get('X-Forwarded-Proto', '') == 'https' or self.headers.get('CF-Connecting-IP')) else ''
            self.send_header('Set-Cookie', f'td_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{_secure}')
            self.end_headers()
            return
        # 其他路由需要认证
        if not self._require_auth():
            return
        if self.path == '/providers':
            """返回动态 provider 列表（不含 apiKey）"""
            providers = _get_providers()
            safe = [{'id': p['id'], 'label': p['label'], 'baseUrl': p['baseUrl'],
                     'quotaType': p['quotaType']} for p in providers]
            body = json.dumps(safe).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', _cors_origin(self))
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
            self._proxy_quota(p['quotaApi'], p['apiKey'], pid)
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
                active_run_stale_ms = 2 * 60 * 1000  # 2分钟安全 TTL（issue-0123：事件丢失时减少恢复延迟）
                # Y3(2026-09-11)：_agent_active_runs 已改 sessionKey 维度，聚合回 agentId
                # （该 agent 任一 session 窗口在跑 = working）
                for sk in list(_agent_active_runs.keys()):
                    _aid = _parse_agent_id_from_session_key(sk) or sk
                    last_ts = _agent_ws_activity.get(_aid, 0)
                    if now - last_ts < active_run_stale_ms:
                        active_run_agents.add(_aid)
                    else:
                        # 超过2分钟没有刷新，清除可能泄漏的标记
                        _agent_active_runs.pop(sk, None)

                # === WS 实时活动检测：作为 hasActiveRun 的补充兜底 ===
                # _agent_ws_activity 中有最近收到事件的 agentId → last_activity_ms
                # 30秒内的 TTL，hasActiveRun 是精确信号，WS活动只做补充
                ws_active_agents = set()
                ws_threshold = 30 * 1000  # 30秒内有事件 = 活跃
                for aid, ts in _agent_ws_activity.items():
                    if now - ts < ws_threshold:
                        ws_active_agents.add(aid)

                # === 本地矫正（issue-86939）：Gateway 的 status 字段可能过时 ===
                # 如果 agent 不在 active_run_agents 中，且 _agent_ws_activity 超过60秒没更新，
                # 则认为该 agent 已经不在跑，即使 Gateway 的 status="running" 也是过时值。
                # 在优先级链中跳过 working 判定，直接落到 age 检测。
                local_correction_agents = set()
                for _aid in list(_agent_ws_activity.keys()):
                    if _aid not in active_run_agents:
                        _last_act = _agent_ws_activity.get(_aid, 0)
                        if now - _last_act > 60 * 1000:  # 60秒无 WS 活动
                            local_correction_agents.add(_aid)

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
                    # 本地矫正（issue-86939）：被标记的 agent 跳过 working 判定，
                    # 直接落到 age 检测（优先级4/5）
                    _corrected = aid in local_correction_agents
                    # 判定优先级：
                    #   1. hasActiveRun=True（Gateway 精确信号）→ working
                    #      本地矫正时跳过（active_run_agents 已不含该 agent）
                    #   2. 有活跃exec进程 → working（强信号）
                    #   3. WS 30秒内有事件 → working（补充兜底）
                    #      本地矫正时跳过（防止 Gateway 过时 status 残留）
                    #   4. updatedAt 90秒内 → working（覆盖模型思考窗口）
                    #      ⚠️ local_correction 激活时降级为 waiting（issue-0165）
                    #   5. updatedAt 90秒-10分钟 → waiting
                    #   6. 超过10分钟 → idle
                    # hasActiveRun 是最精确的信号，直接来自 Gateway 内部状态
                    if not _corrected and aid in active_run_agents:
                        status = 'working'
                    elif aid in active_agents:
                        status = 'working'
                    elif not _corrected and aid in ws_active_agents:
                        status = 'working'
                    elif effective_age is None or effective_age > 10 * 60 * 1000:
                        status = 'idle'
                    elif effective_age > 90 * 1000:
                        status = 'waiting'
                    elif _corrected:
                        # local_correction 激活时，Gateway 的 updatedAt 不可信（可能被后台刷新），
                        # 降级为 waiting 而非 working。等真正的新 turn 开始时 hasActiveRun=True 会覆盖。
                        status = 'waiting'
                    else:
                        status = 'working'
                    # 僵尸session检测（issue-0155）：idle超过30分钟
                    if status == 'idle' and effective_age is not None and effective_age > 30 * 60 * 1000:
                        status = 'stale'
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
                        'modelDistStale': _mdist.get('stale', False),  # Y4：数据来自旧 jsonl 回退时标滞后
                        'sessionCount': info['sessionCount'],
                        'subagentCount': info['subagentCount'],
                    })
                agent_order = {aid: i for i, aid in enumerate(ALL_AGENTS)}
                agents.sort(key=lambda a: agent_order.get(a['agentId'], 99))
                _active_run_agent_ids = sorted({(_parse_agent_id_from_session_key(sk) or sk) for sk in _agent_active_runs.keys()})
                body = json.dumps({'agents': agents, 'now': now, 'wsConnected': _ws_connected, 'activeRunAgents': _active_run_agent_ids, 'gatewayHealth': _gateway_health(), 'hermesHealth': _hermes_health()}).encode()
            except Exception as e:
                body = json.dumps({'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', _cors_origin(self))
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
            self.send_header('Access-Control-Allow-Origin', _cors_origin(self))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/system-tasks':
            """系统任务栏数据（非Gateway衍生的定时任务进程，仅看板可见，不进TG推送）"""
            try:
                with _sys_task_lock:
                    data = {'tasks': _sys_task_latest['tasks'], 'unknown': _sys_task_latest['unknown'],
                            'ts': _sys_task_latest['ts'], 'now': int(time.time() * 1000)}
                body = json.dumps(data).encode()
            except Exception as e:
                body = json.dumps({'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', _cors_origin(self))
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
                # 2026-08-20 模板移入项目 templates/ 纳管（脱离 scripts/ 双git夹缝），随项目git版本化
                html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'token_dashboard.html')
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
                # 静态文件也需要认证
                if not self._require_auth():
                    return
                super().do_GET()

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    # 启动 Gateway WebSocket 监听线程（实时 agent 活动状态）
    _ws_thread = threading.Thread(target=_ws_listener_loop, daemon=True, name='ws-listener')
    _ws_thread.start()

    # 系统任务栏（2026-08-20）：先加载持久化状态并同步扫一次，再起后台扫描线程
    _sys_task_load_state()
    _sys_task_scan_once()
    _systask_thread = threading.Thread(target=_sys_task_monitor_loop, daemon=True, name='sys-task-monitor')
    _systask_thread.start()

    # 内存泄漏防护（应用层，plist 硬限可能未生效）
    _mem_thread = threading.Thread(target=_memory_watchdog_loop, daemon=True, name='mem-watchdog')
    _mem_thread.start()

    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', 18888), H)
    httpd.serve_forever()
