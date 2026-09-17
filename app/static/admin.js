const adminState = { templates: [], token: "", key: "" };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

function authHeaders(json = true) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  if (adminState.token) headers.Authorization = `Bearer ${adminState.token}`;
  if (adminState.key) headers["X-Admin-Key"] = adminState.key;
  return headers;
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

const record = (title, subtitle, buttons = "") => `<article class="record"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small>${buttons}</article>`;

async function loadQuality() {
  const data = await api("/api/v1/ops/quality");
  const metrics = [
    ["样本", data.sample_size], ["错误率", `${(data.error_rate * 100).toFixed(1)}%`],
    ["确认解决率", data.confirmed_resolution_rate == null ? "待采集" : `${(data.confirmed_resolution_rate * 100).toFixed(1)}%`],
    ["动作成功率", data.action_success_rate == null ? "待采集" : `${(data.action_success_rate * 100).toFixed(1)}%`],
    ["知识有据率", data.grounded_answer_rate == null ? "待采集" : `${(data.grounded_answer_rate * 100).toFixed(1)}%`],
    ["P95", `${data.p95_latency_ms} ms`],
  ];
  $("#quality").innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
}

async function loadPacks() {
  const [templates, packs] = await Promise.all([api("/api/v1/admin/domain-packs/templates"), api("/api/v1/admin/domain-packs")]);
  adminState.templates = templates;
  $("#templateSelect").innerHTML = templates.map((item) => `<option value="${escapeHtml(item.slug)}">${escapeHtml(item.name)}</option>`).join("");
  $("#packs").innerHTML = packs.map((item) => record(`${item.config.name} · v${item.version}`, `${item.status} · ${item.checksum.slice(0, 12)}`, item.status === "draft" ? `<button data-publish="${item.id}">发布</button>` : "")).join("") || "暂无修订";
}

async function loadConnectors() {
  const connectors = await api("/api/v1/admin/connectors");
  $("#connectors").innerHTML = connectors.map((item) => record(item.name, `${item.id} · ${item.provider} · ${item.status} · ${item.capabilities.join(", ")}`)).join("") || "暂无连接器";
}

async function loadTickets() {
  const tickets = await api("/api/v1/tickets?status=open");
  $("#tickets").innerHTML = tickets.map((item) => record(
    item.id,
    `${item.priority} · ${item.reason}`,
    `<button data-reply="${item.id}">回复</button><button data-resume="${item.conversation_id}">恢复自动</button><button data-resolve="${item.id}">解决</button>`,
  )).join("") || "当前没有待处理工单";
}

async function loadOutbound() {
  const messages = await api("/api/v1/ops/outbound-messages?limit=30");
  $("#outbound").innerHTML = messages.map((item) => record(
    item.id,
    `${item.status} · 尝试 ${item.attempts} 次${item.error_code ? ` · ${item.error_code}` : ""}`,
    item.status === "dead_letter" ? `<button data-retry="${item.id}">重试</button>` : "",
  )).join("") || "当前没有外发消息";
}

async function refresh() {
  try {
    await Promise.all([loadQuality(), loadPacks(), loadConnectors(), loadTickets(), loadOutbound()]);
    $("#status").textContent = "已连接";
  } catch (error) { $("#status").textContent = `连接失败：${error.message}`; }
}

$("#bearerToken").value = adminState.token;
$("#connect").addEventListener("click", () => {
  adminState.token = $("#bearerToken").value.trim();
  adminState.key = $("#adminKey").value.trim();
  refresh();
});
$("#refreshPacks").addEventListener("click", loadPacks);
$("#refreshConnectors").addEventListener("click", loadConnectors);
$("#refreshTickets").addEventListener("click", loadTickets);
$("#refreshOutbound").addEventListener("click", loadOutbound);
$("#outbound").addEventListener("click", async (event) => {
  const id = event.target.dataset.retry;
  if (!id) return;
  await api(`/api/v1/ops/outbound-messages/${id}/retry`, { method: "POST" });
  await loadOutbound();
});
$("#tickets").addEventListener("click", async (event) => {
  const replyId = event.target.dataset.reply;
  const resumeId = event.target.dataset.resume;
  const resolveId = event.target.dataset.resolve;
  if (replyId) {
    const content = window.prompt("输入给顾客的回复");
    if (!content) return;
    await api(`/api/v1/tickets/${replyId}/reply`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ content }),
    });
  } else if (resumeId) {
    await api(`/api/v1/conversations/${resumeId}/automation`, {
      method: "PATCH", body: JSON.stringify({ state: "auto" }),
    });
  } else if (resolveId) {
    const resolution = window.prompt("输入处理结论");
    if (!resolution) return;
    await api(`/api/v1/tickets/${resolveId}`, {
      method: "PATCH", body: JSON.stringify({ status: "resolved", resolution }),
    });
  }
  await Promise.all([loadTickets(), loadOutbound()]);
});
$("#createDraft").addEventListener("click", async () => {
  const template = adminState.templates.find((item) => item.slug === $("#templateSelect").value);
  if (!template) return;
  await api("/api/v1/admin/domain-packs", { method: "POST", body: JSON.stringify(template.config) });
  await loadPacks();
});
$("#packs").addEventListener("click", async (event) => {
  const id = event.target.dataset.publish;
  if (!id) return;
  await api(`/api/v1/admin/domain-packs/${id}/publish`, { method: "POST" });
  await loadPacks();
});
$("#knowledgeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.target);
  try {
    const source = await api("/api/v1/admin/knowledge", { method: "POST", body: JSON.stringify({ source_key: data.get("source_key"), content: data.get("content"), metadata: { title: data.get("title") } }) });
    $("#knowledgeResult").textContent = source.injection_flags.length ? "检测到提示注入，禁止发布。" : `Draft v${source.version} 已保存，等待审批发布。`;
  } catch (error) { $("#knowledgeResult").textContent = error.message; }
});

$("#mappingForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try {
    const mapping = await api("/api/v1/admin/customer-mappings", {
      method: "POST", body: JSON.stringify(data),
    });
    $("#mappingResult").textContent = `已映射到 ${mapping.customer_id}`;
    event.target.reset();
  } catch (error) { $("#mappingResult").textContent = error.message; }
});

$("#memberForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try {
    const member = await api("/api/v1/admin/members", {
      method: "POST", body: JSON.stringify({ ...data, active: true }),
    });
    $("#memberResult").textContent = `已保存 ${member.subject}`;
    event.target.reset();
  } catch (error) { $("#memberResult").textContent = error.message; }
});
