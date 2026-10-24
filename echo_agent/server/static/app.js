const state = {
  projects: [],
  runs: [],
  currentProjectId: null,
  currentProject: null,
  runStatus: null,
  refreshTimer: null,
  selectedFactId: null,
  selectedIntentId: null,
  graphZoom: 1,
  userGraphZoom: false,
  graphBaseWidth: 900,
  graphBaseHeight: 520,
  busy: false,
  editingProjectId: null,
  editAttachments: [],
  logs: [],
  logProjectId: null,
  logAfterId: 0,
  logFollow: true,
  logRequest: 0,
  logTimer: null,
  showDetailLogs: false,
};

const els = {
  layout: document.querySelector(".layout"),
  status: document.querySelector("#connection-status"),
  projectList: document.querySelector("#project-list"),
  form: document.querySelector("#create-project-form"),
  refreshLogs: document.querySelector("#refresh-logs"),
  clearLogs: document.querySelector("#clear-logs"),
  editProject: document.querySelector("#edit-project"),
  runProject: document.querySelector("#run-project"),
  stopProject: document.querySelector("#stop-project"),
  deleteProject: document.querySelector("#delete-project"),
  title: document.querySelector("#project-title"),
  subtitle: document.querySelector("#project-subtitle"),
  summary: document.querySelector("#summary-row"),
  graphViewport: document.querySelector("#graph-viewport"),
  graph: document.querySelector("#project-graph"),
  graphDetail: document.querySelector("#graph-detail"),
  logsPanel: document.querySelector(".logs-panel"),
  logs: document.querySelector("#log-list"),
  runStatusBar: document.querySelector("#run-status-bar"),
  showDetailLogs: document.querySelector("#show-detail-logs"),
  stopAll: document.querySelector("#stop-all"),
  openCreate: document.querySelector("#open-create"),
  closeCreate: document.querySelector("#close-create"),
  cancelCreate: document.querySelector("#cancel-create"),
  createDialog: document.querySelector("#create-dialog"),
  createDialogTitle: document.querySelector("#create-dialog-title"),
  attachmentNote: document.querySelector("#attachment-note"),
  createError: document.querySelector("#create-error"),
  submitCreate: document.querySelector("#submit-create"),
  openSettings: document.querySelector("#open-settings"),
  closeSettings: document.querySelector("#close-settings"),
  settingsDialog: document.querySelector("#settings-dialog"),
  settingsForm: document.querySelector("#settings-form"),
  apiKeyHint: document.querySelector("#api-key-hint"),
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
    state.runs = await api("/projects/runs");
    setStatus(`online · ${new Date().toLocaleTimeString()}`);
    renderProjects();
    if (!state.currentProjectId && state.projects.length) {
      await selectProject(state.projects[0].id);
    }
  } catch (error) {
    setStatus(`error · ${error.message}`);
  }
}

function runFor(projectId) {
  return state.runs.find((item) => item.project_id === projectId) || null;
}

function projectBadge(project) {
  const run = runFor(project.id);
  if (run?.queued) return "queued";
  if (run?.running) return "running";
  return project.status;
}

function statusLabel(status) {
  return {
    inactive: "未启动",
    active: "已激活",
    running: "运行中",
    queued: "排队中",
    stoped: "已停止",
    completed: "已完成",
    failed: "失败",
  }[status] || status;
}

