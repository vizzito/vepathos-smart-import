"""SMART_IMPORT_CORS_ORIGINS: parser CSV + default."""
from smart_import.config import parse_csv_list


def test_cors_csv_varios_origins():
    assert parse_csv_list(
        "https://app.vepathos.com, https://admin.vepathos.com",
        ["*"],
    ) == ["https://app.vepathos.com", "https://admin.vepathos.com"]


def test_cors_vacio_cae_al_default():
    assert parse_csv_list(None, ["*"]) == ["*"]
    assert parse_csv_list("   ", ["*"]) == ["*"]


def test_cors_un_origin_sin_wildcard():
    assert parse_csv_list("https://app.vepathos.com", ["*"]) == [
        "https://app.vepathos.com"
    ]
