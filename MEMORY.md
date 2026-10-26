# any-mcp memory

Lessons that outlive a session. Newest first.

## 2026-10-26 - session messaging tools, no config key

v0.4.0 adds `sessions_list` (`GET /agent/v1/sessions`) and `session_send(slug, text)` (`POST /agent/v1/sessions/{slug}/messages`, `slug` only in the path, body `{"text"}`) to the orchestrator plugin. `OrchestratorConfig` gains no key for them: the orchestrator renders the two tools for every session and its server decides what each caller may list and send (superleader rule), so a config key would only have to move in lockstep with the orchestrator for no gain. The plugin always lists eleven tools; the rendered `allow` list stays the visibility control.

## 2026-10-24 - orchestrator plugin is built into any-mcp

The orchestrator plugin (`any_mcp.orchestrator:OrchestratorPlugin`) ships inside any-mcp (option a), so the orchestrator's agent onStart needs no extra pip line and no second package to pin: the one any-mcp release carries every agent-to-orchestrator tool for hive, cluster-scaling and agent-init-scripts. Tool visibility is set by the rendered entry's `allow` list, not by the plugin.
