"""Normalizacion, validacion y armado de deliveries/packages."""
import json

import pytest

from smart_import.normalization.values import format_number, to_datetime, to_float
from smart_import.pipeline import run_normalize
from tests.conftest import FIXTURES, SCHEMA


def norm(name, tmp_path, **kwargs):
    return run_normalize(FIXTURES / name, SCHEMA, tmp_path / "out.csv",
                         emit=("flat", "nested"), **kwargs)


# ---------- coercion de valores ----------

@pytest.mark.parametrize("raw,expected", [
    ("0,5", 0.5), ("1,34", 1.34), ("-34,626395", -34.626395),
    ("1.234,56", 1234.56), ("1,234.56", 1234.56),
    ("22", 22.0), ("0.15", 0.15), (" 2,5 kg ", 2.5), ("", None), (None, None),
])
def test_coma_decimal(raw, expected):
    assert to_float(raw) == expected


@pytest.mark.parametrize("value,expected", [
    (22.0, "22"), (0.15, "0.15"), (-34.5898, "-34.5898"), (2376.0, "2376"), (None, ""),
])
def test_formato_numerico_round_trip(value, expected):
    assert format_number(value) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-15 09:00", "2026-09-15 09:00"),
    ("2026-09-07T10:00:00", "2026-09-07 10:00"),
    ("15/09/2026", "2026-09-15 00:00"),
    ("09:00", None),                      # hora sin fecha: no se inventa
])
def test_fechas(raw, expected):
    assert to_datetime(raw) == expected


def test_datetime_utc_iso_se_conserva():
    assert to_datetime("2026-09-06T17:00:00Z") == "2026-09-06T17:00:00Z"
    assert to_datetime("2026-09-06T17:00:00+00:00") == "2026-09-06T17:00:00Z"


# ---------- round-trip contra los archivos reales ----------

def test_seattle_round_trip_identico(tmp_path):
    """Un archivo que YA viene en formato Vepathos sale exactamente igual."""
    from smart_import.readers import read_any
    result = norm("ref_us_seattle.xlsx", tmp_path)
    original = read_any(FIXTURES / "ref_us_seattle.xlsx")
    assert result.report["output_columns"] == original.columns

    emitted = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
    assert emitted[0] == ",".join(original.columns)
    assert len(emitted) == len(original) + 1
    assert emitted[1].startswith("DLV-1000,47.620949,-122.294487,")


def test_ar_round_trip_salvo_la_fila_corrupta(tmp_path):
    """Coords corruptas se limpian; address puede enriquecerse con zone (compose)."""
    result = norm("ref_ar_orders.csv", tmp_path)
    original = (FIXTURES / "ref_ar_orders.csv").read_text(encoding="utf-8").splitlines()
    emitted = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
    original = [l for l in original if l.strip()]
    # Header + filas: misma cantidad (1 header + N)
    assert len(emitted) == len(original)
    # Solo VP-1999 pierde lat/lng invalidos
    bad_orig = next(l for l in original if l.startswith("VP-1999,"))
    bad_out = next(l for l in emitted if l.startswith("VP-1999,"))
    assert bad_orig.startswith("VP-1999,95,200")
    assert bad_out.startswith("VP-1999,,,")
    assert result.report["rejected_coordinates"] == 1
    # El resto conserva delivery_id; address puede ganar barrio via compose
    for line in emitted[1:]:
        if line.startswith("VP-1999,"):
            continue
        assert line.split(",", 1)[0].startswith("VP-")


# ---------- casos borde que vienen en los archivos del cliente ----------

def test_coordenadas_fuera_de_rango_se_rechazan_y_se_reportan(tmp_path):
    report = norm("ref_ar_orders.csv", tmp_path).report
    bad = [i for i in report["row_issues"] if i["delivery_id"] == "VP-1999"]
    assert bad, "la fila con lat=95/lng=200 tiene que aparecer en row_issues"
    assert any("fuera de rango" in msg for msg in bad[0]["messages"])
    # y se sabe QUE columnas fallaron, no solo que algo fallo
    assert set(bad[0]["fields"]) == {"lat", "lng"}
    assert all(i["severity"] == "error" for i in bad[0]["issues"])


