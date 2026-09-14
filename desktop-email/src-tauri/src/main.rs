#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, State};

const SIDECAR_EVENT: &str = "sidecar-event";
const DEFAULT_MAX_DEPTH: u32 = 10;
const DEFAULT_MAX_EXTRACT_BYTES: u64 = 500 * 1024 * 1024;
const MAX_ALLOWED_DEPTH: u32 = 100;
const MAX_ALLOWED_EXTRACT_BYTES: u64 = 10 * 1024 * 1024 * 1024;
const CANCEL_GRACE_PERIOD: Duration = Duration::from_secs(5);
const CANCEL_POLL_INTERVAL: Duration = Duration::from_millis(50);
const PROCESS_REAP_GRACE: Duration = Duration::from_secs(1);

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(rename_all = "snake_case")]
struct ExtractionLimits {
    max_depth: u32,
    max_extract_bytes: u64,
}

impl Default for ExtractionLimits {
    fn default() -> Self {
        Self {
            max_depth: DEFAULT_MAX_DEPTH,
            max_extract_bytes: DEFAULT_MAX_EXTRACT_BYTES,
        }
    }
}

impl ExtractionLimits {
    fn validate(&self) -> Result<(), String> {
        if self.max_depth > MAX_ALLOWED_DEPTH {
            return Err(format!(
                "最大嵌套深度必须在 0 至 {MAX_ALLOWED_DEPTH} 之间"
            ));
        }
        if self.max_extract_bytes > MAX_ALLOWED_EXTRACT_BYTES {
            return Err(format!(
                "最大提取字节数必须在 0 至 {MAX_ALLOWED_EXTRACT_BYTES} 之间"
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(rename_all = "snake_case")]
struct BatchRequest {
    batch_id: String,
    request_id: String,
    inputs: Vec<String>,
    output_dir: String,
    #[serde(default)]
    limits: ExtractionLimits,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
struct CommandAccepted {
    batch_id: String,
    request_id: String,
    status: &'static str,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "snake_case")]
struct SelectedFile {
    path: String,
    name: String,
    size: u64,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "snake_case")]
struct SkippedFile {
    path: String,
    reason: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "snake_case")]
struct InputSelection {
    files: Vec<SelectedFile>,
    skipped_files: Vec<SkippedFile>,
    inputs: Vec<String>,
}

struct SidecarProcess {
    batch_id: String,
    stdin: ChildStdin,
    child: Child,
    cancel_requested: bool,
    cancel_watchdog_started: bool,
    force_killed: bool,
}

#[derive(Default)]
struct AppState {
    process: Arc<Mutex<Option<SidecarProcess>>>,
    events: Arc<Mutex<Vec<Value>>>,
}

fn absolute(path: impl AsRef<Path>) -> PathBuf {
    let path = path.as_ref();
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

fn selected_file(path: PathBuf) -> Result<SelectedFile, String> {
    let path =
        fs::canonicalize(absolute(path)).map_err(|error| format!("读取输入文件失败：{error}"))?;
    let metadata = fs::metadata(&path).map_err(|error| format!("读取输入文件信息失败：{error}"))?;
    if !metadata.is_file() {
        return Err(format!("输入路径不是文件：{}", path.display()));
    }
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| format!("输入文件名无法解码：{}", path.display()))?;
    Ok(SelectedFile {
        path: path.to_string_lossy().into_owned(),
        name: name.to_string(),
        size: metadata.len(),
    })
}

fn canonical_input(path: PathBuf) -> Result<PathBuf, String> {
    let path = absolute(path);
    fs::canonicalize(&path)
        .map_err(|error| format!("读取输入路径失败：{}：{error}", path.display()))
}

fn push_unique_input(inputs: &mut Vec<String>, path: &Path) {
    let value = path.to_string_lossy().into_owned();
    if !inputs.iter().any(|existing| existing == &value) {
        inputs.push(value);
    }
}

fn is_email_path(path: &Path) -> bool {
    path.extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("eml") || value.eq_ignore_ascii_case("msg"))
}

fn collect_input_files(
    root: &Path,
    files: &mut Vec<PathBuf>,
    skipped_files: &mut Vec<SkippedFile>,
) -> Result<(), String> {
    let entries = fs::read_dir(root)
        .map_err(|error| format!("扫描邮件目录失败：{}：{error}", root.display()))?;
    for entry in entries {
        let entry = entry.map_err(|error| format!("读取邮件目录项失败：{error}"))?;
        let path = entry.path();
        let file_type = entry
            .file_type()
            .map_err(|error| format!("读取输入类型失败：{}：{error}", path.display()))?;
        if file_type.is_dir() {
            collect_input_files(&path, files, skipped_files)?;
        } else if file_type.is_file() {
            if is_email_path(&path) {
                files.push(path);
            } else {
                skipped_files.push(SkippedFile {
                    path: path.to_string_lossy().into_owned(),
                    reason: "已跳过独立非邮件文件".to_string(),
                });
            }
        }
    }
    Ok(())
}

#[cfg(debug_assertions)]
fn dev_sidecar() -> PathBuf {
    absolute(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../sidecar/main.py"))
}

#[cfg(debug_assertions)]
fn sidecar_python() -> String {
    if let Ok(value) = std::env::var("EMAIL_SIDECAR_PYTHON") {
        if !value.trim().is_empty() {
            return value;
        }
    }
    let candidate = if cfg!(windows) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/Scripts/python.exe")
    } else {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.venv/bin/python")
    };
    if candidate.is_file() {
        return candidate.to_string_lossy().into_owned();
    }
    "python3".to_string()
}

fn bundled_sidecar(app: &AppHandle) -> Option<PathBuf> {
    let resource_dir = app.path().resource_dir().ok()?;
    let executable = if cfg!(windows) {
        resource_dir.join("sidecar/email-sidecar.exe")
    } else {
        resource_dir.join("sidecar/email-sidecar")
    };
    executable.is_file().then_some(executable)
}

fn configure_sidecar_command(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;

        // Keep the sidecar's console-based JSONL stdin/stdout contract while
        // preventing a console window from flashing for desktop users.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = command;
}

fn sidecar_command(app: &AppHandle) -> Result<Command, String> {
    #[cfg(debug_assertions)]
    {
        if let Ok(value) = std::env::var("EMAIL_SIDECAR_PATH") {
            let executable = absolute(value);
            if executable.extension().and_then(|value| value.to_str()) == Some("py") {
                let python = sidecar_python();
                let mut command = Command::new(python);
                command.arg(executable).arg("--stdio");
                configure_sidecar_command(&mut command);
                return Ok(command);
            }
            let mut command = Command::new(executable);
            command.arg("--stdio");
            configure_sidecar_command(&mut command);
            return Ok(command);
        }
        if let Some(path) = bundled_sidecar(app) {
            let mut command = Command::new(path);
            command.arg("--stdio");
            configure_sidecar_command(&mut command);
            return Ok(command);
        }
        let script = dev_sidecar();
        if script.is_file() {
            let python = sidecar_python();
            let mut command = Command::new(python);
            command.arg(script).arg("--stdio");
            configure_sidecar_command(&mut command);
            return Ok(command);
        }
    }

    #[cfg(not(debug_assertions))]
    {
        if let Some(path) = bundled_sidecar(app) {
            let mut command = Command::new(path);
            command.arg("--stdio");
            configure_sidecar_command(&mut command);
            return Ok(command);
        }
    }

    Err("找不到随应用安装的 sidecar。请重新运行发行构建并安装完整应用。".to_string())
}

fn write_command(stdin: &mut ChildStdin, command: &Value) -> Result<(), String> {
    let encoded =
        serde_json::to_vec(command).map_err(|error| format!("编码 sidecar 命令失败：{error}"))?;
    stdin
        .write_all(&encoded)
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|error| format!("发送 sidecar 命令失败：{error}"))
}

fn stop_child(child: &mut Child) {
    let _ = child.kill();
    let deadline = Instant::now() + PROCESS_REAP_GRACE;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if Instant::now() >= deadline => return,
            Ok(None) => std::thread::sleep(CANCEL_POLL_INTERVAL),
            Err(_) => return,
        }
    }
}

