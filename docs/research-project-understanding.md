# 增强Weave对现有项目理解能力：研究报告

*生成日期: 2026-05-18 | 来源: 30+篇 | 置信度: 高*

## 执行摘要

经过对 Claude Code、Cursor、Aider、Meta等主流AI编码系统的深度研究，结合 Weave 当前代码库的分析，**增强项目理解需要三层架构：结构层（确定性提取）、语义层（LLM解释）、上下文注入层（动态路由）**。Weave 已有良好的基础（工具系统、记忆系统、依赖图），但在自动化项目分析、框架感知、上下文路由方面存在关键缺失。

---

## 1. 行业现状：三大流派的竞争

### 1.1 索引优先（Index-First）— Cursor、Augment Code、Sourcegraph Cody
- 打开项目时构建持久化向量嵌入和代码图
- 每次请求前运行语义搜索，注入最相关的代码块
- **优势**: 检索精准、延迟低
- **劣势**: 冷启动慢、需要基础设施、索引可能过时

### 1.2 代理搜索（Agentic Search）— Claude Code
- **无预构建索引**，用 Glob/Grep/Read 工具实时探索代码库
- Explore 子代理（Haiku模型）在独立上下文窗口中做代码库探索，返回压缩摘要
- **优势**: 零设置、无基础设施、天然任务特定
- **劣势**: 每次任务延迟高、探索成本大

### 1.3 混合图增强（Hybrid Graph-Augmented）— CodeCompass、Meta、AgenticCodebase
- 结合结构依赖图 + 按需检索
- **CodeCompass** (学术论文，2026): 用 Neo4j 图暴露 IMPORTS/INHERITS/INSTANTIATES 边，在隐藏依赖任务上达到 **99.4% 完成率**，比纯代理高 23.2 个百分点
- **Meta**: 50+ AI 代理群提取部落知识，59 个上下文文件覆盖 4100+ 文件
- **优势**: 最高架构覆盖率
- **劣势**: 需要图基础设施

**Weave 应该采用的路径**：混合图增强，因为我们已有 `analysis/dependency_graph.py` 的基础。

---

## 2. Weave 现状 vs 差距分析

| 能力 | 现状 | 缺失 |
|------|------|------|
| 项目配置加载 | `.weave/config.yaml` 基本配置 | 无自动检测和生成 |
| 代码库扫描/索引 | **完全缺失** | 无项目类型检测、无结构分析 |
| 依赖图 | Python AST 导入分析 | 仅限Python，无框架感知 |
| 记忆系统 | 完善的持久化记忆 | 无初始项目知识注入 |
| 代码探索工具 | read/write/edit/bash/glob/grep/git + MCP | 缺少结构化查询工具 |
| 编排器上下文 | 可接受 project_context 参数 | 依赖手动配置，无自动发现 |
| 代理上下文注入 | 运行时环境变量 | 无项目架构理解 |

---

## 3. 具体增强方案（按优先级排列）

### 3.1 第一优先级：项目初始化器（Project Onboarder）

**灵感来源**: Claude Code 的 `/init` 命令、Meta 的"指南针而非百科全书"原则

在 Weave 中添加 `project analyze` 命令，自动扫描目标项目并生成 `.weave/config.yaml` 和项目上下文文件：

```
目标项目 → 项目分析器 → 输出:
  ├── .weave/config.yaml          # 检测到的语言、框架、测试命令
  ├── .weave/context/             # 项目知识文件
  │   ├── architecture.md         # 架构概述 (~1000 tokens)
  │   ├── conventions.md          # 编码约定
  │   ├── dependencies.md         # 依赖关系图摘要
  │   └── modules/                # 每个模块的导航指南
  │       ├── auth.md
  │       ├── api.md
  │       └── ...
  └── .weave/index/               # 结构化索引
      └── graph.pkl               # 序列化的依赖图
```

**设计原则**（来自 Meta 的经验）：
- 每个上下文文件 25-35 行（~1000 tokens）
- 四个标准部分：快速命令、关键文件、非显而易见的模式、交叉引用
- 全部 59 个文件 < 0.1% 上下文窗口 → 可以总是加载

**关键实现点**：
- 用 LLM 分析项目结构（类似 Meta 的 explorer + analyst 模式）
- 框架检测：检测 pyproject.toml/package.json/go.mod/Cargo.toml 等
- 入口点检测：main.py、app.py、manage.py、src/index.ts 等
- 测试框架检测：pytest、jest、go test 等

