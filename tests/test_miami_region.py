"""Paste de Miami no puede terminar en Argentina porque phone_region default es AR."""
from pathlib import Path

from smart_import.addresses import HeuristicAddressParser
from smart_import.pipeline import run_normalize
from tests.conftest import SCHEMA

PASTE = (Path(__file__).resolve().parents[1]
         / "examples" / "geocode-truth" / "miami_whatsapp_6.txt").read_text(encoding="utf-8")


def test_parser_no_reduce_ne_1st_a_solo_ne():
    parsed = HeuristicAddressParser().parse("Brickell 350 NE 1st Ave")
    assert parsed.get("house_number") == "350"
    assert "1st" in (parsed.get("road") or "")
    assert parsed.get("road") != "NE"


def test_paste_miami_con_phone_region_ar_no_pone_argentina(tmp_path: Path):
    src = tmp_path / "paste.txt"
    src.write_text(PASTE, encoding="utf-8")
    result = run_normalize(
        src, SCHEMA, tmp_path / "out.csv", emit=("nested",),
        phone_region="AR",
    )
    assert result.deliveries
    for delivery in result.deliveries:
        addr = delivery.get("address") or ""
        assert "Argentina" not in addr, addr
        assert "United States" in addr or "Miami" in addr, addr


def test_paste_miami_con_depot_us(tmp_path: Path):
    src = tmp_path / "paste.txt"
    src.write_text(PASTE, encoding="utf-8")
    result = run_normalize(
        src, SCHEMA, tmp_path / "out.csv", emit=("nested",),
        phone_region="AR",
        depot_city="Miami", depot_country="United States",
    )
    addrs = [d.get("address") or "" for d in result.deliveries]
    names = [d.get("customer_name") or "" for d in result.deliveries]
    assert len(result.deliveries) == 6
    assert all("Argentina" not in a for a in addrs)
    assert any("NE 1st Ave" in a and "350" in a for a in addrs)
    assert any("NW 7th Ave" in a and "1200" in a for a in addrs)
    assert any("Collins" in a and "1500" in a for a in addrs)
    assert "Carlos Ruiz" in names
    assert not any("PH" in n for n in names)
