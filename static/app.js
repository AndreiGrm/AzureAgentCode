const POLL_MS = 2000;

let cfg = null;
let ticketFilter = null;
let currentNav = "dashboard"; // Voce del menu laterale attiva.
let currentView = { type: "list" };
let lastStatus = null;
let correctionSubmitting = false;
let fixSubmitting = false;
let reviewRunning = false;
let dashboardLoading = false;
let qualityRunning = false;
let currentTicketTab = "active";
let currentDetailTab = "details";

const ticketInfoCache = new Map();
const ticketInfoRequests = new Map();

const ACTION_LABELS = {
  branch_created: "Branch creato",
  generating_plan: "Generazione piano",
  plan_ready: "Piano generato",
  plan_approved: "Piano approvato",
  implementing: "Implementazione in corso",
  implemented: "Pronto per la verifica",
  pr_opened: "PR aperta",
  blocked: "Bloccato",
  stopped: "Fermato manualmente",
  decomposing: "Scomposizione Epic",
  epic_decomposed: "Epic scomposta",
  classifying: "Valutazione commento",
  mechanical_fix: "Fix meccanico",
  needs_human: "Serve revisione umana",
  autofix_applied: "Autofix lint/format",
  fixing: "Applicazione fix in corso",
  fix_applied: "Fix applicato",
  fix_requested: "Fix richiesto",
  self_review: "Self-review completata",
  quality_passed: "Verifiche tecniche superate",
  quality_failed: "Verifiche tecniche fallite",
  correction_sent: "Correzione inviata",
  pr_checked: "Controllo PR completato",
  comment_resolved: "Commento risolto",
  comment_resolve_failed: "Fix commento non riuscito",
  comment_skipped: "Commento ignorato",
  comment_batch_planned: "Piano correzioni PR pronto",
  comment_batch_applied: "Correzioni PR applicate",
  comment_batch_failed: "Correzioni PR non completate",
  comment_batch_committed: "Correzioni PR pubblicate",
  comment_batch_commit_failed: "Commit correzioni PR non riuscito",
  ticket_chat_planned: "Piano dalla chat pronto",
  restart_requested: "Ripartenza da zero richiesta",
  external_change: "Aggiornato su Azure Boards",
  external_completed: "Completato su Azure Boards",
  pr_completed: "PR completata",
  pr_abandoned: "PR abbandonata",
  closed: "Chiuso manualmente",
  deleted: "Eliminato",
  reopened: "Riaperto",
  error: "Errore",
};

const LIVE_ACTIONS = ["implementing", "fixing", "classifying"];
const VERIFY_ACTIONS = ["implemented", "fix_applied", "quality_passed", "quality_failed"];
const PR_REVIEW_ACTIONS = [
  "pr_opened", "pr_checked", "mechanical_fix", "autofix_applied", "needs_human",
  "comment_batch_planned", "comment_batch_applied", "comment_batch_failed",
  "comment_batch_committed", "comment_batch_commit_failed",
];
const COMPLETED_ACTIONS = ["pr_completed", "pr_abandoned", "closed", "external_completed"];
const TICKET_FLOW = [
  { id: "loaded", label: "Caricati", description: "In attesa di essere analizzati" },
  { id: "plan", label: "Piano pronto", description: "Richiedono l'approvazione del piano" },
  { id: "progress", label: "In lavorazione", description: "L'agente sta lavorando o verificando" },
  { id: "review", label: "Attende revisione", description: "Richiedono una tua azione o revisione" },
  { id: "pr", label: "PR completata", description: "Lavorazione conclusa" },
];

function ticketFlowStage(action) {
  if (isCompletedAction(action)) return "pr";
  if (PR_REVIEW_ACTIONS.includes(action)) return "review";
  if (LIVE_ACTIONS.includes(action) || VERIFY_ACTIONS.includes(action)) return "progress";
  if (["plan_ready", "plan_approved", "ticket_chat_planned"].includes(action)) return "plan";
  return "loaded";
}

function ticketStatusTone(action) {
  if (["error", "blocked", "quality_failed", "comment_batch_failed",
    "comment_batch_commit_failed", "comment_resolve_failed"].includes(action)) {
    return "error";
  }
  if (["plan_ready", "implemented", "quality_passed", "pr_opened",
    "needs_human", "fix_requested", "correction_sent", "comment_batch_planned"].includes(action)) {
    return "waiting";
  }
  if (LIVE_ACTIONS.includes(action) || ["generating_plan", "decomposing", "mechanical_fix"].includes(action)) {
    return "running";
  }
  if (isCompletedAction(action) || ["plan_approved", "epic_decomposed", "self_review",
    "fix_applied", "autofix_applied", "pr_checked", "comment_batch_applied",
    "comment_batch_committed", "comment_resolved", "comment_skipped"].includes(action)) {
    return "done";
  }
  return "neutral";
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action;
}

function isCompletedAction(action) {
  return COMPLETED_ACTIONS.includes(action);
}

function stageForAction(action) {
  if (action === "plan_ready") return "plan";
  if (LIVE_ACTIONS.includes(action)) return "live";
  if (VERIFY_ACTIONS.includes(action)) return "verify";
  if (PR_REVIEW_ACTIONS.includes(action)) return "pr-review";
  if (isCompletedAction(action)) return "completed";
  return "none";
}

function workItemUrl(id) {
  if (!cfg || !id) return null;
  return `${cfg.org_url}/${cfg.project}/_workitems/edit/${id}`;
}

function prUrl(id) {
  if (!cfg || !id) return null;
  return `${cfg.org_url}/${cfg.project}/_git/${cfg.repo_id}/pullrequest/${id}`;
}

function formatTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "medium" });
}

function showDialog({ title, message, confirmLabel = "Conferma", cancelLabel = "Annulla", isError = false }) {
  const dialog = document.getElementById("app-dialog");
  const titleEl = document.getElementById("app-dialog-title");
  const messageEl = document.getElementById("app-dialog-message");
  const confirmBtn = document.getElementById("app-dialog-confirm");
  const cancelBtn = document.getElementById("app-dialog-cancel");

  titleEl.textContent = title;
  messageEl.textContent = message;
  confirmBtn.textContent = confirmLabel;
  confirmBtn.classList.toggle("dialog-error-action", isError);
  cancelBtn.hidden = isError;

  return new Promise((resolve) => {
    const close = () => {
      dialog.removeEventListener("close", onClose);
      cancelBtn.removeEventListener("click", onCancel);
      resolve(dialog.returnValue === "confirm");
    };
    const onClose = close;
    const onCancel = () => dialog.close("cancel");

    dialog.addEventListener("close", onClose, { once: true });
    cancelBtn.addEventListener("click", onCancel, { once: true });
    dialog.showModal();
    confirmBtn.focus();
  });
}

function confirmAction(message, confirmLabel = "Conferma") {
  return showDialog({ title: "Conferma azione", message, confirmLabel });
}

async function showError(message) {
  await showDialog({
    title: "Operazione non riuscita",
    message,
    confirmLabel: "Chiudi",
    isError: true,
  });
}

let pendingRequests = 0;
let actionFeedbackTimer = null;

function showActionFeedback(message, isError = false) {
  const feedback = document.getElementById("action-feedback");
  feedback.textContent = message;
  feedback.classList.toggle("error", isError);
  feedback.hidden = false;
  window.clearTimeout(actionFeedbackTimer);
  actionFeedbackTimer = window.setTimeout(() => { feedback.hidden = true; }, 5000);
}

async function refreshAfterTicketMutation() {
  await Promise.all([
    refreshTickets(true),
    refreshHistory(true),
    refreshStatus(true),
    currentNav === "dashboard" ? loadDashboard() : Promise.resolve(),
    currentNav === "notifications" ? loadNotifications(true) : Promise.resolve(),
  ]);
}

function setRequestLoading(isLoading, label) {
  const loader = document.getElementById("request-loader");
  if (isLoading) {
    pendingRequests += 1;
    document.getElementById("request-loader-label").textContent = label;
    loader.hidden = false;
    return;
  }

  pendingRequests = Math.max(0, pendingRequests - 1);
  loader.hidden = pendingRequests === 0;
}

async function fetchJson(url, options, { showLoader = true, loadingLabel } = {}) {
  const method = (options && options.method) || "GET";
  const label = loadingLabel || (method === "GET" ? "Caricamento dati..." : "Elaborazione richiesta...");
  if (showLoader) setRequestLoading(true, label);
  try {
    const res = await fetch(url, options);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Errore ${res.status}`);
    }
    return res.json();
  } finally {
    if (showLoader) setRequestLoading(false);
  }
}

async function loadConfig() {
  cfg = await fetchJson("/api/config");
}

function renderStatusCard(script, data) {
  const card = document.getElementById(`status-${script}`);
  const line = card.querySelector(".status-line");
  const stopBtn = card.querySelector(".stop-btn");
  const blockBtn = card.querySelector(".block-btn");
  const ev = data.latest_event;
  const workItemId = ev ? ev.work_item_id : null;

  if (data.active) {
    card.classList.add("active");
    const ticket = workItemId ? `ticket #${workItemId}` : "";
    const step = ev ? actionLabel(ev.action) : "avvio in corso...";
    line.textContent = `In esecuzione — ${ticket} ${step}`.trim();
    stopBtn.hidden = false;
    if (workItemId) {
      blockBtn.hidden = false;
      blockBtn.textContent = `Blocca ticket #${workItemId}`;
      blockBtn.dataset.workItemId = workItemId;
    } else {
      blockBtn.hidden = true;
    }
  } else {
    card.classList.remove("active");
    line.textContent = "Inattivo";
    stopBtn.hidden = true;
    blockBtn.hidden = true;
  }
}

async function refreshStatus(silent = false) {
  const status = await fetchJson("/api/status", undefined, { showLoader: !silent });
  lastStatus = status;
  const anyActive = status.ingest.active || status.review.active;
  for (const script of ["ingest", "review"]) {
    renderStatusCard(script, status[script]);
  }
  document.getElementById("btn-ingest").disabled = anyActive;
  if (currentView.type === "detail") {
    updateCorrectionUI();
  }
}

async function refreshAutomaticIngestStatus(silent = false) {
  const data = await fetchJson(
    "/api/automatic-ingest/status",
    undefined,
    { showLoader: !silent }
  );
  const status = document.getElementById("automatic-ingest-status");
  const label = document.getElementById("automatic-ingest-label");
  const intervalMinutes = Math.round(data.interval_seconds / 60);
  const lastCheck = data.last_check ? `Ultimo controllo: ${formatTime(data.last_check)}.` : "";
  const nextCheck = data.next_check ? ` Prossimo: ${formatTime(data.next_check)}.` : "";
  label.textContent = `Controllo automatico ogni ${intervalMinutes} minuti. ${lastCheck}${nextCheck}`;
  status.classList.toggle("error", String(data.outcome).startsWith("error:"));
  status.title = `Esito: ${data.outcome}`;
}

