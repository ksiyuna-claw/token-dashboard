# Fix: 前端缺少 stale 状态映射导致显示 ❓

**时间**：2026-08-11 13:00
**发现者**：匡书记
**修复者**：基围虾
**严重度**：🟡 低（显示问题，不影响功能）

## 问题

看板上皮皮虾、斑节虾、小河虾显示 ❓。

## 根因

issue-0155 修复合入了后端新状态 `stale`（idle 超30分钟的僵尸 session），但前端 `token_dashboard.html` 的三个映射表（statusIcon / statusCls / statusText）和排序表（statusOrder）都没有 `stale` 键。`statusIcon[st]` 返回 `undefined`，fallback 到 `||'❓'`。

## 修复

前端四个映射表加 `stale` 条目：

```js
statusOrder: {working: 0, waiting: 1, idle: 2, stale: 3}
statusIcon:  {working:'🔨', waiting:'⏳', idle:'😴', stale:'💤'}
statusCls:   {working:'agent-card-working', waiting:'agent-card-waiting', idle:'agent-card-idle', stale:'agent-card-idle'}
statusText:  {working:'工作中', waiting:'等待中', idle:'空闲', stale:'沉睡'}
```

stale 复用 idle 的样式（半透明灰），排在所有状态最后。

## 修改文件

- `/Users/kuangsiyu/.openclaw/workspace/ai_workspace/scripts/token_dashboard.html`（第621、632-634行）

## 教训

后端新增状态枚举时，必须同步检查前端所有状态映射表。这次是 issue-0155 合入时只改了后端没改前端。
