# M5: Production Orchestration — Dogfooding Weave

## Problem Statement

Weave M4 完成了 Agent-as-Worker 架构（ClaudeCodeBackend + CodexBackend + Budget Manager），M5.0 稳定性基础已修复。但系统从未用真实任务验证过。solo developer 无法通过 Weave 自动完成日常的 bug 修复和小功能开发——每次都需要手动运行 CLI、手动检查结果、手动提交 PR。缺乏可观测性使得问题诊断困难，缺乏 GitHub 集成使得流程无法闭环。

## Evidence

- `monitoring/otel.py` 仅 116 行，无 per-node span 树，无法 trace 完整执行过程
- GitHub 集成完全缺失——无 webhook、无 PR 创建、无 Issue 解析
- WorkOS Horizon dogfooding 报告：agent 直接暴露平台薄弱点，形成 compounding improvement loop
- Miniforge：530+ PRs 用自己合并自己，证明 DAG executor + policy gates 的 dogfooding 模式可行

## Proposed Solution

将 M5 定位为 **dogfooding milestone**：让 Weave 用 Weave 开发 Weave。分 5 个子里程碑递进构建能力（M5.0 已完成），每个阶段都能用 Weave 跑真实任务验证。优先构建可观测性（M5.1），再构建 Issue Source/Change Sink 插件接口 + GitHub 插件实现（M5.2-M5.3），然后提升编排智能（M5.4-M5.5）。

## Key Hypothesis

We believe DAG orchestration + Claude Code Worker 能让 solo developer 通过提交 GitHub Issue 完成日常 bug 修复和小功能开发。
We'll know we're right when Weave 自己的 bug fix PR 中有 50%+ 由 Weave 自动生成并合入。

## What We're NOT Building

- 多用户/团队协作 — M5 聚焦 solo developer dogfooding
- Web UI 大改版 — 仅在现有 Visualizer 上增加 Token 面板
- 新语言/框架支持 — 仅 Python 项目（Weave 自身）
- 自定义 Agent Teams — 仅使用 ClaudeCodeBackend，CodexBackend 作为备选
- USD 成本换算 — 仅跟踪 token 计数

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|--------------|
| Weave auto-generated PRs | 50%+ of bug-fix PRs | GitHub PR author stats |
| Issue → PR end-to-end time | < 30 min for simple bugs | GitHub Issue timeline |
| DAG completion rate | > 80% without manual intervention | Run status in control_plane |

---

## Users & Context

**Primary User**
- **Who**: Weave 项目 solo maintainer
- **Trigger**: GitHub Issue 添加 `weave` 标签
- **Success state**: Issue 创建后自动收到 PR，review 后合入

**Job to Be Done**

When I submit a GitHub Issue for a bug fix or small feature, I want Weave to automatically decompose, implement, and submit a PR, so I can focus on architecture decisions and complex problems instead of repetitive coding tasks.

**Non-Users**
- 团队协作场景下的多个 developer（M5 不覆盖）
- 非 Python 项目的用户（M5 仅验证 Weave 自身）

---

## Solution Detail

### Core Capabilities (MoSCoW)

| Priority | Capability | Rationale |
|----------|------------|-----------|
| Must | 4 层 Trace（Run → Node → LLM Turn → Tool Call） | 无 trace 则无法诊断失败 |
| Must | Per-Run token 报告 | token 用量可见 |
| Must | Issue Source / Change Sink 插件接口 + GitHub 插件 | 核心价值流 |
| Must | PR 创建 + code review 评论 | 核心价值流终点 |
| Should | Visualizer Token 面板 | 可视化 token 趋势 |
| Should | 预算感知规划 | 智能分配 token 预算 |
| Should | 依赖感知分解 | 多文件修改更可靠 |
| Could | CI status check 监控 | auto-merge 的前提 |
| Could | PR auto-merge | 全自动闭环 |
| Won't | 多用户权限 | M5 不做 |
| Won't | 新 UI 页面 | M5 不做 |
| Won't | USD 成本换算 | 只跟踪 token |

### MVP Scope

M5.1-M5.3：可观测性基础 + 插件接口 + GitHub Issue → PR 完整闭环。

### User Flow

```
1. Developer 在 GitHub 创建 Issue，添加 "weave" 标签
2. CLI/Webhook 触发 → LLM 从多个 weave 标签 Issue 中选优先级最高的
3. Orchestrator (Opus) 分解为 DAG → AgentBackend (Sonnet) 执行各节点
4. ClaudeCodeBackend 在独立 worktree 中执行，SDK streaming 记录 4 层 trace
5. 执行完成后：git push → create PR → LLM 生成 code review 评论
6. 成功：正常 PR；部分失败：Draft PR + Issue 评论
7. Developer 在 GitHub review PR → approve → merge
```

