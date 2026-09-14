"""Colisiones de grilla / nombres contenidos: 1st≠71st, Brickell≠Key, etc.

El geocoder tiene que quedarse con la CALLE pedida. Recorrer 'todas las
alturas 350' y elegir la más cerca del depot elegiría 350 NE 2nd St — igual de
mal. Estos tests no usan PBF: arman un indice minimo.
"""
from __future__ import annotations

import sqlite3

import pytest

from smart_import.geocoding.address import normalize_text, parse
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.osm_index import SCHEMA_SQL
from smart_import.geocoding.scoring import _street_score

pytestmark = pytest.mark.geocoding


@pytest.mark.parametrize("query,cand,ok", [
    ("350 NE 1st Ave, Miami", "Northeast 1st Avenue", True),
    ("350 NE 1st Ave, Miami", "NE 1st Ave", True),
    ("350 NE 1st Ave, Miami", "Northeast 71st Street", False),
    ("350 NE 1st Ave, Miami", "Northeast 21st Avenue", False),
    ("350 NE 1st Ave, Miami", "Northwest 1st Avenue", False),
    ("350 NE 1st Ave, Miami", "Southeast 1st Avenue", False),
    ("350 NE 1st Ave, Miami", "Northeast 1st Street", True),  # mismo 1st + NE
    ("1200 NW 7th Ave, Miami", "Northwest 7th Avenue", True),
    ("1200 NW 7th Ave, Miami", "Northeast 7th Avenue", False),
    ("1200 NW 7th Ave, Miami", "Northwest 17th Avenue", False),
    ("801 Brickell Ave, Miami", "Brickell Avenue", True),
    ("801 Brickell Ave, Miami", "Brickell Ave", True),
    ("801 Brickell Ave, Miami", "Brickell Key Boulevard", False),
    ("801 Brickell Ave, Miami", "Brickell Bay Drive", False),
    ("200 Ocean Dr, Miami", "Ocean Drive", True),
    ("100 Biscayne Blvd", "Biscayne Boulevard", True),
    ("AV CORRIENTES 919, CABA", "Avenida Corrientes", True),
    ("AV CORRIENTES 919, CABA", "Cabo Corrientes", False),
    ("11 de septiembre 1735, CABA", "11 de Septiembre de 1888", True),
    ("11 de septiembre 1735, CABA", "29 de Septiembre", False),
    ("Av. Rivadavia 4800", "Avenida Rivadavia", True),
    ("Fragata Sarmiento 1572, Tandil", "Fragata Sarmiento", True),
    ("Fragata Sarmiento 1572, Tandil", "Sarmiento", False),
    ("1500 Collins Ave Miami Beach", "Collins Avenue", True),
    ("E 55 ST, 1619, 11234", "East 55th Street", True),
    ("E 55 ST, 1619, 11234", "West 55th Street", False),
    ("E 55 ST, 1619, 11234", "East 56th Street", False),
    ("1445 68 ST, 11219", "68th Street", True),
    ("1445 68 ST, 11219", "168th Street", False),
    ("AVE U, 737, 11223", "Avenue U", True),
    ("W END AVE, 675, 10025", "West End Avenue", True),
    ("Avenue Mozart 12, Bruxelles", "Mozartstraat", True),
    ("Str. Mihai Viteazul 10", "Strada Mihai Viteazul", True),
])
def test_street_score_acepta_o_rechaza(query, cand, ok):
    parsed = parse(query)
    score = _street_score(parsed, normalize_text(cand), parsed.normalized)
    if ok:
        assert score >= 0.80, (cand, score)
    else:
        # Fuzzy puede dar 0.80; no puede ser 1.0 (eso elige la calle equivocada).
        assert score < 1.0, (cand, score)


def _index(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for i, (lat, lon, house, street, city, text) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES ('node',?,?,?,?,NULL,?,?,?,NULL,NULL,NULL,NULL,?)",
            (i, lat, lon, "building", house, street, city, normalize_text(text)),
        )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return LocalOSMGeocoder(path)


