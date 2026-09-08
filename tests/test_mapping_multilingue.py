"""El mapper contra headers en idiomas que el schema NO lista.

La lista de alias cubre es/en (y algo de pt). Este archivo fija que eso NO sea
la barrera: cuando el nombre de la columna no dice nada, decide el CONTENIDO,
que es el mismo en todos los idiomas.

Si un dia se agregan packs de alias, estos casos tienen que seguir pasando por
la razon vieja o por la nueva — pero pasar.
"""
import pytest

from smart_import.config import Config
from smart_import.mapping import build_mapper
from smart_import.readers.base import FileMeta, Table

NOMBRES = ["Ana Perez", "Juan Lopez", "Maria Gomez", "Carlos Ruiz", "Lucia Diaz"]
TELEFONOS = ["1160485121", "1177939607", "1192059444", "1165941137", "1119897929"]
BULTOS = ["2", "1", "3", "1", "2"]
PESOS = ["2.75", "3.98", "1.62", "0.90", "3.43"]

#: (id, headers, direcciones). Los valores no-direccion son siempre los mismos:
#: lo unico que cambia entre casos es el idioma, que es el punto.
CASOS = [
    ("es", ["Domicilio Entrega", "Cliente", "Cel", "Bultos", "Peso kg"],
     ["Av. Corrientes 100, CABA", "Callao 2862, CABA", "Lavalle 3690, CABA",
      "Maipú 1271, CABA", "Cerrito 1032, CABA"]),
    ("en", ["Delivery Address", "Recipient", "Mobile", "Packages", "Weight"],
     ["350 5th Ave, New York", "12 Main Street, Boston", "88 Oak Road, Chicago",
      "5 Pine Lane, Denver", "77 Elm Drive, Austin"]),
    ("pt", ["Endereço", "Destinatário", "Celular", "Volumes", "Peso"],
     ["Rua Augusta 1500, Sao Paulo", "Av Paulista 900, Sao Paulo",
      "Rua Oscar Freire 22, SP", "Alameda Santos 45, SP", "Rua Haddock Lobo 7, SP"]),
    ("fr", ["Adresse de livraison", "Destinataire", "Téléphone", "Colis", "Poids"],
     ["12 Rue de Rivoli, Paris", "5 Avenue Foch, Paris",
      "30 Boulevard Saint-Germain", "8 Rue Cler, Paris", "22 Rue Mouffetard, Paris"]),
    ("de", ["Lieferadresse", "Empfänger", "Telefonnummer", "Pakete", "Gewicht"],
     ["Hauptstrasse 12, Berlin", "Kastanienallee 45, Berlin", "Torstrasse 8, Berlin",
      "Bergmannstrasse 90, Berlin", "Oranienstrasse 3, Berlin"]),
    ("it", ["Indirizzo", "Destinatario", "Cellulare", "Colli", "Peso"],
     ["Via Roma 25, Milano", "Corso Buenos Aires 7, Milano", "Via Dante 44, Milano",
      "Viale Monza 12, Milano", "Via Torino 90, Milano"]),
    ("nl", ["Bezorgadres", "Ontvanger", "Telefoon", "Pakketten", "Gewicht"],
     ["Damrak 1, Amsterdam", "Prinsengracht 263, Amsterdam",
      "Kalverstraat 92, Amsterdam", "Nieuwezijds 45, Amsterdam", "Rokin 7, Amsterdam"]),
    ("pl", ["Adres dostawy", "Odbiorca", "Telefon", "Paczki", "Waga"],
     ["Krucza 5, Warszawa", "Marszalkowska 100, Warszawa", "Nowy Swiat 22, Warszawa",
      "Chmielna 8, Warszawa", "Zlota 44, Warszawa"]),
    ("headers-inutiles", ["col_1", "col_2", "col_3", "col_4", "col_5"],
     ["Av. Corrientes 100, CABA", "Callao 2862, CABA", "Lavalle 3690, CABA",
      "Maipú 1271, CABA", "Cerrito 1032, CABA"]),
]

ESPERADOS = ("address", "customer_name", "phone", "quantity", "weight_kg")


def _detect(columns, direcciones, schema):
    table = Table(meta=FileMeta(path="x.csv", format="csv"), columns=list(columns),
                  rows=list(zip(direcciones, NOMBRES, TELEFONOS, BULTOS, PESOS)))
    return build_mapper(Config.from_env()).detect(table, schema)


@pytest.mark.parametrize("caso", CASOS, ids=lambda c: c[0])
def test_los_cinco_campos_se_resuelven(caso, schema):
    _, columns, direcciones = caso
    got = {m.target for m in _detect(columns, direcciones, schema).mapping.values()}
    faltan = [t for t in ESPERADOS if t not in got]
    assert not faltan, f"sin resolver: {faltan}"


@pytest.mark.parametrize("caso", CASOS, ids=lambda c: c[0])
def test_no_se_inventan_campos_que_el_archivo_no_trae(caso, schema):
    _, columns, direcciones = caso
    got = {m.target for m in _detect(columns, direcciones, schema).mapping.values()}
    assert not (got - set(ESPERADOS)), f"mapeo a campos inexistentes: {got - set(ESPERADOS)}"


def test_un_parecido_de_string_no_le_gana_al_contenido(schema):
    """'Ontvanger' (destinatario, nl) se parece 0.82 a 'container'.

    Antes se llevaba la columna de nombres a `packaging`. Un parecido entre dos
    idiomas es la evidencia mas floja que hay.
    """
    _, columns, direcciones = next(c for c in CASOS if c[0] == "nl")
    mapping = _detect(columns, direcciones, schema).mapping
    assert mapping["Ontvanger"].target == "customer_name"


