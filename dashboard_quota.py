"""Provider quota discovery, caching, and upstream/local snapshot adapters."""
import base64
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

QUOTA_CACHE_TTL = 60
GCP_BILLING_CACHE_TTL = 600
GCP_BILLING_DEFAULT_DATASET = 'billing_export'
ANTIGRAVITY_REFRESH_COOLDOWN = 15
ANTIGRAVITY_REFRESH_TIMEOUT = 15
ANTIGRAVITY_REFRESH_MAX_ATTEMPTS = 3
ANTIGRAVITY_REFRESH_SETTLE_WAIT = 0.5
_SAFE_BQ_IDENT_RE = re.compile(r'^[A-Za-z0-9_]+$')
_SAFE_GCP_PROJECT_RE = re.compile(r'^[a-z][a-z0-9-]{4,61}[a-z0-9]$')

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
_ag_refresh_lock = threading.Lock()
_ag_last_refresh_ts = 0.0
_gcp_token_cache = {}
_gcp_token_lock = threading.Lock()


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
_GCP_BILLING_CACHE = QuotaCache(ttl_seconds=GCP_BILLING_CACHE_TTL)


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

        gcp_cfg = proj_cfg.get('gcp_billing') if isinstance(proj_cfg.get('gcp_billing'), dict) else {}
        gcp_sa_path = gcp_cfg.get('sa_path') or os.path.join(project_dir, 'data', 'gcp_billing_sa.json')
        gcp_dataset = gcp_cfg.get('dataset') or GCP_BILLING_DEFAULT_DATASET
        if isinstance(gcp_sa_path, str) and gcp_sa_path and os.path.isfile(gcp_sa_path):
            result.append({
                'id': 'google_billing',
                'label': 'Google Gemini（付费 API）',
                'baseUrl': 'https://console.cloud.google.com/billing',
                'apiKey': 'service-account',
                'quotaApi': 'bigquery',
                'quotaType': 'gcp_billing',
                'saPath': gcp_sa_path,
                'dataset': str(gcp_dataset),
            })

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
    elif low.startswith('3p'):
        family = 'Claude/GPT'
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
            if reset_at_ms is not None and reset_at_ms <= 0:
                reset_at_ms = None
            if reset_at_ms is None and reset_sec is not None and reset_sec > 0:
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


def _read_snapshot_stamp(snapshot_path):
    """Return (updatedAt_ms, mtime_ns) for an Antigravity snapshot file, or Nones."""
    try:
        st = os.stat(snapshot_path)
        mtime_ns = st.st_mtime_ns
    except OSError:
        return None, None
    try:
        with open(snapshot_path, encoding='utf-8') as f:
            raw = json.load(f)
        updated_at = raw.get('updatedAt') if isinstance(raw, dict) else None
        updated_ms = int(updated_at) if is_finite_number(updated_at) and updated_at > 0 else None
    except Exception:
        updated_ms = None
    return updated_ms, mtime_ns


def _snapshot_advanced(before_stamp, after_stamp):
    """Check whether a snapshot file became freshly populated or newer than before_stamp."""
    before_ts, before_mtime = before_stamp
    after_ts, after_mtime = after_stamp
    if after_ts is None:
        return False
    if before_ts is None or after_ts > before_ts:
        return True
    if before_mtime is not None and after_mtime is not None and after_mtime > before_mtime:
        return True
    return False


