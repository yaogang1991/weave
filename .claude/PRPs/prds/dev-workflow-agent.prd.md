# Development Workflow Agent

## Problem Statement

开发者使用 Claude Code 开发新需求时，需要手动走完 PRD 生成、需求对齐、实现、代码审查、PR 提交的完整流程，每一步都要反复确认和切换上下文，重复性工作多、效率低下。当前虽有各环节的独立技能（/ecc:prp-prd、/grill-me 等），但缺乏一个串联全流程的自动化 Agent。

## Evidence

- 项目负责人（用户本人）反馈：重复性工作多，需要一步步确认，效率低下
- 项目已有完整 ECC 技能体系覆盖各环节，但技能间需手动串联
- 社区已有多个 PRD→PR 自动化流水线项目（pipeline-skills、coco-workflow、mpx-claude-code），证明模式可行

## Proposed Solution

创建一个基于 Claude Code Sub-agent 的开发工作流 Agent（`.claude/agents/dev-workflow.md`），输入需求描述后自动串联 6 个 ECC 技能完成全流程：PRD 生成 → 需求对齐（grill-me + grill-with-docs）→ 文档更新 → 实现 → 代码审查 → PR 提交。所有交互点（grill 提问、审批确认）由 LLM 自行判定，用户只需在起点输入需求描述、在终点收到 PR 链接。

## Key Hypothesis

我们相信这个 Agent 能将需求到 PR 的全流程自动化，使开发者只需输入需求描述即可等待 PR 提交。
验证标准：对齐 PRD 后，全程零人工干预，最终输出可直接审查的 PR。

## What We're NOT Building

- CI/CD 部署流水线 — 超出开发阶段范围
- 多用户/团队协作 — 当前仅个人使用
- 需求自动发现 — 需要用户手动输入需求描述
- 复杂度自动路由（简单需求跳过步骤）— MVP 阶段统一走完整流程

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|--------------|
| 需求→PR 全流程完成率 | ≥90% | 统计成功输出 PR 的任务占比 |
| 人工干预次数 | 0次（对齐后） | 统计流程中需要人工介入的次数 |
| PR 一次审查通过率 | ≥70% | PR 首次 review 后无需 major changes |

## Open Questions

- [ ] LLM 自行判定对齐问题时，判定质量的兜底策略是什么？
- [ ] 流程中某个环节失败时的回退策略：重试 vs 跳过 vs 终止？

---

## Users & Context

**Primary User**
- **Who**: 个人开发者（项目负责人）
- **Current behavior**: 收到新需求后，手动依次执行 /ecc:prp-prd → /grill-me → /grill-with-docs → /ecc:prp-implement → /ecc:code-review → /ecc:prp-pr
- **Trigger**: 有新需求要实现时
- **Success state**: 输入需求描述，等待后收到可审查的 PR 链接

**Job to Be Done**
当有新需求要实现时，我想要执行这个 agent 自动对齐 PRD 并完成全流程实现，以便将需求开发效率最大化。

**Non-Users**
非开发者、需要多人协作的团队场景

---

## Solution Detail

### Core Capabilities (MoSCoW)

| Priority | Capability | Rationale |
|----------|------------|-----------|
| Must | Agent 定义文件 (.claude/agents/dev-workflow.md) | Sub-agent 入口，定义工具、模型、流程指令 |
| Must | 6 步流水线编排（PRD→Grill→Docs→Implement→Review→PR） | 核心价值，串联现有 ECC 技能 |
| Must | LLM 自主判定（替代人工交互） | 全自动化的关键，grill 和审批环节由 LLM 按推荐选项自行决策 |
| Must | 文件系统状态传递（PRD→Plan→实现→PR） | 技能间通过 .claude/PRPs/ 目录传递中间产物 |
| Should | 流程进度报告 | 每完成一个阶段向用户报告当前进度 |
| Should | 失败重试与回退 | 单个环节失败时自动重试或优雅降级 |
| Could | 复杂度自动路由 | 简单 bug fix 跳过完整流程，走轻量路径 |
| Won't | 多用户协作支持 | 当前仅个人使用场景 |

### MVP Scope

一个 `.claude/agents/dev-workflow.md` 文件，定义开发工作流 Agent 的完整行为：工具集、流程指令、LLM 自主判定策略。用户通过调用此 Agent 触发，Agent 自动走完 6 步流水线。

### User Flow

```
用户输入需求描述
    ↓
Agent 启动，调用 /ecc:prp-prd 生成 PRD
    ↓
调用 /grill-me 进行需求对齐（LLM 自主回答，选推荐选项）
    ↓
调用 /grill-with-docs 进行文档对齐（LLM 自主回答，选推荐选项）
    ↓
更新 CONTEXT.md / ADR 等文档
    ↓
调用 /ecc:prp-implement 执行实现
    ↓
调用 /ecc:code-review 执行代码审查
    ↓
调用 /ecc:prp-pr 提交 PR
    ↓
返回 PR 链接给用户
```

