const state = {
  sessionId: createSessionId(),
  sending: false,
};

const elements = {
  chatForm: document.querySelector("#chat-form"),
  input: document.querySelector("#message-input"),
  messageList: document.querySelector("#message-list"),
  conversationScroll: document.querySelector("#conversation-scroll"),
  sendButton: document.querySelector("#send-button"),
  sessionId: document.querySelector("#session-id"),
  sessionName: document.querySelector("#session-name"),
  orderList: document.querySelector("#order-list"),
  orderDetail: document.querySelector("#order-detail"),
  connectionDot: document.querySelector("#connection-dot"),
  connectionText: document.querySelector("#connection-text"),
  intent: document.querySelector("#intent-value"),
  compliance: document.querySelector("#compliance-value"),
};

function createSessionId() {
  return `web_${crypto.getRandomValues(new Uint32Array(1))[0].toString(16).slice(0, 8)}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    "\"": "&quot;",
  }[character]));
}

function renderInlineMarkdown(value) {
  return value
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function renderMarkdown(content) {
  const lines = escapeHtml(content).replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let listType = null;

  function closeList() {
    if (listType) {
      blocks.push(`</${listType}>`);
      listType = null;
    }
  }

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      closeList();
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      blocks.push(`<h${heading[1].length}>${renderInlineMarkdown(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) {
      closeList();
      blocks.push("<hr />");
      continue;
    }
    const orderedItem = line.match(/^\d+\.\s+(.+)$/);
    const unorderedItem = line.match(/^[-*]\s+(.+)$/);
    if (orderedItem || unorderedItem) {
      const nextListType = orderedItem ? "ol" : "ul";
      if (listType && listType !== nextListType) closeList();
      if (!listType) {
        blocks.push(`<${nextListType}>`);
        listType = nextListType;
      }
      blocks.push(`<li>${renderInlineMarkdown((orderedItem || unorderedItem)[1])}</li>`);
      continue;
    }
    if (line.startsWith("&gt;")) {
      closeList();
      blocks.push(`<blockquote>${renderInlineMarkdown(line.replace(/^&gt;\s?/, ""))}</blockquote>`);
      continue;
    }
    closeList();
    blocks.push(`<p>${renderInlineMarkdown(line)}</p>`);
  }
  closeList();
  return blocks.join("") || "<p>暂无内容。</p>";
}

function updateSessionUi() {
  elements.sessionId.textContent = state.sessionId;
  elements.sessionName.textContent = `会话 ${state.sessionId.slice(-4)}`;
}

function updateInputPlaceholder() {
  elements.input.placeholder = window.matchMedia("(max-width: 760px)").matches
    ? "输入问题，例如：查询演示订单"
    : "输入问题，例如：查询订单 ORD-20260801-0001";
}

function formatDuration(milliseconds) {
  if (milliseconds >= 1000) {
    return `${(milliseconds / 1000).toFixed(1)} 秒`;
  }
  return `${Math.round(milliseconds)} 毫秒`;
}

function formatIntent(intent) {
  const labels = {
    knowledge_rag: "知识库问答",
    ticket_handler: "工单处理",
    compliance_checker: "合规审查",
  };
  return labels[intent] || intent || "智能客服";
}

function scrollToLatest() {
  elements.conversationScroll.scrollTop = elements.conversationScroll.scrollHeight;
}

function appendMessage(role, content, metadata = "") {
  const node = document.createElement("article");
  node.className = `message ${role}`;
  node.innerHTML = `
    <div class="message-avatar">${role === "user" ? "你" : "AI"}</div>
    <div>
      <div class="message-bubble"></div>
      <div class="message-meta"></div>
    </div>`;
  const bubble = node.querySelector(".message-bubble");
  if (role.startsWith("assistant")) {
    bubble.classList.add("markdown-content");
    bubble.innerHTML = renderMarkdown(content);
  } else {
    bubble.textContent = content;
  }
  node.querySelector(".message-meta").textContent = metadata;
  elements.messageList.append(node);
  scrollToLatest();
  return node;
}

