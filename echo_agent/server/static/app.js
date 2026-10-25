const state = {
  projects: [],
  currentProjectId: null,
  currentProject: null,
  runStatus: null,
  refreshTimer: null,
  selectedFactId: null,
  selectedIntentId: null,
  graphZoom: 1,
  graphBaseWidth: 900,
  graphBaseHeight: 520,
  busy: false,
};

const els = {
  status: document.querySelector("#connection-status"),
  projectList: document.querySelector("#project-list"),
  form: document.querySelector("#create-project-form"),
  taskType: document.querySelector("#create-task-type"),
  targetUrlField: document.querySelector("#target-url-field"),
  cryptoFilesField: document.querySelector("#crypto-files-field"),
  cryptoInputs: document.querySelector("#crypto-inputs"),
  cryptoInputTitle: document.querySelector("#crypto-input-title"),
  cryptoInputList: document.querySelector("#crypto-input-list"),
  cryptoCandidates: document.querySelector("#crypto-candidates"),
  cryptoCandidateTitle: document.querySelector("#crypto-candidate-title"),
  cryptoCandidateList: document.querySelector("#crypto-candidate-list"),
  uploadForm: document.querySelector("#upload-input-form"),
  refreshProjects: document.querySelector("#refresh-projects"),
  refreshCurrent: document.querySelector("#refresh-current"),
  refreshLogs: document.querySelector("#refresh-logs"),
  clearLogs: document.querySelector("#clear-logs"),
  activateProject: document.querySelector("#activate-project"),
  runProject: document.querySelector("#run-project"),
  stopProject: document.querySelector("#stop-project"),
  deleteProject: document.querySelector("#delete-project"),
  title: document.querySelector("#project-title"),
  subtitle: document.querySelector("#project-subtitle"),
  summary: document.querySelector("#summary-row"),
  graphViewport: document.querySelector("#graph-viewport"),
  graph: document.querySelector("#project-graph"),
  graphDetail: document.querySelector("#graph-detail"),
  graphZoomIn: document.querySelector("#graph-zoom-in"),
  graphZoomOut: document.querySelector("#graph-zoom-out"),
  graphZoomFit: document.querySelector("#graph-zoom-fit"),
  graphZoomReset: document.querySelector("#graph-zoom-reset"),
  graphZoomLabel: document.querySelector("#graph-zoom-label"),
  logs: document.querySelector("#log-list"),
};

function authToken() {
  return localStorage.getItem("echoAgentToken") || "";
}

async function api(path, options = {}, retried = false) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = authToken();
  if (token) {
    headers["X-Echo-Agent-Token"] = token;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && !retried) {
    const entered = window.prompt("需要访问令牌（X-Echo-Agent-Token）：");
    if (entered && entered.trim()) {
      localStorage.setItem("echoAgentToken", entered.trim());
      return api(path, options, true);
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function loadProjects() {
  try {
    state.projects = await api("/projects");
    setStatus(`online · ${new Date().toLocaleTimeString()}`);
    renderProjects();
    if (!state.currentProjectId && state.projects.length) {
      await selectProject(state.projects[0].id);
    }
  } catch (error) {
    setStatus(`error · ${error.message}`);
  }
}

function renderProjects() {
  els.projectList.innerHTML = "";
  if (!state.projects.length) {
    els.projectList.innerHTML = '<div class="empty">No projects</div>';
    return;
  }
  for (const project of state.projects) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `project-item ${project.id === state.currentProjectId ? "active" : ""}`;
    item.innerHTML = `
      <strong>${escapeHtml(project.name)}</strong>
      <span class="badge ${project.status}">${project.status}</span>
      <small>#${project.id}</small>
      <small>${escapeHtml(project.target_url || "no target")}</small>
    `;
    item.addEventListener("click", () => selectProject(project.id));
    els.projectList.appendChild(item);
  }
}

async function selectProject(projectId) {
  state.currentProjectId = projectId;
  await refreshCurrent();
  scheduleRefresh();
}

async function refreshCurrent() {
  if (!state.currentProjectId) return;
  state.currentProject = await api(`/projects/${state.currentProjectId}`);
  state.runStatus = await api(`/projects/${state.currentProjectId}/run`);
  renderCurrentProject();
  await loadLogs();
  await refreshProjectsOnly();
}

async function refreshProjectsOnly() {
  state.projects = await api("/projects");
  renderProjects();
}

function renderCurrentProject() {
  const project = state.currentProject;
  if (!project) {
    clearCurrentProject();
    return;
  }
  if (state.selectedFactId !== null && !project.facts.some((fact) => fact.id === state.selectedFactId)) {
    state.selectedFactId = null;
  }
  if (state.selectedIntentId !== null && !project.intents.some((intent) => intent.id === state.selectedIntentId)) {
    state.selectedIntentId = null;
  }
  els.title.textContent = project.name;
  els.subtitle.textContent = `#${project.id} · ${project.objective || "No objective"}`;
  if (project.task_type === "crypto") {
    const inputs = project.crypto?.inputs || [];
    els.cryptoInputs.hidden = false;
    els.cryptoInputTitle.textContent = `输入清单 · ${inputs.length} 个文件`;
    els.cryptoInputList.innerHTML = inputs.length ? inputs.map((input) => `<div><code>${escapeHtml(input.id)}</code> · ${escapeHtml(input.role)} · ${escapeHtml(input.filename || input.path)} · ${input.size_bytes} B · SHA-256 <code>${escapeHtml(input.sha256)}</code></div>`).join("") : "<div>暂无上传文件</div>";
    renderCryptoCandidates(project);
  } else {
    els.cryptoInputs.hidden = true;
    els.cryptoInputList.innerHTML = "";
    els.cryptoCandidates.hidden = true;
    els.cryptoCandidateList.innerHTML = "";
  }
  const values = [
    project.status,
    project.target_url || "-",
    `${project.facts.length} facts · ${project.intents.length} intents`,
    project.docker || "-",
    state.runStatus?.running ? "running" : "idle",
  ];
  [...els.summary.querySelectorAll("strong")].forEach((node, index) => {
    node.textContent = values[index];
  });
  updateActionButtons();
  drawGraph(project);
  renderGraphDetail(project);
}

function hexToDisplayText(hex) {
  if (typeof hex !== "string" || !hex || hex.length % 2 || !/^[0-9a-f]+$/i.test(hex)) return null;
  try {
    const bytes = new Uint8Array(hex.match(/.{2}/g).map((pair) => parseInt(pair, 16)));
    const text = new TextDecoder().decode(bytes);
    if (!text || text.includes("\ufffd")) return null;
    if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(text)) return null;
    return text;
  } catch {
    return null;
  }
}

