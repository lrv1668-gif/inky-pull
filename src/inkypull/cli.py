"""`inkypull` command line."""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import sys
from pathlib import Path

from PIL import Image

from . import __version__
from .config import Config, ConfigError, find_config_path, load
from .display import DisplayError, open_display
from .frame import Frame
from .image import prepare

log = logging.getLogger("inkypull")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        # One line per poll is noise in the journal; inkypull logs what matters itself.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except (ConfigError, DisplayError) as e:
        print(f"inkypull: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inkypull", description=__doc__)
    p.add_argument("--version", action="version", version=f"inkypull {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(required=True, metavar="command")

    def common(cmd: argparse.ArgumentParser, url: bool = True) -> None:
        cmd.add_argument("-c", "--config", type=Path, help="config file (default: search)")
        if url:
            cmd.add_argument("--url", help="image URL (overrides the config's `url`)")
        cmd.add_argument(
            "--display",
            default="inky",
            help="`inky` (default) or `file:<path.png>` to write a preview instead",
        )
        cmd.add_argument(
            "--resolution",
            type=_resolution,
            default=(1600, 1200),
            help="size for file displays, e.g. 1600x1200 (default)",
        )

    run = sub.add_parser("run", help="poll the URL and keep the display up to date")
    common(run)
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("once", help="fetch once and update the display if it changed")
    common(once)
    once.add_argument("--force", action="store_true", help="redraw even if unchanged")
    once.set_defaults(func=cmd_once)

    show = sub.add_parser("show", help="show a local image file")
    common(show, url=False)
    show.add_argument("file", type=Path)
    show.set_defaults(func=cmd_show)

    buttons = sub.add_parser("test-buttons", help="print button presses (Ctrl-C to stop)")
    buttons.add_argument("-c", "--config", type=Path, help="config file (for custom pins)")
    buttons.set_defaults(func=cmd_test_buttons)

    doctor = sub.add_parser("doctor", help="check that inky, SPI and GPIO are set up")
    doctor.add_argument("-c", "--config", type=Path, help="config file to validate")
    doctor.set_defaults(func=cmd_doctor)
    return p


def _resolution(value: str) -> tuple[int, int]:
    try:
        w, h = (int(n) for n in value.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, e.g. 1600x1200") from None
    return w, h


def _config(args, *, need_url: bool = True) -> Config:
    path = args.config or find_config_path()
    url = getattr(args, "url", None)
    if path:
        config = load(path)
        return config.with_url(url) if url else config
    if url:
        return Config(url=url)
    if need_url:
        raise ConfigError("no config file found; pass --config or --url")
    return Config(url="http://localhost/")


def _frame(args, config: Config, *, buttons=None) -> Frame:
    display = open_display(
        args.display,
        driver=config.driver,
        saturation=config.saturation,
        resolution=args.resolution,
    )
    # State tracks what's on the real panel; previews to a file shouldn't touch it.
    return Frame(config, display, buttons=buttons, state_dir=args.display == "inky")


def cmd_run(args) -> int:
    config = _config(args)
    buttons = None
    if args.display == "inky" and config.buttons:
        from .buttons import GpioButtons

        pins = {name: b.pin for name, b in config.buttons.items()}
        try:
            buttons = GpioButtons(pins, config.long_press)
        except Exception as e:
            log.error("buttons disabled: %s", e)

    with _frame(args, config, buttons=buttons) as frame:
        signal.signal(signal.SIGTERM, lambda *_: frame.stop())
        with contextlib.suppress(KeyboardInterrupt):
            frame.run()
    return 0


def cmd_once(args) -> int:
    config = _config(args)
    with _frame(args, config) as frame:
        frame.poll_once(force=args.force, show_errors=False)
        frame.wait_idle()
        if frame.last_error:
            print(f"inkypull: {frame.last_error}", file=sys.stderr)
            return 1
    return 0


def cmd_show(args) -> int:
    config = _config(args, need_url=False)
    display = open_display(
        args.display,
        driver=config.driver,
        saturation=config.saturation,
        resolution=args.resolution,
    )
    with Image.open(args.file) as img:
        image = prepare(
            img,
            tuple(display.resolution),
            fit=config.fit,
            background=config.background,
            rotate=config.rotate,
            portrait_rotate=config.portrait_rotate,
        )
    display.show(image)
    return 0


def cmd_test_buttons(args) -> int:
    from .buttons import GpioButtons
    from .config import DEFAULT_PINS

    pins = dict(DEFAULT_PINS)
    path = args.config or find_config_path()
    if path:
        pins.update({name: b.pin for name, b in load(path).buttons.items()})
    buttons = GpioButtons(pins, 1.0)
    print("Watching " + ", ".join(f"{n}=GPIO{p}" for n, p in pins.items()) + " — Ctrl-C to stop")
    buttons.start(lambda name, long: print(f"{name} {'long press' if long else 'press'}"))
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        buttons.close()
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def check(passed: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(
            f"{'✓' if passed else '✗'} {label}" + (f"\n    {hint}" if hint and not passed else "")
        )

    check(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    try:
        import inky

        check(True, f"inky {getattr(inky, '__version__', '?')} at {Path(inky.__file__).parent}")
    except ImportError:
        check(
            False,
            "inky is importable",
            "install it with Pimoroni's installer (github.com/pimoroni/inky), "
            "then run inkypull from that venv",
        )
    spi = sorted(Path("/dev").glob("spidev*"))
    check(
        bool(spi),
        "SPI enabled" + (f" ({', '.join(p.name for p in spi)})" if spi else ""),
        "sudo raspi-config nonint do_spi 0, then reboot",
    )
    chips = sorted(Path("/dev").glob("gpiochip*"))
    check(bool(chips), "GPIO chips present", "is this a Raspberry Pi?")
    try:
        import gpiod  # noqa: F401
        import gpiodevice  # noqa: F401

        check(True, "gpiod + gpiodevice importable (buttons)")
    except ImportError:
        check(False, "gpiod + gpiodevice importable (buttons)", "they come with inky>=2.0")
    try:
        from .display import InkyDisplay

        d = InkyDisplay()
        check(True, f"display detected: {d.resolution[0]}x{d.resolution[1]}")
    except Exception as e:
        check(False, "display detected", str(e))
    path = args.config or find_config_path()
    if path:
        try:
            config = load(path)
            check(True, f"config {path}: {config.url}, {len(config.buttons)} button(s)")
        except ConfigError as e:
            check(False, f"config {path}", str(e))
    else:
        print("- no config file found (looked in $INKYPULL_CONFIG, /etc/inkypull, ~/.config)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
