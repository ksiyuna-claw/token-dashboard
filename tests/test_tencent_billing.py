"""Unit tests for Tencent Cloud Billing read-only discovery and adapter."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dashboard_quota as dq


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TencentBillingTests(unittest.TestCase):
    def test_get_providers_discovers_tencent_billing_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / 'data'
            data_dir.mkdir()
            (data_dir / 'gcp_billing_sa.json').write_text(json.dumps({'type': 'service_account'}), encoding='utf-8')

            openclaw_json = root / 'openclaw.json'
            openclaw_json.write_text(json.dumps({
                'models': {
                    'providers': {
                        'deepseek': {'baseUrl': 'https://api.deepseek.com', 'apiKey': 'sk-deepseek-1234567890'},
                    }
                }
            }), encoding='utf-8')
            auth_profiles = root / 'auth-profiles.json'
            auth_profiles.write_text(json.dumps({'profiles': {}}), encoding='utf-8')
            codex_auth = root / 'codex_auth.json'
            codex_auth.write_text(json.dumps({'tokens': {'access_token': 'x' * 30}}), encoding='utf-8')
            (root / 'config.json').write_text(json.dumps({
                'runpod_key': 'rp-' + '1' * 20,
                'tikhub_key': 'th-' + '1' * 25,
                'tencent_billing': {
                    'secret_id': 'AKIDTEST1234567890',
                    'secret_key': 'SECRETKEYTEST1234567890',
                },
            }), encoding='utf-8')

            providers = dq.get_providers(
                str(openclaw_json),
                str(auth_profiles),
                str(root),
                codex_auth_path=str(codex_auth),
            )
            ids = [p['id'] for p in providers]
            self.assertEqual(ids[0], 'codex')
            self.assertEqual(ids[1], 'antigravity')
            self.assertEqual(ids[-5:], ['google_billing', 'tencent_billing', 'runpod', 'tikhub', 'deepseek'])
            tc = next(p for p in providers if p['id'] == 'tencent_billing')
            self.assertEqual(tc['quotaType'], 'tencent_billing')

    def test_fetch_tencent_billing_with_live_balance_and_cam_fallback(self):
        class FakeOpener:
            def __init__(self, allow_balance):
                self.allow_balance = allow_balance

            def open(self, req, timeout=15):
                action = req.headers.get('X-tc-action') or req.headers.get('X-TC-Action')
                if action == 'DescribeBillSummaryByProduct':
                    return _FakeResponse({
                        'Response': {
                            'Ready': 1,
                            'SummaryTotal': {'RealTotalCost': '1.25', 'CashPayAmount': '1.25', 'VoucherPayAmount': '0'},
                        }
                    })
                if action == 'DescribeCostSummaryByProduct':
                    return _FakeResponse({
                        'Response': {
                            'Ready': 1,
                            'Total': {'RealTotalCost': '12.98'},
                            'Data': [
                                {'BusinessCodeName': '轻量应用服务器', 'RealTotalCost': '7.05'},
                                {'BusinessCodeName': '域名注册', 'RealTotalCost': '5.93'},
                                {'BusinessCodeName': '零消耗产品', 'RealTotalCost': '0.00'},
                            ],
                        }
                    })
                if action == 'DescribeAccountBalance':
                    if self.allow_balance:
                        return _FakeResponse({
                            'Response': {
                                'RealBalance': 3530,
                                'CashAccountBalance': 3530,
                                'FreezeAmount': 0,
                                'OweAmount': 0,
                            }
                        })
                    return _FakeResponse({
                        'Response': {
                            'Error': {'Code': 'UnauthorizedOperation.CamNoAuth', 'Message': 'no auth'}
                        }
                    })
                raise AssertionError('unexpected action: ' + str(action))

        cache1 = dq.QuotaCache(ttl_seconds=300, store={}, lock=dq.threading.Lock())
        st1, pl1 = dq.fetch_tencent_billing(
            'AKIDTEST123',
            'SECRETKEY123',
            cache=cache1,
            opener=FakeOpener(allow_balance=True),
            now_fn=lambda: 1_795_747_200,  # 2026-11-27 UTC+8
        )
        self.assertEqual(st1, 200)
        self.assertTrue(pl1['available'])
        self.assertTrue(pl1['balanceAvailable'])
        self.assertEqual(pl1['balanceCny'], 35.3)
        self.assertEqual(pl1['monthRealBill'], 1.25)
        self.assertEqual(pl1['monthCost'], 12.98)
        self.assertEqual(pl1['products'], [
            {'name': '轻量应用服务器', 'cost': 7.05},
            {'name': '域名注册', 'cost': 5.93},
        ])

        cache2 = dq.QuotaCache(ttl_seconds=300, store={}, lock=dq.threading.Lock())
        st2, pl2 = dq.fetch_tencent_billing(
            'AKIDTEST123',
            'SECRETKEY123',
            cache=cache2,
            opener=FakeOpener(allow_balance=False),
            now_fn=lambda: 1_795_747_200,
        )
        self.assertEqual(st2, 200)
        self.assertTrue(pl2['available'])
        self.assertFalse(pl2['balanceAvailable'])
        self.assertIsNone(pl2['balanceCny'])
        self.assertEqual(pl2['monthCost'], 12.98)


if __name__ == '__main__':
    unittest.main()
