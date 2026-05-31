# M5.3: Execute → PR

## Problem Statement

M5.2 实现了 GitHub Issue → DAG 执行的流程，但执行完成后代码留在本地 worktree 中，没有推送回 GitHub。solo developer 和未来其他使用 Weave 的开发者无法在无人值守场景下完成 Issue → PR 的完整闭环，每次都需要手动 git push + gh pr create。

## Evidence

- `_execute_issue()` 在 `service.run_job()` 返回后仅做标签流转，从不调用 `host.push_changes()` 或 `host.create_pr()`
- `GitHubCodeHost` 已有 push/create_pr(draft)/comment/labels 全部方法，但从未被 post-execution 流程调用
- M5 PRD 明确定义 M5.3 为 "Execute → PR"，M5.2 只完成了一半（到执行为止）
- SWE-agent、OpenHands、Devin 等竞品都在执行后自动创建 Draft PR 作为默认行为

## Proposed Solution

在 `integrations/github/` 下新增 `post_execution.py` 和 `pr_body.py`，实现执行后的三态判断（零产出/部分成功/全部成功）、git push、PR body 生成（模板 + LLM code review 嵌入）、PR 创建的完整编排。`cli/github.py:_execute_issue()` 调用 `post_execution.handle_result()` 一步完成。

## Key Hypothesis

We believe post-execution PR automation will let Weave autonomously complete the Issue → PR lifecycle for solo developers.
We'll know we're right when a real GitHub Issue with `weave` label results in a PR appearing on GitHub with embedded code review, without any manual intervention.

## What We're NOT Building

- CI status check 监控 — M5.5 范围
- PR auto-merge — M5.5 范围
- 自动重试循环 — 手动 `issue-run` 同一 issue number 触发
- 跨 repo PR / fork PR — M5 聚焦 single repo
- USD 成本换算 — 只跟踪 token
- Webhook 触发 — M5.2 已有 CLI 触发，Webhook 留给 M5.5

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|--------------|
| Issue → PR end-to-end | 成功 Issue 自动生成 PR | 手动验证 |
| 三态处理正确性 | 每种状态行为符合预期 | 单元测试覆盖 |
| PR body 质量 | 包含 diff stat + code review + test plan | 手动审查 |

---

## Users & Context

**Primary User**
- **Who**: Weave 项目 solo maintainer 及未来使用 Weave 的开发者
- **Current behavior**: 手动运行 `issue-run`，执行完后手动 git push + gh pr create
- **Trigger**: GitHub Issue 添加 `weave` 标签后执行完成
- **Success state**: 执行完成后 PR 自动出现在 GitHub，包含 code review 评论

**Job to Be Done**

When Weave finishes executing a GitHub Issue, I want it to automatically push changes and create a PR with code review, so I can focus on reviewing rather than manual git operations.

**Non-Users**
- 团队协作场景（M5 不覆盖）
- 非 GitHub 平台（M5 仅实现 GitHub）

---

## Solution Detail

### Core Capabilities (MoSCoW)

| Priority | Capability | Rationale |
|----------|------------|-----------|
| Must | 三态判断（零产出/部分成功/全部成功） | 决定是否建 PR、Draft 还是正式 PR |
| Must | Git push 到远端分支 | 代码必须到达 GitHub |
| Must | PR body 生成（diff stat + LLM review + 模板） | PR 出现时自带完整信息 |
| Must | PR 创建（Draft/正式） | 核心价值 |
| Must | 失败处理（push 失败/零产出的 Issue 评论） | 不留半成品 |
| Should | force push 重试支持 | 同 issue 重跑时更新已有 PR |
| Won't | CI check 监控 | M5.5 |
| Won't | Auto-merge | M5.5 |
| Won't | Webhook 触发 | M5.5 |

### User Flow

```
1. issue-run 执行完成 → run_job() 返回 Run
2. post_execution.handle_result() 被调用
3. 检测 git diff --stat 判断是否有代码产出
4. 有产出 → LLM 生成 code review → 拼 PR body
5. git push --force-with-lease 到远端
6. push 成功 → gh pr create（Draft 或正式）
7. 更新标签：weave-running → weave-pr（成功）/ weave-failed（失败）
8. PR 出现在 GitHub，body 包含 Summary + Changes + Code Review + Test plan
```

---

## Technical Approach

**Feasibility**: HIGH

