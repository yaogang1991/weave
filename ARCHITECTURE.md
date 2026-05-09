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
               │
               ▼
    ┌─────────────────────┐
    │  Intelligent        │  ← LLM 驱动的编排 Agent
    │  Orchestrator       │     分析需求 → 分解任务 → 生成 DAG
    └──────────┬──────────┘
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
    └──────┘└──────┘└──────┘
               │
        ┌──────┼──────┐
        │      │      │
        ▼      ▼      ▼
    ┌──────┐┌──────┐┌──────┐
    │Local ││Work- ││Docker│  ← M2: 执行后端抽象
    │      ││tree  ││(stub)│     配置驱动后端选择
    └──────┘└──────┘└──────┘
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

### 6. Control Plane (`control_plane/`)

**职责**：CLI 控制面，任务生命周期管理

| 组件 | 说明 |
|------|------|
| `models.py` | Job/Run 数据模型、状态枚举 |
| `repository.py` | 持久化存储（原子写入） |
| `service.py` | 执行服务（submit/run/resume） |
| `worker.py` | Worker 队列消费者（Lease 机制） |
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

### 7. 使用项目自定义 Agent

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
├── evaluator/
│   └── engine.py                  # 评估引擎
├── reporter/
│   └── logger.py                  # 报告生成
├── projects/
│   └── example/agents.yaml        # 自定义 Agent 示例
├── docs/
│   ├── roadmap.md                 # 里程碑路线图
│   └── m1_personal_spec.md        # M1 工程规格
├── tests/                         # 测试套件
├── main.py                        # CLI 入口
├── README.md                      # 面向用户的说明（中文）
└── ARCHITECTURE.md                # 本文档
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

*日期: 2026-05-10*
*状态: M2 已完成*
