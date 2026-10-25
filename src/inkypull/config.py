"""Configuration: a TOML file parsed into frozen dataclasses, validated up front."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import time
from pathlib import Path
from typing import Any

BUTTON_NAMES = ("A", "B", "C", "D")

# BCM GPIO numbers for the Inky Impression 13.3". The smaller Impressions use 16 for C.
DEFAULT_PINS = {"A": 5, "B": 6, "C": 25, "D": 24}

FITS = ("contain", "cover", "stretch")
ROTATIONS = (0, 90, 180, 270)

SEARCH_PATHS = (
    Path("/etc/inkypull/config.toml"),
    Path.home() / ".config" / "inkypull" / "config.toml",
)


class ConfigError(ValueError):
    """The configuration is invalid; the message says where."""


@dataclass(frozen=True)
class Action:
    """One thing a button does. Which fields matter depends on `kind`."""

    kind: str
    url: str | None = None
    method: str = "POST"
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    run: tuple[str, ...] = ()
    timeout: float = 30.0
    urls: tuple[str, ...] = ()
    then: str | None = None


# Allowed fields per action kind, and which of them are required.
ACTIONS: dict[str, tuple[set[str], set[str]]] = {
    "refresh": (set(), set()),
    "http": ({"url", "method", "json", "headers", "timeout", "then"}, {"url"}),
    "command": ({"run", "timeout", "then"}, {"run"}),
    "cycle": ({"urls"}, {"urls"}),
    "status": (set(), set()),
    "none": (set(), set()),
}


@dataclass(frozen=True)
class Button:
    pin: int
    press: Action | None = None
    long_press: Action | None = None


@dataclass(frozen=True)
class Config:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    poll_interval: float = 300.0
    timeout: float = 30.0
    long_poll: float | None = None
    saturation: float = 0.5
    fit: str = "contain"
    background: str = "white"
    rotate: int | str = 0
    portrait_rotate: int = 90
    driver: str = "auto"
    quiet_hours: tuple[time, time] | None = None
    error_after: int = 3
    state_dir: Path | None = None
    long_press: float = 1.0
    buttons: dict[str, Button] = field(default_factory=dict)

    @property
    def sources(self) -> tuple[str, ...]:
        """Every URL the frame may show: the configured one plus any `cycle` targets."""
        urls = [self.url]
        for button in self.buttons.values():
            for action in (button.press, button.long_press):
                if action and action.kind == "cycle":
                    urls.extend(u for u in action.urls if u not in urls)
        return tuple(urls)

    def with_url(self, url: str) -> Config:
        return replace(self, url=url)


def find_config_path() -> Path | None:
    """`$INKYPULL_CONFIG`, then /etc/inkypull, then ~/.config/inkypull."""
    if env := os.environ.get("INKYPULL_CONFIG"):
        return Path(env)
    return next((p for p in SEARCH_PATHS if p.is_file()), None)


def load(path: Path | str) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data)


def parse(data: dict[str, Any]) -> Config:
    data = dict(data)
    url = data.pop("url", None)
    if not isinstance(url, str) or not url:
        raise ConfigError("`url` is required: the image URL to show")
    _check_url(url, "url")

    kwargs: dict[str, Any] = {"url": url}
    if "headers" in data:
        kwargs["headers"] = _str_dict(data.pop("headers"), "headers")
    for key in ("poll_interval", "timeout", "long_poll"):
        if key in data:
            kwargs[key] = parse_duration(data.pop(key), key)
    if kwargs.get("long_poll", 0) >= kwargs.get("timeout", Config.timeout):
        raise ConfigError("`long_poll` must be shorter than `timeout`, or requests time out first")
    if "saturation" in data:
        sat = data.pop("saturation")
        if not isinstance(sat, int | float) or not 0 <= sat <= 1:
            raise ConfigError("`saturation` must be a number from 0 to 1")
        kwargs["saturation"] = float(sat)
    if "fit" in data:
        kwargs["fit"] = _choice(data.pop("fit"), FITS, "fit")
    if "background" in data:
        kwargs["background"] = str(data.pop("background"))
    if "rotate" in data:
        rotate = data.pop("rotate")
        if rotate != "auto":
            rotate = _choice(rotate, ROTATIONS, "rotate")
        kwargs["rotate"] = rotate
    if "portrait_rotate" in data:
        kwargs["portrait_rotate"] = _choice(
            data.pop("portrait_rotate"), (90, 270), "portrait_rotate"
        )
    if "driver" in data:
        kwargs["driver"] = str(data.pop("driver"))
    if "quiet_hours" in data:
        kwargs["quiet_hours"] = parse_quiet_hours(data.pop("quiet_hours"))
    if "error_after" in data:
        n = data.pop("error_after")
        if not isinstance(n, int) or n < 1:
            raise ConfigError("`error_after` must be a whole number of at least 1")
        kwargs["error_after"] = n
    if "state_dir" in data:
        kwargs["state_dir"] = Path(data.pop("state_dir")).expanduser()

    buttons_table = data.pop("buttons", {})
    if not isinstance(buttons_table, dict):
        raise ConfigError("`[buttons]` must be a table")
    kwargs["long_press"], kwargs["buttons"] = _parse_buttons(buttons_table)

    if data:
        raise ConfigError(f"unknown setting(s): {', '.join(sorted(data))}")
    return Config(**kwargs)


def _parse_buttons(table: dict[str, Any]) -> tuple[float, dict[str, Button]]:
    table = dict(table)
    long_press = parse_duration(table.pop("long_press", 1.0), "buttons.long_press")
    pins = dict(DEFAULT_PINS)
    for name, pin in table.pop("pins", {}).items():
        if name not in BUTTON_NAMES:
            raise ConfigError(
                f"buttons.pins: unknown button {name!r} (use {', '.join(BUTTON_NAMES)})"
            )
        if not isinstance(pin, int) or pin < 0:
            raise ConfigError(f"buttons.pins.{name} must be a BCM GPIO number")
        pins[name] = pin

    buttons: dict[str, Button] = {}
    for name, spec in table.items():
        where = f"buttons.{name}"
        if name not in BUTTON_NAMES:
            raise ConfigError(f"{where}: unknown button (use {', '.join(BUTTON_NAMES)})")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} must be a table with `press` and/or `long_press`")
        spec = dict(spec)
        press = _parse_action(spec.pop("press"), f"{where}.press") if "press" in spec else None
        held = (
            _parse_action(spec.pop("long_press"), f"{where}.long_press")
            if "long_press" in spec
            else None
        )
        if spec:
            raise ConfigError(f"{where}: unknown field(s) {', '.join(sorted(spec))}")
        buttons[name] = Button(pin=pins[name], press=press, long_press=held)
    return long_press, buttons


def _parse_action(spec: Any, where: str) -> Action:
    if isinstance(spec, str):
        spec = {"action": spec}
    if not isinstance(spec, dict) or "action" not in spec:
        raise ConfigError(f'{where}: expected {{ action = "..." }}')
    spec = dict(spec)
    kind = spec.pop("action")
    if kind not in ACTIONS:
        raise ConfigError(f"{where}: unknown action {kind!r} (use {', '.join(ACTIONS)})")
    allowed, required = ACTIONS[kind]
    if unknown := set(spec) - allowed:
        raise ConfigError(f"{where}: `{kind}` doesn't take {', '.join(sorted(unknown))}")
    if missing := required - set(spec):
        raise ConfigError(f"{where}: `{kind}` needs {', '.join(sorted(missing))}")

    fields: dict[str, Any] = {"kind": kind}
    if "url" in spec:
        _check_url(spec["url"], f"{where}.url")
        fields["url"] = spec["url"]
    if "method" in spec:
        fields["method"] = str(spec["method"]).upper()
    if "json" in spec:
        fields["json"] = spec["json"]
    if "headers" in spec:
        fields["headers"] = _str_dict(spec["headers"], f"{where}.headers")
    if "timeout" in spec:
        fields["timeout"] = parse_duration(spec["timeout"], f"{where}.timeout")
    if "run" in spec:
        run = spec["run"]
        if not isinstance(run, list) or not run or not all(isinstance(a, str) for a in run):
            raise ConfigError(
                f'{where}.run must be a list of strings, e.g. ["systemctl", "reboot"]'
            )
        fields["run"] = tuple(run)
    if "urls" in spec:
        urls = spec["urls"]
        if not isinstance(urls, list) or not urls:
            raise ConfigError(f"{where}.urls must be a non-empty list of URLs")
        for i, u in enumerate(urls):
            _check_url(u, f"{where}.urls[{i}]")
        fields["urls"] = tuple(urls)
    if "then" in spec:
        if spec["then"] != "refresh":
            raise ConfigError(f'{where}.then: only "refresh" is supported')
        fields["then"] = "refresh"
    return Action(**fields)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}


def parse_duration(value: Any, where: str) -> float:
    """Seconds as a number, or a string like "90", "1.5s", "5m", "2h"."""
    if isinstance(value, bool):
        raise ConfigError(f"`{where}` must be a duration")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str) and (m := _DURATION.match(value)):
        seconds = float(m.group(1)) * _UNITS[m.group(2)]
    else:
        raise ConfigError(f'`{where}` must be a duration, e.g. 300 or "5m"')
    if seconds <= 0:
        raise ConfigError(f"`{where}` must be greater than zero")
    return seconds


def parse_quiet_hours(value: Any) -> tuple[time, time]:
    """ "23:00-06:30" → (23:00, 06:30). The range may wrap past midnight."""
    try:
        start, end = (time.fromisoformat(part.strip()) for part in str(value).split("-"))
    except ValueError:
        raise ConfigError('`quiet_hours` must look like "23:00-06:30"') from None
    if start == end:
        raise ConfigError("`quiet_hours` start and end must differ")
    return start, end


def in_quiet_hours(quiet: tuple[time, time] | None, now: time) -> bool:
    if quiet is None:
        return False
    start, end = quiet
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _choice(value: Any, options: tuple, where: str) -> Any:
    if value not in options:
        raise ConfigError(f"`{where}` must be one of {', '.join(map(str, options))}")
    return value


def _check_url(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        raise ConfigError(f"`{where}` must be an http:// or https:// URL")


def _str_dict(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"`{where}` must be a table of strings")
    return {str(k): str(v) for k, v in value.items()}
