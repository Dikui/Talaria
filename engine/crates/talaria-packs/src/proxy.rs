//! Loopback URL detection so local test servers bypass host proxy environment variables.

use std::net::IpAddr;
use std::str::FromStr;

/// Return `true` if `url` points to `localhost`, `localhost.`, or a loopback IP address.
pub fn is_loopback_url(url: &str) -> bool {
    let Some((_, rest)) = url.split_once("://") else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host_port = authority
        .rsplit_once('@')
        .map_or(authority, |(_, hp)| hp);
    if host_port.is_empty() {
        return false;
    }

    let host = if let Some(inner) = host_port.strip_prefix('[') {
        match inner.split_once(']') {
            Some((h, _)) => h,
            None => return false,
        }
    } else if host_port.matches(':').count() == 1 {
        host_port.split_once(':').map_or(host_port, |(h, _)| h)
    } else {
        host_port
    };

    host.eq_ignore_ascii_case("localhost")
        || host.eq_ignore_ascii_case("localhost.")
        || IpAddr::from_str(host).is_ok_and(|ip| ip.is_loopback())
}

#[cfg(test)]
mod tests {
    use super::is_loopback_url;

    #[test]
    fn detects_loopback_urls() {
        assert!(is_loopback_url("http://127.0.0.1:8080/v5/market/kline"));
        assert!(is_loopback_url("http://localhost:8765"));
        assert!(is_loopback_url("http://localhost./api"));
        assert!(is_loopback_url("ws://[::1]:9000/ws"));
        assert!(is_loopback_url("http://::1"));
        assert!(!is_loopback_url("https://api.bybit.com/v5/market/kline"));
        assert!(!is_loopback_url("wss://stream.bybit.com/v5/public/linear"));
        assert!(!is_loopback_url("not-a-url"));
    }
}
