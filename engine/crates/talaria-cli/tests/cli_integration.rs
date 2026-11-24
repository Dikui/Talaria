//! Integration tests for `talaria-data` CLI binary (`contract-check`, `replay` guard,
//! standalone `up`, exclusive `service.lock`, and `--exit-with-parent`).

use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn temp_dir(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("talaria-cli-test-{name}-{}-{nonce}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn bin_path() -> &'static str {
    env!("CARGO_BIN_EXE_talaria-data")
}

#[test]
fn contract_check_and_replay_default_dir_guard() {
    let out = Command::new(bin_path())
        .args(["contract-check"])
        .output()
        .unwrap();
    assert!(out.status.success(), "stderr: {}", String::from_utf8_lossy(&out.stderr));
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert!(stdout.contains("\"ok\": true"));
    assert!(stdout.contains("BTC-USDT-PERP"));

    // Replay into default `.talaria` directory must be rejected!
    let tmp_replay = temp_dir("replay-script");
    let replay_file = tmp_replay.join("events.jsonl");
    fs::write(&replay_file, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();

    let bad_replay = Command::new(bin_path())
        .current_dir(&tmp_replay)
        .args([
            "replay",
            "--data-dir",
            ".talaria",
            "--replay-file",
            replay_file.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        !bad_replay.status.success(),
        "replay must refuse default .talaria data directory"
    );
    assert!(
        !tmp_replay.join(".talaria").exists(),
        "rejected replay command must not create .talaria directory"
    );
    let _ = fs::remove_dir_all(&tmp_replay);
}

#[test]
fn up_exit_with_parent_and_exclusive_lock() {
    let data_dir = temp_dir("exit-parent");
    fs::write(
        data_dir.join("service.json"),
        r#"{"version":1,"features":{"market_data":false,"cold_store":true,"alerts":true,"scanner":true}}"#,
    )
    .unwrap();

    let mut child = Command::new(bin_path())
        .args([
            "up",
            "--data-dir",
            data_dir.to_str().unwrap(),
            "--port",
            "0",
            "--exit-with-parent",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();

    let stdin = child.stdin.take().unwrap();
    let desc_path = data_dir.join("runtime").join("service-descriptor.json");

    // Wait up to 5s for descriptor
    let deadline = Instant::now() + Duration::from_secs(5);
    while !desc_path.is_file() {
        assert!(Instant::now() < deadline, "descriptor not created in time");
        thread::sleep(Duration::from_millis(25));
    }

    let raw_desc = fs::read_to_string(&desc_path).unwrap();
    let base_url = raw_desc
        .lines()
        .find_map(|l| l.trim().strip_prefix("\"base_url\": \""))
        .and_then(|rest| rest.strip_suffix("\","))
        .expect("base_url in service-descriptor.json");
    let addr = base_url.strip_prefix("http://").unwrap();

    // Hit /api/health over loopback TCP
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut resp = String::new();
    stream.read_to_string(&mut resp).unwrap();
    assert!(resp.contains("200 OK"), "unexpected health response: {resp}");
    assert!(resp.contains("\"ok\":true"), "unexpected health json: {resp}");

    // Second `talaria-data up` on same --data-dir must fail cleanly with lock error
    let second = Command::new(bin_path())
        .args(["up", "--data-dir", data_dir.to_str().unwrap(), "--port", "0"])
        .output()
        .unwrap();
    assert!(!second.status.success());

    // Closing parent stdin pipe must cause clean graceful shutdown under `--exit-with-parent`
    drop(stdin);
    let wait_deadline = Instant::now() + Duration::from_secs(5);
    let exit_status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            Instant::now() < wait_deadline,
            "daemon did not exit after parent stdin closed"
        );
        thread::sleep(Duration::from_millis(25));
    };
    assert!(exit_status.success(), "exit status: {exit_status}");
    assert!(!desc_path.exists(), "descriptor should be removed on clean exit");

    let _ = fs::remove_dir_all(&data_dir);
}