fn record_event<R: tauri::Runtime>(app: &AppHandle<R>, events: &Arc<Mutex<Vec<Value>>>, event: Value) {
    if let Ok(mut buffer) = events.lock() {
        buffer.push(event.clone());
    }
    let _ = app.emit(SIDECAR_EVENT, event);
}

fn emit_bridge_error<R: tauri::Runtime>(
    app: &AppHandle<R>,
    events: &Arc<Mutex<Vec<Value>>>,
    batch_id: &str,
    request_id: &str,
    code: &str,
    message: String,
) {
    record_event(
        app,
        events,
        json!({
            "protocol_version": 1,
            "type": "bridge_error",
            "batch_id": batch_id,
            "request_id": request_id,
            "stage": "sidecar",
            "code": code,
            "error": message,
            "location": "sidecar",
        }),
    );
}

fn clear_process(state: &Arc<Mutex<Option<SidecarProcess>>>, batch_id: &str) {
    let process = {
        let mut guard = match state.lock() {
            Ok(value) => value,
            Err(_) => return,
        };
        if guard
            .as_ref()
            .is_some_and(|process| process.batch_id == batch_id)
        {
            guard.take()
        } else {
            None
        }
    };
    if let Some(process) = process {
        reap_process(process);
    }
}

fn reap_process(process: SidecarProcess) {
    let SidecarProcess {
        stdin, mut child, ..
    } = process;
    drop(stdin);
    let deadline = Instant::now() + PROCESS_REAP_GRACE;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if Instant::now() >= deadline => {
                stop_child(&mut child);
                return;
            }
            Ok(None) => std::thread::sleep(CANCEL_POLL_INTERVAL),
            Err(_) => {
                stop_child(&mut child);
                return;
            }
        }
    }
}

