"""R4-C oracle: expected ranking/depth/LCA is independent of production Perl."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'bin/consensus_taxonomy_by_hash.pl'
RANKS = 'kingdom phylum class order family genus species'.split()
ANIMAL = ['Metazoa', 'Arthropoda', 'Insecta', 'Diptera', 'Muscidae', 'Musca', 'Musca domestica']
COI_COLLISION = ['Metazoa', 'Arthropoda', 'Arachnida', 'Araneae', 'Salticidae', 'Thyene', 'Thyene coccineovittata']
ITS_COLLISION = ['Viridiplantae', 'Streptophyta', 'Jungermanniopsida', 'Porellales', 'Porellaceae', 'Porella', 'Porella arborisvitae']


def oracle_best(rows):
    def key(row):
        return (-Decimal(row[6]), Decimal(row[3]), -int(row[4]), -Decimal(row[5]))
    per_subject = {}
    for row in rows:
        previous = per_subject.get(row[1])
        if previous is None or (key(row), row) < (key(previous), previous):
            per_subject[row[1]] = row
    best = min(map(key, per_subject.values()))
    return sorted(row[1] for row in per_subject.values() if key(row) == best)


def oracle_lca(lineages):
    result = ['NA'] * 7
    for i in range(7):
        names = {r[i] for r in lineages}
        if len(names - {'NA'}) > 1:
            break
        if len(names) == 1 and 'NA' not in names:
            result[i] = next(iter(names))
    return result


def command(args, cwd, env=None):
    return subprocess.run(list(map(str, args)), cwd=cwd, text=True, capture_output=True,
                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'NXF_OFFLINE': 'true', **(env or {})})


def lineage(ranks):
    return ';'.join(p + '__' + v for p, v in zip('kpcofgs', ranks))


def fixture(path, definitions=None, marker='COI', seed=True, authority=None, metadata=None):
    path.mkdir(parents=True, exist_ok=True)
    definitions = definitions or {'-1': ANIMAL}
    (path / 'tax').mkdir()
    for name in ['nodes.dmp', 'names.dmp', 'merged.dmp', 'delnodes.dmp']:
        (path / 'tax' / name).write_text(name + '\n')
    (path / 'ref.nhr').write_text('bounded reference fixture\n')
    (path / 'seed.tsv').write_text(''.join(f'{tax}\t-11\t-111\t-1111\tspecies\n' for tax in definitions if tax.startswith('-')))
    (path / 'authority.tsv').write_text(authority if authority is not None else ''.join(f'{tax}\t{lineage(r)}\n' for tax, r in definitions.items() if tax.startswith('-')))
    (path / 'metadata.tsv').write_text(metadata if metadata is not None else ''.join(f'acc{tax}\t0\tS{tax}|kraken:taxid|{tax} {marker} {lineage(r)}\n' for tax, r in definitions.items()))
    tools = path / 'tools'
    tools.mkdir()
    (tools / 'blastdbcmd').write_text('#!/bin/sh\ncat "$R4C_METADATA"\n')
    (tools / 'blastdbcmd').chmod(0o755)
    query = f'sample|Consensus1|{marker}|reads-10|OTU=OTUB_1-{marker}'
    sequence = 'ACGT' * 25
    h = hashlib.md5(sequence.encode()).hexdigest()
    (path / 'input.fa').write_text(f'>{query}\n{sequence}\n')
    (path / 'identity.tsv').write_text(f'OTUB_1-{marker}\t{marker}|'+ 'a'*32 + f'\tr|{marker}|sup\t' + 'a'*32 + f'\t{marker}\n')
    config = ['--marker', marker, '--kingdom', next(iter(definitions.values()))[0], '--database', path / 'ref',
              '--taxonomy', path / 'tax', '--seed', path / 'seed.tsv' if seed else 'null', '--lineage', path / 'authority.tsv',
              '--family', '92', '--genus', '95', '--species', '98', '--evalue', '11', '--max_hsps', '50']
    env = {'PATH': str(tools) + os.pathsep + os.environ['PATH'], 'R4C_METADATA': str(path / 'metadata.tsv')}
    return {'path': path, 'query': query, 'hash': h, 'config': config, 'env': env, 'marker': marker}


def hsp(f, tax='-1', subject=None, bits='200', evalue='1e-20', length='100', identity='99', qstart='1', qend='100'):
    return [f['hash'], subject or f'S{tax}|kraken:taxid|{tax}', '0', evalue, length, identity, bits, qstart, qend, '1', '100', '100']


def prepare(f, prefix='run'):
    p = f['path']
    cp = command(['perl', HELPER, 'prepare', '--fasta', p / 'input.fa', '--identity-map', p / 'identity.tsv',
                  '--state', p / 'state.tsv', '--prefix', p / prefix, *f['config']], p, f['env'])
    return cp


def run(f, raw, prefix='run'):
    cp = prepare(f, prefix)
    assert cp.returncode == 0, cp.stderr
    p = f['path']
    (p / 'raw.tsv').write_text(''.join('\t'.join(r) + '\n' for r in raw))
    cp = command(['perl', HELPER, 'complete', '--prefix', p / prefix, '--raw', p / 'raw.tsv'], p, f['env'])
    assert cp.returncode == 0, cp.stderr
    return p / (prefix + '.taxonomy')


def read(path):
    lines = path.read_text().splitlines()
    body = ''.join(x+'\n' for x in lines[1:-1])
    assert lines[-1] == f'#END\t{len(lines)-2}\t{hashlib.sha256(body.encode()).hexdigest()}'
    header = lines[1].split('\t')
    assert len(header) == 23
    return [dict(zip(header, row.split('\t'))) for row in lines[2:-1]]


def project(f, sidecar):
    p = f['path']
    cp = command(['perl', HELPER, 'merge', '--out', p/'merged.tsv', '--full', p/'full.tsv', '--csv', p/'raw.csv', sidecar], p, f['env'])
    assert cp.returncode == 0, cp.stderr
    cp = command(['perl', ROOT/'bin/reporting_blast_consensus.pl', p/'raw.csv', p/'full.tsv', 'R'], p,
                 {**f['env'], 'RTBIOSCAN_CONSENSUS_TAXONOMY': str(p/'merged.tsv')})
    assert cp.returncode == 0, cp.stderr
    lines = (p/'R_blast_consensus_tax_rpt.txt').read_text().splitlines()
    assert all(len(line.split('\t')) == 17 for line in lines)
    return [dict(zip(lines[0].split('\t'), line.split('\t'))) for line in lines[1:]]


@pytest.mark.parametrize('metric,strong,weak', [('bits','201','200'),('evalue','1e-21','1e-20'),('length','100','99'),('identity','99','98')])
def test_hsp_priority_and_order(tmp_path, metric, strong, weak):
    f = fixture(tmp_path)
    first = hsp(f, **{metric: strong})
    second = hsp(f, qstart='2', **{metric: weak})
    # A stronger bitscore must beat a weaker row's better identity.
    if metric == 'bits':
        first[5], second[5] = '96', '100'
    expected = oracle_best([first, second])
    output = []
    for i, rows in enumerate([[first, second], [second, first], [first, second, first]]):
        sidecar = run(f, rows, f'run{i}')
        r = read(sidecar)[0]
        assert [row[1] for row in json.loads(r['candidates_json'])] == expected
        assert Decimal(json.loads(r['candidates_json'])[0][6]) == Decimal(first[6])
        assert Decimal(json.loads(r['candidates_json'])[0][5]) == Decimal(first[5])
        assert [Decimal(json.loads(r['candidates_json'])[0][j]) for j in (3,4,5,6)] == [Decimal(first[j]) for j in (3,4,5,6)]
        output.append(sidecar.read_bytes())
    assert len(set(output)) == 1


@pytest.mark.parametrize('pid,depth,taxid', [('91.999',3,'-1111'),('92',4,'-111'),('94.999',4,'-111'),('95',5,'-11'),('97.999',5,'-11'),('98',6,'-1')])
def test_exact_thresholds(tmp_path, pid, depth, taxid):
    f = fixture(tmp_path)
    r = read(run(f, [hsp(f, identity=pid)]))[0]
    assert int(r['depth']) == depth
    assert r['resolved_taxid'] == taxid
    assert [r[k] for k in RANKS] == ANIMAL[:depth+1]+['NA']*(6-depth)


@pytest.mark.parametrize('marker,ranks', [('COI', COI_COLLISION), ('ITS2', ITS_COLLISION)])
def test_real_signed_collision_is_marker_scoped(tmp_path, marker, ranks):
    authority = '-1156\t'+lineage(COI_COLLISION)+'\n-1156\t'+lineage(ITS_COLLISION)+'\n'
    f = fixture(tmp_path, {'-1156': ranks}, marker=marker, authority=authority)
    sidecar = run(f, [hsp(f, tax='-1156')])
    r = read(sidecar)[0]
    assert r['resolved_taxid'] == '-1156' and [r[k] for k in RANKS] == ranks
    public = project(f, sidecar)
    assert len(public) == 1 and public[0]['taxid'] == '-1156' and public[0]['blast_hit'] == 'S-1156'


@pytest.mark.parametrize('different_rank', [6,5,4,0])
def test_equal_best_lca(tmp_path, different_rank):
    other = ANIMAL.copy()
    for i in range(different_rank, 7):
        other[i] = f'Other{i}'
    # A wrong kingdom is separately inconsistent; ordinary LCA tests use a shared kingdom.
    f = fixture(tmp_path, {'-1': ANIMAL, '-2': other})
    sidecar = run(f, [hsp(f, tax='-2'), hsp(f)])
    r = read(sidecar)[0]
    if different_rank == 0:
        assert r['status'] == 'REFERENCE_INCONSISTENT'
    else:
        assert r['status'] == 'AMBIGUOUS_TIE'
        assert [r[k] for k in RANKS] == oracle_lca([ANIMAL, other])
        assert r['origin'] == 'LCA' and r['candidate_count'] == '2'
    public = project(f, sidecar)
    assert public[0]['taxid'] == 'NA' and public[0]['blast_hit'] == 'NA'


def test_same_taxid_tied_subjects_keep_provenance(tmp_path):
    f = fixture(tmp_path)
    sidecar=run(f, [hsp(f, subject='Z|kraken:taxid|-1'), hsp(f, subject='A|kraken:taxid|-1')])
    r = read(sidecar)[0]
    assert (r['status'], r['origin'], r['resolved_taxid'], r['candidate_count']) == ('ASSIGNED','DIRECT','-1','2')
    assert [x[1] for x in json.loads(r['candidates_json'])] == ['A|kraken:taxid|-1','Z|kraken:taxid|-1']
    public=project(f,sidecar)
    assert len(public)==1 and public[0]['taxid']=='-1' and public[0]['blast_hit']=='NA'


@pytest.mark.parametrize('case,status', [('missing','REFERENCE_UNRESOLVED'),('disabled','REFERENCE_UNRESOLVED'),('contradiction','REFERENCE_INCONSISTENT'),('completeness','REFERENCE_UNRESOLVED'),('kingdom','REFERENCE_INCONSISTENT')])
def test_authority_dispositions(tmp_path, case, status):
    other = ANIMAL.copy()
    kwargs = {}
    if case == 'missing': kwargs['authority'] = ''
    if case == 'disabled': kwargs['seed'] = False
    if case in ('contradiction', 'completeness'):
        other[-1] = 'Other species' if case == 'contradiction' else 'NA'
        kwargs['authority'] = '-1\t'+lineage(ANIMAL)+'\n-1\t'+lineage(other)+'\n'
    if case == 'kingdom':
        other[0] = 'Fungi'
        kwargs['metadata'] = 'acc\t0\tS-1|kraken:taxid|-1 COI '+lineage(other)+'\n'
    f = fixture(tmp_path, **kwargs)
    sidecar = run(f, [hsp(f)])
    assert read(sidecar)[0]['status'] == status
    assert project(f, sidecar)[0]['consensus_kingdom'] == 'Unassigned'


@pytest.mark.parametrize('missing', [4,5,6])
def test_missing_rank_does_not_invent_name(tmp_path, missing):
    ranks = ANIMAL.copy(); ranks[missing] = 'NA'
    f = fixture(tmp_path, {'-1': ranks})
    r = read(run(f, [hsp(f)]))[0]
    expected = max(i for i, name in enumerate(ranks) if name != 'NA')
    assert int(r['depth']) == expected and r[RANKS[missing]] == 'NA'


def test_named_na_substring(tmp_path):
    ranks = ANIMAL.copy();ranks[-1] = 'NATAL species'
    f = fixture(tmp_path, {'-1': ranks})
    assert read(run(f, [hsp(f)]))[0]['depth'] == '6'


def test_no_hit_and_empty_input(tmp_path):
    f = fixture(tmp_path)
    sidecar = run(f, [])
    assert read(sidecar)[0]['status'] == 'NO_HIT'
    assert project(f, sidecar)[0]['taxid'] == 'NA'
    (tmp_path/'input.fa').write_text('')
    assert read(run(f, [], 'empty')) == []


@pytest.mark.parametrize('level', ['family','genus','species'])
def test_consumers_share_actual_depth_and_stable_ownership(tmp_path, level):
    ranks = ANIMAL.copy();ranks[4] = 'NA'
    f = fixture(tmp_path, {'-1': ranks})
    sidecar = run(f, [hsp(f)]); project(f, sidecar)
    cons = tmp_path/'Consensus/sample';(cons/'OriginalReads').mkdir(parents=True)
    (cons/'sample_Merged_Consensus.fasta').write_text((tmp_path/'input.fa').read_text())
    (cons/'OriginalReads/Consensus1_reads.list').write_text('readY|COI|sup\n')
    provenance = tmp_path/'provenance.tsv'
    provenance.write_text('round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\nr1\tsample\tOTUB_1-COI\tConsensus1_sample\t1\n')
    for helper, args, expected in [('consensus_assigned_otu_keys.pl',['--provenance',provenance],['OTUB_1-COI']),('consensus_recovered_reads.pl',['--consensus-dir',tmp_path/'Consensus'],['readY'])]:
        out = tmp_path/(helper+'.out')
        cp = command(['perl',ROOT/'bin'/helper,'--blast-report',tmp_path/'full.tsv','--taxonomy-sidecar',sidecar,'--min-level',level,'--out',out,*args],tmp_path)
        assert cp.returncode == 0, cp.stderr
        assert out.read_text().splitlines() == expected


def test_cache_only_reprojection_and_stale_signature(tmp_path):
    f = fixture(tmp_path)
    first = run(f, [hsp(f)])
    cp = command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',first,'--out',tmp_path/'state.tsv'],tmp_path)
    assert cp.returncode == 0, cp.stderr
    second = run(f, [], 'cached')
    assert (tmp_path/'cached.fasta').read_bytes() == b''
    assert second.read_bytes() == first.read_bytes()
    cp = command(['perl',HELPER,'validate','--input',first,'--expected','0'*64],tmp_path)
    assert cp.returncode != 0 and 'stale' in cp.stderr
    (tmp_path/'ref.nhr').write_text('changed reference content\n')
    assert prepare(f,'stale').returncode == 0
    assert (tmp_path/'stale.fasta').stat().st_size > 0


@pytest.mark.parametrize('damage', ['truncate','checksum','duplicate'])
def test_reject_bad_sidecar_without_replacing_state(tmp_path, damage):
    f = fixture(tmp_path);source = run(f,[hsp(f)])
    good = source.read_bytes();(tmp_path/'state.tsv').write_bytes(good)
    if damage == 'truncate': source.write_bytes(good[:-10])
    elif damage == 'checksum': source.write_bytes(good.replace(b'Musca domestica',b'Musca altered'))
    else:
        rows = good.decode().splitlines();rows.insert(-1,rows[-2]);body=''.join(x+'\n' for x in rows[1:-1]);rows[-1]=f'#END\t{len(rows)-2}\t{hashlib.sha256(body.encode()).hexdigest()}'
        source.write_text('\n'.join(rows)+'\n')
    cp = command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',source,'--out',tmp_path/'state.tsv'],tmp_path)
    assert cp.returncode != 0 and (tmp_path/'state.tsv').read_bytes() == good


def test_conflicting_hsp_duplicate_refused(tmp_path):
    f = fixture(tmp_path);assert prepare(f).returncode == 0
    (tmp_path/'raw.tsv').write_text('\t'.join(hsp(f))+'\n'+'\t'.join(hsp(f,bits='201'))+'\n')
    cp = command(['perl',HELPER,'complete','--prefix',tmp_path/'run','--raw',tmp_path/'raw.tsv'],tmp_path,f['env'])
    assert cp.returncode != 0 and 'conflicting duplicate' in cp.stderr
    assert not (tmp_path/'run.taxonomy').exists()


def test_bold_only_voucher_retained(tmp_path):
    f = fixture(tmp_path/'attribution', {'-1156': ITS_COLLISION}, marker='ITS2')
    sidecar = run(f,[hsp(f,tax='-1156')]);public=project(f,sidecar)
    ns=runpy.run_path(str(ROOT/'tests/test_voucher_export.py'))
    results,_,consensus,tables=ns['make_state'](tmp_path/'voucher')
    ns['write_identity'](results,'run-one',[('sample','ITS2','sample','sample_ITS2')])
    ns['write_fasta'](consensus,'sample',[(f['query'],'ACGT'*25)])
    shutil.copyfile(f['path']/'R_blast_consensus_tax_rpt.txt',tables/'round_blast_consensus_tax_rpt.txt')
    out=tmp_path/'voucher-out';cp=ns['run_export'](results,out)
    assert cp.returncode == 0,cp.stderr
    assert public[0]['taxid']=='-1156'
    assert 'Porella' in (out/'voucher_summary.tsv').read_text()
    assert 'ACGT'*25 in (out/'voucher_sequences.fasta').read_text()


def test_numeric_pinned_lineage_batch(tmp_path):
    f=fixture(tmp_path, {'101':ANIMAL})
    tool=tmp_path/'tools/taxonkit'
    tool.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
ranks=['Metazoa','Arthropoda','Insecta','Diptera','Muscidae','Musca','Musca domestica']
for line in Path(sys.argv[-1]).read_text().splitlines():
    tax=line.split('\\t')[0]
    if sys.argv[1]=='lineage': print(tax+'\\tancestors')
    elif '-t' in sys.argv: print(tax+'\\tancestors\\tids\\tspecies\\tlineage\\t101;11;111;1111')
    else:
        depth={'101':6,'11':5,'111':4,'1111':3}[tax]
        lin=';'.join(p+'__'+(v if i<=depth else '') for i,(p,v) in enumerate(zip('Kpcofgs',ranks)))
        print(tax+'\\tancestors\\t'+lin)
''')
    tool.chmod(0o755)
    sidecar=run(f,[hsp(f,tax='101')])
    assert read(sidecar)[0]['resolved_taxid']=='101'
    assert project(f,sidecar)[0]['taxid']=='101'


def test_hash_seed_determinism_and_reference_order(tmp_path):
    other=ANIMAL.copy();other[-1]='Musca other'
    f=fixture(tmp_path,{'-1':ANIMAL,'-2':other})
    outputs=[]
    for seed in ['0','1','9281']:
        f['env'].update(PERL_HASH_SEED=seed,PERL_PERTURB_KEYS='2')
        outputs.append(project(f,run(f,[hsp(f,tax='-2'),hsp(f)],'run'+seed)))
    assert outputs[0]==outputs[1]==outputs[2]
    for file in ['authority.tsv','metadata.tsv']:
        path=tmp_path/file;path.write_text('\n'.join(reversed(path.read_text().splitlines()))+'\n')
    assert project(f,run(f,[hsp(f),hsp(f,tax='-2')],'reversed'))==outputs[0]


def test_stable_ownership_through_renumbering_and_zero_projection(tmp_path):
    f=fixture(tmp_path);sidecar=run(f,[hsp(f)])
    assert command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',sidecar,'--out',tmp_path/'state.tsv'],tmp_path).returncode==0
    for name in ['input.fa','identity.tsv']:
        p=tmp_path/name;p.write_text(p.read_text().replace('OTUB_1-COI','OTUB_9-COI'))
    renamed=read(run(f,[],'renamed'))[0]
    assert renamed['display_otu_key']=='OTUB_9-COI'
    assert renamed['stable_otu_key']=='COI|'+'a'*32
    (tmp_path/'identity.tsv').write_text('')
    zero=read(run(f,[],'zero'))[0]
    assert zero['stable_otu_key']=='NA' and zero['status']=='ASSIGNED'


@pytest.mark.parametrize('damage', ['malformed_authority','malformed_hsp','partial_hsp'])
def test_bad_inputs_do_not_publish(tmp_path,damage):
    f=fixture(tmp_path)
    if damage=='malformed_authority': (tmp_path/'authority.tsv').write_text('-1\tbad;lineage\n')
    assert prepare(f).returncode==0
    text='\t'.join(hsp(f))+'\n'
    if damage=='malformed_hsp':text=text.replace('\t200\t','\tbad\t')
    if damage=='partial_hsp':text=text[:-1]
    (tmp_path/'raw.tsv').write_text(text)
    cp=command(['perl',HELPER,'complete','--prefix',tmp_path/'run','--raw',tmp_path/'raw.tsv'],tmp_path,f['env'])
    assert cp.returncode!=0 and not (tmp_path/'run.taxonomy').exists()


def test_failed_creation_and_rename_preserve_old_state(tmp_path):
    f=fixture(tmp_path);source=run(f,[hsp(f)])
    missing=tmp_path/'absent/state.tsv'
    cp=command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',source,'--out',missing],tmp_path)
    assert cp.returncode!=0 and not missing.exists()
    destination=tmp_path/'destination';destination.mkdir();(destination/'old').write_text('prior authority\n')
    cp=command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',source,'--out',destination],tmp_path)
    assert cp.returncode!=0 and (destination/'old').read_text()=='prior authority\n'
    out=tmp_path/'state.tsv'
    for _ in range(2):
        cp=command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',source,'--out',out],tmp_path)
        assert cp.returncode==0 and out.read_bytes()==source.read_bytes()


def test_real_blast_equal_best_fixture(tmp_path):
    if not shutil.which('makeblastdb') or not shutil.which('blastn'):
        pytest.skip('local BLAST unavailable; no download')
    other=ANIMAL.copy();other[-1]='Musca other'
    f=fixture(tmp_path,{'-1':ANIMAL,'-2':other})
    # Repeatable high-complexity nucleotide fixture, unrelated to taxonomy oracle.
    import random
    rng=random.Random(1403);seq=''.join(rng.choice('ACGT') for _ in range(250))
    (tmp_path/'input.fa').write_text('>'+f['query']+'\n'+seq+'\n')
    (tmp_path/'reference.fa').write_text(''.join(f'>S{tax}|kraken:taxid|{tax} COI {lineage(ranks)}\n{seq}\n' for tax,ranks in [('-1',ANIMAL),('-2',other)]))
    cp=command(['makeblastdb','-in',tmp_path/'reference.fa','-dbtype','nucl','-out',tmp_path/'ref'],tmp_path)
    assert cp.returncode==0,cp.stderr
    f['env']['PATH']=os.environ['PATH']
    assert prepare(f).returncode==0
    count=command(['perl',HELPER,'target-count','--database',tmp_path/'ref'],tmp_path)
    assert count.returncode==0 and count.stdout.strip()=='2',count.stderr
    cp=command(['blastn','-query',tmp_path/'run.fasta','-db',tmp_path/'ref','-task','megablast','-dust','no',
                '-outfmt','6 qseqid sseqid staxids evalue length pident bitscore qstart qend sstart send qlen',
                '-perc_identity','92','-evalue','11','-max_hsps','50','-max_target_seqs',count.stdout.strip(),'-word_size','50','-qcov_hsp_perc','50'],tmp_path)
    assert cp.returncode==0,cp.stderr
    (tmp_path/'raw.tsv').write_text(cp.stdout)
    cp=command(['perl',HELPER,'complete','--prefix',tmp_path/'run','--raw',tmp_path/'raw.tsv'],tmp_path,f['env'])
    assert cp.returncode==0,cp.stderr
    row=read(tmp_path/'run.taxonomy')[0]
    assert row['status']=='AMBIGUOUS_TIE' and row['depth']=='5' and row['candidate_count']=='2'


def reseal(path, change):
    rows=path.read_text().splitlines();change(rows)
    body=''.join(x+'\n' for x in rows[1:-1])
    rows[-1]=f'#END\t{len(rows)-2}\t{hashlib.sha256(body.encode()).hexdigest()}'
    path.write_text('\n'.join(rows)+'\n')


@pytest.mark.parametrize('damage',['row_signature','header_signature','missing_query','public_alias','conflicting_result'])
def test_publication_rejects_incompatible_generation(tmp_path,damage):
    f=fixture(tmp_path);sidecar=run(f,[hsp(f)]);good=sidecar.read_bytes()
    old=tmp_path/'state.tsv';old.write_bytes(good)
    def change(rows):
        if damage=='header_signature': rows[0]=rows[0].rsplit('\t',1)[0]+'\t'+'0'*64
        elif damage=='missing_query': del rows[2]
        else:
            v=rows[2].split('\t')
            if damage=='row_signature': v[-1]='0'*64
            elif damage=='public_alias': v[0]+='|alias=1';rows.insert(3,rows[2])
            elif damage=='conflicting_result': v[0]=v[0].replace('Consensus1','Consensus2');v[5]='Consensus2_sample';v[15]='Musca conflicting';rows.insert(3,rows[2])
            rows[2]='\t'.join(v)
            rows[2:-1]=sorted(rows[2:-1])
    reseal(sidecar,change)
    cp=command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',sidecar,'--out',old],tmp_path)
    assert cp.returncode!=0 and old.read_bytes()==good


@pytest.mark.parametrize('damage',['signature','missing_query','overlap','length','stale_authority'])
def test_prepared_plan_is_validated(tmp_path,damage):
    f=fixture(tmp_path);assert prepare(f).returncode==0
    if damage=='stale_authority': (tmp_path/'authority.tsv').write_text('-1\t'+lineage(COI_COLLISION)+'\n')
    else:
        def change(rows):
            p=json.loads(rows[1])
            if damage=='signature': p['signature']='0'*64
            elif damage=='missing_query': p['queries']=[]
            elif damage=='overlap': p['cached'][f['hash']]={}
            else: p['new'][f['hash']]=101
            rows[1]=json.dumps(p,sort_keys=True,separators=(',',':'))
        reseal(tmp_path/'run.plan',change)
    (tmp_path/'raw.tsv').write_text('\t'.join(hsp(f))+'\n')
    cp=command(['perl',HELPER,'complete','--prefix',tmp_path/'run','--raw',tmp_path/'raw.tsv'],tmp_path,f['env'])
    assert cp.returncode!=0 and not (tmp_path/'run.taxonomy').exists()


@pytest.mark.parametrize('fault',['create','partial','rename','term_before','term_after'])
def test_atomic_publication_interruption_and_retry(tmp_path,fault):
    import resource
    f=fixture(tmp_path)
    old=run(f,[],'old').read_bytes();source=run(f,[hsp(f)])
    target=tmp_path/'state.tsv';target.write_bytes(old);new=source.read_bytes()
    assert old!=new
    (tmp_path/'R4CPublishFault.pm').write_text(r'''package R4CPublishFault;
BEGIN {
 require File::Temp;
 my $original=\&File::Temp::tempfile;
 no warnings 'redefine';
 *File::Temp::tempfile=sub {
   die "injected create failure\n" if $ENV{R4C_FAULT} eq 'create';
   return $original->(@_);
 };
 *CORE::GLOBAL::rename=sub {
   my ($from,$to)=@_;
   return CORE::rename($from,$to) unless $to eq $ENV{R4C_DEST};
   if ($ENV{R4C_FAULT} eq 'rename') {$!=13;return 0;}
   kill 'TERM',$$ if $ENV{R4C_FAULT} eq 'term_before';
   my $ok=CORE::rename($from,$to);
   kill 'TERM',$$ if $ENV{R4C_FAULT} eq 'term_after';
   return $ok;
 };
}
1;
''')
    args=list(map(str,['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',source,'--out',target]))
    env={**os.environ,'PERL5OPT':'-MR4CPublishFault','PERL5LIB':str(tmp_path),'R4C_FAULT':fault,'R4C_DEST':str(target)}
    def partial(): resource.setrlimit(resource.RLIMIT_FSIZE,(128,128))
    cp=subprocess.run(args,cwd=tmp_path,env=env,capture_output=True,text=True,preexec_fn=partial if fault=='partial' else None)
    assert cp.returncode!=0
    assert target.read_bytes()==(new if fault=='term_after' else old)
    assert command(args,tmp_path).returncode==0
    assert target.read_bytes()==new
    assert command(args,tmp_path).returncode==0 and target.read_bytes()==new


@pytest.mark.parametrize('identity,depth',[('91.999',3),('92',4),('94.999',4),('95',5),('97.999',5),('98',6)])
def test_consumer_threshold_inverses(tmp_path,identity,depth):
    f=fixture(tmp_path);sidecar=run(f,[hsp(f,identity=identity)]);project(f,sidecar)
    cons=tmp_path/'Consensus/sample';(cons/'OriginalReads').mkdir(parents=True)
    (cons/'sample_Merged_Consensus.fasta').write_text((tmp_path/'input.fa').read_text())
    (cons/'OriginalReads/Consensus1_reads.list').write_text('readY|COI|sup\n')
    provenance=tmp_path/'provenance.tsv';provenance.write_text('round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\nr1\tsample\tOTUB_1-COI\tConsensus1_sample\t1\n')
    for minimum,level in [(4,'family'),(5,'genus'),(6,'species')]:
        for helper,args,value in [('consensus_assigned_otu_keys.pl',['--provenance',provenance],'OTUB_1-COI'),('consensus_recovered_reads.pl',['--consensus-dir',tmp_path/'Consensus'],'readY')]:
            out=tmp_path/(helper+'.out')
            cp=command(['perl',ROOT/'bin'/helper,'--blast-report',tmp_path/'full.tsv','--taxonomy-sidecar',sidecar,'--min-level',level,'--out',out,*args],tmp_path)
            assert cp.returncode==0,cp.stderr
            assert out.read_text().strip()==(value if depth>=minimum else '')


def taxdump(path):
    definitions=[(1,1,'no rank','root'),(2,1,'kingdom','Metazoa'),(3,2,'phylum','Arthropoda'),(4,3,'class','Insecta'),(1111,4,'order','Diptera'),(111,1111,'family','Muscidae'),(11,111,'genus','Musca'),(101,11,'species','Musca domestica'),(102,11,'species','Musca other')]
    (path/'nodes.dmp').write_text(''.join(f'{i}\t|\t{parent}\t|\t{rank}\t|\t\t|\t0\t|\t0\t|\t1\t|\t0\t|\t0\t|\t0\t|\t0\t|\t0\t|\t\t|\n' for i,parent,rank,name in definitions))
    (path/'names.dmp').write_text(''.join(f'{i}\t|\t{name}\t|\t\t|\tscientific name\t|\n' for i,parent,rank,name in definitions))
    (path/'merged.dmp').write_text('');(path/'delnodes.dmp').write_text('')


@pytest.mark.parametrize('mixed',[False,True])
def test_real_taxonkit_numeric_and_mixed_lca(tmp_path,mixed):
    if not shutil.which('taxonkit'):pytest.skip('local TaxonKit unavailable; no download')
    other=ANIMAL.copy();other[-1]='Musca other'
    f=fixture(tmp_path,{'-1':ANIMAL,'102':other} if mixed else {'101':ANIMAL});taxdump(tmp_path/'tax')
    sidecar=run(f,[hsp(f,tax='-1'),hsp(f,tax='102')] if mixed else [hsp(f,tax='101')])
    row=read(sidecar)[0]
    assert row['status']==('AMBIGUOUS_TIE' if mixed else 'ASSIGNED')
    assert row['resolved_taxid']==('NA' if mixed else '101')
    assert row['depth']==('5' if mixed else '6')


def test_legacy_cache_is_not_authority(tmp_path):
    f=fixture(tmp_path)
    (tmp_path/'consensus_blast_cache_COI.tsv').write_text('obsolete\tmalformed\tpositive_wrong_winner\n')
    assert prepare(f).returncode==0 and (tmp_path/'run.fasta').stat().st_size>0
    row=read(run(f,[hsp(f)]))[0]
    assert row['resolved_taxid']=='-1'


@pytest.mark.parametrize('changed',['family','genus','species','evalue','max_hsps','seed','disabled','lineage','taxonomy','database','kingdom'])
def test_signature_binds_every_scientific_input(tmp_path,changed):
    f=fixture(tmp_path);sidecar=run(f,[hsp(f)]);before=sidecar.read_bytes()
    assert command(['perl',HELPER,'publish','--plan',tmp_path/'run.plan','--input',sidecar,'--out',tmp_path/'state.tsv'],tmp_path).returncode==0
    if changed in ('family','genus','species','evalue','max_hsps','kingdom'):
        i=f['config'].index('--'+changed)+1
        f['config'][i]={'family':'93','genus':'96','species':'99','evalue':'10','max_hsps':'49','kingdom':'Fungi'}[changed]
    elif changed=='disabled':f['config'][f['config'].index('--seed')+1]='null'
    elif changed=='seed':(tmp_path/'seed.tsv').write_text('-1\t-12\t-111\t-1111\tspecies\n')
    else:
        file={'lineage':tmp_path/'authority.tsv','taxonomy':tmp_path/'tax/names.dmp','database':tmp_path/'ref.nhr'}[changed]
        file.write_text(file.read_text()+'\n')
    assert prepare(f,'changed').returncode==0
    assert (tmp_path/'changed.fasta').stat().st_size>0
    assert (tmp_path/'state.tsv').read_bytes()==before


def test_exact_biological_tie_and_multiquery_order(tmp_path):
    f=fixture(tmp_path);ids=[f['query'].replace('Consensus1',f'Consensus{i}') for i in [9,2,10,3]]
    (tmp_path/'input.fa').write_text(''.join('>'+q+'\n'+'ACGT'*25+'\n' for q in ids))
    a=hsp(f,qstart='1');b=hsp(f,qstart='2')
    sidecar=run(f,[b,a,b,a]);rows=read(sidecar)
    assert [r['long_seq_id'] for r in rows]==sorted(ids)
    assert all(json.loads(r['candidates_json'])[0][7]=='1' for r in rows)
    public=project(f,sidecar)
    assert len(public)==len(ids)
    assert [r['consensus_id'] for r in public]==[q.split('|')[1]+'_sample' for q in sorted(ids)]


def test_positive_taxid_cannot_override_invalid_status_or_lineage(tmp_path):
    f=fixture(tmp_path);sidecar=run(f,[]);project(f,sidecar)
    def damage(rows):
        v=rows[2].split('\t');v[8]='101';rows[2]='\t'.join(v)
    reseal(sidecar,damage)
    provenance=tmp_path/'provenance.tsv';provenance.write_text('round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n')
    (tmp_path/'Consensus').mkdir()
    for helper,args in [('consensus_assigned_otu_keys.pl',['--provenance',provenance]),('consensus_recovered_reads.pl',['--consensus-dir',tmp_path/'Consensus'])]:
        out=tmp_path/(helper+'.out');out.write_text('prior-complete\n')
        cp=command(['perl',ROOT/'bin'/helper,'--blast-report',tmp_path/'full.tsv','--taxonomy-sidecar',sidecar,'--min-level','family','--out',out,*args],tmp_path)
        assert cp.returncode!=0 and out.read_text()=='prior-complete\n'


def test_collision_markers_merge_without_cross_vote(tmp_path):
    paths=[]
    for i,(marker,ranks) in enumerate([('COI',COI_COLLISION),('ITS2',ITS_COLLISION)],1):
        f=fixture(tmp_path/marker,{'-1156':ranks},marker=marker)
        source=f['path']/'input.fa';source.write_text(source.read_text().replace('Consensus1',f'Consensus{i}'))
        paths.append(run(f,[hsp(f,tax='-1156')]))
    cp=command(['perl',HELPER,'merge','--out',tmp_path/'merged.tsv','--full',tmp_path/'full.tsv','--csv',tmp_path/'raw.csv',*reversed(paths)],tmp_path)
    assert cp.returncode==0,cp.stderr
    rows=read(tmp_path/'merged.tsv')
    assert [(r['marker'],r['resolved_taxid'],r['species'],r['candidate_count']) for r in rows]==[('COI','-1156','Thyene coccineovittata','1'),('ITS2','-1156','Porella arborisvitae','1')]


def test_conflicting_reference_disposition_is_hash_order_independent(tmp_path):
    f=fixture(tmp_path);titles=[]
    for i,rank in enumerate([4,5,6]):
        names=ANIMAL.copy();names[rank]='Different'+str(rank)
        titles.append(f'acc{i}\t0\tS{i}|kraken:taxid|-1 COI '+lineage(names)+'\n')
    outputs=[]
    for seed in ['0','1','81']:
        (tmp_path/'metadata.tsv').write_text(''.join(titles if seed=='0' else reversed(titles)))
        f['env'].update(PERL_HASH_SEED=seed,PERL_PERTURB_KEYS='2')
        sidecar=run(f,[hsp(f)],'seed'+seed)
        assert read(sidecar)[0]['status']=='REFERENCE_INCONSISTENT'
        outputs.append(sidecar.read_bytes())
    assert len(set(outputs))==1


def consumer_representative(report, workdir, reverse):
    # Drive the unchanged bin/append_reports.pl exactly as the existing consumer
    # harness does, replacing only the consensus report with the R4-C public rows.
    from tests import test_append_reports as consumer
    temp=workdir/'temp';temp.mkdir(parents=True);(workdir/consumer.ROUND_DIR).mkdir()
    ri,ot,bo,_,dm,ou=consumer.base_inputs(False);B=consumer.BARCODE
    consumer.write_tsv(workdir/f'{B}_read_info_rpt.txt',consumer.READ_INFO_HEADER,ri)
    consumer.write_tsv(workdir/f'{B}_on_target_rpt.txt',consumer.ON_TARGET_HEADER,ot)
    consumer.write_tsv(workdir/f'{B}_blast_otu_pretax_rpt.txt',consumer.BLAST_OTU_HEADER,bo)
    consumer.write_boundary_report_pair(workdir/f'{B}_demult_rpt.txt','demult_rpt',consumer.DEMULT_REPORT_HEADER,dm,consumer.DEMUX_IDENTITY_CONTEXT)
    consumer.write_boundary_report_pair(workdir/f'{B}_otu_def_rpt.txt','otu_def_rpt',consumer.OTU_REPORT_HEADER,ou,consumer.DEMUX_IDENTITY_CONTEXT)
    lines=report.read_text().splitlines();body=lines[1:][::-1] if reverse else lines[1:]
    (workdir/f'{B}_blast_consensus_tax_rpt.txt').write_text(''.join(x+'\n' for x in [lines[0]]+body))
    consumer.sequencing_template(workdir/'sequencing_template.tsv');(workdir/f'{consumer.ROUND_DIR}.pod5').write_bytes(b'test')
    cp=command(['perl',consumer.APPEND_REPORTS,consumer.ROUND_DIR,temp,workdir/f'{consumer.ROUND_DIR}.pod5',B,workdir/'sequencing_template.tsv'],
               workdir,{'RTBIOSCAN_DEMUX_IDENTITY_CONTEXT':consumer.DEMUX_IDENTITY_CONTEXT})
    assert cp.returncode==0,cp.stderr
    rep=(temp/f'{B}_blast_consensus_tax_representative_rpt.txt').read_text().splitlines()
    assert rep[0]==lines[0]
    return [dict(zip(rep[0].split('\t'),x.split('\t'))) for x in rep[1:]]


def test_public_percent_identity_is_fixed_point_and_survives_representative_selection(tmp_path):
    f=fixture(tmp_path)
    # Four queries with distinct sequences; the first two are otherwise tied
    # (same sample, species, reads and alignment length) and differ only by identity.
    spec=[('Consensus1','10','ACGT','100.000'),('Consensus2','10','TGCA','98.000'),('Consensus3','5','GATC','99.286'),('Consensus4','5','CTAG','92.500')]
    fasta='';rows=[]
    for name,reads,unit,identity in spec:
        query=f['query'].replace('Consensus1',name).replace('reads-10','reads-'+reads);seq=unit*25
        fasta+='>'+query+'\n'+seq+'\n'
        rows.append([hashlib.md5(seq.encode()).hexdigest(),'S-1|kraken:taxid|-1','0','1e-20','100',identity,'200','1','100','1','100','100'])
    (tmp_path/'input.fa').write_text(fasta)
    sidecar=run(f,rows);public=project(f,sidecar)
    expected={name+'_sample':identity for name,_,_,identity in spec}
    assert {r['consensus_id']:r['perc_id'] for r in public}==expected
    assert all(len(r['perc_id'].split('.'))==2 and len(r['perc_id'].split('.')[1])==3 and r['perc_id'].replace('.','').isdigit() for r in public)
    assert all(Decimal(r['perc_id'])==Decimal(json.loads(x['candidates_json'])[0][5]) for r,x in zip(public,read(sidecar)))
    csv=[line.split(',') for line in (tmp_path/'raw.csv').read_text().splitlines()]
    assert csv[0][4]=='pident' and sorted(x[4] for x in csv[1:])==sorted(expected.values())
    assert all(x[2]=='1e-20' for x in csv[1:])
    assert [r['consensus_species'] for r in public]==['Musca domestica']*3+['Unassigned']
    report=tmp_path/'R_blast_consensus_tax_rpt.txt'
    for reverse in (False,True):
        rep=consumer_representative(report,tmp_path/('reversed' if reverse else 'forward'),reverse)
        winners={(r['sample'],r['consensus_species']):r for r in rep}
        assert winners[('sample','Musca domestica')]['consensus_id']=='Consensus1_sample'
        assert winners[('sample','Musca domestica')]['perc_id']=='100.000'
