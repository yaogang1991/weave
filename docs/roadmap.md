# Weave Milestone Roadmap

---

**最后更新:** 2026-05-29
**当前版本:** M6.9 (OTEL trace propagation to CLI subprocess)

---

## 概览

| 里程碑 | 目标 | 状态 | 完成日期 |
|--------|------|------|----------|
| **M1** | 单用户无人值守任务执行系统 | ✅ 已完成 | 2026-05-09 |
| **M1.1** | 稳定化 — 审批票据化 + 统一 Guardrail 入口 | ✅ 已完成 | 2026-05-09 |
| **M2** | 单用户高可靠自治 — 健康检查 + 隔离后端 + Web 控制台 | ✅ 已完成 | 2026-05-10 |
| **M3.0** | 知识系统 — SPECs, ADRs, 知识索引 | ✅ 已完成 | 2026-05-10 |
| **M3.1** | 多模型路由 — 按 Agent 类型分配模型 | ✅ 已完成 | 2026-05-10 |
| **M3.2** | Agent 记忆 — 持久化跨任务/跨会话记忆系统 | ✅ 已完成 | 2026-05-10 |
| **M3.3** | 自学习 — 执行模式分析 + 自动优化 | ✅ 已完成 | 2026-05-10 |
| **M3.4** | DAG 模板 — 可复用 YAML 模板 + 变量替换 | ✅ 已完成 | 2026-05-10 |
| **M3.5** | 影响分析 — 变更影响预测 + 依赖图 + 变更验证 | ✅ 已完成 | 2026-05-10 |
| **Refactor** | Execution Hooks — 解耦子系统为生命周期回调 | ✅ 已完成 | 2026-05-11 |
| **Refactor** | 大文件拆分 — models/main/worker/engine 等 | ✅ 已完成 | 2026-05-15 |
| **M3.6** | MCP Client + Skills — MCP 工具发现 + YAML 技能定义 | ✅ 已完成 | 2026-05-15 |
| **Security** | Phase 1 — 凭证隔离 + 不可变状态 (#456, #457) | ✅ 已完成 | 2026-05-16 |
| **Infra** | Apache-2.0 许可证 + CONTRIBUTING.md + PR 模板 | ✅ 已完成 | 2026-05-17 |
| **M4.0** | 项目理解 — 项目 Onboarder（技术栈检测 + 模块摘要 + 约定提取） | 🔲 规划中 | — |
| **M4.1** | 项目理解 — 增强依赖图（tree-sitter 多语言 + 7 种边类型） | 🔲 规划中 | — |
| **M4.2** | 项目理解 — 上下文路由（BM25 + 图距离融合 + Token 预算） | 🔲 规划中 | — |
| **M4.3** | 项目理解 — 项目感知代理（Viewer/Editor 分离 + 约定感知 Prompt） | 🔲 规划中 | — |
| **M6.1** | BackendContext 扩展 + 默认 backend 切换到 claude_code | ✅ 已完成 | 2026-05-27 |
| **M6.2** | Node Guardrails + stderr tail + semantic timeout | ✅ 已完成 | 2026-05-28 |
| **M6.3** | LightweightLLMCaller + BuiltinBackend/BackendRegistry 重构 | ✅ 已完成 | 2026-05-28 |
| **M6.4** | 清理 + 文档更新 | ✅ 已完成 | 2026-05-29 |
| **M6.5** | Stream-JSON event parsing for ClaudeCodeBackend CLI path | ✅ 已完成 | 2026-05-28 |
| **M6.6** | Node timeout semantic (progress-driven) | ✅ 已完成 | 2026-05-28 |
| **M6.7** | Session Resume + BackendResult 扩展 + bidirectional comms | ✅ 已完成 | 2026-05-29 |
| **M6.8** | MCP Config 传递到外部 Backend | ✅ 已完成 | 2026-05-29 |
| **M6.9** | OTEL trace propagation to CLI subprocess | ✅ 已完成 | 2026-05-29 |

---

## M1 — Personal Edition

**目标:** 单用户、CLI 驱动的多 Agent 软件开发系统，实现"扔任务进去，系统自动跑完"。

### Stage 1: Foundation (Tasks 01-03)
- **Task 01** ✅ `docs/m1_personal_spec.md` — 范围、状态机、接口、DoD
- **Task 02** ✅ `control_plane/models.py` — JobStatus, RunStatus, RetryPolicy, Job, Run 模型
- **Task 03** ✅ `control_plane/repository.py` — 原子写入持久化存储与状态转换

### Stage 2: Control Plane Core (Tasks 04-06)
- **Task 04** ✅ `main.py` CLI 命令 — submit/status/list/cancel
- **Task 05** ✅ `control_plane/service.py` — 提取执行服务供复用
- **Task 06** ✅ `control_plane/worker.py` — Worker 循环 + Lease 机制

### Stage 3: Reliability & Replan (Tasks 07-08)
- **Task 07** ✅ 超时/重试/死信队列
- **Task 08** ✅ Replan 闭环 — 保留成功节点，max_replans 限制

### Stage 4: Guardrails & Monitoring (Tasks 09-11)
- **Task 09** ✅ 个人模式 Guardrails — 风险等级、确认流程、白名单
- **Task 10** ✅ 指标聚合 — 成功率、延迟 P95、重试率、失败 TopN
- **Task 11** ✅ 告警 — 连续失败、时长阈值、死信 Webhook

### Stage 5: Recovery & Polish (Tasks 12-14)
- **Task 12** ✅ 重启恢复 — 孤儿 Job 清理、Lease 过期处理
- **Task 13** ✅ 核心路径测试套件
- **Task 14** ✅ 文档更新

---

## M1.1 — Stabilization

**目标:** 把 M1 的 85%~90% 完成度提升到 100%，聚焦审批票据化、统一 Guardrail 入口、文档真值化。

### Stage 1: Task 1 — 审批票据持久化
- ✅ `control_plane/approval.py` — ApprovalTicket 模型 + ApprovalRepository
- ✅ TicketStatus 枚举: pending/approved/rejected/expired
- ✅ Worker 重启后识别并处理 pending ticket

### Stage 2: Task 2 + 3 — 统一入口 + 非交互模式
- ✅ `guardrails/policy.py` — 统一 `guarded_execute()` 三态返回: allowed/blocked/pending_approval
- ✅ 非交互模式 — `--non-interactive` / `WEAVE_NON_INTERACTIVE`
- ✅ 高风险操作自动创建 pending ticket

### Stage 3: Task 4 + 5 — CLI 工具链 + 运行态恢复
- ✅ `tickets`/`approve`/`reject` CLI 命令
- ✅ Worker 启动恢复扫描 pending ticket
- ✅ `resume_run()` 审批中断后自动恢复

### Stage 4: Task 6 + 7 — 指标升级 + 文档真值化
- ✅ 审批维度指标: approval_pending_count, approval_avg_wait_sec
- ✅ 告警规则: pending_approvals_over_threshold
- ✅ 文档 Implemented/Partial/Planned 标签化
- ✅ 合同测试: CLI 命令存在、状态枚举完整、错误码存在

---

## M2 — Single-User High-Reliability Autonomy

**目标:** 从"能自动跑"升级为"能长期稳定跑 + 快速恢复 + 最小化可视控制面"。

### M2.0 (P0) — Health Check + Watchdog + Fail-Fast
**目标:** Watchdog 检测 hang agent，阈值内（<30s）自动进入失败链。

- ✅ `core/models.py` — NodeHealth 模型 + 心跳协议
  - NodeHealth 枚举: HEALTHY / MISSED / UNHEALTHY / DEAD
  - 心跳事件: node.heartbeat, node.heartbeat_missed, node.unhealthy_killed
  - DAGNode.last_heartbeat_at, DAGNode.health_status
- ✅ `core/dag_engine.py` — Watchdog 协程
  - 监控运行节点的 last_heartbeat_at
  - 阈值: heartbeat_interval(5s) × miss_threshold(3) ≈ 15s
  - 超时标记 unhealthy，触发失败处理器
- ✅ `monitoring/alerts.py` — 健康告警规则

### M2.1 (P0) — Worktree Backend
**目标:** 每个 Job/Run 在隔离的 git worktree 中执行，不污染主仓库。

- ✅ `backend/worktree.py` — Git worktree 隔离后端
  - create_worktree(job_id, run_id) → worktree_path
  - cleanup_worktree (成功时) / preserve_worktree (失败时调试)
- ✅ `backend/lifecycle.py` — BackendManager
  - 生命周期管理: create/use/cleanup/preserve
  - 配置驱动: 默认后端 + 按 Job 覆盖
  - 风险等级 → 后端映射 (HIGH → worktree)
  - 自动降级 (worktree → local)

### M2.2 (P1) — Execution Backend Abstraction
**目标:** 通过配置切换后端，业务逻辑无需改动。

- ✅ `backend/base.py` — ExecutionBackend 抽象接口
  - setup() / get_work_dir() / cleanup() / preserve() / is_available()
- ✅ `backend/local.py` — 本地直接执行后端
- ✅ `backend/docker_stub.py` — Docker 后端 stub（M3 实现）
- ✅ 配置路由: WEAVE_DEFAULT_BACKEND=local|worktree

### M2.3 (P1) — Web Console MVP
**目标:** 3 页面 + 5 操作，核心操作无需 CLI。

- ✅ `visualizer/server.py` — FastAPI Web 服务
  - WebSocket 实时 DAG 监控
  - Job/Run 管理 API (重试/取消)
  - 审批 Ticket 管理 API
  - 指标与告警端点
- ✅ `visualizer/static/index.html` — 主仪表盘
  - DAG 可视化 (vis-network)
  - 实时事件流
  - 节点详情面板
- ✅ `visualizer/static/console.html` — 管理控制台
  - Jobs/Runs/Tickets/Alerts 四页
  - 审批操作 (approve/reject)
  - 状态指示器

---

## M3.2 — Agent Memory

**目标:** 持久化跨任务、跨会话的 Agent 记忆系统，让 Agent 具备学习和记忆能力。

### 记忆架构

```
MemoryScope: PRIVATE (per-agent) → SESSION (session shared) → GLOBAL (cross-session)
MemoryType:  FACT | EXPERIENCE | PREFERENCE | CONTEXT
```

### 核心模块

- ✅ `core/models.py` — MemoryEntry, MemoryScope, MemoryType 模型 + EventType 扩展
- ✅ `core/config.py` — MemoryConfig 配置（TTL、容量、检索限制）
- ✅ `memory/store.py` — 原子写入持久化存储（文件级别隔离）
  - 目录布局: global/agents/{type}/sessions/{id}/
  - CRUD: store/get/update/delete
  - 查询: list_entries/search/get_relevant
  - 维护: cleanup_expired/enforce_limits/recompute_relevance
- ✅ `memory/manager.py` — 高层记忆操作接口
  - store_learning/store_task_outcome/store_preference
  - get_context_for_agent/format_memory_prompt
  - extract_and_store（自动从执行结果提取学习）
  - run_maintenance/get_stats
  - 关键词提取 + 相关度评分（recency × frequency × keyword_overlap）
- ✅ `memory/sharing.py` — 跨 Agent 记忆共享
  - share_with_downstream（DAG 节点间记忆传递）
  - promote_to_session/promote_to_global（记忆提升）
- ✅ `agent/agent_pool.py` — WorkerAgent 记忆注入/提取钩子
  - execute() 前注入记忆到 system prompt
  - execute() 后自动提取学习存储
- ✅ `core/dag_engine.py` — DAG 节点间记忆共享钩子
  - _collect_input_artifacts() 中触发 MemorySharing
- ✅ `control_plane/service.py` — MemoryManager 初始化与注入
- ✅ `main.py` — 5 个 CLI 命令: memory-search/list/stats/add/cleanup
- ✅ `visualizer/server.py` — 6 个 REST API 端点
- ✅ `tests/test_memory.py` — 63 个测试，覆盖率 90%

### 记忆注入流程

```
Agent 执行前: get_context_for_agent() → format_memory_prompt() → 注入 system prompt
Agent 执行后: extract_and_store() → 自动提取 fact/experience
DAG 节点间: share_with_downstream() → 上游记忆共享给下游 Agent
定期维护: cleanup_expired + enforce_limits + recompute_relevance
```

### 环境变量

- `WEAVE_MEMORY_PATH` — 存储路径（默认 ./data/memory）
- `WEAVE_MEMORY_ENABLED` — 启用/禁用（默认 true）
- `WEAVE_MEMORY_MAX_ENTRIES` — 每 Agent 最大条目数（默认 500）
- `WEAVE_MEMORY_MAX_LENGTH` — 内容最大长度（默认 1000 字符）
- `WEAVE_MEMORY_TTL_DAYS` — 默认过期天数（默认 90）
- `WEAVE_MEMORY_RETRIEVAL_LIMIT` — 每次注入最大条目数（默认 10）
- `WEAVE_MEMORY_DECAY_DAYS` — 相关度衰减半衰期（默认 30 天）

---

## M3.3 — Self-Learning

**目标:** 从执行历史中自动学习模式，优化编排策略。

### 核心模块

- ✅ `core/models.py` — LearningInsight, LearningCategory, InsightType 模型 + EventType 扩展
- ✅ `core/config.py` — LearningConfig 配置（分析间隔、置信度阈值、最大洞察数）
- ✅ `learning/analyzer.py` — 执行模式分析引擎
  - 失败模式分析（高频错误类别、低成功率 Agent）
  - 成功模式分析（有效策略识别）
  - Agent 性能分析（每 Agent 成功率、耗时）
  - 规划质量分析（DAG 结构 vs 成功率）
- ✅ `learning/optimizer.py` — 洞察 → 记忆转换
  - 高置信度洞察 → GLOBAL FACT 记忆
  - 反模式 → GLOBAL EXPERIENCE 记忆
  - Agent 特定洞察 → PRIVATE 记忆
  - `get_planning_hints()` — 编排器规划提示注入
- ✅ `learning/scheduler.py` — 定期分析调度
  - 基于间隔的自动分析触发
  - `run_analysis()` → analyze + optimize + store
  - 状态持久化
- ✅ `orchestrator/intelligent_orchestrator.py` — 学习提示注入到规划流程
- ✅ `control_plane/service.py` — LearningScheduler 初始化与调用
- ✅ `main.py` — 3 个 CLI 命令: learning-analyze/insights/status
- ✅ `visualizer/server.py` — 3 个 REST API 端点
- ✅ `tests/test_learning.py` — 33 个测试，覆盖率 91%

### 数据流

```
MetricsCollector + MemoryManager → Analyzer → Optimizer → MemoryManager
                                                        → Orchestrator (plan hints)
```

### 环境变量

- `WEAVE_LEARNING_PATH` — 学习数据路径（默认 ./data/learning）
- `WEAVE_LEARNING_ENABLED` — 启用/禁用（默认 true）

---

## M3.4 — DAG Templates

**目标:** 可复用 YAML 模板，支持变量替换，跳过 LLM 规划。

### 核心模块

- ✅ `core/models.py` — DAGTemplate 模型（name, description, version, category, variables, nodes, edges, reasoning_template）
- ✅ `templates/library.py` — TemplateRegistry 模板注册表
  - list_templates() — 发现所有 .yaml/.yml 模板
  - get_template(name) — 按名称加载（带缓存）
  - instantiate(name, variables) — 变量替换 → DAG
- ✅ `templates/*.yaml` — 7 个内置模板
  - build_api — 构建 REST API（4 节点）
  - add_feature — 添加新功能（3 节点）
  - fix_bug — 分析修复 Bug（3 节点）
  - refactor — 重构代码（4 节点）
  - add_tests — 添加测试（2 节点）
  - add_auth — 添加认证（4 节点）
  - setup_project — 项目脚手架（3 节点）
- ✅ `orchestrator/intelligent_orchestrator.py` — plan_from_template() 方法
- ✅ `main.py` — templates CLI 命令 + plan/run --template/--var 参数
- ✅ `visualizer/server.py` — 2 个 REST API 端点（GET /api/templates, POST /api/templates/{name}/instantiate）
- ✅ `tests/test_templates.py` — 41 个测试

### 使用方式

```bash
# 列出所有模板
python main.py templates

# 查看模板详情
python main.py templates --name build_api

# 使用模板规划
python main.py plan "Build API" --template build_api --var feature=Todo --var language=Python

# 使用模板执行
python main.py run "Build API" --template build_api --var feature=Todo
```

---

## M3.5 — Impact Analysis

**目标:** 执行前预测文件影响范围，执行后验证变更匹配度，持续学习提升预测准确度。

### 核心模块

- ✅ `core/models.py` — ImpactRiskLevel, ImpactScope, VerificationResult 模型 + EventType 扩展
- ✅ `core/config.py` — ImpactConfig 配置（覆盖率阈值、最大预测文件数）
- ✅ `analysis/dependency_graph.py` — 文件级依赖图（ast 解析 Python import）
  - build() — 扫描项目构建双向依赖图
  - get_dependents()/get_dependencies() — 传递性依赖查询
  - to_dict() — 序列化
- ✅ `analysis/impact_predictor.py` — 影响预测引擎
  - predict() — 关键词匹配 + 依赖图扩展 + 历史记忆查询
  - predict_static() — 纯静态分析（无需 LLM）
  - 风险等级计算: LOW/MEDIUM/HIGH/CRITICAL
- ✅ `analysis/change_verifier.py` — 变更验证
  - capture_snapshot() — 文件 mtime 快照
  - verify() — 比对预测与实际变更，计算覆盖率
- ✅ `control_plane/service.py` — 执行前后钩子（预测 + 验证 + 记忆存储）
- ✅ `main.py` — 3 个 CLI 命令: impact-predict/impact-graph/impact-history
- ✅ `visualizer/server.py` — 4 个 REST API 端点
- ✅ `tests/test_impact.py` — 41 个测试

### 数据流

```
Requirement → ImpactPredictor.predict() → ImpactScope
    ↓ 存入 job.metadata
Execute DAG → ChangeVerifier.verify() → VerificationResult
    ↓ 存入 GLOBAL EXPERIENCE 记忆
Future predictions improve via memory lookup
```

### 使用方式

```bash
# 预测影响范围
python main.py impact-predict "Fix bug in DAG engine" --project .

# 查看依赖图
python main.py impact-graph --project .

# 查看历史预测
python main.py impact-history
```

### 环境变量

- `WEAVE_IMPACT_PATH` — 分析数据路径（默认 ./data/impact）
- `WEAVE_IMPACT_ENABLED` — 启用/禁用（默认 true）

---

## Refactor — Execution Hooks

**目标:** 将 RunService 中耦合的记忆/学习/影响分析逻辑解耦为独立的生命周期回调，核心执行方法从 150+ 行缩减至 ~50 行。

### 重构内容

- ✅ `control_plane/hooks.py` — ExecutionContext + ExecutionHook ABC + 三个实现
  - `MemoryHook` — 创建 per-job MemoryManager，服务级维护仅运行一次（threading.Lock）
  - `LearningHook` — 触发学习分析，暴露 optimizer 给 Orchestrator（构造函数注入 repository）
  - `ImpactHook` — 执行前预测影响范围，执行后验证变更（构造函数注入 llm_config）
- ✅ `control_plane/service.py` — 重构 `_execute_plan_and_run`
  - 移除 `_ensure_subsystems`、`_build_memory_manager`、`_init_learning_system`、`_init_impact_analyzer`
  - 新增 `_register_hooks()` — 依赖注入点
  - 元数据在 before_hooks 和 after_hooks 后分别持久化
- ✅ `orchestrator/plan_validator.py` — 新增 PlanValidator 模块（修复缺失模块导致的测试收集失败）
- ✅ `tests/test_hooks.py` — 19 个测试（注入、隔离、顺序、容错）

### 设计原则

```
1. 依赖注入 — hooks 通过构造函数接收外部依赖（repository, llm_config）
2. 顺序保证 — MemoryHook 先于 ImpactHook（确保 ctx.memory_manager 可用）
3. 容错 — 所有 hook 错误被捕获并记录，永不中断核心执行流
4. 可扩展 — 新增 hook 只需继承 ExecutionHook 并注册到 _register_hooks()
```

### 全量测试

- `python3 -m pytest -v` — 940 passed, 0 failed

---

## M6 — Brain/Hands Separation

**目标:** 将 Weave 从自建 Agent 循环迁移为纯编排层（meta-harness），执行委托给 ClaudeCodeBackend / CodexBackend，BuiltinBackend 保留为规划/评估后端。基于 [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) 架构理念，以及 [ADR-0017](docs/adrs/0017-brain-hands-separation.md) 决策。

### M6.1 — BackendContext 扩展 + 默认 backend 切换

- ✅ `core/backend_models.py` — BackendContext 扩展 `memory_prompt`、`project_context` 字段
- ✅ `core/node_executor.py` — 注入 memory/project context 到 BackendContext
- ✅ `agent/backends/claude_code.py` — 使用新字段构建 prompt
- ✅ 默认 backend 从 `builtin` 切换到 `claude_code`

### M6.2 — Node Guardrails + stderr tail + semantic timeout

- ✅ `core/node_executor.py` — pre_check/post_check 机制（从 tool-call 级提升到 node 级）
- ✅ `agent/backends/stderr_tail.py` — StderrTail: 尾随 stderr 提取进度事件
- ✅ `core/config.py` — NodeTimeoutConfig 增加动态复杂度缩放的 `stall_timeout`

### M6.3 — LightweightLLMCaller + BackendRegistry 重构

- ✅ `agent/backends/base.py` — AgentBackend 抽象接口（Protocol）
- ✅ `agent/backends/builtin.py` — BuiltinBackend: 包装 LightweightLLMCaller 或 AgentPool
- ✅ `agent/backends/registry.py` — BackendRegistry: 管理多 backend 实例 + fallback
- ✅ `agent/agent_pool.py` — 标记为 deprecated（M6.3），保留供 BuiltinBackend 使用

### M6.4 — 清理 + 文档更新

- ✅ 代码清理与文档同步
- ✅ `tools/registry.py` — 保留 write/edit/bash 供 BuiltinBackend 兼容

### M6.5 — Stream-JSON Event Parsing

- ✅ `agent/backends/stream_parser.py` — StreamParser: 解析 CLI backend 的流式 JSON 事件
- ✅ `core/activity_detector.py` — 检测 backend 输出中的有意义事件

### M6.6 — Node Timeout Semantic (Progress-Driven)

- ✅ 基于 stderr 进度事件的语义超时，替代固定时间超时
- ✅ `agent/backends/stderr_tail.py` — 集成到超时检测逻辑

### M6.7 — Session Resume + Bidirectional Comms

- ✅ `agent/backends/bidirectional.py` — 双向通信协议（支持会话恢复）
- ✅ `core/backend_models.py` — BackendResult 扩展会话状态字段
- ✅ 会话恢复机制

### M6.8 — MCP Config 传递到外部 Backend

- ✅ `mcp/config_export.py` — MCP 配置导出器，传递给外部 backend
- ✅ CLI backend 启动时注入 MCP 服务器配置

### M6.9 — OTEL Trace Propagation

- ✅ OpenTelemetry trace context 传播到 CLI 子进程
- ✅ 端到端可观测性：编排层 → CLI backend → 子进程

### 架构变化总览

```
迁移前:  ToolRegistry.execute(tool_name, args) → guardrail_check → execute
迁移后:  NodeExecutor.execute_node(node) → pre_check → backend.execute() → post_check

迁移前:  AgentPool → AgentWorker (自建 LLM 循环 + 工具管理)
迁移后:  BackendRegistry → ClaudeCodeBackend/CodexBackend (外部 Agent)
                            ↘ BuiltinBackend (轻量 LLM 调用, 无工具循环)
```

---

## 完整文件清单

```
weave/
├── core/                          # 核心模型与引擎
│   ├── models.py                  # 统一重导出
│   ├── dag_models.py              # DAG 模型
│   ├── event_models.py            # 事件模型
│   ├── guardrail_models.py        # Guardrail 模型
│   ├── memory_models.py           # M3.2: 记忆模型
│   ├── analysis_models.py         # M3.5: 影响分析模型
│   ├── eval_models.py             # 评估模型
│   ├── tool_models.py             # 工具模型
│   ├── mcp_models.py              # MCP 模型
│   ├── artifact_handoff.py        # 交接逻辑
│   ├── exceptions.py              # 自定义异常
│   ├── config.py                  # 配置管理
│   ├── agent_registry.py          # Agent 注册表
│   ├── llm_client.py              # LLM 客户端
│   ├── llm_router.py              # M3.1: 多模型路由
│   ├── dag_engine.py              # DAG 引擎
│   ├── node_executor.py           # 单节点执行
│   ├── evaluation_pipeline.py     # M6: 后执行评估管道
│   ├── quality_gate.py            # 质量检查
│   ├── retry_policy.py            # 重试策略
│   ├── progress.py                # 进度追踪 + 异常检测
│   ├── subprocess_runner.py       # 通用子进程执行
│   ├── backend_models.py          # M6.1: BackendContext/Result/Status
│   ├── activity_detector.py       # M6.5: 事件检测
│   ├── watchdog.py                # Watchdog
│   └── project_config.py          # 项目配置
├── cli/                           # CLI 命令（从 main.py 拆分）
│   ├── execution.py               # plan/execute/run/viz
│   ├── jobs.py                    # submit/status/list/cancel/worker/recover
│   ├── approval.py                # tickets/approve/reject
│   ├── memory.py                  # memory 命令
│   ├── learning.py                # learning 命令
│   ├── impact.py                  # impact 命令
│   ├── skills.py                  # skills/skill/templates
│   └── utils.py                   # 共享工具
├── control_plane/                 # 任务控制面
│   ├── models.py                  # Job/Run 模型
│   ├── repository.py              # 持久化存储
│   ├── service.py                 # RunService
│   ├── hooks.py                   # Execution Hooks
│   ├── execution_factory.py       # 工厂
│   ├── job_lifecycle.py           # Job 生命周期
│   ├── run_lifecycle.py           # Run 生命周期
│   │   # _write_job_result() lives in service.py (#572)
│   ├── backend_lifecycle.py       # Backend 集成
│   ├── worker.py                  # Worker 消费者
│   ├── worker_executor.py         # Job 执行
│   ├── worker_recovery.py         # 恢复
│   └── approval.py                # 审批票据
├── backend/                       # 执行后端
│   ├── base.py                    # 抽象接口
│   ├── local.py                   # 本地执行
│   ├── worktree.py                # Git worktree
│   ├── sandbox.py                 # SandboxProvider
│   ├── docker_stub.py             # Docker stub
│   └── lifecycle.py               # BackendManager
├── orchestrator/
│   ├── intelligent_orchestrator.py # 编排 Agent
│   ├── plan_validator.py          # DAG 验证
│   ├── llm_utils.py               # LLM 工具
│   └── prompts/                   # Prompt 模板
├── agent/
│   ├── worker.py                  # Agent Worker (deprecated M6.3, retained for BuiltinBackend)
│   ├── agent_pool.py              # Agent 池 (deprecated M6.3, retained for BuiltinBackend)
│   ├── prompts.py                 # System prompts (retained for BuiltinBackend compat)
│   └── backends/                  # M6: Agent Backend 抽象层
│       ├── base.py                # AgentBackend 抽象接口
│       ├── builtin.py             # BuiltinBackend (轻量 LLM 调用)
│       ├── claude_code.py         # ClaudeCodeBackend (Claude Code CLI)
│       ├── codex.py               # CodexBackend (Codex CLI)
│       ├── registry.py            # BackendRegistry (多 backend + fallback)
│       ├── stderr_tail.py         # StderrTail (进度事件提取)
│       ├── stream_parser.py       # StreamParser (流式 JSON 解析)
│       └── bidirectional.py       # 双向通信协议 (会话恢复)
├── evaluator/
│   ├── engine.py                  # 评估编排
│   ├── runner.py                  # 执行器
│   ├── models.py                  # 评估模型
│   ├── artifact.py                # Artifact 评估
│   ├── compat.py                  # 兼容层
│   ├── checkers/                  # 条件检查器
│   └── lint/                      # Lint 解析
├── tools/
│   ├── registry.py                # 工具注册表
│   └── command_runner.py          # 命令执行
├── guardrails/
│   └── policy.py                  # 安全策略
├── mcp/
│   ├── client.py                  # MCP 客户端
│   ├── server.py                  # MCP 服务器
│   ├── analysis_tools.py          # M4.3: 分析工具
│   ├── weave_tools_server.py      # M4.3: 独立 MCP 服务器
│   └── config_export.py           # M6.8: 配置导出器
├── skills/
│   └── registry.py                # SkillRegistry
├── session/
│   └── store.py                   # JSONL 存储
├── memory/                        # M3.2: Agent 记忆
│   ├── store.py
│   ├── manager.py
│   └── sharing.py
├── learning/                      # M3.3: 自学习
│   ├── analyzer.py
│   ├── optimizer.py
│   └── scheduler.py
├── templates/                     # M3.4: DAG 模板
│   ├── library.py
│   └── *.yaml                     # 7 个内置模板
├── analysis/                      # M3.5: 影响分析
│   ├── dependency_graph.py
│   ├── impact_predictor.py
│   └── change_verifier.py
├── monitoring/
│   ├── metrics.py
│   └── alerts.py
├── visualizer/                    # M2.3: Web 控制台
│   ├── server.py
│   ├── cli_renderer.py
│   ├── event_bridge.py
│   └── static/
├── reporter/
│   └── logger.py
├── projects/
│   └── example/agents.yaml
├── docs/
│   ├── roadmap.md                 # 本文档
│   ├── m1_personal_spec.md
│   ├── m4_directions.md           # M4 方向调研与决策
│   ├── research-project-understanding.md  # 项目理解技术方案
│   ├── research-strategy-direction.md     # 战略方向评估
│   ├── config_reference.md
│   ├── dev_guide.md
│   ├── evaluator_criterion_semantics.md
│   ├── architecture_improvements.md
│   ├── knowledge_index.py
│   ├── specs/                     # 模块规格
│   └── adrs/                      # 架构决策
├── tests/                         # 测试套件
├── main.py                        # CLI 入口
├── README.md
├── ARCHITECTURE.md
├── AGENTS.md
├── CONTRIBUTING.md
└── CLAUDE.md
```

---

## M4 — Project Understanding

**目标**: 让 Weave 在执行任务前理解已有项目，实现"像高级工程师一样修改代码"。

**设计文档**: `docs/plans/2026-05-18-m4-project-understanding-design.md`
**技术研究**: `docs/research-project-understanding.md`

### M4.0 — Project Onboarder (Weeks 1-3)

**目标**: 扫描已有项目，自动生成项目索引。首次 run/plan 时自动触发。

- `project/models.py` — TechStack, ModuleSummary, CodeConventions, ProjectIndex
- `project/stack_detector.py` — 技术栈自动检测（语言分布 + 框架 + 测试框架 + linter）
- `project/module_summarizer.py` — 模块摘要提取（AST 确定性 + 可选 LLM 语义）
- `project/convention_extractor.py` — 代码约定提取（缩进、命名、类型注解风格）
- `project/indexer.py` — 索引编排入口
- `cli/project.py` — project-analyze / project-index CLI 命令
- `.weave/index/` — 索引文件存储（graph.json, modules.json, tech_stack.json, conventions.json）

### M4.1 — Enhanced Dependency Graph (Weeks 4-7)

**目标**: Python-only → 多语言多边类型图引擎。

- `project/graph/engine.py` — ProjectGraph（build/query/subgraph/to_text）
- `project/graph/models.py` — EdgeType (IMPORTS/INHERITS/CALLS/IMPLEMENTS/DECORATES/TESTS/REFERENCES)
- `project/graph/base_parser.py` — 语言解析器基类
- `project/graph/python_parser.py` — Python AST + tree-sitter
- `project/graph/js_ts_parser.py` — JS/TS tree-sitter
- `project/graph/go_parser.py` — Go tree-sitter
- `project/graph/rust_parser.py` — Rust tree-sitter
- `analysis/dependency_graph.py` → 迁移为 project/graph/engine.py 的薄适配器

### M4.2 — Context Router (Weeks 8-10)

**目标**: 根据任务描述选择最相关的项目上下文，Token 预算内注入。

- `project/context/router.py` — BM25 + 图距离融合排序
- `project/context/selector.py` — FULL/SIGNATURES/COMPRESSED 输出模式
- 注入 orchestrator planning prompt (4000 tokens)
- 注入 agent system prompt (2000 tokens)
- 注入 evaluator (1000 tokens)

### M4.3 — Project-Aware Agents (Weeks 11-13)

**目标**: Viewer/Editor 代理分离，项目感知 Prompt。

- `project/agents/viewer.py` — 只读分析代理（定位 + 理解）
- `project/agents/prompts.py` — 项目感知 prompt 模板
- Viewer/Editor 代理注册到 AgentRegistry
- `orchestrator/prompts/planning.md` — 新增 Rule 20/21（项目感知规划 + 最小化差异原则）
- `templates/fix_bug_v2.yaml` — 利用 Viewer/Editor 的新模板

### 新增依赖

```
tree-sitter, tree-sitter-python, tree-sitter-javascript,
tree-sitter-typescript, tree-sitter-go, tree-sitter-rust
```

### 新增环境变量

```
WEAVE_PROJECT_INDEX_ENABLED, WEAVE_PROJECT_INDEX_PATH,
WEAVE_PROJECT_INDEX_AUTO, WEAVE_PROJECT_GRAPH_MAX_DEPTH,
WEAVE_CONTEXT_BUDGET
```

### 新增 CLI 命令

```bash
python main.py project-analyze --project <path>
python main.py project-index --project <path>
python main.py project-graph --project <path> [--query X]
python main.py project-context "task" --project <path>
```
