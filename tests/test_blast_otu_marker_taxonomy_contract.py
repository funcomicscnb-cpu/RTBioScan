"""R4-B scientific oracle: expected parsing/LCA/voting never imports Perl code."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from collections import Counter

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'bin/otu_refine_blastreport.pl'
CACHE = ROOT / 'bin/cache_blast_by_hash.pl'
INSTALL = ROOT / 'bin/install_taxonomy_release.pl'
RANKS = ('kingdom', 'phylum', 'class', 'order', 'family', 'genus', 'species')
PREFIX = 'kpcofgs'
ANIMAL = ['Metazoa', 'Arthropoda', 'Insecta', 'Diptera', 'Muscidae', 'Musca', 'Musca domestica']
PLANT = ['Viridiplantae', 'Streptophyta', 'Magnoliopsida', 'Rosales', 'Rosaceae', 'Rosa', 'Rosa canina']


def oracle_parse(text):
    if not text.strip() or text.strip().upper() == 'NA':
        return None
    out = ['NA'] * 7
    previous = -1
    for field in text.strip().split(';'):
        match = re.fullmatch(r'([KPCOFGSkpcofgs])__(.*)', field.strip())
        if not match:
            return None
        rank = PREFIX.index(match[1].lower())
        if rank <= previous:
            return None
        previous = rank
        name = match[2].strip()
        out[rank] = name if name and name.lower() not in ('na', 'unassigned') else 'NA'
    return out


def oracle_lca(lineages):
    common = ['NA'] * 7
    for rank in range(7):
        names = {lineage[rank] for lineage in lineages}
        if len(names - {'NA'}) > 1:
            break
        if len(names) == 1 and 'NA' not in names:
            common[rank] = next(iter(names))
    return common


def oracle_vote(votes):
    counts = Counter((tax, tuple(lineage)) for tax, lineage in votes)
    winners = [key for key, n in counts.items() if n == max(counts.values())]
    if len(winners) == 1:
        return 'ASSIGNED', list(winners[0][1])
    lineage = oracle_lca([key[1] for key in winners])
    return ('AMBIGUOUS_TIE' if any(v != 'NA' for v in lineage) else 'REFERENCE_UNRESOLVED'), lineage


def lineage(ranks, upper=False):
    return ';'.join(f'{p.upper() if upper else p}__{v}' for p, v in zip(PREFIX, ranks))


def seal(kind, signature, rows, version=None):
    body = ''.join(row + '\n' for row in rows)
    version = version or (1 if kind == 'EVIDENCE' else 2)
    return f'#RTB-R4-{kind}\t{version}\t{signature}\n{body}#END\t{len(rows)}\t{hashlib.sha256(body.encode()).hexdigest()}\n'


def command(args, cwd, env=None):
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', NXF_OFFLINE='true')
    e.update(env or {})
    return subprocess.run([str(v) for v in args], cwd=cwd, env=e, text=True, capture_output=True)


def fixture(tmp_path, assignments=None, definitions=None, groups=None, memories=None, reference=None, authority=None, representative=True):
    """Assignments: (query, marker, list of tied taxids or None, percent identity)."""
    assignments = assignments or [('a', 'COI', ['-1'], 99)]
    definitions = definitions or {'COI': {'-1': ANIMAL}}
    memories = memories or {}
    taxdir = tmp_path / 'taxonomy'
    taxdir.mkdir()
    for name in ('nodes.dmp', 'names.dmp', 'merged.dmp', 'delnodes.dmp'):
        (taxdir / name).write_text(name + '\n')
    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()
    numeric = {tax: lineage(r) for defs in definitions.values() for tax, r in defs.items() if int(tax) > 0}
    (tmp_path / 'numeric.json').write_text(json.dumps(numeric))
    tool = fakebin / 'taxonkit'
    tool.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
rows = Path(sys.argv[-1]).read_text().splitlines()
values = json.loads(Path(os.environ['R4B_TEST_NUMERIC']).read_text())
for row in rows:
    tax = row.split('\\t')[0]
    if sys.argv[1] == 'lineage': print(tax+'\\tancestor')
    else: print(tax+'\\tancestor\\t'+values.get(tax, ''))
''')
    tool.chmod(0o755)
    env = {'PATH': str(fakebin) + os.pathsep + os.environ['PATH'], 'R4B_TEST_NUMERIC': str(tmp_path / 'numeric.json')}
    cfg = {'targets': [], 'taxonomy_dir': str(taxdir), 'sidecar': str(tmp_path / 'status.tsv'), 'hash_map': str(tmp_path / 'hashes.tsv'), 'lineage': str(tmp_path / 'lineage.tsv')}
    authrows = []
    for marker, defs in definitions.items():
        target = {'marker': marker, 'kingdom': 'Metazoa' if marker == 'COI' else 'Viridiplantae', 'family': 90, 'genus': 95, 'species': 98, 'evalue': '1e-5', 'max_hsps': 1}
        for name, suffix in [('database', 'db'), ('seed', 'seed'), ('evidence', 'evidence'), ('memtax', 'memtax'), ('metadata', 'metadata')]:
            target[name] = str(tmp_path / f'{marker}.{suffix}')
        Path(target['database'] + '.nhr').write_text('reference fixture\n')
        seedrows, memrows, metarows = [], [], []
        for tax, ranks in defs.items():
            memory = memories.get((marker, tax), [tax, 'NA', 'NA', 'NA', 'species'])
            memrows.append('\t'.join([tax] + memory))
            if int(tax) < 0:
                seedrows.append('\t'.join([tax, *memory[1:4], memory[4]]))
                authrows.append(f'{tax}\t{lineage(ranks)}')
            ref = (reference or {}).get((marker, tax), ranks)
            metarows.append(f'accession_{tax}\t0\topaque_{marker}_{tax}|kraken:taxid|{tax} {marker} {lineage(ref)}')
        Path(target['seed']).write_text('\n'.join(seedrows) + ('\n' if seedrows else ''))
        Path(target['metadata']).write_text('\n'.join(metarows) + '\n')
        args = [CACHE, '--signature', target['database'], taxdir] + [f'{k}={target[v]}' for k, v in [('idfam', 'family'), ('idgen', 'genus'), ('idspec', 'species'), ('evalue', 'evalue'), ('maxhsps', 'max_hsps')]] + ['word=50', 'qcov=50', f'target={marker}', 'seed=' + target['seed']]
        cp = command(args, tmp_path, env)
        assert cp.returncode == 0, cp.stderr
        sig = cp.stdout.split('\t')[0]
        rows = []
        for name, mark, tied, pid in assignments:
            if mark != marker:
                continue
            query = name if '|' in name else f'{name}|{marker}|sup'
            h = hashlib.md5(query.split('|')[0].encode()).hexdigest()
            if tied is None:
                rows.append('\t'.join([query, h, *(['NA'] * 11), 'NO_HIT']))
            else:
                for subject, tax in enumerate(tied):
                    rows.append('\t'.join(map(str, [query, h, f'subject_{subject}_{tax}', tax, '1e-20', 100, pid, 200, 1, 100, 1, 100, 100, 'DEFERRED_TIE' if len(tied) > 1 else 'UNIQUE'])))
        Path(target['evidence']).write_text(seal('EVIDENCE', sig, rows))
        Path(target['memtax']).write_text(seal('MEMTAX', sig, memrows))
        cfg['targets'].append(target)
    Path(cfg['lineage']).write_text(authority if authority is not None else '\n'.join(authrows) + ('\n' if authrows else ''))
    if groups is None:
        groups = [[name if '|' in name else f'{name}|{marker}|sup' for name, marker, _, _ in assignments]]
    clstr = []
    hashes = []
    for n, members in enumerate(groups):
        clstr.append(f'>Cluster {n}')
        for i, query in enumerate(members):
            isrep = representative and i == 0
            clstr.append(f'{i}\t100nt, >{query}... ' + ('*' if isrep else 'at +/99%'))
            if isrep:
                hashes.append(query.split('|')[0] + '\t' + hashlib.md5(query.encode()).hexdigest())
    (tmp_path / 'reads.clstr').write_text('\n'.join(clstr) + '\n')
    Path(cfg['hash_map']).write_text('\n'.join(hashes) + ('\n' if hashes else ''))
    (tmp_path / 'contract.json').write_text(json.dumps(cfg, sort_keys=True))
    return cfg, env


def run_fixture(tmp_path, env=None):
    return command(['perl', SCRIPT, '--r4b', tmp_path / 'reads.clstr', tmp_path / 'lineage.tsv', tmp_path / 'contract.json'], tmp_path, env)


def sidecar(tmp_path):
    lines = (tmp_path / 'status.tsv').read_text().splitlines()
    assert lines[0].startswith('#RTB-R4B-TAXONOMY\t1\t')
    names = lines[1].split('\t')[1:]
    rows = [dict(zip(names, row.split('\t'))) for row in lines[2:-1]]
    assert len(lines[-1].split('\t')) == 3
    return rows


@pytest.mark.parametrize('upper', [False, True])
@pytest.mark.parametrize('depth', [3, 4, 5, 6])
def test_prefix_depth_and_numeric_signed_parity(tmp_path, upper, depth):
    ranks = ANIMAL[:depth+1] + ['NA']*(6-depth)
    cfg, env = fixture(tmp_path, authority='-1\t' + lineage(ranks, upper) + '  \r\n', definitions={'COI': {'-1': ranks}})
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert oracle_parse(row['lineage']) == ranks
    assert row['lineage'] == lineage(ranks).replace('k__', 'K__', 1)
    assert int(row['depth']) == depth
    for level, minimum in [('family', 4), ('genus', 5), ('species', 6)]:
        a, u = tmp_path / 'a', tmp_path / 'u'
        for which, out in [('assigned', a), ('unassigned', u)]:
            result = command(['perl', ROOT/f'bin/blast_{which}_read_ids.pl', tmp_path/'status.tsv', out, '--min-level', level], tmp_path, env)
            assert result.returncode == 0, result.stderr
        assert a.read_text().splitlines() == (['a'] if depth >= minimum else [])
        assert u.read_text().splitlines() == ([] if depth >= minimum else ['a'])


@pytest.mark.parametrize('bad', ['', 'NA', 'k__Metazoa;p__X;p__Y', 'p__X;k__Metazoa', 'k__Metazoa;garbage', 'k__Metazoa;;g__Musca'])
def test_malformed_authority_fails_closed(tmp_path, bad):
    _, env = fixture(tmp_path, authority='-1\t'+bad+'\n')
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['status'] == 'REFERENCE_UNRESOLVED'
    assert row['votes'] == '0' and row['read_reason'].startswith('-1:')


@pytest.mark.parametrize('kind', ['missing', 'identical_duplicate', 'conflicting_duplicate', 'contradictory_title', 'completeness_only'])
def test_authority_dispositions(tmp_path, kind):
    authority = '-1\t'+lineage(ANIMAL)+'\n'
    ref = ANIMAL.copy()
    if kind == 'missing': authority = ''
    if kind == 'identical_duplicate': authority *= 2
    if kind == 'conflicting_duplicate': authority += '-1\t'+lineage(ANIMAL[:5]+['Other', 'Other species'])+'\n'
    if kind == 'contradictory_title': ref[4] = 'OtherFamily'
    if kind == 'completeness_only': authority = '-1\t'+lineage(ANIMAL[:5]+['NA', 'NA'])+'\n'
    _, env = fixture(tmp_path, authority=authority, reference={('COI', '-1'): ref})
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['status'] == ('ASSIGNED' if kind in ('identical_duplicate', 'completeness_only') else 'REFERENCE_UNRESOLVED')
    if kind == 'conflicting_duplicate':
        assert 'conflicting_marker_lineages' in row['read_reason']
    if kind == 'completeness_only':
        assert row['depth'] == '4'
        assert 'completeness_only' in row['read_reason']
        assert oracle_parse(row['lineage'])[5:] == ['NA', 'NA']


@pytest.mark.parametrize('same_taxid,depth', [(True, 6), (False, 6), (False, 5), (False, 4), (False, 3), (False, -1)])
def test_read_tie_lca_oracle(tmp_path, same_taxid, depth):
    other = ANIMAL.copy()
    if depth < 6:
        for i in range(depth+1, 7): other[i] = f'Other{i}'
    defs = {'COI': {'-1': ANIMAL, '-2': other}}
    cfg, env = fixture(tmp_path, [('a', 'COI', ['-1', '-1' if same_taxid else '-2'], 99)], defs)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    expected = ANIMAL if same_taxid else oracle_lca([ANIMAL, other])
    assert oracle_parse(row['lineage']) == expected
    assert row['status'] == ('ASSIGNED' if same_taxid else 'REFERENCE_INCONSISTENT' if depth == -1 else 'AMBIGUOUS_TIE')
    assert row['taxid'] == ('-1' if same_taxid else 'NA')


@pytest.mark.parametrize('counts', [(3, 1, 0), (2, 2, 0), (2, 2, 2)])
def test_projection_voting_oracle(tmp_path, counts):
    defs = {'COI': {f'-{i+1}': ANIMAL[:6]+[f'Musca species {i}'] for i in range(3)}}
    assignments, votes = [], []
    for i, count in enumerate(counts):
        for j in range(count):
            tax = f'-{i+1}'
            assignments.append((f'r{i}_{j}', 'COI', [tax], 99))
            votes.append((tax, defs['COI'][tax]))
    _, env = fixture(tmp_path, assignments, defs)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    expected_status, expected_lineage = oracle_vote(votes)
    rows = sidecar(tmp_path)
    assert {r['status'] for r in rows} == {expected_status}
    assert all(oracle_parse(r['lineage']) == expected_lineage for r in rows)
    assert {int(r['votes']) for r in rows} == {sum(counts)}


def test_real_minus_1156_collision_and_zero_rep_projection(tmp_path):
    coi = ['Metazoa', 'Arthropoda', 'Arachnida', 'Araneae', 'Salticidae', 'Thyene', 'Thyene coccineovittata']
    its = ['Viridiplantae', 'Streptophyta', 'Jungermanniopsida', 'Porellales', 'Porellaceae', 'Porella', 'Porella arborisvitae']
    defs = {'COI': {'-1156': coi}, 'ITS2': {'-1156': its}}
    _, env = fixture(tmp_path, [('a', 'COI', ['-1156'], 99), ('b', 'ITS2', ['-1156'], 99)], defs)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    rows = {r['marker']: r for r in sidecar(tmp_path)}
    assert oracle_parse(rows['COI']['lineage']) == coi
    assert oracle_parse(rows['ITS2']['lineage']) == its
    assert rows['COI']['stable_key'].startswith('COI|')
    assert rows['ITS2']['stable_key'] == 'NA'
    cp = command(['perl', ROOT/'bin/blast_assigned_otu_keys.pl', tmp_path/'status.tsv', tmp_path/'keys', '--persistent'], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    assert (tmp_path/'keys').read_text().splitlines() == [rows['COI']['stable_key']]


@pytest.mark.parametrize('tie', [False, True])
def test_wrong_kingdom_is_not_no_hit(tmp_path, tie):
    _, env = fixture(tmp_path, [('a', 'COI', ['-1', '-2'] if tie else ['-2'], 99)], {'COI': {'-1': ANIMAL, '-2': ANIMAL}}, reference={('COI', '-2'): PLANT})
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['status'] == row['read_status'] == 'REFERENCE_INCONSISTENT'
    assert row['votes'] == '0'


def test_aliases_no_hit_and_one_vote(tmp_path):
    assignments = [('a|COI|hac', 'COI', ['-1'], 99), ('a|COI|sup', 'COI', ['-1'], 99), ('b', 'COI', None, 99)]
    _, env = fixture(tmp_path, assignments)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    rows = sidecar(tmp_path)
    assert {r['member_count'] for r in rows} == {'2'}
    assert {r['votes'] for r in rows} == {'1'}
    assert json.loads(rows[0]['status_counts']) == {'ASSIGNED': 1, 'NO_HIT': 1}


@pytest.mark.parametrize('defect', ['missing_evidence', 'stale_evidence', 'stale_memtax', 'partial_evidence', 'unequal_tie', 'wrong_query_marker'])
def test_sealed_handoff_rejection_preserves_previous_output(tmp_path, defect):
    cfg, env = fixture(tmp_path, [('a', 'COI', ['-1', '-2'], 99)], {'COI': {'-1': ANIMAL, '-2': ANIMAL}})
    target = cfg['targets'][0]
    path = Path(target['memtax'] if defect == 'stale_memtax' else target['evidence'])
    text = path.read_text()
    if defect == 'missing_evidence': path.unlink()
    elif defect.startswith('stale_'):
        lines = text.splitlines(); parts = lines[0].split('\t'); parts[-1] = 'f'*64; lines[0] = '\t'.join(parts); path.write_text('\n'.join(lines)+'\n')
    elif defect == 'partial_evidence': path.write_text('\n'.join(text.splitlines()[:-1])+'\n')
    else:
        lines = text.splitlines(); sig = lines[0].split('\t')[-1]; rows = lines[1:-1]
        if defect == 'unequal_tie':
            v = rows[1].split('\t'); v[7] = '201'; rows[1] = '\t'.join(v)
        else: rows = [r.replace('|COI|', '|ITS2|') for r in rows]
        path.write_text(seal('EVIDENCE', sig, rows))
    (tmp_path/'status.tsv').write_text('old sentinel\n')
    cp = run_fixture(tmp_path, env)
    assert cp.returncode != 0
    assert (tmp_path/'status.tsv').read_text() == 'old sentinel\n'


@pytest.mark.parametrize('damage', ['status', 'depth', 'count', 'marker', 'empty_rank', 'duplicate', 'partial'])
def test_sidecar_consumers_fail_closed_and_atomic(tmp_path, damage):
    _, env = fixture(tmp_path)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    lines = (tmp_path/'status.tsv').read_text().splitlines()
    names = lines[1].split('\t')[1:]
    rows = lines[2:-1]
    if damage == 'partial':
        lines = lines[:-1]
    else:
        v = rows[0].split('\t')
        field, value = {'status': ('status', 'ASSINGED'), 'depth': ('depth', '5'), 'count': ('votes', '2'), 'marker': ('marker', 'ITS2'), 'empty_rank': ('lineage', lineage(ANIMAL).replace('g__Musca', 'g__')), 'duplicate': ('status', 'ASSIGNED')}[damage]
        v[names.index(field)] = value
        rows[0] = '\t'.join(v)
        if damage == 'duplicate': rows.append(rows[0])
        body = '\n'.join(rows)+'\n'
        lines = lines[:2]+rows+[f'#END\t{len(rows)}\t{hashlib.sha256(body.encode()).hexdigest()}']
    (tmp_path/'status.tsv').write_text('\n'.join(lines)+'\n')
    out = tmp_path/'assigned'; out.write_text('old\n')
    cp = command(['perl', ROOT/'bin/blast_assigned_read_ids.pl', tmp_path/'status.tsv', out], tmp_path, env)
    assert cp.returncode != 0
    assert out.read_text() == 'old\n'


def test_reference_validator_new_output_and_source_hashes(tmp_path):
    _, env = fixture(tmp_path)
    contract = tmp_path/'contract.json'
    out = tmp_path/'new-view.json'
    cp = command(['perl', INSTALL, '--write-marker-view', contract, out], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    original = out.read_bytes()
    cp = command(['perl', INSTALL, '--write-marker-view', contract, out], tmp_path, env)
    assert cp.returncode != 0 and out.read_bytes() == original
    cp = command(['perl', INSTALL, '--validate-marker-view', contract, out], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    view = json.loads(original); view['sources'].pop(str(tmp_path/'lineage.tsv'))
    out.write_text(json.dumps(view))
    cp = command(['perl', INSTALL, '--validate-marker-view', contract, out], tmp_path, env)
    assert cp.returncode != 0


@pytest.mark.parametrize('count,expected', [(0, 'NO_HIT'), (1, 'ASSIGNED')])
def test_no_hit_and_cache_only_retry(tmp_path, count, expected):
    cfg, env = fixture(tmp_path, [('a', 'COI', ['-1'] if count else None, 99)])
    a = run_fixture(tmp_path, env)
    assert a.returncode == 0, a.stderr
    old = (tmp_path/'status.tsv').read_bytes()
    (tmp_path/'.r4b-status-interrupted').write_text('partial unrelated temp\n')
    b = run_fixture(tmp_path, env)
    assert b.returncode == 0 and a.stdout == b.stdout
    assert old == (tmp_path/'status.tsv').read_bytes()
    assert sidecar(tmp_path)[0]['status'] == expected
    # Changing configured lineage does not change sealed R4-A evidence; the R4-B
    # generation signature must change and populated contradiction must reject.
    if count:
        Path(cfg['lineage']).write_text('-1\t'+lineage(ANIMAL[:4]+['Other', 'Musca', 'Musca domestica'])+'\n')
        c = run_fixture(tmp_path, env)
        assert c.returncode == 0, c.stderr
        assert (tmp_path/'status.tsv').read_bytes() != old
        assert sidecar(tmp_path)[0]['status'] == 'REFERENCE_UNRESOLVED'


def test_all_prefix_case_variants_and_prefix_similar_names(tmp_path):
    ranks = ANIMAL.copy(); ranks[6] = 'Musca k__unaltered'
    texts = [';'.join(f'{p.upper() if mask & (1 << i) else p}__{ranks[i]}' for i, p in enumerate(PREFIX)) for mask in range(128)]
    source = tmp_path/'prefixes'; source.write_text('\n'.join(texts)+'\n')
    expr = 'while (<>) { chomp; my $r=TaxonUtil::canonical_lineage($_); print TaxonUtil::lineage_text($r),"\\n"; }'
    cp = command(['perl', '-I'+str(ROOT/'bin/lib'), '-MRTBioScan::OTURefineBlastreport', '-e', expr, source], tmp_path)
    assert cp.returncode == 0, cp.stderr
    assert len(set(cp.stdout.splitlines())) == 1
    assert all(oracle_parse(text) == ranks for text in cp.stdout.splitlines())
    # Execute the actual production stripping expression; embedded taxon-name
    # prefixes are names, while rank prefixes at lineage boundaries are syntax.
    main = (ROOT/'main.nf').read_text()
    expr = re.search(r"sed -E '([^']+)' tmp > blast_report_annotated_otu.txt", main)[1].replace('\\\\', '\\')
    row = tmp_path/'public'; row.write_text('q\t-1\t'+lineage(ranks)+'\n')
    cp = command(['sed', '-E', expr, row], tmp_path)
    assert cp.returncode == 0
    assert cp.stdout == 'q\t-1\t'+';'.join(ranks)+'\n'


@pytest.mark.parametrize('pid,depth,tax', [(94, 4, '-3'), (95, 5, '-2'), (98, 6, '-1')])
def test_selected_hsp_tier_and_signed_ancestor_identities(tmp_path, pid, depth, tax):
    defs = {'COI': {'-1': ANIMAL, '-2': ANIMAL[:6]+['NA'], '-3': ANIMAL[:5]+['NA']*2, '-4': ANIMAL[:4]+['NA']*3}}
    mem = {('COI', '-1'): ['-1', '-2', '-3', '-4', 'species'], ('COI', '-2'): ['NA', '-2', '-3', '-4', 'genus'], ('COI', '-3'): ['NA', 'NA', '-3', '-4', 'family'], ('COI', '-4'): ['NA', 'NA', 'NA', '-4', 'order']}
    _, env = fixture(tmp_path, [('a', 'COI', ['-1'], pid)], defs, memories=mem)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert (row['depth'], row['taxid']) == (str(depth), tax)


@pytest.mark.parametrize('resolvable', [False, True])
def test_numeric_synthetic_tie_and_numeric_ancestor(tmp_path, resolvable):
    other = ANIMAL[:6]+['Musca other']
    defs = {'COI': {'-1': ANIMAL, '2': other, '10': ANIMAL[:6]+['NA']}}
    mem = {('COI', '-1'): ['-1', '10', 'NA', 'NA', 'species'], ('COI', '2'): ['2', '10', 'NA', 'NA', 'species'], ('COI', '10'): ['NA', '10', 'NA', 'NA', 'genus']}
    _, env = fixture(tmp_path, [('a', 'COI', ['-1', '2'], 99)], defs, memories=mem)
    if not resolvable:
        values = json.loads((tmp_path/'numeric.json').read_text()); values['2'] = ''
        (tmp_path/'numeric.json').write_text(json.dumps(values))
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['status'] == ('AMBIGUOUS_TIE' if resolvable else 'REFERENCE_UNRESOLVED')
    if resolvable:
        assert row['taxid'] == '10' and row['depth'] == '5'


def test_numeric_direct_assignment(tmp_path):
    _, env = fixture(tmp_path, [('a', 'COI', ['123'], 99)], {'COI': {'123': ANIMAL}})
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    assert sidecar(tmp_path)[0]['taxid'] == '123'


def test_no_common_lineage_oracle_at_lca_boundary(tmp_path):
    expr = 'my $v=JSON::PP->new->decode($ARGV[0]); my @r=map { {ranks=>$_} } @$v; my $r=RTBioScan::OTURefineBlastreport::r4b_lca(\\@r,{});print $r->{status};'
    cp = command(['perl', '-I'+str(ROOT/'bin/lib'), '-MRTBioScan::OTURefineBlastreport', '-e', expr, json.dumps([ANIMAL, PLANT])], tmp_path)
    assert cp.returncode == 0, cp.stderr
    assert oracle_lca([ANIMAL, PLANT]) == ['NA']*7
    assert cp.stdout == 'REFERENCE_UNRESOLVED'


@pytest.mark.parametrize('seed', [0, 1, 143])
def test_reversal_repetition_hash_seed_and_cluster_renumbering(tmp_path, seed):
    defs = {'COI': {'-1': ANIMAL, '-2': ANIMAL[:6]+['Musca other']}}
    _, env = fixture(tmp_path, [('a', 'COI', ['-1'], 99), ('b', 'COI', ['-2'], 99)], defs)
    env.update(PERL_HASH_SEED=str(seed), PERL_PERTURB_KEYS='2')
    first = run_fixture(tmp_path, env)
    assert first.returncode == 0, first.stderr
    expected = sidecar(tmp_path)
    for name in ('lineage.tsv', 'COI.metadata'):
        path=tmp_path/name; rows=path.read_text().splitlines(); path.write_text('\n'.join(reversed(rows+rows))+'\n')
    path=tmp_path/'reads.clstr'; rows=path.read_text().splitlines(); path.write_text('>Cluster 71\n'+'\n'.join(reversed(rows[1:]))+'\n')
    second = run_fixture(tmp_path, env)
    assert second.returncode == 0, second.stderr
    got = sidecar(tmp_path)
    for a, b in zip(expected, got):
        assert a['lineage'] == b['lineage'] and a['taxid'] == b['taxid'] and a['status'] == b['status']
        assert a['stable_key'] == b['stable_key']


def test_stable_key_reuse_never_transfers_display_authority(tmp_path):
    _, env = fixture(tmp_path)
    first = run_fixture(tmp_path, env)
    assert first.returncode == 0, first.stderr
    old = sidecar(tmp_path)[0]['stable_key']
    (tmp_path/'hashes.tsv').write_text('a\t'+'9'*32+'\n')
    second = run_fixture(tmp_path, env)
    assert second.returncode == 0, second.stderr
    new = sidecar(tmp_path)[0]['stable_key']
    assert old != new and new == 'COI|'+'9'*32
    cp = command(['perl', ROOT/'bin/blast_assigned_otu_keys.pl', tmp_path/'status.tsv', tmp_path/'keys', '--persistent'], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    assert (tmp_path/'keys').read_text().splitlines() == [new]
    (tmp_path/'hashes.tsv').write_text('')
    assert run_fixture(tmp_path, env).returncode == 0
    cp = command(['perl', ROOT/'bin/blast_assigned_otu_keys.pl', tmp_path/'status.tsv', tmp_path/'keys', '--persistent'], tmp_path, env)
    assert cp.returncode == 0 and (tmp_path/'keys').read_text() == ''
    assert 'omitted from persistent' in cp.stderr


def test_interrupted_sidecar_publication_preserves_prior_generation(tmp_path):
    import resource
    cfg, env = fixture(tmp_path)
    destination = tmp_path/'status.tsv'; destination.write_text('previous generation\n')
    e=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', NXF_OFFLINE='true', **env)
    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (200, 200))
    cp=subprocess.run(['perl', str(SCRIPT), '--r4b', str(tmp_path/'reads.clstr'), str(tmp_path/'lineage.tsv'), str(tmp_path/'contract.json')], cwd=tmp_path, env=e, capture_output=True, preexec_fn=limit)
    assert cp.returncode != 0
    assert destination.read_text() == 'previous generation\n'
    retry=run_fixture(tmp_path, env)
    assert retry.returncode == 0, retry.stderr
    assert sidecar(tmp_path)[0]['status'] == 'ASSIGNED'


def test_empty_cluster_and_old_state(tmp_path):
    cfg, env=fixture(tmp_path)
    (tmp_path/'reads.clstr').write_text('')
    cp=run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == '#seq_id\ttax_id\tlineage\n'
    assert sidecar(tmp_path) == []
    (tmp_path/'reads.clstr').write_text('>Cluster 0\n0\t100nt, >a|COI|sup... *\n')
    Path(cfg['targets'][0]['memtax']).write_text('-1\t-1\tNA\tNA\n')
    previous=(tmp_path/'status.tsv').read_bytes()
    cp=run_fixture(tmp_path, env)
    assert cp.returncode != 0 and (tmp_path/'status.tsv').read_bytes()==previous


def test_validator_inventory_and_source_hash_contract(tmp_path):
    cfg, env=fixture(tmp_path)
    cp=command(['perl', INSTALL, '--validate-marker-lineages', tmp_path/'contract.json'], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    result=json.loads(cp.stdout)
    expected={str(tmp_path/'contract.json'), cfg['lineage'], cfg['targets'][0]['metadata'], cfg['targets'][0]['seed']}
    assert set(result['sources']) == expected
    assert all(result['sources'][path]['sha256']==hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in expected)
    assert result['output_sha256']==hashlib.sha256(('-1' and 'COI\t-1\t'+lineage(ANIMAL).replace('k__', 'K__', 1)+'\n').encode()).hexdigest()
    metadata=Path(cfg['targets'][0]['metadata'])
    metadata.write_text(metadata.read_text()+metadata.read_text().replace('|taxid|-1', '|taxid|-2').replace('|kraken:taxid|-1', '|kraken:taxid|-2'))
    cp=command(['perl', INSTALL, '--validate-marker-lineages', tmp_path/'contract.json'], tmp_path, env)
    assert cp.returncode == 2
    assert any(v['reason']=='conflicting_accession' for v in json.loads(cp.stdout)['issues'])


# Literal audit examples; production has no ID allowlist.
AUDITED_COI = {'-1326': {'configured': ['Metazoa', 'Arthropoda', 'Arachnida', 'Araneae', 'Trachelidae', 'Meriola', 'NA'],
           'deployed': ['Metazoa', 'Arthropoda', 'Malacostraca', 'Decapoda', 'Xanthidae', 'Meriola', 'NA'],
           'kind': 'conflicting_nonmissing_ranks'},
 '-1329': {'configured': ['Metazoa',
                          'Arthropoda',
                          'Arachnida',
                          'Araneae',
                          'Trachelidae',
                          'Meriola',
                          'Meriola californica'],
           'deployed': ['Metazoa',
                        'Arthropoda',
                        'Malacostraca',
                        'Decapoda',
                        'Xanthidae',
                        'Meriola',
                        'Meriola_californica'],
           'kind': 'conflicting_nonmissing_ranks'},
 '-1330': {'configured': ['Metazoa',
                          'Arthropoda',
                          'Arachnida',
                          'Araneae',
                          'Trachelidae',
                          'Meriola',
                          'Meriola decepta'],
           'deployed': ['Metazoa',
                        'Arthropoda',
                        'Malacostraca',
                        'Decapoda',
                        'Xanthidae',
                        'Meriola',
                        'Meriola_decepta'],
           'kind': 'conflicting_nonmissing_ranks'},
 '-13530': {'configured': ['Metazoa',
                           'Arthropoda',
                           'Insecta',
                           'Hemiptera',
                           'Reduviidae',
                           'NA',
                           'Alcmena spinifex'],
            'deployed': ['Metazoa',
                         'Arthropoda',
                         'Arachnida',
                         'Araneae',
                         'Salticidae',
                         'Alcmena',
                         'Alcmena_spinifex'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-17545': {'configured': ['Metazoa',
                           'Arthropoda',
                           'Insecta',
                           'Hymenoptera',
                           'Ichneumonidae',
                           'Rhabdotus',
                           'Rhabdotus reflexus'],
            'deployed': ['Metazoa',
                         'Mollusca',
                         'Gastropoda',
                         'Stylommatophora',
                         'Bulimulidae',
                         'Rhabdotus',
                         'Rhabdotus_reflexus'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-22356': {'configured': ['Metazoa', 'Arthropoda', 'Insecta', 'Lepidoptera', 'Erebidae', 'Xenosoma', 'NA'],
            'deployed': ['Metazoa',
                         'Arthropoda',
                         'Diplopoda',
                         'Polydesmida',
                         'Paradoxosomatidae',
                         'Xenosoma',
                         'NA'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-22357': {'configured': ['Metazoa',
                           'Arthropoda',
                           'Insecta',
                           'Lepidoptera',
                           'Erebidae',
                           'Xenosoma',
                           'Xenosoma flaviceps'],
            'deployed': ['Metazoa',
                         'Arthropoda',
                         'Diplopoda',
                         'Polydesmida',
                         'Paradoxosomatidae',
                         'Xenosoma',
                         'Xenosoma_flaviceps'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-37821': {'configured': ['Metazoa',
                           'Arthropoda',
                           'Insecta',
                           'Psocoptera',
                           'Psocidae',
                           'Cyclotus',
                           'Cyclotus taivanus'],
            'deployed': ['Metazoa',
                         'Mollusca',
                         'Gastropoda',
                         'Architaenioglossa',
                         'Cyclophoridae',
                         'Cyclotus',
                         'Cyclotus_taivanus'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-39035': {'configured': None,
            'deployed': ['Metazoa', 'Mollusca', 'Cephalopoda', 'Sepiida', 'Sepiolidae', 'Rossia', 'NA'],
            'kind': 'missing_configured_lineage'},
 '-40319': {'configured': ['Metazoa',
                           'Cycliophora',
                           'Eucycliophora',
                           'Symbiida',
                           'Symbiidae',
                           'Symbion',
                           'NA'],
            'deployed': ['Metazoa', 'Cycliophora', 'NA', 'NA', 'NA', 'Symbion', 'NA'],
            'kind': 'completeness_only'},
 '-40320': {'configured': ['Metazoa',
                           'Cycliophora',
                           'Eucycliophora',
                           'Symbiida',
                           'Symbiidae',
                           'Symbion',
                           'Symbion americanus'],
            'deployed': ['Metazoa', 'Cycliophora', 'NA', 'NA', 'NA', 'Symbion', 'Symbion_americanus'],
            'kind': 'completeness_only'},
 '-40321': {'configured': ['Metazoa',
                           'Cycliophora',
                           'Eucycliophora',
                           'Symbiida',
                           'Symbiidae',
                           'Symbion',
                           'Symbion pandora'],
            'deployed': ['Metazoa', 'Cycliophora', 'NA', 'NA', 'NA', 'Symbion', 'Symbion_pandora'],
            'kind': 'completeness_only'},
 '-41429': {'configured': ['Metazoa',
                           'Mollusca',
                           'Gastropoda',
                           'Stylommatophora',
                           'Sagdidae',
                           'Psephurus',
                           'Psephurus gladius'],
            'deployed': ['Metazoa',
                         'Chordata',
                         'Actinopteri',
                         'Acipenseriformes',
                         'Polyodontidae',
                         'Psephurus',
                         'Psephurus_gladius'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-41496': {'configured': ['Metazoa',
                           'Mollusca',
                           'Polyplacophora',
                           'Chitonida',
                           'Chitonidae',
                           'Lucilina',
                           'NA'],
            'deployed': ['Metazoa', 'Mollusca', 'Gastropoda', 'NA', 'Triphoridae', 'Lucilina', 'NA'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-41576': {'configured': ['Metazoa',
                           'Placozoa',
                           'Polyplacotomia',
                           'Polyplacotomea',
                           'Polyplacotomidae',
                           'Polyplacotoma',
                           'Polyplacotoma mediterranea'],
            'deployed': ['Metazoa',
                         'Placozoa',
                         'NA',
                         'NA',
                         'NA',
                         'Polyplacotoma',
                         'Polyplacotoma_mediterranea'],
            'kind': 'completeness_only'},
 '-41581': {'configured': ['Metazoa',
                           'Platyhelminthes',
                           'Rhabditophora',
                           'Rhabdocoela',
                           'Trigonostomidae',
                           'Trigonostomum',
                           'Trigonostomum mucoreum'],
            'deployed': ['Metazoa',
                         'Arthropoda',
                         'Insecta',
                         'Coleoptera',
                         'Scarabaeidae',
                         'Trigonostomum',
                         'Trigonostomum_mucoreum'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-41592': {'configured': ['Metazoa',
                           'Platyhelminthes',
                           'Trematoda',
                           'Azygiida',
                           'Hemiuridae',
                           'Cameronia',
                           'Cameronia nisari'],
            'deployed': ['Metazoa',
                         'Nematoda',
                         'Chromadorea',
                         'Rhabditida',
                         'Thelastomatidae',
                         'Cameronia',
                         'Cameronia_nisari'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-41613': {'configured': ['Metazoa',
                           'Porifera',
                           'Hexactinellida',
                           'Lyssacinosida',
                           'Euplectellidae',
                           'Caledoniella',
                           'NA'],
            'deployed': ['Metazoa',
                         'Mollusca',
                         'Gastropoda',
                         'Littorinimorpha',
                         'Vanikoridae',
                         'Caledoniella',
                         'NA'],
            'kind': 'conflicting_nonmissing_ranks'},
 '-41668': {'configured': None,
            'deployed': ['Metazoa',
                         'Mollusca',
                         'Bivalvia',
                         'Solemyida',
                         'Solemyidae',
                         'Solemya',
                         'Solemya_velum'],
            'kind': 'missing_configured_lineage'},
 '-41669': {'configured': None,
            'deployed': ['Metazoa', 'Mollusca', 'Bivalvia', 'Venerida', 'Vesicomyidae', 'Vesicomya', 'NA'],
            'kind': 'missing_configured_lineage'},
 '-41675': {'configured': None,
            'deployed': ['Metazoa',
                         'Arthropoda',
                         'Insecta',
                         'Hemiptera',
                         'Pentatomidae',
                         'Plautia',
                         'Plautia_stali'],
            'kind': 'missing_configured_lineage'}}


@pytest.mark.parametrize('taxid', sorted(AUDITED_COI))
def test_audited_coi_reference_dispositions(tmp_path, taxid):
    example=AUDITED_COI[taxid]
    configured=example['configured']
    ranks=configured or example['deployed']
    authority=taxid+'\t'+lineage(configured)+'\n' if configured else ''
    cfg, env=fixture(tmp_path, [('a', 'COI', [taxid], 99)], {'COI': {taxid:ranks}}, reference={('COI',taxid):example['deployed']}, authority=authority)
    cp=run_fixture(tmp_path,env)
    assert cp.returncode==0,cp.stderr
    row=sidecar(tmp_path)[0]
    if example['kind']=='completeness_only':
        assert row['status']=='ASSIGNED'
        assert oracle_parse(row['lineage'])==configured
        assert 'completeness_only' in row['read_reason']
    else:
        assert row['status']=='REFERENCE_UNRESOLVED'
        assert row['votes']=='0'
        assert taxid in row['read_reason']
    cp=command(['perl',INSTALL,'--validate-marker-lineages',tmp_path/'contract.json'],tmp_path,env)
    assert cp.returncode==(0 if example['kind']=='completeness_only' else 2)


def test_minor_ambiguity_and_rejection_do_not_taint_direct_plurality(tmp_path):
    defs={'COI':{'-1':ANIMAL,'-2':ANIMAL[:6]+['Musca other'],'-3':ANIMAL[:6]+['Musca third']}}
    assignments=[('a','COI',['-1'],99),('b','COI',['-1'],99),('c','COI',['-2','-3'],99),('d','COI',None,99)]
    _,env=fixture(tmp_path,assignments,defs)
    cp=run_fixture(tmp_path,env)
    assert cp.returncode==0,cp.stderr
    row=sidecar(tmp_path)[0]
    assert row['status']=='ASSIGNED' and row['taxid']=='-1'
    assert json.loads(row['status_counts'])=={'ASSIGNED':2,'AMBIGUOUS_TIE':1,'NO_HIT':1}


@pytest.mark.parametrize('mixed_lca',[False,True])
def test_species_genus_competition_and_winning_origin(tmp_path,mixed_lca):
    defs={'COI':{'-1':ANIMAL,'-2':ANIMAL[:6]+['Musca other'],'-3':ANIMAL[:6]+['NA']}}
    mem={('COI','-1'):['-1','-3','NA','NA','species'],('COI','-2'):['-2','-3','NA','NA','species'],('COI','-3'):['NA','-3','NA','NA','genus']}
    assignments=[('a','COI',['-1','-2'] if mixed_lca else ['-1'],99),('b','COI',['-3'],99)]
    _,env=fixture(tmp_path,assignments,defs,memories=mem)
    cp=run_fixture(tmp_path,env)
    assert cp.returncode==0,cp.stderr
    row=sidecar(tmp_path)[0]
    assert row['status']=='AMBIGUOUS_TIE' and row['depth']=='5' and row['taxid']=='-3'


def test_persistent_keys_reject_legacy_unvalidated_evidence(tmp_path):
    source=tmp_path/'legacy'
    source.write_text('#seq_id\ttax_id\tlineage\na|COI|sup|OTUB_0-COI\t-1\t'+lineage(ANIMAL)+'\n')
    out=tmp_path/'keys';out.write_text('prior\n')
    cp=command(['perl',ROOT/'bin/blast_assigned_otu_keys.pl',source,out,'--persistent'],tmp_path)
    assert cp.returncode!=0 and out.read_text()=='prior\n'


def test_round_sidecar_publication_validates_before_replace(tmp_path):
    _,env=fixture(tmp_path)
    assert run_fixture(tmp_path,env).returncode==0
    dest=tmp_path/'persistent.tsv';dest.write_text('prior\n')
    source=tmp_path/'status.tsv';good=source.read_bytes();source.write_bytes(good[:-20])
    cp=command(['perl',SCRIPT,'--publish-status',source,dest],tmp_path,env)
    assert cp.returncode!=0 and dest.read_text()=='prior\n'
    source.write_bytes(good)
    cp=command(['perl',SCRIPT,'--publish-status',source,dest],tmp_path,env)
    assert cp.returncode==0 and dest.read_bytes()==good


@pytest.mark.parametrize('taxid',['123','-123','NA'])
@pytest.mark.parametrize('level',['family','genus','species'])
def test_taxid_alone_never_satisfies_threshold(tmp_path,taxid,level):
    source=tmp_path/'table';source.write_text('#seq_id\ttax_id\tlineage\na|COI|sup|OTUB_0-COI\t'+taxid+'\tNA\n')
    a,u=tmp_path/'assigned',tmp_path/'unassigned'
    for which,out in [('assigned',a),('unassigned',u)]:
        cp=command(['perl',ROOT/f'bin/blast_{which}_read_ids.pl',source,out,'--min-level',level],tmp_path)
        assert cp.returncode==0,cp.stderr
    assert a.read_text()=='' and u.read_text()=='a\n'


def test_installed_pinned_numeric_authority(tmp_path):
    import shutil
    taxonomy = Path(os.environ.get('R4_PINNED_TAXDUMP', ROOT/'db/taxonomy/releases/ncbi-taxdump-2024-06-24'))
    if not shutil.which('taxonkit') or not (taxonomy/'nodes.dmp').is_file():
        pytest.skip('installed TaxonKit and pinned local taxonomy required')
    human = ['Metazoa', 'Chordata', 'Mammalia', 'Primates', 'Hominidae', 'Homo', 'Homo sapiens']
    cfg, env = fixture(tmp_path, [('a', 'COI', ['9606'], 99)], {'COI': {'9606': human}})
    cfg['taxonomy_dir'] = str(taxonomy)
    target = cfg['targets'][0]
    args = [CACHE, '--signature', target['database'], taxonomy] + [f'{k}={target[v]}' for k, v in [('idfam', 'family'), ('idgen', 'genus'), ('idspec', 'species'), ('evalue', 'evalue'), ('maxhsps', 'max_hsps')]] + ['word=50', 'qcov=50', 'target=COI', 'seed='+target['seed']]
    cp = command(args, tmp_path)
    assert cp.returncode == 0, cp.stderr
    signature = cp.stdout.split('\t')[0]
    for kind, key in [('EVIDENCE', 'evidence'), ('MEMTAX', 'memtax')]:
        path = Path(target[key]); rows = path.read_text().splitlines()[1:-1]
        path.write_text(seal(kind, signature, rows))
    (tmp_path/'contract.json').write_text(json.dumps(cfg, sort_keys=True))
    cp = run_fixture(tmp_path, {'PATH': os.environ['PATH']})
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['taxid'] == '9606' and row['status'] == 'ASSIGNED'
    assert oracle_parse(row['lineage']) == human


def test_strict_wrapper_preserves_workload_artifact_schema(tmp_path):
    cfg, env = fixture(tmp_path)
    report = tmp_path/'legacy.txt'; report.write_text('ignored legacy row\n')
    env.update(RTB_R4B_ENABLE='1', RTB_R4B_CONTRACT=str(tmp_path/'contract.json'))
    cp = command(['/bin/bash', ROOT/'bin/otu_refine_blastreport_parallel.sh', report, tmp_path/'reads.clstr', tmp_path/'lineage.tsv', '2'], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    stats = dict(row.split('\t') for row in (tmp_path/'otu_refine_workload_stats.tsv').read_text().splitlines()[1:])
    assert len(stats) == 19
    assert stats['cluster_count'] == stats['cluster_records'] == stats['annotated_rows'] == '1'
    assert stats['annotated_file_bytes'] == str(len(cp.stdout.encode()))
    assert stats['blastreport_rows'] == '1' and stats['shard_count'] == '0'
    assert stats['shard_scheduler_mode'] == 'single_pass'
    for name in ('otu_refine_phase_timings.tsv', 'otu_refine_phase_timings_ms.tsv'):
        assert '\twrapper_total\t' in (tmp_path/name).read_text()


@pytest.mark.parametrize('representative', [False, True])
@pytest.mark.parametrize('level,minimum', [('family', 4), ('genus', 5), ('species', 6)])
@pytest.mark.parametrize('case,depth', [('numeric', 6), ('synthetic', 6), ('family', 4), ('genus', 5), ('order', 3), ('species_lca', 6), ('genus_lca', 5), ('family_lca', 4), ('unresolved', -1), ('inconsistent', -1)])
def test_protection_status_depth_and_key_matrix(tmp_path, representative, level, minimum, case, depth):
    tax = '123' if case == 'numeric' else '-1'
    ranks = ANIMAL[:depth+1]+['NA']*(6-depth) if case in ('family', 'genus', 'order') else ANIMAL
    defs = {'COI': {tax: ranks}}
    tied = [tax]
    if case.endswith('_lca'):
        other = ANIMAL[:depth+1]+[f'Other{i}' for i in range(depth+1, 7)]
        defs['COI']['-2'] = other; tied.append('-2')
    authority = '' if case == 'unresolved' else None
    reference = {('COI', tax): PLANT} if case == 'inconsistent' else None
    _, env = fixture(tmp_path, [('a', 'COI', tied, 99)], defs, authority=authority, reference=reference, representative=representative)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert int(row['depth']) == depth
    expected_status = 'REFERENCE_UNRESOLVED' if case == 'unresolved' else 'REFERENCE_INCONSISTENT' if case == 'inconsistent' else 'AMBIGUOUS_TIE' if case.endswith('_lca') else 'ASSIGNED'
    assert row['status'] == expected_status
    if case.endswith('_lca'):
        assert row['taxid'] == 'NA'
    for which in ('assigned', 'unassigned'):
        cp = command(['perl', ROOT/f'bin/blast_{which}_read_ids.pl', tmp_path/'status.tsv', tmp_path/which, '--min-level', level], tmp_path, env)
        assert cp.returncode == 0, cp.stderr
    eligible = depth >= minimum
    assert (tmp_path/'assigned').read_text().splitlines() == (['a'] if eligible else [])
    assert (tmp_path/'unassigned').read_text().splitlines() == ([] if eligible else ['a'])
    cp = command(['perl', ROOT/'bin/blast_assigned_otu_keys.pl', tmp_path/'status.tsv', tmp_path/'keys', '--persistent', '--min-level', level], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    assert (tmp_path/'keys').read_text().splitlines() == ([row['stable_key']] if representative and eligible else [])


def test_tie_evidence_reversal_and_memtax_seed_rejection(tmp_path):
    defs = {'COI': {'-1': ANIMAL, '-2': ANIMAL[:6]+['Musca other']}}
    cfg, env = fixture(tmp_path, [('a', 'COI', ['-1', '-2'], 99)], defs)
    assert run_fixture(tmp_path, env).returncode == 0
    before = sidecar(tmp_path)[0]
    target = cfg['targets'][0]
    p = Path(target['evidence']); lines = p.read_text().splitlines()
    p.write_text(seal('EVIDENCE', lines[0].split('\t')[-1], list(reversed(lines[1:-1]))))
    assert run_fixture(tmp_path, env).returncode == 0
    after = sidecar(tmp_path)[0]
    for name in ('status', 'lineage', 'taxid', 'origin', 'source_taxids', 'stable_key'):
        assert before[name] == after[name]
    p = Path(target['memtax']); lines = p.read_text().splitlines(); rows = lines[1:-1]
    v = rows[0].split('\t'); v[1] = '-99'; rows[0] = '\t'.join(v)
    p.write_text(seal('MEMTAX', lines[0].split('\t')[-1], rows))
    old = (tmp_path/'status.tsv').read_bytes()
    cp = run_fixture(tmp_path, env)
    assert cp.returncode != 0 and 'MEMTAX disagrees' in cp.stderr
    assert (tmp_path/'status.tsv').read_bytes() == old


def test_failed_public_write_preserves_previous_sidecar(tmp_path):
    _, env = fixture(tmp_path)
    (tmp_path/'status.tsv').write_text('prior generation\n')
    program = 'open my $out,"<","/dev/null" or die $!;RTBioScan::OTURefineBlastreport::run_marker_contract(@ARGV,$out);'
    cp = command(['perl', '-I'+str(ROOT/'bin/lib'), '-MRTBioScan::OTURefineBlastreport', '-e', program, tmp_path/'reads.clstr', tmp_path/'lineage.tsv', tmp_path/'contract.json'], tmp_path, env)
    assert cp.returncode != 0 and 'write annotated output' in cp.stderr
    assert (tmp_path/'status.tsv').read_text() == 'prior generation\n'


@pytest.mark.parametrize('damage', ['conflicting', 'missing_depth'])
def test_lca_does_not_retain_unvalidated_numeric_ancestor(tmp_path, damage):
    defs = {'COI': {'-1': ANIMAL, '-2': ANIMAL[:6]+['Musca other'], '10': ANIMAL[:6]+['NA']}}
    mem = {('COI', '-1'): ['-1', '10', 'NA', 'NA', 'species'], ('COI', '-2'): ['-2', '10', 'NA', 'NA', 'species']}
    _, env = fixture(tmp_path, [('a', 'COI', ['-1', '-2'], 99)], defs, memories=mem)
    numeric = json.loads((tmp_path/'numeric.json').read_text())
    numeric['10'] = lineage(ANIMAL[:5]+['OtherGenus', 'NA'] if damage == 'conflicting' else ANIMAL[:5]+['NA', 'NA'])
    (tmp_path/'numeric.json').write_text(json.dumps(numeric))
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['status'] == 'AMBIGUOUS_TIE' and row['taxid'] == 'NA' and row['depth'] == '5'
    assert 'unvalidated_numeric_ancestor:10' in row['read_reason']
    assert oracle_parse(row['lineage']) == ANIMAL[:6]+['NA']


def test_completeness_diagnostics_survive_read_lca(tmp_path):
    ranks = ANIMAL[:5]+['NA', 'NA']
    defs = {'COI': {'-1': ranks, '-2': ranks}}
    _, env = fixture(tmp_path, [('a', 'COI', ['-1', '-2'], 99)], defs, reference={('COI', '-1'): ANIMAL, ('COI', '-2'): ANIMAL[:6]+['Musca other']})
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    row = sidecar(tmp_path)[0]
    assert row['status'] == 'AMBIGUOUS_TIE' and row['depth'] == '4'
    assert '-1:completeness_only' in row['read_reason'] and '-2:completeness_only' in row['read_reason']
    assert oracle_parse(row['lineage']) == ranks


def test_optional_workload_write_failure_is_nonfatal(tmp_path):
    _, env = fixture(tmp_path)
    env.update(RTB_R4B_ENABLE='1', RTB_R4B_CONTRACT=str(tmp_path/'contract.json'), OTU_REFINE_WORKLOAD_STATS_FILE=str(tmp_path/'absent'/'stats.tsv'))
    cp = command(['/bin/bash', ROOT/'bin/otu_refine_blastreport_parallel.sh', tmp_path/'legacy.txt', tmp_path/'reads.clstr', tmp_path/'lineage.tsv'], tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    assert 'unable to publish OTU refinement workload diagnostics' in cp.stderr
    assert sidecar(tmp_path)[0]['status'] == 'ASSIGNED'
    assert cp.stdout.startswith('#seq_id\ttax_id\tlineage\n')


# Gating corrections: the integration path below never uses fixture() or seal()
# to manufacture producer signatures, EVIDENCE, or MEMTAX.
def production_fragment(start, end, bindings):
    text = (ROOT / 'main.nf').read_text()
    text = text[text.index(start):text.index(end, text.index(start))]
    for key, value in bindings.items():
        text = text.replace('${' + key + '}', str(value))
    assert not re.search(r'(?<!\\)\$\{(?:params\.|baseDir|db_dir|taxdb_dir|barcode|qced_reads_nr|round_barcode)', text)
    return text.replace('\\$', '$')


def real_producer_case(tmp_path, seed_mode):
    import random
    import shutil
    import shlex
    tools = {name: shutil.which(name) for name in ('blastn', 'makeblastdb', 'blastdbcmd', 'seqkit', 'taxonkit')}
    if not all(tools.values()):
        pytest.skip('installed BLAST, SeqKit and TaxonKit required for real handoff')
    versions = {}
    for name, path in tools.items():
        cp = command([path, 'version' if name in ('seqkit', 'taxonkit') else '-version'], tmp_path)
        assert cp.returncode == 0, cp.stderr
        versions[name] = {'path': path, 'version': cp.stdout.strip(), 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}
    (tmp_path / 'real-tool-versions.json').write_text(json.dumps(versions, indent=2))
    taxdir = tmp_path / 'taxonomy'
    taxdir.mkdir()
    human = ['Metazoa', 'Chordata', 'Mammalia', 'Primates', 'Hominidae', 'Homo', 'Homo sapiens']
    nodes = [(1, 1, 'no rank', 'root'), (2759, 1, 'superkingdom', 'Eukaryota')]
    parent = 2759
    for tax, rank, name in zip((33208, 7711, 40674, 9443, 9604, 9605, 9606), RANKS, human):
        nodes.append((tax, parent, rank, name))
        parent = tax
    (taxdir / 'nodes.dmp').write_text(''.join('\t|\t'.join(map(str, [tax, parent, rank, '', 0, 0, 1, 0, 0, 0, 0, 0, ''])) + '\t|\n' for tax, parent, rank, _ in nodes))
    (taxdir / 'names.dmp').write_text(''.join(f'{tax}\t|\t{name}\t|\t\t|\tscientific name\t|\n' for tax, _, _, name in nodes))
    for name in ('merged.dmp', 'delnodes.dmp'):
        (taxdir / name).write_text('')
    rng = random.Random(4701)
    sequences = [''.join(rng.choice('ACGT') for _ in range(240)) for _ in range(3)]
    refs = tmp_path / 'reference.fa'
    refs.write_text(f'>opaque_human|kraken:taxid|9606 COI {lineage(human)}\n{sequences[0]}\n'
                    f'>opaque_synthetic|kraken:taxid|-1 COI {lineage(ANIMAL)}\n{sequences[1]}\n')
    cp = command([tools['makeblastdb'], '-in', refs, '-dbtype', 'nucl', '-out', tmp_path / 'db'], tmp_path)
    assert cp.returncode == 0, cp.stderr
    reads = ['human|COI|sup', 'synthetic|COI|sup', 'nohit|COI|sup']
    (tmp_path / 'reads.fa').write_text(''.join(f'>{r}\n{s}\n' for r, s in zip(reads, sequences)))
    (tmp_path / 'reads.clstr').write_text(''.join(f'>Cluster {i}\n0\t240nt, >{r}... *\n' for i, r in enumerate(reads)))
    (tmp_path / 'hashes.tsv').write_text(''.join(f'{r.split("|")[0]}\t{hashlib.md5(s.encode()).hexdigest()}\n' for r, s in zip(reads, sequences)))
    (tmp_path / 'lineage.tsv').write_text('-1\t' + lineage(ANIMAL) + '\n')
    seed = tmp_path / ('seed with spaces.tsv' if seed_mode == 'spaces' else 'seed.tsv')
    seed.write_text('-1\tNA\tNA\tNA\tspecies\n')
    seed_value = str(seed) if seed_mode in ('enabled', 'spaces') else ('null' if seed_mode == 'null' else '')
    bindings = {'baseDir': ROOT, 'db_dir': str(tmp_path) + '/', 'taxdb_dir': taxdir,
                'barcode': 'B01', 'round_barcode': 'R01', 'qced_reads_nr': tmp_path / 'reads.clstr',
                'params.target_taxa': 'Metazoa', 'params.blast_id_family': 90,
                'params.blast_id_genus': 95, 'params.blast_id_spec': 98,
                'params.nonncbi_memtax': seed_value, 'params.nonncbi_id2lineage_target': 'unused',
                'params.blast_evalue': '1e-5', 'params.blast_max_hsps': 1}
    # The lineage argument in the unchanged main.nf call is baseDir/relative.
    bindings['params.nonncbi_id2lineage_target'] = os.path.relpath(tmp_path / 'lineage.tsv', ROOT)
    values = {'BIN_DIR': ROOT / 'bin', 'BLAST_INPUT_FASTA': tmp_path / 'reads.fa',
              'STATE_DIR': tmp_path / 'state', 'ROUND_DIR': tmp_path / 'round', 'THREADS': '1',
              'OTU_HASH_MAP_STATE': tmp_path / 'hashes.tsv', 'TAXONKIT_DB': taxdir,
              '_p_targets': 'COI', '_p_blast_db_specs': 'db', '_p_nonncbi_memtax': seed_value,
              '_p_blast_id_family': '90', '_p_blast_id_genus': '95', '_p_blast_id_spec': '98',
              'OTU_REFINE_PHASE_TIMINGS_FILE': 'phase.tsv', 'OTU_REFINE_PHASE_TIMINGS_MS_FILE': 'phase-ms.tsv',
              'OTU_REFINE_WORKLOAD_STATS_FILE': 'workload.tsv'}
    header = 'set -euo pipefail\nunset RTB_R4B_CONTRACT\nnow_ms() { echo 0; }\nappend_otu_refine_breakdown() { :; }\n'
    header += ''.join(f'export {key}={shlex.quote(str(value))}\n' for key, value in values.items())
    producer = production_fragment('"\\$BIN_DIR/blast_otu_pretax.sh"', '\n\t\t_t_per_target_blast_end=', bindings)
    consumer = production_fragment('    export RTB_R4B_ENABLE=1', '\n\t    # Ensure OTU tokens', bindings)
    (tmp_path / 'producer.sh').write_text(header + producer + '\n')
    (tmp_path / 'consumer.sh').write_text(header + consumer + '\n')
    for name in ('producer', 'consumer'):
        cp = command(['/bin/bash', '-n', tmp_path / (name + '.sh')], tmp_path)
        assert cp.returncode == 0, cp.stderr
    return {'human': human, 'seed': seed, 'mode': seed_mode}


def run_real_script(tmp_path, name):
    cp = command(['/bin/bash', tmp_path / (name + '.sh')], tmp_path)
    with (tmp_path / (name + '.log')).open('a') as out:
        out.write(cp.stdout + cp.stderr)
    return cp


def assert_real_assignments(tmp_path, enabled, human):
    rows = sidecar_at(tmp_path / 'blast_otu_taxonomy_v1.tsv')
    assert [row['status'] for row in rows] == ['ASSIGNED', 'ASSIGNED' if enabled else 'REFERENCE_UNRESOLVED', 'NO_HIT']
    assert rows[0]['taxid'] == '9606' and oracle_parse(rows[0]['lineage']) == human
    assert rows[1]['taxid'] == ('-1' if enabled else 'NA')
    assert rows[2]['lineage'] == lineage(['NA'] * 7, upper=False).replace('k__', 'K__', 1)
    return rows


def sidecar_at(path):
    lines = path.read_text().splitlines()
    names = lines[1].split('\t')[1:]
    body = ''.join(row + '\n' for row in lines[2:-1])
    assert lines[-1] == f'#END\t{len(lines)-3}\t{hashlib.sha256(body.encode()).hexdigest()}'
    return [dict(zip(names, row.split('\t'))) for row in lines[2:-1]]


@pytest.mark.parametrize('seed_mode', ['enabled', 'spaces', 'null', 'empty'])
def test_real_r4a_producer_main_bindings_and_stale_rejection(tmp_path, seed_mode):
    case = real_producer_case(tmp_path, seed_mode)
    cp = run_real_script(tmp_path, 'producer')
    assert cp.returncode == 0, cp.stderr
    evidence = tmp_path / 'state/otu_blast_evidence_COI.tsv'
    memory = tmp_path / 'state/memtax1.txt'
    assert evidence.read_text().startswith('#RTB-R4-EVIDENCE\t1\t')
    assert memory.read_text().startswith('#RTB-R4-MEMTAX\t2\t')
    assert evidence.read_text().splitlines()[0].split('\t')[2] == memory.read_text().splitlines()[0].split('\t')[2]
    cp = run_real_script(tmp_path, 'consumer')
    assert cp.returncode == 0, cp.stderr
    assert_real_assignments(tmp_path, seed_mode in ('enabled', 'spaces'), case['human'])
    public = (tmp_path / 'blast_report_annotated_otu.txt').read_bytes()
    status = (tmp_path / 'blast_otu_taxonomy_v1.tsv').read_bytes()
    sealed = evidence.read_bytes(), memory.read_bytes()
    # Recompute/no-new and genuine producer cache-only retry converge bytewise.
    for name in ('consumer', 'producer', 'consumer'):
        cp = run_real_script(tmp_path, name)
        assert cp.returncode == 0, cp.stderr
    assert (evidence.read_bytes(), memory.read_bytes()) == sealed
    assert (tmp_path / 'B01_preblast_new1.fasta').read_bytes() == b''
    assert (tmp_path / 'blast_report_annotated_otu.txt').read_bytes() == public
    assert (tmp_path / 'blast_otu_taxonomy_v1.tsv').read_bytes() == status
    script = tmp_path / 'consumer.sh'
    original = script.read_text()
    script.write_text(original.replace('export RTB_R4B_EVALUE="1e-5"', 'export RTB_R4B_EVALUE="1e-4"'))
    cp = run_real_script(tmp_path, 'consumer')
    assert cp.returncode != 0 and 'stale R4-A evidence' in cp.stderr
    script.write_text(original)
    cp = run_real_script(tmp_path, 'consumer')
    assert cp.returncode == 0, cp.stderr
    assert (tmp_path / 'blast_otu_taxonomy_v1.tsv').read_bytes() == status


def reseal_seed_fixture(tmp_path, cfg, env, seed):
    t = cfg['targets'][0]
    args = [CACHE, '--signature', t['database'], cfg['taxonomy_dir']]
    args += [f'{k}={t[v]}' for k, v in [('idfam', 'family'), ('idgen', 'genus'), ('idspec', 'species'), ('evalue', 'evalue'), ('maxhsps', 'max_hsps')]]
    args += ['word=50', 'qcov=50', 'target=COI', 'seed=' + seed]
    cp = command(args, tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    signature = cp.stdout.split('\t')[0]
    for kind, key in [('EVIDENCE', 'evidence'), ('MEMTAX', 'memtax')]:
        path = Path(t[key])
        path.write_text(seal(kind, signature, path.read_text().splitlines()[1:-1]))
    return signature


@pytest.mark.parametrize('seed_mode', ['omitted', 'empty', 'null', 'enabled', 'spaces', 'missing', 'unreadable'])
@pytest.mark.parametrize('surface', ['contract', 'environment'])
def test_seed_configuration_matrix(tmp_path, seed_mode, surface):
    import shutil
    cfg, env = fixture(tmp_path, [('a', 'COI', ['9606'], 99)], {'COI': {'9606': ANIMAL}})
    t = cfg['targets'][0]
    seed = Path(t['seed'])
    if seed_mode == 'spaces':
        new = tmp_path / 'seed with spaces.tsv'
        seed.rename(new)
        seed = new
    disabled = seed_mode in ('omitted', 'empty', 'null', 'missing')
    value = 'null' if seed_mode == 'null' else ('' if disabled else str(seed))
    reseal_seed_fixture(tmp_path, cfg, env, value)
    if seed_mode == 'null':
        (tmp_path / 'null').write_text('must never be read as a seed\n')
    if seed_mode == 'missing':
        value = str(tmp_path / 'missing-real-seed.tsv')
    if seed_mode == 'unreadable':
        seed.chmod(0)
        if os.access(seed, os.R_OK):
            seed.chmod(0o644)
            pytest.skip('host can read mode-000 file')
    t['seed'] = value
    if seed_mode == 'omitted':
        del t['seed']
    if surface == 'contract':
        (tmp_path / 'contract.json').write_text(json.dumps(cfg))
        args = ['perl', SCRIPT, '--r4b', tmp_path / 'reads.clstr', tmp_path / 'lineage.tsv', tmp_path / 'contract.json']
    else:
        state = tmp_path / 'state'
        state.mkdir()
        shutil.copy2(t['evidence'], state / 'otu_blast_evidence_COI.tsv')
        shutil.copy2(t['memtax'], state / 'memtax1.txt')
        # Fixture metadata supplier is unrelated to signature generation.
        tool = tmp_path / 'fakebin/blastdbcmd'
        tool.write_text('#!/bin/sh\ncat "' + t['metadata'] + '"\n')
        tool.chmod(0o755)
        env.update(RTB_R4B_TARGETS='COI', RTB_R4B_KINGDOMS='Metazoa', RTB_R4B_DATABASES=t['database'],
                   RTB_R4B_FAMILY='90', RTB_R4B_GENUS='95', RTB_R4B_SPECIES='98', RTB_R4B_EVALUE='1e-5',
                   RTB_R4B_MAX_HSPS='1', RTB_R4B_DB_ROOT='', RTB_R4B_BASE=str(tmp_path),
                   RTB_R4B_STATE=str(state), RTB_R4B_HASH_MAP=cfg['hash_map'], RTB_R4B_SIDECAR=cfg['sidecar'], TAXONKIT_DB=cfg['taxonomy_dir'])
        env['RTB_R4B_SEEDS'] = value
        args = ['perl', SCRIPT, '--r4b', tmp_path / 'reads.clstr', tmp_path / 'lineage.tsv']
        if seed_mode == 'omitted':
            args = ['env', '-u', 'RTB_R4B_SEEDS'] + args
    previous = 'previous complete generation\n'
    Path(cfg['sidecar']).write_text(previous)
    try:
        cp = command(args, tmp_path, env)
    finally:
        if seed_mode == 'unreadable':
            seed.chmod(0o644)
    if seed_mode in ('missing', 'unreadable'):
        assert cp.returncode != 0 and 'missing/unreadable file' in cp.stderr
        assert Path(cfg['sidecar']).read_text() == previous
    else:
        assert cp.returncode == 0, cp.stderr
        assert sidecar(tmp_path)[0]['status'] == 'ASSIGNED'
        first = Path(cfg['sidecar']).read_bytes()
        cp = command(args, tmp_path, env)
        assert cp.returncode == 0, cp.stderr
        assert Path(cfg['sidecar']).read_bytes() == first


def test_real_disabled_enabled_seed_transitions(tmp_path):
    case = real_producer_case(tmp_path, 'null')
    for name in ('producer', 'consumer'):
        cp = run_real_script(tmp_path, name)
        assert cp.returncode == 0, cp.stderr
    evidence = tmp_path / 'state/otu_blast_evidence_COI.tsv'
    disabled = evidence.read_bytes()
    for name in ('producer', 'consumer'):
        p = tmp_path / (name + '.sh')
        p.write_text(p.read_text().replace('_p_nonncbi_memtax=null', '_p_nonncbi_memtax=' + str(case['seed'])).replace('"null"', '"' + str(case['seed']) + '"'))
    cp = run_real_script(tmp_path, 'consumer')
    assert cp.returncode != 0 and 'stale R4-A evidence' in cp.stderr
    for name in ('producer', 'consumer'):
        cp = run_real_script(tmp_path, name)
        assert cp.returncode == 0, cp.stderr
    assert_real_assignments(tmp_path, True, case['human'])
    assert evidence.read_bytes().splitlines()[0] != disabled.splitlines()[0]
    for name in ('producer', 'consumer'):
        p = tmp_path / (name + '.sh')
        p.write_text(p.read_text().replace(str(case['seed']), 'null'))
    cp = run_real_script(tmp_path, 'consumer')
    assert cp.returncode != 0 and 'stale R4-A evidence' in cp.stderr
    for name in ('producer', 'consumer'):
        cp = run_real_script(tmp_path, name)
        assert cp.returncode == 0, cp.stderr
    assert_real_assignments(tmp_path, False, case['human'])
    assert evidence.read_bytes() == disabled


def test_wrong_word_signature_is_rejected(tmp_path):
    cfg, env = fixture(tmp_path)
    t = cfg['targets'][0]
    args = [CACHE, '--signature', t['database'], cfg['taxonomy_dir'], 'idfam=90', 'idgen=95', 'idspec=98', 'evalue=1e-5', 'maxhsps=1', 'word=11', 'qcov=50', 'target=COI', 'seed=' + t['seed']]
    cp = command(args, tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    wrong = cp.stdout.split('\t')[0]
    for kind, key in [('EVIDENCE', 'evidence'), ('MEMTAX', 'memtax')]:
        p = Path(t[key])
        p.write_text(seal(kind, wrong, p.read_text().splitlines()[1:-1]))
    Path(cfg['sidecar']).write_text('old generation\n')
    cp = run_fixture(tmp_path, env)
    assert cp.returncode != 0 and 'stale R4-A evidence' in cp.stderr
    assert Path(cfg['sidecar']).read_text() == 'old generation\n'


def public_boundary_fixture(tmp_path):
    defs = {'-1': ANIMAL, '-2': ANIMAL[:6] + ['Musca other'], '-3': ANIMAL,
            '-4': ANIMAL, '-5': ANIMAL[:5] + ['NA', 'NA'], '-6': ANIMAL[:6] + ['Musca NATALIA']}
    cases = [('direct', ['-1']), ('tie', ['-1', '-2']), ('inconsistent', ['-3']),
             ('unresolved', ['-4']), ('nohit', None), ('partial', ['-5']), ('name', ['-6'])]
    assignments = [(name + '|COI|sup|adapter=sample_A', 'COI', taxes, 99) for name, taxes in cases]
    groups = [[name] for name, _, _, _ in assignments]
    authority = ''.join(tax + '\t' + lineage(ranks) + '\n' for tax, ranks in defs.items() if tax != '-4')
    cfg, env = fixture(tmp_path, assignments, {'COI': defs}, groups, reference={('COI', '-3'): PLANT}, authority=authority)
    cp = run_fixture(tmp_path, env)
    assert cp.returncode == 0, cp.stderr
    return cfg, env, cp.stdout


def test_public_boundary_keeps_internal_status_and_legacy_consumers(tmp_path):
    from tests.test_consensus_recovery_integrity import _run_taxonomy_fixture, _read_status
    cfg, env, public = public_boundary_fixture(tmp_path)
    rows = sidecar(tmp_path)
    assert [r['status'] for r in rows] == ['ASSIGNED', 'AMBIGUOUS_TIE', 'REFERENCE_INCONSISTENT', 'REFERENCE_UNRESOLVED', 'NO_HIT', 'ASSIGNED', 'ASSIGNED']
    public_rows = [line.split('\t') for line in public.splitlines()[1:]]
    missing = lineage(['NA'] * 7).replace('k__', 'K__', 1)
    fallback = 'K__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned'
    for row, visible in zip(rows, public_rows):
        assert len(visible) == 3 and visible[:2] == [row['read_id'], row['taxid']]
        if row['status'] in ('ASSIGNED', 'AMBIGUOUS_TIE'):
            assert visible[2] == row['lineage']
        else:
            assert row['taxid'] == row['read_taxid'] == 'NA'
            assert row['lineage'] == row['read_lineage'] == missing
            assert row['depth'] == row['read_depth'] == '-1'
            assert visible[2] == fallback
    assert 'Musca NATALIA' in public and 'g__NA;s__NA' in public
    # Exact legacy parser behavior must remain unchanged, including arbitrary prefixes.
    cp = command(['perl', '-e', 'require $ARGV[0]; print TaxonUtil::is_assigned_lineage("K__NA;p__NA"),"\\n"; print TaxonUtil::is_assigned_lineage("X__NA"),"\\n";', ROOT / 'bin/lib/taxon_util.pl'], tmp_path)
    assert cp.returncode == 0 and cp.stdout == '1\n1\n'
    source = tmp_path / 'public.tsv'
    source.write_text(public)
    (tmp_path / 'sizes.tsv').write_text(''.join(r['otu_id'] + '\t1\n' for r in rows))
    (tmp_path / 'members.tsv').write_text(''.join(r['otu_id'] + '\t' + r['read_id'] + '\n' for r in rows))
    previous = tmp_path / 'initial.tsv'
    previous.write_text('')
    for round_number in (1, 2, 3):
        next_state = tmp_path / f'streak-{round_number}.tsv'
        prune = tmp_path / f'prune-{round_number}.list'
        cp = command(['perl', ROOT / 'bin/otu_unassigned_streak_update.pl', tmp_path / 'sizes.tsv', tmp_path / 'members.tsv', source, previous, 3, 1, 50, prune, tmp_path / f'stats-{round_number}.tsv', next_state, cfg['hash_map']], tmp_path)
        assert cp.returncode == 0, cp.stderr
        observed = dict(line.split('\t') for line in next_state.read_text().splitlines())
        assert observed == {r['stable_key']: str(round_number) for r in rows[2:5]}
        assert prune.read_text().splitlines() == (['inconsistent', 'nohit', 'unresolved'] if round_number == 3 else [])
        previous = next_state
    # Execute the unchanged production cleanup before the real consensus consumer.
    (tmp_path / 'blast_report_annotated_otu.txt').write_text(public)
    cleanup = production_fragment("sed -E 's/(^|[;\t])[KkPpCcOoFfGgSs]__", '\n\t        else', {})
    cleanup = cleanup[cleanup.index("sed -E", 1):].replace('\\\\1', '\\1')
    cp = command(['/bin/bash', '-c', cleanup], tmp_path)
    assert cp.returncode == 0, cp.stderr
    cleaned = (tmp_path / 'blast_report_annotated.txt').read_text()
    cleaned = ''.join(line + '\n' for line in cleaned.splitlines() if not line.startswith('#'))
    declarations = {r['otu_id']: (r['read_id'].rsplit('|', 1)[0], 'ACGT' + 'A' * (i+1) + 'CGT') for i, r in enumerate(rows)}
    fasta = ''.join('>' + read_id + '\n' + sequence + '\n' for read_id, sequence in declarations.values())
    scores = ''.join(r['read_id'].split('|')[0] + '\tsup\t30\n' for r in rows)
    consensus = tmp_path / 'consensus'
    consensus.mkdir()
    from tests.test_consensus_stable_otu_identity_contract import write_current_evidence
    current = write_current_evidence(consensus, declarations)
    cp = _run_taxonomy_fixture(consensus, cleaned, fasta, scores, mode='allow_unassigned', samples='sample_A\n', extra_env=current)
    assert cp.returncode == 0, cp.stderr
    assert _read_status(consensus / 'Consensus/prefilter_status.tsv')['prefilter_output_rows'] == '7'
    meta = (consensus / 'Consensus/sample_A/otu_meta.tsv').read_text()
    assert all(r['otu_id'] in meta for r in rows)
    assert (tmp_path / 'status.tsv').read_text().count('REFERENCE_UNRESOLVED') > 0
    assert _read_status(consensus / 'Consensus/prefilter_status.tsv')['prefilter_metazoa_coi_rows'] == '4'
    required = tmp_path / 'required-consensus'
    required.mkdir()
    current = write_current_evidence(required, declarations)
    cp = _run_taxonomy_fixture(required, cleaned, fasta, scores, mode='required', samples='sample_A\n', extra_env=current)
    assert cp.returncode == 0, cp.stderr
    assert _read_status(required / 'Consensus/prefilter_status.tsv')['prefilter_output_rows'] == '4'
    admitted = {line.split('\t')[0] for line in (required / 'Consensus/sample_A/otu_meta.tsv').read_text().splitlines()}
    assert admitted == {rows[i]['otu_id'] for i in (0, 1, 5, 6)}


def test_seed_signatures_distinguish_enabled_disabled_and_content(tmp_path):
    cfg, env = fixture(tmp_path)
    t = cfg['targets'][0]
    enabled = reseal_seed_fixture(tmp_path, cfg, env, t['seed'])
    disabled = reseal_seed_fixture(tmp_path, cfg, env, 'null')
    empty = reseal_seed_fixture(tmp_path, cfg, env, '')
    assert len({enabled, disabled, empty}) == 3
    Path(t['seed']).write_text('-1\t-2\tNA\tNA\tspecies\n')
    changed = reseal_seed_fixture(tmp_path, cfg, env, t['seed'])
    assert changed not in {enabled, disabled, empty}
