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
            self.assertTrue(await page.locator('#page-monitor').is_visible())
            self.assertFalse(await page.locator('#page-quota').is_visible())
            await page.locator('#pageTabs button[data-page="quota"]').click()
            self.assertTrue(await page.locator('#page-quota').is_visible())
            self.assertFalse(await page.locator('#page-monitor').is_visible())
            await page.wait_for_function("document.querySelectorAll('#providerGrid .card').length === 2")
            cards_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('Antigravity（Google AI 订阅）', cards_text)
            self.assertIn('暂无快照', cards_text)
            self.assertLess(cards_text.index('Codex'), cards_text.index('Antigravity'))
            self.assertTrue(await page.locator('#btnRefreshAntigravity').is_visible())
            ag_classes_empty = await page.locator('[data-provider-card="antigravity"]').get_attribute('class')
            self.assertIn('card-wide', ag_classes_empty or '')

            # Verify linxia resolves to 磷虾 and XSS did not execute (cron moved to #page-ops tab)
            self.assertEqual(await page.locator('#summaryGrid').count(), 0)
            await page.locator('#pageTabs button[data-page="ops"]').click()
            await page.wait_for_function("document.querySelectorAll('#cronOpenclawBody tr').length >= 1")
            await page.locator('#foldOpenclaw > summary').click()
            cron_text = await page.locator('#cronOpenclawBody').inner_text()
            self.assertIn('磷虾', cron_text)
            self.assertIsNone(await page.evaluate('window.__xss'))
            await page.locator('#pageTabs button[data-page="quota"]').click()

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
            self.assertIn('Claude/GPT 5小时窗口', updated_text)
            self.assertIn('Claude/GPT 周额度', updated_text)
            self.assertIn('AI Credits 余额', updated_text)
            self.assertIn('42', updated_text)

            ag_classes = await page.locator('[data-provider-card="antigravity"]').get_attribute('class')
            self.assertIn('card-wide', ag_classes or '')
            ag_cols = page.locator('[data-provider-card="antigravity"] .ag-cols .ag-col')
            self.assertEqual(await ag_cols.count(), 2)
            col0_box = await ag_cols.nth(0).bounding_box()
            col1_box = await ag_cols.nth(1).bounding_box()
            self.assertIsNotNone(col0_box)
            self.assertIsNotNone(col1_box)
            self.assertLess(col0_box['x'], col1_box['x'])

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

    async def test_gcp_billing_card_states_and_four_column_bottom_row(self):
        providers = [
            {'id': 'codex', 'label': 'Codex（ChatGPT订阅）', 'baseUrl': 'https://chatgpt.com', 'quotaType': 'codex_usage'},
            {'id': 'antigravity', 'label': 'Antigravity（Google AI 订阅）', 'baseUrl': 'https://antigravity.google', 'quotaType': 'antigravity_quota'},
            {'id': 'kimi', 'label': 'Kimi（Moonshot）', 'baseUrl': 'https://api.kimi.com', 'quotaType': 'kimi_coding_plan'},
            {'id': 'zhipu', 'label': '智谱 BigModel（国内）', 'baseUrl': 'https://open.bigmodel.cn', 'quotaType': 'zhipu_quota'},
            {'id': 'neurospark', 'label': 'NeuroSpark（Claude/GLM中转）', 'baseUrl': 'https://api.neuro-spark.ai', 'quotaType': 'neurospark_usage'},
            {'id': 'google_billing', 'label': 'Google Gemini（付费 API）', 'baseUrl': 'https://console.cloud.google.com/billing', 'quotaType': 'gcp_billing'},
            {'id': 'runpod', 'label': 'RunPod GPU云（数字人口播）', 'baseUrl': 'https://api.runpod.io', 'quotaType': 'runpod_gpu'},
            {'id': 'tikhub', 'label': 'TikHub（社媒数据采集）', 'baseUrl': 'https://api.tikhub.io', 'quotaType': 'tikhub_balance'},
            {'id': 'deepseek', 'label': 'DeepSeek', 'baseUrl': 'https://api.deepseek.com', 'quotaType': 'deepseek_balance'},
        ]
        future_ms = int(time.time() * 1000) + 100_000_000
        quotas = {
            'codex': {'plan': 'prolite', 'allowed': True, 'primaryWindow': {'usedPercent': 19, 'limitWindowSeconds': 604800, 'resetAt': int(time.time()) + 300000}, 'secondaryWindow': None},
            'antigravity': {
                'available': True, 'updatedAt': int(time.time() * 1000), 'ageSec': 5,
                'buckets': [
                    {'id': 'gemini-5h', 'label': 'Gemini 5小时窗口', 'unit': 3, 'usedPercent': 18.4, 'resetAtMs': future_ms, 'disabled': False},
                    {'id': '3p-weekly', 'label': 'Claude/GPT 周额度', 'unit': 6, 'usedPercent': 95.6, 'resetAtMs': future_ms, 'disabled': False},
                ],
                'credits': [],
            },
            'kimi': {'usage': {'limit': '100', 'used': '45', 'resetTime': '2026-12-03T00:00:00Z'}, 'limits': []},
            'zhipu': {'code': 200, 'data': {'level': 'max', 'limits': [{'type': 'TOKENS_LIMIT', 'unit': 6, 'percentage': 43, 'nextResetTime': future_ms}]}},
            'neurospark': {'totalSpend': 1.32, 'todaySpend': 0.0, 'totalCalls': 64, 'todayCalls': 0},
            'google_billing': {
                'available': False,
                'reason': 'waiting_first_export',
                'dataset': 'billing_export<img src=x onerror="window.__xss_gb=1">',
                'hint': '已连通 · 首份账单生成中（2~24h）',
            },
            'runpod': {'balance': 8.20, 'runningCount': 0, 'podCount': 0, 'pods': []},
            'tikhub': {'balance': 4.88, 'freeCredit': 0, 'active': True},
            'deepseek': {'is_available': False, 'balance_infos': [{'currency': 'CNY', 'total_balance': '0.00'}]},
        }
        errors = []
        async with quota_page(providers, quotas) as page:
            page.on('pageerror', lambda err: errors.append(str(err)))
            await page.locator('#pageTabs button[data-page="quota"]').click()
            await page.wait_for_function("document.querySelectorAll('#providerGrid .card').length === 9")
            self.assertIn('⚠️', await page.locator('#pageBadgeQuota').inner_text())
            grid_text = await page.locator('#providerGrid').inner_text()
            self.assertIn('Google Gemini（付费 API）', grid_text)
            self.assertIn('待首次出账', grid_text)
            self.assertIn('首份账单生成中', grid_text)
            self.assertIsNone(await page.evaluate('window.__xss_gb'))

            # Verify bottom 4 cards share the exact same row (y coordinate) and width
            quarter_cards = page.locator('#providerGrid .card.card-quarter')
            self.assertEqual(await quarter_cards.count(), 4)
            boxes = [await quarter_cards.nth(i).bounding_box() for i in range(4)]
            self.assertTrue(all(boxes))
            for i in range(3):
                self.assertAlmostEqual(boxes[i]['y'], boxes[i + 1]['y'], delta=2.0)
                self.assertLess(boxes[i]['x'], boxes[i + 1]['x'])

            # Switch to populated BigQuery billing data and verify spend + credits
            quotas['google_billing'] = {
                'available': True,
                'dataset': 'billing_export',
                'currency': 'JPY',
                'monthGross': 320.5,
                'monthCredits': 320.5,
                'monthNet': 0.0,
                'todayGross': 12.3,
            }
            await page.evaluate('fetchProviderQuotas()')
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('¥320.50')")
            paid_text = await page.locator('[data-provider-card="google_billing"]').inner_text()
            self.assertIn('本月消耗（JPY）', paid_text)
            self.assertIn('¥320.50', paid_text)
            self.assertIn('赠金抵扣 ¥320.50', paid_text)
            self.assertIn('今日 ¥12.30', paid_text)
            self.assertIn('赠金覆盖', paid_text)
            self.assertEqual(errors, [])

    async def test_ten_card_layout_with_tencent_billing_balance_and_readonly_fallback(self):
        providers = [
            {'id': 'codex', 'label': 'Codex（ChatGPT订阅）', 'baseUrl': 'https://chatgpt.com', 'quotaType': 'codex_usage'},
            {'id': 'antigravity', 'label': 'Antigravity（Google AI 订阅）', 'baseUrl': 'https://antigravity.google', 'quotaType': 'antigravity_quota'},
            {'id': 'kimi', 'label': 'Kimi（Moonshot）', 'baseUrl': 'https://api.kimi.com', 'quotaType': 'kimi_coding_plan'},
            {'id': 'zhipu', 'label': '智谱 BigModel（国内）', 'baseUrl': 'https://open.bigmodel.cn', 'quotaType': 'zhipu_quota'},
            {'id': 'neurospark', 'label': 'NeuroSpark（Claude/GLM中转）', 'baseUrl': 'https://api.neuro-spark.ai', 'quotaType': 'neurospark_usage'},
            {'id': 'google_billing', 'label': 'Google Gemini（付费 API）', 'baseUrl': 'https://console.cloud.google.com/billing', 'quotaType': 'gcp_billing'},
            {'id': 'tencent_billing', 'label': '腾讯云（聚萤科技）', 'baseUrl': 'https://console.cloud.tencent.com/expense', 'quotaType': 'tencent_billing'},
            {'id': 'runpod', 'label': 'RunPod GPU云（数字人口播）', 'baseUrl': 'https://api.runpod.io', 'quotaType': 'runpod_gpu'},
            {'id': 'tikhub', 'label': 'TikHub（社媒数据采集）', 'baseUrl': 'https://api.tikhub.io', 'quotaType': 'tikhub_balance'},
            {'id': 'deepseek', 'label': 'DeepSeek', 'baseUrl': 'https://api.deepseek.com', 'quotaType': 'deepseek_balance'},
        ]
        future_ms = int(time.time() * 1000) + 100_000_000
        quotas = {
            'codex': {'plan': 'prolite', 'allowed': True, 'primaryWindow': {'usedPercent': 19, 'limitWindowSeconds': 604800, 'resetAt': int(time.time()) + 300000}, 'secondaryWindow': None},
            'antigravity': {
                'available': True, 'updatedAt': int(time.time() * 1000), 'ageSec': 5,
                'buckets': [
                    {'id': 'gemini-5h', 'label': 'Gemini 5小时窗口', 'unit': 3, 'usedPercent': 18.4, 'resetAtMs': future_ms, 'disabled': False},
                    {'id': '3p-weekly', 'label': 'Claude/GPT 周额度', 'unit': 6, 'usedPercent': 5.0, 'resetAtMs': future_ms, 'disabled': False},
                ],
                'credits': [],
            },
            'kimi': {'usage': {'limit': '100', 'used': '10', 'resetTime': '2026-12-03T00:00:00Z'}, 'limits': []},
            'zhipu': {'code': 200, 'data': {'level': 'max', 'limits': [{'type': 'TOKENS_LIMIT', 'unit': 6, 'percentage': 10, 'nextResetTime': future_ms}]}},
            'neurospark': {'totalSpend': 1.32, 'todaySpend': 0.0, 'totalCalls': 64, 'todayCalls': 0},
            'google_billing': {'available': False, 'reason': 'waiting_first_export', 'dataset': 'billing_export', 'hint': '已连通 · 首份账单生成中（2~24h）'},
            'tencent_billing': {
                'available': True,
                'month': '2026-11',
                'balanceAvailable': True,
                'balanceCny': 35.3,
                'freezeCny': 0.0,
                'oweCny': 0.0,
                'monthRealBill': 0.0,
                'monthCost': 12.98,
                'products': [{'name': '轻量应用服务器<img src=x onerror="window.__xss_tc=1">', 'cost': 7.05}],
            },
            'runpod': {'balance': 8.20, 'runningCount': 0, 'podCount': 0, 'pods': []},
            'tikhub': {'balance': 4.88, 'freeCredit': 0, 'active': True},
            'deepseek': {'is_available': False, 'balance_infos': [{'currency': 'CNY', 'total_balance': '0.00'}]},
        }
        errors = []
        async with quota_page(providers, quotas) as page:
            page.on('pageerror', lambda err: errors.append(str(err)))
            await page.locator('#pageTabs button[data-page="quota"]').click()
            await page.wait_for_function("document.querySelectorAll('#providerGrid .card').length === 10")
            self.assertEqual(await page.locator('#pageBadgeQuota').inner_text(), '10')
            tc_text = await page.locator('[data-provider-card="tencent_billing"]').inner_text()
            self.assertIn('账户余额（CNY）', tc_text)
            self.assertIn('¥35.30', tc_text)
            self.assertIn('本月成本 ¥12.98', tc_text)
            self.assertIn('余额实时', tc_text)
            self.assertIsNone(await page.evaluate('window.__xss_tc'))

            # Row 3: google_billing + tencent_billing as 2 half-width cards
            half_cards = page.locator('#providerGrid .card.card-half')
            self.assertEqual(await half_cards.count(), 2)
            hb0 = await half_cards.nth(0).bounding_box()
            hb1 = await half_cards.nth(1).bounding_box()
            self.assertAlmostEqual(hb0['y'], hb1['y'], delta=2.0)
            self.assertLess(hb0['x'], hb1['x'])

            # Row 4: runpod, tikhub, deepseek share bottom row
            all_cards = page.locator('#providerGrid .card')
            b_boxes = [await all_cards.nth(idx).bounding_box() for idx in (7, 8, 9)]
            self.assertAlmostEqual(b_boxes[0]['y'], b_boxes[1]['y'], delta=2.0)
            self.assertAlmostEqual(b_boxes[1]['y'], b_boxes[2]['y'], delta=2.0)

            # Fallback state without balance permission shows read-only bill summary cleanly
            quotas['tencent_billing']['balanceAvailable'] = False
            quotas['tencent_billing']['balanceCny'] = None
            await page.evaluate('fetchProviderQuotas()')
            await page.wait_for_function("document.getElementById('providerGrid').innerText.includes('纯只读账单')")
            fb_text = await page.locator('[data-provider-card="tencent_billing"]').inner_text()
            self.assertIn('本月成本（CNY）', fb_text)
            self.assertIn('¥12.98', fb_text)
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
