# Plan: M5.3 Execute → PR

## Summary

在 `integrations/github/` 下新增 `pr_body.py` 和 `post_execution.py`，实现 DAG 执行后的三态判断、git commit + push、PR body 生成（模板 + LLM code review）、PR 创建的完整闭环。修改 `CodeHost` 接口支持 cwd，在 `service.py` 注入 work_dir 到 dag_result。

## User Story

As a solo developer using Weave, I want executed issues to automatically generate PRs on GitHub with code review, so I can focus on reviewing rather than manual git operations.

## Problem → Solution

**Before**: `_execute_issue()` 跑完 `run_job()` 后只做标签流转，代码留在 worktree，无 PR。
**After**: `run_job()` 返回后自动 commit → push → 生成 PR body（含 LLM review）→ 创建 PR → 标签流转。

## Metadata
- **Complexity**: Medium
- **Source PRD**: `.claude/PRPs/prds/m5-3-execute-to-pr.prd.md`
- **Estimated Files**: 8 (2 new, 5 modify, 1 test)

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `integrations/github/github_host.py` | all | CodeHost 实现，需加 cwd |
| P0 | `cli/github.py` | all | _execute_issue() 接入点 |
| P0 | `core/llm_client.py` | 86, 174 | LLMClient 构造 + call() 签名 |
| P1 | `integrations/base.py` | all | CodeHost ABC |
| P1 | `control_plane/service.py` | 632-701 | _resolve_run_outcome() |
| P1 | `control_plane/run_lifecycle.py` | 29-48 | mark_succeeded/mark_failed |
| P2 | `core/subprocess_runner.py` | 34-49 | run_with_progress 签名（已支持 cwd） |
| P2 | `integrations/models.py` | all | NormalizedIssue 模型 |

---

## Patterns to Mirror

### LLM_CALL (CORRECTED — sync call wrapped in asyncio.to_thread)

`LLMClient.__init__` takes `LLMConfig`, NOT kwargs. `call()` is sync, NOT async. No `chat()` method exists.

```python
// SOURCE: core/llm_client.py:86, core/llm_client.py:174
from core.llm_client import LLMClient
from core.config import LLMConfig

config = LLMConfig(
    api_key=self._llm_config.api_key,
    model="claude-sonnet-4-6",
    provider="anthropic",
)
client = LLMClient(config)
response = await asyncio.to_thread(
    client.call,
    messages=[{"role": "user", "content": prompt}],
    tools=[],
    max_tokens_override=500,
)
text = response.get("content", "")
```

### SUBPROCESS (with cwd)

```python
// SOURCE: core/subprocess_runner.py:34-49, integrations/github/github_host.py:27-35
result = await asyncio.to_thread(
    run_with_progress,
    ["git", "push", "origin", branch, "--force-with-lease"],
    timeout=60,
    cwd=work_dir,  # supported since line 40
)
if result.returncode != 0:
    logger.error("git push failed: %s", result.stderr)
    return False
```

### TEST_MOCK_SUBPROCESS

```python
// SOURCE: tests/test_integrations.py:153-156
@pytest.mark.asyncio
async def test_something(self):
    with patch("integrations.github.github_host.run_with_progress") as mock:
        mock.return_value = MagicMock(returncode=0)
```

### TEST_MOCK_LLM

```python
// SOURCE: tests/test_worker.py:19-21, tests/conftest.py:47-53
mock_llm = MagicMock()
mock_llm.call.return_value = {"role": "assistant", "content": "review text"}
```

### ERROR_HANDLING (never raise, return error result)

```python
// SOURCE: integrations/github/github_host.py
if result.returncode != 0:
    logger.error("... failed: %s", result.stderr)
    return ""  # or False
```

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `integrations/github/pr_body.py` | CREATE | PR body 生成 |
| `integrations/github/post_execution.py` | CREATE | 三态编排 |
| `integrations/base.py` | UPDATE | push_changes() 加 cwd |
| `integrations/github/github_host.py` | UPDATE | push_changes() 传递 cwd |
| `control_plane/service.py` | UPDATE | _resolve_run_outcome 注入 work_dir |
| `cli/github.py` | UPDATE | 调用 post_execution + 修复 run.error |
| `tests/test_pr_body.py` | CREATE | pr_body 测试 |
| `tests/test_post_execution.py` | CREATE | post_execution 测试 |

## NOT Building

- CI status check, auto-merge, auto-retry loop, cross-repo PR, webhook trigger

---

## Step-by-Step Tasks

### Task 1: integrations/github/pr_body.py — CREATE

