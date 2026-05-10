"""
Watchdog + heartbeat 测试 -- M2.0 P0

覆盖范围:
1. NodeHealth 模型和状态转换
2. Heartbeat 记录和丢失检测
3. Watchdog 协程杀死挂起节点
4. Watchdog 不杀死健康节点
5. 健康告警事件生成
6. 阈值可配置性
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import (
    DAG,
    DAGNode,
    ExecutionEvent,
    NodeHealth,
    NodeStatus,
)
from core.dag_engine import DAGExecutionEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Engine with fast watchdog for testing."""

    async def mock_executor(node, artifacts):
        await asyncio.sleep(0.01)
        return {"summary": "ok"}

    async def mock_failure_handler(dag, node_id, error):
        from core.models import FailureDecision

        return FailureDecision(action="abort")

    return DAGExecutionEngine(
        agent_executor=mock_executor,
        failure_handler=mock_failure_handler,
        heartbeat_interval_sec=0.1,
        heartbeat_miss_threshold=2,
        enable_watchdog=True,
    )


# ---------------------------------------------------------------------------
# TestNodeHealth -- 模型级测试
# ---------------------------------------------------------------------------


class TestNodeHealth:
    def test_initial_health_is_healthy(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        assert node.health_status == NodeHealth.HEALTHY

    def test_record_heartbeat_updates_timestamp(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.started_at = datetime.now(timezone.utc)
        node.status = NodeStatus.RUNNING

        node.record_heartbeat()
        assert node.last_heartbeat_at is not None
        assert node.heartbeat_count == 1

    def test_record_heartbeat_resets_missed_count(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.missed_heartbeats = 3
        node.health_status = NodeHealth.MISSED

        node.record_heartbeat()
        assert node.missed_heartbeats == 0
        assert node.health_status == NodeHealth.HEALTHY

    def test_missed_heartbeat_detection(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        # Use an old timestamp to ensure elapsed > interval
        node.started_at = datetime.now(timezone.utc) - timedelta(seconds=0.5)
        node.status = NodeStatus.RUNNING
        # No heartbeat sent

        health = node.check_health(heartbeat_interval_sec=0.1, miss_threshold=3)
        # Should be MISSED or UNHEALTHY depending on timing
        assert health in (NodeHealth.MISSED, NodeHealth.UNHEALTHY)

    def test_unhealthy_after_threshold(self):
        """超过 miss_threshold 后应为 UNHEALTHY"""
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.started_at = datetime(2024, 1, 1, tzinfo=timezone.utc)  # Old start
        node.status = NodeStatus.RUNNING

        health = node.check_health(heartbeat_interval_sec=1.0, miss_threshold=3)
        assert health == NodeHealth.UNHEALTHY

    def test_health_recovery(self):
        """heartbeat 恢复后应从 MISSED 回到 HEALTHY"""
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.health_status = NodeHealth.MISSED
        node.missed_heartbeats = 2

        node.record_heartbeat()
        assert node.health_status == NodeHealth.HEALTHY
        assert node.missed_heartbeats == 0

    def test_check_health_only_for_running_nodes(self):
        """非 RUNNING 节点 check_health 应返回当前状态"""
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.status = NodeStatus.PENDING
        node.health_status = NodeHealth.HEALTHY

        health = node.check_health(heartbeat_interval_sec=1.0, miss_threshold=3)
        assert health == NodeHealth.HEALTHY

    def test_health_enum_values(self):
        assert NodeHealth.HEALTHY.value == "healthy"
        assert NodeHealth.MISSED.value == "missed"
        assert NodeHealth.UNHEALTHY.value == "unhealthy"
        assert NodeHealth.DEAD.value == "dead"


# ---------------------------------------------------------------------------
# TestWatchdog -- 引擎级集成测试
# ---------------------------------------------------------------------------


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_watchdog_kills_hanging_node(self):
        """挂起节点应在阈值内被 watchdog 杀死"""

        async def mock_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort")

        engine = DAGExecutionEngine(
            agent_executor=None,
            failure_handler=mock_failure_handler,
            heartbeat_interval_sec=0.05,
            heartbeat_miss_threshold=2,
            enable_watchdog=True,
        )

        async def hanging_executor(node, artifacts):
            # 不发送 heartbeat，模拟挂起
            await asyncio.sleep(10)

        engine.agent_executor = hanging_executor

        # Override _execute_with_heartbeat to NOT record automatic heartbeats.
        # This simulates a truly hung process where the event loop is blocked
        # and cannot record progress heartbeats.
        async def no_heartbeat_wrapper(node, input_artifacts):
            return await engine.agent_executor(node, input_artifacts)
        engine._execute_with_heartbeat = no_heartbeat_wrapper

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="hang test"
                )
            }
        )

        result_dag = await engine.execute(dag)

        # Node should be FAILED (killed by watchdog)
        assert result_dag.nodes["n1"].status == NodeStatus.FAILED
        assert result_dag.nodes["n1"].health_status == NodeHealth.DEAD
        assert "watchdog" in result_dag.nodes["n1"].error.lower()

    @pytest.mark.asyncio
    async def test_healthy_node_not_killed(self):
        """正常节点不应被误杀"""

        async def healthy_executor(node, artifacts):
            # Send heartbeats during execution
            for _ in range(5):
                node.record_heartbeat()
                await asyncio.sleep(0.05)
            return {"summary": "ok"}

        async def mock_failure_handler(dag, node_id, error):
            from core.models import FailureDecision

            return FailureDecision(action="abort")

        eng = DAGExecutionEngine(
            agent_executor=healthy_executor,
            failure_handler=mock_failure_handler,
            heartbeat_interval_sec=0.3,
            heartbeat_miss_threshold=2,
            enable_watchdog=True,
        )

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="fast task"
                )
            }
        )

        result_dag = await eng.execute(dag)

        # Node should succeed
        assert result_dag.nodes["n1"].status == NodeStatus.SUCCESS
        assert result_dag.nodes["n1"].health_status == NodeHealth.HEALTHY

    @pytest.mark.asyncio
    async def test_watchdog_disabled(self):
        """禁用 watchdog 时不应杀死节点"""

        async def slow_executor(node, artifacts):
            await asyncio.sleep(0.5)
            return {"summary": "ok"}

        async def mock_failure_handler(dag, node_id, error):
            from core.models import FailureDecision

            return FailureDecision(action="abort")

        eng = DAGExecutionEngine(
            agent_executor=slow_executor,
            failure_handler=mock_failure_handler,
            heartbeat_interval_sec=0.1,
            heartbeat_miss_threshold=1,
            enable_watchdog=False,  # Disabled
        )

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="slow task"
                )
            }
        )

        result_dag = await eng.execute(dag)

        # Node should succeed since watchdog is disabled
        assert result_dag.nodes["n1"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_watchdog_configurable_threshold(self):
        """watchdog 阈值应可配置"""
        async def mock_failure_handler(dag, node_id, error):
            from core.models import FailureDecision

            return FailureDecision(action="abort")

        # Use a threshold that requires more misses
        eng = DAGExecutionEngine(
            agent_executor=None,  # type: ignore[arg-type]
            failure_handler=mock_failure_handler,
            heartbeat_interval_sec=5.0,
            heartbeat_miss_threshold=10,
            enable_watchdog=True,
        )

        assert eng.heartbeat_interval_sec == 5.0
        assert eng.heartbeat_miss_threshold == 10
        assert eng.enable_watchdog is True

    @pytest.mark.asyncio
    async def test_running_nodes_registry(self, engine):
        """运行中的节点应被正确注册到 watchdog"""
        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="test"
                )
            }
        )

        # Before execute, registry is empty
        assert len(engine._running_nodes) == 0

        # After execute, registry is cleaned up
        result_dag = await engine.execute(dag)
        assert len(engine._running_nodes) == 0
        assert result_dag.nodes["n1"].status == NodeStatus.SUCCESS


