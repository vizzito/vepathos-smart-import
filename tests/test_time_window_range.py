"""Compound input windows: parsing, intent, precedence and lossless diagnostics."""
import csv
import json
from datetime import date
from pathlib import Path

import pytest

from smart_import import pipeline
from smart_import.time_window_range import parse_window

SCHEMA = 'schemas/vepathos_flat_v1.json'
DAY = date(2026, 9, 21)
FORMS = ['09:00 - 11:00', '09:00-11:00', '9 a 11', '9 a 11 hs',
         '09:00 a 11:00', 'de 9 a 11', '9:00 AM - 11:00 AM', '09:00–11:00',
         '09:00—11:00', '09:00/11:00', '09:00 to 11:00', '9h-11h',
         'entre 9 y 11', 'between 9 and 11', ' FROM 9 TO 11 ', '9 hs - 11 hs']


def normalize(tmp_path, rows, **kwargs):
    path = tmp_path / 'input.csv'
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['delivery_id', 'lat', 'lng', *columns])
        w.writeheader()
        for i, row in enumerate(rows):
            w.writerow({'delivery_id': f'D{i}', 'lat': -37.321, 'lng': -59.123, **row})
    opts = dict(service_date=DAY)
    opts.update(kwargs)
    return pipeline.run_normalize(path, SCHEMA, tmp_path / 'out.csv', **opts)


@pytest.mark.parametrize('text', FORMS)
def test_formats(text):
    hit = parse_window(text)
    assert (hit.kind, hit.start, hit.end) == ('range', (9, 0), (11, 0))


@pytest.mark.parametrize('text,kind', [
    ('hasta las 11', 'before'), ('después de las 14', 'after'),
    ('22:00 - 02:00', 'overnight'), ('', 'empty'), ('a coordinar', 'invalid'),
    ('todo el día', 'invalid'), ('24:00-25:00', 'invalid'), ('9:60-11:00', 'invalid'),
    ('13 AM - 14 PM', 'invalid'), ('0 AM - 11 AM', 'invalid'), ('9-9', 'invalid'),
    ('9-11 o cuando puedan', 'invalid'), ('9-11 / 14-16', 'invalid'),
    ('2024-10-25', 'invalid'), ('11-4000-1000', 'invalid'), ('9-11 kg', 'invalid'),
    ('9-11.5', 'invalid'), ('9-110', 'invalid'),
])
def test_classifies_without_inventing(text, kind):
    assert parse_window(text).kind == kind


def test_mixed_column_and_canonical_output(tmp_path):
    result = normalize(tmp_path, [{'time_window': v} for v in FORMS])
    assert len(result.outcome.rows) == len(FORMS)
    assert all(r.values['tw_start'] == '2026-09-21 09:00' for r in result.outcome.rows)
    assert all(r.values['tw_end'] == '2026-09-21 11:00' for r in result.outcome.rows)
    assert 'time_window' not in result.report['output_columns']
    assert result.report['mapping']['time_window']['target'] == 'time_window'
    assert 'time_window' not in result.report['unmapped']
    assert not result.report['ambiguous']


@pytest.mark.parametrize('alias', ['delivery_window', 'ventana de entrega', 'horario de entrega', 'janela de entrega'])
def test_explicit_alias(tmp_path, alias):
    result = normalize(tmp_path, [{alias: '9-11'}])
    assert result.report['mapping'][alias]['target'] == 'time_window'
    assert result.outcome.rows[0].values['tw_end'].endswith('11:00')


@pytest.mark.parametrize('header', ['horario', 'franja', 'ventana', 'columna_desconocida'])
def test_content_and_generic_names_require_review(tmp_path, header):
    result = normalize(tmp_path, [{header: '09:00-11:00'}] * 3)
    assert any(a['column'] == header and a['suggested'] == 'time_window'
               for a in result.report['ambiguous'])


@pytest.mark.parametrize('header', ['horario de atención', 'opening hours', 'horario de cierre'])
def test_business_hours_not_delivery_window(tmp_path, header):
    result = normalize(tmp_path, [{header: '09:00-11:00'}] * 3)
    assert 'tw_start' not in result.report['output_columns']
    assert header in result.report['unmapped']


@pytest.mark.parametrize('spec', ['time_window', {'campo': 'time_window', 'formato': 'rango'},
                                   {'field': 'time_window', 'format': 'range'}])
