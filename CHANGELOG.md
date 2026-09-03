# Token Dashboard 变更日志

### 2026-08-15 (#11) — 修复 subagent updatedAt 冻结导致看板误判「不在工作」（issue-0183）（基围虾 / 匡书记指令）

**背景**：2026-08-15 12:08 CEO spawn 基围虾两个并行任务，任务真实执行中（sqlite transcript
12:10-12:31 每分钟持续写入），但 12:18 匡书记查 token 看板发现基围虾（CTO）显示不在工作——
监控数据失真（issue-0183，P2）。排查实锤：CLI 数据源 `openclaw sessions --json` 对运行中
subagent session 的 updatedAt 冻结在 spawn 观察时刻（16 分钟不刷新），叠加 Gateway 对
subagent run 不发 WS 活跃事件（双通道同时对 subagent 失明）→ effective_age 超 90 秒阈值
→ 误判 waiting「不在工作」。

**变更内容**（仅 `token_dashboard_server.py`，2 处）：

1. 新增 `_refresh_updated_at_from_sqlite()`：读各 agent 的
   `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite` 的 `sessions.updated_at`
   （真实写入时间，运行中实测 age<1s），按 session_key 精确对应后取 max 覆盖 CLI 滞后
   updatedAt。会话彻底结束后 sqlite updated_at 停止前进，不影响 idle/stale 判定
2. `_get_cached_sessions()`：在 `merge_with_snapshot()` 之后调用上述函数（sqlite 真源覆盖），
   且 ageMs 改为在 sqlite 覆盖后重算（R1 修复：否则前端直接消费的 s.ageMs 仍是 CLI 滞后值）

**已知债务**：R2-1（对焦虾第2轮）——sqlite 查询为全表扫无 LIMIT/索引；sessions 为本地小表
（仅元数据行，千行级），全表扫开销可忽略；若量级增长需加 LIMIT/索引（代码内注释已标注）。

**不改动**：
- 状态判定逻辑本身（WS TTL 90秒 / updatedAt working 阈值 90秒 / idle 600秒）不变
- subagent 归属聚合逻辑不变（一直存在且正常，subagentCount 实时准确）
- 前端 `token_dashboard.html`、`merge_with_snapshot()` 快照逻辑、WS 监听均不动

**恢复方法**：删除 `_refresh_updated_at_from_sqlite()` 函数及 `_get_cached_sessions()` 中的
调用行（ageMs 重算段一并回退）即可回到修复前行为。

**验证**：端到端复现实测（2026-08-15 13:24）——修复前父 session yield 挂起时子 session
ageMs=167829ms（判 waiting，完全复现事故场景）；修复后 spawn 测试子 session + 父 session
挂起，子 session 两次采样（间隔 25 秒）看板均显示 working、ageMs<3 秒。✅ 通过

### 2026-08-14 (#10) — 隐藏 zai（智谱海外）卡片，停止其额度查询（基围虾 / 匡书记指令）

**背景**：智谱海外 Coding Plan 于 2026-08-07 到期未续费。2026-08-14 基围虾用
`openclaw onboard --auth-choice zai-coding-cn` 重写 openclaw.json 的 zai provider，
实际挂国内端点 open.bigmodel.cn + 国内 Key，与 zhipu 完全同 Key 同端点同额度接口。
看板 `_get_providers()` 动态读 openclaw.json → “智谱 BigModel（海外）”卡片重新出现，
且每次轮询多打一次与 zhipu 卡完全重复的额度 API（数据一模一样，纯浪费带宽）。

**匡书记指令（2026-08-14 21:11）**：方案A改良版——代码留着以防万一，
但看板不展示该卡片、不为其消耗内存/带宽。

**变更内容**（仅 `token_dashboard_server.py`）：

1. 新增模块级 `HIDDEN_PROVIDERS = {'zai'}`（带注释说明原因和恢复方法）
2. `_get_providers()` 循环开头跳过隐藏 provider → zai 不进 /providers 响应 →
   前端无卡片；`/quota/zai` 找不到 provider 返 404 → 服务端不再为其发起任何额度查询/缓存

**不改动**：
- openclaw.json 的 zai provider（全厂 default 模型 zai/glm-5.2 的路由依赖，绝不能删）
- `PROVIDER_QUOTA_API` 的 `'api.z.ai'` 条目、`PROVIDER_LABELS` 的 `'zai'` 条目均保留（以防万一）
- zhipu（智谱国内）卡片不受影响

**恢复方法**：从 `HIDDEN_PROVIDERS` 集合中移除 `'zai'` 即可。

**备注**：config.json 的 `zhipu.overseas_key`（8b7e...开头）已无任何代码引用，
保留不动 —— 2026-08-07 已到期未续费，死配置。

### 2026-07-22 (#9) — Session认证改用JWT无状态Token（海螺 / 匡书记指令）

