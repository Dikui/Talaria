"""使用组件运行时的示例策略，在首次观察到目标收盘价时给出意图。"""

from decimal import Decimal

from talaria.account.scheme import Slot, SlotPlugin
from talaria.components import ComponentMeta, Ctx
from talaria.foundation.collaboration.contracts import Abstain, AbstainReason, SetupInsight


class ReplayStrategy:
    meta = ComponentMeta(
        name="replay_example", version="1", inputs=("bars",),
        parameters_schema={
            "type": "object", "required": ["trigger_price"],
            "properties": {"trigger_price": {"type": "string"}, "max_position": {"type": "string"}},
        },
        output_type="strategy_decision",
    )

    def __init__(self, trigger_price, **params):
        self.trigger_price = Decimal(trigger_price)
        self.max_position = params.get("max_position")
        self.triggered = False

    def on_bar(self, ctx: Ctx):
        bar = ctx.bars()[-1]
        if self.triggered or bar.close != self.trigger_price:
            return Abstain(reason_code=AbstainReason.EVIDENCE_CONFLICT, message="未达到入场条件")
        self.triggered = True
        return SetupInsight(
            direction="LONG", summary="达到目标收盘价",
            entry_low=bar.close, entry_high=bar.close + 2,
            invalidation_stop=bar.close - 2, take_profit=[bar.close + 2],
            max_position=self.max_position,
        )


def make_strategy(_context, params):
    return ReplayStrategy(**params)


plugin = SlotPlugin(Slot.STRATEGY, (2,), make_strategy, ReplayStrategy.meta.parameters_schema)