function setSending(sending) {
  state.sending = sending;
  elements.sendButton.disabled = sending;
  elements.input.disabled = sending;
  elements.sendButton.querySelector("span").textContent = sending ? "处理中" : "发送";
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error("health request failed");
    elements.connectionDot.classList.remove("is-offline");
    elements.connectionText.textContent = "服务已连接";
  } catch {
    elements.connectionDot.classList.add("is-offline");
    elements.connectionText.textContent = "服务不可用";
  }
}

function formatOrderResult(order) {
  if (!order.found) {
    return `未找到演示订单 ${order.order_id}。请从右侧列表选择，或输入完整订单号。`;
  }
  const tracking = order.tracking_number
    ? `${order.courier_company}，运单号 ${order.tracking_number}`
    : "暂未发货";
  return [
    `订单查询结果（${order.data_source}）`,
    `订单号：${order.order_id}`,
    `状态：${order.status_label}`,
    `支付：${order.payment_status_label}`,
    `实付金额：${order.amount} 元`,
    `商品：${order.product}`,
    `物流：${tracking}`,
    `售后：${order.after_sale_status_label}`,
    "说明：本地国内电商演示数据，不代表真实平台订单。",
  ].join("\n");
}

function renderOrderDetail(order) {
  if (!order.found) {
    elements.orderDetail.className = "empty-detail";
    elements.orderDetail.innerHTML = `<i data-lucide="circle-alert"></i><p>未找到 ${escapeHtml(order.order_id)}，请选择右侧已有订单。</p>`;
    refreshIcons();
    return;
  }
  elements.orderDetail.className = "order-detail";
  elements.orderDetail.innerHTML = `
    <div class="detail-status">
      <strong>${escapeHtml(order.order_id)}</strong>
      <span class="status-label ${escapeHtml(order.status)}">${escapeHtml(order.status_label)}</span>
    </div>
    <div class="detail-grid">
      <div><span>实付金额</span><strong>${escapeHtml(order.amount)} 元</strong></div>
      <div><span>支付状态</span><strong>${escapeHtml(order.payment_status_label)}</strong></div>
      <div><span>物流公司</span><strong>${escapeHtml(order.courier_company || "暂未发货")}</strong></div>
      <div><span>售后状态</span><strong>${escapeHtml(order.after_sale_status_label)}</strong></div>
      <div><span>收货城市</span><strong>${escapeHtml(order.city)}</strong></div>
      <div><span>收货人</span><strong>${escapeHtml(order.recipient_name_masked)}</strong></div>
    </div>
    <div><span class="section-label">商品</span><strong>${escapeHtml(order.product)}</strong></div>
    <p class="source-note">${escapeHtml(order.data_source)}。${escapeHtml(order.tracking_number ? `运单号：${order.tracking_number}` : "当前无运单号")}</p>`;
}

async function loadDemoOrders() {
  elements.orderList.innerHTML = '<p class="loading-row">正在载入订单...</p>';
  try {
    const response = await fetch("/api/demo/orders?limit=6");
    if (!response.ok) throw new Error("orders request failed");
    const payload = await response.json();
    elements.orderList.innerHTML = payload.orders.map((order) => `
      <button class="order-button" type="button" data-order-id="${escapeHtml(order.order_id)}">
        <span>
          <span class="order-id">${escapeHtml(order.order_id)}</span>
          <span class="order-meta">实付 ${escapeHtml(order.pay_amount)} 元</span>
        </span>
        <span class="status-label ${escapeHtml(order.status)}">${escapeHtml(order.status_label)}</span>
      </button>`).join("");
    elements.orderList.querySelectorAll("[data-order-id]").forEach((button) => {
      button.addEventListener("click", () => queryDemoOrder(button.dataset.orderId));
    });
  } catch {
    elements.orderList.innerHTML = '<p class="loading-row">订单载入失败，请刷新页面后重试。</p>';
  }
}

