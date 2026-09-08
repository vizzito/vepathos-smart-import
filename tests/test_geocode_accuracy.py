"""Precisión del geocoder contra coordenadas verificadas.

Es el instrumento que decide si esto se puede ofrecer como servicio de
geolocalización. Fija la precisión actual para que cualquier cambio de scoring o
de parser se note en vez de descubrirse en producción.

Los tests que necesitan un índice OSM real se saltan solos si no está.
"""
import pytest

from smart_import.geocoding.accuracy import (
    Group, calibration, compose_street_and_number, dump_rows, find_column,
    find_house_column, format_calibration, format_libpostal_report,
    format_rows_table, has_house_number, load_truth,
)
from tests.conftest import ROOT

TRUTH = ROOT / "examples" / "geocode-truth"


def _segment_pos(sent: str, label: str) -> int | None:
    """Índice del segmento exacto (coma), no substring. Evita 'Patricias Argentinas'."""
    target = label.casefold()
    for i, part in enumerate(p.strip() for p in (sent or "").split(",")):
        if part.casefold() == target:
            return i
    return None


# ---------- deteccion de columnas: que entre cualquier export ----------

def test_accuracy_prende_libpostal_si_esta_instalado():
    """geocode-accuracy / real_geo deben usar enhanced, no heuristic a ciegas."""
    from smart_import.addresses.factory import build_address_parser
    from smart_import.addresses.libpostal_parser import is_installed
    from smart_import.config import Config
    from smart_import.geocoding.address import (
        bind_parser_config, unbind_parser_config, _address_parser,
    )

    if not is_installed():
        pytest.skip("libpostal no instalado")
    cfg = Config.from_env().replace(libpostal_enabled=True)
    bind_parser_config(cfg)
    try:
        parser = _address_parser()
        assert parser.name == "enhanced"
        assert build_address_parser(cfg).name == "enhanced"
    finally:
        unbind_parser_config()


def test_caba_prefiere_indice_de_extract_no_argentina():
    """Sin PBF de extract, CABA debe usar n-34.48…sqlite, no argentina.sqlite."""
    from smart_import.geocoding.osm_index import prefer_index
    from tests.conftest import ROOT

    indexes = ROOT / "data" / "indexes"
    caba = prefer_index(
        indexes / "argentina.sqlite", indexes,
        -34.6037, -58.3816,
        bbox=(-34.537, -34.697, -58.356, -58.530),
    )
    assert caba.name.startswith("n-34."), caba.name
    assert "argentina" not in caba.name
    assert "florida" not in caba.name.lower()


def test_detecta_las_columnas_del_export_de_mercadolibre():
    filas, columnas = load_truth(TRUTH / "caba_ml_13.csv")
    assert columnas == {"address": "address_line",
                        "lat": "receiver_address_latitude",
                        "lng": "receiver_address_longitude"}
    assert len(filas) >= 13


def test_detecta_las_columnas_del_json_nested():
    filas, columnas = load_truth(TRUTH / "caba_stops_2907.json")
    assert columnas == {"address": "address", "lat": "lat", "lng": "lng"}
    assert len(filas) == 2907


def test_detecta_las_columnas_del_json_tandil():
    """Tandil: calle+altura+CP+ciudad. Contraste de CABA 2907 (81% sin nro)."""
    filas, columnas = load_truth(TRUTH / "tandil_stops_326.json")
    assert columnas == {"address": "address", "lat": "lat", "lng": "lng"}
    assert len(filas) == 326
    assert all(has_house_number(str(f["address"])) for f in filas)
    assert any("Tandil" in str(f["address"]) for f in filas)
    assert any("Sarmiento" in str(f["address"]) for f in filas)


def test_detecta_las_columnas_del_paste_miami():
    filas, columnas = load_truth(TRUTH / "miami_whatsapp_6.json")
    assert columnas["address"] == "address"
    assert columnas["lat"] == "lat"
    assert columnas["lng"] == "lng"
    assert len(filas) == 6
    assert all(has_house_number(str(f["address"])) for f in filas)
    assert any("Biscayne" in str(f["address"]) for f in filas)
    assert any("Collins" in str(f["address"]) for f in filas)


