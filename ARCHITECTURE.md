# Harness 架构文档
## 智能多Agent编排系统

---

## 一、核心理念

### 三个关键设计决策

1. **编排 Agent 也是 Agent**：不是硬编码状态机，而是 LLM 驱动的规划器
2. **能力注册制**：编排 Agent 不预设 Worker 类型，通过注册表发现
3. **默认最小化**：只有 planner/generator/evaluator 三个基础角色，项目按需扩展

---

## 二、架构图

```
用户输入: "构建支持 OAuth2 的电商全栈应用"

              │
              ▼
    ┌─────────────────────┐
    │  CLI Control Plane  │  ← M1: submit/status/list/cancel
    │  (control_plane/)   │     M1.1: tickets/approve/reject
    │                     │     基于文件系统的任务队列 + Worker 消费者
    └──────────┬──────────┘
               │
               ▼
    ┌─────────────────────────────────────────────────────┐
    │  Intelligent Orchestrator                            │
    │  (orchestrator/)                                     │
    │                                                      │
    │  ┌──────────────┐  ┌──────────────┐                 │
    │  │ LLM Planning │  │ Template     │  ← M3.4: 跳过  │
    │  │ (默认路径)    │  │ Instantiation│     LLM 规划    │
    │  └──────┬───────┘  └──────┬───────┘                 │
    │         │  学习提示注入 ←──┘                         │
    │         │     ↑                                      │
    │         │     │ M3.3: Learning Optimizer              │
    │         │     │ get_planning_hints()                  │
    └─────────┼───────────────────────────────────────────┘
              │
              │ 查询
              ▼
    ┌─────────────────────┐
    │   Agent Registry    │  ← 能力注册表
    │                     │     默认: planner/generator/evaluator
    │   + 项目自定义       │     扩展: ui_designer/db_admin/...
    └──────────┬──────────┘
               │
               │ 返回 DAG
               ▼
    ┌─────────────────────┐
    │   DAG Engine        │  ← 拓扑排序 + 并行执行
    │                     │     Level 0: [planner]
    │  - Topological sort │     Level 1: [generator_ui, generator_api] 并行
    │  - Parallel exec    │     Level 2: [evaluator]
    │  - Failure handling │
    │  - Watchdog (M2)    │  ← 心跳监控，hang agent 自动处理
    └──────────┬──────────┘
               │
        ┌──────┼──────┐
        │      │      │
        ▼      ▼      ▼
    ┌──────┐┌──────┐┌──────┐
    │Worker││Worker││Worker│  ← 独立上下文、独立工具
    │Agent ││Agent ││Agent │     通过 HandoffArtifact 交接
    └──┬───┘└──┬───┘└──┬───┘
       │       │       │
       │ M3.2: 记忆注入/提取  │
       │←──────────────────┘
       ▼
    ┌─────────────────────┐
    │   Memory System     │  ← M3.2: Agent 记忆
    │  (memory/)          │     PRIVATE → SESSION → GLOBAL
    │                     │     记忆注入/提取/共享/维护
    └──────────┬──────────┘
               │
        ┌──────┼──────┐
        │      │      │
        ▼      ▼      ▼
    ┌──────┐┌──────┐┌──────┐
    │Local ││Work- ││Docker│  ← M2: 执行后端抽象
    │      ││tree  ││(stub)│     配置驱动后端选择
    └──────┘└──────┘└──────┘
               │
        ┌──────┼──────────────────┐
        │      │                  │
        ▼      ▼                  ▼
    ┌────────────────┐  ┌──────────────────────┐
    │  Learning      │  │  Impact Analysis     │
    │  (learning/)   │  │  (analysis/)         │
    │  M3.3: 自学习   │  │  M3.5: 影响分析      │
    │  分析→优化→记忆 │  │  预测→执行→验证      │
    └────────────────┘  └──────────────────────┘
               │
               ▼
    ┌─────────────────────┐
    │  Monitoring Layer   │  ← 指标聚合 + 告警 + 健康监控
    │  (monitoring/)      │     成功率、延迟、Token 用量、心跳告警
    └─────────────────────┘
               │
               ▼
    ┌─────────────────────┐
    │  Web Console        │  ← M2.3: 可视化控制台
    │  (visualizer/)      │     DAG 实时监控、审批管理、告警面板
    └─────────────────────┘
```

