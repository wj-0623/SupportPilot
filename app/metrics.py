from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "supportpilot_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "supportpilot_http_request_duration_seconds", "HTTP request latency", ["method", "path"]
)
CHAT_REQUESTS = Counter("supportpilot_chat_requests_total", "Chat requests", ["intent", "mode"])
HANDOFFS = Counter("supportpilot_handoffs_total", "Human handoff tickets created", ["priority"])
AGENT_RUNS = Counter(
    "supportpilot_agent_runs_total", "Persisted agent runs", ["status", "intent", "mode"]
)
NODE_LATENCY = Histogram(
    "supportpilot_agent_node_duration_seconds", "Agent graph node latency", ["node"]
)
TOOL_CALLS = Counter("supportpilot_agent_tool_calls_total", "Agent tool calls", ["tool"])
FEEDBACK = Counter("supportpilot_feedback_total", "Customer feedback", ["rating", "reason"])
ACTION_EXECUTIONS = Counter(
    "supportpilot_action_executions_total",
    "Commerce action outcomes",
    ["action", "status", "provider"],
)
CONNECTOR_LATENCY = Histogram(
    "supportpilot_connector_duration_seconds", "Commerce connector latency", ["action", "provider"]
)
KNOWLEDGE_RETRIEVALS = Counter(
    "supportpilot_knowledge_retrievals_total", "Knowledge retrieval results", ["grounded"]
)
AUDIT_EVENTS = Counter("supportpilot_audit_events_total", "Persisted API audit events", ["status"])
OUTBOUND_DELIVERIES = Counter(
    "supportpilot_outbound_deliveries_total",
    "Customer-facing channel delivery outcomes",
    ["status", "provider"],
)