**背景**：Token看板登录session存在内存字典 `_sessions = {}` 里，每次看板重启session全丢，用户被迫重新登录。改用JWT（无状态token），token自带签名+过期时间，服务器不需要存储任何session状态，重启不丢失登录。

**变更内容**：

1. **新增JWT工具函数**（`token_dashboard_server.py`）
   - `_get_jwt_secret()` — 延迟初始化JWT密钥，基于认证密码派生
   - `_b64url_encode()` / `_b64url_decode()` — Base64 URL安全编码
   - `_jwt_create(user, expires_in)` — 生成JWT token（header.payload.signature）
   - `_jwt_verify(token)` — 验证签名+过期时间，使用 `hmac.compare_digest` 防时序攻击
   - 新增 imports：`hmac, hashlib, base64`

2. **替换session相关函数**
   - `_create_session()` → 调用 `_jwt_create()`，不再生成随机token存字典
   - `_check_session()` → 从Cookie读JWT并用 `_jwt_verify()` 验证，不再查字典

3. **删除不再需要的代码**
   - 删除 `_sessions = {}` 内存字典
   - 删除 `_SESSION_FILE` 持久化路径
   - 删除 `_save_sessions()` / `_load_sessions()` / `_cleanup_expired_sessions()`
   - 删除 `_load_sessions()` 启动调用
   - 删除 do_POST /login 中的 `_cleanup_expired_sessions()` 调用
   - 简化 /logout 路径：不再遍历Cookie删session，只让浏览器删Cookie

4. **不改动**
   - Cookie设置逻辑（Max-Age/SameSite/remember等）不变
   - 前端不动
   - 认证流程不变（_require_auth / _send_auth_challenge 等）
   - 本地免认证逻辑不变

**验证结果**：
   - ✅ 语法检查通过
   - ✅ 看板启动正常，页面可访问
   - ✅ 登录生成JWT token（格式：header.payload.signature）
   - ✅ **重启看板后同一个Cookie仍然有效**（核心目标达成）

### 2026-07-15 (#8) — 更新Token看板定时任务/运行服务展示信息（基围虾 / 匡书记指令）

**背景**：匡书记要求检查并更新Token看板上的定时任务/运行服务记录，确保信息准确反映最新配置。

**变更内容**：

1. **心跳配置展示修复**（`token_dashboard_server.py`）
   - `_get_heartbeats()` 原逻辑：跳过 `every=0m` 的 agent，导致关闭心跳的虾（如罗氏虾）不在看板显示
   - **修复**：改为返回**全部 agent 的心跳状态**，`every=0m/0` 的显示为「已关闭」+ `enabled=false`
   - 效果：看板现在清晰展示所有12只虾的心跳状态，罗氏虾显示「○ 关闭」，基围虾显示「360m ● 启用」

2. **Cloudflare Tunnel 用途描述更新**（`token_dashboard_server.py`）
   - `LAUNCH_PURPOSE['com.openclaw.cloudflared']` 原描述：「量化看板 · Cloudflare Tunnel 外网穿透」
   - **更新**：「运维 · Cloudflare Tunnel 外网穿透（token-dashboard.crypto-signal.work / quant-dashboard）」
   - 原因：该 tunnel 同时服务于 token-dashboard 公网域名和量化看板，描述需准确

3. **Cron总览页面新增心跳配置板块**（`cron.html`）
   - 原 `/cron` 页面只有 OpenClaw Cron / 系统 Crontab / LaunchAgent 三个板块
   - **新增**：第四个「💓 心跳配置」板块，展示所有虾的心跳周期、投递目标、启停状态
   - 与主看板（`token_dashboard.html`）的心跳表格数据同源，保持一致

**验证结果**：
- ✅ 主看板 `/`：心跳表格显示12只虾，罗氏虾「已关闭 ○ 关闭」、基围虾「360m telegram ● 启用」
- ✅ `/cron` 页面：新增心跳板块，数据与主看板一致
- ✅ LaunchAgent 列表：`com.openclaw.cloudflared` 描述更新为含 token-dashboard 域名
- ✅ `/cron-json` API：返回全部12条 heartbeat 记录（含已关闭的）

**服务重启**：`launchctl unload/load com.openclaw.token-dashboard`，服务正常启动（PID 54896）

**执行人**：基围虾（CTO）

---

### 2026-07-15 (#7) — Token看板公网部署 + HTTP Basic Auth密码保护（基围虾 / 匡书记指令）

**背景**：Token看板原先仅本地访问（localhost:18888），匡书记要求随时随地查看虾厂运行状态。