def test_1st_ave_no_elige_71st_aunque_haya_350_ahi(tmp_path):
    g = _index(tmp_path / "a.sqlite", [
        (25.83994, -80.18957, "350", "Northeast 71st Street", "Miami",
         "350 northeast 71st street miami fl 33138"),
        (25.77766, -80.19247, "300", "Northeast 1st Avenue", "Miami",
         "300 northeast 1st avenue miami fl 33132"),
        (25.80699, -80.18840, "350", "Northeast 32nd Street", "Miami",
         "350 northeast 32nd street miami fl 33137"),
    ])
    r = g.geocode("350 NE 1st Ave, Miami, United States",
                  origin=(25.77427, -80.19366))
    assert r.has_coords
    assert r.lat == pytest.approx(25.77766, abs=1e-4)
    assert "71st" not in (r.matched_text or "")
    assert "32nd" not in (r.matched_text or "")
    assert r.precision == "street"


def test_nw_no_elige_ne_con_la_misma_altura(tmp_path):
    g = _index(tmp_path / "b.sqlite", [
        (25.7750, -80.1910, "1200", "Northeast 7th Avenue", "Miami",
         "1200 northeast 7th avenue"),
        (25.7858, -80.2070, "1200", "Northwest 7th Avenue", "Miami",
         "1200 northwest 7th avenue"),
    ])
    r = g.geocode("1200 NW 7th Ave, Miami, United States",
                  origin=(25.77427, -80.19366))
    assert r.has_coords
    assert r.lon == pytest.approx(-80.2070, abs=1e-3)
    assert "northwest" in (r.matched_text or "")


def test_brickell_ave_no_elige_brickell_key(tmp_path):
    g = _index(tmp_path / "c.sqlite", [
        (25.76838, -80.18671, "801", "Brickell Key Boulevard", "Miami",
         "801 brickell key boulevard"),
        (25.76561, -80.19039, "801", "Brickell Avenue", "Miami",
         "801 brickell avenue one brickell square"),
        (25.76470, -80.18936, "801", "Brickell Bay Drive", "Miami",
         "801 brickell bay drive"),
    ])
    r = g.geocode("801 Brickell Ave, Miami, United States",
                  origin=(25.77427, -80.19366))
    assert r.has_coords
    assert r.lat == pytest.approx(25.76561, abs=1e-4)
    assert "key" not in (r.matched_text or "")
    assert "bay" not in (r.matched_text or "")
    assert r.precision == "housenumber"


def test_ocean_dr_no_elige_ocean_lane(tmp_path):
    g = _index(tmp_path / "d.sqlite", [
        (25.69855, -80.15715, "200", "Ocean Lane Drive", "Key Biscayne",
         "200 ocean lane drive key biscayne"),
        (25.77111, -80.13301, "200", "Ocean Drive", "Miami",
         "200 ocean drive miami fl 33139"),
    ])
    r = g.geocode("200 Ocean Dr, Miami, United States",
                  origin=(25.77427, -80.19366))
    assert r.has_coords
    assert r.lat == pytest.approx(25.77111, abs=1e-4)


def test_corrientes_no_elige_cabo_corrientes(tmp_path):
    g = _index(tmp_path / "e.sqlite", [
        (-34.80, -58.48, "919", "Cabo Corrientes", "Monte Grande",
         "919 cabo corrientes"),
        (-34.60345, -58.37958, "902", "Avenida Corrientes",
         "Ciudad Autónoma de Buenos Aires",
         "902 avenida corrientes ciudad autonoma de buenos aires"),
    ])
    r = g.geocode("AV CORRIENTES 919, CABA, Argentina",
                  origin=(-34.6037, -58.3816))
    assert r.has_coords
    assert "cabo" not in (r.matched_text or "")
    assert r.lat == pytest.approx(-34.60345, abs=1e-3)