function describeTarget(target) {
  const attachmentPrefix = "/home/kali/attachments/";
  if (target.startsWith(attachmentPrefix)) return `附件 · ${target.slice(attachmentPrefix.length)}`;
  if (/^https?:\/\//i.test(target)) return `URL · ${target}`;
  if (!/[\s/]/.test(target)) return `IP · ${target}`;
  return target;
}

function targetSummary(project) {
  const lines = String(project.target_url || "").split(/\n/).map((item) => item.trim()).filter(Boolean);
  if (!lines.length) return "未设置目标";
  return lines.map(describeTarget).join(" · ");
}

function renderProjects() {
  els.projectList.innerHTML = "";
  if (!state.projects.length) {
    els.projectList.innerHTML = '<div class="empty">还没有项目</div>';
    return;
  }
  for (const project of state.projects) {
    const item = document.createElement("div");
    const badge = projectBadge(project);
    const running = badge === "running";
    item.className = `project-item ${project.id === state.currentProjectId ? "active" : ""}`;
    item.innerHTML = `
      <button type="button" class="project-select">
        <span class="project-item-top">
          <strong>${escapeHtml(project.name)}</strong>
          <span class="badge ${escapeHtml(badge)}">${escapeHtml(statusLabel(badge))}</span>
        </span>
        <span class="project-item-meta">#${project.id} · ${escapeHtml(targetSummary(project))}</span>
      </button>
      <button type="button" class="project-delete" ${running || state.busy ? "disabled" : ""} title="${running ? "运行中，请先停止" : "删除项目"}">删除</button>
    `;
    item.querySelector(".project-select").addEventListener("click", () => selectProject(project.id));
    item.querySelector(".project-delete").addEventListener("click", () => deleteProjectById(project.id));
    els.projectList.appendChild(item);
  }
}

async function deleteProjectById(projectId) {
  if (state.busy) return;
  const project = state.projects.find((item) => item.id === projectId);
  if (project && projectBadge(project) === "running") {
    setStatus("error · 项目正在运行，请先停止后再删除");
    return;
  }
  const name = project?.name || `#${projectId}`;
  const confirmed = window.confirm(`删除项目「${name}」？图和日志会一起删除。`);
  if (!confirmed) return;
  await withAction("deleting project...", async () => {
    await api(`/projects/${projectId}`, { method: "DELETE" });
    if (state.currentProjectId === projectId) clearCurrentProject();
    await loadProjects();
  });
}

async function selectProject(projectId) {
  if (state.currentProjectId !== projectId) {
    state.userGraphZoom = false;
    state.logFollow = true;
    if (els.graphViewport) {
      els.graphViewport.scrollLeft = 0;
      els.graphViewport.scrollTop = 0;
    }
  }
  state.currentProjectId = projectId;
  await refreshCurrent();
  scheduleRefresh();
}

async function refreshCurrent() {
  if (!state.currentProjectId) return;
  state.currentProject = await api(`/projects/${state.currentProjectId}`);
  state.runStatus = await api(`/projects/${state.currentProjectId}/run`);
  state.runs = await api("/projects/runs");
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
  els.subtitle.textContent = `#${project.id} · ${project.objective || "未填写目的"}`;
  const values = [
    project.status,
    targetSummary(state.currentProject || project),
    `${project.facts.length} facts · ${project.intents.length} intents`,
    state.runStatus?.queued ? "queued" : state.runStatus?.running ? "running" : "idle",
  ];
  [...els.summary.querySelectorAll("strong")].forEach((node, index) => {
    node.textContent = values[index];
  });
  updateActionButtons();
  drawGraph(project);
  renderGraphDetail(project);
}

function updateActionButtons() {
  const project = state.currentProject;
  const hasProject = Boolean(project);
  const status = project?.status;
  const dispatcherActive = Boolean(state.runStatus?.queued) || Boolean(state.runStatus?.running);
  const canRun = !dispatcherActive && (status === "inactive" || status === "active" || status === "stoped");
  const canStop = dispatcherActive || status === "running";
  els.refreshLogs.disabled = !hasProject || state.busy;
  els.clearLogs.disabled = !hasProject || state.busy;
  els.editProject.disabled = !hasProject || state.busy || canStop;
  els.runProject.disabled = !hasProject || state.busy || !canRun;
  els.stopProject.disabled = !hasProject || state.busy || !canStop;
  els.deleteProject.disabled = !hasProject || state.busy || canStop;
}

const MODE_LABELS = {
  first_run: "首次执行",
  "first-run": "首次执行",
  explore: "探索",
  reason: "推理",
  completed: "已完成",
  skipped: "已跳过",
};

function modeLabel(mode) {
  return MODE_LABELS[mode] || mode || "";
}

function logMessage(log) {
  return String(log?.message || "");
}

function classifyLog(log) {
  const message = logMessage(log);
  if (message.startsWith("codex agent still running")) return "heartbeat";
  if (message.startsWith("codex output:") || message.startsWith("codex result:")) return "output";
  if (log.level === "error" || log.level === "warning" || log.level === "success") return "phase";
  if (
    message.startsWith("docker ")
    || message.startsWith("project status transition:")
    || message.startsWith("project container lifecycle")
    || message.startsWith("codex preflight")
    || message.startsWith("codex prompt written")
    || message.startsWith("codex runtime prepared")
    || message.startsWith("shared resource")
  ) return "process";
  return "phase";
}

function intentSuffix(metadata) {
  return metadata?.intent_id ? ` · intent #${metadata.intent_id}` : "";
}

function metadataIdList(metadata, key) {
  const values = metadata?.[key];
  if (!Array.isArray(values)) return [];
  return values.map((value) => Number(value)).filter((value) => Number.isInteger(value));
}

function factLabels(metadata) {
  return metadataIdList(metadata, "fact_ids").map((id) => `F${id}`).join("、");
}

function startedIntentText(metadata, fallbackCount) {
  const labels = metadataIdList(metadata, "intent_ids").map((id) => `#${id}`);
  if (!labels.length && (metadata?.intent_id === 0 || metadata?.intent_id)) labels.push(`#${metadata.intent_id}`);
  if (labels.length) return `启动 intent ${labels.join("、")}`;
  return fallbackCount ? `启动 ${fallbackCount} 个 intent` : "启动 intent";
}

function intentNodeText(intentId, metadata, retry) {
  const facts = factLabels(metadata);
  const head = `intent #${intentId}`;
  if (facts) return retry ? `${head} 将重试，得到 ${facts}` : `${head} 得到 ${facts}`;
  if (retry || metadata?.retryable) return `${head} 将重试`;
  if (!Array.isArray(metadata?.fact_ids) && (metadata?.has_fact || metadata?.has_answer)) return `${head} 得到新节点`;
  return `${head} 未得到新节点`;
}

function finalResultText(message, metadata) {
  if (message === "reason marked project completed") {
    const answer = String(metadata?.answer || "").replace(/\s+/g, " ").trim();
    return answer ? `项目完成 · ${answer.slice(0, 180)}` : "项目完成";
  }
  if (message === "reason made no progress; dispatcher will stop") return "调度停止：推理没有进展";
  if (message === "reason kept timing out; dispatcher will stop") return "调度停止：推理反复超时";
  if (message.startsWith("target appears offline")) return "调度停止：目标多次不可达";
  if (message === "server dispatcher stopped") return "调度结束";
  return "";
}

function isScheduleLog(log) {
  const message = logMessage(log);
  if (/^project entered (explore|reason|first_run) mode$/.test(message)) return true;
  if (message === "first-run initial intent started") return true;
  if (/^explore started with \d+ active intents$/.test(message)) return true;
  if (/^explore intent \d+ (finished|released for retry)$/.test(message)) return true;
  if (message === "first-run finished") return true;
  return Boolean(finalResultText(message, log.metadata || {}));
}

function phaseSummary(log) {
  const message = logMessage(log);
  const metadata = log.metadata || {};
  const finalResult = finalResultText(message, metadata);
  if (finalResult) return finalResult;
  const step = message.match(/^server dispatcher step (\d+) finished$/);
  if (step) {
    const mode = modeLabel(metadata.mode) || "步骤";
    return metadata.message ? `${mode}完成：${metadata.message}` : `${mode}完成`;
  }
  if (message === "server dispatcher started") return "调度已启动";
  if (message === "project queued until a concurrent run slot is free") return "已进入排队，等待并发名额";
  const entered = message.match(/^project entered (.+) mode$/);
  if (entered) return `进入${modeLabel(entered[1]) || entered[1]}`;
  if (message === "first-run initial intent started") return startedIntentText(metadata);
  if (message === "first-run finished") return intentNodeText(metadata.intent_id ?? "?", metadata, Boolean(metadata.retryable));
  const exploreStart = message.match(/^explore started with (\d+) active intents$/);
  if (exploreStart) return startedIntentText(metadata, exploreStart[1]);
  const exploreIntent = message.match(/^explore intent (\d+) (finished|released for retry)$/);
  if (exploreIntent) return intentNodeText(exploreIntent[1], metadata, exploreIntent[2] !== "finished");
  const exploreDone = message.match(/^explore finished (\d+) intents, released (\d+)$/);
  if (exploreDone) return `探索结束：完成 ${exploreDone[1]}，重试 ${exploreDone[2]}`;
  if (message === "reason started") return "开始推理";
  const codexStart = message.match(/^codex agent process starting in (.+) mode$/);
  if (codexStart) return `Codex 开始${modeLabel(codexStart[1]) || codexStart[1]}${intentSuffix(metadata)}`;
  const codexEnd = message.match(/^codex agent process finished in (.+) mode$/);
  if (codexEnd) return `Codex 结束${modeLabel(codexEnd[1]) || codexEnd[1]}`;
  if (message.startsWith("codex agent no-output timed out")) return `Codex 长时间没有输出${intentSuffix(metadata)}`;
  const failed = message.match(/^codex agent failed in (.+) mode with exit code (\d+)/);
  if (failed) return `Codex ${modeLabel(failed[1]) || failed[1]}失败，退出码 ${failed[2]}`;
  return message;
}

function outputParts(log) {
  const message = logMessage(log);
  const stdout = message.startsWith("codex result:");
  return {
    kind: stdout ? "标准输出" : "进度输出",
    body: message.replace(/^codex (?:result|output):\n?/, ""),
  };
}

function latestOutputLog() {
  return latestLog((item) => classifyLog(item) === "output");
}

function latestHeartbeat() {
  return latestLog((item) => classifyLog(item) === "heartbeat");
}

function parseLogTime(value) {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? null : parsed;
}

function formatClock(value) {
  const parsed = parseLogTime(value);
  if (parsed === null) return value || "";
  return new Date(parsed).toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

function latestLog(predicate) {
  for (let index = state.logs.length - 1; index >= 0; index -= 1) {
    if (predicate(state.logs[index])) return state.logs[index];
  }
  return null;
}

function currentMode() {
  const log = latestLog((item) => {
    const message = logMessage(item);
    return Boolean(item.metadata?.mode)
      || /^project entered .+ mode$/.test(message)
      || /^codex agent process starting in .+ mode$/.test(message)
      || message.startsWith("explore started")
      || message === "reason started"
      || message === "first-run initial intent started";
  });
  if (!log) return "";
  if (log.metadata?.mode) return modeLabel(log.metadata.mode);
  const message = logMessage(log);
  const entered = message.match(/^project entered (.+) mode$/);
  if (entered) return modeLabel(entered[1]);
  const starting = message.match(/^codex agent process starting in (.+) mode$/);
  if (starting) return modeLabel(starting[1]);
  if (message.startsWith("explore started")) return "探索";
  if (message === "reason started") return "推理";
  if (message === "first-run initial intent started") return "首次执行";
  return "";
}

function currentIntent() {
  const log = latestLog((item) => {
    if (item.metadata?.intent_id) return true;
    return /^explore intent \d+ /.test(logMessage(item));
  });
  if (!log) return "";
  if (log.metadata?.intent_id) return `#${log.metadata.intent_id}`;
  const matched = logMessage(log).match(/^explore intent (\d+) /);
  return matched ? `#${matched[1]}` : "";
}

function runAnchorTime() {
  const started = latestLog((item) => logMessage(item) === "server dispatcher started")
    || latestLog((item) => logMessage(item).startsWith("codex agent process starting"));
  return started ? parseLogTime(started.created_at) : null;
}

function renderRunStatus() {
  const bar = els.runStatusBar;
  if (!bar) return;
  if (!state.currentProjectId) {
    bar.textContent = "未选择项目";
    bar.className = "run-status-bar";
    bar.removeAttribute("title");
    return;
  }
  const run = state.runStatus;
  bar.title = run?.last_message || "";
  if (run?.running) {
    const parts = ["运行中"];
    const mode = currentMode();
    const intent = currentIntent();
    if (mode) parts.push(mode);
    if (intent) parts.push(`intent ${intent}`);
    const startedAt = runAnchorTime();
    if (startedAt !== null) parts.push(`已运行 ${formatDuration(Date.now() - startedAt)}`);
    const heartbeat = latestHeartbeat();
    const outputLines = Number(heartbeat?.metadata?.output_lines);
    if (Number.isFinite(outputLines)) parts.push(`已输出 ${outputLines} 行`);
    const idleSeconds = idleSecondsNow();
    let silent = false;
    if (idleSeconds === null) {
      parts.push(startedAt !== null && Date.now() - startedAt >= 60000 ? "空闲超过 60 秒" : "等待输出");
      silent = startedAt !== null && Date.now() - startedAt >= 60000;
    } else if (idleSeconds >= 60) {
      parts.push(`空闲 ${formatDuration(idleSeconds * 1000)}`);
      silent = true;
    } else if (idleSeconds >= 1) {
      parts.push(`空闲 ${formatDuration(idleSeconds * 1000)}`);
    } else {
      parts.push("刚刚有输出");
    }
    bar.textContent = parts.join(" · ");
    bar.className = silent ? "run-status-bar is-silent" : "run-status-bar is-running";
    return;
  }
  if (run?.queued) {
    const position = String(run.last_message || "").match(/queued at position (\d+)/);
    bar.textContent = position ? `排队中 · 第 ${position[1]} 位` : "排队中";
    bar.className = "run-status-bar is-queued";
    return;
  }
  bar.textContent = "空闲";
  bar.className = "run-status-bar is-idle";
}

function idleSecondsNow() {
  const output = latestOutputLog();
  const outputAt = output ? parseLogTime(output.created_at) : null;
  if (outputAt !== null) return (Date.now() - outputAt) / 1000;
  const heartbeat = latestHeartbeat();
  if (!heartbeat || !logMessage(heartbeat).includes("no new output")) return null;
  const idle = Number(heartbeat.metadata?.idle_seconds);
  const at = parseLogTime(heartbeat.created_at);
  if (!Number.isFinite(idle) || at === null) return null;
  return idle + (Date.now() - at) / 1000;
}

function shouldShowLog(log) {
  if (state.showDetailLogs) return classifyLog(log) === "output";
  return isScheduleLog(log);
}

function mainLogEmptyText() {
  return state.showDetailLogs ? "暂无详情日志" : "暂无调度日志";
}

function renderLogNode(log) {
  const kind = classifyLog(log);
  const item = document.createElement("div");
  item.className = `log-entry ${log.level} log-${kind}`;
  item.dataset.logId = String(log.id);
  const time = document.createElement("time");
  const kindLabel = kind === "output" ? outputParts(log).kind : kind === "process" ? "过程" : "阶段";
  time.textContent = `${formatClock(log.created_at)} · ${log.level} · ${kindLabel}`;
  item.appendChild(time);
  const body = document.createElement("p");
  if (kind === "output") {
    const parts = outputParts(log);
    body.className = "log-output-body";
    body.textContent = parts.body || "（空输出）";
    item.appendChild(body);
    return item;
  }
  body.textContent = kind === "phase" ? phaseSummary(log) : logMessage(log);
  item.appendChild(body);
  return item;
}

function stickToLatestLog() {
  requestAnimationFrame(() => {
    if (state.logFollow) els.logs.scrollTop = 0;
  });
}

function intentNumberList(metadata, key) {
  return metadataIdList(metadata, key);
}

const ANSWER_WRAPPER_PREFIX = "agent reported possible final answer:";
const PARTIAL_OUTPUT_PREFIX = "未完成执行的输出摘录：";

function projectFacts() {
  return state.currentProject?.facts || [];
}

function factById(id) {
  return projectFacts().find((fact) => Number(fact.id) === Number(id)) || null;
}

function factRole(fact) {
  if (!fact) return "";
  const description = String(fact.description || "");
  if (fact.kind === "refuted") return "证伪";
  if (fact.kind === "error" || description.startsWith(PARTIAL_OUTPUT_PREFIX)) return "失败";
  if (description.startsWith(ANSWER_WRAPPER_PREFIX)) return "候选答案";
  if (fact.kind === "evidence") return "证据";
  return "";
}

function factLabel(id) {
  const role = factRole(factById(id));
  return role ? `F${id} ${role}` : `F${id}`;
}

function factSupportsAnswer(fact, answer) {
  const description = String(fact?.description || "");
  if (fact?.kind !== "evidence" || !answer || !description.includes(answer)) return false;
  if (description.startsWith(PARTIAL_OUTPUT_PREFIX)) return false;
  if (description.startsWith(ANSWER_WRAPPER_PREFIX)) {
    const source = description.split("\n").find((line) => line.startsWith("source:"));
    return Boolean(source && source.includes(answer));
  }
  return true;
}

function intentTargets(intent) {
  const raw = Array.isArray(intent?.tos) && intent.tos.length ? intent.tos : [intent?.to];
  const targets = [];
  for (const value of raw) {
    const id = Number(value);
    if (!Number.isInteger(id) || id < 0 || targets.includes(id)) continue;
    targets.push(id);
  }
  return targets;
}

function graphEdges(intents) {
  const edges = [];
  for (const intent of intents) {
    const targets = intentTargets(intent);
    if (!targets.length) {
      edges.push({ intent, to: null, key: `intent-${intent.id}` });
      continue;
    }
    for (const to of targets) edges.push({ intent, to, key: `intent-${intent.id}-fact-${to}` });
  }
  return edges;
}

function answerBasisFactId(answer) {
  const text = String(answer || "").trim();
  const project = state.currentProject;
  if (!project || !text) return null;
  const linked = (project.intents || []).find((intent) => (
    Number(intent.to) === 1
    && intent.description === "确认该事实包含最终答案"
    && factSupportsAnswer(factById(intent.from), text)
  ));
  if (linked) return Number(linked.from);
  const producer = (project.intents || []).find((intent) => {
    const targets = intentTargets(intent);
    return targets.includes(1) && targets.some((id) => id !== 1 && factSupportsAnswer(factById(id), text));
  });
  if (producer) {
    return intentTargets(producer).find((id) => id !== 1 && factSupportsAnswer(factById(id), text));
  }
  const supports = projectFacts().filter((fact) => factSupportsAnswer(fact, text));
  if (!supports.length) return null;
  return Number(supports.sort((left, right) => Number(right.id) - Number(left.id))[0].id);
}

function phaseTitle(phase) {
  const name = phase.kind === "first" ? "首次执行" : phase.kind === "reason" ? "推理" : "探索";
  const status = phase.status === "done" ? "已完成" : phase.status === "stopped" ? "已停止" : "进行中";
  return `${name} · ${status}`;
}

function intentResultText(intent, phaseDone) {
  const labels = intent.facts.map(factLabel);
  if (intent.retry) {
    return labels.length ? `intent #${intent.id} 将重试，已保留 ${labels.join("、")}` : `intent #${intent.id} 将重试`;
  }
  const onlyProblems = labels.length > 0 && labels.every((label) => label.endsWith(" 失败") || label.endsWith(" 证伪"));
  if (intent.failed && (labels.length === 0 || onlyProblems)) {
    return labels.length ? `intent #${intent.id} 失败，已保留 ${labels.join("、")}` : `intent #${intent.id} 失败`;
  }
  if (labels.length) return `intent #${intent.id} 得到 ${labels.join("、")}`;
  if (intent.failed) return `intent #${intent.id} 失败`;
  if (intent.legacyFacts) return `intent #${intent.id} 得到新节点`;
  if (intent.finished || phaseDone) return `intent #${intent.id} 未得到新事实`;
  return `intent #${intent.id} 进行中`;
}

function scheduleRows(phase) {
  const rows = [{
    id: phase.id,
    role: "phase",
    level: phase.status === "stopped" ? "error" : phase.level,
    time: phase.time,
    text: phaseTitle(phase),
  }];
  if (phase.kind === "reason") {
    if (phase.result.startsWith("项目完成")) {
      const basis = answerBasisFactId(phase.answer);
      rows.push({
        id: `${phase.id}-result`,
        role: "child",
        level: "success",
        text: basis === null ? phase.result : `${phase.result}，依据 F${basis}`,
      });
    } else if (phase.done && phase.plannedIntents.length) {
      rows.push({
        id: `${phase.id}-plan`,
        role: "child",
        level: "info",
        text: `得到 intent ${phase.plannedIntents.map((id) => `#${id}`).join("、")}`,
      });
    } else if (phase.done && phase.plannedCount) {
      rows.push({
        id: `${phase.id}-plan`,
        role: "child",
        level: "info",
        text: `得到 ${phase.plannedCount} 个 intent`,
      });
    } else if (phase.done && !phase.result) {
      rows.push({
        id: `${phase.id}-plan`,
        role: "child",
        level: "info",
        text: "未得到新 intent",
      });
    }
  } else {
    for (const intent of phase.intentRows) {
      rows.push({
        id: `${phase.id}-intent-${intent.id}`,
        role: "child",
        level: intent.retry ? "warning" : intent.failed ? "error" : "info",
        text: intentResultText(intent, phase.status !== "running"),
      });
    }
  }
  if (phase.stopReason) {
    rows.push({
      id: `${phase.id}-stop`,
      role: "child",
      level: "error",
      text: phase.stopReason,
    });
  }
  return rows;
}

function scheduleNarrative(logs) {
  const phases = [];
  let current = null;

  function start(kind, log) {
    current = {
      kind,
      id: `${kind}-${log.id}`,
      level: log.level || "info",
      time: formatClock(log.created_at),
      status: "running",
      intentRows: [],
      plannedIntents: [],
      plannedCount: 0,
      done: false,
      result: "",
      answer: "",
      stopReason: "",
    };
    phases.push(current);
  }

  function touch(log) {
    if (!current) return;
    current.time = formatClock(log.created_at);
    if (log.level === "error" || log.level === "success") current.level = log.level;
  }

  function ensureIntent(id) {
    const numericId = Number(id);
    let row = current.intentRows.find((item) => item.id === numericId);
    if (!row) {
      row = { id: numericId, facts: [], retry: false, failed: false, finished: false, legacyFacts: false };
      current.intentRows.push(row);
    }
    return row;
  }

  function markStopped(reason) {
    if (!current) return;
    current.status = "stopped";
    current.done = true;
    current.stopReason = reason;
    current.level = "error";
  }

  for (const log of logs) {
    const message = logMessage(log);
    const metadata = log.metadata || {};
    if (message === "project entered first_run mode") {
      start("first", log);
      continue;
    }
    if (message === "project entered explore mode") {
      start("explore", log);
      continue;
    }
    if (message === "project entered reason mode") {
      start("reason", log);
      continue;
    }
    if (message === "reason started") {
      if (current?.kind !== "reason") start("reason", log);
      continue;
    }
    const planned = message.match(/^reason planned (\d+) new intents$/);
    if (planned) {
      if (current?.kind !== "reason") start("reason", log);
      touch(log);
      current.done = true;
      current.status = "done";
      current.plannedCount = Number(planned[1]);
      current.plannedIntents = intentNumberList(metadata, "intent_ids");
      continue;
    }
    if (message === "reason marked project completed") {
      if (current?.kind !== "reason") start("reason", log);
      touch(log);
      current.done = true;
      current.status = "done";
      current.answer = String(metadata.answer || "");
      current.result = finalResultText(message, metadata);
      current.level = "success";
      continue;
    }
    if (
      message === "reason made no progress; dispatcher will stop"
      || message === "reason kept timing out; dispatcher will stop"
      || message.startsWith("target appears offline")
    ) {
      if (!current) start(message.startsWith("target") ? "explore" : "reason", log);
      touch(log);
      markStopped(finalResultText(message, metadata));
      continue;
    }
    if (message === "first-run initial intent started" || /^explore started with \d+ active intents$/.test(message)) {
      const kind = message === "first-run initial intent started" ? "first" : "explore";
      if (current?.kind !== kind) start(kind, log);
      touch(log);
      const ids = intentNumberList(metadata, "intent_ids");
      const single = Number(metadata.intent_id);
      const started = ids.length ? ids : (Number.isInteger(single) ? [single] : []);
      for (const id of started) ensureIntent(id);
      continue;
    }
    const failed = message.match(/^(?:intent (\d+)|first-run) execution failed/);
    const unsaved = message.match(/^(?:explore intent (\d+)|first-run) result was not saved/);
    if (failed || unsaved) {
      const kind = message.startsWith("first-run") ? "first" : "explore";
      if (current?.kind !== kind) start(kind, log);
      touch(log);
      const intentId = Number(metadata.intent_id ?? failed?.[1] ?? unsaved?.[1]);
      if (Number.isInteger(intentId)) {
        const row = ensureIntent(intentId);
        row.failed = true;
        row.finished = true;
      }
      continue;
    }
    const exploreIntent = message.match(/^explore intent (\d+) (finished|released for retry)$/);
    if (exploreIntent || message === "first-run finished") {
      const kind = message === "first-run finished" ? "first" : "explore";
      if (current?.kind !== kind) start(kind, log);
      touch(log);
      const intentId = exploreIntent ? Number(exploreIntent[1]) : Number(metadata.intent_id);
      if (Number.isInteger(intentId)) {
        const row = ensureIntent(intentId);
        row.finished = true;
        const retry = exploreIntent ? exploreIntent[2] !== "finished" : Boolean(metadata.retryable);
        row.retry = retry;
        for (const factId of intentNumberList(metadata, "fact_ids")) {
          if (!row.facts.includes(factId)) row.facts.push(factId);
        }
        if (!Array.isArray(metadata.fact_ids) && (metadata.has_fact || metadata.has_answer)) row.legacyFacts = true;
      }
      if (message === "first-run finished") {
        current.done = true;
        if (current.status === "running") current.status = "done";
      }
      continue;
    }
    if (/^explore finished \d+ intents/.test(message)) {
      if (current?.kind !== "explore") start("explore", log);
      touch(log);
      current.done = true;
      if (current.status === "running") current.status = "done";
      continue;
    }
    if (message === "server dispatcher stopped" && current?.status === "running") {
      current.status = "stopped";
      current.done = true;
      if (!current.stopReason) current.stopReason = "调度结束";
      current = null;
    }
  }
  return phases.slice().reverse().flatMap(scheduleRows);
}

function renderScheduleLine(line) {
  const item = document.createElement("div");
  item.className = `log-entry ${line.level || "info"} ${line.role === "phase" ? "log-phase" : "log-schedule-child"}`;
  item.dataset.logId = String(line.id);
  if (line.role === "phase") {
    const time = document.createElement("time");
    time.textContent = `${line.time || ""} · 阶段`;
    item.appendChild(time);
  }
  const body = document.createElement("p");
  body.textContent = line.text;
  item.appendChild(body);
  return item;
}

function paintLogs() {
  const follow = state.logFollow;
  const previousTop = els.logs.scrollTop;
  els.logs.replaceChildren();
  const entries = state.showDetailLogs
    ? state.logs.filter((log) => classifyLog(log) === "output").slice().reverse()
    : scheduleNarrative(state.logs);
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = mainLogEmptyText();
    els.logs.appendChild(empty);
  } else if (state.showDetailLogs) {
    for (const log of entries) els.logs.appendChild(renderLogNode(log));
  } else {
    for (const line of entries) els.logs.appendChild(renderScheduleLine(line));
  }
  if (follow) stickToLatestLog();
  else els.logs.scrollTop = previousTop;
  renderRunStatus();
}

function appendLogs(logs) {
  if (!state.showDetailLogs) {
    paintLogs();
    return;
  }
  const follow = state.logFollow;
  const previousTop = els.logs.scrollTop;
  const previousHeight = els.logs.scrollHeight;
  const empty = els.logs.querySelector(".empty");
  let added = 0;
  for (const log of logs) {
    if (!shouldShowLog(log)) continue;
    if (els.logs.querySelector(`[data-log-id="${log.id}"]`)) continue;
    if (empty) empty.remove();
    els.logs.prepend(renderLogNode(log));
    added += 1;
  }
  if (!els.logs.querySelector(".log-entry")) {
    let placeholder = els.logs.querySelector(".empty");
    if (!placeholder) {
      placeholder = document.createElement("div");
      placeholder.className = "empty";
      els.logs.appendChild(placeholder);
    }
    placeholder.textContent = mainLogEmptyText();
  }
  if (follow && added) stickToLatestLog();
  else if (added) els.logs.scrollTop = previousTop + (els.logs.scrollHeight - previousHeight);
  renderRunStatus();
}

async function loadLogs(options = {}) {
  if (!state.currentProjectId) return;
  const projectId = state.currentProjectId;
  const reset = options.reset === true || state.logProjectId !== projectId;
  if (reset) state.logRequest += 1;
  const requestId = state.logRequest;
  const afterId = reset ? 0 : state.logAfterId;
  const query = reset ? "limit=200" : `limit=200&after_id=${afterId}`;
  const logs = await api(`/projects/${projectId}/logs?${query}`);
  if (requestId !== state.logRequest || state.currentProjectId !== projectId) return;
  if (reset) {
    state.logs = logs;
    state.logProjectId = projectId;
    state.logAfterId = logs.reduce((max, log) => Math.max(max, Number(log.id) || 0), 0);
    paintLogs();
    return;
  }
  const known = new Set(state.logs.map((log) => log.id));
  const fresh = logs.filter((log) => !known.has(log.id));
  if (!fresh.length) {
    renderRunStatus();
    return;
  }
  state.logs.push(...fresh);
  state.logAfterId = state.logs.reduce((max, log) => Math.max(max, Number(log.id) || 0), state.logAfterId);
  appendLogs(fresh);
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
  if (!state.userGraphZoom) {
    const fitted = computeFitZoom();
    if (fitted) state.graphZoom = fitted;
  }
  applyGraphZoom();
  svg.appendChild(graphDefs());
  svg.appendChild(graphBackground(width, height));
  const viewport = svgElement("g", {});
  svg.appendChild(viewport);
  const edges = graphEdges(intents);
  const edgePorts = assignEdgePorts(edges, factPositions, intentPositions);
  const edgeLanes = assignEdgeLanes(edges, factPositions, intentPositions);
  const edgeLayer = svgElement("g", { class: "graph-edge-layer" });
  const nodeLayer = svgElement("g", { class: "graph-node-layer" });
  viewport.appendChild(edgeLayer);
  viewport.appendChild(nodeLayer);

  for (const graphEdge of edges) {
    const intent = graphEdge.intent;
    const from = factPositions.get(intent.from);
    const to = graphEdge.to === null ? undefined : factPositions.get(graphEdge.to);
    const intentTarget = graphEdge.to === null ? intentPositions.get(intent.id) : undefined;
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
    const edge = routeEdge(start, end, {
      port: edgePorts.get(graphEdge.key) || 0,
      laneY: edgeLanes.get(graphEdge.key),
      sourceHalf: from ? 92 : 82,
      targetHalf: to ? 92 : 82,
    });
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
    nodeLayer.appendChild(intentNode(
      pos.x,
      pos.y,
      `I${intent.id}`,
      intent.description || "pending intent",
      intent.status,
      intent.id === state.selectedIntentId,
      () => {
        state.selectedIntentId = intent.id;
        state.selectedFactId = null;
        drawGraph(project);
        renderGraphDetail(project);
      },
    ));
  }
}

function clampZoom(value) {
  return Math.max(0.2, Math.min(1.8, value));
}

function computeFitZoom() {
  const viewport = els.graphViewport;
  if (!viewport || !state.graphBaseWidth || !state.graphBaseHeight) return null;
  const availableWidth = viewport.clientWidth - 12;
  const availableHeight = viewport.clientHeight - 12;
  if (availableWidth < 80 || availableHeight < 80) return null;
  return clampZoom(Math.min(1, availableWidth / state.graphBaseWidth, availableHeight / state.graphBaseHeight));
}

function applyGraphZoom() {
  const zoom = state.graphZoom;
  els.graph.style.width = `${Math.round(state.graphBaseWidth * zoom)}px`;
  els.graph.style.height = `${Math.round(state.graphBaseHeight * zoom)}px`;
}

function isGraphHitTarget(target) {
  return Boolean(target?.closest?.(".graph-node, .graph-edge-group, .edge-label"));
}

function wheelDeltaY(event) {
  let delta = event.deltaY;
  if (event.deltaMode === 1) delta *= 16;
  if (event.deltaMode === 2) delta *= viewportPageDelta(event);
  return delta;
}

function viewportPageDelta(event) {
  const viewport = event.currentTarget;
  return viewport?.clientHeight || 480;
}

function zoomGraphAt(clientX, clientY, nextZoom) {
  const viewport = els.graphViewport;
  if (!viewport) return;
  const oldZoom = state.graphZoom || 1;
  const zoom = clampZoom(nextZoom);
  if (Math.abs(zoom - oldZoom) < 0.001) return;
  const rect = viewport.getBoundingClientRect();
  const localX = clientX - rect.left;
  const localY = clientY - rect.top;
  const contentX = (viewport.scrollLeft + localX) / oldZoom;
  const contentY = (viewport.scrollTop + localY) / oldZoom;
  state.userGraphZoom = true;
  state.graphZoom = zoom;
  applyGraphZoom();
  viewport.scrollLeft = contentX * zoom - localX;
  viewport.scrollTop = contentY * zoom - localY;
}

function bindGraphInteractions() {
  const viewport = els.graphViewport;
  if (!viewport || viewport.dataset.graphBound === "1") return;
  viewport.dataset.graphBound = "1";

  viewport.addEventListener("wheel", (event) => {
    if (!state.currentProject || !state.graphBaseWidth) return;
    const delta = wheelDeltaY(event);
    if (!delta) return;
    event.preventDefault();
    zoomGraphAt(event.clientX, event.clientY, state.graphZoom * Math.exp(-delta * 0.0018));
  }, { passive: false });

  let drag = null;
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || isGraphHitTarget(event.target)) return;
    drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      scrollLeft: viewport.scrollLeft,
      scrollTop: viewport.scrollTop,
      moved: false,
    };
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) {
      drag.moved = true;
      viewport.classList.add("is-panning");
    }
    viewport.scrollLeft = drag.scrollLeft - dx;
    viewport.scrollTop = drag.scrollTop - dy;
  });
  const endDrag = (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    drag = null;
    viewport.classList.remove("is-panning");
  };
  viewport.addEventListener("pointerup", endDrag);
  viewport.addEventListener("pointercancel", endDrag);
}

