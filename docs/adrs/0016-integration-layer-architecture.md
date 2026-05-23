# Integration Layer: Separate Read/Write Adapters

IssueTracker (read) and CodeHost (write) are defined as separate abstract interfaces
rather than a single "GitHubClient" class. This separation exists because:

1. **Permission models differ** — fetching issues requires read access to the issue tracker;
   pushing changes and creating PRs requires write access to the code host. In some orgs
   these may use different credentials or even different platforms (GitHub Issues + GitLab repos).

2. **Testing isolation** — mock the tracker without affecting sink behaviour and vice versa.
   Tests for LLM ranking only need tracker output; tests for PR creation only need the sink.

3. **Single Responsibility** — IssueTracker transforms external data into NormalizedIssue;
   CodeHost transforms execution results into platform actions. One adapts input, the other
   adapts output. Combining them creates a god-object.

The `integrations/` module (not `plugins/`) hosts these adapters. "Integration" describes
the actual concern — connecting Weave to external systems — without implying a generic
plugin framework. M5 has exactly one implementation (GitHub); the interface is designed
for extensibility but does not build a discovery mechanism.