function candidateBadge(verification, acceptance) {
  if (acceptance) return { text: "平台已接受", className: "ok" };
  if (!verification) return { text: "未验证", className: "pending" };
  if (verification.status === "pass") return { text: "验证通过", className: "ok" };
  if (verification.status === "fail") return { text: "验证失败", className: "bad" };
  return { text: "无法验证", className: "pending" };
}

function renderCryptoCandidates(project) {
  const crypto = project.crypto || {};
  const goal = crypto.goal || {};
  const candidates = (project.facts || []).filter((fact) => fact.kind === "candidate");
  els.cryptoCandidates.hidden = false;
  els.cryptoCandidateTitle.textContent = `候选与验证 · ${candidates.length} 个候选`;
  const goalParts = [
    `answer_kind=${goal.answer_kind || "plaintext"}`,
    `answer_form=${goal.answer_form || "literal"}`,
  ];
  if (goal.answer_form === "hash" && goal.answer_hash) goalParts.push(`hash=${goal.answer_hash}`);
  goalParts.push(`verification=${goal.verification_mode || "strict"}`);
  goalParts.push(`solution=${goal.solution_policy || "unique"}`);
  const goalLine = `<div class="candidate-goal">完成目标：<code>${escapeHtml(goalParts.join(" · "))}</code></div>`;
  if (!candidates.length) {
    els.cryptoCandidateList.innerHTML = `${goalLine}<div>暂无候选</div>`;
    return;
  }
  const verifications = crypto.verifications || [];
  const acceptances = crypto.acceptances || [];
  const rows = candidates.map((fact) => {
    const candidate = fact.crypto?.candidate || {};
    const verification = [...verifications].reverse().find((item) => item.candidate_fact_id === fact.id);
    const acceptance = [...acceptances].reverse().find((item) => item.candidate_fact_id === fact.id);
    const checks = verification?.checks || {};
    const badge = candidateBadge(verification, acceptance);
    const details = [];
    if (candidate.scheme) details.push(`scheme=${candidate.scheme}`);
    if (candidate.answer_kind) details.push(`answer_kind=${candidate.answer_kind}`);
    if (candidate.m) details.push(`m=${trimText(candidate.m, 80)}`);
    if (verification?.status) details.push(`验证=${verification.status}${verification.method ? ` (${verification.method})` : ""}`);
    if (verification?.backend) details.push(`后端=${verification.backend}`);
    if (typeof verification?.elapsed_ms === "number") details.push(`耗时=${verification.elapsed_ms}ms`);
    if (verification?.terminated_reason && verification.terminated_reason !== "completed") {
      details.push(`终止=${verification.terminated_reason}`);
    }
    if (acceptance?.operator) details.push(`回执=${acceptance.operator}${acceptance.accepted_at ? ` @ ${acceptance.accepted_at}` : ""}`);
    const preimageText = hexToDisplayText(checks.preimage_hex);
    const preimageLine = checks.preimage_hex
      ? `<div>恢复出的明文：${preimageText !== null ? `<code>${escapeHtml(trimText(preimageText, 200))}</code> ` : ""}<code>${escapeHtml(trimText(checks.preimage_hex, 200))}</code></div>`
      : "";
    const expectedLine = checks.answer_hash_expected
      ? `<div>期望哈希（${escapeHtml(checks.answer_hash_algorithm || "")}）：<code>${escapeHtml(checks.answer_hash_expected)}</code></div>`
      : "";
    const reasonLine = checks.reason
      ? `<div>验证说明：<code>${escapeHtml(trimText(checks.reason, 300))}</code></div>`
      : "";
    return `<div class="candidate-row">
      <div><strong>#${fact.id}</strong> <span class="candidate-badge ${badge.className}">${escapeHtml(badge.text)}</span> answer=<code>${escapeHtml(trimText(candidate.answer ?? "", 200))}</code></div>
      ${details.length ? `<div class="candidate-meta">${escapeHtml(details.join(" · "))}</div>` : ""}
      ${preimageLine}${expectedLine}${reasonLine}
      <div class="candidate-desc">${escapeHtml(trimText(fact.description || "", 300))}</div>
    </div>`;
  });
  els.cryptoCandidateList.innerHTML = goalLine + rows.join("");
}

function updateActionButtons() {
  const project = state.currentProject;
  const hasProject = Boolean(project);
  const status = project?.status;
  const canRun = status === "active" || status === "stoped";
  const canStop = status === "running";
  els.refreshCurrent.disabled = !hasProject || state.busy;
  els.refreshLogs.disabled = !hasProject || state.busy;
  els.clearLogs.disabled = !hasProject || state.busy;
  els.activateProject.disabled = !hasProject || state.busy || status !== "inactive";
  els.runProject.disabled = !hasProject || state.busy || !canRun;
  els.stopProject.disabled = !hasProject || state.busy || !canStop;
  els.deleteProject.disabled = !hasProject || state.busy || canStop;
  els.uploadForm.querySelector('button[type="submit"]').disabled = !hasProject || state.busy || project.task_type !== "crypto" || canStop || project.status === "completed";
}

