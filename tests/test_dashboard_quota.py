"""Unit tests for dashboard_quota, statusLine snapshot writer, and server quota routing."""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard_quota as dq
import token_dashboard_server as server

STATUSLINE_SCRIPT = Path('/Users/kuangsiyu/.gemini/antigravity-cli/quota_snapshot_statusline.py')


def _load_statusline_module():
    spec = importlib.util.spec_from_file_location('quota_snapshot_statusline', STATUSLINE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DashboardQuotaTests(unittest.TestCase):
    def test_provider_ordering_places_codex_first_antigravity_second_deepseek_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oc_json = root / 'openclaw.json'
            auth_json = root / 'auth-profiles.json'
            codex_json = root / 'codex_auth.json'
            (root / 'config.json').write_text(json.dumps({
                'runpod_key': 'rp_' + '1' * 20,
                'tikhub_key': 'th_' + '2' * 24,
            }), encoding='utf-8')
            oc_json.write_text(json.dumps({
                'models': {
                    'providers': {
                        'deepseek': {'baseUrl': 'https://api.deepseek.com', 'apiKey': 'sk-ds-' + 'a' * 20},
                        'kimi': {'baseUrl': 'https://api.kimi.com/coding/v1', 'apiKey': 'sk-km-' + 'b' * 20},
                        'zhipu': {'baseUrl': 'https://open.bigmodel.cn/api/coding/paas/v4', 'apiKey': 'zp-' + 'c' * 20},
                        'zai': {'baseUrl': 'https://open.bigmodel.cn/api/coding/paas/v4', 'apiKey': 'zp-' + 'c' * 20},
                        'neurospark': {'baseUrl': 'https://api.neuro-spark.ai/v1', 'apiKey': 'ns-' + 'd' * 20},
                    }
                }
            }), encoding='utf-8')
            auth_json.write_text(json.dumps({'profiles': {}}), encoding='utf-8')
            codex_json.write_text(json.dumps({'tokens': {'access_token': 'tok_' + 'x' * 30}}), encoding='utf-8')

            providers = dq.get_providers(str(oc_json), str(auth_json), str(root), codex_auth_path=str(codex_json))
            ids = [p['id'] for p in providers]
            self.assertEqual(ids[0], 'codex')
            self.assertEqual(ids[1], 'antigravity')
            self.assertEqual(ids[-1], 'deepseek')
            self.assertNotIn('zai', ids)
            self.assertEqual(ids, ['codex', 'antigravity', 'kimi', 'zhipu', 'neurospark', 'runpod', 'tikhub', 'deepseek'])

    def test_fetch_antigravity_missing_file_returns_available_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, payload = dq.fetch_antigravity(os.path.join(tmp, 'missing.json'))
            self.assertEqual(status, 200)
            self.assertFalse(payload['available'])
            self.assertEqual(payload['reason'], 'no_snapshot')

    def test_fetch_antigravity_parses_buckets_and_credits_and_ignores_invalid_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / 'antigravity_quota.json'
            snap.write_text(json.dumps({
                'updatedAt': 1_700_000_000_000,
                'planTier': 'Ultra',
                'source': 'antigravity-cli-statusline',
                'quota': {
                    'gemini-weekly': {
                        'remaining_fraction': 0.875,
                        'reset_time': '2026-12-01T08:00:00Z',
                        'reset_in_seconds': 500000,
                    },
                    'gemini-5h': {
                        'remaining_fraction': 0.5,
                        'reset_in_seconds': 14400,
                        'disabled': True,
                    },
                    '3p-5h': {
                        'remaining_fraction': 1.0,
                        'reset_time': '1970-01-01T00:00:00Z',
                        'reset_in_seconds': 0,
                    },
                    '3p-weekly': {
                        'remaining_fraction': 1.0,
                        'reset_time': '1970-01-01T00:00:00Z',
                        'reset_in_seconds': 0,
                    },
                    'ai-credits': {
                        'remaining_amount': 42.5,
                    },
                    'broken-bucket': {
                        'remaining_fraction': 1.5,
                    },
                },
            }), encoding='utf-8')
            status, payload = dq.fetch_antigravity(str(snap), now_ms=1_700_000_120_000)
            self.assertEqual(status, 200)
            self.assertTrue(payload['available'])
            self.assertEqual(payload['planTier'], 'Ultra')
            self.assertEqual(payload['ageSec'], 120)
            self.assertEqual(len(payload['buckets']), 4)
            b5h, bwk, tp5h, tpwk = payload['buckets']
            self.assertEqual(b5h['id'], 'gemini-5h')
            self.assertEqual(b5h['unit'], 3)
            self.assertEqual(b5h['usedPercent'], 50.0)
            self.assertTrue(b5h['disabled'])
            self.assertEqual(b5h['resetAtMs'], 1_700_000_000_000 + 14400 * 1000)
            self.assertEqual(bwk['id'], 'gemini-weekly')
            self.assertEqual(bwk['unit'], 6)
            self.assertEqual(bwk['usedPercent'], 12.5)
            self.assertEqual(tp5h['label'], 'Claude/GPT 5小时窗口')
            self.assertIsNone(tp5h['resetAtMs'])
            self.assertEqual(tpwk['label'], 'Claude/GPT 周额度')
            self.assertIsNone(tpwk['resetAtMs'])
            self.assertEqual(len(payload['credits']), 1)
            self.assertEqual(payload['credits'][0]['remainingAmount'], 42.5)

    def test_fetch_antigravity_fail_closed_on_corrupt_or_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.json'
            bad.write_text('{not-json', encoding='utf-8')
            status, payload = dq.fetch_antigravity(str(bad))
            self.assertEqual(status, 502)
            self.assertIn('error', payload)

            bad.write_text(json.dumps({
                'updatedAt': 1_700_000_000_000,
                'quota': {'bad': {'remaining_fraction': -0.1}},
            }), encoding='utf-8')
            status2, payload2 = dq.fetch_antigravity(str(bad))
            self.assertEqual(status2, 502)
            self.assertIn('error', payload2)

    def test_quota_cache_hit_and_stale_fallback(self):
        store = {}
        cache = dq.QuotaCache(ttl_seconds=60, store=store, lock=threading.Lock())
        calls = []

        def ok_fetcher():
            calls.append('ok')
            return {'balance': 12.34}

        status1, data1 = cache.get_or_fetch('testp', ok_fetcher)
        self.assertEqual((status1, data1), (200, {'balance': 12.34}))
        status2, data2 = cache.get_or_fetch('testp', ok_fetcher)
        self.assertEqual((status2, data2), (200, {'balance': 12.34}))
        self.assertEqual(len(calls), 1)

        # Force cache expiry and simulate upstream failure -> stale fallback
        store['testp'] = (time.time() - 120, {'balance': 12.34})
        status3, data3 = cache.get_or_fetch('testp', lambda: (_ for _ in ()).throw(TimeoutError('slow')), timeout_label='TestAPI')
        self.assertEqual(status3, 200)
        self.assertTrue(data3['cached'])
        self.assertEqual(data3['balance'], 12.34)

        # No stale cache + timeout -> 504
        status4, data4 = cache.get_or_fetch('missing_p', lambda: (_ for _ in ()).throw(TimeoutError('slow')), timeout_label='TestAPI')
        self.assertEqual(status4, 504)
        self.assertFalse(data4['cached'])

    def test_refresh_antigravity_snapshot_runs_models_and_enforces_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap = root / 'antigravity_quota.json'
            fake_bin = root / 'agy'
            fake_bin.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            fake_bin.chmod(0o755)
            snap.write_text(json.dumps({
                'updatedAt': 1_700_000_000_000,
                'source': 'antigravity-cli-statusline',
                'quota': {'gemini-5h': {'remaining_fraction': 0.8}},
            }), encoding='utf-8')

            calls = []
            clock = [1000.0]

            def fake_runner(cmd, **kwargs):
                calls.append(cmd)
                snap.write_text(json.dumps({
                    'updatedAt': 1_700_000_050_000,
                    'source': 'antigravity-cli-statusline',
                    'quota': {'gemini-5h': {'remaining_fraction': 0.6}},
                }), encoding='utf-8')
                return subprocess.CompletedProcess(cmd, 0, stdout='ok', stderr='')

            with patch.object(dq, '_ag_last_refresh_ts', 0.0):
                status, payload = dq.refresh_antigravity_snapshot(
                    str(snap),
                    agy_bin=str(fake_bin),
                    cooldown_sec=15,
                    now_fn=lambda: clock[0],
                    runner=fake_runner,
                )
                self.assertEqual(status, 200)
                self.assertTrue(payload['refreshed'])
                self.assertFalse(payload['throttled'])
                self.assertEqual(payload['buckets'][0]['usedPercent'], 40.0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], [str(fake_bin), 'models'])

                # Immediate repeat inside cooldown window should NOT spawn agy again
                clock[0] = 1005.0
                status2, payload2 = dq.refresh_antigravity_snapshot(
                    str(snap),
                    agy_bin=str(fake_bin),
                    cooldown_sec=15,
                    now_fn=lambda: clock[0],
                    runner=fake_runner,
                )
                self.assertEqual(status2, 200)
                self.assertFalse(payload2['refreshed'])
                self.assertTrue(payload2['throttled'])
                self.assertEqual(payload2['cooldownRemainingSec'], 10)
                self.assertEqual(len(calls), 1)

                # Timeout after cooldown should fall back to existing snapshot cleanly
                clock[0] = 1020.0
                def timeout_runner(cmd, **kwargs):
                    raise subprocess.TimeoutExpired(cmd, 15)

                status3, payload3 = dq.refresh_antigravity_snapshot(
                    str(snap),
                    agy_bin=str(fake_bin),
                    cooldown_sec=15,
                    now_fn=lambda: clock[0],
                    runner=timeout_runner,
                )
                self.assertEqual(status3, 200)
                self.assertFalse(payload3['refreshed'])
                self.assertIn('超时', payload3['refreshError'])
                self.assertEqual(payload3['buckets'][0]['usedPercent'], 40.0)

    def test_codex_valid_pct_contract(self):
        for valid in (0, 0.0, 14, 99.5, 100):
            self.assertTrue(dq.codex_valid_pct(valid))
        for invalid in (None, False, True, '14', -1, float('nan'), float('inf'), float('-inf')):
            self.assertFalse(dq.codex_valid_pct(invalid))