const GRAPH = {
  marginX: 150,
  marginY: 168,
  factColumnGap: 440,
  intentColumnGap: 260,
  rowGap: 140,
  minWidth: 1040,
  minHeight: 560,
};

function layoutGraph(facts, intents) {
  const columns = groupFactsByDepth(facts, intents);
  orderColumnsByParents(columns, intents);
  const factDepthById = new Map();
  columns.forEach((columnFacts, columnIndex) => {
    columnFacts.forEach((fact) => factDepthById.set(fact.id, columnIndex));
  });
  const factPositions = layoutFactsLeftToRight(columns, intents);
  const intentPositions = layoutOpenIntents(intents, factPositions, factDepthById, columns.length);
  const points = [...factPositions.values(), ...intentPositions.values()];
  const maxX = points.reduce((max, point) => Math.max(max, point.x), GRAPH.marginX);
  const maxY = points.reduce((max, point) => Math.max(max, point.y), GRAPH.marginY);
  const width = Math.max(GRAPH.minWidth, maxX + GRAPH.marginX + 30);
  const height = Math.max(GRAPH.minHeight, maxY + 150);
  return { width, height, factPositions, intentPositions };
}

function groupOpenIntents(intents) {
  const grouped = new Map();
  for (const intent of intents) {
    if (intentTargets(intent).length) continue;
    const key = intent.from === null ? "__initial__" : String(intent.from);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(intent);
  }
  return grouped;
}