**部署内容**：
- 🌐 **公网域名**：`https://token-dashboard.crypto-signal.work`
- 🔒 **HTTP Basic Auth**：用户名 `kuangsiyu`，密码从量化看板 `.env` 读取（`DASHBOARD_PASSWORD`），不硬编码
- 🛡️ **密码保护范围**：全部路径（/、/providers、/agent-status、/cron-json、/health-json、静态文件等）
- 🔄 **Cloudflare Tunnel**：复用现有 `quant-dashboard` tunnel，新增 ingress 规则 `token-dashboard.crypto-signal.work → http://localhost:18888`
- 📋 **DNS记录**：通过 Cloudflare API 添加 CNAME → `4ebc3299-4133-4f10-89a1-b08f862cc683.cfargotunnel.com`

**技术实现**：
- `token_dashboard_server.py`：
  - 导入 `base64` 模块
  - 新增 `_AUTH_ENV_PATH`、`_BASIC_AUTH_USER`、`_BASIC_AUTH_PASS`
  - 新增 `_load_auth_password()`：从量化看板 `.env` 读取 `DASHBOARD_PASSWORD`
  - 新增 `_check_basic_auth()`：解析 Authorization header，验证用户名密码
  - 新增 `_send_auth_challenge()`：返回 401 + `WWW-Authenticate: Basic realm="Token Dashboard"`
  - 新增 `_require_auth()`：统一认证检查，未通过返回 401
    - **本地访问白名单**：直接访问 `127.0.0.1`/`localhost`（无 `CF-Connecting-IP` 头）免认证，方便调试
    - **Cloudflare Tunnel 请求**：带 `CF-Connecting-IP` 头，需要认证
  - `do_GET()` 入口：所有请求先过 `_require_auth()`，未认证直接返回 401
  - 静态文件分支（`super().do_GET()`）：同样先过认证

**验证结果**：
- ✅ `curl http://127.0.0.1:18888/` → 200（本地免认证）
- ✅ `curl -u kuangsiyu:shrimp2026 https://token-dashboard.crypto-signal.work/` → 200
- ✅ 浏览器访问 → 弹出密码框，输入后正常显示看板
- ✅ `curl https://token-dashboard.crypto-signal.work/`（无密码）→ 401
- ✅ `/providers`、`/agent-status` 等 API 端点同样受保护

**运维备注**：
- 密码变更：修改量化看板 `.env` 的 `DASHBOARD_PASSWORD`，重启 `token-dashboard` 服务生效
- 回滚：删除 `~/.cloudflared/config.yml` 中 `token-dashboard` ingress 规则 + DNS记录，重启 cloudflared
- 本地访问仍可用：`http://localhost:18888`（无认证，方便调试）

**执行人**：基围虾（CTO）

---

### 2026-06-29 (#6) — 加入 Kimi（Moonshot）Coding Plan 额度支持（匡书记指令）

**背景**：Kimi 当前在用（`kimi/kimi-for-coding`），但 Token 看板不显示其额度。

**踩坑（2026-06-29 当日发现）**：
- Kimi 有两套完全独立的 Key 体系：**Coding Plan Key**（`sk-kimi-...`，72字符，用于 Coding/对话）和 **Open Platform Key**（`sk-...`，48字符，用于普通API按量付费），**不可混用**
- 普通余额端点 `api.moonshot.cn/v1/users/me/balance` 对 Coding Plan 用户永远返回0，必须用 Coding Plan 专用额度端点 `api.kimi.com/coding/v1/usages`
- **必须带 User-Agent: `KimiCLI/1.6`**，否则 401
- 端点返回：周额度（7天滚动）+ 5分钟频限 + 并行限制

**后端 token_dashboard_server.py：**
- `PROVIDER_QUOTA_API` 新增 `api.kimi.com` → `kimi_coding_plan` 映射
- `PROVIDER_LABELS` 新增 `kimi: 'Kimi（Moonshot）'`
- `_get_providers()`：kimi provider 优先匹配 Coding Plan 额度 API（baseUrl 与额度端点 host 不同，走 pid 硬匹配）
- `_proxy_quota()`：kimi.com 加入代理列表；新增 User-Agent 注入逻辑（`KimiCLI/1.6`）

**前端 token_dashboard.html：**
- CSS `.provider-tag-kimi` 粉色样式
- `PROVIDER_TAG_MAP` 新增 `kimi`
- `MODEL_COLORS` 新增 kimi/moonshot 粉色配色
- `modelAbbr()` 返回 `'Kimi'`
- `fetchProviderQuotas()` 新增 `kimi_coding_plan` 分支：渲染周额度进度条 + 5分钟频限 + 下次重置时间

**文档**：README.md 新增「Provider 踩坑记录」章节，记录 Kimi 两套 Key 体系差异及额度查询注意事项。

**审核**：对焦虾四维审查通过（0 红色阻断，3 项 🟡 建议非阻塞）。

---