class StatuslineWriterTests(unittest.TestCase):
    def test_sanitize_drops_pii_and_preserves_old_snapshot_on_empty_quota(self):
        mod = _load_statusline_module()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'antigravity_quota.json'
            sample = {
                'email': 'secret@example.com',
                'cwd': '/private/path',
                'transcript_path': '/private/transcript.jsonl',
                'conversation_title': 'Secret Title',
                'plan_tier': 'Pro',
                'version': '1.5.16',
                'quota': {
                    'gemini-weekly': {
                        'remaining_fraction': 0.91,
                        'reset_time': '2026-12-01T00:00:00Z',
                        'reset_in_seconds': 400000,
                    }
                },
            }
            proc = subprocess.run(
                ['python3', str(STATUSLINE_SCRIPT), '--out', str(target)],
                input=json.dumps(sample),
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, '')
            saved = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(saved['planTier'], 'Pro')
            self.assertNotIn('email', saved)
            self.assertNotIn('cwd', saved)
            self.assertNotIn('transcript_path', saved)
            self.assertEqual(saved['quota']['gemini-weekly']['remaining_fraction'], 0.91)

            # Empty quota during startup must NOT overwrite valid snapshot
            proc2 = subprocess.run(
                ['python3', str(STATUSLINE_SCRIPT), '--out', str(target)],
                input=json.dumps({'agent_state': 'initializing', 'quota': {}}),
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc2.returncode, 0)
            saved2 = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(saved2['quota'], saved['quota'])

            # Malformed stdin still exits 0 without stdout noise
            proc3 = subprocess.run(
                ['python3', str(STATUSLINE_SCRIPT), '--out', str(target)],
                input='{bad-json',
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc3.returncode, 0)
            self.assertEqual(proc3.stdout, '')

    def test_default_snapshot_path_points_to_zhitai_data_dir(self):
        mod = _load_statusline_module()
        self.assertEqual(
            mod.DEFAULT_SNAPSHOT_PATH,
            '/Volumes/ZHITAI/codex_project/token-dashboard/data/antigravity_quota.json',
        )


