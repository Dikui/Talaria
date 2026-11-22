"""交易所连接的回环直连判断。"""

from ipaddress import ip_address
from urllib.parse import urlsplit


def is_loopback_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
        if host is None:
            return False
        return host in ("localhost", "localhost.") or ip_address(host).is_loopback
    except ValueError:
        return False