fn process_cancel_status(
    state: &Arc<Mutex<Option<SidecarProcess>>>,
    batch_id: &str,
) -> (bool, bool) {
    let guard = match state.lock() {
        Ok(value) => value,
        Err(_) => return (false, false),
    };
    guard
        .as_ref()
        .filter(|process| process.batch_id == batch_id)
        .map(|process| (process.cancel_requested, process.force_killed))
        .unwrap_or((false, false))
}

fn start_cancel_watchdog(state: Arc<Mutex<Option<SidecarProcess>>>, batch_id: String) {
    std::thread::spawn(move || {
        let deadline = Instant::now() + CANCEL_GRACE_PERIOD;
        loop {
            std::thread::sleep(CANCEL_POLL_INTERVAL);
            let mut guard = match state.lock() {
                Ok(value) => value,
                Err(_) => return,
            };
            let Some(process) = guard.as_mut() else {
                return;
            };
            if process.batch_id != batch_id || !process.cancel_requested {
                return;
            }
            match process.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) if Instant::now() >= deadline => {
                    process.force_killed = true;
                    let _ = process.child.kill();
                    return;
                }
                Ok(None) => {}
                Err(_) => {
                    // Keep the process alive until the deadline. The stdout
                    // reader will report the concrete exit/read failure.
                }
            }
        }
    });
}

fn drain_stderr(stderr: impl std::io::Read + Send + 'static) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stderr);
        let mut sink = String::new();
        while reader.read_line(&mut sink).is_ok() {
            if sink.is_empty() {
                break;
            }
            sink.clear();
        }
    });
}

