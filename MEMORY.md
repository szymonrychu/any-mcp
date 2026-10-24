# any-mcp memory

Lessons that outlive a session. Newest first.

## 2026-10-24 - orchestrator plugin is built into any-mcp

The orchestrator plugin (`any_mcp.orchestrator:OrchestratorPlugin`) ships inside any-mcp (option a), so the orchestrator's agent onStart needs no extra pip line and no second package to pin: the one any-mcp release carries every agent-to-orchestrator tool for hive, cluster-scaling and agent-init-scripts. Tool visibility is set by the rendered entry's `allow` list, not by the plugin.
