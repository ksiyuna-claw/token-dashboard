#!/usr/bin/env python3
"""虾厂 Token 看板 HTTP 服务"""
import http.server, json, subprocess, os, time, urllib.request, urllib.error

DIR = os.path.join(os.path.expanduser('~'), '.openclaw/workspace/ai_workspace/scripts')
SNAPSHOT = os.path.join(DIR, 'token_snapshot.json')

# ── 用途标注（服务/调度 → 所属项目）──────────────────────────
CRON_PURPOSE = {
    '虾厂巡检(凌晨)': '运维 · 清晨语义巡检（罗氏虾）',
    '虾厂巡检(晚)': '运维 · 傍晚综合巡检',
    'Token用量推送': '运维 · Token用量TG推送',
    'AI视频行业日报(周二)': '内容 · AI视频行业周报',
    '桐姐运势-每日素材推送': '国学运势 · 每日素材推送',
    '每周前沿Agent研究扫描': 'AI研究 · Agent前沿扫描',
}

CRONTAB_PURPOSE = {
    # 通过命令关键词匹配，见 _get_crontab() 中的 if/elif 链
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
    'io.github.clash-verge-rev.clash-verge-rev': '网络 · Clash Verge 代理工具',
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

    # 保存新快照
    save_snapshot({'sessions': list(new_snap_sessions.values()), 'updatedAt': int(time.time()*1000)})
    return current_data

def _get_openclaw_cron():
    """获取 openclaw cron list --json 数据"""
    try:
        r = subprocess.run(
            ['openclaw', 'cron', 'list', '--json'],
            capture_output=True, text=True, timeout=15
        )
        # 跳过 config warnings，找到第一个 { 开始的 JSON
        output = r.stdout
        idx = output.find('{')
        if idx < 0:
            return []
        data = json.loads(output[idx:])
        jobs = []
        for item in data if isinstance(data, list) else data.get('jobs', data.get('crons', [])):
            sched = item.get('schedule', {})
            state = item.get('state', {})
            # 计算人类可读周期
            expr = sched.get('expr', '')
            human = _cron_expr_to_human(expr)
            name = item.get('name', '')
            jobs.append({
                'id': item.get('id', ''),
                'name': name,
                'agentId': item.get('agentId', ''),
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
    except Exception as e:
        return [{'error': str(e)}]

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

def _get_crontab():
    """获取系统 crontab"""
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        entries = []
        for line in r.stdout.strip().split('\n'):
            line = line.rstrip()
            # 跳过环境变量行（KEY=VALUE）和空行
            if not line or '=' in line.split(None, 1)[0] if line.split() else True:
                continue
            if line.startswith('#'):
                entries.append({
                    'raw': line,
                    'schedule': '',
                    'command': '',
                    'purpose': line[2:].strip()[:60],
                    'enabled': False,
                })
                continue
            # 尝试解析 cron 表达式（5段 + 命令）
            parts = line.split(None, 5)
            if len(parts) >= 6:
                schedule = ' '.join(parts[:5])
                command = parts[5][:80]
            else:
                continue  # 格式不对，跳过
            # 匹配用途
            purpose = ''
            full_cmd = ' '.join(parts[5:]).lower()
            if 'twitter_monitor' in full_cmd:
                purpose = '量化看板 · Twitter KOL 监控'
            elif 'econ_monitor' in full_cmd:
                purpose = '量化看板 · 经济日历检查'
            elif 'price_collector' in full_cmd:
                purpose = '量化看板 · 价格采集'
            elif 'health_monitor' in full_cmd:
                purpose = '量化看板 · 健康监控'
            elif 'signal_tracker' in full_cmd:
                purpose = '量化看板 · 信号追踪'
            elif 'health_check.py' in full_cmd:
                purpose = '运维 · 健康巡检（15min）'
            elif 'clean_workspace_tmp' in full_cmd:
                purpose = '运维 · tmp清理（每天07:00）'
            entries.append({
                'raw': line,
                'schedule': schedule,
                'command': command,
                'purpose': purpose,
                'enabled': True,
            })
        return entries
    except Exception as e:
        return [{'error': str(e)}]

def _get_launch_agents():
    """获取 LaunchAgent 服务信息"""
    agents = []
    patterns = [
        os.path.expanduser('~/Library/LaunchAgents/com.openclaw.*.plist'),
        os.path.expanduser('~/Library/LaunchAgents/ai.*.plist'),
        os.path.expanduser('~/Library/LaunchAgents/io.github.clash-verge-rev.*.plist'),
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
        """代理请求智谱额度 API，返回 JSON 给浏览器"""
        try:
            req = urllib.request.Request(api_url,
                headers={'Authorization': 'Bearer ' + api_key})
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
        if self.path == '/quota-overseas':
            self._proxy_quota(
                'https://api.z.ai/api/monitor/usage/quota/limit',
                os.environ.get('ZHIPU_ZAI_KEY', ''))
        elif self.path == '/quota-domestic':
            self._proxy_quota(
                'https://open.bigmodel.cn/api/monitor/usage/quota/limit',
                os.environ.get('ZHIPU_DOMESTIC_KEY', ''))
        elif self.path == '/quota-deepseek':
            self._proxy_quota(
                'https://api.deepseek.com/user/balance',
                os.environ.get('DEEPSEEK_KEY', ''))
        elif self.path == '/sessions-json':
            try:
                r = subprocess.run(
                    ['openclaw', 'sessions', '--json', '--all-agents', '--limit', '50'],
                    capture_output=True, text=True, timeout=15
                )
                d = json.loads(r.stdout)
                now = int(time.time() * 1000)
                for x in d.get('sessions', []):
                    if 'updatedAt' in x:
                        x['ageMs'] = now - x['updatedAt']
                # 合并快照
                d = merge_with_snapshot(d)
                body = json.dumps(d).encode()
            except Exception as e:
                body = json.dumps({'error': str(e)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/cron-json':
            data = {
                'openclawCron': _get_openclaw_cron(),
                'crontab': _get_crontab(),
                'launchAgents': _get_launch_agents(),
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
        elif self.path == '/health-json':
            data = _get_system_health()
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)
        else:
            if self.path == '/':
                self.path = '/token_dashboard.html'
            super().do_GET()

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    httpd = http.server.HTTPServer(('127.0.0.1', 18888), H)
    httpd.serve_forever()