---

## 三、核心组件

### 1. Agent Registry (`core/agent_registry.py`)

**职责**：Worker Agent 的能力注册与发现

**默认注册**（出厂设置，不可删除）：
- `planner` — 需求分析、任务分解、架构设计
- `generator` — 代码实现、文件编辑、功能开发
- `evaluator` — 质量评估、测试验证、代码审查

**项目扩展**：通过 `.harness/agents.yaml` 注册自定义 Agent

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev, tailwind_css]
    constraints: [Only modifies frontend/src/]
```

**关键接口**：
```python
registry.register(capability)        # 注册
registry.list_agents()               # 列出所有
registry.to_prompt_description()     # 生成 prompt 用的描述
```

### 2. Intelligent Orchestrator (`orchestrator/intelligent_orchestrator.py`)

**职责**：LLM 驱动的规划与异常处理

**两个核心方法**：

| 方法 | 触发时机 | 功能 |
|------|---------|------|
| `plan()` | 用户提交需求时 | 分析需求 → 查询注册表 → 生成 DAG |
| `adapt_to_failure()` | 节点执行失败时 | 分析上下文 → 决定 retry/skip/abort/replan |

**动态 Prompt**：编排 Agent 的 system prompt 是运行时生成的，基于注册表中的实际 Agent 列表

```python
# 编排 Agent 看到的 prompt 示例
"""
Available Worker Agents:
- planner: Planner — 负责任务分解和架构设计
- generator: Generator — 负责代码实现
- evaluator: Evaluator — 负责质量评估
- ui_designer: UI Designer — 负责前端UI（项目注册）

请为以下需求生成执行计划DAG...
"""
```

### 3. DAG Engine (`core/dag_engine.py`)

**职责**：DAG 的拓扑调度与执行

**执行流程**：
1. 拓扑排序 → 分层
2. 同层并行执行（asyncio.gather）
3. Watchdog 后台协程监控节点心跳（M2）
4. 失败时回调编排 Agent 决策
5. 返回完整执行结果

**Watchdog 机制（M2）**：
- 后台协程定期检查运行节点的 `last_heartbeat_at`
- 阈值: `heartbeat_interval(5s) × miss_threshold(3) ≈ 15s`
- 心跳丢失 → 标记 UNHEALTHY → 触发失败处理器
- 超时 → 标记 DEAD → 节点终止

**并发控制**：`max_parallel` 参数限制同时执行的 Agent 数

### 4. Agent Pool (`agent/agent_pool.py`)

**职责**：管理多个独立的 Worker Agent 实例

**关键特性**：
- 每个 Agent 类型一个实例（延迟创建）
- 独立上下文（Context Isolation）
- Agent 类型特定的 system prompt
- Handoff Artifact 收集与传递

### 5. 数据模型 (`core/models.py`)

| 模型 | 说明 |
|------|------|
| `AgentCapability` | Agent 能力描述（注册用） |
| `DAGNode` | DAG 节点 = 一个 Agent 任务 |
| `DAGEdge` | DAG 边 = 依赖关系 |
| `DAG` | 完整执行计划 |
| `HandoffArtifact` | Agent 间结构化交接产物 |
| `FailureDecision` | 编排 Agent 的失败处理决策 |
| `NodeHealth` | M2: 节点健康状态枚举（HEALTHY/MISSED/UNHEALTHY/DEAD） |
| `MemoryEntry` | M3.2: 记忆条目（scope, type, content, keywords, relevance） |
| `MemoryScope` | M3.2: PRIVATE / SESSION / GLOBAL |
| `MemoryType` | M3.2: FACT / EXPERIENCE / PREFERENCE / CONTEXT |
| `LearningInsight` | M3.3: 学习洞察（category, insight_type, confidence, impact） |
| `DAGTemplate` | M3.4: DAG 模板（name, variables, nodes, edges） |
| `ImpactScope` | M3.5: 影响预测结果（predicted_files, risk_level, confidence） |
| `VerificationResult` | M3.5: 变更验证结果（coverage, accuracy, passes） |

### 6. Control Plane (`control_plane/`)

**职责**：CLI 控制面，任务生命周期管理

| 组件 | 说明 |
|------|------|
| `models.py` | Job/Run 数据模型、状态枚举 |
| `repository.py` | 持久化存储（原子写入） |
| `service.py` | 执行服务（submit/run/resume） |
| `worker.py` | Worker 队列消费者（Lease 机制） |
| `hooks.py` | Execution Hooks — 生命周期回调（MemoryHook, LearningHook, ImpactHook） |
| `approval.py` | M1.1: 审批票据系统（ApprovalTicket + ApprovalRepository） |

### 7. Execution Backend (`backend/`)

**职责**：M2 执行环境隔离抽象

| 组件 | 说明 |
|------|------|
| `base.py` | ExecutionBackend 抽象接口 |
| `local.py` | 本地直接执行 |
| `worktree.py` | Git worktree 隔离（每个 Job 独立分支） |
| `docker_stub.py` | Docker 后端 stub（M3） |
| `lifecycle.py` | BackendManager — 配置驱动选择、风险映射、自动降级 |

### 8. Monitoring (`monitoring/`)

**职责**：指标聚合与告警

| 组件 | 说明 |
|------|------|
| `metrics.py` | 任务成功率、延迟 P95、重试率、Token 用量、审批维度指标 |
| `alerts.py` | 连续失败、时长阈值、死信、心跳异常告警 |

### 9. Web Console (`visualizer/`)

**职责**：M2.3 可视化控制台

| 组件 | 说明 |
|------|------|
| `server.py` | FastAPI 服务 — WebSocket DAG 监控 + REST API |
| `cli_renderer.py` | CLI 终端 DAG 渲染 |
| `event_bridge.py` | 事件桥接（Session 事件 → WebSocket） |
| `static/index.html` | 主仪表盘 — DAG 可视化、实时事件流 |
| `static/console.html` | 管理控制台 — Jobs/Runs/Tickets/Alerts |

### 10. Agent Memory (`memory/`) — M3.2

**职责**：持久化跨任务、跨会话的 Agent 记忆

| 组件 | 说明 |
|------|------|
| `store.py` | 原子写入持久化存储（文件级隔离 + 内存索引） |
| `manager.py` | 高层 API：存储、检索、注入、自动提取 |
| `sharing.py` | 跨 Agent 记忆共享（scope 提升） |

记忆生命周期：
```
Agent 执行前 → get_context_for_agent() → 注入 system prompt
Agent 执行后 → extract_and_store() → 自动提取 fact/experience
DAG 节点间 → share_with_downstream() → 上游记忆共享给下游
定期维护 → cleanup_expired + enforce_limits + recompute_relevance
```

### 11. Self-Learning (`learning/`) — M3.3

**职责**：从执行历史中自动学习模式，优化编排策略

| 组件 | 说明 |
|------|------|
| `analyzer.py` | 执行模式分析（失败/成功/Agent性能/规划质量） |
| `optimizer.py` | 洞察 → 记忆转换 + 编排提示生成 |
| `scheduler.py` | 定期分析调度（间隔/最小样本数控制） |

数据流：
```
MetricsCollector + MemoryManager → Analyzer → Optimizer → MemoryManager
                                                        → Orchestrator (plan hints)
