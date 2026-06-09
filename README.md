# 🦐 Token Dashboard

> **匡书记的虾厂出品** | [GitHub](https://github.com/ksiyuna-claw)

OpenClaw 多 Agent 的 API 用量监控看板 + Telegram 定时推送。

## 功能

- **Web 看板**：实时显示智谱（海外/国内）、DeepSeek 的配额使用情况 + 各 Agent 的 token 消耗
- **TG 推送**：每 N 小时推送用量报告到 Telegram
- **智能告警**：根据「消耗速度 vs 时间进度」判定安全/偏快/危险/已停

## 架构

```
token_dashboard_server.py   ← HTTP 服务 (端口 18888)
  ├── 托管 token_dashboard.html 前端
  ├── /quota-overseas     ← 代理智谱海外 API
  ├── /quota-domestic     ← 代理智谱国内 API
  ├── /quota-deepseek     ← 代理 DeepSeek API
  └── /sessions-json      ← 代理 openclaw sessions

token_usage_push.py        ← TG 推送脚本（由 systemd timer 触发）
```

## 快速开始

### 1. 设置环境变量

```bash
export ZHIPU_ZAI_KEY="your-zhipu-overseas-api-key"
export ZHIPU_DOMESTIC_KEY="your-zhipu-domestic-api-key"
export DEEPSEEK_KEY="your-deepseek-api-key"
export TG_BOT_TOKEN="your-telegram-bot-token"
export TG_CHAT_ID="your-telegram-chat-id"
```

### 2. 启动看板

```bash
python3 token_dashboard_server.py
# 浏览器访问 http://127.0.0.1:18888/
```

### 3. 配置定时推送（systemd）

```bash
mkdir -p ~/.config/systemd/user/

# 看板常驻服务
cat > ~/.config/systemd/user/token-dashboard.service << 'EOF'
[Unit]
Description=Token Dashboard
After=network.target

[Service]
Type=simple
Environment=PATH=/home/YOUR_USER/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
ExecStart=/usr/bin/python3 /path/to/token_dashboard_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 推送服务（oneshot）
cat > ~/.config/systemd/user/token-push.service << 'EOF'
[Unit]
Description=Token Push
After=network.target

[Service]
Type=oneshot
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
Environment=PATH=/home/YOUR_USER/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 /path/to/token_usage_push.py
EOF

# 定时器（每6小时）
cat > ~/.config/systemd/user/token-push.timer << 'EOF'
[Unit]
Description=Token Push Timer

[Timer]
OnCalendar=00/6:00
Persistent=false

[Install]
WantedBy=timers.target
EOF

# 启动
systemctl --user daemon-reload
systemctl --user enable --now token-dashboard.service
systemctl --user enable --now token-push.timer
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `ZHIPU_ZAI_KEY` | 智谱海外 Coding Plan API Key (api.z.ai) |
| `ZHIPU_DOMESTIC_KEY` | 智谱国内 API Key (open.bigmodel.cn) |
| `DEEPSEEK_KEY` | DeepSeek API Key |
| `TG_BOT_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | 接收推送的 Telegram Chat ID |

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
