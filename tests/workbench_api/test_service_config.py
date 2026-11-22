from __future__ import annotations

import json
import stat

import pytest

from talaria.workbench_api.service_config import (
    FEATURE_LABELS,
    FEATURE_NAMES,
    FeatureToggles,
    ServiceConfigError,
    describe_features,
    load_service_config,
    service_config_path,
    write_default_service_config,
)

pytestmark = [pytest.mark.small, pytest.mark.spec]


def _write_config(data_dir, payload) -> None:
    path = service_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_config_enables_every_feature_and_does_not_create_it(tmp_path):
    config = load_service_config(tmp_path)

    assert config.features == FeatureToggles()
    assert config.features.enabled_names() == list(FEATURE_NAMES)
    assert config.features.disabled_names() == []
    assert config.path == tmp_path / "service.json"
    assert config.exists is False
    assert not config.path.exists()


def test_full_config_overrides_every_feature(tmp_path):
    _write_config(
        tmp_path,
        {
            "version": 1,
            "features": {
                "market_data": False,
                "cold_store": False,
                "alerts": False,
                "scanner": False,
            },
        },
    )

    config = load_service_config(tmp_path)

    assert config.exists is True
    assert config.features.market_data is False
    assert config.features.cold_store is False
    assert config.features.alerts is False
    assert config.features.scanner is False
    assert config.features.disabled_names() == list(FEATURE_NAMES)


def test_partial_config_keeps_unspecified_features_enabled(tmp_path):
    _write_config(tmp_path, {"version": 1, "features": {"scanner": False}})

    features = load_service_config(tmp_path).features

    assert features == FeatureToggles(scanner=False)
    assert features.enabled_names() == ["market_data", "cold_store", "alerts"]
    assert features.disabled_names() == ["scanner"]


def test_version_may_be_omitted(tmp_path):
    _write_config(tmp_path, {"features": {"alerts": False}})

    assert load_service_config(tmp_path).features.alerts is False


def test_invalid_json_reports_path(tmp_path):
    path = service_config_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ServiceConfigError) as excinfo:
        load_service_config(tmp_path)

    message = str(excinfo.value)
    assert str(path) in message
    assert "JSON" in message


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([], "JSON 对象"),
        ({"version": 2, "features": {}}, "version"),
        ({"version": True, "features": {}}, "version"),
        ({"features": []}, "features"),
        ({"features": {"warmup": True}}, "warmup"),
        ({"features": {"alerts": "off"}}, "alerts"),
        ({"features": {"alerts": 1}}, "alerts"),
    ],
)
def test_invalid_payloads_are_rejected(tmp_path, payload, expected):
    _write_config(tmp_path, payload)

    with pytest.raises(ServiceConfigError) as excinfo:
        load_service_config(tmp_path)

    message = str(excinfo.value)
    assert expected in message
    assert str(service_config_path(tmp_path)) in message
    if "features" in payload and isinstance(payload["features"], dict):
        bad_key = next(
            (key for key in payload["features"] if key not in FEATURE_NAMES),
            None,
        )
        if bad_key is not None:
            assert "可用开关" in message


def test_config_allows_comment_metadata(tmp_path):
    _write_config(
        tmp_path,
        {"version": 1, "comment": "由 talaria service install 生成", "features": {"alerts": False}},
    )

    assert load_service_config(tmp_path).features.alerts is False


@pytest.mark.regression
@pytest.mark.parametrize("key", ["feature", "featuers", "verison", "scanner"])
def test_unknown_top_level_key_reports_expected_keys(tmp_path, key):
    # platform/01: 开关拼写错误不能退回默认全开。
    _write_config(tmp_path, {key: {"scanner": False}})

    with pytest.raises(ServiceConfigError) as excinfo:
        load_service_config(tmp_path)

    message = str(excinfo.value)
    assert key in message
    assert "features" in message
    assert str(service_config_path(tmp_path)) in message


def test_write_default_config_is_readable_and_does_not_overwrite(tmp_path):
    path = write_default_service_config(tmp_path)

    assert path == service_config_path(tmp_path)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["features"] == {name: True for name in FEATURE_NAMES}

    path.write_text(json.dumps({"features": {"scanner": False}}), encoding="utf-8")
    assert write_default_service_config(tmp_path) == path
    assert load_service_config(tmp_path).features.scanner is False


def test_describe_features_lists_every_switch_in_order():
    description = describe_features(FeatureToggles(cold_store=False, alerts=False))

    labels = "；".join(
        f"{FEATURE_LABELS[name]}={'开' if name in ('market_data', 'scanner') else '关'}"
        for name in FEATURE_NAMES
    )
    assert description == labels
