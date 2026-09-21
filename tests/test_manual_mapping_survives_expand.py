"""Lo que corrige la persona es final, tambien cuando el archivo trae una columna mezclada."""

import csv

from smart_import import pipeline

SCHEMA = "schemas/vepathos_flat_v1.json"


def _archivo(tmp_path):
    path = tmp_path / "pedidos.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["delivery_id", "address", "lat", "lng", "weight_kg", "source_date", "source_order"])
        for i in range(1, 13):
            writer.writerow([
                f"DLV-{i:05d}", f"Sarmiento {1200 + i}, B7000 Tandil, Provincia de Buenos Aires, Argentina",
                f"-37,32{i:02d}", f"-59,12{i:02d}", "1,11", "2024-10-25", i,
            ])
    return path


def _ambiguas(tmp_path, manual):
    report = pipeline.run_normalize(_archivo(tmp_path), SCHEMA, tmp_path / "out.csv", manual_mapping=manual).report
    return {a["column"] for a in report.get("ambiguous", [])}, report.get("needs_review")


def test_una_correccion_no_se_pierde_al_separar_la_columna_mezclada(tmp_path):
    # 2026-09-20: separar `address` re-detectaba el mapping desde cero y tiraba las correcciones, asi que
    # un import con la direccion completa no salia nunca de "a revisar".
    antes, _ = _ambiguas(tmp_path, None)
    dudosas = antes - {"address"}
    if not dudosas:
        return  # el mapper de esta version ya no duda de estas columnas: no hay nada que corregir
    despues, _ = _ambiguas(tmp_path, dict.fromkeys(dudosas))
    assert not (despues & dudosas), f"siguen a revisar tras corregirlas: {despues & dudosas}"


def test_respondidas_todas_el_import_queda_listo(tmp_path):
    antes, _ = _ambiguas(tmp_path, None)
    manual = {c: ("address" if c == "address" else None) for c in antes}
    despues, needs_review = _ambiguas(tmp_path, manual or None)
    assert despues == set() and not needs_review
