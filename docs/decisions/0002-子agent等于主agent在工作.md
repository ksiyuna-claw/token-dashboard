# ADR-0002: 子agent等于主agent在工作

## 日期
2026-08-10

## 状态
已采纳

## 背景
看板展示主agent状态时，需要决定是否把子agent的活动算进去。如果不算，主agent spawn 了分身在干活，但看板显示"空闲"，误导判断。

## 决策
**分身子agent（同 agentId 派生，如 `agent:main:subagent:xxx`）在工作时，主agent显示"工作中"。跨agent不算（罗氏虾 spawn 海星，不算罗氏虾在工作）。**

## 规则细节
- 判定依据：子agent session key 的 agent 段与主agent一致
- 示例：`agent:main:subagent:abc` 是 main 的分身 → main 显示 working
- 示例：`agent:haixing:subagent:xyz` 不是 main 的分身 → 不影响 main 状态

## 原因
分身是主的直接延伸，不是独立第三方。主agent派出分身 = 主agent在工作。

## 决策人
匡书记（2026-08-10 确认）

## 备注
- 2026-08-10 issue-0165 复盘时，CEO 误以为子agent不应参与判定，差点改错。此 ADR 防止重蹈覆辙
