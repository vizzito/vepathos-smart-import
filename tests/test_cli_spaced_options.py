"""CLI: ciudades / paises de varias palabras sin comillas."""
from smart_import.cli import join_spaced_option_values


def test_depot_city_une_dos_palabras_hasta_el_proximo_flag():
    argv = [
        "geocode-accuracy",
        "--truth", "ar.json",
        "--depot-city", "san", "francisco",
        "--origin-lat", "37.729",
        "--origin-lon", "-122.4141",
        "--enhance",
    ]
    out = join_spaced_option_values(argv)
    assert out[out.index("--depot-city") + 1] == "san francisco"
    assert "--origin-lat" in out
    assert "37.729" in out
    assert "-122.4141" in out


def test_depot_country_une_united_states():
    out = join_spaced_option_values(
        ["--depot-country", "United", "States", "--enhance"])
    assert out[out.index("--depot-country") + 1] == "United States"
    assert out[-1] == "--enhance"


def test_depot_address_une_hasta_el_flag():
    out = join_spaced_option_values(
        ["--depot-address", "Av.", "Cordoba", "3500", "--depot-city", "CABA"])
    assert out[out.index("--depot-address") + 1] == "Av. Cordoba 3500"
    assert out[out.index("--depot-city") + 1] == "CABA"


def test_forma_con_igual_tambien_une():
    out = join_spaced_option_values(
        ["--depot-city=san", "francisco", "--limit", "10"])
    assert "--depot-city=san francisco" in out
    assert out[out.index("--limit") + 1] == "10"


def test_ya_entrecomillado_no_se_rompe():
    out = join_spaced_option_values(
        ["--depot-city", "San Francisco", "--origin-lat", "1"])
    assert out[out.index("--depot-city") + 1] == "San Francisco"
