#!/usr/bin/env python3
"""虾厂 Token 看板 HTTP 服务"""
import http.server, json, subprocess, os, time, urllib.request, urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_config():
    cfg_path = os.path.join(SCRIPT_DIR, 'config.json')
    with open(cfg_path) as f:
        return json.load(f)

CFG = load_config()
SNAPSHOT = os.path.join(CFG['work_dir'], 'token_snapshot.json')

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
            old = snap_by_key[key]
            if s.get('totalTokens') is None or s.get('totalTokens', 0) == 0:
                s['inputTokens'] = old.get('inputTokens', 0)
                s['outputTokens'] = old.get('outputTokens', 0)
                s['totalTokens'] = old.get('totalTokens', 0)
            if s.get('model', '-') == '-':
                s['model'] = old.get('model', '-')
            new_snap_sessions[key] = old

    save_snapshot({'sessions': list(new_snap_sessions.values()), 'updatedAt': int(time.time()*1000)})
    return current_data

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=CFG['work_dir'], **kw)

    def _proxy_quota(self, api_url, api_key):
        """代理请求额度 API，返回 JSON 给浏览器"""
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
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_GET(self):
        if self.path == '/quota-overseas':
            self._proxy_quota(
                'https://api.z.ai/api/monitor/usage/quota/limit',
                CFG['zhipu']['overseas_key'])
        elif self.path == '/quota-domestic':
            self._proxy_quota(
                'https://open.bigmodel.cn/api/monitor/usage/quota/limit',
                CFG['zhipu']['domestic_key'])
        elif self.path == '/quota-deepseek':
            self._proxy_quota(
                'https://api.deepseek.com/user/balance',
                CFG['deepseek_key'])
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
                d = merge_with_snapshot(d)
                body = json.dumps(d).encode()
            except Exception as e:
                body = json.dumps({'error': str(e)}).encode()
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
