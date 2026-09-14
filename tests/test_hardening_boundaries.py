"""Security boundaries with synthetic local data, no production dependencies."""
import asyncio
from contextlib import asynccontextmanager
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from smart_import.config import Config
from smart_import.execution import Execution, ExecutionLost, executing
from smart_import.identity import tenant
from smart_import.jobs import JobStore, NORMALIZED
from smart_import.job_store_redis import RedisJobStore
from tests.fake_redis import FakeRedis


@pytest.fixture(params=['memory', 'redis'])
def store(request, tmp_path):
    return JobStore(tmp_path) if request.param == 'memory' else RedisJobStore(FakeRedis())


def test_late_attempt_cannot_change_committed_state(store):
    job = store.create('input.csv', 'vepathos_flat_v1')
    first, second = 'a' * 32, 'b' * 32
    assert store.claim_run(job.id, first, 60)
    with executing(Execution(job.id, first)):
        stale = store.get(job.id)
        stale.error = 'first'
        store.save(stale)
        store.release_run(job.id, first)
        assert store.claim_run(job.id, second, 60)
        stale.error = 'late'
        with pytest.raises(ExecutionLost):
            store.save(stale)
    assert store.get(job.id).error == 'first'
    store.release_run(job.id, first)
    assert store.owns_run(job.id, second)
    assert not store.renew_run(job.id, first, 60)


def test_listing_is_scoped_to_authenticated_tenant(store):
    reset = tenant.set('one')
    try:
        first = store.create('one.csv', 'vepathos_flat_v1')
        tenant.set('two')
        second = store.create('two.csv', 'vepathos_flat_v1')
        assert [j.id for j in store.list()] == [second.id]
        tenant.set('one')
        assert [j.id for j in store.list(limit=1)] == [first.id]
    finally:
        tenant.reset(reset)


def test_api_auth_and_object_ownership(monkeypatch, tmp_path):
    mod = importlib.import_module('smart_import.api.app')
    store = JobStore(tmp_path)
    monkeypatch.setattr(mod, 'store', store)
    monkeypatch.setattr(mod, 'CFG', Config().replace(api_keys={'a'*32: 'one', 'b'*32: 'two'}))
    reset = tenant.set('one')
    try:
        job = store.create('one.csv', 'vepathos_flat_v1')
        job.touch(NORMALIZED)
    finally:
        tenant.reset(reset)
    with TestClient(mod.app, raise_server_exceptions=False) as client:
        assert client.get(f'/imports/{job.id}').status_code == 401
        assert client.get(f'/imports/{job.id}', headers={'Authorization': 'Bearer '+'a'*32}).status_code == 200
        for method, path in [('get', ''), ('get', '/preview'), ('get', '/download'), ('delete', '')]:
            response = getattr(client, method)(f'/imports/{job.id}{path}', headers={'Authorization': 'Bearer '+'b'*32})
            assert response.status_code == 404
        assert store.get(job.id) is not None


def test_chunked_body_is_limited_before_parse():
    from smart_import.api.guards import RequestGuards
    async def scenario():
        called = False
        @asynccontextmanager
        async def admission():
            yield
        async def app(scope, receive, send):
            nonlocal called
            called = True
            while (await receive()).get('more_body'):
                pass
        chunks = iter([{'type': 'http.request', 'body': b'x' * 700000, 'more_body': True}]*2)
        async def receive():
            return next(chunks)
        messages = []
        async def send(message):
            messages.append(message)
        guards = RequestGuards(app, lambda: Config(), admission)
        await guards({'type': 'http', 'method': 'PUT', 'path': '/mapping', 'headers': []}, receive, send)
        assert called
        assert messages[0]['status'] == 413
    asyncio.run(scenario())


def test_xlsx_rejects_archive_expansion(tmp_path, monkeypatch):
    from zipfile import ZipFile, ZIP_DEFLATED
    from smart_import.readers import excel_reader
    archive = tmp_path / 'large.xlsx'
    with ZipFile(archive, 'w', ZIP_DEFLATED) as z:
        z.writestr('xl/sharedStrings.xml', 'x' * 4096)
    monkeypatch.setattr(excel_reader, 'MAX_EXPANDED_BYTES', 1024)
    with pytest.raises(ValueError, match='expansión'):
        excel_reader.read_xlsx(archive)


def test_http_transfer_bounds_body_and_removes_partial(tmp_path):
    import httpx
    from smart_import.artifacts import HttpArtifactStore, ArtifactRejected, RAW
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b'x'*2000)))
    store = HttpArtifactStore('http://local', 'token', tmp_path, client=client, max_bytes=1000)
    try:
        with pytest.raises(ArtifactRejected):
            store.resolve('imp_0123456789ab', RAW, None)
        assert not [p for p in tmp_path.rglob('*') if p.is_file()]
    finally:
        client.close()


def test_expired_cache_entry_is_not_returned(tmp_path, monkeypatch):
    from smart_import.geocoding import cache as module
    from smart_import.geocoding.base import GeocodeResult
    now = [1000.]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    cache = module.GeocodeCache(tmp_path / 'cache.sqlite', ttl_s=10)
    try:
        cache.put('synthetic', GeocodeResult(status='matched', lat=1., lon=2.))
        assert cache.get('synthetic') is not None
        now[0] += 11
        assert cache.get('synthetic') is None
    finally:
        cache.close()
