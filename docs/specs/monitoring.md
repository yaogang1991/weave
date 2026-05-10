# Module SPEC: monitoring/ (metrics.py + alerts.py)

## Purpose

Local metrics aggregation and alerting for the harness control plane. `MetricsCollector` computes
operational statistics (success rate, duration percentiles, retry rates, throughput, approval
metrics) from job and approval repositories. `MetricsReporter` renders collected metrics as JSON
or Markdown reports. `AlertManager` evaluates configurable alert rules against live metrics and
dispatches notifications via webhook (with console fallback).

## Public Interfaces

### Class `MetricsCollector` (metrics.py)

```python
class MetricsCollector:
    def __init__(
        self,
        job_repository: JobRepository,
        approval_repository: ApprovalRepository | None = None,
    ) -> None
```

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `collect` | `(since: datetime \| None = None, until: datetime \| None = None) -> dict[str, Any]` | `dict` | Aggregates all metrics within an optional time window |

**Private Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `_calc_summary` | `(jobs: list[Job]) -> dict[str, Any]` | Computes total, succeeded, failed, canceled, dead_letter counts and success_rate |
| `_calc_duration_stats` | `(jobs: list[Job]) -> dict[str, Any]` | Computes mean, P50, P95, P99, max duration from associated Run records |
| `_calc_retry_stats` | `(jobs: list[Job]) -> dict[str, Any]` | Computes total_attempts, avg_attempts, jobs_with_retries, retry_rate |
| `_calc_failure_stats` | `(jobs: list[Job], top_n: int = 5) -> dict[str, Any]` | Computes total_failures and top N error categories/reasons |
| `_calc_throughput` | `(jobs: list[Job]) -> dict[str, Any]` | Computes jobs_per_hour, peak_hour, peak_count |
| `_calc_approval_stats` | `() -> dict[str, Any] \| None` | Computes approval metrics (pending, wait times, auto-approve rate, risk distribution) |

---

### Class `MetricsReporter` (metrics.py)

```python
class MetricsReporter:
    # No __init__ parameters
```

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `generate_json_report` | `(metrics: dict[str, Any], output_path: str \| None = None) -> str` | `str` | Serializes metrics dict to JSON; optionally writes to file |
| `generate_markdown_report` | `(metrics: dict[str, Any], output_path: str \| None = None) -> str` | `str` | Generates a Markdown report with tables for summary, duration, retries, failures, and approvals; optionally writes to file |

---

### Class `AlertManager` (alerts.py)

```python
class AlertManager:
    def __init__(
        self,
        job_repository: JobRepository,
        approval_repository: ApprovalRepository | None = None,
        webhook_url: str = "",
        cooldown_sec: int = 300,
    ) -> None
```

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `add_rule` | `(rule: AlertRule) -> None` | `None` | Registers an alert rule |
| `on_alert` | `(handler: Callable[[AlertEvent], None]) -> None` | `None` | Registers a custom alert handler callback |
| `check_all` | `() -> list[AlertEvent]` | `list[AlertEvent]` | Evaluates all enabled rules, returns triggered alerts |
| `send_alert` | `(alert: AlertEvent) -> bool` | `bool` | Dispatches an alert (webhook first, console fallback); respects cooldown; returns success status |

**Private Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `_check_rule` | `(rule: AlertRule) -> AlertEvent \| None` | Dispatches to type-specific checker based on `rule.rule_type` |
| `_check_consecutive_failures` | `(rule: AlertRule) -> AlertEvent \| None` | Checks last 20 jobs for consecutive failures; severity escalates at 2x threshold |
| `_check_duration_threshold` | `(rule: AlertRule) -> AlertEvent \| None` | Checks if P95 duration exceeds threshold |
| `_check_dead_letter` | `(rule: AlertRule) -> AlertEvent \| None` | Checks dead_letter queue count |
| `_check_pending_approvals` | `(rule: AlertRule) -> AlertEvent \| None` | Checks pending approval ticket count; severity escalates at 2x threshold |
| `_check_approval_timeout_spike` | `(rule: AlertRule) -> AlertEvent \| None` | Checks expired tickets in last 1 hour |
| `_check_node_unhealthy_killed` | `(rule: AlertRule) -> AlertEvent \| None` | Checks jobs with `error_category == "watchdog"` |
| `_check_heartbeat_miss_spike` | `(rule: AlertRule) -> AlertEvent \| None` | Placeholder (returns `None`) |
| `_send_webhook` | `(alert: AlertEvent) -> None` | POSTs JSON payload to `webhook_url` via `urllib.request` |
| `_send_console` | `(alert: AlertEvent) -> None` | Prints formatted alert to stdout |

---

### Data Classes (alerts.py)

```python
@dataclass
class AlertRule:
    name: str
    rule_type: str   # "consecutive_failures" | "duration_threshold" | "dead_letter"
                     # "pending_approvals" | "approval_timeout_spike"
                     # "node_unhealthy_killed" | "heartbeat_miss_spike"
    threshold: float
    enabled: bool = True
    webhook_url: str = ""

@dataclass
class AlertEvent:
    rule_name: str
    severity: str                                      # "warning" | "critical"
    message: str
    timestamp: str = field(default_factory=...)        # ISO 8601 UTC
    details: dict[str, Any] = field(default_factory=dict)
```

---

