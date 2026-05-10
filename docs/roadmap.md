# Harness Milestone Roadmap

---

**最后更新:** 2026-05-10
**当前版本:** M3.5

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

---

## M1 — Personal Edition

**目标:** 单用户、CLI 驱动的多 Agent 软件开发 Harness，实现"扔任务进去，系统自动跑完"。

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
- ✅ 非交互模式 — `--non-interactive` / `HARNESS_NON_INTERACTIVE`
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
- ✅ 配置路由: HARNESS_DEFAULT_BACKEND=local|worktree

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

- `HARNESS_MEMORY_PATH` — 存储路径（默认 ./data/memory）
- `HARNESS_MEMORY_ENABLED` — 启用/禁用（默认 true）
- `HARNESS_MEMORY_MAX_ENTRIES` — 每 Agent 最大条目数（默认 500）
- `HARNESS_MEMORY_MAX_LENGTH` — 内容最大长度（默认 1000 字符）
- `HARNESS_MEMORY_TTL_DAYS` — 默认过期天数（默认 90）
- `HARNESS_MEMORY_RETRIEVAL_LIMIT` — 每次注入最大条目数（默认 10）
- `HARNESS_MEMORY_DECAY_DAYS` — 相关度衰减半衰期（默认 30 天）

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

- `HARNESS_LEARNING_PATH` — 学习数据路径（默认 ./data/learning）
- `HARNESS_LEARNING_ENABLED` — 启用/禁用（默认 true）

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

- `HARNESS_IMPACT_PATH` — 分析数据路径（默认 ./data/impact）
- `HARNESS_IMPACT_ENABLED` — 启用/禁用（默认 true）

---

## 完整文件清单

```
harness/
├── core/                          # 核心模型与引擎
│   ├── models.py                  # 所有数据模型（含 NodeHealth 心跳）
│   ├── config.py                  # 配置管理
│   ├── agent_registry.py          # Agent 能力注册表
│   ├── llm_client.py              # 统一 LLM 客户端
│   └── dag_engine.py              # DAG 引擎（含 Watchdog 协程）
├── control_plane/                 # M1: CLI 控制面
│   ├── models.py                  # Job/Run 数据模型
│   ├── repository.py              # 持久化存储
│   ├── service.py                 # 执行服务
│   ├── worker.py                  # Worker 队列消费者
│   └── approval.py                # M1.1: 审批票据系统
├── backend/                       # M2: 执行后端
│   ├── base.py                    # 抽象接口
│   ├── local.py                   # 本地执行
│   ├── worktree.py                # Git worktree 隔离
│   ├── docker_stub.py             # Docker stub（预留）
│   └── lifecycle.py               # 后端生命周期管理
├── monitoring/                    # M1: 监控
│   ├── metrics.py                 # 指标聚合
│   └── alerts.py                  # 告警系统（含健康告警）
├── visualizer/                    # M2.3: Web 控制台
│   ├── server.py                  # FastAPI 服务
│   ├── cli_renderer.py            # CLI DAG 渲染
│   ├── event_bridge.py            # WebSocket 事件桥
│   └── static/
│       ├── index.html             # 主仪表盘
│       └── console.html           # 管理控制台
├── orchestrator/
│   └── intelligent_orchestrator.py # 智能编排 Agent
├── agent/
│   ├── worker.py                  # Agent Worker
│   └── agent_pool.py              # Agent 实例池
├── session/
│   └── store.py                   # JSONL 事件存储
├── tools/
│   └── registry.py                # 工具注册表
├── guardrails/
│   └── policy.py                  # 安全策略
├── memory/                        # M3.2: Agent 记忆系统
│   ├── store.py                   # 持久化存储（原子写入）
│   ├── manager.py                 # 高层记忆操作接口
│   └── sharing.py                 # 跨 Agent 记忆共享
├── learning/                      # M3.3: 自学习系统
│   ├── analyzer.py                # 执行模式分析引擎
│   ├── optimizer.py               # 洞察 → 记忆转换
│   └── scheduler.py               # 定期分析调度
├── templates/                     # M3.4: DAG 模板系统
│   ├── library.py                 # TemplateRegistry 模板注册表
│   ├── build_api.yaml             # REST API 模板
│   ├── add_feature.yaml           # 新功能模板
│   ├── fix_bug.yaml               # Bug 修复模板
│   ├── refactor.yaml              # 重构模板
│   ├── add_tests.yaml             # 测试模板
│   ├── add_auth.yaml              # 认证模板
│   └── setup_project.yaml         # 项目脚手架模板
├── analysis/                      # M3.5: 影响分析系统
│   ├── dependency_graph.py        # 文件级依赖图
│   ├── impact_predictor.py        # 影响预测引擎
│   └── change_verifier.py         # 变更验证
├── evaluator/
│   └── engine.py                  # 评估引擎
├── reporter/
│   └── logger.py                  # 报告生成
├── projects/
│   └── example/agents.yaml        # 自定义 Agent 示例
├── docs/
│   ├── roadmap.md                 # 本文档
│   └── m1_personal_spec.md        # M1 工程规格
├── tests/                         # 测试套件
├── main.py                        # CLI 入口
├── requirements.txt
├── pyproject.toml
├── README.md                      # 面向用户的说明（中文）
├── ARCHITECTURE.md                # 架构设计（中文）
├── AGENTS.md                      # Agent 开发指南
└── CLAUDE.md                      # Claude Code 指引
```
