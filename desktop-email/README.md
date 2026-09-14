# 邮件解析桌面应用（批次切片）

本目录实现邮件批次导入的可运行切片：从桌面选择多个 EML/MSG 或文件夹，启动真实 Python sidecar，将标题、地址、发送时间、完整纯文本正文和附件写入独立邮件目录。正文优先使用纯文本；只有 HTML 或 RTF 时转换为可读纯文本，并保留历史引用和签名。文件夹扫描会汇总跳过的独立非邮件文件，并排除结果输出目录。

## 运行 sidecar 集成测试

```sh
../.venv/bin/pip install -r sidecar/requirements.txt
../.venv/bin/python -m unittest discover -s tests -v
```

测试启动真实 `sidecar/main.py --stdio` 子进程，通过 stdin 写入一个批次命令，再从 stdout 读取 JSON Lines 事件。`tests/test_batch.py` 覆盖损坏输入隔离、目录扫描、输出目录排除、部分失败和 100 封批次。测试不会写入持久目录或数据库。

## JSON Lines 协议

启动命令：

```json
{"type":"start","batch_id":"batch-1","request_id":"request-1","inputs":["/absolute/path/mail.eml"],"output_dir":"/absolute/path/results","limits":{"max_depth":10,"max_extract_bytes":524288000}}
```

`limits` 是每个根邮件独立使用的递归配置。`max_depth` 把根邮件记为深度 0，默认值为 10，允许范围为 0–100；设置为 0 时只写根邮件。`max_extract_bytes` 默认值为 500 MiB，允许范围为 0–10 GiB。它只累计本根邮件中实际允许展开的嵌套邮件附件的解码后原始字节，并在每层递归中共用剩余预算。根邮件正文、普通附件和因达到限制而保留的邮件附件原件不计入预算，因此这不是所有结果落盘字节的硬上限。达到深度或预算后，sidecar 停止继续展开该附件，把原始附件写入当前邮件的 `attachments/`，在 Markdown 附件链接和错误记录中说明原因，并返回 `item_partial_failed`。其他根邮件继续处理。配置对象字段必须完整且为范围内整数，前端会在启动前提示非法值；sidecar 在批次解析开始前返回 `batch_rejected`（`stage=limits`、`code=invalid_limits`）。

桌面端的 `InputSelection.inputs` 保留用户选择的文件或文件夹根；sidecar 在收到最终 `output_dir` 后重新扫描这些根。`batch_started.mail_paths` 是最终发现的邮件清单，前端以它作为候选列表真值，`skipped_files` 和 `excluded_output` 也以 sidecar 的批次事件为准。`excluded_output` 表示被剪枝的输出路径或子树数量，不会递归读取输出目录来计数。

sidecar 返回的正常事件都带 `protocol_version`、`batch_id` 和 `request_id`。事件顺序为：

```text
batch_started → input_skipped | input_failed → item_started →
item_succeeded | item_partial_failed | item_failed | item_cancelled → batch_completed
```

`item_partial_failed` 表示正文或部分附件已落盘，但结果不完整；事件包含 `errors`，每项记录 `source_path`、`stage`、`code`、`error` 和 `location`。`bridge_error` 及 `batch_completed.errors` 也进入前端错误清单，并按来源、阶段、代码、位置和原因去重。`batch_completed.summary` 的固定字段为 `total`、`succeeded`、`partial_failed`、`skipped`、`failed`、`cancelled` 和 `skipped_files`。批次状态为 `completed`、`partial_failed`、`failed` 或 `cancelled`。

取消命令使用当前批次的 `batch_id`：

```json
{"type":"cancel","batch_id":"batch-1","request_id":"cancel-1"}
```

sidecar 会立即返回 `cancel_requested`，停止调度尚未开始的邮件，并在当前邮件的安全落盘边界结束后发送 `item_cancelled`。已完成邮件先提交的结果目录会保留；正在写入的暂存目录在取消时清理，不会生成 `.complete.json`。再次使用同一输出目录运行时，完整结果由完整性标记跳过，其余输入重新处理。stdin 在发送 `start` 后关闭属于一次性批次的正常用法；sidecar 会等待当前批次发布 `batch_completed` 后再退出。桌面端取消等待 sidecar 的正常终态，若 sidecar 超过 5 秒仍未退出，Rust 桥接会强制终止进程，并发布 `bridge_error` 与 `batch_completed(status=cancelled)`，未完成项保持可重试。

Rust 桥接将这些事件转发为 `sidecar-event`，同时提供 `drain_batch_events` 给前端轮询。sidecar 输出意外 EOF 时，桥接会把 `bridge_error` 和失败的 `batch_completed` 写入同一个事件缓冲，前端不会停留在运行中。

## 结果布局

```text
输出目录/
  清理后的主题-邮件内容前缀/
    邮件.md
    attachments/
    emails/
    .complete.json
```

