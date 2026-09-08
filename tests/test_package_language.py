"""La capa de paqueteria: vocabulario, typos, cada-uno vs total, y anti-falsos.

El corpus de abajo es el contrato. Cada caso es una frase que un despachante
escribio de verdad en un WhatsApp o en una celda de Excel.
"""
import pytest

from smart_import.extraction.canvas import TextCanvas
from smart_import.extraction.packages import extract_packages, has_package_signal
from smart_import.packages import parse_packages, skeleton
from smart_import.packages.lexicon import get_package_lexicon


def parse(text):
    return parse_packages(text)


# ---------- (1) vocabulario: el sustantivo sale del catalogo ----------

@pytest.mark.parametrize("text, quantity, packaging", [
    ("2 paquetes", 2, "parcel"),
    ("3 cajas", 3, "box"),
    ("1 sobre", 1, "envelope"),
    ("2 bolsas", 2, "bag"),
    ("5 pallets", 5, "pallet"),
    ("12 unidades", 12, "unit"),
    ("3 boxes", 3, "box"),
    ("2 envelopes", 2, "envelope"),
    ("4 caixas", 4, "box"),          # pt
    ("2 colis", 2, "parcel"),        # fr
    ("3 kisten", 3, "case"),         # de: plural que resuelve el fuzzy
])
def test_sustantivos_de_varios_idiomas(text, quantity, packaging):
    result = parse(text)
    assert result.quantity == quantity
    if packaging:
        assert result.packaging == packaging


@pytest.mark.parametrize("text, quantity", [
    ("3 pkgs", 3), ("2 pkg", 2), ("4 btos", 4), ("3 cjs", 3),
    ("2 vols", 2), ("3 pzas.", 3), ("2u", 2), ("5 uds", 5),
])
def test_abreviaturas_de_despacho(text, quantity):
    assert parse(text).quantity == quantity


def test_una_letra_suelta_no_es_un_bulto():
    """'2 u' pegado es un bulto; separado y sin punto es cualquier cosa."""
    assert parse("2u").quantity == 2
    assert parse("2 u.").quantity == 2
    assert parse("2 u").quantity is None


# ---------- (2) numeros escritos con letras ----------

@pytest.mark.parametrize("text, quantity", [
    ("un paquete", 1), ("dos cajas", 2), ("tres bultos", 3),
    ("cinco sobres", 5), ("doce cajas", 12),
    ("two boxes", 2), ("three parcels", 3), ("ten bags", 10),
    ("duas caixas", 2), ("deux colis", 2),
    ("una docena de cajas", 12), ("media docena de botellas", 6),
    ("2 docenas de sobres", 24), ("un par de cajas", 2),
])
def test_numeros_en_letras(text, quantity):
    assert parse(text).quantity == quantity


def test_un_kilo_es_un_peso_no_un_bulto():
    result = parse("3 paquetes de un kilo")
    assert result.quantity == 3
    assert result.weight_per_unit_kg == 1.0
    assert result.weight_total_kg == 3.0


# ---------- (3) typos: se corrige por esqueleto fonetico ----------

def test_esqueleto_colapsa_la_ortografia():
    assert skeleton("paquetes") == skeleton("paketes")
    assert skeleton("cajas") == skeleton("kajas")
    assert skeleton("bultoss") == skeleton("bultos")


@pytest.mark.parametrize("text, quantity, packaging", [
    ("4 paketed", 4, "parcel"),
    ("2 paqetes", 2, "parcel"),
    ("3 kajas", 3, "box"),
    ("2 bultoss", 2, "package"),
])
def test_typos_se_leen_igual(text, quantity, packaging):
    result = parse(text)
    assert (result.quantity, result.packaging) == (quantity, packaging)
    assert result.confidence < 0.9, "un typo no puede tener la misma confianza"


def test_el_fuzzy_no_se_come_una_calle():
    """Palabras que se parecen de lejos a un bulto pero son toponimos."""
    for text in ("Av. Rivadavia 211 Caballito CABA",
                 "11 de Septiembre Nro 1913",
                 "Av. del Libertador 887, Buenos Aires",
                 "23 MG Road, Bengaluru 560001",
                 "Ruta 8 km 45, Pilar",
                 "unidad funcional 3, Callao 1123"):
        assert parse(text).is_empty, text


