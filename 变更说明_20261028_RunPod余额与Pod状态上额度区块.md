# 变更说明 2026-10-28：RunPod 余额与 Pod 状态上额度区块

## 背景
匡书记 20:03 指令：新增 RunPod 云端显卡平台（数字人口播项目，已充值），余额和 pod 开机状态加到 token 看板「模型供应商额度」列。

## 改动清单

### 1. token_dashboard_server.py（3处）
- `_get_providers()` 末尾静态注入 `runpod` 条目（label=RunPod GPU云（数字人口播），quotaType=runpod_gpu）。RunPod 非 LLM provider、不在 openclaw.json models.providers，不走动态发现；key 从项目 `config.json` 的 `runpod_key` 读取，读取失败仅跳过该卡片不影响其他 provider。
- 新方法 `_proxy_runpod(api_key, provider_id)`：两步合成查询——
  ① GraphQL v1 `POST https://api.runpod.io/graphql?api_key=<key>`，`{ myself { clientBalance } }` 取余额；
  ② REST v2 `GET https://api.runpod.io/v2/pods`（Bearer 认证）取活跃 pod 列表（截前6个：name/status/gpu/costPerHr/startedAt，字段名按官方 OpenAPI 示例）。
  缓存/超时 fallback 策略与 `_proxy_quota` 一致（60s TTL + 过期缓存兜底 + 502/504）。走 `_PROXY_OPENER`（127.0.0.1:7890 代理，国内访问必需）。
- `/quota/<pid>` 路由按 quotaType 分发：runpod_gpu → `_proxy_runpod`，其余 → `_proxy_quota` 不变。

### 2. templates/token_dashboard.html（1处）
`fetchProviderQuotas()` 新增 `runpod_gpu` 渲染分支：余额（USD）大字绿色 + 每个 pod 一行（名称 · GPU 型号 / $时薪）+ 底部状态标签（GPU 运行中 ×N / GPU 未开机）。复用现有 card/quota-row/tag 样式，10-22 的响应守卫与 30s 快速重试自动覆盖。

### 3. config.json / config.example.json
- config.json 新增 `runpod_key`（50字符，与 api-keys_SOP.md L403 / env.sh L39 同源）。config.json 已被 .gitignore 忽略，不进 git。
- config.example.json 新增占位 `YOUR_RUNPOD_API_KEY`。

### 4. README.md
功能全景段「模型供应商额度」行补 RunPod 说明（含静态注入/key位置注记），顺带修正该行仍列 MiniMax 的过期文案（7-23 已下线）。

## 关键踩坑记录（本次调研实锤）
- RunPod GraphQL 余额字段名是 `clientBalance`，不是 `balance`（后者 GRAPHQL_VALIDATION_FAILED）；introspection 被服务端禁用。
- `/graphql/v2` 端点实测 404 不存在（api-keys_SOP.md 原登记有误，皮皮虾另行勘误中）；可用的是 v1 `?api_key=` query param 认证。
- REST v2 认证：`Authorization: Bearer <key>`（官方 OpenAPI securitySchemes bearerAuth）。

## 验证记录
1. `python3 -m py_compile` 通过；模板 <script> 段提取后 `node --check` 通过。
2. import 冒烟：`_get_providers()` 返回 `[deepseek, kimi, zhipu, runpod]`，runpod keylen=50。
3. `launchctl kickstart -k` 重启后实测：
   - `GET /providers` → 4 provider 含 runpod(runpod_gpu)
   - `GET /quota/runpod` → `{"balance": 12.3945853091, "podCount": 0, "pods": []}`（与 19:52 手测一致）
   - `GET /` → HTTP 200
4. HTML 改动即时生效（模板每请求读盘），无需额外操作。

## 回滚
- 代码：`cp workspace-jiweixia/tmp/token_dashboard_server.py.bak.20261028` 与 `token_dashboard.html.bak.20261028` 回对应文件 + kickstart 重启。
- config：删 `runpod_key` 键即摘除卡片（server 读不到 key 不注入）。

## 第1轮审核修复记录（round1：2红1黄，2026-10-28）

对焦虾第1轮审出 R1/R2 红 + Y1 黄，全部修复（报告：`logs/审核记录/RunPod余额与Pod状态上额度区块/02_对焦虾审核_v1.md`）。修复分两棒：基围虾主体 20:31 完成初版（scheduleQuotaRetry/mEsc/runningCount雏形）；20:36 续接修复分身对照审核规格补齐两处硬缺口（展示列表排除 TERMINATED、每行始终展示 status）并升级回归为真码驱动。