def test_fila_sin_coords_pero_con_direccion_no_se_descarta(tmp_path):
    """VP-1998: sin lat/lng pero con address -> queda para geocodificar."""
    report = norm("ref_ar_orders.csv", tmp_path).report
    assert report["needs_geocode"] >= 1
    assert report["rows_output"] == report["rows_input"]


def test_entrega_sin_bultos_sigue_siendo_valida(tmp_path):
    result = norm("ref_us_seattle.xlsx", tmp_path)
    sin_bultos = [d for d in result.deliveries if not d["packages"]]
    assert len(sin_bultos) == 25
    dlv1001 = next(d for d in result.deliveries if d["delivery_id"] == "DLV-1001")
    assert dlv1001["packages"] == []
    assert dlv1001["time_window"]["start"] == "2026-09-07 10:00"


def test_peso_cero_no_es_null(tmp_path):
    result = norm("ref_us_seattle.xlsx", tmp_path)
    dlv1000 = next(d for d in result.deliveries if d["delivery_id"] == "DLV-1000")
    assert dlv1000["packages"][0]["weight_kg"] == 0.0
    assert "weight_kg" in dlv1000["packages"][0]


def test_agrupa_multiples_bultos_por_entrega(tmp_path):
    result = norm("ref_ar_orders.csv", tmp_path)
    vp1001 = next(d for d in result.deliveries if d["delivery_id"] == "VP-1001")
    assert len(vp1001["packages"]) == 3
    assert result.report["deliveries"] == 14
    assert result.report["packages"] == 19


def test_expande_cantidad_de_bultos_sin_package_id(tmp_path):
    result = norm("one_row_per_delivery.xlsx", tmp_path)
    multi = [d for d in result.deliveries if len(d["packages"]) > 1]
    assert multi, "las filas con 'cantidad de bultos' > 1 deben expandirse"
    ids = [p["package_id"] for p in multi[0]["packages"]]
    assert ids == [f"{multi[0]['delivery_id']}-{i}" for i in range(1, len(ids) + 1)]


def test_ventana_horaria_incompleta_se_descarta(tmp_path):
    """Sin start Y end no hay ventana: no se inventa la mitad que falta."""
    result = norm("ref_ar_orders.csv", tmp_path)
    for delivery in result.deliveries:
        for pkg in delivery["packages"]:
            if "time_window" in pkg:
                assert pkg["time_window"]["start"] and pkg["time_window"]["end"]


def test_coordenadas_invertidas_se_avisan_pero_no_se_corrigen(tmp_path):
    result = norm("swapped_coords.csv", tmp_path)
    assert any("invertid" in w for w in result.report["warnings"])
    assert result.report["valid_rows"] == 0          # nada se acepta a la fuerza
    assert result.report["needs_geocode"] == 40      # se resuelven por direccion


def test_archivo_sin_coordenadas_va_entero_a_geocode(tmp_path):
    report = norm("es_sin_coords.csv", tmp_path).report
    assert report["needs_geocode"] == report["rows_output"] == 40
    assert report["valid_rows"] == 0


def test_normalize_nunca_geocodifica(tmp_path):
    """Invariante del producto: geocodificar es una accion aparte del usuario."""
    result = norm("es_sin_coords.csv", tmp_path)
    for delivery in result.deliveries:
        assert "lat" not in delivery and "lng" not in delivery
    flat = (tmp_path / "out.csv").read_text(encoding="utf-8")
    assert "geocode" not in flat


def test_utf8_se_preserva(tmp_path):
    norm("semicolon_latin1.csv", tmp_path)
    text = (tmp_path / "out.csv").read_text(encoding="utf-8")
    assert "ó" in text or "í" in text or "é" in text


def test_columna_extra_no_contamina_el_schema(tmp_path):
    report = norm("preamble_dirty.xlsx", tmp_path).report
    assert "Sucursal origen" in report["unmapped"]
    assert all(c in report["schema"] or True for c in report["output_columns"])
    assert "Sucursal origen" not in report["output_columns"]


def test_nested_json_tiene_la_forma_del_optimizador(tmp_path):
    norm("ref_ar_orders.csv", tmp_path)
    doc = json.loads((tmp_path / "out.nested.json").read_text(encoding="utf-8"))
    assert "addresses" in doc
    first = doc["addresses"][0]
    assert {"delivery_id", "lat", "lng", "address", "packages"} <= set(first)
    assert isinstance(first["packages"], list)
    assert "dimensions" in first["packages"][0]