async function loadLogs() {
  if (!state.currentProjectId) return;
  const logs = await api(`/projects/${state.currentProjectId}/logs?limit=200`);
  renderLogs(logs);
}

function renderLogs(logs) {
  els.logs.innerHTML = "";
  if (!logs.length) {
    els.logs.innerHTML = '<div class="empty">No runtime logs</div>';
    return;
  }
  for (const log of logs.slice().reverse()) {
    const item = document.createElement("div");
    item.className = `log-entry ${log.level}`;
    item.innerHTML = `
      <time>${escapeHtml(log.created_at)} · ${escapeHtml(log.level)}</time>
      <p>${escapeHtml(log.message)}</p>
    `;
    els.logs.appendChild(item);
  }
}

function drawGraph(project) {
  const svg = els.graph;
  svg.innerHTML = "";
  const facts = project.facts;
  const intents = project.intents;
  if (!facts.length && !intents.length) {
    svg.innerHTML = '<text x="36" y="52" fill="#667085" font-size="18">No graph data</text>';
    return;
  }

  const layout = layoutGraph(facts, intents);
  const { width, height, factPositions, intentPositions } = layout;
  state.graphBaseWidth = width;
  state.graphBaseHeight = height;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  applyGraphZoom();
  svg.appendChild(graphDefs());
  svg.appendChild(graphBackground(width, height));
  const viewport = svgElement("g", {});
  svg.appendChild(viewport);
  const edgeOffsets = computeEdgeOffsets(intents);
  const edgeLayer = svgElement("g", { class: "graph-edge-layer" });
  const nodeLayer = svgElement("g", { class: "graph-node-layer" });
  viewport.appendChild(edgeLayer);
  viewport.appendChild(nodeLayer);

  for (const intent of intents) {
    const from = factPositions.get(intent.from);
    const renderedTo = renderedIntentTo(intent);
    const to = factPositions.get(renderedTo);
    const intentTarget = intentPositions.get(intent.id);
    if (!from && !to && !intentTarget) continue;
    const start = from || { x: 70, y: Math.max(70, to?.y || intentTarget?.y || 70) };
    const end = to || intentTarget || { x: Math.min(width - 80, start.x + 180), y: start.y };
    const selected = intent.id === state.selectedIntentId;
    const onClick = () => {
      state.selectedIntentId = intent.id;
      state.selectedFactId = null;
      drawGraph(project);
      renderGraphDetail(project);
    };
    const edge = renderedTo === null && intentTarget
      ? openIntentEdgePath(start.x, start.y, end.x, end.y)
      : edgePath(start.x, start.y, end.x, end.y, edgeOffsets.get(intent.id) || 0);
    edgeLayer.appendChild(line(edge.path, statusColor(intent.status), selected, onClick, intent));
    edgeLayer.appendChild(label(edge.labelX, edge.labelY, `#${intent.id}`, selected, onClick, intent));
  }

  for (const fact of facts) {
    const pos = factPositions.get(fact.id);
    nodeLayer.appendChild(node(pos.x, pos.y, `F${fact.id}`, fact.description, fact.id === state.selectedFactId, () => {
      state.selectedFactId = fact.id;
      state.selectedIntentId = null;
      drawGraph(project);
      renderGraphDetail(project);
    }, fact.kind || "evidence"));
  }

  for (const intent of intents) {
    const pos = intentPositions.get(intent.id);
    if (!pos) continue;
    nodeLayer.appendChild(intentNode(pos.x, pos.y, `I${intent.id}`, intent.description || "pending intent", intent.status, intent.id === state.selectedIntentId));
  }
}

