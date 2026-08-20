# Fix: subagent updatedAt 冻结 16 分钟 → 看板误判 CTO「不在工作」（issue-0183）

**时间**：2026-08-15（12:18 发现，13:24 修复验证通过）
**发现者**：匡书记
**修复者**：基围虾
**严重度**：🟠 中高（P2，监控数据失真、误导判断，非功能故障）

## 现象

2026-08-15 12:08 CEO spawn 基围虾（CTO）两个并行子任务。任务真实执行中——sqlite transcript 12:10-12:31 每分钟持续有事件写入（12:10=12条、12:12=9条、12:21=15条），但 12:18 匡书记查 token 看板：**基围虾显示不在工作**，与实际执行状态矛盾。持续整个 subagent 运行期间（16+ 分钟），看板始终未反映该活动。

## 三层证据链

### 证据一：WS 日志 vs sqlite 双通道对比

看板 WS 日志（/tmp/token_dashboard.log）：jiweixia 在 12:10:48 收到 `hasActiveRun=False` 事件后，直到 12:27:17 才再有下一事件——**中间 16 分钟空白**。而同一时段 sqlite transcript 证明子 session `fb5bb194` 每分钟都在持续写入。结论：**WS 事件通道对 subagent run 失明**（Gateway 对 subagent run 不发 `sessions.changed(hasActiveRun=True)` 事件）。

### 证据二：CLI updatedAt 冻结实验

修复前，用当时还在跑的本 session `74276398` 实测对比：

| 数据源 | updatedAt age |
|--------|---------------|
| `openclaw sessions --json` | **64 秒**（停在上一次投递时刻 13:31:53，不随实际工作刷新） |
| sqlite `sessions.updated_at` | **2 秒**（真实写入时间） |

结论：**CLI 数据源对运行中 subagent 的 updatedAt 冻结在 Gateway 观察时刻**（transcript_observed_at），子 session 运行期间不再刷新。

### 证据三：判定逻辑推演复现（12:18 时点）

用看板同款判定逻辑回放 12:18 时点：主 session 最后活动 12:06:49（age 11.2分钟）、子 session CLI updatedAt 最晚不超过 12:10:47（Gateway 最后一次刷新点，age≥7.2分钟）、WS 无活跃标记、无 exec 进程匹配 → effective_age 7.2-9.4 分钟，远超 90 秒 working 阈值 → 判 waiting「不在工作」。**与匡书记 12:18 看到的现象完全一致，复现成立。**

## 设计初衷核验

CHANGELOG 2026-06-23 明确记载设计初衷：「修复 subagent 模型思考期间被误判为等待中」「subagent 真实工作中应显示 working」。12:18 场景不是当初覆盖的「思考 30-90 秒窗口」，而是 **subagent 持续工作 16 分钟但两个数据信号通道（CLI updatedAt / WS 事件）同时冻结**——超出 WS TTL 90 秒 / updatedAt 90 秒阈值的设计覆盖范围。**这不是设计如此，是数据源缺陷**；修复让 updatedAt 回归「最后真实活动时刻」的本义，判定逻辑本身未改，与设计初衷一致。

## 根因（两层信号同时失明）

1. **CLI 数据源滞后（主因）**：`openclaw sessions --json` 对 subagent session 返回的 updatedAt ≈ spawn 观察时刻，子 session 运行期间不再刷新。实测运行中子 session sqlite updated_at age=0.2 秒，但 CLI 返回值停在 spawn 时刻（滞后 8 分钟）。
2. **WS 信号对 subagent 失效（次因）**：Gateway 对 subagent run 不发 `sessions.changed(hasActiveRun=True)` 事件（见证据一）。

双信号同时失明 → effective_age 超 90 秒 → 判 waiting/idle。**不是聚合逻辑缺失**（subagent 合并逻辑一直存在且正常，subagentCount=5-6 实时准确）。

## 修复方案

`token_dashboard_server.py` 2 处改动：

1. **新增 `_refresh_updated_at_from_sqlite()`**（L775）：读各 agent 的 `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite` 的 `sessions.updated_at`（真实写入时间，运行中实测 age<1s），按 session_key 精确对应后取 max 覆盖 CLI 滞后值。会话彻底结束后 sqlite updated_at 停止前进，不影响 idle/stale 判定。
2. **`_get_cached_sessions()` 挂载点**（L836）：在 `merge_with_snapshot()` 之后调用上述函数；且 ageMs 改为在 sqlite 覆盖后重算（R1 修复：否则前端直接消费的 `s.ageMs` 仍是 CLI 滞后值）。

**不改动**：状态判定阈值（WS TTL 90s / working 90s / idle 600s）、subagent 聚合逻辑、前端、快照逻辑。

## 验证结果

端到端复现实测（2026-08-15 13:24）：

| 场景 | ageMs | 看板显示 |
|------|-------|---------|
| 修复前：父 session yield 挂起 + 子 session 运行 | **167829ms**（≈167秒） | ❌ waiting（复现事故） |
| 修复后：spawn 测试子 session + 父 session 挂起 | **<3秒**（两次采样间隔25秒均如此） | ✅ working |

✅ 完全复现事故场景下修复生效。

## 已知债务

- **R2-1**（对焦虾第2轮提出，已记 CHANGELOG #11）：sqlite 查询为全表扫无 LIMIT/索引。sessions 为本地小表（仅元数据行，千行级），全表扫开销可忽略；若量级增长需加 LIMIT/索引（代码内 L807 注释已标注）。

## 教训

- 「看板没显示」≠「没在工作」——先查双数据通道（CLI/WS）是否对该 session 类型失明，再下结论。
- CLI 工具输出的时间字段不一定等于真实活动时间，可能是缓存/观察时刻；关键判定必须用最接近真源的存储（sqlite）。