### Factory Function (alerts.py)

```python
def create_default_alerts(
    job_repository: JobRepository,
    approval_repository: ApprovalRepository | None = None,
    webhook_url: str = "",
) -> AlertManager
```

Creates an `AlertManager` with six pre-configured rules:

| Rule Name | Type | Threshold |
|-----------|------|-----------|
| `consecutive_failures` | `consecutive_failures` | 3 |
| `duration_threshold` | `duration_threshold` | 300 (5 min) |
| `dead_letter` | `dead_letter` | 1 |
| `pending_approvals_over_threshold` | `pending_approvals` | 3 |
| `approval_timeout_spike` | `approval_timeout_spike` | 2 |
| `node_unhealthy_killed` | `node_unhealthy_killed` | 1 |

## Data Flow

```
JobRepository + ApprovalRepository
        |
        v
MetricsCollector.collect(since?, until?)
        |
        +--->_calc_summary(jobs) ---------> {"total", "succeeded", "failed", "canceled", "dead_letter", "success_rate"}
        +--->_calc_duration_stats(jobs) ---> {"count", "mean_sec", "p50_sec", "p95_sec", "p99_sec", "max_sec"}
        +--->_calc_retry_stats(jobs) ------> {"total_attempts", "avg_attempts", "jobs_with_retries", "retry_rate"}
        +--->_calc_failure_stats(jobs) ----> {"total_failures", "top_errors": [{reason, count}]}
        +--->_calc_throughput(jobs) -------> {"jobs_per_hour", "peak_hour", "peak_count"}
        +--->_calc_approval_stats() -------> {"pending_count", "approved_count", ..., "risk_distribution"}
        |
        v
MetricsReporter.generate_json_report() or .generate_markdown_report()

AlertManager:
        AlertRule[] ---> check_all() ---> AlertEvent[] ---> send_alert()
                                                             |
                                                        webhook POST (primary)
                                                        console print (fallback)
                                                        registered handlers (always)
```

## Error Codes

No custom error codes. Error handling strategy:

| Condition | Behavior |
|-----------|----------|
| Empty job list | `_calc_summary` returns all-zero dict; duration stats return zeroed dict |
| No runs for job | Duration list is empty, zeroed stats returned |
| Webhook failure | Exception caught, prints warning, falls back to console |
| Console failure | Exception caught silently |
| Handler exception | Exception caught silently, other handlers still invoked |
| Cooldown active | `send_alert()` returns `False`, no notification sent |

## Dependencies

| Dependency | Type | Usage |
|------------|------|-------|
| `control_plane.models` | Internal | `Job`, `JobStatus` |
| `control_plane.repository` | Internal | `JobRepository` |
| `control_plane.approval` | Internal | `ApprovalRepository`, `TicketStatus` |
| `statistics` | Stdlib | Mean calculation |
| `collections.Counter` | Stdlib | Throughput counting |
| `json` | Stdlib | Report serialization |
| `urllib.request` | Stdlib | Webhook HTTP POST |
| `dataclasses` | Stdlib | `AlertRule`, `AlertEvent` definitions |

## Configuration

| Parameter | Default | Scope | Description |
|-----------|---------|-------|-------------|
| `webhook_url` | `""` | `AlertManager.__init__` / `create_default_alerts` | Global webhook URL; can be overridden per-rule via `AlertRule.webhook_url` |
| `cooldown_sec` | `300` | `AlertManager.__init__` | Minimum seconds between repeated alerts for the same rule |
| `top_n` | `5` | `_calc_failure_stats` parameter | Number of top error reasons to report |

## Extension Points

1. **New alert rule types**: Add a new `rule_type` string to `AlertRule`, implement a
   `_check_{type}()` method on `AlertManager`, and add a dispatch branch in `_check_rule()`.
2. **Custom alert handlers**: Register via `on_alert(handler)` for integration with PagerDuty,
   Slack, email, etc.
3. **Per-rule webhook override**: `AlertRule.webhook_url` allows routing specific alerts to
   different endpoints (currently defined but not used in `_send_webhook`).
4. **Metrics output formats**: Extend `MetricsReporter` with new `generate_*_report()` methods.
5. **Heartbeat miss spike**: `_check_heartbeat_miss_spike` is a placeholder returning `None`;
   implement when metrics infrastructure supports heartbeat tracking.

## Invariants

1. **Metrics are read-only**: `MetricsCollector` never mutates `JobRepository` or
   `ApprovalRepository` state.
2. **Alerts never block the main flow**: All alert dispatch (webhook, console, handlers) is
   wrapped in try/except. Failures are logged but never propagated.
3. **Cooldown deduplication**: The same rule cannot trigger more than once within `cooldown_sec`
   seconds. Tracked via `_last_alert_time` dict keyed by `AlertEvent.rule_name`.
4. **Severity escalation**: `consecutive_failures` and `pending_approvals` rules escalate to
   `"critical"` when the measured value reaches 2x the configured threshold.
5. **Duration percentile guard**: P95 uses `durations[-1]` when fewer than 20 data points exist;
   P99 uses `durations[-1]` when fewer than 100 data points exist.
6. **Success rate precision**: Rounded to 2 decimal places (percentage).
7. **Failure classification**: Failed/dead-letter jobs are classified first by `error_category`,
   then by `last_error[:50]` as a fallback key.
