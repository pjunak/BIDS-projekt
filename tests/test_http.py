import pytest

from bids_scrapers.http import Blocked, GaveUp, PoliteClient
from conftest import Response


def client(cfg):
    return PoliteClient(cfg.http, cfg.user_agent)


def test_retries_5xx_honouring_retry_after(cfg, server, sleeps):
    answers = iter([Response(503, "busy", {"Retry-After": "7"}), Response(200, "ok")])
    server.routes["/x"] = lambda req: next(answers)
    resp = client(cfg).get(server.url + "/x")
    assert resp.status_code == 200 and resp.text == "ok"
    assert len(server.hits("/x")) == 2
    assert 7 in sleeps


def test_403_stops_immediately(cfg, server):
    server.routes["/x"] = Response(403, "go away")
    with pytest.raises(Blocked):
        client(cfg).get(server.url + "/x")
    assert len(server.hits("/x")) == 1


def test_429_with_huge_retry_after_stops(cfg, server):
    server.routes["/x"] = Response(429, "slow down", {"Retry-After": "86400"})
    with pytest.raises(Blocked):
        client(cfg).get(server.url + "/x")
    assert len(server.hits("/x")) == 1


def test_gives_up_after_max_retries(cfg, server):
    server.routes["/x"] = Response(500, "broken")
    with pytest.raises(GaveUp):
        client(cfg).get(server.url + "/x")
    assert len(server.hits("/x")) == cfg.http.max_retries + 1


def test_404_is_returned_not_retried(cfg, server):
    resp = client(cfg).get(server.url + "/missing")
    assert resp.status_code == 404
    assert len(server.requests) == 1


def test_pauses_when_rate_limit_nearly_used(cfg, server, sleeps):
    server.routes["/x"] = Response(200, "ok", {"X-RateLimit-Limit": "40", "X-RateLimit-Remaining": "1"})
    client(cfg).get(server.url + "/x")
    assert 60.0 in sleeps


def test_paces_requests(cfg, server, sleeps):
    cfg.http.min_delay = cfg.http.max_delay = 3.0
    server.routes["/x"] = Response(200, "ok")
    c = client(cfg)
    c.get(server.url + "/x")
    c.get(server.url + "/x")
    assert any(2.5 < s <= 3.0 for s in sleeps)


def test_long_pause_every_n_requests(cfg, server, sleeps):
    cfg.http.long_pause_every = 2
    cfg.http.long_pause_min = cfg.http.long_pause_max = 42.0
    server.routes["/x"] = Response(200, "ok")
    c = client(cfg)
    for _ in range(3):
        c.get(server.url + "/x")
    assert sleeps.count(42.0) == 1


def test_user_agent_is_honest(cfg, server):
    cfg.contact = "student@example.org"
    server.routes["/x"] = Response(200, "ok")
    client(cfg).get(server.url + "/x")
    ua = server.requests[0].headers["User-Agent"]
    assert "bids-scrapers" in ua and "student@example.org" in ua
