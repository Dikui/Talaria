import { describe, expect, it } from "vitest";
import type { InstrumentSpec } from "@talaria/contracts";

import {
  extractBaseAsset,
  groupInstrumentsByVenue,
  pickInstrumentForVenue,
  symbolDisplayName,
  venueDisplayName,
  venueShortLabel,
} from "./venues";

function makeSpec(instrument_id: string, venue: string): InstrumentSpec {
  return {
    instrument_id,
    venue,
    symbol: instrument_id,
    asset_class: "CRYPTO_PERP",
    tick_size: "0.1",
    qty_step: "0.001",
    min_qty: "0.001",
    price_precision: 1,
    qty_precision: 3,
  };
}

describe("workbench venue helpers", () => {
  const instruments: InstrumentSpec[] = [
    makeSpec("BTC-USDT-PERP", "okx"),
    makeSpec("ETH-USDT-PERP", "okx"),
    makeSpec("BTC-BN-USDT-PERP", "binance"),
    makeSpec("SOL-BN-USDT-PERP", "binance"),
    makeSpec("BTC-USDT-BN-SPOT", "binance_spot"),
    makeSpec("ETH-HL-USD-PERP", "hyperliquid"),
    makeSpec("BTC-CB-USD-SPOT", "coinbase"),
  ];

  it("formats venueDisplayName in zh and en locales", () => {
    expect(venueDisplayName("okx", "zh")).toBe("OKX");
    expect(venueDisplayName("okx", "en")).toBe("OKX");
    expect(venueDisplayName("binance", "zh")).toBe("Binance 永续 (归档)");
    expect(venueDisplayName("binance", "en")).toBe("Binance Perp (Archive)");
    expect(venueDisplayName("binance_spot", "zh")).toBe("Binance 现货 (归档)");
    expect(venueDisplayName("binance_spot", "en")).toBe("Binance Spot (Archive)");
    expect(venueDisplayName("hyperliquid", "en")).toBe("Hyperliquid");
    expect(venueDisplayName("coinbase", "en")).toBe("Coinbase");
    expect(venueDisplayName("bybit", "en")).toBe("Bybit");
    expect(venueDisplayName("alpaca", "en")).toBe("Alpaca");
    expect(venueDisplayName("kraken", "en")).toBe("KRAKEN");

    expect(venueShortLabel("hyperliquid")).toBe("HL");
    expect(venueShortLabel("binance")).toBe("BN Perp");
    expect(venueShortLabel("binance_spot")).toBe("BN Spot");
    expect(venueShortLabel("kraken")).toBe("KRAKEN");
  });

  it("strips venue-disambiguation infixes in symbolDisplayName", () => {
    expect(symbolDisplayName({ instrument_id: "BTC-USDT-PERP", venue: "okx" })).toBe(
      "BTC-USDT-PERP",
    );
    expect(symbolDisplayName({ instrument_id: "BTC-BN-USDT-PERP", venue: "binance" })).toBe(
      "BTC-USDT-PERP",
    );
    expect(symbolDisplayName({ instrument_id: "BTC-USDT-BN-SPOT", venue: "binance_spot" })).toBe(
      "BTC-USDT-SPOT",
    );
    expect(symbolDisplayName({ instrument_id: "BTC-HL-USD-PERP", venue: "hyperliquid" })).toBe(
      "BTC-USD-PERP",
    );
    expect(symbolDisplayName({ instrument_id: "BTC-CB-USD-SPOT", venue: "coinbase" })).toBe(
      "BTC-USD-SPOT",
    );
  });

  it("groups instruments by venue preserving first-seen order", () => {
    const grouped = groupInstrumentsByVenue(instruments);
    expect(grouped.map((g) => g.venue)).toEqual([
      "okx",
      "binance",
      "binance_spot",
      "hyperliquid",
      "coinbase",
    ]);
    expect(grouped[0].instruments.map((i) => i.instrument_id)).toEqual([
      "BTC-USDT-PERP",
      "ETH-USDT-PERP",
    ]);
    expect(grouped[1].instruments.map((i) => i.instrument_id)).toEqual([
      "BTC-BN-USDT-PERP",
      "SOL-BN-USDT-PERP",
    ]);
    expect(grouped[2].instruments.map((i) => i.instrument_id)).toEqual([
      "BTC-USDT-BN-SPOT",
    ]);
  });

  it("picks matching base asset instrument for target venue, or falls back to first", () => {
    expect(extractBaseAsset("eth-bn-usdt-perp")).toBe("ETH");
    expect(
      pickInstrumentForVenue(instruments, "binance", "BTC-USDT-PERP")?.instrument_id,
    ).toBe("BTC-BN-USDT-PERP");
    expect(
      pickInstrumentForVenue(instruments, "binance_spot", "BTC-USDT-PERP")?.instrument_id,
    ).toBe("BTC-USDT-BN-SPOT");
    expect(
      pickInstrumentForVenue(instruments, "binance", "ETH-USDT-PERP")?.instrument_id,
    ).toBe("BTC-BN-USDT-PERP");
    expect(
      pickInstrumentForVenue(instruments, "hyperliquid", "ETH-USDT-PERP")?.instrument_id,
    ).toBe("ETH-HL-USD-PERP");
    expect(pickInstrumentForVenue(instruments, "bybit", "BTC-USDT-PERP")).toBeNull();
  });
});