function computeEdgeOffsets(intents) {
  const groups = new Map();
  for (const intent of intents) {
    const key = `${intent.from ?? "null"}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(intent);
  }
  const offsets = new Map();
  for (const group of groups.values()) {
    group.sort((left, right) => left.id - right.id);
    const center = (group.length - 1) / 2;
    group.forEach((intent, index) => {
      offsets.set(intent.id, Math.max(-60, Math.min(60, (index - center) * 24)));
    });
  }
  return offsets;
}

function applyGraphZoom() {
  const zoom = state.graphZoom;
  els.graph.style.width = `${Math.max(320, state.graphBaseWidth * zoom)}px`;
  els.graph.style.height = `${Math.max(240, state.graphBaseHeight * zoom)}px`;
  if (els.graphZoomLabel) {
    els.graphZoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  }
}

function setGraphZoom(nextZoom) {
  state.graphZoom = Math.max(0.35, Math.min(1.8, nextZoom));
  if (state.currentProject) drawGraph(state.currentProject);
}

function fitGraphZoom() {
  const viewport = els.graphViewport;
  if (!viewport || !state.graphBaseWidth || !state.graphBaseHeight) return;
  const availableWidth = Math.max(320, viewport.clientWidth - 28);
  const availableHeight = Math.max(240, viewport.clientHeight - 28);
  setGraphZoom(Math.min(1, availableWidth / state.graphBaseWidth, availableHeight / state.graphBaseHeight));
}

function zoomGraphAt(delta, clientX, clientY) {
  const viewport = els.graphViewport;
  if (!viewport) {
    setGraphZoom(state.graphZoom + delta);
    return;
  }
  const beforeZoom = state.graphZoom;
  const rect = viewport.getBoundingClientRect();
  const offsetX = clientX - rect.left + viewport.scrollLeft;
  const offsetY = clientY - rect.top + viewport.scrollTop;
  const nextZoom = Math.max(0.35, Math.min(1.8, beforeZoom + delta));
  if (nextZoom === beforeZoom) return;
  state.graphZoom = nextZoom;
  if (state.currentProject) drawGraph(state.currentProject);
  const ratio = nextZoom / beforeZoom;
  viewport.scrollLeft = offsetX * ratio - (clientX - rect.left);
  viewport.scrollTop = offsetY * ratio - (clientY - rect.top);
}

function layoutGraph(facts, intents) {
  const columns = groupFactsByDepth(facts, intents);
  orderColumnsByParents(columns, intents);
  const graph = {
    marginX: 120,
    marginY: 92,
    factColumnGap: 360,
    intentColumnGap: 245,
    rowGap: 118,
    nodeWidth: 184,
    minHeight: 560,
  };
  const maxOpenIntentCount = Math.max(0, ...Array.from(groupOpenIntents(intents).values(), (items) => items.length));
  const maxColumnFacts = Math.max(1, ...columns.map((columnFacts) => columnFacts.length));
  const width = Math.max(
    1040,
    graph.marginX * 2 + Math.max(0, columns.length - 1) * graph.factColumnGap + graph.intentColumnGap + graph.nodeWidth
  );
  const height = Math.max(
    graph.minHeight,
    graph.marginY * 2 + Math.max(maxColumnFacts, maxOpenIntentCount, 1) * graph.rowGap
  );
  const factDepthById = new Map();
  columns.forEach((columnFacts, columnIndex) => {
    columnFacts.forEach((fact) => factDepthById.set(fact.id, columnIndex));
  });
  const factPositions = layoutFactsLeftToRight(columns, graph, height);
  const intentPositions = layoutOpenIntents(intents, factPositions, factDepthById, columns.length, graph, height);
  return { width, height, factPositions, intentPositions };
}

function groupOpenIntents(intents) {
  const grouped = new Map();
  for (const intent of intents) {
    if (renderedIntentTo(intent) !== null) continue;
    const key = intent.from === null ? "__initial__" : String(intent.from);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(intent);
  }
  return grouped;
}

function renderedIntentTo(intent) {
  return intent.to;
}

function layoutOpenIntents(intents, factPositions, factDepthById, factColumnCount, graph, height) {
  const grouped = groupOpenIntents(intents);
  const positions = new Map();
  for (const [key, group] of grouped.entries()) {
    group.sort((left, right) => left.id - right.id);
    const source = key === "__initial__" ? { x: 70, y: height - 80 } : factPositions.get(Number(key));
    if (!source) continue;
    const sourceDepth = key === "__initial__" ? 0 : (factDepthById.get(Number(key)) ?? 0);
    const isLastFactColumn = sourceDepth >= factColumnCount - 1;
    const x = isLastFactColumn
      ? source.x + graph.intentColumnGap
      : source.x + Math.round(graph.factColumnGap * 0.58);
    const rowGap = 76;
    const totalHeight = (group.length - 1) * rowGap;
    const startY = Math.max(graph.marginY, Math.min(height - totalHeight - graph.marginY, source.y - totalHeight / 2));
    group.forEach((intent, index) => {
      positions.set(intent.id, { x, y: startY + index * rowGap });
    });
  }
  return positions;
}

function layoutFactsLeftToRight(columns, graph, height) {
  const positions = new Map();
  const startX = columns.length > 1 ? graph.marginX : Math.round(graph.marginX + graph.factColumnGap);
  for (const [columnIndex, columnFacts] of columns.entries()) {
    const x = startX + columnIndex * graph.factColumnGap;
    const rowGap = columnFacts.length > 4 ? Math.max(graph.rowGap, 128) : graph.rowGap;
    const totalHeight = (columnFacts.length - 1) * rowGap;
    const startY = Math.max(graph.marginY, Math.round(height / 2 - totalHeight / 2));
    columnFacts.forEach((fact, rowIndex) => {
      positions.set(fact.id, { x, y: startY + rowIndex * rowGap });
    });
  }
  return positions;
}

function orderColumnsByParents(columns, intents) {
  const columnIndexByFact = new Map();
  columns.forEach((columnFacts, columnIndex) => {
    columnFacts.forEach((fact, rowIndex) => {
      columnIndexByFact.set(fact.id, { columnIndex, rowIndex });
    });
  });
  const incomingParents = new Map();
  for (const intent of intents) {
    const to = renderedIntentTo(intent);
    if (intent.from === null || to === null) continue;
    const parent = columnIndexByFact.get(intent.from);
    const child = columnIndexByFact.get(to);
    if (!parent || !child || parent.columnIndex >= child.columnIndex) continue;
    if (!incomingParents.has(to)) incomingParents.set(to, []);
    incomingParents.get(to).push(parent.rowIndex);
  }
  for (let columnIndex = 0; columnIndex < columns.length; columnIndex += 1) {
    columns[columnIndex].sort((left, right) => {
      if (left.id === 0) return -1;
      if (right.id === 0) return 1;
      if (left.id === 1) return -1;
      if (right.id === 1) return 1;
      const leftParents = incomingParents.get(left.id) || [];
      const rightParents = incomingParents.get(right.id) || [];
      const leftScore = leftParents.length ? average(leftParents) : columnIndexByFact.get(left.id)?.rowIndex ?? left.id;
      const rightScore = rightParents.length ? average(rightParents) : columnIndexByFact.get(right.id)?.rowIndex ?? right.id;
      return leftScore - rightScore || left.id - right.id;
    });
    columns[columnIndex].forEach((fact, rowIndex) => {
      columnIndexByFact.set(fact.id, { columnIndex, rowIndex });
    });
  }
}

function average(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function groupFactsByDepth(facts, intents) {
  const byId = new Map(facts.map((fact) => [fact.id, fact]));
  const outgoing = new Map();
  const incomingCount = new Map(facts.map((fact) => [fact.id, 0]));
  for (const intent of intents) {
    const to = renderedIntentTo(intent);
    if (intent.from === null || to === null || !byId.has(intent.from) || !byId.has(to)) {
      continue;
    }
    if (!outgoing.has(intent.from)) outgoing.set(intent.from, []);
    outgoing.get(intent.from).push({ ...intent, renderedTo: to });
    incomingCount.set(to, (incomingCount.get(to) || 0) + 1);
  }
  for (const items of outgoing.values()) {
    items.sort((left, right) => left.id - right.id);
  }

  const depth = new Map();
  const roots = facts
    .filter((fact) => fact.id === 0 || (incomingCount.get(fact.id) || 0) === 0)
    .sort((left, right) => left.id - right.id);
  const queue = roots.map((fact) => {
    depth.set(fact.id, fact.id === 0 ? 0 : 1);
    return fact.id;
  });

  while (queue.length) {
    const currentId = queue.shift();
    const currentDepth = depth.get(currentId) || 0;
    for (const intent of outgoing.get(currentId) || []) {
      const nextDepth = currentDepth + 1;
      if ((depth.get(intent.renderedTo) ?? -1) < nextDepth) {
        depth.set(intent.renderedTo, nextDepth);
        queue.push(intent.renderedTo);
      }
    }
  }

  const fallbackDepth = Math.max(0, ...depth.values()) + 1;
  for (const fact of facts) {
    if (!depth.has(fact.id)) depth.set(fact.id, fallbackDepth);
  }
  if (byId.has(1)) {
    const rightmostDepth = Math.max(0, ...depth.values());
    depth.set(1, rightmostDepth + 1);
  }

  const grouped = new Map();
  for (const fact of facts) {
    const factDepth = depth.get(fact.id) || 0;
    if (!grouped.has(factDepth)) grouped.set(factDepth, []);
    grouped.get(factDepth).push(fact);
  }
  return [...grouped.entries()]
    .sort(([left], [right]) => left - right)
    .map(([, columnFacts]) => columnFacts.sort((left, right) => left.id - right.id));
}

function node(x, y, title, description, selected = false, onClick = null, kind = "evidence") {
  const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
  group.classList.add("graph-node", "fact-node", `fact-${kind || "evidence"}`);
  if (title === "F0" || kind === "start") group.classList.add("start-fact");
  if (title === "F1" || kind === "goal") group.classList.add("goal-fact");
  if (selected) group.classList.add("selected");
  group.setAttribute("tabindex", "0");
  group.setAttribute("role", "button");
  group.setAttribute("aria-label", `${title} ${description || ""}`);
  const rect = svgElement("rect", {
    x: x - 92,
    y: y - 42,
    width: 184,
    height: 84,
    rx: 12,
  });
  group.appendChild(rect);
  group.appendChild(svgText(x - 76, y - 15, title, { class: "node-title" }));
  appendWrappedSvgText(group, x - 76, y + 8, description || "", 21, 2);
  if (onClick) {
    group.addEventListener("click", onClick);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onClick();
      }
    });
  }
  return group;
}

function intentNode(x, y, title, description, status, selected = false, onClick = null) {
  const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
  group.classList.add("graph-node", "intent-node", `intent-${status}`);
  if (selected) group.classList.add("selected");
  group.setAttribute("tabindex", "0");
  group.setAttribute("role", "button");
  group.setAttribute("aria-label", `${title} ${status} ${description || ""}`);
  group.appendChild(svgElement("rect", {
    x: x - 82,
    y: y - 23,
    width: 164,
    height: 46,
    rx: 23,
  }));
  group.appendChild(svgText(x - 66, y - 3, title, { class: "node-title" }));
  appendWrappedSvgText(group, x - 18, y - 3, description || status, 14, 1);
  if (onClick) {
    group.addEventListener("click", onClick);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onClick();
      }
    });
  }
  return group;
}

function appendWrappedSvgText(group, x, y, text, maxCharsPerLine, maxLines) {
  const lines = wrapText(text, maxCharsPerLine, maxLines);
  for (const [index, lineText] of lines.entries()) {
    group.appendChild(svgText(x, y + index * 16, lineText, { class: "node-description" }));
  }
}

function wrapText(value, maxCharsPerLine, maxLines) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return [""];
  const lines = [];
  let remaining = text;
  while (remaining && lines.length < maxLines) {
    const limit = maxCharsPerLine;
    let slice = remaining.slice(0, limit);
    if (remaining.length > limit) {
      const lastSpace = slice.lastIndexOf(" ");
      if (lastSpace > 4) slice = slice.slice(0, lastSpace);
    }
    remaining = remaining.slice(slice.length).trimStart();
    if (remaining && lines.length === maxLines - 1) {
      slice = `${slice.slice(0, Math.max(0, limit - 1))}...`;
      remaining = "";
    }
    lines.push(slice);
  }
  return lines;
}

function svgElement(name, attrs) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) {
    element.setAttribute(key, String(value));
  }
  return element;
}

function svgText(x, y, text, attrs = {}) {
  const node = svgElement("text", { x, y, ...attrs });
  node.textContent = text;
  return node;
}

function edgePath(x1, y1, x2, y2, offset = 0) {
  const nodeHalfWidth = 92;
  const sourceGap = 14;
  const targetGap = 14;
  const anchorOffset = Math.max(-34, Math.min(34, offset * 0.55));
  const startX = x1 + nodeHalfWidth + sourceGap;
  const startY = y1 + anchorOffset;
  const endX = x2 - nodeHalfWidth - targetGap;
  const endY = y2 - anchorOffset;
  const dx = endX - startX;
  const dy = endY - startY;
  const distance = Math.max(100, Math.abs(dx));
  const bend = Math.min(260, Math.max(100, distance * 0.52));
  const direction = dx >= 0 ? 1 : -1;
  const curveOffset = Math.max(-72, Math.min(72, offset));
  const verticalPull = Math.max(-80, Math.min(80, dy * 0.22));
  const c1x = startX + bend * direction;
  const c2x = endX - bend * direction;
  const c1y = startY + verticalPull + curveOffset;
  const c2y = endY - verticalPull - curveOffset;
  const path = `M ${startX} ${startY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${endX} ${endY}`;
  const mid = cubicPoint(
    { x: startX, y: startY },
    { x: c1x, y: c1y },
    { x: c2x, y: c2y },
    { x: endX, y: endY },
    0.5,
  );
  return {
    path,
    labelX: mid.x,
    labelY: mid.y - 12,
  };
}

function openIntentEdgePath(x1, y1, x2, y2) {
  const sourceHalfWidth = 92;
  const targetHalfWidth = 82;
  const startX = x1 + sourceHalfWidth + 14;
  const startY = y1;
  const endX = x2 - targetHalfWidth - 12;
  const endY = y2;
  const dx = endX - startX;
  const dy = endY - startY;
  const distance = Math.max(90, Math.abs(dx));
  const bend = Math.min(170, Math.max(75, distance * 0.58));
  const direction = dx >= 0 ? 1 : -1;
  const verticalPull = Math.max(-70, Math.min(70, dy * 0.2));
  const c1x = startX + bend * direction;
  const c2x = endX - bend * direction;
  const c1y = startY + verticalPull;
  const c2y = endY - verticalPull;
  const path = `M ${startX} ${startY} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${endX} ${endY}`;
  const mid = cubicPoint(
    { x: startX, y: startY },
    { x: c1x, y: c1y },
    { x: c2x, y: c2y },
    { x: endX, y: endY },
    0.5,
  );
  return {
    path,
    labelX: mid.x,
    labelY: mid.y - 12,
  };
}

function cubicPoint(p0, p1, p2, p3, t) {
  const inv = 1 - t;
  return {
    x: inv ** 3 * p0.x + 3 * inv ** 2 * t * p1.x + 3 * inv * t ** 2 * p2.x + t ** 3 * p3.x,
    y: inv ** 3 * p0.y + 3 * inv ** 2 * t * p1.y + 3 * inv * t ** 2 * p2.y + t ** 3 * p3.y,
  };
}

function line(pathData, color, selected = false, onClick = null, intent = null) {
  const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
  group.classList.add("graph-edge-group");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.classList.add("graph-edge");
  if (selected) path.classList.add("selected");
  path.setAttribute("d", pathData);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", selected ? "4" : "2.2");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("marker-end", `url(#arrow-${colorName(color)})`);
  const hitPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  hitPath.classList.add("graph-edge-hit");
  hitPath.setAttribute("d", pathData);
  hitPath.setAttribute("fill", "none");
  hitPath.setAttribute("stroke", "transparent");
  hitPath.setAttribute("stroke-width", "16");
  hitPath.setAttribute("tabindex", "0");
  hitPath.setAttribute("role", "button");
  hitPath.setAttribute("aria-label", intent ? `Intent ${intent.id} ${intent.status} ${intent.description || ""}` : "Intent");
  if (onClick) {
    hitPath.addEventListener("click", onClick);
    hitPath.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onClick();
      }
    });
  }
  group.appendChild(path);
  group.appendChild(hitPath);
  return group;
}