def test_detecta_y_compone_orders_tiendanube_caba():
    """Calle y altura vienen separadas; el nro de orden no es la altura."""
    filas, columnas = load_truth(TRUTH / "caba_orders_13.json")
    assert columnas["address"] == "address"
    assert columnas["lat"] == "latitude"
    assert columnas["lng"] == "longitude"
    assert columnas["house"] == "shipping_address.number"
    assert len(filas) == 13
    assert find_house_column(list(filas[0])) == "shipping_address.number"
    rivadavia = next(f for f in filas if f.get("contact_name") == "Juan Pérez")
    assert rivadavia["address"] == "Av. Rivadavia 4800"
    assert rivadavia["shipping_address.number"] == "4800"
    assert rivadavia["number"] == 1001
    sin_coord = [f for f in filas if f.get("latitude") is None]
    assert len(sin_coord) == 3
    assert all(has_house_number(str(f["address"])) for f in filas)


@pytest.mark.parametrize("columnas,campo,esperado", [
    (["shipping_lat", "x"], "lat", "shipping_lat"),
    (["receiver_address_longitude"], "lng", "receiver_address_longitude"),
    (["Direccion de entrega"], "address", "Direccion de entrega"),
    (["nombre", "telefono"], "lat", None),
])
def test_columna_por_alias_o_por_substring(columnas, campo, esperado):
    assert find_column(columnas, campo) == esperado


def test_columna_faltante_falla_con_un_mensaje_util(tmp_path):
    csv = tmp_path / "sin_coords.csv"
    csv.write_text("nombre,telefono\nAna,123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no encuentro"):
        load_truth(csv)


# ---------- separar lo medible de lo que no lo es ----------

@pytest.mark.parametrize("direccion,tiene", [
    ("Av. Cabildo 2400", True),
    ("AV JUAN DE GARAY 03845", True),
    ("Av Victorica", False),          # sin altura: no se resuelve a puerta ni en teoria
    ("Nueva York, Argentina", False),
])
def test_reconoce_si_hay_altura(direccion, tiene):
    assert has_house_number(direccion) is tiene


@pytest.mark.parametrize("calle,altura,esperado", [
    ("Av. Rivadavia", "4800", "Av. Rivadavia 4800"),
    ("Av. Rivadavia 4800", "4800", "Av. Rivadavia 4800"),
    ("Av. Nazca", None, "Av. Nazca"),
])
def test_compone_calle_y_altura_sin_duplicar(calle, altura, esperado):
    assert compose_street_and_number(calle, altura) == esperado


def test_el_porcentaje_es_sobre_el_total_no_sobre_los_que_tuvieron_pin():
    """Un servicio se juzga por lo que resuelve de lo que le mandaste."""
    g = Group("test")
    g.add("valid", 50.0)
    g.add("valid", 80.0)
    g.add("needs_geocoding", None)     # sin pin
    g.add("needs_geocoding", None)
    d = g.as_dict()
    assert d["total"] == 4 and d["with_pin"] == 2
    assert d["within_100m"] == 2
    assert d["within_100m_pct"] == 50.0    # 2/4, no 2/2


def test_accuracy_enriquece_como_la_ui_con_depot():
    """Mismos params que POST /geocode: origin + geolocalizador (depot_city)."""
    from smart_import.geocoding.depot_context import depot_from_params
    from smart_import.geocoding.query import build_geocode_query

    crudo = "AV CORRIENTES 919, Argentina"
    # Como la UI: pin del depot + city del geolocalizador ("Near CABA")
    depot = depot_from_params(
        origin_lat=-34.6037, origin_lon=-58.3816,
        depot_city="CABA", depot_country="Argentina",
        max_distance_km=500.0,
    )
    sent = build_geocode_query(crudo, depot=depot)
    assert sent == "AV CORRIENTES 919, CABA, Argentina"


