// Declaring the app's own commands makes Tauri create an "allow-<command>" permission for each one.
// Without this, a command can't be granted to the CRM pages (served from http://127.0.0.1), and calls fail with
// "Command ... not allowed by ACL".
const COMMANDS: &[&str] = &["backend_status", "retry_backend", "check_update", "install_update"];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