---

## Technical Approach

**Feasibility**: MEDIUM-HIGH

**Architecture Notes**

- 新增 `plugins/` 模块，定义 `IssueSource`（拉取+解析+优先级排序）和 `ChangeSink`（PR+评论+标签流转）两个 ABC
- GitHub 作为 `plugins/github/` 内置插件实现，含独立的 webhook API server
- OTel 集成扩展现有 `monitoring/otel.py`，添加 4 层 span 树
- Trace 事件融入现有 EventType 枚举（新增 6 个值），复用 JSONL event log
- ClaudeCodeBackend 改造：从 `run()` 一次性结果 → `query()` 流式消费
- 每个 Issue 执行在独立 worktree + 独立分支（`fix/{issue-number}-{slug}`）
- GitHub API 使用 `gh` CLI（零依赖）
- Issue 优先级由 LLM 自动排序，不需要人工介入
- Plan 阶段用 Opus，其余用 Sonnet

**Technical Risks**

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Claude Code SDK API 不稳定 | M | CLI fallback 已实现，加 retry |
| 长任务可靠性衰减 | M | 限制单 DAG 节点数，失败自动降级 |

---

## Implementation Phases

| # | Phase | Description | Status | Parallel | Depends | PRP Plan |
|---|-------|-------------|--------|----------|---------|----------|
| 1 | ~~M5.0 Stability Foundation~~ | ~~修 replan loop + follow-up fixes~~ | done | - | - | - |
| 2 | M5.1 Observability | 4 层 Trace + token 报告 + Visualizer 增强 | in-progress | - | - | `.claude/PRPs/plans/m5-1-observability-trace-token.plan.md` |
| 3 | M5.2 Plugin Interface + GitHub Issue → Execute | 插件 ABC + GitHub 插件 + Issue 自动执行 | pending | - | 2 | - |
| 4 | M5.3 Execute → PR | ChangeSink + PR 创建 + code review + 失败处理 | pending | - | 3 | - |
| 5 | M5.4 Orchestration Intelligence | 预算感知 + 依赖感知 + 重规划 v2 | pending | - | 4 | - |
| 6 | M5.5 Full Loop | PR review 自动化 + CI + auto-merge | pending | - | 5 | - |

### Phase Details

**Phase 1: M5.0 Stability Foundation** — DONE

**Phase 2: M5.1 Observability (Week 1-2)**
- **Goal**: 4 层 trace 可见（Run → Node → LLM Turn → Tool Call）
- **Scope**:
  - 扩展 `monitoring/otel.py`：4 层 span 树
    - Run span: 总耗时、总 token、最终状态
    - Node span: 每个节点的耗时、token、成功/失败
    - LLM Turn span: 每次模型调用的 input/output tokens、耗时、cache 命中
    - Tool Call span: 每次工具调用的名称、参数摘要、结果、耗时
  - EventType 扩展：新增 `TRACE_RUN_START`、`TRACE_RUN_END`、`TRACE_NODE_START`、`TRACE_NODE_END`、`TRACE_LLM_TURN`、`TRACE_TOOL_CALL`
  - Per-Run token 报告（写入 JSONL event，Job 级别汇总所有 Run）
  - Visualizer Token 面板（/api/runs/{id}/tokens，token 分布柱状图，最近 20 Run 趋势）
  - ClaudeCodeBackend 改造：`run()` → `query()` streaming
- **New/modified files**:
  - `monitoring/otel.py` — 扩展为 4 层 span 树 + GenAI conventions
  - `monitoring/token_reporter.py` — 新增，per-Run token 报备
  - `core/event_models.py` — 新增 6 个 TRACE EventType
  - `agent/backends/claude_code.py` — 改造为流式消费
  - `core/dag_engine.py` — 节点执行前后创建 Run/Node span
  - `core/node_executor.py` — LLM Turn span
  - `visualizer/server.py` — 新增 token API
  - `visualizer/static/index.html` — Token 面板
- **Trace 存储**: JSONL event log（复用 session/store），可选 OTLP gRPC 导出到 Jaeger/Tempo
- **Success signal**: 跑一次 `weave run`，在 trace 中看到完整 4 层调用链 + token 计数
- **Dogfood**: 用 Weave 实现自己的 OTel 集成代码

