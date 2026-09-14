"""Generador de corpus: sin red, solo reglas de muestreo y formato."""
import gzip
import io
import json
import random
import zipfile

from smart_import.tools.address_corpus import (
    CorpusRecord,
    LANGUAGE_PACK,
    LIBPOSTAL_DEFAULT_TSV,
    LOCAL_OA_FIXTURES,
    OPENADDRESSES_PRESETS,
    PRESET_PACKS,
    build_address,
    ensure_truth_corpus,
    iter_geojson_features,
    load_openaddresses_fixture,
    load_openaddresses_zip,
    load_parser_text_corpus,
    mix_corpus_records,
    oa_row_to_record,
    resolve_sample_seed,
    sample_filename_tag,
    with_corpus_suffixes,
    with_seed_suffix,
    parse_truth_generate_spec,
    reservoir_sample,
    suggest_accuracy_flags,
    tag_record_preset,
    write_csv_corpus,
    write_json_corpus,
    preset_for_group,
    _normalize_oa_feature,
    _normalize_oa_row,
    _parse_parser_tsv_line,
)


def test_normalize_oa_row_requiere_calle_numero_coords():
    assert _normalize_oa_row({"STREET": "Corrientes", "NUMBER": "1234",
                              "LAT": "-34.6", "LON": "-58.38"}) is not None
    assert _normalize_oa_row({"STREET": "Corrientes", "NUMBER": "1234"}) is None
    assert _normalize_oa_row({"STREET": "X", "NUMBER": "1",
                              "LAT": "999", "LON": "0"}) is None
    assert _normalize_oa_row({"STREET": "street", "NUMBER": "number",
                              "LAT": "0", "LON": "0"}) is None


def test_build_address_estilos():
    row = {
        "street": "Avenida Santa Fe",
        "number": "2500",
        "unit": "4B",
        "city": "Buenos Aires",
        "region": "CABA",
        "postcode": "C1425",
        "_lat_f": "-34.59",
        "_lng_f": "-58.40",
    }
    assert "2500" in build_address(row, "us")
    assert build_address(row, "us").startswith("2500 Avenida Santa Fe")
    assert build_address(row, "eu").startswith("Avenida Santa Fe 2500")
    assert build_address(row, "minimal") == "Avenida Santa Fe 2500, Buenos Aires"
    assert "Depto 4B" in build_address(row, "full")


def test_reservoir_sample_uniforme():
    rng = random.Random(0)
    data = list(range(1000))
    sample = reservoir_sample(iter(data), 10, rng)
    assert len(sample) == 10
    assert len(set(sample)) == 10


