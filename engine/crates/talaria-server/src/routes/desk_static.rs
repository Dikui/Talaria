//! Same-origin `/desk` SPA bootstrap & `tower_http::services::ServeDir` static asset hosting.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Router;
use axum::extract::{Path as AxumPath, Request, State};
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE, SET_COOKIE};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::get;
use serde::Serialize;
use tower::ServiceExt;
use tower_http::services::ServeDir;

use crate::error::ApiJson;
use crate::security::FeatureToggles;
use crate::server::AppState;

const MISSING_DIST_HTML: &str = r#"<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Talaria</title>
<style>body{font-family:system-ui,sans-serif;max-width:44rem;margin:4rem auto;padding:0 1rem;line-height:1.7;color:#1f2328}code{background:#f3f4f6;padding:.15rem .35rem;border-radius:.25rem}</style>
</head><body>
<h1>前端构建产物缺失</h1>
<p>请先构建前端产物后刷新本页。后端 API 可通过 <code>/api/health</code> 检查。</p>
</body></html>"#;

#[derive(Serialize)]
struct DeskBootstrapResponse<'a> {
    token: &'a str,
    data_dir_id: &'a str,
    data_dir: String,
    features: FeatureToggles,
}

pub fn router(state: Arc<AppState>) -> Router<Arc<AppState>> {
    let mut r = Router::new()
        .route("/api/desk/bootstrap", get(desk_bootstrap))
        .route("/desk", get(desk_index))
        .route("/desk/", get(desk_index))
        .route("/desk/link", get(desk_link))
        .route("/desk/{*path}", get(desk_subpath));

    if let Some(dist_dir) = &state.desk_dist {
        let assets_dir = dist_dir.join("assets");
        r = r
            .nest_service("/assets", ServeDir::new(&assets_dir))
            .nest_service("/desk/assets", ServeDir::new(&assets_dir));
    } else {
        r = r.route("/assets/{*path}", get(missing_asset));
    }
    r
}

async fn desk_bootstrap(State(state): State<Arc<AppState>>) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    (
        StatusCode::OK,
        headers,
        ApiJson(DeskBootstrapResponse {
            token: &state.security.token,
            data_dir_id: &state.security.data_dir_id,
            data_dir: state.security.data_dir.to_string_lossy().into_owned(),
            features: state.features,
        }),
    )
        .into_response()
}

async fn desk_index(State(state): State<Arc<AppState>>) -> Response {
    serve_desk_html(&state, true)
}

async fn desk_link(State(state): State<Arc<AppState>>) -> Response {
    serve_desk_html(&state, false)
}

async fn desk_subpath(
    State(state): State<Arc<AppState>>,
    AxumPath(subpath): AxumPath<String>,
    request: Request,
) -> Response {
    if let Some(dist_dir) = &state.desk_dist
        && let Some(candidate) = safe_dist_file(dist_dir, &subpath)
        && candidate.is_file()
    {
        let service = ServeDir::new(dist_dir);
        let rel_uri = format!("/{subpath}");
        let mut req = request;
        if let Ok(parsed) = rel_uri.parse() {
            *req.uri_mut() = parsed;
        }
        let resp = service.oneshot(req).await.into_response();
        return resp;
    }
    serve_desk_html(&state, true)
}

async fn missing_asset() -> Response {
    (
        StatusCode::NOT_FOUND,
        [(CONTENT_TYPE, "text/plain; charset=utf-8")],
        "资源不存在",
    )
        .into_response()
}

fn safe_dist_file(dist_dir: &Path, subpath: &str) -> Option<PathBuf> {
    let clean = subpath.trim_start_matches('/');
    if clean.is_empty() || clean == "index.html" {
        return None;
    }
    let candidate = dist_dir.join(clean);
    let canon_base = std::fs::canonicalize(dist_dir).ok()?;
    let canon_cand = std::fs::canonicalize(candidate).ok()?;
    if canon_cand.starts_with(&canon_base) {
        Some(canon_cand)
    } else {
        None
    }
}

fn serve_desk_html(state: &AppState, include_token: bool) -> Response {
    let html_raw = state
        .desk_dist
        .as_ref()
        .map(|d| d.join("index.html"))
        .and_then(|p| std::fs::read_to_string(p).ok())
        .unwrap_or_else(|| MISSING_DIST_HTML.to_owned());

    let html = if include_token {
        inject_bootstrap_html(&html_raw, &state.security.token, &state.security.data_dir_id)
    } else {
        html_raw
    };

    let mut headers = HeaderMap::new();
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if include_token {
        // Non-HttpOnly by design on same-origin loopback so SPA JS forwards `Authorization: Bearer`.
        let cookie = format!("talaria_token={}; Path=/; SameSite=Strict", state.security.token);
        if let Ok(val) = HeaderValue::from_str(&cookie) {
            headers.insert(SET_COOKIE, val);
        }
    }
    (StatusCode::OK, headers, Html(html)).into_response()
}

fn inject_bootstrap_html(raw: &str, token: &str, data_dir_id: &str) -> String {
    let token_json = serde_json::to_string(token).unwrap_or_else(|_| "\"\"".to_owned());
    let dir_json = serde_json::to_string(data_dir_id).unwrap_or_else(|_| "\"\"".to_owned());
    let snippet = format!(
        r#"<meta name="talaria-token" content="{token}"><meta name="talaria-data-dir-id" content="{data_dir_id}"><script>window.__TALARIA_BOOTSTRAP__={{token:{token_json},data_dir_id:{dir_json}}};</script>"#
    );
    if let Some(pos) = raw.find("</head>") {
        let mut out = String::with_capacity(raw.len() + snippet.len());
        out.push_str(&raw[..pos]);
        out.push_str(&snippet);
        out.push_str(&raw[pos..]);
        out
    } else if let Some(pos) = raw.find("</HEAD>") {
        let mut out = String::with_capacity(raw.len() + snippet.len());
        out.push_str(&raw[..pos]);
        out.push_str(&snippet);
        out.push_str(&raw[pos..]);
        out
    } else {
        format!("{snippet}{raw}")
    }
}
