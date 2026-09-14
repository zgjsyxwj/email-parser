fn main() {
    tauri_build::build();

    if std::env::var("PROFILE").as_deref() == Ok("release") {
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let stage_dir = manifest_dir.join("../.build/staged/sidecar");
        let executable = if std::env::var("TARGET")
            .map(|target| target.contains("windows"))
            .unwrap_or(false)
        {
            stage_dir.join("email-sidecar.exe")
        } else {
            stage_dir.join("email-sidecar")
        };
        if !executable.is_file() {
            panic!(
                "release 构建需要已打包的 Python sidecar：{}；请先运行 `python3 scripts/build_release.py`",
                executable.display()
            );
        }
    }
}
