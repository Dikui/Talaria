"""analysis/02: versioned operators declare their parameter schema and golden vector."""

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext

import pytest

from talaria.foundation.operators import IndicatorName

pytestmark = [pytest.mark.small, pytest.mark.spec]


def test_builtin_operators_register_with_versioned_parameters_and_a_golden_vector():
    from talaria.components.operators import OPERATORS

    for name in IndicatorName:
        operator = OPERATORS.get(name, version="1")
        assert operator.name == name
        assert operator.parameters_schema["properties"]["period"]["default"] >= 1
        assert operator.golden is not None
        assert operator.golden.expected
        assert operator.compute(
            operator.golden.bars, operator.golden.params, derivatives=operator.golden.derivatives,
        ) == operator.golden.expected

    boll = OPERATORS.get("BOLL", version="1")
    assert boll.parameters_schema["properties"]["period"]["minimum"] == 2
    assert boll.parameters_schema["properties"]["std_dev"]["default"] == "2"
    vwap = OPERATORS.get("VWAP", version="1")
    assert vwap.parameters_schema["properties"]["anchor"]["enum"] == ["snapshot", "session"]


def test_registration_requires_a_golden_vector():
    from talaria.components.operators import OPERATORS, OperatorRegistry

    sma = OPERATORS.get("SMA", version="1")
    registry = OperatorRegistry()
    with pytest.raises(ValueError, match="黄金向量"):
        registry.register(replace(sma, golden=None))
    with pytest.raises(KeyError):
        registry.get(sma.name, version=sma.version)


def test_registry_rejects_changed_results_under_an_existing_golden_vector():
    from talaria.components.operators import OPERATORS, OperatorRegistry

    sma = OPERATORS.get("SMA", version="1")

    def mismatched_compute(bars, params, *, derivatives):
        return ({"value": None}, {"value": None})

    registry = OperatorRegistry()
    with pytest.raises(ValueError, match="黄金向量"):
        registry.register(replace(sma, compute=mismatched_compute))


def test_golden_verification_uses_consistent_decimal_precision():
    from talaria.components.operators import OPERATORS, OperatorRegistry

    funding = OPERATORS.get("FUNDING_QUANTILE", version="1")
    registry = OperatorRegistry()
    with localcontext() as context:
        context.prec = 2
        registry.register(funding)
    assert registry.get(funding.name, version=funding.version) == funding


@pytest.mark.regression
@pytest.mark.parametrize("entry_point", ["registered", "batch", "incremental"])
def test_operator_evaluation_uses_golden_decimal_context(entry_point):
    from talaria.components.operators import OPERATORS
    from talaria.foundation.operators import IndicatorSpec, compute_series, update_series

    operator = OPERATORS.get("FUNDING_QUANTILE", version="1")
    vector = operator.golden
    spec = IndicatorSpec(IndicatorName.FUNDING_QUANTILE, period=1)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        if entry_point == "registered":
            result = operator.compute(vector.bars, vector.params, derivatives=vector.derivatives)
        elif entry_point == "batch":
            result = compute_series(vector.bars, spec, derivatives=vector.derivatives).points
        else:
            state = compute_series((), spec, derivatives=())
            result = update_series(state, vector.bars, spec, derivatives=vector.derivatives).points
        assert context.prec == 2
        assert context.rounding == ROUND_DOWN
        assert context.traps[Inexact]
    assert result == vector.expected


@pytest.mark.regression
@pytest.mark.parametrize(("name", "expected"), [
    (IndicatorName.EMA, (None, Decimal(11), Decimal(13), None, None, Decimal(21))),
    (IndicatorName.ATR, (None, None, Decimal(3), None, None, None)),
])
def test_registered_legacy_variant_matches_old_alert_outputs_point_by_point(name, expected):
    from talaria.components.operators import OPERATORS
    from talaria.foundation.bars import BarStatus
    from talaria.foundation.operators import IndicatorSpec
    from talaria.foundation.operators.core import legacy_alert_points
    from tests.foundation.operators.test_core import bar

    bars = [
        bar(0, close="10"), bar(1, close="12"), bar(2, close="14", high="16"),
        bar(3, status=BarStatus.FEED_GAP),
        bar(4, close="20"), bar(5, close="22"),
    ]
    spec = IndicatorSpec(name, period=2)
    legacy = OPERATORS.get(f"legacy_{name.lower()}", version="1")
    assert tuple(legacy_alert_points(bars, spec)) == legacy.compute(
        bars, {"period": 2}, derivatives=None,
    ) == tuple({"value": value} for value in expected)
