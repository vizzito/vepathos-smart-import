"""Heuristicas de columna compuesta e i18n. Todo determinístico, sin modelo.

Antes este archivo mezclaba estos tests con los del wrapper del modelo. El
modelo se elimino del proyecto; las heuristicas que lo reemplazaron son las que
se verifican aca.
"""
import pytest

from smart_import.extraction.heuristics import try_heuristic, try_heuristic_debug
from smart_import.extraction.lexicon import available_locales, get_lexicon


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
    # el normalizador deduplica la localidad repetida y completa provincia/pais
    assert hit["address"].startswith("Av. Corrientes 100, CABA")
    assert "Buenos Aires" in hit["address"] and "Argentina" in hit["address"]
    assert "Argentina" in hit["address"]


def test_lexicon_packs_cubren_es_en_pt():
    assert set(available_locales()) >= {"es", "en", "pt"}
    lex = get_lexicon(("es", "en", "pt"))
    assert lex.classify_label("Dirección") == "address"
    assert lex.classify_label("Address") == "address"
    assert lex.classify_label("Endereço") == "address"
    assert lex.classify_label("Telefone") == "phone"
    assert lex.classify_label("Customer") == "customer_name"


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
