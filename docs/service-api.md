# Aster service API

This page documents the Feishu CLI diagnostic endpoint.

## Authentication

CLI/API clients use `Authorization: Bearer <local token>`. The token is generated
in `<data_dir>/.api-token` and is separate from provider credentials. The service
binds to loopback and validates Host and any supplied Origin. Cookie clients
must send `X-Aster-UI: 1`; POST requests also require the exact same Origin.
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
