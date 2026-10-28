# Integration acceptance
- Validate required/unknown arguments.
- Compare actual source result and output schema.
- Verify pagination and empty results.
- Verify credentials never appear in errors/logs.
- Kill the server after a fixture write: inspect persisted effect; no automatic replay.
- Restart and verify read-only recovery, then close and check child processes.
- Mark external service correctness unsupported until its authorized integration test runs.