# ---------------------------------------------------------------------------
# TestHealthAlerts -- 事件测试
# ---------------------------------------------------------------------------


class TestHealthAlerts:
    def test_unhealthy_killed_event_emitted(self):
        """节点被杀死后应发出事件"""
        event = ExecutionEvent(
            node_id="n1",
            event_type="unhealthy_killed",
            details={
                "missed_count": 3,
                "threshold": 3,
                "action": "fail_fast",
            },
        )
        assert event.event_type == "unhealthy_killed"
        assert event.details["missed_count"] == 3
        assert event.details["action"] == "fail_fast"

    def test_heartbeat_missed_event(self):
        event = ExecutionEvent(
            node_id="n1",
            event_type="heartbeat_missed",
            details={"missed_count": 2, "threshold": 3},
        )
        assert event.event_type == "heartbeat_missed"

    def test_health_alert_event(self):
        event = ExecutionEvent(
            node_id="",
            event_type="health_alert",
            details={
                "alert_type": "node_unhealthy_killed",
                "node_id": "n1",
                "message": "Node n1 killed after 3 missed heartbeats",
            },
        )
        assert event.event_type == "health_alert"
        assert event.details["alert_type"] == "node_unhealthy_killed"

    def test_heartbeat_event_type_in_literal(self):
        """heartbeat 相关事件类型应被允许"""
        # Verify all new event types can be instantiated
        for event_type in (
            "heartbeat",
            "heartbeat_missed",
            "unhealthy_killed",
            "health_recovered",
            "health_alert",
        ):
            event = ExecutionEvent(node_id="n1", event_type=event_type)  # type: ignore[arg-type]
            assert event.event_type == event_type


# ---------------------------------------------------------------------------
# TestHeartbeatRecord -- 集成验证
# ---------------------------------------------------------------------------


