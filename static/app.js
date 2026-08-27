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
  branch_created: "Branch created",
  generating_plan: "Generating plan",
  plan_ready: "Plan generated",
  plan_approved: "Plan approved",
  implementing: "Implementation in progress",
  implemented: "Ready for verification",
  pr_opened: "PR opened",
  blocked: "Blocked",
  stopped: "Stopped manually",
  decomposing: "Decomposing Epic",
  epic_decomposed: "Epic decomposed",
  classifying: "Evaluating comment",
  mechanical_fix: "Mechanical fix",
  needs_human: "Needs human review",
  autofix_applied: "Autofix lint/format",
  fixing: "Applying fix",
  fix_applied: "Fix applied",
  fix_requested: "Fix requested",
  self_review: "Self-review completed",
  quality_passed: "Technical checks passed",
  quality_failed: "Technical checks failed",
  correction_sent: "Correction sent",
  pr_checked: "PR check completed",
  comment_resolved: "Comment resolved",
  comment_resolve_failed: "Comment fix failed",
  comment_skipped: "Comment ignored",
  comment_batch_planned: "PR fixes plan ready",
  comment_batch_applied: "PR fixes applied",
  comment_batch_failed: "PR fixes incomplete",
  comment_batch_committed: "PR fixes published",
  comment_batch_commit_failed: "PR fixes commit failed",
  ticket_chat_planned: "Plan from chat ready",
  restart_requested: "Restart from scratch requested",
  external_change: "Updated in Azure Boards",
  external_completed: "Completed in Azure Boards",
  pr_completed: "PR completed",
  pr_abandoned: "PR abandoned",
  closed: "Closed manually",
  deleted: "Deleted",
  reopened: "Reopened",
  error: "Error",
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
  { id: "loaded", label: "Loaded", description: "Waiting to be analyzed" },
  { id: "plan", label: "Plan ready", description: "Require plan approval" },
  { id: "progress", label: "In progress", description: "The agent is working or checking" },
  { id: "review", label: "Awaiting review", description: "Require your action or review" },
  { id: "pr", label: "PR completed", description: "Work completed" },
];

const WORKFLOW_MAP_NODES = [
  ["discover", "Discover tickets", "Ingest", "Azure DevOps SDK + PAT", "Reads assigned current-iteration work items from Azure Boards."],
  ["decompose", "Decompose Epic", "Ingest", "Copilot + Headroom + Graphify", "Splits an Epic into independent PBIs before implementation."],
  ["branch", "Create or reuse branch", "Git", "git", "Creates the feature/bugfix branch or resumes the known branch."],
  ["graphify", "Query Graphify", "Context", "graphify query", "Uses the existing code graph before broad Read, Grep, or Glob searches."],
  ["headroom", "Optimize context", "Context", "headroom wrap copilot", "Routes supported Copilot runs through Headroom when AGENT_USE_HEADROOM is true."],
  ["plan", "Generate plan", "Agent", "Copilot + Headroom + Graphify", "Produces a read-only implementation plan and waits for approval."],
  ["approve-plan", "Approve plan", "User", "Dashboard user action", "Approves the plan before any implementation begins."],
  ["implement", "Implement ticket", "Agent", "Claude SDK + Read/Edit/Bash + git", "Edits code, commits, and pushes the approved work item."],
  ["autofix", "Run deterministic autofix", "Quality", "prettier + nx lint", "Runs formatting and lint fixes without an AI agent."],
  ["technical-summary", "Save technical summary", "History", "SQLite history", "Persists implementation details and live corrections for later review."],
  ["run-checks", "Run technical checks", "Quality", "project test/lint/type-check/build", "Executes the detected test, lint, type-check, and build commands."],
  ["create-pr", "Create PR", "Azure DevOps", "Azure DevOps SDK + PAT", "Creates a pull request only after checks pass and the configured policy allows it."],
  ["autocomplete", "Auto-complete PR", "Azure DevOps", "Azure DevOps SDK + PAT", "Requests Azure DevOps auto-completion when explicitly selected."],
  ["review-pr", "Run synthetic review", "Review", "Claude SDK + git diff", "Posts review findings using the configured review workflow."],
  ["read-comments", "Read PR comments", "Review", "Azure DevOps SDK + PAT", "Loads unresolved Azure DevOps comment threads."],
  ["classify-comment", "Classify comment", "Agent", "Claude SDK + Read/Edit/Bash", "Separates mechanical fixes from issues requiring human judgment."],
  ["plan-batch", "Plan comment fixes", "Review", "Copilot + Headroom + Graphify", "Builds a plan for the selected PR comment fixes."],
  ["apply-batch", "Apply planned fixes", "Agent", "Claude SDK + Read/Edit", "Applies approved comment fixes without committing first."],
  ["commit-batch", "Commit and push fixes", "Git", "Claude SDK + git + tests", "Commits the approved batch and pushes it to the PR branch."],
  ["reply-resolve", "Reply and resolve", "Azure DevOps", "Azure DevOps SDK + PAT", "Publishes a reply and resolves a selected review thread."],
  ["request-fix", "Request a fix", "User", "Dashboard + Claude SDK", "Sends a correction for a completed implementation."],
  ["live-correction", "Send live correction", "User", "Dashboard + Claude SDK", "Interrupts a compatible active agent turn and injects the feedback."],
  ["block", "Block ticket", "User", "Azure DevOps SDK + PAT", "Stops automatic progress and marks the ticket as blocked."],
  ["close", "Close ticket", "User", "Azure DevOps SDK + PAT", "Marks the ticket completed manually in Azure Boards."],
  ["reopen", "Reopen ticket", "User", "Azure DevOps SDK + PAT", "Removes completion/blocking tags so automation can continue."],
  ["restart", "Restart from scratch", "User", "Azure DevOps SDK + git", "Clears workflow state, optionally deletes the local branch, and starts planning again."],
  ["ticket-chat", "Plan through ticket chat", "User", "Copilot + Headroom + Graphify", "Saves a ticket request and generates a read-only plan."],
  ["workflow-settings", "Save workflow policy", "Settings", "Dashboard + SQLite", "Changes agent routing and Azure DevOps communication policy."],
  ["workflow-chat", "Configure workflow chat", "Settings", "Dashboard + SQLite", "Interprets supported natural-language routing and approval instructions."],
  ["agent-settings", "Configure agent", "Settings", "Dashboard + .env", "Selects Claude, Copilot, auto routing, model, budgets, and Headroom."],
  ["notifications", "Read notifications", "Dashboard", "SQLite history", "Shows attention, quality, and workflow events."],
  ["history", "Inspect history", "Dashboard", "SQLite history", "Shows persisted runs, decisions, and messages."],
  ["dashboard", "Inspect metrics", "Dashboard", "SQLite history", "Filters completion, tokens, cost, and throughput metrics."],
];