**Architecture Notes**
- 新增 `integrations/github/post_execution.py` — 三态判断 + push + PR 创建的编排入口
- 新增 `integrations/github/pr_body.py` — git diff --stat 提取 + LLM code review + 模板拼接
- `_execute_issue()` 调用 `handle_result(run, metadata, host)` — 一行接入
- worktree 路径从 `run.dag_result["work_dir"]` 获取（`run_job()` 内部写入，`_execute_issue()` 消费）
- push 前显式执行 `git add -A && git commit`（agent 写文件但不一定 commit）
- `CodeHost.push_changes()` 加 `cwd` 参数，确保在 worktree 目录执行 git 操作
- LLM 调用用 Sonnet（不是 Opus），失败时 fallback 到纯模板（无 review 段）
- 所有 git 操作通过 `core/subprocess_runner.run_with_progress()` 执行

**Technical Risks**

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| worktree 路径获取不到 | L | `run_job()` 已在 `_RunContext` 中持有 work_dir，写入 dag_result 即可 |
| Agent 未 commit | M | post_execution 在 push 前显式 git add + commit |
| LLM review 调用超时 | M | catch + fallback 到纯模板 PR body |
| push 冲突（远端有更新） | L | `--force-with-lease` 安全 force push |
| PR body 过长 | L | diff --stat 而非完整 diff，review 截断 4000 字符 |

---

## Implementation Phases

| # | Phase | Description | Status | Parallel | Depends | PRP Plan |
|---|-------|-------------|--------|----------|---------|----------|
| 1 | pr_body.py | PR body 生成（diff stat + LLM review + 模板） | in-progress | - | - | `.claude/PRPs/plans/m5-3-execute-to-pr.plan.md` |
| 2 | post_execution.py | 三态判断 + push + PR 创建编排 | in-progress | - | 1 | `.claude/PRPs/plans/m5-3-execute-to-pr.plan.md` |
| 3 | _execute_issue() 接入 | 调用 handle_result()，存 worktree 路径 | in-progress | - | 2 | `.claude/PRPs/plans/m5-3-execute-to-pr.plan.md` |
| 4 | 测试 | 三态 + pr_body + 集成测试 | pending | - | 3 | - |

### Phase Details

**Phase 1: pr_body.py**
- **Goal**: 给定 worktree 路径，生成完整 PR body
- **Scope**:
  - `generate_pr_body(work_dir, issue, llm_config)` → str
  - 内部调用 `git diff --stat HEAD` 获取变更统计
  - LLM（Sonnet）读 `git diff` 输出，生成 code review（变更摘要 + 风险点 + 测试覆盖建议）
  - LLM 失败时 fallback：PR body 只含 Summary + Changes + Test plan，无 Code Review 段
  - 模板结构：Summary + Changes + Code Review + Test plan + Fixes #N
- **Success signal**: `generate_pr_body()` 返回格式正确的 markdown 字符串

**Phase 2: post_execution.py**
- **Goal**: 根据执行结果决定 post-execution 行为
- **Scope**:
  - `PostExecutionResult` 模型：
    ```python
    class PostExecutionResult(BaseModel):
        status: Literal["no_output", "partial", "success", "push_failed"]
        pr_url: str = ""
        issue_comment: str = ""
    ```
  - `handle_result(run, job_metadata, host)` → PostExecutionResult
  - 从 `run.dag_result["work_dir"]` 获取 worktree 路径
  - 三态判断：
    - `git diff --stat` 空 → 零产出 → Issue 评论 "执行完成但无代码变更" → 返回
    - 有 diff + `run.status != SUCCEEDED` → 部分成功 → Draft PR + Issue 评论
    - 有 diff + `run.status == SUCCEEDED` → 全部成功 → 正常 PR
  - push 前执行 `git add -A && git commit -m "Fix #{issue_number}: {issue.title}"`
  - 调用 `host.push_changes(repo, branch, cwd=work_dir)`，失败 → Issue 评论 "代码推送失败" → 返回
  - 调用 `pr_body.generate_pr_body(work_dir, issue, llm_config)` 获取 body
  - 调用 `host.create_pr()` 创建 PR
  - 所有 git 操作 cwd 指向 worktree
- **Success signal**: 给定 mock Run + mock Host，三态分别产生正确行为