### 2026-06-25 (#5) — 模型分布区重构 + 海外区分 + subagent 合并（基围虾 / 匡书记指令）

本轮匡书记实时迭代，分 4 次送审（v1-v4 全部对焦虾交叉审核通过，0 红色阻断）。

**前端 token_dashboard.html：**
- 🐛 **空数据兜底**（v1）：`renderModelArea` 无近期调用时不再 `return null`（整块消失），改渲染灰色「暂无调用」占位 chip + 灰条，保持所有卡片视觉一致
- 🎨 **卡片对齐**（v2）：固定 `.ac-model` 34px + `.ac-model-chips` 单行（nowrap+overflow hidden）消除 chip 数量导致的高度跳变；`!s`（无 session）分支去「状态」行 + 补上下文条，与 `s` 分支结构对齐 → 全部卡片模型条/上下文条横向对齐（DOM 实测 mbarTop 全 76、ctxTop 全 131）
- 🔄 **模型区重写**（v3）：最新调用的模型排最左（不再按次数）+ 最左 chip 加循环光扫（标识“当前在跑”）；模型≥3 时压缩成「色点+首字母」(DS/GLM/QW…，hover 看全名)；删除时间戳（最左即最新，信息冗余）→ 顺带解决多 chip 挤压
- 🌐 **海外/国内区分**（v4）：
  - 额度卡片：海外智谱（name 含“海外”）加蓝色左边框 + 蓝标题 + 🌐 徽章
  - 模型 chip：provider==='zai'（海外端点）的 chip 加 🌐（如“glm-5.2×10 🌐”）
  - 空兜底：modelProvider==='zai' 也标 🌐
- 🧹 清理 dead code：`relTimeShort` 函数 + `.ac-model-time` CSS

**服务端 token_dashboard_server.py（v4）：**
- 🔗 **subagent 模型合并**：`_recent_model_dist` 现合并主 session + 所有 subagent session 的 transcript，按时间统一排序后取最近 N 条统计 → agent 的模型 chip 区包含分身的模型调用（“谁在干活、用什么模型”，GLM/DS 限流时一眼看到分身切了什么模型）
  - 抽取 `_read_session_transcript` 供主/子复用；`agent_info` 收集 `subagentSessionIds`；调用点 `sorted(set(...))` 去重防双倍计数
  - cache_key 加 `|subs:` 后缀区分主-only 与合并路径
- `_last_actual_model` 现为零调用 dead code（向后兼容保留）

**审核**：v1/v2/v3/v4 共 4 轮对焦虾交叉审核全部 ✅ 通过（0 红色阻断），dead code/typo/去重等 advisory 已采纳。

**运维**：diff 命令退出码 1（文件有差异）被 exec 误显 ⚠，以后 diff 加 `|| true` 规避（非 bug）。

### 2026-06-24 (#4) — html 覆盖事故修复 + 源码去分叉（基围虾 / 匡书记指令）

- 🐛 **html 改动覆盖事故**：罗氏虾改「模型分布」功能时基于旧版 html，覆盖了基围虾 19:26 加的 minimax 前端改动（MiniMax卡片/nav/百炼卡片全丢）。根因：多 agent 改同一 html + 物理双份分叉。
- 🔧 **合并修复**：以罗氏虾模型分布版为基底，重新合入 minimax_quota + no_quota(百炼) + nav 三处，适配罗氏虾重构后的 renderQuotaCard(name,subtitle,limits,level) 4参签名。四块功能共存无破坏，JS node --check 通过。
- 🧹 **源码去分叉**：删 3 个僵尸副本（scripts/server.py、projects/html、projects/push）+ 6 个 .bak，每文件物理唯一。拓扑：server.py@projects(git管理,plist跑)、html+push@scripts(DIR/cron指)。
- ⚠️ **中途事故**：python open(w) 无 encoding 把 html 写空过一次（surrogate崩溃+'w'截断），从备份恢复干净。教训已记 MEMORY.md。
- ✅ **对焦虾审核通过**（临时审核 v1，三块全核实属实无破坏）。
- 📋 遗留 advisory（后续处理）：①renderQuotaCard 的 level 死参数可移除 ②no_quota 文案硬编码「阿里云控制台」③README systemd 部署示例漂移（应改 launchctl/openclaw cron）。
- 🐞 暴露 review_tracker 计数 bug：技术故障(限流)被算进内容不通过轮次，致 self_check 误判，待修。

### 2026-06-23 (#2) — MiniMax 接入 + 模型显示修复（基围虾 / 匡书记指令）

