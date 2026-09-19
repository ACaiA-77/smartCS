// Native Node checks exercise actual auth/request/recovery functions, not an auth bypass.
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");

class Element {
  constructor() {
    this.children = []; this.parts = {}; this.dataset = {}; this.value = "";
    this.textContent = ""; this.hidden = false; this._html = "";
    this.classList = {add() {}, remove() {}};
  }
  set innerHTML(value) { this._html = value; this.children = []; }
  get innerHTML() { return this._html; }
  querySelector(selector) {
    if (selector === "[data-resume]") return null;
    return this.parts[selector] ||= new Element();
  }
  querySelectorAll() { return []; }
  append(child) { child.parent = this; this.children.push(child); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  addEventListener() {}
  setAttribute() {}
  focus() {}
}

function harness(responses = [], saved = {}) {
  const source = fs.readFileSync("web/app.js", "utf8");
  const nodes = new Map(), calls = [], confirmations = [];
  const storage = {...saved};
  Object.defineProperties(storage, {
    getItem: {value: key => storage[key] ?? null},
    setItem: {value: (key, value) => {storage[key] = value;}},
    removeItem: {value: key => {delete storage[key];}},
  });
  const context = {crypto: webcrypto, performance, sessionStorage: storage,
    window: {confirm: message => {confirmations.push(message); return true;}},
    document: {querySelector: selector => {
      if (!nodes.has(selector)) nodes.set(selector, new Element());
      return nodes.get(selector);
    }, createElement: () => new Element()},
    fetch: async (url, options) => {
      calls.push({url, options});
      const value = responses.shift();
      assert(value, `unexpected fetch ${url}`);
      if (typeof value === "function") return value(url, options);
      return value;
    }};
  const setup = source.slice(0, source.indexOf('elements.loginForm.addEventListener("submit"'));
  vm.runInNewContext(setup + `
    globalThis.api = {state, elements, initializeAuth, login, logout, activateUser,
      sendChatMessage, resumeChat, restoreSession, loadDemoOrders, selectSession,
      resetSession, queryDemoOrder, clearIdentity};
  `, context);
  return {api: context.api, calls, responses, storage, source, confirmations, context,
    messages: () => nodes.get("#message-list").children.map(node => node.querySelector(".message-bubble").innerHTML || node.querySelector(".message-bubble").textContent).join("\n")};
}

const reply = (status, body) => ({status, ok: status >= 200 && status < 300, json: async () => body});
const userA = {account_id: 11, username: "customer-a"};
const userB = {account_id: 12, username: "customer-b"};
const loginReplies = user => [reply(200, user), reply(200, {sessions: []}), reply(200, {orders: []})];
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
const success = text => reply(200, {response: text, session_id: "server-session", intent: "conversation"});

function assertNoIdentity(calls) {
  for (const call of calls) {
    assert(!call.url.includes("user_id"));
    assert(!/"(?:user_id|business_user_id)"/.test(call.options.body || ""));
    assert.equal(call.options.credentials, "same-origin");
  }
}

test("unauthenticated startup makes no customer-data request and clears legacy state", async () => {
  const h = harness([reply(401, {detail: "authentication required"})], {"smartcs.session": '"old"', "smartcs.pending": '"private"'});
  await h.api.initializeAuth();
  assert.deepEqual(h.calls.map(call => call.url), ["/api/auth/me"]);
  assert.equal(h.api.state.user, null);
  assert.equal(h.api.elements.appShell.hidden, true);
  assert.equal(h.api.elements.loginPage.hidden, false);
  assert.equal(Object.keys(h.storage).length, 0);
  await h.api.sendChatMessage("not authenticated");
  await h.api.loadDemoOrders();
  assert.equal(h.calls.length, 1);
});

test("login uses cookie-only contract and never persists a password or token", async () => {
  const h = harness([reply(200, {user: userA}), reply(200, {sessions: []}), reply(200, {orders: []})]);
  h.api.elements.username.value = "customer-a";
  h.api.elements.password.value = "test-only-not-a-credential";
  await h.api.login();
  assert.equal(h.calls[0].url, "/api/auth/login");
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {username: "customer-a", password: "test-only-not-a-credential"});
  assert.equal(h.api.elements.appShell.hidden, false);
  assert.equal(h.api.elements.accountName.textContent, "customer-a");
  assert.equal(h.api.elements.password.value, "");
  assert.equal(h.api.state.sessionId, null);
  assert(!JSON.stringify(h.storage).includes("test-only-not-a-credential"));
  assert(!/token|Authorization/.test(h.source));
  assertNoIdentity(h.calls);
});

