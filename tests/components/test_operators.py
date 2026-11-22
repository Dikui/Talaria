"""analysis/02: versioned operators declare their parameter schema and golden vector."""

from dataclasses import replace
from decimal import localcontext

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
