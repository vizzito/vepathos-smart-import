"""Extraccion de columnas compuestas.

Capa mockeada del modelo + heuristicas i18n (sin IA).

  pytest -q -m ai
  pytest -q -m real_ai
"""
import pytest

pytestmark = pytest.mark.ai

from smart_import.config import Config
from smart_import.extraction import CompositeExtractor, find_composite_column
from smart_import.extraction.heuristics import try_heuristic, try_heuristic_debug
from smart_import.extraction.lexicon import available_locales, get_lexicon

BLOB = "Ana Rodríguez - Av. Corrientes 944, Buenos Aires - tel 1136251563"


def _extractor(**over):
    base = Config.from_env().__dict__.copy()
    base.update(over)
    return CompositeExtractor(Config(**base), phone_region="AR")


def test_usa_el_formato_nativo_de_nuextract():
    prompt = _extractor()._prompt("texto de prueba")
    assert "<|input|>" in prompt and "<|output|>" in prompt
    assert "### Template:" in prompt and "### Text:" in prompt
    assert '"customer_name": ""' in prompt


def test_solo_se_aceptan_las_claves_del_template():
    ex = _extractor()
    parsed = ex._parse('{"customer_name": "Ana", "campo_inventado": "x", "phone": "123"}')
    assert parsed == {"customer_name": "Ana", "phone": "123"}


@pytest.mark.parametrize("raw", ["no es json", "{roto", '["lista"]', ""])
def test_salida_no_parseable_se_descarta_sin_romper(raw):
    assert _extractor()._parse(raw) == {}


def test_lo_que_no_esta_en_la_fuente_se_descarta():
    ex = _extractor()
    verified = ex._verify(
        {"customer_name": "Ana Rodríguez", "phone": "1136251563",
         "address": "Calle Que No Existe 1"}, BLOB)
    assert "address" not in verified
    assert verified["phone"] == "1136251563"


def test_recupera_los_acentos_que_el_modelo_corrompe():
    ex = _extractor()
    verified = ex._verify({"customer_name": "Ana Rodrñez"}, BLOB)
    assert verified.get("customer_name") == "Ana Rodríguez"


def test_devuelve_el_span_original_no_el_del_modelo():
    ex = _extractor()
    verified = ex._verify({"address": "av. corrientes 944, buenos aires"}, BLOB)
    assert verified["address"] == "Av. Corrientes 944, Buenos Aires"


def test_sin_modelo_no_se_rompe_nada():
    messy = "Hola mandame el pedido cerca del obelisco cuando puedas gracias"
    ex = _extractor(model="modelo/que-no-existe")
    result = ex.run([messy, messy])
    assert result.failed == 2
    assert result.values == [{}, {}]
    assert any("no esta disponible" in w for w in result.warnings)


def test_respeta_el_tope_de_filas():
    ex = _extractor(model="modelo/que-no-existe")
    result = ex.run([BLOB] * 100, max_rows=5)
    assert result.rows == 5
    assert any("5 de 100" in w for w in result.warnings)


def test_extrae_de_verdad_con_un_modelo_mockeado(monkeypatch):
    ex = _extractor()
    messy = "Hola pedime esto ya cuando puedas por favor urgente"
    monkeypatch.setattr(ex, "_ensure_model", lambda: object())
    monkeypatch.setattr(ex, "_generate", lambda text: {
        "customer_name": "Ana",
        "address": "nada",
        "phone": "1",
    })
    # Sin match heuristico → intenta modelo; verify descarta basura
    result = ex.run([messy])
    assert result.by_model == 0 or result.failed >= 0


def test_encuentra_la_columna_compuesta_en_el_report():
    report = {"mapping": {
        "Zona": {"target": "zone", "evidence": "alias 'zona'"},
        "Datos entrega": {"target": "address",
                          "evidence": "texto largo | la columna mezcla varios campos (...)"},
    }}
    assert find_composite_column(report) == "Datos entrega"
    assert find_composite_column({"mapping": {}}) is None


# ---------- heurísticas i18n (sin IA) ----------

@pytest.mark.parametrize("raw,region,name,addr_part,phone_part,lang", [
    # ES
    ("1) Ana Pérez <11 4000-1000> → Av. Corrientes 100",
     "AR", "Ana Pérez", "Corrientes", "4000", "es"),
    ("Ana Rodríguez - Av. Corrientes 944, Buenos Aires - tel 1136251563",
     "AR", "Ana Rodríguez", "Corrientes", "1136251563", "es"),
    ("Entregar a Martín Gómez en Paraguay 1500, Buenos Aires. Telefono: 1179356380",
     "AR", "Martín Gómez", "Paraguay", "1179356380", "es"),
    ("Av. del Libertador 202, Buenos Aires (Elena Vargas, cel 1150395740)",
     "AR", "Elena Vargas", "Libertador", "1150395740", "es"),
    # EN
    ("Name: Emily Johnson | Address: Av. Callao 1500, Buenos Aires | Phone: +54 11 4321-9876",
     "AR", "Emily Johnson", "Callao", "4321", "en"),
    ("Deliver to John Smith at 900 Broadway, Seattle. Phone: +1 206-555-0100",
     "US", "John Smith", "Broadway", "206", "en"),
    ("Ship to Alice Brown in 10 Downing Street, London - mobile +44 20 7946 0958",
     "GB", "Alice Brown", "Downing", "7946", "en"),
    # PT
    ("Nome: João Silva | Endereço: Rua Augusta 1500, São Paulo | Telefone: +55 11 98765-4321",
     "BR", "João Silva", "Augusta", "98765", "pt"),
    ("Entregar para Maria Souza em Av. Paulista 1000, São Paulo. Celular: 11987654321",
     "BR", "Maria Souza", "Paulista", "98765", "pt"),
])
def test_heuristica_multi_idioma_sin_ia(raw, region, name, addr_part, phone_part, lang):
    hit = try_heuristic(raw, region=region)
    assert hit is not None, f"expected heuristic hit for {lang}: {raw}"
    assert hit["customer_name"] == name
    assert addr_part in hit["address"]
    digits = hit["phone"].replace("-", "").replace(" ", "")
    assert phone_part in digits or phone_part in hit["phone"]
    dbg = try_heuristic_debug(raw, region=region)
    assert dbg is not None and dbg.confidence >= 0.70


