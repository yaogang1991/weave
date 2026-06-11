# M7.4 — 五核心 AgentSpec 统一模型设计

**日期:** 2026-06-08
**Issue:** #1109
**状态:** 设计完成，待实施

---

## 1. 问题陈述

Weave 的 Agent 抽象分散在 7+ 个配置映射中，全部通过 `agent_type` 字符串键（如 `"planner"`, `"generator"`, `"evaluator"`）隐式关联。注册一个自定义 Agent 类型需要修改 7+ 个不同位置才能获得完整行为。没有"一个对象描述一个 Agent"的统一模型。

**当前分布：**

| 位置 | 按 agent_type 键入的内容 |
|------|-------------------------|
| `AgentCapability` (dag_models.py) | 描述、skills、input/output_schema |
| `ModelRoutingConfig.routing` (config/llm.py) | 模型选择 |
| `NodeTimeoutConfig.overrides` (config/timeout.py) | 执行超时 |
| `NodeTimeoutConfig.stall_overrides` (config/timeout.py) | 停顿超时 |
| `WatchdogConfig.agent_overrides` (config/timeout.py) | 心跳参数 |
| `TokenEstimationConfig.overhead_margins` (config/domains.py) | Token 预算余量 |
| `RetryPolicyEngine` (retry_policy.py) | 重试行为（硬编码） |
| `EvaluationPipeline` (evaluation_pipeline.py) | 是否运行评估（硬编码） |

## 2. 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| AgentSpec 与 AgentCapability 的关系 | **替换**（非包装） | AgentCapability 的字段正是 5 核心模型涵盖的，包装只会增加认知负担 |
| 配置接入策略 | **AgentSpec 作为配置聚合根** | AgentSpec 是真相源，散布的 config maps 是派生物 |
| Harness 归属 | 编排层，不属于 AgentSpec | 同一 Agent 可在不同 Harness 上运行 |
| 信息隐藏层级 | Configuration（外部）vs Mechanism（内部） | 调用者不需要知道具体模型、工具实现、存储后端 |
| 调用者覆盖范围 | 只能收紧，不能放宽 | 安全优先：Agent 设计者的默认值是天花板 |
| 核心优先级 | Boundary > Contract > Lifecycle > Brain > Capability | 安全底线 > 质量标准 > 恢复策略 > 推理能力 > 工具可用 |

## 3. 数据模型

### 3.1 AgentSpec 统一模型

```python
class AgentSpec(BaseModel):
    """统一 Agent 描述模型 — 5 核心正交模型"""
    name: str                         # Agent 唯一标识（替代 AgentCapability.id）
    version: str = "1.0.0"            # 版本控制
    description: str = ""             # 人类可读描述

    contract: ContractSpec            # 进出什么、怎么算成功
    brain: BrainSpec                  # 用什么模型、什么配置
    capability: CapabilitySpec        # 有什么工具、需要谁帮忙
    boundary: BoundarySpec            # 花多少、跑多久、何时停
    lifecycle: LifecycleSpec          # 记住什么、失败了怎么办
```

### 3.2 ContractSpec

```python
class ContractSpec(BaseModel):
    """Agent 的输入输出契约和成功标准"""
    input_schema: dict[str, Any] = {}     # JSON Schema（结构化，替代 list[str]）
    output_schema: dict[str, Any] = {}    # JSON Schema（结构化，替代 list[str]）
    success_criteria: list[SuccessCriterion] = []
```

### 3.3 BrainSpec

```python
class QualityTier(str, Enum):
    FAST = "fast"           # haiku 级别 — 轻量快速
    BALANCED = "balanced"   # sonnet 级别 — 平衡性价比
    HIGH = "high"           # sonnet 级别 + 高参数 — 高质量
    PREMIUM = "premium"     # opus 级别 — 最高质量

class BrainSpec(BaseModel):
    """Agent 的推理配置"""
    quality_tier: QualityTier = QualityTier.BALANCED
    model_id: str | None = None       # 覆盖（None = 按 quality_tier 路由）
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: str = ""
```

### 3.4 CapabilitySpec

```python
class AgentDependency(BaseModel):
    """Agent 间依赖声明"""
    agent_name: str                    # 依赖的 Agent 名称
    purpose: str                       # 为什么需要
    fallback: str | None = None        # 不可用时的备选

class CapabilitySpec(BaseModel):
    """Agent 的工具和依赖"""
    skills: list[str] = []             # 能力标签
    tools: list[str] = []              # 工具列表（显式声明）
    dependencies: list[AgentDependency] = []
    constraints: list[str] = []
```