function label(x, y, text, selected = false, onClick = null, intent = null) {
  const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
  group.classList.add("edge-label");
  if (selected) group.classList.add("selected");
  group.setAttribute("tabindex", "0");
  group.setAttribute("role", "button");
  group.setAttribute("aria-label", intent ? `Intent ${intent.id} ${intent.status} ${intent.description || ""}` : text);
  const labelText = svgText(x, y + 4, text, { "text-anchor": "middle" });
  const width = Math.max(28, text.length * 7 + 12);
  group.appendChild(svgElement("rect", {
    x: x - width / 2,
    y: y - 11,
    width,
    height: 20,
    rx: 10,
  }));
  group.appendChild(labelText);
  if (onClick) {
    group.addEventListener("click", onClick);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onClick();
      }
    });
  }
  return group;
}

function statusColor(status) {
  if (status === "completed") return "#137333";
  if (status === "running") return "#8a5a00";
  if (status === "failed") return "#b42318";
  return "#006d77";
}

function colorName(color) {
  if (color === "#137333") return "completed";
  if (color === "#8a5a00") return "running";
  if (color === "#b42318") return "failed";
  return "active";
}

function graphDefs() {
  const defs = svgElement("defs", {});
  for (const [name, color] of [
    ["active", "#006d77"],
    ["completed", "#137333"],
    ["running", "#8a5a00"],
    ["failed", "#b42318"],
  ]) {
    const marker = svgElement("marker", {
      id: `arrow-${name}`,
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 5,
      markerHeight: 5,
      orient: "auto-start-reverse",
    });
    marker.appendChild(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: color }));
    defs.appendChild(marker);
  }
  return defs;
}