async function stopRun(script) {
  if (!await confirmAction(`Fermare il run di ${script} in corso?`, "Ferma")) return;
  try {
    await fetchJson(`/api/stop/${script}`, { method: "POST" });
    showActionFeedback(`Run ${script} fermato. Stato aggiornato.`);
  } catch (err) {
    showActionFeedback(`Impossibile fermare ${script}: ${err.message}`, true);
    await showError(`Impossibile fermare ${script}: ${err.message}`);
  }
  await refreshAfterTicketMutation();
}

async function blockWorkItem(workItemId) {
  if (!await confirmAction(
    `Taggare il ticket #${workItemId} come agent:blocked? Non verra' piu' ripreso automaticamente.`,
    "Blocca"
  )) return;
  try {
    await fetchJson(`/api/block/${workItemId}`, { method: "POST" });
    showActionFeedback(`Ticket #${workItemId} bloccato. Vista aggiornata.`);
  } catch (err) {
    showActionFeedback(`Impossibile bloccare il ticket #${workItemId}: ${err.message}`, true);
    await showError(`Impossibile bloccare il ticket #${workItemId}: ${err.message}`);
  }
  await refreshAfterTicketMutation();
}

// --- Ticket ------------------------------------------------------------

function statusBadgeClass(action) {
  return `status-badge status-${ticketStatusTone(action)}`;
}

function ensureTicketLane(list, stageId) {
  let lane = list.querySelector(`.ticket-lane[data-stage="${stageId}"]`);
  if (lane) return lane;

  const stage = TICKET_FLOW.find((item) => item.id === stageId);
  lane = document.createElement("section");
  lane.className = "ticket-lane";
  lane.dataset.stage = stageId;

  const header = document.createElement("header");
  header.className = "ticket-lane-header";
  const heading = document.createElement("h3");
  heading.textContent = stage.label;
  const description = document.createElement("p");
  description.textContent = stage.description;
  const count = document.createElement("span");
  count.className = "ticket-lane-count";
  count.setAttribute("aria-label", "Numero di ticket");
  header.append(heading, count, description);

  const cards = document.createElement("div");
  cards.className = "ticket-lane-cards";
  lane.append(header, cards);
  list.append(lane);
  return lane;
}

function initializeTicketLanes(list, stages) {
  stages.forEach((stage) => ensureTicketLane(list, stage.id));
}

function updateTicketLaneCounts(list) {
  for (const lane of list.querySelectorAll(".ticket-lane")) {
    const cards = Array.from(lane.querySelectorAll(".ticket-card"));
    lane.querySelector(".ticket-lane-count").textContent = String(cards.length);
    let emptyState = lane.querySelector(".ticket-lane-empty");
    if (cards.length === 0 && !emptyState) {
      emptyState = document.createElement("p");
      emptyState.className = "ticket-lane-empty";
      emptyState.textContent = "Nessun ticket in questa fase";
      lane.querySelector(".ticket-lane-cards").append(emptyState);
    } else if (cards.length > 0 && emptyState) {
      emptyState.remove();
    }
    lane.hidden = false;
  }
}

function formatTicketUpdate(timestamp) {
  if (!timestamp) return "n/d";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? "n/d" : formatTime(timestamp);
}

function updateTicketCardMetadata(card, info) {
  card.querySelector(".ticket-card-title").textContent = info.title || "(senza titolo)";
  card.querySelector(".ticket-card-points").textContent =
    `Story points: ${info.story_points == null ? "n/d" : info.story_points}`;
}

function ensureTicketMetadata(workItemId, card) {
  const key = String(workItemId);
  if (ticketInfoCache.has(key)) {
    updateTicketCardMetadata(card, ticketInfoCache.get(key));
    return;
  }

  let request = ticketInfoRequests.get(key);
  if (!request) {
    request = fetchJson(`/api/work-item/${workItemId}`)
    .then((info) => {
      const metadata = {
        title: info.title || "(senza titolo)",
        story_points: info.story_points,
      };
      ticketInfoCache.set(key, metadata);
      return metadata;
    })
    .catch(() => {
      const metadata = { title: "?", story_points: null };
      ticketInfoCache.set(key, metadata);
      return metadata;
    })
    .finally(() => {
      ticketInfoRequests.delete(key);
    });
    ticketInfoRequests.set(key, request);
  }
  request.then((info) => updateTicketCardMetadata(card, info));
}

function renderTicketCard(ticket) {
  const card = document.createElement("article");
  card.className = "ticket-card";
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.dataset.workItemId = String(ticket.work_item_id);

  const header = document.createElement("div");
  header.className = "ticket-card-header";
  const id = document.createElement("span");
  id.className = "ticket-card-id";
  id.textContent = `#${ticket.work_item_id}`;
  const badge = document.createElement("span");
  badge.className = statusBadgeClass(ticket.action);
  header.append(id, badge);

  const title = document.createElement("div");
  title.className = "ticket-card-title";
  title.textContent = "Caricamento titolo...";

  const footer = document.createElement("div");
  footer.className = "ticket-card-footer";
  const points = document.createElement("span");
  points.className = "ticket-card-meta ticket-card-points";
  points.textContent = "Story points: …";
  const updated = document.createElement("span");
  updated.className = "ticket-card-updated";
  footer.append(points, updated);

  card.append(header, title, footer);
  updateTicketCard(card, ticket);
  ensureTicketMetadata(ticket.work_item_id, card);
  card.addEventListener("click", () => showDetailView(ticket.work_item_id));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      showDetailView(ticket.work_item_id);
    }
  });
  return card;
}

function updateTicketCard(card, ticket) {
  const badge = card.querySelector(".status-badge");
  badge.className = statusBadgeClass(ticket.action);
  badge.textContent = actionLabel(ticket.action);
  card.querySelector(".ticket-card-updated").textContent =
    `Aggiornato: ${formatTicketUpdate(ticket.ts)}`;
  card.setAttribute("aria-label", `Ticket #${ticket.work_item_id}: ${actionLabel(ticket.action)}`);
  card.dataset.action = ticket.action;
  card.dataset.ts = ticket.ts || "";
}

async function refreshTickets(silent = false) {
  const tickets = await fetchJson("/api/tickets?limit=100", undefined, { showLoader: !silent });
  const activeList = document.getElementById("tickets-list");
  const completedList = document.getElementById("completed-list");
  initializeTicketLanes(activeList, TICKET_FLOW.filter((stage) => stage.id !== "pr"));
  initializeTicketLanes(completedList, TICKET_FLOW.filter((stage) => stage.id === "pr"));

  const seenActive = new Set();
  const seenCompleted = new Set();
  let warningCount = 0;
  let errorCount = 0;

  for (const ticket of tickets) {
    const key = String(ticket.work_item_id);
    const completed = isCompletedAction(ticket.action);
    const targetList = completed ? completedList : activeList;
    const otherList = completed ? activeList : completedList;
    const targetLane = ensureTicketLane(targetList, ticketFlowStage(ticket.action));
    (completed ? seenCompleted : seenActive).add(key);
    if (!completed) {
      if (["error", "blocked", "quality_failed"].includes(ticket.action)) errorCount += 1;
      else if (["plan_ready", "implemented", "quality_passed", "pr_opened"].includes(ticket.action)) {
        warningCount += 1;
      }
    }

    // Un ticket puo' passare da attivo a completato (o viceversa, con
    // "Riapri") tra un poll e l'altro: spostalo invece di duplicarlo.
    const stale = otherList.querySelector(`[data-work-item-id="${key}"]`);
    if (stale) stale.remove();

    let card = targetList.querySelector(`[data-work-item-id="${key}"]`);
    if (card) {
      updateTicketCard(card, ticket);
    } else {
      card = renderTicketCard(ticket);
    }
    targetLane.querySelector(".ticket-lane-cards").appendChild(card);

    if (currentView.type === "detail" && currentView.workItemId === key) {
      updateDetailStage(ticket);
    }
  }

  for (const card of Array.from(activeList.querySelectorAll(".ticket-card"))) {
    if (!seenActive.has(card.dataset.workItemId)) card.remove();
  }
  for (const card of Array.from(completedList.querySelectorAll(".ticket-card"))) {
    if (!seenCompleted.has(card.dataset.workItemId)) card.remove();
  }
  updateTicketLaneCounts(activeList);
  updateTicketLaneCounts(completedList);
  updateTicketAttentionBadges(warningCount, errorCount);
  applyTicketTabFilter();
}

function updateTicketAttentionBadges(warningCount, errorCount) {
  const warningBadge = document.getElementById("ticket-warning-badge");
  const errorBadge = document.getElementById("ticket-error-badge");
  warningBadge.hidden = warningCount === 0;
  errorBadge.hidden = errorCount === 0;
  warningBadge.title = `${warningCount} ticket richiedono attenzione`;
  errorBadge.title = `${errorCount} ticket bloccati o in errore`;
}

function applyTicketTabFilter() {
  const attentionActions = ["plan_ready", "implemented", "quality_passed", "pr_opened"];
  const issueActions = ["error", "blocked", "quality_failed"];
  for (const card of document.querySelectorAll("#tickets-list .ticket-card")) {
    const action = card.dataset.action;
    const visible = currentTicketTab === "active"
      || (currentTicketTab === "attention" && attentionActions.includes(action))
      || (currentTicketTab === "issues" && issueActions.includes(action));
    card.hidden = !visible;
  }
  for (const lane of document.querySelectorAll("#tickets-list .ticket-lane")) {
    lane.hidden = currentTicketTab !== "active"
      && !Array.from(lane.querySelectorAll(".ticket-card")).some((card) => !card.hidden);
  }
}

