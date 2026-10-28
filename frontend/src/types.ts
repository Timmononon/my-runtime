export interface Session {
  id: string;
  title: string;
  cwd: string;
  agent_id: string;
  project_id: string | null;
  updated_at: number;
  archived_at?: number | null;
  deleted_at?: number | null;
  lineage?: Lineage;
  selection?: RunSelection;
  selection_warning?: string;
}
export interface RunSelection {
  provider_id: string;
  model: string;
  reasoning_effort: string | null;
  selected_skill: string | null;
}
export interface ProviderModel {
  id: string;
  name?: string;
  context_window?: number;
  max_output_tokens?: number;
  input_modalities?: string[];
  effort_levels?: string[];
}
export interface ModelProvider {
  id: string;
  name: string;
  base_url: string;
  protocol: string;
  revision: number;
  configured: boolean;
  builtin?: boolean;
  credential_source?: "environment" | "file" | "vault" | "unconfigured";
  models: ProviderModel[];
}
export interface ProviderProtocol {
  id: string;
  label: string;
  supported: boolean;
}
export interface Project {
  id: string;
  name: string;
  cwd: string;
  created_at: number;
  updated_at: number;
  session_count: number;
  removed_at?: number | null;
}
export interface Lineage {
  kind: string;
  parent_id: string | null;
  boundary_seq: number;
  closed_at: number | null;
}
export interface Artifact {
  artifact_id: string;
  attachment_id?: string;
  filename?: string;
  path?: string;
  title?: string;
  size?: number;
  mime_type?: string;
  kind?: string;
  download_path?: string;
}
export interface ChatMessage {
  role: string;
  content: string;
  tool_calls: {
    id: string;
    name: string;
    arguments: Record<string, unknown>;
  }[];
  tool_call_id?: string;
  metadata: Record<string, unknown> & {
    artifacts?: Artifact[];
    is_error?: boolean;
    inherited_from_session?: string;
  };
}
export interface MessageRecord {
  id: string;
  seq: number;
  run_id: string | null;
  created_at: number;
  message: ChatMessage;
}
export interface Run {
  id: string;
  kind?: string;
  session_id: string;
  status: string;
  answer?: string;
  error?: string | null;
  created_at: number;
  updated_at: number;
  pending_requests?: Pending[];
  tool_executions?: Execution[];
  usage?: Usage[];
  budget?: { model_requests: number; tool_calls: number; iterations: number };
  cost_estimate?: {
    status?: string;
    amount?: string | null;
    currency?: string;
    total?: number;
  };
}
export interface Pending {
  id: string;
  kind: string;
  state: string;
  payload: {
    tool_name?: string;
    arguments?: Record<string, unknown>;
    risk?: string;
    question?: string;
    options?: unknown[];
    description?: string;
  };
}
export interface Execution {
  id: string;
  tool_name: string;
  tool_call_id: string;
  state: string;
  risk: string;
  result?: { is_error?: boolean; error_category?: string; outcome?: string };
}
export interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
  cache_hit_tokens: number | null;
  reasoning_tokens: number | null;
}
export interface ContextUsage {
  schema_version: number;
  session_id: string;
  status: "estimated" | "unavailable";
  source: "active_context";
  model: string | null;
  provider_id: string | null;
  used_tokens: number | null;
  context_window: number | null;
  percent: number | null;
  reserved_output_tokens: number | null;
  breakdown: {
    system_tokens: number | null;
    tools_tokens: number | null;
    message_tokens: number;
    calibration_tokens: number | null;
  };
  updated_at: number | null;
}
export interface SessionDetail extends Session {
  messages: MessageRecord[];
  runs: Run[];
  plan: {
    revision: number;
    snapshot: {
      steps?: {
        id?: string;
        text?: string;
        title?: string;
        description?: string;
        status?: string;
      }[];
      lifecycle?: string;
    };
  } | null;
}
export interface RuntimeEvent {
  event_id: number;
  type: string;
  run_id: string;
  session_id: string;
  payload: Record<string, unknown>;
  created_at: number;
}
export interface Bootstrap {
  cwd: string;
  model: string;
  provider_configured: boolean;
  instance_id: string;
  schema_version: number;
  ui_version: number;
  selection?: RunSelection;
  providers?: ModelProvider[];
  protocols?: ProviderProtocol[];
}
export interface Mail {
  id: string;
  text: string;
  state: string;
  steering: number;
  attachments: string[];
}
export interface Performance {
  phase: string;
  phase_seconds: Record<string, number>;
  usage: Usage[];
  budget: Run["budget"];
  trace: Record<string, unknown>[];
  timeline?: {
    event_id: number;
    type: string;
    at: number;
    offset_ms: number;
    phase?: string;
    name?: string;
    model?: string;
    outcome?: string;
    duration_ms?: number;
    latency_ms?: number;
    before_tokens?: number;
    after_tokens?: number;
    purpose?: string;
  }[];
  batches?: {
    event_id: number;
    at: number;
    count: number;
    parallel: boolean;
    max_concurrency: number;
    duration_ms: number | null;
    tools: {
      tool_call_id: string;
      name: string;
      duration_ms: number | null;
      outcome: string | null;
      is_error: boolean | null;
    }[];
  }[];
  cache_summary?: {
    requests: number;
    reported_requests: number;
    unknown_requests: number;
    cache_hit_tokens: number | null;
    cache_miss_tokens: number | null;
    hit_ratio: number | null;
  };
  cost_estimate?: {
    status: string;
    amount: string | null;
    known_subtotal: string;
    unknown_requests: number;
    currency: string | null;
    pricing_version: string | null;
    requests: number;
  };
}
export interface Permissions {
  allowed_tools: string[] | null;
  denied_tools: string[];
  ask_tools: string[];
  auto_approve_tools: string[];
}
export interface ManagedAgent {
  id: string;
  name: string;
  instructions: string;
  revision: string;
  selection: RunSelection;
  permissions: Permissions;
  effective_permissions: Permissions;
}
export interface MCPHealth {
  name: string;
  status: string;
  required: boolean;
  connected: boolean;
  tool_count: number;
  reconnect_attempts: number;
  next_retry_at: number | null;
  error_category: string | null;
}
export interface ManagedMCP {
  name: string;
  transport?: string;
  command?: string | null;
  args?: string[];
  cwd?: string | null;
  url?: string | null;
  env?: Record<string, string>;
  headers?: Record<string, string>;
  timeout?: number;
  required?: boolean;
  health?: MCPHealth;
  [key: string]: unknown;
}
export interface ManagedHook {
  event: string;
  executable: string;
  args: string[];
  timeout_seconds: number;
  output_limit: number;
  tool_names: string[] | null;
}
export interface RuntimeManagement {
  schema_version: number;
  revision: string;
  agents: ManagedAgent[];
  mcp_servers: ManagedMCP[];
  hooks: ManagedHook[];
  permissions: Permissions;
  extensions: {
    embedding: Record<string, unknown>;
    embedding_status?: {
      status: string;
      available: boolean;
      configured: boolean;
      backend?: "remote" | "local";
      local?: {
        state: "unloaded" | "loading" | "ready" | "error" | "closed";
        device: string | null;
        error_code: string | null;
        dependencies_available: boolean;
      };
    };
    web_search: Record<string, unknown>;
    browser: Record<string, unknown>;
  };
}
export interface Skill {
  name: string;
  description?: string;
  digest: string;
  version?: string;
  license?: string;
  modified?: boolean;
}
export interface Fact {
  id: string;
  entity: string;
  entity_type?: string | null;
  target_type?: string | null;
  relation: string;
  value: unknown;
  scope: string;
  status: string;
  version: number;
  observed_at: string;
  evidence: {
    kind: string;
    quote: string;
    verified: boolean;
    label?: string;
    message_id?: string;
  }[];
}
export interface Job {
  id: string;
  kind: string;
  prompt: string;
  session_id: string;
  enabled: boolean;
  config: Record<string, unknown>;
  next_fire: number | null;
  last_fire: number | null;
}