# ---------- (4) unidades: todo se guarda en kg / cm / cm3 ----------

@pytest.mark.parametrize("text, kg", [
    ("2.5kg", 2.5), ("2,5 kg", 2.5), ("800 g", 0.8), ("500grs", 0.5),
    ("10 lb", 4.536), ("3 pounds", 1.361), ("2 libras", 0.907),
    ("1 tonelada", 1000.0), ("4 kilos", 4.0), ("2 k", 2.0),
])
def test_unidades_de_peso(text, kg):
    assert parse(text).weight_total_kg == pytest.approx(kg, abs=0.001)


def test_medidas_y_volumen():
    result = parse("medidas 50 x 40 x 30 cm")
    assert (result.length_cm, result.width_cm, result.height_cm) == (50.0, 40.0, 30.0)
    assert parse("5 pallets 0.8 m3").volume_cm3 == 800000.0


def test_peso_etiquetado_sin_unidad_se_asume_kg():
    result = parse("peso 3, 2 bultos")
    assert result.weight_total_kg == 3.0
    assert result.quantity == 2


# ---------- (5) cada uno vs total: la parte que cambia el numero ----------

def test_cada_uno_explicito_multiplica():
    result = parse("4 paquetes de 4 kilos cada uno")
    assert result.quantity == 4
    assert result.weight_per_unit_kg == 4.0
    assert result.weight_total_kg == 16.0
    assert result.weight_declared_per_unit


def test_each_en_ingles_con_libras():
    result = parse("three boxes of 10 pounds each")
    assert result.quantity == 3
    assert result.weight_per_unit_kg == pytest.approx(4.536, abs=0.001)
    assert result.weight_total_kg == pytest.approx(13.608, abs=0.001)


def test_de_mas_cantidad_se_lee_por_bulto():
    """'2 cajas DE 3 kg' es como habla un despachante: 3 kg cada caja."""
    result = parse("2 cajas de 3 kg")
    assert (result.weight_per_unit_kg, result.weight_total_kg) == (3.0, 6.0)


def test_sin_pista_el_peso_sigue_siendo_total():
    """Sin 'de' ni 'cada uno' no se inventa nada: el default de siempre."""
    result = parse("2 cajas 3 kg")
    assert (result.weight_per_unit_kg, result.weight_total_kg) == (1.5, 3.0)
    assert not result.weight_declared_per_unit


def test_en_total_explicito_no_multiplica():
    result = parse("4 bultos de 10 kg en total")
    assert result.weight_total_kg == 10.0


def test_peso_por_unidad_antes_de_la_cantidad():
    assert parse("2,5 kilos c/u, 4 bultos").weight_total_kg == 10.0


# ---------- (6) varios grupos en una frase ----------

def test_grupos_distintos_suman_y_no_declaran_tipo():
    result = parse("2 sobres y 1 caja")
    assert result.quantity == 3
    assert result.packaging is None, "tipos mezclados: no se declara uno"
    assert len(result.items) == 2


def test_un_numero_explicito_apaga_los_sustantivos_sueltos():
    """'2 paquetes (caja 3kg + sobre)' son 2 bultos, no 4."""
    assert parse("2 paquetes (caja 3kg + sobre)").quantity == 2


def test_sustantivo_solo_vale_uno_si_hay_peso():
    assert parse("paquete fragil 1.2kg").quantity == 1
    assert parse("dejar el paquete en porteria").is_empty


def test_sobre_como_preposicion_no_es_un_bulto():
    assert parse("dejar 2 sobre la mesa").is_empty
    assert parse("2 sobres").quantity == 2


# ---------- (7) el adaptador al schema ----------

def test_extract_packages_llena_el_schema(ar_context):
    canvas = TextCanvas("Ana Perez, Av Corrientes 100, 4 paketed de 4 kilos cada uno")
    values = {v.field: v.value for v in extract_packages(canvas, ar_context)}
    assert values["quantity"] == 4
    assert values["weight_kg"] == 4.0, "el schema guarda el peso de UN bulto"
    assert values["packaging"] == "parcel"


