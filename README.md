# 🦐 Token Dashboard

OpenClaw 多 Agent 的 API 用量监控看板 + Telegram 定时推送。

## 功能

- **Web 看板**：实时显示智谱（海外/国内）、DeepSeek 的配额使用情况 + 各 Agent 的 token 消耗
- **TG 推送**：每 N 小时推送用量报告到 Telegram
- **智能告警**：根据「消耗速度 vs 时间进度」判定安全/偏快/危险/已停

## 架构

```
token_dashboard_server.py   ← HTTP 服务 (端口 18888)，内嵌前端页面
  ├── /quota-overseas     ← 代理智谱海外 API
  ├── /quota-domestic     ← 代理智谱国内 API
  ├── /quota-deepseek     ← 代理 DeepSeek API
  ├── /quota-minimax      ← 代理 MiniMax API
  ├── /quota-kimi         ← 代理 Kimi Coding Plan API
  └── /sessions-json      ← 代理 openclaw sessions

本仓库只包含核心服务端。前端 HTML 和 TG 推送脚本请在部署时按需配置。
```

## 快速开始

### 1. 准备配置文件

```bash
cp config.example.json config.json
# 编辑 config.json，填入你的 API key
```

### 2. 启动看板

```bash
python3 token_dashboard_server.py
# 浏览器访问 http://127.0.0.1:18888/
```

### 3. 配置定时推送

TG 推送脚本不包含在本仓库中。请根据你的环境（systemd / launchctl / cron）自行配置定时调用推送脚本。

## 配置说明

`config.json` 结构：

```json
{
  "zhipu": {
    "overseas_key": "智谱海外(Coding Plan) API Key",
    "domestic_key": "智谱国内 API Key"
  },
  "deepseek_key": "DeepSeek API Key",
  "telegram": {
    "bot_token": "Telegram Bot Token",
    "chat_id": "接收推送的 Chat ID"
  },
  "work_dir": "openclaw scripts 目录，存放 snapshot 和 HTML"
}
```

## Provider 踩坑记录

### Kimi (Moonshot) Coding Plan 额度查询
**2026-06-29 踩坑总结**

Kimi 有两套完全独立的 Key 体系，**不可混用**：

| Key 类型 | 格式示例 | 用途 | 余额/额度查询端点 |
|---------|---------|------|----------------|
| **Coding Plan Key** | `sk-kimi-...` (72字符) | Coding/对话/API调用 | `https://api.kimi.com/coding/v1/usages` |
| **Open Platform Key** | `sk-Hik...` (48字符) | 普通API按量付费 | `https://api.moonshot.cn/v1/users/me/balance` |

**关键坑点**：
1. **余额端点必须用 Coding Plan Key**：`api.moonshot.cn/v1/users/me/balance` 返回的是普通Open Platform余额，Coding Plan用户永远是0
2. **Coding Plan 额度端点**：`api.kimi.com/coding/v1/usages`，返回周额度（7天滚动）+ 5分钟频限
3. **必须带 User-Agent**：`KimiCLI/1.6`，否则 401
4. **代理配置**：`api.kimi.com` 也需要走代理（和 DeepSeek/MiniMax 一样）

**返回数据示例**：
```json
{
  "usage": {"limit": "100", "used": "1", "remaining": "99", "resetTime": "2026-07-06T06:44:44Z"},
  "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"limit": "100", "used": "6", "remaining": "94"}}]
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

### 2026-09-11 系统性维修（工单 tickets/0911-看板系统性维修/，commit 53590ce，+252/-117）

**背景**：issue-0295实时状态不准调查（匡书记9-7发现小河虾typing中看板显示无工作）→根因锁定+只读代码审查发现18项问题（docs/research/代码审查_20260911.md）→匡书记拍板系统性维修。实时状态架构改造同日决策**搁置**（职能分家：实时状态以官方Control UI为准，本看板专注成本趋势）。

**修复内容**：
- 🔴 2确认bug：8.2升级删`sessions`表致两处查询必败被静默吞——R1=0183修复（sqlite真源覆盖）复活、R2=定时任务总览「模型」列恢复真值（采纳B1直取session_windows.model列）
- 🟡 11项风险处置：10修1记（Y1重连sync不清状态/Y3 sessionKey维度/Y4-Y5 stale角标/Y6 token重读/Y7-Y9口径与看门狗修正/Y11转义；Y2=Gateway侧限制记录关闭；Y10=0.0.0.0绑定保守不动仅记录）
- 🔵 6项优化：ps快照复用/cron降频/缓存key降维/限流分桶/文案修正/import提级

**效果实测**：运行中任务updatedAt实时跟进（age=22秒，修复前冻结在spawn时刻）；模型列11/41显示真值；服务重启中断<3秒；对焦虾执行审核PASS（0红3黄）含独立实测复核。

**执行插曲**：首轮分身异常中断（跑完零汇报+服务被杀停约20分钟，CEO 01:34手动救活），续接分身干净完成——见运维issue-0318。

**跨天观察**：次日04:00网关重启窗口验证Y1修复效果（CEO认领）。
