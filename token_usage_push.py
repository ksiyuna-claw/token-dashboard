#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 用量 Telegram 推送 - 每小时推送各平台额度和 Agent 会话状态"""
import json, subprocess, urllib.request, time, os
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_config():
    cfg_path = os.path.join(SCRIPT_DIR, 'config.json')
    with open(cfg_path) as f:
        return json.load(f)

CFG = load_config()
TZ = timezone(timedelta(hours=8))

AGENT_NAMES = {
    'main': '\U0001f9e2罗氏虾', 'xiaohexia': '\U0001f9e2小河虾',
    'jiweixia': '\U0001f9e2基围虾', 'pipixia': '\U0001f9e2皮皮虾',
    'caoxia': '\U0001f9e2草虾', 'banjiexia': '\U0001f9e2斑节虾',
    'duijiaoxia': '\U0001f9e2对焦虾', 'shanbei': '\U0001f9e2扇贝',
    'hailuo': '\U0001f9e2海螺',
}
AGENT_ORDER = ['main','xiaohexia','jiweixia','pipixia','caoxia','banjiexia','duijiaoxia','shanbei','hailuo']
PERIOD_MS = {3: 5*3600*1000, 6: 7*24*3600*1000, 5: 24*3600*1000}

def fmt(n):
    if n >= 1000000: return f"{n/1000000:.1f}M"
    if n >= 1000: return f"{n/1000:.1f}K"
    return str(n)

