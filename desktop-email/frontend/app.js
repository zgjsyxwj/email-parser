const icons = {
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
  folder: '<path d="M3 7V5h6l2 2h10v13H3Z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  upload: '<path d="M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/>',
};

const state = {
  items: [],
  filter: "all",
  outputDir: "",
  inputPaths: [],
  settingsOpen: false,
  maxDepth: "10",
  maxExtractMiB: "500",
  running: false,
  cancelling: false,
  batchId: null,
  requestId: null,
  status: "ready",
  summary: null,
  errors: [],
  skippedFiles: [],
  pollTimer: null,
};

const statusLabels = {
  pending: "待解析",
  running: "正在解析",
  success: "已完成",
  skipped: "已跳过",
  failed: "解析失败",
  partial_failed: "部分失败",
  cancelled: "待重试",
};

const appRoot = document.getElementById("app");
const toast = document.getElementById("toast");
let toastTimer = null;

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ""}</svg>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[character]));
}

function invoke(command, args) {
  const tauri = window.__TAURI__?.core || window.__TAURI_INTERNALS__;
  if (!tauri || typeof tauri.invoke !== "function") {
    return Promise.reject(new Error("请在邮件解析桌面应用中运行此操作"));
  }
  return tauri.invoke(command, args);
}

function notify(message) {
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3600);
}

function errorMessage(error, fallback) {
  if (typeof error === "string" && error.trim()) return error;
  if (error && typeof error.message === "string" && error.message.trim()) return error.message;
  if (error && typeof error.error === "string" && error.error.trim()) return error.error;
  return fallback;
}

function addError(error, fallback = {}) {
  const sourcePath = error?.source_path || fallback.source_path || "";
  const message = error?.error || error?.message || fallback.message || "解析失败";
  const entry = {
    name: fallback.name || sourcePath || "批次错误",
    source_path: sourcePath,
    message,
    stage: error?.stage || fallback.stage || "",
    code: error?.code || fallback.code || "",
    location: error?.location || fallback.location || "",
  };
  const key = [entry.source_path, entry.stage, entry.code, entry.location, entry.message].join("\u0000");
  if (!state.errors.some((existing) => existing.key === key)) {
    state.errors.push({ ...entry, key });
  }
}

function addSkippedFile(file) {
  const path = file?.path || "";
  if (!path || state.skippedFiles.some((existing) => existing.path === path)) return;
  state.skippedFiles.push({ path, reason: file.reason || "已跳过独立非邮件文件" });
}

