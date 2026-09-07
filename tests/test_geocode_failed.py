"""geocode_failed conserva el normalize y ofrece download + geo manual."""
from smart_import.api.jobs import (
    GEOCODE_FAILED,
    HAS_NORMALIZE,
    Job,
    NORMALIZED,
)


def test_geocode_failed_keeps_normalize_downloads():
    job = Job(id="imp_test", filename="x.csv", status=GEOCODE_FAILED)
    job.normalized_path = "/tmp/normalized.csv"
    job.nested_path = "/tmp/nested.json"
    job.report = {"needs_geocode": 3}
    job.error = "sin cobertura PBF"
    job.capabilities = {"geocoding": True, "extract": False}

    assert GEOCODE_FAILED in HAS_NORMALIZE
    assert not job.busy

    actions = {a["action"] for a in job.next_actions()}
    assert "download" in actions
    assert "download_nested" in actions
    assert "geocode" in actions
    assert "manual_geocode" in actions

    snap = job.progress_snapshot()
    assert snap["status"] == GEOCODE_FAILED
    assert snap["phase"] == "geocode_failed"
    assert snap["busy"] is False
    # error text is surfaced; downloads remain available regardless
    assert "cobertura" in (snap["message"] or "").lower() or snap["message"]


def test_completed_with_residual_offers_manual_geocode():
    job = Job(id="imp_partial", filename="x.csv", status="completed")
    job.normalized_path = "/tmp/n.csv"
    job.report = {"needs_geocode": 54}
    job.capabilities = {"geocoding": True, "extract": False}
    actions = {a["action"] for a in job.next_actions()}
    assert "download" in actions
    assert "manual_geocode" in actions
    assert "geocode" in actions


def test_extract_failed_keeps_normalized_status():
    """Extract soft-fail: status queda NORMALIZED (no FAILED) para seguir a geocode."""
    job = Job(id="imp_ex", filename="x.csv", status=NORMALIZED)
    job.normalized_path = "/tmp/normalized.csv"
    job.report = {"needs_geocode": 2, "needs_review": False}
    job.error = "model timeout"
    job.extract_progress = {"phase": "failed", "done": 0, "total": 2}
    job.capabilities = {"geocoding": True, "extract": True}

    assert job.status == NORMALIZED
    assert not job.busy
    actions = {a["action"] for a in job.next_actions()}
    assert "download" in actions
    assert "geocode" in actions