### 3.5 BoundarySpec

```python
class ResourceBudget(BaseModel):
    """Per-agent 资源预算"""
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_iterations: int | None = None

class TerminationConditions(BaseModel):
    """终止条件"""
    convergence_threshold: float | None = None
    convergence_rounds: int | None = None

class BoundarySpec(BaseModel):
    """Agent 的资源边界和时间约束"""
    timeout: int | None = None                # 执行超时（秒）
    stall_timeout: int | None = None          # 停顿超时（秒）
    resource_budget: ResourceBudget | None = None
    termination: TerminationConditions | None = None
    max_retries: int = 3
```

### 3.6 LifecycleSpec

```python
class ErrorStrategy(str, Enum):
    FAIL_FAST = "fail_fast"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    FALLBACK_AGENT = "fallback_agent"
    CIRCUIT_BREAKER = "circuit_breaker"
    REPLAN = "replan"

class ErrorPolicy(BaseModel):
    """错误恢复策略链"""
    strategy: ErrorStrategy = ErrorStrategy.RETRY_WITH_BACKOFF
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_cap: float = 60.0
    fallback_agent: str | None = None

class LifecycleSpec(BaseModel):
    """Agent 的记忆和错误恢复配置"""
    memory_enabled: bool = True
    memory_scope: str | None = None       # "session" | "global"
    error_policy: ErrorPolicy | None = None
```

### 3.7 InvocationOverrides

```python
class InvocationOverrides(BaseModel):
    """调用者覆盖接口 — 只能收紧，不能放宽"""
    success_criteria: list[SuccessCriterion] | None = None
    budget: ResourceBudget | None = None
    timeout: int | None = None
```

## 4. 替换策略

### 4.1 模型替换映射

| 现有模型/概念 | 新归宿 | 变更类型 |
|---|---|---|
| `AgentCapability` | → `AgentSpec` + `CapabilitySpec` | 替换 |
| `AgentCapability.id` | → `AgentSpec.name` | 重命名 |
| `AgentCapability.name` | → `AgentSpec.description`（短描述） | 迁移 |
| `AgentCapability.skills` | → `CapabilitySpec.skills` | 迁移 |
| `AgentCapability.input_schema` (list[str]) | → `ContractSpec.input_schema` (dict) | 类型升级 |
| `AgentCapability.output_schema` (list[str]) | → `ContractSpec.output_schema` (dict) | 类型升级 |
| `AgentCapability.system_prompt` | → `BrainSpec.system_prompt` | 迁移 |
| `AgentCapability.constraints` | → `CapabilitySpec.constraints` | 迁移 |
| `NodeTimeoutConfig.overrides[agent_type]` | → `BoundarySpec.timeout` | 迁移 |
| `NodeTimeoutConfig.stall_overrides[agent_type]` | → `BoundarySpec.stall_timeout` | 迁移 |
| `ModelRoutingConfig.routing[agent_type]` | → `BrainSpec.quality_tier` + `model_id` | 迁移 |
| `DAGNode.max_retries` 默认值(3) | → `BoundarySpec.max_retries` | 迁移 |
| retry_policy backoff 参数 | → `ErrorPolicy.backoff_base/cap` | 迁移 |

### 4.2 向后兼容

- `AgentCapability` 保留为兼容类，提供 `from_spec()` 和 `to_spec()` 方法
- `DAGNode.agent_type` 保持为字符串，引用 `AgentSpec.name`
- 现有 Config 类保留，从 `AgentSpec` 实例构建（新增 `from_specs()` 类方法）
- `core/models.py` re-export 保持不变，新增 `AgentSpec` 及子模型

### 4.3 默认 AgentSpec 定义

```python
# planner
AgentSpec(
    name="planner",
    description="Plans tasks by decomposing requirements into DAG nodes",
    contract=ContractSpec(
        input_schema={"type": "string", "description": "User requirement"},
        output_schema={"type": "object", "description": "DAG plan with nodes and edges"},
    ),
    brain=BrainSpec(
        quality_tier=QualityTier.HIGH,
        system_prompt="You are a planning agent...",
    ),
    capability=CapabilitySpec(
        skills=["task_decomposition", "dependency_analysis", "dag_generation"],
        dependencies=[],
    ),
    boundary=BoundarySpec(timeout=300, max_retries=2),
    lifecycle=LifecycleSpec(memory_enabled=True, error_policy=ErrorPolicy(strategy=ErrorStrategy.REPLAN)),
)
```

