"""Published assignment-table schema order is part of the report contract."""
import csv
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('RTB_RENDER_MODULE', ROOT / 'bin/report_render.py'))
OTU = ['taxon', 'sample', 'marker', 'family', 'genus', 'species',
       'otu_count', 'frozen_otu_count', 'frozen_otu_reads_total',
       'frozen_otu_reads_sample_total', 'otu_reads_sample_total',
       'reads_total', 'otu_reads_global_total', 'frozen_otu_reads_global_total',
       'perc_id_min', 'perc_id_max', 'aln_length_min', 'aln_length_max']
CONSENSUS = ['taxon', 'sample', 'marker', 'family', 'genus', 'species',
             'consensus_count', 'consolidated_consensus_count',
             'consolidated_consensus_reads_total', 'reads_total',
             'perc_id_min', 'perc_id_max', 'aln_length_min', 'aln_length_max']


def module():
    spec = importlib.util.spec_from_file_location('report_render_assignment_order', SCRIPT)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def read(path):
    with path.open(newline='', encoding='utf-8') as handle:
        lines = list(csv.reader(handle, delimiter='\t'))
    assert lines and len(lines[0]) == len(set(lines[0]))
    assert all(len(row) == len(lines[0]) for row in lines[1:])
    return lines[0], [dict(zip(lines[0], row)) for row in lines[1:]]


def render(tmp_path, otu=None, consensus=None, identity_mode='collapse', interest=False):
    obj = {
        'identity_mode': identity_mode,
        'otu': {'assignments_by_level': dict(otu or {})},
        'consensus': {'assignments_by_level': dict(consensus or {})},
    }
    if interest:
        obj['otu']['assignments_by_level']['species_interest_enabled'] = True
    directory = tmp_path / 'figures'
    module().write_chart_tsvs([obj], directory, run_id='R10D1')
    return directory


def test_known_columns_are_canonical_for_100_permutations(tmp_path):
    row = {key: str(i) for i, key in enumerate(OTU)}
    baseline = None
    orders = [list(row), list(reversed(row))]
    for seed in range(100):
        keys = list(row)
        random.Random(seed).shuffle(keys)
        orders.append(keys)
    for index, keys in enumerate(orders):
        ordered = {key: row[key] for key in keys}
        directory = render(tmp_path / str(index), otu={'species': [ordered]})
        path = directory / 'otu_assignments_species.tsv'
        header, data = read(path)
        assert header == OTU
        assert data == [row]
        if baseline is None:
            baseline = path.read_bytes()
        else:
            assert path.read_bytes() == baseline


def test_present_subset_one_column_empty_and_track_appendix(tmp_path):
    directory = render(tmp_path / 'subset', otu={'species': [{'reads_total': 5, 'taxon': 'T'}]})
    assert read(directory / 'otu_assignments_species.tsv') == (
        ['taxon', 'reads_total'], [{'taxon': 'T', 'reads_total': '5'}])
    directory = render(tmp_path / 'one', otu={'species': [{'taxon': 'T'}]})
    assert read(directory / 'otu_assignments_species.tsv') == (['taxon'], [{'taxon': 'T'}])
    directory = render(tmp_path / 'empty')
    assert read(directory / 'otu_assignments_species.tsv') == (OTU, [])
    assert read(directory / 'consensus_assignments_species.tsv') == (CONSENSUS, [])
    directory = render(tmp_path / 'track', otu={'species': [{
        'track_unit_id': 'TU', 'species_interest': True, 'taxon': 'T'}]},
        identity_mode='track', interest=True)
    assert read(directory / 'otu_assignments_species.tsv')[0] == [
        'taxon', 'species_interest', 'track_unit_id']


