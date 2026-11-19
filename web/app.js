const WORKSPACE_TEXT = {
  defaultSessionTitle: "新工作流对话",
  desktopPlaceholder: "输入指令（Enter 发送 / Shift+Enter 换行 / Ctrl+K 聚焦）",
  mobilePlaceholder: "输入指令，或点选右侧笔记/实体",
  loginButton: "进入工作台",
  emptyDetailHtml:
    '<i data-lucide="mouse-pointer-click"></i><p>选择上方的笔记或本体实体，可在此以 Markdown / 关系邻接图预览并一键发往对话。</p>',
};

const state = {
  user: null,
  sessionId: null,
  pending: null,
  sending: false,
  authBusy: false,
  authOperation: 0,
  epoch: 0,
  view: 0,
  inspectorTab: "knowledge",
  knowledgeTab: "notes",
  filterQuery: "",
  vaultSearchResults: null,
  vaultSearchQuery: "",
  copilotData: null,
  selectedCopilotKey: null,
};

function readSaved(key) {
  try {
    return JSON.parse(sessionStorage.getItem(key));
  } catch {
    return null;
  }
}

function saveSession() {
  if (!state.user) return;
  try {
    sessionStorage.setItem(
      `smartcs.account.${state.user.account_id}`,
      JSON.stringify({ sessionId: state.sessionId, pending: state.pending }),
    );
  } catch {
    /* Storage-disabled browsers still support the current page session. */
  }
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
  connectionDot: document.querySelector("#connection-dot"),
  connectionText: document.querySelector("#connection-text"),
  quickPrompts: document.querySelector("#quick-prompts"),
  refreshContext: document.querySelector("#refresh-context"),
  badgeSession: document.querySelector("#badge-session"),
  badgeConnectors: document.querySelector("#badge-connectors"),
  badgeTools: document.querySelector("#badge-tools"),
  badgePending: document.querySelector("#badge-pending"),
  pendingTabCount: document.querySelector("#pending-tab-count"),
  pendingPanelBadge: document.querySelector("#pending-panel-badge"),
  copilotConnectorBadge: document.querySelector("#copilot-connector-badge"),
  copilotToolBadge: document.querySelector("#copilot-tool-badge"),
  copilotConnectors: document.querySelector("#copilot-connectors"),
  copilotTools: document.querySelector("#copilot-tools"),
  copilotPendingList: document.querySelector("#copilot-pending-list"),
  copilotItemCount: document.querySelector("#copilot-item-count"),
  copilotItemList: document.querySelector("#copilot-item-list"),
  copilotFilterInput: document.querySelector("#copilot-filter-input"),
  copilotDetail: document.querySelector("#copilot-detail"),
};

function clearSavedAccounts(keepAccount = null) {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith("smartcs.") && key !== `smartcs.account.${keepAccount}`) {
        sessionStorage.removeItem(key);
      }
    }
  } catch {
    /* No persistent state is required to sign out. */
  }
}

function updateTopbarSessionBadge() {
  if (!elements.badgeSession) return;
  elements.badgeSession.textContent = state.sessionId
    ? `session: ${state.sessionId.slice(0, 12)}`
    : "session: new";
}

function clearConversation() {
  state.view += 1;
  state.sessionId = null;
  state.pending = null;
  elements.input.value = "";
  elements.messageList.innerHTML = "";
  elements.sessionName.textContent = WORKSPACE_TEXT.defaultSessionTitle;
  updateTopbarSessionBadge();
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
  state.copilotData = null;
  state.selectedCopilotKey = null;
  state.filterQuery = "";
  if (elements.copilotFilterInput) elements.copilotFilterInput.value = "";
  clearConversation();
  if (purgeSaved) clearSavedAccounts();
  elements.sessionList.innerHTML = "";
  if (elements.copilotConnectors) elements.copilotConnectors.innerHTML = "";
  if (elements.copilotTools) elements.copilotTools.innerHTML = "";
  if (elements.copilotPendingList) elements.copilotPendingList.innerHTML = "";
  if (elements.copilotItemList) elements.copilotItemList.innerHTML = "";
  if (elements.copilotItemCount) elements.copilotItemCount.textContent = "0";
  if (elements.copilotToolBadge) elements.copilotToolBadge.textContent = "";
  if (elements.copilotConnectorBadge) elements.copilotConnectorBadge.textContent = "";
  if (elements.pendingTabCount) {
    elements.pendingTabCount.textContent = "0";
    elements.pendingTabCount.classList.remove("has-items");
  }
  if (elements.copilotDetail) {
    elements.copilotDetail.className = "empty-detail";
    elements.copilotDetail.innerHTML = WORKSPACE_TEXT.emptyDetailHtml;
  }
  elements.accountName.textContent = "";
  elements.password.value = "";
  elements.username.value = "";
  setAuthView(false);
  elements.authMessage.textContent = message;
  setSending(false);
}

function handleUnauthorized() {
  clearIdentity("登录已失效，请重新登录。");
}

function showResumeButton() {
  appendMessage("assistant", "当前会话仍有处理中的请求，请稍候。");
}

class StaleResponse extends Error {}
function assertCurrent(epoch, view = state.view) {
  if (epoch !== state.epoch || view !== state.view) throw new StaleResponse();
}

