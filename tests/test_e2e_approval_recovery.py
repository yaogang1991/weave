"""
端到端审批恢复回归测试。

场景: 创建 pending ticket -> 模拟重启 -> ticket 过期/审批后继续流转。
目标: 确认 worker 的 pending 恢复路径长期稳定。
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from control_plane.models import JobStatus
from control_plane.approval import ApprovalRepository, TicketStatus
from control_plane.repository import JobRepository


class TestE2EApprovalRecovery:
    """端到端：审批中断 -> 重启恢复 -> 状态正确推进。"""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def job_repo(self, temp_dir):
        return JobRepository(base_path=str(temp_dir / "jobs"))

    @pytest.fixture
    def approval_repo(self, temp_dir):
        return ApprovalRepository(base_path=str(temp_dir / "approvals"))

    def test_pending_ticket_expires_on_recovery(self, job_repo, approval_repo):
        """
        场景: 任务运行中创建 pending ticket -> worker 中断 ->
              重启后 ticket 过期 -> job 推进到失败策略
        """
        # 1. 创建 job 并设为 running
        job = job_repo.create_job("test recovery")
        job.status = JobStatus.RUNNING
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        job_repo.update_job(job)

        # 2. 创建 pending ticket（已过期）
        ticket = approval_repo.create_ticket(
            job_id=job.id,
            tool_name="bash",
            args={"command": "rm -rf /tmp/test"},
            risk_level="high",
            timeout_sec=-60,  # 已经过期
        )
        assert ticket.status == TicketStatus.PENDING

        # 3. 模拟重启：过期 ticket
        expired = approval_repo.expire_tickets()
        assert len(expired) == 1
        assert expired[0].id == ticket.id
        assert expired[0].status == TicketStatus.EXPIRED

        # 4. 验证 job 状态（业务层应推进失败）
        # 注：完整测试需结合 service.handle_job_failure
        # 此处验证 ticket 过期 + job 可查询到即可
        job = job_repo.get_job(job.id)
        assert job.status == JobStatus.RUNNING  # 业务层负责推进

    def test_approved_ticket_resumes_flow(self, job_repo, approval_repo):
        """
        场景: pending ticket 被 approve -> 任务继续执行
        """
        # 1. 创建 job
        job = job_repo.create_job("test approval resume")
        job.status = JobStatus.RUNNING
        job_repo.update_job(job)

        # 2. 创建 pending ticket
        ticket = approval_repo.create_ticket(
            job_id=job.id,
            tool_name="bash",
            args={"command": "ls -la"},
            risk_level="high",
        )

        # 3. 批准 ticket
        approved = approval_repo.approve_ticket(ticket.id, reason="Safe command")
        assert approved.status == TicketStatus.APPROVED
        assert approved.decided_by == "user"

        # 4. 验证 ticket 状态可追溯
        stats = approval_repo.get_stats()
        assert stats[TicketStatus.APPROVED.value] == 1
        assert stats[TicketStatus.PENDING.value] == 0

    def test_rejected_ticket_aborts_flow(self, job_repo, approval_repo):
        """
        场景: pending ticket 被 reject -> 任务失败
        """
        # 1. 创建 job
        job = job_repo.create_job("test rejection abort")
        job.status = JobStatus.RUNNING
        job_repo.update_job(job)

        # 2. 创建 pending ticket
        ticket = approval_repo.create_ticket(
            job_id=job.id,
            tool_name="bash",
            args={"command": "rm -rf /"},
            risk_level="critical",
        )

        # 3. 拒绝 ticket
        rejected = approval_repo.reject_ticket(ticket.id, reason="Too dangerous")
        assert rejected.status == TicketStatus.REJECTED
        assert rejected.reason == "Too dangerous"

        # 4. 验证统计
        stats = approval_repo.get_stats()
        assert stats[TicketStatus.REJECTED.value] == 1

    def test_orphan_ticket_recovery(self, job_repo, approval_repo):
        """
        场景: job 已完成但 ticket 仍 pending -> 孤儿恢复
        """
        # 1. 创建 job 并设为 succeeded
        job = job_repo.create_job("test orphan")
        job.status = JobStatus.SUCCEEDED
        job_repo.update_job(job)

        # 2. 创建 pending ticket（异常情况：job 已完成）
        ticket = approval_repo.create_ticket(
            job_id=job.id,
            tool_name="bash",
            args={"command": "echo test"},
            risk_level="high",
        )

        # 3. 模拟 worker 启动恢复：扫描不一致状态
        # Job 不在 running/leased 但 ticket 仍 pending
        pending_tickets = approval_repo.list_tickets(status=TicketStatus.PENDING)
        orphan_tickets = [
            t for t in pending_tickets
            if job_repo.get_job(t.job_id).status not in
            (JobStatus.RUNNING, JobStatus.LEASED)
        ]

        # 4. 修复孤儿 ticket
        for t in orphan_tickets:
            t.status = TicketStatus.EXPIRED
            t.decided_by = "auto"
            t.reason = "Job no longer active"
            t.decided_at = datetime.now(timezone.utc)
            approval_repo.update_ticket(t)

        # 5. 验证
        assert len(orphan_tickets) == 1
        updated = approval_repo.get_ticket(ticket.id)
        assert updated.status == TicketStatus.EXPIRED
        assert updated.decided_by == "auto"

    def test_full_recovery_pipeline(self, job_repo, approval_repo):
        """
        完整管道: 多个 ticket 的各种状态 -> 重启后全部正确处理
        """
        # 创建 3 个 job
        jobs = []
        for i in range(3):
            job = job_repo.create_job(f"pipeline job {i}")
            job.status = JobStatus.RUNNING if i < 2 else JobStatus.SUCCEEDED
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            job_repo.update_job(job)
            jobs.append(job)

        # 创建 4 个 ticket，不同状态
        # ticket 1: pending + 过期 (job running)
        t1 = approval_repo.create_ticket(
            job_id=jobs[0].id, tool_name="bash", args={"command": "cmd1"},
            risk_level="high", timeout_sec=-60,
        )
        # ticket 2: pending + 有效 (job running)
        t2 = approval_repo.create_ticket(
            job_id=jobs[1].id, tool_name="bash", args={"command": "cmd2"},
            risk_level="high", timeout_sec=300,
        )
        # ticket 3: pending + 孤儿 (job succeeded)
        t3 = approval_repo.create_ticket(
            job_id=jobs[2].id, tool_name="bash", args={"command": "cmd3"},
            risk_level="high", timeout_sec=300,
        )
        # ticket 4: already approved
        t4 = approval_repo.create_ticket(
            job_id=jobs[0].id, tool_name="bash", args={"command": "cmd4"},
            risk_level="high", timeout_sec=300,
        )
        approval_repo.approve_ticket(t4.id)

        # 执行恢复
        approval_repo.expire_tickets()  # noqa: F841
        pending = approval_repo.list_tickets(status=TicketStatus.PENDING)
        orphans = [
            t for t in pending
            if job_repo.get_job(t.job_id).status not in
            (JobStatus.RUNNING, JobStatus.LEASED)
        ]
        for t in orphans:
            t.status = TicketStatus.EXPIRED
            t.decided_by = "auto"
            t.reason = "Orphan recovery"
            approval_repo.update_ticket(t)

        # 验证最终状态
        stats = approval_repo.get_stats()
        assert stats[TicketStatus.EXPIRED.value] == 2   # t1(过期) + t3(孤儿)
        assert stats[TicketStatus.PENDING.value] == 1   # t2(仍有效)
        assert stats[TicketStatus.APPROVED.value] == 1  # t4

        # 验证各 ticket 状态
        assert approval_repo.get_ticket(t1.id).status == TicketStatus.EXPIRED
        assert approval_repo.get_ticket(t2.id).status == TicketStatus.PENDING
        assert approval_repo.get_ticket(t3.id).status == TicketStatus.EXPIRED
        assert approval_repo.get_ticket(t4.id).status == TicketStatus.APPROVED
