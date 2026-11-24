"""在独立 Chrome 会话验证员工状态。所有页面请求都在浏览器测试内拦截。"""
import asyncio
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
async def dashboard_page(status_request):
    """status_request 接收 Playwright route；其它端点只提供隔离数据。"""
    async with async_playwright() as playwright:
        with tempfile.TemporaryDirectory(prefix='dashboard-chrome-') as profile:
            context = await playwright.chromium.launch_persistent_context(
                profile, channel='chrome', headless=True, service_workers='block',
                args=['--disable-background-networking', '--no-first-run'],
                viewport={'width': 1280, 'height': 860},
            )
            async def handle(route):
                path = urlsplit(route.request.url)
                if path.netloc != 'token-dashboard.test':
                    await route.abort()
                elif path.path == '/':
                    await route.fulfill(content_type='text/html; charset=utf-8', body=TEMPLATE.read_bytes())
                elif path.path == '/agent-status':
                    await status_request(route)
                elif path.path == '/providers':
                    await route.fulfill(json=[])
                elif path.path == '/sessions-json':
                    await route.fulfill(json={'sessions': []})
                elif path.path == '/health-json':
                    await route.fulfill(json={'mem_used_gb': 0, 'mem_total_gb': 16})
                else:
                    await route.fulfill(json={})

            await context.route('**/*', handle)
            try:
                page = context.pages[0]
                await page.goto(ORIGIN, wait_until='domcontentloaded')
                yield page
            finally:
                await context.close()


class ActivityPage(unittest.IsolatedAsyncioTestCase):
    async def test_employee_cards(self):
        gate = asyncio.Event()
        errors = []
        mode = 'normal'
        data = {}
        async def respond(route):
            await gate.wait()
            if mode == 'failure':
                await route.fulfill(status=502, body='upstream unavailable')
            elif mode == 'timeout':
                await route.abort('timedout')
            elif mode == 'cached':
                await route.fulfill(headers={'Age':'90'}, json=data)
            else:
                await route.fulfill(json=data)

        async with dashboard_page(respond) as page:
            page.on('pageerror', lambda err: errors.append(str(err)))
            await page.wait_for_selector('.agent-card')
            summary = page.locator('#agentStatusSummary')
            card = page.locator('[data-agent="jiweixia"]')
            steward = page.locator('[data-agent="main"]')
            agents = await page.evaluate('AGENT_ORDER')
            now = time.time_ns() // 1_000_000
            rows = {aid: dict(agentId=aid, status='idle', activityKind='none',
                              activityStale=False, confirmedAt=now, subagentActiveCount=0)
                    for aid in agents}
            data.update(now=now, lastConfirmedAt=now, statusFresh=True, agents=list(rows.values()))
            self.assertEqual(await summary.inner_text(), '正在读取')
            self.assertNotIn('空闲', await card.inner_text())
            gate.set()
            await page.wait_for_function("document.getElementById('agentStatusSummary').textContent === '全部空闲'")

            async def poll():
                data['now'] += 1
                await page.evaluate('fetchAgentStatus()')

            cto = rows['jiweixia']
            cto.update(status='working', activityKind='subagent', subagentActiveCount=2)
            await poll()
            self.assertEqual(await summary.inner_text(), '1只虾在工作')
            self.assertIn('分身执行', await card.inner_text())
            self.assertIn('2个分身在跑', await card.inner_text())
            self.assertIn('空闲', await steward.locator('.ac-activity').inner_text())
            await page.locator('[data-filter="subagent"]').click()
            self.assertEqual(await summary.inner_text(), '1只虾在工作')
            self.assertNotIn('无活跃会话', await card.inner_text())

            cto.update(activityKind='background', viaSubagent=True, subagentActiveCount=1)
            await poll()
            self.assertIn('工作中 · 分身的后台程序运行中', await card.inner_text())
            self.assertEqual(await card.get_attribute('data-model-active'), 'false')

            cto.update(status='waiting', activityKind='review', subagentActiveCount=7)
            await poll()
            self.assertEqual(await summary.inner_text(), '1只在等待')
            self.assertIn('等对焦虾审核', await card.inner_text())
            self.assertNotIn('个分身在跑', await card.inner_text())
            cto.update(activityKind='submitted')
            await poll()
            self.assertIn('已提交，等结果', await card.inner_text())
            cto.update(status='working', activityKind='remote')
            await poll()
            self.assertIn('云端任务运行中', await card.inner_text())
            cto.update(activityStale=True)
            await poll()
            self.assertIn('上次确认', await card.inner_text())
            self.assertEqual(await summary.inner_text(), '1只状态待确认')
            cto.update(activityStale=False)

            data['statusFresh'] = False
            rows['pipixia']['activityStale'] = True
            await poll()
            self.assertEqual(await summary.inner_text(), '1只虾在工作 · 1只状态待确认')
            self.assertEqual(await steward.locator('.ac-activity').inner_text(), '空闲')
            rows['pipixia']['activityStale'] = False
            data['statusFresh'] = True
            data['agents'] = [cto]
            await poll()
            self.assertEqual(await summary.inner_text(), f'1只虾在工作 · {len(rows)-1}只状态待确认')
            self.assertIn('上次确认', await steward.locator('.ac-activity').inner_text())
            del cto['confirmedAt']
            await poll()
            self.assertNotIn('只虾在工作', await summary.inner_text())
            self.assertIn('上次确认', await card.locator('.ac-activity').inner_text())
            cto.update(confirmedAt=now)
            del cto['activityKind']
            await poll()
            self.assertNotIn('只虾在工作', await summary.inner_text())
            cto.update(activityKind='remote')
            data['agents'] = list(rows.values())
            await poll()
            original_time = data['now']
            data['now'] -= 2
            cto.update(status='idle', activityKind='none')
            await poll()
            self.assertIn('云端任务运行中', await card.inner_text(), '旧快照不能覆盖新快照')
            data['now'] = original_time

            for failure in ['failure', 'timeout', 'cached']:
                mode = failure
                await poll()
                self.assertNotIn('空闲', await summary.inner_text())
                self.assertIn('上次确认', await page.locator('#statusUpdate').inner_text())
                self.assertFalse(await page.locator('.agent-card-working').count())
                mode = 'normal'
                await poll()
                self.assertEqual(await summary.inner_text(), '全部空闲')
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
