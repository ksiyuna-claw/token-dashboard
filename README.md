# 🦐 Token Dashboard — 全厂AI运营监控面板

## 修改前先读

员工工作状态的原始要求与现行认定办法见[本人、分身和后台程序](docs/decisions/0007-员工工作状态.md)。

额度板块的布局曾因实看效果撤回；改版时对照董事长的原意、撤回原因和当次验证。

[变更说明_20261123_额度板块布局调整.md](<变更说明_20261123_额度板块布局调整.md>) · [CHANGELOG.md](<CHANGELOG.md>)

2026-11-24 补上上述入口，便于追溯设计取舍。以后实质改动在本产品的变更记录中追加日期、原因、验证结果及必要的还原位置。

> 项目说明书（2026-10-11重写：原功能段严重过时且缺区块清单，导致维护者自己都误判过两次——本文件现按「第一信息源」标准维护，**任何功能变动当天必须同步本文件的功能全景段**，规矩同运维SOP「文档同步规矩」段）。

**一句话定位**：OpenClaw全厂多Agent的用量与自动化监控面板——token消耗、供应商额度、邮箱、系统健康、全部定时任务，一个页面看全。

- **现役生产目录**：`/Volumes/ZHITAI/codex_project/token-dashboard/`（2026-11-25 自 `~/.openclaw/workspace/ai_workspace/projects/token-dashboard/` 迁入）
- **维护权属**：**水母 🪼**（Antigravity 侧）牵头主维护与交付；OpenClaw 侧（罗氏虾 / 基围虾）转为只读使用与外围巡检协作方。

## 访问入口

| 入口 | 地址 | 说明 |
|------|------|------|
| 本机 | http://127.0.0.1:18888/ | launchd服务 `com.openclaw.token-dashboard`（KeepAlive常驻） |
| 公网 | https://token-dashboard.crypto-signal.work/ | cloudflared `quant-dashboard` 隧道（与量化看板家族共用，配置在 `~/.cloudflared/config.yml`）。**传输协议=http2（TCP 443）**，2026-10-16 起长期固定：当日家网持续阻断 UDP 7844（QUIC）致整条隧道 530/1033，降级 http2 后恢复且为官方标准处置；**勿切回 quic**（该网络 UDP 长会话不可靠，且显式钉 quic 会关闭自动降级）。根因分析与排障档案：`ai_workspace/tickets/1016-cloudflared隧道修复/` |
| 手机查数 | TG发「查token」 | 触发 `ai_workspace/scripts/token_usage_push.py` 推送（手动口令模式，无自动调度） |

## 功能全景（3大页卡7大区块+额度告警逻辑，2026-11-26 实测核实）

页面顶部为 3 个可切换页卡（支持 `#monitor` / `#projects` / `#ops` 与角标异常透传）：**`📊 运营监控`**（默认首页）、**`📂 活跃项目`**、**`⚙️ 运维与任务`**。

| 区块（所属页卡） | 内容（代码实锤） | 数据源 |
|------|------|--------|
| 🦐 虾厂员工（运营监控） | 13 位员工卡片。分别显示本人、分身或后台程序工作、等待审核／云端结果、空闲或状态待确认，保留模型标签+7个tab：主会话/心跳/子代理/Slash/微信Bot/系统/全部 | `/agent-status`（15秒轮询）+ `/sessions-json` |
| 📦 模型供应商额度（运营监控） | Codex / **Antigravity（2026-11-25上线、11-27 升级跨 2 列双栏宽卡：左 Gemini 5h/周、右 Claude·GPT 5h/周，与其余 7 卡排成 3×3 满格；右上角支持「🔄 刷新」触发零对话 `agy models` 实时同步，含时间戳校验与竞态自动重试）** / Kimi / 智谱国内 / NeuroSpark / RunPod / TikHub / DeepSeek（尾位）共 8 张卡 | `/providers` 列表 + `/quota/<id>` 由独立模块 `dashboard_quota.py` 统一代理与缓存（60s TTL+旧缓存兜底）。Codex=读 `~/.codex/auth.json` 调 ChatGPT 后端；**Antigravity=纯只读 `data/antigravity_quota.json` 本地脱敏快照（由官方 `agy` CLI `statusLine` 钩子自动落盘；点击卡片刷新按钮仅调官方零对话 `agy models` 子命令并带单飞锁、时间戳真值核验、最多 3 轮竞态自动重试与成功后再 15s 冷却，不碰 Google OAuth，界面标注采集时间及 AI Credits）**；RunPod/TikHub=`config.json`；NeuroSpark=`openclaw.json`+`curl_cffi` |
| 📦 Hermes 网关健康（运营监控） | 进程状态 + 心跳 + watchdog触发记录 + **TG通道状态四态**（connected ✅绿 / retrying（Xs未恢复）黄 / 冻结 ⚠️红告警醒目 / unknown ❓灰）；纯读展示零探测逻辑，四态判定全在后端 | `/agent-status` 的 `hermesHealth`（新增 `tg_state`/`tg_state_age` 字段，15秒轮询）；同源 watchdog v5.2（30min双信号自愈：进程+心跳+TG状态）读 `~/.hermes/gateway_state.json` |
| 📬 邮箱监控（运营监控） | 未读邮件与今日收件状态；瞬时抖动/重启时保留上次正常数据并 15 秒自动重试，登录过期直报提示 | `email_monitor.py`（系统crontab每30分钟）写 `data/email_status.json` → `/email-status`（2分钟轮询） |
| 🩺 系统健康（运营监控） | **7 卡片**：CPU / 内存 / Swap / 内置磁盘已用 / **ZHITAI 外置盘（容量与挂载状态）** / 负载 1m5m15m / CDP Chrome（含CDP异常警告条） | `/health-json`（30秒轮询） |
| 📂 活跃项目进度（独立页卡 `#projects`） | **（2026-11-25上线）** 只读扫盘展示 ZHITAI `/Volumes/ZHITAI/codex_project/*` 与 OpenClaw `~/.openclaw/workspace/ai_workspace/projects/*` 各项目 `项目状态.md`（兼容标准 6 项头部与旧式纪要回退），支持全部/进行中/待拍板/纪要式/暂停·完结筛选、`🔄 刷新扫盘` 与共享 `待审规则建议.md` 计数徽标 | `/projects-status`（2分钟轮询，`?refresh=1` 强制重扫；后端 `dashboard_projects.py` + 30s TTL 缓存，严格排除 `workspace-xiaohexia`） |
| ⏱ 定时任务（运维与任务 `#ops`） | ① OpenClaw闹钟表（8列；静态字典+前缀规则动态生成，全量用途补全）② 系统crontab ③ LaunchAgent（含 `com.openclaw.*`/`com.shrimp.*`/`local.jgy-*`/`ai.*` 共20项）④ 心跳表；**内嵌子块**：Gateway健康+Web前端轮询说明表；另有独立总览页 `/cron` | `/cron-json` 读 `openclaw cron list` 等（5分钟轮询） |
| 🖥️ 系统任务（运维与任务 `#ops`） | 16 项现役非 Gateway 衍生定时脚本与 launchd 常驻服务监控（虾厂运维巡检/清理/轮转/云端两层备份/定时重启/周日清理/智泰观察期备份/邮件/聚光萤/语音代理/国学 bot） | `/system-tasks`（15秒轮询） |

