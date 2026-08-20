# ADR-0004: WS 实时监听 Gateway 事件

## 日期
2026-06-20

## 状态
已采纳

## 背景
看板需要实时知道各虾状态。最初方案是每 15 秒 spawn `openclaw` CLI 查询，但太慢（进程启动开销）太重（频繁创建销毁进程），无法满足实时性要求。

## 决策
后台线程连接 Gateway WebSocket（`ws://127.0.0.1:18789/`），订阅 sessions 事件，维护 `_agent_active_runs` 和 `_agent_ws_activity` 两个内存字典。

## 规则细节
- WS 端点：`ws://127.0.0.1:18789/`
- `sessions.changed` 事件 → 更新 hasActiveRun 状态（写入 `_agent_active_runs`）
- 其他非 `health` 事件 → 更新 WS 活动时间戳（写入 `_agent_ws_activity`）
- 断线自动重连：10 秒间隔
- 重连后调 `sessions.list` 重建完整状态

## 决策人
基围虾（CTO）

## 备注
- WS 方案将状态延迟从 15 秒降低到近实时
- 内存字典是进程级状态，看板重启后需重连重建
