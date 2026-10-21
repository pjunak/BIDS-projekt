import gzip

from bids_scrapers.http import PoliteClient
from bids_scrapers.opendata import CHANGED, NEW, NOT_MODIFIED, UNCHANGED, Store
from conftest import Response


def make(cfg):
    return PoliteClient(cfg.http, cfg.user_agent), Store(cfg.data_dir, "test")


def test_conditional_download_cycle(cfg, server):
    state = {"etag": '"v1"', "body": "a,b\n1,2\n"}

    def route(req):
        if req.headers.get("If-None-Match") == state["etag"]:
            return Response(304)
        return Response(200, state["body"], {"ETag": state["etag"]})

    server.routes["/f.csv"] = route
    client, store = make(cfg)
    url = server.url + "/f.csv"

    assert store.fetch(client, url, "f.csv") == NEW
    assert store.path("f.csv").read_text() == "a,b\n1,2\n"
    assert store.entries["f.csv"]["etag"] == '"v1"'

    assert store.fetch(client, url, "f.csv") == NOT_MODIFIED
    assert server.requests[-1].headers["If-None-Match"] == '"v1"'

    state.update(etag='"v2"', body="a,b\n1,3\n")
    assert store.fetch(client, url, "f.csv") == CHANGED
    assert store.path("f.csv").read_text() == "a,b\n1,3\n"
    history = list((store.root / "_history").glob("f__*.csv"))
    assert len(history) == 1 and history[0].read_text() == "a,b\n1,2\n"

    # The manifest survives a restart.
    assert Store(cfg.data_dir, "test").entries["f.csv"]["etag"] == '"v2"'


def test_same_content_without_etag_is_unchanged(cfg, server):
    server.routes["/f.csv"] = Response(200, "same")
    client, store = make(cfg)
    assert store.fetch(client, server.url + "/f.csv", "f.csv") == NEW
    assert store.fetch(client, server.url + "/f.csv", "f.csv") == UNCHANGED
    assert not (store.root / "_history").exists()


def test_keep_history_false(cfg, server):
    bodies = iter(["one", "two"])
    server.routes["/f"] = lambda req: Response(200, next(bodies))
    client, store = make(cfg)
    store.fetch(client, server.url + "/f", "f", keep_history=False)
    assert store.fetch(client, server.url + "/f", "f", keep_history=False) == CHANGED
    assert store.path("f").read_text() == "two"
    assert not (store.root / "_history").exists()


def test_decode_false_keeps_gzip_bytes(cfg, server):
    payload = gzip.compress(b'{"polozky": []}')
    # Served like data.mpsv.cz serves .json.gz: compressed file + Content-Encoding: gzip
    server.routes["/x.json.gz"] = Response(200, payload, {"Content-Encoding": "gzip"})
    client, store = make(cfg)
    store.fetch(client, server.url + "/x.json.gz", "x.json.gz", decode=False)
    assert store.path("x.json.gz").read_bytes() == payload
    assert gzip.decompress(store.path("x.json.gz").read_bytes()) == b'{"polozky": []}'


def test_http_error_raises_and_leaves_no_file(cfg, server):
    import pytest

    from bids_scrapers.opendata import DownloadError

    client, store = make(cfg)
    with pytest.raises(DownloadError):
        store.fetch(client, server.url + "/missing", "missing.csv")
    assert not store.path("missing.csv").exists()
    assert "missing.csv" not in store.entries
