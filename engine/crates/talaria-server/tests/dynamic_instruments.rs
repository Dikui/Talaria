//! Integration tests for online/catalog instrument search and dynamic instrument registration
//! (`GET /api/instruments/search`, `POST /api/instruments`, runtime hot-registration, and SQLite persistence).

use std::fs;
use std::str::FromStr;

use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::market::InstrumentSpec;
use talaria_packs::CatalogInstrument;
use talaria_server::security::DATA_DIR_HEADER;
use talaria_server::{ServerConfig, start_server};
use tempfile::TempDir;

fn doge_spec() -> InstrumentSpec {
    InstrumentSpec {
        instrument_id: "DOGE-USDT-PERP".to_owned(),
        venue: "bybit".to_owned(),
        symbol: "DOGEUSDT".to_owned(),
        asset_class: "crypto_perp".to_owned(),
        tick_size: Decimal::from_str("0.00001").unwrap(),
        qty_step: Decimal::from_str("1").unwrap(),
        min_qty: Decimal::from_str("1").unwrap(),
        price_precision: 5,
        qty_precision: 0,
    }
}

fn sui_spec() -> InstrumentSpec {
    InstrumentSpec {
        instrument_id: "SUI-USDT-PERP".to_owned(),
        venue: "bybit".to_owned(),
        symbol: "SUIUSDT".to_owned(),
        asset_class: "crypto_perp".to_owned(),
        tick_size: Decimal::from_str("0.0001").unwrap(),
        qty_step: Decimal::from_str("1").unwrap(),
        min_qty: Decimal::from_str("1").unwrap(),
        price_precision: 4,
        qty_precision: 0,
    }
}

#[tokio::test]
async fn search_and_add_instrument_persists_across_restart() {
    let tmp_data = TempDir::new().unwrap();
    let tmp_fix = TempDir::new().unwrap();

    // Seed offline catalog fixture so search test runs deterministically without internet access.
    let catalog = vec![
        CatalogInstrument {
            spec: doge_spec(),
            display_name: Some("DOGE/USDT Perpetual (Bybit)".to_owned()),
            base_asset: Some("DOGE".to_owned()),
            quote_asset: Some("USDT".to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        },
        CatalogInstrument {
            spec: sui_spec(),
            display_name: Some("SUI/USDT Perpetual (Bybit)".to_owned()),
            base_asset: Some("SUI".to_owned()),
            quote_asset: Some("USDT".to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        },
    ];
    fs::write(
        tmp_fix.path().join("catalog.json"),
        serde_json::to_string_pretty(&catalog).unwrap(),
    )
    .unwrap();

    let mut cfg = ServerConfig::new(tmp_data.path());
    cfg.pack = "crypto_perp".to_owned();
    cfg.fixture_dir = Some(tmp_fix.path().to_path_buf());

    let srv = start_server(cfg.clone()).await.unwrap();
    let base = srv.base_url().to_owned();
    let auth = srv.token().to_owned();
    let dir_id = srv.descriptor.data_dir_id.clone();
    let client = reqwest::Client::builder().no_proxy().build().unwrap();

    // 1. Search catalog for "doge" -> finds DOGE-USDT-PERP as unregistered
    let search_resp: Value = client
        .get(format!("{base}/api/instruments/search?q=doge"))
        .bearer_auth(&auth)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let results = search_resp["results"].as_array().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["spec"]["instrument_id"], "DOGE-USDT-PERP");
    assert_eq!(results[0]["registered"], false);

    // 2. Reject invalid venue for crypto_perp
    let mut bad_spec = doge_spec();
    bad_spec.venue = "alpaca".to_owned();
    let bad_resp = client
        .post(format!("{base}/api/instruments"))
        .bearer_auth(&auth)
        .header(DATA_DIR_HEADER, &dir_id)
        .json(&json!({ "spec": bad_spec }))
        .send()
        .await
        .unwrap();
    assert!(bad_resp.status().is_client_error());

    // 3. Add DOGE-USDT-PERP dynamically
    let add_resp: Value = client
        .post(format!("{base}/api/instruments"))
        .bearer_auth(&auth)
        .header(DATA_DIR_HEADER, &dir_id)
        .json(&json!({ "spec": doge_spec() }))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(add_resp["created"], true);
    assert_eq!(add_resp["instrument"]["instrument_id"], "DOGE-USDT-PERP");

    // 4. Immediately visible in GET /api/instruments and marked registered in search
    let list: Vec<InstrumentSpec> = client
        .get(format!("{base}/api/instruments"))
        .bearer_auth(&auth)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(list.iter().any(|s| s.instrument_id == "DOGE-USDT-PERP"));

    let search_after: Value = client
        .get(format!("{base}/api/instruments/search?q=doge"))
        .bearer_auth(&auth)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(search_after["results"][0]["registered"], true);

    // 5. Immediately usable in watchlist without restart
    let grp: Value = client
        .post(format!("{base}/api/watchlist/groups"))
        .bearer_auth(&auth)
        .header(DATA_DIR_HEADER, &dir_id)
        .json(&json!({ "name": "Memes" }))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let group_id = grp["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|g| g["name"] == "Memes")
        .unwrap()["group_id"]
        .as_str()
        .unwrap()
        .to_owned();

    let add_item_resp = client
        .post(format!("{base}/api/watchlist/items"))
        .bearer_auth(&auth)
        .header(DATA_DIR_HEADER, &dir_id)
        .json(&json!({ "group_id": group_id, "instrument_id": "DOGE-USDT-PERP" }))
        .send()
        .await
        .unwrap();
    assert!(add_item_resp.status().is_success());

    // 6. Restart server on the same data_dir and verify DOGE-USDT-PERP persists
    srv.shutdown().await;

    let srv2 = start_server(cfg).await.unwrap();
    let list2: Vec<InstrumentSpec> = client
        .get(format!("{}/api/instruments", srv2.base_url()))
        .bearer_auth(srv2.token())
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(list2.iter().any(|s| s.instrument_id == "DOGE-USDT-PERP"));
    srv2.shutdown().await;
}
