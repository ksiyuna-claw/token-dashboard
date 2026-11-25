"""Provider quota discovery, caching, and upstream/local snapshot adapters."""
import datetime
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

QUOTA_CACHE_TTL = 60

_PROXY_HANDLER = urllib.request.ProxyHandler({
    'http': 'http://127.0.0.1:7890',
    'https': 'http://127.0.0.1:7890',
})
_PROXY_OPENER = urllib.request.build_opener(_PROXY_HANDLER)

PROVIDER_QUOTA_API = {
    'open.bigmodel.cn': ('https://open.bigmodel.cn/api/monitor/usage/quota/limit', 'zhipu_quota'),
    'api.z.ai': ('https://api.z.ai/api/monitor/usage/quota/limit', 'zhipu_quota'),
    'api.deepseek.com': ('https://api.deepseek.com/user/balance', 'deepseek_balance'),
    'api.moonshot.cn': ('https://api.moonshot.cn/v1/users/me/balance', 'kimi_balance'),
    'api.kimi.com': ('https://api.kimi.com/coding/v1/usages', 'kimi_coding_plan'),
    'api.neuro-spark.ai': ('https://api.neuro-spark.ai/v1/usage', 'neurospark_usage'),
}

PROVIDER_LABELS = {
    'zhipu': '智谱 BigModel（国内）',
    'zai': '智谱 BigModel（海外）',
    'deepseek': 'DeepSeek',
    'neurospark': 'NeuroSpark（Claude/GLM中转）',
    'kimi': 'Kimi（Moonshot）',
}

HIDDEN_PROVIDERS = {'zai'}

_quota_cache = {}
_quota_cache_lock = threading.Lock()


def is_finite_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def codex_valid_pct(value):
    """Validate Codex usage percentage as a finite non-negative number."""
    return is_finite_number(value) and value >= 0


class QuotaCache:
    """Thread-safe TTL cache with stale fallback on upstream failure."""

    def __init__(self, ttl_seconds=QUOTA_CACHE_TTL, store=None, lock=None):
        self.ttl_seconds = ttl_seconds
        self._store = _quota_cache if store is None else store
        self._lock = _quota_cache_lock if lock is None else lock

    def get_or_fetch(self, provider_id, fetcher, timeout_label='上游API'):
        now = time.time()
        with self._lock:
            entry = self._store.get(provider_id)
            if entry and now - entry[0] < self.ttl_seconds:
                return 200, entry[1]
            stale = entry

        try:
            data = fetcher()
            with self._lock:
                self._store[provider_id] = (now, data)
            return 200, data
        except (urllib.error.URLError, TimeoutError) as exc:
            if stale:
                fallback = dict(stale[1])
                fallback['cached'] = True
                return 200, fallback
            return 504, {'error': f'{timeout_label}超时: {exc}' if timeout_label != '上游API' else '上游API超时', 'cached': False}
        except Exception as exc:
            if stale:
                fallback = dict(stale[1])
                fallback['cached'] = True
                return 200, fallback
            msg = str(exc)
            if 'imeout' in msg or 'timed out' in msg:
                return 504, {'error': f'{timeout_label}超时: {msg}', 'cached': False}
            return 502, {'error': msg, 'cached': False}


_DEFAULT_CACHE = QuotaCache()


