"""Unit tests for dashboard_projects scanner, parser, privacy guard, and server route."""
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard_projects as dp
import token_dashboard_server as server


class DashboardProjectsParserTests(unittest.TestCase):
    def test_standard_header_statuses_and_body_bullet_isolation(self):
        cases = [
            ('进行中（已上线）', 'active', '进行中'),
            ('进行中（已完成第一阶段，待进入第二阶段）', 'active', '进行中'),
            ('待董事长拍板（已完成初稿）', 'decision', '待拍板'),
            ('暂停中', 'paused', '暂停'),
            ('已完结（归档）', 'done', '已完结'),
        ]
        for raw_status, exp_key, exp_label in cases:
            md = f"""# 示例项目 — 项目状态

- **项目**：测试项目
- **牵头端与负责人**：水母 🪼
- **状态**：{raw_status}
- **最后更新**：2026-11-25 21:30
- **停在哪**：已完成核心模块开发
- **下一步**：提交验收

## 正文细节
- **状态**：正文里的伪字段不应覆盖头部
- **下一步**：不应覆盖头部下一步
"""
            item = dp.parse_project_status_markdown(md, 'demo-dir', 'ZHITAI', mtime_ms=1_700_000_000_000)
            self.assertEqual(item['name'], '测试项目')
            self.assertEqual(item['owner'], '水母 🪼')
            self.assertEqual(item['statusKey'], exp_key)
            self.assertEqual(item['statusLabel'], exp_label)
            self.assertEqual(item['formatMode'], 'standard')
            self.assertEqual(item['stoppedAt'], '已完成核心模块开发')
            self.assertEqual(item['nextStep'], '提交验收')

    def test_legacy_descending_headings_and_markdown_strip(self):
        md = """# 聚光萤看板项目状态

产品定位说明。

## 2026-11-24 两页抓榜与补证优先已安装

11 月 25 日 05:00 起，[小红书看板](https://example.com) 恢复两页抓榜。详细见后续说明。

## 2026-11-23 旧条目
旧内容。
"""
        item = dp.parse_project_status_markdown(md, '聚光萤看板', 'ZHITAI', mtime_ms=1_700_000_000_000)
        self.assertEqual(item['formatMode'], 'legacy')
        self.assertEqual(item['statusKey'], 'legacy')
        self.assertEqual(item['statusLabel'], '纪要式')
        self.assertEqual(item['updatedText'], '2026-11-24')
        self.assertIn('两页抓榜与补证优先已安装', item['stoppedAt'])
        self.assertIn('小红书看板', item['stoppedAt'])
        self.assertNotIn('https://example.com', item['stoppedAt'])

    def test_legacy_ascending_and_init_headings_plus_top_update_prose(self):
        md_asc = """# 项目状态

## 2026-11-15 初始化
- 已确定：由Codex牵头。

## 2026-11-15 开发迁入与本地基线完成
已完成本地基线迁移。下一步等待确认样稿方向。
"""
        item = dp.parse_project_status_markdown(md_asc, 'AI颜值打分', 'ZHITAI', mtime_ms=1_700_000_000_000)
        self.assertEqual(item['owner'], '八爪鱼 / Codex')
        self.assertIn('开发迁入与本地基线完成', item['stoppedAt'])

        md_top = """# 项目状态

更新：2026-11-12。由Codex八爪鱼牵头；已开启业务对话，首轮能力核验完成，等待董事长确认。

## 已完成
完成迁移。
"""
        item_top = dp.parse_project_status_markdown(md_top, 'AI视频自动化剪辑', 'ZHITAI', mtime_ms=1_700_000_000_000)
        self.assertEqual(item_top['owner'], '八爪鱼 / Codex')
        self.assertIn('2026-11-12', item_top['updatedText'])
        self.assertIn('首轮能力核验完成', item_top['stoppedAt'])

        md_mtime = "# 项目状态\n\n只有正文没有日期。\n"
        item_mtime = dp.parse_project_status_markdown(md_mtime, 'no-date', 'ZHITAI', mtime_ms=1_795_613_880_000)
        self.assertTrue(item_mtime['updatedText'].startswith('2026-'))
        self.assertEqual(item_mtime['updatedTsMs'], 1_795_613_880_000)