```

### 12. DAG Templates (`templates/`) — M3.4

**职责**：可复用 YAML 模板，跳过 LLM 规划

| 组件 | 说明 |
|------|------|
| `library.py` | TemplateRegistry — 发现、加载、实例化 |
| `*.yaml` | 7 个内置模板（build_api, fix_bug, add_feature 等） |

使用：
```bash
python main.py run "Build API" --template build_api --var feature=Todo --var language=Python
```

### 13. Impact Analysis (`analysis/`) — M3.5

**职责**：执行前预测影响范围，执行后验证变更匹配度

| 组件 | 说明 |
|------|------|
| `dependency_graph.py` | Python ast 解析 import，构建双向文件依赖图 |
| `impact_predictor.py` | 关键词匹配 + 依赖图扩展 + 历史记忆回查 |
| `change_verifier.py` | 前后快照比对，计算覆盖率/准确度 |

数据流：
```
Requirement → ImpactPredictor → ImpactScope → Execute DAG → ChangeVerifier → VerificationResult
    ↓ 存入 job.metadata                                                        ↓ 存入记忆
```

### 14. Execution Hooks (`control_plane/hooks.py`) — Refactoring

**职责**：将记忆、学习、影响分析从核心执行流程解耦为生命周期回调

| 组件 | 说明 |
|------|------|
| `ExecutionContext` | 可变上下文，在 hooks 间传递 per-job 状态 |
| `ExecutionHook` | 抽象基类，定义 `before_execution` / `after_execution` |
| `MemoryHook` | 创建 per-job MemoryManager，服务级维护仅运行一次 |
| `LearningHook` | 触发学习分析，暴露 `optimizer` 给 Orchestrator |
| `ImpactHook` | 执行前预测影响范围，执行后验证变更 |

设计原则：
```
1. 依赖注入 — hooks 通过构造函数接收 repository、llm_config
2. 顺序保证 — MemoryHook 先于 ImpactHook（确保 memory_manager 可用）
3. 容错 — 所有 hook 错误被捕获并记录，不中断执行
4. 元数据持久化 — before/after hooks 写入 ctx.metadata，合并到 job.metadata
```

执行流程（重构后）：
```
_execute_plan_and_run():
    1. before_hooks() → MemoryHook, LearningHook, ImpactHook
    2. persist metadata
    3. orchestrator.plan() → DAG
    4. engine.execute(dag) → result_dag
    5. after_hooks() → ImpactHook 验证
    6. persist metadata
