// Hides the extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! DericBI CRM desktop shell.
//!
//! The CRM itself is a Python engine (the `backend` folder) that serves the interface on
//! 127.0.0.1. This program starts that engine, shows a small splash page while it warms up,
//! then points the window at it. When the window closes, the engine is stopped too.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::Manager;

const DEFAULT_PORT: u16 = 8765;
const START_TIMEOUT: Duration = Duration::from_secs(90);
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Serialize)]
struct Status {
    /// "starting" | "ready" | "error"
    state: String,
    url: String,
    message: String,
}

struct Shared {
    child: Mutex<Option<Child>>,
    status: Mutex<Status>,
    /// Bumped on every (re)start so an old waiting thread can tell it is out of date.
    generation: AtomicU64,
}

impl Shared {
    fn new() -> Self {
        Shared {
            child: Mutex::new(None),
            status: Mutex::new(Status {
                state: "starting".into(),
                url: String::new(),
                message: String::new(),
            }),
            generation: AtomicU64::new(0),
        }
    }
}

fn set_status(shared: &Shared, state: &str, url: &str, message: &str) {
    *shared.status.lock().unwrap() = Status {
        state: state.into(),
        url: url.into(),
        message: message.into(),
    };
}

/// Prefer the usual port so the browser-side settings (which are stored per address) survive restarts.
fn pick_port() -> u16 {
    if TcpListener::bind(("127.0.0.1", DEFAULT_PORT)).is_ok() {
        return DEFAULT_PORT;
    }
    TcpListener::bind(("127.0.0.1", 0))
        .ok()
        .and_then(|l| l.local_addr().ok())
        .map(|a| a.port())
        .unwrap_or(DEFAULT_PORT)
}

fn hide_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    {
        let _ = cmd;
    }
}

fn data_dir_hint() -> String {
    if cfg!(windows) {
        match std::env::var("APPDATA") {
            Ok(p) => format!("{}\\DericBI-CRM", p),
            Err(_) => "%APPDATA%\\DericBI-CRM".into(),
        }
    } else {
        "~/.local/share/DericBI-CRM".into()
    }
}

/// Start the engine. Installed app: the bundled `dericbi-backend` next to this program.
/// Development: run `backend/main.py` with Python.
fn spawn_backend(port: u16) -> Result<Child, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let dir = exe.parent().ok_or("cannot find the app folder")?.to_path_buf();
    let bundled = dir.join(if cfg!(windows) { "dericbi-backend.exe" } else { "dericbi-backend" });
    let port_s = port.to_string();
    let parent_pid = std::process::id().to_string();

    if bundled.exists() {
        let mut cmd = Command::new(&bundled);
        cmd.args(["--port", &port_s, "--parent-pid", &parent_pid])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_window(&mut cmd);
        return cmd.spawn().map_err(|e| format!("could not start {}: {}", bundled.display(), e));
    }

    let script: PathBuf = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("backend")
        .join("main.py");
    if !script.exists() {
        return Err("the CRM engine is missing (no dericbi-backend next to the app and no backend/main.py)".into());
    }

    // (program, extra leading args)
    let mut candidates: Vec<(String, Vec<&str>)> = Vec::new();
    if let Ok(p) = std::env::var("DERICBI_PYTHON") {
        candidates.push((p, vec![]));
    }
    if cfg!(windows) {
        candidates.push(("python".into(), vec![]));
        candidates.push(("py".into(), vec!["-3"]));
    } else {
        candidates.push(("python3".into(), vec![]));
        candidates.push(("python".into(), vec![]));
    }

    let mut last_err = String::from("Python was not found");
    for (program, lead_args) in candidates {
        let mut cmd = Command::new(&program);
        cmd.args(&lead_args)
            .arg(&script)
            .args(["--port", &port_s, "--parent-pid", &parent_pid])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        hide_window(&mut cmd);
        match cmd.spawn() {
            Ok(child) => return Ok(child),
            Err(e) => last_err = format!("could not run '{}': {}", program, e),
        }
    }
    Err(format!("{}. Install Python 3 and run: pip install -r backend/requirements.txt", last_err))
}

/// True once the engine answers /api/health.
fn is_healthy(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    let req = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        port
    );
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut body = String::new();
    let _ = s.read_to_string(&mut body);
    body.starts_with("HTTP/1.1 200") && body.contains("\"ok\":true")
}

fn child_has_exited(shared: &Shared) -> bool {
    match shared.child.lock().unwrap().as_mut() {
        Some(c) => matches!(c.try_wait(), Ok(Some(_))),
        None => true,
    }
}

fn kill_child(shared: &Shared) {
    if let Some(mut child) = shared.child.lock().unwrap().take() {
        #[cfg(windows)]
        {
            // Kill the whole tree so the engine's browser windows go too.
            let mut cmd = Command::new("taskkill");
            cmd.args(["/PID", &child.id().to_string(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            hide_window(&mut cmd);
            let _ = cmd.status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn start_backend(shared: Arc<Shared>) {
    let generation = shared.generation.fetch_add(1, Ordering::SeqCst) + 1;
    kill_child(&shared);
    set_status(&shared, "starting", "", "");

    let port = pick_port();
    match spawn_backend(port) {
        Ok(child) => {
            *shared.child.lock().unwrap() = Some(child);
            thread::spawn(move || {
                let started = Instant::now();
                loop {
                    if shared.generation.load(Ordering::SeqCst) != generation {
                        return; // a newer start replaced this one
                    }
                    if is_healthy(port) {
                        set_status(&shared, "ready", &format!("http://127.0.0.1:{}/", port), "");
                        return;
                    }
                    if child_has_exited(&shared) {
                        set_status(
                            &shared,
                            "error",
                            "",
                            &format!(
                                "The CRM engine stopped while starting. Details are in backend.log in {}",
                                data_dir_hint()
                            ),
                        );
                        return;
                    }
                    if started.elapsed() > START_TIMEOUT {
                        set_status(
                            &shared,
                            "error",
                            "",
                            "The CRM engine took too long to start. Close the app and try again.",
                        );
                        return;
                    }
                    thread::sleep(Duration::from_millis(250));
                }
            });
        }
        Err(e) => set_status(&shared, "error", "", &e),
    }
}

#[tauri::command]
fn backend_status(shared: tauri::State<'_, Arc<Shared>>) -> Status {
    shared.status.lock().unwrap().clone()
}

#[tauri::command]
fn retry_backend(shared: tauri::State<'_, Arc<Shared>>) {
    start_backend(shared.inner().clone());
}

fn main() {
    let shared = Arc::new(Shared::new());
    let for_setup = shared.clone();

    let app = tauri::Builder::default()
        // Opening the app twice just brings the existing window forward.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .manage(shared.clone())
        .invoke_handler(tauri::generate_handler![backend_status, retry_backend])
        .setup(move |_app| {
            start_backend(for_setup.clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build DericBI CRM");

    app.run(move |_handle, event| {
        if let tauri::RunEvent::Exit = event {
            kill_child(&shared);
        }
    });
}
