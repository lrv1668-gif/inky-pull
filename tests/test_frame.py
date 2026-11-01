import json

import httpx
import pytest
import respx
from conftest import gradient, png_bytes

from inkypull import Frame, MemoryDisplay
from inkypull.config import parse

URL = "http://frame.example.com/frame.png"
OTHER = "http://frame.example.com/other.png"


def _png(image=None, **headers):
    return httpx.Response(
        200,
        content=png_bytes(image or gradient()),
        headers={"content-type": "image/png", **headers},
    )


@pytest.fixture
def display():
    return MemoryDisplay((160, 120))


def _frame(display, client, tmp_path=None, **settings):
    config = parse({"url": URL, **settings})
    return Frame(config, display, client=client, state_dir=tmp_path or False)


def _settle(frame):
    assert frame.wait_idle(5)


@respx.mock
def test_poll_shows_new_image_once(display, client):
    respx.get(URL).mock(side_effect=[_png(etag='"1"'), httpx.Response(304)])
    with _frame(display, client) as frame:
        assert frame.poll_once()
        _settle(frame)
        assert not frame.poll_once()
        _settle(frame)
    assert len(display.shown) == 1
    assert display.shown[0].size == (160, 120)


@respx.mock
def test_errors_show_welcome_then_error_screen_once(display, client):
    respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    with _frame(display, client, error_after=3) as frame:
        for _ in range(5):
            frame.poll_once()
            _settle(frame)
        assert "refused" in frame.last_error
    # Welcome on the first failure (nothing shown yet), error screen at the 3rd, then nothing.
    assert len(display.shown) == 2


@respx.mock
def test_error_screen_is_replaced_when_host_returns(display, client):
    respx.get(URL).mock(
        side_effect=[_png(etag='"1"'), httpx.ConnectError("down"), _png(etag='"1"')]
    )
    with _frame(display, client, error_after=1) as frame:
        for _ in range(3):
            frame.poll_once()
            _settle(frame)
    # image, error screen, then the same image again (forced full fetch, not a 304).
    assert len(display.shown) == 3
    assert respx.calls[2].request.headers.get("if-none-match") is None


@respx.mock
def test_failed_display_retries_next_poll(client):
    class Flaky(MemoryDisplay):
        fail = True

        def show(self, image):
            if Flaky.fail:
                Flaky.fail = False
                raise OSError("busy timeout")
            super().show(image)

    display = Flaky((160, 120))
    respx.get(URL).mock(return_value=_png(etag='"1"'))
    with _frame(display, client) as frame:
        frame.poll_once()
        _settle(frame)
        frame.poll_once()
        _settle(frame)
    assert len(display.shown) == 1


@respx.mock
def test_state_survives_restart_without_redraw(display, client, tmp_path):
    respx.get(URL).mock(side_effect=[_png(etag='"1"'), httpx.Response(304)])
    with _frame(display, client, tmp_path) as frame:
        frame.poll_once()
        _settle(frame)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["on_panel"].startswith(f"url:{URL}#")

    with _frame(display, client, tmp_path) as frame:
        assert not frame.poll_once()
        _settle(frame)
    assert len(display.shown) == 1
    assert respx.calls[1].request.headers["if-none-match"] == '"1"'


@respx.mock
def test_refresh_button_forces_redraw(display, client):
    respx.get(URL).mock(return_value=_png(etag='"1"'))
    settings = {"buttons": {"A": {"press": "refresh"}}}
    with _frame(display, client, **settings) as frame:
        frame.poll_once()
        _settle(frame)
        frame.press("A")
        frame._actions.close()  # let the queued action run
        assert frame._force
        frame.poll_once(force=True)
        _settle(frame)
    assert len(display.shown) == 2


@respx.mock
def test_cycle_button_switches_url_and_persists(display, client, tmp_path):
    respx.get(URL).mock(return_value=_png())
    respx.get(OTHER).mock(return_value=_png(gradient((100, 100))))
    settings = {"buttons": {"C": {"press": {"action": "cycle", "urls": [URL, OTHER]}}}}
    with _frame(display, client, tmp_path, **settings) as frame:
        frame.poll_once()
        _settle(frame)
        frame.press("C")
        frame._actions.close()
        assert frame.url == OTHER
        frame.poll_once()
        _settle(frame)
    assert len(display.shown) == 2

    with _frame(display, client, tmp_path, **settings) as frame:
        assert frame.url == OTHER


