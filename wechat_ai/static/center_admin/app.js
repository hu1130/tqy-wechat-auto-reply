(() => {
  "use strict";

  const STORAGE = {
    centerUrl: "wechat-center-admin:center-url",
    tenantId: "wechat-center-admin:tenant-id",
    apiKey: "wechat-center-admin:api-key",
  };

  const VIEW_META = {
    overview: ["概览", "OPERATIONS"],
    messages: ["消息", "CONVERSATIONS"],
    "reply-jobs": ["AI 任务", "AUTOMATION"],
    deliveries: ["发送任务", "OUTBOX"],
    contacts: ["联系人策略", "WECHAT CONTACTS"],
    "friend-requests": ["好友申请", "FRIEND REQUESTS"],
    "operation-jobs": ["运营任务", "WECHAT OPERATIONS"],
    "operation-settings": ["运营设置", "OPERATION LIMITS"],
    settings: ["配置", "TENANT SETTINGS"],
    audit: ["审计", "AUDIT LOG"],
  };

  const STATUS_LABELS = {
    active: "启用",
    disabled: "停用",
    online: "在线",
    degraded: "异常",
    running: "运行中",
    healthy: "正常",
    offline: "离线",
    queued: "排队",
    processing: "处理中",
    generated: "已生成",
    claimed: "已领取",
    sending: "发送中",
    completed: "已完成",
    failed: "失败",
    uncertain: "待确认",
    created: "已创建",
    approved: "已批准",
    executing: "执行中",
    succeeded: "成功",
    cancelled: "已取消",
    pending: "待处理",
    rejected: "已拒绝",
    accepted: "已通过",
    synced: "已同步",
    stale: "待同步",
    ai_auto: "AI 自动回复",
    manual: "仅人工",
    paused: "暂停",
    blocked: "屏蔽",
    inbound: "收到",
    outbound: "发出",
  };

  const FRIEND_REQUEST_ENDPOINTS = {
    "approve-selected": "/v1/friend-requests/approve-selected",
    "reject-selected": "/v1/friend-requests/reject-selected",
    "approve-batch": "/v1/friend-requests/approve-batch",
    "approve-all": "/v1/friend-requests/approve-all",
  };

  const state = {
    centerUrl: "",
    tenantId: "",
    apiKey: "",
    currentView: "overview",
    session: null,
    tenant: null,
    members: [],
    channels: [],
    agents: [],
    messages: [],
    replyJobs: [],
    deliveries: [],
    contacts: [],
    selectedContacts: new Set(),
    friendRequests: [],
    selectedFriendRequests: new Set(),
    operationJobs: [],
    operationLimits: null,
    audit: [],
  };

  let pendingConfirmation = null;

  const byId = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

  function defaultCenterUrl() {
    const path = window.location.pathname.replace(/\/+$/, "");
    const adminMarker = "/admin";
    const markerIndex = path.lastIndexOf(adminMarker);
    const prefix = markerIndex >= 0 ? path.slice(0, markerIndex) : "";
    return `${window.location.origin}${prefix}`;
  }

  function normalizeCenterUrl(value) {
    const candidate = String(value || "").trim() || defaultCenterUrl();
    const parsed = new URL(candidate, window.location.href);
    if (!(["http:", "https:"].includes(parsed.protocol))) {
      throw new Error("Center URL 必须使用 HTTP 或 HTTPS");
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error("Center URL 不能包含账号、查询参数或片段");
    }
    return `${parsed.origin}${parsed.pathname.replace(/\/$/, "")}`;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    const headers = {
      Accept: "application/json",
      Authorization: `Bearer ${state.apiKey}`,
      "X-Tenant-ID": state.tenantId,
      ...(options.headers || {}),
    };
    const init = {
      method: options.method || "GET",
      headers,
      credentials: "omit",
      signal: controller.signal,
    };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    try {
      const response = await fetch(`${state.centerUrl}${path}`, init);
      let payload = {};
      try {
        payload = await response.json();
      } catch (error) {
        if (response.status !== 204) {
          throw new ApiError(response.status, "中心服务返回了无效响应");
        }
      }
      if (!response.ok) {
        throw new ApiError(response.status, payload.error || `请求失败 (${response.status})`);
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("请求超时");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function extractArray(payload, ...keys) {
    for (const key of keys) {
      if (Array.isArray(payload && payload[key])) return payload[key];
    }
    if (Array.isArray(payload)) return payload;
    return [];
  }

  function extractObject(payload, ...keys) {
    for (const key of keys) {
      const value = payload && payload[key];
      if (value && typeof value === "object" && !Array.isArray(value)) return value;
    }
    if (payload && typeof payload === "object" && !Array.isArray(payload)) return payload;
    return null;
  }

  function firstValue(record, keys, fallback = "-") {
    for (const key of keys) {
      const value = record && record[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return fallback;
  }

  function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function shortId(value) {
    const text = String(value || "-");
    return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
  }

  function newIdempotencyKey(prefix) {
    const suffix = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${suffix}`;
  }

  function showToast(message, kind = "success") {
    const toast = byId("toast");
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 3200);
  }

  function showGlobalError(message = "") {
    const banner = byId("global-error");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function setViewState(view, kind, message = "") {
    const panel = document.querySelector(`[data-view-panel="${view}"]`);
    const target = panel && panel.querySelector(":scope > .view-state");
    if (!target) return;
    target.dataset.kind = kind || "";
    target.textContent = message;
    target.hidden = !message;
  }

  function setFormBusy(form, busy) {
    all(`#${form.id} button, #${form.id} input, #${form.id} select, #${form.id} textarea`)
      .forEach((control) => { control.disabled = busy; });
  }

  function makeCell(value, className = "") {
    const td = document.createElement("td");
    td.textContent = value === undefined || value === null || value === "" ? "-" : String(value);
    if (className) td.className = className;
    return td;
  }

  function statusBadge(status) {
    const normalized = String(status || "unknown").toLowerCase();
    const badge = document.createElement("span");
    badge.className = "badge";
    if (["active", "online", "running", "healthy", "generated", "completed", "outbound", "succeeded", "accepted", "synced", "ai_auto"].includes(normalized)) {
      badge.classList.add("badge-success");
    } else if (["failed", "offline", "disabled", "rejected", "blocked", "cancelled"].includes(normalized)) {
      badge.classList.add("badge-danger");
    } else if (["uncertain", "queued", "pending", "paused", "stale", "degraded", "rolled_back"].includes(normalized)) {
      badge.classList.add("badge-warning");
    } else if (["processing", "claimed", "sending", "inbound", "created", "approved", "executing", "manual"].includes(normalized)) {
      badge.classList.add("badge-info");
    }
    badge.textContent = STATUS_LABELS[normalized] || status || "未知";
    return badge;
  }

  function emptyRow(tbody, columns, message) {
    const row = document.createElement("tr");
    const cell = makeCell(message, "empty-cell");
    cell.colSpan = columns;
    row.append(cell);
    tbody.replaceChildren(row);
  }

  function renderRows(tbody, rows, columns, emptyMessage) {
    if (!rows.length) {
      emptyRow(tbody, columns, emptyMessage);
      return;
    }
    tbody.replaceChildren(...rows);
  }

  function detailItem(label, value) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = value === undefined || value === null || value === "" ? "-" : String(value);
    detail.title = detail.textContent;
    wrapper.append(term, detail);
    return wrapper;
  }

  function apiErrorText(error) {
    if (error instanceof ApiError && error.status === 401) return "认证失败，请重新登录";
    if (error instanceof ApiError && error.status === 403) return "当前密钥没有执行此操作的权限";
    return error && error.message ? error.message : "请求失败";
  }

  function queryString(values) {
    const params = new URLSearchParams();
    Object.entries(values).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    });
    const query = params.toString();
    return query ? `?${query}` : "";
  }

  function channelLabel(channel) {
    return `${firstValue(channel, ["display_name", "name"], channel.id)} · ${firstValue(channel, ["channel", "type"], "wechat")}`;
  }

  function populateChannelSelect(select, { allowAll = false } = {}) {
    const selected = select.value;
    const options = [];
    if (allowAll) {
      const allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = "全部渠道";
      options.push(allOption);
    }
    state.channels.forEach((channel) => {
      const option = document.createElement("option");
      option.value = channel.id;
      option.textContent = channelLabel(channel);
      options.push(option);
    });
    if (!options.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无渠道账号";
      options.push(option);
    }
    select.replaceChildren(...options);
    if (selected && state.channels.some((channel) => channel.id === selected)) select.value = selected;
    select.disabled = !state.channels.length;
  }

  function hydrateOperationChannelChoices() {
    populateChannelSelect(byId("contact-channel"));
    populateChannelSelect(byId("friend-request-channel"));
    populateChannelSelect(byId("operation-job-channel"), { allowAll: true });
    populateChannelSelect(byId("operation-settings-channel"));
  }

  async function ensureChannels() {
    if (!state.channels.length) {
      const payload = await api("/v1/channel-accounts?limit=200");
      state.channels = extractArray(payload, "channel_accounts", "channels", "accounts");
    }
    hydrateOperationChannelChoices();
    return state.channels;
  }

  function closeConfirmation(result) {
    const dialog = byId("confirm-dialog");
    if (dialog.open) dialog.close();
    if (pendingConfirmation) {
      const resolve = pendingConfirmation;
      pendingConfirmation = null;
      resolve(result);
    }
  }

  function confirmAction({ title, message, confirmLabel = "确认", danger = true }) {
    if (pendingConfirmation) closeConfirmation(false);
    byId("confirm-title").textContent = title;
    byId("confirm-message").textContent = message;
    const submit = byId("confirm-submit");
    submit.textContent = confirmLabel;
    submit.className = `button ${danger ? "button-danger" : "button-primary"}`;
    byId("confirm-dialog").showModal();
    return new Promise((resolve) => { pendingConfirmation = resolve; });
  }

  async function login(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const errorBox = byId("login-error");
    errorBox.hidden = true;
    try {
      state.centerUrl = normalizeCenterUrl(byId("center-url").value);
      state.tenantId = byId("tenant-id").value.trim();
      state.apiKey = byId("api-key").value.trim();
      if (!state.tenantId || !state.apiKey) throw new Error("租户 ID 和 API Key 必填");
      setFormBusy(form, true);
      const payload = await api("/v1/session");
      state.session = extractObject(payload, "session", "principal");
      sessionStorage.setItem(STORAGE.centerUrl, state.centerUrl);
      sessionStorage.setItem(STORAGE.tenantId, state.tenantId);
      sessionStorage.setItem(STORAGE.apiKey, state.apiKey);
      enterApp();
      await activateView("overview");
    } catch (error) {
      errorBox.textContent = apiErrorText(error);
      errorBox.hidden = false;
    } finally {
      setFormBusy(form, false);
    }
  }

  function enterApp() {
    byId("login-screen").hidden = true;
    byId("app-shell").hidden = false;
    byId("tenant-label").textContent = state.tenantId;
    const role = firstValue(state.session, ["role", "membership_role", "subject_type"]);
    byId("session-role").textContent = role;
  }

  function logout() {
    Object.values(STORAGE).forEach((key) => sessionStorage.removeItem(key));
    state.apiKey = "";
    state.session = null;
    byId("api-key").value = "";
    byId("app-shell").hidden = true;
    byId("login-screen").hidden = false;
    showGlobalError();
  }

  async function restoreSession() {
    const centerUrl = sessionStorage.getItem(STORAGE.centerUrl) || "";
    const tenantId = sessionStorage.getItem(STORAGE.tenantId) || "";
    const apiKey = sessionStorage.getItem(STORAGE.apiKey) || "";
    byId("center-url").value = centerUrl && centerUrl !== window.location.origin ? centerUrl : "";
    byId("tenant-id").value = tenantId;
    if (!tenantId || !apiKey) return;

    state.centerUrl = normalizeCenterUrl(centerUrl);
    state.tenantId = tenantId;
    state.apiKey = apiKey;
    try {
      const payload = await api("/v1/session");
      state.session = extractObject(payload, "session", "principal");
      enterApp();
      await activateView("overview");
    } catch (error) {
      logout();
      byId("login-error").textContent = apiErrorText(error);
      byId("login-error").hidden = false;
    }
  }

  async function activateView(view) {
    if (!VIEW_META[view]) return;
    state.currentView = view;
    all("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; });
    all("[data-view]").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    byId("view-title").textContent = VIEW_META[view][0];
    byId("view-eyebrow").textContent = VIEW_META[view][1];
    showGlobalError();
    await loadCurrentView();
  }

  async function loadCurrentView() {
    const loaders = {
      overview: loadOverview,
      messages: loadMessages,
      "reply-jobs": loadReplyJobs,
      deliveries: loadDeliveries,
      contacts: loadContacts,
      "friend-requests": loadFriendRequests,
      "operation-jobs": loadOperationJobs,
      "operation-settings": loadOperationSettings,
      settings: loadSettings,
      audit: loadAudit,
    };
    const loader = loaders[state.currentView];
    if (!loader) return;
    byId("refresh-button").disabled = true;
    byId("connection-status").textContent = "同步中";
    try {
      await loader();
      byId("connection-status").textContent = "已连接";
    } catch (error) {
      const message = apiErrorText(error);
      setViewState(state.currentView, "error", message);
      byId("connection-status").textContent = "连接异常";
      if (error instanceof ApiError && error.status === 401) showGlobalError(message);
    } finally {
      byId("refresh-button").disabled = false;
    }
  }

  async function loadOverview() {
    setViewState("overview", "loading", "正在加载概览…");
    const [sessionData, tenantData, membersData, channelsData, agentsData, messagesData, repliesData, deliveriesData] = await Promise.all([
      api("/v1/session"),
      api("/v1/tenant"),
      api("/v1/members?limit=200"),
      api("/v1/channel-accounts?limit=200"),
      api("/v1/agents?limit=200"),
      api("/v1/messages?limit=200"),
      api("/v1/reply-jobs?limit=200"),
      api("/v1/deliveries?limit=200"),
    ]);
    state.session = extractObject(sessionData, "session", "principal");
    state.tenant = extractObject(tenantData, "tenant");
    state.members = extractArray(membersData, "members", "memberships");
    state.channels = extractArray(channelsData, "channel_accounts", "channels", "accounts");
    state.agents = extractArray(agentsData, "agents", "agent_instances");
    state.messages = extractArray(messagesData, "messages");
    state.replyJobs = extractArray(repliesData, "reply_jobs", "jobs");
    state.deliveries = extractArray(deliveriesData, "deliveries", "delivery_jobs");
    renderOverview();
    hydrateDeliveryChoices();
    hydrateOperationChannelChoices();
    setViewState("overview", "", "");
  }

  function renderOverview() {
    byId("metric-messages").textContent = String(state.messages.length);
    byId("metric-replies").textContent = String(state.replyJobs.filter((job) => ["queued", "processing"].includes(job.status)).length);
    byId("metric-deliveries").textContent = String(state.deliveries.filter((job) => ["failed", "uncertain"].includes(job.status)).length);
    byId("metric-agents").textContent = String(state.agents.filter((agent) => ["online", "running", "healthy"].includes(String(agent.status).toLowerCase())).length);

    const tenant = state.tenant || {};
    byId("tenant-label").textContent = firstValue(tenant, ["name", "slug"], state.tenantId);
    byId("session-role").textContent = firstValue(state.session, ["role", "membership_role", "subject_type"]);
    byId("tenant-details").replaceChildren(
      detailItem("租户名称", firstValue(tenant, ["name"])),
      detailItem("租户标识", firstValue(tenant, ["slug", "id"], state.tenantId)),
      detailItem("状态", STATUS_LABELS[String(firstValue(tenant, ["status"], "active")).toLowerCase()] || firstValue(tenant, ["status"])),
      detailItem("成员数", state.members.length),
    );

    const channelRows = state.channels.map((channel) => {
      const row = document.createElement("tr");
      row.append(
        makeCell(firstValue(channel, ["display_name", "name"])),
        makeCell(firstValue(channel, ["channel", "type"])),
        makeCell(firstValue(channel, ["external_account_id", "external_id"]), "cell-id"),
      );
      const status = document.createElement("td");
      status.append(statusBadge(channel.status));
      row.append(status);
      return row;
    });
    renderRows(byId("overview-channels"), channelRows, 4, "暂无渠道账号");

    const agentRows = state.agents.map((agent) => {
      const row = document.createElement("tr");
      const capabilities = agent.capabilities_json && typeof agent.capabilities_json === "object"
        ? agent.capabilities_json
        : {};
      const updater = capabilities.updater && typeof capabilities.updater === "object"
        ? capabilities.updater
        : {};
      const updaterStatus = String(updater.status || "");
      const updaterFailure = String(updater.failure_code || "");
      const version = firstValue(updater, ["running_version", "active_version"], "未上报");
      const updateSummary = [
        version,
        updaterStatus && updaterStatus !== "healthy" ? updaterStatus : "",
        updaterFailure,
      ].filter(Boolean).join(" · ");
      const effectiveStatus = updaterFailure || ["failed", "rollback_failed"].includes(updaterStatus)
        ? "degraded"
        : agent.status;
      row.append(
        makeCell(firstValue(agent, ["name", "display_name"])),
        makeCell(shortId(firstValue(agent, ["external_agent_id", "id"])), "cell-id"),
      );
      const status = document.createElement("td");
      status.append(statusBadge(effectiveStatus));
      row.append(
        status,
        makeCell(updateSummary || "未上报", updaterFailure ? "cell-error" : ""),
        makeCell(formatTime(firstValue(agent, ["last_heartbeat_at", "updated_at"], ""))),
      );
      return row;
    });
    renderRows(byId("overview-agents"), agentRows, 5, "暂无 Agent");
  }

  async function loadMessages() {
    setViewState("messages", "loading", "正在加载消息…");
    const payload = await api("/v1/messages?limit=500");
    state.messages = extractArray(payload, "messages").slice().reverse();
    const rows = state.messages.map((message) => {
      const row = document.createElement("tr");
      row.append(makeCell(formatTime(firstValue(message, ["received_at", "created_at"], ""))));
      const direction = document.createElement("td");
      direction.append(statusBadge(message.direction));
      row.append(
        direction,
        makeCell(shortId(firstValue(message, ["customer_display_name", "sender_external_id", "conversation_id"])), "cell-id"),
        makeCell(firstValue(message, ["content"]), "cell-content"),
        makeCell(shortId(firstValue(message, ["channel_account_id"])), "cell-id"),
      );
      return row;
    });
    renderRows(byId("messages-body"), rows, 5, "暂无消息");
    byId("message-count").textContent = `${state.messages.length} 条`;
    hydrateDeliveryChoices();
    setViewState("messages", "", "");
  }

  async function loadReplyJobs() {
    setViewState("reply-jobs", "loading", "正在加载 AI 任务…");
    const status = byId("reply-status").value;
    const suffix = status ? `&status=${encodeURIComponent(status)}` : "";
    const payload = await api(`/v1/reply-jobs?limit=500${suffix}`);
    state.replyJobs = extractArray(payload, "reply_jobs", "jobs").slice().reverse();
    const rows = state.replyJobs.map((job) => {
      const row = document.createElement("tr");
      row.append(makeCell(formatTime(job.created_at)));
      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(job.status));
      row.append(
        statusCell,
        makeCell(job.attempts ?? 0),
        makeCell(shortId(job.source_message_id), "cell-id"),
        makeCell(shortId(job.generated_delivery_id), "cell-id"),
        makeCell(job.error, "cell-error"),
      );
      return row;
    });
    renderRows(byId("reply-jobs-body"), rows, 6, "暂无 AI 任务");
    byId("reply-count").textContent = `${state.replyJobs.length} 条`;
    setViewState("reply-jobs", "", "");
  }

  async function loadDeliveries() {
    setViewState("deliveries", "loading", "正在加载发送任务…");
    const requests = [api("/v1/deliveries?limit=500")];
    if (!state.channels.length) requests.push(api("/v1/channel-accounts?limit=200"));
    if (!state.messages.length) requests.push(api("/v1/messages?limit=500"));
    const results = await Promise.all(requests);
    state.deliveries = extractArray(results[0], "deliveries", "delivery_jobs").slice().reverse();
    let cursor = 1;
    if (!state.channels.length) {
      state.channels = extractArray(results[cursor], "channel_accounts", "channels", "accounts");
      cursor += 1;
    }
    if (!state.messages.length) state.messages = extractArray(results[cursor], "messages");
    hydrateDeliveryChoices();
    renderDeliveries();
    setViewState("deliveries", "", "");
  }

  function renderDeliveries() {
    const filter = byId("delivery-status").value;
    const deliveries = filter ? state.deliveries.filter((item) => item.status === filter) : state.deliveries;
    const rows = deliveries.map((delivery) => {
      const payload = delivery.payload || delivery.payload_json || {};
      const row = document.createElement("tr");
      row.append(makeCell(formatTime(delivery.created_at)));
      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(delivery.status));
      row.append(
        statusCell,
        makeCell(shortId(firstValue(delivery, ["conversation_id"], payload.conversation_external_id)), "cell-id"),
        makeCell(firstValue(payload, ["content"]), "cell-content"),
        makeCell(delivery.attempts ?? 0),
        makeCell(delivery.error, "cell-error"),
      );
      const action = document.createElement("td");
      if (delivery.status === "uncertain") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button-secondary button-small";
        button.textContent = "处理";
        button.addEventListener("click", () => openResolve(delivery.id));
        action.append(button);
      } else {
        action.textContent = "-";
      }
      row.append(action);
      return row;
    });
    renderRows(byId("deliveries-body"), rows, 7, "暂无发送任务");
    byId("delivery-count").textContent = `${deliveries.length} 条`;
  }

  function hydrateDeliveryChoices() {
    const channelSelect = byId("delivery-channel");
    const selected = channelSelect.value;
    const channelOptions = state.channels.map((channel) => {
      const option = document.createElement("option");
      option.value = channel.id;
      option.textContent = `${firstValue(channel, ["display_name", "name"], channel.id)} · ${firstValue(channel, ["channel"], "channel")}`;
      return option;
    });
    if (!channelOptions.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无渠道账号";
      channelOptions.push(option);
    }
    channelSelect.replaceChildren(...channelOptions);
    if (selected && state.channels.some((channel) => channel.id === selected)) channelSelect.value = selected;

    const conversations = new Map();
    state.messages.forEach((message) => {
      if (message.conversation_id && !conversations.has(message.conversation_id)) {
        conversations.set(message.conversation_id, firstValue(message, ["sender_external_id", "customer_display_name"], ""));
      }
    });
    const options = Array.from(conversations, ([id, label]) => {
      const option = document.createElement("option");
      option.value = id;
      option.label = label === "-" ? "" : label;
      return option;
    });
    byId("conversation-options").replaceChildren(...options);
  }

  async function createDelivery(event) {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      setFormBusy(form, true);
      await api("/v1/deliveries", {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey("manual") },
        body: {
          channel_account_id: byId("delivery-channel").value,
          conversation_id: byId("delivery-conversation").value.trim(),
          content: byId("delivery-content").value.trim(),
        },
      });
      byId("delivery-content").value = "";
      showToast("发送任务已创建");
      await loadDeliveries();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  function openResolve(deliveryId) {
    byId("resolve-delivery-id").value = deliveryId;
    byId("resolve-resolution").value = "sent";
    byId("resolve-note").value = "";
    byId("resolve-dialog").showModal();
  }

  async function resolveDelivery(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const deliveryId = byId("resolve-delivery-id").value;
    try {
      setFormBusy(form, true);
      await api(`/v1/deliveries/${encodeURIComponent(deliveryId)}/resolve`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey(`resolve-${deliveryId}`) },
        body: {
          resolution: byId("resolve-resolution").value,
          note: byId("resolve-note").value.trim(),
        },
      });
      byId("resolve-dialog").close();
      showToast("待确认任务已处理");
      await loadDeliveries();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  function contactId(contact) {
    return String(firstValue(contact, ["id", "contact_id"], ""));
  }

  function contactType(contact) {
    const value = contact.contact_type || contact.type || contact.kind;
    if (String(value || "").toLowerCase() === "group" || contact.is_group === true) return "group";
    return "friend";
  }

  function contactPolicy(contact) {
    const value = String(contact.reply_policy || contact.policy || "manual").toLowerCase();
    return ["ai_auto", "manual", "paused", "blocked"].includes(value) ? value : "manual";
  }

  function contactWelcomeEnabled(contact) {
    return Boolean(contact.welcome_enabled ?? contact.send_welcome_message ?? contact.welcome_message_enabled);
  }

  function visibleContacts() {
    const channelId = byId("contact-channel").value;
    const type = byId("contact-type-filter").value;
    const policy = byId("contact-policy-filter").value;
    const query = byId("contact-search").value.trim().toLocaleLowerCase("zh-CN");
    return state.contacts.filter((contact) => {
      if (channelId && contact.channel_account_id && String(contact.channel_account_id) !== channelId) return false;
      if (type && contactType(contact) !== type) return false;
      if (policy && contactPolicy(contact) !== policy) return false;
      if (!query) return true;
      const haystack = [
        contact.display_name,
        contact.name,
        contact.nickname,
        contact.remark,
        contact.alias,
        contact.external_contact_id,
        contact.external_id,
        contact.wechat_id,
      ].filter(Boolean).join(" ").toLocaleLowerCase("zh-CN");
      return haystack.includes(query);
    });
  }

  async function loadContacts() {
    setViewState("contacts", "loading", "正在加载联系人…");
    await ensureChannels();
    const channelId = byId("contact-channel").value;
    if (!channelId) {
      state.contacts = [];
      state.selectedContacts.clear();
      renderContacts();
      setViewState("contacts", "empty", "请先绑定一个微信渠道账号");
      return;
    }
    const payload = await api(`/v1/contacts${queryString({ channel_account_id: channelId, limit: 500 })}`);
    state.contacts = extractArray(payload, "contacts", "channel_contacts");
    const validIds = new Set(state.contacts.map(contactId));
    state.selectedContacts.forEach((id) => { if (!validIds.has(id)) state.selectedContacts.delete(id); });
    renderContacts();
    setViewState("contacts", state.contacts.length ? "" : "empty", state.contacts.length ? "" : "该渠道尚未同步联系人");
  }

  function renderContacts() {
    const contacts = visibleContacts();
    const rows = contacts.map((contact) => {
      const id = contactId(contact);
      const row = document.createElement("tr");
      const selectionCell = document.createElement("td");
      selectionCell.className = "select-cell";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedContacts.has(id);
      checkbox.disabled = !id;
      checkbox.setAttribute("aria-label", `选择 ${firstValue(contact, ["display_name", "name", "nickname"], "联系人")}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedContacts.add(id);
        else state.selectedContacts.delete(id);
        updateContactSelectionState();
      });
      selectionCell.append(checkbox);

      const nameCell = document.createElement("td");
      const name = document.createElement("div");
      name.className = "contact-name";
      const primary = document.createElement("strong");
      primary.textContent = firstValue(contact, ["display_name", "name", "nickname"], "未命名联系人");
      const secondary = document.createElement("span");
      secondary.textContent = firstValue(contact, ["remark", "alias"], "无备注");
      name.append(primary, secondary);
      nameCell.append(name);

      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(firstValue(contact, ["sync_status", "status"], "synced")));

      const policyCell = document.createElement("td");
      const policySelect = document.createElement("select");
      policySelect.setAttribute("aria-label", `${primary.textContent} 回复策略`);
      [
        ["ai_auto", "AI 自动回复"],
        ["manual", "仅人工"],
        ["paused", "暂停"],
        ["blocked", "屏蔽"],
      ].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        policySelect.append(option);
      });
      policySelect.value = contactPolicy(contact);
      policyCell.append(policySelect);

      const welcomeCell = document.createElement("td");
      const welcomeLabel = document.createElement("label");
      welcomeLabel.className = "welcome-switch";
      const welcome = document.createElement("input");
      welcome.type = "checkbox";
      welcome.checked = contactWelcomeEnabled(contact);
      const welcomeText = document.createElement("span");
      welcomeText.textContent = welcome.checked ? "开启" : "关闭";
      welcome.addEventListener("change", () => { welcomeText.textContent = welcome.checked ? "开启" : "关闭"; });
      welcomeLabel.append(welcome, welcomeText);
      welcomeCell.append(welcomeLabel);

      const actionCell = document.createElement("td");
      const save = document.createElement("button");
      save.type = "button";
      save.className = "button button-secondary button-small";
      save.textContent = "保存";
      save.disabled = !id;
      save.addEventListener("click", () => updateContactPolicy(contact, policySelect.value, welcome.checked, save));
      actionCell.append(save);

      row.append(
        selectionCell,
        nameCell,
        makeCell(contactType(contact) === "group" ? "群聊" : "好友"),
        makeCell(shortId(firstValue(contact, ["external_contact_id", "external_id", "wechat_id"])), "cell-id"),
        statusCell,
        policyCell,
        welcomeCell,
        makeCell(formatTime(firstValue(contact, ["last_synced_at", "synced_at", "updated_at"], ""))),
        actionCell,
      );
      return row;
    });
    renderRows(byId("contacts-body"), rows, 9, "没有符合条件的联系人");
    byId("contact-count").textContent = `${contacts.length} 个`;
    updateContactSelectionState(contacts);
  }

  function updateContactSelectionState(contacts = visibleContacts()) {
    const ids = contacts.map(contactId).filter(Boolean);
    const selectedVisible = ids.filter((id) => state.selectedContacts.has(id)).length;
    const selectAll = byId("contacts-select-all");
    selectAll.checked = ids.length > 0 && selectedVisible === ids.length;
    selectAll.indeterminate = selectedVisible > 0 && selectedVisible < ids.length;
    selectAll.disabled = !ids.length;
    byId("contact-selected-count").textContent = String(state.selectedContacts.size);
    byId("contact-batch-form").hidden = state.selectedContacts.size === 0;
  }

  async function updateContactPolicy(contact, policy, welcomeEnabled, button) {
    const id = contactId(contact);
    if (!id) return;
    if (policy === "blocked") {
      const confirmed = await confirmAction({
        title: "确认屏蔽联系人",
        message: "屏蔽后，该联系人或群聊的消息不会进入自动回复。确认保存此策略？",
        confirmLabel: "确认屏蔽",
      });
      if (!confirmed) return;
    }
    button.disabled = true;
    try {
      await api(`/v1/contacts/${encodeURIComponent(id)}/policy`, {
        method: "PUT",
        headers: { "Idempotency-Key": newIdempotencyKey(`contact-${id}`) },
        body: { policy, welcome_enabled: welcomeEnabled },
      });
      showToast("联系人策略已保存");
      await loadContacts();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function updateContactPolicies(event) {
    event.preventDefault();
    if (!state.selectedContacts.size) return;
    const form = event.currentTarget;
    const policy = byId("contact-batch-policy").value;
    if (policy === "blocked") {
      const confirmed = await confirmAction({
        title: "批量屏蔽联系人",
        message: `将屏蔽已选择的 ${state.selectedContacts.size} 个联系人。屏蔽后不会自动回复，确认继续？`,
        confirmLabel: "屏蔽选中项",
      });
      if (!confirmed) return;
    }
    const body = {
      channel_account_id: byId("contact-channel").value,
      contact_ids: Array.from(state.selectedContacts),
      policy,
    };
    const welcome = byId("contact-batch-welcome").value;
    if (welcome !== "keep") body.welcome_enabled = welcome === "on";
    try {
      setFormBusy(form, true);
      await api("/v1/contacts/policies/batch", {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey("contacts-batch") },
        body,
      });
      state.selectedContacts.clear();
      showToast("批量联系人策略已提交");
      await loadContacts();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  function friendRequestId(request) {
    return String(firstValue(request, ["id", "friend_request_id"], ""));
  }

  function friendRequestActionable(request) {
    return !["queued", "claimed", "executing", "uncertain"].includes(
      String(request.operation_status || "").toLowerCase(),
    );
  }

  async function loadFriendRequests() {
    setViewState("friend-requests", "loading", "正在加载好友申请…");
    await ensureChannels();
    const channelId = byId("friend-request-channel").value;
    if (!channelId) {
      state.friendRequests = [];
      state.selectedFriendRequests.clear();
      renderFriendRequests();
      setViewState("friend-requests", "empty", "请先绑定一个微信渠道账号");
      return;
    }
    const payload = await api(`/v1/friend-requests${queryString({ channel_account_id: channelId, status: "pending", limit: 500 })}`);
    state.friendRequests = extractArray(payload, "friend_requests", "requests")
      .filter((request) => (!request.channel_account_id || String(request.channel_account_id) === channelId)
        && (!request.status || String(request.status).toLowerCase() === "pending"));
    state.selectedFriendRequests.clear();
    renderFriendRequests();
    setViewState("friend-requests", state.friendRequests.length ? "" : "empty", state.friendRequests.length ? "" : "该渠道暂无待处理好友申请");
  }

  function renderFriendRequests() {
    const rows = state.friendRequests.map((request) => {
      const id = friendRequestId(request);
      const row = document.createElement("tr");
      const selectionCell = document.createElement("td");
      selectionCell.className = "select-cell";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedFriendRequests.has(id);
      checkbox.disabled = !id || !friendRequestActionable(request);
      checkbox.setAttribute("aria-label", `选择 ${firstValue(request, ["display_name", "nickname", "name"], "好友申请")}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedFriendRequests.add(id);
        else state.selectedFriendRequests.delete(id);
        updateFriendRequestSelectionState();
      });
      selectionCell.append(checkbox);
      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(firstValue(request, ["operation_status", "status"], "pending")));
      row.append(
        selectionCell,
        makeCell(firstValue(request, ["display_name", "nickname", "name"], "未命名申请人")),
        makeCell(firstValue(request, ["verification_message", "message", "greeting"]), "cell-content"),
        makeCell(shortId(firstValue(request, ["external_request_id", "external_contact_id", "wechat_id"])), "cell-id"),
        makeCell(formatTime(firstValue(request, ["requested_at", "created_at", "received_at"], ""))),
        statusCell,
      );
      return row;
    });
    renderRows(byId("friend-requests-body"), rows, 6, "暂无待处理好友申请");
    byId("friend-request-count").textContent = `${state.friendRequests.length} 个待处理`;
    updateFriendRequestSelectionState();
  }

  function updateFriendRequestSelectionState() {
    const ids = state.friendRequests.filter(friendRequestActionable).map(friendRequestId).filter(Boolean);
    const selectedCount = ids.filter((id) => state.selectedFriendRequests.has(id)).length;
    const selectAll = byId("friend-requests-select-all");
    selectAll.checked = ids.length > 0 && selectedCount === ids.length;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < ids.length;
    selectAll.disabled = !ids.length;
    byId("friend-request-selected-count").textContent = `已选择 ${state.selectedFriendRequests.size} 个`;
    byId("friend-approve-selected").disabled = state.selectedFriendRequests.size === 0;
    byId("friend-reject-selected").disabled = state.selectedFriendRequests.size === 0;
    byId("friend-approve-batch").disabled = ids.length === 0;
    byId("friend-approve-all").disabled = ids.length === 0;
  }

  async function runFriendRequestAction(action) {
    const ids = Array.from(state.selectedFriendRequests);
    const batchSize = Number(byId("friend-request-batch-size").value);
    if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 100) {
      showToast("本批数量必须在 1 到 100 之间", "error");
      byId("friend-request-batch-size").focus();
      return;
    }
    if (["approve-selected", "reject-selected"].includes(action) && !ids.length) return;
    if (action === "reject-selected") {
      const confirmed = await confirmAction({
        title: "拒绝好友申请",
        message: `将拒绝选中的 ${ids.length} 个好友申请。该操作会由 Windows Agent 实际执行，确认继续？`,
        confirmLabel: "确认拒绝",
      });
      if (!confirmed) return;
    }
    if (action === "approve-batch") {
      const confirmed = await confirmAction({
        title: "通过本批好友申请",
        message: `最多将 ${batchSize} 个待处理申请加入受限队列，并受每小时、每日上限约束。确认继续？`,
        confirmLabel: "加入本批队列",
        danger: false,
      });
      if (!confirmed) return;
    }
    if (action === "approve-all") {
      const confirmed = await confirmAction({
        title: "全部加入受限队列",
        message: `当前 ${state.friendRequests.length} 个待处理申请会全部加入队列，但仍按批量、间隔、每小时和每日上限执行，不会立即连续操作微信。确认继续？`,
        confirmLabel: "确认全部入队",
      });
      if (!confirmed) return;
    }

    const channelId = byId("friend-request-channel").value;
    const sendWelcomeMessage = action !== "reject-selected" && byId("friend-request-welcome").checked;
    const buttons = ["friend-approve-selected", "friend-reject-selected", "friend-approve-batch", "friend-approve-all"].map(byId);
    buttons.forEach((button) => { button.disabled = true; });
    try {
      let welcomeMessage = "";
      if (sendWelcomeMessage) {
        const limitsPayload = await api(`/v1/operation-limits?channel_account_id=${encodeURIComponent(channelId)}`);
        const limits = extractObject(limitsPayload, "operation_limits", "limits", "settings") || {};
        const persistedMessage = firstValue(limits, ["welcome_message"], "");
        welcomeMessage = persistedMessage === "-" ? "" : String(persistedMessage).trim();
        if (!welcomeMessage) throw new Error("运营设置中尚未配置欢迎语，请先保存欢迎语或关闭发送欢迎语");
      }
      const body = {
        channel_account_id: channelId,
        send_welcome_message: sendWelcomeMessage,
        welcome_message: welcomeMessage,
        auto_enable_reply: byId("friend-request-auto-reply").checked,
      };
      if (["approve-selected", "reject-selected"].includes(action)) body.friend_request_ids = ids;
      if (action === "approve-batch") body.batch_size = batchSize;
      await api(FRIEND_REQUEST_ENDPOINTS[action], {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey(`friend-${action}`) },
        body,
      });
      state.selectedFriendRequests.clear();
      showToast(action === "reject-selected" ? "拒绝任务已创建" : "好友申请处理任务已加入受限队列");
      await loadFriendRequests();
    } catch (error) {
      showToast(apiErrorText(error), "error");
      updateFriendRequestSelectionState();
    }
  }

  async function loadOperationJobs() {
    setViewState("operation-jobs", "loading", "正在加载运营任务…");
    await ensureChannels();
    const channelId = byId("operation-job-channel").value;
    const status = byId("operation-job-status").value;
    const payload = await api(`/v1/operation-jobs${queryString({ channel_account_id: channelId, status, limit: 500 })}`);
    state.operationJobs = extractArray(payload, "operation_jobs", "jobs")
      .filter((job) => (!channelId || !job.channel_account_id || String(job.channel_account_id) === channelId)
        && (!status || String(job.status).toLowerCase() === status))
      .slice()
      .reverse();
    renderOperationJobs();
    setViewState("operation-jobs", state.operationJobs.length ? "" : "empty", state.operationJobs.length ? "" : "暂无符合条件的运营任务");
  }

  function operationTypeLabel(value) {
    const labels = {
      accept_friend: "通过好友申请",
      reject_friend: "拒绝好友申请",
      set_contact_policy: "更新联系人策略",
      send_welcome: "发送欢迎语",
    };
    return labels[value] || value || "微信运营任务";
  }

  function renderOperationJobs() {
    const rows = state.operationJobs.map((job) => {
      const id = String(firstValue(job, ["id", "operation_job_id"], ""));
      const payload = job.payload || job.payload_json || {};
      const status = String(firstValue(job, ["status"], "created")).toLowerCase();
      const row = document.createElement("tr");
      row.append(
        makeCell(formatTime(firstValue(job, ["created_at"], ""))),
        makeCell(operationTypeLabel(firstValue(job, ["operation_type", "type", "action"], payload.operation_type))),
        makeCell(shortId(firstValue(job, ["target_display_name", "target_id", "friend_request_id", "contact_id"], firstValue(payload, ["target_display_name", "target_id", "friend_request_id", "contact_id"]))), "cell-id"),
      );
      const statusCell = document.createElement("td");
      statusCell.append(statusBadge(status));
      row.append(
        statusCell,
        makeCell(firstValue(job, ["attempts", "attempt_count"], 0)),
        makeCell(firstValue(job, ["last_error", "error"]), "cell-error"),
        makeCell(formatTime(firstValue(job, ["updated_at", "completed_at", "claimed_at"], ""))),
      );
      const actionCell = document.createElement("td");
      const actions = document.createElement("div");
      actions.className = "action-row";
      if (status === "queued") {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "button button-danger-outline button-small";
        cancel.textContent = "取消";
        cancel.addEventListener("click", () => cancelOperationJob(id, cancel));
        actions.append(cancel);
      }
      if (["failed", "cancelled"].includes(status)) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "button button-secondary button-small";
        retry.textContent = "重试";
        retry.addEventListener("click", () => retryOperationJob(id, retry));
        actions.append(retry);
      }
      if (status === "uncertain") {
        const resolve = document.createElement("button");
        resolve.type = "button";
        resolve.className = "button button-primary button-small";
        resolve.textContent = "人工确认";
        resolve.addEventListener("click", () => openOperationResolve(id));
        actions.append(resolve);
      }
      if (!actions.childElementCount) actions.textContent = "-";
      actionCell.append(actions);
      row.append(actionCell);
      return row;
    });
    renderRows(byId("operation-jobs-body"), rows, 8, "暂无符合条件的运营任务");
    byId("operation-job-count").textContent = `${state.operationJobs.length} 个`;
  }

  async function cancelOperationJob(id, button) {
    if (!id) return;
    const confirmed = await confirmAction({
      title: "取消运营任务",
      message: "只能取消尚未实际执行的任务。若 Agent 已开始操作，服务端会拒绝取消。确认继续？",
      confirmLabel: "确认取消",
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      await api(`/v1/operation-jobs/${encodeURIComponent(id)}/cancel`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey(`operation-cancel-${id}`) },
        body: { reason: "cancelled_by_tenant_admin" },
      });
      showToast("运营任务已取消");
      await loadOperationJobs();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function retryOperationJob(id, button) {
    if (!id) return;
    button.disabled = true;
    try {
      await api(`/v1/operation-jobs/${encodeURIComponent(id)}/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey(`operation-retry-${id}`) },
        body: {},
      });
      showToast("运营任务已重新排队");
      await loadOperationJobs();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      button.disabled = false;
    }
  }

  function openOperationResolve(id) {
    byId("operation-resolve-id").value = id;
    byId("operation-resolution").value = "succeeded";
    byId("operation-resolve-note").value = "";
    byId("operation-resolve-dialog").showModal();
  }

  async function resolveOperationJob(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const id = byId("operation-resolve-id").value;
    try {
      setFormBusy(form, true);
      await api(`/v1/operation-jobs/${encodeURIComponent(id)}/resolve`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey(`operation-resolve-${id}`) },
        body: {
          resolution: byId("operation-resolution").value,
          note: byId("operation-resolve-note").value.trim(),
        },
      });
      byId("operation-resolve-dialog").close();
      showToast("运营任务结果已确认");
      await loadOperationJobs();
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  function setOperationSettingsAvailable(available) {
    all("#operation-limits-form input, #operation-limits-form textarea, #operation-limits-form button")
      .forEach((control) => { control.disabled = !available; });
    byId("operation-pause").disabled = !available;
    byId("operation-resume").disabled = !available;
  }

  async function loadOperationSettings() {
    setViewState("operation-settings", "loading", "正在加载运营设置…");
    await ensureChannels();
    const channelId = byId("operation-settings-channel").value;
    if (!channelId) {
      state.operationLimits = null;
      setOperationSettingsAvailable(false);
      setViewState("operation-settings", "empty", "请先绑定一个微信渠道账号");
      return;
    }
    setOperationSettingsAvailable(true);
    const payload = await api(`/v1/operation-limits?channel_account_id=${encodeURIComponent(channelId)}`);
    state.operationLimits = extractObject(payload, "operation_limits", "limits", "settings") || {};
    renderOperationLimits(state.operationLimits);
    setViewState("operation-settings", "", "");
  }

  function renderOperationLimits(limits) {
    const paused = Boolean(limits.paused);
    byId("operation-batch-size").value = firstValue(limits, ["batch_size"], 10);
    byId("operation-min-interval").value = firstValue(limits, ["min_interval_seconds"], 5);
    byId("operation-max-hour").value = firstValue(limits, ["max_per_hour"], 30);
    byId("operation-max-day").value = firstValue(limits, ["max_per_day"], 100);
    const message = firstValue(limits, ["welcome_message"], "");
    byId("operation-welcome-message").value = message === "-" ? "" : message;
    byId("operation-auto-enable").checked = Boolean(limits.auto_enable_reply);
    byId("operation-paused").checked = paused;
    byId("operation-paused-status").textContent = paused ? "当前已暂停，Agent 不会领取新的运营任务" : "当前运行中，任务按限制分批执行";
    byId("operation-pause").disabled = paused;
    byId("operation-resume").disabled = !paused;
  }

  async function saveOperationLimits(event = null, forcedPaused = null) {
    if (event) event.preventDefault();
    const form = byId("operation-limits-form");
    if (forcedPaused === null && !form.reportValidity()) return;
    const channelId = byId("operation-settings-channel").value;
    if (!channelId) return;
    const paused = forcedPaused === null ? byId("operation-paused").checked : forcedPaused;
    if (forcedPaused === true) {
      const confirmed = await confirmAction({
        title: "暂停微信运营",
        message: "暂停后，Agent 不再领取新的好友申请和联系人运营任务；已开始执行的任务不会被强行中断。确认暂停？",
        confirmLabel: "确认暂停",
      });
      if (!confirmed) return;
    }
    const current = state.operationLimits || {};
    const body = forcedPaused === null ? {
      paused,
      batch_size: Number(byId("operation-batch-size").value),
      min_interval_seconds: Number(byId("operation-min-interval").value),
      max_per_hour: Number(byId("operation-max-hour").value),
      max_per_day: Number(byId("operation-max-day").value),
      welcome_message: byId("operation-welcome-message").value.trim(),
      auto_enable_reply: byId("operation-auto-enable").checked,
    } : {
      paused,
      batch_size: Number(firstValue(current, ["batch_size"], 10)),
      min_interval_seconds: Number(firstValue(current, ["min_interval_seconds"], 5)),
      max_per_hour: Number(firstValue(current, ["max_per_hour"], 30)),
      max_per_day: Number(firstValue(current, ["max_per_day"], 100)),
      welcome_message: firstValue(current, ["welcome_message"], "") === "-" ? "" : String(firstValue(current, ["welcome_message"], "")),
      auto_enable_reply: Boolean(current.auto_enable_reply),
    };
    setFormBusy(form, true);
    byId("operation-pause").disabled = true;
    byId("operation-resume").disabled = true;
    try {
      const payload = await api(`/v1/operation-limits/${encodeURIComponent(channelId)}`, {
        method: "PUT",
        headers: { "Idempotency-Key": newIdempotencyKey(`operation-limits-${channelId}`) },
        body,
      });
      state.operationLimits = extractObject(payload, "operation_limits", "limits", "settings") || body;
      renderOperationLimits(state.operationLimits);
      showToast(forcedPaused === true ? "微信运营已暂停" : forcedPaused === false ? "微信运营已恢复" : "运营设置已保存");
    } catch (error) {
      showToast(apiErrorText(error), "error");
      renderOperationLimits(state.operationLimits || body);
    } finally {
      setFormBusy(form, false);
      renderOperationLimits(state.operationLimits || body);
    }
  }

  async function loadSettings() {
    setViewState("settings", "loading", "正在加载配置…");
    const [profileData, knowledgeData] = await Promise.all([
      api("/v1/bot-profile"),
      api("/v1/knowledge-base"),
    ]);
    renderProfile(extractObject(profileData, "profile"));
    renderKnowledge(extractObject(knowledgeData, "knowledge_base", "knowledge"));
    setViewState("settings", "", "");
  }

  function renderProfile(profile) {
    const settings = (profile && (profile.settings_json || profile.settings)) || {};
    const provider = settings.llm_provider || "openai_compatible";
    byId("profile-name").value = firstValue(profile, ["name"], "default");
    byId("profile-enabled").checked = Boolean(settings.enabled);
    byId("profile-prompt").value = firstValue(profile, ["prompt"], "") === "-" ? "" : firstValue(profile, ["prompt"], "");
    byId("profile-provider").value = provider;
    byId("profile-model").value = settings.llm_model || (provider === "ollama" ? "qwen2.5:7b" : "gpt-4.1-mini");
    byId("profile-base-url").value = provider === "ollama"
      ? (settings.ollama_base_url || "http://127.0.0.1:11434")
      : (settings.openai_base_url || "https://api.openai.com/v1");
    byId("profile-credential-name").value = settings.api_credential_name || "default";
    byId("profile-timeout").value = settings.request_timeout || 30;
    byId("profile-context").value = settings.max_context_messages || 20;
  }

  function renderKnowledge(knowledge) {
    byId("knowledge-name").value = firstValue(knowledge, ["name"], "default");
    byId("knowledge-status").value = firstValue(knowledge, ["status"], "active");
    const content = firstValue(knowledge, ["content"], "");
    byId("knowledge-content").value = content === "-" ? "" : content;
    updateKnowledgeCount();
  }

  function updateProviderUrl(force = false) {
    const input = byId("profile-base-url");
    const provider = byId("profile-provider").value;
    const knownDefaults = ["https://api.openai.com/v1", "http://127.0.0.1:11434"];
    if (force || !input.value || knownDefaults.includes(input.value)) {
      input.value = provider === "ollama" ? "http://127.0.0.1:11434" : "https://api.openai.com/v1";
    }
  }

  async function saveProfile(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const provider = byId("profile-provider").value;
    const settings = {
      enabled: byId("profile-enabled").checked,
      llm_provider: provider,
      llm_model: byId("profile-model").value.trim(),
      request_timeout: Number(byId("profile-timeout").value),
      max_context_messages: Number(byId("profile-context").value),
      api_credential_name: byId("profile-credential-name").value.trim(),
    };
    settings[provider === "ollama" ? "ollama_base_url" : "openai_base_url"] = byId("profile-base-url").value.trim();
    try {
      setFormBusy(form, true);
      await api("/v1/bot-profile", {
        method: "PUT",
        headers: { "Idempotency-Key": newIdempotencyKey("bot-profile") },
        body: {
          name: byId("profile-name").value.trim(),
          prompt: byId("profile-prompt").value.trim(),
          settings,
        },
      });
      showToast("机器人配置已保存");
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  async function saveKnowledge(event) {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      setFormBusy(form, true);
      await api("/v1/knowledge-base", {
        method: "PUT",
        headers: { "Idempotency-Key": newIdempotencyKey("knowledge-base") },
        body: {
          name: byId("knowledge-name").value.trim(),
          status: byId("knowledge-status").value,
          content: byId("knowledge-content").value,
        },
      });
      showToast("知识库已保存");
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  async function saveCredential(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const name = byId("credential-name").value.trim();
    if (!/^[A-Za-z0-9._-]{1,100}$/.test(name)) {
      showToast("凭据名称只能包含字母、数字、点、下划线和短横线", "error");
      return;
    }
    try {
      setFormBusy(form, true);
      await api(`/v1/ai-credentials/${name}`, {
        method: "PUT",
        headers: { "Idempotency-Key": newIdempotencyKey(`ai-credential-${name}`) },
        body: { value: byId("credential-value").value },
      });
      byId("credential-value").value = "";
      showToast("AI 凭据已更新");
    } catch (error) {
      showToast(apiErrorText(error), "error");
    } finally {
      setFormBusy(form, false);
    }
  }

  function updateKnowledgeCount() {
    byId("knowledge-count").textContent = `${byId("knowledge-content").value.length} / 20000`;
  }

  async function loadAudit() {
    setViewState("audit", "loading", "正在加载审计记录…");
    const payload = await api("/v1/audit?limit=500");
    state.audit = extractArray(payload, "events", "audit_events");
    const rows = state.audit.map((event) => {
      const details = event.details || event.details_json || {};
      const row = document.createElement("tr");
      row.append(
        makeCell(formatTime(event.created_at)),
        makeCell(firstValue(event, ["action"])),
        makeCell(shortId(firstValue(event, ["actor_user_id", "actor_id"]))),
        makeCell(`${firstValue(event, ["entity_type"], "")} ${shortId(firstValue(event, ["entity_id"], ""))}`.trim(), "cell-id"),
        makeCell(Object.keys(details).length ? JSON.stringify(details) : "-", "cell-content"),
      );
      return row;
    });
    renderRows(byId("audit-body"), rows, 5, "暂无审计记录");
    byId("audit-count").textContent = `${state.audit.length} 条`;
    setViewState("audit", "", "");
  }

  function wireEvents() {
    byId("login-form").addEventListener("submit", login);
    byId("logout-button").addEventListener("click", logout);
    byId("refresh-button").addEventListener("click", loadCurrentView);
    all("[data-view]").forEach((button) => button.addEventListener("click", () => activateView(button.dataset.view)));
    byId("reply-status").addEventListener("change", loadReplyJobs);
    byId("delivery-status").addEventListener("change", renderDeliveries);
    byId("delivery-form").addEventListener("submit", createDelivery);
    byId("contact-channel").addEventListener("change", () => {
      state.selectedContacts.clear();
      loadContacts().catch((error) => setViewState("contacts", "error", apiErrorText(error)));
    });
    byId("contact-search").addEventListener("input", renderContacts);
    byId("contact-type-filter").addEventListener("change", renderContacts);
    byId("contact-policy-filter").addEventListener("change", renderContacts);
    byId("contacts-select-all").addEventListener("change", (event) => {
      visibleContacts().forEach((contact) => {
        const id = contactId(contact);
        if (!id) return;
        if (event.currentTarget.checked) state.selectedContacts.add(id);
        else state.selectedContacts.delete(id);
      });
      renderContacts();
    });
    byId("contact-batch-form").addEventListener("submit", updateContactPolicies);
    byId("friend-request-channel").addEventListener("change", () => {
      loadFriendRequests().catch((error) => setViewState("friend-requests", "error", apiErrorText(error)));
    });
    byId("friend-requests-select-all").addEventListener("change", (event) => {
      state.selectedFriendRequests.clear();
      if (event.currentTarget.checked) {
        state.friendRequests.map(friendRequestId).filter(Boolean).forEach((id) => state.selectedFriendRequests.add(id));
      }
      renderFriendRequests();
    });
    byId("friend-approve-selected").addEventListener("click", () => runFriendRequestAction("approve-selected"));
    byId("friend-reject-selected").addEventListener("click", () => runFriendRequestAction("reject-selected"));
    byId("friend-approve-batch").addEventListener("click", () => runFriendRequestAction("approve-batch"));
    byId("friend-approve-all").addEventListener("click", () => runFriendRequestAction("approve-all"));
    byId("operation-job-channel").addEventListener("change", () => {
      loadOperationJobs().catch((error) => setViewState("operation-jobs", "error", apiErrorText(error)));
    });
    byId("operation-job-status").addEventListener("change", () => {
      loadOperationJobs().catch((error) => setViewState("operation-jobs", "error", apiErrorText(error)));
    });
    byId("operation-settings-channel").addEventListener("change", () => {
      loadOperationSettings().catch((error) => setViewState("operation-settings", "error", apiErrorText(error)));
    });
    byId("operation-limits-form").addEventListener("submit", saveOperationLimits);
    byId("operation-pause").addEventListener("click", () => saveOperationLimits(null, true));
    byId("operation-resume").addEventListener("click", () => saveOperationLimits(null, false));
    byId("profile-form").addEventListener("submit", saveProfile);
    byId("knowledge-form").addEventListener("submit", saveKnowledge);
    byId("credential-form").addEventListener("submit", saveCredential);
    byId("knowledge-content").addEventListener("input", updateKnowledgeCount);
    byId("profile-provider").addEventListener("change", () => updateProviderUrl(true));
    byId("resolve-form").addEventListener("submit", resolveDelivery);
    byId("resolve-close").addEventListener("click", () => byId("resolve-dialog").close());
    byId("resolve-cancel").addEventListener("click", () => byId("resolve-dialog").close());
    byId("operation-resolve-form").addEventListener("submit", resolveOperationJob);
    byId("operation-resolve-close").addEventListener("click", () => byId("operation-resolve-dialog").close());
    byId("operation-resolve-cancel").addEventListener("click", () => byId("operation-resolve-dialog").close());
    byId("confirm-form").addEventListener("submit", (event) => {
      event.preventDefault();
      closeConfirmation(true);
    });
    byId("confirm-close").addEventListener("click", () => closeConfirmation(false));
    byId("confirm-cancel").addEventListener("click", () => closeConfirmation(false));
    byId("confirm-dialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      closeConfirmation(false);
    });
  }

  wireEvents();
  restoreSession();
})();