def test_tabla_muestra_coords_address_y_metros():
    row = {
        "address": "Av. Cabildo 3800, Argentina",
        "sent": "av cabildo 3800 argentina",
        "query": "av cabildo 3800 argentina",
        "house": "3800",
        "truth_lat": -34.57, "truth_lng": -58.45,
        "pin_lat": -34.571, "pin_lng": -58.451,
        "error_m": 140.0, "confidence": 0.91, "band": "valid",
        "precision": "housenumber", "libpostal": "skip",
    }
    tabla = format_rows_table([row])
    assert "truth_lat" in tabla and "address" in tabla and "sent" in tabla
    assert "prec" in tabla and "lp" in tabla
    assert "-34.57000" in tabla and "-58.45000" in tabla
    assert "-34.57100" in tabla and "-58.45100" in tabla
    assert "140" in tabla
    assert "Av. Cabildo 3800" in tabla
    assert "av cabildo 3800 argentina" in tabla
    assert "0.91" in tabla
    assert "hn" in tabla and "skip" in tabla
    solo_house = dump_rows([row, {**row, "house": "", "address": "Mexico, Argentina",
                                  "sent": "mexico argentina",
                                  "pin_lat": None, "error_m": None}], only="house")
    # cabecera + separador + 1 fila
    assert len(solo_house) == 3


def test_metricas_incluyen_acierto_y_confianza():
    g = Group("test")
    g.add("valid", 50.0, confidence=0.9)
    g.add("valid", 80.0, confidence=0.85)
    g.add("needs_geocoding", None, confidence=0.2)
    d = g.as_dict()
    assert d["hit_pct"] == 66.7          # 2/3 a <=100 m
    assert d["mean_confidence"] == 0.65  # (0.9+0.85+0.2)/3
    assert d["mean_confidence_pin"] == 0.875


def test_la_calidad_por_banda_se_reporta_separada():
    """Si 'valid' promete precision, tiene que poder verificarse."""
    g = Group("test")
    g.add("valid", 20.0)
    g.add("review", 900.0)
    calidad = g.as_dict()["band_quality"]
    assert calidad["valid"]["median_m"] == 20.0
    assert calidad["review"]["median_m"] == 900.0


def test_resumen_libpostal_distingue_skip_helped_noop():
    texto = format_libpostal_report({
        "mode": "on-demand",
        "enhancer_calls": 10, "enhancer_skips": 90,
        "helped": 2, "noop": 8, "skip_pct": 90.0,
        "by_reason": {"missing_road": 10},
        "helped_examples": ["Plot 42 → road='sector 18'"],
    })
    assert "skip=90 (90%)" in texto
    assert "called=10" in texto and "helped=2" in texto and "noop=8" in texto
    assert "missing_road=10" in texto
    assert "Plot 42" in texto
    off = format_libpostal_report({})
    assert "off" in off


def test_calibracion_marca_verde_lejos_como_falso_positivo():
    rows = [
        {"band": "valid", "confidence": 0.92, "precision": "housenumber",
         "error_m": 30.0, "pin_lat": -34.6},
        {"band": "valid", "confidence": 0.90, "precision": "housenumber",
         "error_m": 1800.0, "pin_lat": -34.6},
        {"band": "review", "confidence": 0.88, "precision": "street",
         "error_m": 900.0, "pin_lat": -34.6},
        {"band": "needs_geocoding", "confidence": 0.86, "precision": "",
         "error_m": None, "pin_lat": None},
    ]
    c = calibration(rows)
    assert c["valid"] == 2
    assert c["valid_overconfident"] == 1
    assert c["street_far"] == 1
    assert c["high_conf"] == 4
    assert c["high_conf_hit_100m"] == 1
    assert c["high_conf_no_pin"] == 1
    texto = format_calibration(rows)
    assert "falso positivo" in texto
    assert "centroide de calle" in texto
    lejos = dump_rows(rows, only="over")
    assert len(lejos) == 3   # cabecera + sep + 1 verde lejos


# ---------- precision real contra el indice ----------

