from datetime import time

import pytest

from inkypull.config import (
    DEFAULT_PINS,
    ConfigError,
    in_quiet_hours,
    load,
    parse,
    parse_duration,
)

URL = "http://hearth.local/display/frame"


def test_parse_minimal_uses_defaults():
    config = parse({"url": URL})
    assert config.url == URL
    assert config.poll_interval == 300
    assert config.buttons == {}


def test_parse_missing_url_raises():
    with pytest.raises(ConfigError, match="url"):
        parse({})


def test_parse_unknown_setting_raises():
    with pytest.raises(ConfigError, match="pol_interval"):
        parse({"url": URL, "pol_interval": 5})


@pytest.mark.parametrize(
    ("value", "seconds"), [(90, 90), ("90", 90), ("1.5s", 1.5), ("5m", 300), ("2h", 7200)]
)
def test_parse_duration_accepts_units(value, seconds):
    assert parse_duration(value, "x") == seconds


@pytest.mark.parametrize("value", ["soon", -1, 0, True])
def test_parse_duration_rejects_bad_values(value):
    with pytest.raises(ConfigError):
        parse_duration(value, "x")


def test_buttons_default_pins_and_actions():
    config = parse(
        {
            "url": URL,
            "buttons": {
                "long_press": "2s",
                "A": {"press": "refresh"},
                "B": {
                    "press": {
                        "action": "http",
                        "url": "http://hearth.local/mode",
                        "then": "refresh",
                    },
                    "long_press": {"action": "command", "run": ["systemctl", "reboot"]},
                },
            },
        }
    )
    assert config.long_press == 2
    assert config.buttons["A"].pin == DEFAULT_PINS["A"]
    assert config.buttons["A"].press.kind == "refresh"
    b = config.buttons["B"]
    assert (b.press.kind, b.press.method, b.press.then) == ("http", "POST", "refresh")
    assert b.long_press.run == ("systemctl", "reboot")


def test_buttons_custom_pins():
    config = parse({"url": URL, "buttons": {"pins": {"C": 16}, "C": {"press": "status"}}})
    assert config.buttons["C"].pin == 16


@pytest.mark.parametrize(
    ("buttons", "message"),
    [
        ({"E": {"press": "refresh"}}, "buttons.E"),
        ({"A": {"press": "explode"}}, "unknown action"),
        ({"A": {"press": {"action": "http"}}}, "needs url"),
        ({"A": {"press": {"action": "refresh", "url": URL}}}, "doesn't take url"),
        ({"A": {"press": {"action": "command", "run": "reboot"}}}, "list of strings"),
        ({"A": {"press": {"action": "http", "url": URL, "then": "status"}}}, "then"),
        ({"A": {"tap": "refresh"}}, "unknown field"),
        ({"pins": {"Z": 3}}, "unknown button"),
    ],
)
def test_buttons_invalid_config_names_the_problem(buttons, message):
    with pytest.raises(ConfigError, match=message):
        parse({"url": URL, "buttons": buttons})


def test_sources_include_cycle_urls():
    other = "http://hearth.local/display/frame?mode=ambient"
    config = parse(
        {"url": URL, "buttons": {"C": {"press": {"action": "cycle", "urls": [URL, other]}}}}
    )
    assert config.sources == (URL, other)


def test_quiet_hours_wraps_midnight():
    quiet = parse({"url": URL, "quiet_hours": "23:00-06:30"}).quiet_hours
    assert in_quiet_hours(quiet, time(23, 30))
    assert in_quiet_hours(quiet, time(3, 0))
    assert not in_quiet_hours(quiet, time(12, 0))


def test_load_reads_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'url = "{URL}"\npoll_interval = "10m"\nrotate = "auto"\n')
    config = load(path)
    assert (config.poll_interval, config.rotate) == (600, "auto")


def test_load_example_configs_are_valid():
    from pathlib import Path

    root = Path(__file__).parent.parent
    for path in [root / "config.example.toml", *sorted((root / "examples").glob("*.toml"))]:
        load(path)