async function request(url, options = {}) {
  const epoch = state.epoch;
  const response = await fetch(url, { credentials: "same-origin", cache: "no-store", ...options });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
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
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function setAuthBusy(busy) {
  state.authBusy = busy;
  elements.loginButton.disabled = busy;
  elements.username.disabled = busy;
  elements.password.disabled = busy;
}

async function activateUser(user) {
  state.epoch += 1;
  state.user = user;
  clearSavedAccounts(user.account_id);
  clearConversation();
  const saved = readSaved(`smartcs.account.${user.account_id}`);
  state.sessionId = typeof saved?.sessionId === "string" ? saved.sessionId : null;
  state.pending = typeof saved?.pending === "string" ? saved.pending : null;
  updateTopbarSessionBadge();
  elements.accountName.textContent = user.username;
  elements.password.value = "";
  setAuthView(true);
  const epoch = state.epoch;
  setSending(true);
  await Promise.all([loadSessions(), loadCopilotContext(), restoreSession()]);
  if (epoch === state.epoch) setSending(false);
}

async function initializeAuth() {
  const operation = ++state.authOperation;
  setAuthBusy(true);
  try {
    await activateUser(await request("/api/auth/me"));
  } catch (error) {
    if (!(error instanceof StaleResponse) && operation === state.authOperation) {
      clearIdentity(
        error.status === 401 ? "请输入你的账户信息。" : "无法连接服务，请稍后登录重试。",
        error.status === 401,
      );
    }
  } finally {
    if (operation === state.authOperation) setAuthBusy(false);
  }
}

async function login() {
  if (state.authBusy) return;
  const operation = ++state.authOperation;
  setAuthBusy(true);
  elements.authMessage.textContent = "正在登录…";
  try {
    const payload = await request(
      "/api/auth/login",
      jsonPost({
        username: elements.username.value.trim(),
        password: elements.password.value,
      }),
    );
    await activateUser(payload.user);
  } catch (error) {
    if (!(error instanceof StaleResponse)) {
      elements.authMessage.textContent =
        error.status === 401 ? "用户名或密码不正确，或账户不可用。" : error.message;
    }
  } finally {
    if (operation === state.authOperation) {
      elements.password.value = "";
      setAuthBusy(false);
    }
  }
}

async function logout() {
  if (
    state.user &&
    (state.sending || state.pending) &&
    !window.confirm("当前仍有未完成的请求。退出不会取消服务端处理，之后可从历史会话继续；确定退出吗？")
  ) {
    return;
  }
  const operation = ++state.authOperation;
  setAuthBusy(true);
  clearIdentity("正在退出登录…");
  try {
    await request("/api/auth/logout", jsonPost());
    elements.authMessage.textContent = "已退出登录。";
    elements.loginButton.textContent = WORKSPACE_TEXT.loginButton;
    elements.loginButton.type = "submit";
    delete elements.loginButton.dataset.retryLogout;
  } catch (error) {
    elements.authMessage.textContent =
      error.status === 401 ? "已退出登录。" : "页面数据已清除，但退出请求失败。请重试退出。";
    if (error.status !== 401) {
      elements.loginButton.textContent = "重试退出";
      elements.loginButton.type = "button";
      elements.loginButton.dataset.retryLogout = "true";
    }
  } finally {
    if (operation === state.authOperation) setAuthBusy(false);
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) =>
    ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    })[character],
  );
}

function renderInlineMarkdown(value, { interactiveWikilinks = false } = {}) {
  const inlineCode = [];
  let text = value.replace(/`([^`]+)`/g, (_match, code) => {
    const token = `\x00IC${inlineCode.length}\x00`;
    inlineCode.push(`<code>${code}</code>`);
    return token;
  });

  text = text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/\[\[([^\[\]\n]+?)\]\]/g, (_match, inner) => {
      const target = inner.split("|")[0].split("#")[0].trim();
      const label = inner.includes("|") ? inner.split("|").slice(1).join("|").trim() : inner;
      if (
        interactiveWikilinks &&
        target &&
        !target.startsWith("/") &&
        !target.split("/").some((part) => part === ".." || part.startsWith("."))
      ) {
        return `<button class="wikilink-chip" type="button" data-wikilink="${target}">[[${label}]]</button>`;
      }
      return `<span class="wikilink-chip">[[${label}]]</span>`;
    });

  return text.replace(/\x00IC(\d+)\x00/g, (_match, index) => inlineCode[Number(index)] || "");
}

function splitTableRow(line) {
  return line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableSeparator(line) {
  return /^\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?$/.test(line);
}

function renderMarkdown(content, options = {}) {
  const lines = escapeHtml(content).replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let listType = null;
  let inCodeBlock = false;
  let codeLines = [];

  function closeList() {
    if (listType) {
      blocks.push(`</${listType}>`);
      listType = null;
    }
  }

  for (let i = 0; i < lines.length; i += 1) {
    const rawLine = lines[i];
    const line = rawLine.trim();

    if (line.startsWith("```")) {
      closeList();
      if (!inCodeBlock) {
        inCodeBlock = true;
        codeLines = [];
      } else {
        blocks.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
        inCodeBlock = false;
        codeLines = [];
      }
      continue;
    }

    if (inCodeBlock) {
      codeLines.push(rawLine);
      continue;
    }

    if (!line) {
      closeList();
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1].trim())) {
      closeList();
      const headers = splitTableRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim().includes("|") && lines[i].trim() !== "") {
        rows.push(splitTableRow(lines[i].trim()));
        i += 1;
      }
      i -= 1;
      const thead = `<thead><tr>${headers.map((h) => `<th>${renderInlineMarkdown(h, options)}</th>`).join("")}</tr></thead>`;
      const tbody = `<tbody>${rows
        .map((row) => `<tr>${row.map((cell) => `<td>${renderInlineMarkdown(cell, options)}</td>`).join("")}</tr>`)
        .join("")}</tbody>`;
      blocks.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      blocks.push(`<h${heading[1].length}>${renderInlineMarkdown(heading[2], options)}</h${heading[1].length}>`);
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
      blocks.push(`<li>${renderInlineMarkdown((orderedItem || unorderedItem)[1], options)}</li>`);
      continue;
    }
    if (line.startsWith("&gt;")) {
      closeList();
      blocks.push(`<blockquote>${renderInlineMarkdown(line.replace(/^&gt;\s?/, ""), options)}</blockquote>`);
      continue;
    }
    closeList();
    blocks.push(`<p>${renderInlineMarkdown(line, options)}</p>`);
  }

  if (inCodeBlock && codeLines.length) {
    blocks.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
  }
  closeList();
  return blocks.join("") || "<p>暂无内容。</p>";
}