@respx.mock
def test_http_button_posts_then_refreshes(display, client):
    mode = respx.post("http://hearth.local/display/mode").mock(return_value=httpx.Response(204))
    action = {
        "action": "http",
        "url": "http://hearth.local/display/mode",
        "json": {"mode": "toggle"},
        "then": "refresh",
    }
    respx.get(URL).mock(side_effect=[_png(etag='"1"'), httpx.Response(304)])
    with _frame(display, client, buttons={"B": {"press": action}}) as frame:
        frame.poll_once()
        _settle(frame)
        frame.press("B")
        frame._actions.close()
        # Wakes the loop, but doesn't force: the server may not have re-rendered yet.
        assert frame._wake.is_set() and not frame._force
        frame.poll_once()
        _settle(frame)
    assert json.loads(mode.calls[0].request.content) == {"mode": "toggle"}
    assert len(display.shown) == 1


def test_long_press_falls_back_to_press_and_command_runs(display, client, tmp_path):
    marker = tmp_path / "pressed"
    settings = {
        "buttons": {
            "D": {"press": {"action": "command", "run": ["touch", str(marker)]}},
        }
    }
    with _frame(display, client, **settings) as frame:
        frame.press("D", long=True)
        frame._actions.close()
    assert marker.exists()


def test_status_button_draws_status_screen(display, client):
    with _frame(display, client, buttons={"D": {"press": "status"}}) as frame:
        frame.press("D")
        frame._actions.close()
        _settle(frame)
    assert len(display.shown) == 1
    assert display.shown[0].mode == "P"


@respx.mock
def test_run_polls_pass_wait_once_conditional(display, client):
    route = respx.get(URL).mock(side_effect=[_png(etag='"1"'), httpx.Response(304)])
    with _frame(display, client, long_poll=60, timeout="120s") as frame:
        frame.poll_once(long_poll=True)
        _settle(frame)
        frame.poll_once(long_poll=True)
    assert "wait" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["wait"] == "60"


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(None, 300), ("120", 120), ("3600", 300), ("0", 1)],
)
@respx.mock
def test_next_delay_follows_retry_after_capped_by_poll_interval(
    display, client, retry_after, expected
):
    headers = {"etag": '"1"'} | ({"retry-after": retry_after} if retry_after else {})
    respx.get(URL).mock(return_value=_png(**headers))
    with _frame(display, client, poll_interval="5m") as frame:
        frame.poll_once()
        _settle(frame)
        assert frame.next_delay(0.1, changed=True) == expected


@respx.mock
def test_next_delay_long_poll_goes_straight_back_unless_answered_at_once(display, client):
    respx.get(URL).mock(return_value=httpx.Response(304, headers={"retry-after": "630"}))
    with _frame(display, client, poll_interval="1m", long_poll="90s", timeout="120s") as frame:
        frame._poller.restore({URL: {"etag": '"1"'}})
        frame.poll_once(long_poll=True)
        assert frame.next_delay(0.1, changed=True) == 60  # never held one: maybe ignores ?wait=
        assert frame.next_delay(90, changed=False) == 0  # held: wait again now
        frame._server_holds = True
        assert frame.next_delay(0.1, changed=True) == 0  # a new image from a holding server
        assert frame.next_delay(0.1, changed=False) == 60  # answered at once: not held


@respx.mock
def test_next_delay_without_validators_never_spins(display, client):
    respx.get(URL).mock(side_effect=[_png(), _png(gradient((100, 100)))])
    with _frame(display, client, poll_interval="1m", long_poll="90s", timeout="120s") as frame:
        frame.poll_once(long_poll=True)
        _settle(frame)
        assert frame.poll_once(long_poll=True)  # new bytes every time, and no ?wait= sent
        assert "wait" not in respx.calls[1].request.url.params
        assert frame.next_delay(0.1, changed=True) == 60


@respx.mock
def test_next_delay_after_a_failure_uses_the_interval(display, client):
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    with _frame(display, client, poll_interval="1m", long_poll="90s", timeout="120s") as frame:
        frame.poll_once(long_poll=True, show_errors=False)
        assert frame.next_delay(90, changed=False) == 60


@respx.mock
def test_refresh_during_a_long_poll_fetches_right_away(display, client):
    route = respx.get(URL).mock(return_value=_png(etag='"1"'))
    with _frame(display, client) as frame:
        frame.poll_once()
        _settle(frame)
        frame._long_polling = True  # as if run() were held in a long poll
        frame.request_refresh()  # what the refresh button runs
        frame.request_refresh()
        _settle(frame)
        assert not frame._force
        assert frame._long_polling  # the forced fetches left the loop's long poll alone
    # Both presses fetched at once (back-to-back redraws may be coalesced into one)
    assert route.call_count == 3
    assert len(display.shown) >= 2