- 🆕 **新增 MiniMax（编程套餐）额度监测**（看板 + TG推送）
  - 端点：`GET https://api.minimaxi.com/v1/token_plan/remains`，Bearer key，**走代理**（实测直连频繁超时，`_proxy_quota` 条件需含 `'minimaxi' in api_url`）
  - 返回 `model_remains[]`：`model_name=general`(文本/代码,关注这个) / `video`(视频,不用)
  - 字段：`current_interval_remaining_percent`(5h窗口剩余%) / `current_weekly_remaining_percent`(周剩余%) / `end_time`/`weekly_end_time`(ms,=nextReset)
  - ⚠️ **方向坑**：MiniMax 给「剩余%」，智谱给「已用%」→ 集成时 `已用%=100-剩余%`
  - 5h窗口=18000000ms 正好匹配 `PERIOD_MS[3]`，周=604800000ms 匹配 `PERIOD_MS[6]`，可直接复用 `renderQuotaCard`/颜色/倒计时逻辑
  - 改动：server `PROVIDER_QUOTA_API`+`PROVIDER_LABELS` 加 minimax；html `fetchProviderQuotas` 加 `minimax_quota` 分支；push 同步加 minimax 分支

- 🐛 **修复 /agent-status 模型显示不准 + 无 provider 标签 + 跳变**
  - **根因1**：原 `agent_info[aid]['model']=s.get('model')` 取的是 session **配置/默认模型**，不是实际调用模型（例：实际用 MiniMax-M3 但显示成默认 ds-pro），且未返回 `modelProvider`，前端 `providerTag(s.modelProvider)` 永远为空
  - **根因2**：原取「updatedAt 最大的 session」含 subagent/心跳/slash，模型随后台任务跳变
  - **修复**：移植推送脚本的 `_last_actual_model()` 从 transcript jsonl 读最后一条 assistant 的真实 model/provider（带30s缓存）；模型来源限定 **direct 主会话**（排除 subagent/heartbeat/slash/main），返回 `modelProvider` 供前端渲染 tag（含 minimax tag，前端 CSS/JS 已就绪）
  - **遗留语义**：一个 agent 常有多个 direct 会话（TG/微信/跨虾）跑不同模型，「最新主会话」仍会随实时活动切换；如需固定某一语义（如仅 owner TG 主线 / 默认模型 / 模型集合）待匡书记定

- 🔴 **关键运维坑（上次崩溃根因，务必记牢）**
  - **看板有两份源码且已分叉**：
    - 运行的后端 = `projects/token-dashboard/token_dashboard_server.py`（plist `WorkingDirectory` 指此，相对路径启动）— git 权威源码
    - `scripts/token_dashboard_server.py` 是部署副本，**不在跑**
    - 运行的前端 = `scripts/token_dashboard.html`（server 里 `DIR` 硬编码 `scripts/`）
    - push 实际跑的是 `scripts/token_usage_push.py`（openclaw cron `[jiweixia] Token用量推送` 触发），projects 那份不在跑
    - ⚠️ 改文件前先确认「谁在跑」：后端改 projects/，前端/推送改 scripts/。否则改了不生效（上次凌晨即栽在此，改了 scripts/ 但跑的是 projects/）
  - **服务由 launchctl 管**（不是 systemd！旧 SOP 写错）：label `com.openclaw.token-dashboard`，重启 `launchctl kickstart -k gui/$(id -u)/com.openclaw.token-dashboard`
  - plist 无 HTTP_PROXY 环境变量 → server 内需在 `_proxy_quota` 显式判断走代理的外语域名：`z.ai`/`deepseek`/`minimaxi` 走 `_PROXY_OPENER`，`bigmodel`（国内CDN稳定）走直连 `urlopen`
  - 顺手修复：重启时清掉了一个卡死跑满 99.6% CPU 的旧看板进程
  - 执行人：基围虾（CTO）

### 2026-06-23
- 🐛 **修复 subagent 模型思考期间被误判为"等待中"**
  - **根因**：模型思考（thinking）期间30-90秒不产生 WS 事件也不更新 updatedAt，
    原 WS TTL=10秒、updatedAt 阈值=20秒，思考中的 agent 被误判为"等待中"
  - **典型场景**：扇贝 spawn 的 subagent 在模型思考时，看板显示"等待中"而非"工作中"
  - **修复**：WS TTL 10秒→90秒，updatedAt working 阈值 20秒→90秒
  - **效果**：覆盖 GLM-5.2 thinking 模式的完整窗口（30-90秒），思考期间持续显示"工作中"
  - **代价**：agent 停止后最长 90秒+15秒轮询=105秒才显示"等待中"（可接受）
  - 执行人：基围虾（subagent）

### 2026-06-20 (#3)
- 🐛 **第五轮：事件监听循环仍不工作，决定整体重写**
  - 根因1：subscribe响应被插队health事件吃掉（已修）
  - 根因2：`except Exception: break` 静默退出（已修：改continue）
  - 根因3（未解决）：subscribe ok=True后事件监听循环仍0事件。CEO独立测试6个事件正常，看板代码0个。怀疑代码结构/缩进问题
  - 决策：停止修补，基围虾用CEO验证过的脚本逻辑整体重写 `_ws_listener_loop()`