function updateSessionUi() {
  saveSession();
  updateTopbarSessionBadge();
  elements.sessionList.querySelectorAll("[data-session-id]").forEach((button) => {
    button.setAttribute("aria-current", button.dataset.sessionId === state.sessionId ? "true" : "false");
  });
}

function updateInputPlaceholder() {
  elements.input.placeholder = window.matchMedia("(max-width: 760px)").matches
    ? WORKSPACE_TEXT.mobilePlaceholder
    : WORKSPACE_TEXT.desktopPlaceholder;
}

function formatDuration(milliseconds) {
  if (milliseconds >= 1000) return `${(milliseconds / 1000).toFixed(1)} 秒`;
  return `${Math.round(milliseconds)} 毫秒`;
}

function formatIntent(intent) {
  const labels = {
    vault: "Obsidian 笔记",
    wiki: "Wiki 编译/巡检",
    ontology: "本体图谱",
    mail: "邮箱通道",
    github: "GitHub",
    feishu: "飞书通道",
    knowledge: "知识检索",
    general: "Copilot",
    pi: "Copilot",
  };
  return labels[intent] || intent || "Copilot";
}

function scrollToLatest() {
  elements.conversationScroll.scrollTop = elements.conversationScroll.scrollHeight;
}

function appendMessage(role, content, metadata = "") {
  const node = document.createElement("article");
  node.className = `message ${role}`;
  const avatarLabel = role === "user" ? "你" : "CP";
  node.innerHTML = `
    <div class="message-avatar">${escapeHtml(avatarLabel)}</div>
    <div>
      <div class="message-bubble"></div>
      <div class="message-meta"></div>
    </div>`;
  const bubble = node.querySelector(".message-bubble");
  if (role.startsWith("assistant") && !role.includes("loading")) {
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

function setInspectorTab(tab) {
  const allowed = new Set(["knowledge", "connectors", "pending"]);
  state.inspectorTab = allowed.has(tab) ? tab : "knowledge";
  document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
    const active = button.dataset.inspectorTab === state.inspectorTab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  ["knowledge", "connectors", "pending"].forEach((name) => {
    const pane = document.querySelector(`#pane-${name}`);
    if (pane) pane.hidden = name !== state.inspectorTab;
  });
}

function setKnowledgeTab(tab) {
  state.knowledgeTab = tab === "entities" ? "entities" : "notes";
  document.querySelectorAll("[data-knowledge-tab]").forEach((button) => {
    const active = button.dataset.knowledgeTab === state.knowledgeTab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (state.knowledgeTab === "notes" && state.filterQuery.trim()) {
    scheduleVaultSearch(state.filterQuery);
  } else {
    renderCopilotItems();
  }
}

function renderWorkspaceBadges(data) {
  const c = data.connectors || {};
  const enabledCount = ["obsidian", "ontology", "mail", "github", "feishu"].filter((k) => c[k]?.enabled).length;
  if (elements.badgeConnectors) {
    elements.badgeConnectors.textContent = `连接器 ${enabledCount}/5`;
    elements.badgeConnectors.classList.toggle("is-ok", enabledCount > 0);
  }
  const summary = data.tool_summary || { total: 0, read: 0, write: 0 };
  if (elements.badgeTools) {
    elements.badgeTools.textContent = `工具 读${summary.read}/写${summary.write}`;
  }
  const pendingCount = (data.pending_actions || []).length;
  if (elements.badgePending) {
    elements.badgePending.hidden = pendingCount === 0;
    elements.badgePending.textContent = `待确认 ${pendingCount}`;
  }
  if (elements.pendingTabCount) {
    elements.pendingTabCount.textContent = String(pendingCount);
    elements.pendingTabCount.classList.toggle("has-items", pendingCount > 0);
  }
  if (elements.pendingPanelBadge) {
    elements.pendingPanelBadge.textContent = `${pendingCount} 项`;
  }
}

function renderCopilotConnectors(data) {
  if (!elements.copilotConnectors || !elements.copilotToolBadge) return;
  const summary = data.tool_summary || { total: 0, read: 0, write: 0 };
  elements.copilotToolBadge.textContent = `读 ${summary.read} · 写 ${summary.write}`;

  const c = data.connectors || {};
  const enabledCount = ["obsidian", "ontology", "mail", "github", "feishu"].filter((k) => c[k]?.enabled).length;
  if (elements.copilotConnectorBadge) {
    elements.copilotConnectorBadge.textContent = `${enabledCount} / 5 已挂载`;
  }

  const obsidianZones = c.obsidian?.zones || {};
  const sourceSegment = c.obsidian?.source_enabled ? ` · source ${obsidianZones.source || 0}` : "";
  const ragSegment = c.obsidian?.rag_index_enabled ? ` · RAG ${c.obsidian.rag_documents || 0} 片段` : "";
  const zoneText = c.obsidian?.enabled
    ? `wiki ${obsidianZones.wiki || 0} · notes ${obsidianZones.notes || 0} · daily ${obsidianZones.daily || 0} · raw ${obsidianZones.raw || 0}${sourceSegment}${ragSegment}`
    : "未设置 OBSIDIAN_VAULT_PATH / OBSIDIAN_SOURCE_VAULT_PATH";
  const mailAccounts = (c.mail?.accounts || []).join(" / ");
  const rows = [
    {
      name: "Obsidian Vault",
      sub: zoneText,
      enabled: Boolean(c.obsidian?.enabled),
      pill: c.obsidian?.enabled ? `${c.obsidian.total_notes || 0} 篇` : "未配置",
    },
    {
      name: "Ontology 本体",
      sub: c.ontology?.enabled ? `已索引 ${c.ontology.entity_count || 0} 个实体` : "未设置 COPILOT_ONTOLOGY_DB_PATH",
      enabled: Boolean(c.ontology?.enabled),
      pill: c.ontology?.enabled ? "已就绪" : "未配置",
    },
    {
      name: "邮箱 (163 / Gmail)",
      sub: c.mail?.enabled ? `账号通道：${mailAccounts || "已配置"}` : "未配置完整邮箱凭据",
      enabled: Boolean(c.mail?.enabled),
      pill: c.mail?.enabled ? "已挂载" : "未配置",
    },
    {
      name: "GitHub",
      sub: c.github?.enabled ? "通知、Issue / PR 与代码检索" : "未设置 GITHUB_TOKEN",
      enabled: Boolean(c.github?.enabled),
      pill: c.github?.enabled ? "已挂载" : "未配置",
    },
    {
      name: "飞书 Feishu",
      sub: c.feishu?.enabled ? "会话读取、文档检索与审批消息" : "未设置 FEISHU_APP_ID / SECRET",
      enabled: Boolean(c.feishu?.enabled),
      pill: c.feishu?.enabled ? "已挂载" : "未配置",
    },
  ];
  elements.copilotConnectors.innerHTML = rows
    .map(
      (row) => `
    <div class="connector-row">
      <div class="connector-meta">
        <span class="connector-name">${escapeHtml(row.name)}</span>
        <span class="connector-sub">${escapeHtml(row.sub)}</span>
      </div>
      <span class="connector-pill ${row.enabled ? "is-on" : "is-off"}">${escapeHtml(row.pill)}</span>
    </div>`,
    )
    .join("");

  if (elements.copilotTools) {
    const tools = data.tools || [];
    const groups = [
      { key: "read", title: "只读查询（READ）", badge: "安全只读", pillClass: "is-on" },
      { key: "local_write", title: "本地与草稿写入（同轮授权）", badge: "低风险", pillClass: "is-on" },
      { key: "external_two_phase", title: "外部副作用（两段式冻结确认）", badge: "高风险受控", pillClass: "is-warn" },
    ];
    elements.copilotTools.innerHTML =
      groups
        .map((group) => {
          const matched = tools.filter((t) => (t.action_tier || "read") === group.key);
          const chips = matched.length
            ? matched.map((t) => `<span class="tool-chip" title="${escapeHtml(t.description)}">${escapeHtml(t.name)}</span>`).join("")
            : '<span class="loading-row">暂无此类工具</span>';
          return `
            <div class="tier-group">
              <div class="tier-header">
                <span>${escapeHtml(group.title)}</span>
                <span class="connector-pill ${group.pillClass}">${escapeHtml(group.badge)} · ${matched.length}</span>
              </div>
              <div class="tool-chips">${chips}</div>
            </div>`;
        })
        .join("") || '<p class="loading-row">暂无已注册工具。</p>';
  }
}

function renderPendingActions(data) {
  if (!elements.copilotPendingList) return;
  const items = data.pending_actions || [];
  if (!items.length) {
    elements.copilotPendingList.innerHTML = '<p class="loading-row">当前没有待确认的外部操作。</p>';
    return;
  }
  elements.copilotPendingList.innerHTML = items
    .map((item) => {
      const argsPreview = Object.entries(item.arguments_preview || {})
        .map(([k, v]) => `${k}: ${v}`)
        .join(" · ");
      return `
        <article class="pending-card">
          <div class="pending-card-head">
            <span class="pending-tool">${escapeHtml(item.tool)}</span>
            <span class="connector-pill is-warn">待确认</span>
          </div>
          <div class="pending-summary">${escapeHtml(item.target_summary || argsPreview || "外部操作快照已冻结")}</div>
          <div class="pending-meta">ID: ${escapeHtml(item.pending_action_id)}</div>
          <div class="pending-meta">会话: ${escapeHtml(item.session_id || "--")} · 过期: ${escapeHtml(item.expires_at || "--")}</div>
          <div class="detail-actions">
            <button class="primary-action" type="button" data-confirm-action="${escapeHtml(item.pending_action_id)}" data-action-session="${escapeHtml(item.session_id || "")}">确认执行</button>
            <button class="danger-action" type="button" data-cancel-action="${escapeHtml(item.pending_action_id)}" data-action-session="${escapeHtml(item.session_id || "")}">取消</button>
          </div>
        </article>`;
    })
    .join("");

  elements.copilotPendingList.querySelectorAll("[data-confirm-action]").forEach((button) => {
    button.addEventListener("click", () =>
      confirmPendingAction(button.dataset.confirmAction, button.dataset.actionSession),
    );
  });
  elements.copilotPendingList.querySelectorAll("[data-cancel-action]").forEach((button) => {
    button.addEventListener("click", () =>
      cancelPendingAction(button.dataset.cancelAction, button),
    );
  });
}

async function cancelPendingAction(actionId, button) {
  if (!state.user || !actionId) return;
  const epoch = state.epoch;
  const card = button?.closest(".pending-card");
  const cardButtons = card ? Array.from(card.querySelectorAll("button")) : button ? [button] : [];
  cardButtons.forEach((btn) => {
    btn.disabled = true;
  });
  try {
    await request(`/api/copilot/pending/${encodeURIComponent(actionId)}/cancel`, { method: "POST" });
    assertCurrent(epoch);
    await loadCopilotContext();
  } catch (error) {
    if (epoch !== state.epoch || error instanceof StaleResponse) return;
    if (error.status === 404 || error.status === 409) {
      await loadCopilotContext();
      appendMessage("assistant", `该待确认操作当前不可取消（${error.message}），已刷新待确认队列。`);
      return;
    }
    cardButtons.forEach((btn) => {
      btn.disabled = false;
    });
    appendMessage("assistant", `取消待确认操作未完成：${error.message}`);
  }
}

async function confirmPendingAction(actionId, targetSessionId) {
  if (!state.user || state.sending) return;
  if (targetSessionId && state.sessionId !== targetSessionId) {
    await selectSession(targetSessionId);
  }
  await sendChatMessage(`确认执行 ${actionId}`);
}

function renderCopilotItems() {
  if (!elements.copilotItemList || !elements.copilotItemCount) return;
  const data = state.copilotData;
  if (!data) return;
  const needle = state.filterQuery.trim().toLowerCase();

  if (state.knowledgeTab === "entities") {
    const entities = (data.entities || []).filter((entity) => {
      if (!needle) return true;
      return `${entity.name} ${entity.entity_type} ${entity.canonical_key}`.toLowerCase().includes(needle);
    });
    elements.copilotItemCount.textContent = `${entities.length} 项`;
    elements.copilotItemList.innerHTML =
      entities
        .map((entity) => {
          const selected = state.selectedCopilotKey === `entity:${entity.id}` ? " is-selected" : "";
          return `
            <button class="copilot-item${selected}" type="button" data-entity-id="${escapeHtml(entity.id)}">
              <span class="zone-badge">${escapeHtml(entity.entity_type)}</span>
              <span>
                <span class="copilot-item-title">${escapeHtml(entity.name)}</span>
                <span class="copilot-item-sub">${escapeHtml(entity.canonical_key)}</span>
              </span>
            </button>`;
        })
        .join("") || '<p class="loading-row">没有匹配的本体实体。</p>';
    elements.copilotItemList.querySelectorAll("[data-entity-id]").forEach((button) => {
      button.addEventListener("click", () => loadCopilotEntity(button.dataset.entityId));
    });
    return;
  }

  const useServerSearch = Boolean(needle && state.vaultSearchQuery === needle && Array.isArray(state.vaultSearchResults));
  const notes = useServerSearch
    ? state.vaultSearchResults
    : (data.notes || []).filter((note) => {
        if (!needle) return true;
        const tags = (note.tags || []).join(" ");
        return `${note.title || ""} ${note.path || ""} ${note.zone || ""} ${tags}`.toLowerCase().includes(needle);
      });
  const totalNotes = data.connectors?.obsidian?.total_notes || notes.length;
  elements.copilotItemCount.textContent = needle ? `${notes.length} 命中` : `${notes.length} / ${totalNotes} 篇`;
  elements.copilotItemList.innerHTML =
    notes
      .map((note) => {
        const selected = state.selectedCopilotKey === `note:${note.path}` ? " is-selected" : "";
        const matchLabel = note.match_type ? ` · ${note.match_type}` : "";
        return `
          <button class="copilot-item${selected}" type="button" data-note-path="${escapeHtml(note.path)}">
            <span class="zone-badge">${escapeHtml((note.zone || "note") + matchLabel)}</span>
            <span>
              <span class="copilot-item-title">${escapeHtml(note.title || note.path)}</span>
              <span class="copilot-item-sub">${escapeHtml(note.path)}</span>
            </span>
          </button>`;
      })
      .join("") || '<p class="loading-row">没有匹配的 Obsidian 笔记。</p>';
  elements.copilotItemList.querySelectorAll("[data-note-path]").forEach((button) => {
    button.addEventListener("click", () => loadCopilotNote(button.dataset.notePath));
  });
}

let vaultSearchTimer = null;
let vaultSearchController = null;

function scheduleVaultSearch(rawQuery) {
  const query = String(rawQuery || "").trim();
  if (vaultSearchTimer) {
    clearTimeout(vaultSearchTimer);
    vaultSearchTimer = null;
  }
  if (vaultSearchController) {
    vaultSearchController.abort();
    vaultSearchController = null;
  }
  if (!query || !state.user || state.knowledgeTab !== "notes") {
    state.vaultSearchQuery = "";
    state.vaultSearchResults = null;
    renderCopilotItems();
    return;
  }
  const epoch = state.epoch;
  vaultSearchTimer = setTimeout(async () => {
    const controller = new AbortController();
    vaultSearchController = controller;
    try {
      const payload = await request(`/api/copilot/vault/search?query=${encodeURIComponent(query)}&limit=15`, {
        signal: controller.signal,
      });
      if (epoch !== state.epoch || state.filterQuery.trim() !== query) return;
      state.vaultSearchQuery = query.toLowerCase();
      state.vaultSearchResults = payload.hits || [];
      renderCopilotItems();
    } catch (error) {
      if (error?.name === "AbortError" || epoch !== state.epoch) return;
      state.vaultSearchQuery = "";
      state.vaultSearchResults = null;
      renderCopilotItems();
    } finally {
      if (vaultSearchController === controller) {
        vaultSearchController = null;
      }
    }
  }, 280);
}

async function resolveWikilinkPath(target) {
  const clean = String(target || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/^\.\//, "")
    .replace(/\.md$/i, "");
  if (!clean) return null;
  const notes = state.copilotData?.notes || [];
  const exact = notes.find((n) => n.path.replace(/\.md$/i, "").toLowerCase() === clean.toLowerCase());
  if (exact) return exact.path;
  const stem = clean.split("/").pop()?.toLowerCase() || "";
  if (!clean.includes("/")) {
    const bySlug = notes.find((n) => {
      const nStem = n.path.split("/").pop()?.replace(/\.md$/i, "") || "";
      return nStem.toLowerCase() === clean.toLowerCase();
    });
    if (bySlug) return bySlug.path;
  }
  try {
    const found = await request(`/api/copilot/vault/notes?q=${encodeURIComponent(stem)}&limit=20`);
    const candidates = found.notes || [];
    const direct = candidates.find(
      (n) =>
        n.path.replace(/\.md$/i, "").toLowerCase() === clean.toLowerCase() ||
        n.path.replace(/^source\//i, "").replace(/\.md$/i, "").toLowerCase() === clean.toLowerCase(),
    );
    if (direct) return direct.path;
    const byName = candidates.find(
      (n) => (n.path.split("/").pop()?.replace(/\.md$/i, "") || "").toLowerCase() === stem,
    );
    if (byName) return byName.path;
  } catch {
    /* Fall back to direct path construction below. */
  }
  if (!clean.includes("/")) return `wiki/${clean}.md`;
  return `${clean}.md`;
}

async function loadCopilotContext() {
  if (!state.user) return;
  const epoch = state.epoch;
  if (elements.copilotConnectors) elements.copilotConnectors.innerHTML = '<p class="loading-row">正在读取工作台能力…</p>';
  if (elements.copilotItemList) elements.copilotItemList.innerHTML = '<p class="loading-row">正在载入知识条目…</p>';
  try {
    const data = await request("/api/copilot/context");
    assertCurrent(epoch);
    state.copilotData = data;
    renderWorkspaceBadges(data);
    renderCopilotConnectors(data);
    renderCopilotItems();
    renderPendingActions(data);
  } catch (error) {
    if (epoch !== state.epoch || error instanceof StaleResponse) return;
    if (elements.copilotConnectors) elements.copilotConnectors.innerHTML = '<p class="loading-row">工作台状态载入失败，请刷新重试。</p>';
    if (elements.copilotItemList) elements.copilotItemList.innerHTML = "";
  }
}

async function loadCopilotNote(path) {
  if (!state.user) return;
  const epoch = state.epoch;
  setInspectorTab("knowledge");
  setKnowledgeTab("notes");
  state.selectedCopilotKey = `note:${path}`;
  renderCopilotItems();
  elements.copilotDetail.className = "copilot-detail";
  elements.copilotDetail.innerHTML = '<p class="loading-row">正在读取笔记…</p>';
  try {
    const note = await request(`/api/copilot/vault/note?path=${encodeURIComponent(path)}`);
    assertCurrent(epoch);
    const tagsHtml = (note.tags || []).map((tag) => `<span class="tag-chip">#${escapeHtml(tag)}</span>`).join("");
    const linksHtml = (note.links || [])
      .map(
        (link) =>
          `<button class="wikilink-chip" type="button" data-wikilink="${escapeHtml(link.target)}">[[${escapeHtml(link.target)}]]</button>`,
      )
      .join(" ");
    const backlinksHtml = (note.backlinks || [])
      .map(
        (item) =>
          `<button class="wikilink-chip" type="button" data-backlink-path="${escapeHtml(item.path)}">← ${escapeHtml(item.title || item.path)}</button>`,
      )
      .join(" ");
    elements.copilotDetail.innerHTML = `
      <div class="detail-status">
        <strong>${escapeHtml(note.title || note.path)}</strong>
        <span class="zone-badge">${escapeHtml(note.zone || "note")}</span>
      </div>
      <p class="source-note">${escapeHtml(note.path)}</p>
      ${tagsHtml ? `<div class="tag-row">${tagsHtml}</div>` : ""}
      <div class="note-preview markdown-content">${renderMarkdown(note.content || "", { interactiveWikilinks: true })}</div>
      ${linksHtml ? `<div class="tag-row">${linksHtml}</div>` : ""}
      ${backlinksHtml ? `<div class="tag-row"><span class="source-note">反向链接：</span>${backlinksHtml}</div>` : ""}
      <div class="detail-actions">
        <button class="secondary-action" type="button" id="ask-copilot-note">让 Copilot 解读这篇笔记</button>
      </div>`;
    elements.copilotDetail.querySelectorAll("[data-wikilink]").forEach((button) => {
      button.addEventListener("click", async () => {
        const resolved = await resolveWikilinkPath(button.dataset.wikilink);
        if (resolved) loadCopilotNote(resolved);
      });
    });
    elements.copilotDetail.querySelectorAll("[data-backlink-path]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.backlinkPath) loadCopilotNote(button.dataset.backlinkPath);
      });
    });
    document.querySelector("#ask-copilot-note")?.addEventListener("click", () => {
      sendChatMessage(`请用 vault_read_note 读取 ${note.path} 并总结核心要点与关联。`);
    });
  } catch (error) {
    if (epoch !== state.epoch || error instanceof StaleResponse) return;
    elements.copilotDetail.className = "empty-detail";
    elements.copilotDetail.innerHTML = `<i data-lucide="circle-alert"></i><p>笔记读取失败：${escapeHtml(error.message)}</p>`;
    refreshIcons();
  }
}

async function loadCopilotEntity(entityId) {
  if (!state.user) return;
  const epoch = state.epoch;
  setInspectorTab("knowledge");
  setKnowledgeTab("entities");
  state.selectedCopilotKey = `entity:${entityId}`;
  renderCopilotItems();
  elements.copilotDetail.className = "copilot-detail";
  elements.copilotDetail.innerHTML = '<p class="loading-row">正在读取本体实体…</p>';
  try {
    const payload = await request(`/api/copilot/ontology/entity/${encodeURIComponent(entityId)}`);
    assertCurrent(epoch);
    const entity = payload.entity;
    const neighbors = payload.neighbors || [];
    const attrsEntries = Object.entries(entity.attrs || {});
    const attrsHtml = attrsEntries
      .map(
        ([k, v]) =>
          `<div><span class="tag-chip">${escapeHtml(k)}</span> <span class="source-note">${escapeHtml(typeof v === "string" ? v : JSON.stringify(v))}</span></div>`,
      )
      .join("");
    const neighborsHtml = neighbors
      .map((item) => {
        const arrow = item.direction === "incoming" ? "←" : "→";
        const targetId = item.entity?.id || "";
        return `
          <button class="neighbor-button" type="button" data-jump-entity="${escapeHtml(targetId)}">
            <span class="connector-meta">
              <span class="connector-name">${arrow} ${escapeHtml(item.rel_type)} · ${escapeHtml(item.entity?.name || "")}</span>
              <span class="connector-sub">${escapeHtml(item.entity?.canonical_key || "")}</span>
            </span>
            <span class="zone-badge">${escapeHtml(item.entity?.entity_type || "")}</span>
          </button>`;
      })
      .join("");
    elements.copilotDetail.innerHTML = `
      <div class="detail-status">
        <strong>${escapeHtml(entity.name)}</strong>
        <span class="zone-badge">${escapeHtml(entity.entity_type)}</span>
      </div>
      <p class="source-note">${escapeHtml(entity.canonical_key)}</p>
      ${attrsHtml ? `<div class="note-preview">${attrsHtml}</div>` : ""}
      ${neighborsHtml ? `<div class="connector-list">${neighborsHtml}</div>` : '<p class="source-note">暂无直接关系边。</p>'}
      <div class="detail-actions">
        <button class="secondary-action" type="button" id="ask-copilot-entity">在对话中分析此实体</button>
      </div>`;
    elements.copilotDetail.querySelectorAll("[data-jump-entity]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.jumpEntity) loadCopilotEntity(button.dataset.jumpEntity);
      });
    });
    document.querySelector("#ask-copilot-entity")?.addEventListener("click", () => {
      sendChatMessage(`请用 onto_query 查询实体「${entity.name}」（${entity.entity_type}），并关联相关笔记或记录。`);
    });
  } catch (error) {
    if (epoch !== state.epoch || error instanceof StaleResponse) return;
    elements.copilotDetail.className = "empty-detail";
    elements.copilotDetail.innerHTML = `<i data-lucide="circle-alert"></i><p>实体读取失败：${escapeHtml(error.message)}</p>`;
    refreshIcons();
  }
}

