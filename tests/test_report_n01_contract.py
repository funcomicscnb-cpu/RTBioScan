"""Bounded N-01 reporter regression; run sequentially with unittest or pytest.

Oracle provenance: sealed N01_MINIMAL_FIX_DESIGN_20261003T224122Z, patch
SHA256 a44200f54f7d183879004cc41ea8c7cc7e505d6ffca7bc0120f69ef23ed280c1.
No engine/S1 campaign. Mutants use disposable copies; production is never patched.
N01_PERL optionally selects an existing Perl; N01_VALIDATION_ROOT retains evidence.
"""
import copy
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from tests import report_n01_fixture as fixture

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = list(itertools.product(('12', '21'), (0, 42), (0, 2), (1, 2)))
PRE_FIX_COMMIT = 'a514d6d04ab79eb2e275a4a0015d5286273ffc65'
BINDING_SHA = 'e148a04df96f2645ed4b0e7be8c69bcdd63d97dc4cd10fc1fb37c72e37c7d79c'
FLAGS = [('otu-def', 'members.tsv'), ('blast-otu', 'blast_current.tsv'),
         ('blast-otu-cumulative', 'blast.tsv'), ('otu-sizes-round', 'sizes.tsv'),
         ('otu-lock-summary', 'lock.tsv'), ('sample-roster', 'roster.tsv'),
         ('replicate-identity', 'identity.tsv'), ('read-info', 'read_info.tsv'),
         ('on-target', 'on_target.tsv'), ('demult', 'demult.tsv'),
         ('blast-consensus', 'consensus.tsv'), ('round-index-file', 'round_index.tsv'),
         ('blast-unassigned-ids', 'empty.list')]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_binding(value):
    if not isinstance(value, dict):
        raise ValueError('invalid binding object')
    rows = value.get('mapping', [])
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('missing or ambiguous binding')
    if any(not isinstance(row, dict) or set(row) != {'symbolic', 'production'} for row in rows):
        raise ValueError('unexpected binding record keys')
    symbols = [r.get('symbolic') for r in rows]
    targets = [r.get('production') for r in rows]
    if any(not isinstance(x, str) for x in symbols + targets):
        raise ValueError('ambiguous target')
    if len(set(symbols)) != 2 or len(set(targets)) != 2:
        raise ValueError('non-bijective binding')
    if dict(zip(symbols, targets)) != {'S1_1': 'rep_1', 'S1_2': 'rep_2'}:
        raise ValueError('owner mapping mismatch')


def science_projection(obj):
    """Exclude only approved display fields, retaining all arrays and other values."""
    result = copy.deepcopy(obj)
    for source in ('otu', 'consensus'):
        for level in ('family', 'genus', 'species'):
            fields = {'family': ('genus', 'species'), 'genus': ('family', 'species'),
                      'species': ('family', 'genus')}[level]
            for row in result[source]['assignments_by_level'][level]:
                for field in fields:
                    row.pop(field, None)
    return result


def snapshot(directory):
    return {str(p.relative_to(directory)): (sha(p.read_bytes()), p.stat().st_mtime_ns,
                                           p.stat().st_ino)
            for p in directory.rglob('*') if p.is_file()}


def affected_paths(obj):
    """Input-only scope oracle, including filtered populated surfaces."""
    names = []
    for source in ('otu', 'consensus'):
        for level in ('family', 'genus', 'species'):
            surfaces = [(None, f'{source}_assignments_{level}.tsv')]
            if level == 'species':
                count = 'frozen_otu_count' if source == 'otu' else 'consolidated_consensus_count'
                prefix = 'frozen_otu' if source == 'otu' else 'consolidated_consensus'
                surfaces += [(count, f'{prefix}_assignments_species.tsv'),
                             (None, f'{source}_assignments_by_sample_species.tsv')]
            fields = {'family': ('genus', 'species'), 'genus': ('family', 'species'),
                      'species': ('family', 'genus')}[level]
            count = 'otu_count' if source == 'otu' else 'consensus_count'
            for filter_field, name in surfaces:
                rows = [r for r in obj[source]['assignments_by_level'][level]
                        if filter_field is None or r.get(filter_field, 0) > 0]
                needed = any('replicate_reads' in r for r in rows) or any(
                    r.get(count, 0) > 1 and any(r.get(f) not in (None, '') for f in fields)
                    for r in rows)
                if needed:
                    names.extend(('figures/' + name,
                                  'signatures/figures/N01_diagnostic/' + name + '.sig'))
    return sorted(names)


