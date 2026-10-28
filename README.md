# 🦐 Token Dashboard — 全厂AI运营监控面板

> 项目说明书（2026-10-11重写：原功能段严重过时且缺区块清单，导致维护者自己都误判过两次——本文件现按「第一信息源」标准维护，**任何功能变动当天必须同步本文件的功能全景段**，规矩同运维SOP「文档同步规矩」段）。

**一句话定位**：OpenClaw全厂多Agent的用量与自动化监控面板——token消耗、供应商额度、邮箱、系统健康、全部定时任务，一个页面看全。

## 访问入口

| 入口 | 地址 | 说明 |
|------|------|------|
| 本机 | http://127.0.0.1:18888/ | launchd服务 `com.openclaw.token-dashboard`（KeepAlive常驻） |
| 公网 | https://token-dashboard.crypto-signal.work/ | cloudflared `quant-dashboard` 隧道（与量化看板家族共用，配置在 `~/.cloudflared/config.yml`）。**传输协议=http2（TCP 443）**，2026-10-16 起长期固定：当日家网持续阻断 UDP 7844（QUIC）致整条隧道 530/1033，降级 http2 后恢复且为官方标准处置；**勿切回 quic**（该网络 UDP 长会话不可靠，且显式钉 quic 会关闭自动降级）。根因分析与排障档案：`ai_workspace/tickets/1016-cloudflared隧道修复/` |
| 手机查数 | TG发「查token」 | 触发 `ai_workspace/scripts/token_usage_push.py` 推送（手动口令模式，无自动调度） |

## 功能全景（页面7区块+额度告警逻辑，2026-10-11 curl实测+代码通读核实）

| 区块 | 内容（代码实锤） | 数据源 |
|------|------|--------|
| 🦐 虾厂员工 | 12虾agent卡片（模型chips+工作状态动画光效）+7个tab：主会话/心跳/子代理/Slash/微信Bot/系统/全部 | `/agent-status`（15秒轮询）+ `/sessions-json` |
| 📦 模型供应商额度 | 智谱国内/Kimi/DeepSeek 额度卡片 + **RunPod GPU云卡片**（2026-10-28上线：余额USD + 活跃pod数/每pod小时成本，服务端额度缓存60s） | `/providers` 列表 + `/quota/<id>` 代理各家官方API（5分钟轮询）；RunPod=GraphQL clientBalance + REST v2 /v2/pods 合成，key在项目config.json（非LLM provider，静态注入，gitignore已忽略） |
| 📦 Hermes 网关健康（2026-10-14 独立成区块，位于邮箱监控之前） | 进程状态 + 心跳 + watchdog触发记录 + **TG通道状态四态**（connected ✅绿 / retrying（Xs未恢复）黄 / 冻结 ⚠️红告警醒目 / unknown ❓灰）；纯读展示零探测逻辑，四态判定全在后端 | `/agent-status` 的 `hermesHealth`（新增 `tg_state`/`tg_state_age` 字段，15秒轮询）；同源 watchdog v5.2（30min双信号自愈：进程+心跳+TG状态）读 `~/.hermes/gateway_state.json` |
| 📬 邮箱监控 | 未读邮件状态 | `email_monitor.py`（系统crontab每30分钟）写 `data/email_status.json` → `/email-status` |
| 🩺 系统健康 | 6卡片：CPU/内存/Swap/磁盘/负载1m5m15m/CDP Chrome（含CDP异常警告条） | `/health-json`（30秒轮询） |
| ⏱ **定时任务**（2026-09-07上线，四层折叠） | ① OpenClaw闹钟表：任务名/Agent/用途/周期/上次执行/状态/下次执行/模型（8列；用途列=静态字典+前缀规则动态生成零维护）② 系统crontab ③ LaunchAgent ④ 心跳表；**内嵌子块**：Gateway健康+Web前端轮询说明表（Hermes网关健康2026-10-14独立成区块上移至邮箱监控之前）；另有独立总览页 `/cron` | `/cron-json` 读 `openclaw cron list` 等（5分钟轮询） |
| 📊 总览 | 6卡片：员工总数/总Session/主会话/子代理/总输入Token/总输出Token | 前端聚合sessions数据 |
| 🖥️ 系统任务 | 系统级任务卡片 | `/system-tasks`（15秒轮询） |

**页面刷新机制**：虾厂状态+系统任务15秒 / 系统健康30秒 / 额度+会话+定时任务5分钟 / 邮箱2分钟（四档轮询，均不消耗token）

**监控覆盖边界（设计如此，非遗漏）**：dashscope（阿里百炼）只跑ASR/声图不走LLM——不监控；火山ark推理key查不了余额——不监控；硅基流动旧余额端点已废弃——暂不监控。

## 架构

```
token_dashboard_server.py   ← HTTP服务 :18888（launchd常驻，项目目录内，2396行）
全部端点（12个，2026-10-11代码实测）：
  /                    ← 主页面（templates/token_dashboard.html，1191行）
  /cron                ← 独立定时任务总览页（cron.html）
  /login /logout       ← 登录/登出（公网访问鉴权）
  /agent-status        ← 虾厂员工状态（15秒轮询）
  /sessions-json       ← 会话数据（代理 openclaw sessions）
  /providers           ← 供应商列表（按openclaw.json动态）
  /quota/<id>          ← 各家额度代理
  /cron-json           ← 定时任务四层数据（cron list+crontab -l+launchctl list+心跳）
  /email-status        ← 邮箱监控状态
  /system-tasks        ← 系统任务（15秒轮询）
  /health-json         ← 系统健康（30秒轮询）

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
