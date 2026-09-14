"""Executable acceptance tests for the production hardening fixes.

Run: .venv/bin/python -m pytest -q tests/test_audit_regressions.py
All regression assertions must pass.
All files, indexes and HTTP requests are local synthetic fixtures.
"""
import csv
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smart_import.artifacts import LocalArtifactStore
from smart_import.config import Config
from smart_import.geocoding.base import GeocodeResult, STATUS_LOW
from smart_import.geocoding.bands import band_from_row
from smart_import.geocoding.cache import GeocodeCache
from smart_import.geocoding.runner import _stamp, run
from smart_import.jobs import JobStore, NORMALIZED, GEOCODING
from smart_import.readers import json_reader
from tests.test_geocoding import index


def test_cache_respects_changed_bbox(index, tmp_path):
    src = tmp_path / "input.csv"
    src.write_text("address\nAv. Corrientes 1234\n")
    boxes = [(-34, -35, -58, -59), (-31, -32, -64, -65)]
    results = []
    for i, box in enumerate(boxes):
        dst = tmp_path / f"out{i}.csv"
        run(src, dst, index, bbox=box, config=Config(), cache_path=tmp_path / "cache.sqlite")
        with dst.open() as fh:
            results.append(next(csv.DictReader(fh)))
    assert float(results[0]["lat"]) == pytest.approx(-34.6037)
    assert float(results[1]["lat"]) == pytest.approx(-31.4201)


def suspect():
    return GeocodeResult(status=STATUS_LOW, lat=-34.6, lon=-58.4,
                         confidence=.91, precision="street_mismatch",
                         detail={"soft_reject": True, "reason": "street mismatch"})


def test_cache_never_promotes_suspect_pin(tmp_path):
    cache = GeocodeCache(tmp_path / "cache.sqlite")
    try:
        first, second = {}, {}
        _stamp(first, suspect())
        cache.put("Av. Corrientes 1234", suspect())
        _stamp(second, cache.get("Av. Corrientes 1234"))
        assert first["geocode_band"] == "review"
        assert second["geocode_band"] == "review"
    finally:
        cache.close()


def test_issues_never_promotes_suspect_pin():
    row = {"lat": "-34.6", "lng": "-58.4"}
    _stamp(row, suspect())
    assert row["geocode_band"] == "review"
    assert band_from_row(row) == "review"


@pytest.fixture
def api(monkeypatch, tmp_path):
    mod = importlib.import_module("smart_import.api.app")
    monkeypatch.setattr(mod, "store", JobStore(tmp_path / "jobs"))
    monkeypatch.setattr(mod, "artifacts", LocalArtifactStore(tmp_path / "jobs"))
    monkeypatch.setattr(mod, "broker", None)
    monkeypatch.setattr(mod, "CFG", Config().replace(geocoding_enabled=True, pbf_dir=str(tmp_path)))
    # Do not start real geocoding or access any regional PBFs.
    monkeypatch.setattr(mod._geocode_pool, "submit", lambda *a, **kw: None)
    return mod, TestClient(mod.app, raise_server_exceptions=False)


def normalized_job(api, tmp_path):
    mod, _ = api
    job = mod.store.create("test.csv", "vepathos_flat_v1")
    raw = tmp_path / "raw.csv"
    raw.write_text("delivery_id,address\n1,Av. Corrientes 1234\n")
    job.raw_path = job.normalized_path = str(raw)
    job.report = {"rows_output": 1, "needs_geocode": 1}
    job.touch(NORMALIZED)
    return job


def test_invalid_bbox_is_client_error(api, tmp_path):
    _, client = api
    job = normalized_job(api, tmp_path)
    r = client.post(f"/imports/{job.id}/geocode", params={"bbox": "x,0,0,0"})
    assert r.status_code in (400, 422)


@pytest.mark.parametrize("lat,lon", [(91, 181), ("nan", 0), (0, "inf")])
def test_invalid_origin_is_rejected(api, tmp_path, lat, lon):
    _, client = api
    job = normalized_job(api, tmp_path)
    r = client.post(f"/imports/{job.id}/geocode", params={"origin_lat": lat, "origin_lon": lon})
    assert r.status_code in (400, 422)


def test_mapping_rejects_busy_job(api, tmp_path, monkeypatch):
    mod, client = api
    job = normalized_job(api, tmp_path)
    job.touch(GEOCODING)
    monkeypatch.setattr(mod, "_run_normalize", lambda *a, **kw: {"ran": True})
    r = client.put(f"/imports/{job.id}/mapping", json={"address": "address"})
    assert r.status_code == 409


def test_mapping_invalidates_old_geocode(api, tmp_path):
    mod, client = api
    job = normalized_job(api, tmp_path)
    old = tmp_path / "old.csv"
    old.write_text("delivery_id,address,lat,lng\n1,OLD_DESTINATION,-34.6,-58.4\n")
    job.geocoded_path = str(old)
    job.geocode_report = {"rows": 1, "matched": 1}
    r = client.put(f"/imports/{job.id}/mapping", json={"address": "address"})
    assert r.status_code == 200
    preview = client.get(f"/imports/{job.id}/preview").json()
    assert preview["source"] == "normalized"
    assert "OLD_DESTINATION" not in str(preview)


def test_json_child_expansion_is_bounded(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"addresses": [{"address": "Corrientes 1234",
                                              "packages": [{"quantity": 1}] * 20}]}))
    table = json_reader.read(path, max_rows=3)
    assert len(table.rows) <= 3


