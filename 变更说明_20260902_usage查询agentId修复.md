# 变更说明：usage查询agentId报错调查（第二轮）

> 项目：token-dashboard ｜ 执行：基围虾(CTO) ｜ 日期：2026-09-03
> 任务：修复Token看板usage查询报错（第二轮，含第一轮调查结论修正）

## TL;DR

**报错真实存在，但发起方不是看板代码**。gateway.log 中 `usage.cost` 的
"session key main has no explicit owner" 报错来自 **OpenClaw 官方 App 客户端**
（经 relay tunnel 转发），该客户端查用量页面时漏传 `agentId` 参数。
看板（token_dashboard_server.py）**没有任何 usage.cost 调用**，本轮对看板代码
**零改动**——第一轮"修复必须看板代码内传agentId"的结论不成立，予以修正。

## 一、第一轮结论回顾

- 配置层开关 `agents.defaults.sessionStore.agentId` 试过无效，openclaw.json 已逐字节还原
- 第一轮判断：token_dashboard_server.py 调 gateway WS 接口（usage.cost 等）时用裸
  session key "main"，需显式传 agentId

## 二、第二轮调查过程（证据链）

### 2.1 gateway.log 报错定位

```
[ws] ⇄ res ✗ usage.cost 33ms errorCode=INVALID_REQUEST
errorMessage=Multiple agents are configured, but session key "main" has no
explicit owner. Pass agentId or use an agent-prefixed session key.
conn=732031ff…22f6 id=relay-186
```

完整请求（gateway.log 58058 行）：

```json
{"type":"req","id":"relay-186","method":"usage.cost",
 "params":{"mode":"specific","endDate":"2026-09-03","startDate":"2026-09-03","utcOffset":"UTC+8"}}
```

- `id=relay-*`：请求经 **Relay tunnel**（gateway 插件，连 `wss://openclaw-service.yoooclaw.com`）转发
- params 中 **没有 sessionKey、没有 agentId**
- 33 次报错全部走 relay 通道（09-02 21:28 ~ 09-03 10:26）

### 2.2 同批流量分析（确认客户端身份）

同一秒内同一连接的相邻请求：

| id | method | 关键参数 | 是否带agent归属 |
|----|--------|----------|----------------|
| relay-182 | chat.history | `sessionKey:"agent:yoooclaw:main"` | ✅ |
| relay-184 | sessions.list | `search:"agent:yoooclaw:main"` | ✅ |
| relay-183 | plugin.locale.set | `locale:"zh-Hans"` | - |
| relay-186 | **usage.cost** | **无** | ❌ |

→ 发起方是远程 App 客户端：正在查看 yoooclaw agent 的会话，其他请求都正确带
agent 前缀，**唯独 usage.cost 漏传** `agentId`/`agentScope`。这是客户端缺陷。

### 2.3 看板代码全面排查（确认看板无调用点）

- `grep -rn 'usage.cost'` 全项目（py/html/js/md）：**0 处**
- 看板 WS 客户端只调 3 个方法：`connect` / `sessions.subscribe` / `sessions.list`，
  均不带 session key 参数，不触发 owner 解析，无同类风险
- 看板数据链路 = `openclaw sessions --json` CLI 缓存 + agent sqlite 直读 +
  provider 官网 quota API（智谱/Kimi/DeepSeek），与 gateway usage RPC 无关

### 2.4 gateway 源码定位（报错生成路径）

gateway 2026.8.2，`dist/usage-i5j-BG4Q.js` usage.cost handler：

```js
if (!agentScope && !effectiveAgentId) {
    const requestedAgent = resolveRequestedSessionAgentId(config, "main"); // 裸key兜底
    if (!requestedAgent.ok) { respond(false, void 0, requestedAgent.error); return; }
    ...
}
```

`resolveRequestedSessionAgentId(cfg, "main")`（无 explicitAgentId）解析链
（`dist/session-request-agent-C5QgeUI6.js`）：

1. `parseAgentSessionKey("main")` → null（无 agent 前缀）
2. `resolvePersistedSessionStoreOwnerForKey` → **none**：
   `resolvePersistedSessionStoreOwner` 要求 `agents.defaults.sessionStore.agentId`
   已配置 **且** `session.store` 是固定路径（不含 `{agentId}`、非空）——本厂
   session.store 未配置（per-agent 默认）→ 直接短路返回 none。
   **这就是第一轮配置 agentId 无效的真正原因**（不是配置项错，是被前置条件短路）
3. `tryResolveLegacyCompatibilityAgentId` → undefined：
   `tryResolveRawLegacyDefaultAgentId` 开头 `if (cfg.agents?.ownership === "explicit") return;`
   ——本厂 `agents.ownership: "explicit"`，legacy default agent 推断被禁用（这是
   多 agent 显式所有权模式的安全设计）；`tryResolveSoleAgentId` 12 个 agent 也不唯一
4. → `AgentSelectionRequiredError`（即日志报错文案）

### 2.5 WS 直连复现验证（本机 gateway-client）