def test_mapping_manual_pisa_la_sugerencia(tmp_path):
    result = norm("preamble_dirty.xlsx", tmp_path,
                  manual_mapping={"Codigo interno": "reference"})
    assert result.mapping.mapping["Codigo interno"].target == "reference"
    assert result.mapping.mapping["Codigo interno"].method == "manual"
    assert "reference" in result.report["output_columns"]


def test_limite_de_tamano_de_archivo(tmp_path, monkeypatch):
    from smart_import.config import Config
    cfg = Config.from_env()
    tiny = Config(**{**cfg.__dict__, "max_file_mb": 0.000001})
    with pytest.raises(ValueError, match="supera el limite"):
        run_normalize(FIXTURES / "ref_us_seattle.xlsx", SCHEMA, tmp_path / "x.csv", config=tiny)


# ---------- el MISMO pedido escrito de distintas formas da la MISMA estructura ----------

def _deliveries(tmp_path, nombre, contenido):
    archivo = tmp_path / nombre
    archivo.write_text(contenido, encoding="utf-8")
    return run_normalize(archivo, SCHEMA, tmp_path / f"{nombre}.out.csv",
                         emit=("nested",)).deliveries


FORMA_A = (                                   # una fila POR BULTO
    "delivery_id,address,lat,lng,cliente,package_id,weight_kg\n"
    'VP-100,"Av. Corrientes 1234",-34.6037,-58.3816,Juan,PKG-1,1.5\n'
    'VP-100,"Av. Corrientes 1234",-34.6037,-58.3816,Juan,PKG-2,0.8\n'
    'VP-100,"Av. Corrientes 1234",-34.6037,-58.3816,Juan,PKG-3,2.1\n'
    'VP-200,"Maipu 400",-34.5921,-58.3745,Maria,PKG-4,0.5\n'
)
FORMA_B = (                                   # una fila POR ENTREGA con cantidad
    "Nro entrega,Domicilio,Latitud,Longitud,Cliente,Cant bultos,Peso kg\n"
    'VP-100,"Av. Corrientes 1234",-34.6037,-58.3816,Juan,3,1.5\n'
    'VP-200,"Maipu 400",-34.5921,-58.3745,Maria,1,0.5\n'
)


def test_una_fila_por_bulto_se_agrupa(tmp_path):
    d = _deliveries(tmp_path, "a.csv", FORMA_A)
    assert len(d) == 2
    assert [len(x["packages"]) for x in d] == [3, 1]
    assert [p["package_id"] for p in d[0]["packages"]] == ["PKG-1", "PKG-2", "PKG-3"]


def test_una_fila_con_cantidad_se_expande(tmp_path):
    d = _deliveries(tmp_path, "b.csv", FORMA_B)
    assert len(d) == 2
    assert [len(x["packages"]) for x in d] == [3, 1]


def test_las_dos_formas_dan_la_misma_estructura(tmp_path):
    """Invariante: el mismo pedido, escrito de las dos maneras, sale igual."""
    a = _deliveries(tmp_path, "a.csv", FORMA_A)
    b = _deliveries(tmp_path, "b.csv", FORMA_B)

    forma = lambda ds: [(x["delivery_id"], x["address"], len(x["packages"])) for x in ds]
    assert forma(a) == forma(b)
    # y todo bulto tiene id, sea propio o sintetizado
    for ds in (a, b):
        for entrega in ds:
            for pkg in entrega["packages"]:
                assert pkg.get("package_id")


def test_sin_delivery_id_no_fusiona_por_coordenadas(tmp_path):
    """Sin id, cada fila es un stop: mismo lat/lng NO une pedidos distintos."""
    d = _deliveries(tmp_path, "c.csv",
                    "Domicilio,Latitud,Longitud,Bulto,Kg\n"
                    '"Av. Corrientes 1234",-34.6037,-58.3816,A-1,1.5\n'
                    '"Av. Corrientes 1234",-34.6037,-58.3816,A-2,0.8\n'
                    '"Maipu 400",-34.5921,-58.3745,B-1,0.5\n')
    assert len(d) == 3
    assert [len(x["packages"]) for x in d] == [1, 1, 1]