async function loadSessions() {
  if (!state.user) return;
  const epoch = state.epoch;
  try {
    const payload = await request("/api/sessions");
    assertCurrent(epoch);
    elements.sessionList.innerHTML =
      payload.sessions
        .map(
          (session) => `
      <div class="history-row">
        <button class="session-select" type="button" data-session-id="${escapeHtml(session.session_id)}">${escapeHtml(session.title || WORKSPACE_TEXT.defaultSessionTitle)}</button>
        <button class="session-delete" type="button" data-delete-session="${escapeHtml(session.session_id)}" aria-label="删除会话：${escapeHtml(session.title || WORKSPACE_TEXT.defaultSessionTitle)}" title="删除会话">×</button>
      </div>`,
        )
        .join("") || '<p class="history-empty">暂无历史会话</p>';
    const selected = payload.sessions.find((session) => session.session_id === state.sessionId);
    if (selected) elements.sessionName.textContent = selected.title || WORKSPACE_TEXT.defaultSessionTitle;
    elements.sessionList.querySelectorAll("[data-session-id]").forEach((button) => {
      button.addEventListener("click", () => selectSession(button.dataset.sessionId));
    });
    elements.sessionList.querySelectorAll("[data-delete-session]").forEach((button) => {
      button.addEventListener("click", () => deleteSession(button.dataset.deleteSession));
    });
    updateSessionUi();
  } catch (error) {
    if (epoch === state.epoch && !(error instanceof StaleResponse)) {
      elements.sessionList.innerHTML = '<p class="history-empty">历史载入失败，请点击刷新。</p>';
    }
  }
}