test("server session is saved before a lost POST, then explicit retry keeps its request ID", async () => {
  const h = harness([...loginReplies(userA), reply(200, {session_id: "server-session"}), () => {throw new Error("network disconnected");}]);
  await h.api.initializeAuth();
  await h.api.sendChatMessage("请创建工单");
  const original = JSON.parse(h.calls.find(call => call.url === "/api/chat").options.body);
  assert.equal(original.session_id, "server-session");
  assert.equal(JSON.parse(h.storage["smartcs.account.11"]).pending.client_request_id, original.client_request_id);
  h.responses.push(reply(409, {detail: "not active"}), reply(200, {status: "finished", client_request_id: "old"}), success("已完成"), reply(200, {sessions: []}));
  await h.api.resumeChat();
  const chatCalls = h.calls.filter(call => call.url === "/api/chat");
  assert.equal(chatCalls.length, 2);
  assert.deepEqual(JSON.parse(chatCalls[1].options.body), original);
  assert.equal(h.api.state.pending, null);
  assertNoIdentity(h.calls);
});

test("a different running server request is not overwritten or retried", async () => {
  const h = harness([...loginReplies(userA), reply(200, {session_id: "server-session"}), () => {throw new Error("offline");}]);
  await h.api.initializeAuth();
  await h.api.sendChatMessage("新的消息");
  h.responses.push(reply(409, {detail: "not active"}), reply(200, {status: "running", client_request_id: "other-request"}));
  await h.api.resumeChat();
  assert.equal(h.calls.filter(call => call.url === "/api/chat").length, 1);
  assert.equal(h.api.state.pending.message, "新的消息");
});

test("authenticated refresh restores only that account's pending message without identity parameters", async () => {
  const h = harness([...loginReplies(userA), reply(200, {messages: [], title: "原会话"}), reply(200, {status: "running", client_request_id: "new"})], {
    "smartcs.account.11": JSON.stringify({sessionId: "server-session", pending: {client_request_id: "new", message: "原始消息"}}),
    "smartcs.account.12": JSON.stringify({sessionId: "other-account"}),
  });
  await h.api.initializeAuth();
  assert.equal(h.api.state.pending.message, "原始消息");
  assert.equal(h.api.state.sessionId, "server-session");
  assert.equal(h.storage["smartcs.account.12"], undefined);
  assert.equal(h.api.state.sending, false);
  assert.equal(h.calls.filter(call => call.options.method === "POST").length, 0);
  assertNoIdentity(h.calls);
});

test("WAIT_CONFIRM refresh never auto-confirms a refund", async () => {
  const h = harness([...loginReplies(userA), reply(200, {messages: [{role: "assistant", content: "请确认退款"}]}), reply(200, {status: "waiting", client_request_id: "request-1"})], {
    "smartcs.account.11": JSON.stringify({sessionId: "server-session", pending: {client_request_id: "request-1", message: "申请退款"}}),
  });
  await h.api.initializeAuth();
  assert.equal(h.api.state.pending, null);
  assert(h.messages().includes("请确认退款"));
  assert(h.calls.every(call => !call.options.method));
});

test("logout warns about in-flight work and a late A reply cannot reappear under B", async () => {
  const delayed = deferred();
  const h = harness([...loginReplies(userA), reply(200, {session_id: "server-session"}), () => delayed.promise]);
  await h.api.initializeAuth();
  const sending = h.api.sendChatMessage("A private message");
  await tick();
  h.responses.push(reply(200, {logged_out: true}));
  await h.api.logout();
  assert.equal(h.confirmations.length, 1);
  assert.equal(Object.keys(h.storage).length, 0);
  assert.equal(h.messages(), "");
  assert.equal(h.api.elements.appShell.hidden, true);
  h.responses.push(reply(200, {user: userB}), reply(200, {sessions: []}), reply(200, {orders: []}));
  h.api.elements.username.value = "customer-b";
  h.api.elements.password.value = "test-only";
  await h.api.login();
  delayed.resolve(success("A private answer"));
  await sending;
  assert.equal(h.api.state.user.account_id, 12);
  assert.equal(h.messages(), "");
  assert(!JSON.stringify(h.storage).includes("A private"));
  assertNoIdentity(h.calls);
});

test("a late unauthorized response from A must not log B out", async () => {
  const delayed = deferred();
  const h = harness([...loginReplies(userA), reply(200, {session_id: "server-session"}), () => delayed.promise]);
  await h.api.initializeAuth();
  const sending = h.api.sendChatMessage("hello");
  await tick();
  h.responses.push(reply(200, {logged_out: true}));
  await h.api.logout();
  h.responses.push(reply(200, {user: userB}), reply(200, {sessions: []}), reply(200, {orders: []}));
  await h.api.login();
  delayed.resolve(reply(401, {detail: "expired"}));
  await sending;
  assert.equal(h.api.state.user.account_id, 12);
  assert.equal(h.api.elements.appShell.hidden, false);
});