def countdown(ts):
    if not ts: return "-"
    d = ts/1000 - time.time()
    if d <= 0: return "已恢复"
    h = int(d // 3600); m = int((d % 3600) // 60)
    if h > 24: return f"{h//24}天{h%24}时"
    return f"{h}时{m}分" if h > 0 else f"{m}分"

def time_progress(ts, unit):
    if not ts or unit not in PERIOD_MS: return None
    d = ts/1000 - time.time()
    period_s = PERIOD_MS[unit] / 1000
    remaining = max(d, 0)
    elapsed = period_s - remaining
    return f"{min(max(elapsed / period_s * 100, 0), 100):.0f}%"

# 消耗速度 vs 时间进度
def quota_status(used_pct, next_reset_ms, unit):
    if used_pct >= 100:
        return "\U0001f534已停"
    period = PERIOD_MS.get(unit)
    if not period or not next_reset_ms:
        if used_pct >= 90: return "\U0001f534危险"
        if used_pct >= 60: return "\U0001f7e1偏快"
        return "\U0001f7e2安全"
    elapsed = period - (next_reset_ms - int(time.time()*1000))
    if elapsed <= 0 or elapsed < 10 * 60 * 1000:
        return "\U0001f535观察中"
    time_pct = min(elapsed / period * 100, 100)
    ratio = used_pct / max(time_pct, 0.1)
    if ratio < 1.0:
        return "\U0001f7e2安全"
    if ratio < 1.5:
        return "\U0001f7e1偏快"
    return "\U0001f534危险"

def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def send_telegram(text):
    payload = json.dumps({'chat_id': CFG['telegram']['chat_id'], 'text': text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{CFG['telegram']['bot_token']}/sendMessage",
        data=payload,
        headers={'Content-Type': 'application/json'}
    )
    urllib.request.urlopen(req, timeout=30)

def main():
    now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
    lines = [f"\U0001f4ca 虾厂 Token 用量报告", f"\U0001f550 {now_str}", ""]

    # 供应商额度
    for name, key, api in [
        ("\U0001f30f 智谱海外(Z.AI)", CFG['zhipu']['overseas_key'], "https://api.z.ai/api/monitor/usage/quota/limit"),
        ("\U0001f4e6 智谱国内(BigModel)", CFG['zhipu']['domestic_key'], "https://open.bigmodel.cn/api/monitor/usage/quota/limit"),
    ]:
        try:
            j = fetch_json(api, {'Authorization': f'Bearer {key}'})
            if j.get('code') != 200: continue
            level = j['data'].get('level', 'lite')
            lines.append(f"{name} · {level.upper()}档")
            WEEKLY_PROMPTS = {'lite': 400, 'pro': 1500, 'max': 2400}
            WINDOW_PROMPTS = {'lite': 80, 'pro': 300, 'max': 600}
            for lim in j['data']['limits']:
                if lim['type'] != 'TOKENS_LIMIT':
                    continue
                est_total = (WEEKLY_PROMPTS if lim['unit'] == 6 else WINDOW_PROMPTS).get(level, 400)
                est_remain = int(est_total * (1 - lim['percentage'] / 100))
                label = {3: '5h窗口', 6: '周额度'}.get(lim['unit'], 'Token')
                nrt = lim.get('nextResetTime')
                status = quota_status(lim['percentage'], nrt, lim['unit'])
                tp = time_progress(nrt, lim['unit'])
                tp_str = f"，时间进度 {tp}" if tp else ""
                tag = '海外' if 'Z.AI' in name else '国内'
                lines.append(f"  {status} [{tag}] {label}: {lim['percentage']}%已用{tp_str}，{countdown(nrt)}恢复")
            lines.append("")
        except Exception as e:
            lines.append(f"{name}: \u274c {e}")
            lines.append("")

    # DeepSeek 余额
    try:
        j = fetch_json('https://api.deepseek.com/user/balance',
                       {'Authorization': f"Bearer {CFG['deepseek_key']}"})
        info = j.get('balance_infos', [{}])[0]
        total = info.get('total_balance', '?')
        avail = '\u2705' if j.get('is_available') else '\u274c'
        cur = info.get('currency', 'CNY')
        lines.append(f"{avail} DeepSeek 余额: {cur == 'CNY' and '\u00a5' or cur}{total}")
        lines.append("")
    except Exception as e:
        lines.append(f"\U0001f41f DeepSeek: \u274c {e}")
        lines.append("")

    # Agent 会话
    try:
        r = subprocess.run(['openclaw', 'sessions', '--json', '--all-agents', '--limit', '50'],
                          capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout)
        all_sessions = d.get('sessions', [])

        merged = {}
        for s in all_sessions:
            aid = s.get('agentId', '?')
            if aid not in merged:
                merged[aid] = {'in': 0, 'out': 0, 'total': 0, 'ctx': 200000, 'model': '-', 'sub': 0, '_directs': []}
            m = merged[aid]
            m['in'] += s.get('inputTokens') or 0
            m['out'] += s.get('outputTokens') or 0

            key = s.get('key', '')
            is_direct = ('telegram:direct' in key or ':direct:' in key)
            is_main_session = key.endswith(':main') or (key == 'agent:' + aid + ':main')
            if is_main_session:
                is_direct = False

            if is_direct:
                m['_directs'].append(s)
                m['sub'] += 1
            else:
                m['sub'] += 1

        PROVIDER_LABELS = {'zai': '海外', 'zhipu': '国内', 'deepseek': 'DS', 'minimax': 'MiniMax', 'google': 'Google', 'ollama': '本地'}
        AGENTS_DIR = os.path.expanduser('~/.openclaw/agents')

        def last_actual_model(session_id, agent_id):
            tdir = os.path.join(AGENTS_DIR, agent_id, 'sessions')
            if not os.path.isdir(tdir):
                return None, None
            primary = []
            archive = []
            for fn in os.listdir(tdir):
                if fn.startswith(session_id) and fn.endswith('.jsonl') and 'trajectory' not in fn:
                    primary.append(os.path.join(tdir, fn))
                elif fn.startswith(session_id) and '.jsonl.reset.' in fn:
                    archive.append(os.path.join(tdir, fn))
            primary.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            archive.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            candidates = primary or archive
            if not candidates:
                return None, None
            last_model, last_provider = None, None
            try:
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
                            model = msg['model']
                            if model in ('delivery-mirror', 'gateway'):
                                continue
                            last_model = model
                            last_provider = msg.get('provider', '')
            except Exception:
                pass
            return last_model, last_provider

        for aid, m in merged.items():
            if not m['_directs']:
                continue
            m['_directs'].sort(key=lambda s: s.get('updatedAt', 0), reverse=True)
            latest = m['_directs'][0]
            m['total'] = latest.get('totalTokens') or 0
            m['ctx'] = latest.get('contextTokens') or 200000
            actual_model, actual_provider = last_actual_model(latest.get('sessionId', ''), aid)
            m['model'] = actual_model or latest.get('model') or '-'
            m['provider'] = PROVIDER_LABELS.get(actual_provider, '') if actual_provider else PROVIDER_LABELS.get(latest.get('modelProvider', ''), latest.get('modelProvider', ''))
            m['sub'] -= 1
            del m['_directs']

        total_in = sum(m['in'] for m in merged.values())
        total_out = sum(m['out'] for m in merged.values())

        lines.append(f"\U0001f916 全员状态 (总输入{fmt(total_in)} 总输出{fmt(total_out)})")
        for aid in AGENT_ORDER:
            m = merged.get(aid, {'in':0,'out':0,'total':0,'ctx':200000,'model':'-','sub':0})
            cp = round(m['total'] / m['ctx'] * 100) if m['ctx'] else 0
            name = AGENT_NAMES.get(aid, aid)
            sub = f" +{m['sub']}子代理" if m['sub'] else ""
            provider_tag = f"[{m.get('provider','')}]" if m.get('provider') else ''
            lines.append(f"  {name}: {fmt(m['total'])} ({cp}%ctx) {m['model']} {provider_tag}{sub}")
    except Exception as e:
        lines.append(f"\U0001f916 会话: \u274c {e}")

    lines.append("")
    lines.append("\U0001f310 看板: http://127.0.0.1:18888/")
    send_telegram('\n'.join(lines))

if __name__ == '__main__':
    main()
