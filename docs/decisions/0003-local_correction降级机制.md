# ADR-0003: local_correction 降级机制

## 日期
2026-06-24

## 状态
已采纳（issue-0165 已修复）

## 背景
Gateway 的 `hasActiveRun` 事件偶发丢失（不发 False），导致看板永久显示"工作中"。需要一个本地纠偏机制，当检测到 WS 活动长时间无更新时，跳过不可靠的信号源。

## 决策
当 WS 活动超过 60 秒没更新时，激活 `local_correction`，跳过 `hasActiveRun` 和 WS 活动的 working 判定，降级到 age 检测。

## 规则细节
- 触发条件：WS 活动时间戳距今 > 60 秒
- 激活后跳过：优先级 1（hasActiveRun）和优先级 3（WS活动）
- 降级到优先级 4–7（age 检测）

## ✅ 已修复（issue-0165，2026-08-11）
`local_correction` 原来只跳过了优先级 1 和 3，漏了优先级 4 的 `age < 90s → working` 分支。当 Gateway 持续刷新 `updatedAt` 时，仍然卡在 working。

### 修复内容
`_corrected=True` 时，age 检测的 else 分支输出 `waiting` 而非 `working`。等真正的新 turn 开始时 `hasActiveRun=True` 会覆盖。

## 决策人
基围虾（设计）+ 匡书记（审批修复方向）

## 备注
- 相关 issue：issue-0165
- 修复前看板可能误显示 working，需结合人工确认