function makeId(prefix) {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function counts() {
  const done = state.items.filter((item) => ["success", "skipped", "partial_failed", "failed", "cancelled"].includes(item.status)).length;
  return {
    total: state.items.length,
    done,
    success: state.items.filter((item) => item.status === "success").length,
    skipped: state.items.filter((item) => item.status === "skipped").length,
    partial_failed: state.items.filter((item) => item.status === "partial_failed").length,
    failed: state.items.filter((item) => item.status === "failed").length,
  };
}

function fileSize(size) {
  if (!Number.isFinite(size) || size < 1024) return `${size || 0} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function addSelectedFiles(selectedFiles) {
  if (state.running) {
    notify("当前批次正在处理，请先取消后再添加邮件。");
    return;
  }
  const selection = selectedFiles;
  const files = Array.from(selection.files || []);
  const skippedFiles = Array.from(selection.skipped_files || []);
  const inputs = Array.from(selection.inputs || []).filter((path) => typeof path === "string" && path);
  const knownInputs = new Set(state.inputPaths);
  for (const path of inputs) {
    if (!knownInputs.has(path)) {
      state.inputPaths.push(path);
      knownInputs.add(path);
    }
  }
  for (const file of skippedFiles) {
    addSkippedFile(file);
  }
  const emails = files.filter((file) => file && typeof file.path === "string" && /\.(eml|msg)$/i.test(file.name));
  const skipped = skippedFiles.length + files.length - emails.length;
  const known = new Set(state.items.map((item) => item.path));
  for (const file of emails) {
    const path = file.path;
    if (known.has(path)) continue;
    state.items.push({
      id: makeId("item"),
      path,
      name: file.name,
      size: fileSize(file.size),
      status: "pending",
      mailDir: null,
      markdownPath: null,
      error: null,
    });
    known.add(path);
  }
  if (emails.length === 0 && skipped > 0) notify(`已跳过 ${skipped} 个非邮件文件，只接受 .eml 或 .msg 邮件。`);
  else if (skipped > 0) notify(`已加入 ${emails.length} 封 EML/MSG，跳过 ${skipped} 个非邮件文件。`);
  else if (emails.length > 0) notify(`已加入 ${emails.length} 封 EML/MSG。`);
  render();
}

async function chooseInputFiles() {
  try {
    addSelectedFiles(await invoke("choose_input_files"));
  } catch (error) {
    notify(errorMessage(error, "无法选择 EML 或 MSG 邮件。"));
  }
}

async function chooseInputFolder() {
  try {
    addSelectedFiles(await invoke("choose_input_folder"));
  } catch (error) {
    notify(errorMessage(error, "无法扫描邮件目录。"));
  }
}

function statusBadge(status) {
  return `<span class="status ${escapeHtml(status)}">${escapeHtml(statusLabels[status] || status)}</span>`;
}

function filteredItems() {
  if (state.filter === "all") return state.items;
  return state.items.filter((item) => item.status === state.filter);
}

function renderErrors() {
  if (!state.errors.length) return "";
  return `<div class="error-panel"><strong>需要处理的邮件</strong>${state.errors.map((error) => `<div class="error-row"><b>${escapeHtml(error.name || error.source_path || "批次错误")}</b>：${escapeHtml(error.message || error.error)}${error.stage ? `（${escapeHtml(error.stage)}${error.code ? ` / ${escapeHtml(error.code)}` : ""}${error.location ? ` / ${escapeHtml(error.location)}` : ""}）` : ""}</div>`).join("")}</div>`;
}

function renderTable() {
  const rows = filteredItems();
  if (!rows.length) {
    const text = state.items.length ? "此分类暂无邮件" : "还没有加入邮件。选择一封 EML 或 MSG，开始整理。";
    return `<div class="table-wrap" tabindex="0" role="region" aria-label="邮件列表"><table class="table"><thead><tr><th>邮件</th><th>大小</th><th>状态</th></tr></thead><tbody><tr><td colspan="3"><div class="empty">${text}</div></td></tr></tbody></table></div>`;
  }
  return `<div class="table-wrap" tabindex="0" role="region" aria-label="邮件列表"><table class="table"><thead><tr><th>邮件</th><th>大小</th><th>状态</th></tr></thead><tbody>${rows.map((item) => `<tr><td><div class="mailcell"><span class="file-icon">${/\.msg$/i.test(item.name) ? "MSG" : "EML"}</span><div class="grow"><div class="mail-title" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div><div class="mail-meta">${item.error ? escapeHtml(item.error) : "源文件保持原位"}</div></div></div></td><td class="note mono">${escapeHtml(item.size)}</td><td>${statusBadge(item.status)}${item.mailDir ? `<div class="result-actions">${item.markdownPath ? `<button class="result-open" data-action="open" data-path="${escapeHtml(item.markdownPath)}">打开邮件.md ${icon("open")}</button>` : ""}<button class="result-open" data-action="open" data-path="${escapeHtml(item.mailDir)}">打开目录 ${icon("open")}</button></div>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderSettings() {
  const disabled = state.running ? " disabled" : "";
  return `<section class="settings" aria-labelledby="settings-title">
    <button id="settings-title" class="quiet settings-toggle" data-action="settings" aria-expanded="${state.settingsOpen}" aria-controls="settings-fields"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>解析设置</button>
    <div id="settings-fields" class="settings-fields"${state.settingsOpen ? "" : " hidden"}>
    <label for="max-depth">最大嵌套深度</label>
    <input id="max-depth" data-setting="maxDepth" type="number" min="0" max="100" step="1" required value="${escapeHtml(state.maxDepth)}"${disabled} />
    <label for="max-extract">累计展开上限（MiB）</label>
    <input id="max-extract" data-setting="maxExtractMiB" type="number" min="0" max="10240" step="1" required value="${escapeHtml(state.maxExtractMiB)}"${disabled} />
    <p class="note">按每封根邮件计量。超限时保留嵌套邮件原件。</p>
    </div>
  </section>`;
}

function render() {
  const tableScrollTop = appRoot.querySelector(".table-wrap")?.scrollTop || 0;
  const errorScrollTop = appRoot.querySelector(".error-panel")?.scrollTop || 0;
  const total = counts();
  const percent = total.total ? Math.round((total.done / total.total) * 100) : 0;
  const actionLabel = state.running ? (state.cancelling ? "正在取消…" : "取消解析") : ["done", "partial_failed", "failed", "cancelled"].includes(state.status) ? "再次解析" : "开始解析";
  const actionIcon = state.running ? "stop" : "arrow";
  const actionDisabled = !state.inputPaths.length || !state.outputDir || state.cancelling ? " disabled" : "";
  const runningNote = state.cancelling ? "正在取消，已完成结果会保留" : "正在解析，请稍候";
  appRoot.innerHTML = `<div class="app"><aside class="rail"><div class="brand"><span class="brand-mark">${icon("mail")}</span>邮件解析</div><div class="rail-nav">${icon("folder")}解析工作台</div>${renderSettings()}</aside><main class="main"><section class="drop-strip" data-drop>${icon("upload")}<div class="grow"><h3>拖入 EML 或 MSG 邮件及文件夹</h3></div><button data-action="files">${icon("plus")}添加邮件</button><button data-action="folder">${icon("folder")}选择文件夹</button></section><div class="toolbar"><div class="filters"><button class="filter ${state.filter === "all" ? "active" : ""}" data-filter="all">全部 <span class="count">${total.total}</span></button><button class="filter ${state.filter === "success" ? "active" : ""}" data-filter="success">已完成 <span class="count">${total.success}</span></button><button class="filter ${state.filter === "partial_failed" ? "active" : ""}" data-filter="partial_failed">部分失败 <span class="count">${total.partial_failed}</span></button><button class="filter ${state.filter === "failed" ? "active" : ""}" data-filter="failed">失败 <span class="count">${total.failed}</span></button></div><button class="quiet" data-action="errors" ${state.errors.length ? "" : "disabled"}>错误清单 <span class="count">${state.errors.length}</span></button></div>${renderTable()}${state.errors.length && state.errorsVisible ? renderErrors() : ""}<footer class="queue-foot">${icon("folder")}<div class="path"><strong>保存到</strong><span>${state.outputDir ? escapeHtml(state.outputDir) : "尚未选择输出目录"}</span></div><button class="quiet" data-action="output">更改</button><div class="grow"></div><button class="primary" data-action="${state.running ? "cancel" : "start"}"${actionDisabled}>${icon(actionIcon)}${actionLabel}</button></footer><div class="progress" role="progressbar" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"><i style="transform:scaleX(${percent / 100})"></i></div><div class="flex between note"><span>${state.running ? runningNote : state.status === "done" ? "本批次解析结束" : state.status === "partial_failed" ? "批次完成，部分邮件需要处理" : state.status === "failed" ? "批次失败，错误邮件可重试" : state.status === "cancelled" ? "已取消，已完成结果已保留" : "准备就绪"}${state.skippedFiles.length ? `；已跳过 ${state.skippedFiles.length} 个独立文件` : ""}</span><span class="mono">${total.done} / ${total.total} 封</span></div></main></div>`;
  appRoot.querySelector(".table-wrap").scrollTop = tableScrollTop;
  const errorPanel = appRoot.querySelector(".error-panel");
  if (errorPanel) errorPanel.scrollTop = errorScrollTop;
}

async function chooseOutput() {
  try {
    const selected = await invoke("choose_output_dir");
    if (selected) {
      state.outputDir = selected;
      render();
    }
  } catch (error) {
    notify(errorMessage(error, "无法选择输出目录。"));
  }
}

async function inspectDroppedPaths(paths) {
  if (!Array.isArray(paths) || !paths.length) return;
  try {
    addSelectedFiles(await invoke("inspect_input_paths", { paths }));
  } catch (error) {
    notify(errorMessage(error, "无法读取拖入的 EML 或 MSG。"));
  }
}

async function setupNativeDrop() {
  const eventApi = window.__TAURI__?.event;
  if (!eventApi || typeof eventApi.listen !== "function") return;
  try {
    await eventApi.listen("tauri://drag-drop", (event) => {
      const payload = event?.payload;
      const paths = payload?.paths;
      inspectDroppedPaths(paths);
    });
  } catch {
    // Native file pickers remain available if a platform does not expose drag events.
  }
}

async function startBatch() {
  for (const input of appRoot.querySelectorAll("[data-setting]")) {
    if (!input.checkValidity()) {
      state.settingsOpen = true;
      appRoot.querySelector("#settings-fields").hidden = false;
      appRoot.querySelector("#settings-title").setAttribute("aria-expanded", "true");
      input.reportValidity();
      return;
    }
  }
  if (!state.inputPaths.length) return notify("请先添加 EML、MSG 或输入文件夹。");
  if (!state.outputDir) return notify("请先选择输出目录。");
  const missing = state.items.find((item) => !item.path || !/^([A-Za-z]:[\\/]|\\\\|\/)/.test(item.path));
  if (missing) return notify("无法取得源文件路径，请从桌面应用中重新选择 EML 或 MSG。");
  state.batchId = makeId("batch");
  state.requestId = makeId("request");
  state.running = true;
  state.cancelling = false;
  state.status = "running";
  state.errors = [];
  state.errorsVisible = false;
  state.skippedFiles = [];
  state.items.forEach((item) => { item.status = "pending"; item.error = null; item.mailDir = null; item.markdownPath = null; });
  render();
  try {
    await invoke("start_batch", { request: { batch_id: state.batchId, request_id: state.requestId, inputs: state.inputPaths, output_dir: state.outputDir, limits: { max_depth: Number(state.maxDepth), max_extract_bytes: Number(state.maxExtractMiB) * 1024 * 1024 } } });
    window.clearInterval(state.pollTimer);
    state.pollTimer = window.setInterval(pollEvents, 120);
    await pollEvents();
  } catch (error) {
    state.running = false;
    state.cancelling = false;
    state.status = "failed";
    notify(errorMessage(error, "启动解析失败。"));
    render();
  }
}

async function pollEvents() {
  if (!state.batchId) return;
  try {
    const events = await invoke("drain_batch_events", { batchId: state.batchId });
    for (const event of events || []) applyEvent(event);
  } catch (error) {
    window.clearInterval(state.pollTimer);
    state.running = false;
    state.status = "failed";
    notify(errorMessage(error, "读取解析进度失败。"));
    render();
  }
}

function applyEvent(event) {
  if (!event || event.batch_id !== state.batchId) return;
  if (event.type === "batch_rejected") {
    window.clearInterval(state.pollTimer);
    state.running = false;
    state.cancelling = false;
    state.status = "failed";
    addError(event, { name: "批次配置", message: event.error || "批次配置无效" });
    notify(event.error || "批次配置无效。");
    render();
    return;
  }
  if (event.type === "batch_started" && Array.isArray(event.mail_paths)) {
    const discovered = new Set(event.mail_paths);
    state.items = state.items.filter((candidate) => discovered.has(candidate.path));
  }
  const item = event.source_path
    ? state.items.find((candidate) => candidate.path === event.source_path)
    : Number.isInteger(event.index) ? state.items[event.index] : null;
  if (event.type === "item_started" && item) item.status = "running";
  if (event.type === "item_succeeded" && item) {
    item.status = event.status === "skipped" ? "skipped" : "success";
    item.mailDir = event.mail_dir || null;
    item.markdownPath = event.markdown_path || null;
  }
  if (event.type === "item_partial_failed" && item) {
    item.status = "partial_failed";
    item.error = event.errors?.[0]?.error || event.error || "部分解析失败";
    item.mailDir = event.mail_dir || null;
    item.markdownPath = event.markdown_path || null;
    const errors = Array.isArray(event.errors) && event.errors.length ? event.errors : [event];
    for (const error of errors) {
      addError(error, { name: item.name, source_path: item.path, message: "部分解析失败" });
    }
  }
  if (event.type === "item_failed" && item) {
    item.status = "failed";
    item.error = event.error || "解析失败";
    addError(event, { name: item.name, source_path: item.path, message: item.error });
  }
  if (event.type === "input_failed") {
    addError(event, { name: event.source_path || "输入扫描", message: "输入扫描失败" });
  }
  if (event.type === "bridge_error") {
    addError(event, { name: "sidecar 桥接", message: "sidecar 通信失败" });
  }
  if (event.type === "cancel_requested") state.cancelling = true;
  if (event.type === "input_skipped") {
    addSkippedFile({ path: event.source_path, reason: event.error || "已跳过独立非邮件文件" });
  }
  if (event.type === "item_cancelled" && item) item.status = "cancelled";
  if (event.type === "batch_completed") {
    for (const error of Array.isArray(event.errors) ? event.errors : []) {
      addError(error);
    }
    const terminalStatus = event.status === "cancelled"
      ? "cancelled"
      : event.status === "failed"
        ? "failed"
        : null;
    if (terminalStatus) {
      const fallbackError = Array.isArray(event.errors)
        ? event.errors.find((error) => error && typeof error.error === "string")
        : null;
      for (const pendingItem of state.items) {
        if (pendingItem.status !== "pending" && pendingItem.status !== "running") continue;
        pendingItem.status = terminalStatus;
        if (terminalStatus === "failed") {
          pendingItem.error = fallbackError?.error || "批次提前结束，未完成邮件解析失败";
        }
      }
    }
    window.clearInterval(state.pollTimer);
    state.running = false;
    state.cancelling = false;
    state.status = event.status === "cancelled"
      ? "cancelled"
      : event.status === "failed"
        ? "failed"
        : event.status === "partial_failed"
          ? "partial_failed"
          : "done";
    state.summary = event.summary || null;
  }
  render();
}

async function cancelBatch() {
  if (!state.batchId || !state.running || state.cancelling) return;
  state.cancelling = true;
  render();
  try {
    await invoke("cancel_batch", { batchId: state.batchId });
    notify("已请求取消，正在保留已完成结果。");
  } catch (error) {
    state.cancelling = false;
    notify(errorMessage(error, "无法取消当前批次。"));
    render();
  }
}

async function openResult(path) {
  try {
    await invoke("open_result", { path });
  } catch (error) {
    notify(errorMessage(error, "无法打开结果。"));
  }
}

document.addEventListener("click", (event) => {
  const filter = event.target.closest("[data-filter]");
  if (filter) {
    state.filter = filter.dataset.filter;
    render();
    appRoot.querySelector(".table-wrap").scrollTop = 0;
    return;
  }
  const action = event.target.closest("[data-action]");
  if (!action) return;
  const name = action.dataset.action;
  if (name === "settings") {
    state.settingsOpen = !state.settingsOpen;
    action.setAttribute("aria-expanded", String(state.settingsOpen));
    appRoot.querySelector("#settings-fields").hidden = !state.settingsOpen;
  }
  if (name === "files") chooseInputFiles();
  if (name === "folder") chooseInputFolder();
  if (name === "output") chooseOutput();
  if (name === "errors") { state.errorsVisible = !state.errorsVisible; render(); }
  if (name === "start") startBatch();
  if (name === "cancel") cancelBatch();
  if (name === "open") openResult(action.dataset.path);
});

document.addEventListener("input", (event) => {
  const input = event.target.closest("[data-setting]");
  if (!input || state.running) return;
  state[input.dataset.setting] = input.value;
});

document.addEventListener("dragover", (event) => {
  event.preventDefault();
  const drop = event.target.closest("[data-drop]");
  if (drop) drop.classList.add("drop-active");
});
document.addEventListener("dragleave", (event) => {
  const drop = event.target.closest("[data-drop]");
  if (drop) drop.classList.remove("drop-active");
});
document.addEventListener("drop", (event) => {
  event.preventDefault();
  document.querySelectorAll(".drop-active").forEach((element) => element.classList.remove("drop-active"));
  const paths = Array.from(event.dataTransfer?.files || [])
    .map((file) => file.path)
    .filter((path) => typeof path === "string" && path);
  if (paths.length) inspectDroppedPaths(paths);
});

state.errorsVisible = false;
render();
setupNativeDrop();