function layoutOpenIntents(intents, factPositions, factDepthById, factColumnCount) {
  const grouped = groupOpenIntents(intents);
  const positions = new Map();
  for (const [key, group] of grouped.entries()) {
    group.sort((left, right) => left.id - right.id);
    const source = key === "__initial__" ? { x: GRAPH.marginX, y: GRAPH.marginY } : factPositions.get(Number(key));
    if (!source) continue;
    const sourceDepth = key === "__initial__" ? 0 : (factDepthById.get(Number(key)) ?? 0);
    const isLastFactColumn = sourceDepth >= factColumnCount - 1;
    const x = isLastFactColumn
      ? source.x + GRAPH.intentColumnGap
      : source.x + Math.round(GRAPH.factColumnGap * 0.5);
    const rowGap = 78;
    const startY = source.y + 96;
    group.forEach((intent, index) => {
      positions.set(intent.id, { x, y: startY + index * rowGap });
    });
  }
  return positions;
}

function layoutFactsLeftToRight(columns, intents) {
  const positions = new Map();
  columns.forEach((columnFacts, columnIndex) => {
    const x = GRAPH.marginX + columnIndex * GRAPH.factColumnGap;
    columnFacts.forEach((fact, rowIndex) => {
      positions.set(fact.id, { x, y: GRAPH.marginY + rowIndex * GRAPH.rowGap });
    });
  });
  const links = linkedFactIds(intents);
  for (let iteration = 0; iteration < 6; iteration += 1) {
    for (const columnFacts of columns) {
      const desired = columnFacts.map((fact) => medianY(links.get(fact.id) || [], positions) ?? positions.get(fact.id).y);
      let cursor = GRAPH.marginY;
      columnFacts.forEach((fact, index) => {
        const y = Math.max(cursor, desired[index]);
        positions.get(fact.id).y = y;
        cursor = y + GRAPH.rowGap;
      });
    }
  }
  const minY = Math.min(...Array.from(positions.values(), (point) => point.y));
  const shift = GRAPH.marginY - minY;
  if (shift) {
    for (const point of positions.values()) point.y += shift;
  }
  return positions;
}