```

---

## 四、使用方式

### 1. 生成执行计划（不执行）

```bash
python main.py plan "Build a REST API for user authentication"

# 输出:
# Plan saved: ./data/plans/plan_abc123.json
# Reasoning: This is a backend-focused task requiring...
# Execution levels:
#   Level 0: plan
#   Level 1: impl
#   Level 2: eval
```

### 2. 执行已保存的计划

```bash
python main.py execute ./data/plans/plan_abc123.json
```

### 3. 一键规划+执行

```bash
python main.py run "Add OAuth2 support" --project ./my-project
```

### 4. Worker 模式（无人值守）

```bash
# 启动 Worker
python main.py worker --concurrency 1

# 提交任务
python main.py submit "Build a REST API for user auth"

# 非交互模式
python main.py worker --non-interactive
```

### 5. 审批管理（M1.1）

```bash
# 查看待审批票据
python main.py tickets

# 批准
python main.py approve <ticket_id> --reason "已审查，安全"

# 拒绝
python main.py reject <ticket_id> --reason "风险过高"
```

### 6. Web 控制台（M2.3）

```bash
# 启动 Web 服务
python main.py viz

# 浏览器访问
# http://localhost:8765 — 主仪表盘
# http://localhost:8765/console.html — 管理控制台
```

### 7. DAG 模板快速路径（M3.4）

跳过 LLM 规划，直接从 YAML 模板实例化 DAG：

```bash
# 列出所有模板
python main.py templates

# 查看模板详情
python main.py templates --name build_api

# 使用模板执行
python main.py run "Build Todo API" --template build_api --var feature=Todo --var language=Python

# 使用模板规划
python main.py plan "Fix login bug" --template fix_bug --var bug="null pointer on empty email"
```

### 8. Agent 记忆管理（M3.2）

```bash
# 搜索记忆
python main.py memory-search "authentication flow"

# 添加项目约定（全局记忆）
python main.py memory-add "使用 pytest 异步测试模式" --type fact --scope global --keywords pytest async

# 查看记忆统计
python main.py memory-stats

# 手动维护
python main.py memory-cleanup
```

### 9. 学习系统（M3.3）

```bash
# 触发分析（自动在执行后按间隔触发）
python main.py learning-analyze

# 查看学习洞察
python main.py learning-insights

# 学习系统状态
python main.py learning-status
```

### 10. 影响分析（M3.5）

```bash
# 预测影响范围
python main.py impact-predict "重构 DAG 引擎" --project .

# 查看依赖图
python main.py impact-graph --project .

# 查看历史预测
python main.py impact-history
```

### 11. 使用项目自定义 Agent

在项目根目录创建 `.harness/agents.yaml`：

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev]
    constraints: [Only modifies frontend/src/]
```