- **ACTION**: 创建 PR body 生成模块
- **IMPLEMENT**:
  ```python
  """PR body generation -- diff stat + LLM code review + template."""
  from __future__ import annotations

  import asyncio
  import logging
  from typing import Any

  from core.subprocess_runner import run_with_progress
  from integrations.models import NormalizedIssue

  logger = logging.getLogger(__name__)


  async def get_diff_stat(work_dir: str) -> str:
      result = await asyncio.to_thread(
          run_with_progress, ["git", "diff", "--stat", "HEAD"],
          timeout=30, cwd=work_dir,
      )
      return result.stdout.strip() if result.returncode == 0 else ""


  async def get_full_diff(work_dir: str, max_chars: int = 15000) -> str:
      result = await asyncio.to_thread(
          run_with_progress, ["git", "diff", "HEAD"],
          timeout=60, cwd=work_dir,
      )
      if result.returncode != 0:
          return ""
      text = result.stdout.strip()
      return text[:max_chars] if len(text) > max_chars else text


  async def generate_llm_review(diff_text: str, llm_config: Any) -> str:
      if not diff_text:
          return ""
      try:
          from core.config import LLMConfig
          from core.llm_client import LLMClient

          config = LLMConfig(
              api_key=llm_config.api_key,
              model=getattr(llm_config, "model", "claude-sonnet-4-6"),
              provider=getattr(llm_config, "provider", "anthropic"),
              base_url=getattr(llm_config, "base_url", None),
          )
          client = LLMClient(config)
          prompt = (
              "Review this code diff. Provide:\n"
              "1. Brief summary of changes (2-3 sentences)\n"
              "2. Potential risks or issues\n"
              "3. Test coverage suggestions\n\n"
              f"```\n{diff_text}\n```"
          )
          response = await asyncio.to_thread(
              client.call,
              messages=[{"role": "user", "content": prompt}],
              tools=[],
              max_tokens_override=500,
          )
          content = response.get("content", "")
          return content.strip()[:4000]
      except Exception:
          logger.exception("LLM review generation failed")
          return ""


  async def generate_pr_body(
      work_dir: str,
      issue: NormalizedIssue,
      llm_config: Any = None,
  ) -> str:
      diff_stat = await get_diff_stat(work_dir)
      review_section = ""
      if llm_config:
          full_diff = await get_full_diff(work_dir)
          review = await generate_llm_review(full_diff, llm_config)
          if review:
              review_section = f"\n## Code Review\n{review}\n"
      return (
          f"## Summary\nFix #{issue.number}: {issue.title}\n\n"
          f"## Changes\n```\n{diff_stat}\n```\n"
          f"{review_section}"
          f"\n## Test plan\n- [ ] python -m pytest -v --tb=short\n\n"
          f"Fixes #{issue.number}\n"
      )
  ```
- **MIRROR**: `core/llm_client.py:86` for LLMClient(LlmConfig), `core/llm_client.py:174` for call() sync
- **GOTCHA**: `LLMClient.call()` is SYNC, must wrap in `asyncio.to_thread`. Constructor takes `LLMConfig` object, NOT kwargs.
- **VALIDATE**: `python -c "from integrations.github.pr_body import generate_pr_body; print('OK')"`

### Task 2: integrations/github/post_execution.py — CREATE