---

## Technical Approach

**Feasibility**: HIGH

**Architecture Notes**
- 基于 Claude Code Sub-agent 体系，Agent 定义为 `.claude/agents/dev-workflow.md`
- 通过 Skill tool 依次调用 ECC 技能，每个技能独立上下文
- 中间产物通过文件系统传递：PRD → `.claude/PRPs/prds/`，Plan → `.claude/PRPs/plans/`
- LLM 自主判定策略：对 grill 类提问，始终选择第一个（推荐）选项；对确认类提问，始终确认

**Technical Risks**

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| 上下文窗口溢出（6步链式调用） | M | 每个技能独立上下文，通过文件传递状态而非累积对话 |
| LLM 自主判定质量不足 | M | grill 类问题选择推荐选项（已由 ECC 优化），降低决策风险 |
| 单环节失败导致全流程中断 | M | 每步检查文件产物是否存在，失败时报告具体环节和原因 |
| 技能间文件路径不一致 | L | PRD 文件路径作为后续技能的输入参数，硬编码路径约定 |

---

## Implementation Phases

| # | Phase | Description | Status | Parallel | Depends | PRP Plan |
|---|-------|-------------|--------|----------|---------|----------|
| 1 | Agent 定义 | 创建 .claude/agents/dev-workflow.md | pending | - | - | - |
| 2 | 流程指令编写 | 编写 6 步流水线的详细指令 | pending | - | 1 | - |
| 3 | LLM 自主判定策略 | 定义各环节的自动决策规则 | pending | with 2 | 1 | - |
| 4 | 集成测试 | 端到端测试完整流水线 | pending | - | 2, 3 | - |

### Phase Details

**Phase 1: Agent 定义**
- **Goal**: 创建 Claude Code Sub-agent 定义文件
- **Scope**: `.claude/agents/dev-workflow.md`，包含 YAML frontmatter（name、description、tools、model）
- **Success signal**: Agent 文件存在且可被 Claude Code 识别

**Phase 2: 流程指令编写**
- **Goal**: 在 Agent 定义文件中编写完整的 6 步流水线指令
- **Scope**: 详细描述每个步骤的输入、输出、调用方式、文件传递约定
- **Success signal**: 指令覆盖全部 6 步，路径约定一致

**Phase 3: LLM 自主判定策略**
- **Goal**: 定义各环节的自动决策规则
- **Scope**: grill-me/grill-with-docs 的回答策略、确认类操作的默认行为
- **Success signal**: 每个 grill 提问点都有明确的自动回答规则

**Phase 4: 集成测试**
- **Goal**: 端到端验证完整流水线
- **Scope**: 用一个简单需求跑完 6 步，验证 PR 生成
- **Success signal**: 输入需求描述 → 输出 PR 链接，全程无人工干预

### Parallelism Notes

Phase 2 和 Phase 3 可并行编写，它们分别处理流程步骤和决策策略，最终合并到同一个 Agent 定义文件中。

---

## Decisions Log

| Decision | Choice | Alternatives | Rationale |
|----------|--------|--------------|-----------|
| Agent 类型 | Claude Code Sub-agent | 独立脚本、自定义编排引擎 | 直接利用 ECC 技能体系，无需额外基础设施 |
| 交互模式 | 全自动（LLM 自主判定） | 半自动（人工确认关键点） | 用户明确要求全自动，grill 推荐选项已优化 |
| 状态传递 | 文件系统 | 内存/数据库 | 技能间通过 .claude/PRPs/ 目录天然支持 |
| 模型选择 | 继承父级（Opus） | Sonnet（降成本） | 对齐和规划需要高质量推理，值得用 Opus |

---

## Research Summary

**Market Context**
- Claude Code 四大扩展原语（Skills、Hooks、Agents、MCP）为 Agent 构建提供完整支撑
- 社区已有 8+ 个 PRD→PR 自动化项目，核心模式一致，验证了可行性
- Anthropic Managed Agents 的 brain/hands/session 分离提供了架构参考

**Technical Context**
- ECC 技能体系已覆盖全部 6 个环节（prp-prd、grill-me、grill-with-docs、prp-implement、code-review、prp-pr）
- Claude Code Sub-agent 体系支持工具限制、模型选择、上下文隔离
- 技能间通过文件系统传递中间产物，天然解耦

---

*Generated: 2026-05-27*
*Status: DRAFT - needs validation*