function selectTicketTab(tab) {
  currentTicketTab = tab;
  for (const button of document.querySelectorAll(".ticket-tab")) {
    const active = button.dataset.ticketTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  applyTicketTabFilter();
}

// --- Storico (sidebar) --------------------------------------------------

function renderHistoryRow(event) {
  const tr = document.createElement("tr");
  tr.className = `level-${event.level}`;
  tr.dataset.eventId = String(event.id);

  const ticketCell = document.createElement("td");
  if (event.work_item_id) {
    const url = workItemUrl(event.work_item_id);
    if (url) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.className = "ticket-link";
      a.textContent = `#${event.work_item_id}`;
      ticketCell.appendChild(a);
    } else {
      ticketCell.textContent = `#${event.work_item_id}`;
    }
  }

  const messageCell = document.createElement("td");
  messageCell.textContent = event.message;
  if (event.detail) {
    const detail = document.createElement("div");
    detail.className = "detail";
    detail.textContent = event.detail;
    messageCell.appendChild(detail);
  }

  const prCell = document.createElement("td");
  if (event.pr_id) {
    const url = prUrl(event.pr_id);
    const a = document.createElement("a");
    a.href = url || "#";
    a.target = "_blank";
    a.className = "ticket-link";
    a.textContent = event.branch || `PR #${event.pr_id}`;
    prCell.appendChild(a);
  } else {
    prCell.textContent = event.branch || "";
  }

  const tsCell = document.createElement("td");
  tsCell.textContent = formatTime(event.ts);

  const actionCell = document.createElement("td");
  actionCell.textContent = actionLabel(event.action);

  tr.append(tsCell, ticketCell, prCell, actionCell, messageCell);
  return tr;
}

async function refreshHistory(silent = false) {
  const params = new URLSearchParams({ limit: "200" });
  if (ticketFilter) params.set("work_item_id", ticketFilter);

  const events = await fetchJson(`/api/history?${params}`, undefined, { showLoader: !silent });
  const body = document.getElementById("history-body");

  const seen = new Set();
  for (const event of events) {
    const id = String(event.id);
    seen.add(id);
    const existing = body.querySelector(`tr[data-event-id="${id}"]`);
    body.appendChild(existing || renderHistoryRow(event));
  }

  for (const tr of Array.from(body.children)) {
    if (!seen.has(tr.dataset.eventId)) tr.remove();
  }
}

// --- Navigazione (sidebar: Ticket / Storico) ------------------------------

function applyLayout() {
  for (const btn of document.querySelectorAll(".nav-btn")) {
    btn.classList.toggle("active", btn.dataset.view === currentNav);
  }
  const inDetail = currentView.type === "detail";
  document.getElementById("view-tickets").hidden = inDetail || currentNav !== "tickets";
  document.getElementById("view-completed").hidden = inDetail || currentNav !== "completed";
  document.getElementById("view-history").hidden = inDetail || currentNav !== "history";
  document.getElementById("view-workflow").hidden = inDetail || currentNav !== "workflow";
  document.getElementById("view-dashboard").hidden = inDetail || currentNav !== "dashboard";
  document.getElementById("view-notifications").hidden = inDetail || currentNav !== "notifications";
  document.getElementById("view-settings").hidden = inDetail || currentNav !== "settings";
  document.getElementById("ticket-detail-panel").hidden = !inDetail;
}

function showNav(view) {
  currentNav = view;
  if (currentView.type === "detail") currentView = { type: "list" };
  applyLayout();
  if (view === "settings") loadSettings();
  if (view === "dashboard") loadDashboard();
  if (view === "notifications") loadNotifications();
  if (view === "workflow") loadWorkflow();
}

// --- Workflow ---------------------------------------------------------------

function renderWorkflowChat(messages) {
  const container = document.getElementById("workflow-chat-messages");
  container.textContent = "";
  if (messages.length === 0) {
    container.textContent = "Nessuna modifica richiesta. Il workflow predefinito usa Copilot con Claude come fallback.";
    return;
  }
  for (const message of messages) {
    const item = document.createElement("article");
    item.className = `workflow-chat-message ${message.role}`;
    const author = document.createElement("strong");
    author.textContent = message.role === "user" ? "Tu" : "Workflow";
    const content = document.createElement("p");
    content.textContent = message.content;
    item.append(author, content);
    container.appendChild(item);
  }
  container.scrollTop = container.scrollHeight;
}

function renderWorkflow(data) {
  const settings = data.settings;
  document.getElementById("workflow-summary").textContent = data.summary;
  document.getElementById("workflow-routing").value = settings.routing_mode;
  document.getElementById("workflow-azure").value = settings.azure_communication;
  document.getElementById("workflow-copilot-step").hidden = settings.routing_mode === "claude_only";
  document.getElementById("workflow-claude-step").hidden = settings.routing_mode === "copilot_only";
  document.getElementById("workflow-pr-step").classList.toggle(
    "manual",
    settings.azure_communication === "manual_only"
  );
  renderWorkflowChat(data.messages || []);
}

async function loadWorkflow() {
  const hint = document.getElementById("workflow-settings-hint");
  try {
    renderWorkflow(await fetchJson("/api/workflow"));
    hint.textContent = "";
    hint.classList.remove("error");
  } catch (err) {
    hint.textContent = `Impossibile caricare il workflow: ${err.message}`;
    hint.classList.add("error");
  }
}

async function saveWorkflow() {
  const hint = document.getElementById("workflow-settings-hint");
  const button = document.getElementById("workflow-save-btn");
  button.disabled = true;
  try {
    const result = await fetchJson("/api/workflow", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        routing_mode: document.getElementById("workflow-routing").value,
        azure_communication: document.getElementById("workflow-azure").value,
      }),
    });
    hint.textContent = "Workflow salvato.";
    hint.classList.remove("error");
    await loadWorkflow();
  } catch (err) {
    hint.textContent = `Salvataggio non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    button.disabled = false;
  }
}

async function sendWorkflowChat() {
  const input = document.getElementById("workflow-chat-input");
  const hint = document.getElementById("workflow-chat-hint");
  const button = document.getElementById("workflow-chat-send-btn");
  const text = input.value.trim();
  if (!text) return;
  button.disabled = true;
  hint.textContent = "Aggiornamento workflow...";
  hint.classList.remove("error");
  try {
    renderWorkflow(await fetchJson("/api/workflow/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }));
    input.value = "";
    hint.textContent = "Workflow aggiornato.";
  } catch (err) {
    hint.textContent = `Aggiornamento non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    button.disabled = false;
  }
}

// --- Notifiche --------------------------------------------------------------

async function loadNotifications(silent = false) {
  const data = await fetchJson("/api/notifications", undefined, { showLoader: !silent });
  const badge = document.getElementById("notification-badge");
  badge.hidden = data.unread_count === 0;
  badge.textContent = data.unread_count;

  if (currentNav !== "notifications") return;
  const container = document.getElementById("notifications-list");
  container.innerHTML = "";
  if (data.items.length === 0) {
    container.textContent = "Nessuna notifica.";
    return;
  }
  for (const notification of data.items) {
    const item = document.createElement("article");
    item.className = `notification-item${notification.read_at ? "" : " unread"}`;
    const content = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = notification.message;
    const date = document.createElement("p");
    date.textContent = formatTime(notification.created_at);
    content.append(title, date);
    item.appendChild(content);
    if (!notification.read_at) {
      const button = document.createElement("button");
      button.textContent = "Segna come letto";
      button.addEventListener("click", async () => {
        await fetchJson(`/api/notifications/${notification.id}/read`, { method: "POST" });
        await loadNotifications();
      });
      item.appendChild(button);
    }
    if (notification.work_item_id) {
      item.addEventListener("click", (event) => {
        if (event.target.tagName !== "BUTTON") showDetailView(notification.work_item_id);
      });
    }
    container.appendChild(item);
  }
}

// --- Dashboard --------------------------------------------------------------

function formatCost(cost) {
  return `$${cost.toFixed(4)}`;
}