- **R1（非RUNNING误报运行中）**：
  - 服务端 `_proxy_runpod`：展示列表改为 `visible_pods`（排除 `TERMINATED`，大小写/空白防御）；`podCount`=可见Pod总数、`runningCount`=仅计 `status=='RUNNING'`（审核一行改法落地，不再用 `len(pods)` 把关机Pod计成运行中）；每个Pod保留真实 `status`。
  - 前端 runpod 分支：底部标签改用运行数判定——`runningCount>0`=「GPU 运行中 ×N」、`podCount>0 但 runningCount=0`=「GPU 已停 ×N」（黄标）、全0=「GPU 未开机」；**每个Pod行始终展示真实 status**（RUNNING 显 `RUNNING · $0.59/时`；EXITED 等即使带 cost 也先显状态——RunPod 对 EXITED 也会返回 cost，不能只显时薪）；旧缓存无 `runningCount` 字段时前端按 pods 列表 filter 兜底；计数统一 `parseInt` 归一防非数字串进 innerHTML。
- **R2（XSS注入）**：runpod 分支所有用户可控动态字符串（`pd.name`/`pd.gpu`/`pd.status`/`j.error`，含 `p.label`/`host`）统一 `mEsc()` 转义——与看板现有转义函数同源（L328）；数值字段（balance/costPerHr/runningCount/podCount）先 parseFloat/parseInt 再拼接。
- **Y1（单provider失败不触发30s重试）**：提取统一入口 `scheduleQuotaRetry()`（30秒防重定时器保留）；`allSettled` 循环内 rejected 项置 `quotaAnyFailed=true`，fulfilled 但 `j.error` 存在也置位；循环后统一调度；原外层 catch 内联 setTimeout 改为调用统一入口。**单RunPod 502 → 30秒后自动重新请求**已有真实回归用例（见下）。

### 回归证据（18/18 全过，脚本 workspace-jiweixia/tmp/runpod_regression.js）
脚本 v2 关键升级：不再用手工复制的镜像逻辑，直接从模板正则提取**真实** `mEsc/renderNoQuota/scheduleQuotaRetry/fetchProviderQuotas` 函数体，在沙箱（mock fetch/document/setTimeout）中驱动真码；后端则提取 `_proxy_runpod` 真实映射块源码 exec 断言：
- Y1-502（审核指定回归③）：`/quota/runpod` 返 502 → 30s 重试定时器被调度（且仅1个）→ 触发定时器后 runpod 被重新请求、卡片恢复余额、成功不再叠加定时器 ✅
- R1 混合（审核指定回归①）：RUNNING+EXITED(+cost)+TERMINATED → 标签「运行中 ×1」非×2；RUNNING 行显 `RUNNING · $0.44/时`；EXITED 行显 `EXITED · $0.10/时`（有时薪也显状态）；TERMINATED 行显真实状态 ✅
- R1 全停：旧缓存无 runningCount → 前端 filter 兜底 → 「已停 ×2」非「未开机」；空Pod → 「GPU 未开机」 ✅
- R2 注入（审核指定回归②，审核员同款 onerror 载荷）：恶意pod名 `<img src=x onerror=window.__runpodXss=1>` + gpu `<svg onload=...>` + error分支 `<script>` → 全部转义为纯文本，无原生 img/svg/script 标签节点 ✅
- R2 计数域：runningCount/podCount 被污染成HTML串时 parseInt 归零，不进 innerHTML ✅
- 后端映射块（真实源码 exec）：TERMINATED 排除、runningCount=1、真实 status 保留、GPU `RTX 4090×2` 拼接 ✅
- `python3 ast.parse` + 模板 script 段提取 `node --check` ✅

### 上线实测（20:49 `launchctl kickstart -k gui/$UID/com.openclaw.token-dashboard` 重启后）
- `GET /providers` → 4 provider 含 `runpod(runpod_gpu)` ✅（RUNNING 正向路径不回归）
- `GET /quota/runpod` → `{"balance": 12.0544899637, "podCount": 1, "runningCount": 1, "pods":[{"name":"pipixia-dh-compare","status":"RUNNING","gpu":"NVIDIA A40","costPerHr":0.59,"startedAt":"2026-10-28T12:11:14.93Z"}]}`——真实开机场景（数字人口播 A40），runningCount 与 status 联动正确 ✅
- 主页 HTTP 200 ✅

### 流程留痕
- 第1轮审核报告：`logs/审核记录/RunPod余额与Pod状态上额度区块/02_对焦虾审核_v1.md`（2红1黄）
- 续接修复注记：`tmp/repair_round1_续接注记.md`；red 处置全程静默（无TG推送）
- 第2轮 submit：20:33 `workflow.sh submit` 完成（task_state round=2，spawn_task_round2.txt 已生成）；续接分身补齐修复后 20:50 spawn 对焦虾复审