function graphBackground(width, height) {
  const group = svgElement("g", { class: "graph-background" });
  group.appendChild(svgElement("rect", { x: 0, y: 0, width, height, rx: 0 }));
  const step = 48;
  for (let x = step; x < width; x += step) {
    group.appendChild(svgElement("line", { x1: x, y1: 0, x2: x, y2: height }));
  }
  for (let y = step; y < height; y += step) {
    group.appendChild(svgElement("line", { x1: 0, y1: y, x2: width, y2: y }));
  }
  return group;
}

function renderGraphDetail(project) {
  const detail = els.graphDetail;
  if (!detail) return;
  const intent = project.intents.find((item) => item.id === state.selectedIntentId);
  if (intent) {
    renderIntentDetail(project, intent, detail);
    return;
  }
  const fact = project.facts.find((item) => item.id === state.selectedFactId);
  if (!fact) {
    detail.className = "graph-detail empty-detail";
    detail.textContent = project.facts.length || project.intents.length ? "点击 fact 节点或 intent 边查看详情" : "暂无图数据";
    return;
  }
  const incoming = project.intents.filter((intent) => intent.to === fact.id);
  const outgoing = project.intents.filter((intent) => intent.from === fact.id);
  detail.className = "graph-detail";
  detail.innerHTML = `
    <div class="graph-detail-header">
      <strong>Fact #${escapeHtml(fact.id)}</strong>
      <span class="badge ${escapeHtml(fact.kind || "evidence")}">${escapeHtml(fact.kind || "evidence")}</span>
      <button type="button" class="ghost-button" id="clear-graph-selection">关闭</button>
    </div>
    <p>${escapeHtml(fact.description)}</p>
    <div class="graph-links">
      ${renderIntentSummary("来源 intent", incoming)}
      ${renderIntentSummary("后续 intent", outgoing)}
    </div>
  `;
  bindClearGraphSelection(project, detail);
}

