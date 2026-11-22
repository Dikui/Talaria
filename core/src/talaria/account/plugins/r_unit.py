"""按最差入场价至硬止损的距离定仓。"""

from collections.abc import Mapping
from fractions import Fraction

from talaria.account.contracts import AccountState, EntryCandidate, SizingDecision, SizingPreview, SizingRejected
from talaria.account.plugins.risk_math import (
    candidate_exposure, equity, isolated_liquidation, portfolio, sign, unit_risk, valid_entry,
    visible_marks, worst_entry,
)
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.scheme import PluginContext, PluginParams, Slot, SlotPlugin
from talaria.foundation.margin import MarginValuation
from talaria.foundation.market import MarketPack
from talaria.packs import DEFAULT_PACK, get_market_pack


_DEFAULT = AccountRiskConfig()


class RUnitSizer:
    def __init__(self, pack: MarketPack, accounts: Mapping[str, AccountRiskConfig]) -> None:
        self._pack = pack
        self._accounts = accounts

    def size(
        self, candidate: EntryCandidate, account_state: AccountState, valuation: MarginValuation | None,
    ) -> SizingDecision:
        config = self._accounts.get(account_state.sub_account_id, _DEFAULT)
        instrument = self._pack.instrument(candidate.instrument_id)
        if instrument is None:
            raise SizingRejected("UNKNOWN_INSTRUMENT")
        position = account_state.rounds.get(candidate.round_id)
        stop = position.stop if position is not None else candidate.invalidation_stop
        if not valid_entry(candidate, stop):
            raise SizingRejected("INVALID_STOP_GEOMETRY")
        try:
            net_equity = equity(account_state, config, valuation, candidate.ts_decision)
        except (KeyError, ValueError) as exc:
            raise SizingRejected("MARGIN_VALUATION_UNAVAILABLE") from exc
        budget = net_equity * config.r_unit_pct
        entry = worst_entry(candidate)
        multiplier = self._pack.traits(candidate.instrument_id).margin.contract_multiplier
        per_unit = unit_risk(candidate.direction, entry, stop, config) * multiplier
        lots = Fraction(budget) // (Fraction(per_unit) * Fraction(instrument.qty_step))
        quantity = lots * instrument.qty_step
        if quantity < instrument.min_qty or quantity <= 0:
            raise SizingRejected("BELOW_MIN_QUANTITY")
        return SizingDecision(
            quantity=quantity,
            basis=f"risk_budget={budget}, worst_entry={entry}, stop={stop}, risk_per_unit={per_unit}",
        )

    def preview_sizing(
        self, draft: EntryCandidate, account_state: AccountState, valuation: MarginValuation | None,
    ) -> SizingPreview:
        decision = self.size(draft, account_state, valuation)
        if valuation is None:
            return SizingPreview(sizing=decision)
        config = self._accounts.get(account_state.sub_account_id, _DEFAULT)
        position = account_state.rounds.get(draft.round_id)
        stop = position.stop if position is not None else draft.invalidation_stop
        requested = candidate_exposure(draft, decision.quantity, stop)
        exposures = (*account_state.positions, *account_state.reservations, requested)
        if config.margin_mode == "ISOLATED":
            group = [item for item in exposures if item.round_id == requested.round_id]
            price = isolated_liquidation(group, config, self._pack, valuation, draft.ts_decision)
        else:
            holdings = portfolio(account_state, config, exposures)
            try:
                marks = visible_marks(account_state, config, holdings.positions, draft.ts_decision)
                price = valuation.cross_liquidation_price(
                    holdings, marks, draft.instrument_id, as_of=draft.ts_decision,
                )
            except (KeyError, ValueError):
                price = None
        return SizingPreview(
            sizing=decision, liquidation_price=price,
            liquidation_distance=None if price is None else sign(draft.direction) * (stop - price),
        )


def _create(context: PluginContext, params: PluginParams) -> RUnitSizer:
    return RUnitSizer(get_market_pack(DEFAULT_PACK), context.account_risk)


r_unit = SlotPlugin(Slot.SIZING, (1,), _create)