**Phase 3: M5.2 Plugin Interface + GitHub Issue → Execute (Week 3-4)**
- **Goal**: 定义插件接口 + GitHub Issue 拉取 + LLM 自动选优先级 + 自动执行
- **Scope**:
  - 插件接口（`plugins/base.py`）：
    - `IssueSource` ABC：`fetch_issues()`、`parse_issue()`、`rank_issues()`、`update_label()`
    - `ChangeSink` ABC：`create_branch()`、`push_changes()`、`create_pr()`、`comment_on_issue()`、`create_review_comment()`
  - GitHub 插件（`plugins/github/`）：
    - `github_source.py` — IssueSource 实现（`gh` CLI + LLM 排序）
    - `github_sink.py` — ChangeSink 实现（`gh` CLI）
    - `webhook.py` — 独立 webhook API server（FastAPI app，自行启动 uvicorn）
    - `branch_manager.py` — branch 创建（`fix/{issue-number}-{slug}`）+ worktree 管理
  - CLI 命令：
    - `weave issue-poll --repo owner/repo` — 拉取 → LLM 排序 → 自动执行最高的
    - `weave issue-run <number>` — 指定 Issue 执行
    - `weave issue-status` — 查看状态
  - Issue context：仅注入 `.weave/config.yaml`，具体探索交给 Worker
  - Issue 标签流转：`weave` → `weave-running`
  - 并发度：1（串行）
- **New/modified files**:
  - `plugins/__init__.py` — 新增
  - `plugins/base.py` — 新增，IssueSource + ChangeSink ABC
  - `plugins/github/__init__.py` — 新增
  - `plugins/github/github_source.py` — 新增
  - `plugins/github/github_sink.py` — 新增
  - `plugins/github/webhook.py` — 新增，独立 FastAPI + uvicorn
  - `plugins/github/branch_manager.py` — 新增
  - `cli/github.py` — 新增，issue-poll/issue-run/issue-status
  - `main.py` — 注册新 CLI 命令
  - `control_plane/service.py` — submit_job 支持 plugin source
- **Success signal**: 创建 test Issue + `weave` 标签 → `weave issue-poll` → 自动执行 → 本地验证通过
- **Dogfood**: 用 Weave 修复 Weave 的 GitHub issues

**Phase 4: M5.3 Execute → PR (Week 5-6)**
- **Goal**: 执行完成后通过 ChangeSink 自动创建 PR + code review + 失败处理
- **Scope**:
  - PR 创建（`gh pr create`，含 Draft 模式）
  - PR body 模板：
    ```markdown
    ## Summary
    {LLM 生成的 2-3 句变更摘要}

    ## Changes
    {git diff --stat}

    ## Test plan
    - [ ] {pytest 命令}

    Fixes #{issue_number}

    Generated by Weave
    ```
  - Automated code review（LLM 读 git diff → 变更摘要 + 风险点 + tests/lint 状态）
  - 失败处理：
    - 规划失败（零产出）→ Issue 评论说明
    - 部分成功（有代码但测试挂）→ Draft PR + Issue 评论
    - 全部成功 → 正常 PR
  - 重试时 force push 覆盖同分支，更新已有 PR
  - Issue 标签流转：`weave-running` → `weave-pr`（成功）/ `weave-failed`（失败）
- **New/modified files**:
  - `plugins/github/github_sink.py` — 实现 push + PR + Draft PR + Issue 评论
  - `evaluator/pr_review_engine.py` — 新增，diff 分析 + review 评论
  - `control_plane/service.py` — post-execution hook: push + PR + review
- **Success signal**: Issue → Weave 修复 → PR 出现在 GitHub → 包含 review 评论
- **Dogfood**: Weave 修自己的 bug → 自动提 PR → 你 review 后 merge

**Phase 5: M5.4 Orchestration Intelligence (Week 7-8)**
- **Goal**: 编排器能处理更复杂的多文件任务
- **Scope**:
  - 预算感知规划（根据 Issue 复杂度分配 per-node token 预算）
  - 依赖感知分解（利用 `analysis/dependency_graph.py`）
  - 自适应重规划 v2（分析失败原因 → 局部新 DAG）
  - 项目上下文注入（`.weave/config.yaml` + 依赖图 → Worker prompt）
  - Hierarchical Task Execution 基础（超大任务拆为子任务序列）
- **New/modified files**:
  - `orchestrator/intelligent_orchestrator.py` — 增强 plan()
  - `orchestrator/budget_planner.py` — 新增
  - `orchestrator/hierarchical.py` — 新增，两层编排
  - `core/dag_engine.py` — 支持 sub-DAG 嵌套
