"""Real Lua contract on an ephemeral Unix-socket Redis (no production port)."""
import os
import shutil
import subprocess
import time
import tempfile

import pytest

from smart_import.execution import Execution, ExecutionLost, executing
from smart_import.job_store_redis import RedisJobStore
from smart_import.jobs import NORMALIZED


@pytest.mark.skipif(os.environ.get("RUN_LOCAL_REDIS_TESTS") != "1",
                    reason="opt-in local Redis integration")
def test_real_redis_atomic_fencing(tmp_path):
    executable = shutil.which('redis-server')
    if not executable:
        pytest.skip('redis-server not installed')
    redis = pytest.importorskip('redis')
    scratch = tempfile.TemporaryDirectory(prefix='si-r-', dir='/tmp')
    from pathlib import Path
    socket = Path(scratch.name) / 'r.sock'
    process = subprocess.Popen([executable, '--port', '0', '--unixsocket', str(socket),
                                '--save', '', '--appendonly', 'no', '--dir', str(tmp_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from redis.retry import Retry
    from redis.backoff import NoBackoff
    client = redis.Redis(unix_socket_path=str(socket), socket_timeout=.1, retry=Retry(NoBackoff(), 0))
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.ConnectionError:
                if process.poll() is not None:
                    pytest.fail("Ephemeral Redis failed to start")
                time.sleep(.01)
        store = RedisJobStore(client)
        job = store.create('synthetic.csv', 'vepathos_flat_v1')
        job.touch(NORMALIZED)
        store.save(job)
        assert store.claim_normalize(job.id)
        assert not store.claim_geocode(job.id)
        first, second = 'a'*32, 'b'*32
        assert store.claim_run(job.id, first, 60)
        assert not store.claim_run(job.id, first, 60)
        with executing(Execution(job.id, first)):
            stale = store.get(job.id)
            store.save(stale)
            store.release_run(job.id, first)
            assert store.claim_run(job.id, second, 60)
            stale.touch(NORMALIZED)
            with pytest.raises(ExecutionLost):
                store.save(stale)
        store.release_run(job.id, first)
        assert not store.renew_run(job.id, first, 60)
        assert store.renew_run(job.id, second, 60)
        assert store.get(job.id).busy
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)
        scratch.cleanup()