function renderDashboard(data) {
  document.getElementById("dashboard-completed-count").textContent = data.summary.completed_count;
  document.getElementById("dashboard-story-points").textContent =
    data.summary.story_points.toLocaleString("it-IT");
  document.getElementById("dashboard-tokens").textContent =
    data.summary.total_tokens.toLocaleString("it-IT");
  document.getElementById("dashboard-cost").textContent = formatCost(data.summary.cost_usd);

  const body = document.getElementById("dashboard-body");
  body.innerHTML = "";
  if (data.items.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "hint";
    cell.textContent = "Nessun PBI completato nel periodo selezionato.";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  for (const item of data.items) {
    const row = document.createElement("tr");
    row.append(
      Object.assign(document.createElement("td"), { textContent: `#${item.work_item_id}` }),
      Object.assign(document.createElement("td"), { textContent: item.title }),
      Object.assign(document.createElement("td"), {
        textContent: item.story_points == null ? "n/d" : item.story_points,
      }),
      Object.assign(document.createElement("td"), {
        textContent: item.total_tokens.toLocaleString("it-IT"),
      }),
      Object.assign(document.createElement("td"), { textContent: formatCost(item.cost_usd) }),
      Object.assign(document.createElement("td"), { textContent: formatTime(item.completed_at) }),
    );
    row.addEventListener("click", () => showDetailView(item.work_item_id));
    body.appendChild(row);
  }
}

function renderAttention(items) {
  const container = document.getElementById("attention-list");
  container.innerHTML = "";
  if (items.length === 0) {
    container.textContent = "Nessuna azione richiesta in questo momento.";
    return;
  }
  for (const item of items) {
    const button = document.createElement("button");
    button.className = "attention-item";
    button.textContent = `#${item.work_item_id} — ${item.attention_reason}`;
    button.addEventListener("click", () => showDetailView(item.work_item_id));
    container.appendChild(button);
  }
}

async function loadDashboard() {
  if (dashboardLoading) return;
  const hint = document.getElementById("dashboard-hint");
  const completedFrom = document.getElementById("dashboard-from").value;
  const completedTo = document.getElementById("dashboard-to").value;
  const completionAction = document.getElementById("dashboard-completion-action").value;
  const workItemType = document.getElementById("dashboard-work-item-type").value;
  const search = document.getElementById("dashboard-search").value.trim();
  const params = new URLSearchParams();
  if (completedFrom) params.set("completed_from", completedFrom);
  if (completedTo) params.set("completed_to", completedTo);
  if (completionAction !== "all") params.set("completion_action", completionAction);
  if (workItemType !== "all") params.set("work_item_type", workItemType);
  if (search) params.set("search", search);

  dashboardLoading = true;
  document.getElementById("dashboard-apply-btn").disabled = true;
  hint.textContent = "Caricamento metriche...";
  hint.classList.remove("error");
  try {
    const [data, attention] = await Promise.all([
      fetchJson(`/api/dashboard?${params}`),
      fetchJson("/api/attention"),
    ]);
    renderDashboard(data);
    renderAttention(attention);
    hint.textContent = "";
  } catch (err) {
    hint.textContent = `Impossibile caricare la dashboard: ${err.message}`;
    hint.classList.add("error");
  } finally {
    dashboardLoading = false;
    document.getElementById("dashboard-apply-btn").disabled = false;
  }
}

// --- Impostazioni ----------------------------------------------------------
// Pagina per configurare le variabili lette da config.py (URL organizzazione,
// progetto, repo, path locale, e il PAT Azure DevOps). Il PAT non torna mai
// in chiaro dal server: solo mascherato (o vuoto se non impostato). Un campo
// lasciato vuoto al salvataggio non modifica il valore esistente lato server.

async function loadSettings() {
  const hint = document.getElementById("settings-hint");
  hint.textContent = "";
  hint.classList.remove("error");
  let fields;
  try {
    fields = await fetchJson("/api/settings");
  } catch (err) {
    hint.textContent = err.message;
    hint.classList.add("error");
    return;
  }
  renderSettingsForm(fields);
  showOnboardingForMissingField(fields);
  try {
    renderTokenBudget(await fetchJson("/api/token-budget"));
  } catch (err) {
    // Permette di continuare a configurare Azure DevOps anche se la dashboard
    // Python ancora in esecuzione non include il nuovo endpoint di budget.
    document.getElementById("settings-token-budget").hidden = true;
    document.getElementById("settings-token-budget-hint").textContent =
      "Riavvia la dashboard per visualizzare il budget token.";
  }
  await checkAppUpdate();
}

let onboardingFields = [];
let onboardingSubmitting = false;

function showOnboardingForMissingField(fields) {
  onboardingFields = fields.filter((field) => field.required && !field.is_set);
  const dialog = document.getElementById("onboarding-dialog");
  if (onboardingFields.length === 0) {
    if (dialog.open) dialog.close();
    return;
  }

  const field = onboardingFields[0];
  document.getElementById("onboarding-message").textContent =
    `Primo avvio: passo ${fields.filter((item) => item.required).length - onboardingFields.length + 1} di ${fields.filter((item) => item.required).length}.`;
  const label = document.getElementById("onboarding-label");
  label.textContent = field.label;
  const input = document.getElementById("onboarding-input");
  input.type = field.secret ? "password" : "text";
  input.value = "";
  input.placeholder = field.secret ? "Inserisci un valore sicuro" : "";
  input.required = true;
  input.dataset.settingsKey = field.key;
  document.getElementById("onboarding-hint").textContent =
    "Questa impostazione viene salvata solo nel tuo profilo Windows.";
  if (!dialog.open) dialog.showModal();
  input.focus();
}

async function saveOnboardingField(event) {
  event.preventDefault();
  if (onboardingSubmitting) return;
  const input = document.getElementById("onboarding-input");
  const hint = document.getElementById("onboarding-hint");
  const value = input.value.trim();
  if (!value) {
    hint.textContent = "Inserisci un valore prima di continuare.";
    hint.classList.add("error");
    return;
  }

  onboardingSubmitting = true;
  document.getElementById("onboarding-submit").disabled = true;
  hint.classList.remove("error");
  hint.textContent = "Salvataggio...";
  try {
    const fields = await fetchJson("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: { [input.dataset.settingsKey]: value } }),
    });
    renderSettingsForm(fields);
    showOnboardingForMissingField(fields);
    if (onboardingFields.length === 0) {
      await loadConfig();
      await Promise.all([loadDashboard(), refreshAutomaticIngestStatus(), tick()]);
    }
  } catch (err) {
    hint.textContent = `Salvataggio non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    onboardingSubmitting = false;
    document.getElementById("onboarding-submit").disabled = false;
  }
}

let appReleaseUrl = "";

async function checkAppUpdate() {
  const hint = document.getElementById("app-version-hint");
  const checkButton = document.getElementById("check-app-update-btn");
  const releaseButton = document.getElementById("open-app-release-btn");
  checkButton.disabled = true;
  releaseButton.hidden = true;
  hint.classList.remove("error");
  hint.textContent = "Controllo aggiornamenti...";
  try {
    const update = await fetchJson("/api/app-update");
    appReleaseUrl = update.release_url;
    if (update.update_available) {
      hint.textContent = `Versione installata: ${update.current_version}. È disponibile ${update.latest_version}.`;
      releaseButton.hidden = !appReleaseUrl;
    } else if (!update.latest_version) {
      hint.textContent = `Versione installata: ${update.current_version}. Non è ancora stata pubblicata una Release GitHub.`;
    } else {
      hint.textContent = `Versione installata: ${update.current_version}. Hai già l'ultima versione (${update.latest_version}).`;
    }
  } catch (err) {
    appReleaseUrl = "";
    hint.textContent = `Impossibile controllare gli aggiornamenti: ${err.message}`;
    hint.classList.add("error");
  } finally {
    checkButton.disabled = false;
  }
}

function openAppRelease() {
  if (appReleaseUrl) window.open(appReleaseUrl, "_blank", "noopener");
}

function renderTokenBudget(budget) {
  const box = document.getElementById("settings-token-budget");
  const hint = document.getElementById("settings-token-budget-hint");
  box.hidden = false;
  document.getElementById("settings-token-budget-limit").textContent =
    budget.limit_tokens === null ? "Non impostato" : budget.limit_tokens.toLocaleString("it-IT");
  document.getElementById("settings-token-budget-used").textContent =
    budget.used_tokens.toLocaleString("it-IT");
  document.getElementById("settings-token-budget-remaining").textContent =
    budget.remaining_tokens === null ? "Illimitati" : budget.remaining_tokens.toLocaleString("it-IT");
  hint.textContent = budget.is_exhausted
    ? "Budget esaurito: i nuovi run vengono bloccati finché non aumenti il limite."
    : "Il consumo è aggiornato dai provider che restituiscono le metriche token (Claude SDK).";
  hint.classList.toggle("error", budget.is_exhausted);
}

function renderSettingsForm(fields) {
  const form = document.getElementById("settings-form");
  form.innerHTML = "";
  let externalAgentHelp = null;
  for (const field of fields) {
    const row = document.createElement("div");
    row.className = "settings-row";
    row.dataset.settingsKey = field.key;

    const label = document.createElement("label");
    label.htmlFor = `settings-${field.key}`;
    label.textContent = field.label + (field.required ? " *" : "");
    row.appendChild(label);

    const input = field.control === "select"
      ? document.createElement("select")
      : document.createElement("input");
    input.id = `settings-${field.key}`;
    input.name = field.key;
    input.autocomplete = "off";
    if (field.control === "select") {
      for (const optionData of field.options) {
        const option = document.createElement("option");
        option.value = optionData.value;
        option.textContent = optionData.label;
        option.selected = optionData.value === field.value;
        input.appendChild(option);
      }
    } else {
      input.type = field.secret ? "password" : "text";
    }
    if (field.secret) {
      input.placeholder = field.is_set ? `Impostato (${field.value}) — lascia vuoto per non modificare` : "Non impostato";
    } else if (field.control !== "select") {
      input.value = field.value;
    }
    row.appendChild(input);
    form.appendChild(row);

    if (field.key === "AGENT_COMMAND") {
      externalAgentHelp = document.createElement("div");
      externalAgentHelp.className = "external-agent-help";
      externalAgentHelp.innerHTML = [
        "<strong>Come funziona</strong>",
        "Il dashboard invia il prompt allo standard input del comando ed usa il suo standard output come risposta dell'agente.",
        "<code>claude -p</code> e' un esempio se Claude CLI e' installato e autenticato, ma usa comunque il tuo account e i token Claude.",
        "Per evitare il budget Claude, configura qui il comando di un CLI diverso, gia' installato e autenticato, che legge stdin e stampa la risposta su stdout.",
        "Il comando riceve anche le variabili <code>AGENT_MODEL</code>, <code>AGENT_ALLOWED_TOOLS</code> e <code>AGENT_MAX_OUTPUT_TOKENS</code>.",
      ].map((text) => `<p>${text}</p>`).join("");
      form.appendChild(externalAgentHelp);
    }
  }

  const provider = form.elements.AGENT_PROVIDER;
  const commandRow = form.querySelector('[data-settings-key="AGENT_COMMAND"]');
  const updateExternalAgentVisibility = () => {
    const usesExternalAgent = provider.value === "command";
    commandRow.hidden = !usesExternalAgent;
    externalAgentHelp.hidden = !usesExternalAgent && provider.value !== "copilot_cli";
    if (provider.value === "copilot_cli") {
      externalAgentHelp.innerHTML = [
        "<strong>GitHub Copilot CLI (sperimentale)</strong>",
        "Installa con <code>winget install GitHub.Copilot</code> e autentica con <code>copilot login</code>.",
        "Il CLI e' al momento interattivo: la dashboard non avvia piani o modifiche automatiche, per evitare run bloccati in attesa di approvazione.",
        "Puoi usarlo manualmente dal repository con <code>copilot</code>.",
      ].map((text) => `<p>${text}</p>`).join("");
    } else {
      externalAgentHelp.innerHTML = [
        "<strong>Come funziona</strong>",
        "Il dashboard invia il prompt allo standard input del comando ed usa il suo standard output come risposta dell'agente.",
        "<code>claude -p</code> e' un esempio se Claude CLI e' installato e autenticato, ma usa comunque il tuo account e i token Claude.",
        "Per evitare il budget Claude, configura qui il comando di un CLI diverso, gia' installato e autenticato, che legge stdin e stampa la risposta su stdout.",
        "Il comando riceve anche le variabili <code>AGENT_MODEL</code>, <code>AGENT_ALLOWED_TOOLS</code> e <code>AGENT_MAX_OUTPUT_TOKENS</code>.",
      ].map((text) => `<p>${text}</p>`).join("");
    }
  };
  provider.addEventListener("change", updateExternalAgentVisibility);
  updateExternalAgentVisibility();
}

