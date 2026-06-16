# 第三方 LLM 兼容性矩阵

> **最后更新:** 2026-06-16

Weave 的 `claude_code` 后端通过 Claude CLI（`claude -p`）执行节点，`builtin` 后端直接调用 LLM API（无工具循环）。两者对第三方 LLM 的兼容性差异较大，尤其在 Anthropic 直连受限、需要走第三方 Anthropic 兼容代理（如 bigmodel/GLM、Kimi）的部署场景下。

## 兼容性矩阵

| 提供商（接入方式） | `claude_code` 后端 | `builtin` 后端 |
|---|---|---|
| Anthropic 原生 | ✅ 完全支持 | ✅ 完全支持 |
| bigmodel GLM-5.x（anthropic 兼容代理） | ❌ 工具调用不可靠、思维 token 洪水 | ⚠️ 可用，但无工具循环（仅轻量 LLM 调用） |
| Kimi（anthropic 兼容代理） | ❓ 未测试 | ❓ 未测试 |

## 已知问题：GLM-5.x 经 `claude_code` 后端

**现象**：同一模型、同一权限模式、同一"必须使用 Write 工具"的指令——
- 直接调用 `claude -p`（不经过 weave）：**可靠地**调用 Write 工具创建文件；
- 经 weave 的 `claude_code` 后端调用：每个 generator 节点都以 `zero_output_artifacts` 失败（模型把代码作为**纯文本**输出，而非 `tool_use` 调用）。

**失败模式**：
1. **工具调用降级为文本输出**：GLM-5.2 经 weave 调用时不发 `tool_use`，而是输出 fenced 代码块或纯 markdown 正文 → 产物发现失败。
2. **思维 token 洪水**：长时间持续发送 `{"type":"system","subtype":"thinking_tokens"}` 事件而无任何 `assistant`/`tool_use` 产出 → wall-clock 预算被耗尽后才超时。

**根因（未完全定位）**：差异不在模型本身，而在 weave 组装的复合 prompt（`## Task` 标记、`--session-id`、额外系统上下文等）。详见 #1137。

## #1136 修复：非交互模式权限提升

`run` / `execute` / `submit` / `worker` 的 `--non-interactive` 标志（以及 `WEAVE_NON_INTERACTIVE` 环境变量）现可将 `claude_code` 后端的 Claude CLI 权限模式从 `default` 提升为 `bypassPermissions`（#1125），避免非交互场景下文件写入被静默拒绝。

- **同步命令**（`run` / `execute`）：标志经 `add_execution_args` 注册，直接传入 `ClaudeCodeRuntimeConfig.from_core_config`。
- **异步命令**（`submit`）：意图写入 `job.metadata["non_interactive"]`，worker 执行该 job 时合并覆盖工厂构造期的默认值（`ExecutionFactory.create_execution_engine` 的 `non_interactive_override`）——即使 worker 未带 `--non-interactive`，该 job 仍按非交互执行。
- **边界**：权限提升只解决“写入被拒绝”，不解决第三方 LLM“工具不被调用”（那是 #1137）。

## 已实施的缓解（#1137）

| Track | 措施 | 状态 |
|---|---|---|
| 1. Generator prompt 显式要求工具调用 | 在 `_build_prompt` 中要求必须用 Write/Edit，禁止纯文本输出代码 | 主观增益，部分模型有效 |
| 2. 思维 token 洪水快速失败 | `StreamParser` 检测连续 `thinking_tokens` 事件，达到阈值抛 `ThinkingFloodError` 快速失败（不消耗重试预算） | #1146 |
| 3a. 无 fence 文档恢复 | 提取器在无 fenced 块但文本呈 markdown 文档结构时，恢复为 `README.md` 产物 | #1147 |
| 3b. 提取失败诊断日志 | 在各早返回点输出 INFO 级原因（文本过短 / 非 generator / 块全被过滤） | 本 PR |

## 部署建议

- **Anthropic 原生用户**：两个后端均可，`claude_code` 推荐（有完整工具循环）。
- **GLM/Kimi 等第三方代理用户**：
  - 短期：planner/evaluator 节点用 `builtin` 后端（轻量调用不受影响）；generator 节点仍可能失败，需关注 #1137 后续进展。
  - 关注上述缓解 PR 是否合并。
- **诊断 generator 节点 `zero_output_artifacts`**：查看 `text artifact extraction skipped: ...` INFO 日志（#1137 track 3b）可快速定位是文本过短、非 generator、还是块全被过滤。

## 环境

- weave commit `860812e` + #1136 修复 + track 1 本地补丁
- claude CLI 2.1.169
- GLM-5.2 经 `open.bigmodel.cn/api/anthropic`

相关 issue：#1135、#1136、#1137。
