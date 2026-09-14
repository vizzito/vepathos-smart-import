"""Contract tests without installing an MCP SDK into the production venv."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

spec = importlib.util.spec_from_file_location("audit_adapter", Path(__file__).with_name("mcp_adapter_proposal.py"))
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)

VALID = {"address": "Av. Corrientes 1234", "city": "Buenos Aires", "country": "AR",
         "idempotency_key": "audit_request_0001"}


@pytest.mark.parametrize("change", [
    {"address": "x" * 2049}, {"address": " "}, {"address": "x\x00y"},
    {"origin": {"lat": 91, "lon": 0}}, {"origin": {"lat": float("nan"), "lon": 0}},
    {"geocode": "true"}, {"url": "http://169.254.169.254/"}, {"country": "Argentina"},
])
def test_invalid_input_never_reaches_backend(change):
    async def backend(*args):
        pytest.fail("backend called for invalid input")
    output = asyncio.run(adapter.invoke(backend, "smart_input", VALID | change, "tenant-a"))
    assert output.error.code == "INVALID_ARGUMENT"
    assert "169.254" not in output.model_dump_json()


def test_timeout_is_semantic_and_cancels_coroutine():
    async def backend(*args):
        await asyncio.sleep(1)
    output = asyncio.run(adapter.invoke(backend, "smart_input", VALID, "tenant-a", timeout_s=.001))
    assert output.error.code == "TIMEOUT" and output.error.retryable


def test_accepted_result_and_principal_propagation():
    async def backend(principal, name, request):
        assert principal == "tenant-a" and request.geocode is False
        return adapter.ToolOutput(state="accepted", job_id="imp_123456abcdef", poll_after_ms=1000)
    output = asyncio.run(adapter.invoke(backend, "smart_input", VALID, "tenant-a"))
    assert output.state == "accepted"


def test_unexpected_errors_do_not_expose_pii():
    async def backend(*args):
        raise RuntimeError("SECRET_TOKEN and customer address")
    output = asyncio.run(adapter.invoke(backend, "smart_input", VALID, "tenant-a"))
    assert output.error.code == "INTERNAL"
    assert "SECRET_TOKEN" not in output.model_dump_json()


def test_approximate_result_cannot_claim_matched():
    with pytest.raises(ValidationError):
        adapter.AddressResult(original_address="x", normalized_address="x",
                              point=adapter.Point(lat=0., lon=0.), precision="street",
                              status="matched", confidence=.99, requires_review=False)


def test_schemas_forbid_extra_properties():
    for model in (adapter.SmartInput, adapter.GetInput, adapter.ToolOutput):
        assert model.model_json_schema()["additionalProperties"] is False
