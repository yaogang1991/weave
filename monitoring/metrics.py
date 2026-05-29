"""
本地指标聚合系统。

从 JobRepository 中读取数据，计算关键指标：
- job_success_rate: 成功率
- job_duration_p95: 执行时长 P95
- node_retry_rate: 节点重试率
- failure_topn: 失败原因 TOP N
"""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_plane.approval import ApprovalRepository, TicketStatus
from control_plane.models import Job, JobStatus
from control_plane.repository import JobRepository


class MetricsCollector:
    """指标收集器。"""

    def __init__(
        self,
        job_repository: JobRepository,
        approval_repository: ApprovalRepository | None = None,
    ) -> None:
        self.job_repository = job_repository
        self.approval_repository = approval_repository

    def collect(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """收集所有指标。

        Args:
            since: 起始时间（可选）
            until: 结束时间（可选）
        """
        jobs = self.job_repository.list_jobs()
        if since or until:
            jobs = [
                j
                for j in jobs
                if (not since or j.created_at >= since)
                and (not until or j.created_at <= until)
            ]

        metrics: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "period": {
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
            },
            "summary": self._calc_summary(jobs),
            "duration": self._calc_duration_stats(jobs),
            "retries": self._calc_retry_stats(jobs),
            "failures": self._calc_failure_stats(jobs),
            "throughput": self._calc_throughput(jobs),
        }

        # 新增审批维度
        if self.approval_repository:
            metrics["approvals"] = self._calc_approval_stats()

        return metrics

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _calc_summary(self, jobs: list[Job]) -> dict[str, Any]:
        total = len(jobs)
        if total == 0:
            return {
                "total": 0,
                "succeeded": 0,
                "failed": 0,
                "canceled": 0,
                "dead_letter": 0,
                "success_rate": 0.0,
            }

        succeeded = sum(1 for j in jobs if j.status == JobStatus.SUCCEEDED)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        dead_letter = sum(1 for j in jobs if j.status == JobStatus.DEAD_LETTER)
        canceled = sum(1 for j in jobs if j.status == JobStatus.CANCELED)

        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "canceled": canceled,
            "dead_letter": dead_letter,
            "success_rate": round(succeeded / total * 100, 2) if total > 0 else 0.0,
        }

    # ------------------------------------------------------------------
    # Duration stats (from associated Run records)
    # ------------------------------------------------------------------

    def _calc_duration_stats(self, jobs: list[Job]) -> dict[str, Any]:
        """计算执行时长统计（从关联的 Run 记录中获取）。"""
        durations: list[float] = []
        for job in jobs:
            runs = self.job_repository.list_runs_by_job(job.id)
            for run in runs:
                if run.completed_at and run.started_at:
                    durations.append(
                        (run.completed_at - run.started_at).total_seconds()
                    )

        if not durations:
            return {
                "count": 0,
                "mean_sec": 0,
                "p50_sec": 0,
                "p95_sec": 0,
                "p99_sec": 0,
                "max_sec": 0,
            }

        durations.sort()
        n = len(durations)
        p50 = durations[int(n * 0.50)]
        p95 = durations[int(n * 0.95)] if n >= 20 else durations[-1]
        p99 = durations[int(n * 0.99)] if n >= 100 else durations[-1]

        return {
            "count": n,
            "mean_sec": round(statistics.mean(durations), 2),
            "p50_sec": round(p50, 2),
            "p95_sec": round(p95, 2),
            "p99_sec": round(p99, 2),
            "max_sec": round(max(durations), 2),
        }

    # ------------------------------------------------------------------
    # Retry stats
    # ------------------------------------------------------------------

    def _calc_retry_stats(self, jobs: list[Job]) -> dict[str, Any]:
        """计算重试统计。"""
        total_attempts = sum(j.attempt for j in jobs)
        total_jobs = len(jobs)
        jobs_with_retries = sum(1 for j in jobs if j.attempt > 1)

        return {
            "total_attempts": total_attempts,
            "avg_attempts": round(total_attempts / total_jobs, 2)
            if total_jobs > 0
            else 0,
            "jobs_with_retries": jobs_with_retries,
            "retry_rate": round(jobs_with_retries / total_jobs * 100, 2)
            if total_jobs > 0
            else 0.0,
        }

    # ------------------------------------------------------------------
    # Failure stats (TOP N)
    # ------------------------------------------------------------------

    def _calc_failure_stats(
        self, jobs: list[Job], top_n: int = 5
    ) -> dict[str, Any]:
        """计算失败原因 TOP N。"""
        error_counts: dict[str, int] = {}
        for job in jobs:
            if (
                job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTER)
                and job.error_category
            ):
                error_counts[job.error_category] = (
                    error_counts.get(job.error_category, 0) + 1
                )
            elif (
                job.status in (JobStatus.FAILED, JobStatus.DEAD_LETTER)
                and job.last_error
            ):
                # 使用 last_error 前 50 字作为分类
                key = job.last_error[:50]
                error_counts[key] = error_counts.get(key, 0) + 1

        top_errors = sorted(
            error_counts.items(), key=lambda x: x[1], reverse=True
        )[:top_n]

        return {
            "total_failures": sum(error_counts.values()),
            "top_errors": [
                {"reason": k, "count": v} for k, v in top_errors
            ],
        }

    # ------------------------------------------------------------------
    # Throughput
    # ------------------------------------------------------------------

    def _calc_throughput(self, jobs: list[Job]) -> dict[str, Any]:
        """计算吞吐量（按小时）。"""
        hour_counts: Counter = Counter()
        for job in jobs:
            # 使用 updated_at 作为完成时间（Job 没有独立的 completed_at 字段）
            hour_key = job.updated_at.strftime("%Y-%m-%d %H:00")
            hour_counts[hour_key] += 1

        if not hour_counts:
            return {
                "jobs_per_hour": 0,
                "peak_hour": None,
                "peak_count": 0,
            }

        peak_hour, peak_count = hour_counts.most_common(1)[0]
        avg_per_hour = sum(hour_counts.values()) / len(hour_counts)

        return {
            "jobs_per_hour": round(avg_per_hour, 2),
            "peak_hour": peak_hour,
            "peak_count": peak_count,
        }

    # ------------------------------------------------------------------
    # Approval stats
    # ------------------------------------------------------------------

    def _calc_approval_stats(self) -> dict[str, Any] | None:
        """计算审批相关指标。"""
        if not self.approval_repository:
            return None

        stats = self.approval_repository.get_stats()
        all_tickets = self.approval_repository.list_tickets()

        # 等待中的审批数
        pending_count = stats.get(TicketStatus.PENDING.value, 0)

        # 平均等待时长（仅统计已决定的）
        wait_times: list[float] = []
        for ticket in all_tickets:
            if ticket.status in (TicketStatus.APPROVED, TicketStatus.REJECTED):
                if ticket.decided_at and ticket.requested_at:
                    wait_sec = (
                        ticket.decided_at - ticket.requested_at
                    ).total_seconds()
                    if wait_sec >= 0:
                        wait_times.append(wait_sec)

        avg_wait = (
            round(statistics.mean(wait_times), 2) if wait_times else 0
        )
        p95_wait = (
            round(
                sorted(wait_times)[int(len(wait_times) * 0.95)], 2
            )
            if len(wait_times) >= 20
            else (max(wait_times) if wait_times else 0)
        )

        # 自动通过率
        total_decided = stats.get(
            TicketStatus.APPROVED.value, 0
        ) + stats.get(TicketStatus.REJECTED.value, 0)
        auto_approved = sum(
            1
            for t in all_tickets
            if t.status == TicketStatus.APPROVED
            and t.decided_by == "auto"
        )
        auto_approve_rate = (
            round(auto_approved / total_decided * 100, 2)
            if total_decided > 0
            else 0
        )

        # 人工干预率（需要人确认的占总审批的比例）
        total_tickets = sum(stats.values())
        manual_approved = sum(
            1
            for t in all_tickets
            if t.status == TicketStatus.APPROVED
            and t.decided_by == "user"
        )
        manual_rate = (
            round(manual_approved / total_tickets * 100, 2)
            if total_tickets > 0
            else 0
        )

        # 按风险级别分布
        risk_distribution: dict[str, int] = {}
        for ticket in all_tickets:
            risk_distribution[ticket.risk_level] = (
                risk_distribution.get(ticket.risk_level, 0) + 1
            )

        return {
            "pending_count": pending_count,
            "approved_count": stats.get(
                TicketStatus.APPROVED.value, 0
            ),
            "rejected_count": stats.get(
                TicketStatus.REJECTED.value, 0
            ),
            "expired_count": stats.get(
                TicketStatus.EXPIRED.value, 0
            ),
            "total_count": total_tickets,
            "avg_wait_sec": avg_wait,
            "p95_wait_sec": p95_wait,
            "auto_approve_rate": auto_approve_rate,
            "manual_intervention_rate": manual_rate,
            "risk_distribution": risk_distribution,
        }