def _caba_index():
    """PBF + indice que cubre CABA, o skip. Mira ROUTE_OPTIMIZER_DATA / PBF_DIR."""
    import os

    from smart_import.addresses.libpostal_parser import is_installed
    from smart_import.config import Config
    from smart_import.geocoding.accuracy import data_bbox
    from smart_import.geocoding.osm_index import index_path_for
    from smart_import.geocoding.pbf_registry import PbfRegistry

    cfg = Config.from_env()
    if is_installed():
        cfg = cfg.replace(libpostal_enabled=True)
    root = cfg.pbf_dir or os.getenv("ROUTE_OPTIMIZER_DATA") or ""
    if not root:
        pytest.skip("sin ROUTE_OPTIMIZER_DATA ni SMART_IMPORT_PBF_DIR")
    filas, columnas = load_truth(TRUTH / "caba_ml_13.csv")
    bbox = data_bbox(filas, columnas)
    centro = ((bbox[0] + bbox[1]) / 2, (bbox[2] + bbox[3]) / 2)
    entrada = PbfRegistry.scan(root).resolve(lat=centro[0], lon=centro[1], bbox=bbox)
    if entrada is None:
        pytest.skip(f"ningun PBF cubre CABA en {root} (usa la raiz data/, no solo _extracts)")
    from smart_import.geocoding.osm_index import prefer_index
    index = prefer_index(
        index_path_for(entrada, cfg.index_dir), cfg.index_dir,
        centro[0], centro[1], bbox,
    )
    if not index.exists():
        pytest.skip(
            f"falta el indice {index.name}; "
            "corre geocode-accuracy una vez o build-geocoder-index"
        )
    return cfg, index, centro


def _miami_index():
    """PBF + indice Florida (Miami), o skip."""
    import os

    from smart_import.config import Config
    from smart_import.geocoding.osm_index import index_path_for
    from smart_import.geocoding.pbf_registry import PbfRegistry

    cfg = Config.from_env()
    from smart_import.addresses.libpostal_parser import is_installed
    if is_installed():
        cfg = cfg.replace(libpostal_enabled=True)
    root = cfg.pbf_dir or os.getenv("ROUTE_OPTIMIZER_DATA") or ""
    if not root:
        pytest.skip("sin ROUTE_OPTIMIZER_DATA ni SMART_IMPORT_PBF_DIR")
    miami = (25.77427, -80.19366)
    entrada = PbfRegistry.scan(root).resolve(lat=miami[0], lon=miami[1])
    if entrada is None:
        pytest.skip(f"ningun PBF cubre Miami en {root}")
    from smart_import.geocoding.osm_index import prefer_index
    index = prefer_index(
        index_path_for(entrada, cfg.index_dir), cfg.index_dir,
        miami[0], miami[1],
    )
    if not index.exists():
        pytest.skip(
            f"falta el indice {index.name}; "
            "corre geocode-accuracy --truth examples/geocode-truth/miami_whatsapp_6.json"
        )
    return cfg, index, miami


def _tandil_index():
    """PBF + indice que cubre Tandil, o skip. El extract de CABA no alcanza."""
    import os

    from smart_import.addresses.libpostal_parser import is_installed
    from smart_import.config import Config
    from smart_import.geocoding.accuracy import data_bbox
    from smart_import.geocoding.osm_index import index_path_for, prefer_index
    from smart_import.geocoding.pbf_registry import PbfRegistry

    cfg = Config.from_env()
    if is_installed():
        cfg = cfg.replace(libpostal_enabled=True)
    root = cfg.pbf_dir or os.getenv("ROUTE_OPTIMIZER_DATA") or ""
    if not root:
        pytest.skip("sin ROUTE_OPTIMIZER_DATA ni SMART_IMPORT_PBF_DIR")
    filas, columnas = load_truth(TRUTH / "tandil_stops_326.json")
    bbox = data_bbox(filas, columnas)
    centro = ((bbox[0] + bbox[1]) / 2, (bbox[2] + bbox[3]) / 2)
    entrada = PbfRegistry.scan(root).resolve(lat=centro[0], lon=centro[1], bbox=bbox)
    if entrada is None:
        pytest.skip(f"ningun PBF cubre Tandil en {root}")
    index = prefer_index(
        index_path_for(entrada, cfg.index_dir), cfg.index_dir,
        centro[0], centro[1], bbox,
    )
    if not index.exists():
        pytest.skip(
            f"falta el indice {index.name}; "
            "corre geocode-accuracy --truth examples/geocode-truth/tandil_stops_326.json"
        )
    return cfg, index, centro


def _caba_web_depot(cfg, centro):
    """Mismos query params que POST /imports/{id}/geocode desde la UI.

    origin_* = pin del depot; depot_city = geolocalizador ('Near CABA').
    """
    from smart_import.geocoding.depot_context import depot_from_params

    return depot_from_params(
        origin_lat=centro[0], origin_lon=centro[1],
        depot_city="CABA", depot_country="Argentina",
        max_distance_km=float(cfg.max_geocode_distance_km),
    )


