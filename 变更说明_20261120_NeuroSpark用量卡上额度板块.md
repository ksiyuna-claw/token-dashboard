# 变更说明：NeuroSpark 用量卡上额度板块（2026-11-20）

- 工单来源：匡书记 2026-11-20 23:35 口令「把 NeuroSpark 的余额也放到 token 看板上，在原项目文件夹操作」
- 执行：基围虾（jiweixia）

## 需求与调研结论

NeuroSpark（api.neuro-spark.ai，Claude/GLM 等中转，openclaw.json models.providers 在册）上看板额度板块。**实测其 API 无余额端点**：one-api 标准 dashboard/billing 族、credits/billing/customer/subscription/quota/account 共 11 条路径全部 404——后付费（customer 计费）模式，余额只在网页控制台。可用数据源=`GET /v1/usage`（Bearer）：total_spend/today_spend/total_calls/today_calls/rows[分模型 spend/calls/tokens]。**卡片因此为「消费卡」语义**（累计/今日消费+调用量+分模型 top3），这是 API 能提供的余额最接近等价物，已向匡书记说明。

## 改动（四触点）

1. **server.py `PROVIDER_QUOTA_API`**：加 `'api.neuro-spark.ai': ('https://api.neuro-spark.ai/v1/usage', 'neurospark_usage')`——NeuroSpark 是 openclaw.json 在册 provider，走既有 baseUrl 匹配注入（apiKey 从 openclaw.json 现读，不复制到 config.json）
2. **server.py `PROVIDER_LABELS`**：加 `'neurospark': 'NeuroSpark（Claude/GLM中转）'`
3. **server.py `_proxy_neurospark`**：🔴 WAF 拦 urllib（实测 403，curl 通）→ curl_cffi impersonate chrome（TikHub/issue-0379 同族根治）；响应结构化 totalSpend/todaySpend/totalCalls/todayCalls/topModels(spend>0 前3)；🔴 凭证与 customer_id/account_id 均不透出（只进请求头）；60s 缓存+旧缓存兜底+超时504/错误502
4. **前端 `neurospark_usage` 分支**：手绘消费卡（消费制无上限无法映射 limits 数组，按 skill 规则手绘，RunPod/TikHub 同款）：累计消费/今日消费+次数/top3 模型 spend 行/总调用 tag；数值 isFinite fail-closed；动态字符串 mEsc

同步：README 功能表额度行补 NeuroSpark 卡描述。

## 验证（实测）

1. `py_compile` PASS；前端 script 段提取 `node --check` PASS
2. kickstart 重启后 `/providers` 含 neurospark（label=NeuroSpark（Claude/GLM中转））
3. `/quota/neurospark` → `{"totalSpend": 0.2597, "todaySpend": 0.0, "totalCalls": 40, "topModels": [glm-5.3 $0.1998×12, claude-sonnet-5 $0.0524×3, Kimi-K3.1-Pro $0.0016×1]}`——无凭证/客户标识字段
4. 公网 https://token-dashboard.crypto-signal.work → 302（登录跳转，正常）

## 回滚

备份：`workspace-jiweixia/tmp/1119tikhub/`（server.py/html，.bak.20261120）；删 PROVIDER_QUOTA_API/LABELS 两行+方法+分支即摘卡。

## 备忘

- api.neuro-spark.ai 与 api.tikhub.io 同款 WAF（拦 Python 原生 TLS 指纹）——厂内「WAF 拦 urllib」中转站已有两例，新接外部 API 先 curl 后 urllib 可预判

## 第1轮red修复记录（R1+R2，2026-11-20 23:47）

**R1（非法消费数值静默转0）**：`_num(v, default=0.0)` 把 str/None 等非法输入返回 0——total_spend='abc' 曾伪装成真实 $0 消费（审核员独立重放实锤）。修复：改 `_strict(v, name)`，核心四字段（total/today spend/calls）必须有限非负数值且非 bool，非法即抛 ValueError 走旧缓存兑底/502；模型行策略=非法行跳过、合法行保留。前端 isFinite 保留作第二道防线。

**R2（缺本单反例三连回执）**：新建 NeuroSpark 专属 `tmp/m1_selftest.md`，九组真实实测全 PASS：①无效凭证→401→502 ②HTTP503→502 ③非法数值四连（str/None/负数/bool）→全部502不伪装0（R1主回归）④缺字段→502 ⑤模型行非法跳过+合法保留（断言 bad被滤/good在位）⑥新鲜缓存+无效凭证未触上游 ⑦过期缓存+上游挂→旧值cached:true ⑧生产无凭证字段 ⑨二调0.034s缓存命中。实测时间 2026-11-20 23:47:59，全部真实命令输出在档。

## 第2轮red修复记录（R1续项，2026-11-20 23:56）

**根因（诚实记录）**：round1修复时我把「归档TikHub旧回执(mv)」与「写入NeuroSpark新回执(write同名文件)」两个有依赖的操作**放在同一消息批次并行执行**——实际执行序为 write先覆盖、mv后改名，导致 NeuroSpark 新版被改名成归档名、`tmp/m1_selftest.md` 路径空置，审核员核不到文件。教训：有先后依赖的文件操作禁止同批并行。

**修复**：`mv tmp/m1_selftest_tikhub_归档1119.md → tmp/m1_selftest.md`（head 验证内容=NeuroSpark 版 3074字节、10处PASS、九组用例完整）。TikHub 原版回执在此事故中被覆盖，其内容已随 TikHub flow 审核闭环存档于 `logs/审核记录/TikHub余额卡上额度板块/`（该 flow 已终态，无后续依赖）。

## 第3轮red修复记录（R1，2026-11-21 00:03）

**R1（Infinity/NaN 模型行破坏 JSON，上轮边缘项 E1 升级）**：修复前 spend=∞ 行进 topModels，默认 json.dumps 输出非标准 Infinity，浏览器 r.json() 解析 reject → 卡片渲染失败+Infinity 入缓存持续污染（审核员独立重放实锤）。修复：used 过滤加 `math.isfinite(float(spend))`（∞/NaN 行直接跳过）；calls 转换加有限性检查（∞→0 兑底，防 int(inf) 抛 OverflowError）。反例⑩入档 m1_selftest.md：四行混合 rows（∞spend/NaN spend/∞calls/good）→∞/NaN 行被滤、有限行保留、全响应 `json.dumps(allow_nan=False)` 严格序列化 PASS；生产复跑严格 JSON PASS。实测时间 2026-11-21 00:03:31。
