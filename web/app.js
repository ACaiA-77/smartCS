const state = {
  user: null,
  sessionId: null,
  pending: null,
  sending: false,
  authBusy: false,
  authOperation: 0,
  epoch: 0,
  view: 0,
};

function readSaved(key) {
  try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; }
}

function saveSession() {
  if (!state.user) return;
  try {
    sessionStorage.setItem(`smartcs.account.${state.user.account_id}`, JSON.stringify({
      sessionId: state.sessionId, pending: state.pending,
    }));
  } catch { /* Storage-disabled browsers still support the current page session. */ }
}

const elements = {
  loginPage: document.querySelector("#login-page"),
  loginForm: document.querySelector("#login-form"),
  loginButton: document.querySelector("#login-button"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  authMessage: document.querySelector("#auth-message"),
  appShell: document.querySelector("#app-shell"),
  accountName: document.querySelector("#account-name"),
  sessionList: document.querySelector("#session-list"),
  chatForm: document.querySelector("#chat-form"),
  input: document.querySelector("#message-input"),
  messageList: document.querySelector("#message-list"),
  conversationScroll: document.querySelector("#conversation-scroll"),
  sendButton: document.querySelector("#send-button"),
  sessionName: document.querySelector("#session-name"),
  orderList: document.querySelector("#order-list"),
  orderDetail: document.querySelector("#order-detail"),
  connectionDot: document.querySelector("#connection-dot"),
  connectionText: document.querySelector("#connection-text"),
  orderCount: document.querySelector("#order-count"),
};

function clearSavedAccounts(keepAccount = null) {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith("smartcs.") && key !== `smartcs.account.${keepAccount}`) sessionStorage.removeItem(key);
    }
  } catch { /* No persistent state is required to sign out. */ }
}

function clearConversation() {
  state.view += 1;
  state.sessionId = null;
  state.pending = null;
  elements.input.value = "";
  elements.messageList.innerHTML = "";
  elements.sessionName.textContent = "新客服咨询";
  elements.orderDetail.className = "empty-detail";
  elements.orderDetail.innerHTML = "<p>选择你的订单后，详细信息会显示在这里。</p>";
}

function setAuthView(authenticated) {
  if (document.body) document.body.dataset.authState = authenticated ? "authenticated" : "unauthenticated";
  elements.loginPage.hidden = authenticated;
  elements.appShell.hidden = !authenticated;
  if (elements.loginPage.style) elements.loginPage.style.display = authenticated ? "none" : "";
  if (elements.appShell.style) elements.appShell.style.display = authenticated ? "" : "none";
  elements.loginPage.setAttribute("aria-hidden", authenticated ? "true" : "false");
  elements.appShell.setAttribute("aria-hidden", authenticated ? "false" : "true");
}

function clearIdentity(message = "请登录后继续。", purgeSaved = true) {
  state.epoch += 1;
  state.user = null;
  clearConversation();
  if (purgeSaved) clearSavedAccounts();
  elements.sessionList.innerHTML = "";
  elements.orderList.innerHTML = "";
  elements.orderCount.textContent = "";
  elements.accountName.textContent = "";
  elements.password.value = "";
  elements.username.value = "";
  setAuthView(false);
  elements.authMessage.textContent = message;
  setSending(false);
}

// A late response must never repopulate a signed-out page or another account/view.
class StaleResponse extends Error {}
function assertCurrent(epoch, view = state.view) {
  if (epoch !== state.epoch || view !== state.view) throw new StaleResponse();
}

