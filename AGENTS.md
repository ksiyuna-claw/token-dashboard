# 🦐 Token Dashboard（虾厂运营监控看板）

本项目为虾厂全端 AI 运营监控面板（本地 `http://127.0.0.1:18888/`，公网 `https://token-dashboard.crypto-signal.work/`），现已迁入 `/Volumes/ZHITAI/codex_project/token-dashboard/`。

## 项目归属与协作边界

- **牵头与维护负责人**：**水母 🪼**（Antigravity 侧虾厂总裁），直接向董事长（匡书记）汇报。
- **交付口径**：属水母牵头并负责终验的专属基础设施项目，由水母直接实施、测试与向董事长交付，不进入 OpenClaw 中枢 L0 / 对焦虾审核 / red / settle 流程。
- **OpenClaw 侧分工**：罗氏虾（CEO）与基围虾（CTO）已转为只读使用与外围巡检协作方。端口 `18888`、外网隧道 `token-dashboard.crypto-signal.work`、launchd 服务标签 `com.openclaw.token-dashboard` 与 OpenClaw 侧 `health_check.py` / `token_dashboard_selfheal.sh` 巡检接口保持兼容。
- **安全与隐私红线**：严禁触碰 `/Users/kuangsiyu/.openclaw/workspace-xiaohexia/` 下任何私密文件；严禁将 Antigravity 登录态/OAuth 或 `agy` CLI 包装进第三方 Agent（`antigravity_quota.json` 仅由官方 `agy` statusLine 钩子本地只读落盘）。

## 董事长的长期工作协同偏好

1. **报问题必带解法**：说明卡点并给出最可行方案与下一步。
2. **提建议必标信心指数**：标注百分比信心指数并说明判断依据或最大不确定性。
3. **主线推荐，不做选项剧场**：明确唯一主线推荐，备选仅用于解释为什么不选。
4. **调查、建议、行动三分离**：未获董事长明确批准前，不动 live 配置与服务。
5. **诚实汇报证据**：区分文件存在、代码/单测输出、浏览器实测与服务实际生效。

## 开工与维护铁律

1. **每次新对话开工先读**：
   - 本目录 `项目状态.md`（当前进度、切换点与交接回执）
   - 本目录 `README.md`（产品第一信息源，包含功能全景与架构）
   - [Token看板系统运维_SOP.md](file:///Users/kuangsiyu/.agents/skills/虾厂工作手册/references/运维/Token看板系统运维_SOP.md)（操作面与排障）
2. **文档双同步规矩**：改动功能、页面区块、服务路径或启动配置当天，必须同步更新 `README.md`（功能全景）、`Token看板系统运维_SOP.md`（操作面）及 `CHANGELOG.md`。
3. **后台设施变更申请制**：修改 `~/Library/LaunchAgents/com.openclaw.token-dashboard.plist`、系统 `crontab`、端口或隧道配置前，必须先向董事长申请批准；改完代码后跑通 `unittest` 全量单测并用内置浏览器实测 `http://127.0.0.1:18888/`。
4. **临时文件约定**：本目录独用临时文件放 `/Volumes/ZHITAI/codex_project/token-dashboard/TMP/`，用完及时清理。
5. **「收工」落盘触发**：董事长说「收工」时，按 [共同记忆与收工落盘规范.md](file:///Users/kuangsiyu/.agents/skills/虾厂工作手册/references/通用/共同记忆与收工落盘规范.md) 更新本目录 `项目状态.md`；仅当产生跨项目通用新规矩时，向 `/Volumes/ZHITAI/codex_project/虾厂共同记忆/待审规则建议.md` 追加一行。
