from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import gradient, png_bytes

from inkypull.fetch import FetchError, Poller, parse_retry_after

URL = "http://frame.example.com/frame.png"


def _png(**headers):
    return httpx.Response(
        200, content=png_bytes(gradient()), headers={"content-type": "image/png", **headers}
    )


@respx.mock
def test_fetch_sends_etag_and_treats_304_as_unchanged(client):
    route = respx.get(URL).mock(side_effect=[_png(etag='"v1"'), httpx.Response(304)])
    poller = Poller(client)
    assert poller.fetch(URL) is not None
    assert poller.fetch(URL) is None
    assert route.calls[1].request.headers["if-none-match"] == '"v1"'


@respx.mock
def test_fetch_sends_last_modified(client):
    stamp = "Wed, 21 Oct 2026 07:28:00 GMT"
    route = respx.get(URL).mock(side_effect=[_png(**{"last-modified": stamp}), httpx.Response(304)])
    poller = Poller(client)
    poller.fetch(URL)
    poller.fetch(URL)
    assert route.calls[1].request.headers["if-modified-since"] == stamp


@respx.mock
def test_fetch_same_bytes_without_validators_is_unchanged(client):
    respx.get(URL).mock(side_effect=[_png(), _png()])
    poller = Poller(client)
    assert poller.fetch(URL) is not None
    assert poller.fetch(URL) is None


@respx.mock
def test_fetch_force_skips_validators_and_returns_image(client):
    route = respx.get(URL).mock(side_effect=[_png(etag='"v1"'), _png(etag='"v1"')])
    poller = Poller(client)
    poller.fetch(URL)
    assert poller.fetch(URL, force=True) is not None
    assert "if-none-match" not in route.calls[1].request.headers


@respx.mock
def test_fetch_forget_downloads_in_full(client):
    route = respx.get(URL).mock(side_effect=[_png(etag='"v1"'), _png(etag='"v1"')])
    poller = Poller(client)
    poller.fetch(URL)
    poller.forget(URL)
    assert poller.fetch(URL) is not None
    assert "if-none-match" not in route.calls[1].request.headers


@respx.mock
def test_fetch_rejects_html(client):
    respx.get(URL).mock(return_value=httpx.Response(200, html="<h1>hi</h1>"))
    with pytest.raises(FetchError, match="text/html, not an image"):
        Poller(client).fetch(URL)


@respx.mock
def test_fetch_rejects_corrupt_image(client):
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"nope", headers={"content-type": "image/png"})
    )
    with pytest.raises(FetchError, match="can't read"):
        Poller(client).fetch(URL)


@respx.mock
def test_fetch_http_error_and_connection_error(client):
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.ConnectError("refused")])
    poller = Poller(client)
    with pytest.raises(FetchError, match="HTTP 503"):
        poller.fetch(URL)
    with pytest.raises(FetchError, match="couldn't reach"):
        poller.fetch(URL)


@respx.mock
def test_fetch_sends_configured_headers(client):
    route = respx.get(URL).mock(return_value=_png())
    Poller(client, headers={"Authorization": "Bearer test"}).fetch(URL)
    assert route.calls[0].request.headers["authorization"] == "Bearer test"


@respx.mock
def test_export_restore_round_trips(client):
    respx.get(URL).mock(side_effect=[_png(etag='"v1"'), httpx.Response(304)])
    first = Poller(client)
    first.fetch(URL)
    second = Poller(client)
    second.restore(first.export())
    assert second.fetch(URL) is None


@respx.mock
def test_fetch_long_poll_sends_wait_only_when_conditional(client):
    route = respx.get(URL).mock(side_effect=[_png(etag='"v1"'), httpx.Response(304)])
    poller = Poller(client)
    poller.fetch(URL, wait=90)
    poller.fetch(URL, wait=90)
    assert "wait" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["wait"] == "90"


@respx.mock
def test_fetch_long_poll_keeps_the_urls_own_query(client):
    url = URL + "?mode=daily"
    route = respx.get(url__startswith=URL).mock(
        side_effect=[_png(etag='"v1"'), httpx.Response(304)]
    )
    poller = Poller(client)
    poller.fetch(url, wait=90)
    poller.fetch(url, wait=90)
    params = route.calls[1].request.url.params
    assert params["mode"] == "daily" and params["wait"] == "90"


@respx.mock
def test_fetch_force_never_long_polls(client):
    route = respx.get(URL).mock(return_value=_png(etag='"v1"'))
    poller = Poller(client)
    poller.fetch(URL)
    poller.fetch(URL, force=True, wait=90)
    assert "wait" not in route.calls[1].request.url.params


@respx.mock
def test_fetch_records_retry_after(client):
    respx.get(URL).mock(
        side_effect=[_png(etag='"v1"', **{"retry-after": "630"}), httpx.ConnectError("down")]
    )
    poller = Poller(client)
    poller.fetch(URL)
    assert poller.retry_after == 630
    with pytest.raises(FetchError):
        poller.fetch(URL)
    assert poller.retry_after is None


def test_parse_retry_after_seconds_and_dates():
    now = datetime(2026, 10, 21, 7, 28, tzinfo=UTC)
    assert parse_retry_after("120") == 120
    assert parse_retry_after("Wed, 21 Oct 2026 07:30:00 GMT", now) == 120
    assert parse_retry_after("Wed, 21 Oct 2026 07:00:00 GMT", now) == 0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
