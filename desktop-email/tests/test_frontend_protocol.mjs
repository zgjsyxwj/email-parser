import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const appSource = readFileSync(
  fileURLToPath(new URL("../frontend/app.js", import.meta.url)),
  "utf8",
);

function loadApp() {
  const table = { scrollTop: 0 };
  const appRoot = {
    innerHTML: "",
    querySelector(selector) {
      return selector === ".table-wrap" ? table : null;
    },
  };
  const toast = { hidden: true, textContent: "" };
  const document = {
    getElementById(id) {
      return id === "app" ? appRoot : toast;
    },
    addEventListener() {},
    querySelectorAll() {
      return [];
    },
  };
  const window = {
    __TAURI__: undefined,
    clearInterval,
    clearTimeout,
    setInterval,
    setTimeout,
  };
  const context = {
    console,
    Date,
    Math,
    document,
    window,
    clearInterval,
    clearTimeout,
    setInterval,
    setTimeout,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${appSource}\nglobalThis.__protocolTest = { state, applyEvent, counts };`,
    context,
    { filename: "frontend/app.js" },
  );
  return context.__protocolTest;
}

function item(path, status) {
  return {
    id: path,
    path,
    name: path.split("/").at(-1),
    size: "1 KB",
    status,
    mailDir: null,
    markdownPath: null,
    error: null,
  };
}

function batchStarted(batchId, paths) {
  return {
    type: "batch_started",
    batch_id: batchId,
    request_id: "request",
    total: paths.length,
    mail_paths: paths,
    skipped_files: 0,
  };
}

test("batch_completed cancelled 收敛仍处于 pending/running 的邮件", () => {
  const { state, applyEvent, counts } = loadApp();
  const paths = ["/tmp/complete.eml", "/tmp/running.eml", "/tmp/pending.eml"];
  state.batchId = "cancel-batch";
  state.items = [item(paths[0], "success"), item(paths[1], "running"), item(paths[2], "pending")];
  state.running = true;

  applyEvent(batchStarted(state.batchId, paths));
  applyEvent({
    type: "item_started",
    batch_id: state.batchId,
    request_id: "request",
    index: 1,
    source_path: paths[1],
    status: "running",
  });
  applyEvent({
    type: "item_cancelled",
    batch_id: state.batchId,
    request_id: "request",
    index: 1,
    source_path: paths[1],
    status: "cancelled",
  });
  applyEvent({
    type: "batch_completed",
    batch_id: state.batchId,
    request_id: "request",
    status: "cancelled",
    summary: { total: 3, succeeded: 1, partial_failed: 0, skipped: 0, failed: 0, cancelled: 2, skipped_files: 0 },
    errors: [],
  });

  assert.deepEqual(state.items.map(({ status }) => status), ["success", "cancelled", "cancelled"]);
  assert.equal(state.running, false);
  assert.equal(state.cancelling, false);
  assert.equal(state.status, "cancelled");
  assert.equal(counts().done, 3);
});

test("batch_completed failed 为未完成邮件设置失败终态并保留批次错误", () => {
  const { state, applyEvent, counts } = loadApp();
  const paths = ["/tmp/complete.eml", "/tmp/running.eml", "/tmp/pending.eml"];
  state.batchId = "failed-batch";
  state.items = [item(paths[0], "success"), item(paths[1], "running"), item(paths[2], "pending")];
  state.running = true;

  applyEvent(batchStarted(state.batchId, paths));
  applyEvent({
    type: "item_started",
    batch_id: state.batchId,
    request_id: "request",
    index: 1,
    source_path: paths[1],
    status: "running",
  });
  applyEvent({
    type: "item_failed",
    batch_id: state.batchId,
    request_id: "request",
    index: 1,
    source_path: paths[1],
    status: "failed",
    stage: "parse",
    code: "invalid_eml",
    error: "邮件格式无效",
    location: "source",
  });
  applyEvent({
    type: "bridge_error",
    batch_id: state.batchId,
    request_id: "request",
    stage: "sidecar",
    code: "unexpected_eof",
    error: "sidecar stdout unexpectedly closed",
    location: "sidecar",
  });
  applyEvent({
    type: "batch_completed",
    batch_id: state.batchId,
    request_id: "request",
    status: "failed",
    summary: { total: 3, succeeded: 1, partial_failed: 0, skipped: 0, failed: 2, cancelled: 0, skipped_files: 0 },
    errors: [{ stage: "sidecar", code: "unexpected_eof", error: "sidecar stdout unexpectedly closed", location: "sidecar" }],
  });

  assert.deepEqual(state.items.map(({ status }) => status), ["success", "failed", "failed"]);
  assert.equal(state.items[1].error, "邮件格式无效");
  assert.equal(state.items[2].error, "sidecar stdout unexpectedly closed");
  assert.equal(state.running, false);
  assert.equal(state.status, "failed");
  assert.equal(counts().done, 3);
  assert.equal(state.errors.length, 2);
});
