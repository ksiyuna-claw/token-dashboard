"""Unit tests for Google Cloud BigQuery Billing Export discovery and adapter."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dashboard_quota as dq


def _generate_test_sa(project_id='gen-lang-client-0930787546'):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('utf-8')
    return {
        'type': 'service_account',
        'project_id': project_id,
        'client_email': f'ro@{project_id}.iam.gserviceaccount.com',
        'private_key': pem,
        'token_uri': 'https://oauth2.googleapis.com/token',
    }


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class GcpBillingTests(unittest.TestCase):
    def test_get_providers_discovers_gcp_billing_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / 'data'
            data_dir.mkdir()
            (data_dir / 'gcp_billing_sa.json').write_text(json.dumps(_generate_test_sa()), encoding='utf-8')

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
            self.assertEqual(ids[-1], 'deepseek')
            self.assertEqual(ids[-4:], ['google_billing', 'runpod', 'tikhub', 'deepseek'])
            gb = next(p for p in providers if p['id'] == 'google_billing')
            self.assertEqual(gb['quotaType'], 'gcp_billing')
            self.assertEqual(gb['dataset'], 'billing_export')

    def test_fetch_gcp_billing_waiting_first_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            sa_path = Path(tmp) / 'gcp_billing_sa.json'
            sa_path.write_text(json.dumps(_generate_test_sa()), encoding='utf-8')

            class FakeOpener:
                def open(self, req, timeout=15):
                    url = req.full_url
                    if 'oauth2.googleapis.com' in url:
                        return _FakeResponse({'access_token': 'tok-123', 'expires_in': 3600})
                    if url.endswith('/datasets/billing_export'):
                        return _FakeResponse({'location': 'US'})
                    if '/tables' in url:
                        return _FakeResponse({'tables': []})
                    raise AssertionError('unexpected url: ' + url)

            cache = dq.QuotaCache(ttl_seconds=600, store={}, lock=dq.threading.Lock())
            with patch.dict(dq._gcp_token_cache, {}, clear=True):
                status, payload = dq.fetch_gcp_billing(
                    str(sa_path),
                    cache=cache,
                    opener=FakeOpener(),
                    now_fn=lambda: 1_700_000_000,
                )
            self.assertEqual(status, 200)
            self.assertFalse(payload['available'])
            self.assertEqual(payload['reason'], 'waiting_first_export')
            self.assertEqual(payload['projectId'], 'gen-lang-client-0930787546')
            self.assertEqual(payload['dataset'], 'billing_export')
            self.assertNotIn('error', payload)

    def test_fetch_gcp_billing_parses_spend_credits_and_rejects_bad_dataset(self):
        st_bad, pl_bad = dq.fetch_gcp_billing('/nonexistent.json', dataset='bad;DROP TABLE')
        self.assertEqual(st_bad, 502)
        self.assertIn('非法', pl_bad['error'])

        with tempfile.TemporaryDirectory() as tmp:
            sa_path = Path(tmp) / 'gcp_billing_sa.json'
            sa_path.write_text(json.dumps(_generate_test_sa()), encoding='utf-8')

            class FakeOpener:
                def open(self, req, timeout=15):
                    url = req.full_url
                    if 'oauth2.googleapis.com' in url:
                        return _FakeResponse({'access_token': 'tok-456', 'expires_in': 3600})
                    if url.endswith('/datasets/billing_export'):
                        return _FakeResponse({'location': 'asia-northeast1'})
                    if '/tables' in url:
                        return _FakeResponse({
                            'tables': [
                                {'tableReference': {'tableId': 'gcp_billing_export_v1_01ABCD_EF2345'}},
                            ]
                        })
                    if url.endswith('/queries'):
                        return _FakeResponse({
                            'jobComplete': True,
                            'rows': [{
                                'f': [
                                    {'v': 'JPY'},
                                    {'v': '125.5000'},
                                    {'v': '-125.5000'},
                                    {'v': '0.0000'},
                                    {'v': '42.1000'},
                                    {'v': '8'},
                                    {'v': '2026-11-27 08:30:00+00'},
                                ]
                            }],
                        })
                    raise AssertionError('unexpected url: ' + url)

            cache = dq.QuotaCache(ttl_seconds=600, store={}, lock=dq.threading.Lock())
            with patch.dict(dq._gcp_token_cache, {}, clear=True):
                status, payload = dq.fetch_gcp_billing(
                    str(sa_path),
                    cache=cache,
                    opener=FakeOpener(),
                    now_fn=lambda: 1_700_000_000,
                )
            self.assertEqual(status, 200)
            self.assertTrue(payload['available'])
            self.assertEqual(payload['currency'], 'JPY')
            self.assertEqual(payload['monthGross'], 125.5)
            self.assertEqual(payload['monthCredits'], 125.5)
            self.assertEqual(payload['monthNet'], 0.0)
            self.assertEqual(payload['todayGross'], 42.1)
            self.assertEqual(payload['rowCount'], 8)
            self.assertEqual(payload['tableId'], 'gcp_billing_export_v1_01ABCD_EF2345')


if __name__ == '__main__':
    unittest.main()