async function saveSettings(e) {
  e.preventDefault();
  const form = document.getElementById("settings-form");
  const hint = document.getElementById("settings-hint");
  const saveBtn = document.getElementById("settings-save-btn");

  const values = {};
  for (const input of form.querySelectorAll("input, select")) {
    values[input.name] = input.value;
  }

  saveBtn.disabled = true;
  hint.classList.remove("error");
  hint.textContent = "Salvataggio...";
  try {
    const fields = await fetchJson("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    renderSettingsForm(fields);
    hint.textContent = "Impostazioni salvate.";
    await loadConfig();
  } catch (err) {
    hint.textContent = err.message;
    hint.classList.add("error");
  } finally {
    saveBtn.disabled = false;
  }
}

// --- Vista dettaglio -----------------------------------------------------
// Aprire il dettaglio non cambia currentNav: si resta "sulla stessa voce di
// menu" da cui si e' entrati (Ticket o Completed), cosi' "Torna alla lista"
// riporta al posto giusto senza dover tracciare uno stato separato.

function showListView() {
  currentView = { type: "list" };
  applyLayout();
}

function selectDetailTab(tab) {
  currentDetailTab = tab;
  for (const button of document.querySelectorAll(".detail-tab")) {
    const active = button.dataset.detailTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }

  const detailLeft = document.getElementById("detail-workitem");
  const detailRight = document.querySelector("#ticket-detail-panel .detail-right");
  detailLeft.classList.toggle("detail-tab-hidden", tab !== "details");
  detailRight.classList.toggle("detail-tab-hidden", tab === "details");

  const workflowIds = [
    "detail-status-line", "usage-box", "plan-box", "verify-box", "pr-review-box",
    "completed-box", "correction-box",
  ];
  for (const id of workflowIds) {
    document.getElementById(id).classList.toggle("detail-tab-hidden", tab !== "workflow");
  }
  document.getElementById("ticket-chat-box").classList.toggle("detail-tab-hidden", tab !== "chat");
  document.getElementById("detail-timeline-heading").classList.toggle("detail-tab-hidden", tab !== "timeline");
  document.getElementById("detail-history-list").classList.toggle("detail-tab-hidden", tab !== "timeline");
}

function updateDetailWorkflowBadge(ticket) {
  const warning = document.getElementById("detail-workflow-warning");
  const error = document.getElementById("detail-workflow-error");
  const warningActions = ["plan_ready", "implemented", "quality_passed", "pr_opened"];
  const errorActions = ["error", "blocked", "quality_failed"];
  warning.hidden = !warningActions.includes(ticket.action);
  error.hidden = !errorActions.includes(ticket.action);
}

async function showDetailView(workItemId) {
  const id = String(workItemId);
  if (currentView.type === "detail" && currentView.workItemId === id) return;
  currentView = { type: "detail", workItemId: id };
  applyLayout();
  selectDetailTab("details");
  document.getElementById("detail-heading").textContent = `Ticket #${id}`;

  document.getElementById("correction-input").value = "";
  document.getElementById("correction-hint").textContent = "";
  document.getElementById("fix-input").value = "";
  document.getElementById("fix-hint").textContent = "";
  document.getElementById("review-report").textContent = "";
  document.getElementById("plan-hint").textContent = "";
  document.getElementById("pr-review-hint").textContent = "";
  document.getElementById("check-pr-result").textContent = "";
  document.getElementById("pr-comments-list").innerHTML = "";
  document.getElementById("pr-comment-batch-box").hidden = true;
  document.getElementById("pr-comment-batch-plan").textContent = "";
  document.getElementById("pr-comment-batch-hint").textContent = "";
  selectedPrCommentIds.clear();
  prCommentPlanningNotes.clear();
  currentPrCommentBatch = null;
  prCommentsLoadedForWorkItem = null;
  document.getElementById("reopen-hint").textContent = "";
  document.getElementById("ticket-chat-input").value = "";
  document.getElementById("ticket-chat-hint").textContent = "";
  document.getElementById("ticket-chat-messages").innerHTML = "";
  updateFixUI();

  await Promise.all([
    loadWorkItemInfo(id),
    loadPlanIfAny(id),
    renderDetailTimeline(id, document.getElementById("detail-history-list")),
    refreshUsage(id),
    loadQuality(id),
    loadTicketChat(id),
  ]);
}

async function loadWorkItemInfo(workItemId) {
  const loading = document.getElementById("detail-workitem-loading");
  loading.hidden = false;
  try {
    const info = await fetchJson(`/api/work-item/${workItemId}`);
    document.getElementById("detail-title").textContent = info.title;
    document.getElementById("detail-story-points").textContent =
      info.story_points != null ? `Story points: ${info.story_points}` : "Story points: n/d";
    document.getElementById("detail-description").textContent = info.description || "(nessuna descrizione)";
    document.getElementById("detail-acceptance").textContent = info.acceptance_criteria
      ? `Acceptance criteria:\n${info.acceptance_criteria}` : "";

    const figmaEl = document.getElementById("detail-figma-links");
    figmaEl.innerHTML = "";
    for (const url of info.figma_urls || []) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.className = "ticket-link";
      a.textContent = `Design su Figma: ${url}`;
      figmaEl.appendChild(a);
      figmaEl.appendChild(document.createElement("br"));
    }
  } catch (err) {
    document.getElementById("detail-title").textContent = `Ticket #${workItemId}`;
    document.getElementById("detail-story-points").textContent = "";
    document.getElementById("detail-description").textContent = `Impossibile caricare i dettagli: ${err.message}`;
    document.getElementById("detail-acceptance").textContent = "";
    document.getElementById("detail-figma-links").textContent = "";
  } finally {
    loading.hidden = true;
  }
}

async function refreshUsage(workItemId, silent = false) {
  try {
    const usage = await fetchJson(`/api/usage/${workItemId}`, undefined, { showLoader: !silent });
    document.getElementById("usage-cost").textContent = `$${usage.cost_usd.toFixed(4)}`;
    document.getElementById("usage-tokens").textContent = usage.total_tokens.toLocaleString("it-IT");
  } catch (err) {
    // silenzioso: l'utilizzo non e' informazione critica, non deve rompere il resto della vista
  }
}

function renderQuality(quality) {
  const report = document.getElementById("quality-report");
  const createPrBtn = document.getElementById("create-pr-btn");
  const autoCompleteBtn = document.getElementById("create-pr-autocomplete-btn");
  const passed = quality && quality.status === "passed";
  createPrBtn.disabled = !passed;
  autoCompleteBtn.disabled = !passed;

  if (!quality) {
    report.textContent = "Esegui le verifiche tecniche obbligatorie prima di creare la PR.";
    return;
  }
  const labels = {
    passed: "Verifiche superate. Puoi creare la PR.",
    failed: "Almeno una verifica non e' riuscita: correggi il branch e riesegui.",
    unavailable: "Il repository non dichiara comandi di verifica rilevabili.",
  };
  report.textContent = `${labels[quality.status] || "Stato verifiche non disponibile."}\nCommit verificato: ${quality.commit_sha.slice(0, 12)}`;
  for (const check of quality.checks) {
    const line = document.createElement("div");
    line.textContent = `${check.status === "passed" ? "✓" : "✗"} ${check.name}: ${check.command} (${check.duration_seconds}s)`;
    report.appendChild(line);
  }
}

async function loadQuality(workItemId) {
  try {
    const quality = await fetchJson(`/api/quality/${workItemId}`);
    renderQuality(quality);
  } catch (err) {
    document.getElementById("quality-report").textContent = `Impossibile leggere le verifiche: ${err.message}`;
  }
}

async function runQualityChecks() {
  if (qualityRunning) return;
  const button = document.getElementById("quality-check-btn");
  const report = document.getElementById("quality-report");
  qualityRunning = true;
  button.disabled = true;
  report.textContent = "Verifiche locali in corso: non viene usata alcuna IA...";
  try {
    const quality = await fetchJson(`/api/quality/${currentView.workItemId}`, { method: "POST" });
    renderQuality(quality);
    showActionFeedback("Verifiche completate. Vista aggiornata.");
    await refreshAfterTicketMutation();
  } catch (err) {
    report.textContent = `Verifiche non eseguibili: ${err.message}`;
    showActionFeedback(`Verifiche non riuscite: ${err.message}`, true);
  } finally {
    qualityRunning = false;
    button.disabled = false;
  }
}

async function loadPlanIfAny(workItemId) {
  try {
    const plan = await fetchJson(`/api/plan/${workItemId}`);
    document.getElementById("plan-input").value = plan.text;
  } catch (err) {
    document.getElementById("plan-input").value = "";
  }
}

let ticketChatSubmitting = false;

async function loadTicketChat(workItemId) {
  const container = document.getElementById("ticket-chat-messages");
  try {
    const messages = await fetchJson(`/api/ticket-chat/${workItemId}`);
    renderTicketChat(messages);
  } catch (err) {
    container.textContent = `Impossibile caricare la chat: ${err.message}`;
  }
}

function renderTicketChat(messages) {
  const container = document.getElementById("ticket-chat-messages");
  container.innerHTML = "";
  if (messages.length === 0) {
    container.textContent = "Nessun messaggio: descrivi cosa vuoi fare dopo su questo ticket.";
    return;
  }
  for (const message of messages) {
    const item = document.createElement("div");
    item.className = "ticket-timeline-item";
    const head = document.createElement("div");
    const author = message.role === "user" ? "Tu" : "Agente";
    head.textContent = `${author} — ${formatTime(message.created_at)}`;
    const content = document.createElement("div");
    content.className = "detail";
    content.textContent = message.content;
    item.append(head, content);
    container.appendChild(item);
  }
}