async function ensureSession() {
  if (state.sessionId) return;
  const epoch = state.epoch;
  const view = state.view;
  const session = await request("/api/sessions", jsonPost());
  assertCurrent(epoch, view);
  state.sessionId = session.session_id;
  updateSessionUi();
}

async function selectSession(sessionId) {
  if (!state.user || state.sending) return;
  if (state.pending) {
    showResumeButton();
    return;
  }
  clearConversation();
  state.sessionId = sessionId;
  updateSessionUi();
  const epoch = state.epoch;
  setSending(true);
  await restoreSession();
  if (epoch === state.epoch) setSending(false);
}

async function deleteSession(sessionId) {
  if (!state.user || state.sending || state.pending) return;
  if (!window.confirm("删除这条会话及其恢复记录？此操作无法撤销。")) return;
  const epoch = state.epoch;
  const view = state.view;
  setSending(true);
  try {
    await request(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    assertCurrent(epoch, view);
    if (sessionId === state.sessionId) {
      clearConversation();
      updateSessionUi();
    }
    await loadSessions();
  } catch (error) {
    if (epoch === state.epoch && !(error instanceof StaleResponse)) {
      appendMessage("assistant", `删除未完成：${error.message}`);
    }
  } finally {
    if (epoch === state.epoch) setSending(false);
  }
}

async function streamChat(body, onStatus) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) {
    handleUnauthorized();
    throw new Error("登录已过期，请重新登录");
  }
  if (!response.ok || !response.body) {
    let detail = `请求失败 (${response.status})`;
    try {
      const err = await response.json();
      if (err?.detail) detail = err.detail;
    } catch {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalText = null;
  let meta = {};
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let eventName = "message";
      let dataText = "";
      for (const line of rawEvent.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataText += line.slice(5).trim();
      }
      if (!dataText) continue;
      let payload;
      try {
        payload = JSON.parse(dataText);
      } catch {
        continue;
      }
      if (eventName === "status" && payload.text && onStatus) onStatus(payload.text);
      else if (eventName === "final" && typeof payload.text === "string") finalText = payload.text;
      else if (eventName === "meta") meta = payload;
      else if (eventName === "error") {
        const err = new Error(payload.detail || "流式请求中断");
        err.status = payload.status || 500;
        throw err;
      }
    }
  }
  if (!finalText) throw new Error("未收到模型最终回复");
  return { response: finalText, session_id: meta.session_id || body.session_id, intent: meta.intent || "pi" };
}