### 3.2 第二优先级：增强依赖图引擎

**灵感来源**: CodeCompass (学术论文)、AgenticCodebase、ops-codegraph-tool

Weave 已有 `analysis/dependency_graph.py` 做 Python AST 导入分析。需要增强为：

```python
# 当前: 仅 Python 导入
# 增强: 多语言、多边类型

边类型:
  - IMPORTS        # 导入关系
  - INHERITS       # 继承关系
  - CALLS          # 函数调用链
  - IMPLEMENTS     # 接口实现
  - DECORATES      # 装饰器关系（graphsift 的创新）
  - TESTS          # 测试覆盖关系
  - REFERENCES     # 引用关系
```

**关键参考** — CodeCompass 的基准测试结果：
- 语义可发现任务 (G1): 所有方法都OK
- 结构可发现任务 (G2): 图导航比纯代理高 15+ 百分点
- 隐藏依赖任务 (G3): 图导航达到 **99.4%**，纯代理仅 76.2%

**实现路径**：
1. 扩展 `analysis/dependency_graph.py` 支持多边类型
2. 添加 tree-sitter 解析器支持多语言（参考 AgenticCodebase 的方法）
3. 将图存储为可序列化格式，支持增量更新
4. 通过 MCP 或内部 API 暴露图查询

### 3.3 第三优先级：项目上下文路由系统

**灵感来源**: Codified Context 论文、Meta 的路由层

当 Weave 接到"修复认证bug"或"添加OAuth2支持"的任务时，需要知道这涉及哪些文件：

```
用户任务 → 上下文路由器 → 加载相关上下文
                ↓
    ┌─────────────────────────────┐
    │  1. 关键词匹配              │
    │  2. 图遍历（从匹配节点出发）  │
    │  3. BM25 + 图距离融合排序    │
    │  4. Token预算感知选择        │
    └─────────────────────────────┘
                ↓
    选择的项目上下文 → 注入编排器/代理
```

**graphsift 的创新**值得借鉴：
- BM25 关键词重叠 30% + 图距离衰减 70% 的融合排序
- Token 预算强制限制（不超出上下文窗口）
- 每个文件根据相关性得分选择输出模式：FULL / SIGNATURES / COMPRESSED
- 结果：**80-150x token 减少**，F1 ≈ 0.85

### 3.4 第四优先级：项目感知的代理工作流

**灵感来源**: SWE-Adept、SWE-Edit、Aider 的仓库地图

增强 Weave 的代理层，使其在已有项目上工作时：

```
阶段1: 项目理解（轻量探索代理）
  ├── 读取 .weave/context/ 导航文件
  ├── 查询依赖图定位相关代码
  └── 输出: 任务相关的文件列表 + 架构约束

阶段2: 问题定位（深度分析代理）
  ├── 在定位到的文件中搜索问题
  ├── 跟踪调用链理解数据流
  └── 输出: 需要修改的精确位置

阶段3: 实施修改（实施代理）
  ├── 在隔离的 worktree 中工作
  ├── 遵循项目约定（从上下文文件中加载）
  └── 输出: 代码变更

阶段4: 验证（评估代理）
  ├── 运行项目测试
  ├── 检查约定合规性
  └── 输出: 验证结果
```

**SWE-Edit 的关键洞察**：
- 分离 Viewer 和 Editor 子代理，减少上下文污染
- Viewer 提取任务相关的代码片段，不加载整个文件
- Editor 从自然语言计划执行编辑，解耦推理和格式敏感的代码生成
- 结果：**推理成本降低 17.9%**，解决率提高 2.1%

---

## 4. 实施路线图

### 阶段 1（2-3 周）— 基础项目分析
1. 实现 `project analyze` CLI 命令
2. 框架/语言自动检测
3. 生成 `.weave/config.yaml`
4. 基础架构文档生成

### 阶段 2（3-4 周）— 增强依赖图
1. 扩展边类型（CALLS、INHERITS、DECORATES、TESTS）
2. 添加 tree-sitter 多语言支持
3. 增量更新机制
4. 图查询 API

### 阶段 3（2-3 周）— 上下文路由
1. 实现 BM25 + 图距离融合排序
2. Token 预算感知的上下文选择
3. 项目上下文注入到编排器
4. 模块级上下文按需加载