### 2026-06-20
- 🔧 优化agent停止检测延迟：WS TTL 30s→10s，updatedAt working阈值 60s→20s
  - 改前：agent停止后最长61秒才检测到
  - 改后：agent停止后最长35秒检测到（20s降waiting + 15s前端轮询）
  - idle阈值600s不变
  - 执行人：基围虾（subagent）

### 2026-06-18
- 新增海马（haima）— CFO（财税+法务+预算），模型 zhipu/glm-5.2，fallback deepseek/deepseek-v4-pro，Bot @ksiyu_011_bot
- 扇贝（shanbei）职责调整 — 原CFO → 现纯量化/股票/虚拟货币
- 执行人：CEO罗氏虾（通过sessions_spawn）

### 2026-06-18 (#2)
- 🆕 海星项目新增2条Cron：
  - **AI影视数据采集**：每日 05:00，agent haixing（海星），model zhipu/glm-5.2，fallback deepseek/deepseek-v4-flash，delivery via @haixing_bot
  - **网站健康检查**：每3小时，agent haixing（海星），model zhipu/glm-5.2，fallback deepseek/deepseek-v4-flash，delivery via @haixing_bot
- 修复：网站健康检查 cron agentId 从 main 改为 haixing（统一归属海星项目）
- 来源：CTO基围虾（匡书记批准）

### 2026-06-20 (#1)
- 🐛 **修复 WS 监听死功能**（重大修复）
  - **根因1**：`connect` RPC 使用 `minProtocol: 4`，但 Gateway 要求 `minProtocol: 3` 才能通过验证。原代码 connect 返回 `ok: false` 后仍继续执行，导致 WS 实际未认证成功
  - **根因2**：connect 成功后未调用 subscribe（当时用的 `chat.subscribe` 方法名有误，见 #2 修复）
  - **根因3**：connect 响应未检查 `ok` 字段，只检查了 `type == 'res'`
  - **修复**：①`minProtocol` 改为 3 ②connect 后追加 subscribe 调用 ③配置 `logging.basicConfig` 使 WS 调试日志可见
  - 验证：重启后 `wsConnected: True`，日志可见 subscribe 响应，事件流正常接收（agent/health/presence 事件）
  - 执行人：基围虾（subagent）

### 2026-06-20 (#2)
- 🐛 **修复 WS 监听方法名错误 + scope缺失**（CEO实测发现）
  - **根因1**：上次修复用的 `chat.subscribe` 方法 Gateway 根本不支持（返回 `unknown method: chat.subscribe`），正确方法是 **`sessions.subscribe`**
  - **根因2**：scopes 缺少 `operator.admin`，导致权限不足
  - **修复**：①`chat.subscribe` → `sessions.subscribe` ②scopes 追加 `operator.admin` ③新增 `session.message` 和 `sessions.changed` 事件监听（subscribe 后实际推送的事件类型）
  - 验证：15秒内收到9个非health事件（session.tool/session.message/agent），涵盖 jiweixia 和 pipixia
  - 执行人：基围虾（subagent）

### 2026-06-23 (#3) — 看板 Crash 修复 + 安全审计整改（基围虾 / 匡书记指令）

- 🐛 **修复看板内存泄漏（RSS 2GB → 34MB）**
  - 根因1：`_model_cache` 无界增长（session_id=UUID 永不重复），每次 Gateway 重启新增一批 → 加 `_MODEL_CACHE_MAX=256` + LRU 淘汰
  - 根因2：`save_snapshot` 无裁剪 → 加 `_SNAPSHOT_MAX_KEYS=500`
  - 根因3（4h后引入的新 bug!）：`_trim_snapshot_data` 函数定义丢失 → 补回
  - plist 加软件 RSS 512MB / 硬件 RSS 1GB；/dev/null → 日志文件

- 🔴 **修复 WS 死循环内存泄漏（50 分钟 RSS 2GB，看板反复崩溃的终极根因）**
  - 代码：事件监听循环 `except Exception: _log_ws_error(e); continue  # 不能 break，要 continue`
  - 机制：Gateway 重启 → WS 断开 → `ws.recv()` 抛异常 → `continue` 回到已死的 socket → 立即再抛 → 死循环，每秒数千次异常对象堆积
  - 修复：`continue` → `break`，让外层 10s 重连循环接管
  - 教训：LLM 过度防御注释「不能 break」是反直觉的陷阱——`continue` 不是不退出，是退回去再跑一遍，连接死后就是永动机
  - 代码来源：2026-06-20 WS 监听器重写时加入，始终未 commit git（Not Committed Yet），作者不可追溯（推测 subagent 生成）

