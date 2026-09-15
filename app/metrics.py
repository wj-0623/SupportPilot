from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "shopsage_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "shopsage_http_request_duration_seconds", "HTTP request latency", ["method", "path"]
)
CHAT_REQUESTS = Counter("shopsage_chat_requests_total", "Chat requests", ["intent", "mode"])
HANDOFFS = Counter("shopsage_handoffs_total", "Human handoff tickets created", ["priority"])
AGENT_RUNS = Counter(
    "shopsage_agent_runs_total", "Persisted agent runs", ["status", "intent", "mode"]
)
NODE_LATENCY = Histogram(
    "shopsage_agent_node_duration_seconds", "Agent graph node latency", ["node"]
)
TOOL_CALLS = Counter("shopsage_agent_tool_calls_total", "Agent tool calls", ["tool"])
FEEDBACK = Counter("shopsage_feedback_total", "Customer feedback", ["rating", "reason"])
ACTION_EXECUTIONS = Counter(
    "shopsage_action_executions_total", "Commerce action outcomes", ["action", "status", "provider"]
)
CONNECTOR_LATENCY = Histogram(
    "shopsage_connector_duration_seconds", "Commerce connector latency", ["action", "provider"]
)
KNOWLEDGE_RETRIEVALS = Counter(
    "shopsage_knowledge_retrievals_total", "Knowledge retrieval results", ["grounded"]
)
