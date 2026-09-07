"""CLI: python -m smart_import.vocab setup|build|stats"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .builder import build
from .store import VocabularyStore, default_sqlite_path, reset_store_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smart_import.vocab")
    sub = parser.add_subparsers(dest="cmd", required=True)

    setup_p = sub.add_parser(
        "setup",
        help="Genera smart_import_vocab.sqlite desde catalog + UNECE/GS1/libpostal",
    )
    setup_p.add_argument(
        "--out", type=str, default=None,
        help="Ruta destino (default: data/vocab/smart_import_vocab.sqlite)",
    )
    setup_p.add_argument(
        "--geonames", action="store_true",
        help="También regenera locality_expand_geonames.json (descarga ~2–3 MB)",
    )
    setup_p.add_argument(
        "--geonames-out", type=str, default=None,
        help="Ruta del JSON GeoNames (default: smart_import/resources/locality_expand_geonames.json)",
    )
    setup_p.add_argument(
        "--catalog-only", action="store_true",
        help="Solo catalog.json (sin UNECE/GS1/libpostal)",
    )

    build_p = sub.add_parser("build", help="Alias de setup (sin GeoNames)")
    build_p.add_argument("--out", type=str, default=None)
    build_p.add_argument("--catalog-only", action="store_true")

    stats_p = sub.add_parser("stats", help="Cuenta conceptos y aliases")
    stats_p.add_argument("--db", type=str, default=None)

    args = parser.parse_args(argv)

    if args.cmd in {"setup", "build"}:
        dest = args.out or str(default_sqlite_path())
        if args.cmd == "setup" and args.geonames:
            _run_geonames(getattr(args, "geonames_out", None))
        extras = not args.catalog_only
        store = build(
            dest,
            catalog=True,
            unece=extras,
            gs1=extras,
            libpostal=extras,
            overrides=True,
        )
        print(json.dumps({"path": dest, **store.stats()}, ensure_ascii=False, indent=2))
        store.close()
        reset_store_cache()
        return 0

    path = args.db or str(default_sqlite_path())
    store = VocabularyStore.open(path, create=False)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    store.close()
    return 0


def _run_geonames(output: str | None = None) -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "import_geonames_localities.py"
    if not script.exists():
        raise SystemExit(f"falta {script} (hace falta en la imagen / repo)")
    cmd = [sys.executable, str(script)]
    if output:
        cmd += ["-o", str(output)]
    print("refrescando GeoNames cities15000 …")
    subprocess.check_call(cmd, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
