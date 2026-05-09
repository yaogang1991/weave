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
    │  CLI Control Plane  │  ← M1 新增：submit / status / list / cancel / recover
    │  (control_plane/)   │     基于文件系统的任务队列 + Worker 消费者
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
               ▼
    ┌─────────────────────┐
    │  Monitoring Layer   │  ← M1 新增：指标聚合 + 告警
    │  (monitoring/)      │     成功率、延迟、Token 用量、失败率告警
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
3. 失败时回调编排 Agent 决策
4. 返回完整执行结果

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

### 4. 使用项目自定义 Agent

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
├── control_plane/          <- New: M1 Control Plane
│   ├── models.py           # Job/Run data models
│   ├── repository.py       # Persistence storage
│   ├── service.py          # Execution service
│   └── worker.py           # Worker queue consumer
├── monitoring/             <- New: M1 Monitoring
│   ├── metrics.py          # Metrics aggregation
│   └── alerts.py           # Alerting system
├── core/
│   ├── models.py                    # All data models (DAG/AgentCapability/Event/Session/Guardrail...)
│   ├── config.py                    # Configuration management
│   ├── llm_client.py                # Unified LLM client
│   ├── agent_registry.py            # Agent capability registry
│   └── dag_engine.py               # DAG execution engine
├── orchestrator/
│   └── intelligent_orchestrator.py  # Intelligent orchestration Agent
├── agent/
│   ├── worker.py                    # Agent Worker (LLM call loop)
│   └── agent_pool.py               # Agent instance pool
├── session/
│   └── store.py                     # Event storage
├── tools/
│   └── registry.py                  # Tool registry
├── projects/
│   └── example/
│       └── agents.yaml              # Example: project custom Agent
├── docs/
│   └── m1_personal_spec.md          # M1 specification document
├── main.py                          # CLI entry point
├── README.md
└── ARCHITECTURE.md                  # This document
```

---

## 六、项目特化

通过 `.harness/agents.yaml` 注册项目自定义 Agent，编排 Agent 会自动发现并在规划时使用：

```bash
python main.py run "设计登录页面" --project ./my-project
```

---

## 七、下一步计划

| 特性 | 说明 |
|------|------|
| **MCP 集成** | Agent 通过 MCP 调用外部工具（GitHub、数据库、浏览器） |
| **多模型路由** | planner 用轻量模型，generator 用强模型，evaluator 用中等模型 |
| **图编排** | 非线性 DAG（条件分支、循环、动态添加节点） |
| **执行监控 UI** | Web 界面实时显示 DAG 执行状态 |
| **Agent 记忆共享** | 跨项目的 Agent 经验积累 |

---

*日期: 2026-05-08*
*状态: 核心架构完成*