def refresh_antigravity_snapshot(
    snapshot_path,
    agy_bin=None,
    cooldown_sec=ANTIGRAVITY_REFRESH_COOLDOWN,
    timeout_sec=ANTIGRAVITY_REFRESH_TIMEOUT,
    now_fn=time.time,
    runner=subprocess.run,
    max_attempts=ANTIGRAVITY_REFRESH_MAX_ATTEMPTS,
    settle_wait_sec=ANTIGRAVITY_REFRESH_SETTLE_WAIT,
    sleep_fn=time.sleep,
):
    """Trigger official zero-LLM `agy models` so statusLine refreshes local quota snapshot."""
    global _ag_last_refresh_ts
    bin_path = agy_bin or shutil.which('agy') or os.path.expanduser('~/.local/bin/agy')

    with _ag_refresh_lock:
        now = now_fn()
        if cooldown_sec > 0 and _ag_last_refresh_ts > 0 and (now - _ag_last_refresh_ts) < cooldown_sec:
            status, payload = fetch_antigravity(snapshot_path)
            if isinstance(payload, dict):
                payload = dict(payload)
                payload['refreshed'] = False
                payload['snapshotUpdated'] = False
                payload['throttled'] = True
                payload['cooldownRemainingSec'] = max(1, int(round(cooldown_sec - (now - _ag_last_refresh_ts))))
            return status, payload

        if not bin_path or not os.path.isfile(bin_path) or not os.access(bin_path, os.X_OK):
            status, payload = fetch_antigravity(snapshot_path)
            if status == 200 and isinstance(payload, dict):
                payload = dict(payload)
                payload['refreshed'] = False
                payload['snapshotUpdated'] = False
                payload['refreshError'] = '未找到 agy CLI 可执行文件'
                return 200, payload
            return 502, {'error': '未找到 agy CLI 可执行文件'}

        env = os.environ.copy()
        env.setdefault('HOME', os.path.expanduser('~'))
        before_stamp = _read_snapshot_stamp(snapshot_path)
        attempts_used = 0
        snapshot_updated = False
        last_rc = 0
        max_tries = max(1, int(max_attempts))

        for _ in range(max_tries):
            attempts_used += 1
            try:
                proc = runner(
                    [bin_path, 'models'],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                status, payload = fetch_antigravity(snapshot_path)
                if status == 200 and isinstance(payload, dict):
                    payload = dict(payload)
                    payload['refreshed'] = False
                    payload['snapshotUpdated'] = False
                    payload['attempts'] = attempts_used
                    payload['refreshError'] = f'同步超时（{timeout_sec}s），显示上次快照'
                    return 200, payload
                return 504, {'error': f'agy models 同步超时（{timeout_sec}s）'}
            except OSError as exc:
                status, payload = fetch_antigravity(snapshot_path)
                if status == 200 and isinstance(payload, dict):
                    payload = dict(payload)
                    payload['refreshed'] = False
                    payload['snapshotUpdated'] = False
                    payload['attempts'] = attempts_used
                    payload['refreshError'] = f'执行失败: {exc}'
                    return 200, payload
                return 502, {'error': f'agy models 执行失败: {exc}'}

            last_rc = proc.returncode
            if proc.returncode != 0:
                break

            if _snapshot_advanced(before_stamp, _read_snapshot_stamp(snapshot_path)):
                snapshot_updated = True
                break

            if settle_wait_sec and settle_wait_sec > 0:
                steps = 5
                step_sleep = float(settle_wait_sec) / steps
                for _ in range(steps):
                    sleep_fn(step_sleep)
                    if _snapshot_advanced(before_stamp, _read_snapshot_stamp(snapshot_path)):
                        snapshot_updated = True
                        break
                if snapshot_updated:
                    break

        if snapshot_updated:
            _ag_last_refresh_ts = now_fn()

        status, payload = fetch_antigravity(snapshot_path)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload['snapshotUpdated'] = snapshot_updated
            payload['refreshed'] = snapshot_updated
            payload['throttled'] = False
            payload['attempts'] = attempts_used
            if last_rc != 0:
                payload['refreshError'] = f'agy models 退出码 {last_rc}'
        return status, payload


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


def _b64u_encode(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b'=').decode('ascii')


def _mint_gcp_access_token(sa_info, opener=_PROXY_OPENER, now_fn=time.time):
    """Mint and cache a read-only OAuth2 access token from a GCP service account JSON."""
    client_email = sa_info.get('client_email') or ''
    private_key = sa_info.get('private_key') or ''
    token_uri = sa_info.get('token_uri') or 'https://oauth2.googleapis.com/token'
    cache_key = f'{client_email}|{token_uri}'
    now = int(now_fn())
    with _gcp_token_lock:
        cached = _gcp_token_cache.get(cache_key)
        if cached and cached[1] - now > 300:
            return cached[0]

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header_b64 = _b64u_encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode('utf-8'))
    claim_b64 = _b64u_encode(json.dumps({
        'iss': client_email,
        'scope': 'https://www.googleapis.com/auth/bigquery.readonly',
        'aud': token_uri,
        'iat': now,
        'exp': now + 3600,
    }).encode('utf-8'))
    signing_input = f'{header_b64}.{claim_b64}'.encode('ascii')
    key_obj = serialization.load_pem_private_key(private_key.encode('utf-8'), password=None)
    signature = key_obj.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f'{header_b64}.{claim_b64}.{_b64u_encode(signature)}'

    req = urllib.request.Request(
        token_uri,
        data=urlencode({
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': assertion,
        }).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    with opener.open(req, timeout=15) as resp:
        tok = json.loads(resp.read())
    access_token = tok.get('access_token')
    if not access_token:
        raise RuntimeError('GCP OAuth token响应缺少 access_token')
    expires_in = int(tok.get('expires_in') or 3600)
    with _gcp_token_lock:
        _gcp_token_cache[cache_key] = (access_token, now + expires_in)
    return access_token


def _bq_request(url, access_token, body=None, opener=_PROXY_OPENER, timeout=15):
    """Send an authenticated read-only JSON request to the BigQuery REST API."""
    headers = {'Authorization': 'Bearer ' + access_token}
    data = None
    method = 'GET'
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode('utf-8')
        method = 'POST'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_gcp_billing(
    sa_path,
    provider_id='google_billing',
    dataset=GCP_BILLING_DEFAULT_DATASET,
    cache=_GCP_BILLING_CACHE,
    opener=_PROXY_OPENER,
    now_fn=time.time,
):
    """Query current-month spend and promotional credit usage from BigQuery Billing Export."""
    if not _SAFE_BQ_IDENT_RE.match(str(dataset or '')):
        return 502, {'error': f'非法 BigQuery 数据集名: {dataset!r}', 'cached': False}
    if not sa_path or not os.path.isfile(sa_path):
        return 502, {'error': '未找到 GCP 只读服务账号密钥文件', 'cached': False}

    def _fetch():
        with open(sa_path, encoding='utf-8') as f:
            sa_info = json.load(f)
        proj = str(sa_info.get('project_id') or '').strip()
        if not _SAFE_GCP_PROJECT_RE.match(proj) or not sa_info.get('client_email') or not sa_info.get('private_key'):
            raise ValueError('GCP 服务账号 JSON 字段不完整或 project_id 非法')

        token = _mint_gcp_access_token(sa_info, opener=opener, now_fn=now_fn)
        ds_url = f'https://bigquery.googleapis.com/bigquery/v2/projects/{quote(proj)}/datasets/{quote(dataset)}'
        ds_meta = _bq_request(ds_url, token, opener=opener)
        location = str(ds_meta.get('location') or 'US')

        tb_url = f'{ds_url}/tables?maxResults=50'
        tb_resp = _bq_request(tb_url, token, opener=opener)
        export_tables = sorted(
            [
                str((t.get('tableReference') or {}).get('tableId') or '')
                for t in (tb_resp.get('tables') or [])
                if re.match(r'^gcp_billing_export_v1_[A-Za-z0-9_]+$', str((t.get('tableReference') or {}).get('tableId') or ''))
            ],
            reverse=True,
        )
        if not export_tables:
            return {
                'available': False,
                'reason': 'waiting_first_export',
                'projectId': proj,
                'dataset': dataset,
                'location': location,
                'hint': '已连通 · 首份账单生成中（2~24h）',
            }

        table_id = export_tables[0]
        sql = (
            "SELECT "
            "ANY_VALUE(currency) AS currency, "
            "ROUND(SUM(cost), 4) AS gross_cost, "
            "ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 4) AS credits_amount, "
            "ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 4) AS net_cost, "
            "ROUND(SUM(IF(DATE(usage_start_time, 'Asia/Shanghai') = CURRENT_DATE('Asia/Shanghai'), cost, 0)), 4) AS today_gross, "
            "COUNT(*) AS row_count, "
            "MAX(CAST(export_time AS STRING)) AS last_export_time "
            f"FROM `{proj}.{dataset}.{table_id}` "
            "WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE('Asia/Shanghai'))"
        )
        q_url = f'https://bigquery.googleapis.com/bigquery/v2/projects/{quote(proj)}/queries'
        q_resp = _bq_request(
            q_url,
            token,
            body={'query': sql, 'useLegacySql': False, 'location': location},
            opener=opener,
        )
        if q_resp.get('jobComplete') is False:
            raise RuntimeError('BigQuery 查询超时未完成')

        rows = q_resp.get('rows') or []
        vals = (rows[0].get('f') or []) if rows and isinstance(rows[0], dict) else []

        def _cell(idx):
            if idx < len(vals) and isinstance(vals[idx], dict):
                return vals[idx].get('v')
            return None

        def _float_cell(idx):
            raw_v = _cell(idx)
            try:
                val = float(raw_v) if raw_v is not None else 0.0
                return val if math.isfinite(val) else 0.0
            except (TypeError, ValueError):
                return 0.0

        currency = str(_cell(0) or 'USD').upper()
        gross_cost = round(_float_cell(1), 4)
        credits_raw = _float_cell(2)
        net_cost = round(_float_cell(3), 4)
        today_gross = round(_float_cell(4), 4)
        try:
            row_count = int(_cell(5) or 0)
        except (TypeError, ValueError):
            row_count = 0
        last_export_time = _cell(6)

        return {
            'available': True,
            'projectId': proj,
            'dataset': dataset,
            'tableId': table_id,
            'location': location,
            'currency': currency,
            'monthGross': gross_cost,
            'monthCredits': round(max(0.0, -credits_raw), 4),
            'monthNet': net_cost,
            'todayGross': today_gross,
            'rowCount': row_count,
            'lastExportTime': str(last_export_time) if last_export_time else None,
        }

    return cache.get_or_fetch(provider_id, _fetch, timeout_label='Google BigQuery API')


def fetch_quota(provider, antigravity_snapshot_path, codex_auth_path=None, cache=_DEFAULT_CACHE):
    """Dispatch quota query by provider quotaType."""
    qtype = provider.get('quotaType')
    pid = provider.get('id')
    if qtype == 'antigravity_quota':
        return fetch_antigravity(antigravity_snapshot_path)
    if qtype == 'gcp_billing':
        return fetch_gcp_billing(
            provider['saPath'],
            provider_id=pid,
            dataset=provider.get('dataset') or GCP_BILLING_DEFAULT_DATASET,
        )
    if qtype == 'runpod_gpu':
        return fetch_runpod(provider['apiKey'], pid, cache=cache)
    if qtype == 'tikhub_balance':
        return fetch_tikhub(provider['apiKey'], pid, cache=cache)
    if qtype == 'neurospark_usage':
        return fetch_neurospark(provider['apiKey'], pid, cache=cache)
    if qtype == 'codex_usage':
        return fetch_codex(pid, codex_auth_path=codex_auth_path, cache=cache)
    return fetch_generic_quota(provider['quotaApi'], provider['apiKey'], pid, cache=cache)