let selectedWorkflowMapNode = null;
let workflowMapTransform = { scale: 1, x: 0, y: 0 };

function renderWorkflowMap() {
  const canvas = document.getElementById("workflow-map-canvas");
  if (canvas.children.length) return;
  for (const [id, title, group, tools, description] of WORKFLOW_MAP_NODES) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "workflow-map-node";
    node.dataset.nodeId = id;
    node.innerHTML = `<span>${group}</span><strong>${title}</strong><small>Uses: ${tools}</small>`;
    node.addEventListener("click", () => selectWorkflowMapNode(id));
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") selectWorkflowMapNode(id);
    });
    canvas.appendChild(node);
  }
  setupWorkflowMapInteractions();
}

function updateWorkflowMapTransform() {
  const canvas = document.getElementById("workflow-map-canvas");
  canvas.style.transform = `translate(${workflowMapTransform.x}px, ${workflowMapTransform.y}px) scale(${workflowMapTransform.scale})`;
}

function selectWorkflowMapNode(nodeId) {
  selectedWorkflowMapNode = WORKFLOW_MAP_NODES.find(([id]) => id === nodeId) || null;
  for (const node of document.querySelectorAll(".workflow-map-node")) {
    const selected = node.dataset.nodeId === nodeId;
    node.classList.toggle("selected", selected);
    node.setAttribute("aria-pressed", String(selected));
  }
  const detail = document.getElementById("workflow-map-detail");
  detail.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = `${selectedWorkflowMapNode[1]} — ${selectedWorkflowMapNode[2]}`;
  const description = document.createElement("p");
  description.textContent = selectedWorkflowMapNode[4];
  const tools = document.createElement("p");
  tools.textContent = `Uses: ${selectedWorkflowMapNode[3]}`;
  detail.append(title, description, tools);
  document.getElementById("workflow-feedback-input").disabled = false;
  document.getElementById("workflow-feedback-send-btn").disabled = false;
  document.getElementById("workflow-feedback-input").focus();
}

function setupWorkflowMapInteractions() {
  const viewport = document.getElementById("workflow-map-viewport");
  let dragStart = null;
  viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    const scale = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    workflowMapTransform.scale = Math.max(.45, Math.min(2.5, workflowMapTransform.scale * scale));
    updateWorkflowMapTransform();
  }, { passive: false });
  viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".workflow-map-node")) return;
    dragStart = { x: event.clientX, y: event.clientY, startX: workflowMapTransform.x, startY: workflowMapTransform.y };
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!dragStart) return;
    workflowMapTransform.x = dragStart.startX + event.clientX - dragStart.x;
    workflowMapTransform.y = dragStart.startY + event.clientY - dragStart.y;
    updateWorkflowMapTransform();
  });
  viewport.addEventListener("pointerup", () => { dragStart = null; });
}

