from bids_scrapers.cli import main, ping
from conftest import Response


def test_ping_success_and_failure(server):
    server.routes["/hc"] = Response(200, "OK")
    server.routes["/hc/fail"] = Response(200, "OK")
    ping(server.url + "/hc", ok=True)
    ping(server.url + "/hc", ok=False)
    assert [r.path for r in server.requests] == ["/hc", "/hc/fail"]


def test_ping_never_raises():
    ping("http://127.0.0.1:9/unreachable", ok=True)
    ping("", ok=False)


def test_status_on_empty_data_dir(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('data_dir = "data"\n')
    assert main(["-c", str(config), "status"]) == 0
    out = capsys.readouterr().out
    assert "sreality: no data yet" in out and "policie: no data yet" in out


def test_unknown_opendata_source_is_rejected(tmp_path):
    import pytest

    config = tmp_path / "config.toml"
    config.write_text('data_dir = "data"\n')
    with pytest.raises(SystemExit):
        main(["-c", str(config), "opendata", "nonsense"])


def test_unknown_config_option_is_rejected(tmp_path):
    import pytest

    config = tmp_path / "config.toml"
    config.write_text('[sreality]\nmax_detail_per_run = 5\n')  # typo
    with pytest.raises(ValueError, match="sreality.max_detail_per_run"):
        main(["-c", str(config), "status"])