def test_el_paso_saca_del_canvas_todo_lo_que_leyo(ar_context):
    canvas = TextCanvas("Av Corrientes 100 CABA, 2 cajas de 3 kg cada una")
    extract_packages(canvas, ar_context)
    left = canvas.remaining_text().lower()
    assert "cajas" not in left and "kg" not in left and "cada una" not in left
    assert "corrientes 100" in left


def test_has_package_signal_sigue_siendo_conservador():
    assert has_package_signal("2 bultos 5 kg")
    assert has_package_signal("3 pkgs")
    assert not has_package_signal("Av. Corrientes 100, Palermo, CABA")
    assert not has_package_signal("")


# ---------- (8) tabular: una celda escrita a mano ----------

def _normalize_cells(schema, columns, rows):
    from smart_import.mapping.base import ColumnMapping, MappingResult
    from smart_import.normalization.row_normalizer import RowNormalizer
    from smart_import.readers.base import FileMeta, Table

    mapping = MappingResult()
    for column, target in columns.items():
        mapping.mapping[column] = ColumnMapping(column, target, 1.0, "manual", "test")
    table = Table(meta=FileMeta(path="t.csv", format="csv", size_bytes=0),
                  columns=list(columns), rows=rows)
    return RowNormalizer(schema).run(table, mapping)


COLUMNS = {"delivery_id": "delivery_id", "address": "address",
           "Bultos": "quantity", "Peso": "weight_kg", "Embalaje": "packaging"}


def test_celda_con_texto_la_lee_el_parser(schema):
    """'3 cajas de 10 lb c/u' coercionado da 310: aca tiene que dar 3."""
    outcome = _normalize_cells(schema, COLUMNS, [
        ("A1", "Av Corrientes 100", "3 cajas de 10 lb c/u", None, None),
    ])
    values = outcome.rows[0].values
    assert values["quantity"] == 3
    assert values["weight_kg"] == pytest.approx(4.536, abs=0.001), \
        "en tabular el peso de la columna es UNITARIO"
    assert values["packaging"] == "box"
    assert any("paqueteria" in w for w in outcome.warnings)


def test_celda_numerica_no_pasa_por_la_capa(schema):
    """El 99% de los archivos: numeros limpios, camino de siempre, sin avisos."""
    outcome = _normalize_cells(schema, COLUMNS, [
        ("A2", "Av Santa Fe 200", "2", "2.75", None),
    ])
    assert outcome.rows[0].values["quantity"] == 2
    assert outcome.rows[0].values["weight_kg"] == 2.75
    assert not outcome.rows[0].issues
    assert not outcome.warnings


def test_libras_en_la_celda_se_convierten(schema):
    outcome = _normalize_cells(schema, COLUMNS, [
        ("A3", "Callao 400", "4", "3 lb", None),
    ])
    assert outcome.rows[0].values["weight_kg"] == pytest.approx(1.361, abs=0.001)


def test_no_se_inventa_una_columna_que_el_archivo_no_trajo(schema):
    """Sin columna de embalaje, el tipo de bulto no aparece en la salida."""
    columns = {k: v for k, v in COLUMNS.items() if k != "Embalaje"}
    outcome = _normalize_cells(schema, columns, [
        ("A4", "Av Cabildo 300", "una docena de sobres", "800 grs"),
    ])
    assert outcome.rows[0].values["quantity"] == 12
    assert outcome.rows[0].values["weight_kg"] == 0.8
    assert "packaging" not in outcome.targets_present


# ---------- (9) el lexico sale de datos, no del codigo ----------

def test_el_vocabulario_viene_del_catalogo():
    lexicon = get_package_lexicon()
    assert len(lexicon.nouns) > 200, "el catalogo de packaging tiene que estar cargado"
    assert lexicon.nouns["caja"]["canonical"] == "box"
    assert lexicon.nouns["caja"]["code"] == "BX"
    assert lexicon.weight_units["lb"] == pytest.approx(0.45359237)