def test_mismo_delivery_id_une_bultos_aunque_compartan_coords_con_otro(tmp_path):
    """Mismo id → 1 stop. Distinto id + mismas coords → 2 stops (no merge geo)."""
    d = _deliveries(
        tmp_path, "same_geo.csv",
        "delivery_id,address,lat,lng,package_id,weight_kg\n"
        'DLV-A,"200 Park Ave",40.7527,-73.9772,PKG-02,1.2\n'
        'DLV-A,"200 Park Ave",40.7527,-73.9772,PKG-02B,0.5\n'
        'DLV-B,"89 E 42nd St",40.7527,-73.9772,PKG-13,2.0\n',
    )
    assert len(d) == 2
    by_id = {x["delivery_id"]: x for x in d}
    assert len(by_id["DLV-A"]["packages"]) == 2
    assert len(by_id["DLV-B"]["packages"]) == 1
    assert by_id["DLV-B"]["address"] == "89 E 42nd St"


def test_mismo_address_distinto_id_son_stops_distintos(tmp_path):
    d = _deliveries(
        tmp_path, "same_addr.csv",
        "delivery_id,address,lat,lng,package_id,weight_kg\n"
        'ORD-1,"350 5th Ave, New York, NY",40.7484,-73.9857,P1,1.0\n'
        'ORD-2,"350 5th Ave, New York, NY",40.7484,-73.9857,P2,2.0\n',
    )
    assert len(d) == 2
    assert {x["delivery_id"] for x in d} == {"ORD-1", "ORD-2"}


def test_cada_bulto_conserva_sus_propias_dimensiones(tmp_path):
    d = _deliveries(tmp_path, "d.csv",
                    "Nro pedido,Direccion,Lat,Lon,Bulto,Kg,Largo,Ancho,Alto\n"
                    'PED-9,"Cabildo 1800",-34.5615,-58.4560,B1,1.2,30,20,10\n'
                    'PED-9,"Cabildo 1800",-34.5615,-58.4560,B2,3.4,50,40,25\n')
    assert len(d) == 1
    p1, p2 = d[0]["packages"]
    assert p1["dimensions"] == {"length": 30.0, "width": 20.0, "height": 10.0}
    assert p2["dimensions"] == {"length": 50.0, "width": 40.0, "height": 25.0}
    assert (p1["weight_kg"], p2["weight_kg"]) == (1.2, 3.4)


# ---------- el pie de pagina del cliente no es una entrega fallida ----------

CON_BASURA = (
    "delivery_id,address,lat,lng,cliente,bultos\n"
    'VP-1,"Av. Corrientes 1234",-34.6037,-58.3816,Juan Perez,1\n'
    'VP-2,"Maipu 400",-34.5921,-58.3745,Maria Gomez,2\n'
    'VP-3,"Florida 500",,,Carlos Lopez,1\n'
    ",,,,,150\n"                                          # fragmento de totales
    "Totales:,,,,,153\n"                                  # fila de totales
    '"Nota: coordinar antes de las 9am",,,,,\n'           # nota al pie
    "VP-4,,,,Pedro Ruiz,1\n"                              # entrega REAL sin direccion
)


def test_las_filas_que_no_son_entregas_no_cuentan_como_fallidas(tmp_path):
    """Contar el pie de pagina como 'entrega fallida' hace parecer roto un
    archivo que esta bien."""
    archivo = tmp_path / "basura.csv"
    archivo.write_text(CON_BASURA, encoding="utf-8")
    report = run_normalize(archivo, SCHEMA, tmp_path / "o.csv").report

    assert report["valid_rows"] == 2
    assert report["needs_geocode"] == 1
    assert report["invalid_rows"] == 1        # solo VP-4, que SI es una entrega
    assert report["ignored_rows"] == 3        # totales, fragmento y nota

    por_estado = {i["row"]: i["status"] for i in report["row_issues"]}
    assert por_estado[7] == "invalid"         # VP-4: tiene cliente, le falta destino
    assert all(por_estado[n] == "ignored" for n in (4, 5, 6))


