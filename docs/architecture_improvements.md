# 架构改进设计文档

## 概述

基于 OpenAI Symphony 和 Anthropic Managed Agents 的对比分析，对 harness 进行 5 项架构改进。

## 改动 1: Backend 架构拆分（正交维度）

### 问题
`BackendType` 把文件隔离（worktree）和执行环境（docker）放在一个 enum 互斥选择，但它们是正交维度。

### 方案
拆为两个独立枚举：
- `WorkspaceIsolation`: LOCAL（直接目录）| WORKTREE（Git Worktree）
- `ExecutionSandbox`: LOCAL（宿主进程）| DOCKER（容器）

`ExecutionBackend` 只负责 workspace 管理，新增 `SandboxProvider` 管理执行环境。`BackendManager` 组合两个维度。

### 文件变更
- `backend/base.py` — `BackendType` → `WorkspaceIsolation` + `ExecutionSandbox`
- `backend/sandbox.py` — 新文件，`SandboxProvider` / `LocalSandbox` / `DockerSandbox`
- `backend/lifecycle.py` — `BackendManager` 接受 workspace + sandbox 参数
- `backend/local.py`, `backend/worktree.py` — `backend_type` → `workspace_type`
- `backend/docker_stub.py` — 更新引用
- `backend/__init__.py` — 更新导出
- `core/config.py` — `default_backend` → `workspace_isolation` + `execution_sandbox`

## 改动 2: 项目级配置文件

### 问题
Prompt 和运维配置硬编码在 Python 代码中，不可按项目定制。

### 方案
新增 `.harness/config.yaml`，只放运维参数（不放 prompt，LLM 编排 Agent 自行规划）：

```yaml
runtime:
  max_parallel: 3
  max_turns: 50
  turn_timeout_sec: 600
  max_retries: 3
  base_backoff_sec: 1.0
  max_backoff_sec: 300
  backoff_multiplier: 2.0

hooks:
  after_create: "pip install -r requirements.txt"
  before_run: "pytest --co -q"
  after_run: "ruff check ."
  before_remove: "cp -r ./artifacts /tmp/backup"
  timeout_sec: 60

guardrails:
  denied_commands: ["rm -rf", "sudo"]
  approval_policy: "accept_edits"

project_context:
  language: python
  framework: fastapi
  test_runner: pytest
```

### 文件变更
- `core/project_config.py` — 新文件，Pydantic 模型 + YAML 加载
- `control_plane/service.py` — 加载项目配置，传递给 engine/orchestrator

## 改动 3: Workspace Hooks 生命周期

### 问题
无用户可扩展的生命周期 hook 机制。

### 方案
在 workspace 生命周期的 4 个节点执行用户配置的 shell 命令（subprocess 方式，与 Symphony 一致）。

失败语义：
- `after_create` / `before_run`: 致命（抛出 HookError）
- `after_run` / `before_remove`: 只记日志

### 文件变更
- `backend/lifecycle.py` — 新增 `execute_hook` 方法、`HookResult`、`HookError`
- `control_plane/service.py` — 新增 `_finalize_backend` 辅助方法，在 4 个生命周期节点调用 hooks

## 改动 4: 热重载配置

### 问题
修改配置需要重启 worker。

### 方案
在 `TaskWorker` 的 poll loop 中检查 `.harness/config.yaml` 的 mtime，变更时重新加载。只影响新 dispatch 的任务。

### 文件变更
- `control_plane/worker.py` — 新增 `_check_config_reload` 和 `register_project_path` 方法

## 改动 5: 精细化重试/退避

### 问题
退避参数硬编码，Job 级别固定 5s，无 attempt 上下文注入。

### 方案
- 退避参数从 RuntimeConfig 读取（base/multiplier/max 均可配）
- DAG 引擎 `_compute_backoff` 使用可配参数
- Job 执行时注入 `attempt` 和 `is_retry` 到编排 Agent 上下文

### 文件变更
- `core/dag_engine.py` — `__init__` 新增 backoff_base/max_backoff/backoff_multiplier 参数
- `control_plane/service.py` — `_create_execution_engine` 传入退避参数，`_execute_plan_and_run` 注入 attempt 上下文