**Phase 3: _execute_issue() 接入 + CodeHost cwd**
- **Goal**: `cli/github.py` 调用 post_execution，CodeHost 支持 cwd
- **Scope**:
  - `CodeHost.push_changes()` 加 `cwd: str | None = None` 参数，透传给 `run_with_progress`
  - `GitHubCodeHost.push_changes()` 实现更新，使用 `cwd` 参数
  - `control_plane/service.py` 的 `_resolve_run_outcome()` 把 `work_dir` 写入 `run.dag_result["work_dir"]`
  - `_execute_issue()` 在 `run_job()` 返回后从 `run.dag_result["work_dir"]` 取 worktree 路径
  - 调用 `handle_result(run, metadata, host)` 传入 worktree 路径
  - 根据 `PostExecutionResult.status` 更新标签（保留现有标签流转逻辑）
  - 修复 `run.error` bug：改为 `run.dag_result.get("error", "Unknown error")`
- **Success signal**: `_execute_issue()` 完整流程可运行

**Phase 4: 测试**
- **Goal**: 覆盖所有新增逻辑
- **Scope**:
  - `tests/test_pr_body.py` — diff stat 解析、LLM review 生成、模板渲染、fallback
  - `tests/test_post_execution.py` — 三态判断、push 失败处理、PR 创建
  - 更新 `tests/test_integrations.py` — `_execute_issue()` 集成测试
- **Success signal**: 新增测试全部通过，现有测试无回归

---

## Decisions Log

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Post-execution 逻辑位置 | 新建 `integrations/github/post_execution.py` | 独立模块，职责清晰，便于测试 |
| 2 | 代码产出检测 | `git diff --stat HEAD` | 直接反映文件变更，不依赖 dag_result |
| 3 | 测试通过检测 | `run.status == SUCCEEDED` | DAG 执行已含评估，不需要额外跑 evaluator |
| 4 | PR body 策略 | 模板 + LLM code review 嵌入 | PR 出现时自带完整 review，一步到位 |
| 5 | Code review 嵌入 PR body | 合并进 `pr_body.py` | 不做单独 comment，减少 GitHub API 调用 |
| 6 | 三态逻辑 | 零产出/部分成功(Draft)/全部成功(正式) | 对应三种不同的 GitHub 操作 |
| 7 | 重试策略 | `--force-with-lease`，手动触发 | 安全 force push，M5.3 不做自动重试循环 |
| 8 | Push 失败处理 | 不建 PR + Issue 评论 + weave-failed 标签 | 不留半成品 |
| 9 | Worktree 路径 | `run_job()` 写入 `run.dag_result["work_dir"]` | `_execute_issue()` 无法预知路径，由 run_job 内部写入 |
| 10 | LLM 模型 | Sonnet（非 Opus） | Review 不需要最强模型，节省成本 |
| 11 | LLM 失败 fallback | 纯模板 PR body（无 review 段） | 不因 review 失败阻塞 PR 创建 |
| 12 | Git diff 范围 | `git diff --stat HEAD`（非完整 diff） | body 长度可控，完整 diff 给 LLM review |
| 13 | Git commit | post_execution 在 push 前显式 git add + commit | agent 写文件但不一定 commit，push 需要 commit |
| 14 | CodeHost cwd | `push_changes()` 加 `cwd` 参数 | worktree 与当前目录不同，git 操作需指定 cwd |
| 15 | run.error bug | 改为 `run.dag_result.get("error", "Unknown error")` | Run 模型无 error 字段，dag_result 含 error key |

---

## Research Summary

**Market Context**
- SWE-agent: 始终 Draft PR + hook 触发 + pre-flight 检查 + trajectory 嵌入 PR body
- OpenHands: 三档上传模式（branch/draft/ready），变量替换 commit message
- Devin: 模板发现链 + CI auto-fix 循环 + severity labels review
- Copilot: LLM 生成 PR body + `/pr auto` 修复循环
- 共性模式：Draft PR 是安全默认；pre-flight 检查防重复；模板分层

**Technical Context**
- `GitHubCodeHost` 已有全部底层方法（push/create_pr(draft)/comment/labels）
- Post-execution hook 体系存在但不如直接调用 `post_execution` 简洁
- `run.dag_result` 包含节点执行摘要
- `core/subprocess_runner.py` 可复用于所有 git/gh 命令
- 已知 bug: `cli/github.py:95` 访问 `run.error`，但 Run 模型无此字段

---

*Generated: 2026-05-26*
*Status: VALIDATED — grill-me + grill-with-docs completed, 15 decisions resolved*
