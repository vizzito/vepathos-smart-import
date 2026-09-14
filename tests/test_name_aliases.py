"""Alias bilingues OSM: Avenue Mozart ↔ Mozartstraat, sin if de pais."""
from __future__ import annotations

import sqlite3

from smart_import.geocoding.address import normalize_text, parse
from smart_import.geocoding.name_aliases import (
    StreetAliasStore,
    alias_names_from_tags,
    names_from_tags,
    pair_names,
)
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.osm_index import SCHEMA_SQL, _record
from smart_import.geocoding.scoring import _street_score, _strip_way_type


def test_names_from_tags_junta_idiomas_y_alt_name():
    tags = {
        "highway": "residential",
        "name": "Mozartstraat",
        "name:fr": "Avenue Mozart",
        "name:nl": "Mozartstraat",
        "alt_name": "Mozartlaan; Av. Mozart",
        "name:etymology": "Wolfgang Amadeus Mozart",
        "name:left": "Otra",
    }
    names = names_from_tags(tags)
    assert "Mozartstraat" in names
    assert "Avenue Mozart" in names
    assert "Mozartlaan" in names
    assert "Av. Mozart" in names
    assert "Wolfgang Amadeus Mozart" not in names
    assert "Otra" not in names


def test_poi_name_no_se_alias_con_la_calle():
    tags = {
        "building": "yes",
        "name": "Administration Communale de Useldange",
        "addr:street": "Rue de l'Église",
        "addr:housenumber": "1",
    }
    assert alias_names_from_tags(tags) == ["Rue de l'Église"]
    assert pair_names(alias_names_from_tags(tags)) == []


def test_highway_bilingue_si_se_empareja():
    tags = {
        "highway": "residential",
        "name": "Mozartstraat",
        "name:fr": "Avenue Mozart",
        "alt_name": "No es otra grafia",
        "addr:street": "Mozartstraat",
    }
    names = alias_names_from_tags(tags)
    assert "Mozartstraat" in names
    assert "Avenue Mozart" in names
    assert "No es otra grafia" not in names
    assert pair_names(names)
    pairs = pair_names(["Avenue Mozart", "Mozartstraat"])
    keys = {(k, normalize_text(v)) for k, v in pairs}
    assert ("avenue mozart", "mozartstraat") in keys
    assert ("mozartstraat", "avenue mozart") in keys


def test_strip_way_type_sufijo_pegado():
    assert _strip_way_type("mozartstraat") == "mozart"
    assert _strip_way_type("avenue mozart") == "mozart"
    assert _strip_way_type("hauptstrasse") == "haupt"


def test_street_score_mozart_frances_vs_neerlandes():
    parsed = parse("Avenue Mozart 12, Bruxelles")
    score = _street_score(parsed, normalize_text("Mozartstraat"), parsed.normalized)
    assert score >= 0.80


def test_index_record_mete_name_fr_en_fts():
    rec = _record(
        {"highway": "residential", "name": "Mozartstraat", "name:fr": "Avenue Mozart"},
        50.83, 4.38, "way", 1,
    )
    assert rec is not None
    text = rec[-1]
    assert "mozartstraat" in text
    assert "avenue mozart" in text
    assert "mozart" in text.split()


def test_alias_store_expande_query(tmp_path):
    store = StreetAliasStore(tmp_path / "aliases.sqlite")
    store.add_names(["Avenue Mozart", "Mozartstraat"])
    store.commit()
    assert "Mozartstraat" in store.expand("Avenue Mozart")
    assert "Avenue Mozart" in store.expand("Mozartstraat")
    store.close()


def test_geocode_avenue_mozart_con_indice_solo_neerlandes(tmp_path):
    """Indice viejo: FTS tiene Mozartstraat, no el frances. El mapa desbloquea."""
    idx = tmp_path / "idx.sqlite"
    conn = sqlite3.connect(idx)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
        " street, city, district, state, postcode, country, normalized_text)"
        " VALUES ('node', 1, 50.827, 4.379, 'building', NULL, '12',"
        " 'Mozartstraat', 'Ixelles', NULL, NULL, '1050', NULL, ?)",
        (normalize_text("12 Mozartstraat Ixelles 1050"),),
    )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()

    aliases = tmp_path / "aliases.sqlite"
    store = StreetAliasStore(aliases)
    store.add_names(["Avenue Mozart", "Mozartstraat"])
    store.commit()
    store.close()

    blind = LocalOSMGeocoder(idx)
    try:
        miss = blind.geocode("Avenue Mozart 12, 1050 Ixelles")
    finally:
        blind.close()
    assert not miss.has_coords or miss.confidence < 0.70

    geo = LocalOSMGeocoder(idx, aliases_path=aliases)
    try:
        hit = geo.geocode("Avenue Mozart 12, 1050 Ixelles")
    finally:
        geo.close()
    assert hit.has_coords
    assert abs(hit.lat - 50.827) < 0.001
    assert abs(hit.lon - 4.379) < 0.001