def test_load_openaddresses_zip_desde_memoria():
    csv_body = (
        "HASH,NUMBER,STREET,CITY,REGION,POSTCODE,LON,LAT\n"
        "abc1,100,Main St,Springfield,IL,62701,-89.65,39.78\n"
        "abc2,200,Oak Ave,Springfield,IL,62702,-89.66,39.79\n"
        "bad,,NoNumber,,,,\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("us/il/statewide.csv", csv_body)
    records = load_openaddresses_zip(
        buf.getvalue(), sample_size=2, seed=1,
        source_label="test", styles=("oa_default", "us"))
    assert len(records) == 4  # 2 filas × 2 estilos
    assert all(r.lat is not None and r.lng is not None for r in records)
    styles = {r.style for r in records}
    assert styles == {"oa_default", "us"}


def test_parse_parser_tsv_line():
    assert _parse_parser_tsv_line("en\tUS\t100 Main St") == ("100 Main St", "en", "US")
    assert _parse_parser_tsv_line("es\tAv. Corrientes 1234") == ("Av. Corrientes 1234", "es", None)


def test_load_parser_text_corpus_fixture():
    records = load_parser_text_corpus(LIBPOSTAL_DEFAULT_TSV, sample_size=3, seed=1)
    assert len(records) == 3
    assert all(not r.has_coords for r in records)


def test_normalize_oa_feature():
    feature = {
        "properties": {"hash": "x", "number": "100", "street": "Main St", "city": "Town"},
        "geometry": {"type": "Point", "coordinates": [-58.38, -34.60]},
    }
    row = _normalize_oa_feature(feature)
    assert row is not None
    assert row["street"] == "Main St"
    assert float(row["_lat_f"]) == -34.60


def test_iter_geojson_features():
    payload = {
        "type": "FeatureCollection",
        "features": [{
            "properties": {"number": "1", "street": "Fake", "hash": "h1"},
            "geometry": {"coordinates": [-58.0, -34.0], "type": "Point"},
        }],
    }
    raw = gzip.compress(json.dumps(payload).encode())
    rows = list(iter_geojson_features(raw))
    assert len(rows) == 1


def test_iter_geojson_features_ndjson():
    line = json.dumps({
        "type": "Feature",
        "properties": {"number": "100", "street": "Main", "hash": "h2"},
        "geometry": {"coordinates": [-58.38, -34.60], "type": "Point"},
    })
    raw = gzip.compress((line + "\n").encode())
    rows = list(iter_geojson_features(raw))
    assert len(rows) == 1
    assert rows[0]["street"] == "Main"


def test_load_openaddresses_fixture_argentina():
    records = load_openaddresses_fixture("argentina", sample_size=5, seed=1)
    assert len(records) == 5
    assert all(r.has_coords for r in records)
    assert LOCAL_OA_FIXTURES["argentina"].is_file()


def test_parse_truth_generate_spec():
    spec = parse_truth_generate_spec(
        __import__("pathlib").Path("argentina_fixture_n150.json"))
    assert spec is not None
    assert spec.preset == "argentina"
    assert spec.sample_size == 150
    assert spec.use_fixture is True

    batch = parse_truth_generate_spec(
        __import__("pathlib").Path("spain_n500.json"))
    assert batch is not None
    assert batch.preset == "spain"
    assert batch.use_fixture is False

    assert parse_truth_generate_spec(
        __import__("pathlib").Path("caba_stops_2907.json")) is None


def test_ensure_truth_corpus_crea_fixture_si_falta(tmp_path):
    out = tmp_path / "argentina_fixture_n150.json"
    assert not out.is_file()
    path = ensure_truth_corpus(out, seed=42)
    assert path == out.resolve()
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["preset"] == "argentina"
    assert payload["with_coords"] == 15
    assert payload["source"] == "generate_address_corpus"
    assert payload["stops"][0]["source"] == "fixture:argentina"
    assert (tmp_path / "argentina_fixture_n150.csv").is_file()


def test_fetch_oa_asset_sigue_redirect_sin_bearer(monkeypatch):
    import urllib.error
    from smart_import.tools import address_corpus as mod

    calls: list[tuple[str, str | None]] = []

    def fake_open(req, timeout=0):
        url = req.full_url
        auth = req.headers.get("Authorization")
        calls.append((url, auth))
        if "batch.openaddresses.io" in url:
            raise urllib.error.HTTPError(
                url, 302, "Found", {"Location": "https://cdn.example/data.gz"}, None)
        from io import BytesIO
        return BytesIO(b"gzdata")

    class FakeOpener:
        def open(self, req, timeout=0):
            return fake_open(req, timeout)

    monkeypatch.setattr(mod.urllib.request, "build_opener",
                        lambda *a, **k: FakeOpener())
    data = mod._fetch_oa_asset(
        "https://batch.openaddresses.io/api/job/1/output/source.geojson.gz",
        timeout=10, token="oa.test")
    assert data == b"gzdata"
    assert calls[0][1] == "Bearer oa.test"
    assert calls[1][1] is None


def test_write_json_corpus(tmp_path):
    rec = oa_row_to_record(
        {"street": "Defensa", "number": "800", "city": "CABA",
         "_lat_f": "-34.617", "_lng_f": "-58.372", "hash": "x1"},
        source="test", style="oa_default", country="AR",
    )
    out = tmp_path / "sample.json"
    write_json_corpus([rec], out, note="test")
    payload = out.read_text(encoding="utf-8")
    assert "Defensa" in payload
    assert '"with_coords": 1' in payload


def test_new_sample_cambia_el_seed():
    assert resolve_sample_seed(42, new_sample=False) == 42
    assert resolve_sample_seed(None, new_sample=False) == 42
    a = resolve_sample_seed(42, new_sample=True)
    b = resolve_sample_seed(42, new_sample=True)
    assert a != 42
    assert b != a
    assert sample_filename_tag(100, 42) == "n100_s42"
    assert sample_filename_tag(100, 184729) == "n100_s184729"
    assert sample_filename_tag(100, 42, noise_level=5) == "n100_noise5_s42"
    from pathlib import Path
    assert with_seed_suffix(Path("languages_mix_n100.json"), 42).name == (
        "languages_mix_n100_s42.json")
    assert with_seed_suffix(Path("languages_mix_n100_s42.json"), 99).name == (
        "languages_mix_n100_s42.json")
    assert with_corpus_suffixes(
        Path("languages_mix_n100.json"), seed=42, noise_level=5,
    ).name == "languages_mix_n100_noise5_s42.json"
    assert with_corpus_suffixes(
        Path("languages_mix_n100_s42.json"), seed=99, noise_level=5,
    ).name == "languages_mix_n100_noise5_s42.json"
    assert with_corpus_suffixes(
        Path("languages_mix_n100_noise5_s42.json"), seed=1, noise_level=3,
    ).name == "languages_mix_n100_noise5_s42.json"


def test_mix_corpus_un_archivo_barajado(tmp_path):
    ar = tag_record_preset(oa_row_to_record(
        {"street": "Defensa", "number": "800", "city": "CABA",
         "_lat_f": "-34.617", "_lng_f": "-58.372", "hash": "a1"},
        source="oa", style="oa_default", country="AR"), "argentina")
    fr = tag_record_preset(oa_row_to_record(
        {"street": "Rue Jadin", "number": "16", "city": "Paris",
         "_lat_f": "48.88", "_lng_f": "2.30", "hash": "f1"},
        source="oa", style="oa_default", country="FR"), "france")
    mixed = mix_corpus_records([[ar], [fr]], seed=1)
    assert len(mixed) == 2
    assert {r.country for r in mixed} == {"AR", "FR"}
    assert {r.extra["preset"] for r in mixed} == {"argentina", "france"}

    out = tmp_path / "languages_mix_n2.json"
    write_json_corpus(mixed, out, note="mix", pack="languages",
                      countries=["AR", "FR"])
    payload = __import__("json").loads(out.read_text(encoding="utf-8"))
    assert payload["pack"] == "languages"
    assert payload["rows"] == 2
    assert "AR" in payload["countries"]
    csv_path = tmp_path / "languages_mix_n2.csv"
    write_csv_corpus(mixed, csv_path)
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert "preset" in header
    assert "country" in header


def test_language_pack_cubre_treinta_paises():
    assert "languages" in PRESET_PACKS
    for name in LANGUAGE_PACK:
        assert name in OPENADDRESSES_PRESETS, name
    countries = {OPENADDRESSES_PRESETS[n].country for n in LANGUAGE_PACK}
    assert len(LANGUAGE_PACK) >= 30
    assert len(countries) >= 30
    assert {"AR", "FR", "BR", "DE", "IT", "NL", "PL", "US", "NO"} <= countries
    assert "norway" in LANGUAGE_PACK
    france = suggest_accuracy_flags([], preset="france")
    assert "--depot-city Paris" in france
    germany = suggest_accuracy_flags([], preset="germany")
    assert "--depot-city Berlin" in germany


def test_preset_for_group_acepta_slug_e_iso():
    france = preset_for_group("france")
    assert france is not None
    assert france.country == "FR"
    assert france.depot_city == "Paris"
    assert preset_for_group("FR") is france or preset_for_group("FR").country == "FR"
    assert preset_for_group("fr").country == "FR"
    assert preset_for_group("nope") is None


def test_suggest_accuracy_flags_respeta_pais():
    paris = CorpusRecord(
        address="Rue Jadin 16, Paris", lat=48.881, lng=2.305,
        city="Paris 17e Arrondissement", country="FR", has_coords=True)
    hint = suggest_accuracy_flags([paris], preset="france")
    assert "--depot-city Paris" in hint
    assert "--depot-country France" in hint
    assert "CABA" not in hint
    assert "--origin-lat 48.8566" in hint
    assert "--origin-lon 2.3522" in hint

    caba = suggest_accuracy_flags([paris], preset="argentina")
    assert "--depot-city CABA" in caba
    assert "--depot-country Argentina" in caba

    sf = CorpusRecord(
        address="8 Dellbrook Ave", lat=37.75, lng=-122.45,
        city="San Francisco", country="US", has_coords=True)
    inferred = suggest_accuracy_flags([sf])
    assert '--depot-city "San Francisco"' in inferred
    assert "--depot-country" in inferred
    assert "--origin-lat 37.7500" in inferred
    assert "--origin-lon -122.4500" in inferred