**页面刷新机制**：虾厂状态+系统任务15秒 / 系统健康30秒 / 活跃项目扫盘+邮箱2分钟（邮箱瞬时失败15秒自动重试） / 额度+会话+定时任务5分钟（Antigravity 支持按钮实时同步，均不消耗LLM对话token）

**监控覆盖边界（设计如此，非遗漏）**：dashscope（阿里百炼）只跑ASR/声图不走LLM——不监控；火山ark推理key查不了余额——不监控；硅基流动旧余额端点已废弃——暂不监控。

## 架构

```
token_dashboard_server.py   ← HTTP服务 :18888（launchd常驻，项目目录内）
dashboard_quota.py          ← 供应商发现、60s缓存兜底、各家额度代理与 Antigravity 本地快照适配器
dashboard_projects.py       ← 活跃项目 项目状态.md 只读扫盘解析与待审规则计数
dashboard_activity.py / dashboard_background.py / dashboard_status.py ← 员工工作状态实时采集三件套
全部端点（14个）：
  /                          ← 主页面（templates/token_dashboard.html，含「📊 运营监控」「📂 活跃项目」「⚙️ 运维与任务」3页卡）
  /cron                      ← 独立定时任务总览页（cron.html）
  /login /logout             ← 登录/登出（公网访问鉴权）
  /agent-status              ← 虾厂员工状态（15秒轮询）
  /sessions-json             ← 会话数据（代理 openclaw sessions）
  /providers                 ← 供应商列表（按openclaw.json动态）
  /quota/<id>                ← 各家额度代理
  POST /quota/antigravity/refresh ← 手动触发官方零对话 `agy models` 实时刷新本地配额快照（15s 冷却）
  /projects-status           ← 活跃项目进度只读扫盘（2分钟轮询，30s缓存，`?refresh=1` 即时重扫）
  /cron-json           ← 定时任务四层数据（cron list+crontab -l+launchctl list+心跳）
  /email-status        ← 邮箱监控状态
  /system-tasks        ← 系统任务（15秒轮询）
  /health-json         ← 系统健康（30秒轮询）
```

token_usage_push.py        ← TG「查token」推送脚本（scripts/目录，手动口令触发）
email_monitor.py           ← 邮件监听（crontab每30分钟）
token_snapshot.json        ← 快照缓存（scripts/目录，断电恢复用）
```

## 快速开始（本厂部署向）

- 服务由 launchd 管理：`launchctl print gui/$(id -u)/com.openclaw.token-dashboard`
- 重启服务：`launchctl kickstart -k gui/$(id -u)/com.openclaw.token-dashboard`
- 日常运维操作/排障：见运维SOP（虾厂工作手册 → references/运维/Token看板系统运维_SOP.md）

## Provider 踩坑记录

### Kimi (Moonshot) Coding Plan 额度查询
**2026-06-29 踩坑总结**（🔴 2026-10-11注：这段是宝——当天有人重新挖此端点花了一小时，答案一直在这里）

Kimi 有两套完全独立的 Key 体系，**不可混用**：

| Key 类型 | 格式示例 | 用途 | 余额/额度查询端点 |
|---------|---------|------|------------------|
| **Coding Plan Key** | `sk-kimi-...` (72字符) | Coding/对话/API调用 | `https://api.kimi.com/coding/v1/usages` |
| **Open Platform Key** | `sk-Hik...` (48字符) | 普通API按量付费 | `https://api.moonshot.cn/v1/users/me/balance` |