async function request(url, options = {}) {
  const epoch = state.epoch;
  const response = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  assertCurrent(epoch);
  if (response.status === 401) clearIdentity("登录已失效，请重新登录。");
  if (!response.ok) {
    const error = new Error(typeof payload.detail === "string" ? payload.detail : "请求失败，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return payload;
}

function jsonPost(body = {}) {
  return {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
}

function setAuthBusy(busy) {
  state.authBusy = busy;
  elements.loginButton.disabled = busy;
  elements.username.disabled = busy;
  elements.password.disabled = busy;
}

async function activateUser(user) {
  if (state.user && String(state.user.account_id) !== String(user.account_id)) clearIdentity();
  state.epoch += 1;
  clearConversation();
  clearSavedAccounts(user.account_id);
  state.user = user;
  const saved = readSaved(`smartcs.account.${user.account_id}`);
  state.sessionId = typeof saved?.sessionId === "string" ? saved.sessionId : null;
  state.pending = state.sessionId && typeof saved?.pending?.client_request_id === "string" ? saved.pending : null;
  elements.accountName.textContent = user.username;
  elements.password.value = "";
  setAuthView(true);
  const epoch = state.epoch;
  setSending(true);
  await Promise.all([loadSessions(), loadDemoOrders(), restoreSession()]);
  if (epoch === state.epoch) setSending(false);
}

async function initializeAuth() {
  const operation = ++state.authOperation;
  setAuthBusy(true);
  try {
    await activateUser(await request("/api/auth/me"));
  } catch (error) {
    if (!(error instanceof StaleResponse) && operation === state.authOperation) {
      clearIdentity(error.status === 401 ? "请输入你的账户信息。" : "无法连接服务，请稍后登录重试。", error.status === 401);
    }
  } finally { if (operation === state.authOperation) setAuthBusy(false); }
}

async function login() {
  if (state.authBusy) return;
  const operation = ++state.authOperation;
  setAuthBusy(true);
  elements.authMessage.textContent = "正在登录…";
  try {
    const payload = await request("/api/auth/login", jsonPost({
      username: elements.username.value.trim(), password: elements.password.value,
    }));
    await activateUser(payload.user);
  } catch (error) {
    if (!(error instanceof StaleResponse)) elements.authMessage.textContent = error.status === 401 ? "用户名或密码不正确，或账户不可用。" : error.message;
  } finally {
    if (operation === state.authOperation) { elements.password.value = ""; setAuthBusy(false); }
  }
}

async function logout() {
  if (state.user && (state.sending || state.pending) && !window.confirm("当前仍有未完成的请求。退出不会取消服务端处理，之后可从历史会话继续；尚未送达的消息将不再重试。确定退出吗？")) return;
  const operation = ++state.authOperation;
  setAuthBusy(true);
  clearIdentity("正在退出登录…");
  try {
    await request("/api/auth/logout", jsonPost());
    elements.authMessage.textContent = "已退出登录。";
    elements.loginButton.textContent = "登录";
    elements.loginButton.type = "submit";
    delete elements.loginButton.dataset.retryLogout;
  } catch (error) {
    elements.authMessage.textContent = error.status === 401 ? "已退出登录。" : "页面数据已清除，但退出请求失败。请重试退出。";
    // Keep login blocked until cookie clearing succeeds; don't claim server logout on network failure.
    if (error.status !== 401) {
      elements.loginButton.textContent = "重试退出";
      elements.loginButton.type = "button";
      elements.loginButton.dataset.retryLogout = "true";
    }
  } finally { if (operation === state.authOperation) setAuthBusy(false); }
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
  saveSession();
  elements.sessionList.querySelectorAll("[data-session-id]").forEach(button => {
    button.setAttribute("aria-current", button.dataset.sessionId === state.sessionId ? "true" : "false");
  });
}

function updateInputPlaceholder() {
  elements.input.placeholder = window.matchMedia("(max-width: 760px)").matches
    ? "输入问题，或选择你的订单"
    : "输入问题，例如：我想查询订单物流";
}

function formatDuration(milliseconds) {
  if (milliseconds >= 1000) {
    return `${(milliseconds / 1000).toFixed(1)} 秒`;
  }
  return `${Math.round(milliseconds)} 毫秒`;
}

function formatIntent(intent) {
  const labels = {
    conversation: "客服对话",
    knowledge_rag: "知识库问答",
    order_query: "订单查询",
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
  elements.sendButton.disabled = sending || !state.user;
  elements.input.disabled = sending || !state.user;
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
    return `未找到订单 ${order.order_id}。请从我的订单中选择，或核对订单号。`;
  }
  const tracking = order.tracking_number
    ? `${order.courier_company}，运单号 ${order.tracking_number}`
    : "暂未发货";
  return [
    "订单查询结果",
    `订单号：${order.order_id}`,
    `状态：${order.status_label}`,
    `支付：${order.payment_status_label}`,
    `实付金额：${order.pay_amount || order.amount} 元`,
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
      <div><span>实付金额</span><strong>${escapeHtml(order.pay_amount || order.amount)} 元</strong></div>
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
  if (!state.user) return;
  const epoch = state.epoch;
  elements.orderList.innerHTML = '<p class="loading-row">正在载入订单...</p>';
  try {
    const payload = await request("/api/demo/orders?limit=6");
    assertCurrent(epoch);
    elements.orderCount.textContent = `${payload.orders.length} 笔`;
    elements.orderList.innerHTML = payload.orders.map((order) => `
      <button class="order-button" type="button" data-order-id="${escapeHtml(order.order_id)}">
        <span>
          <span class="order-id">${escapeHtml(order.order_id)}</span>
          <span class="order-meta">实付 ${escapeHtml(order.pay_amount)} 元</span>
        </span>
        <span class="status-label ${escapeHtml(order.status)}">${escapeHtml(order.status_label)}</span>
      </button>`).join("") || '<p class="loading-row">当前账户暂无订单。</p>';
    elements.orderList.querySelectorAll("[data-order-id]").forEach((button) => {
      button.addEventListener("click", () => queryDemoOrder(button.dataset.orderId));
    });
  } catch (error) {
    if (epoch !== state.epoch || error instanceof StaleResponse) return;
    elements.orderList.innerHTML = '<p class="loading-row">订单载入失败，请刷新页面后重试。</p>';
  }
}

async function loadSessions() {
  if (!state.user) return;
  const epoch = state.epoch;
  try {
    const payload = await request("/api/sessions");
    assertCurrent(epoch);
    elements.sessionList.innerHTML = payload.sessions.map(session => `
      <div class="history-row">
        <button class="session-select" type="button" data-session-id="${escapeHtml(session.session_id)}">${escapeHtml(session.title || "新客服咨询")}</button>
        <button class="session-delete" type="button" data-delete-session="${escapeHtml(session.session_id)}" aria-label="删除会话：${escapeHtml(session.title || "新客服咨询")}" title="删除会话">×</button>
      </div>`).join("") || '<p class="history-empty">暂无历史会话</p>';
    const selected = payload.sessions.find(session => session.session_id === state.sessionId);
    if (selected) elements.sessionName.textContent = selected.title || "新客服咨询";
    elements.sessionList.querySelectorAll("[data-session-id]").forEach(button => {
      button.addEventListener("click", () => selectSession(button.dataset.sessionId));
    });
    elements.sessionList.querySelectorAll("[data-delete-session]").forEach(button => {
      button.addEventListener("click", () => deleteSession(button.dataset.deleteSession));
    });
    updateSessionUi();
  } catch (error) {
    if (epoch === state.epoch && !(error instanceof StaleResponse)) elements.sessionList.innerHTML = '<p class="history-empty">历史载入失败，请点击刷新。</p>';
  }
}

async function ensureSession() {
  if (state.sessionId) return;
  const epoch = state.epoch, view = state.view;
  const session = await request("/api/sessions", jsonPost());
  assertCurrent(epoch, view);
  state.sessionId = session.session_id;
  updateSessionUi();
}

async function selectSession(sessionId) {
  if (!state.user || state.sending) return;
  if (state.pending) { showResumeButton(); return; }
  clearConversation();
  state.sessionId = sessionId;
  updateSessionUi();
  const epoch = state.epoch, view = state.view;
  setSending(true);
  await restoreSession();
  if (epoch === state.epoch) setSending(false);
}

async function deleteSession(sessionId) {
  if (!state.user || state.sending || state.pending) return;
  if (!window.confirm("删除这条会话及其恢复记录？此操作无法撤销。")) return;
  const epoch = state.epoch, view = state.view;
  setSending(true);
  try {
    await request(`/api/sessions/${encodeURIComponent(sessionId)}`, {method: "DELETE"});
    assertCurrent(epoch, view);
    if (sessionId === state.sessionId) { clearConversation(); updateSessionUi(); }
    await loadSessions();
  } catch (error) {
    if (epoch === state.epoch && !(error instanceof StaleResponse)) appendMessage("assistant", `删除未完成：${error.message}`);
  } finally { if (epoch === state.epoch) setSending(false); }
}

async function queryDemoOrder(orderId) {
  if (!state.user || state.sending || state.pending) return;
  const epoch = state.epoch, view = state.view;
  setSending(true);
  let loading;
  try {
    await ensureSession();
    assertCurrent(epoch, view);
    appendMessage("user", `查询订单 ${orderId}`);
    loading = appendMessage("assistant loading", "正在查询订单…");
    const payload = await request("/api/tools/call", jsonPost({
        name: "order_query",
        arguments: { order_id: orderId },
        session_id: state.sessionId,
    }));
    assertCurrent(epoch, view);
    loading.remove();
    if (!payload.success) throw new Error(payload.error || "查询失败");
    renderOrderDetail(payload.result);
    appendMessage("assistant", formatOrderResult(payload.result), "订单查询");
    await loadSessions();
  } catch (error) {
    if (epoch !== state.epoch || view !== state.view || error instanceof StaleResponse) return;
    loading?.remove();
    appendMessage("assistant", `订单查询失败：${error.message}`);
  } finally { if (epoch === state.epoch && view === state.view) setSending(false); }
}

async function sendChatMessage(message, retry = false) {
  if (!state.user || state.sending || !message.trim()) return;
  if (state.pending && !retry) { showResumeButton(); return; }
  const epoch = state.epoch, view = state.view;
  setSending(true);
  const startedAt = performance.now();
  let loading;
  try {
    await ensureSession();
    assertCurrent(epoch, view);
    if (!state.pending) {
      state.pending = {client_request_id: crypto.randomUUID(), message: message.trim()};
      saveSession();
    }
    appendMessage("user", message.trim(), "当前会话");
    elements.input.value = "";
    loading = appendMessage("assistant loading", "正在处理你的问题…");
    const payload = await request("/api/chat", jsonPost({
        message: message.trim(),
        session_id: state.sessionId,
        client_request_id: state.pending.client_request_id,
    }));
    assertCurrent(epoch, view);
    loading.remove();
    state.pending = null;
    saveSession();
    elements.messageList.querySelector("[data-resume]")?.closest("article")?.remove();
    appendMessage(
      "assistant",
      payload.response,
      `${formatIntent(payload.intent)} · ${formatDuration(performance.now() - startedAt)}`,
    );
    await loadSessions();
  } catch (error) {
    if (epoch !== state.epoch || view !== state.view || error instanceof StaleResponse) return;
    loading?.remove();
    appendMessage("assistant", `本次请求未完成：${error.message}`, "请求错误");
    if (state.pending) showResumeButton();
  } finally {
    if (epoch === state.epoch && view === state.view) { setSending(false); elements.input.focus(); }
  }
}

function resetSession() {
  if (!state.user || state.sending) return;
  if (state.pending) { showResumeButton(); return; }
  clearConversation();
  updateSessionUi();
  refreshIcons();
  elements.input.focus();
}

function showResumeButton() {
  if (elements.messageList.querySelector("[data-resume]")) return;
  const node = appendMessage("assistant", "有一条请求尚未完成。继续处理会恢复已保存步骤，不会自动确认新的退款。", "请求已保留");
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.resume = "true";
  button.textContent = "继续处理";
  button.addEventListener("click", resumeChat);
  node.querySelector(".message-bubble").append(button);
}

async function resumeChat() {
  if (!state.user || state.sending || !state.pending || !state.sessionId) return;
  const session = state.sessionId;
  const epoch = state.epoch, view = state.view;
  setSending(true);
  try {
    try {
      await request(`/api/checkpoints/${encodeURIComponent(session)}/resume`, jsonPost({client_request_id: state.pending.client_request_id}));
    } catch (error) {
      assertCurrent(epoch, view);
      if (![404, 409].includes(error.status)) throw error;
      // A disconnected initial POST may never have reached the server. Query before explicit retry.
      let checkpoint = null;
      try { checkpoint = await request(`/api/checkpoints/${encodeURIComponent(session)}`); }
      catch (statusError) { if (statusError.status !== 404) throw statusError; }
      assertCurrent(epoch, view);
      const missingRequest = !checkpoint || (checkpoint.status !== "running" && checkpoint.client_request_id !== state.pending.client_request_id);
      if (missingRequest && state.pending.message) {
        setSending(false);
        await sendChatMessage(state.pending.message, true);
        return;
      }
      throw error;
    }
    assertCurrent(epoch, view);
    state.pending = null;
    saveSession();
    await restoreSession();
  } catch (error) {
    if (epoch !== state.epoch || view !== state.view || error instanceof StaleResponse) return;
    appendMessage("assistant", `恢复未完成：${error.message}`, "断点已保留");
  } finally { if (epoch === state.epoch && view === state.view) setSending(false); }
}

async function restoreSession() {
  if (!state.user || !state.sessionId) return;
  const epoch = state.epoch, view = state.view, session = state.sessionId;
  try {
    const payload = await request(`/api/sessions/${encodeURIComponent(session)}`);
    assertCurrent(epoch, view);
    elements.sessionName.textContent = payload.title || "客服咨询";
    elements.messageList.innerHTML = "";
    for (const message of payload.messages) appendMessage(message.role, message.content, "已保存会话");
    let cp;
    try { cp = await request(`/api/checkpoints/${encodeURIComponent(session)}`); }
    catch (error) { if (error.status !== 404) throw error; }
    assertCurrent(epoch, view);
    if (cp) {
      if (cp.status === "running") {
        if (!state.pending) state.pending = {client_request_id: cp.client_request_id};
      } else if (state.pending?.client_request_id === cp.client_request_id) {
        state.pending = null;
      }
      saveSession();
    }
    if (state.pending) showResumeButton();
  } catch (error) {
    if (epoch !== state.epoch || view !== state.view || error instanceof StaleResponse) return;
    if ([403, 404].includes(error.status)) { clearConversation(); updateSessionUi(); }
    appendMessage("assistant", "暂时无法读取这条会话，请刷新历史后重试。");
    if (state.pending) showResumeButton();
  }
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

elements.loginForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (elements.loginButton.dataset.retryLogout) return;
  login();
});
elements.loginButton.addEventListener("click", () => {
  if (elements.loginButton.dataset.retryLogout) logout();
});
document.querySelector("#logout-button").addEventListener("click", logout);
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
document.querySelector("#history-button").addEventListener("click", loadSessions);
document.querySelector("#refresh-orders").addEventListener("click", loadDemoOrders);

setSending(false);
updateInputPlaceholder();
refreshIcons();
checkHealth();
initializeAuth();
window.addEventListener("resize", updateInputPlaceholder);