def test_unknown_fields_are_sorted_and_values_stay_mapped(tmp_path):
    first = {'é field': 'accent', 'z field': 'zed', 'taxon': 'T1',
             ' α': 'alpha', 'reads_total': 7}
    second = {'reads_total': None, 'taxon': 'T2', 'z field': 'other',
              'é field': 'accent2', ' α': 'alpha2'}
    directory = render(tmp_path / 'first', otu={'species': [first, second]})
    header, data = read(directory / 'otu_assignments_species.tsv')
    assert header == ['taxon', 'reads_total', ' α', 'z field', 'é field']
    assert data == [{'taxon': 'T1', 'reads_total': '7', ' α': 'alpha',
                     'z field': 'zed', 'é field': 'accent'},
                    {'taxon': 'T2', 'reads_total': '', ' α': 'alpha2',
                     'z field': 'other', 'é field': 'accent2'}]
    reversed_first = dict(reversed(list(first.items())))
    reversed_second = dict(reversed(list(second.items())))
    other = render(tmp_path / 'reverse', otu={'species': [reversed_first, reversed_second]})
    assert (other / 'otu_assignments_species.tsv').read_bytes() == (
        directory / 'otu_assignments_species.tsv').read_bytes()
    directory = render(tmp_path / 'single_unknown', otu={'species': [
        {'taxon': 'T', 'extra': 'x'}]})
    assert read(directory / 'otu_assignments_species.tsv')[0] == ['taxon', 'extra']


@pytest.mark.parametrize('name,source,level,metric', [
    ('otu_assignments_species.tsv', 'otu', 'species', 'otu_count'),
    ('otu_assignments_genus.tsv', 'otu', 'genus', 'otu_count'),
    ('otu_assignments_family.tsv', 'otu', 'family', 'otu_count'),
    ('consensus_assignments_species.tsv', 'consensus', 'species', 'consensus_count'),
    ('consensus_assignments_genus.tsv', 'consensus', 'genus', 'consensus_count'),
    ('consensus_assignments_family.tsv', 'consensus', 'family', 'consensus_count'),
    ('frozen_otu_assignments_species.tsv', 'otu', 'species', 'frozen_otu_count'),
    ('consolidated_consensus_assignments_species.tsv', 'consensus', 'species', 'consolidated_consensus_count'),
    ('otu_assignments_by_sample_species.tsv', 'otu', 'species', 'otu_count'),
    ('consensus_assignments_by_sample_species.tsv', 'consensus', 'species', 'consensus_count'),
])
def test_every_nonempty_assignment_variant(tmp_path, name, source, level, metric):
    row1 = {'z extra': 'Z', 'reads_total': 9, metric: 2, 'taxon': 'A'}
    row2 = {'taxon': 'B', metric: 1, 'reads_total': 0, 'a extra': 'A'}
    data = {level: [row1, row2]}
    directory = render(tmp_path, **{source: data})
    header, rows = read(directory / name)
    canonical = OTU if source == 'otu' else CONSENSUS
    assert header == [key for key in canonical if key in row1 or key in row2] + ['a extra', 'z extra']
    assert rows == [
        {'taxon': 'A', metric: '2', 'reads_total': '9', 'a extra': '', 'z extra': 'Z'},
        {'taxon': 'B', metric: '1', 'reads_total': '0', 'a extra': 'A', 'z extra': ''},
    ]
    reversed_data = {level: [dict(reversed(list(row1.items()))),
                             dict(reversed(list(row2.items())))]}
    other = render(tmp_path / 'reversed', **{source: reversed_data})
    assert (other / name).read_bytes() == (directory / name).read_bytes()
    assert {path.name for path in directory.iterdir()} == {path.name for path in other.iterdir()}


def test_python_hash_seeds_leave_bytes_identical(tmp_path):
    script = '''import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
row = json.loads(sys.argv[3])
r.write_chart_tsvs([{"otu": {"assignments_by_level": {"species": [row]}}}],
                   pathlib.Path(sys.argv[2]), run_id="R10D1")
'''
    row = {'z extra': 'Z', 'reads_total': 9, 'taxon': 'A', 'a extra': 'A'}
    outputs = []
    for seed in ('0', '1', '4242', 'random'):
        target = tmp_path / seed
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-c', script, str(SCRIPT), str(target),
                                 json.dumps(row)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        outputs.append((target / 'otu_assignments_species.tsv').read_bytes())
    assert len(set(outputs)) == 1