def test_json_repair_preserves_quoted_strings():
    result, _ = json_reader.loads_resilient(
        '{"addresses":[{"address":"Calle NaN 42.","lat":-34.6,}],}')
    assert result["addresses"][0]["address"] == "Calle NaN 42."


def test_json_salvage_has_bounded_scan_work(monkeypatch):
    text = '{"addresses":[' + '{"address":' * 200
    scanned = 0
    original = json_reader._matching_brace

    def count_scan(raw, start, *, budget=None):
        nonlocal scanned
        before = budget[0]
        result = original(raw, start, budget=budget)
        scanned += before - budget[0]
        return result

    monkeypatch.setattr(json_reader, "_matching_brace", count_scan)
    json_reader.salvage_list_document(text)
    assert scanned <= len(text) * 4


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_same_worker_cannot_claim_two_executions(tmp_path, backend):
    if backend == "redis":
        from smart_import.job_store_redis import RedisJobStore
        from tests.fake_redis import FakeRedis
        store = RedisJobStore(FakeRedis())
    else:
        store = JobStore(tmp_path)
    assert store.claim_run("imp_test", "same-process", 60)
    assert not store.claim_run("imp_test", "same-process", 60)


def test_skipped_address_counts_as_not_found(index, tmp_path):
    src = tmp_path / "input.csv"
    src.write_text("address\nx\n")
    report = run(src, tmp_path / "out.csv", index, config=Config(),
                 cache_path=tmp_path / "cache.sqlite")
    assert report.skipped_low_confidence == 1
    assert report.not_found == 1


def test_valid_json_and_nulls_remain_usable(tmp_path):
    path = tmp_path / "input.json"
    path.write_text('[null,{"address":"José Hernández 1234","phone":null}]')
    assert len(json_reader.read(path).rows) == 1


@pytest.mark.parametrize("address", ['\" OR 1=1; --', "https://169.254.169.254/latest/meta-data/", "Qzxwv 99999"])
def test_untrusted_text_is_not_a_url_or_sql(index, address):
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
    geocoder = LocalOSMGeocoder(index)
    try:
        result = geocoder.geocode(address)
        assert result.status == "not_found"
    finally:
        geocoder.close()


def test_504_transfer_is_retried():
    from smart_import.artifacts.http import HttpArtifactStore, _fallar_si
    from types import SimpleNamespace
    store = HttpArtifactStore("https://unused.invalid", "test", "/tmp/unused", reintentos=2)
    calls = []
    def request():
        calls.append(1)
        _fallar_si(SimpleNamespace(status_code=504 if len(calls) == 1 else 200), "fixture")
        return "ok"
    assert store._con_reintentos("fixture", request) == "ok"
    assert len(calls) == 2


def test_429_transfer_is_recoverable():
    from smart_import.artifacts.http import ArtifactRejected, _fallar_si
    from types import SimpleNamespace
    try:
        _fallar_si(SimpleNamespace(status_code=429), "fixture")
    except Exception as exc:
        assert not isinstance(exc, ArtifactRejected)


def test_index_cannot_escape_root(api, tmp_path, monkeypatch, index):
    from smart_import.worker.handlers import run_geocode_job, WorkerContext
    import smart_import.geocoding.locality as locality
    import smart_import.geocoding.runner as runner
    mod, _ = api
    job = normalized_job(api, tmp_path)
    monkeypatch.setattr(locality, "fill_depot_from_index", lambda *a, **kw: None)
    attempted = []
    def capture(src, dst, path, **kw):
        attempted.append(path)
        raise ValueError("audit stopped before any query")
    monkeypatch.setattr(runner, "run", capture)
    cfg = mod.CFG.replace(index_dir=str(tmp_path / "allowed"))
    run_geocode_job(WorkerContext(cfg, mod.store, mod.artifacts, mod.logger),
                    job.id, None, None, str(index))
    assert attempted == []


def test_admission_runs_before_upload_spooling(api, monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile
    mod, client = api
    written = 0
    original = UploadFile.write
    async def spy(self, data):
        nonlocal written
        written += len(data)
        return await original(self, data)
    @asynccontextmanager
    async def full():
        raise HTTPException(429, "full")
        yield
    monkeypatch.setattr(UploadFile, "write", spy)
    monkeypatch.setattr(mod, "_admitted", full)
    r = client.post("/imports", files={"file": ("fixture.csv", b"a\n" * 4096)})
    assert r.status_code == 429
    assert written == 0


def test_unit_does_not_replace_house_number():
    from smart_import.geocoding.address import parse
    parsed = parse("José Hernández 1234, Piso 2 Depto B")
    assert parsed.house_number == "1234"
    assert parsed.road == "José Hernández"


def test_canonical_unicode_has_identical_components():
    import unicodedata
    from smart_import.geocoding.address import parse
    text = "José Hernández 1234"
    composed, decomposed = parse(text), parse(unicodedata.normalize("NFD", text))
    assert unicodedata.normalize("NFC", decomposed.road) == composed.road
    assert decomposed.house_number == composed.house_number


@pytest.mark.parametrize("address", ["Av. Crrnts 1234, Buenos Aires", "Av. Corrientes y Av. Callao, Buenos Aires"])
def test_ambiguous_input_does_not_claim_an_exact_door(index, address):
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
    geocoder = LocalOSMGeocoder(index)
    try:
        result = geocoder.geocode(address)
        assert result.status != "matched"
        assert result.precision != "housenumber"
    finally:
        geocoder.close()