async function sendTicketChatMessage() {
  const input = document.getElementById("ticket-chat-input");
  const hint = document.getElementById("ticket-chat-hint");
  const text = input.value.trim();
  if (!text || ticketChatSubmitting) return;
  ticketChatSubmitting = true;
  input.disabled = true;
  document.getElementById("ticket-chat-send-btn").disabled = true;
  hint.textContent = "Analisi e piano in corso...";
  hint.classList.remove("error");
  try {
    const result = await fetchJson(`/api/ticket-chat/${currentView.workItemId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    input.value = "";
    renderTicketChat(result.messages);
    hint.textContent = "Piano pronto.";
    showActionFeedback("Piano pronto. Vista aggiornata.");
    await refreshAfterTicketMutation();
  } catch (err) {
    hint.textContent = `Invio non riuscito: ${err.message}`;
    hint.classList.add("error");
    showActionFeedback(`Invio non riuscito: ${err.message}`, true);
  } finally {
    ticketChatSubmitting = false;
    input.disabled = false;
    document.getElementById("ticket-chat-send-btn").disabled = false;
  }

}

async function restartTicketFromScratch() {
  const workItemId = currentView.workItemId;
  const confirmed = await confirmAction(
    `Ripartire da zero con il ticket #${workItemId}? Verranno rimossi i tag del ciclo precedente e cancellato il branch locale associato.`,
    "Riparti da zero"
  );
  if (!confirmed) return;
  const button = document.getElementById("restart-ticket-btn");
  const hint = document.getElementById("ticket-chat-hint");
  button.disabled = true;
  hint.textContent = "Azzeramento e nuova pianificazione in avvio...";
  hint.classList.remove("error");
  try {
    await fetchJson(`/api/restart-from-scratch/${workItemId}`, { method: "POST" });
    hint.textContent = "Ticket azzerato: l'agente sta generando un nuovo piano.";
    await Promise.all([
      refreshStatus(true),
      refreshAutomaticIngestStatus(true),
      refreshTickets(true),
      refreshHistory(true),
    ]);
    await loadNotifications(true);
    showActionFeedback("Ticket azzerato: la nuova pianificazione è in corso.");
  } catch (err) {
    const message = `Impossibile ripartire da zero: ${err.message}`;
    hint.textContent = message;
    hint.classList.add("error");
    showActionFeedback(message, true);
    await showError(message);
  } finally {
    button.disabled = false;
  }
}

async function renderDetailTimeline(workItemId, containerEl, silent = false) {
  const events = await fetchJson(
    `/api/history?work_item_id=${workItemId}&limit=100`,
    undefined,
    { showLoader: !silent }
  );
  const chronological = [...events].reverse();

  if (chronological.length === 0) {
    containerEl.textContent = "Nessun evento registrato.";
    return;
  }
  if (containerEl.children.length === 0) {
    containerEl.textContent = "";
  }

  const seen = new Set();
  for (const ev of chronological) {
    const id = String(ev.id);
    seen.add(id);
    let item = containerEl.querySelector(`[data-event-id="${id}"]`);
    if (!item) {
      item = document.createElement("div");
      item.className = "ticket-timeline-item";
      item.dataset.eventId = id;
      const head = document.createElement("div");
      const ts = document.createElement("span");
      ts.className = "ts";
      const action = document.createElement("span");
      action.className = "action";
      const msg = document.createElement("span");
      msg.className = "msg";
      head.append(ts, action, msg);
      const detail = document.createElement("div");
      detail.className = "detail";
      item.append(head, detail);
    }
    item.querySelector(".ts").textContent = `${formatTime(ev.ts)} — `;
    item.querySelector(".action").textContent = actionLabel(ev.action);
    item.querySelector(".msg").textContent = `: ${ev.message}`;
    const detailEl = item.querySelector(".detail");
    detailEl.textContent = ev.detail || "";
    detailEl.hidden = !ev.detail;
    containerEl.appendChild(item);
  }
  for (const item of Array.from(containerEl.children)) {
    if (!seen.has(item.dataset.eventId)) item.remove();
  }
}

function updateDetailStage(ticket) {
  const statusLine = document.getElementById("detail-status-line");
  statusLine.textContent = `${actionLabel(ticket.action)} — ${ticket.message}`;
  statusLine.className = `status-line level-${ticket.level}`;

  const stage = stageForAction(ticket.action);
  document.getElementById("plan-box").hidden = stage !== "plan";
  document.getElementById("verify-box").hidden = stage !== "verify";
  document.getElementById("correction-box").hidden = stage !== "live";
  document.getElementById("pr-review-box").hidden = stage !== "pr-review";
  document.getElementById("completed-box").hidden = stage !== "completed";
  document.getElementById("btn-close-ticket").hidden = stage === "completed";
  updateDetailWorkflowBadge(ticket);
  if (stage === "pr-review" && currentView.type === "detail") {
    void loadPrComments();
  }

  if (stage === "verify") {
    document.getElementById("verify-summary").textContent =
      ticket.detail || "Nessun riassunto tecnico disponibile per questo ticket.";
  }
  if (stage === "live") {
    updateCorrectionUI();
  }
  if (stage === "completed") {
    document.getElementById("completed-reason").textContent = ticket.message;
  }
}

// --- Correzione live (durante implementing/fixing/classifying) -----------

function isRunActiveForTicket(workItemId) {
  if (!lastStatus) return false;
  return ["ingest", "review"].some((script) => {
    const s = lastStatus[script];
    return s.active && s.latest_event && String(s.latest_event.work_item_id) === String(workItemId);
  });
}

function updateCorrectionUI() {
  if (currentView.type !== "detail") return;
  const active = isRunActiveForTicket(currentView.workItemId);
  const input = document.getElementById("correction-input");
  const btn = document.getElementById("correction-send-btn");
  const hint = document.getElementById("correction-hint");
  input.disabled = !active || correctionSubmitting;
  btn.disabled = !active || correctionSubmitting || !input.value.trim();
  if (!correctionSubmitting) {
    hint.textContent = active ? "" : "Nessun run attivo su questo ticket in questo momento.";
    hint.classList.remove("error");
  }
}

async function sendCorrection() {
  const input = document.getElementById("correction-input");
  const hint = document.getElementById("correction-hint");
  const text = input.value.trim();
  if (!text || correctionSubmitting) return;
  correctionSubmitting = true;
  updateCorrectionUI();
  try {
    await fetchJson(`/api/correction/${currentView.workItemId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    input.value = "";
    hint.textContent = "Correzione inviata.";
    hint.classList.remove("error");
  } catch (err) {
    // il run potrebbe essere terminato nel frattempo: non svuotare il testo scritto
    hint.textContent = `Invio non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    correctionSubmitting = false;
    await refreshStatus();
    updateCorrectionUI();
  }
}

// --- Piano (gate di approvazione) -----------------------------------------

async function savePlan() {
  const input = document.getElementById("plan-input");
  const hint = document.getElementById("plan-hint");
  try {
    await fetchJson(`/api/plan/${currentView.workItemId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: input.value }),
    });
    hint.textContent = "Modifiche salvate.";
    hint.classList.remove("error");
  } catch (err) {
    hint.textContent = `Salvataggio non riuscito: ${err.message}`;
    hint.classList.add("error");
  }
}

async function approvePlan() {
  if (!await confirmAction("Approvare questo piano e avviare l'implementazione?", "Approva e avvia")) return;
  const hint = document.getElementById("plan-hint");
  try {
    await savePlan();
    await fetchJson(`/api/plan/${currentView.workItemId}/approve`, { method: "POST" });
    hint.textContent = "Piano approvato, implementazione in corso.";
    hint.classList.remove("error");
    showActionFeedback("Piano approvato: implementazione avviata.");
    await refreshAfterTicketMutation();
  } catch (err) {
    hint.textContent = `Approvazione non riuscita: ${err.message}`;
    hint.classList.add("error");
    showActionFeedback(`Approvazione non riuscita: ${err.message}`, true);
  }
}

// --- Verifica: self-review, fix post-implementazione, Crea PR ------------

function updateFixUI() {
  const btn = document.getElementById("fix-send-btn");
  const input = document.getElementById("fix-input");
  btn.disabled = fixSubmitting || !input.value.trim();
  input.disabled = fixSubmitting;
}

async function sendFix() {
  const input = document.getElementById("fix-input");
  const hint = document.getElementById("fix-hint");
  const text = input.value.trim();
  if (!text || fixSubmitting) return;
  fixSubmitting = true;
  updateFixUI();
  try {
    await fetchJson(`/api/fix/${currentView.workItemId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    input.value = "";
    hint.textContent = "Richiesta di correzione inviata.";
    hint.classList.remove("error");
  } catch (err) {
    hint.textContent = `Invio non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    fixSubmitting = false;
    updateFixUI();
  }
}

async function startReviewCode() {
  const btn = document.getElementById("review-code-btn");
  const reportEl = document.getElementById("review-report");
  if (reviewRunning) return;
  reviewRunning = true;
  btn.disabled = true;
  reportEl.textContent = "Review in corso, puo' richiedere qualche minuto...";
  try {
    const result = await fetchJson(`/api/review-code/${currentView.workItemId}`, { method: "POST" });
    reportEl.textContent = result.review || "(nessun contenuto restituito)";
  } catch (err) {
    reportEl.textContent = `Review non riuscita: ${err.message}`;
  } finally {
    reviewRunning = false;
    btn.disabled = false;
  }
}

async function closeTicket() {
  const workItemId = currentView.workItemId;
  if (!await confirmAction(
    `Chiudere il ticket #${workItemId}? Verra' spostato in "Completed" e ingest/review non lo toccheranno piu' (viene anche bloccato).`,
    "Chiudi ticket"
  )) {
    return;
  }
  try {
    await fetchJson(`/api/close/${workItemId}`, { method: "POST" });
    showActionFeedback(`Ticket #${workItemId} chiuso e spostato in Completed.`);
    await refreshAfterTicketMutation();
  } catch (err) {
    showActionFeedback(`Chiusura non riuscita: ${err.message}`, true);
    await showError(`Impossibile chiudere il ticket #${workItemId}: ${err.message}`);
  }
}

async function deleteTicket() {
  const workItemId = currentView.workItemId;
  const confirmed = await confirmAction(
    `Eliminare il ticket #${workItemId}? Verrà spostato nel cestino di Azure Boards e non sarà più lavorato dall'agente.`,
    "Elimina ticket"
  );
  if (!confirmed) return;

  const button = document.getElementById("btn-delete-ticket");
  button.disabled = true;
  try {
    await fetchJson(
      `/api/tickets/${workItemId}`,
      { method: "DELETE" },
      { loadingLabel: "Eliminazione ticket..." }
    );
    ticketInfoCache.delete(String(workItemId));
    showActionFeedback(`Ticket #${workItemId} spostato nel cestino Azure Boards.`);
    await refreshAfterTicketMutation();
    showListView();
  } catch (err) {
    const message = `Impossibile eliminare il ticket #${workItemId}: ${err.message}`;
    showActionFeedback(message, true);
    await showError(message);
  } finally {
    button.disabled = false;
  }
}

let reopenSubmitting = false;

async function reopenTicket() {
  const hint = document.getElementById("reopen-hint");
  if (reopenSubmitting) return;
  reopenSubmitting = true;
  try {
    await fetchJson(`/api/reopen/${currentView.workItemId}`, { method: "POST" });
    hint.textContent = "Ticket riaperto.";
    hint.classList.remove("error");
    showActionFeedback("Ticket riaperto. Vista aggiornata.");
    await refreshAfterTicketMutation();
  } catch (err) {
    hint.textContent = `Impossibile riaprire: ${err.message}`;
    hint.classList.add("error");
    showActionFeedback(`Riapertura non riuscita: ${err.message}`, true);
  } finally {
    reopenSubmitting = false;
  }
}

// --- Controlla PR (synthetic review, posta commenti reali) --------------

let checkPrRunning = false;

async function startCheckPr() {
  const btn = document.getElementById("check-pr-btn");
  const resultEl = document.getElementById("check-pr-result");
  if (checkPrRunning) return;
  checkPrRunning = true;
  btn.disabled = true;
  resultEl.textContent = "Controllo in corso (le tre persona junior/senior/tech-lead), puo' richiedere qualche minuto...";
  try {
    const result = await fetchJson(`/api/check-pr/${currentView.workItemId}`, { method: "POST" });
    let text = result.summary ? `${result.summary}\n\n` : "";
    text += `${result.posted} commenti pubblicati sulla PR`;
    if (result.failed) text += `, ${result.failed} non pubblicabili (manca file/riga)`;
    text += ".";
    resultEl.textContent = text;
  } catch (err) {
    resultEl.textContent = `Controllo non riuscito: ${err.message}`;
  } finally {
    checkPrRunning = false;
    btn.disabled = false;
  }
}

// --- Leggi commenti (triage manuale: risolvi o ignora, decidi tu) -------

let prCommentsLoading = false;
let prCommentsLoadedForWorkItem = null;
let prBatchSubmitting = false;
const selectedPrCommentIds = new Set();
const prCommentPlanningNotes = new Map();
let currentPrCommentBatch = null;

async function loadPrComments(force = false) {
  const hint = document.getElementById("pr-review-hint");
  if (prCommentsLoading || (!force && prCommentsLoadedForWorkItem === currentView.workItemId)) return;
  prCommentsLoading = true;
  hint.textContent = "Caricamento commenti...";
  hint.classList.remove("error");
  try {
    const comments = await fetchJson(`/api/pr-comments/${currentView.workItemId}`);
    await loadPrCommentBatch();
    renderPrComments(comments);
    prCommentsLoadedForWorkItem = currentView.workItemId;
    hint.textContent = comments.length ? `${comments.length} commenti da valutare.` : "";
  } catch (err) {
    hint.textContent = `Impossibile leggere i commenti: ${err.message}`;
    hint.classList.add("error");
  } finally {
    prCommentsLoading = false;
  }
}

async function loadPrCommentBatch() {
  const box = document.getElementById("pr-comment-batch-box");
  try {
    const batch = await fetchJson(`/api/pr-comment-batch/${currentView.workItemId}`);
    renderPrCommentBatch(batch);
  } catch (err) {
    if (!err.message.includes("Nessun piano")) throw err;
    currentPrCommentBatch = null;
    box.hidden = selectedPrCommentIds.size === 0;
    updatePrCommentBatchActions();
  }
}

function renderPrComments(comments) {
  const container = document.getElementById("pr-comments-list");
  container.innerHTML = "";
  if (comments.length === 0) {
    container.textContent = "Nessun commento da valutare.";
    document.getElementById("pr-comment-batch-box").hidden = true;
    updatePrCommentBatchActions();
    return;
  }
  for (const c of comments) {
    const item = document.createElement("div");
    item.className = "pr-comment-item";
    if (c.dismissed || c.resolved) {
      item.classList.add(c.dismissed ? "dismissed" : "resolved");
      selectedPrCommentIds.delete(c.thread_id);
    }

    const head = document.createElement("div");
    head.className = "pr-comment-head";
    const author = c.published_date ? `${c.author} — ${formatTime(c.published_date)}` : c.author;
    head.textContent = author;
    if (c.dismissed || c.resolved) {
      const ignored = document.createElement("span");
      ignored.className = c.dismissed ? "pr-comment-ignored" : "pr-comment-resolved";
      ignored.textContent = c.dismissed ? "Ignorato dal piano" : "Risolto su Azure DevOps";
      head.append(ignored);
    }
    item.appendChild(head);

    if (c.file_path) {
      item.append(createCommentFileReference(c.file_path, c.line));
    }

    const body = document.createElement("div");
    body.className = "pr-comment-body";
    renderPrCommentBody(body, c.content);
    item.appendChild(body);

    const actions = document.createElement("div");
    actions.className = "detail-actions";
    const selectLabel = document.createElement("label");
    selectLabel.className = "pr-comment-select";
    const select = document.createElement("input");
    select.type = "checkbox";
    select.checked = !c.dismissed && !c.resolved && selectedPrCommentIds.has(c.thread_id);
    select.disabled = c.dismissed || c.resolved;
    select.addEventListener("change", () => {
      if (select.checked) selectedPrCommentIds.add(c.thread_id);
      else selectedPrCommentIds.delete(c.thread_id);
      noteBox.hidden = !select.checked;
      document.getElementById("pr-comment-batch-box").hidden = selectedPrCommentIds.size === 0;
      updatePrCommentBatchActions();
    });
    const selectText = document.createElement("span");
    selectText.textContent = "Includi nel piano";
    selectLabel.append(select, selectText);
    const noteBox = document.createElement("label");
    noteBox.className = "pr-comment-plan-note";
    noteBox.hidden = !select.checked;
    noteBox.textContent = "Nota per il piano (facoltativa)";
    const noteInput = document.createElement("textarea");
    noteInput.rows = 2;
    noteInput.placeholder = "Indica vincoli, priorita' o aspetti da considerare.";
    noteInput.value = prCommentPlanningNotes.get(c.thread_id) || "";
    noteInput.addEventListener("input", () => {
      prCommentPlanningNotes.set(c.thread_id, noteInput.value);
    });
    noteBox.append(noteInput);
    if (c.dismissed) {
      const restoreBtn = document.createElement("button");
      restoreBtn.type = "button";
      restoreBtn.textContent = "Reincludi nel piano";
      restoreBtn.addEventListener("click", () => restoreComment(c.thread_id, restoreBtn));
      actions.append(selectLabel, restoreBtn);

      const reply = document.createElement("div");
      reply.className = "pr-comment-reply";
      const replyLabel = document.createElement("label");
      replyLabel.textContent = "Risposta per Azure DevOps";
      const replyInput = document.createElement("textarea");
      replyInput.rows = 3;
      replyInput.placeholder = "Scrivi una risposta: verra' pubblicata sul thread e il commento verra' risolto.";
      const replyBtn = document.createElement("button");
      replyBtn.type = "button";
      replyBtn.textContent = "Rispondi e risolvi";
      replyBtn.disabled = true;
      replyInput.addEventListener("input", () => {
        replyBtn.disabled = !replyInput.value.trim();
      });
      replyBtn.addEventListener("click", () => replyAndResolveComment(c.thread_id, replyInput, replyBtn));
      replyLabel.append(replyInput);
      reply.append(replyLabel, replyBtn);
      item.append(reply);
    } else if (!c.resolved) {
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.textContent = "Ignora";
      dismissBtn.addEventListener("click", () => dismissComment(c.thread_id, dismissBtn));
      actions.append(selectLabel, dismissBtn);
    }
    item.appendChild(actions);
    if (!c.dismissed && !c.resolved) item.appendChild(noteBox);

    const status = document.createElement("p");
    status.className = "hint";
    item.appendChild(status);

    container.appendChild(item);
  }
  document.getElementById("pr-comment-batch-box").hidden = false;
  updatePrCommentBatchActions();
}

function renderPrCommentBody(container, content) {
  const codePattern = /```[^\n]*\n([\s\S]*?)```/g;
  let cursor = 0;
  let match;
  while ((match = codePattern.exec(content)) !== null) {
    appendCommentText(container, content.slice(cursor, match.index));
    appendCommentFileReferences(container, match[1]);
    const code = document.createElement("pre");
    const codeContent = document.createElement("code");
    codeContent.textContent = match[1].trim();
    code.append(codeContent);
    container.append(code);
    cursor = codePattern.lastIndex;
  }
  appendCommentText(container, content.slice(cursor));
}

function appendCommentFileReferences(container, text) {
  const paths = [...new Set(
    Array.from(text.matchAll(/(?:^|[\s"'(])((?:[\w.-]+\/)+[\w.-]+\.[a-zA-Z0-9]+)(?=$|[\s"'`),:])/g))
      .map((match) => match[1])
  )];
  for (const path of paths) {
    container.append(createCommentFileReference(path));
  }
}

function createCommentFileReference(path, line) {
  const reference = document.createElement("div");
  reference.className = "pr-comment-file-reference";
  const extension = path.includes(".") ? path.split(".").pop().toUpperCase() : "FILE";
  const fileName = path.split("/").pop();
  const icon = document.createElement("span");
  icon.className = "pr-comment-file-icon";
  icon.textContent = extension;
  const details = document.createElement("div");
  details.className = "pr-comment-file-details";
  const name = document.createElement("strong");
  name.textContent = fileName;
  const value = document.createElement("code");
  value.textContent = path;
  details.append(name, value);
  reference.append(icon, details);
  if (line != null) {
    const lineNumber = document.createElement("span");
    lineNumber.className = "pr-comment-file-line";
    lineNumber.textContent = `Riga ${line}`;
    reference.append(lineNumber);
  }
  return reference;
}

function appendCommentText(container, text) {
  for (const paragraph of text.trim().split(/\n\s*\n/)) {
    if (!paragraph) continue;
    const line = document.createElement("p");
    const adviceMatch = paragraph.match(/^\*\*\[([^·\]]+)\s*·\s*([^\]]+)\]\*\*$/);
    if (adviceMatch) {
      const source = adviceMatch[1].trim();
      const severity = adviceMatch[2].trim();
      line.className = "pr-comment-advice";
      const sourceTag = document.createElement("span");
      sourceTag.className = "pr-comment-advice-source";
      sourceTag.textContent = source;
      const severityTag = document.createElement("span");
      severityTag.className = `pr-comment-advice-severity severity-${severity.toLowerCase().replace(/\s+/g, "-")}`;
      severityTag.textContent = severity;
      line.append(sourceTag, severityTag);
    } else {
      appendInlineCommentText(line, paragraph.replace(/\*\*/g, ""));
    }
    container.append(line);
  }
}

function appendInlineCommentText(container, text) {
  const inlineCodePattern = /`([^`]+)`/g;
  const normalizedText = text.replace(/\s+/g, " ");
  let cursor = 0;
  let match;
  while ((match = inlineCodePattern.exec(normalizedText)) !== null) {
    container.append(document.createTextNode(normalizedText.slice(cursor, match.index)));
    const code = document.createElement("code");
    code.className = "pr-comment-inline-code";
    code.textContent = match[1];
    container.append(code);
    cursor = inlineCodePattern.lastIndex;
  }
  container.append(document.createTextNode(normalizedText.slice(cursor)));
}

async function dismissComment(threadId, button) {
  if (!await confirmAction("Ignorare solo questo commento? Gli altri commenti della PR resteranno disponibili.", "Ignora commento")) {
    return;
  }
  button.disabled = true;
  try {
    const result = await fetchJson(
      `/api/pr-comments/${currentView.workItemId}/${threadId}/dismiss`,
      { method: "POST" }
    );
    selectedPrCommentIds.delete(threadId);
    prCommentPlanningNotes.delete(threadId);
    renderPrComments(result.comments);
    const hint = document.getElementById("pr-review-hint");
    const availableCount = result.comments.filter((comment) => !comment.dismissed).length;
    hint.textContent = availableCount
      ? `${availableCount} commenti da valutare.`
      : "Nessun commento da valutare.";
  } catch (err) {
    button.disabled = false;
    await showError(`Impossibile ignorare il commento: ${err.message}`);
  }
}

async function restoreComment(threadId, button) {
  button.disabled = true;
  try {
    await fetchJson(`/api/pr-comments/${currentView.workItemId}/${threadId}/restore`, { method: "POST" });
    await loadPrComments(true);
  } catch (err) {
    button.disabled = false;
    await showError(`Impossibile reincludere il commento: ${err.message}`);
  }
}

async function replyAndResolveComment(threadId, input, button) {
  if (!await confirmAction(
    "Pubblicare questa risposta su Azure DevOps e risolvere il commento selezionato?",
    "Rispondi e risolvi"
  )) return;
  button.disabled = true;
  input.disabled = true;
  try {
    await fetchJson(`/api/pr-comments/${currentView.workItemId}/${threadId}/reply-and-resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: input.value }),
    });
    await loadPrComments(true);
  } catch (err) {
    input.disabled = false;
    button.disabled = !input.value.trim();
    await showError(`Impossibile rispondere e risolvere il commento: ${err.message}`);
  }
}

