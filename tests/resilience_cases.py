"""Catalogo de casos de resiliencia de entrada (100+).

Cada caso es chico (pocas entregas) y declara una expectativa minima o exacta.
No versionamos los archivos: se materializan en tmp_path al correr el test.

Capas cubiertas:
  - csv / tsv / txt tabular: filas rotas, delimiters, faltantes, coords malas
  - txt free_text: prosa, viñetas, comas de lenguaje (no partir)
  - json: repair/salvage, nulls, truncados, basura
  - regression_safe: caminos que YA andaban y no deben degradarse
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


Builder = Callable[[], str]


@dataclass(frozen=True)
class ResilienceCase:
    id: str
    suffix: str                          # .csv .tsv .txt .json
    build: Builder
    #: minimo de entregas que NO deben perderse
    min_deliveries: int = 0
    #: si se setea, debe coincidir exacto (casos golden / regression_safe)
    exact_deliveries: int | None = None
    min_packages: int | None = None
    phone_region: str = "AR"
    #: "tabular" | "free_text" | None (no assert)
    expect_text_mode: str | None = None
    #: substrings que deben aparecer en addresses del nested
    must_see: tuple[str, ...] = ()
    #: substrings que NO deben inventarse / colarse
    must_not_see: tuple[str, ...] = ()
    #: notes del input (repair/salvage/texto libre)
    notes_any: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    #: el job no debe fallar (siempre True en esta bateria)
    must_succeed: bool = True


def _csv(header: str, *rows: str) -> str:
    return header + "\n" + "\n".join(rows) + "\n"


def _json_addresses(*objs: dict) -> str:
    import json
    return json.dumps({"addresses": list(objs)}, ensure_ascii=False, indent=2)


def _addr(lat, lng, address, pkgs, **extra) -> dict:
    return {"lat": lat, "lng": lng, "address": address, "packages": pkgs, **extra}


def _pkg(pid, w=1.0, **extra) -> dict:
    return {"package_id": pid, "weight_kg": w, **extra}


# ---------------------------------------------------------------------------
# Builders: CSV limpios / rotos
# ---------------------------------------------------------------------------

def _clean_csv_n(n: int, prefix: str = "C") -> Builder:
    def build() -> str:
        rows = [
            f"{prefix}-{i:03d},Cliente {i},Av. Test {i*10},CABA,"
            f"-34.60{i%10},-58.38{i%10},1"
            for i in range(1, n + 1)
        ]
        return _csv(
            "delivery_id,customer_name,address,zone,lat,lng,quantity",
            *rows,
        )
    return build


def _csv_with_poison_rows(good: int = 4) -> Builder:
    """Filas sanas intercaladas con basura que no debe tumbar el archivo."""
    def build() -> str:
        rows = [
            "delivery_id,customer_name,address,lat,lng,quantity",
            "OK-1,Ana,Av. Corrientes 100,-34.60,-58.38,1",
            ",,,,,",  # vacia
            "TOTALES,sum,,,,,,",  # footerish
            "OK-2,Juan,Av. Santa Fe 200,-34.59,-58.40,1",
            "basura sin comas suficientes",
            'OK-3,Maria,"Calle Falsa, 123",-34.58,-58.41,2',
            "OK-4,Carlos,Lavalle 500,-34.57,-58.42,1",
        ]
        # padding extras
        for i in range(5, good + 1):
            rows.append(f"OK-{i},Cli{i},Calle {i},-34.5{i},-58.3{i},1")
        return "\n".join(rows) + "\n"
    return build


def _csv_missing_coords(n: int = 3) -> Builder:
    def build() -> str:
        rows = [
            f"M-{i},Nombre {i},Av. Cabildo {100+i},,,"  # sin lat/lng
            for i in range(1, n + 1)
        ]
        return _csv("delivery_id,customer_name,address,lat,lng,quantity", *rows)
    return build


def _csv_bad_coords() -> Builder:
    return lambda: _csv(
        "delivery_id,customer_name,address,lat,lng,quantity",
        "B-1,Ana,Av. Corrientes 100,95,200,1",          # fuera de rango
        "B-2,Juan,Av. Santa Fe 200,-122.3,47.6,1",     # invertidas
        "B-3,Maria,Av. Cabildo 300,-34.60,-58.38,1",    # ok
    )


def _csv_semicolon(n: int = 3) -> Builder:
    def build() -> str:
        rows = [f"S-{i};Cliente {i};Direccion {i};-34.6;-58.4;1" for i in range(1, n + 1)]
        return "delivery_id;customer_name;address;lat;lng;quantity\n" + "\n".join(rows) + "\n"
    return build


def _csv_uneven_columns() -> Builder:
    return lambda: (
        "delivery_id,customer_name,address,lat,lng,quantity\n"
        "U-1,Ana,Av. A 1,-34.6,-58.4,1\n"
        "U-2,Juan,Av. B 2,-34.5\n"                      # faltan cols
        "U-3,Maria,Av. C 3,-34.4,-58.3,1,EXTRA,JUNK\n"  # de mas
    )


def _tsv_clean(n: int = 3) -> Builder:
    def build() -> str:
        rows = [f"T-{i}\tCli {i}\tCalle {i}\t-34.6\t-58.4\t1" for i in range(1, n + 1)]
        return "delivery_id\tcustomer_name\taddress\tlat\tlng\tquantity\n" + "\n".join(rows) + "\n"
    return build


# ---------------------------------------------------------------------------
# TXT free text
# ---------------------------------------------------------------------------

def _whatsapp_mini(n: int) -> Builder:
    """n entregas estilo WhatsApp (viñetas) — debe ir a free_text."""
    names = ["Ana Perez", "Juan Lopez", "Maria Gomez", "Carlos Ruiz",
             "Lucia Fernandez", "Martin Castro", "Santiago Herrera",
             "Facundo Molina", "Diego Martinez", "Julia Rios",
             "Nicolas Diaz", "Sofia Torres"]
    streets = [
        "Av. Corrientes 100", "Av. Santa Fe 137", "Av. Cabildo 174",
        "Av. Rivadavia 211", "Av. Cordoba 248", "Av. Pueyrredon 359",
        "Lavalle 507", "Malabia 1136", "Darwin 1395", "Olazabal 1728",
        "11 de Septiembre 1913", "Av. Elcano 2098",
    ]

    def build() -> str:
        lines = ["Hola chicos! Listado de entregas:\n"]
        for i in range(n):
            name = names[i % len(names)]
            street = streets[i % len(streets)]
            tel = f"11-40{i:02d}-{1000+i}"
            lines.append(
                f"- {name} ({tel}) entrega en {street}, CABA.\n"
            )
        lines.append("\nSaludos,\nDespacho\n")
        return "".join(lines)
    return build


def _prose_with_commas() -> Builder:
    """Comas de lenguaje: NO debe tabularse por ','."""
    return lambda: (
        "Hola equipo, dejo los envios de hoy, gracias!\n\n"
        "- Ana Perez, tel 11-4000-1000, vive en Av. Corrientes 100, CABA.\n"
        "- Juan Lopez (1140011001) en Palermo, Av. Santa Fe 200, por favor.\n"
        "- Maria Gomez, Av. Cabildo 300 Belgrano, cel 11-4002-1002.\n\n"
        "Saludos,\nDespacho\n"
    )


def _numbered_list(n: int = 4) -> Builder:
    def build() -> str:
        lines = []
        for i in range(1, n + 1):
            lines.append(
                f"{i}) Cliente {i} <11-400{i}-100{i}> → Av. Test {i*10}, CABA"
            )
        return "\n".join(lines) + "\n"
    return build


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def _json_clean(n: int) -> Builder:
    def build() -> str:
        addrs = [
            _addr(40.7 + i * 0.001, -74.0 - i * 0.001,
                  f"{100+i} Test Ave, New York, NY",
                  [_pkg(f"P-{i}", 1.0 + i * 0.1)],
                  zone="NYC")
            for i in range(n)
        ]
        return _json_addresses(*addrs)
    return build


def _json_trailing_dots() -> Builder:
    # texto crudo (no json.dumps) para meter -71.
    return lambda: '''{
  "addresses": [
    {"lat": 42.44, "lng": -71.15, "address": "Ok 1 St", "packages": [{"package_id": "A", "weight_kg": 1}]},
    {"lat": 42., "lng": -71., "address": "Truncated St", "packages": [{"package_id": "B", "weight_kg": 2}]},
    {"lat": 42.45, "lng": -71.16, "address": "Ok 2 St", "packages": [{"package_id": "C", "weight_kg": 1}]}
  ]
}
'''


def _json_missing_dim_brace() -> Builder:
    return lambda: '''{
  "addresses": [
    {"lat": 42.44, "lng": -71.15, "address": "Warren St", "packages": [
      {"package_id": "W1", "weight_kg": 1.1, "dimensions": {"length": 10, "width": 5, "height": 2},
       "packaging": "BOX", "value_cents": 0, "value_currency": "USD"},
      {"package_id": "W2", "weight_kg": 2.2, "dimensions": {"length": 11, "width": 6, "height": 3
      "packaging": "BOX", "value_cents": 0, "value_currency": "USD"}
    ]},
    {"lat": 42.45, "lng": -71.16, "address": "After Warren", "packages": [{"package_id": "W3", "weight_kg": 1}]}
  ]
}
'''


def _json_garbage_coords() -> Builder:
    return lambda: '''{
  "addresses": [
    {"lat": 42.44, "lng": -71.15, "address": "Good St", "packages": [{"package_id": "G1", "weight_kg": 1}]},
    {"lat": 42.oppeep, "lng": -pepe.15, "address": "Bad Coords St", "packages": [{"package_id": "G2", "weight_kg": 1}]},
    {"lat": 42.46, "lng": -71.17, "address": "Good 2 St", "packages": [{"package_id": "G3", "weight_kg": 1}]}
  ]
}
'''


def _json_fragment_and_good() -> Builder:
    return lambda: '''{
  "addresses": [
    {"lat": 42.44, "lng": -71.15, "address": "Before Frag", "packages": [{"package_id": "F1", "weight_kg": 1}]},
    {ambridge St, Boston", "zone": "G-2", "packages": [{"package_id": "FX", "weight_kg": 1}]},
    {"lat": 42.45, "lng": -71.16, "address": "After Frag", "packages": [{"package_id": "F2", "weight_kg": 1}]},
    {cester Ave", "zone": "G-2", "packages": [{"package_id": "FY", "weight_kg": 1}]},
    {"lat": 42.46, "lng": -71.17, "address": "Last Good", "packages": [{"package_id": "F3", "weight_kg": 1}]}
  ]
}
'''


def _json_only_packages_no_dest() -> Builder:
    return lambda: '''{
  "addresses": [
    {"lat": 42.44, "lng": -71.15, "address": "Has Dest", "packages": [{"package_id": "D1", "weight_kg": 1}]},
    {"packages": [{"package_id": "D2", "weight_kg": 1}, {"package_id": "D3", "weight_kg": 2}]}
  ]
}
'''


def _json_null_tw_multi_pkg() -> Builder:
    return lambda: _json_addresses(
        _addr(42.44, -71.15, "Harrison Ave", [
            _pkg("P1", 3.2, time_window=None, packaging="BOX",
                 value_cents=0, value_currency="USD", status="PENDING"),
            _pkg("P2", 0.7, time_window=None, packaging="BOX",
                 value_cents=0, value_currency="USD", status="PENDING"),
        ], zone="G-3"),
        _addr(42.45, -71.16, "Albany St", [
            _pkg("P3", 1.0, time_window={
                "start": "2018-07-24 13:00",
                "end": "2018-07-24 21:00",
                "time_zone": "UTC",
            }, packaging="BOX", value_cents=0, value_currency="USD"),
        ], zone="G-2"),
    )


def _json_same_coords_diff_id() -> Builder:
    return lambda: _json_addresses(
        _addr(40.7527, -73.9772, "200 Park Ave", [_pkg("A", 1.2)],
              delivery_id="DLV-A", zone="NYC"),
        _addr(40.7527, -73.9772, "89 E 42nd St", [_pkg("B", 2.0)],
              delivery_id="DLV-B", zone="NYC"),
    )


def _json_trailing_comma() -> Builder:
    return lambda: '''{
  "addresses": [
    {"lat": 1.0, "lng": 2.0, "address": "A St", "packages": [{"package_id": "1"}],},
  ],
}
'''


def _json_truncated_end() -> Builder:
    return lambda: '''{
  "addresses": [
    {"lat": 42.44, "lng": -71.15, "address": "Complete St", "packages": [{"package_id": "C1", "weight_kg": 1}]},
    {"lat": 42.45, "lng": -71.16, "address": "Also Complete", "packages": [{"package_id": "C2", "weight_kg": 1}]},
    {"lat": 42.46, "lng": -71.17, "address": "Truncated St", "packages": [
'''


# ---------------------------------------------------------------------------
# Assemble catalog (100+)
# ---------------------------------------------------------------------------

def build_catalog() -> list[ResilienceCase]:
    cases: list[ResilienceCase] = []

    # --- regression_safe: caminos que ya andaban ---
    for n in (1, 2, 3, 5, 8):
        cases.append(ResilienceCase(
            id=f"csv_clean_{n}",
            suffix=".csv",
            build=_clean_csv_n(n),
            exact_deliveries=n,
            tags=("regression_safe", "csv", "clean"),
        ))
    for n in (2, 3, 5):
        cases.append(ResilienceCase(
            id=f"tsv_clean_{n}",
            suffix=".tsv",
            build=_tsv_clean(n),
            exact_deliveries=n,
            tags=("regression_safe", "tsv", "clean"),
        ))
    for n in (1, 2, 3, 5):
        cases.append(ResilienceCase(
            id=f"json_clean_{n}",
            suffix=".json",
            build=_json_clean(n),
            exact_deliveries=n,
            min_packages=n,
            phone_region="US",
            tags=("regression_safe", "json", "clean"),
        ))
    for n in (3, 5, 8, 12):
        cases.append(ResilienceCase(
            id=f"txt_whatsapp_mini_{n}",
            suffix=".txt",
            build=_whatsapp_mini(n),
            min_deliveries=max(1, n - 2),  # segmenter puede fusionar borde
            exact_deliveries=n if n <= 8 else None,
            expect_text_mode="free_text",
            must_not_see=("Saludos", "Despacho", "Hola chicos"),
            tags=("regression_safe", "txt", "free_text"),
        ))

    # Whatsapp-like exact for small n where we know segmenter works
    cases.append(ResilienceCase(
        id="txt_prose_commas_not_tabular",
        suffix=".txt",
        build=_prose_with_commas(),
        min_deliveries=3,
        expect_text_mode="free_text",
        must_see=("Corrientes", "Santa Fe", "Cabildo"),
        tags=("regression_safe", "txt", "free_text", "comma_trap"),
    ))

    # --- CSV rotos / faltantes ---
    cases.append(ResilienceCase(
        id="csv_poison_rows_keep_good",
        suffix=".csv",
        build=_csv_with_poison_rows(4),
        min_deliveries=4,
        must_see=("Corrientes", "Santa Fe", "Falsa", "Lavalle"),
        tags=("csv", "broken", "poison"),
    ))
    cases.append(ResilienceCase(
        id="csv_missing_coords_geocode",
        suffix=".csv",
        build=_csv_missing_coords(5),
        exact_deliveries=5,
        tags=("csv", "missing_coords"),
    ))
    cases.append(ResilienceCase(
        id="csv_bad_and_swapped_coords",
        suffix=".csv",
        build=_csv_bad_coords(),
        min_deliveries=3,  # las 3 filas existen; coords malas → geocode
        must_see=("Cabildo",),
        tags=("csv", "bad_coords", "swapped"),
    ))
    cases.append(ResilienceCase(
        id="csv_semicolon_delim",
        suffix=".csv",
        build=_csv_semicolon(4),
        exact_deliveries=4,
        tags=("csv", "delimiter"),
    ))
    cases.append(ResilienceCase(
        id="csv_uneven_columns",
        suffix=".csv",
        build=_csv_uneven_columns(),
        min_deliveries=2,
        must_see=("Av. A", "Av. C"),
        tags=("csv", "uneven"),
    ))

    # Variantes parametricas CSV faltantes / basura
    for n in range(2, 8):
        cases.append(ResilienceCase(
            id=f"csv_missing_coords_{n}",
            suffix=".csv",
            build=_csv_missing_coords(n),
            exact_deliveries=n,
            tags=("csv", "missing_coords", "param"),
        ))
    for poison in range(3, 9):
        cases.append(ResilienceCase(
            id=f"csv_poison_keep_{poison}",
            suffix=".csv",
            build=_csv_with_poison_rows(poison),
            min_deliveries=min(poison, 4),
            tags=("csv", "poison", "param"),
        ))
    for n in range(2, 7):
        cases.append(ResilienceCase(
            id=f"csv_semicolon_{n}",
            suffix=".csv",
            build=_csv_semicolon(n),
            exact_deliveries=n,
            tags=("csv", "delimiter", "param"),
        ))

    # --- TSV rotos ---
    for n in range(2, 6):
        cases.append(ResilienceCase(
            id=f"tsv_clean_param_{n}",
            suffix=".tsv",
            build=_tsv_clean(n),
            exact_deliveries=n,
            tags=("tsv", "clean", "param"),
        ))

    # --- TXT ---
    for n in range(2, 11):
        cases.append(ResilienceCase(
            id=f"txt_whatsapp_param_{n}",
            suffix=".txt",
            build=_whatsapp_mini(n),
            min_deliveries=max(1, n - 1),
            expect_text_mode="free_text",
            tags=("txt", "free_text", "param"),
        ))
    for n in (3, 4, 5, 6, 8):
        cases.append(ResilienceCase(
            id=f"txt_numbered_{n}",
            suffix=".txt",
            build=_numbered_list(n),
            min_deliveries=max(1, n - 1),
            tags=("txt", "numbered"),
        ))

    # --- JSON repair / salvage ---
    cases.extend([
        ResilienceCase(
            id="json_trailing_dots",
            suffix=".json",
            build=_json_trailing_dots(),
            exact_deliveries=3,
            must_see=("Ok 1", "Truncated", "Ok 2"),
            notes_any=("reparado",),
            phone_region="US",
            tags=("json", "repair"),
        ),
        ResilienceCase(
            id="json_missing_dim_brace",
            suffix=".json",
            build=_json_missing_dim_brace(),
            min_deliveries=2,
            must_see=("Warren", "After Warren"),
            phone_region="US",
            tags=("json", "repair", "dimensions"),
        ),
        ResilienceCase(
            id="json_garbage_coords",
            suffix=".json",
            build=_json_garbage_coords(),
            min_deliveries=2,
            must_see=("Good St", "Good 2"),
            phone_region="US",
            tags=("json", "repair", "coords"),
        ),
        ResilienceCase(
            id="json_fragments_keep_neighbors",
            suffix=".json",
            build=_json_fragment_and_good(),
            min_deliveries=3,
            must_see=("Before Frag", "After Frag", "Last Good"),
            must_not_see=("ambridge", "cester"),
            phone_region="US",
            tags=("json", "salvage"),
        ),
        ResilienceCase(
            id="json_only_packages_no_dest",
            suffix=".json",
            build=_json_only_packages_no_dest(),
            min_deliveries=1,
            must_see=("Has Dest",),
            phone_region="US",
            tags=("json", "ignored"),
        ),
        ResilienceCase(
            id="json_null_tw_and_currency",
            suffix=".json",
            build=_json_null_tw_multi_pkg(),
            exact_deliveries=2,
            min_packages=3,
            must_see=("Harrison", "Albany"),
            phone_region="US",
            tags=("json", "tw", "currency"),
        ),
        ResilienceCase(
            id="json_same_coords_two_ids",
            suffix=".json",
            build=_json_same_coords_diff_id(),
            exact_deliveries=2,
            must_see=("Park Ave", "42nd"),
            phone_region="US",
            tags=("json", "grouping", "regression_safe"),
        ),
        ResilienceCase(
            id="json_trailing_comma",
            suffix=".json",
            build=_json_trailing_comma(),
            exact_deliveries=1,
            phone_region="US",
            tags=("json", "repair"),
        ),
        ResilienceCase(
            id="json_truncated_file",
            suffix=".json",
            build=_json_truncated_end(),
            min_deliveries=2,
            must_see=("Complete St", "Also Complete"),
            must_not_see=("Truncated St",),
            phone_region="US",
            tags=("json", "salvage", "truncated"),
        ),
    ])

    # JSON clean param extras
    for n in range(4, 11):
        cases.append(ResilienceCase(
            id=f"json_clean_param_{n}",
            suffix=".json",
            build=_json_clean(n),
            exact_deliveries=n,
            phone_region="US",
            tags=("json", "clean", "param"),
        ))

    # Mix: CSV con address rara pero tabulado
    for i, street in enumerate([
        "Av. Corrientes 100", "Calle 50 nro 1234", "11 de Septiembre 1913",
        "Av. Elcano 2098 esquina Superi", "Plot No. 42 Sector 18",
        "1171 1st Ave", "23 MG Road", "Av. Paulista, 1578",
        "Manzana 12 Casa 5", "Flat 14B Shanti Nagar Mumbai",
        "350 5th Ave New York", "Rue de Rivoli 10 Paris",
    ], start=1):
        cases.append(ResilienceCase(
            id=f"csv_weird_address_{i}",
            suffix=".csv",
            build=lambda s=street, k=i: _csv(
                "delivery_id,customer_name,address,lat,lng,quantity",
                f"W-{k},Cli,{s},-34.60,-58.38,1",
                f"W-{k}b,Cli2,Av. Siempre Viva {k},-34.61,-58.39,1",
            ),
            exact_deliveries=2,
            tags=("csv", "weird_address"),
        ))

    # CSV: quantity column present (1 delivery; expansion is separate concern)
    for n in range(1, 6):
        cases.append(ResilienceCase(
            id=f"csv_qty_{n}",
            suffix=".csv",
            build=lambda k=n: _csv(
                "delivery_id,customer_name,address,lat,lng,quantity",
                f"Q-{k},Cli,Av. Qty {k},-34.60,-58.38,{k}",
            ),
            exact_deliveries=1,
            tags=("csv", "quantity"),
        ))

    # JSON: multi-package per stop
    for n_pkg in (1, 2, 3, 4, 5):
        cases.append(ResilienceCase(
            id=f"json_multipkg_{n_pkg}",
            suffix=".json",
            build=lambda k=n_pkg: _json_addresses(
                _addr(42.44, -71.15, f"Multi {k} St",
                      [_pkg(f"M-{j}", 0.5 + j) for j in range(1, k + 1)],
                      zone="G-1"),
            ),
            exact_deliveries=1,
            min_packages=n_pkg,
            phone_region="US",
            tags=("json", "packages"),
        ))

    # TXT free text: mixed bullets without phones
    for n in range(2, 7):
        cases.append(ResilienceCase(
            id=f"txt_no_phone_{n}",
            suffix=".txt",
            build=lambda k=n: (
                "Listado:\n"
                + "".join(
                    f"- Cliente {i} entrega en Av. Libre {i*10}, CABA.\n"
                    for i in range(1, k + 1)
                )
                + "\nFin\n"
            ),
            min_deliveries=max(1, n - 1),
            expect_text_mode="free_text",
            tags=("txt", "no_phone"),
        ))

    # Pipe-delimited as .txt (should tabular if consistent)
    for n in range(2, 6):
        cases.append(ResilienceCase(
            id=f"txt_pipe_tabular_{n}",
            suffix=".txt",
            build=lambda k=n: (
                "delivery_id|customer_name|address|lat|lng|quantity\n"
                + "\n".join(
                    f"P-{i}|Cli {i}|Calle {i}|-34.6|-58.4|1" for i in range(1, k + 1)
                )
                + "\n"
            ),
            exact_deliveries=n,
            tags=("txt", "pipe", "tabular"),
        ))

    # TXT con direcciones internacionales (free text)
    intl = [
        ("Rahul Sharma, Flat 14B, Shanti Nagar, Mumbai", "IN"),
        ("1171 1st Ave, Seattle, WA", "US"),
        ("Av. Paulista, 1578, Sao Paulo", "BR"),
        ("10 Downing Street, London", "GB"),
    ]
    for i, (line, region) in enumerate(intl, start=1):
        cases.append(ResilienceCase(
            id=f"txt_intl_{i}",
            suffix=".txt",
            build=lambda ln=line: (
                f"Hola,\n\n- Cliente Test (1140001000) entrega en {ln}.\n"
                f"- Otro Cliente (1140001001) en Av. Corrientes 100, CABA.\n\n"
                f"Saludos\n"
            ),
            min_deliveries=1,
            expect_text_mode="free_text",
            phone_region=region if region in ("AR", "US", "BR", "IN", "GB") else "AR",
            tags=("txt", "intl"),
        ))

    # BOM / preamble CSV
    cases.append(ResilienceCase(
        id="csv_bom_utf8",
        suffix=".csv",
        build=lambda: "\ufeff" + _clean_csv_n(3)(),
        exact_deliveries=3,
        tags=("csv", "bom"),
    ))
    cases.append(ResilienceCase(
        id="csv_preamble_lines",
        suffix=".csv",
        build=lambda: (
            "Reporte de entregas\n"
            "Fecha: hoy\n"
            "\n"
            + _clean_csv_n(3)()
        ),
        min_deliveries=2,
        tags=("csv", "preamble"),
    ))

    # Dedupe ids
    seen: set[str] = set()
    unique: list[ResilienceCase] = []
    for c in cases:
        if c.id in seen:
            continue
        seen.add(c.id)
        unique.append(c)
    return unique


CATALOG: list[ResilienceCase] = build_catalog()
