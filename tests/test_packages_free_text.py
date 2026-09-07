"""Peel de paquetes, huérfanos sin destino, y peso total en free-text."""
from smart_import.assemble.grouping import assemble
from smart_import.extraction.canvas import TextCanvas
from smart_import.extraction.free_text import FreeTextExtractor
from smart_import.extraction.packages import extract_packages, widen_package_span
from smart_import.extraction.pipeline import FieldExtractionPipeline
from smart_import.normalization.row_normalizer import NormalizedRow


# ---------- (1) consumir labels + valores antes de address ----------

def test_widen_incluye_etiqueta_qty():
    text = "Villa 12 Al Barsha Dubai qty: 3 boxes"
    start = text.index("3 boxes")
    end = start + len("3 boxes")
    assert widen_package_span(text, start, end) == (text.index("qty"), end)


def test_etiquetas_qty_peso_dims_salen_del_canvas(ar_context):
    text = ("Omar +971501234567 Villa 12 Al Barsha Dubai qty: 3 boxes "
            "weight 10kg dimensions 50x40x30cm")
    canvas = TextCanvas(text)
    values = extract_packages(canvas, ar_context)
    for v in values:
        canvas.consume(v.span)
    left = canvas.remaining_text().lower()
    assert "qty" not in left
    assert "weight" not in left
    assert "dimensions" not in left
    assert "boxes" not in left
    assert "10kg" not in left.replace(" ", "")
    assert "villa 12" in left


def test_address_no_queda_con_qty_ni_dimensions(ar_context):
    pipe = FieldExtractionPipeline(context=ar_context)
    rec = pipe.run(
        "B4 Omar +971501234567 Villa 12 Al Barsha Dubai qty: 3 boxes")
    addr = (rec.get("address") or "").lower()
    assert "qty" not in addr
    assert rec.get("quantity") == 3

    rec2 = pipe.run(
        "H7 dimensions 60 x 40 x 30 cm weight 22 kg 2 volumes "
        "Rohan MIDC Andheri +919777788899")
    addr2 = (rec2.get("address") or "").lower()
    assert "dimensions" not in addr2
    assert "weight" not in addr2
    assert rec2.get("quantity") == 2
    assert rec2.get("weight_kg") == 22.0
    assert rec2.get("length_cm") == 60.0


# ---------- (2) no emitir entrega solo con bultos ----------

def test_solo_bultos_sin_destino_se_ignora(ar_context):
    ex = FreeTextExtractor(context=ar_context)
    doc = ex.run_document(
        "I1 solo 2 bultos 5 kg 20x30x40 cm sin destinatario\n"
        "I2 qty 3 packages weight 10kg dimensions 50x40x30cm\n"
        "I3 entregar 1 paquete 800 g mañana\n"
        "Ana Perez 1140001000 Av Corrientes 100 CABA 2 bultos 8 kg\n"
    )
    sources = " | ".join(r.source for r in doc.records)
    assert "I1" not in sources and "I2" not in sources and "I3" not in sources
    assert any("Corrientes" in (r.get("address") or "") for r in doc.records)
    assert any("bultos solos" in " ".join(i["reasons"]) for i in doc.ignored)


def test_con_telefono_solo_si_emite(ar_context):
    """Telefono cuenta como destino accionable aunque la direccion sea floja."""
    ex = FreeTextExtractor(context=ar_context)
    doc = ex.run_document("llamar al 11-4000-1000 para coordinar 2 bultos 5 kg")
    assert len(doc.records) >= 1
    assert doc.records[0].get("phone")


# ---------- (3) weight = total en free-text ----------

def test_assemble_peso_total_se_reparte_en_free_text(schema):
    row = NormalizedRow(
        index=1,
        values={
            "delivery_id": "A1",
            "address": "Av Corrientes 100",
            "quantity": 2,
            "weight_kg": 8.0,
            "length_cm": 20.0,
            "width_cm": 30.0,
            "height_cm": 40.0,
        },
        status="needs_geocoding",
    )
    deliveries, warnings = assemble([row], schema, weight_is_total=True)
    assert len(deliveries) == 1
    pkgs = deliveries[0]["packages"]
    assert len(pkgs) == 2
    assert pkgs[0]["weight_kg"] == 4.0
    assert pkgs[1]["weight_kg"] == 4.0
    assert sum(p["weight_kg"] for p in pkgs) == 8.0
    assert any("peso en texto libre" in w for w in warnings)


def test_assemble_tabular_sigue_clonando_peso_unitario(schema):
    row = NormalizedRow(
        index=1,
        values={
            "delivery_id": "VP-100",
            "address": "Av Corrientes 1234",
            "quantity": 3,
            "weight_kg": 1.5,
        },
        status="ok",
    )
    deliveries, _ = assemble([row], schema, weight_is_total=False)
    pkgs = deliveries[0]["packages"]
    assert len(pkgs) == 3
    assert all(p["weight_kg"] == 1.5 for p in pkgs)