fn forward_events<R: tauri::Runtime + 'static>(
    app: AppHandle<R>,
    state: Arc<Mutex<Option<SidecarProcess>>>,
    events: Arc<Mutex<Vec<Value>>>,
    batch_id: String,
    request_id: String,
    stdout: impl std::io::Read + Send + 'static,
) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        let mut completed = false;
        let mut terminal_error = "sidecar stdout unexpectedly closed".to_string();
        let mut total = 0u64;
        let mut succeeded = 0u64;
        let mut partial_failed = 0u64;
        let mut skipped = 0u64;
        let mut failed = 0u64;
        let mut cancelled = 0u64;
        let mut skipped_files = 0u64;
        let mut mail_paths: Vec<String> = Vec::new();
        let mut terminal_items: Vec<bool> = Vec::new();
        for line in reader.lines() {
            match line {
                Ok(line) if line.trim().is_empty() => {}
                Ok(line) => match serde_json::from_str::<Value>(&line) {
                    Ok(event) => {
                        match event.get("type").and_then(Value::as_str) {
                            Some("batch_started") => {
                                total = event
                                    .get("total")
                                    .and_then(Value::as_u64)
                                    .unwrap_or(0);
                                mail_paths = event
                                    .get("mail_paths")
                                    .and_then(Value::as_array)
                                    .map(|paths| {
                                        paths
                                            .iter()
                                            .filter_map(Value::as_str)
                                            .map(str::to_string)
                                            .collect()
                                    })
                                    .unwrap_or_default();
                                let item_slots = usize::try_from(total).unwrap_or(mail_paths.len());
                                terminal_items = vec![false; item_slots.max(mail_paths.len())];
                                skipped_files = event
                                    .get("skipped_files")
                                    .and_then(Value::as_u64)
                                    .unwrap_or(0);
                            }
                            Some("item_succeeded") => {
                                if event.get("status").and_then(Value::as_str) == Some("skipped") {
                                    skipped += 1;
                                } else {
                                    succeeded += 1;
                                }
                            }
                            Some("item_partial_failed") => partial_failed += 1,
                            Some("item_failed") => failed += 1,
                            Some("item_cancelled") => cancelled += 1,
                            Some("input_failed") => failed += 1,
                            _ => {}
                        }
                        if matches!(
                            event.get("type").and_then(Value::as_str),
                            Some("item_succeeded")
                                | Some("item_partial_failed")
                                | Some("item_failed")
                                | Some("item_cancelled")
                        ) {
                            let index = event
                                .get("index")
                                .and_then(Value::as_u64)
                                .and_then(|value| usize::try_from(value).ok())
                                .filter(|index| *index < terminal_items.len())
                                .or_else(|| {
                                    event
                                        .get("source_path")
                                        .and_then(Value::as_str)
                                        .and_then(|source| {
                                            mail_paths.iter().position(|path| path == source)
                                        })
                                });
                            if let Some(index) = index {
                                terminal_items[index] = true;
                            }
                        }
                        let is_completed =
                            event.get("type").and_then(Value::as_str) == Some("batch_completed");
                        record_event(&app, &events, event);
                        if is_completed {
                            completed = true;
                            clear_process(&state, &batch_id);
                            break;
                        }
                    }
                    Err(error) => {
                        terminal_error = format!("sidecar 输出不是 JSON：{error}");
                        emit_bridge_error(
                            &app,
                            &events,
                            &batch_id,
                            &request_id,
                            "invalid_json",
                            terminal_error.clone(),
                        );
                    }
                },
                Err(error) => {
                    terminal_error = format!("读取 sidecar 输出失败：{error}");
                    emit_bridge_error(
                        &app,
                        &events,
                        &batch_id,
                        &request_id,
                        "stdout_read_error",
                        terminal_error.clone(),
                    );
                    break;
                }
            }
        }
        if !completed {
            let (cancel_requested, force_killed) = process_cancel_status(&state, &batch_id);
            let status = if cancel_requested { "cancelled" } else { "failed" };
            let error_code = if force_killed {
                "cancel_timeout"
            } else if cancel_requested {
                "cancelled_eof"
            } else {
                "unexpected_eof"
            };
            if force_killed {
                terminal_error = format!(
                    "sidecar 在取消请求后超过 {} 秒仍未退出，已强制终止",
                    CANCEL_GRACE_PERIOD.as_secs()
                );
            } else if cancel_requested {
                terminal_error = "sidecar 在取消后提前退出，未发布批次终态".to_string();
            }
            let item_slots = usize::try_from(total).unwrap_or(mail_paths.len());
            let item_slots = item_slots.max(mail_paths.len());
            let item_status = if cancel_requested {
                "item_cancelled"
            } else {
                "item_failed"
            };
            for index in 0..item_slots {
                if terminal_items.get(index).copied().unwrap_or(false) {
                    continue;
                }
                let mut item_event = json!({
                    "protocol_version": 1,
                    "type": item_status,
                    "batch_id": batch_id,
                    "request_id": request_id,
                    "index": index,
                    "status": if cancel_requested { "cancelled" } else { "failed" },
                });
                if let Some(source_path) = mail_paths.get(index) {
                    item_event["source_path"] = Value::String(source_path.clone());
                } else {
                    item_event["source_path"] = Value::Null;
                }
                if !cancel_requested {
                    item_event["stage"] = Value::String("sidecar".to_string());
                    item_event["code"] = Value::String(error_code.to_string());
                    item_event["error"] = Value::String(terminal_error.clone());
                    item_event["location"] = Value::String("sidecar".to_string());
                    failed += 1;
                } else {
                    cancelled += 1;
                }
                record_event(&app, &events, item_event);
            }
            if !cancel_requested && item_slots == 0 {
                // There is no input item to attach the unexpected EOF to.
                // Keep a batch-level failure visible in the fixed summary.
                failed += 1;
            }
            emit_bridge_error(
                &app,
                &events,
                &batch_id,
                &request_id,
                error_code,
                terminal_error.clone(),
            );
            record_event(
                &app,
                &events,
                json!({
                    "protocol_version": 1,
                    "type": "batch_completed",
                    "batch_id": batch_id,
                    "request_id": request_id,
                    "status": status,
                    "summary": {
                        "total": total,
                        "succeeded": succeeded,
                        "partial_failed": partial_failed,
                        "skipped": skipped,
                        "failed": failed,
                        "cancelled": cancelled,
                        "skipped_files": skipped_files,
                    },
                    "errors": [{
                        "source_path": null,
                        "stage": "sidecar",
                        "code": error_code,
                        "error": terminal_error,
                        "location": "sidecar",
                    }],
                }),
            );
        }
        clear_process(&state, &batch_id);
    });
}

