# 看板 Session 状态实时性问题

## 创建时间
2026-06-19

## 问题描述
看板无法准实时反映agent工作状态（工作/等待中）：
- agent实际在工作但看板显示"等待中"
- agent已停止但看板仍显示"工作中"超过2分钟

## 目标
**15秒延迟内准确显示agent状态**（工作/等待中/空闲）

## 当前方案及局限

### 判定逻辑
1. **进程检测**（`_detect_active_process_agents`）：检测agent有没有活跃的exec进程（python/node/ffmpeg）
2. **时间戳兜底**：updatedAt超过120秒未更新 → waiting；超过10分钟 → idle

### 根本局限
| 问题 | 原因 |
|------|------|
| 模型思考时被误判为"等待中" | 模型思考30-90秒不更新时间戳，但agent确实在工作 |
| agent停止后延迟显示 | 120秒阈值意味着最长等120秒+15秒轮询=135秒才更新 |
| 进程检测覆盖不全 | 只能检测exec进程，不能检测"模型正在生成回复"的状态 |

### 为什么不能简单调阈值
- 阈值低（如15秒）→ 模型思考超过15秒就被误判为"等待中"
- 阈值高（如120秒）→ 停止的agent要等2分钟才显示"等待中"
- **时间戳法从根本上无法区分"模型在思考"和"真的停了"**

## 正确的解决方向

### ~~方案A：OpenClaw sessions API 暴露 isRunning 字段~~（不可行）
- 当前 `openclaw sessions --json` 返回的字段里**没有 running/busy 状态**
- 需要OpenClaw上游改代码，不受我们控制

### 方案B：WebSocket 实时事件（✅ 已确认可行，正在修复）
- OpenClaw Gateway有WebSocket事件流（chat/agent/presence/health/heartbeat/cron）
- **根因发现（2026-06-20）**：WS代码只发了 `connect` RPC做认证，**从未调用 `chat.subscribe`**
- OpenClaw文档明确写道：`chat.subscribe → event:"chat"`，不subscribe就不收到chat事件
- `_agent_ws_activity` 字典从上线第一天就是空的，WS维度一直是死功能
- **修复**：在connect RPC成功后追加 `chat.subscribe` 调用
- **预期效果**：subscribe后，任何agent的chat活动（收到消息/输出回复/调工具）都会实时推送事件

### 调研参考（2026-06-20 GitHub调研）
- **AgentOps**（agentops-ai/agentops）：Python SDK hook到执行链，不适用我们的外部观察场景
- **AgentPulse**（jstuart0/agentpulse）：watch session文件mtime变化，思路可靠但比WS慢
- **结论**：OpenClaw自带的WS+subscribe是最快最省资源的方案，修好即可

### 方案C：查询Gateway内部状态（备选）
- 通过Gateway的admin API查询每个agent的run queue状态
- 如果有pending/active run → working；否则 → waiting
- 作为WS方案的降级backup

## 历史改动记录
| 日期 | 改动 | 效果 |
|------|------|------|
| 2026-06-19 | 基围虾修复：快照漏key + 60秒轮询额度数据删除 + Cache-Control头 | 部分改善 |
| 2026-06-19 | 阈值120秒→15秒 | 误判思考中的虾为"等待中"，回滚 |
| 2026-06-19 | 阈值15秒→120秒 | 当前状态，平衡但不够好 |

## 当前状态
**已优化**（2026-06-23）：利用 OpenClaw Gateway 内部的 `hasActiveRun` 信号，实现精确检测。

### 新方案：hasActiveRun 精确信号（2026-06-23）

**根因发现**：`sessions.changed` WS 事件携带 `hasActiveRun: true/false`，直接来自 Gateway 内部的 `chatAbortControllers` — 同一个机制用于向 Telegram 发送 typing indicator。

**信号链路**：
```
用户发消息 → chatAbortController 创建 → sessions.changed(hasActiveRun=true) → Telegram typing ON
模型回复完成 → chatAbortController 删除 → sessions.changed(hasActiveRun=false) → Telegram typing OFF
```

**判定优先级**：
1. `hasActiveRun=true` → working（精确信号）
2. 有活跃 exec 进程 → working
3. WS 90秒内有事件 → working（fallback）
4. updatedAt 90秒内 → working
5. updatedAt 10分钟内 → waiting
6. 超过10分钟 → idle

**安全机制**：5分钟无刷新的 hasActiveRun 条目自动清除（防丢失 stop 事件）

## 历史改动记录
| 日期 | 改动 | 效果 |
|------|------|------|
| 2026-06-19 | 基围虾修复：快照漏key + 60秒轮询额度数据删除 + Cache-Control头 | 部分改善 |
| 2026-06-19 | 阈值120秒→15秒 | 误判思考中的虾为"等待中"，回滚 |
| 2026-06-19 | 阈值15秒→120秒 | 平衡但不够好 |
| 2026-06-19 | 加WS监听（方案A+C） | WS连接成功但从未收到chat事件 |
| 2026-06-20 | CEO调研确认根因：缺少 `chat.subscribe` 调用 | 已修复 |
| 2026-06-23 | WS TTL 10秒→90秒 + updatedAt 90秒 | 覆盖思考窗口但停止检测延迟105秒 |
| 2026-06-23 | 利用 hasActiveRun 精确信号替代超时猜测 | 精确检测，与 Telegram typing 同同步 |

---

## 🔴 升级方向：腾讯云部署 + SSE推送（2026-07-08 匡书记确立）

### 痛点
1. **公司无法访问看板**：看板跑在Mac mini本地（0.0.0.0:18888），公司局域网不通，手机在外面看不到
2. **session状态延迟4分钟+**：当前TTL=5分钟（2026-06-23从90秒回退），subagent完成后看板仍显示"工作中"长达4分钟以上

### 目标架构（方案C：云服务器中转 + SSE）

```
Mac mini (Gateway WS)  →  腾讯云 (看板服务 + SSE)  →  手机/电脑 (公网访问)
      ↑                           ↑                         ↑
  数据源                      中转站                    查看端
```

- **Mac mini → 腾讯云**：WS连接推送状态变化（虾厂已有这套机制）
- **腾讯云 → 手机**：SSE推送（Server-Sent Events，服务器主动推）
- **腾讯云**：公网IP 120.53.15.86，4核4G/3Mbps，足够

### 必须一起做的事
1. **多线程改造**：当前看板是单线程 http.server.HTTPServer，SSE长连接会阻塞所有请求，必须改为 ThreadingHTTPServer
2. **鉴权（加密码登录）**：云服务器是公网的，不加密码=虾厂内部数据裸奔互联网
3. **SSE端点**：新增 /events 端点，收到WS事件时通过SSE广播给前端
4. **连接管理**：Mac mini到云服务器的WS断连重连机制、SSE客户端断开清理

### 对焦虾审核结论（2026-07-08）
- 🔴 当前单线程架构无法直接上SSE，需要先改多线程
- 🔴 当前看板零鉴权，上公网前必须加
- 建议分步执行：先轮询优化治标，再整体迁移到云

### 暂不做的原因
- 凌晨2点赶工改服务器架构风险高
- 需要专门安排时间认真做（基围虾执行 + 对焦虾审）
- 轮询优化方案（TTL缩短+自适应轮询）可以先解决眼下延迟问题

### 记录人
罗氏虾（CEO），2026-07-08 凌晨2点