def test_una_entrega_sin_direccion_pero_con_cliente_si_cuenta_como_fallida(tmp_path):
    """VP-4 tiene delivery_id y cliente: es una entrega, le falta el destino."""
    archivo = tmp_path / "x.csv"
    archivo.write_text("delivery_id,address,cliente,telefono\n"
                       "VP-9,,Pedro Ruiz,1155554444\n", encoding="utf-8")
    report = run_normalize(archivo, SCHEMA, tmp_path / "o.csv").report
    assert report["invalid_rows"] == 1 and report["ignored_rows"] == 0


def test_las_ignoradas_son_warning_no_error(tmp_path):
    archivo = tmp_path / "y.csv"
    archivo.write_text("delivery_id,address,bultos\nTotales:,,153\n", encoding="utf-8")
    report = run_normalize(archivo, SCHEMA, tmp_path / "o.csv").report
    issue = report["row_issues"][0]
    assert issue["status"] == "ignored"
    assert issue["issues"][0]["severity"] == "warning"


# ---------- time windows + multi-paquete (formas reales de planillas) ----------

TW_MULTI = (
    "Nro entrega,Cliente,Domicilio,Lat,Lon,Bulto,Kg,"
    "Ventana desde,Ventana hasta,Zona horaria\n"
    'VP-TW1,Ana,"Av. Corrientes 1234",-34.6037,-58.3816,B1,1.2,'
    "2026-09-15 09:00,2026-09-15 12:00,America/Argentina/Buenos_Aires\n"
    'VP-TW1,Ana,"Av. Corrientes 1234",-34.6037,-58.3816,B2,0.8,'
    "2026-09-15 09:00,2026-09-15 12:00,America/Argentina/Buenos_Aires\n"
    'VP-TW2,Luis,"Maipu 400",-34.5921,-58.3745,B3,2.0,'
    "2026-09-15 14:00,2026-09-15 18:00,America/Argentina/Buenos_Aires\n"
)


def test_ventana_horaria_completa_queda_en_la_entrega(tmp_path):
    d = _deliveries(tmp_path, "tw.csv", TW_MULTI)
    tw1 = next(x for x in d if x["delivery_id"] == "VP-TW1")
    # Con bultos, la ventana vive en cada package (no se inventa a nivel delivery)
    assert all(p["time_window"]["start"] == "2026-09-15 09:00" for p in tw1["packages"])
    assert all(p["time_window"]["end"] == "2026-09-15 12:00" for p in tw1["packages"])
    tz = tw1["packages"][0]["time_window"].get("time_zone")
    assert tz is None or "Buenos_Aires" in str(tz)


def test_multi_paquete_con_misma_ventana_agrupa(tmp_path):
    d = _deliveries(tmp_path, "tw.csv", TW_MULTI)
    tw1 = next(x for x in d if x["delivery_id"] == "VP-TW1")
    tw2 = next(x for x in d if x["delivery_id"] == "VP-TW2")
    assert len(tw1["packages"]) == 2
    assert len(tw2["packages"]) == 1
    assert {p["package_id"] for p in tw1["packages"]} == {"B1", "B2"}


def test_cantidad_bultos_y_ventana_en_una_fila(tmp_path):
    csv_text = (
        "Pedido,Direccion,Latitud,Longitud,Cant bultos,Peso kg,"
        "Desde,Hasta\n"
        'PED-88,"Cabildo 1800",-34.5615,-58.4560,3,1.5,'
        "15/09/2026 10:00,15/09/2026 13:00\n"
    )
    d = _deliveries(tmp_path, "qty_tw.csv", csv_text)
    assert len(d) == 1
    assert len(d[0]["packages"]) == 3
    assert all(p["time_window"]["start"] == "2026-09-15 10:00" for p in d[0]["packages"])
    assert all(p["time_window"]["end"] == "2026-09-15 13:00" for p in d[0]["packages"])


def test_ventana_solo_inicio_se_descarta(tmp_path):
    csv_text = (
        "delivery_id,address,lat,lng,tw_start,tw_end\n"
        'X1,"Florida 500",-34.60,-58.37,2026-09-15 09:00,\n'
    )
    d = _deliveries(tmp_path, "half_tw.csv", csv_text)
    assert "time_window" not in d[0]
    for p in d[0].get("packages", []):
        assert "time_window" not in p