function renderIntentDetail(project, intent, detail) {
  const fromFact = project.facts.find((fact) => fact.id === intent.from);
  const toFact = project.facts.find((fact) => fact.id === intent.to);
  detail.className = "graph-detail";
  detail.innerHTML = `
    <div class="graph-detail-header">
      <strong>Intent #${escapeHtml(intent.id)}</strong>
      <span class="badge ${escapeHtml(intent.status)}">${escapeHtml(intent.status)}</span>
      <span class="badge ${escapeHtml(intent.outcome || "pending")}">${escapeHtml(intent.outcome || "pending")}</span>
      ${intent.action_key ? `<span class="badge">${escapeHtml(intent.action_key)}</span>` : ""}
      <button type="button" class="ghost-button" id="clear-graph-selection">关闭</button>
    </div>
    <p>${escapeHtml(intent.description || "无描述")}</p>
    ${intent.note ? `<p class="intent-note">${escapeHtml(intent.note)}</p>` : ""}
    <div class="graph-links">
      ${renderFactEndpoint("From", intent.from, fromFact)}
      ${renderFactEndpoint("To", intent.to, toFact)}
    </div>
  `;
  bindClearGraphSelection(project, detail);
}

function renderFactEndpoint(title, factId, fact) {
  const text = fact ? trimText(fact.description, 100) : "无";
  const idText = factId === null ? "null" : `F${factId}`;
  return `
    <div>
      <span>${escapeHtml(title)}</span>
      <em>${escapeHtml(idText)} · ${escapeHtml(text)}</em>
    </div>
  `;
}

function bindClearGraphSelection(project, detail) {
  detail.querySelector("#clear-graph-selection")?.addEventListener("click", () => {
    state.selectedFactId = null;
    state.selectedIntentId = null;
    drawGraph(project);
    renderGraphDetail(project);
  });
}

function renderIntentSummary(title, intents) {
  if (!intents.length) {
    return `<div><span>${escapeHtml(title)}</span><em>无</em></div>`;
  }
  const items = intents.map((intent) => {
    const description = trimText(intent.description || "无描述", 80);
    const outcome = intent.outcome ? ` · ${intent.outcome}` : "";
    return `<li>#${escapeHtml(intent.id)} · ${escapeHtml(intent.status)}${escapeHtml(outcome)} · ${escapeHtml(description)}</li>`;
  }).join("");
  return `<div><span>${escapeHtml(title)}</span><ul>${items}</ul></div>`;
}

function scheduleRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    if (state.currentProjectId && !state.busy) refreshCurrent().catch(() => {});
  }, 5000);
}

function clearCurrentProject() {
  state.currentProjectId = null;
  state.currentProject = null;
  state.runStatus = null;
  state.selectedFactId = null;
  state.selectedIntentId = null;
  els.title.textContent = "Select a project";
  els.subtitle.textContent = "View task inputs, fact/intent graph, and runtime logs";
  [...els.summary.querySelectorAll("strong")].forEach((node) => {
    node.textContent = "-";
  });
  els.graph.innerHTML = "";
  els.cryptoInputs.hidden = true;
  els.cryptoInputList.innerHTML = "";
  els.cryptoCandidates.hidden = true;
  els.cryptoCandidateList.innerHTML = "";
  if (els.graphDetail) {
    els.graphDetail.className = "graph-detail empty-detail";
    els.graphDetail.textContent = "点击 fact 节点或 intent 边查看详情";
  }
  els.logs.innerHTML = "";
  updateActionButtons();
}

function setStatus(message) {
  els.status.textContent = message;
}