def test_fragata_sarmiento_no_es_sarmiento():
    """Substring no alcanza: son calles distintas. Localidad pegada no tumba Collins."""
    fragata = parse("Fragata Sarmiento 1572, Tandil")
    assert fragata.road and "fragata" in normalize_text(fragata.road)
    assert _street_score(fragata, normalize_text("Sarmiento"),
                         fragata.normalized) < 0.80
    assert _street_score(fragata, normalize_text("Fragata Sarmiento"),
                         fragata.normalized) == 1.0
    collins = parse("1500 Collins Ave Miami Beach, United States")
    assert _street_score(collins, normalize_text("Collins Avenue"),
                         collins.normalized) == 1.0


def test_fts_fragata_sarmiento_exige_ambos_tokens():
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("Fragata Sarmiento 1572, Tandil")
    q = LocalOSMGeocoder._precise_query(g, p)
    assert q is not None
    assert " AND " in q
    assert '"fragata" OR "sarmiento"' not in q
    assert "fragata" in q and "sarmiento" in q and "1572" in q
    loose = LocalOSMGeocoder._precise_query(g, p, strict=False)
    assert loose is not None
    assert '"fragata" OR "sarmiento"' in loose
    date_q = LocalOSMGeocoder._fts_query(g, parse("11 de septiembre 1735, CABA"),
                                         strict=False)
    assert " AND " in date_q
    assert '"11" OR "septiembre"' not in date_q


def test_and_falla_or_recupera_si_osm_no_tiene_todos_los_tokens(tmp_path):
    """AND 'collins' AND 'miami' AND 'beach' no pega un nodo que solo dice Collins.

    OR es recall, no otra calle: el score ya ignora la localidad pegada al road.
    """
    p = parse("1500 Collins Ave Miami Beach, United States")
    words = LocalOSMGeocoder._search_words(
        LocalOSMGeocoder.__new__(LocalOSMGeocoder), p)
    assert "collins" in [w.casefold() for w in words]
    g = _index(tmp_path / "collins.sqlite", [
        (25.7906, -80.1300, "1500", "Collins Avenue", "Miami Beach",
         "1500 collins avenue"),
        (25.7780, -80.1900, "1500", "Brickell Avenue", "Miami",
         "1500 brickell avenue miami"),
    ])
    r = g.geocode(
        "1500 Collins Ave Miami Beach, United States",
        origin=(25.7743, -80.1937),
    )
    assert r.has_coords
    assert "collins" in (r.matched_text or "")
    assert "brickell" not in (r.matched_text or "")


def test_fragata_sarmiento_no_salta_a_sarmiento_por_la_altura(tmp_path):
    """Sin el 1572, interpolar en Fragata (1502/1599), no en Sarmiento 1502 del centro."""
    g = _index(tmp_path / "fragata.sqlite", [
        (-37.3200, -59.1239, "1502", "Sarmiento", "Tandil",
         "1502 sarmiento"),
        (-37.3226, -59.1618, "1502", "Fragata Sarmiento", "Tandil",
         "1502 fragata sarmiento"),
        (-37.3217, -59.1622, "1599", "Fragata Sarmiento", "Tandil",
         "1599 fragata sarmiento"),
    ])
    r = g.geocode(
        "Fragata Sarmiento 1572, B7000 Tandil, Argentina",
        origin=(-37.3212, -59.1344),
    )
    assert r.has_coords
    assert "fragata" in (r.matched_text or "")
    # Entre 1502 (-37.3226,-59.1618) y 1599 (-37.3217,-59.1622), no en un extremo.
    assert min(-37.3226, -37.3217) < r.lat < max(-37.3226, -37.3217)
    assert min(-59.1618, -59.1622) <= r.lon <= max(-59.1618, -59.1622)
    assert r.lon != pytest.approx(-59.1622, abs=1e-6)
    assert r.precision == "street"
    assert r.detail.get("reason") == "interpolated_house"
    # AND ya trajo Fragata: el OR no debe ni listar la homonima.
    p = parse("Fragata Sarmiento 1572, B7000 Tandil, Argentina")
    streets = {c.street for c in g._candidates(p, None)}
    assert "Sarmiento" not in streets
    assert any(s and "Fragata" in s for s in streets)