编排 Agent 会自动发现这些 Agent 并在规划时使用：

```bash
python main.py run "设计登录页面" --project ./my-project
# 编排 Agent 会自动分配 ui_designer 参与执行
```

---

## 五、文件清单

```
harness/
├── core/                          # 核心模型与引擎
│   ├── models.py                  # 所有数据模型（含 M3 记忆/学习/模板/影响分析模型）
│   ├── config.py                  # 配置管理（含 M3 MemoryConfig/LearningConfig/ImpactConfig）
│   ├── agent_registry.py          # Agent 能力注册表
│   ├── llm_client.py              # 统一 LLM 客户端
│   └── dag_engine.py              # DAG 引擎（含 Watchdog + 记忆共享）
├── control_plane/                 # M1: CLI 控制面
│   ├── models.py                  # Job/Run 数据模型
│   ├── repository.py              # 持久化存储
│   ├── service.py                 # 执行服务（hooks 驱动的生命周期）
│   ├── hooks.py                   # Execution Hooks（Memory/Learning/Impact）
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
│   ├── server.py                  # FastAPI 服务（含 M3 REST API）
│   ├── cli_renderer.py            # CLI DAG 渲染
│   ├── event_bridge.py            # WebSocket 事件桥
│   └── static/
│       ├── index.html             # 主仪表盘
│       └── console.html           # 管理控制台
├── orchestrator/
│   ├── intelligent_orchestrator.py # 智能编排 Agent（含学习提示注入 + 模板规划）
│   └── plan_validator.py           # DAG 结构验证与自动修复
├── agent/
│   ├── worker.py                  # Agent Worker
│   └── agent_pool.py              # Agent 实例池（含记忆注入/提取）
├── session/
│   └── store.py                   # JSONL 事件存储
├── tools/
│   └── registry.py                # 工具注册表
├── guardrails/
│   └── policy.py                  # 安全策略
├── memory/                        # M3.2: Agent 记忆系统
│   ├── store.py                   # 持久化存储（原子写入 + 内存索引）
│   ├── manager.py                 # 高层记忆操作接口
│   └── sharing.py                 # 跨 Agent 记忆共享
├── learning/                      # M3.3: 自学习系统
│   ├── analyzer.py                # 执行模式分析引擎
│   ├── optimizer.py               # 洞察 → 记忆转换
│   └── scheduler.py               # 定期分析调度
├── templates/                     # M3.4: DAG 模板系统
│   ├── library.py                 # TemplateRegistry
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
│   ├── roadmap.md                 # 里程碑路线图
│   ├── m1_personal_spec.md        # M1 工程规格
│   ├── config_reference.md        # 配置参考（含 M3 配置）
│   ├── dev_guide.md               # 开发指南（含 M3 扩展指南）
│   ├── knowledge_index.py         # M3.0: 知识索引
│   ├── specs/                     # 模块规格文档
│   │   ├── memory.md              # M3.2: Agent 记忆规格
│   │   ├── learning.md            # M3.3: 自学习规格
│   │   ├── templates.md           # M3.4: DAG 模板规格
│   │   ├── impact_analysis.md     # M3.5: 影响分析规格
│   │   └── ...                    # M1/M2 模块规格
│   └── adrs/                      # 架构决策记录
│       ├── 0009-memory-scope-promotion.md  # M3.2: 三层 scope
│       ├── 0011-yaml-dag-templates.md      # M3.4: YAML 模板
│       ├── 0012-static-impact-analysis.md  # M3.5: 静态影响分析
│       └── ...                             # M1/M2 ADRs
├── tests/                         # 测试套件
├── main.py                        # CLI 入口
├── README.md                      # 面向用户的说明
├── ARCHITECTURE.md                # 本文档
├── AGENTS.md                      # Agent 开发指南
└── CLAUDE.md                      # Claude Code 指引
```

---

## 六、项目特化

通过 `.harness/agents.yaml` 注册项目自定义 Agent，编排 Agent 会自动发现并在规划时使用：

```bash
python main.py run "设计登录页面" --project ./my-project
```

---

---

## 七、下一步计划

详见 `docs/roadmap.md`。

---

*日期: 2026-05-11*
*状态: M3 已完成*