def test_manual_mapping(tmp_path, spec):
    result = normalize(tmp_path, [{'horario de atención': '9-11'}],
                       manual_mapping={'horario de atención': spec})
    assert result.outcome.rows[0].values['tw_end'].endswith('11:00')
    assert not result.report['ambiguous']


def test_ignored_range_stays_ignored(tmp_path):
    result = normalize(tmp_path, [{'time_window': '9-11'}], manual_mapping={'time_window': None})
    assert not result.outcome.rows[0].values.get('tw_start')
    assert not result.report['time_window_issues']


@pytest.mark.parametrize('text', ['', 'a coordinar', 'todo el día', 'hasta las 11', 'después de las 14', '22-2'])
def test_warning_per_row_without_invalidating_delivery(tmp_path, text):
    result = normalize(tmp_path, [{'time_window': text}])
    assert result.report['valid_rows'] == 1
    assert not result.outcome.rows[0].values.get('tw_start')
    issue = result.report['time_window_issues'][0]
    assert issue['raw'] == text and issue['row'] == 1 and issue['severity'] == 'warning'


def test_missing_date_not_taken_from_excluded_source_date(tmp_path):
    result = normalize(tmp_path, [{'time_window': '9-11', 'source_date': '2024-10-25'}],
                       service_date=None, manual_mapping={'source_date': None})
    assert result.report['time_window_issues'][0]['code'] == 'missing_service_date'
    assert not result.outcome.rows[0].values.get('tw_start')


def test_scalar_precedence_conflict_fallback_and_no_mixing(tmp_path):
    rows = [
        {'time_window': '9-11', 'tw_start': '2026-09-21 09:00', 'tw_end': '2026-09-21 11:00'},
        {'time_window': '9-11', 'tw_start': '2026-09-21 10:00', 'tw_end': '2026-09-21 12:00'},
        {'time_window': '9-11', 'tw_start': '', 'tw_end': ''},
        {'time_window': '9-11', 'tw_start': '2026-09-21 10:00', 'tw_end': ''},
        {'time_window': '9-11', 'tw_start': 'garbage', 'tw_end': 'garbage'},
    ]
    result = normalize(tmp_path, rows)
    starts = [r.values.get('tw_start') for r in result.outcome.rows]
    assert starts == ['2026-09-21 09:00', '2026-09-21 10:00', '2026-09-21 09:00', None, None]
    assert [i['code'] for i in result.report['time_window_issues']] == ['conflict', 'invalid_endpoints', 'invalid_endpoints']


def test_manual_range_wins_over_automatic_endpoints(tmp_path):
    result = normalize(tmp_path, [{'time_window': '9-11', 'tw_start': '2026-09-21 10:00',
                                  'tw_end': '2026-09-21 12:00'}], manual_mapping={'time_window': 'time_window'})
    assert result.outcome.rows[0].values['tw_start'].endswith('09:00')


def test_explicit_endpoint_exclusion_not_regenerated(tmp_path):
    result = normalize(tmp_path, [{'time_window': '9-11', 'tw_start': '2026-09-21 10:00'}],
                       manual_mapping={'tw_start': None})
    assert not result.outcome.rows[0].values.get('tw_start')
    assert result.report['time_window_issues'][0]['code'] == 'excluded'


def test_utc_and_roundtrip(tmp_path):
    result = normalize(tmp_path, [{'time_window': '9-11'}], timezone='America/Argentina/Buenos_Aires')
    row = result.outcome.rows[0].values
    assert (row['tw_start'], row['tw_end'], row['tw_timezone']) == (
        '2026-09-21T12:00:00Z', '2026-09-21T14:00:00Z', 'UTC')
    repeated = pipeline.run_normalize(result.outputs['flat'], SCHEMA, timezone='America/Argentina/Buenos_Aires')
    assert repeated.outcome.rows[0].values['tw_start'] == row['tw_start']


@pytest.mark.parametrize('day,text', [(date(2026, 3, 8), '02:15-03:30'), (date(2026, 11, 1), '01:15-02:30')])
def test_dst_unsafe_times_warn(tmp_path, day, text):
    result = normalize(tmp_path, [{'time_window': text}], service_date=day, timezone='America/New_York')
    assert result.report['time_window_issues'][0]['code'] == 'ambiguous_local_time'
    assert not result.outcome.rows[0].values.get('tw_start')


