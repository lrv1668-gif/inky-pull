# inkypull

Pull an image from a URL and show it on a [Pimoroni Inky](https://github.com/pimoroni/inky) e-paper display — and do something useful when you press its buttons.

inkypull is a small, long-running thin client for a Raspberry Pi behind an Inky Impression. Something else renders the picture (a home dashboard, a script, a static file server); inkypull keeps the panel showing the latest version of it:

- **Only refreshes when the image changed.** Conditional requests (`ETag` / `Last-Modified`), with a content-hash fallback for servers that send neither. A Spectra 6 refresh flickers for ~30 s, so this matters.
- **Pre-dithered images pass straight through.** An image that only uses the panel's six inks (e.g. one a server already dithered with ordered/Bayer dithering) is sent undithered, pixel-for-pixel. Anything else — photos, screenshots — is dithered by `inky` (Floyd–Steinberg).
- **Configurable buttons.** Each of A–D gets a press and a long-press action: refresh, call an HTTP endpoint, run a command, cycle between URLs, show a status screen.
- **Calm failure.** After a few consecutive failures it draws a "can't reach" screen once, and goes back to the image as soon as the host answers again. State survives restarts, so a reboot doesn't redraw an unchanged picture.

Images only: the URL must return `image/*` (PNG, JPEG, …). inkypull doesn't render web pages.

## Requirements

- A Raspberry Pi (developed for a Pi 4 Model B; a Zero 2 W works too) with an Inky display — written for the **Inky Impression 13.3"** (Spectra 6, 1600×1200), works with any board `inky` supports.
- **Pimoroni's `inky` library already installed and working** — run their installer first (it enables SPI/I2C and creates the `~/.virtualenvs/pimoroni` venv). inkypull doesn't set up the panel itself.
- Python 3.11+ (Raspberry Pi OS Bookworm or later).

## Install

```bash
~/.virtualenvs/pimoroni/bin/pip install git+https://github.com/lrv1668-gif/inky-pull.git
~/.virtualenvs/pimoroni/bin/inkypull doctor
```

`doctor` checks that `inky` imports, SPI and GPIO are available, the board is detected, and your config parses.

Then configure and start the service:

```bash
sudo mkdir -p /etc/inkypull
sudo cp config.example.toml /etc/inkypull/config.toml   # edit url = ...
sudo cp systemd/inkypull.service /etc/systemd/system/   # edit User= / ExecStart= if needed
sudo systemctl daemon-reload
sudo systemctl enable --now inkypull
journalctl -u inkypull -f
```

It's a service rather than a cron job because it listens for button presses between refreshes. If you don't need buttons, `inkypull once` from cron or a systemd timer works too.

## Usage

```text
inkypull run            poll the URL and keep the display up to date (the service)
inkypull once [--force] fetch once; update the display if the image changed
inkypull show FILE      show a local image
inkypull test-buttons   print button presses, to check pins on new hardware
inkypull doctor         check the inky / SPI / GPIO setup
```

While the service is running it holds the panel and the button GPIO lines, so `sudo systemctl stop inkypull` before running `once`, `show`, `test-buttons` or `doctor` by hand (otherwise you'll see "line busy" or a detection error).

Common options: `--config PATH`, `--url URL` (overrides the config; enough on its own for a quick test), `--display file:out.png` to write a preview PNG instead of driving the panel (with `--resolution 1600x1200`), `-v` for debug logs.

The config is read from `--config`, else `$INKYPULL_CONFIG`, `/etc/inkypull/config.toml`, `~/.config/inkypull/config.toml`.

## Configuration

See [`config.example.toml`](config.example.toml) for every setting. The essentials:

```toml
url = "http://example.com/frame.png"
poll_interval = "5m"
fit = "contain"        # contain | cover | stretch
rotate = 0             # 0 | 90 | 180 | 270 (clockwise) | "auto" for portrait images
saturation = 0.5       # palette vividness for images inky dithers
quiet_hours = "23:00-06:30"
```

### Buttons

```toml
[buttons]
pins = { A = 5, B = 6, C = 25, D = 24 }   # BCM numbers; the defaults suit the 13.3"
long_press = "1s"

[buttons.A]
press = "refresh"

[buttons.B]
press = { action = "http", method = "POST", url = "http://example.com/next", then = "refresh" }

[buttons.C]
press = { action = "cycle", urls = ["http://example.com/a.png", "http://example.com/b.png"] }

[buttons.D]
press = "status"
long_press = { action = "command", run = ["sudo", "systemctl", "reboot"] }
```

| Action | Fields | Does |
| --- | --- | --- |
| `refresh` | — | Fetch now and redraw, even if unchanged (clears ghosting) |
| `http` | `url`, `method` (POST), `json`, `headers`, `timeout`, `then` | Calls an endpoint — e.g. tell your server to switch modes |
| `command` | `run` (argv list, no shell), `timeout`, `then` | Runs a program |
| `cycle` | `urls` | Switches to the next image URL; remembered across restarts |
| `status` | — | Shows the URL, hostname/IP, last update and last error |
| `none` | — | Nothing |

`then = "refresh"` checks for a new image as soon as the action succeeds, but only redraws once the image has actually changed — so pressing a button that tells your server to re-render doesn't first flash the old picture. A long press without a `long_press` action runs `press`. Mistakes (unknown actions, missing fields) are reported at startup with the button named.

Button pins on the smaller Impressions differ (C is GPIO 16) — `inkypull test-buttons` confirms yours.

## Serving images for inkypull

Any URL that returns an image works. To get the best results:

- Send an `ETag` (and honour `If-None-Match` with `304`) so polling is nearly free.
- Render at the panel's resolution (1600×1200 for the 13.3").
- For crisp UI, dither on the server and send a PNG that only uses the six Spectra 6 inks, in any palette order — black, white, yellow, red, blue, green. inkypull recognises both inky's pure values (`#000000 #ffffff #ffff00 #ff0000 #0000ff #00ff00`) and the measured, white-balanced ones (`#000000 #ffffff #ffff6e #f16f74 #5e5b91 #5a8d6c`) and maps them exactly.

[`examples/hearth.toml`](examples/hearth.toml) is a complete config for [Hearth](https://hearth.lucas-vertrees.me), a self-hosted home dashboard: Button B steps through its frame modes (hold to go back to the schedule).

## Python API

```python
from inkypull import Frame, InkyDisplay, load

config = load("/etc/inkypull/config.toml")
with Frame(config, InkyDisplay(driver=config.driver, saturation=config.saturation)) as frame:
    frame.poll_once()
    frame.wait_idle()
```

`Frame` takes injectable `display`, `buttons` and `httpx.Client`; `MemoryDisplay` and `FileDisplay` are handy off-Pi. The pieces stand alone too: `inkypull.fetch.Poller`, `inkypull.image.prepare` / `to_ink_palette`.

## Development

```bash
uv venv --python 3.11 && uv pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

No hardware needed: tests use `MemoryDisplay` and mocked HTTP. Try it end to end with `--display file:out.png`.

## License

MIT