`.complete.json` 是用于后续完整性判断的隐藏标记；只有 Markdown 和标记同时有效时才可判定结果完整。未展开的邮件附件会保留原件，并通过 `item_partial_failed` 报告原因。

## sidecar 依赖和开发启动

依赖版本记录在 `sidecar/requirements.txt`。Rust 开发启动时会优先使用工作区旁的 `.venv`（Windows 使用 `.venv/Scripts/python.exe`）；也可以用 `EMAIL_SIDECAR_PYTHON` 指定 Python。发行构建应提供带依赖的 sidecar 可执行文件。

## 发行构建

发行构建使用 PyInstaller `onedir`。可执行文件和 `_internal` 目录一起交付，Tauri 会将它们复制到安装包的 `Resources/sidecar`。release 构建找不到该目录中的可执行 sidecar 时会直接失败；release 不读取源码 `main.py`、`EMAIL_SIDECAR_PATH` 或外部 Python。debug 仍保留工作区 `.venv` 和源码启动方式。

PyInstaller 不是 cross-compiler，因此必须在目标操作系统和对应 CPU 架构上构建。先安装锁定的 Python 构建依赖和 Tauri CLI：

```sh
../.venv/bin/python -m pip install -r sidecar/requirements-build.txt
npm ci
```

当前 macOS ARM64 构建命令和产物：

```sh
../.venv/bin/python scripts/build_release.py --target aarch64-apple-darwin
```

产物位于 `src-tauri/target/aarch64-apple-darwin/release/bundle/macos/邮件解析.app` 和 `src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/邮件解析_0.1.0_aarch64.dmg`。构建脚本同时写入 `.build/releases/aarch64-apple-darwin/manifest.json`；`.build/` 是本地构建目录，不应放入源码发布包。

Windows x64 必须在 Windows 原生环境执行对应命令：

```powershell
cd C:\email\desktop-email
# 若尚未创建虚拟环境，先执行：py -3 -m venv ..\.venv
..\.venv\Scripts\python.exe -m pip install -r sidecar\requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败" }
npm ci
..\.venv\Scripts\python.exe scripts\build_release.py --target x86_64-pc-windows-msvc --python ..\.venv\Scripts\python.exe
```

请按实际源码位置调整 `cd` 路径。安装依赖与发行构建必须使用同一个 Python。`sidecar/requirements.txt` 使用 UTF-8，并在首行声明编码，避免部分 Windows pip 按系统 GBK 编码读取中文注释而失败。如果旧源码出现 `UnicodeDecodeError: 'gbk'`，可先执行 `$env:PYTHONUTF8 = "1"`，再重新安装构建依赖；安装成功后再构建。

Windows 构建会生成 NSIS 和 MSI 产物，并在 `src-tauri/target/x86_64-pc-windows-msvc/release/bundle/` 下保存。当前工作环境是 macOS 26.5.2 ARM64，没有 Windows 环境，因此 Windows sidecar、安装包和运行验收尚未完成，不能用 macOS 结果替代。

Windows 的 `tauri-build` 需要 `src-tauri/icons/icon.ico`；该文件由仓库内的 `icon.svg` 使用 Tauri CLI 生成并随源码提供。当前主机没有 Windows，图标资源和 Windows 构建链尚未在目标系统实测。

sidecar 构建输出必须包含 `email-sidecar`（Windows 为 `email-sidecar.exe`）及其 `_internal` 目录。Windows 使用 console sidecar 保留 stdin/stdout JSONL 协议，Rust 通过 `CREATE_NO_WINDOW` 隐藏控制台窗口；不要使用 PyInstaller `--noconsole`。

## Tauri 开发检查

```sh
cargo check --offline --manifest-path src-tauri/Cargo.toml
node --check frontend/app.js
```

开发时 Rust 会运行 `sidecar/main.py`，也可通过 `EMAIL_SIDECAR_PATH` 指定调试 sidecar。发行版只使用安装包 `Resources/sidecar` 中的可执行文件。输入源只读，结果通过系统的 `open`、`cmd /C start` 或 `xdg-open` 打开。

## 调试日志

桌面应用默认启用文件调试日志，使用 Tauri 官方日志插件轮转：单文件约 5 MB，保留 3 个历史文件。

- Windows：`%LOCALAPPDATA%\com.emailparser.desktop\logs`
- macOS：`~/Library/Logs/com.emailparser.desktop`

日志包含应用版本、sidecar 启动信息、批次 ID、邮件序号、源文件名、可读取的邮件标题、解析耗时、状态、失败阶段和错误码。解析前失败时可通过文件名定位；解析后的失败可用相同批次 ID 和序号关联先前记录的标题。日志不主动记录正文、附件内容或完整 JSONL 数据。

单独运行源码 sidecar 时默认只记录启动和退出；设置 `EMAIL_LOG_LEVEL=DEBUG` 可启用详细日志。日志输出到 stderr，stdout 仅用于 JSONL。桌面端会持续读取 stderr 并写入应用日志。收集故障信息时请附上对应时间段的日志。