function updatePrCommentBatchActions(batch = currentPrCommentBatch) {
  const planBtn = document.getElementById("pr-comments-plan-btn");
  const applyBtn = document.getElementById("pr-comments-apply-btn");
  const commitBtn = document.getElementById("pr-comments-commit-btn");
  planBtn.hidden = Boolean(batch && batch.status === "changes_applied");
  planBtn.textContent = batch && batch.status === "plan_ready" ? "Rigenera piano" : "Crea piano selezionati";
  planBtn.disabled = prBatchSubmitting || selectedPrCommentIds.size === 0;
  applyBtn.hidden = !batch || batch.status !== "plan_ready";
  applyBtn.disabled = prBatchSubmitting;
  commitBtn.hidden = !batch || batch.status !== "changes_applied";
  commitBtn.disabled = prBatchSubmitting;
}

function renderPrCommentBatch(batch) {
  const box = document.getElementById("pr-comment-batch-box");
  const plan = document.getElementById("pr-comment-batch-plan");
  const hint = document.getElementById("pr-comment-batch-hint");
  box.hidden = false;
  currentPrCommentBatch = batch;
  if (batch.status === "plan_ready") {
    selectedPrCommentIds.clear();
    for (const threadId of batch.thread_ids) selectedPrCommentIds.add(threadId);
  }
  plan.textContent = batch.plan_text;
  if (batch.status === "plan_ready") {
    hint.textContent = "Piano pronto: approva per applicare le modifiche, senza commit.";
  } else if (batch.status === "changes_applied") {
    hint.textContent = "Modifiche applicate senza commit: controllale e approva commit e push quando sei pronto.";
  } else if (batch.status === "completed") {
    hint.textContent = "Commit e push completati; i thread selezionati sono stati risolti.";
  }
  updatePrCommentBatchActions(batch);
}