class N01Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binding = (ROOT / 'tests/data/n01_replicate_binding.json').read_bytes()
        assert sha(binding) == BINDING_SHA
        validate_binding(json.loads(binding))
        cls.temporary = tempfile.TemporaryDirectory(prefix='report-n01-')
        cls.directory = Path(os.environ.get('N01_VALIDATION_ROOT', cls.temporary.name)).resolve()
        cls.directory.mkdir(parents=True, exist_ok=True)
        fixture.make_fixture(cls.directory)
        cls.expected = json.loads((cls.directory / 'bundle/EXPECTED_REPLICATE_CELL.json').read_bytes())
        cls.perl = os.environ.get('N01_PERL', shutil.which('perl'))
        assert cls.perl
        cls.sources = {name: (ROOT / 'bin' / name).read_text() for name in
                       ('report_round_json.pl', 'report_run_json.pl', 'report_render.py')}
        cls.legacy = {name: subprocess.check_output(
            ['git', '-C', str(ROOT), 'show', PRE_FIX_COMMIT + ':bin/' + name]).decode()
                      for name in cls.sources}
        cls.legacy_bin = cls.install('legacy', cls.legacy)
        cls.renderer = load('n01_renderer', ROOT / 'bin/report_render.py')
        cls.old_renderer = load('n01_legacy_renderer', cls.legacy_bin / 'report_render.py')
        cls.native = load('n01_history', ROOT / 'bin/report_history_state.py')
        cls.wrapper = cls.directory / 'clock_harness.pl'
        cls.wrapper.write_text('BEGIN { *CORE::GLOBAL::gmtime = sub { CORE::gmtime(1791051395) }; }\n'
                               'my $target = shift @ARGV; do $target; die $@ if $@;\n')
        cls.results = []
        cls.reports = []

    @classmethod
    def tearDownClass(cls):
        for name, source in cls.sources.items():
            assert (ROOT / 'bin' / name).read_text() == source
        (cls.directory / 'validation_records.json').write_text(json.dumps(cls.results, indent=2) + '\n')
        cls.temporary.cleanup()

    @classmethod
    def install(cls, name, replacements):
        directory = cls.directory / 'copies' / name / 'bin'
        directory.mkdir(parents=True)
        for path in (ROOT / 'bin').iterdir():
            target = directory / path.name
            if path.name in replacements:
                target.write_text(replacements[path.name])
            else:
                target.symlink_to(path.resolve())
        return directory

    @classmethod
    def environment(cls, seed, perturb):
        env = dict(os.environ, LC_ALL='C', LANG='C', PERL_HASH_SEED=str(seed),
                   PERL_PERTURB_KEYS=str(perturb), PERL5LIB='', PERLLIB='', PERL5OPT='',
                   PERL_LOCAL_LIB_ROOT='', PYTHONDONTWRITEBYTECODE='1',
                   RTBIOSCAN_CONFIGURED_MARKERS='COI|ITS2')
        env['PATH'] = str(Path(cls.perl).parent) + ':/usr/bin:/bin'
        return env

    @classmethod
    def execute(cls, command, directory, seed, perturb):
        directory.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(command, cwd=directory, env=cls.environment(seed, perturb),
                                capture_output=True, timeout=60)
        (directory / 'stdout').write_bytes(result.stdout)
        (directory / 'stderr').write_bytes(result.stderr)
        cls.results.append({'command': command, 'cwd': str(directory), 'seed': seed,
                            'perturb': perturb, 'exit': result.returncode,
                            'stdout_sha256': sha(result.stdout), 'stderr_sha256': sha(result.stderr)})
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        return result

    @classmethod
    def round_report(cls, script, active, directory, seed, perturb):
        command = [cls.perl, str(script), '--run-id', 'N01_diagnostic', '--state-id', 'stateA',
                   '--barcode', 'RTBioScan', '--round-barcode', 'round3', '--schema-version', '2.1',
                   '--identity-mode', 'collapse', '--targets', 'COI|ITS2', '--target-taxa',
                   'Metazoa|Viridiplantae', '--timestamp-utc', '2026-10-03T18:16:35Z',
                   '--asset-snapshot-policy', 'latest_only', '--out', str(directory / 'round_report.json')]
        for flag, filename in FLAGS:
            command += ['--' + flag, str(active / filename)]
        result = cls.execute(command, directory, seed, perturb)
        return (directory / 'round_report.json').read_bytes(), result.stderr

    @classmethod
    def run_report(cls, script, raw, directory, seed, perturb):
        directory.mkdir(parents=True, exist_ok=True)
        history = directory / 'history.jsonl'
        history.write_bytes(raw)
        command = [cls.perl, str(cls.wrapper), str(script), '--history', str(history),
                   '--out', str(directory / 'run_report.json'), '--run-id', 'N01_diagnostic',
                   '--barcode', 'RTBioScan', '--state-id', 'stateA', '--schema-version', '2.1',
                   '--outdir', 'diagnostic', '--report-rel-path', 'report.html', '--status', 'complete']
        cls.execute(command, directory, seed, perturb)
        return (directory / 'run_report.json').read_bytes()

    def test_01_symbolic_binding_rejections(self):
        value = json.loads((ROOT / 'tests/data/n01_replicate_binding.json').read_bytes())
        validate_binding(value)
        variants = {'invalid_object': None,
                    'invalid_record': {'mapping': [value['mapping'][0], None]},
                    'missing_key': {'mapping': [value['mapping'][0], {'symbolic': 'S1_2'}]},
                    'extra_key': {'mapping': [{**value['mapping'][0], 'alt': 'rep_2'}, value['mapping'][1]]},
                    'absent': {}, 'missing': {'mapping': value['mapping'][:1]},
                    'duplicate_symbol': {'mapping': value['mapping'][:1] * 2},
                    'duplicate_target': {'mapping': [value['mapping'][0], {'symbolic': 'S1_2', 'production': 'rep_1'}]},
                    'ambiguous': {'mapping': [value['mapping'][0], {'symbolic': 'S1_2', 'production': ['rep_2']}]},
                    'swapped': {'mapping': [{'symbolic': 'S1_1', 'production': 'rep_2'}, {'symbolic': 'S1_2', 'production': 'rep_1'}]}}
        for name, mutant in variants.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_binding(mutant)
        self.results.append({'binding_rejections': sorted(variants), 'sealed_mapping_preserved': True})

    def test_02_fresh_domain_round_run_and_preservation(self):
        populations = {'main': None, **fixture.CASES}
        for population, case in populations.items():
            hashes, run_hashes = set(), set()
            for order, seed, perturb, repeat in DOMAIN:
                name = f'o{order}_s{seed}_p{perturb}_r{repeat}'
                active = self.directory / 'active_inputs'
                if case is None:
                    shutil.rmtree(active, ignore_errors=True)
                    shutil.copytree(self.directory / 'inputs' / ('order' + order), active)
                else:
                    fixture.make_inputs(self.directory, case, order, active)
                directory = self.directory / 'domain' / population / name
                raw, _ = self.round_report(ROOT / 'bin/report_round_json.pl', active, directory, seed, perturb)
                obj = json.loads(raw)
                fixture.validate_obj(obj, active, self.expected)
                self.assertEqual(raw.rstrip(b'\n'), json.dumps(obj, sort_keys=True, separators=(',', ':')).encode())
                legacy_raw, _ = self.round_report(self.legacy_bin / 'report_round_json.pl', active,
                                                 self.directory / 'legacy_rounds' / population / name, seed, perturb)
                self.assertEqual(science_projection(obj), science_projection(json.loads(legacy_raw)))
                run_raw = self.run_report(ROOT / 'bin/report_run_json.pl', raw, directory / 'run', seed, perturb)
                run = json.loads(run_raw)
                self.assertEqual(run['run_summary']['otu'], obj['otu'])
                self.assertEqual(run['run_summary']['consensus'], obj['consensus'])
                self.assertEqual((run['state_id'], run['rounds_count']), ('stateA', 1))
                self.assertEqual(run_raw.rstrip(b'\n'), json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
                old_run = json.loads(self.run_report(self.legacy_bin / 'report_run_json.pl', raw,
                                                    directory / 'legacy_run', seed, perturb))
                self.assertEqual(run, old_run)
                hashes.add(sha(raw)); run_hashes.add(sha(run_raw))
                self.reports.append((population, name, raw, legacy_raw))
            self.assertEqual((len(hashes), len(run_hashes)), (1, 1))
            self.results.append({'population': population, 'cases': 16, 'round_hashes': sorted(hashes),
                                 'run_hashes': sorted(run_hashes), 'input_oracle': True,
                                 'complete_science_projection_preserved': True})

    @classmethod
    def invoke(cls, renderer, obj, directory):
        renderer.write_chart_tsvs([copy.deepcopy(obj)], directory / 'figures',
                                  run_id='N01_diagnostic', sig_root=directory / 'signatures')

    @classmethod
    def authority(cls, directory, raw):
        state = directory / 'stateA'
        (state / 'round3').mkdir(parents=True)
        (state / '_state').mkdir()
        (state / 'round3/round_report.json').write_bytes(raw)
        (state / '_state/report_history.jsonl').write_bytes(raw)
        (state / '_state/round_index.tsv').write_text('round3\t3\n')
        return state

    @classmethod
    def context(cls, state):
        _, _, context, issues = cls.native.run_terminals(
            state, state / '_state/round_index.tsv', 'round3', 'N01_diagnostic', 'RTBioScan',
            True, verify_history=True)
        view = cls.native.History(state, 'round3', run_id='N01_diagnostic', barcode='RTBioScan')
        assert view.classify() == 'complete' and not issues
        return {'context': context, 'revision': cls.native.revision(view, context), 'state': view.state}

    def cache_check(self, renderer, obj, directory):
        self.invoke(self.old_renderer, obj, directory)
        before = snapshot(directory)
        self.invoke(renderer, obj, directory)
        after = snapshot(directory)
        changed = sorted(k for k in before if before[k] != after[k])
        self.assertEqual(changed, affected_paths(obj))
        self.assertEqual({k: v[0] for k, v in before.items() if k.startswith('figures/')},
                         {k: v[0] for k, v in after.items() if k.startswith('figures/')})
        self.invoke(renderer, obj, directory)
        self.assertEqual(after, snapshot(directory))
        return changed

    def test_03_cache_lifecycle_and_interruptions(self):
        inventories = {}
        for population, name, raw, legacy_raw in self.reports:
            obj = json.loads(raw)
            directory = self.directory / 'cold' / population / name
            self.invoke(self.renderer, obj, directory)
            before = snapshot(directory)
            self.invoke(self.renderer, obj, directory)
            self.assertEqual(before, snapshot(directory))
            inventory = {k: v[0] for k, v in before.items()}
            self.assertEqual(inventories.setdefault(population, inventory), inventory)
        for population in ('main', *fixture.CASES):
            _, name, raw, legacy_raw = next(r for r in self.reports if r[0] == population)
            obj = json.loads(legacy_raw)
            directory = self.directory / 'warm' / population
            state = self.authority(directory, legacy_raw)
            authority = snapshot(state); context = self.context(state)
            changed = self.cache_check(self.renderer, obj, directory)
            self.assertEqual(authority, snapshot(state)); self.assertEqual(context, self.context(state))
            self.results.append({'warm': population, 'changed_paths': changed, 'once_only': True,
                                 'authority_before_after': context, 'retained_round_sha256': sha(legacy_raw)})
            for mode in ('before_replace', 'before_signature'):
                interrupted = self.directory / 'interrupt' / population / mode
                self.invoke(self.old_renderer, obj, interrupted)
                target = interrupted / 'figures/otu_assignments_species.tsv'
                old = target.read_bytes(); hit = []
                original_write, original_replace = self.renderer.atomic_write_text, os.replace
                def reject_replace(source, destination):
                    if Path(destination) == target:
                        hit.append('replace'); raise OSError('N01 injected replacement interruption')
                    return original_replace(source, destination)
                def reject_signature(path, data):
                    if Path(path).name == target.name + '.sig':
                        hit.append('signature'); raise OSError('N01 injected signature interruption')
                    return original_write(path, data)
                with patch.object(os, 'replace', reject_replace) if mode == 'before_replace' else patch.object(self.renderer, 'atomic_write_text', reject_signature):
                    with self.assertRaises(OSError):
                        self.invoke(self.renderer, obj, interrupted)
                self.assertTrue(hit); self.assertEqual(target.read_bytes(), old)
                self.assertFalse(list(interrupted.rglob('*.tmp')))
                self.invoke(self.renderer, obj, interrupted)
                complete = snapshot(interrupted)
                self.invoke(self.renderer, obj, interrupted)
                self.assertEqual(complete, snapshot(interrupted))
                self.assertEqual(context, self.context(state)); self.assertEqual(authority, snapshot(state))
                self.results.append({'interruption': population + '/' + mode, 'resume_retry': True})
        # Populated filtered surfaces and unrelated scalar-only multi-source/no-display boundary.
        obj = json.loads(self.reports[0][3])
        for source, count in (('otu', 'frozen_otu_count'), ('consensus', 'consolidated_consensus_count')):
            for row in obj[source]['assignments_by_level']['species']:
                row[count] = 1
        self.cache_check(self.renderer, obj, self.directory / 'populated_filtered')
        scalar = copy.deepcopy(obj)
        for source in ('otu', 'consensus'):
            for level in ('family', 'genus', 'species'):
                for row in scalar[source]['assignments_by_level'][level]:
                    row.pop('replicate_reads', None)
                    row['otu_count' if source == 'otu' else 'consensus_count'] = 1
        self.assertEqual(self.cache_check(self.renderer, scalar, self.directory / 'scalar_only'), [])
        missing = copy.deepcopy(scalar)
        for source in ('otu', 'consensus'):
            for level in ('family', 'genus', 'species'):
                for row in missing[source]['assignments_by_level'][level]:
                    row['otu_count' if source == 'otu' else 'consensus_count'] = 2
                    for field in {'family': ('genus', 'species'), 'genus': ('family', 'species'), 'species': ('family', 'genus')}[level]:
                        row[field] = None
        self.assertEqual(self.cache_check(self.renderer, missing, self.directory / 'missing_display'), [])
        changed = copy.deepcopy(obj)
        changed['otu']['assignments_by_level']['species'][0]['reads_total'] += 1
        target = self.directory / 'changed_input'
        self.invoke(self.renderer, obj, target); before = snapshot(target)
        self.invoke(self.renderer, changed, target); after = snapshot(target)
        self.assertNotEqual(before, after)
        self.invoke(self.renderer, changed, target); self.assertEqual(after, snapshot(target))

    @staticmethod
    def instrument(source):
        counter = iter(('otu_id', 'consensus_id'))
        def insert(match):
            field = next(counter)
            return ('      warn "N01_CONTRIBUTOR\\t$level\\t$taxon\\t$sample\\t$marker\\t$row->{'
                    + field + '}\\n" unless exists $groups{$gkey};\n' + match.group(0))
        source, count = re.subn(r'      my \$g = \$groups\{\$gkey\} \|\|= \{', insert, source)
        assert count == 2
        return source

    def trace_ok(self, stderr, active):
        traces = {}
        for line in stderr.decode().splitlines():
            if line.startswith('N01_CONTRIBUTOR\t'):
                _, level, taxon, sample, marker, identity = line.split('\t')
                source = 'consensus' if identity.startswith('cons_') else 'otu'
                traces[(source, level, taxon, sample, marker)] = identity
        return all(traces[(source,) + key] == fixture.oracle(members, key[0])['id']
                   for source in ('otu', 'consensus')
                   for key, members in fixture.input_groups(active, source).items()
                   if key[0] in ('genus', 'species'))

    def test_04_actual_reporter_mutants(self):
        control = self.sources['report_round_json.pl']
        pattern = r'  @rows = sort \{\n.*?  \} @rows;\n\n(?=  my %levels)'
        self.assertEqual(len(re.findall(pattern, control, re.S)), 2)
        def restore(name):
            pattern = r'^sub ' + name + r' \{.*?(?=^sub |\Z)'
            original = re.search(pattern, self.legacy['report_round_json.pl'], re.M | re.S).group()
            return re.sub(pattern, lambda _: original, control, flags=re.M | re.S)
        variants = {
            'instrumented_control': (control, 'species_equal_parents'),
            'first_contributor': (re.sub(pattern, '', control, flags=re.S), 'species_original_conflict'),
            'independent_min_mixed_lineage': (control.replace("$level ne 'species' && defined $row->{genus}", "defined $row->{genus}").replace("($level eq 'family' && ($row->{genus} cmp $g->{genus}) < 0)", "(($level eq 'family' || $level eq 'species') && ($row->{genus} cmp $g->{genus}) < 0)"), 'species_crossed_lineage'),
            'reverse_ancestor_order': (re.sub(pattern, lambda m: m.group().replace('@rows = sort', '@rows = reverse sort'), control, flags=re.S), 'species_original_conflict'),
            'missing_values_first': (control.replace("(!defined($a->{family}) || $a->{family} eq '') <=> (!defined($b->{family}) || $b->{family} eq '')", "(!defined($b->{family}) || $b->{family} eq '') <=> (!defined($a->{family}) || $a->{family} eq '')").replace("(!defined($a->{genus}) || $a->{genus} eq '') <=> (!defined($b->{genus}) || $b->{genus} eq '')", "(!defined($b->{genus}) || $b->{genus} eq '') <=> (!defined($a->{genus}) || $a->{genus} eq '')"), 'species_missing_before_populated'),
            'omitted_source_id_tie': (re.sub(r"\n      \|\| \(\$a->\{(?:otu_id|consensus_id)\} // ''\) cmp \(\$b->\{(?:otu_id|consensus_id)\} // ''\)", '', control), 'species_equal_parents'),
            'reversed_source_id_tie': (control.replace("($a->{otu_id} // '') cmp ($b->{otu_id} // '')", "($b->{otu_id} // '') cmp ($a->{otu_id} // '')").replace("($a->{consensus_id} // '') cmp ($b->{consensus_id} // '')", "($b->{consensus_id} // '') cmp ($a->{consensus_id} // '')"), 'species_equal_parents'),
            'only_otu_reduction_fixed': (restore('collect_consensus_assignments_by_level'), 'species_original_conflict'),
            'only_consensus_reduction_fixed': (restore('collect_otu_assignments_by_level'), 'species_original_conflict'),
            'first_descendant': (control.replace("|| ($level eq 'family' && ($row->{genus} cmp $g->{genus}) < 0)", '').replace("|| (($level eq 'family' || $level eq 'genus') && ($row->{species} cmp $g->{species}) < 0)", ''), 'descendant_crossed'),
            'largest_descendant': (control.replace('cmp $g->{genus}) < 0', 'cmp $g->{genus}) > 0').replace('cmp $g->{species}) < 0', 'cmp $g->{species}) > 0'), 'descendant_crossed'),
            'numeric_taxonomy': (control.replace('$row->{genus} cmp $g->{genus}', '$row->{genus} <=> $g->{genus}').replace('$row->{species} cmp $g->{species}', '$row->{species} <=> $g->{species}'), 'descendant_numeric'),
            'wrong_source_field': (control.replace('$g->{species} = $row->{species} if', '$g->{species} = $row->{genus} if'), 'species_original_conflict'),
            'mutate_representative_value': (control.replace('$g->{species} = $row->{species} if', '$g->{species} = "N01_wrong" if'), 'species_original_conflict'),
            'locale_representative': (control.replace('$row->{genus} cmp $g->{genus}', 'lc($row->{genus}) cmp lc($g->{genus})').replace('$row->{species} cmp $g->{species}', 'lc($row->{species}) cmp lc($g->{species})'), 'descendant_case'),
            'no_round_canonical': (control.replace('->canonical->latin1->encode($obj)', '->latin1->encode($obj)'), 'species_equal_parents'),
            'reverse_round_canonical': (control.replace('->canonical->latin1->encode($obj)', '->canonical->sort_by(sub { $JSON::PP::b cmp $JSON::PP::a })->latin1->encode($obj)'), 'species_equal_parents'),
        }
        extra = {
            'descendant_crossed': {'level': 'family', 'A': ('Shared_Family', 'A', 'Z'), 'B': ('Shared_Family', 'Z', 'A')},
            'descendant_numeric': {'level': 'family', 'A': ('Shared_Family', '2', '2'), 'B': ('Shared_Family', '10', '10')},
            'descendant_case': {'level': 'family', 'A': ('Shared_Family', 'Z', 'Z'), 'B': ('Shared_Family', 'a', 'a')},
        }
        for name, (source, population) in variants.items():
            self.assertTrue(name == 'instrumented_control' or source != control)
            directory = self.install(name, {'report_round_json.pl': self.instrument(source)})
            self.execute([self.perl, '-c', str(directory / 'report_round_json.pl')],
                         self.directory / 'syntax' / name, 0, 0)
            failures = 0
            for order, seed, perturb in itertools.product(('12', '21'), (0, 42), (0, 2)):
                active = self.directory / 'active_inputs'
                fixture.make_inputs(self.directory, extra.get(population, fixture.CASES.get(population)), order, active)
                out = self.directory / 'mutants' / name / f'o{order}_s{seed}_p{perturb}'
                raw, stderr = self.round_report(directory / 'report_round_json.pl', active, out, seed, perturb)
                obj = json.loads(raw)
                try:
                    fixture.validate_obj(obj, active, self.expected)
                    self.assertTrue(self.trace_ok(stderr, active))
                    self.assertEqual(raw.rstrip(b'\n'), json.dumps(obj, sort_keys=True, separators=(',', ':')).encode())
                except AssertionError:
                    failures += 1
                if name == 'instrumented_control':
                    expected, _ = self.round_report(ROOT / 'bin/report_round_json.pl', active, out / 'uninstrumented_control', seed, perturb)
                    self.assertEqual(raw, expected)
            self.assertEqual(failures, 0) if name == 'instrumented_control' else self.assertGreater(failures, 0, name)
            self.results.append({'mutant': name, 'fresh_processes': 8, 'oracle_failures': failures,
                                 'verdict': 'PASS' if name == 'instrumented_control' else 'KILLED'})
        run_source = self.sources['report_run_json.pl'].replace('->canonical->utf8->encode($record)', '->utf8->encode($record)')
        directory = self.install('no_run_canonical', {'report_run_json.pl': run_source})
        self.execute([self.perl, '-c', str(directory / 'report_run_json.pl')], self.directory / 'syntax/run_mutant', 0, 0)
        hashes = set()
        for seed, perturb in itertools.product((0, 42), (0, 2)):
            raw = self.run_report(directory / 'report_run_json.pl', self.reports[0][2], self.directory / 'mutants/no_run_canonical' / f's{seed}_p{perturb}', seed, perturb)
            obj = json.loads(raw)
            self.assertNotEqual(raw.rstrip(b'\n'), json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
            hashes.add(sha(raw))
        self.assertGreater(len(hashes), 1)
        self.results.append({'mutant': 'no_run_canonical', 'fresh_processes': 4, 'verdict': 'KILLED'})

    def test_05_cache_mutants(self):
        control = self.sources['report_render.py']
        variants = {
            'stale_cache_version': control.replace('RENDER_ASSIGNMENTS_N01_SIGNATURE_VERSION = "assignments-n01-v1"', 'RENDER_ASSIGNMENTS_N01_SIGNATURE_VERSION = "figures-v1"'),
            'over_broad_invalidation': control.replace('if n01_affected else RENDER_FIGURES_SIGNATURE_VERSION', 'if True else RENDER_FIGURES_SIGNATURE_VERSION'),
            'every_retry_invalidation': control.replace('if sig_path.exists() and path.exists() and read_signature(sig_path) == sig:', 'if False:'),
            'signature_before_table': control.replace('            _write_tsv(path, header, rows, na_token=na_token, atomic=n01_affected)\n            if path.exists():\n                atomic_write_text(sig_path, sig + "\\n")', '            atomic_write_text(sig_path, sig + "\\n")\n            _write_tsv(path, header, rows, na_token=na_token, atomic=n01_affected)'),
            'nonatomic_assignment': control.replace('atomic=n01_affected', 'atomic=False'),
        }
        obj = json.loads(self.reports[0][3])
        for name, source in variants.items():
            self.assertNotEqual(source, control)
            compile(source, name, 'exec')
            directory = self.install(name, {'report_render.py': source})
            module = load('n01_' + name, directory / 'report_render.py')
            out = self.directory / 'cache_mutants' / name
            if name in ('stale_cache_version', 'over_broad_invalidation', 'every_retry_invalidation'):
                with self.assertRaises(AssertionError):
                    self.cache_check(module, obj, out)
            elif name == 'signature_before_table':
                self.invoke(self.old_renderer, obj, out)
                incoming = copy.deepcopy(obj)
                incoming['otu']['assignments_by_level']['species'][0]['reads_total'] += 1
                target = out / 'figures/otu_assignments_species.tsv'
                original = module.atomic_write_text
                def reject(path, data):
                    if Path(path) == target:
                        raise OSError('N01 injected data interruption')
                    return original(path, data)
                with patch.object(module, 'atomic_write_text', reject), self.assertRaises(OSError):
                    self.invoke(module, incoming, out)
                self.invoke(module, incoming, out)
                reference = self.directory / 'cache_mutants/signature_reference'
                self.invoke(self.renderer, incoming, reference)
                self.assertNotEqual(target.read_bytes(), (reference / 'figures' / target.name).read_bytes())
            else:
                self.invoke(self.old_renderer, obj, out)
                target = out / 'figures/otu_assignments_species.tsv'
                hit = []
                original = os.replace
                def reject(source, destination):
                    if Path(destination) == target:
                        hit.append(True); raise OSError('N01 injected replacement interruption')
                    return original(source, destination)
                with patch.object(os, 'replace', reject):
                    self.invoke(module, obj, out)
                self.assertFalse(hit)
            self.results.append({'mutant': name, 'syntax_valid': True, 'verdict': 'KILLED'})

    def test_06_byte_comparators_and_encoding(self):
        # Extract only actual changed comparator and serializer boundaries, never substitute an oracle.
        control = self.sources['report_round_json.pl']
        blocks = re.findall(r'  @rows = sort \{\n.*?  \} @rows;', control, re.S)
        for source, block in zip(('otu_id', 'consensus_id'), blocks):
            rows = [('"A"', 'OTU_2'), ('"A"', 'OTU_10'), ('"a"', 'lower'), ('"10"', 'num10'),
                    ('"2"', 'num2'), ('"Z"', 'Z'), ('pack("H*","c3a9")', 'utf8'), ('""', 'missing2'), ('undef', 'missing1')]
            code = 'use strict; use warnings; use JSON::PP;\nmy @rows = (\n' + ',\n'.join(
                '{family=>' + family + ',genus=>"X",' + source + '=>"' + name + '"}' for family, name in rows
            ) + '\n);\n' + block + '\nprint JSON::PP->new->canonical->encode([map {$_->{' + source + '}} @rows]);\n'
            path = self.directory / (source + '_comparator.pl'); path.write_text(code)
            result = self.execute([self.perl, str(path)], self.directory / 'comparator' / source, 42, 2)
            self.assertEqual(json.loads(result.stdout), ['num10', 'num2', 'OTU_10', 'OTU_2', 'Z', 'lower', 'utf8', 'missing1', 'missing2'])
        for filename, variable, encoding in (('report_round_json.pl', 'obj', 'latin1'), ('report_run_json.pl', 'record', 'utf8')):
            encoder = re.search(r'JSON::PP->new->canonical->' + encoding + r'->encode\(\$' + variable + r'\)', self.sources[filename]).group()
            # Latin-1 <=ff stays a raw byte; UTF-8 emits the code point encoded; >ff is escaped by latin1.
            code = 'use JSON::PP; my $' + variable + '={z=>chr(0x100),a=>chr(0xe9)}; print ' + encoder + ';'
            path = self.directory / (encoding + '_encoder.pl'); path.write_text(code)
            result = self.execute([self.perl, str(path)], self.directory / 'encoding' / encoding, 0, 0)
            expected = b'{"a":"\xe9","z":"\\u0100"}' if encoding == 'latin1' else '{"a":"é","z":"Ā"}'.encode()
            self.assertEqual(result.stdout, expected)
        self.results.append({'bytewise_comparators': 'both reducers', 'serializer_encoding': 'Latin-1/UTF-8 preserved'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
