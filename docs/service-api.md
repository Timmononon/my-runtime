# Aster service API

## Electron desktop workbench

The `desktop_workbench_v1` capability is advertised by `GET /health` and `GET /capabilities`. Electron loads the HTML and compiled frontend from its installed resources. The Runtime serves the authenticated loopback API. Its root and `/assets/` paths do not serve the workbench.

The local launcher requests a one-time ticket with bearer authentication at `POST /ui/tickets`. The Electron main process redeems it at `POST /ui/auth` in its own session before loading the workbench. The renderer uses an HttpOnly session cookie and `X-Aster-UI: 1`. Mutating cookie requests require the exact local origin. `GET /ui/bootstrap` returns startup information and `POST /ui/logout` ends the session.

This page documents Feishu CLI diagnostics and group task sources.

## Authentication

CLI/API clients use `Authorization: Bearer <local token>`. The token is generated
in `<data_dir>/.api-token` and is separate from provider credentials. The service
binds to loopback and validates Host and any supplied Origin. Cookie clients
must send `X-Aster-UI: 1`; POST and PATCH requests also require the exact same Origin.
Bearer clients need no browser Origin header, but any supplied Origin must be
allowed. The diagnostic endpoint is an authenticated local-operator action.

## Feishu CLI diagnostics (0.1.13)

Capability `feishu_cli_status_v1` adds explicit `POST /feishu/check` on the
existing Bearer or UI-cookie boundary. Cookie requests require `X-Aster-UI: 1`
and exact same Origin. It is not a public health endpoint; clients discover the
capability instead of guessing from the version.

| Request | Behavior |
|---|---|
| No body, `{}`, or `{"verify_business":false}` | Runs only the managed native CLI's fixed local `auth status` diagnostic |
| `{"verify_business":true}` | Also performs one fixed `calendar +agenda` read with `identity=user` in this daemon's execution context |

`verify_business` is a strict boolean, default false. Additional body fields,
non-boolean values and all query parameters return 422. No caller-supplied argv,
executable, environment, profile, identity or business target is accepted. The
check never starts a model, creates a session/run or writes Runtime database,
configuration or provider credentials. The optional business probe is a real network
read; it cannot send messages or modify cloud documents.

The response is a bounded diagnostic projection:

| Field | Meaning |
|---|---|
| `cli_installed`, `app_configured` | Boolean installation/application status |
| `user` | `available`, nullable `token_status`, nullable `scope_count`, and `scopes` names |
| `bot` | `available` and nullable `token_status` |
| `account_fingerprint`, `user_binding_fingerprint` | Optional anonymous 16-hex SHA256 prefixes for comparing bindings; no raw account IDs |
| `windows_registry` | `supported`, nullable `key_exists` and `value_count`; only fixed HKCU `lark-cli` key metadata is queried, never value names or credential values |
| `diagnostic_only`, `business_access_verified` | Safe diagnostic output marker; business verification is false unless the fixed real user read succeeds |
| `business_probe` | Present when requested: `attempted`, `success`, with optional bounded `error_type`, `error_subtype`, integer `error_code` |
| `is_error`, `outcome`, optional `error_type` / `error_subtype` | Check result and safe failure categories |

HTTP 200 means the requested check completed without a diagnostic error. Local
`ready` or a successful local-only check does not prove business access; inspect
`business_access_verified`. Failed diagnostics/probes return 503 with safe
categories, without calendar content, IDs, secrets, raw CLI output or recovery
URLs. The adapter never responds to an auth failure by switching `user` to `bot`,
initiating OAuth/login or retrying under another identity. The model tool permits only exact
`argv=["auth","status"]` as a read-only management exception; login/config remain
human setup. See [Feishu usage](feishu-cli.md).

## Feishu group task sources (0.1.20)

Capability `feishu_task_sources_v1` enables the Workbench’s Feishu management page.
The application owns the native CLI event subscription. Incoming group messages
enter the shared task triage and Worker pipeline through their verified app,
group, sender and mention metadata. [Feishu task source setup](feishu-task-sources.md)

| Method and path | Result |
| --- | --- |
| `POST /feishu/discover` | Verify the current bot and list joined groups through read-only Feishu calls |
| `GET /feishu/sources` | List local sources, optionally filtered by `source_session_id` |
| `POST /feishu/sources` | Create a disabled source with `source_session_id`, `chat_id`, optional `name`, `allowed_senders` and `required_keywords` |
| `GET /feishu/sources/{id}` | Read a source and its connection status |
| `PATCH /feishu/sources/{id}` | Edit `name`, `allowed_senders`, `required_keywords` or `enabled` using `expected_revision` |
| `GET /feishu/sources/{id}/events` | Read newest-first receipts with `limit` and optional `before` pagination |
| `GET /feishu/sources/{id}/tickets` | Read linked local task tickets |

These routes use the existing local Runtime authentication. Management GETs
read local state. A source retains its verified application and target group
binding. The native event consumer supplies events to the Runtime, and SQLite
ties each accepted receipt to its task and authorization. A ticket includes a
bounded `feishu` origin projection for navigation and authorization state.
The shared `/tasks/triage/settings` endpoint controls the classification model.
