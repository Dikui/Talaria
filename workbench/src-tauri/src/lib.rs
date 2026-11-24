pub mod service;

use std::sync::Mutex;

use tauri::{Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};

use service::{
    ServiceConnectionInfo, ShellRuntimeState, ShellSettings, bootstrap_script, default_data_dir,
    discover_or_spawn_service,
};

struct AppState(Mutex<ShellRuntimeState>);

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
    let mut runtime = ShellRuntimeState::new(data_dir);
    let dist_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("packages")
        .join("talaria-desk-ui")
        .join("dist");
    discover_or_spawn_service(&mut runtime, Some(&dist_dir));

    let init_script = runtime
        .connection_info()
        .map(|info| bootstrap_script(&info))
        .unwrap_or_default();

    tauri::Builder::default()
        .manage(AppState(Mutex::new(runtime)))
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
            open_popout_window,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Talaria Workbench Tauri application");
}