- **Success signal**: 提交涉及 3+ 文件的功能 request → Weave 正确分解依赖并逐步执行
- **Dogfood**: 用 Weave 实现自己的编排增强功能

**Phase 6: M5.5 Full Loop (Week 9-10)**
- **Goal**: PR review → CI check → merge 全自动
- **Scope**:
  - PR review 自动化（检测 reviewer 评论 → 自动修复 → push fix）
  - CI status check 监控（等待 CI green）
  - Auto-merge（CI green + review approved → merge）
  - Issue 自动关闭（PR merged → close Issue）
  - 失败回退（CI red → 自动 comment 说明）
- **New/modified files**:
  - `plugins/github/github_sink.py` — 增加 merge + close 功能
  - `plugins/github/webhook.py` — 增加 PR review/CI webhook 处理
- **Success signal**: Issue → DAG → PR → CI green → auto-merge → Issue closed，全程无人工干预
- **Dogfood**: Weave 自己的完整开发循环

---

## Decisions Log

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | 触发方式 | CLI `issue-poll` 先行，Webhook 后加 | 先保持控制，验证后再自动化 |
| 2 | Trace 粒度 | 4 层（Run → Node → LLM Turn → Tool Call） | 精确诊断需要最细粒度 |
| 3 | Trace 存储 | JSONL event log + 可选 OTLP 导出 | 复用现有事件体系，零外部依赖 |
| 4 | Issue context | Worker 自行探索，仅注入 .weave/config.yaml | Worker 有全套工具，依赖图留给 M5.4 |
| 5 | 分支策略 | `fix/{issue-number}-{slug}`，重试 force push | 人类可读，不产生废弃分支 |
| 6 | Code review | LLM 读 diff → 变更摘要 + 风险点 + tests/lint | MVP 够用 |
| 7 | 失败处理 | Issue 评论 + Draft PR（部分产出时） | 不丢信息 |
| 8 | Issue 过滤 | Label-based（`weave` 标签，硬编码） | 最简单 |
| 9 | 并发 | 1（默认串行） | solo dev 避免文件冲突 |
| 10 | Issue 优先级 | LLM 自动排序（紧急程度 + 可独立解决性） | 不需要人介入 |
| 11 | 模型选择 | plan=Opus，其余=Sonnet | plan 最关键 |
| 12 | PR body | Summary + Changes + Test plan + Fixes #N | 简洁、关联 Issue |
| 13 | Webhook 安全 | HMAC-SHA256 secret token | 最标准最简单 |
| 14 | PR merge | 人工 review 后 auto-merge | 安全优先 |
| 15 | Visualizer | Token 分布柱状图 + 历史 20 Run 趋势 | 足够诊断 |
| 16 | OTel 导出 | JSONL 默认，OTLP 可选 | 零依赖默认 |
| 17 | 标签流转 | `weave` → `weave-running` → `weave-pr`/`weave-failed` | 生命周期清晰 |
| 18 | 模块组织 | `plugins/` 接口 + `plugins/github/` 内置实现 | 按职责定义接口，GitHub 作为插件 |
| 19 | Webhook 归属 | GitHub 插件自行实现 API server | 插件自包含，不污染核心 |
| 20 | 成本跟踪 | 只跟踪 token 计数，不做 USD 换算 | M5 阶段够用 |
| 21 | Trace 事件 | 新增 6 个 TRACE EventType，融入现有 JSONL | 不建新存储系统 |
| 22 | Slug 生成 | Issue title 前 5 词，小写+连字符，≤50 字符 | 简单可预测 |

---

## Research Summary

**Market Context**
- OTel 是 AI agent 可观测性标准（Datadog/Honeycomb/New Relic 原生支持）
- GenAI Semantic Conventions 覆盖 LLM call/tool span，multi-agent 关系层尚未标准化
- Dogfooding 成功案例：Miniforge（530+ PRs）、WorkOS Horizon（compounding loop）、Kitchen Loop（zero regressions）

**Technical Context**
- OTel 基础已有（`monitoring/otel.py` 116 行），需扩展为 4 层 span 树
- Claude Code SDK 支持 `query()` 流式返回 per-tool-call 事件 + per-message token
- GitHub 集成完全缺失，需从零构建
- `gh` CLI 已验证可用
- M5.0 稳定性修复已完成
- Weave 有两层 Backend：`ExecutionBackend`（workspace 隔离）和 `AgentBackend`（agent 执行），M5 不混淆

---

*Generated: 2026-05-22*
*Status: VALIDATED — grill-me + grill-with-docs completed, 22 decisions resolved*
*CONTEXT.md created, ADR-0016 pending*