def test_all_warnings_in_downloadable_report(tmp_path):
    result = normalize(tmp_path, [{'time_window': 'a coordinar'}] * 250)
    report = json.loads(Path(result.outputs['report']).read_text())
    assert len(report['row_issues']) == 200
    assert report['row_issues_total'] == 250 and report['row_issues_truncated']
    assert len(report['time_window_issues']) == 250
    assert report['time_window_summary']['rows_with_warnings'] == 250


def test_tandil_250_windows(tmp_path):
    source = Path('tests/fixtures/tandil_time_windows.csv')
    result = pipeline.run_normalize(source, SCHEMA, tmp_path / 'tandil.csv', emit=('flat', 'nested'),
        service_date=DAY, manual_mapping={'source_date': None, 'source_order': None, 'address': 'address'})
    assert result.report['rows_output'] == result.report['valid_rows'] == 250
    assert result.report['time_window_summary']['generated'] == 250
    assert not result.report['time_window_issues']
    original = list(csv.DictReader(source.open()))
    output = list(csv.DictReader(Path(result.outputs['flat']).open()))
    for before, after in zip(original, output):
        start, end = before['time_window'].split(' - ')
        assert after['tw_start'] == f'2026-09-21 {start}'
        assert after['tw_end'] == f'2026-09-21 {end}'
    assert len(result.deliveries) == 250
    assert all(d['time_window']['start'] and d['time_window']['end'] for d in result.deliveries)


def test_composite_address_does_not_erase_manual_decisions(tmp_path):
    result = normalize(tmp_path, [{'Datos': 'Juan Perez - Av. Corrientes 1250 Buenos Aires - tel 1144445555',
                                  'horario': '9-11', 'source_date': '2024-10-25', 'source_order': i}
                                 for i in range(1, 13)],
                       manual_mapping={'horario': {'campo': 'time_window', 'formato': 'rango'},
                                       'source_date': None, 'source_order': None})
    assert result.report['time_window_summary']['generated'] == 12
    assert result.report['mapping']['horario']['method'] == 'manual'
    assert 'source_date' in result.report['unmapped'] and 'source_order' in result.report['unmapped']
    assert not any(a['column'] in {'horario', 'source_date', 'source_order'} for a in result.report['ambiguous'])


def test_api_compound_spec_and_service_date_survive_remapping():
    from fastapi.testclient import TestClient
    from smart_import.api.app import app
    client = TestClient(app)
    data = b'delivery_id,lat,lng,horario\nD1,-37.321,-59.123,9-11\n'
    uploaded = client.post('/imports', files={'file': ('tw.csv', data)},
                           params={'service_date': '2026-09-21'})
    assert uploaded.status_code == 201
    job = uploaded.json()
    updated = client.put(f"/imports/{job['job_id']}/mapping",
                         json={'horario': {'campo': 'time_window', 'formato': 'rango'}})
    assert updated.status_code == 200
    report = updated.json()['report']
    assert report['service_date'] == '2026-09-21'
    assert report['time_window_summary']['generated'] == 1
    assert not report['ambiguous']


def test_text_extraction_shares_clock_range_parser():
    from smart_import.extraction.time_window import find_time_expression, to_window
    hit = find_time_expression('Entrega a Ana, TW: 9:00 AM – 11:00 AM; llamar antes')
    assert to_window(hit, DAY) == {'tw_start': '2026-09-21 09:00', 'tw_end': '2026-09-21 11:00'}
    assert find_time_expression('Calle 9-11 numero 500') is None


def test_no_reclassification_of_explicit_other_fields(tmp_path):
    result = normalize(tmp_path, [{'reference': '09:00-11:00'}] * 3)
    assert result.report['mapping']['reference']['target'] == 'reference'
    assert 'time_window' not in result.report['output_columns']


def test_cli_service_date(tmp_path):
    from typer.testing import CliRunner
    from smart_import.cli import app
    source = tmp_path / 'clock.csv'
    source.write_text('delivery_id,lat,lng,time_window\nD1,-37.321,-59.123,9-11\n')
    output = tmp_path / 'normalized.csv'
    args = ['normalize', '--input', str(source), '--output', str(output)]
    result = CliRunner().invoke(app, [*args, '--service-date', '2026-09-21'])
    assert result.exit_code == 0, result.output
    assert '2026-09-21 09:00' in output.read_text()
    invalid = CliRunner().invoke(app, [*args, '--service-date', '2026-02-30'])
    assert invalid.exit_code != 0 and 'YYYY-MM-DD' in invalid.output