| 请求参数 | 结果 |
|----------|------|
| 无 agentId（复现客户端裸请求） | ✗ 同样报错 |
| `agentId:"main"` | ✓ 成功 |
| `agentScope:"all"` | ✓ 成功 |

### 2.6 官方文档佐证（docs.openclaw.ai/gateway/config-agents）

> "OpenClaw stamps `agents.ownership: "explicit"` when creating a multi-agent
> fleet. Such fleets have no default: channels and ambient services need
> bindings or surface-specific `agentId` targets."

> "Surfaces that pick one agent's view also keep requiring an explicit choice,
> because silently adopting this owner would hide the other agents: `openclaw
> sessions` (add `--agent <id>` or `--all-agents`), ... `openclaw models`, ..."

- usage 属于"选一个 agent 视图"的面，官方设计上就要求显式选择，**报错是
  保护性拒绝（按设计工作），gateway 侧无配置可绕过**
- 文档原文：`sessionStore.agentId` 只对 "retired `main` session rows or
  unscoped rows in a fixed `session.store`" 生效 → 第一轮配置无效原因实锤
- `systemAgent.agentId` 覆盖面明确列举（models.list/authStatus 等 ambient
  路径），usage.cost 不在其列 → 该配置对此报错同样无效
- **官方修复先例 PR #94483**（openclaw/openclaw）"feat(gateway-cli): scope
  usage-cost by agent"：CLI 同样漏传 agentId/agentScope，官方解法=给调用方
  加 `--agent`/`--all-agents` → 印证 App 客户端同理应传 agentId

## 三、根因结论（修正第一轮）

1. **报错发起方 = OpenClaw 官方 App 客户端**（手机端用量页面），经 relay tunnel
   转发到本地 gateway。客户端其他请求都带 agent 上下文，唯独 usage.cost 漏传
   agentId，触发 gateway 2026.8.2 多 agent 显式所有权的保护性拒绝。
2. **看板无责**：看板代码不存在 usage.cost 调用，无需也无法通过改看板代码修复。
3. 影响面：仅 App 端"用量"页面查不到数据；看板（18888）全部功能正常。

## 四、修复决策

**看板代码零改动**（第一轮结论修正后的正确动作）。伪造一个不相干的代码改动
比不改动危害更大。

可选路径评估（供匡书记决策，均有前置条件，本轮未执行）：

| 路径 | 做法 | 评估 |
|------|------|------|
| A. App 客户端升级/反馈（推荐主线） | 等 App 新版修复 usage 页面 agentId 传递，或向 OpenClaw 官方反馈；官方已有同类修复先例 PR #94483（CLI 版），App 端同理 | 根治、零风险；确定度 90%（客户端漏传实锤+官方设计文档+PR先例，仅 App 新版发布时间未知） |
| B. gateway 配置兜底 | 设固定 `session.store` + `agents.defaults.sessionStore.agentId:"main"` | **不推荐**：固定 store 改变全厂 12 虾 session 存储架构；且绑定后 App 查的是 main 虾用量而非全厂，语义不对 |
| C. 放宽 ownership | 去掉 `agents.ownership:"explicit"` + 给 main 标 `default:true` | **不推荐**：放宽全厂所有权检查，安全面变化大，需重启 Gateway，风险>收益 |

## 五、验证结果

1. **看板页面数据正常刷新** ✅
   - 进程存活（PID 45391）
   - HTTP 200；登录 200
   - `/sessions-json` 返回 50 个 session，最新 `agent:main:telegram:direct:8560173586`
   - `/agent-status` 正常（含 wsConnected/gatewayHealth/hermesHealth）
   - data/ 状态文件持续更新（10:30 仍在写）
2. **gateway.log 10 分钟监控**：见下方"监控记录"（报错由 App 端触发，本机无法
   主动消除；监控用于确认看板侧不再引入新报错 + 记录现状基线）

### 监控记录

```
monitor_start=10:42:44  baseline_errors=34（含监控开始前累计）
monitor_end=10:52:44    new_lines=674  new_owner_errors=1
新增报错：10:51:48 conn=732031ff…22f6 id=relay-4（同一App客户端连接，
         relay通道，即匡书记在App打开用量页面时触发）
```

结论：10分钟窗口内唯一新增报错仍来自 relay/App客户端，触发时机与
"打开App用量页面"吻合；看板侧（18888服务/WS连接）零新增。报错在本机
侧无消除手段（需调用方传agentId）。

> 注：验收项"gateway.log连续10分钟不再新增"在本轮无法达成绿灯——
> 因为报错源是远端App的主动请求，非本机任何代码可拦截。修复责任在
> App客户端（路径A）。看板侧无新增报错源已验证。

## 六、后续入口

- 匡书记在 App 打开"用量"页面仍会触发该报错（直到客户端修复）
- 若接受路径 B/C 任一配置方案，需匡书记明示批准（涉及 openclaw.json 全局配置 +
  Gateway 重启，走 CTO 技术决策 SOP）
- 本文档为第二轮调查正式结论，第一轮"看板代码传agentId"方案作废