function resetWorkflowMap() {
  workflowMapTransform = { scale: 1, x: 0, y: 0 };
  updateWorkflowMapTransform();
}

async function sendWorkflowMapFeedback(event) {
  event.preventDefault();
  if (!selectedWorkflowMapNode) return;
  const input = document.getElementById("workflow-feedback-input");
  const hint = document.getElementById("workflow-feedback-hint");
  const text = input.value.trim();
  if (!text) return;
  try {
    await fetchJson("/api/workflow/feedback", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: selectedWorkflowMapNode[0], text }),
    });
    input.value = "";
    hint.textContent = `Saved for “${selectedWorkflowMapNode[1]}”.`;
    hint.classList.remove("error");
    showActionFeedback("Workflow feedback saved.");
  } catch (err) {
    hint.textContent = `Unable to save feedback: ${err.message}`;
    hint.classList.add("error");
  }
}

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
  return d.toLocaleString("en-US", { dateStyle: "short", timeStyle: "medium" });
}

function showDialog({ title, message, confirmLabel = "Confirm", cancelLabel = "Cancel", isError = false }) {
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

function confirmAction(message, confirmLabel = "Confirm") {
  return showDialog({ title: "Confirm action", message, confirmLabel });
}

async function showError(message) {
  await showDialog({
    title: "Operation failed",
    message,
    confirmLabel: "Close",
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
  const label = loadingLabel || (method === "GET" ? "Loading data..." : "Processing request...");
  if (showLoader) setRequestLoading(true, label);
  try {
    const res = await fetch(url, options);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Error ${res.status}`);
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
    const step = ev ? actionLabel(ev.action) : "starting...";
    line.textContent = `Running — ${ticket} ${step}`.trim();
    stopBtn.hidden = false;
    if (workItemId) {
      blockBtn.hidden = false;
      blockBtn.textContent = `Block ticket #${workItemId}`;
      blockBtn.dataset.workItemId = workItemId;
    } else {
      blockBtn.hidden = true;
    }
  } else {
    card.classList.remove("active");
    line.textContent = "Inactive";
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
  const lastCheck = data.last_check ? `Last check: ${formatTime(data.last_check)}.` : "";
  const nextCheck = data.next_check ? ` Next: ${formatTime(data.next_check)}.` : "";
  label.textContent = `Automatic check every ${intervalMinutes} minutes. ${lastCheck}${nextCheck}`;
  status.classList.toggle("error", String(data.outcome).startsWith("error:"));
  status.title = `Outcome: ${data.outcome}`;
}

async function stopRun(script) {
  if (!await confirmAction(`Stop the current ${script} run?`, "Stop")) return;
  try {
    await fetchJson(`/api/stop/${script}`, { method: "POST" });
    showActionFeedback(`${script} run stopped. Status updated.`);
  } catch (err) {
    showActionFeedback(`Unable to stop ${script}: ${err.message}`, true);
    await showError(`Unable to stop ${script}: ${err.message}`);
  }
  await refreshAfterTicketMutation();
}

async function blockWorkItem(workItemId) {
  if (!await confirmAction(
    `Tag ticket #${workItemId} as agent:blocked? It will no longer be picked up automatically.`,
    "Block"
  )) return;
  try {
    await fetchJson(`/api/block/${workItemId}`, { method: "POST" });
    showActionFeedback(`Ticket #${workItemId} blocked. View updated.`);
  } catch (err) {
    showActionFeedback(`Unable to block ticket #${workItemId}: ${err.message}`, true);
    await showError(`Unable to block ticket #${workItemId}: ${err.message}`);
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
  count.setAttribute("aria-label", "Number of tickets");
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
      emptyState.textContent = "No tickets at this stage";
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
  card.querySelector(".ticket-card-title").textContent = info.title || "(untitled)";
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
        title: info.title || "(untitled)",
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
  title.textContent = "Loading title...";

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
    `Updated: ${formatTicketUpdate(ticket.ts)}`;
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
  warningBadge.title = `${warningCount} tickets require attention`;
  errorBadge.title = `${errorCount} tickets are blocked or have errors`;
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
    container.textContent = "No changes requested. The default workflow uses Copilot with Claude as a fallback.";
    return;
  }
  for (const message of messages) {
    const item = document.createElement("article");
    item.className = `workflow-chat-message ${message.role}`;
    const author = document.createElement("strong");
    author.textContent = message.role === "user" ? "You" : "Workflow";
    const content = document.createElement("p");
    content.textContent = message.content;
    item.append(author, content);
    container.appendChild(item);
  }
  container.scrollTop = container.scrollHeight;
}

function renderWorkflow(data) {
  renderWorkflowMap();
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
    hint.textContent = `Unable to load workflow: ${err.message}`;
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
    hint.textContent = "Workflow saved.";
    hint.classList.remove("error");
    await loadWorkflow();
  } catch (err) {
    hint.textContent = `Save failed: ${err.message}`;
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
  hint.textContent = "Updating workflow...";
  hint.classList.remove("error");
  try {
    renderWorkflow(await fetchJson("/api/workflow/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }));
    input.value = "";
    hint.textContent = "Workflow updated.";
  } catch (err) {
    hint.textContent = `Update failed: ${err.message}`;
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
    container.textContent = "No notifications.";
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
      button.textContent = "Mark as read";
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
    data.summary.story_points.toLocaleString("en-US");
  document.getElementById("dashboard-tokens").textContent =
    data.summary.total_tokens.toLocaleString("en-US");
  document.getElementById("dashboard-cost").textContent = formatCost(data.summary.cost_usd);

  const body = document.getElementById("dashboard-body");
  body.innerHTML = "";
  if (data.items.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "hint";
    cell.textContent = "No PBIs completed in the selected period.";
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
        textContent: item.total_tokens.toLocaleString("en-US"),
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
    container.textContent = "No action required at this time.";
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
  hint.textContent = "Loading metrics...";
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
    hint.textContent = `Unable to load dashboard: ${err.message}`;
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
      "Restart the dashboard to view the token budget.";
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
    `First launch: step ${fields.filter((item) => item.required).length - onboardingFields.length + 1} of ${fields.filter((item) => item.required).length}.`;
  const label = document.getElementById("onboarding-label");
  label.textContent = field.label;
  const input = document.getElementById("onboarding-input");
  input.type = field.secret ? "password" : "text";
  input.value = "";
  input.placeholder = field.secret ? "Enter a secure value" : "";
  input.required = true;
  input.dataset.settingsKey = field.key;
  document.getElementById("onboarding-hint").textContent =
    "This setting is saved only in your Windows profile.";
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
    hint.textContent = "Enter a value before continuing.";
    hint.classList.add("error");
    return;
  }

  onboardingSubmitting = true;
  document.getElementById("onboarding-submit").disabled = true;
  hint.classList.remove("error");
  hint.textContent = "Saving...";
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
    hint.textContent = `Save failed: ${err.message}`;
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
  const installButton = document.getElementById("install-app-update-btn");
  const releaseButton = document.getElementById("open-app-release-btn");
  checkButton.disabled = true;
  installButton.hidden = true;
  releaseButton.hidden = true;
  hint.classList.remove("error");
  hint.textContent = "Checking for updates...";
  try {
    const update = await fetchJson("/api/app-update");
    appReleaseUrl = update.release_url;
    if (update.update_available) {
      hint.textContent = `Installed version: ${update.current_version}. Version ${update.latest_version} is available.`;
      releaseButton.hidden = !appReleaseUrl;
      installButton.hidden = !(update.installer_available && window.pywebview?.api?.install_pending_update);
    } else if (!update.latest_version) {
      hint.textContent = `Installed version: ${update.current_version}. No GitHub Release has been published yet.`;
    } else {
      hint.textContent = `Installed version: ${update.current_version}. You already have the latest version (${update.latest_version}).`;
    }
  } catch (err) {
    appReleaseUrl = "";
    hint.textContent = `Unable to check for updates: ${err.message}`;
    hint.classList.add("error");
  } finally {
    checkButton.disabled = false;
  }
}

async function installAppUpdate() {
  const hint = document.getElementById("app-version-hint");
  const button = document.getElementById("install-app-update-btn");
  if (!window.pywebview?.api?.install_pending_update) {
    hint.textContent = "Direct installation is available only in the installed desktop app.";
    hint.classList.add("error");
    return;
  }
  if (!await confirmAction(
    "Download the verified update, close the app, and install it now?",
    "Download and install"
  )) return;
  button.disabled = true;
  hint.classList.remove("error");
  hint.textContent = "Downloading update and verifying SHA-256...";
  try {
    const update = await fetchJson("/api/app-update/download", { method: "POST" });
    hint.textContent = `Installer ${update.installer_name} verified. Closing the app to update...`;
    await window.pywebview.api.install_pending_update();
  } catch (err) {
    hint.textContent = `Update failed: ${err.message}`;
    hint.classList.add("error");
    button.disabled = false;
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
    budget.limit_tokens === null ? "Not set" : budget.limit_tokens.toLocaleString("en-US");
  document.getElementById("settings-token-budget-used").textContent =
    budget.used_tokens.toLocaleString("en-US");
  document.getElementById("settings-token-budget-remaining").textContent =
    budget.remaining_tokens === null ? "Unlimited" : budget.remaining_tokens.toLocaleString("en-US");
  hint.textContent = budget.is_exhausted
    ? "Budget exhausted: new runs are blocked until you increase the limit."
    : "Usage is updated by providers that return token metrics (Claude SDK).";
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
      input.placeholder = field.is_set ? `Set (${field.value}) — leave empty to keep unchanged` : "Not set";
    } else if (field.control !== "select") {
      input.value = field.value;
    }
    row.appendChild(input);
    form.appendChild(row);

    if (field.key === "AGENT_COMMAND") {
      externalAgentHelp = document.createElement("div");
      externalAgentHelp.className = "external-agent-help";
      externalAgentHelp.innerHTML = [
        "<strong>How it works</strong>",
        "The dashboard sends the prompt to the command's standard input and uses its standard output as the agent's response.",
        "<code>claude -p</code> is an example if Claude CLI is installed and authenticated, but it still uses your Claude account and tokens.",
        "To avoid the Claude budget, configure a different installed and authenticated CLI command here that reads stdin and writes its response to stdout.",
        "The command also receives the <code>AGENT_MODEL</code>, <code>AGENT_ALLOWED_TOOLS</code>, and <code>AGENT_MAX_OUTPUT_TOKENS</code> variables.",
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
        "<strong>GitHub Copilot CLI (experimental)</strong>",
        "Install it with <code>winget install GitHub.Copilot</code> and authenticate with <code>copilot login</code>.",
        "The CLI is currently interactive: the dashboard does not start automatic plans or changes, preventing runs from blocking while awaiting approval.",
        "You can use it manually from the repository with <code>copilot</code>.",
      ].map((text) => `<p>${text}</p>`).join("");
    } else {
      externalAgentHelp.innerHTML = [
        "<strong>How it works</strong>",
        "The dashboard sends the prompt to the command's standard input and uses its standard output as the agent's response.",
        "<code>claude -p</code> is an example if Claude CLI is installed and authenticated, but it still uses your Claude account and tokens.",
        "To avoid the Claude budget, configure a different installed and authenticated CLI command here that reads stdin and writes its response to stdout.",
        "The command also receives the <code>AGENT_MODEL</code>, <code>AGENT_ALLOWED_TOOLS</code>, and <code>AGENT_MAX_OUTPUT_TOKENS</code> variables.",
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
  hint.textContent = "Saving...";
  try {
    const fields = await fetchJson("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    renderSettingsForm(fields);
    hint.textContent = "Settings saved.";
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
    document.getElementById("detail-description").textContent = info.description || "(no description)";
    document.getElementById("detail-acceptance").textContent = info.acceptance_criteria
      ? `Acceptance criteria:\n${info.acceptance_criteria}` : "";

    const figmaEl = document.getElementById("detail-figma-links");
    figmaEl.innerHTML = "";
    for (const url of info.figma_urls || []) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.className = "ticket-link";
      a.textContent = `Design in Figma: ${url}`;
      figmaEl.appendChild(a);
      figmaEl.appendChild(document.createElement("br"));
    }
  } catch (err) {
    document.getElementById("detail-title").textContent = `Ticket #${workItemId}`;
    document.getElementById("detail-story-points").textContent = "";
    document.getElementById("detail-description").textContent = `Unable to load details: ${err.message}`;
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
    report.textContent = "Run the required technical checks before creating the PR.";
    return;
  }
  const labels = {
    passed: "Checks passed. You can create the PR.",
    failed: "At least one check failed: fix the branch and run them again.",
    unavailable: "The repository declares no detectable check commands.",
  };
  report.textContent = `${labels[quality.status] || "Check status unavailable."}\nVerified commit: ${quality.commit_sha.slice(0, 12)}`;
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
    document.getElementById("quality-report").textContent = `Unable to read checks: ${err.message}`;
  }
}

async function runQualityChecks() {
  if (qualityRunning) return;
  const button = document.getElementById("quality-check-btn");
  const report = document.getElementById("quality-report");
  qualityRunning = true;
  button.disabled = true;
  report.textContent = "Local checks in progress: no AI is being used...";
  try {
    const quality = await fetchJson(`/api/quality/${currentView.workItemId}`, { method: "POST" });
    renderQuality(quality);
    showActionFeedback("Checks completed. View updated.");
    await refreshAfterTicketMutation();
  } catch (err) {
    report.textContent = `Unable to run checks: ${err.message}`;
    showActionFeedback(`Checks failed: ${err.message}`, true);
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
    container.textContent = `Unable to load chat: ${err.message}`;
  }
}

function renderTicketChat(messages) {
  const container = document.getElementById("ticket-chat-messages");
  container.innerHTML = "";
  if (messages.length === 0) {
    container.textContent = "No messages: describe what you want to do next for this ticket.";
    return;
  }
  for (const message of messages) {
    const item = document.createElement("div");
    item.className = "ticket-timeline-item";
    const head = document.createElement("div");
    const author = message.role === "user" ? "You" : "Agent";
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
  hint.textContent = "Analysis and planning in progress...";
  hint.classList.remove("error");
  try {
    const result = await fetchJson(`/api/ticket-chat/${currentView.workItemId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    input.value = "";
    renderTicketChat(result.messages);
    hint.textContent = "Plan ready.";
    showActionFeedback("Plan ready. View updated.");
    await refreshAfterTicketMutation();
  } catch (err) {
    hint.textContent = `Send failed: ${err.message}`;
    hint.classList.add("error");
    showActionFeedback(`Send failed: ${err.message}`, true);
  } finally {
    ticketChatSubmitting = false;
    input.disabled = false;
    document.getElementById("ticket-chat-send-btn").disabled = false;
  }

}

async function restartTicketFromScratch() {
  const workItemId = currentView.workItemId;
  const confirmed = await confirmAction(
    `Start over for ticket #${workItemId}? Tags from the previous cycle and the associated local branch will be removed.`,
    "Start over"
  );
  if (!confirmed) return;
  const button = document.getElementById("restart-ticket-btn");
  const hint = document.getElementById("ticket-chat-hint");
  button.disabled = true;
  hint.textContent = "Reset and new planning starting...";
  hint.classList.remove("error");
  try {
    await fetchJson(`/api/restart-from-scratch/${workItemId}`, { method: "POST" });
    hint.textContent = "Ticket reset: the agent is generating a new plan.";
    await Promise.all([
      refreshStatus(true),
      refreshAutomaticIngestStatus(true),
      refreshTickets(true),
      refreshHistory(true),
    ]);
    await loadNotifications(true);
    showActionFeedback("Ticket reset: new planning is in progress.");
  } catch (err) {
    const message = `Unable to start over: ${err.message}`;
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
    containerEl.textContent = "No events recorded.";
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
      ticket.detail || "No technical summary is available for this ticket.";
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
    hint.textContent = active ? "" : "No run is active for this ticket at the moment.";
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
    hint.textContent = "Correction sent.";
    hint.classList.remove("error");
  } catch (err) {
    // il run potrebbe essere terminato nel frattempo: non svuotare il testo scritto
    hint.textContent = `Send failed: ${err.message}`;
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
    hint.textContent = "Changes saved.";
    hint.classList.remove("error");
  } catch (err) {
    hint.textContent = `Save failed: ${err.message}`;
    hint.classList.add("error");
  }
}

async function approvePlan() {
  if (!await confirmAction("Approve this plan and start implementation?", "Approve and start")) return;
  const hint = document.getElementById("plan-hint");
  try {
    await savePlan();
    await fetchJson(`/api/plan/${currentView.workItemId}/approve`, { method: "POST" });
    hint.textContent = "Plan approved, implementation in progress.";
    hint.classList.remove("error");
    showActionFeedback("Plan approved: implementation started.");
    await refreshAfterTicketMutation();
  } catch (err) {
    hint.textContent = `Approval failed: ${err.message}`;
    hint.classList.add("error");
    showActionFeedback(`Approval failed: ${err.message}`, true);
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
    hint.textContent = "Fix request sent.";
    hint.classList.remove("error");
  } catch (err) {
    hint.textContent = `Send failed: ${err.message}`;
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
  reportEl.textContent = "Review in progress; this may take a few minutes...";
  try {
    const result = await fetchJson(`/api/review-code/${currentView.workItemId}`, { method: "POST" });
    reportEl.textContent = result.review || "(no content returned)";
  } catch (err) {
    reportEl.textContent = `Review failed: ${err.message}`;
  } finally {
    reviewRunning = false;
    btn.disabled = false;
  }
}

async function closeTicket() {
  const workItemId = currentView.workItemId;
  if (!await confirmAction(
    `Close ticket #${workItemId}? It will be moved to "Completed" and ingest/review will no longer process it (it will also be blocked).`,
    "Close ticket"
  )) {
    return;
  }
  try {
    await fetchJson(`/api/close/${workItemId}`, { method: "POST" });
    showActionFeedback(`Ticket #${workItemId} closed and moved to Completed.`);
    await refreshAfterTicketMutation();
  } catch (err) {
    showActionFeedback(`Close failed: ${err.message}`, true);
    await showError(`Unable to close ticket #${workItemId}: ${err.message}`);
  }
}

async function deleteTicket() {
  const workItemId = currentView.workItemId;
  const confirmed = await confirmAction(
    `Delete ticket #${workItemId}? It will be moved to the Azure Boards recycle bin and will no longer be processed by the agent.`,
    "Delete ticket"
  );
  if (!confirmed) return;

  const button = document.getElementById("btn-delete-ticket");
  button.disabled = true;
  try {
    await fetchJson(
      `/api/tickets/${workItemId}`,
      { method: "DELETE" },
      { loadingLabel: "Deleting ticket..." }
    );
    ticketInfoCache.delete(String(workItemId));
    showActionFeedback(`Ticket #${workItemId} moved to the Azure Boards recycle bin.`);
    await refreshAfterTicketMutation();
    showListView();
  } catch (err) {
    const message = `Unable to delete ticket #${workItemId}: ${err.message}`;
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
    hint.textContent = "Ticket reopened.";
    hint.classList.remove("error");
    showActionFeedback("Ticket reopened. View updated.");
    await refreshAfterTicketMutation();
  } catch (err) {
    hint.textContent = `Unable to reopen: ${err.message}`;
    hint.classList.add("error");
    showActionFeedback(`Reopen failed: ${err.message}`, true);
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
  resultEl.textContent = "Check in progress (the three junior/senior/tech-lead personas); this may take a few minutes...";
  try {
    const result = await fetchJson(`/api/check-pr/${currentView.workItemId}`, { method: "POST" });
    let text = result.summary ? `${result.summary}\n\n` : "";
    text += `${result.posted} comments posted on the PR`;
    if (result.failed) text += `, ${result.failed} could not be posted (missing file/line)`;
    text += ".";
    resultEl.textContent = text;
  } catch (err) {
    resultEl.textContent = `Check failed: ${err.message}`;
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
  hint.textContent = "Loading comments...";
  hint.classList.remove("error");
  try {
    const comments = await fetchJson(`/api/pr-comments/${currentView.workItemId}`);
    await loadPrCommentBatch();
    renderPrComments(comments);
    prCommentsLoadedForWorkItem = currentView.workItemId;
    hint.textContent = comments.length ? `${comments.length} comments to evaluate.` : "";
  } catch (err) {
    hint.textContent = `Unable to read comments: ${err.message}`;
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
    if (!err.message.includes("No fix plan")) throw err;
    currentPrCommentBatch = null;
    box.hidden = selectedPrCommentIds.size === 0;
    updatePrCommentBatchActions();
  }
}

function renderPrComments(comments) {
  const container = document.getElementById("pr-comments-list");
  container.innerHTML = "";
  if (comments.length === 0) {
    container.textContent = "No comments to evaluate.";
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
      ignored.textContent = c.dismissed ? "Ignored by the plan" : "Resolved in Azure DevOps";
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
    selectText.textContent = "Include in plan";
    selectLabel.append(select, selectText);
    const noteBox = document.createElement("label");
    noteBox.className = "pr-comment-plan-note";
    noteBox.hidden = !select.checked;
    noteBox.textContent = "Note for the plan (optional)";
    const noteInput = document.createElement("textarea");
    noteInput.rows = 2;
    noteInput.placeholder = "Specify constraints, priorities, or aspects to consider.";
    noteInput.value = prCommentPlanningNotes.get(c.thread_id) || "";
    noteInput.addEventListener("input", () => {
      prCommentPlanningNotes.set(c.thread_id, noteInput.value);
    });
    noteBox.append(noteInput);
    if (c.dismissed) {
      const restoreBtn = document.createElement("button");
      restoreBtn.type = "button";
      restoreBtn.textContent = "Include in plan again";
      restoreBtn.addEventListener("click", () => restoreComment(c.thread_id, restoreBtn));
      actions.append(selectLabel, restoreBtn);

      const reply = document.createElement("div");
      reply.className = "pr-comment-reply";
      const replyLabel = document.createElement("label");
      replyLabel.textContent = "Reply for Azure DevOps";
      const replyInput = document.createElement("textarea");
      replyInput.rows = 3;
      replyInput.placeholder = "Write a reply: it will be published on the thread and the comment will be resolved.";
      const replyBtn = document.createElement("button");
      replyBtn.type = "button";
      replyBtn.textContent = "Reply and resolve";
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
      dismissBtn.textContent = "Ignore";
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
  if (!await confirmAction("Ignore only this comment? The other PR comments will remain available.", "Ignore comment")) {
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
      ? `${availableCount} comments to evaluate.`
      : "No comments to evaluate.";
  } catch (err) {
    button.disabled = false;
    await showError(`Unable to ignore the comment: ${err.message}`);
  }
}

async function restoreComment(threadId, button) {
  button.disabled = true;
  try {
    await fetchJson(`/api/pr-comments/${currentView.workItemId}/${threadId}/restore`, { method: "POST" });
    await loadPrComments(true);
  } catch (err) {
    button.disabled = false;
    await showError(`Unable to include the comment again: ${err.message}`);
  }
}

async function replyAndResolveComment(threadId, input, button) {
  if (!await confirmAction(
    "Publish this reply in Azure DevOps and resolve the selected comment?",
    "Reply and resolve"
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
    await showError(`Unable to reply to and resolve the comment: ${err.message}`);
  }
}

function updatePrCommentBatchActions(batch = currentPrCommentBatch) {
  const planBtn = document.getElementById("pr-comments-plan-btn");
  const applyBtn = document.getElementById("pr-comments-apply-btn");
  const commitBtn = document.getElementById("pr-comments-commit-btn");
  planBtn.hidden = Boolean(batch && batch.status === "changes_applied");
  planBtn.textContent = batch && batch.status === "plan_ready" ? "Regenerate plan" : "Create plan for selected comments";
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
    hint.textContent = "Plan ready: approve to apply changes without committing.";
  } else if (batch.status === "changes_applied") {
    hint.textContent = "Changes applied without a commit: review them and approve the commit and push when ready.";
  } else if (batch.status === "completed") {
    hint.textContent = "Commit and push completed; the selected threads have been resolved.";
  }
  updatePrCommentBatchActions(batch);
}

async function createPrCommentBatchPlan() {
  if (prBatchSubmitting || selectedPrCommentIds.size === 0) return;
  const hint = document.getElementById("pr-comment-batch-hint");
  prBatchSubmitting = true;
  updatePrCommentBatchActions();
  hint.textContent = "Creating the plan for selected comments...";
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
    hint.textContent = `Plan failed: ${err.message}`;
    hint.classList.add("error");
  } finally {
    prBatchSubmitting = false;
    updatePrCommentBatchActions();
  }
}

async function applyPrCommentBatch() {
  if (prBatchSubmitting || !await confirmAction(
    "Apply the plan's changes without committing or pushing?",
    "Apply changes"
  )) return;
  const hint = document.getElementById("pr-comment-batch-hint");
  prBatchSubmitting = true;
  updatePrCommentBatchActions();
  hint.textContent = "Applying approved changes without committing...";
  try {
    const result = await fetchJson(`/api/pr-comment-batch/${currentView.workItemId}/apply`, { method: "POST" });
    renderPrCommentBatch(result);
  } catch (err) {
    hint.textContent = `Application failed: ${err.message}`;
    hint.classList.add("error");
  } finally {
    prBatchSubmitting = false;
    updatePrCommentBatchActions();
  }
}

async function commitPrCommentBatch() {
  if (prBatchSubmitting || !await confirmAction(
    "Confirm committing and pushing the changes already applied?",
    "Commit e push"
  )) return;
  const hint = document.getElementById("pr-comment-batch-hint");
  prBatchSubmitting = true;
  updatePrCommentBatchActions();
  hint.textContent = "Running the approved commit and push...";
  try {
    const result = await fetchJson(`/api/pr-comment-batch/${currentView.workItemId}/commit`, { method: "POST" });
    renderPrCommentBatch(result);
    if (result.committed)     await loadPrComments(true);
  } catch (err) {
    hint.textContent = `Commit or push failed: ${err.message}`;
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
    await showError(`Unable to create the PR for #${workItemId}: ${err.message}`);
  }
  await Promise.all([refreshTickets(), refreshHistory()]);
}

async function triggerRun(script) {
  try {
    await fetchJson(`/api/run/${script}`, { method: "POST" });
    showActionFeedback(`${script} run started. Status updated.`);
  } catch (err) {
    showActionFeedback(`Unable to start ${script}: ${err.message}`, true);
    await showError(`Unable to start ${script}: ${err.message}`);
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
    console.error("Dashboard update error:", err);
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
document.getElementById("install-app-update-btn").addEventListener("click", installAppUpdate);
document.getElementById("open-app-release-btn").addEventListener("click", openAppRelease);
document.getElementById("onboarding-form").addEventListener("submit", saveOnboardingField);
document.getElementById("onboarding-dialog").addEventListener("cancel", (event) => {
  if (onboardingFields.length > 0) event.preventDefault();
});
document.getElementById("dashboard-apply-btn").addEventListener("click", loadDashboard);
document.getElementById("notifications-refresh-btn").addEventListener("click", loadNotifications);
document.getElementById("workflow-save-btn").addEventListener("click", saveWorkflow);
document.getElementById("workflow-chat-send-btn").addEventListener("click", sendWorkflowChat);
document.getElementById("workflow-feedback-form").addEventListener("submit", sendWorkflowMapFeedback);
document.getElementById("workflow-map-zoom-in").addEventListener("click", () => {
  workflowMapTransform.scale = Math.min(2.5, workflowMapTransform.scale * 1.2);
  updateWorkflowMapTransform();
});
document.getElementById("workflow-map-zoom-out").addEventListener("click", () => {
  workflowMapTransform.scale = Math.max(.45, workflowMapTransform.scale / 1.2);
  updateWorkflowMapTransform();
});
document.getElementById("workflow-map-reset").addEventListener("click", resetWorkflowMap);

(async function init() {
  applyLayout();
  let configAvailable = true;
  try {
    await loadConfig();
  } catch (err) {
    // Configurazione (ancora) incompleta, es. PAT non impostato: manda
    // l'utente dritto in Impostazioni invece di lasciare la dashboard rotta.
    console.warn("Configuration unavailable, opening Settings:", err.message);
    showNav("settings");
    configAvailable = false;
  }
  if (configAvailable) await loadDashboard();
  if (configAvailable) await refreshAutomaticIngestStatus();
  await tick();
  setInterval(tick, POLL_MS);
})();