class ServerRoutingAndHealthTests(unittest.TestCase):
    def test_td_data_dir_resolves_from_project_dir(self):
        expected_data_dir = os.path.join(server._PROJECT_DIR, 'data')
        self.assertEqual(server._TD_DATA_DIR, expected_data_dir)
        self.assertEqual(
            server._SYS_TASK_STATE_FILE,
            os.path.join(expected_data_dir, 'system_tasks_state.json'),
        )

    def test_hermes_health_uses_ps_snapshot_instead_of_extra_ps_call(self):
        fake_rows = [(4321, 1, 5400, 12345, 'python -m hermes_cli.main gateway')]
        with patch.object(server, '_ps_snapshot', return_value=fake_rows), \
             patch.object(server.subprocess, 'check_output', side_effect=AssertionError('should not spawn ps')):
            info = server._hermes_health()
        self.assertTrue(info['running'])
        self.assertEqual(info['pid'], 4321)
        self.assertEqual(info['uptimeHours'], 1.5)

    def test_query_string_stripped_for_providers_route(self):
        handler = server.H.__new__(server.H)
        handler.path = '/providers?t=12345'
        handler.client_address = ('127.0.0.1', 54321)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        captured = {}

        def fake_send_json(status, data, extra_headers=None):
            captured['status'] = status
            captured['data'] = data

        handler._send_json = fake_send_json
        with patch.object(server, '_get_providers', return_value=[{
            'id': 'antigravity',
            'label': 'Antigravity（Google AI 订阅）',
            'baseUrl': 'https://antigravity.google',
            'apiKey': 'local-snapshot',
            'quotaType': 'antigravity_quota',
        }]):
            handler.do_GET()

        self.assertEqual(captured['status'], 200)
        self.assertEqual(len(captured['data']), 1)
        self.assertNotIn('apiKey', captured['data'][0])

    def test_post_antigravity_refresh_route_local_and_auth_guard(self):
        handler = server.H.__new__(server.H)
        handler.path = '/quota/antigravity/refresh'
        handler.client_address = ('127.0.0.1', 54322)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        captured = {}

        def fake_send_json(status, data, extra_headers=None):
            captured['status'] = status
            captured['data'] = data

        handler._send_json = fake_send_json
        with patch.object(server, '_dq_refresh_antigravity', return_value=(200, {'available': True, 'refreshed': True})):
            handler.do_POST()
        self.assertEqual(captured['status'], 200)
        self.assertTrue(captured['data']['refreshed'])

        # Remote tunnel request without session cookie must hit auth challenge (302)
        remote = server.H.__new__(server.H)
        remote.path = '/quota/antigravity/refresh'
        remote.client_address = ('198.51.100.1', 12345)
        remote.headers = {'CF-Connecting-IP': '198.51.100.1'}
        remote.wfile = io.BytesIO()
        remote_responses = []
        remote.send_response = lambda code: remote_responses.append(code)
        remote.send_header = lambda k, v: None
        remote.end_headers = lambda: None
        remote.do_POST()
        self.assertEqual(remote_responses, [302])


if __name__ == '__main__':
    unittest.main()