function linkedFactIds(intents) {
  const links = new Map();
  const add = (left, right) => {
    if (!links.has(left)) links.set(left, []);
    links.get(left).push(right);
  };
  for (const intent of intents) {
    if (intent.from === null || intent.from === undefined) continue;
    for (const to of intentTargets(intent)) {
      add(intent.from, to);
      add(to, intent.from);
    }
  }
  return links;
}

function medianY(ids, positions) {
  const values = ids.map((id) => positions.get(id)?.y).filter((value) => value !== undefined).sort((left, right) => left - right);
  if (!values.length) return null;
  const middle = Math.floor(values.length / 2);
  return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

function orderColumnsByParents(columns, intents) {
  const columnOf = new Map();
  columns.forEach((columnFacts, columnIndex) => {
    columnFacts.forEach((fact) => columnOf.set(fact.id, columnIndex));
  });
  const parents = new Map();
  const children = new Map();
  const add = (bucket, factId, neighborId) => {
    if (!bucket.has(factId)) bucket.set(factId, []);
    bucket.get(factId).push(neighborId);
  };
  for (const intent of intents) {
    if (intent.from === null || intent.from === undefined) continue;
    for (const to of intentTargets(intent)) {
      const parentColumn = columnOf.get(intent.from);
      const childColumn = columnOf.get(to);
      if (parentColumn === undefined || childColumn === undefined || parentColumn >= childColumn) continue;
      add(parents, to, intent.from);
      add(children, intent.from, to);
    }
  }
  const rowOf = new Map();
  const reindex = () => {
    columns.forEach((columnFacts, columnIndex) => {
      columnFacts.forEach((fact, rowIndex) => rowOf.set(fact.id, { columnIndex, rowIndex }));
    });
  };
  reindex();
  const scoreOf = (factId, neighborIds) => {
    const rows = (neighborIds || [])
      .map((id) => rowOf.get(id)?.rowIndex)
      .filter((row) => row !== undefined)
      .sort((left, right) => left - right);
    if (!rows.length) return null;
    const middle = Math.floor(rows.length / 2);
    return rows.length % 2 ? rows[middle] : (rows[middle - 1] + rows[middle]) / 2;
  };
  for (let pass = 0; pass < 4; pass += 1) {
    const forward = pass % 2 === 0;
    const order = forward
      ? columns.map((_, index) => index)
      : columns.map((_, index) => columns.length - 1 - index);
    for (const columnIndex of order) {
      columns[columnIndex].sort((left, right) => {
        if (left.id === 0) return right.id === 0 ? 0 : -1;
        if (right.id === 0) return 1;
        const neighbors = forward ? parents : children;
        const leftScore = scoreOf(left.id, neighbors.get(left.id));
        const rightScore = scoreOf(right.id, neighbors.get(right.id));
        if (leftScore === null && rightScore === null) return left.id - right.id;
        if (leftScore === null) return 1;
        if (rightScore === null) return -1;
        return leftScore - rightScore || left.id - right.id;
      });
      reindex();
    }
  }
}

function groupFactsByDepth(facts, intents) {
  const byId = new Map(facts.map((fact) => [fact.id, fact]));
  const outgoing = new Map();
  const incomingCount = new Map(facts.map((fact) => [fact.id, 0]));
  for (const intent of intents) {
    if (intent.from === null || intent.from === undefined || !byId.has(intent.from)) continue;
    for (const to of intentTargets(intent)) {
      if (!byId.has(to)) continue;
      if (!outgoing.has(intent.from)) outgoing.set(intent.from, []);
      outgoing.get(intent.from).push({ ...intent, renderedTo: to });
      incomingCount.set(to, (incomingCount.get(to) || 0) + 1);
    }
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
      if (nextDepth > facts.length) continue;
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
  if (byId.has(1) && (incomingCount.get(1) || 0) === 0) {
    const rightmostDepth = Math.max(0, ...depth.values());
    depth.set(1, Math.max(depth.get(1) || 0, rightmostDepth));
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

function assignEdgePorts(edges, factPositions, intentPositions) {
  const groups = new Map();
  for (const edge of edges) {
    const key = String(edge.intent.from ?? "null");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(edge);
  }
  const ports = new Map();
  for (const group of groups.values()) {
    group.sort((left, right) => edgeTargetY(left, factPositions, intentPositions) - edgeTargetY(right, factPositions, intentPositions) || left.intent.id - right.intent.id || (left.to ?? -1) - (right.to ?? -1));
    const step = group.length > 1 ? Math.min(16, 52 / (group.length - 1)) : 0;
    const start = -((group.length - 1) * step) / 2;
    group.forEach((edge, index) => ports.set(edge.key, start + index * step));
  }
  return ports;
}

function edgeTargetY(edge, factPositions, intentPositions) {
  const target = edge.to === null || edge.to === undefined
    ? intentPositions.get(edge.intent.id)
    : factPositions.get(edge.to);
  return target?.y ?? edge.intent.id;
}

function assignEdgeLanes(edges, factPositions, intentPositions) {
  const lanes = new Map();
  const skipped = edges.filter((edge) => {
    const source = factPositions.get(edge.intent.from);
    const target = edge.to === null || edge.to === undefined
      ? intentPositions.get(edge.intent.id)
      : factPositions.get(edge.to);
    if (!source || !target) return false;
    return !isAdjacentEdge(source, target);
  }).sort((left, right) => left.intent.id - right.intent.id || (left.to ?? -1) - (right.to ?? -1));
  const points = [...factPositions.values(), ...intentPositions.values()];
  const minY = points.length ? Math.min(...points.map((point) => point.y)) : GRAPH.marginY;
  const maxY = points.length ? Math.max(...points.map((point) => point.y)) : GRAPH.marginY;
  skipped.forEach((edge, index) => {
    const band = Math.floor(index / 2);
    const onTop = index % 2 === 0;
    lanes.set(edge.key, onTop ? minY - 84 - band * 18 : maxY + 84 + band * 18);
  });
  return lanes;
}

function isAdjacentEdge(source, target) {
  const gap = target.x - source.x;
  return gap > 0 && gap <= GRAPH.factColumnGap * 1.25;
}

function routeEdge(source, target, options) {
  const sourceHalf = options.sourceHalf ?? 92;
  const targetHalf = options.targetHalf ?? 92;
  const port = options.port || 0;
  const forward = target.x >= source.x;
  const startX = source.x + (forward ? sourceHalf + 14 : -(sourceHalf + 14));
  const endX = target.x - (forward ? targetHalf + 14 : -(targetHalf + 14));
  const startY = source.y + port;
  const endY = target.y;
  if (options.laneY === undefined && isAdjacentEdge(source, target)) {
    const dx = endX - startX;
    const c1x = startX + dx * 0.42;
    const c2x = startX + dx * 0.58;
    const path = `M ${startX} ${startY} C ${c1x} ${startY}, ${c2x} ${endY}, ${endX} ${endY}`;
    const mid = cubicPoint(
      { x: startX, y: startY },
      { x: c1x, y: startY },
      { x: c2x, y: endY },
      { x: endX, y: endY },
      0.5,
    );
    return { path, labelX: mid.x, labelY: mid.y - 12 };
  }
  const laneY = options.laneY ?? (Math.min(startY, endY) - 84);
  const outX = startX + (forward ? 28 : -28);
  const inX = endX - (forward ? 28 : -28);
  const path = `M ${startX} ${startY} L ${outX} ${startY} L ${outX} ${laneY} L ${inX} ${laneY} L ${inX} ${endY} L ${endX} ${endY}`;
  return { path, labelX: (outX + inX) / 2, labelY: laneY - 12 };
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
  const flowing = intent?.status === "running";
  if (flowing) path.classList.add("is-flowing");
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
  if (flowing) group.appendChild(flowingEdge(pathData));
  group.appendChild(hitPath);
  return group;
}

function flowingEdge(pathData) {
  const flow = document.createElementNS("http://www.w3.org/2000/svg", "path");
  flow.classList.add("graph-edge-flow");
  flow.setAttribute("d", pathData);
  flow.setAttribute("fill", "none");
  return flow;
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
  const predecessors = neighborFacts(project, fact, "in");
  const successors = neighborFacts(project, fact, "out");
  detail.className = "graph-detail";
  detail.innerHTML = `
    <div class="graph-detail-header">
      <strong>Fact #${escapeHtml(fact.id)}</strong>
      <span class="badge ${escapeHtml(fact.kind || "evidence")}">${escapeHtml(fact.kind || "evidence")}</span>
      <button type="button" class="ghost-button" id="clear-graph-selection">关闭</button>
    </div>
    <p>${escapeHtml(fact.description)}</p>
    <div class="graph-links">
      ${renderFactNeighbors("前继节点", predecessors)}
      ${renderFactNeighbors("后继节点", successors)}
    </div>
  `;
  bindClearGraphSelection(project, detail);
}

function neighborFacts(project, fact, direction) {
  const facts = [];
  const seen = new Set();
  for (const intent of project.intents) {
    const targets = intentTargets(intent);
    const neighborIds = direction === "in"
      ? (targets.includes(fact.id) && intent.from !== null && intent.from !== undefined ? [intent.from] : [])
      : (intent.from === fact.id ? targets : []);
    for (const neighborId of neighborIds) {
      if (neighborId === null || neighborId === undefined || seen.has(neighborId)) continue;
      const neighbor = project.facts.find((item) => item.id === neighborId);
      if (!neighbor) continue;
      seen.add(neighborId);
      facts.push(neighbor);
    }
  }
  return facts;
}

function renderFactNeighbors(title, facts) {
  if (!facts.length) {
    return `<div><span>${escapeHtml(title)}</span><em>无</em></div>`;
  }
  const items = facts.map((fact) => {
    const description = trimText(fact.description || "无描述", 90);
    return `<li>F${escapeHtml(fact.id)} · ${escapeHtml(fact.kind || "evidence")} · ${escapeHtml(description)}</li>`;
  }).join("");
  return `<div><span>${escapeHtml(title)}</span><ul>${items}</ul></div>`;
}

function renderIntentDetail(project, intent, detail) {
  const fromFact = project.facts.find((fact) => fact.id === intent.from);
  const targets = intentTargets(intent);
  const targetHtml = targets.length
    ? targets.map((factId) => renderFactEndpoint("To", factId, project.facts.find((fact) => fact.id === factId))).join("")
    : renderFactEndpoint("To", null, null);
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
      ${targetHtml}
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

function scheduleRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  if (state.logTimer) clearInterval(state.logTimer);
  state.refreshTimer = setInterval(() => {
    if (state.currentProjectId && !state.busy) refreshCurrent().catch(() => {});
  }, 5000);
  state.logTimer = setInterval(() => {
    if (!state.currentProjectId) return;
    renderRunStatus();
    if (state.busy || !(state.runStatus?.running || state.runStatus?.queued)) return;
    loadLogs().catch(() => {});
  }, 2000);
}

function clearCurrentProject() {
  state.currentProjectId = null;
  state.currentProject = null;
  state.runStatus = null;
  state.selectedFactId = null;
  state.selectedIntentId = null;
  els.title.textContent = "选择项目";
  els.subtitle.textContent = "查看任务输入、fact / intent 图和运行状态";
  [...els.summary.querySelectorAll("strong")].forEach((node) => {
    node.textContent = "-";
  });
  els.graph.innerHTML = "";
  if (els.graphDetail) {
    els.graphDetail.className = "graph-detail empty-detail";
    els.graphDetail.textContent = "点击 fact 节点或 intent 边查看详情";
  }
  els.logs.replaceChildren();
  state.logs = [];
  state.logProjectId = null;
  state.logAfterId = 0;
  state.logFollow = true;
  renderRunStatus();
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

function formChecked(name) {
  return Boolean(els.form.querySelector(`[name="${name}"]`)?.checked);
}

function syncTargetFields() {
  document.querySelector("#target-url-field").hidden = !formChecked("use_url");
  document.querySelector("#target-ip-field").hidden = !formChecked("use_ip");
  document.querySelector("#target-file-field").hidden = !formChecked("use_attachment");
  document.querySelector("#hint-field").hidden = !formChecked("use_hint");
}

async function uploadAttachment(projectId, file) {
  const headers = { "Content-Type": "application/octet-stream" };
  const token = authToken();
  if (token) headers["X-Echo-Agent-Token"] = token;
  const params = new URLSearchParams({ name: file.name });
  const response = await fetch(`/projects/${projectId}/attachment?${params}`, {
    method: "PUT",
    headers,
    body: file,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || body.error || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function setCreateError(message) {
  if (!els.createError) return;
  const text = String(message || "").trim();
  els.createError.hidden = !text;
  els.createError.textContent = text;
}

const ATTACHMENT_PREFIX = "/home/kali/attachments/";

function splitTargets(targetUrl) {
  const urls = [];
  const ips = [];
  const attachments = [];
  for (const line of String(targetUrl || "").split(/\n/)) {
    const item = line.trim();
    if (!item) continue;
    if (item.startsWith(ATTACHMENT_PREFIX)) attachments.push(item);
    else if (/^https?:\/\//i.test(item)) urls.push(item);
    else if (!/[\s/]/.test(item)) ips.push(item);
  }
  return { url: urls[0] || "", ip: ips[0] || "", attachments };
}

function setProjectDialogMode(editing) {
  if (els.createDialogTitle) els.createDialogTitle.textContent = editing ? "编辑项目" : "创建项目";
  if (els.submitCreate) els.submitCreate.textContent = editing ? "保存" : "创建";
}

function setAttachmentNote(attachments) {
  if (!els.attachmentNote) return;
  if (!attachments.length) {
    els.attachmentNote.hidden = true;
    els.attachmentNote.textContent = "";
    return;
  }
  const names = attachments.map((path) => path.split("/").pop()).join("、");
  els.attachmentNote.hidden = false;
  els.attachmentNote.textContent = `当前附件：${names}。不重新选择文件则保留。`;
}

function openCreateDialog() {
  state.editingProjectId = null;
  state.editAttachments = [];
  els.form.reset();
  setProjectDialogMode(false);
  setAttachmentNote([]);
  syncTargetFields();
  setCreateError("");
  if (els.submitCreate) els.submitCreate.disabled = false;
  els.createDialog.showModal();
  els.form.querySelector("[name=name]")?.focus();
}

function openEditDialog() {
  const project = state.currentProject;
  if (!project) return;
  const running = Boolean(state.runStatus?.running) || Boolean(state.runStatus?.queued) || project.status === "running";
  if (running) {
    setStatus("error · 项目正在运行或排队，请先停止后再编辑");
    return;
  }
  const targets = splitTargets(project.target_url);
  state.editingProjectId = project.id;
  state.editAttachments = targets.attachments;
  els.form.reset();
  els.form.name.value = project.name || "";
  els.form.objective.value = project.objective || "";
  els.form.use_url.checked = Boolean(targets.url);
  els.form.target_url.value = targets.url;
  els.form.use_ip.checked = Boolean(targets.ip);
  els.form.target_ip.value = targets.ip;
  els.form.use_attachment.checked = targets.attachments.length > 0;
  els.form.use_hint.checked = Boolean(String(project.hint || "").trim());
  els.form.hint.value = project.hint || "";
  setProjectDialogMode(true);
  setAttachmentNote(targets.attachments);
  syncTargetFields();
  setCreateError("");
  if (els.submitCreate) els.submitCreate.disabled = false;
  els.createDialog.showModal();
  els.form.querySelector("[name=name]")?.focus();
}

function closeCreateDialog() {
  els.createDialog.close();
}

els.openCreate.addEventListener("click", openCreateDialog);
els.editProject.addEventListener("click", openEditDialog);
els.closeCreate.addEventListener("click", closeCreateDialog);
els.cancelCreate.addEventListener("click", closeCreateDialog);
els.form.addEventListener("change", (event) => {
  if (event.target instanceof HTMLInputElement && event.target.type === "checkbox") syncTargetFields();
});
syncTargetFields();

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setCreateError("");
  if (els.submitCreate) els.submitCreate.disabled = true;
  try {
  await withAction("creating project...", async () => {
    const data = new FormData(els.form);
    const name = String(data.get("name") || "").trim();
    const objective = String(data.get("objective") || "").trim();
    const useUrl = formChecked("use_url");
    const useIp = formChecked("use_ip");
    const useAttachment = formChecked("use_attachment");
    const useHint = formChecked("use_hint");
    if (!name) throw new Error("项目名称不能为空");
    if (!objective) throw new Error("目的不能为空");
    if (!useUrl && !useIp && !useAttachment) throw new Error("请至少勾选一种目标");
    const targets = [];
    if (useUrl) {
      const target = String(data.get("target_url") || "").trim();
      if (!/^https?:\/\//i.test(target)) throw new Error("URL 需要以 http:// 或 https:// 开头");
      targets.push(target);
    }
    if (useIp) {
      const target = String(data.get("target_ip") || "").trim();
      if (!target || /[\s/]/.test(target)) throw new Error("请填写 IP，可以带端口");
      targets.push(target);
    }
    let file = null;
    if (useAttachment) {
      file = els.form.attachment.files[0];
      if (!file) throw new Error("请上传附件");
    }
    const hint = useHint ? String(data.get("hint") || "").trim() : "";
    if (useHint && !hint) throw new Error("已勾选 Hint，请填写内容");
    if (useAttachment && !file && state.editingProjectId) targets.push(...state.editAttachments);
    const payload = { name, hint, objective };
    if (targets.length) payload.target_url = targets.join("\n");
    const editingId = state.editingProjectId;
    let project;
    if (editingId) {
      project = await api(`/projects/${editingId}/details`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      if (file) project = await uploadAttachment(project.id, file);
    } else {
      project = await api("/projects", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (file) {
        try {
          project = await uploadAttachment(project.id, file);
        } catch (error) {
          await api(`/projects/${project.id}`, { method: "DELETE" }).catch(() => {});
          throw error;
        }
      }
    }
    state.editingProjectId = null;
    state.editAttachments = [];
    els.form.reset();
    setProjectDialogMode(false);
    setAttachmentNote([]);
    syncTargetFields();
    els.createDialog.close();
    if (editingId && state.currentProjectId === project.id) await refreshCurrent();
    else {
      await refreshProjectsOnly();
      await selectProject(project.id);
    }
  });
  } catch (error) {
    setCreateError(error.message || "创建失败");
  } finally {
    if (els.submitCreate) els.submitCreate.disabled = false;
  }
});

els.refreshLogs.addEventListener("click", () => loadLogs({ reset: true }).catch((error) => setStatus(`error · ${error.message}`)));
function syncDetailLogLayout() {
  const enabled = state.showDetailLogs;
  els.layout?.classList.toggle("is-detail-logs", enabled);
  els.logsPanel?.classList.toggle("is-detail", enabled);
}

els.showDetailLogs?.addEventListener("change", () => {
  state.showDetailLogs = els.showDetailLogs.checked;
  syncDetailLogLayout();
  paintLogs();
});
syncDetailLogLayout();
els.logs.addEventListener("scroll", () => {
  state.logFollow = els.logs.scrollTop < 48;
});
els.clearLogs.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  const confirmed = window.confirm("Clear all logs for this project?");
  if (!confirmed) return;
  await withAction("clearing logs...", async () => {
    await api(`/projects/${state.currentProjectId}/logs`, {
      method: "DELETE",
    });
    await loadLogs({ reset: true });
  });
});

els.runProject.addEventListener("click", async () => {
  if (!state.currentProjectId) return;
  await withAction("starting dispatcher...", async () => {
    await api(`/projects/${state.currentProjectId}/run`, {
      method: "POST",
      body: JSON.stringify({}),
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

els.deleteProject.addEventListener("click", () => {
  if (!state.currentProjectId) return;
  deleteProjectById(state.currentProjectId);
});

els.stopAll.addEventListener("click", async () => {
  const confirmed = window.confirm("停止所有正在运行和排队中的项目？");
  if (!confirmed) return;
  await withAction("stopping all projects...", async () => {
    await api("/projects/runs", { method: "DELETE" });
    await loadProjects();
    if (state.currentProjectId) await refreshCurrent();
  });
});

els.openSettings.addEventListener("click", async () => {
  const settings = await api("/settings");
  const form = els.settingsForm;
  form.max_concurrent_projects.value = settings.max_concurrent_projects;
  form.codex_max_slots.value = settings.codex_max_slots;
  form.base_url.value = settings.base_url || "";
  form.model.value = settings.model || "";
  form.api_key.value = "";
  form.clear_api_key.checked = false;
  els.apiKeyHint.textContent = settings.api_key_set
    ? `当前已保存 API Key${settings.api_key_hint ? `，末尾 ${settings.api_key_hint}` : ""}。留空则保持不变。`
    : "当前没有 API Key。";
  if (!settings.models_unified) {
    els.apiKeyHint.textContent += " 各模式模型目前不一致，保存后会统一成上面的模型。";
  }
  els.settingsDialog.showModal();
});

els.closeSettings.addEventListener("click", () => els.settingsDialog.close());

els.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = els.settingsForm;
  const data = new FormData(form);
  await withAction("saving settings...", async () => {
    await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        max_concurrent_projects: Number(data.get("max_concurrent_projects")),
        codex_max_slots: Number(data.get("codex_max_slots")),
        explore_max_workers: Number(data.get("codex_max_slots")),
        base_url: data.get("base_url"),
        model: data.get("model"),
        api_key: data.get("api_key") || null,
        clear_api_key: data.get("clear_api_key") === "on",
      }),
    });
    els.settingsDialog.close();
  });
});


if (window.ResizeObserver && els.graphViewport) {
  let pending = 0;
  let fitting = false;
  const observer = new ResizeObserver(() => {
    if (fitting || state.userGraphZoom || !state.currentProject) return;
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(() => {
      if (fitting || state.userGraphZoom || !state.currentProject) return;
      const fitted = computeFitZoom();
      if (!fitted || Math.abs(fitted - state.graphZoom) < 0.02) return;
      fitting = true;
      state.graphZoom = fitted;
      drawGraph(state.currentProject);
      fitting = false;
    });
  });
  observer.observe(els.graphViewport);
}

bindGraphInteractions();
loadProjects();