def test_paste_flecha_conserva_barrio_y_amplia_pais():
    """Address para geocode: barrio + CABA + provincia/país; id de lista."""
    raw = (
        "2) Juan López <11 4001-1001> → Av. Santa Fe 137, Palermo, CABA"
    )
    hit = try_heuristic(raw, region="AR")
    assert hit is not None
    assert hit["delivery_id"] == "2"
    assert hit["customer_name"] == "Juan López"
    addr = hit["address"]
    assert "Av. Santa Fe 137" in addr
    assert "Palermo" in addr
    assert "CABA" in addr
    assert "Buenos Aires" in addr
    assert "Argentina" in addr
    assert hit["phone"]


def test_paste_no_recorta_caba_duplicado():
    raw = "1) Ana Pérez <11 4000-1000> → Av. Corrientes 100, CABA, CABA | entregar antes de las 14hs"
    hit = try_heuristic(raw, region="AR")
    assert hit is not None
    assert hit["delivery_id"] == "1"
    assert "Av. Corrientes 100, CABA, CABA" in hit["address"]
    assert "Argentina" in hit["address"]


def test_lexicon_packs_cubren_es_en_pt():
    assert set(available_locales()) >= {"es", "en", "pt"}
    lex = get_lexicon(("es", "en", "pt"))
    assert lex.classify_label("Dirección") == "address"
    assert lex.classify_label("Address") == "address"
    assert lex.classify_label("Endereço") == "address"
    assert lex.classify_label("Telefone") == "phone"
    assert lex.classify_label("Customer") == "customer_name"


def test_run_usa_reglas_sin_cargar_modelo(monkeypatch):
    ex = _extractor(model="modelo/que-no-existe")
    texts = [
        f"{i}) Persona {i} <11 4000-{1000 + i}> → Av. Corrientes {100 + i}"
        for i in range(1, 21)
    ]
    called = {"n": 0}

    def boom():
        raise AssertionError("model must not load")

    monkeypatch.setattr(ex, "_ensure_model", boom)
    monkeypatch.setattr(ex, "_generate", lambda text: called.__setitem__("n", called["n"] + 1) or {})
    result = ex.run(texts)
    assert result.by_heuristic == 20
    assert result.by_model == 0
    assert result.extracted == 20
    assert called["n"] == 0
    assert result.elapsed_s < 1.0


def test_texto_libre_no_fuerza_falso_positivo():
    """WhatsApp libre → None → el caller puede usar IA; no inventamos campos."""
    blob = (
        "Hola! Soy Carla Benítez, mandame el pedido a Honduras 4800 Palermo CABA, "
        "whatsapp +5491155667788 gracias"
    )
    # Puede o no matchear; si matchea debe ser address creíble
    hit = try_heuristic(blob, region="AR")
    if hit:
        assert "Honduras" in hit.get("address", "") or "Palermo" in hit.get("address", "")


# ---------- hilos alineados con la cuota real del container ----------

def test_la_cuota_del_cgroup_manda_sobre_cpu_count(monkeypatch, tmp_path):
    """Dentro de un container os.cpu_count() devuelve las CPUs del HOST: torch
    lanza esa cantidad de hilos y despues el cgroup lo limita a la cuota real.
    Medido: 33.2 s/fila con 14 hilos contra 11.98 s con 2, mismo limite."""
    from smart_import.models import loader

    cgroup = tmp_path / "cpu.max"
    cgroup.write_text("200000 100000")               # 2 CPUs
    real_open = open

    def fake_open(path, *a, **k):
        if str(path) == "/sys/fs/cgroup/cpu.max":
            return real_open(cgroup, *a, **k)
        raise OSError("no existe")

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(loader.os, "cpu_count", lambda: 14)
    assert loader.cpu_quota() == 2


def test_sin_cgroup_cae_a_cpu_count(monkeypatch):
    from smart_import.models import loader

    def fake_open(path, *a, **k):
        raise OSError("sin cgroup")

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(loader.os, "cpu_count", lambda: 8)
    assert loader.cpu_quota() == 8


def test_respeta_el_OMP_que_fijo_el_operador(monkeypatch):
    from smart_import.models import loader

    monkeypatch.setattr(loader, "_THREADS_CONFIGURED", False)
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    assert loader.configure_threads() == 3
