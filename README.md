# 邮件解析

基于 Tauri 2 和 Python sidecar 的桌面应用。支持批量解析 EML、MSG，保存正文及普通附件，并保留嵌套邮件的包含关系。

开发、测试、结果布局和本机构建命令见 [应用文档](desktop-email/README.md)。领域术语见 [CONTEXT.md](CONTEXT.md)。仓库不包含业务邮件和解析结果。

## Windows 安装包

推送到 `main` 或在 GitHub Actions 中手动运行 **Build Windows**，会在 Windows x64 环境执行测试并生成 NSIS `.exe` 和 MSI `.msi` 安装包。

打开 [构建列表](https://github.com/zgjsyxwj/email-parser/actions/workflows/windows.yml)，进入成功的运行，在 **Artifacts** 下载 `email-parser-windows-x64`。解压后选择一种安装包即可。安装包内置 Python sidecar，使用者不需要另装 Python。产物保留 30 天，并附带 SHA-256 构建清单。当前安装包未配置代码签名。
