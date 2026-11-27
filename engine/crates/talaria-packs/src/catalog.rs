//! Online exchange instrument catalog discovery, parsing, caching, and search.
//!
//! Supports all built-in market packs (`crypto_perp`/Bybit, `okx_perp`, `binance_perp`,
//! `hyperliquid_perp`, `coinbase_spot`, `us_equity`/Alpaca, and `crypto_suite`).

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use reqwest::Client;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use talaria_contracts::InstrumentSpec;

use crate::error::PackError;
use crate::proxy::is_loopback_url;
use crate::traits::HistoryConnectorConfig;

/// Default TTL for cached exchange instrument catalogs (5 minutes).
const CATALOG_CACHE_TTL: Duration = Duration::from_secs(300);

/// Default max search results returned to callers.
pub const DEFAULT_SEARCH_LIMIT: usize = 50;

/// Single catalog search entry returned by online exchange discovery.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CatalogInstrument {
    pub spec: InstrumentSpec,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_asset: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quote_asset: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(default)]
    pub registered: bool,
}

#[derive(Clone)]
struct CacheEntry {
    fetched_at: Instant,
    items: Vec<CatalogInstrument>,
}

static CATALOG_CACHE: LazyLock<Mutex<HashMap<String, CacheEntry>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Clear the in-memory catalog cache (useful in tests or forced refresh).
pub fn clear_catalog_cache() {
    if let Ok(mut guard) = CATALOG_CACHE.lock() {
        guard.clear();
    }
}

fn cache_get(key: &str) -> Option<Vec<CatalogInstrument>> {
    let guard = CATALOG_CACHE.lock().ok()?;
    let entry = guard.get(key)?;
    if entry.fetched_at.elapsed() <= CATALOG_CACHE_TTL {
        Some(entry.items.clone())
    } else {
        None
    }
}

fn cache_put(key: String, items: Vec<CatalogInstrument>) {
    if let Ok(mut guard) = CATALOG_CACHE.lock() {
        guard.insert(
            key,
            CacheEntry {
                fetched_at: Instant::now(),
                items,
            },
        );
    }
}

fn build_http_client(base_url: &str, timeout: Duration) -> Result<Client, PackError> {
    let mut builder = Client::builder()
        .timeout(timeout)
        .user_agent("talaria-data/catalog");
    if is_loopback_url(base_url) {
        builder = builder.no_proxy();
    }
    builder
        .build()
        .map_err(|e| PackError::FeedTransport(format!("catalog HTTP client build error: {e}")))
}

fn decimal_precision(d: Decimal) -> i32 {
    d.normalize().scale().min(8) as i32
}

fn parse_pos_decimal(raw: &str) -> Option<Decimal> {
    let d = Decimal::from_str(raw.trim()).ok()?;
    if d > Decimal::ZERO { Some(d) } else { None }
}

fn step_from_precision(prec: i32) -> Decimal {
    let p = prec.clamp(0, 8) as u32;
    Decimal::new(1, p)
}

/// Filter and rank catalog items by `query`, deduplicating by `instrument_id`.
pub fn filter_and_rank_catalog(
    items: Vec<CatalogInstrument>,
    query: &str,
    limit: usize,
) -> Vec<CatalogInstrument> {
    let q = query.trim().to_ascii_uppercase();
    let max_items = if limit == 0 {
        DEFAULT_SEARCH_LIMIT
    } else {
        limit.min(200)
    };

    let mut scored: Vec<(u8, String, CatalogInstrument)> = Vec::new();
    for item in items {
        if q.is_empty() {
            scored.push((0, item.spec.instrument_id.clone(), item));
            continue;
        }
        let id_u = item.spec.instrument_id.to_ascii_uppercase();
        let sym_u = item.spec.symbol.to_ascii_uppercase();
        let base_u = item
            .base_asset
            .as_deref()
            .unwrap_or("")
            .to_ascii_uppercase();
        let name_u = item
            .display_name
            .as_deref()
            .unwrap_or("")
            .to_ascii_uppercase();

        let score = if base_u == q || sym_u == q || id_u == q {
            0
        } else if base_u.starts_with(&q) || sym_u.starts_with(&q) || id_u.starts_with(&q) {
            1
        } else if id_u.contains(&q) || sym_u.contains(&q) || base_u.contains(&q) {
            2
        } else if name_u.contains(&q) {
            3
        } else {
            continue;
        };
        scored.push((score, id_u, item));
    }

    scored.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::with_capacity(scored.len().min(max_items));
    for (_, _, item) in scored {
        if seen.insert(item.spec.instrument_id.clone()) {
            out.push(item);
            if out.len() >= max_items {
                break;
            }
        }
    }
    out
}

