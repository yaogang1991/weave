# Harness Milestone Roadmap

---

**最后更新:** 2026-05-10
**当前版本:** M2

---

## 概览

| 里程碑 | 目标 | 状态 | 完成日期 |
|--------|------|------|----------|
| **M1** | 单用户无人值守任务执行系统 | ✅ 已完成 | 2026-05-09 |
| **M1.1** | 稳定化 — 审批票据化 + 统一 Guardrail 入口 | ✅ 已完成 | 2026-05-09 |
| **M2** | 单用户高可靠自治 — 健康检查 + 隔离后端 + Web 控制台 | ✅ 已完成 | 2026-05-10 |

---

## M1 — Personal Edition

**目标:** 单用户、CLI 驱动的多 Agent 软件开发 Harness，实现"扔任务进去，系统自动跑完"。

### Stage 1: Foundation (Tasks 01-03)
- **Task 01** ✅ `docs/m1_personal_spec.md` — 范围、状态机、接口、DoD
- **Task 02** ✅ `control_plane/models.py` — JobStatus, RunStatus, RetryPolicy, Job, Run 模型
- **Task 03** ✅ `control_plane/repository.py` — 原子写入持久化存储与状态转换

### Stage 2: Control Plane Core (Tasks 04-06)
- **Task 04** ✅ `main.py` CLI 命令 — submit/status/list/cancel
- **Task 05** ✅ `control_plane/service.py` — 提取执行服务供复用
- **Task 06** ✅ `control_plane/worker.py` — Worker 循环 + Lease 机制

### Stage 3: Reliability & Replan (Tasks 07-08)
- **Task 07** ✅ 超时/重试/死信队列
- **Task 08** ✅ Replan 闭环 — 保留成功节点，max_replans 限制

### Stage 4: Guardrails & Monitoring (Tasks 09-11)
- **Task 09** ✅ 个人模式 Guardrails — 风险等级、确认流程、白名单
- **Task 10** ✅ 指标聚合 — 成功率、延迟 P95、重试率、失败 TopN
- **Task 11** ✅ 告警 — 连续失败、时长阈值、死信 Webhook

### Stage 5: Recovery & Polish (Tasks 12-14)
- **Task 12** ✅ 重启恢复 — 孤儿 Job 清理、Lease 过期处理
- **Task 13** ✅ 核心路径测试套件
- **Task 14** ✅ 文档更新

---

## M1.1 — Stabilization

**目标:** 把 M1 的 85%~90% 完成度提升到 100%，聚焦审批票据化、统一 Guardrail 入口、文档真值化。

### Stage 1: Task 1 — 审批票据持久化
- ✅ `control_plane/approval.py` — ApprovalTicket 模型 + ApprovalRepository
- ✅ TicketStatus 枚举: pending/approved/rejected/expired
- ✅ Worker 重启后识别并处理 pending ticket

### Stage 2: Task 2 + 3 — 统一入口 + 非交互模式
- ✅ `guardrails/policy.py` — 统一 `guarded_execute()` 三态返回: allowed/blocked/pending_approval
- ✅ 非交互模式 — `--non-interactive` / `HARNESS_NON_INTERACTIVE`
- ✅ 高风险操作自动创建 pending ticket

### Stage 3: Task 4 + 5 — CLI 工具链 + 运行态恢复
- ✅ `tickets`/`approve`/`reject` CLI 命令
- ✅ Worker 启动恢复扫描 pending ticket
- ✅ `resume_run()` 审批中断后自动恢复

### Stage 4: Task 6 + 7 — 指标升级 + 文档真值化
- ✅ 审批维度指标: approval_pending_count, approval_avg_wait_sec
- ✅ 告警规则: pending_approvals_over_threshold
- ✅ 文档 Implemented/Partial/Planned 标签化
- ✅ 合同测试: CLI 命令存在、状态枚举完整、错误码存在

---

## M2 — Single-User High-Reliability Autonomy

**目标:** 从"能自动跑"升级为"能长期稳定跑 + 快速恢复 + 最小化可视控制面"。

### M2.0 (P0) — Health Check + Watchdog + Fail-Fast
**目标:** Watchdog 检测 hang agent，阈值内（<30s）自动进入失败链。

- ✅ `core/models.py` — NodeHealth 模型 + 心跳协议
  - NodeHealth 枚举: HEALTHY / MISSED / UNHEALTHY / DEAD
  - 心跳事件: node.heartbeat, node.heartbeat_missed, node.unhealthy_killed
  - DAGNode.last_heartbeat_at, DAGNode.health_status
- ✅ `core/dag_engine.py` — Watchdog 协程
  - 监控运行节点的 last_heartbeat_at
  - 阈值: heartbeat_interval(5s) × miss_threshold(3) ≈ 15s
  - 超时标记 unhealthy，触发失败处理器
- ✅ `monitoring/alerts.py` — 健康告警规则

### M2.1 (P0) — Worktree Backend
**目标:** 每个 Job/Run 在隔离的 git worktree 中执行，不污染主仓库。

- ✅ `backend/worktree.py` — Git worktree 隔离后端
  - create_worktree(job_id, run_id) → worktree_path
  - cleanup_worktree (成功时) / preserve_worktree (失败时调试)
- ✅ `backend/lifecycle.py` — BackendManager
  - 生命周期管理: create/use/cleanup/preserve
  - 配置驱动: 默认后端 + 按 Job 覆盖
  - 风险等级 → 后端映射 (HIGH → worktree)
  - 自动降级 (worktree → local)

### M2.2 (P1) — Execution Backend Abstraction
**目标:** 通过配置切换后端，业务逻辑无需改动。

- ✅ `backend/base.py` — ExecutionBackend 抽象接口
  - setup() / get_work_dir() / cleanup() / preserve() / is_available()
- ✅ `backend/local.py` — 本地直接执行后端
- ✅ `backend/docker_stub.py` — Docker 后端 stub（M3 实现）
- ✅ 配置路由: HARNESS_DEFAULT_BACKEND=local|worktree

### M2.3 (P1) — Web Console MVP
**目标:** 3 页面 + 5 操作，核心操作无需 CLI。

- ✅ `visualizer/server.py` — FastAPI Web 服务
  - WebSocket 实时 DAG 监控
  - Job/Run 管理 API (重试/取消)
  - 审批 Ticket 管理 API
  - 指标与告警端点
- ✅ `visualizer/static/index.html` — 主仪表盘
  - DAG 可视化 (vis-network)
  - 实时事件流
  - 节点详情面板
- ✅ `visualizer/static/console.html` — 管理控制台
  - Jobs/Runs/Tickets/Alerts 四页
  - 审批操作 (approve/reject)
  - 状态指示器

---

## 完整文件清单

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
│   ├── roadmap.md                 # 本文档
│   └── m1_personal_spec.md        # M1 工程规格
├── tests/                         # 测试套件
├── main.py                        # CLI 入口
├── requirements.txt
├── pyproject.toml
├── README.md                      # 面向用户的说明（中文）
├── ARCHITECTURE.md                # 架构设计（中文）
├── AGENTS.md                      # Agent 开发指南
└── CLAUDE.md                      # Claude Code 指引
```
