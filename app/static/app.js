const state = {
  conversationId: null,
  customerId: sessionStorage.getItem("supportpilot.customerId") || "demo-001",
  apiKey: sessionStorage.getItem("supportpilot.apiKey") || "",
  accessToken: sessionStorage.getItem("supportpilot.accessToken") || "",
  busy: false,
};

const elements = {
  form: document.querySelector("#chatForm"),
  input: document.querySelector("#messageInput"),
  send: document.querySelector("#sendButton"),
  messages: document.querySelector("#messages"),
  suggestions: document.querySelector("#suggestions"),
  modeBadge: document.querySelector("#modeBadge"),
  settingsDialog: document.querySelector("#settingsDialog"),
  settingsForm: document.querySelector("#settingsForm"),
  customerId: document.querySelector("#customerIdInput"),
  apiKey: document.querySelector("#apiKeyInput"),
  accessToken: document.querySelector("#accessTokenInput"),
};

function headers() {
  const result = { "Content-Type": "application/json" };
  if (state.accessToken) result.Authorization = `Bearer ${state.accessToken}`;
  if (state.apiKey) result["X-API-Key"] = state.apiKey;
  return result;
}

function scrollMessages() {
  elements.messages.scrollTo({ top: elements.messages.scrollHeight, behavior: "smooth" });
}

function addFeedbackControls(bubble, messageId) {
  if (!messageId) return;
  const row = document.createElement("div");
  row.className = "feedback-row";
  const label = document.createElement("span");
  label.textContent = "这条回答有帮助吗？";
  row.appendChild(label);
  [["有帮助", 1], ["没帮助", -1]].forEach(([text, rating]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.addEventListener("click", async () => {
      const response = await fetch(`/api/v1/messages/${messageId}/feedback`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({
          customer_id: state.customerId,
          rating,
          resolved: rating === 1,
          reason: rating === -1 ? "other" : null,
        }),
      });
      if (response.ok) {
        row.replaceChildren(Object.assign(document.createElement("span"), { textContent: "谢谢反馈" }));
      } else {
        const payload = await response.json();
        row.replaceChildren(Object.assign(document.createElement("span"), { textContent: payload.detail || "反馈提交失败" }));
      }
    });
    row.appendChild(button);
  });
  bubble.appendChild(row);
}

function addActionControl(bubble, action) {
  if (!action) return;
  const row = document.createElement("div");
  row.className = "feedback-row";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = action.action === "cancel_order" ? "确认取消订单" : "确认执行";
  button.addEventListener("click", async () => {
    button.disabled = true;
    const response = await fetch(`/api/v1/actions/${action.id}/confirm`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ confirmation_digest: action.confirmation_digest }),
    });
    const payload = await response.json();
    row.textContent = response.ok ? "已确认，任务已进入安全执行队列。" : (payload.detail || "确认失败");
  });
  row.appendChild(button);
  bubble.appendChild(row);
}

function addMessage(role, content, data = {}) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  if (role === "assistant") {
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = "S";
    article.appendChild(avatar);
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const paragraph = document.createElement("p");
  paragraph.textContent = content;
  bubble.appendChild(paragraph);

  if (data.citations?.length) {
    const row = document.createElement("div");
    row.className = "citation-row";
    data.citations.forEach((item) => {
      const citation = document.createElement("span");
      citation.className = "citation";
      citation.textContent = `来源 · ${item.title}`;
      row.appendChild(citation);
    });
    bubble.appendChild(row);
  }

  if (data.trace?.length) {
    const toggle = document.createElement("button");
    toggle.className = "trace-toggle";
    toggle.type = "button";
    toggle.textContent = "查看执行轨迹";
    const trace = document.createElement("pre");
    trace.className = "trace";
    trace.textContent = data.trace.join("\n");
    toggle.addEventListener("click", () => trace.classList.toggle("open"));
    bubble.append(toggle, trace);
  }

  if (role === "assistant") addActionControl(bubble, data.pending_action);

  if (role === "assistant") addFeedbackControls(bubble, data.message_id);

  const meta = document.createElement("span");
  meta.className = "message-meta";
  const mode = data.mode ? ` · ${data.mode}` : "";
  meta.textContent = `${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}${mode}`;
  bubble.appendChild(meta);
  article.appendChild(bubble);
  elements.messages.appendChild(article);
  scrollMessages();
  return article;
}

function addTyping() {
  const node = addMessage("assistant", "");
  node.dataset.typing = "true";
  node.querySelector("p").innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  return node;
}

async function sendMessage(text) {
  if (!text.trim() || state.busy) return;
  state.busy = true;
  elements.send.disabled = true;
  addMessage("user", text.trim());
  elements.input.value = "";
  elements.input.style.height = "auto";
  const typing = addTyping();
  try {
    const response = await fetch("/api/v1/chat", {
      method: "POST",
      headers: { ...headers(), "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        customer_id: state.customerId,
        conversation_id: state.conversationId,
        message: text.trim(),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "请求失败");
    state.conversationId = payload.conversation_id;
    typing.remove();
    addMessage("assistant", payload.reply, payload);
  } catch (error) {
    typing.remove();
    addMessage("assistant", `连接失败：${error.message}`);
  } finally {
    state.busy = false;
    elements.send.disabled = false;
    elements.input.focus();
  }
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(elements.input.value);
});

elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

elements.input.addEventListener("input", () => {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 130)}px`;
});

elements.suggestions.addEventListener("click", (event) => {
  if (event.target.matches("button")) sendMessage(event.target.textContent);
});

document.querySelector("#settingsButton").addEventListener("click", () => {
  elements.customerId.value = state.customerId;
  elements.apiKey.value = state.apiKey;
  elements.accessToken.value = state.accessToken;
  elements.settingsDialog.showModal();
});

elements.settingsForm.addEventListener("submit", (event) => {
  if (event.submitter?.value !== "save") return;
  event.preventDefault();
  const nextCustomer = elements.customerId.value.trim() || "demo-001";
  if (nextCustomer !== state.customerId) state.conversationId = null;
  state.customerId = nextCustomer;
  state.apiKey = elements.apiKey.value.trim();
  state.accessToken = elements.accessToken.value.trim();
  sessionStorage.setItem("supportpilot.customerId", state.customerId);
  sessionStorage.setItem("supportpilot.apiKey", state.apiKey);
  sessionStorage.setItem("supportpilot.accessToken", state.accessToken);
  elements.settingsDialog.close();
});

fetch("/health/live")
  .then((response) => response.json())
  .then((health) => {
    elements.modeBadge.classList.add("online");
    elements.modeBadge.lastChild.textContent = health.llm_mode === "openai" ? " Agent 在线" : " 离线演示";
  })
  .catch(() => { elements.modeBadge.lastChild.textContent = " 服务离线"; });