/// Convert static `InstrumentSpec` list into `CatalogInstrument` fallback entries.
pub fn catalog_from_specs(specs: &[InstrumentSpec]) -> Vec<CatalogInstrument> {
    specs
        .iter()
        .cloned()
        .map(|spec| {
            let base = spec
                .instrument_id
                .split('-')
                .next()
                .map(|s| s.to_owned());
            CatalogInstrument {
                display_name: Some(spec.instrument_id.clone()),
                base_asset: base,
                quote_asset: None,
                status: Some("TRADING".to_owned()),
                registered: false,
                spec,
            }
        })
        .collect()
}

/// Optional offline fixture override: reads `<fixture_dir>/catalog.json` if present.
pub fn try_load_fixture_catalog(config: &HistoryConnectorConfig) -> Option<Vec<CatalogInstrument>> {
    let dir = config.fixture_dir.as_ref()?;
    let path = dir.join("catalog.json");
    let raw = std::fs::read_to_string(path).ok()?;
    serde_json::from_str::<Vec<CatalogInstrument>>(&raw).ok()
}

// =========================================================================
// Bybit V5 Linear (`crypto_perp`)
// =========================================================================

pub async fn fetch_bybit_linear_catalog(
    base_url: &str,
    timeout: Duration,
) -> Result<Vec<CatalogInstrument>, PackError> {
    let key = format!("bybit_linear:{base_url}");
    if let Some(cached) = cache_get(&key) {
        return Ok(cached);
    }

    let client = build_http_client(base_url, timeout)?;
    let url = format!(
        "{}/v5/market/instruments-info?category=linear&limit=1000",
        base_url.trim_end_matches('/')
    );
    let resp: Value = client
        .get(&url)
        .send()
        .await
        .map_err(|e| PackError::FeedTransport(format!("bybit catalog request failed: {e}")))?
        .error_for_status()
        .map_err(|e| PackError::FeedTransport(format!("bybit catalog HTTP error: {e}")))?
        .json()
        .await
        .map_err(|e| PackError::FeedTransport(format!("bybit catalog JSON error: {e}")))?;

    let list = resp
        .pointer("/result/list")
        .and_then(Value::as_array)
        .ok_or_else(|| PackError::FeedTransport("bybit catalog missing result.list".to_owned()))?;

    let mut out = Vec::with_capacity(list.len());
    for row in list {
        let status = row.get("status").and_then(Value::as_str).unwrap_or("");
        if !status.eq_ignore_ascii_case("Trading") {
            continue;
        }
        let contract_type = row
            .get("contractType")
            .and_then(Value::as_str)
            .unwrap_or("LinearPerpetual");
        if !contract_type.eq_ignore_ascii_case("LinearPerpetual") {
            continue;
        }
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let base = row
            .get("baseCoin")
            .and_then(Value::as_str)
            .unwrap_or(symbol);
        let quote = row
            .get("quoteCoin")
            .and_then(Value::as_str)
            .unwrap_or("USDT");
        if quote != "USDT" && quote != "USDC" {
            continue;
        }
        let tick_size = row
            .pointer("/priceFilter/tickSize")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or_else(|| step_from_precision(2));
        let qty_step = row
            .pointer("/lotSizeFilter/qtyStep")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or_else(|| step_from_precision(3));
        let min_qty = row
            .pointer("/lotSizeFilter/minOrderQty")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or(qty_step);

        let price_precision = decimal_precision(tick_size);
        let qty_precision = decimal_precision(qty_step);
        let instrument_id = format!("{base}-{quote}-PERP");

        out.push(CatalogInstrument {
            spec: InstrumentSpec {
                instrument_id: instrument_id.clone(),
                venue: "bybit".to_owned(),
                symbol: symbol.to_owned(),
                asset_class: "crypto_perp".to_owned(),
                tick_size,
                qty_step,
                min_qty,
                price_precision,
                qty_precision,
            },
            display_name: Some(format!("{base}/{quote} Perpetual (Bybit)")),
            base_asset: Some(base.to_owned()),
            quote_asset: Some(quote.to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        });
    }

    cache_put(key, out.clone());
    Ok(out)
}

// =========================================================================
// OKX V5 Swap (`okx_perp`)
// =========================================================================

pub async fn fetch_okx_swap_catalog(
    base_url: &str,
    timeout: Duration,
) -> Result<Vec<CatalogInstrument>, PackError> {
    let key = format!("okx_swap:{base_url}");
    if let Some(cached) = cache_get(&key) {
        return Ok(cached);
    }

    let client = build_http_client(base_url, timeout)?;
    let url = format!(
        "{}/api/v5/public/instruments?instType=SWAP",
        base_url.trim_end_matches('/')
    );
    let resp: Value = client
        .get(&url)
        .send()
        .await
        .map_err(|e| PackError::FeedTransport(format!("okx catalog request failed: {e}")))?
        .error_for_status()
        .map_err(|e| PackError::FeedTransport(format!("okx catalog HTTP error: {e}")))?
        .json()
        .await
        .map_err(|e| PackError::FeedTransport(format!("okx catalog JSON error: {e}")))?;

    let data = resp
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| PackError::FeedTransport("okx catalog missing data array".to_owned()))?;

    let mut out = Vec::with_capacity(data.len());
    for row in data {
        let state = row.get("state").and_then(Value::as_str).unwrap_or("");
        if !state.eq_ignore_ascii_case("live") {
            continue;
        }
        let Some(inst_id) = row.get("instId").and_then(Value::as_str) else {
            continue;
        };
        let settle = row.get("settleCcy").and_then(Value::as_str).unwrap_or("");
        if settle != "USDT" {
            continue;
        }
        let base = inst_id.split('-').next().unwrap_or(inst_id);
        let tick_size = row
            .get("tickSz")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or_else(|| step_from_precision(2));
        let ct_val = row
            .get("ctVal")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or(Decimal::ONE);
        let lot_sz = row
            .get("lotSz")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or(Decimal::ONE);
        let min_sz = row
            .get("minSz")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or(lot_sz);

        let qty_step = (ct_val * lot_sz).normalize();
        let min_qty = (ct_val * min_sz).normalize();
        let price_precision = decimal_precision(tick_size);
        let qty_precision = decimal_precision(qty_step);
        let instrument_id = format!("{base}-USDT-PERP");

        out.push(CatalogInstrument {
            spec: InstrumentSpec {
                instrument_id,
                venue: "okx".to_owned(),
                symbol: inst_id.to_owned(),
                asset_class: "crypto_perp".to_owned(),
                tick_size,
                qty_step,
                min_qty,
                price_precision,
                qty_precision,
            },
            display_name: Some(format!("{base}/USDT Swap (OKX)")),
            base_asset: Some(base.to_owned()),
            quote_asset: Some("USDT".to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        });
    }

    cache_put(key, out.clone());
    Ok(out)
}

// =========================================================================
// Binance USDM Futures (`binance_perp`)
// =========================================================================

pub async fn fetch_binance_usdm_catalog(
    base_url: &str,
    timeout: Duration,
) -> Result<Vec<CatalogInstrument>, PackError> {
    let key = format!("binance_usdm:{base_url}");
    if let Some(cached) = cache_get(&key) {
        return Ok(cached);
    }

    let client = build_http_client(base_url, timeout)?;
    let url = format!("{}/fapi/v1/exchangeInfo", base_url.trim_end_matches('/'));
    let resp: Value = client
        .get(&url)
        .send()
        .await
        .map_err(|e| PackError::FeedTransport(format!("binance catalog request failed: {e}")))?
        .error_for_status()
        .map_err(|e| PackError::FeedTransport(format!("binance catalog HTTP error: {e}")))?
        .json()
        .await
        .map_err(|e| PackError::FeedTransport(format!("binance catalog JSON error: {e}")))?;

    let symbols = resp
        .get("symbols")
        .and_then(Value::as_array)
        .ok_or_else(|| PackError::FeedTransport("binance catalog missing symbols array".to_owned()))?;

    let mut out = Vec::with_capacity(symbols.len());
    for row in symbols {
        let status = row.get("status").and_then(Value::as_str).unwrap_or("");
        let contract_type = row
            .get("contractType")
            .and_then(Value::as_str)
            .unwrap_or("");
        if status != "TRADING" || contract_type != "PERPETUAL" {
            continue;
        }
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let base = row
            .get("baseAsset")
            .and_then(Value::as_str)
            .unwrap_or(symbol);
        let quote = row
            .get("quoteAsset")
            .and_then(Value::as_str)
            .unwrap_or("USDT");
        if quote != "USDT" && quote != "USDC" {
            continue;
        }

        let mut tick_size = step_from_precision(2);
        let mut qty_step = step_from_precision(3);
        let mut min_qty = qty_step;

        if let Some(filters) = row.get("filters").and_then(Value::as_array) {
            for f in filters {
                match f.get("filterType").and_then(Value::as_str) {
                    Some("PRICE_FILTER") => {
                        if let Some(d) = f
                            .get("tickSize")
                            .and_then(Value::as_str)
                            .and_then(parse_pos_decimal)
                        {
                            tick_size = d;
                        }
                    }
                    Some("LOT_SIZE") => {
                        if let Some(d) = f
                            .get("stepSize")
                            .and_then(Value::as_str)
                            .and_then(parse_pos_decimal)
                        {
                            qty_step = d;
                        }
                        if let Some(d) = f
                            .get("minQty")
                            .and_then(Value::as_str)
                            .and_then(parse_pos_decimal)
                        {
                            min_qty = d;
                        }
                    }
                    _ => {}
                }
            }
        }

        let price_precision = decimal_precision(tick_size);
        let qty_precision = decimal_precision(qty_step);
        let instrument_id = format!("{base}-{quote}-PERP");

        out.push(CatalogInstrument {
            spec: InstrumentSpec {
                instrument_id,
                venue: "binance".to_owned(),
                symbol: symbol.to_owned(),
                asset_class: "crypto_perp".to_owned(),
                tick_size,
                qty_step,
                min_qty,
                price_precision,
                qty_precision,
            },
            display_name: Some(format!("{base}/{quote} Perpetual (Binance)")),
            base_asset: Some(base.to_owned()),
            quote_asset: Some(quote.to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        });
    }

    cache_put(key, out.clone());
    Ok(out)
}

// =========================================================================
// Hyperliquid Perpetuals (`hyperliquid_perp`)
// =========================================================================

pub async fn fetch_hyperliquid_catalog(
    base_url: &str,
    timeout: Duration,
) -> Result<Vec<CatalogInstrument>, PackError> {
    let key = format!("hyperliquid:{base_url}");
    if let Some(cached) = cache_get(&key) {
        return Ok(cached);
    }

    let client = build_http_client(base_url, timeout)?;
    let url = format!("{}/info", base_url.trim_end_matches('/'));
    let resp: Value = client
        .post(&url)
        .json(&serde_json::json!({ "type": "meta" }))
        .send()
        .await
        .map_err(|e| PackError::FeedTransport(format!("hyperliquid catalog request failed: {e}")))?
        .error_for_status()
        .map_err(|e| PackError::FeedTransport(format!("hyperliquid catalog HTTP error: {e}")))?
        .json()
        .await
        .map_err(|e| PackError::FeedTransport(format!("hyperliquid catalog JSON error: {e}")))?;

    let universe = resp
        .get("universe")
        .and_then(Value::as_array)
        .ok_or_else(|| PackError::FeedTransport("hyperliquid catalog missing universe array".to_owned()))?;

    let mut out = Vec::with_capacity(universe.len());
    for row in universe {
        if row
            .get("isDelisted")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let Some(coin) = row.get("name").and_then(Value::as_str) else {
            continue;
        };
        let sz_decimals = row
            .get("szDecimals")
            .and_then(Value::as_i64)
            .unwrap_or(2)
            .clamp(0, 8) as i32;
        let price_precision = (6 - sz_decimals).clamp(1, 6);
        let tick_size = step_from_precision(price_precision);
        let qty_step = step_from_precision(sz_decimals);
        let instrument_id = format!("{coin}-USD-HL-PERP");

        out.push(CatalogInstrument {
            spec: InstrumentSpec {
                instrument_id,
                venue: "hyperliquid".to_owned(),
                symbol: coin.to_owned(),
                asset_class: "crypto_perp".to_owned(),
                tick_size,
                qty_step,
                min_qty: qty_step,
                price_precision,
                qty_precision: sz_decimals,
            },
            display_name: Some(format!("{coin}/USD Perpetual (Hyperliquid)")),
            base_asset: Some(coin.to_owned()),
            quote_asset: Some("USD".to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        });
    }

    cache_put(key, out.clone());
    Ok(out)
}

// =========================================================================
// Coinbase Exchange Spot (`coinbase_spot`)
// =========================================================================

pub async fn fetch_coinbase_spot_catalog(
    base_url: &str,
    timeout: Duration,
) -> Result<Vec<CatalogInstrument>, PackError> {
    let key = format!("coinbase_spot:{base_url}");
    if let Some(cached) = cache_get(&key) {
        return Ok(cached);
    }

    let client = build_http_client(base_url, timeout)?;
    let url = format!("{}/products", base_url.trim_end_matches('/'));
    let resp: Value = client
        .get(&url)
        .send()
        .await
        .map_err(|e| PackError::FeedTransport(format!("coinbase catalog request failed: {e}")))?
        .error_for_status()
        .map_err(|e| PackError::FeedTransport(format!("coinbase catalog HTTP error: {e}")))?
        .json()
        .await
        .map_err(|e| PackError::FeedTransport(format!("coinbase catalog JSON error: {e}")))?;

    let list = resp
        .as_array()
        .ok_or_else(|| PackError::FeedTransport("coinbase catalog expected JSON array".to_owned()))?;

    let mut out = Vec::with_capacity(list.len());
    for row in list {
        let status = row.get("status").and_then(Value::as_str).unwrap_or("");
        if !status.eq_ignore_ascii_case("online") {
            continue;
        }
        if row
            .get("trading_disabled")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let Some(id) = row.get("id").and_then(Value::as_str) else {
            continue;
        };
        let base = row
            .get("base_currency")
            .and_then(Value::as_str)
            .unwrap_or(id);
        let quote = row
            .get("quote_currency")
            .and_then(Value::as_str)
            .unwrap_or("USD");
        if quote != "USD" && quote != "USDT" && quote != "USDC" {
            continue;
        }
        let tick_size = row
            .get("quote_increment")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or_else(|| step_from_precision(2));
        let qty_step = row
            .get("base_increment")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or_else(|| step_from_precision(4));
        let min_qty = row
            .get("base_min_size")
            .and_then(Value::as_str)
            .and_then(parse_pos_decimal)
            .unwrap_or(qty_step);

        let price_precision = decimal_precision(tick_size);
        let qty_precision = decimal_precision(qty_step);
        let instrument_id = format!("{base}-{quote}-CB-SPOT");

        out.push(CatalogInstrument {
            spec: InstrumentSpec {
                instrument_id,
                venue: "coinbase".to_owned(),
                symbol: id.to_owned(),
                asset_class: "crypto_spot".to_owned(),
                tick_size,
                qty_step,
                min_qty,
                price_precision,
                qty_precision,
            },
            display_name: Some(format!("{base}/{quote} Spot (Coinbase)")),
            base_asset: Some(base.to_owned()),
            quote_asset: Some(quote.to_owned()),
            status: Some("TRADING".to_owned()),
            registered: false,
        });
    }

    cache_put(key, out.clone());
    Ok(out)
}

// =========================================================================
// Alpaca US Equity (`us_equity`)
// =========================================================================

const DEFAULT_ALPACA_ASSETS_BASE: &str = "https://paper-api.alpaca.markets";

fn default_us_equity_directory() -> Vec<(&'static str, &'static str)> {
    vec![
        ("AAPL", "Apple Inc."),
        ("MSFT", "Microsoft Corporation"),
        ("NVDA", "NVIDIA Corporation"),
        ("AMZN", "Amazon.com, Inc."),
        ("GOOGL", "Alphabet Inc."),
        ("META", "Meta Platforms, Inc."),
        ("TSLA", "Tesla, Inc."),
        ("AMD", "Advanced Micro Devices, Inc."),
        ("NFLX", "Netflix, Inc."),
        ("AVGO", "Broadcom Inc."),
        ("SPY", "SPDR S&P 500 ETF Trust"),
        ("QQQ", "Invesco QQQ Trust"),
        ("IWM", "iShares Russell 2000 ETF"),
        ("COIN", "Coinbase Global, Inc."),
        ("MSTR", "MicroStrategy Incorporated"),
        ("PLTR", "Palantir Technologies Inc."),
    ]
}

fn make_us_equity_catalog_item(symbol: &str, name: Option<String>) -> CatalogInstrument {
    let sym = symbol.trim().to_ascii_uppercase();
    CatalogInstrument {
        spec: InstrumentSpec {
            instrument_id: sym.clone(),
            venue: "alpaca".to_owned(),
            symbol: sym.clone(),
            asset_class: "us_equity".to_owned(),
            tick_size: step_from_precision(2),
            qty_step: Decimal::ONE,
            min_qty: Decimal::ONE,
            price_precision: 2,
            qty_precision: 0,
        },
        display_name: name.or_else(|| Some(format!("{sym} (US Equity)"))),
        base_asset: Some(sym),
        quote_asset: Some("USD".to_owned()),
        status: Some("TRADING".to_owned()),
        registered: false,
    }
}

pub async fn fetch_alpaca_equity_catalog(
    base_url: Option<&str>,
    query: &str,
    timeout: Duration,
) -> Result<Vec<CatalogInstrument>, PackError> {
    let assets_base = base_url
        .filter(|u| !u.contains("data.alpaca.markets"))
        .map(str::to_owned)
        .or_else(|| std::env::var("APCA_API_BASE_URL").ok())
        .unwrap_or_else(|| DEFAULT_ALPACA_ASSETS_BASE.to_owned());

    let api_key = std::env::var("APCA_API_KEY_ID").ok();
    let api_secret = std::env::var("APCA_API_SECRET_KEY").ok();

    let key = format!("alpaca_equity:{assets_base}");
    if let Some(cached) = cache_get(&key) {
        return Ok(cached);
    }

    let mut fetched: Option<Vec<CatalogInstrument>> = None;
    if (api_key.is_some() && api_secret.is_some()) || is_loopback_url(&assets_base) {
        let client = build_http_client(&assets_base, timeout)?;
        let url = format!(
            "{}/v2/assets?status=active&asset_class=us_equity",
            assets_base.trim_end_matches('/')
        );
        let mut req = client.get(&url);
        if let (Some(k), Some(s)) = (&api_key, &api_secret) {
            req = req
                .header("APCA-API-KEY-ID", k)
                .header("APCA-API-SECRET-KEY", s);
        }
        if let Ok(resp) = req.send().await
            && let Ok(ok_resp) = resp.error_for_status()
            && let Ok(Value::Array(rows)) = ok_resp.json::<Value>().await
        {
            let mut items = Vec::with_capacity(rows.len());
            for row in rows {
                if !row.get("tradable").and_then(Value::as_bool).unwrap_or(true) {
                    continue;
                }
                let Some(sym) = row.get("symbol").and_then(Value::as_str) else {
                    continue;
                };
                if sym.is_empty() || !sym.chars().all(|c| c.is_ascii_alphanumeric() || c == '.') {
                    continue;
                }
                let name = row.get("name").and_then(Value::as_str).map(str::to_owned);
                items.push(make_us_equity_catalog_item(sym, name));
            }
            if !items.is_empty() {
                cache_put(key, items.clone());
                fetched = Some(items);
            }
        }
    }

    if let Some(items) = fetched {
        return Ok(items);
    }

    // Fallback when Alpaca trading credentials are not configured: built-in directory +
    // synthetic entry for a valid user-typed ticker (1..=6 uppercase letters).
    let mut items: Vec<CatalogInstrument> = default_us_equity_directory()
        .into_iter()
        .map(|(sym, name)| make_us_equity_catalog_item(sym, Some(name.to_owned())))
        .collect();
    let trimmed = query.trim().to_ascii_uppercase();
    if !trimmed.is_empty()
        && trimmed.len() <= 6
        && trimmed.chars().all(|c| c.is_ascii_alphabetic())
        && !items.iter().any(|i| i.spec.symbol == trimmed)
    {
        items.push(make_us_equity_catalog_item(&trimmed, None));
    }
    Ok(items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filter_and_rank_prioritizes_exact_and_prefix() {
        let items = vec![
            make_us_equity_catalog_item("MS", Some("Morgan Stanley".to_owned())),
            make_us_equity_catalog_item("MSFT", Some("Microsoft".to_owned())),
            make_us_equity_catalog_item("AMSC", Some("American Superconductor".to_owned())),
        ];
        let ranked = filter_and_rank_catalog(items, "ms", 10);
        let ids: Vec<&str> = ranked.iter().map(|i| i.spec.instrument_id.as_str()).collect();
        assert_eq!(ids, vec!["MS", "MSFT", "AMSC"]);
    }

    #[test]
    fn decimal_precision_strips_trailing_zeros() {
        assert_eq!(decimal_precision(Decimal::from_str("0.0100").unwrap()), 2);
        assert_eq!(decimal_precision(Decimal::from_str("1.000").unwrap()), 0);
        assert_eq!(decimal_precision(Decimal::from_str("0.000001").unwrap()), 6);
    }
}
