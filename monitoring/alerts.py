"""
最小可用告警系统。

支持七类告警规则：
1. 连续失败 >= N
2. 任务时长 > 阈值
3. dead_letter 新增
4. 审批堆积 (pending_approvals)
5. 审批超时激增 (approval_timeout_spike)
6. 节点被 watchdog 杀死 (node_unhealthy_killed)
7. 心跳丢失激增 (heartbeat_miss_spike)

支持 webhook 通知，webhook 不可用时降级控制台告警。
告警失败不中断主流程。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from control_plane.approval import ApprovalRepository, TicketStatus
from control_plane.models import JobStatus
from control_plane.repository import JobRepository
from monitoring.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AlertRule:
    """告警规则定义。"""

    name: str
    rule_type: str  # "consecutive_failures", "duration_threshold", "dead_letter",
    # "pending_approvals", "approval_timeout_spike",
    # "node_unhealthy_killed", "heartbeat_miss_spike"
    threshold: float  # 阈值
    enabled: bool = True
    webhook_url: str = ""  # 可选的 webhook URL（覆盖全局）


@dataclass
class AlertEvent:
    """告警事件。"""

    rule_name: str
    severity: str  # "warning", "critical"
    message: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Alert manager
# ---------------------------------------------------------------------------


class AlertManager:
    """告警管理器。

    特性：
    - 支持多规则配置
    - webhook 失败降级控制台
    - 告警不阻塞主流程
    - 内置去重（同一规则短时间内不重复告警）
    """

    def __init__(
        self,
        job_repository: JobRepository,
        approval_repository: ApprovalRepository | None = None,
        webhook_url: str = "",
        cooldown_sec: int = 300,
    ) -> None:
        self.job_repository = job_repository
        self.approval_repository = approval_repository
        self.metrics = MetricsCollector(job_repository, approval_repository)
        self.webhook_url = webhook_url
        self.cooldown_sec = cooldown_sec
        self.rules: list[AlertRule] = []
        self._last_alert_time: dict[str, datetime] = {}
        self._alert_handlers: list[Callable[[AlertEvent], None]] = []

    # ------------------------------------------------------------------
    # Rule management
    # ------------------------------------------------------------------

    def add_rule(self, rule: AlertRule) -> None:
        self.rules.append(rule)

    def on_alert(self, handler: Callable[[AlertEvent], None]) -> None:
        """注册告警处理器。"""
        self._alert_handlers.append(handler)

    # ------------------------------------------------------------------
    # Checking
    # ------------------------------------------------------------------

    def check_all(self) -> list[AlertEvent]:
        """检查所有规则，返回触发的告警。"""
        alerts: list[AlertEvent] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            alert = self._check_rule(rule)
            if alert:
                alerts.append(alert)
        return alerts

    def _check_rule(self, rule: AlertRule) -> AlertEvent | None:
        """检查单个规则。"""
        if rule.rule_type == "consecutive_failures":
            return self._check_consecutive_failures(rule)
        if rule.rule_type == "duration_threshold":
            return self._check_duration_threshold(rule)
        if rule.rule_type == "dead_letter":
            return self._check_dead_letter(rule)
        if rule.rule_type == "pending_approvals":
            return self._check_pending_approvals(rule)
        if rule.rule_type == "approval_timeout_spike":
            return self._check_approval_timeout_spike(rule)
        if rule.rule_type == "node_unhealthy_killed":
            return self._check_node_unhealthy_killed(rule)
        if rule.rule_type == "heartbeat_miss_spike":
            return self._check_heartbeat_miss_spike(rule)
        return None

    def _check_consecutive_failures(
        self, rule: AlertRule
    ) -> AlertEvent | None:
        """检查连续失败数。"""
        jobs = self.job_repository.list_jobs()
        # 获取最近的 job（按时间排序）
        recent_jobs = sorted(
            jobs, key=lambda j: j.created_at, reverse=True
        )

        consecutive = 0
        for job in recent_jobs[:20]:  # 只看最近 20 个
            if job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
                consecutive += 1
            elif job.status == JobStatus.SUCCEEDED:
                break  # 连续中断

        if consecutive >= rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="critical"
                if consecutive >= rule.threshold * 2
                else "warning",
                message=f"连续 {consecutive} 个任务失败（阈值: {rule.threshold}）",
                details={
                    "consecutive_failures": consecutive,
                    "threshold": rule.threshold,
                },
            )
        return None

    def _check_duration_threshold(
        self, rule: AlertRule
    ) -> AlertEvent | None:
        """检查任务时长是否超过阈值。"""
        metrics_data = self.metrics.collect()
        p95 = metrics_data.get("duration", {}).get("p95_sec", 0)

        if p95 > rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="warning",
                message=f"任务执行时长 P95 = {p95}s，超过阈值 {rule.threshold}s",
                details={"p95_sec": p95, "threshold_sec": rule.threshold},
            )
        return None

    def _check_dead_letter(self, rule: AlertRule) -> AlertEvent | None:
        """检查是否有新增的 dead_letter。"""
        jobs = self.job_repository.list_jobs(status=JobStatus.DEAD_LETTER)
        count = len(jobs)

        if count >= rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="critical",
                message=f"死信队列有 {count} 个任务（阈值: {rule.threshold}）",
                details={"dead_letter_count": count, "threshold": rule.threshold},
            )
        return None

    def _check_pending_approvals(
        self, rule: AlertRule
    ) -> AlertEvent | None:
        """检查待审批堆积。"""
        if not self.approval_repository:
            return None

        stats = self.approval_repository.get_stats()
        pending_count = stats.get(TicketStatus.PENDING.value, 0)

        if pending_count >= rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="critical"
                if pending_count >= rule.threshold * 2
                else "warning",
                message=f"有 {pending_count} 个审批待处理（阈值: {rule.threshold}）",
                details={
                    "pending_count": pending_count,
                    "threshold": rule.threshold,
                },
            )
        return None

    def _check_approval_timeout_spike(
        self, rule: AlertRule
    ) -> AlertEvent | None:
        """检查审批超时激增。"""
        if not self.approval_repository:
            return None

        # 获取最近 1 小时过期的 ticket 数量
        recent_expired = [
            t
            for t in self.approval_repository.list_tickets(
                status=TicketStatus.EXPIRED
            )
            if t.decided_at
            and (
                datetime.now(timezone.utc) - t.decided_at
            ).total_seconds()
            < 3600
        ]

        if len(recent_expired) >= rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="critical",
                message=f"最近 1 小时有 {len(recent_expired)} 个审批超时过期（阈值: {rule.threshold}）",
                details={
                    "expired_count": len(recent_expired),
                    "threshold": rule.threshold,
                },
            )
        return None

    def _check_node_unhealthy_killed(
        self, rule: AlertRule
    ) -> AlertEvent | None:
        """检查是否有节点被 watchdog 杀死。"""
        jobs = self.job_repository.list_jobs()
        killed_count = 0
        for job in jobs:
            if job.error_category == "watchdog":
                killed_count += 1

        if killed_count >= rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="critical",
                message=f"{killed_count} 个节点被 watchdog 杀死（阈值: {rule.threshold}）",
                details={"killed_count": killed_count, "threshold": rule.threshold},
            )
        return None

    def _check_heartbeat_miss_spike(
        self, rule: AlertRule
    ) -> AlertEvent | None:
        """检查心跳丢失激增。

        检查因 watchdog 心跳超时被杀死的节点数量。
        与 _check_node_unhealthy_killed 的区别：
        - node_unhealthy_killed 检查所有 watchdog 杀死事件
        - heartbeat_miss_spike 专门检查心跳丢失导致的超时

        使用 MetricsCollector 的 failure 统计中 heartbeat 相关错误。
        """
        metrics_data = self.metrics.collect()
        failures = metrics_data.get("failures", {})
        top_errors = failures.get("top_errors", [])

        heartbeat_failures = 0
        for error_entry in top_errors:
            reason = error_entry.get("reason", "").lower()
            if "heartbeat" in reason or "watchdog" in reason:
                heartbeat_failures += error_entry.get("count", 0)

        if heartbeat_failures >= rule.threshold:
            return AlertEvent(
                rule_name=rule.name,
                severity="critical",
                message=(
                    f"心跳丢失激增：{heartbeat_failures} 次 "
                    f"（阈值: {rule.threshold}）"
                ),
                details={
                    "heartbeat_failure_count": heartbeat_failures,
                    "threshold": rule.threshold,
                },
            )
        return None

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_alert(self, alert: AlertEvent) -> bool:
        """发送告警。

        先尝试 webhook，失败则降级控制台。
        不阻塞主流程（异常被捕获）。
        """
        # 检查冷却期
        last_time = self._last_alert_time.get(alert.rule_name)
        if last_time:
            if datetime.now(timezone.utc) - last_time < timedelta(
                seconds=self.cooldown_sec
            ):
                return False  # 冷却中

        success = False

        # 尝试 webhook
        if self.webhook_url:
            try:
                self._send_webhook(alert)
                success = True
            except Exception as e:  # noqa: BLE001
                print(f"[Alert] Webhook failed: {e}, falling back to console")

        # 降级控制台
        try:
            self._send_console(alert)
            success = True
        except Exception:  # noqa: BLE001
            pass

        # 调用注册的处理器
        for handler in self._alert_handlers:
            try:
                handler(alert)
            except Exception:  # noqa: BLE001
                pass

        if success:
            self._last_alert_time[alert.rule_name] = datetime.now(
                timezone.utc
            )

        return success

    def _send_webhook(self, alert: AlertEvent) -> None:
        """发送 webhook 通知。"""
        payload = json.dumps(
            {
                "rule": alert.rule_name,
                "severity": alert.severity,
                "message": alert.message,
                "timestamp": alert.timestamp,
                "details": alert.details,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)

    def _send_console(self, alert: AlertEvent) -> None:
        """控制台告警。"""
        icon = "\U0001f534" if alert.severity == "critical" else "\U0001f7e1"
        print(
            f"{icon} [{alert.severity.upper()}] {alert.rule_name}: {alert.message}"
        )
        if alert.details:
            print(f"   Details: {json.dumps(alert.details, indent=2)}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_default_alerts(
    job_repository: JobRepository,
    approval_repository: ApprovalRepository | None = None,
    webhook_url: str = "",
) -> AlertManager:
    """创建默认告警配置。"""
    manager = AlertManager(
        job_repository, approval_repository, webhook_url=webhook_url
    )

    manager.add_rule(
        AlertRule(
            name="consecutive_failures",
            rule_type="consecutive_failures",
            threshold=3,
            enabled=True,
        )
    )

    manager.add_rule(
        AlertRule(
            name="duration_threshold",
            rule_type="duration_threshold",
            threshold=300,  # 5 分钟
            enabled=True,
        )
    )

    manager.add_rule(
        AlertRule(
            name="dead_letter",
            rule_type="dead_letter",
            threshold=1,
            enabled=True,
        )
    )

    manager.add_rule(
        AlertRule(
            name="pending_approvals_over_threshold",
            rule_type="pending_approvals",
            threshold=3,
            enabled=True,
        )
    )

    manager.add_rule(
        AlertRule(
            name="approval_timeout_spike",
            rule_type="approval_timeout_spike",
            threshold=2,
            enabled=True,
        )
    )

    manager.add_rule(
        AlertRule(
            name="node_unhealthy_killed",
            rule_type="node_unhealthy_killed",
            threshold=1,
            enabled=True,
        )
    )

    return manager
