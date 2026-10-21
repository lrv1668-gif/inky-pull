import httpx
import pytest
import respx
from conftest import gradient, png_bytes

from inkypull.fetch import FetchError, Poller

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
