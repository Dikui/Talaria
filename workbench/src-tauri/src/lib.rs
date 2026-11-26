pub mod account_process;
pub mod service;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};

use account_process::{AccountProcessManager, PaperProcessStatus, PaperSchemeList};
use service::{
    ServiceConnectionInfo, ShellRuntimeState, ShellSettings, bootstrap_script, default_data_dir,
    discover_or_spawn_service,
};

struct AppState(Mutex<ShellRuntimeState>);
struct PaperProcessState(Mutex<AccountProcessManager>);

#[tauri::command]
fn get_service_connection(state: State<'_, AppState>) -> Result<ServiceConnectionInfo, String> {
    let guard = state
        .0
        .lock()
        .map_err(|_| "runtime state lock poisoned".to_string())?;
    if let Some(info) = guard.connection_info() {
        return Ok(info);
    }
    Err(guard
        .startup_error
        .clone()
        .unwrap_or_else(|| "talaria-data service descriptor unavailable".to_string()))
}

#[tauri::command]
fn get_shell_settings(state: State<'_, AppState>) -> Result<ShellSettings, String> {
    let guard = state
        .0
        .lock()
        .map_err(|_| "runtime state lock poisoned".to_string())?;
    Ok(guard.shell_settings())
}

#[tauri::command]
fn set_keep_running_in_background(
    state: State<'_, AppState>,
    keep_running: bool,
) -> Result<ShellSettings, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "runtime state lock poisoned".to_string())?;
    guard.keep_running_in_background = keep_running;
    Ok(guard.shell_settings())
}

#[tauri::command]
fn stop_resident_service(state: State<'_, AppState>) -> Result<(), String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "runtime state lock poisoned".to_string())?;
    guard.force_stop();
    Ok(())
}

#[tauri::command]
fn list_paper_schemes(state: State<'_, PaperProcessState>) -> Result<PaperSchemeList, String> {
    let guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    guard.list_schemes()
}

#[tauri::command]
fn start_paper_account(
    state: State<'_, PaperProcessState>,
    scheme_path: String,
) -> Result<PaperProcessStatus, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    guard.start_scheme(&PathBuf::from(scheme_path))
}

#[tauri::command]
fn stop_paper_account(
    state: State<'_, PaperProcessState>,
    scheme_path: String,
) -> Result<PaperProcessStatus, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    guard.stop_scheme(&PathBuf::from(scheme_path))
}

#[tauri::command]
fn list_paper_processes(
    state: State<'_, PaperProcessState>,
) -> Result<Vec<PaperProcessStatus>, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    Ok(guard.list_processes())
}

#[tauri::command]
fn open_paper_log(
    state: State<'_, PaperProcessState>,
    scheme_path: String,
) -> Result<String, String> {
    let guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    guard.open_log(&PathBuf::from(scheme_path))
}

#[tauri::command]
fn get_paper_keep_alive(state: State<'_, PaperProcessState>) -> Result<bool, String> {
    let guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    Ok(guard.keep_alive_on_exit())
}

#[tauri::command]
fn set_paper_keep_alive(
    state: State<'_, PaperProcessState>,
    keep_alive: bool,
) -> Result<bool, String> {
    let mut guard = state
        .0
        .lock()
        .map_err(|_| "paper process state lock poisoned".to_string())?;
    guard.set_keep_alive_on_exit(keep_alive)
}

#[tauri::command]
fn open_popout_window(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    url_path: String,
    title: String,
    width: Option<f64>,
    height: Option<f64>,
) -> Result<String, String> {
    let (label, init_script) = {
        let mut guard = state
            .0
            .lock()
            .map_err(|_| "runtime state lock poisoned".to_string())?;
        guard.popout_seq += 1;
        let label = format!("popout-{}", guard.popout_seq);
        let init_script = guard
            .connection_info()
            .map(|info| bootstrap_script(&info))
            .unwrap_or_default();
        (label, init_script)
    };

    let clean_path = url_path.trim_start_matches('/');
    let mut builder = WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(clean_path.into()))
        .title(title)
        .inner_size(width.unwrap_or(960.0), height.unwrap_or(680.0))
        .resizable(true);

    if !init_script.is_empty() {
        builder = builder.initialization_script(&init_script);
    }

    builder
        .build()
        .map_err(|err| format!("failed to build popout window: {err}"))?;
    Ok(label)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let data_dir = default_data_dir();
    let mut runtime = ShellRuntimeState::new(data_dir.clone());
    let paper_manager = AccountProcessManager::new(data_dir);
    let dist_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("packages")
        .join("talaria-desk-ui")
        .join("dist");
    discover_or_spawn_service(&mut runtime, Some(&dist_dir));

    let init_script = runtime
        .connection_info()
        .map(|info| bootstrap_script(&info))
        .unwrap_or_default();

    let app = tauri::Builder::default()
        .manage(AppState(Mutex::new(runtime)))
        .manage(PaperProcessState(Mutex::new(paper_manager)))
        .setup(move |app| {
            if !init_script.is_empty()
                && let Some(main_window) = app.get_webview_window("main")
            {
                let _ = main_window.eval(&init_script);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main"
                && let WindowEvent::CloseRequested { .. } = event
            {
                let paper_state = window.state::<PaperProcessState>();
                if let Ok(mut guard) = paper_state.0.lock() {
                    let _ = guard.on_workbench_exit();
                }
                let app_state = window.state::<AppState>();
                if let Ok(mut guard) = app_state.0.lock() {
                    guard.stop_if_configured();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_service_connection,
            get_shell_settings,
            set_keep_running_in_background,
            stop_resident_service,
            list_paper_schemes,
            start_paper_account,
            stop_paper_account,
            list_paper_processes,
            open_paper_log,
            get_paper_keep_alive,
            set_paper_keep_alive,
            open_popout_window,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Talaria Workbench Tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            let paper_state = app_handle.state::<PaperProcessState>();
            if let Ok(mut guard) = paper_state.0.lock() {
                let _ = guard.on_workbench_exit();
            }
            let app_state = app_handle.state::<AppState>();
            if let Ok(mut guard) = app_state.0.lock() {
                guard.stop_if_configured();
            }
        }
    });
}