def test_fts_1st_no_va_en_el_or_del_cuadrante():
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("350 NE 1st Ave, Miami")
    q = LocalOSMGeocoder._precise_query(g, p)
    assert q is not None
    assert 'northeast" OR "1st"' not in q
    assert "1st" in q and "350" in q
    assert " AND " in q


def test_fts_e_55_st_busca_east_y_55th():
    """OA 'E 55 ST' no puede MATCH-ear OSM si pide 'este' AND '55' (sin 55th)."""
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("1619 E 55 ST, 11234")
    q = LocalOSMGeocoder._precise_query(g, p)
    assert q is not None
    assert "1619" in q
    assert "east" in q
    assert "55th" in q
    assert "street" in q
    assert " OR " in q
    assert '"este" AND "east"' not in q
    assert '"55" AND "55th"' not in q


def test_fts_68_st_conserva_street_y_ordinal():
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("1445 68 ST, 11219")
    q = LocalOSMGeocoder._precise_query(g, p)
    assert q is not None
    assert "1445" in q
    assert "68th" in q
    assert "street" in q


def test_fts_esponente_slash_letra_no_exige_and_b():
    """'15/B' debe recuperar OSM '15' / '15B'; AND '15' AND 'B' no pega."""
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("VIALE DODICI GIUGNO, 15/B, 40124")
    clause = LocalOSMGeocoder._house_fts_clause(g, p.house_number or "15/B")
    assert p.house_number == "15/B"
    assert '"15"' in clause
    assert '"15" AND "B"' not in clause and '"15" AND "b"' not in clause


def test_fts_altura_con_guion_busca_ambos_tokens():
    """El indice parte '40-49' en 40 y 49; compactar a 4049 no alcanza."""
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("40-49 172 ST, 11358")
    q = LocalOSMGeocoder._precise_query(g, p)
    assert q is not None
    clause = LocalOSMGeocoder._house_fts_clause(g, p.house_number or "40-49")
    assert clause is not None
    assert '"40"' in clause and '"49"' in clause
    assert " AND " in clause


def test_fts_11_de_septiembre_no_exige_11th():
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    q = LocalOSMGeocoder._fts_query(g, parse("11 de septiembre 1735, CABA"),
                                   strict=False)
    assert '"septiembre"' in q
    assert " AND " in q
    assert '"11" OR "septiembre"' not in q
    assert "street" not in q


def test_e_55_st_elige_street_no_avenue(tmp_path):
    g = _index(tmp_path / "e55.sqlite", [
        (40.6460, -73.9220, "1619", "East 55th Avenue", "Brooklyn",
         "1619 east 55th avenue brooklyn ny 11234"),
        (40.61718, -73.92166, "1619", "East 55th Street", "Brooklyn",
         "1619 east 55th street brooklyn ny 11234"),
    ])
    r = g.geocode("1619 E 55 ST, 11234, New York, NY, United States",
                  origin=(40.65, -73.95))
    assert r.has_coords
    assert r.lat == pytest.approx(40.61718, abs=1e-4)
    assert "street" in (r.matched_text or "")
    assert "avenue" not in (r.matched_text or "")


def test_hyphen_queens_resuelve_altura(tmp_path):
    g = _index(tmp_path / "hyphen.sqlite", [
        (40.72993, -73.86884, "61-30", "Wetherole Street", "Queens",
         "61-30 wetherole street queens ny 11374"),
        (40.73010, -73.86900, "61", "Wetherole Street", "Queens",
         "61 wetherole street queens ny 11374"),
    ])
    r = g.geocode("61-30 WETHEROLE ST, 11374, New York, NY, United States",
                  origin=(40.73, -73.87))
    assert r.has_coords
    assert r.lat == pytest.approx(40.72993, abs=1e-4)
    assert r.precision == "housenumber"


