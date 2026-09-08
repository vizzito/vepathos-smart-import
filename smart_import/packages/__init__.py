"""Lectura de paqueteria en lenguaje natural.

Es al bulto lo que `smart_import.addresses` es a la direccion: una capa propia,
con su vocabulario en datos, que devuelve una estructura con evidencia. Quien la
usa decide que hacer con eso — el extractor de texto libre la vuelca al schema,
el normalizador de filas la usa para rescatar una celda escrita a mano.
"""
from .lexicon import PackageLexicon, get_package_lexicon, skeleton
from .parser import PackageItem, PackageParse, PackageParser, parse_packages

__all__ = [
    "PackageItem", "PackageLexicon", "PackageParse", "PackageParser",
    "get_package_lexicon", "parse_packages", "skeleton",
]
