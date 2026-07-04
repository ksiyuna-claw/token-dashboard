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

## License

MIT