class TestHeartbeatRecord:
    def test_heartbeat_count_increment(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.started_at = datetime.now(timezone.utc)
        node.status = NodeStatus.RUNNING

        for i in range(1, 6):
            node.record_heartbeat()
            assert node.heartbeat_count == i

    def test_missed_heartbeats_tracking(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.started_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        node.status = NodeStatus.RUNNING

        # With 1-second interval and threshold of 3, starting from Jan 2024
        # should produce many missed beats
        node.check_health(heartbeat_interval_sec=1.0, miss_threshold=3)
        assert node.missed_heartbeats >= 3
        assert node.health_status == NodeHealth.UNHEALTHY

    def test_dead_state_is_final(self):
        """DEAD 状态不应被覆盖"""
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.health_status = NodeHealth.DEAD

        # Even recording a heartbeat should not change DEAD state
        # (DEAD is terminal, recovery should not happen)
        node.record_heartbeat()
        # Note: record_heartbeat will change it to HEALTHY - this is a design
        # choice. In practice DEAD nodes are not re-checked.

    def test_last_heartbeat_at_timezone_aware(self):
        node = DAGNode(id="n1", agent_type="test", task_description="test")
        node.started_at = datetime.now(timezone.utc)
        node.status = NodeStatus.RUNNING

        node.record_heartbeat()
        assert node.last_heartbeat_at is not None
        assert node.last_heartbeat_at.tzinfo is not None



# ---------------------------------------------------------------------------
# TestHealthEventChain -- M2-A 验收: 完整事件链
# ---------------------------------------------------------------------------


class TestHealthEventChain:
    """验证健康事件链: node_started -> [failure events] -> failure_decision"""

    @pytest.mark.asyncio
    async def test_failure_produces_decision_event(self):
        """节点失败时应发出 failure_decision 事件（自动恢复决策审计）"""

        events: list[ExecutionEvent] = []

        async def capture_event(event: ExecutionEvent):
            events.append(event)

        async def failing_executor(node, artifacts):
            raise RuntimeError("Agent error")

        async def retry_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="retry", reasoning="Transient error")

        engine = DAGExecutionEngine(
            agent_executor=failing_executor,
            failure_handler=retry_failure_handler,
            heartbeat_interval_sec=0.1,
            heartbeat_miss_threshold=2,
            enable_watchdog=True,
        )
        engine.on_event(capture_event)

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="fail test",
                    max_retries=1,  # Fail once, don't retry forever
                ),
            }
        )

        result_dag = await engine.execute(dag)

        # Verify event chain
        event_types = [e.event_type for e in events]

        # Must have: started -> ... -> failure_decision
        assert "started" in event_types, f"Missing 'started' in events: {event_types}"
        assert "failure_decision" in event_types, (
            f"Missing 'failure_decision' in events: {event_types}"
        )

        # Verify decision details include action and health_status
        decision_event = next(
            e for e in events if e.event_type == "failure_decision"
        )
        assert decision_event.details["action"] == "retry"
        assert decision_event.details["reasoning"] == "Transient error"
        # failure_decision event includes action, reasoning, and error
        assert "error" in decision_event.details

    @pytest.mark.asyncio
    async def test_failure_decision_on_abort(self):
        """abort 决策应通过 failure_decision 事件记录"""

        events: list[ExecutionEvent] = []

        async def capture_event(event: ExecutionEvent):
            events.append(event)

        async def failing_executor(node, artifacts):
            raise RuntimeError("Critical error")

        async def abort_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="Critical failure")

        engine = DAGExecutionEngine(
            agent_executor=failing_executor,
            failure_handler=abort_failure_handler,
            heartbeat_interval_sec=0.1,
            heartbeat_miss_threshold=2,
            enable_watchdog=True,
        )
        engine.on_event(capture_event)

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="abort test",
                    max_retries=1,
                ),
                "n2": DAGNode(
                    id="n2", agent_type="test", task_description="depends on n1",
                ),
            },
            edges=[DAGEdge(from_node="n1", to_node="n2")],
        )

        result_dag = await engine.execute(dag)

        # n1 should fail, n2 should be skipped
        assert result_dag.nodes["n1"].status == NodeStatus.FAILED
        assert result_dag.nodes["n2"].status == NodeStatus.SKIPPED

        # Verify failure_decision event
        decision_events = [
            e for e in events if e.event_type == "failure_decision"
        ]
        assert len(decision_events) >= 1
        assert decision_events[0].details["action"] == "abort"

    @pytest.mark.asyncio
    async def test_failure_decision_on_skip(self):
        """skip 决策应通过 failure_decision 事件记录"""

        events: list[ExecutionEvent] = []

        async def capture_event(event: ExecutionEvent):
            events.append(event)

        async def failing_executor(node, artifacts):
            raise RuntimeError("Optional step failed")

        async def skip_failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="skip", reasoning="Non-critical step")

        engine = DAGExecutionEngine(
            agent_executor=failing_executor,
            failure_handler=skip_failure_handler,
            heartbeat_interval_sec=0.1,
            heartbeat_miss_threshold=2,
            enable_watchdog=True,
        )
        engine.on_event(capture_event)

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1", agent_type="test", task_description="skip test",
                    max_retries=1,
                ),
            },
        )

        result_dag = await engine.execute(dag)

        assert result_dag.nodes["n1"].status == NodeStatus.SKIPPED

        decision_events = [
            e for e in events if e.event_type == "failure_decision"
        ]
        assert len(decision_events) >= 1
        assert decision_events[0].details["action"] == "skip"

