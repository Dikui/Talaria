"""自选观察池接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from talaria.foundation.watchlist import (
    Watchlist,
    WatchlistGroupCreate,
    WatchlistGroupUpdate,
    WatchlistItemCreate,
    WatchlistItemOrder,
    WatchlistOrder,
)
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/watchlist", response_model=Watchlist)
async def get_watchlist(request: Request) -> Watchlist:
    return await get_runtime(request).watchlist.get_watchlist()


@router.post("/api/watchlist/groups", response_model=Watchlist)
async def create_watchlist_group(
    request: Request, payload: WatchlistGroupCreate
) -> Watchlist:
    return await get_runtime(request).watchlist.create_group(payload.name)


@router.put("/api/watchlist/groups/order", response_model=Watchlist)
async def order_watchlist_groups(
    request: Request, payload: WatchlistOrder
) -> Watchlist:
    return await get_runtime(request).watchlist.order_groups(payload.ids)


@router.patch("/api/watchlist/groups/{group_id}", response_model=Watchlist)
async def rename_watchlist_group(
    request: Request, group_id: str, payload: WatchlistGroupUpdate
) -> Watchlist:
    return await get_runtime(request).watchlist.rename_group(
        group_id, payload.name
    )


@router.delete("/api/watchlist/groups/{group_id}", response_model=Watchlist)
async def delete_watchlist_group(
    request: Request, group_id: str
) -> Watchlist:
    return await get_runtime(request).watchlist.delete_group(group_id)


@router.post("/api/watchlist/items", response_model=Watchlist)
async def add_watchlist_item(
    request: Request, payload: WatchlistItemCreate
) -> Watchlist:
    return await get_runtime(request).watchlist.add_item(
        payload.group_id, payload.instrument_id
    )


@router.put("/api/watchlist/items/order", response_model=Watchlist)
async def order_watchlist_items(
    request: Request, payload: WatchlistItemOrder
) -> Watchlist:
    return await get_runtime(request).watchlist.order_items(
        payload.group_id, payload.ids
    )


@router.delete(
    "/api/watchlist/items/{instrument_id}",
    response_model=Watchlist,
)
async def remove_watchlist_item(
    request: Request, instrument_id: str
) -> Watchlist:
    return await get_runtime(request).watchlist.remove_item(instrument_id)