### 阶段 4（2-3 周）— 工作流优化
1. 项目感知的代理提示工程
2. Viewer/Editor 代理分离
3. 检查点机制（参考 SWE-Adept）
4. 项目约定验证

---

## 关键要点

1. **先建结构层，后建语义层** — Riftmap 博客的核心洞察：确定性解析的结构图（便宜、自刷新）比 LLM 生成的上下文文件（昂贵、会过时）更耐用、更高杠杆
2. **图导航比检索更强** — CodeCompass 论文证明：对于隐藏依赖，图导航（99.4%）远优于 BM25 检索（78.2%）和纯代理搜索（76.2%）
3. **上下文文件要小** — Meta 的"指南针原则"：每个文件 ~1000 tokens，可总是加载而不占上下文
4. **代理必须被强制使用图工具** — CodeCompass 发现 58% 的测试即使有图工具也从不调用它，需要明确的提示工程
5. **Weave 已有的记忆系统和工具系统是良好基础** — 关键缺失是自动化项目分析和上下文路由

---

## 来源

1. [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works.md)
2. [CLAUDE.md and AGENTS.md: Configuration Layer](https://tianpan.co/blog/2026-02-25-claude-md-agents-md-ai-coding-agent-instruction-files)
3. [Codebase Intelligence: AI Agents Navigate Large Repos 2026](https://zylos.ai/research/2026-04-19-codebase-intelligence-repository-understanding-ai-agents)
4. [CodeCompass: Navigation Paradox in Agentic Code Intelligence](https://arxiv.org/html/2602.20048)
5. [How Meta Used AI to Map Tribal Knowledge](https://engineering.fb.com/2026/04/06/developer-tools/how-meta-used-ai-to-map-tribal-knowledge-in-large-scale-data-pipelines/)
6. [Meta needed 50+ AI agents — Riftmap](https://riftmap.dev/blog/meta-tribal-knowledge-engine-build-the-graph-first/)
7. [Codified Context Infrastructure](https://arxiv.org/html/2602.20478v1)
8. [Beyond CLAUDE.md: Pre-Extracted Code Metadata](https://medium.com/@olegkomissarov/beyond-claude-md-how-pre-extracted-code-metadata-changes-ai-assisted-development-b8940272715a)
9. [mcp-codebase-index](https://github.com/MikeRecognex/mcp-codebase-index)
10. [AgenticCodebase](https://github.com/agentralabs/agentic-codebase)
11. [Codebase-Memory](https://arxiv.org/html/2603.27277)
12. [graphsift](https://pypi.org/project/graphsift/1.5.0/)
13. [SWE-Adept](https://arxiv.org/pdf/2603.01327v1)
14. [SWE-Edit](https://arxiv.org/html/2604.26102)
15. [How to Use Claude Code on Existing Large Codebase](https://www.lowcode.agency/blog/claude-code-existing-codebase)
16. [Agentic Context Framework](https://github.com/ichbinsoftware/agentic-context-framework)
17. [Search and Indexing Strategies](https://developertoolkit.ai/en/shared-workflows/context-management/codebase-indexing/)
18. [Claude Code Guide 2026: Context Engineering](https://www.generative.inc/the-complete-claude-code-guide-2026-planning-context-engineering-and-high-leverage-development)
19. [The Agent Layer: How AI Coding Tools Work](https://codemyspec.com/blog/the-agent-layer)
20. [How AI Coding Agents Work](https://www.abstractalgorithms.dev/how-ai-coding-agents-work)
21. [Learning Claude Code — Multi-Agent Workflows](https://aayushmnit.com/posts/2026-01-24_ClaudeCode/ClaudeCode.html)
22. [ops-codegraph-tool](https://github.com/optave/ops-codegraph-tool)
23. [stakwork/stakgraph](https://github.com/stakwork/stakgraph)
24. [LocAgent (ACL 2025)](https://aclanthology.org/2025.acl-long.426/)
25. [How to Give AI Coding Agents Better Codebase Context](https://dev.to/corestory/how-to-give-ai-coding-agents-better-codebase-context-2ac3)
26. [The AI-Native Code Intelligence Stack](https://dev.to/corestory/the-ai-native-code-intelligence-stack-where-the-wiki-ends-and-the-graph-begins-2jok)

## 方法论

搜索了 40+ 条查询，分析 30+ 来源。子问题覆盖：项目上下文构建技术、依赖图方法、多代理协作、上下文路由、框架检测。所有核心论点有 2+ 个独立来源支持。
