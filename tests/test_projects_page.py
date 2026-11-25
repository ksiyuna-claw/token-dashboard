"""Playwright UI tests for 3 top-level tabs, projects scanner, ZHITAI health card, and email retry resilience."""
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
async def projects_page(projects_payload, refresh_urls, health_state, email_handler, cron_payload=None, sys_tasks=None):
    async with async_playwright() as playwright:
        with tempfile.TemporaryDirectory(prefix='dashboard-projects-chrome-') as profile:
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
                elif url.path == '/projects-status':
                    if 'refresh=1' in url.query:
                        refresh_urls.append(route.request.url)
                    await route.fulfill(json=projects_payload)
                elif url.path == '/providers':
                    await route.fulfill(json=[])
                elif url.path == '/agent-status':
                    now = time.time_ns() // 1_000_000
                    await route.fulfill(json={'now': now, 'lastConfirmedAt': now, 'statusFresh': True, 'agents': []})
                elif url.path == '/sessions-json':
                    await route.fulfill(json={'sessions': []})
                elif url.path == '/health-json':
                    await route.fulfill(json=health_state['data'])
                elif url.path == '/email-status':
                    status, body = email_handler()
                    if status == 200:
                        await route.fulfill(json=body)
                    else:
                        await route.fulfill(status=status, content_type='text/html', body='Bad Gateway')
                elif url.path == '/cron-json':
                    await route.fulfill(json=cron_payload or {'openclawCron': [], 'crontab': [], 'launchAgents': [], 'heartbeats': []})
                elif url.path == '/system-tasks':
                    await route.fulfill(json=sys_tasks or {'tasks': [], 'unknown': []})
                else:
                    await route.fulfill(json={})

            await context.route('**/*', handle)
            try:
                page = context.pages[0]
                await page.goto(ORIGIN, wait_until='domcontentloaded')
                yield page
            finally:
                await context.close()


class ProjectsPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_tabs_zhitai_disk_email_resilience_and_xss(self):
        now_ms = int(time.time() * 1000)
        projects = [
            {
                'id': 'ZHITAI:p1',
                'name': '待拍板测试<img src=x onerror="window.__xss=1">',
                'dirName': 'p1',
                'source': 'ZHITAI',
                'owner': '水母 🪼',
                'statusKey': 'decision',
                'statusLabel': '待拍板',
                'statusDetail': '',
                'formatMode': 'standard',
                'updatedText': '2026-11-25 21:00',
                'updatedTsMs': now_ms - 60_000,
                'stoppedAt': '已完成方案A<script>window.__xss=2</script>',
                'nextStep': '等董事长确认',
            },
            {
                'id': 'ZHITAI:p2',
                'name': 'Token 看板',
                'dirName': 'token-dashboard',
                'source': 'ZHITAI',
                'owner': '水母 🪼',
                'statusKey': 'active',
                'statusLabel': '进行中',
                'statusDetail': '',
                'formatMode': 'standard',
                'updatedText': '2026-11-25 21:30',
                'updatedTsMs': now_ms - 30_000,
                'stoppedAt': '第一阶段切换完成',
                'nextStep': '开发扫盘板块',
            },
            {
                'id': 'ZHITAI:p3',
                'name': 'OpenClaw 运维',
                'dirName': 'openclaw运维',
                'source': 'ZHITAI',
                'owner': 'Antigravity',
                'statusKey': 'active',
                'statusLabel': '进行中',
                'statusDetail': '',
                'formatMode': 'standard',
                'updatedText': '2026-11-25 20:00',
                'updatedTsMs': now_ms - 120_000,
                'stoppedAt': '基础建档完成',
                'nextStep': '承接首单',
            },
            {
                'id': 'ZHITAI:p4',
                'name': '聚光萤看板',
                'dirName': '聚光萤看板',
                'source': 'ZHITAI',
                'owner': '',
                'statusKey': 'legacy',
                'statusLabel': '纪要式',
                'statusDetail': '',
                'formatMode': 'legacy',
                'updatedText': '2026-11-24',
                'updatedTsMs': now_ms - 86_400_000,
                'stoppedAt': '两页抓榜与补证优先已安装',
                'nextStep': '',
            },
            {
                'id': 'ZHITAI:p5',
                'name': '景合AIGC合作',
                'dirName': '景合AIGC合作',
                'source': 'ZHITAI',
                'owner': '',
                'statusKey': 'legacy',
                'statusLabel': '纪要式',
                'statusDetail': '',
                'formatMode': 'legacy',
                'updatedText': '2026-11-21',
                'updatedTsMs': now_ms - 4 * 86_400_000,
                'stoppedAt': '通用资料库交接',
                'nextStep': '',
            },
            {
                'id': 'ZHITAI:p6',
                'name': '暂停试点',
                'dirName': 'p6',
                'source': 'ZHITAI',
                'owner': '八爪鱼 🐙',
                'statusKey': 'paused',
                'statusLabel': '暂停',
                'statusDetail': '',
                'formatMode': 'standard',
                'updatedText': '2026-11-20',
                'updatedTsMs': now_ms - 5 * 86_400_000,
                'stoppedAt': '等待外部资料',
                'nextStep': '',
            },
            {
                'id': 'ZHITAI:p7',
                'name': '已完结归档项目',
                'dirName': 'p7',
                'source': 'ZHITAI',
                'owner': '水母 🪼',
                'statusKey': 'done',
                'statusLabel': '已完结',
                'statusDetail': '',
                'formatMode': 'standard',
                'updatedText': '2026-11-19',
                'updatedTsMs': now_ms - 6 * 86_400_000,
                'stoppedAt': '已归档交付',
                'nextStep': '',
            },
        ]
        payload = {
            'projects': projects,
            'counts': {'total': 7, 'decision': 1, 'active': 2, 'legacy': 2, 'paused': 1, 'done': 1},
            'roots': [{'label': 'ZHITAI', 'available': True, 'count': 7}],
            'pendingRules': {'available': True, 'pendingCount': 3},
            'scannedAt': now_ms,
        }
        health_state = {
            'data': {
                'cpu_pct': 5.0,
                'mem_used_gb': 4.0,
                'mem_total_gb': 16.0,
                'mem_pct': 25.0,
                'disk_pct': 40,
                'zhitai_mounted': True,
                'zhitai_total_gb': 954.0,
                'zhitai_used_gb': 299.0,
                'zhitai_free_gb': 655.0,
                'zhitai_pct': 32,
            }
        }
        email_calls = [0]

        def handle_email():
            email_calls[0] += 1
            if email_calls[0] == 1:
                return 200, {'unread_count': 1, 'today_count': 2, 'new_count': 0, 'last_check': '2026-11-25T22:00:00+08:00', 'recent_new': []}
            return 502, None

        cron_payload = {
            'openclawCron': [{
                'id': 'j1', 'name': '故障测试任务', 'agentId': 'main', 'purpose': '测试', 'enabled': True,
                'schedule': '0 * * * *', 'scheduleHuman': '每小时', 'lastRunStatus': 'error',
                'lastRunAtMs': None, 'nextRunAtMs': None, 'consecutiveErrors': 2,
            }],
            'crontab': [],
            'launchAgents': [],
            'heartbeats': [],
        }
        sys_tasks = {
            'tasks': [{'name': '智泰硬盘全量备份', 'owner': '基围虾/运维', 'period': '每日07:30', 'running': False, 'last': None}],
            'unknown': [],
        }

        errors = []
        refresh_urls = []
        async with projects_page(payload, refresh_urls, health_state, handle_email, cron_payload=cron_payload, sys_tasks=sys_tasks) as page:
            page.on('pageerror', lambda err: errors.append(str(err)))
            await page.wait_for_function("document.getElementById('pageBadgeProjects').textContent === '7'")
            await page.wait_for_function("document.getElementById('pageBadgeOps').textContent === '❌1'")

            # Default tab is 运营监控; obsolete #summaryGrid removed; ZHITAI card rendered
            self.assertEqual(await page.locator('#summaryGrid').count(), 0)
            self.assertTrue(await page.locator('#page-monitor').is_visible())
            self.assertFalse(await page.locator('#page-projects').is_visible())
            self.assertFalse(await page.locator('#page-ops').is_visible())
            self.assertEqual(await page.locator('#hZhitai').inner_text(), '32%')

            # Unmounted ZHITAI state shows ❌ 未挂载
            health_state['data']['zhitai_mounted'] = False
            health_state['data']['zhitai_pct'] = None
            await page.evaluate('fetchHealth()')
            await page.wait_for_function("document.getElementById('hZhitai').textContent.includes('未挂载')")

            # Transient email 502 retains last-good card HTML instead of wiping to red error
            await page.wait_for_function("document.getElementById('emailCard').innerText.includes('未读邮件')")
            await page.evaluate('fetchEmailStatus()')
            await page.wait_for_function("document.getElementById('emailSummary').innerText.includes('15秒后自动重试')")
            email_card_text = await page.locator('#emailCard').inner_text()
            self.assertIn('未读邮件', email_card_text)
            self.assertNotIn('获取失败', email_card_text)

            # Switch to 活跃项目 tab: all 7 cards render immediately
            await page.locator('#pageTabs button[data-page="projects"]').click()
            self.assertFalse(await page.locator('#page-monitor').is_visible())
            self.assertTrue(await page.locator('#page-projects').is_visible())
            await page.wait_for_function("document.querySelectorAll('#projectGrid .project-card').length === 7")

            await page.locator('#projectTabs button[data-pfilter="active"]').click()
            await page.wait_for_function("document.querySelectorAll('#projectGrid .project-card').length === 2")
            await page.locator('#btnRefreshProjects').click()
            await page.wait_for_function("document.getElementById('btnRefreshProjects').disabled === false")
            self.assertEqual(len(refresh_urls), 1)

            # Switch to 运维与任务 tab: cron & system tasks visible
            await page.locator('#pageTabs button[data-page="ops"]').click()
            self.assertFalse(await page.locator('#page-projects').is_visible())
            self.assertTrue(await page.locator('#page-ops').is_visible())
            self.assertIn('智泰硬盘全量备份', await page.locator('#systemTaskCard').inner_text())

            # Switch back to 运营监控 tab
            await page.locator('#pageTabs button[data-page="monitor"]').click()
            self.assertTrue(await page.locator('#page-monitor').is_visible())
            self.assertFalse(await page.locator('#page-ops').is_visible())

            self.assertIsNone(await page.evaluate('window.__xss'))
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