def test_volumes_en_portugues_son_bultos(schema):
    """Colision real: 'volumes' es bultos en pt y volumen cubico en en."""
    _, columns, direcciones = next(c for c in CASOS if c[0] == "pt")
    mapping = _detect(columns, direcciones, schema).mapping
    assert mapping["Volumes"].target == "quantity"


# ---------- el piso depende del nivel, no del schema entero ----------

def test_el_piso_es_una_escalera_por_nivel():
    """Equivocarse no cuesta lo mismo en el destino que en el bulto."""
    cfg = Config.from_env()
    assert cfg.mapping_floor("delivery") > cfg.mapping_floor("timewindow") \
        > cfg.mapping_floor("package")


def test_el_knob_global_sigue_moviendo_la_escalera_entera(monkeypatch):
    """`REVIEW_THRESHOLD` no deja de servir: mueve los tres pisos a la vez."""
    monkeypatch.setenv("REVIEW_THRESHOLD", "0.60")
    cfg = Config.from_env()
    assert cfg.mapping_floor("delivery") == pytest.approx(0.60)
    assert cfg.mapping_floor("package") == pytest.approx(0.50)


def test_un_nivel_se_puede_fijar_a_mano(monkeypatch):
    monkeypatch.setenv("MAPPING_MIN_PACKAGE", "0.85")
    cfg = Config.from_env()
    assert cfg.mapping_floor("package") == pytest.approx(0.85)
    assert cfg.mapping_floor("delivery") == pytest.approx(0.70), "los otros no se tocan"


def test_el_peso_se_asigna_sin_ayuda_del_header(schema):
    """0.66 no alcanzaba con un piso unico de 0.70 y la regla era codigo muerto."""
    _, columns, direcciones = next(c for c in CASOS if c[0] == "de")
    mapping = _detect(columns, direcciones, schema).mapping
    assert mapping["Gewicht"].target == "weight_kg"
    assert mapping["Gewicht"].confidence < Config.from_env().mapping_floor("delivery"), \
        "se asigna por el piso de `package`, no porque el score sea alto"


def test_subir_el_piso_de_package_lo_vuelve_a_dejar_afuera(monkeypatch, schema):
    """La evidencia no cambia; lo que cambia es cuanta se exige."""
    monkeypatch.setenv("MAPPING_MIN_PACKAGE", "0.90")
    _, columns, direcciones = next(c for c in CASOS if c[0] == "de")
    targets = {m.target for m in _detect(columns, direcciones, schema).mapping.values()}
    assert "weight_kg" not in targets


def test_una_altura_de_calle_es_entera():
    """2.75 no es una puerta. Competia con el peso y le ganaba por 0.04."""
    from smart_import.mapping.heuristics import ColumnProfile, candidates
    decimales = ["2.75", "3.98", "1.62", "0.90", "3.43"]
    enteros = ["100", "2862", "3690", "1271", "1032"]
    assert "house_number" not in {t for t, *_ in candidates(decimales, ColumnProfile(decimales))}
    assert "house_number" in {t for t, *_ in candidates(enteros, ColumnProfile(enteros))}


# ---------- las piezas, por separado ----------

def test_el_tipo_de_via_puede_ir_pegado_al_nombre():
    """Romance lo separa ('Av. Corrientes'); germanico lo pega ('Hauptstrasse')."""
    from smart_import.addresses.scoring import AddressCandidateScorer
    scorer = AddressCandidateScorer()
    for texto in ("Hauptstrasse 12, Berlin", "Kalverstraat 92, Amsterdam",
                  "Prinsengracht 263, Amsterdam"):
        assert scorer.score(texto).score >= 0.9, texto


def test_el_scorer_separa_direcciones_de_todo_lo_demas():
    """El margen entre los dos grupos es lo que permite decidir sin el header."""
    from smart_import.addresses.scoring import AddressCandidateScorer
    scorer = AddressCandidateScorer()
    direcciones = ["Av. Corrientes 100, CABA", "350 5th Ave, New York",
                   "Hauptstrasse 12, Berlin", "Rua Augusta 1500, Sao Paulo"]
    otros = ["Ana Perez", "1160485121", "Palermo", "2.75", "Portero electrico"]
    assert min(scorer.score(t).score for t in direcciones) > \
        max(scorer.score(t).score for t in otros) + 0.3


def test_la_unidad_en_la_celda_identifica_el_peso():
    """'2,75 kg' dice lo que es en cualquier idioma, sin mirar el header."""
    from smart_import.mapping.heuristics import ColumnProfile, candidates
    valores = ["2,75 kg", "3,98 kg", "1,62 kg", "0,90 kg", "3,43 kg"]
    got = {t: s for t, s, _, _ in candidates(valores, ColumnProfile(valores))}
    assert got.get("weight_kg", 0) >= 0.9


def test_una_etiqueta_adentro_de_la_celda_no_esconde_un_telefono():
    """Exports de contactos: 'Mobile: 1199887766'."""
    from smart_import.mapping.heuristics import ColumnProfile, candidates
    from smart_import.normalization.phone import normalize

    valores = [f"Mobile: {t}" for t in TELEFONOS]
    got = {t: s for t, s, _, _ in candidates(valores, ColumnProfile(valores))}
    assert got.get("phone", 0) >= 0.8
    assert normalize("Mobile: 1160485121", "AR")[0] == "+541160485121"
    assert normalize("Mobile:", "AR")[0] is None, "una etiqueta sola no es un telefono"
    # y un telefono normal no se toca
    assert normalize("(011) 4000-1000", "AR")[0] == "+541140001000"