async function queryDemoOrder(orderId) {
  appendMessage("user", `直接查询订单 ${orderId}`, "MCP 工具调用");
  const loading = appendMessage("assistant loading", "正在查询本地订单库...");
  try {
    const response = await fetch("/api/tools/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "order_query",
        arguments: { order_id: orderId, user_id: "web_user" },
      }),
    });
    const payload = await response.json();
    loading.remove();
    if (!payload.success) throw new Error(payload.error || "工具调用失败");
    renderOrderDetail(payload.result);
    appendMessage("assistant", formatOrderResult(payload.result), `订单工具 · ${formatDuration(payload.duration_ms)}`);
    elements.intent.textContent = "order_query";
    elements.compliance.textContent = "直接工具调用";
    elements.compliance.className = "";
  } catch (error) {
    loading.remove();
    appendMessage("assistant", `订单工具调用失败：${error.message}`, "工具错误");
  }
}

async function sendChatMessage(message) {
  if (state.sending || !message.trim()) return;
  appendMessage("user", message.trim(), "当前会话");
  elements.input.value = "";
  setSending(true);
  const startedAt = performance.now();
  const loading = appendMessage("assistant loading", "正在理解问题并调用客服流程...");
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message.trim(),
        user_id: "web_user",
        session_id: state.sessionId,
      }),
    });
    const payload = await response.json();
    loading.remove();
    if (!response.ok) throw new Error(payload.detail || "客服服务响应异常");
    appendMessage(
      "assistant",
      payload.response,
      `${formatIntent(payload.intent)} · ${formatDuration(performance.now() - startedAt)}`,
    );
    elements.intent.textContent = payload.intent || "unknown";
    elements.compliance.textContent = payload.compliance_passed ? "已通过" : "已拦截";
    elements.compliance.className = payload.compliance_passed ? "is-pass" : "is-fail";
  } catch (error) {
    loading.remove();
    appendMessage("assistant", `本次请求未完成：${error.message}`, "请求错误");
    elements.compliance.textContent = "请求失败";
    elements.compliance.className = "is-fail";
  } finally {
    setSending(false);
    elements.input.focus();
  }
}

function resetSession() {
  state.sessionId = createSessionId();
  elements.messageList.innerHTML = "";
  elements.orderDetail.className = "empty-detail";
  elements.orderDetail.innerHTML = '<i data-lucide="mouse-pointer-click"></i><p>选择一笔演示订单后，物流、支付与售后信息会显示在这里。</p>';
  elements.intent.textContent = "等待输入";
  elements.compliance.textContent = "未执行";
  elements.compliance.className = "";
  updateSessionUi();
  refreshIcons();
  elements.input.focus();
}

async function showCurrentHistory() {
  try {
    const response = await fetch(`/api/history/${encodeURIComponent(state.sessionId)}`);
    const payload = await response.json();
    appendMessage("assistant", `当前会话已保存 ${payload.messages.length} 条历史记录。`, "会话状态");
  } catch {
    appendMessage("assistant", "暂时无法读取当前会话历史。", "会话状态");
  }
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

elements.chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  sendChatMessage(elements.input.value);
});
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.chatForm.requestSubmit();
  }
});
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => sendChatMessage(button.dataset.prompt));
});
document.querySelectorAll("[data-order-id]").forEach((button) => {
  button.addEventListener("click", () => queryDemoOrder(button.dataset.orderId));
});
document.querySelector("#new-session").addEventListener("click", resetSession);
document.querySelector("#history-button").addEventListener("click", showCurrentHistory);
document.querySelector("#refresh-orders").addEventListener("click", loadDemoOrders);

updateSessionUi();
updateInputPlaceholder();
refreshIcons();
checkHealth();
loadDemoOrders();
window.addEventListener("resize", updateInputPlaceholder);