function renderStatusTrail(bubble, steps) {
  bubble.innerHTML = `<ul class="status-trail">${steps
    .map((step, idx) => `<li class="status-step${idx === steps.length - 1 ? " is-active" : ""}">${escapeHtml(step)}</li>`)
    .join("")}</ul>`;
  scrollToLatest();
}

async function sendChatMessage(message) {
  if (!state.user || state.sending || !message.trim()) return;
  const epoch = state.epoch;
  const view = state.view;
  setSending(true);
  const startedAt = performance.now();
  let loading;
  const steps = ["正在准备上下文"];
  try {
    await ensureSession();
    assertCurrent(epoch, view);
    const clientRequestId = crypto.randomUUID();
    appendMessage("user", message.trim(), "当前会话");
    elements.input.value = "";
    loading = appendMessage("assistant loading", "");
    const bubble = loading.querySelector(".message-bubble");
    if (bubble) renderStatusTrail(bubble, steps);
    const payload = await streamChat(
      {
        message: message.trim(),
        session_id: state.sessionId,
        client_request_id: clientRequestId,
      },
      (statusText) => {
        if (bubble && epoch === state.epoch && view === state.view) {
          if (steps[steps.length - 1] !== statusText) {
            steps.push(statusText);
          }
          renderStatusTrail(bubble, steps);
        }
      },
    );
    assertCurrent(epoch, view);
    loading.remove();
    state.pending = null;
    saveSession();
    appendMessage(
      "assistant",
      payload.response,
      `${formatIntent(payload.intent)} · ${formatDuration(performance.now() - startedAt)}`,
    );
    await Promise.all([loadSessions(), loadCopilotContext()]);
  } catch (error) {
    if (epoch !== state.epoch || view !== state.view || error instanceof StaleResponse) return;
    loading?.remove();
    appendMessage("assistant", `本次请求未完成：${error.message}`, "请求错误");
  } finally {
    if (epoch === state.epoch && view === state.view) {
      setSending(false);
      elements.input.focus();
    }
  }
}