**关键坑点**：
1. **余额端点必须用 Coding Plan Key**：`api.moonshot.cn` 返回的是普通Open Platform余额，Coding Plan用户永远是0/认证不通
2. **Coding Plan 额度端点**：`api.kimi.com/coding/v1/usages`，返回周额度（7天滚动）+ 5分钟频限 + 加油包余额
3. **历史坑：必须带 User-Agent `KimiCLI/1.6`，否则 401**（2026-10-11实测已放宽，裸curl可通；若再遇401先加UA）
4. **代理配置**：`api.kimi.com` 需走代理（同 DeepSeek/MiniMax）

**返回数据示例**：
```json
{
  "usage": {"limit": "100", "used": "22", "remaining": "78", "resetTime": "2026-10-14T00:35:40Z"},
  "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"limit": "100", "used": "7", "remaining": "93"}}],
  "booster_wallet": {"...": "加油包余额"}
}
```

---

## 告警逻辑

| 消耗/时间比 | 状态 |
|------------|------|
| < 0.5 | 🔵 观察中 |
| 0.5 ~ 1.0 | 🟢 安全 |
| 1.0 ~ 1.5 | 🟡 偏快 |
| > 1.5 | 🔴 危险 |
| 100% | 🔴 已停 |

> 周期开始 < 10 分钟内不判断速度，显示 🔵 观察中。

## 依赖

- Python 3.8+
- `openclaw` CLI（需在 PATH 中）
- 访问海外 API 需配置 HTTP 代理

## 外部服务清单

详见 `docs/design/产品说明书.md` 中的「外部服务清单」章节。

当前运行中的外部服务：
- **聚光萤腾讯云服务器** `120.53.15.86` — H5落地页 + API + MongoDB（HTTP）
- **聚光萤 GitHub CI/CD** — `juguangying-mini`（小程序前端）+ `juguangying-landing`（Flask落地页）
- **聚光萤定时采集** — 全量采集(05:00) + RSS资讯采集(每小时)
- **聚光萤健康监测** — 每30分钟远程检测，异常 TG 告警

已下线：
- ⚠️ `ai-film.crypto-signal.work`（2026-07-09 下线，迁至腾讯云）

## License

MIT

## 维护历史（重大维修记录）

### 2026-10-11 文档双升级（匡书记口令「升级迭代一下sop和该项目readme」）
**README重写为项目说明书**（原功能段停在远古版本只写智谱+DeepSeek，定位是模板化部署说明；当日因「定时任务板块存不存在」「Kimi可不可监控」连续误判——知识都在但埋在踩坑记录里、顶部概览从不更新）。同时**运维SOP联动升级**：立文档分工（README=功能第一信息源/SOP=只管操作排障）、架构图补9-07欠账（/cron-json+公网入口）、踩坑第9条（同型再犯复盘）、双同步规矩（功能变动当天README功能全景+SOP操作面任一不动=没同步）。

### 2026-09-11 系统性维修（工单 tickets/0911-看板系统性维修/，commit 53590ce，+252/-117）

**背景**：issue-0295实时状态不准调查（匡书记9-7发现小河虾typing中看板显示无工作）→根因锁定+只读代码审查发现18项问题（docs/research/代码审查_20260911.md）→匡书记拍板系统性维修。实时状态架构改造同日决策**搁置**（职能分家：实时状态以官方Control UI为准，本看板专注成本趋势）。

**修复内容**：
- 🔴 2确认bug：8.2升级删`sessions`表致两处查询必败被静默吞——R1=0183修复（sqlite真源覆盖）复活、R2=定时任务总览「模型」列恢复真值（采纳B1直取session_windows.model列）
- 🟡 11项风险处置：10修1记（Y1重连sync不清状态/Y3 sessionKey维度/Y4-Y5 stale角标/Y6 token重读/Y7-Y9口径与看门狗修正/Y11转义；Y2=Gateway侧限制记录关闭；Y10=0.0.0.0绑定保守不动仅记录）
- 🔵 6项优化：ps快照复用/cron降频/缓存key降维/限流分桶/文案修正/import提级

**效果实测**：运行中任务updatedAt实时跟进（age=22秒，修复前冻结在spawn时刻）；模型列11/41显示真值；服务重启中断<3秒；对焦虾执行审核PASS（0红3黄）含独立实测复核。

**执行插曲**：首轮分身异常中断（跑完零汇报+服务被杀停约20分钟，CEO 01:34手动救活），续接分身干净完成——见运维issue-0318。

**跨天观察**：次日04:00网关重启窗口验证Y1修复效果（CEO认领）。