## 5. 执行层接入

### 5.1 AgentRegistry

```python
class AgentRegistry:
    _specs: dict[str, AgentSpec]

    def register_spec(self, spec: AgentSpec): ...
    def get_spec(self, name: str) -> AgentSpec | None: ...
    def get(self, name: str) -> AgentCapability | None: ...  # 向后兼容
    def to_prompt_description(self) -> str: ...
    def load_from_yaml(self, path): ...  # 支持 5 核心格式
```

### 5.2 NodeExecutor

- 从 `AgentSpec.boundary.timeout` 读取超时
- 从 `AgentSpec.boundary.resource_budget` 追踪预算
- 从 `AgentSpec.lifecycle.error_policy` 读取重试策略

### 5.3 LLMRouter

- 新增 `get_client_for_spec(spec: AgentSpec)` 方法
- `BrainSpec.quality_tier` 映射到具体 model_id
- `BrainSpec.model_id` 可直接覆盖

### 5.4 EvaluationPipeline

- 从 `AgentSpec.contract.success_criteria` 读取评估标准
- 统一当前分散的 success_criteria 处理逻辑

### 5.5 plan_validator

- 检查 `CapabilitySpec.dependencies` 中声明的依赖是否已注册
- 不可用时检查 `fallback` 策略

## 6. YAML 配置格式

```yaml
agents:
  - name: generator
    version: "1.0.0"
    description: "Generates code from specifications"
    contract:
      input_schema:
        type: object
        description: "Task specification with requirements"
      output_schema:
        type: object
        description: "Generated code artifacts"
      success_criteria:
        - type: FILE_EXISTS
          path: "src/**/*.py"
    brain:
      quality_tier: balanced
      temperature: 0.7
      system_prompt: "You are a code generation agent..."
    capability:
      skills: ["code_writing", "test_execution"]
      tools: ["read", "write", "edit", "bash", "glob", "grep"]
      dependencies:
        - agent_name: planner
          purpose: "Receives task decomposition"
        - agent_name: evaluator
          purpose: "Provides quality feedback"
          fallback: self_eval
    boundary:
      timeout: 600
      stall_timeout: 120
      max_retries: 3
      resource_budget:
        max_tokens: 100000
        max_cost_usd: 1.0
    lifecycle:
      memory_enabled: true
      memory_scope: session
      error_policy:
        strategy: retry_with_backoff
        max_retries: 3
        backoff_base: 2.0
        backoff_cap: 60.0
```

## 7. 实施阶段

### Phase 1: 新建模型（~3-4h）
- T7.4.1: 创建 `core/agent_spec.py`
- T7.4.2: 补齐缺失模型

### Phase 2: 接入现有代码（~4-5h）
- T7.4.3: AgentRegistry 支持 AgentSpec
- T7.4.4: NodeExecutor 从 AgentSpec 读取配置
- T7.4.5: LLMRouter 从 BrainSpec 路由
- T7.4.6: 评估管道从 ContractSpec 读取

### Phase 3: 编排集成（~3-4h）
- T7.4.7: plan_validator 检查 DependencySpec
- T7.4.8: 编排器校验 Capability 与 Contract 一致性
- T7.4.9: 暴露调用者接口

### Phase 4: YAML 配置与测试（~2-3h）
- T7.4.10: 编写 agents.yaml 完整示例
- T7.4.11: 单元测试
- T7.4.12: 集成测试

## 8. 验收标准

- [ ] `core/agent_spec.py` 包含 5 核心子模型 + AgentSpec
- [ ] AgentRegistry 支持从 YAML 加载完整 AgentSpec
- [ ] 调用者接口：`agent.invoke(task, overrides)`
- [ ] BrainSpec.quality_tier 替代直接指定 model_id
- [ ] DependencySpec 在 plan_validator 中被检查
- [ ] ResourceBudget 在 NodeExecutor 中被追踪
- [ ] ErrorPolicy 替代分散的错误处理
- [ ] 所有现有测试通过 + 新模型 80%+ 覆盖率
- [ ] 无 Any 类型在 AgentSpec 相关构造函数中