- **ACTION**: 创建三态 post-execution 编排模块
- **IMPLEMENT**:
  ```python
  """Post-execution handler -- three-state outcome + commit + push + PR."""
  from __future__ import annotations

  import asyncio
  import logging
  from typing import Any, Literal

  from pydantic import BaseModel

  from core.subprocess_runner import run_with_progress
  from integrations.base import CodeHost
  from integrations.github.pr_body import generate_pr_body
  from integrations.models import NormalizedIssue

  logger = logging.getLogger(__name__)


  class PostExecutionResult(BaseModel):
      status: Literal["no_output", "partial", "success", "push_failed"]
      pr_url: str = ""
      issue_comment: str = ""


  async def _has_changes(work_dir: str) -> bool:
      result = await asyncio.to_thread(
          run_with_progress, ["git", "diff", "--stat", "HEAD"],
          timeout=15, cwd=work_dir,
      )
      return result.returncode == 0 and bool(result.stdout.strip())


  async def _commit_changes(work_dir: str, issue: NormalizedIssue) -> bool:
      add_result = await asyncio.to_thread(
          run_with_progress, ["git", "add", "-A"],
          timeout=30, cwd=work_dir,
      )
      if add_result.returncode != 0:
          logger.error("git add failed: %s", add_result.stderr)
          return False
      commit_result = await asyncio.to_thread(
          run_with_progress,
          ["git", "commit", "-m", f"Fix #{issue.number}: {issue.title}"],
          timeout=30, cwd=work_dir,
      )
      if commit_result.returncode != 0:
          logger.error("git commit failed: %s", commit_result.stderr)
          return False
      return True


  async def handle_result(
      run: Any,
      job_metadata: dict[str, Any],
      host: CodeHost,
      llm_config: Any = None,
  ) -> PostExecutionResult:
      from control_plane.models import RunStatus

      work_dir = (run.dag_result or {}).get("work_dir")
      if not work_dir:
          return PostExecutionResult(
              status="push_failed",
              issue_comment="No work directory found in run result.",
          )

      issue_number = job_metadata.get("issue_number", 0)
      issue_title = job_metadata.get("requirement", "")
      branch = job_metadata.get("branch_name", "")
      repo = job_metadata.get("repo", "")

      issue = NormalizedIssue(
          number=issue_number, title=issue_title,
          url=job_metadata.get("issue_url", ""), repo=repo,
      )

      if not await _has_changes(work_dir):
          return PostExecutionResult(
              status="no_output",
              issue_comment="Execution completed but no code changes were produced.",
          )

      if not await _commit_changes(work_dir, issue):
          return PostExecutionResult(
              status="push_failed",
              issue_comment="Failed to commit changes.",
          )

      is_success = run.status == RunStatus.SUCCEEDED
      body = await generate_pr_body(work_dir, issue, llm_config)
      pushed = await host.push_changes(repo, branch, cwd=work_dir)
      if not pushed:
          return PostExecutionResult(
              status="push_failed",
              issue_comment=f"Code push failed for branch `{branch}`.",
          )

      pr_title = f"Fix #{issue_number}: {issue_title[:60]}"
      pr_url = await host.create_pr(repo, branch, pr_title, body, draft=not is_success)

      if is_success:
          return PostExecutionResult(status="success", pr_url=pr_url)

      error = (run.dag_result or {}).get("error", "Unknown error")
      return PostExecutionResult(
          status="partial", pr_url=pr_url,
          issue_comment=(
              f"Weave executed this issue but encountered errors.\n\n"
              f"**Error**: {str(error)[:500]}\n"
              f"**PR**: {pr_url}\n\n"
              f"A draft PR has been created with partial changes."
          ),
      )
  ```
- **MIRROR**: `integrations/github/github_host.py` for error handling (never raise)
- **GOTCHA**: `run.status` 是 `RunStatus` 枚举，用 `== RunStatus.SUCCEEDED` 比较
- **VALIDATE**: `python -c "from integrations.github.post_execution import handle_result, PostExecutionResult; print('OK')"`

### Task 3: integrations/base.py — push_changes() 加 cwd

- **ACTION**: 修改 ABC 签名
- **IMPLEMENT**: `push_changes` 加 `cwd: str | None = None` 参数
  ```python
  @abc.abstractmethod
  async def push_changes(self, repo: str, branch: str, *,
                         cwd: str | None = None) -> bool:
      ...
  ```
- **VALIDATE**: `python -c "from integrations.base import CodeHost; print('OK')"`

### Task 4: integrations/github/github_host.py — push_changes 传递 cwd

- **ACTION**: 更新实现
- **IMPLEMENT**: 修改 `push_changes` 方法签名加 `cwd`，传给 `run_with_progress`
  ```python
  async def push_changes(self, repo: str, branch: str, *,
                         cwd: str | None = None) -> bool:
      result = await asyncio.to_thread(
          run_with_progress,
          ["git", "push", "origin", branch, "--force-with-lease"],
          timeout=60, cwd=cwd,
      )
      ...
  ```
- **GOTCHA**: `run_with_progress` 已支持 `cwd` 参数（`subprocess_runner.py:40`）
- **VALIDATE**: `python -c "from integrations.github.github_host import GitHubCodeHost; print('OK')"`

### Task 5: control_plane/service.py — _resolve_run_outcome 注入 work_dir

- **ACTION**: 在 `_resolve_run_outcome()` 末尾把 `ctx.work_dir` 写入 `run.dag_result`
- **IMPLEMENT**: 在 `return run` 之前（每个 return 点之前），添加：
  ```python
  if ctx.work_dir:
      run.dag_result.setdefault("work_dir", ctx.work_dir)
  ```
  具体位置：succeeded 分支（line 658）、canceled 分支（line 669）、failed/timeout 分支（line 701）的 return 前。
- **GOTCHA**: 必须在 `mark_succeeded/mark_failed` 之后（这些方法会覆盖 `dag_result`），用 `setdefault` 不覆盖已有 key
- **VALIDATE**: 确认 `_resolve_run_outcome` 所有返回路径都注入了 work_dir

### Task 6: cli/github.py — _execute_issue() 接入 + run.error 修复

