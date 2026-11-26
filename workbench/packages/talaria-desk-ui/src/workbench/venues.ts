import type { InstrumentSpec } from "@talaria/contracts";

import { getLocale, type Locale } from "../lib/i18n";

export function venueDisplayName(venue: string, _locale: Locale = getLocale()): string {
  switch (venue.toLowerCase()) {
    case "okx":
      return "OKX";
    case "binance":
      return "Binance";
    case "hyperliquid":
      return "Hyperliquid";
    case "coinbase":
      return "Coinbase";
    case "bybit":
      return "Bybit";
    case "alpaca":
      return "Alpaca";
    default:
      return venue.toUpperCase();
  }
}

export function venueShortLabel(venue: string): string {
  switch (venue.toLowerCase()) {
    case "okx":
      return "OKX";
    case "binance":
      return "BN";
    case "hyperliquid":
      return "HL";
    case "coinbase":
      return "Coinbase";
    case "bybit":
      return "Bybit";
    case "alpaca":
      return "Alpaca";
    default:
      return venue.toUpperCase();
  }
}

export function symbolDisplayName(
  spec: Pick<InstrumentSpec, "instrument_id" | "venue">,
): string {
  return spec.instrument_id.replace(/-(?:BN|HL|CB)-/g, "-");
}

export function extractBaseAsset(instrumentId: string): string {
  const dash = instrumentId.indexOf("-");
  return (dash >= 0 ? instrumentId.slice(0, dash) : instrumentId).toUpperCase();
}

export function groupInstrumentsByVenue(
  instruments: InstrumentSpec[],
): Array<{ venue: string; instruments: InstrumentSpec[] }> {
  const groups = new Map<string, InstrumentSpec[]>();
  for (const inst of instruments) {
    let list = groups.get(inst.venue);
    if (!list) {
      list = [];
      groups.set(inst.venue, list);
    }
    list.push(inst);
  }
  return Array.from(groups, ([venue, list]) => ({ venue, instruments: list }));
}

export function pickInstrumentForVenue(
  instruments: InstrumentSpec[],
  targetVenue: string,
  currentInstrumentId: string,
): InstrumentSpec | null {
  const venueInstruments = instruments.filter((inst) => inst.venue === targetVenue);
  if (venueInstruments.length === 0) return null;
  const base = extractBaseAsset(currentInstrumentId);
  const currentAssetClass = instruments
    .find((inst) => inst.instrument_id === currentInstrumentId)
    ?.asset_class.toLowerCase();
  if (currentAssetClass) {
    const exact = venueInstruments.find(
      (inst) =>
        extractBaseAsset(inst.instrument_id) === base &&
        inst.asset_class.toLowerCase() === currentAssetClass,
    );
    if (exact) return exact;
  }
  return (
    venueInstruments.find((inst) => extractBaseAsset(inst.instrument_id) === base) ??
    venueInstruments[0]
  );
}