def load_project_config(project_dir):
    """Load token-dashboard config.json once, returning {} on failure."""
    cfg_path = os.path.join(project_dir, 'config.json')
    try:
        with open(cfg_path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_auth_keys(auth_profiles_path):
    try:
        with open(auth_profiles_path, encoding='utf-8') as f:
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


def get_providers(
    openclaw_json_path,
    auth_profiles_path,
    project_dir,
    codex_auth_path=None,
):
    """Collect active quota providers in display order."""
    codex_auth_path = codex_auth_path or os.path.expanduser('~/.codex/auth.json')
    try:
        with open(openclaw_json_path, encoding='utf-8') as f:
            cfg = json.load(f)
        providers_cfg = cfg.get('models', {}).get('providers', {})
        auth_keys = _load_auth_keys(auth_profiles_path)
        result = []

        for pid, p in providers_cfg.items():
            if pid in HIDDEN_PROVIDERS:
                continue
            base_url = p.get('baseUrl', '')
            api_key = p.get('apiKey', '') or auth_keys.get(pid, '')
            quota_api, quota_type = None, None
            if pid == 'kimi':
                quota_api, quota_type = PROVIDER_QUOTA_API.get('api.kimi.com')
            if not quota_api:
                for host, (url, qtype) in PROVIDER_QUOTA_API.items():
                    if host in base_url:
                        quota_api, quota_type = url, qtype
                        break
            if not quota_api and quota_type != 'no_quota':
                continue
            if not api_key or len(api_key) < 10:
                continue
            result.append({
                'id': pid,
                'label': PROVIDER_LABELS.get(pid, pid),
                'baseUrl': base_url,
                'apiKey': api_key,
                'quotaApi': quota_api,
                'quotaType': quota_type,
            })

        proj_cfg = load_project_config(project_dir)

        rp_key = proj_cfg.get('runpod_key', '')
        if isinstance(rp_key, str) and len(rp_key) >= 10:
            result.append({
                'id': 'runpod',
                'label': 'RunPod GPU云（数字人口播）',
                'baseUrl': 'https://api.runpod.io',
                'apiKey': rp_key,
                'quotaApi': 'https://api.runpod.io/graphql',
                'quotaType': 'runpod_gpu',
            })

        try:
            with open(codex_auth_path, encoding='utf-8') as f:
                cd_tok = (json.load(f).get('tokens') or {}).get('access_token') or ''
            if isinstance(cd_tok, str) and len(cd_tok) >= 20:
                result.append({
                    'id': 'codex',
                    'label': 'Codex（ChatGPT订阅）',
                    'baseUrl': 'https://chatgpt.com',
                    'apiKey': 'oauth',
                    'quotaApi': 'https://chatgpt.com/backend-api/codex/usage',
                    'quotaType': 'codex_usage',
                })
        except Exception:
            pass

        result.append({
            'id': 'antigravity',
            'label': 'Antigravity（Google AI 订阅）',
            'baseUrl': 'https://antigravity.google',
            'apiKey': 'local-snapshot',
            'quotaApi': 'local-snapshot',
            'quotaType': 'antigravity_quota',
        })

        th_key = proj_cfg.get('tikhub_key', '')
        if isinstance(th_key, str) and len(th_key) >= 20:
            result.append({
                'id': 'tikhub',
                'label': 'TikHub（社媒数据采集）',
                'baseUrl': 'https://api.tikhub.io',
                'apiKey': th_key,
                'quotaApi': 'https://api.tikhub.io/api/v1/tikhub/user/get_user_info',
                'quotaType': 'tikhub_balance',
            })

        first_order = {'codex': 0, 'antigravity': 1}
        last_ids = {'deepseek'}
        result.sort(
            key=lambda p: (
                0 if p['id'] in first_order else 2 if p['id'] in last_ids else 1,
                first_order.get(p['id'], 0),
            )
        )
        return result
    except Exception:
        return []


def _parse_iso_ms(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _infer_antigravity_unit(bucket_id, reset_sec):
    low = bucket_id.lower()
    if any(k in low for k in ('week', '7d', 'weekly')):
        return 6
    if any(k in low for k in ('5h', '5-hour', 'five', 'short', 'rolling')):
        return 3
    if any(k in low for k in ('day', 'daily', '24h')):
        return 5
    if is_finite_number(reset_sec):
        if reset_sec > 86400:
            return 6
        if 0 < reset_sec <= 21600:
            return 3
        if 21600 < reset_sec <= 86400:
            return 5
    return 0


def _antigravity_bucket_label(bucket_id, unit):
    low = bucket_id.lower()
    family = None
    if 'gemini' in low and 'flash' in low:
        family = 'Gemini Flash'
    elif 'gemini' in low and 'pro' in low:
        family = 'Gemini Pro'
    elif 'gemini' in low:
        family = 'Gemini'
    elif 'claude' in low and 'gpt' in low:
        family = 'Claude / GPT'
    elif 'claude' in low:
        family = 'Claude'
    elif 'gpt' in low:
        family = 'GPT'
    elif 'third' in low or 'partner' in low:
        family = '第三方模型'

    unit_text = {3: '5小时窗口', 6: '周额度', 5: '日额度'}.get(unit, '')
    if family and unit_text:
        return f'{family} {unit_text}'
    if family:
        return family
    return bucket_id


def _antigravity_credit_label(bucket_id):
    low = bucket_id.lower()
    if 'credit' in low or 'g1' in low or 'ai' in low:
        return 'AI Credits 余额'
    return bucket_id


def fetch_antigravity(snapshot_path, now_ms=None):
    """Pure read-only reader for the Antigravity CLI statusLine quota snapshot."""
    if not os.path.exists(snapshot_path):
        return 200, {
            'available': False,
            'reason': 'no_snapshot',
            'hint': '暂无快照 · 在终端打开一次 agy 即可自动同步',
        }

    try:
        with open(snapshot_path, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return 502, {'error': 'Antigravity 快照格式异常'}

    if not isinstance(raw, dict) or not isinstance(raw.get('quota'), dict):
        return 502, {'error': 'Antigravity 快照格式异常'}

    updated_at = raw.get('updatedAt')
    if not is_finite_number(updated_at) or updated_at <= 0:
        return 502, {'error': 'Antigravity 快照时间异常'}
    updated_at_ms = int(updated_at)
    cur_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    age_sec = max(0, int((cur_ms - updated_at_ms) / 1000))

    buckets = []
    credits = []
    for bucket_id, item in raw['quota'].items():
        if not isinstance(bucket_id, str) or not bucket_id or not isinstance(item, dict):
            continue
        if 'remaining_fraction' in item:
            frac = item.get('remaining_fraction')
            if not is_finite_number(frac) or not (0.0 <= float(frac) <= 1.0):
                continue
            reset_sec_raw = item.get('reset_in_seconds')
            reset_sec = int(round(float(reset_sec_raw))) if is_finite_number(reset_sec_raw) and reset_sec_raw >= 0 else None
            reset_at_ms = _parse_iso_ms(item.get('reset_time'))
            if reset_at_ms is None and reset_sec is not None:
                reset_at_ms = updated_at_ms + reset_sec * 1000
            unit = _infer_antigravity_unit(bucket_id, reset_sec)
            disabled = item.get('disabled') if isinstance(item.get('disabled'), bool) else False
            buckets.append({
                'id': bucket_id,
                'label': _antigravity_bucket_label(bucket_id, unit),
                'unit': unit,
                'usedPercent': round((1.0 - float(frac)) * 100, 1),
                'remainingFraction': float(frac),
                'resetAtMs': reset_at_ms,
                'resetInSeconds': reset_sec,
                'disabled': disabled,
            })
        elif 'remaining_amount' in item:
            amt = item.get('remaining_amount')
            if is_finite_number(amt) and float(amt) >= 0.0:
                credits.append({
                    'id': bucket_id,
                    'label': _antigravity_credit_label(bucket_id),
                    'remainingAmount': float(amt),
                })

    if not buckets and not credits:
        return 502, {'error': 'Antigravity 快照无有效配额字段'}

    unit_order = {3: 0, 6: 1, 5: 2, 0: 3}
    buckets.sort(
        key=lambda b: (
            0 if 'gemini' in b['id'].lower() else 1 if 'claude' in b['id'].lower() else 2,
            unit_order.get(b['unit'], 9),
            b['id'],
        )
    )

    plan_tier = raw.get('planTier') if isinstance(raw.get('planTier'), str) else ''
    source = raw.get('source') if isinstance(raw.get('source'), str) else 'antigravity-cli-statusline'
    return 200, {
        'available': True,
        'planTier': plan_tier,
        'updatedAt': updated_at_ms,
        'ageSec': age_sec,
        'buckets': buckets,
        'credits': credits,
        'source': source,
    }


def fetch_generic_quota(api_url, api_key, provider_id, cache=_DEFAULT_CACHE):
    def _fetch():
        headers = {'Authorization': 'Bearer ' + api_key}
        if 'kimi.com/coding' in api_url:
            headers['User-Agent'] = 'KimiCLI/1.6'
        req = urllib.request.Request(api_url, headers=headers)
        if any(host in api_url for host in ('z.ai', 'deepseek', 'moonshot', 'kimi.com')):
            with _PROXY_OPENER.open(req, timeout=10) as resp:
                return json.loads(resp.read())
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    return cache.get_or_fetch(provider_id, _fetch, timeout_label='上游API')


def fetch_runpod(api_key, provider_id, cache=_DEFAULT_CACHE):
    def _fetch():
        gql_req = urllib.request.Request(
            'https://api.runpod.io/graphql?api_key=' + quote(api_key),
            data=json.dumps({'query': '{ myself { clientBalance } }'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with _PROXY_OPENER.open(gql_req, timeout=10) as resp:
            balance = json.loads(resp.read())['data']['myself']['clientBalance']

        pods_req = urllib.request.Request(
            'https://api.runpod.io/v2/pods',
            headers={'Authorization': 'Bearer ' + api_key},
        )
        with _PROXY_OPENER.open(pods_req, timeout=10) as resp:
            pods = json.loads(resp.read()).get('pods', [])

        visible_pods = [x for x in pods if (x.get('status') or '').strip().upper() != 'TERMINATED']
        running_count = sum(1 for pd in visible_pods if (pd.get('status') or '').strip().upper() == 'RUNNING')
        pod_list = []
        for pd in visible_pods[:6]:
            gpu = pd.get('gpu') or {}
            gpu_str = (gpu.get('id') or '') + ('×' + str(gpu['count']) if (gpu.get('count') or 1) > 1 else '')
            pod_list.append({
                'name': pd.get('name') or pd.get('id'),
                'status': pd.get('status'),
                'gpu': gpu_str,
                'costPerHr': pd.get('cost'),
                'startedAt': pd.get('startedAt'),
            })
        return {
            'balance': balance,
            'podCount': len(visible_pods),
            'runningCount': running_count,
            'pods': pod_list,
        }

    return cache.get_or_fetch(provider_id, _fetch, timeout_label='RunPod API')


def fetch_tikhub(api_key, provider_id, cache=_DEFAULT_CACHE):
    def _fetch():
        from curl_cffi import requests as cffi_requests

        r = cffi_requests.get(
            'https://api.tikhub.io/api/v1/tikhub/user/get_user_info',
            headers={'Authorization': 'Bearer ' + api_key},
            impersonate='chrome',
            timeout=10,
        )
        if r.status_code != 200:
            raise ValueError('TikHub API HTTP %d' % r.status_code)
        d = r.json()
        if (d or {}).get('code') != 200:
            raise ValueError('TikHub业务码异常: %r' % (d or {}).get('code'))
        u = (d or {}).get('user_data') or {}
        balance = u.get('balance')
        free_credit = u.get('free_credit')
        if not is_finite_number(balance):
            raise ValueError('balance字段缺失或非数值: %r' % (balance,))
        active = bool(u.get('is_active')) and not bool(u.get('account_disabled'))
        return {
            'balance': float(balance),
            'freeCredit': float(free_credit) if is_finite_number(free_credit) else 0.0,
            'active': active,
        }

    return cache.get_or_fetch(provider_id, _fetch, timeout_label='TikHub API')


def fetch_neurospark(api_key, provider_id, cache=_DEFAULT_CACHE):
    def _fetch():
        from curl_cffi import requests as cffi_requests

        r = cffi_requests.get(
            'https://api.neuro-spark.ai/v1/usage',
            headers={'Authorization': 'Bearer ' + api_key},
            impersonate='chrome',
            timeout=10,
        )
        if r.status_code != 200:
            raise ValueError('NeuroSpark API HTTP %d' % r.status_code)
        d = r.json()
        if not isinstance(d, dict) or 'total_spend' not in d:
            raise ValueError('NeuroSpark响应结构异常: 缺total_spend')

        def _strict(val, name):
            if not is_finite_number(val) or val < 0:
                raise ValueError('NeuroSpark字段%s非法: %r' % (name, val))
            return float(val)

        used = [
            x for x in (d.get('rows') or [])
            if isinstance(x, dict)
            and is_finite_number(x.get('spend'))
            and float(x['spend']) > 0
        ]
        used.sort(key=lambda x: -float(x['spend']))

        def _row_calls(val):
            return int(val) if is_finite_number(val) and val >= 0 else 0

        return {
            'totalSpend': _strict(d.get('total_spend'), 'total_spend'),
            'todaySpend': _strict(d.get('today_spend'), 'today_spend'),
            'totalCalls': int(_strict(d.get('total_calls'), 'total_calls')),
            'todayCalls': int(_strict(d.get('today_calls'), 'today_calls')),
            'topModels': [
                {
                    'model': str(x.get('model') or '?')[:40],
                    'spend': float(x['spend']),
                    'calls': _row_calls(x.get('calls')),
                }
                for x in used[:3]
            ],
        }

    return cache.get_or_fetch(provider_id, _fetch, timeout_label='NeuroSpark API')


def fetch_codex(provider_id, codex_auth_path=None, cache=_DEFAULT_CACHE):
    codex_auth_path = codex_auth_path or os.path.expanduser('~/.codex/auth.json')

    def _fetch():
        with open(codex_auth_path, encoding='utf-8') as f:
            auth = json.load(f)['tokens']
        tok = auth.get('access_token') or ''
        acct = auth.get('account_id') or ''
        if not tok:
            raise RuntimeError('auth.json无access_token')
        req = urllib.request.Request(
            'https://chatgpt.com/backend-api/codex/usage',
            headers={
                'Authorization': 'Bearer ' + tok,
                'chatgpt-account-id': acct,
                'User-Agent': 'codex-cli/0.166.0',
            },
        )
        with _PROXY_OPENER.open(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        rl = raw.get('rate_limit') or {}
        pw = rl.get('primary_window') or {}
        sw = rl.get('secondary_window')
        if not codex_valid_pct(pw.get('used_percent')):
            raise ValueError('codex used_percent非法: %r' % (pw.get('used_percent'),))
        return {
            'plan': raw.get('plan_type') or 'unknown',
            'allowed': rl.get('allowed'),
            'primaryWindow': {
                'usedPercent': pw.get('used_percent'),
                'limitWindowSeconds': pw.get('limit_window_seconds'),
                'resetAfterSeconds': pw.get('reset_after_seconds'),
                'resetAt': pw.get('reset_at'),
            } if pw else None,
            'secondaryWindow': {
                'usedPercent': sw.get('used_percent'),
                'limitWindowSeconds': sw.get('limit_window_seconds'),
                'resetAfterSeconds': sw.get('reset_after_seconds'),
                'resetAt': sw.get('reset_at'),
            } if sw and codex_valid_pct(sw.get('used_percent')) else None,
            'hasCredits': (raw.get('credits') or {}).get('has_credits'),
        }

    return cache.get_or_fetch(provider_id, _fetch, timeout_label='Codex API')


def fetch_quota(provider, antigravity_snapshot_path, codex_auth_path=None, cache=_DEFAULT_CACHE):
    """Dispatch quota query by provider quotaType."""
    qtype = provider.get('quotaType')
    pid = provider.get('id')
    if qtype == 'antigravity_quota':
        return fetch_antigravity(antigravity_snapshot_path)
    if qtype == 'runpod_gpu':
        return fetch_runpod(provider['apiKey'], pid, cache=cache)
    if qtype == 'tikhub_balance':
        return fetch_tikhub(provider['apiKey'], pid, cache=cache)
    if qtype == 'neurospark_usage':
        return fetch_neurospark(provider['apiKey'], pid, cache=cache)
    if qtype == 'codex_usage':
        return fetch_codex(pid, codex_auth_path=codex_auth_path, cache=cache)
    return fetch_generic_quota(provider['quotaApi'], provider['apiKey'], pid, cache=cache)
