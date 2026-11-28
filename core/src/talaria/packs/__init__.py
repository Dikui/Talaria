"""横向市场规则包注册表，供启动时按名称选择。"""

from talaria.foundation.market import MarketPack
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

DEFAULT_PACK = CRYPTO_PERP_PACK.name
MARKET_PACKS: dict[str, MarketPack] = {CRYPTO_PERP_PACK.name: CRYPTO_PERP_PACK}


def get_market_pack(name: str) -> MarketPack:
    """返回已注册的市场包；未知名称抛出 ``KeyError``。"""
    return MARKET_PACKS[name]


from talaria.packs.catalog import CatalogMarketPack, resolve_market_pack, traits_for_spec  # noqa: E402

__all__ = [
    "CatalogMarketPack",
    "DEFAULT_PACK",
    "MARKET_PACKS",
    "get_market_pack",
    "resolve_market_pack",
    "traits_for_spec",
]