async function withAction(message, action) {
  state.busy = true;
  setStatus(message);
  updateActionButtons();
  const logTimer = state.currentProjectId
    ? setInterval(() => {
        loadLogs().catch(() => {});
      }, 1000)
    : null;
  try {
    const result = await action();
    setStatus(`done · ${new Date().toLocaleTimeString()}`);
    return result;
  } catch (error) {
    setStatus(`error · ${error.message}`);
    await loadLogs().catch(() => {});
    throw error;
  } finally {
    if (logTimer) clearInterval(logTimer);
    await loadLogs().catch(() => {});
    state.busy = false;
    updateActionButtons();
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function escapeSvg(value) {
  return escapeHtml(value);
}

function trimText(value, length) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length - 1)}...` : text;
}

function updateCreateTaskType() {
  const crypto = els.taskType.value === "crypto";
  els.targetUrlField.hidden = crypto;
  els.cryptoFilesField.hidden = !crypto;
  els.targetUrlField.querySelector("input").required = !crypto;
}

els.taskType.addEventListener("change", updateCreateTaskType);
updateCreateTaskType();

async function uploadBrowserFile(projectId, file, role, data) {
  if (file.size > 32 * 1024 * 1024) throw new Error("单个输入文件不能超过 32 MiB");
  const query = new URLSearchParams({ filename: file.name, role });
  for (const key of ["number_radix", "byte_order", "byte_length"]) {
    if (data.get(key) !== "") query.set(key, data.get(key));
  }
  const reference = await api(`/projects/${projectId}/inputs?${query}`, {
    method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" },
  });
  if (reference.size_bytes !== file.size) throw new Error(`上传大小不匹配：${file.name}`);
  return reference;
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await withAction("creating project...", async () => {
    const data = new FormData(els.form);
    const crypto = data.get("task_type") === "crypto";
    const files = crypto ? [...els.form.querySelector('input[name="input_files"]').files] : [];
    if (files.some((file) => file.size > 32 * 1024 * 1024)) {
      throw new Error("单个输入文件不能超过 32 MiB");
    }
    const payload = {
      target_url: crypto ? null : data.get("target_url"),
      hint: data.get("hint") || "",
      objective: data.get("objective"),
      task_type: crypto ? "crypto" : "general",
    };
    if (crypto) {
      const goal = { answer_kind: data.get("answer_kind"), solution_policy: data.get("solution_policy") };
      if (data.get("answer_encoding")) goal.answer_encoding = data.get("answer_encoding");
      if (data.get("answer_byte_length")) goal.byte_length = Number(data.get("answer_byte_length"));
      if (data.get("answer_byte_order")) goal.byte_order = data.get("answer_byte_order");
      if (data.get("answer_form")) {
        goal.answer_form = data.get("answer_form");
        if (goal.answer_form === "hash") {
          if (!data.get("answer_hash")) throw new Error("答案形态为哈希时必须选择哈希算法");
          goal.answer_hash = data.get("answer_hash");
          // 摘要本身就是答案文本；契约只接受 utf8，避免与明文编码混淆
          goal.answer_encoding = "utf8";
        }
      }
      payload.crypto = { schema_version: 1, inputs: [], goal };
    }
    const project = await api("/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await refreshProjectsOnly();
    await selectProject(project.id);
    for (const file of files) {
      await uploadBrowserFile(project.id, file, data.get("input_role") || "input", data);
    }
    await refreshCurrent();
    els.form.reset();
    updateCreateTaskType();
  });
});

els.uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.currentProjectId || state.currentProject?.task_type !== "crypto") return;
  await withAction("uploading crypto input...", async () => {
    const data = new FormData(els.uploadForm);
    await uploadBrowserFile(state.currentProjectId, data.get("file"), data.get("role") || "input", data);
    els.uploadForm.reset();
    await refreshCurrent();
  });
});

els.refreshProjects.addEventListener("click", () => loadProjects().catch((error) => setStatus(`error · ${error.message}`)));
els.refreshCurrent.addEventListener("click", () => refreshCurrent().catch((error) => setStatus(`error · ${error.message}`)));
els.refreshLogs.addEventListener("click", () => loadLogs().catch((error) => setStatus(`error · ${error.message}`)));
els.graphZoomOut?.addEventListener("click", () => setGraphZoom(state.graphZoom - 0.1));
els.graphZoomIn?.addEventListener("click", () => setGraphZoom(state.graphZoom + 0.1));
els.graphZoomFit?.addEventListener("click", () => fitGraphZoom());
els.graphZoomReset?.addEventListener("click", () => setGraphZoom(1));
els.graphViewport?.addEventListener("wheel", (event) => {
  if (!event.ctrlKey) return;
  event.preventDefault();
  zoomGraphAt(event.deltaY > 0 ? -0.08 : 0.08, event.clientX, event.clientY);
}, { passive: false });
els.clearLogs.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  const confirmed = window.confirm("Clear all logs for this project?");
  if (!confirmed) return;
  await withAction("clearing logs...", async () => {
    await api(`/projects/${state.currentProjectId}/logs`, {
      method: "DELETE",
    });
    await loadLogs();
  });
});

els.activateProject.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  await withAction("activating project and preparing Docker container...", async () => {
    await api(`/projects/${state.currentProjectId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status: "active" }),
    });
    await refreshCurrent();
  });
});

els.runProject.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  await withAction("starting dispatcher...", async () => {
    await api(`/projects/${state.currentProjectId}/run`, {
      method: "POST",
      body: JSON.stringify({
        interval_seconds: 3,
        max_steps: null,
        max_workers: 4,
      }),
    });
    await refreshCurrent();
  });
});

els.stopProject.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  await withAction("stopping dispatcher...", async () => {
    await api(`/projects/${state.currentProjectId}/run`, {
      method: "DELETE",
    });
    await refreshCurrent();
  });
});

els.deleteProject.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  const projectId = state.currentProjectId;
  const confirmed = window.confirm(`Delete project #${projectId}? This removes its graph and logs.`);
  if (!confirmed) return;
  await withAction("deleting project...", async () => {
    await api(`/projects/${projectId}`, {
      method: "DELETE",
    });
    clearCurrentProject();
    await loadProjects();
  });
});

loadProjects();