function resetSession() {
  if (!state.user || state.sending) return;
  clearConversation();
  updateSessionUi();
  refreshIcons();
  elements.input.focus();
}

async function restoreSession() {
  if (!state.user || !state.sessionId) return;
  const epoch = state.epoch;
  const view = state.view;
  const session = state.sessionId;
  try {
    const payload = await request(`/api/sessions/${encodeURIComponent(session)}`);
    assertCurrent(epoch, view);
    elements.sessionName.textContent = payload.title || WORKSPACE_TEXT.defaultSessionTitle;
    elements.messageList.innerHTML = "";
    for (const message of payload.messages) {
      appendMessage(message.role, message.content, "已保存会话");
    }
    state.pending = null;
    saveSession();
  } catch (error) {
    if (epoch !== state.epoch || view !== state.view || error instanceof StaleResponse) return;
    if ([403, 404, 409].includes(error.status)) {
      clearConversation();
      updateSessionUi();
    }
    appendMessage("assistant", "暂时无法读取这条会话，请刷新历史后重试。");
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
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && state.user) {
    event.preventDefault();
    elements.input.focus();
  }
});
elements.quickPrompts?.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => sendChatMessage(button.dataset.prompt));
});
document.querySelectorAll("[data-inspector-tab]").forEach((button) => {
  button.addEventListener("click", () => setInspectorTab(button.dataset.inspectorTab));
});
document.querySelectorAll("[data-knowledge-tab]").forEach((button) => {
  button.addEventListener("click", () => setKnowledgeTab(button.dataset.knowledgeTab));
});
elements.copilotFilterInput?.addEventListener("input", () => {
  state.filterQuery = elements.copilotFilterInput.value;
  if (state.knowledgeTab === "notes") {
    scheduleVaultSearch(state.filterQuery);
  } else {
    renderCopilotItems();
  }
});
document.querySelector("#new-session").addEventListener("click", resetSession);
document.querySelector("#history-button").addEventListener("click", loadSessions);
elements.refreshContext?.addEventListener("click", loadCopilotContext);

updateInputPlaceholder();
setSending(false);
refreshIcons();
document.querySelector("#lucide-script")?.addEventListener("load", refreshIcons);
checkHealth();
initializeAuth();
window.addEventListener("resize", updateInputPlaceholder);
