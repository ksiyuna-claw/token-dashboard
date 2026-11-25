"""Playwright UI tests for Antigravity quota card, cron agent names, and XSS escaping."""
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates/token_dashboard.html'
ORIGIN = 'http://token-dashboard.test'


@asynccontextmanager
async def quota_page(providers, quotas, cron_payload=None, refresh_handler=None):
    async with async_playwright() as playwright:
        with tempfile.TemporaryDirectory(prefix='dashboard-quota-chrome-') as profile:
            context = await playwright.chromium.launch_persistent_context(
                profile,
                channel='chrome',
                headless=True,
                service_workers='block',
                args=['--disable-background-networking', '--no-first-run'],
                viewport={'width': 1280, 'height': 900},
            )

            async def handle(route):
                url = urlsplit(route.request.url)
                if url.netloc != 'token-dashboard.test':
                    await route.abort()
                elif url.path == '/':
                    await route.fulfill(content_type='text/html; charset=utf-8', body=TEMPLATE.read_bytes())
                elif url.path == '/providers':
                    await route.fulfill(json=providers)
                elif url.path == '/quota/antigravity/refresh' and route.request.method == 'POST':
                    payload = refresh_handler() if refresh_handler else quotas.get('antigravity', {})
                    await route.fulfill(json=payload)
                elif url.path.startswith('/quota/'):
                    pid = url.path[len('/quota/'):]
                    await route.fulfill(json=quotas.get(pid, {'error': 'missing'}))
                elif url.path == '/agent-status':
                    now = time.time_ns() // 1_000_000
                    await route.fulfill(json={'now': now, 'lastConfirmedAt': now, 'statusFresh': True, 'agents': []})
                elif url.path == '/sessions-json':
                    await route.fulfill(json={'sessions': []})
                elif url.path == '/health-json':
                    await route.fulfill(json={'cpu_pct': 5.0, 'mem_used_gb': 4.0, 'mem_total_gb': 16.0, 'mem_pct': 25.0, 'disk_pct': 40})
                elif url.path == '/cron-json':
                    await route.fulfill(json=cron_payload or {'openclawCron': [], 'crontab': [], 'launchAgents': [], 'heartbeats': []})
                else:
                    await route.fulfill(json={})

            await context.route('**/*', handle)
            try:
                page = context.pages[0]
                await page.goto(ORIGIN, wait_until='domcontentloaded')
                yield page
            finally:
                await context.close()


class QuotaPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_antigravity_no_snapshot_and_active_snapshot_plus_xss_and_linxia(self):
        providers = [
            {'id': 'codex', 'label': 'Codex（ChatGPT订阅）', 'baseUrl': 'https://chatgpt.com', 'quotaType': 'codex_usage'},
            {'id': 'antigravity', 'label': 'Antigravity（Google AI 订阅）', 'baseUrl': 'https://antigravity.google', 'quotaType': 'antigravity_quota'},
        ]
        quotas = {
            'codex': {
                'plan': 'prolite',
                'allowed': True,
                'primaryWindow': {'usedPercent': 14, 'limitWindowSeconds': 604800, 'resetAt': int(time.time()) + 360000},
                'secondaryWindow': None,
            },
            'antigravity': {
                'available': False,
                'reason': 'no_snapshot',
                'hint': '暂无快照 · 在终端打开一次 agy 即可自动同步',
            },
        }
        cron_payload = {
            'openclawCron': [{
                'id': 'c1',
                'name': 'linxia-job<img src=x onerror="window.__xss=1">',
                'agentId': 'linxia',
                'purpose': '测试<script>window.__xss=2</script>',
                'enabled': True,
                'schedule': '0 * * * *',
                'scheduleHuman': '每小时',
                'lastRunStatus': 'ok',
                'lastRunAtMs': None,
                'nextRunAtMs': None,
                'consecutiveErrors': 0,
            }],
            'crontab': [],
            'launchAgents': [],
            'heartbeats': [],
        }

        errors = []
        refresh_calls = []

        def handle_refresh():
            refresh_calls.append(len(refresh_calls) + 1)
            if len(refresh_calls) == 1:
                quotas['antigravity']['updatedAt'] = int(time.time() * 1000)
                quotas['antigravity']['ageSec'] = 1
                quotas['antigravity']['buckets'][0]['usedPercent'] = 33.3
                out = dict(quotas['antigravity'])
                out['refreshed'] = True
                out['snapshotUpdated'] = True
                out['throttled'] = False
                return out
            if len(refresh_calls) == 2:
                out = dict(quotas['antigravity'])
                out['refreshed'] = False
                out['snapshotUpdated'] = False
                out['throttled'] = True
                out['cooldownRemainingSec'] = 12
                return out
            out = dict(quotas['antigravity'])
            out['refreshed'] = False
            out['snapshotUpdated'] = False
            out['throttled'] = False
            return out

        async with quota_page(providers, quotas, cron_payload=cron_payload, refresh_handler=handle_refresh) as page:
            page.on('pageerror', lambda err: errors.append(str(err)))
            await page.wait_for_function("document.querySelectorAll('#providerGrid .card').length === 2")
            cards_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('Antigravity（Google AI 订阅）', cards_text)
            self.assertIn('暂无快照', cards_text)
            self.assertLess(cards_text.index('Codex'), cards_text.index('Antigravity'))
            self.assertTrue(await page.locator('#btnRefreshAntigravity').is_visible())

            # Verify linxia resolves to 磷虾 and XSS did not execute (cron moved to #page-ops tab)
            self.assertEqual(await page.locator('#summaryGrid').count(), 0)
            await page.locator('#pageTabs button[data-page="ops"]').click()
            await page.wait_for_function("document.querySelectorAll('#cronOpenclawBody tr').length >= 1")
            await page.locator('#foldOpenclaw > summary').click()
            cron_text = await page.locator('#cronOpenclawBody').inner_text()
            self.assertIn('磷虾', cron_text)
            self.assertIsNone(await page.evaluate('window.__xss'))
            await page.locator('#pageTabs button[data-page="monitor"]').click()

            # Switch Antigravity mock to full snapshot with buckets + credits + freshness tag
            snap_ts = int(time.time() * 1000) - 180_000
            quotas['antigravity'] = {
                'available': True,
                'planTier': 'Pro<img src=x onerror="window.__xss=3">',
                'updatedAt': snap_ts,
                'ageSec': 180,
                'buckets': [
                    {
                        'id': 'gemini-5h',
                        'label': 'Gemini 5小时窗口',
                        'unit': 3,
                        'usedPercent': 22.5,
                        'resetAtMs': int(time.time() * 1000) + 10_000_000,
                        'disabled': False,
                    },
                    {
                        'id': 'gemini-weekly',
                        'label': 'Gemini 周额度',
                        'unit': 6,
                        'usedPercent': 6.2,
                        'resetAtMs': int(time.time() * 1000) + 500_000_000,
                        'disabled': False,
                    },
                    {
                        'id': '3p-5h',
                        'label': 'Claude/GPT 5小时窗口',
                        'unit': 3,
                        'usedPercent': 0.0,
                        'resetAtMs': None,
                        'disabled': False,
                    },
                    {
                        'id': '3p-weekly',
                        'label': 'Claude/GPT 周额度',
                        'unit': 6,
                        'usedPercent': 0.0,
                        'resetAtMs': None,
                        'disabled': False,
                    },
                ],
                'credits': [
                    {'id': 'ai-credits', 'label': 'AI Credits 余额', 'remainingAmount': 42}
                ],
            }
            await page.evaluate('fetchProviderQuotas()')
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('Gemini 周额度')")
            updated_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('快照 · 3分钟前', updated_text)
            self.assertIn('Gemini 5小时窗口', updated_text)
            self.assertIn('22.5% 已用', updated_text)
            self.assertIn('Gemini 周额度', updated_text)
            self.assertIn('6.2% 已用', updated_text)
            self.assertIn('Claude/GPT（5h / 周）', updated_text)
            self.assertNotIn('Claude/GPT 5小时窗口', updated_text)
            self.assertIn('AI Credits 余额', updated_text)
            self.assertIn('42', updated_text)

            # Clicking refresh button triggers POST /quota/antigravity/refresh and updates card in-place
            await page.locator('#btnRefreshAntigravity').click()
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('已实时同步最新余量')")
            refreshed_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('33.3% 已用', refreshed_text)
            self.assertNotIn('已恢复 恢复', refreshed_text)

            # Second immediate click shows cooldown feedback cleanly
            await page.locator('#btnRefreshAntigravity').click()
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('12秒冷却')")

            # Third click simulates agy shutdown race (snapshotUpdated=false) -> honest warning, never false green success
            await page.locator('#btnRefreshAntigravity').click()
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('agy 退出过快未完成写盘')")
            stale_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('⚠️ agy 退出过快未完成写盘，请再点一次刷新', stale_text)
            self.assertNotIn('已实时同步最新余量', stale_text)
            self.assertEqual(len(refresh_calls), 3)

            # Once 3p has active consumption (>0%), expand to full progress row
            quotas['antigravity']['buckets'][2]['usedPercent'] = 15.0
            await page.evaluate('fetchProviderQuotas()')
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('Claude/GPT 5小时窗口')")
            expanded_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('15% 已用', expanded_text)
            self.assertIsNone(await page.evaluate('window.__xss'))
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