- 🆕 **新增百炼 DashScope 占位卡片**（社区实锤无余额 API，no_quota 类型）

- 🔴 **P0 Bot Token 明文清理**（对焦虾审计）
  - 6 处明文改为从 `.bot_token_jiweixia`（600）读；api-keys_SOP.md.bak trash

- 🐛 **P1 修复**：死代码标记、Direct heartbeat 对齐、cron 重复删除、定时启停

- 🎨 主页加 `📋 定时任务总览` 导航链接 → /cron 页面

- 📝 执行人：基围虾  / 审计人：对焦虾（v4）

## 2026-06-27 邮件监听模块新增

- **新增文件**：`email_monitor.py` — 每30分钟检查Claw邮箱未读邮件
- **功能**：检测到新邮件时，通过CTO Bot推送给匡书记TG私聊 + token看板显示邮件状态
- **去重**：首次运行初始化基线（不推历史邮件），后续只推真正新到的
- **看板**：新增 `/email-status` API端点 + HTML看板「📬 邮箱监控」卡片（每2分钟刷新）
- **Cron**：`*/30 * * * * python3 email_monitor.py`
- **执行人**：基围虾（CTO）
- **原因**：匡书记要求邮件提醒融合到token-dashboard项目（不是量化信息看板）

### 2026-07-22 (#9) — 修复单线程假死：HTTPServer→ThreadingHTTPServer + quota超时防御（海螺）

**背景**：Token看板使用 `http.server.HTTPServer` 单线程模型，上游API慢响应时阻塞所有请求导致502。

**改动内容**：
1. **HTTPServer → ThreadingHTTPServer**（第1647行）：每个HTTP请求在独立线程处理，上游慢响应不再阻塞其他请求。ThreadingHTTPServer 是 Python 3.7+ 标准库，无需额外依赖。
2. **_proxy_quota timeout 10→5秒**（第1298/1301行）：缩短上游API等待时间。
3. **新增504超时处理**（第1308-1314行）：`urllib.error.URLError` 和 `TimeoutError` 单独捕获，返回 HTTP 504 + `{"error":"上游API超时","cached":false}`，不触发通用502异常路径。

**不改动**：前端轮询、WebSocket监听线程、memory_watchdog、其他路由。

**执行人**：海螺（后端开发）
**原因**：单线程模型导致上游慢响应时整个看板假死502

### 2026-07-22 (#9) — 后端性能优化7项升级（海螺 / 匡书记指令）

**背景**：Token看板经CEO通读+CTO评审，确定7项P0+P1改动，提升性能与安全性。

**变更内容**：

1. **新增@cached通用缓存装饰器**（P0）
   - 新增 `import functools`，新增 `cached(ttl_seconds)` 线程安全缓存装饰器
   - 减少重复subprocess调用

2. **5个数据源函数加缓存**（P0）
   - `_get_system_health()` → `@cached(30)`
   - `_get_openclaw_cron()` → `@cached(60)`
   - `_get_crontab()` → `@cached(60)`
   - `_get_heartbeats()` → `@cached(60)`
   - `_get_launch_agents()` → `@cached(60)`

3. **SQLite查询加LIMIT 30**（P0+）
   - transcript_events查询从全量→ `ORDER BY seq DESC LIMIT 30` + 反转恢复正序
   - 防止超长session拖慢看板

4. **Quota API缓存+超时fallback**（P0）
   - 新增 `_quota_cache` 模块级缓存（60s TTL）
   - 超时/错误时fallback到上次成功缓存（标记`cached:true`），无缓存才返回504
   - 不再裸504

5. **CORS收紧**（P1安全）
   - 新增 `_ALLOWED_ORIGINS` 白名单 + `_cors_origin()` 辅助函数
   - 所有 `*` → 动态判断，只允许 `127.0.0.1:18888` 和 Cloudflare Tunnel域名

6. **AGENT_NAMES去重**（P1）
   - 删除 `_get_heartbeats()` 内部重复定义的 `AGENT_NAMES` dict，统一用全局定义

7. **plistlib替代PlistBuddy**（P1）
   - `_get_launch_agents()` 改用 `plistlib.load()` 一次性读plist
   - 从15个plist × 4次subprocess = 60次 → 0次subprocess

**影响范围**：仅Token看板自身，不影响Gateway/其他虾/外部服务

---

## 2026-07-22 Session持久化（方案A）— 海螺

**问题**：登录session只存内存字典 `_sessions`，看板每次重启session全丢，用户Cookie还在但服务器找不到token，被迫重新登录。

**改动**（仅 `token_dashboard_server.py`）：
1. 新增 `_SESSION_FILE` 常量 → `data/sessions.json`
2. 新增 `_save_sessions()`：内存session同步写磁盘JSON（带锁，异常只warning不炸）
3. 新增 `_load_sessions()`：启动时从磁盘加载，自动过滤过期session
4. 启动流程：`_load_auth_password()` 后调用 `_load_sessions()`
5. 写盘时机：`_create_session()` / `_check_session()`删过期 / `_cleanup_expired_sessions()` / `/logout` 共4处