test("late order-list JSON cannot repopulate a logged-out page", async () => {
  const delayed = deferred();
  const h = harness([...loginReplies(userA)]);
  await h.api.initializeAuth();
  h.responses.push({status: 200, ok: true, json: () => delayed.promise});
  const loading = h.api.loadDemoOrders();
  await tick();
  h.responses.push(reply(200, {logged_out: true}));
  await h.api.logout();
  delayed.resolve({orders: [{order_id: "A-private-order"}]});
  await loading;
  assert.equal(h.api.elements.orderList.innerHTML, "");
});

test("denied saved session clears its identity and restores an editable new conversation", async () => {
  const h = harness([...loginReplies(userA), reply(404, {detail: "session not found"})], {
    "smartcs.account.11": JSON.stringify({sessionId: "someone-elses-session"}),
  });
  await h.api.initializeAuth();
  assert.equal(h.api.state.sessionId, null);
  assert.equal(h.api.state.pending, null);
  assert.equal(h.api.elements.input.disabled, false);
});

test("order query creates a server session and sends only order ID as tool arguments", async () => {
  const h = harness([...loginReplies(userA), reply(200, {session_id: "server-session"}), reply(200, {success: true, result: {found: false, order_id: "own-order"}}), reply(200, {sessions: []})]);
  await h.api.initializeAuth();
  await h.api.queryDemoOrder("own-order");
  const body = JSON.parse(h.calls.find(call => call.url === "/api/tools/call").options.body);
  assert.deepEqual(body, {name: "order_query", arguments: {order_id: "own-order"}, session_id: "server-session"});
  assert.equal(h.api.state.sending, false);
  assertNoIdentity(h.calls);
});

test("temporary auth-check outage hides customer data but keeps recoverable local request", async () => {
  const saved = JSON.stringify({sessionId: "server-session", pending: {client_request_id: "unsent", message: "keep this"}});
  const h = harness([() => {throw new Error("offline");}], {"smartcs.account.11": saved});
  await h.api.initializeAuth();
  assert.equal(h.storage["smartcs.account.11"], saved);
  assert.equal(h.api.elements.appShell.hidden, true);
  assert.equal(h.api.state.user, null);
  assert.equal(h.calls.length, 1);
});

test("canceling logout preserves pending work and never calls logout API", async () => {
  const h = harness([...loginReplies(userA), reply(200, {session_id: "server-session"}), () => {throw new Error("offline");}]);
  await h.api.initializeAuth();
  await h.api.sendChatMessage("original pending message");
  const pending = JSON.stringify(h.api.state.pending);
  h.context.window.confirm = () => false;
  await h.api.logout();
  assert.equal(JSON.stringify(h.api.state.pending), pending);
  assert.equal(h.api.state.user.account_id, 11);
  assert.equal(h.calls.filter(call => call.url === "/api/auth/logout").length, 0);
});

test("failed logout clears visible data and offers explicit cookie-clear retry", async () => {
  const h = harness([...loginReplies(userA), () => {throw new Error("offline");}, reply(200, {logged_out: true})]);
  await h.api.initializeAuth();
  await h.api.logout();
  assert.equal(h.api.elements.appShell.hidden, true);
  assert.equal(h.api.elements.loginButton.dataset.retryLogout, "true");
  assert.equal(h.api.elements.loginButton.type, "button");
  await h.api.logout();
  assert.equal(h.api.elements.loginButton.dataset.retryLogout, undefined);
  assert.equal(h.api.elements.loginButton.type, "submit");
  assert.equal(h.api.state.user, null);
});

test("logout remains busy until cookie clear finishes even when old auth bootstrap completes", async () => {
  const orders = deferred(), logout = deferred();
  const h = harness([reply(200, userA), reply(200, {sessions: []}), () => orders.promise]);
  const initialize = h.api.initializeAuth();
  await tick();
  h.responses.push(() => logout.promise);
  const signingOut = h.api.logout();
  orders.resolve(reply(200, {orders: []}));
  await initialize;
  assert.equal(h.api.elements.loginButton.disabled, true);
  logout.resolve(reply(200, {logged_out: true}));
  await signingOut;
  assert.equal(h.api.elements.loginButton.disabled, false);
  assert.equal(h.api.state.user, null);
});