def _index_geo(path, rows):
    """lat, lon, house, street, city, postcode, text."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for i, (lat, lon, house, street, city, postcode, text) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES ('node',?,?,?,?,NULL,?,?,?,NULL,NULL,?,NULL,?)",
            (i, lat, lon, "building", house, street, city, postcode,
             normalize_text(text)),
        )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return LocalOSMGeocoder(path)


def test_cp_distinto_no_elige_homonimo_de_otra_ciudad(tmp_path):
    """Rue Madame 10 en París 6e no es la de Versailles aunque OSM tenga 10."""
    from smart_import.geocoding.base import STATUS_NOT_FOUND

    g = _index_geo(tmp_path / "madame.sqlite", [
        (48.8029, 2.1350, "10", "Rue Madame", "Versailles", "78000",
         "10 rue madame versailles 78000"),
        (48.8516, 2.3318, "12", "Rue Madame", "Paris", "75006",
         "10 rue madame paris 75006"),
    ])
    r = g.geocode(
        "Rue Madame, 10, Paris 6e Arrondissement, 75006",
        origin=(48.8566, 2.3522),
    )
    if r.has_coords:
        assert r.lat == pytest.approx(48.8516, abs=1e-3)
        assert "versailles" not in (r.matched_text or "")
    else:
        assert r.status == STATUS_NOT_FOUND
        assert r.detail.get("reason") == "place_mismatch"


def test_elige_paris_no_versailles_cuando_ambos_tienen_la_altura(tmp_path):
    g = _index_geo(tmp_path / "madame2.sqlite", [
        (48.8029, 2.1350, "10", "Rue Madame", "Versailles", "78000",
         "10 rue madame versailles 78000"),
        (48.8516, 2.3318, "10", "Rue Madame", "Paris", "75006",
         "10 rue madame paris 75006"),
    ])
    r = g.geocode(
        "Rue Madame, 10, Paris 6e Arrondissement, 75006",
        origin=(48.8566, 2.3522),
    )
    assert r.has_coords
    assert r.lat == pytest.approx(48.8516, abs=1e-4)
    assert r.precision == "housenumber"
    assert "versailles" not in (r.matched_text or "")


def test_zip_distinto_no_elige_otra_ciudad_usa(tmp_path):
    from smart_import.geocoding.base import STATUS_NOT_FOUND

    g = _index_geo(tmp_path / "carol.sqlite", [
        (41.03, -73.93, "8", "Carol Court", "Yonkers", "10701",
         "8 carol court yonkers ny 10701"),
        (40.516, -74.206, "12", "Carol Court", "Staten Island", "10309",
         "8 carol court staten island ny 10309"),
    ])
    r = g.geocode("CAROL CT, 8, 10309, New York, NY, United States",
                  origin=(40.58, -74.15))
    if r.has_coords:
        assert "yonkers" not in (r.matched_text or "")
        assert r.lat == pytest.approx(40.516, abs=1e-2)
    else:
        assert r.status == STATUS_NOT_FOUND
        assert r.detail.get("reason") == "place_mismatch"


def test_zip_igual_permite_alias_de_borough(tmp_path):
    g = _index_geo(tmp_path / "si.sqlite", [
        (40.516, -74.206, "8", "Carol Court", "Staten Island", "10309",
         "8 carol court staten island ny 10309"),
    ])
    r = g.geocode("8 CAROL CT, 10309, New York, NY, United States",
                  origin=(40.58, -74.15))
    assert r.has_coords
    assert r.precision == "housenumber"
    assert r.lat == pytest.approx(40.516, abs=1e-4)


def test_arrondissement_distinto_sin_cp_en_osm(tmp_path):
    from smart_import.geocoding.base import STATUS_NOT_FOUND

    g = _index_geo(tmp_path / "arr.sqlite", [
        (48.86, 2.27, "10", "Rue Madame", "Paris 16e Arrondissement", None,
         "10 rue madame paris 16e"),
    ])
    r = g.geocode(
        "Rue Madame, 10, Paris 6e Arrondissement",
        origin=(48.8566, 2.3522),
    )
    assert r.status == STATUS_NOT_FOUND
    assert r.detail.get("reason") == "place_mismatch"