**不改**：Cookie设置逻辑（Max-Age/SameSite）、认证流程、前端。

**验证**：
- `py_compile` 通过；grep确认7处改动全部落位
- 登录 → `data/sessions.json` 生成 ✓
- kill进程 → launchd自动重启 → 旧Cookie访问仍200（连续两轮验证）✓
- 注意：`_load_sessions()` 的INFO日志在 `logging.basicConfig` 之前执行，被默认WARNING阈值吞掉，日志里看不到「从磁盘加载了N个session」属正常现象，功能不受影响

**影响范围**：仅Token看板自身，不影响Gateway/其他虾/外部服务

---

## 2026-07-22 更正：方案A已被JWT无状态方案取代 — 海螺

**情况**：海螺在执行方案A（JSON文件持久化）过程中，发现 `token_dashboard_server.py` 于22:06被替换为 **JWT无状态认证方案**（HS256签名，密钥从DASHBOARD_PASSWORD派生，服务端零存储）。方案A的改动（_SESSION_FILE/_save_sessions/_load_sessions）已不在当前代码中。

**JWT方案验证结果**（海螺实测）：
- `py_compile` 通过
- 本地127.0.0.1免认证（设计如此，`CF-Connecting-IP`头模拟Tunnel流量才可测认证链）
- 模拟Tunnel无cookie → 302跳登录 ✓
- 模拟Tunnel带JWT cookie → 200 ✓
- kill进程（PID 17481）→ launchd自动重启（PID 18725）→ 同一JWT仍200 ✓（重启不丢登录态，问题已解决）

**遗留**：
- `data/sessions.json` 是方案A测试的残留文件，JWT方案下已无用，可删
- 方案A的审查任务（对焦虾）基于旧代码，结论不适用当前JWT版本，JWT版本建议重新送审

### 2026-07-23 (#10) — MiniMax 编程套餐到期下线（基围虾 / 匡书记指令）

**背景**：MiniMax 编程套餐即将到期不续费，需要从看板上去掉，后端不抓数据。代码注释保留备用。

**变更内容**：

1. **后端 `token_dashboard_server.py`**
   - `PROVIDER_QUOTA_API` 字典：注释掉 `'api.minimaxi.com'` 行
   - `PROVIDER_LABELS` 字典：注释掉 `'minimax'` 行
   - `_proxy_quota` 代理判断：去掉 `'minimaxi' in api_url` 条件（注释说明）

2. **前端 `token_dashboard.html`**
   - CSS：注释掉 `.provider-tag-minimax` 样式
   - `PROVIDER_LABELS`（PROVIDER_TAGS）：注释掉 `minimax` 条目
   - `MODEL_COLORS`：注释掉 `minimax` 颜色映射
   - `modelAbbr()`：注释掉 `if (m.includes('minimax'))` 分支
   - `fetchProviderQuotas()`：注释掉 `else if (p.quotaType === 'minimax_quota')` 整个渲染分支

3. **备注**：所有代码注释保留，未删除，如需恢复取消注释即可

**决策来源**：匡书记指令

**执行人**：基围虾（CTO）

## 2026-09-03 安全加固三连（issue-0235 对焦虾第1轮历史欠账清偿）— 基围虾

**改动**（token_dashboard_server.py）：
1. **R1 密码比较常量时间化**：do_POST 登录校验 `==` 改 `hmac.compare_digest`（用户名+密码双字段），防时序侧信道
2. **R2 JWT 密钥 PBKDF2 派生**：`_JWT_SECRET` 由密码原文改为 `hashlib.pbkdf2_hmac('sha256', 密码, b'token-dashboard-jwt', 100000)`
   - ⚠️ **副作用**：本次部署（2026-09-03 22:5x）后所有现存登录态失效，各用户需重新登录一次（一次性代价）
3. **R3 Cookie 动态 Secure**：登录/登出 Set-Cookie 检测 `X-Forwarded-Proto: https` 或 `CF-Connecting-IP`（Cloudflare Tunnel 特征）时追加 `; Secure`；本地 http://127.0.0.1:18888 不加——对焦虾特别标注禁止一刀切（会使本地登录失效）

**恢复方法**：git 回滚本次改动即可；注意 R2 回滚后 JWT 密钥还原，同样会触发一次全员重新登录

**决策来源**：对焦虾第 1 轮审核红色项（v5 报告 2026-07-22 Y1-Y3 欠账升级，规则"欠账不消不得 pass"）+ CEO 预授权「red 则按流程修复重审」

**执行人**：基围虾（CTO）