class MetricsReporter:
    """指标报告生成器。"""

    def generate_json_report(
        self, metrics: dict[str, Any], output_path: str | None = None
    ) -> str:
        """生成 JSON 格式报告。"""
        report = json.dumps(metrics, indent=2, default=str)
        if output_path:
            dest = Path(output_path)
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(report, encoding="utf-8")
            os.replace(tmp, dest)
        return report

    def generate_markdown_report(
        self, metrics: dict[str, Any], output_path: str | None = None
    ) -> str:
        """生成 Markdown 格式报告。"""
        summary = metrics["summary"]
        duration = metrics["duration"]
        retries = metrics["retries"]
        failures = metrics["failures"]

        lines: list[str] = [
            "# Weave M1 指标报告",
            "",
            f"**生成时间**: {metrics['timestamp']}",
            f"**统计周期**: {metrics['period']['since'] or '全部'} ~ "
            f"{metrics['period']['until'] or '现在'}",
            "",
            "## 概览",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 总任务数 | {summary['total']} |",
            f"| 成功 | {summary['succeeded']} |",
            f"| 失败 | {summary['failed']} |",
            f"| 死信 | {summary['dead_letter']} |",
            f"| 取消 | {summary['canceled']} |",
            f"| **成功率** | **{summary['success_rate']}%** |",
            "",
            "## 执行时长",
            "",
            "| 指标 | 数值(秒) |",
            "|------|----------|",
            f"| 平均 | {duration['mean_sec']} |",
            f"| P50 | {duration['p50_sec']} |",
            f"| P95 | {duration['p95_sec']} |",
            f"| P99 | {duration['p99_sec']} |",
            f"| 最大 | {duration['max_sec']} |",
            "",
            "## 重试统计",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 总尝试次数 | {retries['total_attempts']} |",
            f"| 平均尝试 | {retries['avg_attempts']} |",
            f"| 重试任务数 | {retries['jobs_with_retries']} |",
            f"| 重试率 | {retries['retry_rate']}% |",
            "",
            "## 失败分析 TOP N",
            "",
            "| 原因 | 次数 |",
            "|------|------|",
        ]

        for item in failures["top_errors"]:
            lines.append(f"| {item['reason']} | {item['count']} |")

        if not failures["top_errors"]:
            lines.append("| (无失败) | 0 |")

        lines.append("")

        # 新增审批维度
        approvals = metrics.get("approvals")
        if approvals:
            lines.extend([
                "## 审批统计",
                "",
                "| 指标 | 数值 |",
                "|------|------|",
                f"| 待审批 | {approvals['pending_count']} |",
                f"| 已批准 | {approvals['approved_count']} |",
                f"| 已拒绝 | {approvals['rejected_count']} |",
                f"| 已过期 | {approvals['expired_count']} |",
                f"| 平均等待 | {approvals['avg_wait_sec']}s |",
                f"| P95 等待 | {approvals['p95_wait_sec']}s |",
                f"| 自动通过率 | {approvals['auto_approve_rate']}% |",
                f"| 人工干预率 | {approvals['manual_intervention_rate']}% |",
                "",
                "### 风险分布",
                "",
            ])
            for risk, count in approvals["risk_distribution"].items():
                lines.append(f"- {risk}: {count}")
            lines.append("")

        report = "\n".join(lines)
        if output_path:
            dest = Path(output_path)
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(report, encoding="utf-8")
            os.replace(tmp, dest)
        return report