class DashboardProjectsScannerTests(unittest.TestCase):
    def test_scan_sorting_privacy_exclusions_and_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'codex_project'
            root.mkdir()

            # 1. Decision project (should sort 1st)
            (root / 'proj-decision').mkdir()
            (root / 'proj-decision' / '项目状态.md').write_text(
                '- **项目**：拍板项目\n- **状态**：待董事长拍板\n- **最后更新**：2026-11-24 10:00\n- **停在哪**：方案齐备\n- **下一步**：等批复\n',
                encoding='utf-8',
            )
            # 2. Active newer & older (active sorts after decision, newer first)
            (root / 'proj-active-old').mkdir()
            (root / 'proj-active-old' / '项目状态.md').write_text(
                '- **项目**：老进行中\n- **状态**：进行中\n- **最后更新**：2026-11-24 08:00\n- **停在哪**：步骤1\n- **下一步**：步骤2\n',
                encoding='utf-8',
            )
            (root / 'proj-active-new').mkdir()
            (root / 'proj-active-new' / '项目状态.md').write_text(
                '- **项目**：新进行中\n- **状态**：进行中\n- **最后更新**：2026-11-25 12:00\n- **停在哪**：步骤A\n- **下一步**：步骤B\n',
                encoding='utf-8',
            )
            # 3. Legacy, paused, done
            (root / 'proj-legacy').mkdir()
            (root / 'proj-legacy' / '项目状态.md').write_text('# 项目状态\n\n## 2026-11-25 进展\n普通日志。\n', encoding='utf-8')
            (root / 'proj-paused').mkdir()
            (root / 'proj-paused' / '项目状态.md').write_text('- **项目**：暂停项目\n- **状态**：暂停\n- **最后更新**：2026-11-25 18:00\n', encoding='utf-8')
            (root / 'proj-done').mkdir()
            (root / 'proj-done' / '项目状态.md').write_text('- **项目**：完结项目\n- **状态**：已完结\n- **最后更新**：2026-11-25 19:00\n', encoding='utf-8')

            # Excluded directories & privacy redline
            for bad_name in ('.hidden', 'TMP', 'archive', '虾厂共同记忆', 'workspace-xiaohexia'):
                d = root / bad_name
                d.mkdir()
                (d / '项目状态.md').write_text('- **项目**：不应出现\n- **状态**：进行中\n', encoding='utf-8')

            # Pending rules file with example row + 2 real pending rows + 1 approved row
            rules = Path(tmp) / '待审规则建议.md'
            rules.write_text(
                '| 日期 | 来源端 | 拟改位置 | 建议内容 | 状态 |\n'
                '|---|---|---|---|---|\n'
                '| 2026-11-25 | 水母 | (示例行) | 示例格式 | 示例 |\n'
                '| 2026-11-25 | 水母 | 手册 | 规则1 | 待审 |\n'
                '| 2026-11-25 | 八爪鱼 | SOP | 规则2 | 待审 |\n'
                '| 2026-11-24 | 罗氏虾 | SOP | 规则3 | 已批准 |\n',
                encoding='utf-8',
            )

            missing_root = str(Path(tmp) / 'nonexistent_root')
            forbidden_root = str(Path(tmp) / 'workspace-xiaohexia')
            os.makedirs(forbidden_root, exist_ok=True)
            (Path(forbidden_root) / 'secret' / '项目状态.md').parent.mkdir()
            (Path(forbidden_root) / 'secret' / '项目状态.md').write_text('- **项目**：绝密\n', encoding='utf-8')

            res = dp.scan_active_projects(
                scan_roots=[
                    {'label': 'TEST', 'path': str(root)},
                    {'label': 'MISSING', 'path': missing_root},
                    {'label': 'FORBIDDEN', 'path': forbidden_root},
                ],
                pending_rules_path=str(rules),
                now_ms=1_795_613_880_000,
            )

            names = [p['name'] for p in res['projects']]
            self.assertEqual(names, ['拍板项目', '新进行中', '老进行中', 'proj-legacy', '暂停项目', '完结项目'])
            self.assertEqual(res['counts'], {'total': 6, 'decision': 1, 'active': 2, 'legacy': 1, 'paused': 1, 'done': 1})
            self.assertEqual(res['pendingRules'], {'available': True, 'pendingCount': 2})
            self.assertEqual(res['roots'], [
                {'label': 'TEST', 'available': True, 'count': 6},
                {'label': 'MISSING', 'available': False, 'count': 0},
                {'label': 'FORBIDDEN', 'available': False, 'count': 0},
            ])

    def test_projects_status_http_route(self):
        handler = server.H.__new__(server.H)
        handler.path = '/projects-status?t=99'
        handler.client_address = ('127.0.0.1', 55555)
        handler.headers = {}
        handler.wfile = io.BytesIO()
        captured = {}

        def fake_send_json(status, data, extra_headers=None):
            captured['status'] = status
            captured['data'] = data

        handler._send_json = fake_send_json
        sample_payload = {
            'projects': [{'id': 'ZHITAI:demo', 'name': 'demo'}],
            'counts': {'total': 1, 'decision': 0, 'active': 1, 'legacy': 0, 'paused': 0, 'done': 0},
            'roots': [],
            'pendingRules': {'available': True, 'pendingCount': 0},
            'scannedAt': 1_795_613_880_000,
        }
        with patch.object(server, '_get_projects_status', return_value=sample_payload):
            handler.do_GET()

        self.assertEqual(captured['status'], 200)
        self.assertEqual(captured['data']['counts']['total'], 1)


if __name__ == '__main__':
    unittest.main()
