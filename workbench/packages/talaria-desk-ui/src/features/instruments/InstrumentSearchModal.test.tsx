import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InstrumentSpec } from "@talaria/contracts";

import { api, type CatalogInstrument } from "../../lib/api";
import { setLocale } from "../../lib/i18n";
import { InstrumentSearchModal } from "./InstrumentSearchModal";

const BTC_SPEC: InstrumentSpec = {
  instrument_id: "BTC-USDT-PERP",
  venue: "bybit",
  symbol: "BTCUSDT",
  asset_class: "crypto_perp",
  tick_size: "0.1",
  qty_step: "0.001",
  min_qty: "0.001",
  price_precision: 1,
  qty_precision: 3,
};

const DOGE_SPEC: InstrumentSpec = {
  instrument_id: "DOGE-USDT-PERP",
  venue: "bybit",
  symbol: "DOGEUSDT",
  asset_class: "crypto_perp",
  tick_size: "0.00001",
  qty_step: "1",
  min_qty: "1",
  price_precision: 5,
  qty_precision: 0,
};

const CATALOG_RESULTS: CatalogInstrument[] = [
  {
    spec: BTC_SPEC,
    display_name: "BTC/USDT Perpetual (Bybit)",
    base_asset: "BTC",
    quote_asset: "USDT",
    status: "TRADING",
    registered: true,
  },
  {
    spec: DOGE_SPEC,
    display_name: "DOGE/USDT Perpetual (Bybit)",
    base_asset: "DOGE",
    quote_asset: "USDT",
    status: "TRADING",
    registered: false,
  },
];

describe("InstrumentSearchModal (unified selector & catalog search)", () => {
  beforeEach(() => {
    setLocale("zh");
    vi.restoreAllMocks();
  });

  it("shows existing instruments immediately without remote fetch and switches directly on click", async () => {
    const searchSpy = vi.spyOn(api, "searchInstruments");
    const addSpy = vi.spyOn(api, "addInstrument");

    const onClose = vi.fn();
    const onInstrumentsChanged = vi.fn();
    const onPicked = vi.fn();

    render(
      <InstrumentSearchModal
        open={true}
        currentInstrumentId="BTC-USDT-PERP"
        existingInstruments={[BTC_SPEC]}
        onClose={onClose}
        onInstrumentsChanged={onInstrumentsChanged}
        onPicked={onPicked}
      />,
    );

    expect(screen.getByTestId("symbol-search-item-BTC-USDT-PERP")).toBeTruthy();
    expect(searchSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("symbol-search-item-BTC-USDT-PERP"));

    await waitFor(() => {
      expect(addSpy).not.toHaveBeenCalled();
      expect(onPicked).toHaveBeenCalledWith(BTC_SPEC);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it("queries remote catalog when typing and registers a new instrument on click", async () => {
    const searchSpy = vi.spyOn(api, "searchInstruments").mockResolvedValue({
      pack: "crypto_perp",
      query: "doge",
      results: CATALOG_RESULTS,
    });
    const addSpy = vi.spyOn(api, "addInstrument").mockResolvedValue({
      created: true,
      instrument: DOGE_SPEC,
      instruments: [BTC_SPEC, DOGE_SPEC],
    });

    const onClose = vi.fn();
    const onInstrumentsChanged = vi.fn();
    const onPicked = vi.fn();

    render(
      <InstrumentSearchModal
        open={true}
        existingInstruments={[BTC_SPEC]}
        onClose={onClose}
        onInstrumentsChanged={onInstrumentsChanged}
        onPicked={onPicked}
      />,
    );

    fireEvent.change(screen.getByTestId("symbol-search-input"), {
      target: { value: "doge" },
    });

    await waitFor(() => {
      expect(searchSpy).toHaveBeenCalledWith("doge", { venue: undefined, limit: 50 });
      expect(screen.getByTestId("symbol-search-item-DOGE-USDT-PERP")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("symbol-search-item-DOGE-USDT-PERP"));

    await waitFor(() => {
      expect(addSpy).toHaveBeenCalledWith(DOGE_SPEC);
      expect(onInstrumentsChanged).toHaveBeenCalledWith([BTC_SPEC, DOGE_SPEC]);
      expect(onPicked).toHaveBeenCalledWith(DOGE_SPEC);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });
});