#[tauri::command]
fn start_batch(
    app: AppHandle,
    state: State<'_, AppState>,
    request: BatchRequest,
) -> Result<CommandAccepted, String> {
    if request.batch_id.trim().is_empty() || request.request_id.trim().is_empty() {
        return Err("batch_id 和 request_id 不能为空".to_string());
    }
    if request.inputs.is_empty() {
        return Err("至少需要选择一封 EML 或 MSG".to_string());
    }
    if request.output_dir.trim().is_empty() {
        return Err("请选择输出目录".to_string());
    }
    request.limits.validate()?;

    let process_state = state.inner().process.clone();
    let event_buffer = state.inner().events.clone();
    let state_arc = process_state
        .lock()
        .map_err(|_| "读取 sidecar 状态失败".to_string())?;
    if state_arc.is_some() {
        return Err("已有批次正在处理".to_string());
    }
    drop(state_arc);

    let mut command = sidecar_command(&app)?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command
        .spawn()
        .map_err(|error| format!("启动 Python sidecar 失败：{error}"))?;
    let mut stdin = match child.stdin.take() {
        Some(value) => value,
        None => {
            stop_child(&mut child);
            return Err("sidecar stdin 不可用".to_string());
        }
    };
    let stdout = match child.stdout.take() {
        Some(value) => value,
        None => {
            stop_child(&mut child);
            return Err("sidecar stdout 不可用".to_string());
        }
    };
    let stderr = match child.stderr.take() {
        Some(value) => value,
        None => {
            stop_child(&mut child);
            return Err("sidecar stderr 不可用".to_string());
        }
    };
    drain_stderr(stderr);
    let command_payload = json!({
        "type": "start",
        "batch_id": request.batch_id,
        "request_id": request.request_id,
        "inputs": request.inputs,
        "output_dir": request.output_dir,
        "limits": request.limits,
    });
    if let Err(error) = write_command(&mut stdin, &command_payload) {
        stop_child(&mut child);
        return Err(error);
    }

    let batch_id = command_payload["batch_id"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let request_id = command_payload["request_id"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let mut guard = match process_state.lock() {
        Ok(value) => value,
        Err(_) => {
            stop_child(&mut child);
            return Err("写入 sidecar 状态失败".to_string());
        }
    };
    if guard.is_some() {
        stop_child(&mut child);
        return Err("已有批次正在处理".to_string());
    }
    *guard = Some(SidecarProcess {
        batch_id: batch_id.clone(),
        stdin,
        child,
        cancel_requested: false,
        cancel_watchdog_started: false,
        force_killed: false,
    });
    drop(guard);
    forward_events(app, process_state, event_buffer, batch_id.clone(), request_id.clone(), stdout);

    Ok(CommandAccepted {
        batch_id,
        request_id,
        status: "accepted",
    })
}

#[tauri::command]
fn choose_input_files() -> Result<InputSelection, String> {
    let paths = rfd::FileDialog::new()
        .set_title("选择 EML 或 MSG 邮件")
        .add_filter("邮件", &["eml", "msg"])
        .pick_files()
        .unwrap_or_default();
    let files = paths.into_iter().map(selected_file).collect::<Result<Vec<_>, _>>()?;
    let inputs = files.iter().map(|file| file.path.clone()).collect();
    Ok(InputSelection {
        files,
        skipped_files: Vec::new(),
        inputs,
    })
}

#[tauri::command]
fn choose_input_folder() -> Result<InputSelection, String> {
    let Some(root) = rfd::FileDialog::new()
        .set_title("选择 EML 或 MSG 邮件目录")
        .pick_folder()
    else {
        return Ok(InputSelection {
            files: Vec::new(),
            skipped_files: Vec::new(),
            inputs: Vec::new(),
        });
    };
    let root = canonical_input(root)?;
    let mut paths = Vec::new();
    let mut skipped_files = Vec::new();
    collect_input_files(&root, &mut paths, &mut skipped_files)?;
    paths.sort();
    skipped_files.sort_by(|left, right| left.path.cmp(&right.path));
    skipped_files.dedup_by(|left, right| left.path == right.path);
    let files = paths.into_iter().map(selected_file).collect::<Result<Vec<_>, _>>()?;
    Ok(InputSelection {
        files,
        skipped_files,
        inputs: vec![root.to_string_lossy().into_owned()],
    })
}

#[tauri::command]
fn inspect_input_paths(paths: Vec<String>) -> Result<InputSelection, String> {
    let mut files = Vec::new();
    let mut skipped_files = Vec::new();
    let mut inputs = Vec::new();
    for value in paths {
        let path = canonical_input(PathBuf::from(value))?;
        push_unique_input(&mut inputs, &path);
        if path.is_dir() {
            collect_input_files(&path, &mut files, &mut skipped_files)?;
        } else if is_email_path(&path) {
            files.push(path);
        } else {
            skipped_files.push(SkippedFile {
                path: path.to_string_lossy().into_owned(),
                reason: "已跳过独立非邮件文件".to_string(),
            });
        }
    }
    files.sort();
    files.dedup();
    skipped_files.sort_by(|left, right| left.path.cmp(&right.path));
    skipped_files.dedup_by(|left, right| left.path == right.path);
    let files = files.into_iter().map(selected_file).collect::<Result<Vec<_>, _>>()?;
    Ok(InputSelection {
        files,
        skipped_files,
        inputs,
    })
}

#[tauri::command]
fn cancel_batch(
    state: State<'_, AppState>,
    batch_id: String,
) -> Result<(), String> {
    let mut guard = state
        .process
        .lock()
        .map_err(|_| "读取 sidecar 状态失败".to_string())?;
    let process = guard
        .as_mut()
        .ok_or_else(|| "当前没有运行中的批次".to_string())?;
    if process.batch_id != batch_id {
        return Err("batch_id 与当前批次不一致".to_string());
    }
    let result = write_command(
        &mut process.stdin,
        &json!({"type": "cancel", "batch_id": batch_id, "request_id": "cancel"}),
    );
    if result.is_ok() && !process.cancel_requested {
        process.cancel_requested = true;
        if !process.cancel_watchdog_started {
            process.cancel_watchdog_started = true;
            let process_state = state.inner().process.clone();
            let watchdog_batch_id = process.batch_id.clone();
            drop(guard);
            start_cancel_watchdog(process_state, watchdog_batch_id);
            return Ok(());
        }
    }
    result
}

#[tauri::command]
fn drain_batch_events(state: State<'_, AppState>, batch_id: String) -> Result<Vec<Value>, String> {
    let mut buffer = state
        .events
        .lock()
        .map_err(|_| "读取 sidecar 事件失败".to_string())?;
    let mut matching = Vec::new();
    let mut remaining = Vec::new();
    for event in buffer.drain(..) {
        if event.get("batch_id").and_then(Value::as_str) == Some(batch_id.as_str()) {
            matching.push(event);
        } else {
            remaining.push(event);
        }
    }
    *buffer = remaining;
    Ok(matching)
}

#[tauri::command]
fn choose_output_dir() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择邮件解析输出目录")
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
fn open_result(path: String) -> Result<(), String> {
    let path = absolute(path);
    if !path.exists() {
        return Err(format!("结果不存在：{}", path.display()));
    }
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut value = Command::new("open");
        value.arg(&path);
        value
    };
    #[cfg(target_os = "windows")]
    let mut command = {
        let mut value = Command::new("cmd");
        value.args(["/C", "start", ""]).arg(&path);
        value
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = {
        let mut value = Command::new("xdg-open");
        value.arg(&path);
        value
    };
    command
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("打开结果失败：{error}"))
}

fn main() {
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            start_batch,
            cancel_batch,
            drain_batch_events,
            choose_input_files,
            choose_input_folder,
            inspect_input_paths,
            choose_output_dir,
            open_result
        ])
        .run(tauri::generate_context!())
        .expect("运行邮件解析桌面应用失败");
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::Read;

    #[test]
    fn clear_process_reaps_child_after_stdout_eof() {
        let mut child = Command::new("sh")
            .args(["-c", "exec 1>&-; sleep 30"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("启动测试 sidecar");
        let stdin = child.stdin.take().expect("测试 sidecar stdin");
        let stdout = child.stdout.take().expect("测试 sidecar stdout");
        let mut reader = BufReader::new(stdout);
        let mut output = String::new();
        assert_eq!(reader.read_to_string(&mut output).expect("读取测试 stdout"), 0);

        let state = Arc::new(Mutex::new(Some(SidecarProcess {
            batch_id: "eof-test".to_string(),
            stdin,
            child,
            cancel_requested: false,
            cancel_watchdog_started: false,
            force_killed: false,
        })));
        let started = Instant::now();
        clear_process(&state, "eof-test");
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(state.lock().expect("读取测试状态").is_none());
    }

    #[test]
    fn forward_events_reports_unexpected_eof_and_reaps_child() {
        let app = tauri::test::mock_app();
        let mut child = Command::new("sh")
            .args([
                "-c",
                "printf '%s\\n' '{\"protocol_version\":1,\"type\":\"batch_started\",\"batch_id\":\"forward-eof-test\",\"request_id\":\"request\",\"total\":3,\"mail_paths\":[\"/tmp/a.eml\",\"/tmp/b.eml\",\"/tmp/c.eml\"],\"skipped_files\":0}'; printf '%s\\n' '{\"protocol_version\":1,\"type\":\"item_started\",\"batch_id\":\"forward-eof-test\",\"request_id\":\"request\",\"index\":0,\"source_path\":\"/tmp/a.eml\",\"status\":\"running\"}'; printf '%s\\n' '{\"protocol_version\":1,\"type\":\"item_succeeded\",\"batch_id\":\"forward-eof-test\",\"request_id\":\"request\",\"index\":0,\"source_path\":\"/tmp/a.eml\",\"status\":\"success\"}'; exec 1>&-; sleep 30",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("启动 EOF 测试 sidecar");
        let stdin = child.stdin.take().expect("EOF 测试 sidecar stdin");
        let stdout = child.stdout.take().expect("EOF 测试 sidecar stdout");
        let state = Arc::new(Mutex::new(Some(SidecarProcess {
            batch_id: "forward-eof-test".to_string(),
            stdin,
            child,
            cancel_requested: false,
            cancel_watchdog_started: false,
            force_killed: false,
        })));
        let events = Arc::new(Mutex::new(Vec::new()));
        forward_events(
            app.handle().clone(),
            state.clone(),
            events.clone(),
            "forward-eof-test".to_string(),
            "request".to_string(),
            stdout,
        );

        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let has_terminal = events
                .lock()
                .expect("读取 EOF 测试事件")
                .iter()
                .any(|event| event.get("type").and_then(Value::as_str) == Some("batch_completed"));
            if has_terminal {
                break;
            }
            assert!(Instant::now() < deadline, "EOF 测试没有发布批次终态");
            std::thread::yield_now();
        }
        let emitted = events.lock().expect("读取 EOF 测试结果");
        assert!(emitted.iter().any(|event| {
            event.get("type").and_then(Value::as_str) == Some("bridge_error")
                && event.get("code").and_then(Value::as_str) == Some("unexpected_eof")
        }));
        let completed = emitted
            .iter()
            .find(|event| event.get("type").and_then(Value::as_str) == Some("batch_completed"))
            .expect("EOF 测试批次终态");
        assert_eq!(completed.get("status").and_then(Value::as_str), Some("failed"));
        assert_eq!(completed["summary"]["total"], 3);
        assert_eq!(completed["summary"]["succeeded"], 1);
        assert_eq!(completed["summary"]["failed"], 2);
        assert_eq!(
            completed["summary"]["total"],
            completed["summary"]["succeeded"]
                .as_u64()
                .unwrap()
                + completed["summary"]["partial_failed"].as_u64().unwrap()
                + completed["summary"]["skipped"].as_u64().unwrap()
                + completed["summary"]["failed"].as_u64().unwrap()
                + completed["summary"]["cancelled"].as_u64().unwrap()
        );
        assert_eq!(
            emitted
                .iter()
                .filter(|event| event.get("type").and_then(Value::as_str) == Some("item_failed"))
                .count(),
            2
        );
        drop(emitted);
        let reap_deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if state.lock().expect("读取 EOF 测试状态").is_none() {
                break;
            }
            assert!(Instant::now() < reap_deadline, "EOF 测试子进程没有回收");
            std::thread::yield_now();
        }
    }

    #[test]
    fn forward_events_marks_unfinished_items_cancelled_at_eof() {
        let app = tauri::test::mock_app();
        let mut child = Command::new("sh")
            .args([
                "-c",
                "printf '%s\\n' '{\"protocol_version\":1,\"type\":\"batch_started\",\"batch_id\":\"forward-cancel-eof-test\",\"request_id\":\"request\",\"total\":3,\"mail_paths\":[\"/tmp/a.eml\",\"/tmp/b.eml\",\"/tmp/c.eml\"],\"skipped_files\":0}'; printf '%s\\n' '{\"protocol_version\":1,\"type\":\"item_succeeded\",\"batch_id\":\"forward-cancel-eof-test\",\"request_id\":\"request\",\"index\":0,\"source_path\":\"/tmp/a.eml\",\"status\":\"success\"}'; exec 1>&-; sleep 30",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("启动取消 EOF 测试 sidecar");
        let stdin = child.stdin.take().expect("取消 EOF 测试 sidecar stdin");
        let stdout = child.stdout.take().expect("取消 EOF 测试 sidecar stdout");
        let state = Arc::new(Mutex::new(Some(SidecarProcess {
            batch_id: "forward-cancel-eof-test".to_string(),
            stdin,
            child,
            cancel_requested: true,
            cancel_watchdog_started: true,
            force_killed: false,
        })));
        let events = Arc::new(Mutex::new(Vec::new()));
        forward_events(
            app.handle().clone(),
            state.clone(),
            events.clone(),
            "forward-cancel-eof-test".to_string(),
            "request".to_string(),
            stdout,
        );

        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let has_terminal = events
                .lock()
                .expect("读取取消 EOF 测试事件")
                .iter()
                .any(|event| event.get("type").and_then(Value::as_str) == Some("batch_completed"));
            if has_terminal {
                break;
            }
            assert!(Instant::now() < deadline, "取消 EOF 测试没有发布批次终态");
            std::thread::yield_now();
        }
        let emitted = events.lock().expect("读取取消 EOF 测试结果");
        let cancelled: Vec<&Value> = emitted
            .iter()
            .filter(|event| event.get("type").and_then(Value::as_str) == Some("item_cancelled"))
            .collect();
        assert_eq!(cancelled.len(), 2);
        assert_eq!(cancelled[0]["index"], 1);
        assert_eq!(cancelled[1]["index"], 2);
        let completed = emitted
            .iter()
            .find(|event| event.get("type").and_then(Value::as_str) == Some("batch_completed"))
            .expect("取消 EOF 测试批次终态");
        assert_eq!(completed.get("status").and_then(Value::as_str), Some("cancelled"));
        assert_eq!(completed["summary"]["total"], 3);
        assert_eq!(completed["summary"]["succeeded"], 1);
        assert_eq!(completed["summary"]["cancelled"], 2);
        assert_eq!(
            completed["summary"]["total"],
            completed["summary"]["succeeded"]
                .as_u64()
                .unwrap()
                + completed["summary"]["partial_failed"].as_u64().unwrap()
                + completed["summary"]["skipped"].as_u64().unwrap()
                + completed["summary"]["failed"].as_u64().unwrap()
                + completed["summary"]["cancelled"].as_u64().unwrap()
        );
        drop(emitted);
        let reap_deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if state.lock().expect("读取取消 EOF 测试状态").is_none() {
                break;
            }
            assert!(Instant::now() < reap_deadline, "取消 EOF 测试子进程没有回收");
            std::thread::yield_now();
        }
    }
}