async function createPrCommentBatchPlan() {
  if (prBatchSubmitting || selectedPrCommentIds.size === 0) return;
  const hint = document.getElementById("pr-comment-batch-hint");
  prBatchSubmitting = true;
  updatePrCommentBatchActions();
  hint.textContent = "Creo il piano per i commenti selezionati...";
  try {
    const batch = await fetchJson(`/api/pr-comment-batch/${currentView.workItemId}/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_ids: [...selectedPrCommentIds],
        planning_notes: Object.fromEntries(
          [...selectedPrCommentIds]
            .map((threadId) => [threadId, prCommentPlanningNotes.get(threadId)?.trim() || ""])
            .filter(([, note]) => note)
        ),
      }),
    });
    renderPrCommentBatch(batch);
  } catch (err) {
    hint.textContent = `Piano non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    prBatchSubmitting = false;
    updatePrCommentBatchActions();
  }
}

async function applyPrCommentBatch() {
  if (prBatchSubmitting || !await confirmAction(
    "Applicare le modifiche del piano senza fare commit o push?",
    "Applica modifiche"
  )) return;
  const hint = document.getElementById("pr-comment-batch-hint");
  prBatchSubmitting = true;
  updatePrCommentBatchActions();
  hint.textContent = "Applico le modifiche approvate, senza commit...";
  try {
    const result = await fetchJson(`/api/pr-comment-batch/${currentView.workItemId}/apply`, { method: "POST" });
    renderPrCommentBatch(result);
  } catch (err) {
    hint.textContent = `Applicazione non riuscita: ${err.message}`;
    hint.classList.add("error");
  } finally {
    prBatchSubmitting = false;
    updatePrCommentBatchActions();
  }
}

async function commitPrCommentBatch() {
  if (prBatchSubmitting || !await confirmAction(
    "Confermi commit e push delle modifiche già applicate?",
    "Commit e push"
  )) return;
  const hint = document.getElementById("pr-comment-batch-hint");
  prBatchSubmitting = true;
  updatePrCommentBatchActions();
  hint.textContent = "Eseguo commit e push approvati...";
  try {
    const result = await fetchJson(`/api/pr-comment-batch/${currentView.workItemId}/commit`, { method: "POST" });
    renderPrCommentBatch(result);
    if (result.committed)     await loadPrComments(true);
  } catch (err) {
    hint.textContent = `Commit o push non riuscito: ${err.message}`;
    hint.classList.add("error");
  } finally {
    prBatchSubmitting = false;
    updatePrCommentBatchActions();
  }
}

async function createPrFromDetail(autoComplete = false) {
  const workItemId = currentView.workItemId;
  try {
    const endpoint = autoComplete
      ? `/api/create-pr/${workItemId}/autocomplete`
      : `/api/create-pr/${workItemId}`;
    const result = await fetchJson(endpoint, { method: "POST" });
    window.open(result.url, "_blank");
  } catch (err) {
    await showError(`Impossibile creare la PR per #${workItemId}: ${err.message}`);
  }
  await Promise.all([refreshTickets(), refreshHistory()]);
}

async function triggerRun(script) {
  try {
    await fetchJson(`/api/run/${script}`, { method: "POST" });
    showActionFeedback(`Run ${script} avviato. Stato aggiornato.`);
  } catch (err) {
    showActionFeedback(`Impossibile avviare ${script}: ${err.message}`, true);
    await showError(`Impossibile avviare ${script}: ${err.message}`);
  }
  await refreshAfterTicketMutation();
}

async function tick() {
  try {
    await Promise.all([
      refreshStatus(true),
      refreshAutomaticIngestStatus(true),
      refreshTickets(true),
      refreshHistory(true),
    ]);
    if (currentView.type === "detail") {
      await Promise.all([
        renderDetailTimeline(currentView.workItemId, document.getElementById("detail-history-list"), true),
        refreshUsage(currentView.workItemId, true),
      ]);
    }
  } catch (err) {
    console.error("Errore aggiornamento dashboard:", err);
  }
}

document.getElementById("btn-ingest").addEventListener("click", () => triggerRun("ingest"));

for (const button of document.querySelectorAll(".ticket-tab")) {
  button.addEventListener("click", () => selectTicketTab(button.dataset.ticketTab));
}

for (const btn of document.querySelectorAll(".stop-btn")) {
  btn.addEventListener("click", () => stopRun(btn.dataset.script));
}
for (const btn of document.querySelectorAll(".block-btn")) {
  btn.addEventListener("click", () => blockWorkItem(btn.dataset.workItemId));
}

let filterTimeout = null;
document.getElementById("filter-ticket").addEventListener("input", (e) => {
  ticketFilter = e.target.value ? e.target.value.trim() : null;
  clearTimeout(filterTimeout);
  filterTimeout = setTimeout(refreshHistory, 300);
});

for (const btn of document.querySelectorAll(".nav-btn")) {
  btn.addEventListener("click", () => showNav(btn.dataset.view));
}
for (const button of document.querySelectorAll(".detail-tab")) {
  button.addEventListener("click", () => selectDetailTab(button.dataset.detailTab));
}
document.getElementById("btn-back-to-list").addEventListener("click", showListView);
document.getElementById("btn-close-ticket").addEventListener("click", closeTicket);
document.getElementById("btn-delete-ticket").addEventListener("click", deleteTicket);
document.getElementById("reopen-btn").addEventListener("click", reopenTicket);
document.getElementById("plan-save-btn").addEventListener("click", savePlan);
document.getElementById("plan-approve-btn").addEventListener("click", approvePlan);
document.getElementById("correction-send-btn").addEventListener("click", sendCorrection);
document.getElementById("correction-input").addEventListener("input", updateCorrectionUI);
document.getElementById("fix-send-btn").addEventListener("click", sendFix);
document.getElementById("fix-input").addEventListener("input", updateFixUI);
document.getElementById("review-code-btn").addEventListener("click", startReviewCode);
document.getElementById("quality-check-btn").addEventListener("click", runQualityChecks);
document.getElementById("check-pr-btn").addEventListener("click", startCheckPr);
document.getElementById("read-comments-btn").addEventListener("click", () => loadPrComments(true));
document.getElementById("pr-comments-plan-btn").addEventListener("click", createPrCommentBatchPlan);
document.getElementById("pr-comments-apply-btn").addEventListener("click", applyPrCommentBatch);
document.getElementById("pr-comments-commit-btn").addEventListener("click", commitPrCommentBatch);
document.getElementById("create-pr-btn").addEventListener("click", createPrFromDetail);
document.getElementById("create-pr-autocomplete-btn").addEventListener("click", () => createPrFromDetail(true));
document.getElementById("ticket-chat-send-btn").addEventListener("click", sendTicketChatMessage);
document.getElementById("restart-ticket-btn").addEventListener("click", restartTicketFromScratch);
document.getElementById("settings-form").addEventListener("submit", saveSettings);
document.getElementById("check-app-update-btn").addEventListener("click", checkAppUpdate);
document.getElementById("open-app-release-btn").addEventListener("click", openAppRelease);
document.getElementById("onboarding-form").addEventListener("submit", saveOnboardingField);
document.getElementById("onboarding-dialog").addEventListener("cancel", (event) => {
  if (onboardingFields.length > 0) event.preventDefault();
});
document.getElementById("dashboard-apply-btn").addEventListener("click", loadDashboard);
document.getElementById("notifications-refresh-btn").addEventListener("click", loadNotifications);
document.getElementById("workflow-save-btn").addEventListener("click", saveWorkflow);
document.getElementById("workflow-chat-send-btn").addEventListener("click", sendWorkflowChat);

(async function init() {
  applyLayout();
  let configAvailable = true;
  try {
    await loadConfig();
  } catch (err) {
    // Configurazione (ancora) incompleta, es. PAT non impostato: manda
    // l'utente dritto in Impostazioni invece di lasciare la dashboard rotta.
    console.warn("Configurazione non disponibile, apro Impostazioni:", err.message);
    showNav("settings");
    configAvailable = false;
  }
  if (configAvailable) await loadDashboard();
  if (configAvailable) await refreshAutomaticIngestStatus();
  await tick();
  setInterval(tick, POLL_MS);
})();
