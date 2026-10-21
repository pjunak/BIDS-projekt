import json

import pytest

from bids_scrapers import mpsv
from bids_scrapers.http import PoliteClient
from bids_scrapers.opendata import DownloadError, Store
from conftest import Response

HEADER = "rozhodne_datum;uchazec_pohlavi;obec;obec_kod;kraj_kod;pno\r\n"


def portal_csv(months):
    lines = [HEADER]
    for m in months:
        lines.append(f"{m}-30;M;Brno;582786;116;0,048\r\n")
        lines.append(f"{m}-30;Ž;Brno;582786;116;0,047\r\n")
    return "﻿" + "".join(lines)


def test_split_by_month_keeps_bytes(cfg, tmp_path):
    store = Store(cfg.data_dir, "mpsv")
    src = tmp_path / "all.csv"
    src.write_text(portal_csv(["2025-01", "2025-02"]), encoding="utf-8", newline="")
    res = mpsv._split_by_month(store, src, "t", "http://x", expected_rows=4)
    assert res == {"t/2025-01.csv": "new", "t/2025-02.csv": "new"}
    out = store.path("t/2025-02.csv").read_bytes()
    assert out.startswith("﻿".encode()) and out.count(b"\r\n") == 3
    assert b"2025-01" not in out and not src.exists()


def test_split_rejects_truncated_download(cfg, tmp_path):
    store = Store(cfg.data_dir, "mpsv")
    src = tmp_path / "all.csv"
    src.write_text(portal_csv(["2025-01"]), encoding="utf-8", newline="")
    with pytest.raises(DownloadError):
        mpsv._split_by_month(store, src, "t", "http://x", expected_rows=5)
    assert not list(store.path("t").glob("*.csv")) and not list(store.path("t").glob(".*.part"))


@pytest.fixture
def portal(server, monkeypatch):
    monkeypatch.setattr(mpsv, "PORTAL_URL", server.url + "/portal/{table}/{fmt}")
    state = {"months": ["2025-01", "2025-02", "2025-03"]}

    def json_route(req):
        return Response(200, json.dumps([{"rozhodne_datum": state["months"][-1] + "-30"}]))

    def full(req):
        return Response(200, portal_csv(state["months"]), {"x-total-count": str(2 * len(state["months"]))})

    def latest(req):
        return Response(200, portal_csv(state["months"][-1:]), {"x-total-count": "2"})

    server.routes["/portal/tbl/json"] = json_route
    server.routes["/portal/tbl/csv"] = full
    server.routes["/portal/tbl_vp/csv"] = latest
    return state


def test_portal_sync_full_then_incremental(cfg, server, portal):
    store = Store(cfg.data_dir, "mpsv")
    client = PoliteClient(cfg.http, cfg.user_agent)

    res = mpsv._sync_portal_table(client, store, "t", "tbl")
    assert sorted(res) == ["t/2025-01.csv", "t/2025-02.csv", "t/2025-03.csv"]
    assert len(server.hits("/portal/tbl/csv")) == 1

    assert mpsv._sync_portal_table(client, store, "t", "tbl") == {}  # up to date: only the probe

    portal["months"].append("2025-04")  # a new month -> the small "_vp" table
    assert list(mpsv._sync_portal_table(client, store, "t", "tbl")) == ["t/2025-04.csv"]
    assert len(server.hits("/portal/tbl_vp/csv")) == 1

    store.path("t/2025-02.csv").unlink()  # a gap -> full table again
    portal["months"].append("2025-05")
    res = mpsv._sync_portal_table(client, store, "t", "tbl")
    assert "t/2025-02.csv" in res and "t/2025-05.csv" in res
    assert len(server.hits("/portal/tbl/csv")) == 2


def test_months():
    assert list(mpsv._months((2024, 11), (2025, 2))) == [(2024, 11), (2024, 12), (2025, 1), (2025, 2)]
    assert len(list(mpsv._months(mpsv.LEGACY_FIRST, mpsv.LEGACY_LAST))) == 130