- **ACTION**: 修改 `_execute_issue()` 调用 `handle_result()`
- **IMPLEMENT**:
  1. 导入: `from integrations.github.post_execution import handle_result`
  2. 在 `run_job()` 后，调用 `result = await handle_result(run, metadata, host, llm_config=WeaveConfig.from_env().llm)`
  3. 根据 `result.status` 更新标签:
     - `success` → `weave-pr`
     - `partial` → `weave-pr` + `host.comment_on_issue()`
     - `no_output` → `weave-failed` + `host.comment_on_issue()`
     - `push_failed` → `weave-failed` + `host.comment_on_issue()`
  4. 修复 line 95: `run.error` → `run.dag_result.get("error", "Unknown error")`
  5. 在 metadata 中补充 `requirement` 字段（handle_result 需要用来构建 issue title）
- **GOTCHA**: `_execute_issue()` 已在 metadata 中有 `issue_number`、`issue_url`、`branch_name`、`integration_type`，但缺 `repo` 和 `requirement`
- **VALIDATE**: `python -c "from cli.github import cmd_issue_poll; print('OK')"`

### Task 7: tests/test_pr_body.py — CREATE

- **ACTION**: 测试 pr_body 模块
- **IMPLEMENT**: 4 个测试类
  - `TestGetDiffStat` — mock `run_with_progress`, 验证空/非空返回
  - `TestGetFullDiff` — mock `run_with_progress`, 验证截断到 max_chars
  - `TestGenerateLlmReview` — mock `LLMClient.call`, 验证成功返回 review 文本，失败返回 ""
  - `TestGeneratePrBody` — 集成验证模板格式，含/不含 Code Review 段
- **MIRROR**: `tests/test_integrations.py` mock 模式 + `@pytest.mark.asyncio`
- **VALIDATE**: `python -m pytest tests/test_pr_body.py -v`

### Task 8: tests/test_post_execution.py — CREATE

- **ACTION**: 测试三态判断
- **IMPLEMENT**: 6 个测试
  - `test_no_output` — git diff 空 → status=no_output
  - `test_partial_success` — 有 diff + run.status=FAILED → status=partial, Draft PR
  - `test_full_success` — 有 diff + run.status=SUCCEEDED → status=success, 正常 PR
  - `test_push_failed` — push returns False → status=push_failed
  - `test_commit_failed` — git add fails → status=push_failed
  - `test_no_work_dir` — dag_result 无 work_dir → status=push_failed
- **MIRROR**: mock Run + mock Host + mock run_with_progress
- **VALIDATE**: `python -m pytest tests/test_post_execution.py -v`

---

## Testing Strategy

### Unit Tests

| Test | Input | Expected | Edge |
|---|---|---|---|
| get_diff_stat 空 | returncode=0, stdout="" | "" | Yes |
| get_full_diff 截断 | 20000 chars | truncated 15000 | Yes |
| generate_llm_review 失败 | LLMClient raises | "" | Yes |
| generate_pr_body 无 llm_config | llm_config=None | 无 Code Review 段 | Yes |
| handle_result 零产出 | git diff 空 | status=no_output | Yes |
| handle_result 部分成功 | run.status=FAILED + diff | status=partial | No |
| handle_result 全部成功 | run.status=SUCCEEDED + diff | status=success | No |
| handle_result push 失败 | push returns False | status=push_failed | Yes |
| handle_result 无 work_dir | dag_result={} | status=push_failed | Yes |

---

## Validation Commands

```bash
# Import check
python -c "from integrations.github.post_execution import handle_result, PostExecutionResult; from integrations.github.pr_body import generate_pr_body; print('OK')"

# New tests
python -m pytest tests/test_pr_body.py tests/test_post_execution.py -v --tb=short

# Full suite (no regression)
python -m pytest --tb=short -q

# Lint
flake8 integrations/github/pr_body.py integrations/github/post_execution.py cli/github.py --max-line-length=100
```

---

## Acceptance Criteria
- [ ] `pr_body.py` 生成包含 diff stat + LLM review 的 PR body
- [ ] `post_execution.py` 三态判断正确
- [ ] `CodeHost.push_changes()` 支持 cwd 参数
- [ ] `service.py` 在 dag_result 中注入 work_dir
- [ ] `cli/github.py` 调用 handle_result 并修复 run.error bug
- [ ] 所有新测试通过，现有测试无回归

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| dag_result 被 mark_* 覆盖丢失 work_dir | M | H | 在 mark_* 之后用 setdefault 追加 |
| LLMClient 构造方式与 ranker 同错 | L | H | 本 plan 使用 LLMConfig 对象，不传 kwargs |
| Agent 零产出常见 | M | L | 已有三态处理 |

---

*Generated: 2026-05-26*
*Source PRD: .claude/PRPs/prds/m5-3-execute-to-pr.prd.md*