def _truth_limit() -> int | None:
    import os
    raw = os.getenv("SMART_IMPORT_GEOCODE_TRUTH_LIMIT", "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


@pytest.mark.real_geo
def test_precision_sobre_direcciones_limpias():
    """CSV MercadoLibre (12 con altura). Si mejora, subir estos numeros."""
    from smart_import.geocoding.accuracy import run

    cfg, index, centro = _caba_index()
    depot = _caba_web_depot(cfg, centro)
    reporte = run(TRUTH / "caba_ml_13.csv", index, origin=centro, config=cfg,
                  depot=depot)
    d = reporte.as_dict()["with_house_number"]
    assert d["total"] == 12
    assert d["pin_pct"] >= 80
    assert d["median_m"] <= 700, f"la precision empeoro: {d['median_m']} m"

    from smart_import.geocoding.accuracy import format_report, format_rows_table
    print("\n" + format_rows_table(reporte.rows))
    print("\n" + format_report(reporte))


@pytest.mark.real_geo
def test_recall_paradas_caba_2907():
    """2907 paradas reales vs pin del geocoder. El % es sobre el total, no sobre pines.

    Por defecto corre las 2907 (~1 min). Para un smoke: SMART_IMPORT_GEOCODE_TRUTH_LIMIT=200
    2026-09-06 noche (con altura): ~95% pin, ~77% <=100m, mediana ~45 m.
    Antes (mismo dia): ~14% pin / 9% acierto — CABA no puntuaba y faltaba la altura
    cercana. No bajar umbrales GEOCODE_*.
    """
    from smart_import.geocoding.accuracy import run

    cfg, index, centro = _caba_index()
    depot = _caba_web_depot(cfg, centro)
    reporte = run(
        TRUTH / "caba_stops_2907.json", index, origin=centro,
        config=cfg, limit=_truth_limit(), depot=depot,
    )
    assert "CABA" in reporte.depot_tokens or "Buenos Aires" in reporte.depot_tokens
    # query interna: país como último segmento (no 'Argentina, CABA').
    # No usar sent.index('Argentina'): 'Patricias Argentinas' contiene el substring.
    for row in reporte.rows:
        sent = row.get("sent") or ""
        city_i = _segment_pos(sent, "CABA")
        country_i = _segment_pos(sent, "Argentina")
        if city_i is not None and country_i is not None:
            assert city_i < country_i, sent
            assert not sent.rstrip().endswith("CABA"), sent
    d = reporte.as_dict()
    con = d["with_house_number"]
    assert d["overall"]["total"] >= 500
    # Piso holgado: no fallar por ruido de OSM, si fallar si se rompio el recall.
    assert con["pin_pct"] >= 70, f"recall con altura cayo a {con['pin_pct']}%"
    assert con["hit_pct"] >= 50, f"acierto <=100m cayo a {con['hit_pct']}%"
    calidad = reporte.with_number.as_dict()["band_quality"]
    if "valid" in calidad and "review" in calidad:
        assert calidad["valid"]["median_m"] < calidad["review"]["median_m"]

    from smart_import.geocoding.accuracy import format_report, format_rows_table
    # pytest -s para ver la tabla en la terminal
    print("\n" + format_rows_table(reporte.rows, only="house"))
    print("\n" + format_report(reporte))


@pytest.mark.real_geo
def test_precision_orders_tiendanube_caba_13():
    """13 orders CABA (calle + nro separados). 10 tienen lat/lng; 3 no se miden."""
    from smart_import.geocoding.accuracy import run

    cfg, index, centro = _caba_index()
    depot = _caba_web_depot(cfg, centro)
    reporte = run(TRUTH / "caba_orders_13.json", index, origin=centro,
                  config=cfg, depot=depot)
    d = reporte.as_dict()
    assert reporte.rows_read == 13
    assert d["overall"]["total"] == 10
    con = d["with_house_number"]
    assert con["total"] == 10
    # Piso holgado: input limpio (calle+altura+CABA). No bajar umbrales GEOCODE_*.
    assert con["pin_pct"] >= 50, f"pin con altura cayo a {con['pin_pct']}%"

    from smart_import.geocoding.accuracy import format_report, format_rows_table
    print("\n" + format_rows_table(reporte.rows))
    print("\n" + format_report(reporte))


@pytest.mark.real_geo
def test_precision_paradas_tandil_326():
    """326 paradas Tandil con altura. Mide si OSM de interior rinde mejor que CABA sin nro."""
    from smart_import.geocoding.accuracy import run
    from smart_import.geocoding.depot_context import depot_from_params

    cfg, index, centro = _tandil_index()
    depot = depot_from_params(
        origin_lat=-37.3211784, origin_lon=-59.1343635,
        depot_city="Tandil", depot_country="Argentina",
        max_distance_km=float(cfg.max_geocode_distance_km),
    )
    reporte = run(
        TRUTH / "tandil_stops_326.json", index, origin=(-37.3211784, -59.1343635),
        config=cfg, limit=_truth_limit(), depot=depot,
    )
    d = reporte.as_dict()
    con = d["with_house_number"]
    assert con["total"] == d["overall"]["total"]
    assert con["total"] >= 50
    # Piso holgado: primera medicion. No bajar umbrales GEOCODE_*.
    assert con["pin_pct"] >= 40, f"pin Tandil cayo a {con['pin_pct']}%"
    for row in reporte.rows:
        sent = row.get("sent") or ""
        city_i = _segment_pos(sent, "Tandil")
        country_i = _segment_pos(sent, "Argentina")
        if city_i is not None and country_i is not None:
            assert city_i < country_i, sent

    from smart_import.geocoding.accuracy import format_report, format_rows_table
    print("\n" + format_rows_table(reporte.rows, only="house"))
    print("\n" + format_report(reporte))


@pytest.mark.real_geo
def test_precision_paste_miami_whatsapp_6():
    """Demo web: 6 dirs Miami. Origin y city son Miami; CABA no entra."""
    from smart_import.geocoding.accuracy import run
    from smart_import.geocoding.depot_context import depot_from_params

    cfg, index, miami = _miami_index()
    depot = depot_from_params(
        origin_lat=miami[0], origin_lon=miami[1],
        depot_city="Miami", depot_country="United States",
        max_distance_km=float(cfg.max_geocode_distance_km),
    )
    reporte = run(TRUTH / "miami_whatsapp_6.json", index, origin=miami,
                  config=cfg, depot=depot)
    d = reporte.as_dict()
    assert reporte.rows_read == 6
    assert d["overall"]["total"] == 6
    con = d["with_house_number"]
    assert con["total"] == 6
    assert con["pin_pct"] == 100, f"el placeholder Miami tiene que pinnear las 6: {con['pin_pct']}%"
    assert con["hit_pct"] >= 80, f"acierto Miami cayo a {con['hit_pct']}%"
    for row in reporte.rows:
        sent = row.get("sent") or ""
        assert "Argentina" not in sent, sent
        assert sent.endswith("United States") or "Miami" in sent
        if "1st Ave" in (row.get("address") or ""):
            assert "71st" not in (row.get("osm") or ""), row


@pytest.mark.real_geo
@pytest.mark.parametrize("archivo,minimo", [
    ("tiendanube_ba_12.json", 12),
    ("mercadolibre_ba_12.json", 12),
    ("vepathos_orders_caba_12.json", 12),
    ("shopify_caba_12.json", 12),
])
def test_precision_exports_caba_con_coords(archivo, minimo):
    """Corpus nuevos (solo coords válidas). Piso holgado: no bajar GEOCODE_*."""
    from smart_import.geocoding.accuracy import run

    cfg, index, centro = _caba_index()
    depot = _caba_web_depot(cfg, centro)
    reporte = run(TRUTH / archivo, index, origin=centro, config=cfg, depot=depot)
    d = reporte.as_dict()
    assert d["overall"]["total"] == minimo
    con = d["with_house_number"]
    assert con["total"] == minimo
    assert con["pin_pct"] >= 40, f"{archivo}: pin cayo a {con['pin_pct']}%"
    for row in reporte.rows:
        sent = row.get("sent") or ""
        city_i = _segment_pos(sent, "CABA")
        country_i = _segment_pos(sent, "Argentina")
        if city_i is not None and country_i is not None:
            assert city_i < country_i, sent

    from smart_import.geocoding.accuracy import format_report, format_rows_table
    print("\n" + format_rows_table(reporte.rows))
    print("\n" + format_report(reporte))
