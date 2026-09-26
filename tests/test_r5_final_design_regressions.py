"""Independent joint-v2 ordering, inventory and refusal regressions."""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import traceback
import pytest

REPO = Path(__file__).resolve().parents[1]
AUTH = REPO / 'bin/state_snapshot_authority.pl'
REPAIR = REPO / 'bin/report_read_fate_repair.py'
DEMULT = 'read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n'
BLAST = 'read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n'

def test_broad_to_figures_alias_exemption_is_killed(tmp_path, monkeypatch):
    """The adapted protected test itself must kill the broad-exemption mutant."""
    bindir = tmp_path / 'mutant/bin'
    shutil.copytree(REPO / 'bin', bindir)
    path = bindir / AUTH.name
    source = path.read_text()
    needle = 'return if $p->{$rel};return if figure_alias($root,$rel);'
    assert source.count(needle) == 1
    path.write_text(source.replace(needle, needle + '\n        return if $rel=~m{\\Atables/to_figures/}&&S_ISLNK($st[2]);'))
    monkeypatch.setenv('RTBIOSCAN_RESTART_HANDLER_UNDER_TEST', str(bindir / 'restart_handler.sh'))
    test_path = REPO / 'tests/test_restart_round_lock_cutover.py'
    module = runpy.run_path(str(test_path))
    target = module['test_restore_never_flat_copies_a_presentation_only_structured_snapshot']
    expected_line = next(i for i, line in enumerate(test_path.read_text().splitlines(), 1)
                         if i >= target.__code__.co_firstlineno and line.strip() == 'assert result.returncode != 0')
    with pytest.raises(AssertionError) as killed:
        target(tmp_path / 'case')
    frames = traceback.extract_tb(killed.value.__traceback__)
    assert frames[-1].filename == str(test_path)
    assert frames[-1].lineno == expected_line, frames


def rounds(tmp_path, names, barcodes=None, empty=False):
    state = tmp_path / 'state1'
    (state / '_state').mkdir(parents=True)
    barcodes = barcodes or ['B1'] * len(names)
    for i, (name, barcode) in enumerate(zip(names, barcodes), 1):
        d = state / name
        d.mkdir()
        obj = dict(schema_version='1.6', state_id=state.name, barcode=barcode,
                   round_barcode=name, run_id=name.rsplit('_', 1)[0])
        (d / 'round_report.json').write_text(json.dumps(obj) + '\n')
        row = '' if empty else f'r{i}\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n'
        (d / f'{barcode}_demult_rpt.txt').write_text(DEMULT + row)
        (d / f'{barcode}_blast_otu_pretax_rpt.txt').write_text(BLAST)
    mapping = state / '_state/round_index.tsv'
    mapping.write_text(''.join(f'{name}\t{i}\n' for i, name in enumerate(names, 1)))
    return state, mapping


def order_command(state, boundary, index=None, extra=()):
    args = [sys.executable, str(REPAIR), '--check-live-order', '--state-dir', str(state),
            '--targets', 'COI', '--target-taxa', 'Metazoa', '--current-round-barcode', boundary]
    if index is not None:
        args += ['--round-index-file', str(index)]
    return subprocess.run(args + list(extra), capture_output=True, text=True)


@pytest.mark.parametrize('names', [
    ['runA_0', 'runB_0'], ['runB_0', 'runA_0'],
    ['runA_7', 'runB_7'], ['runZ_90', 'runA_0'],
    ['runC_9', 'runA_0', 'runB_0'],
], ids=['zero-forward', 'zero-reverse', 'equal-suffix', 'suffix-inversion', 'three-feeders'])
def test_authoritative_global_order_and_original_identities(tmp_path, names):
    state, mapping = rounds(tmp_path, names)
    api = runpy.run_path(str(REPAIR))
    indices, selected = api['authoritative_round_context'](state, mapping, names[-1])
    assert [obj['round_barcode'] for _, obj in selected] == names
    assert [indices[name] for name in names] == list(range(1, len(names) + 1))
    before = {p: p.read_bytes() for p in state.rglob('*') if p.is_file()}
    result = order_command(state, names[-1], mapping)
    assert result.returncode == 0 and result.stdout == 'normalize\n', result.stderr
    assert before == {p: p.read_bytes() for p in state.rglob('*') if p.is_file()}


@pytest.mark.parametrize('bad', [
    '', 'runA_0\t0\n', 'runA_0\t01\n', 'runA_0\t1000000000\n',
    'runA_0\t1\nrunA_0\t2\n', 'runA_0\t1\nrunB_0\t1\n',
    'round_barcode\tround_index\nrunA_0\t1\n', 'runA_0\t1',
    'runA_0\t1\textra\n', 'runB_0\t1\n',
])
def test_authoritative_mapping_refuses_before_mutation(tmp_path, bad):
    state, mapping = rounds(tmp_path, ['runA_0'])
    mapping.write_text(bad)
    before = {p.relative_to(state): p.read_bytes() for p in state.rglob('*') if p.is_file()}
    result = order_command(state, 'runA_0', mapping)
    assert result.returncode != 0
    assert 'authoritative round order' in result.stderr
    assert before == {p.relative_to(state): p.read_bytes() for p in state.rglob('*') if p.is_file()}


def test_explicit_empty_index_is_not_legacy(tmp_path):
    state, _ = rounds(tmp_path, ['runA_0'])
    result = order_command(state, 'runA_0', '')
    assert result.returncode != 0
    assert 'nonempty path' in result.stderr


def test_mapped_future_round_is_excluded_and_missing_prefix_refuses(tmp_path):
    names = ['runZ_9', 'runA_0', 'runB_0']
    state, mapping = rounds(tmp_path, names)
    api = runpy.run_path(str(REPAIR))
    _, selected = api['authoritative_round_context'](state, mapping, names[1])
    assert [obj['round_barcode'] for _, obj in selected] == names[:2]
    (state / names[0] / 'round_report.json').unlink()
    with pytest.raises(SystemExit, match='missing retained report'):
        api['authoritative_round_context'](state, mapping, names[1])


def test_global_history_compares_identity_order_not_suffix(tmp_path):
    names = ['runZ_9', 'runA_0', 'runB_0']
    state, mapping = rounds(tmp_path, names)
    history = state / '_state/report_history.jsonl'
    history.write_text(''.join(json.dumps({'round_barcode': n})+'\n' for n in names[:2]))
    result = order_command(state, names[-1], mapping)
    assert result.returncode == 0 and result.stdout == 'append\n'
    history.write_text(''.join(json.dumps({'round_barcode': n})+'\n' for n in reversed(names[:2])))
    assert order_command(state, names[-1], mapping).stdout == 'normalize\n'
    history.write_text(json.dumps({'round_barcode': 'unknown_0'})+'\n')
    assert order_command(state, names[-1], mapping).returncode != 0


def test_feeder_filter_combination_refuses_before_mutation(tmp_path):
    state, mapping = rounds(tmp_path, ['runA_0'])
    result = order_command(state, 'runA_0', mapping, ['--valid-rounds-from-feeder-metadata', str(tmp_path)])
    assert result.returncode != 0
    assert 'cannot be combined' in result.stderr

# Independent wire fixture derived from the approved grammar. No production
# capture, parser or serializer supplies expected bytes/inventory.
F01_EXACT = 'done_pod5.txt qced_reads_hq_accumulated.fasta round_index.tsv read_qscore_rolling.tsv otu_frozen_reps.fasta otu_frozen_reps.fasta.gz otu_active_pool.fasta otu_seen_hashes.tsv otu_consolidated_keys.tsv state_compatibility_manifest.tsv'.split()
PARSER_NAMES = ['demult_rpt.txt', 'demult_rpt.contract.tsv', 'otu_def_rpt.txt', 'otu_def_rpt.contract.tsv', 'demult_bootstrap.seeded']
FATE_NAMES = ['read_fate_demux_seen.tsv', 'read_fate_blast_seen.tsv', 'demux_annotation_cache.tsv']
GRACE_NAMES = ['assigned_otu_member_ids_prev_round.list', 'consensus_assigned_member_ids_prev_round.list']

def digest(data):
    return hashlib.sha256(data).hexdigest()

def fixture_record(root, token='a'*64, cache=True):
    groups = {
        'f01': ('f01', 'state', 'closed', ['state_authority/'+n for n in F01_EXACT]),
        'cache': ('cache', 'state', 'tree', ['sequences/Consensus/.cache']),
        'consensus': ('consensus', 'state', 'vector', ['sequences/Consensus/consensus_ownership.tsv', 'sequences/Consensus/consolidated_consensus_ids.txt']),
        'sup': ('sup', 'state', 'vector', ['state_authority/blastreport_sup_annotated_pre.fastq', 'state_authority/blastreport_sup_annotated_pre.fastq.gz']),
        'blast.1': ('blast', '1:COI', 'all-absent', ['state_authority/otu_blast_cache_COI.tsv', 'state_authority/otu_blast_evidence_COI.tsv', 'state_authority/memtax1.txt']),
        'parser.62': ('parser', 'b', 'all-present', ['state_authority/b_'+n for n in PARSER_NAMES]),
        'fate.62': ('read-fate', 'b', 'all-present', ['state_authority/b_'+n for n in FATE_NAMES]),
        'grace.62': ('grace', 'b', 'vector', ['state_authority/b_'+n for n in GRACE_NAMES]),
    }
    if cache:
        groups['cache'][3].extend(str(p.relative_to(root)) for p in (root/'sequences/Consensus/.cache').rglob('*'))
    lines = ['#RTB-JOINT-AUTHORITY\t2\n', 'state\ts\n', f'boundary\t62\t72756e415f30\t{token}\tfull_round\n', 'context\t73616d706c65\n', 'barcode\t62\n', 'target\t1\t434f49\n']
    entries = []
    for group, (kind,key,rule,paths) in sorted(groups.items()):
        lines.append(f'group\t{group}\t{kind}\t{key.encode().hex()}\t{rule}\t{len(paths)}\n')
        for path in paths:
            p=root/path
            if not p.exists(): fields=('A','-','-')
            elif p.is_dir(): fields=('D','-','-')
            else:
                data=p.read_bytes();fields=('F',str(len(data)),digest(data))
            entries.append((path, f'entry\t{group}\t{path.encode().hex()}\t'+ '\t'.join(fields)+'\n'))
    lines.extend(row for _,row in sorted(entries))
    body=''.join(lines).encode()
    return body+f'#END\t{len(groups)}\t{len(entries)}\t{digest(body)}\n'.encode()

def joint_fixture(root):
    st=root/'state_authority';st.mkdir(parents=True)
    cache=root/'sequences/Consensus/.cache';(cache/'empty').mkdir(parents=True)
    (cache/'.hidden').mkdir();(cache/'.hidden'/'name\twith\nbytes').write_bytes(b'cache payload\n')
    (root/'sequences/Consensus/consensus_ownership.tsv').write_bytes(b'')
    (st/'.lock').touch()
    (st/'done_pod5.txt').write_bytes(b'/reads/runA_0.pod5\t1\t1\t1\tfile\n')
    (st/'qced_reads_hq_accumulated.fasta').write_bytes(b'>r1\nACGT\n')
    (st/'round_index.tsv').write_bytes(b'runA_0\t1\n')
    for kind,header,sha1 in [('demult_rpt',DEMULT,'a9765e68ff8dc59228437312bee11c7259dcb898'),('otu_def_rpt',DEMULT.rstrip('\n')+'\tOTU_id\tOTU_role\n','2eb7f0c712e66bd79311d3656ea325626c330fc8')]:
        (st/f'b_{kind}.txt').write_text(header)
        (st/f'b_{kind}.contract.tsv').write_text(f'contract_version\t1\nreport_kind\t{kind}\ncontext\tsample\nrow_count\t0\nempty_contract\tgenuinely_empty\nheader_sha1\t{sha1}\n')
    (st/'b_demult_bootstrap.seeded').touch()
    (st/'b_read_fate_demux_seen.tsv').touch();(st/'b_read_fate_blast_seen.tsv').touch()
    (st/'b_demux_annotation_cache.tsv').write_text(DEMULT)
    (st/'AUTHORITY').write_bytes(fixture_record(root))
    return root

def auth(*args):
    return subprocess.run(['perl',str(AUTH),*map(str,args)],capture_output=True,text=True)

def byte_inventory(root):
    return {str(p.relative_to(root)): ('D' if p.is_dir() else p.read_bytes()) for p in root.rglob('*')}

@pytest.mark.parametrize('peer', ['equal','absent-first','absent-second','damaged','unequal','pristine-peer'])
def test_joint_root_matrix_independent_inventory(tmp_path, peer):
    t1=joint_fixture(tmp_path/'t1');t2=tmp_path/'t2';shutil.copytree(t1,t2)
    if peer=='absent-first':shutil.rmtree(t1)
    elif peer=='absent-second':shutil.rmtree(t2)
    elif peer=='damaged':(t2/'state_authority/qced_reads_hq_accumulated.fasta').write_bytes(b'>wrong\nTT\n')
    elif peer=='unequal':(t2/'state_authority/AUTHORITY').write_bytes(fixture_record(t2,token='b'*64))
    elif peer=='pristine-peer':shutil.rmtree(t2);t2.mkdir()
    before=(byte_inventory(t1) if t1.exists() else None,byte_inventory(t2) if t2.exists() else None)
    result=auth('select','s',tmp_path/'live',t1,t2)
    if peer in ('equal','absent-first','absent-second'):
        assert result.returncode==0,result.stderr
        assert result.stdout==('2\n' if peer=='absent-first' else '1\n')
    else:assert result.returncode!=0
    assert before==(byte_inventory(t1) if t1.exists() else None,byte_inventory(t2) if t2.exists() else None)

@pytest.mark.parametrize('residue',['.f03-transaction','.f03-transaction.tmp.'+'a'*64,'.f03-txn','state_authority/.AUTHORITY.tmp.'+'a'*64,'tables/.read_fate_normalization_pending'])
def test_transaction_residue_refuses_without_changes(tmp_path,residue):
    t1=joint_fixture(tmp_path/'t1');t2=tmp_path/'t2';shutil.copytree(t1,t2)
    p=t2/residue;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'unknown evidence\n')
    before=byte_inventory(t2)
    result=auth('select','s',tmp_path/'live',t1,t2)
    assert result.returncode!=0
    assert byte_inventory(t2)==before
    assert not (tmp_path/'live').exists()

@pytest.mark.parametrize('damage',['extra-cache','partial-parser','partial-fate','unexpected-ids','unexpected-blast','sup-presence','grace-presence'])
def test_governed_members_and_absence_are_binding(tmp_path,damage):
    t=joint_fixture(tmp_path/'root')
    if damage=='extra-cache':p=t/'sequences/Consensus/.cache/stale';p.write_bytes(b'stale')
    elif damage=='partial-parser':(t/'state_authority/b_demult_rpt.contract.tsv').unlink()
    elif damage=='partial-fate':(t/'state_authority/b_read_fate_blast_seen.tsv').unlink()
    else:
        n={'unexpected-ids':'sequences/Consensus/consolidated_consensus_ids.txt','unexpected-blast':'state_authority/memtax1.txt','sup-presence':'state_authority/blastreport_sup_annotated_pre.fastq.gz','grace-presence':'state_authority/b_assigned_otu_member_ids_prev_round.list'}[damage]
        (t/n).write_bytes(b'')
    assert auth('verify',t).returncode!=0


def test_restore_installs_exact_inventory_and_never_unions_cache(tmp_path):
    root=joint_fixture(tmp_path/'root');live=tmp_path/'live';(live/'_state').mkdir(parents=True)
    (live/'Consensus/.cache/old-empty').mkdir(parents=True)
    (live/'Consensus/.cache/stale').write_bytes(b'stale')
    (live/'Consensus/consolidated_consensus_ids.txt').write_bytes(b'wrong')
    result=auth('install',root,live/'_state')
    assert result.returncode==0,result.stderr
    assert byte_inventory(live/'Consensus/.cache')==byte_inventory(root/'sequences/Consensus/.cache')
    assert not (live/'Consensus/consolidated_consensus_ids.txt').exists()
    assert (live/'_state/qced_reads_hq_accumulated.fasta').read_bytes()==b'>r1\nACGT\n'
    for n in PARSER_NAMES+FATE_NAMES:
        assert (live/'_state'/('b_'+n)).read_bytes()==(root/'state_authority'/('b_'+n)).read_bytes()


def test_completed_v1_refuses_and_pristine_stays_pristine(tmp_path):
    t1=tmp_path/'t1';t2=tmp_path/'t2';t1.mkdir();t2.mkdir()
    assert auth('select','s',tmp_path/'live',t1,t2).stdout=='0\n'
    (t1/'done_pod5.txt').write_bytes(b'completed\n')
    before=byte_inventory(t1)
    result=auth('select','s',tmp_path/'live',t1,t2)
    assert result.returncode!=0 and 'JOINT_V2_REQUIRED' in result.stderr
    assert byte_inventory(t1)==before

def authenticated_fixture(tmp_path):
    root=joint_fixture(tmp_path/'seed')
    live=tmp_path/'s';live.mkdir()
    shutil.copytree(root/'state_authority',live/'_state')
    (live/'_state/AUTHORITY').unlink();(live/'_state/.lock').unlink()
    shutil.copytree(root/'sequences/Consensus',live/'Consensus')
    helper=REPO/'bin/round_lock_generation.pl'
    token='a'*64;acquisition='b'*64;pin='c'*64
    common=['--state-dir',str(live/'_state'),'--round-barcode','runA_0','--scope','full_round','--token',token,'--wait-seconds','1']
    for action,args in [('acquire',['--pin-token',acquisition,'--owner-pid',str(os.getpid()),'--stale-seconds','30']),('handoff',['--pin-token',acquisition]),('pin',['--pin-token',pin,'--owner-pid',str(os.getpid()),'--role','backup_update_and_clean'])]:
        r=subprocess.run(['perl',str(helper),action,*common,*args],capture_output=True,text=True)
        assert r.returncode==0,(action,r.stderr)
    env={**os.environ,'RTBIOSCAN_ROUND_LOCK_STATE_DIR':str(live/'_state'),'RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE':'runA_0','RTBIOSCAN_ROUND_LOCK_SCOPE':'full_round','RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN':token,'RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED':'0'}
    return live,helper,token,pin,env


def publication(tmp_path, *, mutate_after_release=False, authority=AUTH, cut='', seed_roots=False):
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    t1=tmp_path/'t1';t2=tmp_path/'t2';candidate=tmp_path/'candidate'
    if seed_roots:
        joint_fixture(t1);shutil.copytree(t1,t2)
    cmd=['perl',str(authority),'prepare',str(live),'s','b','runA_0',token,pin,'sample','COI',str(candidate),str(t1),str(t2)]
    r=subprocess.run(cmd,capture_output=True,text=True,env=env)
    assert r.returncode==0,r.stderr
    region=tmp_path/'region.py'
    region.write_text('''import os,sys,subprocess
from pathlib import Path
a,c,l,r1,r2,h,pin=sys.argv[1:]
run=lambda *args:subprocess.run(list(args),check=True)
r1=Path(r1);r2=Path(r2);l=Path(l)
assert (r1/'.f03-transaction').read_bytes()==(r2/'.f03-transaction').read_bytes()
assert not (r1/'state_authority/AUTHORITY').exists()
assert not (r2/'state_authority/AUTHORITY').exists()
run('perl',a,'capture',c,str(l),str(r1),pin)
'''+('raise SystemExit(77)\n' if cut=='after-capture' else '')+'''
run('perl',h,'finish','--state-dir',str(l/'_state'),'--round-barcode','runA_0','--scope','full_round','--token','a'*64,'--pin-token',pin)
'''+("(l/'_state/qced_reads_hq_accumulated.fasta').write_bytes(b'>newer\\nTTTT\\n')\n(l/'_state/.read_fate_normalization_pending').write_bytes(b'newer pending must not be touched\\n')\n" if mutate_after_release else '')+('raise SystemExit(77)\n' if cut=='after-finish' else '')+"run('perl',a,'seal',c,str(r1),str(r2))\n")
    r=subprocess.run(['perl',str(authority),'transaction',str(candidate),str(t1),str(t2),str(live),pin,sys.executable,str(region),str(authority),str(candidate),str(live),str(t1),str(t2),str(helper),pin],capture_output=True,text=True,env=env)
    return r,live,t1,t2,candidate,env


def test_authenticated_two_root_publication_never_reopens_live_after_finish(tmp_path):
    r,live,t1,t2,candidate,env=publication(tmp_path,mutate_after_release=True)
    assert r.returncode==0,r.stderr
    assert (t1/'state_authority/AUTHORITY').read_bytes()==candidate.read_bytes()==(t2/'state_authority/AUTHORITY').read_bytes()
    assert (t1/'state_authority/qced_reads_hq_accumulated.fasta').read_bytes()==b'>r1\nACGT\n'
    assert (t2/'state_authority/qced_reads_hq_accumulated.fasta').read_bytes()==b'>r1\nACGT\n'
    assert (live/'_state/qced_reads_hq_accumulated.fasta').read_bytes()==b'>newer\nTTTT\n'
    assert (live/'_state/.read_fate_normalization_pending').read_bytes()==b'newer pending must not be touched\n'
    assert not (t1/'.f03-transaction').exists() and not (t2/'.f03-transaction').exists()
    assert auth('select','s',tmp_path/'absent-live',t1,t2).stdout=='1\n'


def test_completed_retained_retry_converges_without_live_recapture(tmp_path):
    r,live,t1,t2,candidate,env=publication(tmp_path,cut='after-finish')
    assert r.returncode!=0
    assert (t1/'.f03-transaction').read_bytes()==(t2/'.f03-transaction').read_bytes()
    assert auth('select','s',tmp_path/'absent-live',t1,t2).returncode!=0
    (live/'_state/qced_reads_hq_accumulated.fasta').write_bytes(b'wrong later input')
    pending=live/'_state/.read_fate_normalization_pending'
    pending.write_bytes(pending_oracle('s','a'*64,['runA_0'],['b']))
    pending_bytes,pending_inode=pending.read_bytes(),pending.stat().st_ino
    env['RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED']='1'
    r=subprocess.run(['perl',str(AUTH),'resume',str(candidate),str(t1),str(t2)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    assert pending.read_bytes()==pending_bytes and pending.stat().st_ino==pending_inode
    assert (t2/'state_authority/qced_reads_hq_accumulated.fasta').read_bytes()==b'>r1\nACGT\n'

def test_dorado_only_cannot_clear_pending_v4(tmp_path):
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    pending=live/'_state/.read_fate_normalization_pending'
    pending.write_bytes(pending_oracle('s',token,['runA_0'],['b']))
    old,inode=pending.read_bytes(),pending.stat().st_ino
    env['RTBIOSCAN_ROUND_LOCK_SCOPE']='dorado_only'
    roots,before=seeded_roots(tmp_path)
    candidate=tmp_path/'candidate'
    r=subprocess.run(['perl',str(AUTH),'prepare',str(live),'s','b','runA_0',token,pin,'sample','COI',str(candidate),*map(str,roots)],env=env,capture_output=True,text=True)
    assert r.returncode!=0 and ('PENDING' in r.stderr or 'full-round context mismatch' in r.stderr)
    assert pending.read_bytes()==old and pending.stat().st_ino==inode
    assert [byte_inventory(root) for root in roots]==before
    assert not candidate.exists() and not (tmp_path/'worker/success.receipt').exists()


def test_interrupted_capture_remains_marked_and_unrestorable(tmp_path):
    r,live,t1,t2,candidate,env=publication(tmp_path,cut='after-capture')
    assert r.returncode!=0
    assert not (t1/'state_authority/AUTHORITY').exists()
    assert not (t2/'state_authority/AUTHORITY').exists()
    before=(byte_inventory(t1),byte_inventory(t2))
    assert auth('select','s',tmp_path/'absent-live',t1,t2).returncode!=0
    assert before==(byte_inventory(t1),byte_inventory(t2))

def test_fresh_worker_owns_pin_and_verifies_terminal_normalization(tmp_path):
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    rd=live/'runA_0';rd.mkdir()
    obj=dict(schema_version='1.6',state_id=live.name,barcode='b',round_barcode='runA_0',run_id='runA')
    (rd/'round_report.json').write_text(json.dumps(obj)+'\n')
    (rd/'b_demult_rpt.txt').write_text(DEMULT+'r1\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n')
    (rd/'b_blast_otu_pretax_rpt.txt').write_text(BLAST)
    (rd/'b_read_info_rpt.txt').write_text('read_id\nr1\n')
    (rd/'b_on_target_rpt.txt').write_bytes(b'')
    # A duplicate current entry makes both check calls require normalization.
    (live/'_state/report_history.jsonl').write_text((json.dumps(obj)+'\n')*2)
    control=tmp_path/'worker';r=auth('emit-worker',control,'5','60')
    assert r.returncode==0,r.stderr
    output=tmp_path/'output';output.mkdir()
    args=['/bin/bash',str(control/'worker.sh'),'2',str(live),'s','b'.encode().hex(),'runA_0'.encode().hex(),token,str(os.getpid()),pin,str(control/'worker.pin'),str(REPO/'bin/round_lock_process_guard.sh'),str(helper),str(AUTH),str(REPAIR),str(control/'driver.py'),'COI','Metazoa','genus',str(output),str(control)]
    proc=subprocess.Popen(args,env={'PATH':os.environ['PATH'],'LC_ALL':'C','LANG':'C','LC_CTYPE':'C','TMPDIR':str(tmp_path)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        ack=subprocess.run([sys.executable,str(control/'driver.py'),'ack',str(control),str(proc.pid),token,pin,'5'],capture_output=True,text=True)
        stdout,stderr=proc.communicate(timeout=30)
        assert ack.returncode==0,(ack.stderr,stderr)
        assert proc.returncode==0,stdout+stderr
    finally:
        if proc.poll() is None:
            proc.terminate();proc.wait(timeout=5)
    ready=json.loads((control/'ready.json').read_bytes())
    assert ready['worker_pid']==proc.pid!=os.getpid()
    receipt=json.loads((control/'success.receipt').read_bytes())
    assert receipt['context']['worker_pid']==proc.pid
    assert not (live/'_state/.read_fate_normalization_pending').exists()
    assert not (live/'_state/.report_history.lock.lockdir').exists()
    assert (live/'_state/b_read_fate_demux_seen.tsv').read_bytes()==b'r1\n'
    assert (live/'_state/b_read_fate_blast_seen.tsv').read_bytes()==b''
    assert (live/'_state/b_demux_annotation_cache.tsv').read_bytes()==(rd/'b_demult_rpt.txt').read_bytes()
    assert (rd/'b_read_fate_demult_first_seen.tsv').read_bytes()==(rd/'b_demult_rpt.txt').read_bytes()
    history=[json.loads(line) for line in (live/'_state/report_history.jsonl').read_text().splitlines()]
    assert [row['round_barcode'] for row in history]==['runA_0']
    assert history[0]==json.loads((rd/'round_report.json').read_text())
    assert (output/'report_html/runs/runA/run_report.json').is_file()

@pytest.mark.parametrize('mutation', ['cache-union','unequal-roots','legacy-v1','ignore-marker'])
def test_restore_semantic_mutants_are_killed(tmp_path,monkeypatch,mutation):
    mutant=tmp_path/'mutant/bin'
    shutil.copytree(REPO/'bin',mutant)
    helper=mutant/AUTH.name;s=helper.read_text()
    if mutation=='cache-union':
        old='remove_safe(source_path($dest,unhex($h),$install))'
        new='0 # semantic mutant: union instead of exact reconciliation\n'
    elif mutation=='unequal-roots':
        old="fail('JOINT_ROOTS_DIFFER') unless $records[0]{sha} eq $records[1]{sha};"
        new=''
    elif mutation=='legacy-v1':
        old="if completed_residue($r);push@state,'pristine'"
        new="if 0;push@state,'pristine'"
    else:
        old='my($n)=@_;return $n=~/'
        new="my($n)=@_;return 0 if $n eq '.f03-transaction';return $n=~/"
    assert s.count(old)==1,(mutation,s.count(old))
    helper.write_text(s.replace(old,new))
    # Syntax defects do not count as killed semantic mutants.
    syntax=subprocess.run(['perl','-c',str(helper)],capture_output=True,text=True)
    assert syntax.returncode==0,syntax.stderr
    monkeypatch.setitem(globals(),'AUTH',helper)
    case=tmp_path/'case';case.mkdir()
    with pytest.raises(AssertionError):
        if mutation=='cache-union':test_restore_installs_exact_inventory_and_never_unions_cache(case)
        elif mutation=='unequal-roots':test_joint_root_matrix_independent_inventory(case,'unequal')
        elif mutation=='legacy-v1':test_completed_v1_refuses_and_pristine_stays_pristine(case)
        else:test_transaction_residue_refuses_without_changes(case,'.f03-transaction')


def test_parent_pid_worker_pin_mutant_is_killed(tmp_path,monkeypatch):
    helper=tmp_path/'state_snapshot_authority.pl';s=AUTH.read_text()
    old='python3 "$DRIVER" bootstrap "${worker_args[@]}" "$$"'
    new='python3 "$DRIVER" bootstrap "${worker_args[@]}" "$PARENT_PID"'
    assert s.count(old)==1
    helper.write_text(s.replace(old,new))
    monkeypatch.setitem(globals(),'AUTH',helper)
    with pytest.raises(AssertionError,match='worker pin identity mismatch|worker birth changed across pin/exec'):
        test_fresh_worker_owns_pin_and_verifies_terminal_normalization(tmp_path/'case')

PENDING_RETAINED=('read_info_rpt.txt','on_target_rpt.txt','demult_rpt.txt','blast_otu_pretax_rpt.txt','blast_unassigned_reads_round.list','blast_report_annotated_otu_evidence.txt','blast_otu_noadapter_rpt.txt','round_index.tsv')

def pending_oracle(state,generation,names,barcodes,root=None,context=('COI','Metazoa','genus')):
    units=[]
    for i,(name,b) in enumerate(zip(names,barcodes),1):
        pref=b'RTB-ROUND-PREFIX\t1\n'+b''.join(f'{j}\t{n.encode().hex()}\n'.encode() for j,n in enumerate(names[:i],1))
        fields=[b.encode().hex(),name.encode().hex(),str(i),digest(pref)]
        body=f'RTB-READ-FATE-UNIT\t2\nstate\t{state.encode().hex()}\nbarcode\t{fields[0]}\nthrough\t{fields[1]}\nindex\t{i}\nprefix\t{fields[3]}\n'.encode()
        units.append(fields+[digest(body)])
    body=f'RTB-READ-FATE-NORMALIZATION-PENDING\t4\nstate\t{state.encode().hex()}\ngeneration\t{generation}\nscope\tfull_round\ncontext\t'.encode()+'\t'.join(value.encode().hex() for value in context).encode()+b'\nterminal\t'+'\t'.join(units[-1]).encode()+b'\n'
    body+=b''.join(b'unit\t'+'\t'.join(u).encode()+b'\n' for u in units)+f'count\t{len(units)}\n'.encode()
    for index,(name,barcode) in enumerate(zip(names,barcodes),1):
        rd=root/name if root is not None else None
        obj=json.loads((rd/'round_report.json').read_text()) if rd is not None else {'barcode':barcode,'round_barcode':name}
        stable={key:value for key,value in obj.items() if key not in ('read_fate','warnings')}
        stable_sha=digest(json.dumps(stable,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode())
        body+=f'round\t{index}\t{name.encode().hex()}\t{barcode.encode().hex()}\t{stable_sha}\n'.encode()
        for offset,suffix in enumerate(PENDING_RETAINED):
            path=rd/('round_index.tsv' if suffix=='round_index.tsv' else f'{barcode}_{suffix}') if rd is not None else None
            if path is not None and path.is_file():
                data=path.read_bytes();state_fields=f'F\t{len(data)}\t{digest(data)}'
            elif offset<4:
                state_fields=f'F\t0\t{digest(b"")}'
            else:
                state_fields='A'
            body+=f'input\t{index}\t{suffix}\t{state_fields}\n'.encode()
    body+=f'rounds\t{len(names)}\n'.encode()
    return body+b'#END\t'+digest(body).encode()+b'\n'


def run_actual_worker(tmp_path,live,helper,token,pin,barcode,boundary):
    control=tmp_path/'worker';r=auth('emit-worker',control,'5','60');assert r.returncode==0,r.stderr
    output=tmp_path/'output';output.mkdir()
    args=['/bin/bash',str(control/'worker.sh'),'2',str(live),live.name,barcode.encode().hex(),boundary.encode().hex(),token,str(os.getpid()),pin,str(control/'worker.pin'),str(REPO/'bin/round_lock_process_guard.sh'),str(helper),str(AUTH),str(REPAIR),str(control/'driver.py'),'COI','Metazoa','genus',str(output),str(control)]
    proc=subprocess.Popen(args,env={'PATH':os.environ['PATH'],'LC_ALL':'C','LANG':'C','LC_CTYPE':'C','TMPDIR':str(tmp_path)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        ack=subprocess.run([sys.executable,str(control/'driver.py'),'ack',str(control),str(proc.pid),token,pin,'5'],capture_output=True,text=True)
        stdout,stderr=proc.communicate(timeout=30)
        assert ack.returncode==0,(ack.stderr,stderr)
        assert proc.returncode==0,stdout+stderr
    finally:
        if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
    return control,output


@pytest.mark.parametrize('repeated',[False,True])
def test_pending_v4_replays_historical_barcodes_without_new_input(tmp_path,repeated):
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    names=['runA_0','runB_0']+(['runC_0'] if repeated else [])
    barcodes=['b','B2']+(['b'] if repeated else [])
    for i,(name,b) in enumerate(zip(names,barcodes),1):
        rd=live/name;rd.mkdir()
        obj=dict(schema_version='1.6',state_id='s',barcode=b,round_barcode=name,run_id=name.split('_')[0])
        (rd/'round_report.json').write_text(json.dumps(obj)+'\n')
        row='' if i==2 else f'r{i}\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n'
        (rd/f'{b}_demult_rpt.txt').write_text(DEMULT+row)
        (rd/f'{b}_blast_otu_pretax_rpt.txt').write_text(BLAST)
        (rd/f'{b}_read_info_rpt.txt').write_text('read_id\n'+(f'r{i}\n' if row else ''))
        (rd/f'{b}_on_target_rpt.txt').write_bytes(b'')
    (live/'_state/round_index.tsv').write_text(''.join(f'{n}\t{i}\n' for i,n in enumerate(names,1)))
    witness=live/'_state/.read_fate_normalization_pending'
    for i,name in enumerate(names):
        if i:
            token=digest(f'g:{name}'.encode());acquisition=digest(f'acq:{name}'.encode());pin=digest(f'backup:{name}'.encode())
            common=['--state-dir',str(live/'_state'),'--round-barcode',name,'--scope','full_round','--token',token,'--wait-seconds','1']
            for action,args in [('acquire',['--pin-token',acquisition,'--owner-pid',str(os.getpid()),'--stale-seconds','30']),('handoff',['--pin-token',acquisition]),('pin',['--pin-token',pin,'--owner-pid',str(os.getpid()),'--role','backup_update_and_clean'])]:
                r=subprocess.run(['perl',str(helper),action,*common,*args],capture_output=True,text=True);assert r.returncode==0,r.stderr
        if i<len(names)-1:
            witness.write_bytes(pending_oracle('s',token,names[:i+1],barcodes[:i+1],live))
            r=subprocess.run(['perl',str(helper),'finish','--state-dir',str(live/'_state'),'--round-barcode',name,'--scope','full_round','--token',token,'--pin-token',pin],capture_output=True,text=True);assert r.returncode==0,r.stderr
    control,output=run_actual_worker(tmp_path,live,helper,token,pin,barcodes[-1],names[-1])
    receipt=json.loads((control/'success.receipt').read_bytes())
    assert [bytes.fromhex(call['unit'][1]).decode() for call in receipt['calls']]==names
    assert [bytes.fromhex(call['unit'][0]).decode() for call in receipt['calls']]==barcodes
    assert not witness.exists()
    assert (live/'_state/b_read_fate_demux_seen.tsv').read_bytes()==(b'r1\nr3\n' if repeated else b'r1\n')
    assert (live/'_state/B2_read_fate_demux_seen.tsv').read_bytes()==b'r1\n'
    assert (live/'runB_0/B2_read_fate_demult_first_seen.tsv').read_text()==DEMULT
    history=[json.loads(line) for line in (live/'_state/report_history.jsonl').read_text().splitlines()]
    assert [row['round_barcode'] for row in history]==names
    assert history==[json.loads((live/n/'round_report.json').read_bytes()) for n in names]

def adoption_fixture(tmp_path, *, info=b'read_id\nr1\n', equiv=None, context=('COI','Metazoa','genus')):
    live,helper,token,pin,_=authenticated_fixture(tmp_path)
    for name,b in (('runA_0','b'),('runB_0','B2')):
        rd=live/name;rd.mkdir()
        obj=dict(schema_version='1.6',state_id='s',barcode=b,round_barcode=name,run_id=name.split('_')[0])
        (rd/'round_report.json').write_text(json.dumps(obj)+'\n')
        (rd/f'{b}_demult_rpt.txt').write_text(DEMULT+'r1\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n')
        (rd/f'{b}_blast_otu_pretax_rpt.txt').write_text(BLAST)
        (rd/f'{b}_read_info_rpt.txt').write_bytes(info if name=='runA_0' else b'read_id\nr1\n')
        (rd/f'{b}_on_target_rpt.txt').write_bytes(b'')
    if equiv is not None:(live/'runA_0/b_blast_unassigned_reads_round.list').write_bytes(equiv)
    (live/'_state/round_index.tsv').write_text('runA_0\t1\nrunB_0\t2\n')
    witness=live/'_state/.read_fate_normalization_pending'
    witness.write_bytes(pending_oracle('s',token,['runA_0'],['b'],live,context))
    r=subprocess.run(['perl',str(helper),'finish','--state-dir',str(live/'_state'),'--round-barcode','runA_0','--scope','full_round','--token',token,'--pin-token',pin],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    token=digest(b'v4:runB_0');acquisition=digest(b'v4:acqB');pin=digest(b'v4:backupB')
    common=['--state-dir',str(live/'_state'),'--round-barcode','runB_0','--scope','full_round','--token',token,'--wait-seconds','1']
    for action,args in [('acquire',['--pin-token',acquisition,'--owner-pid',str(os.getpid()),'--stale-seconds','30']),('handoff',['--pin-token',acquisition]),('pin',['--pin-token',pin,'--owner-pid',str(os.getpid()),'--role','backup_update_and_clean'])]:
        r=subprocess.run(['perl',str(helper),action,*common,*args],capture_output=True,text=True);assert r.returncode==0,(action,r.stderr)
    return live,helper,token,pin,witness

def live_science_bytes(live):
    paths=[p for round_name in ('runA_0','runB_0') if (live/round_name).exists() for p in (live/round_name).iterdir() if p.is_file()]
    paths += [p for p in (live/'_state').iterdir() if p.is_file() and (p.name=='report_history.jsonl' or p.name.startswith(('b_read_fate','B2_read_fate','b_demux_annotation','B2_demux_annotation')))]
    return {str(p.relative_to(live)):p.read_bytes() for p in paths}

def fresh_v4_fixture(tmp_path):
    live,helper,token,pin,_=authenticated_fixture(tmp_path)
    rd=live/'runA_0';rd.mkdir()
    obj=dict(schema_version='1.6',state_id='s',barcode='b',round_barcode='runA_0',run_id='runA')
    (rd/'round_report.json').write_text(json.dumps(obj)+'\n')
    (rd/'b_read_info_rpt.txt').write_bytes(b'')
    (rd/'b_on_target_rpt.txt').write_bytes(b'')
    (rd/'b_demult_rpt.txt').write_text(DEMULT+'r1\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n')
    (rd/'b_blast_otu_pretax_rpt.txt').write_text(BLAST)
    (live/'_state/report_history.jsonl').write_text((json.dumps(obj)+'\n')*2)
    return live,helper,token,pin,rd

def seeded_roots(tmp_path):
    roots=[tmp_path/'t1',tmp_path/'t2']
    for root in roots:root.mkdir();(root/'prior-authority').write_bytes(b'prior bytes\n')
    return roots,[byte_inventory(root) for root in roots]

def test_pending_v4_present_empty_required_and_absent_equiv_remain_valid(tmp_path):
    live,helper,token,pin,rd=fresh_v4_fixture(tmp_path)
    control,_=run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    assert (control/'success.receipt').is_file()
    assert (rd/'b_read_info_rpt.txt').read_bytes()==b''
    assert (rd/'b_on_target_rpt.txt').read_bytes()==b''
    assert (rd/'b_read_fate_demult_first_seen.tsv').read_text()==DEMULT
    assert all(not (rd/f'b_{suffix}').exists() for suffix in PENDING_RETAINED[4:7])
    assert not (rd/'round_index.tsv').exists()

@pytest.mark.parametrize('suffix',PENDING_RETAINED[:4])
def test_pending_v4_required_absence_refuses_before_capture(tmp_path,suffix):
    live,helper,token,pin,rd=fresh_v4_fixture(tmp_path)
    target=rd/f'b_{suffix}';target.unlink()
    roots,before=seeded_roots(tmp_path);science=live_science_bytes(live)
    diagnostic=(f'retained producer input absent without evidence of legitimate original absence: unit=b/runA_0/1 round=runA_0 path={target}' if suffix in PENDING_RETAINED[:2] else f'authoritative round order: unsafe/missing path {target}')
    with pytest.raises(AssertionError,match=re.escape(diagnostic)):
        run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    assert not (live/'_state/.read_fate_normalization_pending').exists()
    assert live_science_bytes(live)==science
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

@pytest.mark.parametrize('change,diagnostic',[
    ('empty-dist-deleted','retained producer input absent without evidence of legitimate original absence'),
    ('nonempty-dist-deleted','retained producer input absent without evidence of legitimate original absence'),
    ('nonempty-equiv-deleted','retained input disappeared'),
    ('equiv-appears','retained input appeared'),
    ('same-size','retained input bytes changed'),
    ('stable-field','stable round JSON changed'),
    ('stable-type','stable round JSON changed'),
    ('duplicate-json','duplicate round JSON key'),
    ('context','replay context changed'),
])
def test_pending_v4_adoption_refuses_changed_historical_evidence(tmp_path,change,diagnostic):
    info=b'' if change=='empty-dist-deleted' else b'read_id\nr1\n'
    equiv=b'r1\n' if change=='nonempty-equiv-deleted' else None
    context=('COI','Metazoa','species') if change=='context' else ('COI','Metazoa','genus')
    live,helper,token,pin,witness=adoption_fixture(tmp_path,info=info,equiv=equiv,context=context)
    rd=live/'runA_0'
    if change in ('empty-dist-deleted','nonempty-dist-deleted'):(rd/'b_read_info_rpt.txt').unlink()
    elif change=='nonempty-equiv-deleted':(rd/'b_blast_unassigned_reads_round.list').unlink()
    elif change=='equiv-appears':(rd/'b_blast_report_annotated_otu_evidence.txt').write_bytes(b'new\n')
    elif change=='same-size':(rd/'b_read_info_rpt.txt').write_bytes(b'read_id\nr2\n')
    elif change in ('stable-field','stable-type'):
        path=rd/'round_report.json';obj=json.loads(path.read_text());obj['stable_extension']=1 if change=='stable-type' else 'original'
        # Seal the original value, then change it without changing the witness.
        path.write_text(json.dumps(obj)+'\n')
        witness.write_bytes(pending_oracle('s','a'*64,['runA_0'],['b'],live,context))
        obj['stable_extension']=True if change=='stable-type' else 'changed';path.write_text(json.dumps(obj)+'\n')
    elif change=='duplicate-json':
        path=rd/'round_report.json';raw=path.read_text().strip();path.write_text('{"schema_version":"1.6",'+raw[1:]+'\n')
    old=witness.read_bytes();inode=witness.stat().st_ino
    roots=[tmp_path/'t1',tmp_path/'t2']
    for root in roots:root.mkdir();(root/'prior-authority').write_bytes(b'prior bytes\n')
    root_before=[byte_inventory(root) for root in roots]
    science_before=live_science_bytes(live)
    with pytest.raises(AssertionError,match=diagnostic):
        run_actual_worker(tmp_path,live,helper,token,pin,'B2','runB_0')
    assert witness.read_bytes()==old and witness.stat().st_ino==inode
    assert live_science_bytes(live)==science_before
    assert [byte_inventory(root) for root in roots]==root_before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

@pytest.mark.parametrize('change,diagnostic',[
    ('empty-read-info-deleted','retained input disappeared'),
    ('empty-on-target-deleted','retained input disappeared'),
    ('nonempty-dist-deleted','retained input disappeared'),
    ('nonempty-equiv-deleted','retained input disappeared'),
    ('equiv-appears','retained input appeared'),
    ('same-size','retained input bytes changed'),
    ('json-field','stable round JSON changed'),
    ('json-type','stable round JSON changed'),
])
def test_pending_v4_postcapture_fault_refuses_with_exact_witness(tmp_path,monkeypatch,change,diagnostic):
    live,helper,token,pin,rd=fresh_v4_fixture(tmp_path)
    if change=='nonempty-equiv-deleted':(rd/'b_blast_unassigned_reads_round.list').write_bytes(b'r1\n')
    if change=='nonempty-dist-deleted':(rd/'b_read_info_rpt.txt').write_bytes(b'read_id\nr1\n')
    if change=='same-size':(rd/'b_read_info_rpt.txt').write_bytes(b'read_id\nr1\n')
    if change=='json-type':
        p=rd/'round_report.json';obj=json.loads(p.read_text());obj['stable_extension']=1;p.write_text(json.dumps(obj)+'\n')
    before_wire=pending_oracle('s',token,['runA_0'],['b'],live)
    roots,root_before=seeded_roots(tmp_path)
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    repair=bindir/REPAIR.name;source=repair.read_text();anchor='        run_command(run_cmd)\n';assert source.count(anchor)==1
    body={
        'empty-read-info-deleted':"(last_round_dir/f'{barcode}_read_info_rpt.txt').unlink()",
        'empty-on-target-deleted':"(last_round_dir/f'{barcode}_on_target_rpt.txt').unlink()",
        'nonempty-dist-deleted':"(last_round_dir/f'{barcode}_read_info_rpt.txt').unlink()",
        'nonempty-equiv-deleted':"(last_round_dir/f'{barcode}_blast_unassigned_reads_round.list').unlink()",
        'equiv-appears':"(last_round_dir/f'{barcode}_blast_report_annotated_otu_evidence.txt').write_bytes(b'new\\n')",
        'same-size':"(last_round_dir/f'{barcode}_read_info_rpt.txt').write_bytes(b'read_id\\nr2\\n')",
        'json-field':"p=last_round_dir/'round_report.json';o=json.loads(p.read_text());o['stable_extension']='changed';p.write_text(json.dumps(o)+'\\n')",
        'json-type':"p=last_round_dir/'round_report.json';o=json.loads(p.read_text());o['stable_extension']=True;p.write_text(json.dumps(o)+'\\n')",
    }[change]
    inode_file=tmp_path/'published.inode'
    injected=f"        Path({str(inode_file)!r}).write_text(str((state_state_dir/'.read_fate_normalization_pending').stat().st_ino))\n        {body}\n"
    repair.write_text(source.replace(anchor,anchor+injected));compile(repair.read_text(),str(repair),'exec')
    monkeypatch.setitem(globals(),'REPAIR',repair)
    with pytest.raises(AssertionError,match=diagnostic):
        run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    witness=live/'_state/.read_fate_normalization_pending'
    assert inode_file.is_file() and witness.stat().st_ino==int(inode_file.read_text())
    assert witness.read_bytes()==before_wire
    assert [byte_inventory(root) for root in roots]==root_before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

@pytest.mark.parametrize('cut',['before-helper','after-helper'])
def test_pending_v4_helper_boundary_interruptions_keep_witness(tmp_path,monkeypatch,cut):
    live,helper,token,pin,rd=fresh_v4_fixture(tmp_path)
    expected=pending_oracle('s',token,['runA_0'],['b'],live)
    roots,before=seeded_roots(tmp_path)
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    helper_path=bindir/AUTH.name;source=helper_path.read_text()
    if cut=='before-helper':
        anchor='                    runpy.run_path(ctx["repair_helper"], run_name="__main__")\n';fault="                    die('injected helper boundary cut')\n"
        replacement=fault+anchor
    else:
        anchor='            finally:\n                sys.argv = saved\n            verify_durable(ctx, helper, plan, witness)\n'
        fault="            die('injected helper boundary cut')\n"
        replacement=anchor.replace('            verify_durable',fault+'            verify_durable')
    assert source.count(anchor)==1
    source=source.replace(anchor,replacement)
    publish='        os.replace(temp, witness)\n';assert source.count(publish)==1
    source=source.replace(publish,publish+"        (Path(ctx['control']) / 'published.inode').write_text(str(witness.stat().st_ino))\n")
    helper_path.write_text(source)
    monkeypatch.setitem(globals(),'AUTH',helper_path)
    with pytest.raises(AssertionError,match='injected helper boundary cut'):
        run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    witness=live/'_state/.read_fate_normalization_pending'
    assert witness.read_bytes()==expected
    assert witness.stat().st_ino==int((tmp_path/'worker/published.inode').read_text())
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

@pytest.mark.parametrize('cut',['before','after'])
@pytest.mark.parametrize('boundary',['runA_0','runB_0'])
def test_pending_v4_each_adopted_helper_boundary_interrupts_safely(tmp_path,monkeypatch,cut,boundary):
    live,helper,token,pin,witness=adoption_fixture(tmp_path)
    expected=pending_oracle('s',token,['runA_0','runB_0'],['b','B2'],live)
    roots,before=seeded_roots(tmp_path)
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    authority=bindir/AUTH.name;source=authority.read_text()
    if cut=='before':
        anchor='                    runpy.run_path(ctx["repair_helper"], run_name="__main__")\n'
        replacement=f"                    if boundary == {boundary!r}: die('injected adopted helper cut')\n"+anchor
    else:
        anchor='            finally:\n                sys.argv = saved\n            verify_durable(ctx, helper, plan, witness)\n'
        replacement=anchor.replace('            verify_durable',f"            if boundary == {boundary!r}: die('injected adopted helper cut')\n            verify_durable")
    assert source.count(anchor)==1
    source=source.replace(anchor,replacement)
    publish='        os.replace(temp, witness)\n';assert source.count(publish)==1
    source=source.replace(publish,publish+"        (Path(ctx['control']) / 'published.inode').write_text(str(witness.stat().st_ino))\n")
    authority.write_text(source);monkeypatch.setitem(globals(),'AUTH',authority)
    with pytest.raises(AssertionError,match='injected adopted helper cut'):
        run_actual_worker(tmp_path,live,helper,token,pin,'B2','runB_0')
    assert witness.read_bytes()==expected
    assert witness.stat().st_ino==int((tmp_path/'worker/published.inode').read_text())
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

def test_pending_v4_published_wire_distinguishes_empty_and_absent(tmp_path,monkeypatch):
    live,helper,token,pin,rd=fresh_v4_fixture(tmp_path)
    expected=pending_oracle('s',token,['runA_0'],['b'],live)
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    authority=bindir/AUTH.name;source=authority.read_text();anchor='        os.replace(temp, witness)\n';assert source.count(anchor)==1
    authority.write_text(source.replace(anchor,anchor+"        die('injected after v4 publication')\n"))
    monkeypatch.setitem(globals(),'AUTH',authority)
    roots,before=seeded_roots(tmp_path)
    with pytest.raises(AssertionError,match='injected after v4 publication'):
        run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    witness=live/'_state/.read_fate_normalization_pending'
    assert witness.read_bytes()==expected
    api=runpy.run_path(str(tmp_path/'worker/driver.py'))
    states=api['pending_read'](witness,'s',{'runA_0':1})['records'][0]['inputs']
    assert states[:2]==[['F','0',digest(b'')]]*2
    assert states[4:]==[['A']]*4
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

def test_pending_v4_b1_b2_adoption_copies_old_round_records_byte_for_byte(tmp_path,monkeypatch):
    live,helper,token,pin,witness=adoption_fixture(tmp_path)
    old=witness.read_bytes()
    roots,before=seeded_roots(tmp_path)
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    authority=bindir/AUTH.name;source=authority.read_text();anchor='        os.replace(temp, witness)\n';assert source.count(anchor)==1
    authority.write_text(source.replace(anchor,anchor+"        die('injected after v4 adoption publication')\n"))
    monkeypatch.setitem(globals(),'AUTH',authority)
    with pytest.raises(AssertionError,match='injected after v4 adoption publication'):
        run_actual_worker(tmp_path,live,helper,token,pin,'B2','runB_0')
    new=witness.read_bytes()
    assert new==pending_oracle('s',token,['runA_0','runB_0'],['b','B2'],live)
    api=runpy.run_path(str(tmp_path/'worker/driver.py'))
    old_file=tmp_path/'old-pending';old_file.write_bytes(old)
    previous=api['pending_read'](old_file,'s',{'runA_0':1,'runB_0':2})
    adopted=api['pending_read'](witness,'s',{'runA_0':1,'runB_0':2})
    assert adopted['records'][0]['raw']==previous['records'][0]['raw']
    assert len(adopted['records'])==2 and adopted['units'][:1]==previous['units']
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

def test_pending_v4_adoption_accepts_same_bytes_with_new_inode(tmp_path):
    live,helper,token,pin,witness=adoption_fixture(tmp_path)
    original=witness.read_bytes()
    path=live/'runA_0/b_read_info_rpt.txt';old_inode=path.stat().st_ino
    replacement=path.with_name(path.name+'.replacement');replacement.write_bytes(path.read_bytes());os.replace(replacement,path)
    assert path.stat().st_ino!=old_inode
    control,_=run_actual_worker(tmp_path,live,helper,token,pin,'B2','runB_0')
    assert (control/'success.receipt').is_file() and not witness.exists()
    assert original.startswith(b'RTB-READ-FATE-NORMALIZATION-PENDING\t4\n')

def test_mutant_skipping_durable_record_comparison_is_killed(tmp_path,monkeypatch):
    bindir=tmp_path/'mutant/bin';shutil.copytree(REPO/'bin',bindir)
    authority=bindir/AUTH.name;source=authority.read_text()
    anchors=[
        '            compare_captured_records(state, old["records"], current_records, old["context"], context)\n',
        '        verify_captured_records(ctx, helper, mapping, records, prepared[-1][4], context)\n',
        '    verify_records(Path(ctx["state_root"]), mapping, saved["records"], saved["context"], context)\n',
    ]
    for anchor in anchors:
        assert source.count(anchor)==1
        source=source.replace(anchor,anchor[:len(anchor)-len(anchor.lstrip())]+"pass  # semantic mutant: skip durable record comparison\n")
    authority.write_text(source)
    assert subprocess.run(['perl','-c',str(authority)],capture_output=True).returncode==0
    monkeypatch.setitem(globals(),'AUTH',authority)
    case=tmp_path/'case';case.mkdir()
    live,helper,token,pin,witness=adoption_fixture(case)
    original=witness.read_bytes()
    (live/'runA_0/b_read_info_rpt.txt').write_bytes(b'read_id\nr2\n')
    control,_=run_actual_worker(case,live,helper,token,pin,'B2','runB_0')
    # This private mutant accepts the changed historical bytes that the
    # production adoption regression rejects with the old witness intact.
    assert (control/'success.receipt').is_file() and not witness.exists()
    assert original.startswith(b'RTB-READ-FATE-NORMALIZATION-PENDING\t4\n')

def test_old_pending_v3_refuses_without_changing_live_or_roots(tmp_path):
    live,helper,token,pin,witness=adoption_fixture(tmp_path)
    lines=witness.read_text().splitlines(keepends=True);lines[0]=lines[0].replace('\t4\n','\t3\n')
    body=''.join(lines[:-1]).encode();witness.write_bytes(body+f'#END\t{digest(body)}\n'.encode())
    old,inode=witness.read_bytes(),witness.stat().st_ino
    science=live_science_bytes(live);roots,before=seeded_roots(tmp_path)
    with pytest.raises(AssertionError,match='unknown/wrong-state witness'):
        run_actual_worker(tmp_path,live,helper,token,pin,'B2','runB_0')
    assert witness.read_bytes()==old and witness.stat().st_ino==inode
    assert live_science_bytes(live)==science
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()

# Inject faults only in private helper copies. Production has no fault switches.
@pytest.mark.parametrize('cut', [
    'marker-temp-t1', 'marker-temp-t2', 'marker-t1', 'marker-t2',
    'invalidate-t1', 'invalidate-t2', 'verify-t1', 'replicate-t2',
    'verify-both', 'seal-t1', 'seal-t2', 'joint-proof', 'cleanup-t1', 'cleanup-t2',
])
def test_transaction_transition_faults_refuse_until_joint_proof(tmp_path, cut):
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    helper=bindir/AUTH.name;s=helper.read_text()
    if cut.startswith('marker-temp-'):
        root=cut.rsplit('-',1)[1]
        old="rename($tmp,$p) or fail('atomic rename');"
        new=f"die qq(injected {cut}\\n) if $p=~m{{/{root}/\\.f03-transaction\\z}};"+old
    elif cut.startswith('marker-'):
        root=cut.rsplit('-',1)[1]
        old='write_atomic("$r/.f03-transaction",marker_text($m),$m->{sha})'
        new=old+f';die qq(injected {cut}\\n) if $r=~m{{/{root}\\z}}'
    elif cut.startswith('invalidate-'):
        root=cut.rsplit('-',1)[1]
        old="unlink(\"$r/state_authority/AUTHORITY\") or $!{ENOENT} or fail('authority invalidation')"
        new=old+f';die qq(injected {cut}\\n) if $r=~m{{/{root}\\z}}'
    elif cut=='verify-t1':
        old='check_capture($candidate,$t1,$work,0);transfer($candidate,$t1,$t2,0,0,$work);'
        new='check_capture($candidate,$t1,$work,0);die qq(injected verify-t1\\n);transfer($candidate,$t1,$t2,0,0,$work);'
    elif cut=='replicate-t2':
        old='check_capture($candidate,$t1,$work,0);transfer($candidate,$t1,$t2,0,0,$work);'
        new=old+'die qq(injected replicate-t2\\n);'
    elif cut=='verify-both':
        old='check_capture($candidate,$_, $work,0) for($t1,$t2);'
        new=old+'die qq(injected verify-both\\n);'
    elif cut.startswith('seal-'):
        root=cut.rsplit('-',1)[1]
        old='copy_exact($candidate,"$r/state_authority/AUTHORITY",$n,$h,$m->{sha})'
        new=old+f';die qq(injected {cut}\\n) if $r=~m{{/{root}\\z}}'
    elif cut=='joint-proof':
        old='check_capture($candidate,$_, $work,1) for($t1,$t2);assert_marker($m,$_) for($t1,$t2);'
        new=old+'die qq(injected joint-proof\\n);'
    else:
        root=cut.rsplit('-',1)[1]
        old='unlink("$r/.f03-transaction") or fail("marker cleanup $r")'
        new=old+f';die qq(injected {cut}\\n) if $r=~m{{/{root}\\z}}'
    assert s.count(old)==1,(cut,s.count(old))
    helper.write_text(s.replace(old,new))
    syntax=subprocess.run(['perl','-c',str(helper)],capture_output=True,text=True)
    assert syntax.returncode==0,syntax.stderr
    case=tmp_path/'case';case.mkdir()
    r,live,t1,t2,candidate,env=publication(case,authority=helper)
    assert r.returncode!=0 and f'injected {cut}' in r.stderr,r.stderr
    before=(byte_inventory(t1),byte_inventory(t2))
    admitted=auth('select','s',case/'absent-live',t1,t2)
    assert (admitted.returncode==0)==(cut=='cleanup-t2'),admitted.stderr
    assert before==(byte_inventory(t1),byte_inventory(t2))
    if cut=='cleanup-t2':
        assert (t1/'state_authority/AUTHORITY').read_bytes()==candidate.read_bytes()==(t2/'state_authority/AUTHORITY').read_bytes()
    # All post-release canonical transition cuts can finish from retained bytes.
    if cut in ('verify-t1','replicate-t2','verify-both','seal-t1','seal-t2','joint-proof','cleanup-t1','cleanup-t2'):
        (live/'_state/qced_reads_hq_accumulated.fasta').write_bytes(b'>newer\nTTTT\n')
        env['RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED']='1'
        retry=subprocess.run(['perl',str(AUTH),'resume',str(candidate),str(t1),str(t2)],env=env,capture_output=True,text=True)
        assert retry.returncode==0,retry.stderr
        assert (t2/'state_authority/qced_reads_hq_accumulated.fasta').read_bytes()==b'>r1\nACGT\n'
        assert auth('select','s',case/'absent-live',t1,t2).returncode==0


def test_older_completed_worker_cannot_overwrite_newer_marker(tmp_path):
    r,live,t1,t2,candidate,env=publication(tmp_path,cut='after-finish')
    assert r.returncode!=0
    for root in (t1,t2):
        p=root/'.f03-transaction'
        p.write_bytes(p.read_bytes().replace(('a'*64).encode(),('f'*64).encode()))
    before=(byte_inventory(t1),byte_inventory(t2))
    env['RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED']='1'
    r=subprocess.run(['perl',str(AUTH),'resume',str(candidate),str(t1),str(t2)],env=env,capture_output=True,text=True)
    assert r.returncode!=0
    assert before==(byte_inventory(t1),byte_inventory(t2))


def test_root_fences_survive_supervisor_death_until_bash_descendant_exits(tmp_path):
    import time
    import signal
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    t1=tmp_path/'t1';t2=tmp_path/'t2';candidate=tmp_path/'candidate'
    r=subprocess.run(['perl',str(AUTH),'prepare',str(live),'s','b','runA_0',token,pin,'sample','COI',str(candidate),str(t1),str(t2)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    ready=tmp_path/'descendant.ready';release=tmp_path/'release';done=tmp_path/'descendant.done'
    script=tmp_path/'descendant.sh'
    script.write_text('''#!/bin/bash
set -eu
printf '%s\\n' "$$" > "$1"
i=0
while [ ! -f "$2" ] && [ "$i" -lt 100 ]; do
    sleep 0.1
    i=$((i+1))
done
: > "$3"
''')
    proc=subprocess.Popen(['perl',str(AUTH),'transaction',str(candidate),str(t1),str(t2),str(live),pin,'/bin/bash',str(script),str(ready),str(release),str(done)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    probe='use Fcntl qw(:DEFAULT :flock);sysopen(my $f,$ARGV[0],O_RDWR) or die $!;exit(flock($f,LOCK_EX|LOCK_NB)?0:1);'
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert ready.exists()
        assert int(ready.read_text())!=proc.pid
        proc.send_signal(signal.SIGKILL);proc.wait(timeout=5)
        for root in (t1,t2):
            lock=root/'state_authority/.lock'
            p=subprocess.run(['perl','-e',probe,str(lock)],capture_output=True,text=True)
            assert p.returncode==1,'root fence escaped while mutating Bash descendant remained alive'
    finally:
        release.touch()
        if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
        deadline=time.monotonic()+5
        while not done.exists() and time.monotonic()<deadline:time.sleep(.02)
    assert done.exists()
    for root in (t1,t2):
        deadline=time.monotonic()+5
        while True:
            p=subprocess.run(['perl','-e',probe,str(root/'state_authority/.lock')],capture_output=True,text=True)
            if p.returncode==0:break
            assert time.monotonic()<deadline
            time.sleep(.02)

@pytest.mark.parametrize('damage',['none','duplicate','reordered','missing','extra','required-absent','leading-zero','bad-count','bad-round-count','bad-digest','trailing','resealed-malformed','old-v3'])
def test_pending_v4_streamed_grammar_has_an_independent_wire_oracle(tmp_path,damage):
    control=tmp_path/'driver';r=auth('emit-worker',control,'5','60');assert r.returncode==0,r.stderr
    api=runpy.run_path(str(control/'driver.py'))
    names=['runA_0','runB_0'];barcodes=['B1','B2']
    data=pending_oracle('s','a'*64,names,barcodes)
    if damage not in ('none','bad-digest','trailing'):
        lines=data.decode('ascii').splitlines(keepends=True)
        first=next(i for i,line in enumerate(lines) if line.startswith('round\t'))
        if damage=='duplicate':lines[first+9]=lines[first]
        elif damage=='reordered':lines[first+1],lines[first+2]=lines[first+2],lines[first+1]
        elif damage=='missing':del lines[first+1]
        elif damage=='extra':lines.insert(first+1,lines[first+1])
        elif damage=='required-absent':lines[first+1]=f'input\t1\t{PENDING_RETAINED[0]}\tA\n'
        elif damage=='leading-zero':lines[first+1]=lines[first+1].replace('\tF\t0\t','\tF\t00\t')
        elif damage=='bad-count':lines[next(i for i,line in enumerate(lines) if line.startswith('count\t'))]='count\t3\n'
        elif damage=='bad-round-count':lines[-2]='rounds\t3\n'
        elif damage=='resealed-malformed':lines[first+1]=lines[first+1].replace('\tF\t','\tX\t')
        elif damage=='old-v3':lines[0]=lines[0].replace('\t4\n','\t3\n')
        body=''.join(lines[:-1]).encode();data=body+f'#END\t{digest(body)}\n'.encode()
    elif damage=='bad-digest':data=data[:-65]+b'0'*64+b'\n'
    elif damage=='trailing':data+=b'extra\n'
    witness=tmp_path/'pending';witness.write_bytes(data);inode=witness.stat().st_ino
    roots,before=seeded_roots(tmp_path)
    if damage=='none':
        parsed=api['pending_read'](witness,'s',dict(zip(names,(1,2))))
        assert parsed['generation']=='a'*64
        assert [bytes.fromhex(u[1]).decode() for u in parsed['units']]==names
        assert [u[2] for u in parsed['units']]==['1','2']
        assert [record['round'] for record in parsed['records']]==names
    else:
        with pytest.raises(RuntimeError):api['pending_read'](witness,'s',dict(zip(names,(1,2))))
    assert witness.read_bytes()==data and witness.stat().st_ino==inode
    assert [byte_inventory(root) for root in roots]==before
    assert not (tmp_path/'worker/success.receipt').exists()
    assert not (tmp_path/'candidate').exists()


def test_supplied_missing_mapping_never_falls_back(tmp_path):
    state,mapping=rounds(tmp_path,['runA_0'])
    mapping.unlink()
    before=byte_inventory(state)
    r=order_command(state,'runA_0',mapping)
    assert r.returncode!=0,r.stdout
    assert byte_inventory(state)==before


@pytest.mark.parametrize('mutation',['lexical','suffix','missing-map'])
def test_authoritative_order_semantic_mutants_are_killed(tmp_path,monkeypatch,mutation):
    source=REPAIR.read_text()
    if mutation=='missing-map':
        old='if args.round_index_file is not None:'
        new='if args.round_index_file is not None and Path(args.round_index_file).exists():'
    else:
        old='key=mapping.__getitem__)'
        new='key=str)' if mutation=='lexical' else "key=lambda rb: int(rb.rsplit('_', 1)[1]))"
    assert source.count(old)==1
    helper=tmp_path/'report_read_fate_repair.py';helper.write_text(source.replace(old,new))
    compile(helper.read_text(),str(helper),'exec')
    monkeypatch.setitem(globals(),'REPAIR',helper)
    if mutation=='missing-map':
        with pytest.raises(AssertionError):test_supplied_missing_mapping_never_falls_back(tmp_path/'case')
    else:
        names=['runB_0','runA_0'] if mutation=='lexical' else ['runZ_90','runA_0']
        state,mapping=rounds(tmp_path/'case',names)
        r=order_command(state,names[-1],mapping)
        assert r.returncode!=0,'mutant silently accepted non-authoritative order'
        assert 'boundary is not terminal' in r.stderr


def test_pending_witness_prevents_candidate_before_root_mutation(tmp_path):
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    witness=live/'_state/.read_fate_normalization_pending'
    witness.write_bytes(b'pending witness cannot be ignored\n')
    t1=tmp_path/'t1';t2=tmp_path/'t2';candidate=tmp_path/'candidate'
    before=byte_inventory(live)
    r=subprocess.run(['perl',str(AUTH),'prepare',str(live),'s','b','runA_0',token,pin,'sample','COI',str(candidate),str(t1),str(t2)],env=env,capture_output=True,text=True)
    assert r.returncode!=0,r.stdout
    assert 'READ_FATE_NORMALIZATION_PENDING' in r.stderr
    assert not candidate.exists() and not t1.exists() and not t2.exists()
    assert byte_inventory(live)==before


def test_ignoring_required_pending_witness_mutant_is_killed(tmp_path,monkeypatch):
    bindir=tmp_path/'mutant/bin';shutil.copytree(REPO/'bin',bindir)
    helper=bindir/AUTH.name;source=helper.read_text()
    old='my($m,$live,$pin,$role)=@_;pending_absent("$live/_state");'
    assert source.count(old)==1
    helper.write_text(source.replace(old,'my($m,$live,$pin,$role)=@_;'))
    syntax=subprocess.run(['perl','-c',str(helper)],capture_output=True,text=True)
    assert syntax.returncode==0,syntax.stderr
    monkeypatch.setitem(globals(),'AUTH',helper)
    with pytest.raises(AssertionError):test_pending_witness_prevents_candidate_before_root_mutation(tmp_path/'case')


def test_existing_authorities_are_both_invalidated_before_region(tmp_path):
    r,live,t1,t2,candidate,env=publication(tmp_path,seed_roots=True,authority=AUTH)
    assert r.returncode==0,r.stderr
    assert (t1/'state_authority/AUTHORITY').read_bytes()==candidate.read_bytes()==(t2/'state_authority/AUTHORITY').read_bytes()


@pytest.mark.parametrize('root',['t1','t2'])
def test_skipping_either_root_invalidation_mutant_is_killed(tmp_path,monkeypatch,root):
    bindir=tmp_path/'mutant/bin';shutil.copytree(REPO/'bin',bindir)
    helper=bindir/AUTH.name;s=helper.read_text()
    old='for my$r($t1,$t2){unlink("$r/state_authority/AUTHORITY") or $!{ENOENT} or fail(\'authority invalidation\')}'
    new=old.replace('{unlink(',f'{{next if $r eq ${root};unlink(')
    assert s.count(old)==1
    helper.write_text(s.replace(old,new))
    syntax=subprocess.run(['perl','-c',str(helper)],capture_output=True,text=True)
    assert syntax.returncode==0,syntax.stderr
    monkeypatch.setitem(globals(),'AUTH',helper)
    case=tmp_path/'case';case.mkdir()
    with pytest.raises(AssertionError):test_existing_authorities_are_both_invalidated_before_region(case)

@pytest.mark.parametrize('cut',['before-first-write','history','demux','blast','annotation'])
def test_interrupted_normalization_keeps_pending_and_prevents_success(tmp_path,monkeypatch,cut):
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    repair=bindir/REPAIR.name;s=repair.read_text()
    if cut=='before-first-write':
        old='def atomic_write_text(path: Path, text: str) -> None:\n'
        new=old+"    raise RuntimeError('injected normalization cut')\n"
    else:
        suffix={'history':'report_history.jsonl','demux':'_read_fate_demux_seen.tsv','blast':'_read_fate_blast_seen.tsv','annotation':'_demux_annotation_cache.tsv'}[cut]
        old='    tmp_path.replace(path)\n'
        new=old+f"    if path.name.endswith({suffix!r}):\n        raise RuntimeError('injected normalization cut')\n"
    assert s.count(old)==1
    repair.write_text(s.replace(old,new));compile(repair.read_text(),str(repair),'exec')
    monkeypatch.setitem(globals(),'REPAIR',repair)
    case=tmp_path/'case'
    with pytest.raises(AssertionError,match='injected normalization cut'):
        test_fresh_worker_owns_pin_and_verifies_terminal_normalization(case)
    witness=case/'s/_state/.read_fate_normalization_pending'
    assert witness.is_file() and witness.read_bytes().startswith(b'RTB-READ-FATE-NORMALIZATION-PENDING\t4\n')
    assert not (case/'worker/success.receipt').exists()
    assert not (case/'candidate').exists() and not (case/'t1').exists() and not (case/'t2').exists()
    if cut in ('demux','blast','annotation'):
        assert (case/'s/_state/b_read_fate_demux_seen.tsv').read_bytes()==b'r1\n'
    if cut=='annotation':
        assert (case/'s/_state/b_demux_annotation_cache.tsv').read_bytes()==(case/'s/runA_0/b_demult_rpt.txt').read_bytes()


@pytest.mark.parametrize('name',['.f03-transaction','.read_fate_normalization_pending','.round_inflight.lockdir'])
def test_transport_payload_cannot_hide_reserved_control_evidence(tmp_path,name):
    t1=joint_fixture(tmp_path/'t1');t2=tmp_path/'t2';shutil.copytree(t1,t2)
    (t2/'tables').mkdir()
    hidden=t2/'.live_round_payloads/retained'/name
    hidden.parent.mkdir(parents=True);hidden.write_bytes(b'reserved residue\n')
    before=byte_inventory(t2)
    r=auth('select','s',tmp_path/'live',t1,t2)
    assert r.returncode!=0 and 'TRANSACTION_INCOMPLETE' in r.stderr
    assert before==byte_inventory(t2)



def test_worker_refuses_corrupt_bounded_read_fate_json_before_pending_commit(tmp_path,monkeypatch):
    """Every bounded JSON output must be verified before pending unlink."""
    bindir=tmp_path/'mutant/bin';shutil.copytree(REPO/'bin',bindir)
    repair=bindir/REPAIR.name;source=repair.read_text()
    old='        patched = patch_round_read_fate(repo_root, round_dir, round_obj, args.targets, args.target_taxa)\n'
    assert source.count(old)==1
    repair.write_text(source.replace(old,old+'        patched["read_fate"] = {"deliberately_wrong_negative_control": 1}\n'))
    compile(repair.read_text(),str(repair),'exec')
    monkeypatch.setitem(globals(),'REPAIR',repair)
    case=tmp_path/'case'
    try:
        test_fresh_worker_owns_pin_and_verifies_terminal_normalization(case)
    except AssertionError:
        # The success-path fixture must fail when the worker rejects the fault.
        pass
    repaired=json.loads((case/'s/runA_0/round_report.json').read_text())
    assert repaired['read_fate']=={'deliberately_wrong_negative_control':1},'fault was not reached'
    assert not (case/'worker/success.receipt').exists(),'incoherent bounded JSON was accepted as a verified plan'
    assert (case/'s/_state/.read_fate_normalization_pending').is_file()

@pytest.mark.parametrize('fault,diagnostic', [
    ('round-json', 'bounded read_fate mismatch'),
    ('round-balanced', 'bounded read_fate mismatch'),
    ('immutable', 'stable round JSON changed'),
    ('first-demux', 'first-seen verification failed'),
    ('first-blast', 'first-seen verification failed'),
    ('unassigned', 'bounded blast-unassigned mismatch'),
    ('run-json', 'bounded run-report mismatch'),
    ('run-missing', 'missing required retained input'),
    ('history', 'terminal history is not the exact ordered report prefix'),
    ('optional-appears', 'retained input bytes changed'),
    ('input-changes', 'retained input bytes changed'),
])
def test_bounded_output_mutants_fail_before_pending_commit(tmp_path, monkeypatch, fault, diagnostic):
    """Mutate real helper outputs, after successful calculations, without
    altering the verifier or the unchanged scientific reporting contracts."""
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    path=bindir/REPAIR.name;s=path.read_text()
    anchor='        run_command(run_cmd)\n'
    assert s.count(anchor)==1
    body={
        'round-json': "p=last_round_dir/'round_report.json'; o=json.loads(p.read_text()); o['read_fate']={}; p.write_text(json.dumps(o)+'\\n')",
        'round-balanced': "p=last_round_dir/'round_report.json'; o=json.loads(p.read_text()); o['read_fate']['demux_total_reads']=2; o['read_fate']['demux_total_reads_coi']=2; p.write_text(json.dumps(o)+'\\n')",
        'immutable': "p=last_round_dir/'round_report.json'; o=json.loads(p.read_text()); o['schema_version']='changed'; p.write_text(json.dumps(o)+'\\n')",
        'first-demux': "(last_round_dir/f'{barcode}_read_fate_demult_first_seen.tsv').write_text(DEMULT_HEADER+'\\n')",
        'first-blast': "(last_round_dir/f'{barcode}_read_fate_blast_first_seen.tsv').write_text(BLAST_HEADER+'\\nbogus\\n')",
        'unassigned': "(state_state_dir/f'{barcode}_blast_unassigned_current.list').write_text('bogus\\n')",
        'run-json': "o=json.loads(run_report_json.read_text()); o['run_summary']['read_fate']={}; run_report_json.write_text(json.dumps(o)+'\\n')",
        'run-missing': "run_report_json.unlink()",
        'history': "history_path.write_text('')",
        'optional-appears': "(last_round_dir/f'{barcode}_read_info_rpt.txt').write_text('read_id\\n')",
        'input-changes': "(last_round_dir/f'{barcode}_demult_rpt.txt').write_text(DEMULT_HEADER+'\\n')",
    }[fault]
    path.write_text(s.replace(anchor,anchor+'        '+body+'\n'))
    monkeypatch.setitem(globals(),'REPAIR',path)
    case=tmp_path/'case';case.mkdir()
    roots=[case/'t1',case/'t2']
    for root in roots:
        root.mkdir();(root/'prior-authority').write_bytes(b'prior bytes\n')
    before=[byte_inventory(root) for root in roots]
    with pytest.raises(AssertionError,match=diagnostic):
        test_fresh_worker_owns_pin_and_verifies_terminal_normalization(case)
    assert (case/'s/_state/.read_fate_normalization_pending').is_file()
    assert not (case/'worker/success.receipt').exists()
    assert not (case/'candidate').exists()
    assert [byte_inventory(root) for root in roots]==before


@pytest.mark.parametrize('cut', ['before-pending-temp','pending-temp','pending-published','unit-receipt','before-history-release','after-history-release','before-pending-unlink','after-pending-unlink','before-success'])
def test_worker_commit_interruption_matrix(tmp_path, monkeypatch, cut):
    bindir=tmp_path/'cut/bin';shutil.copytree(REPO/'bin',bindir)
    path=bindir/AUTH.name;s=path.read_text()
    anchors={
        'before-pending-temp': '        exclusive(temp, wire)\n',
        'pending-temp': '        exclusive(temp, wire)\n',
        'pending-published': '        os.replace(temp, witness)\n',
        'unit-receipt': '        exclusive(control / f"unit-{u[2]}.receipt", json.dumps(receipt, sort_keys=True).encode() + b"\\n")\n',
        'before-history-release': '    release_history(ctx)\n',
        'after-history-release': '    release_history(ctx)\n',
        'before-pending-unlink': '        witness.unlink()\n',
        'after-pending-unlink': '        witness.unlink()\n',
        'before-success': '    exclusive(control / "success.receipt", json.dumps(receipt, sort_keys=True).encode() + b"\\n")\n',
    }
    anchor=anchors[cut];assert s.count(anchor)==1
    line=(' '* (len(anchor)-len(anchor.lstrip())))+"die('injected commit cut')\n"
    replacement=line+anchor if cut.startswith('before-') else anchor+line
    s=s.replace(anchor,replacement)
    publish='        os.replace(temp, witness)\n';assert s.count(publish)==1
    s=s.replace(publish,publish+"        (Path(ctx['control']) / 'published.inode').write_text(str(witness.stat().st_ino))\n")
    path.write_text(s);monkeypatch.setitem(globals(),'AUTH',path)
    case=tmp_path/'case';case.mkdir()
    roots,root_before=seeded_roots(case)
    with pytest.raises(AssertionError,match='injected commit cut'):
        test_fresh_worker_owns_pin_and_verifies_terminal_normalization(case)
    assert not (case/'worker/success.receipt').exists()
    pending=case/'s/_state/.read_fate_normalization_pending'
    if cut=='before-pending-temp':
        assert not pending.exists() and not list(pending.parent.glob(pending.name+'.tmp.*'))
    elif cut=='pending-temp':
        assert list(pending.parent.glob(pending.name+'.tmp.*'))
    elif cut in ('after-pending-unlink','before-success'):
        assert not pending.exists()
        assert (case/'s/_state/b_read_fate_demux_seen.tsv').read_bytes()==b'r1\n'
    else:
        assert pending.is_file()
        assert pending.read_bytes()==pending_oracle('s','a'*64,['runA_0'],['b'],case/'s')
        assert pending.stat().st_ino==int((case/'worker/published.inode').read_text())
    assert [byte_inventory(root) for root in roots]==root_before
    assert not (case/'candidate').exists()

@pytest.mark.parametrize('mode', ['committed', 'absent', 'corrupt-run-status', 'delete-r4-record'])
def test_worker_verifies_reached_r4_and_resolved_run_status(tmp_path, monkeypatch, mode):
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    state=live/'_state';rd=live/'runA_0';rd.mkdir()
    obj=dict(schema_version='1.6',state_id='s',barcode='b',round_barcode='runA_0',run_id='runA',markers={'order':['COI'],'target_taxa_by_marker':{'COI':'Metazoa'}})
    (rd/'round_report.json').write_text(json.dumps(obj)+'\n')
    (state/'report_history.jsonl').write_text((json.dumps(obj)+'\n')*2)
    read_info='read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\nr1\tr1.pod5\trunA\tb\t100\t10\t100\t12\tNA\tNA\n'
    on_target='read_id\tqc_filter\ton_target_kingdom\nr1\tIN\tON_TARGET\n'
    demult=DEMULT+'r1\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n'
    public=BLAST+'r1\tCOI\thac\ts\thit\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS1\n'
    for root in (state,rd):
        (root/'b_read_info_rpt.txt').write_text(read_info)
        (root/'b_on_target_rpt.txt').write_text(on_target)
    (rd/'b_demult_rpt.txt').write_text(demult)
    (rd/'b_blast_otu_pretax_rpt.txt').write_text(public)
    if mode!='absent':
        products={'reporting':('b_blast_otu_reporting_v1.tsv',b'reporting\n'),'public':('b_blast_otu_pretax_rpt.txt',public.encode()),'noadapter':('b_blast_otu_noadapter_rpt.txt',BLAST.encode())}
        rows=''.join(f'{key}\t{name}\t{len(data)}\t{digest(data)}\n' for key,(name,data) in products.items())
        gen=digest(rows.encode())
        body=f'#RTB-R4D-CUMULATIVE\t1\ngeneration\t{gen}\nprevious\tNA\n'+rows
        for name,data in products.values():(state/f'{name}.gen-{gen}').write_bytes(data)
        (state/'b_blast_otu_cumulative.commit').write_text(body+f'#END\t{digest(body.encode())}\n')
    if mode.startswith('corrupt') or mode=='delete-r4-record':
        bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
        repair=bindir/REPAIR.name;s=repair.read_text();anchor='        run_command(run_cmd)\n';assert s.count(anchor)==1
        body="o=json.loads(run_report_json.read_text()); o['run_status_read_fate']={}; run_report_json.write_text(json.dumps(o)+'\\n')" if mode.startswith('corrupt') else "(state_state_dir/f'{barcode}_blast_otu_cumulative.commit').unlink()"
        repair.write_text(s.replace(anchor,anchor+'        '+body+'\n'));monkeypatch.setitem(globals(),'REPAIR',repair)
        with pytest.raises(AssertionError):
            run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
        assert not (tmp_path/'worker/success.receipt').exists()
        assert (state/'.read_fate_normalization_pending').is_file()
    else:
        control,output=run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
        result=json.loads((tmp_path/'output/report_html/runs/runA/run_report.json').read_text())
        assert ('run_status_read_fate' in result)==(mode=='committed')
        plan=json.loads((control/'repair-plan.json').read_text())
        assert plan['calls'][0]['contracts']['r4']['mode']==mode

CHART_NAMES = ['reads_fate_per_round.tsv','otu_fate_per_round.tsv','otu_active_by_marker_per_round.tsv','consensus_emitted_by_marker_per_round.tsv','otu_assignments_species.tsv','otu_assignments_genus.tsv','otu_assignments_family.tsv','consensus_assignments_species.tsv','consensus_assignments_genus.tsv','consensus_assignments_family.tsv','frozen_otu_assignments_species.tsv','consolidated_consensus_assignments_species.tsv','otu_assignments_by_sample_species.tsv','consensus_assignments_by_sample_species.tsv']

@pytest.mark.parametrize('name',CHART_NAMES)
@pytest.mark.parametrize('prefix',['tables/','tables/to_figures/embedded/'])
def test_all_exact_chart_leaves_are_excluded_without_following_aliases(tmp_path,name,prefix):
    t1=joint_fixture(tmp_path/'t1');t2=tmp_path/'t2';shutil.copytree(t1,t2)
    p=t2/(prefix+name);p.parent.mkdir(parents=True,exist_ok=True)
    p.symlink_to(tmp_path/'absent-render-output')
    assert auth('select','s',tmp_path/'live',t1,t2).returncode==0
    assert auth('presentation-leaf',t2,p).returncode==0

PDF_PATHS = ['plots/pdf/embedded/'+name for name in ['run_reads_fate.pdf','run_otu_fate.pdf','run_informative_otu.pdf','run_consensus_emitted.pdf','run_demultiplex_reads_by_marker.pdf']]
PDF_PATHS += [f'plots/pdf/embedded/run_{kind}_sunburst_COI.pdf' for kind in ['otu','consensus','frozen_otu','consolidated_consensus']]
PDF_PATHS += ['plots/pdf/embedded/samples/S_A_reads_per_barcode.pdf']
PDF_PATHS += [f'plots/pdf/embedded/samples/S_A_{kind}_treemap_{rank}.pdf' for kind in ['otu','consensus'] for rank in ['species','genus','family']]
PDF_PATHS += [f'plots/pdf/samples/S_A/S_A_{kind}.pdf' for kind in ['reads_time_history','reads_cumulative_history','otu_tax_time_history','consensus_tax_time_history']]
PDF_PATHS += [f'plots/pdf/samples/S_A/S_A_{kind}_coi_{shape}.pdf' for kind in ['otu','consensus'] for shape in ['icicle','sunburst']]

@pytest.mark.parametrize('path',PDF_PATHS)
def test_every_proven_pdf_family_and_history_destination(tmp_path,path):
    t1=joint_fixture(tmp_path/'t1');t2=tmp_path/'t2';shutil.copytree(t1,t2)
    (t2/'tables').mkdir()
    asset=path.replace('plots/pdf/','runs/runA/report_assets/')
    row={'run_id':'runA','sample_metrics':{'S A':{'label':'S A'}},'figures':[{'path':asset.removesuffix('.pdf')+'.png','pdf_path':asset}]}
    (t2/'tables/report_history.jsonl').write_text(json.dumps(row)+'\n')
    p=t2/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'presentation')
    assert auth('select','s',tmp_path/'live',t1,t2).returncode==0
    assert auth('presentation-leaf',t2,p).returncode==0


def test_mixed_utf8_history_remains_an_admissible_snapshot_member(tmp_path):
    root = joint_fixture(tmp_path / 't1')
    (root / 'tables').mkdir()
    rows = [
        {'run_id': 'runA', 'barcode': 'b', 'round_barcode': 'runA_0', 'label': 'old Ã\u0085land'},
        {'run_id': 'runA', 'barcode': 'b', 'round_barcode': 'runA_1', 'label': 'Åland'},
    ]
    wire = b'\n'.join(json.dumps(row, ensure_ascii=False).encode('utf-8') for row in rows) + b'\n'
    history = root / 'tables/report_history.jsonl'
    history.write_bytes(wire)
    (root / 'state_authority/AUTHORITY').write_bytes(fixture_record(root))
    peer = tmp_path / 't2'
    shutil.copytree(root, peer)
    result = auth('select', 's', tmp_path / 'live', root, peer)
    assert result.returncode == 0, result.stderr
    assert history.read_bytes() == (peer / 'tables/report_history.jsonl').read_bytes() == wire


@pytest.mark.parametrize('label,tail', [
    ('L\u2028S', b''), ('P\u2029S', b''), ('N\u0085E', b''),
    ('ASCII', b''), ('ASCII', b'\n'), ('ASCII', b'{bad json\n'),
])
def test_rebuild_run_json_keeps_literal_lf_history_record(tmp_path, label, tail):
    outdir = tmp_path / 'results'
    history = outdir / 'temp/ongoing/state/s/_state/report_history.jsonl'
    history.parent.mkdir(parents=True)
    row = {'schema_version': '2.1', 'state_id': 's', 'run_id': 'runA',
           'barcode': 'b', 'round_barcode': 'runA_0',
           'sample_metrics': {'sample': {'sample_label': label}}}
    wire = json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n' + tail
    history.write_bytes(wire)
    result = subprocess.run(
        ['/bin/bash', str(REPO / 'bin/report_rebuild.sh'), '--outdir', str(outdir),
         '--state-id', 's', '--history', str(history), '--run-id', 'runA', '--skip-root-report'],
        capture_output=True, text=True, check=False,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    assert result.returncode == 0, result.stderr
    run_report = outdir / 'report_html/runs/runA/run_report.json'
    run_index = outdir / 'report_html/runs_index.jsonl'
    assert run_report.is_file() and run_index.is_file()
    assert json.loads(run_report.read_text())['schema_version'] == '2.1'
    assert json.loads(run_index.read_text())['schema_version'] == '2.1'
    assert history.read_bytes() == wire
    html = (outdir / 'report_html/runs/runA/report.html').read_text(encoding='utf-8')
    payload = json.loads(html.split('window.REPORT_PAYLOAD = ', 1)[1].split(';\n', 1)[0])
    rendered = payload['rounds'][0]
    assert all(rendered[key] == value for key, value in row.items() if key != 'sample_metrics')
    assert rendered['sample_metrics']['sample']['sample_label'] == label

@pytest.mark.parametrize('fault',['unknown-tsv','unknown-pdf','wrong-depth','empty-unknown-dir','ancestor-alias','leaf-directory','history-traversal','history-unknown','mismatched-sample','unsafe-sample'])
def test_presentation_namespace_refuses_unknown_destinations(tmp_path,fault):
    t1=joint_fixture(tmp_path/'t1');t2=tmp_path/'t2';shutil.copytree(t1,t2)
    (t2/'tables/to_figures/embedded').mkdir(parents=True)
    if fault=='unknown-tsv':(t2/'tables/to_figures/embedded/new.tsv').write_text('unknown')
    elif fault=='unknown-pdf':(t2/'plots/pdf/embedded').mkdir(parents=True);(t2/'plots/pdf/embedded/run_new.pdf').write_text('unknown')
    elif fault=='wrong-depth':(t2/'plots/pdf/embedded/nested').mkdir(parents=True);(t2/'plots/pdf/embedded/nested/run_reads_fate.pdf').write_text('unknown')
    elif fault=='empty-unknown-dir':(t2/'plots/pdf/embedded/unknown').mkdir(parents=True)
    elif fault=='ancestor-alias':
        (t2/'plots').mkdir();outside=tmp_path/'outside';outside.mkdir();(t2/'plots/pdf').symlink_to(outside,target_is_directory=True)
    elif fault=='leaf-directory':(t2/'tables/to_figures/embedded/reads_fate_per_round.tsv').mkdir()
    else:
        path={'history-traversal':'runs/runA/report_assets/../../../current/state/s/state_authority/x.pdf','history-unknown':'runs/runA/report_assets/embedded/run_new.pdf','mismatched-sample':'runs/runA/report_assets/samples/S_A/other_reads_time_history.pdf','unsafe-sample':'runs/runA/report_assets/embedded/run_reads_fate.pdf'}[fault]
        sample='..' if fault=='unsafe-sample' else 'S A'
        (t2/'tables/report_history.jsonl').write_text(json.dumps({'sample_metrics':{sample:{}},'figures':[{'path':'report_assets/ok.png','pdf_path':path}]})+'\n')
    before=byte_inventory(t2)
    result=auth('select','s',tmp_path/'live',t1,t2)
    assert result.returncode!=0,result.stderr
    assert byte_inventory(t2)==before

# The wire oracle deliberately signs invalid scientific group combinations too;
# successful parsing of its envelope alone must never authorize restoration.
def reseal_wire(lines):
    body=b''.join(lines)
    ng=sum(x.startswith(b"group\t") for x in lines)
    ne=sum(x.startswith(b"entry\t") for x in lines)
    return body+f"#END\t{ng}\t{ne}\t{digest(body)}\n".encode()


@pytest.mark.parametrize('family', ['f01','cache','consensus','sup','blast.1','parser.62','fate.62','grace.62'])
def test_each_required_inventory_family_cannot_be_dropped(tmp_path,family):
    root=joint_fixture(tmp_path/'root');record=root/'state_authority/AUTHORITY'
    lines=[line for line in record.read_bytes().splitlines(keepends=True)[:-1]
           if not line.startswith((f'group\t{family}\t'.encode(),f'entry\t{family}\t'.encode()))]
    record.write_bytes(reseal_wire(lines))
    before=byte_inventory(root)
    result=auth('select','s',tmp_path/'live',root,tmp_path/'absent')
    assert result.returncode!=0,result.stdout
    assert byte_inventory(root)==before and not (tmp_path/'live').exists()


@pytest.mark.parametrize('family,names',[('parser',PARSER_NAMES),('read-fate',FATE_NAMES)])
def test_every_partial_unit_subset_refuses_with_an_independent_envelope(tmp_path,family,names):
    for mask in range(1,(1<<len(names))-1):
        root=joint_fixture(tmp_path/f'{family}-{mask}')
        for i,name in enumerate(names):
            if not (mask&(1<<i)):(root/'state_authority'/f'b_{name}').unlink()
        (root/'state_authority/AUTHORITY').write_bytes(fixture_record(root))
        before=byte_inventory(root)
        r=auth('verify',root)
        assert r.returncode!=0,(family,mask,r.stdout)
        assert byte_inventory(root)==before


VECTOR_PATHS=['sequences/Consensus/consensus_ownership.tsv','sequences/Consensus/consolidated_consensus_ids.txt']+['state_authority/'+n for n in ['blastreport_sup_annotated_pre.fastq','blastreport_sup_annotated_pre.fastq.gz']]+['state_authority/b_'+n for n in GRACE_NAMES]
@pytest.mark.parametrize('path',VECTOR_PATHS)
@pytest.mark.parametrize('state',['absent','empty','present'])
def test_optional_vector_members_preserve_exact_presence_and_bytes(tmp_path,path,state):
    root=joint_fixture(tmp_path/'root');p=root/path
    if p.exists():p.unlink()
    expected=None if state=='absent' else b'' if state=='empty' else b'B1\tOTU7|COI\nB1\tOTU7|COI\n'
    if expected is not None:p.write_bytes(expected)
    (root/'state_authority/AUTHORITY').write_bytes(fixture_record(root))
    result=auth('verify',root);assert result.returncode==0,result.stderr
    live=tmp_path/'live';live.mkdir()
    result=auth('install',root,live/'_state');assert result.returncode==0,result.stderr
    destination=live/('_state/'+Path(path).name if path.startswith('state_authority/') else path.replace('sequences/','',1))
    assert (destination.read_bytes() if destination.exists() else None)==expected
    # The same signed record must reject each stale byte/presence substitution.
    if expected is None:p.write_bytes(b'')
    else:p.write_bytes(b'x'+expected[1:] if expected else b'x')
    assert auth('verify',root).returncode!=0


@pytest.mark.parametrize('mode',['absent','empty','nested-empty','metadata-only','pool-only','binary-names'])
def test_cache_exact_tree_state_matrix(tmp_path,mode):
    root=joint_fixture(tmp_path/'root');cache=root/'sequences/Consensus/.cache';shutil.rmtree(cache)
    if mode!='absent':cache.mkdir()
    if mode=='nested-empty':(cache/'nested/empty').mkdir(parents=True)
    if mode=='metadata-only':(cache/'OTU.meta').write_bytes(b'owner\tOTU\n')
    if mode=='pool-only':(cache/'OTU.pool.tsv').write_bytes(b'r1\tACGT\n')
    if mode=='binary-names':(cache/'name\tline\n.bin').write_bytes(b'\0\xff')
    (root/'state_authority/AUTHORITY').write_bytes(fixture_record(root))
    result=auth('verify',root);assert result.returncode==0,result.stderr
    if mode=='absent':cache.mkdir()
    else:(cache/'extra').write_bytes(b'')
    assert auth('verify',root).returncode!=0


@pytest.mark.parametrize('mutation',['identity-swap','bool-number'])
def test_same_content_inode_swap_and_json_type_change_are_not_accepted(tmp_path,monkeypatch,mutation):
    bindir=tmp_path/'fault/bin';shutil.copytree(REPO/'bin',bindir)
    p=bindir/REPAIR.name;s=p.read_text();anchor='        run_command(run_cmd)\n';assert s.count(anchor)==1
    code="p=last_round_dir/f'{barcode}_demult_rpt.txt'; data=p.read_bytes(); p.unlink(); p.write_bytes(data)" if mutation=='identity-swap' else "p=last_round_dir/'round_report.json'; o=json.loads(p.read_text()); o['read_fate']['demux_total_reads']=True; p.write_text(json.dumps(o)+'\\n')"
    p.write_text(s.replace(anchor,anchor+'        '+code+'\n'));monkeypatch.setitem(globals(),'REPAIR',p)
    case=tmp_path/'case';case.mkdir()
    with pytest.raises(AssertionError,match='retained producer changed|bounded read_fate mismatch'):
        test_fresh_worker_owns_pin_and_verifies_terminal_normalization(case)
    assert (case/'s/_state/.read_fate_normalization_pending').exists()
    assert not (case/'worker/success.receipt').exists()

@pytest.mark.parametrize('mode',['check','live','offline','help','invalid','feeder'])
def test_no_order_option_preserves_immutable_legacy_bytes(tmp_path,mode):
    baseline=subprocess.run(['git','show','e5e592e17f12aab54a7b26ad1ce70f74533b64e5:bin/report_read_fate_repair.py'],cwd=REPO,capture_output=True)
    assert baseline.returncode==0,baseline.stderr
    oldbin=tmp_path/'old/bin';shutil.copytree(REPO/'bin',oldbin)
    (oldbin/REPAIR.name).write_bytes(baseline.stdout)
    clock=tmp_path/'clock';clock.mkdir()
    (clock/'FixedClock.pm').write_text('package FixedClock; BEGIN {*CORE::GLOBAL::time=sub(){1700000000}; *CORE::GLOBAL::gmtime=sub{CORE::gmtime(1700000000)};} 1;\n')
    env={**os.environ,'PERL5OPT':f'-I{clock} -MFixedClock','PERL_HASH_SEED':'0','PERL_PERTURB_KEYS':'0','PYTHONDONTWRITEBYTECODE':'1'}
    case=tmp_path/'case';results=[]
    for script in (oldbin/REPAIR.name,REPAIR):
        if case.exists():shutil.rmtree(case)
        case.mkdir();state,mapping=rounds(case,['runA_0','runA_1','runA_2'])
        output=case/'output';output.mkdir()
        args=['--state-dir',str(state),'--targets','COI','--target-taxa','Metazoa','--outdir',str(output),'--skip-render']
        if mode=='check':args+=['--check-live-order','--current-round-barcode','runA_2']
        elif mode=='live':args+=['--live','--current-round-barcode','runA_2']
        elif mode=='help':args=['--help']
        elif mode=='invalid':args=['--not-a-valid-option']
        elif mode=='feeder':
            feeder=case/'feeder';feeder.mkdir()
            args+=['--valid-rounds-from-feeder-metadata',str(feeder)]
        result=subprocess.run([sys.executable,str(script),*args],capture_output=True,env=env)
        # argparse identifies argv[0]; the interface status/message is otherwise exact.
        err=result.stderr.replace(str(script).encode(),b'REPAIR')
        results.append((result.returncode,result.stdout,err,byte_inventory(case)))
    assert results[0]==results[1],mode


@pytest.mark.parametrize('names', [['runA_0','runA_1','runA_2'],['runA_1','runB_1'],['runB_1','runA_1'],['runA_8','runB_0']])
def test_authoritative_order_actual_helper_expected_sidecars(tmp_path,names):
    state,mapping=rounds(tmp_path,names)
    out=tmp_path/'out';out.mkdir()
    r=subprocess.run([sys.executable,str(REPAIR),'--state-dir',str(state),'--round-index-file',str(mapping),'--live','--skip-render','--current-round-barcode',names[-1],'--targets','COI','--target-taxa','Metazoa','--outdir',str(out)],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    assert (state/'_state/B1_read_fate_demux_seen.tsv').read_bytes()==''.join(f'r{i}\n' for i in range(1,len(names)+1)).encode()
    assert (state/'_state/B1_read_fate_blast_seen.tsv').read_bytes()==b''
    expected=DEMULT+''.join(f'r{i}\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n' for i in range(1,len(names)+1))
    assert (state/'_state/B1_demux_annotation_cache.tsv').read_text()==expected
    assert [json.loads(row)['round_barcode'] for row in (state/'_state/report_history.jsonl').read_text().splitlines()]==names

@pytest.mark.parametrize('mutation',['skip-run-validation','early-pending-clear','early-success-receipt'])
def test_verifier_commit_order_mutants_are_killed(tmp_path,monkeypatch,mutation):
    bindir=tmp_path/'mutant/bin';shutil.copytree(REPO/'bin',bindir)
    path=bindir/AUTH.name;s=path.read_text()
    if mutation=='skip-run-validation':
        start=s.index('    actual = json.loads(read(expected["run_path"]))')
        end=s.index('\n\ndef verify_r4_contract',start)
        s=s[:start]+'    verify_r4_contract(ctx, expected)\n'+s[end:]
    else:
        anchor='    receipts, latest = [], {}\n';assert s.count(anchor)==1
        code='    witness.unlink()\n' if mutation=='early-pending-clear' else '    exclusive(control / "success.receipt", b"premature\\n")\n'
        s=s.replace(anchor,anchor+code)
    path.write_text(s);monkeypatch.setitem(globals(),'AUTH',path)
    case=tmp_path/'case';case.mkdir()
    if mutation=='skip-run-validation':
        with pytest.raises(pytest.fail.Exception,match='DID NOT RAISE'):
            test_bounded_output_mutants_fail_before_pending_commit(case,monkeypatch,'run-json','bounded run-report mismatch')
    elif mutation=='early-pending-clear':
        # The unchanged worker detects the missing witness before invoking the
        # helper, but the mutant has already destroyed the replay obligation.
        with pytest.raises(AssertionError,match='missing required retained input'):
            test_fresh_worker_owns_pin_and_verifies_terminal_normalization(case)
        with pytest.raises(AssertionError,match='replay obligation lost'):
            assert (case/'s/_state/.read_fate_normalization_pending').is_file(), 'replay obligation lost'
        assert not (case/'worker/success.receipt').exists()
    else:
        with pytest.raises(AssertionError):
            test_worker_refuses_corrupt_bounded_read_fate_json_before_pending_commit(case,monkeypatch)


def test_worker_refuses_lost_original_optional_input_before_blessing_absence(tmp_path):
    """An originally empty read-info file filters all IDs; losing it must not
    turn that historical input into a legitimately absent (unfiltered) input.
    No repair or report-builder mutant is involved in this negative control.
    """
    live,helper,token,pin,env=authenticated_fixture(tmp_path)
    rd=live/'runA_0';rd.mkdir()
    obj=dict(schema_version='1.6',state_id='s',barcode='b',round_barcode='runA_0',run_id='runA')
    (rd/'round_report.json').write_text(json.dumps(obj)+'\n')
    (rd/'b_demult_rpt.txt').write_text(DEMULT+'r1\tCOI\thac\ts\tnanopore\tgrab\tsub\t1\tsample\ts\n')
    (rd/'b_blast_otu_pretax_rpt.txt').write_text(BLAST)
    info=rd/'b_read_info_rpt.txt';info.write_bytes(b'')
    (rd/'b_on_target_rpt.txt').write_bytes(b'')
    original_report=subprocess.run(['perl',str(REPO/'bin/report_round_json.pl'),'--run-id','runA','--barcode','b','--round-barcode','runA_0','--state-id','s','--schema-version','1.6','--targets','COI','--target-taxa','Metazoa','--read-info',str(info),'--demult',str(rd/'b_demult_rpt.txt'),'--blast-otu',str(rd/'b_blast_otu_pretax_rpt.txt'),'--out',str(rd/'round_report.json')],capture_output=True,text=True)
    assert original_report.returncode==0,original_report.stderr
    initial_output=tmp_path/'initial-output';initial_output.mkdir()
    initial=subprocess.run([sys.executable,str(REPAIR),'--live','--skip-render','--state-dir',str(live),'--current-round-barcode','runA_0','--round-index-file',str(live/'_state/round_index.tsv'),'--targets','COI','--target-taxa','Metazoa','--outdir',str(initial_output)],capture_output=True,text=True)
    assert initial.returncode==0,initial.stderr
    first=rd/'b_read_fate_demult_first_seen.tsv'
    assert first.read_text()==DEMULT
    info.unlink()  # loss of an original retained input, not legitimate absence
    history=live/'_state/report_history.jsonl';history.write_bytes(history.read_bytes()*2)
    roots,root_before=seeded_roots(tmp_path)
    science_before=live_science_bytes(live)
    try:
        control,_=run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    except AssertionError as error:
        # A completed implementation must diagnose the missing original input.
        assert 'original' in str(error).lower() or 'retained' in str(error).lower(),str(error)
        assert not (tmp_path/'worker/success.receipt').exists()
        assert not (live/'_state/.read_fate_normalization_pending').exists()
        assert first.read_text()==DEMULT
        assert live_science_bytes(live)==science_before
        assert [byte_inventory(root) for root in roots]==root_before
        assert not (tmp_path/'candidate').exists()
        return
    assert first.read_text()!=DEMULT, 'loss did not reach the first-seen scientific output'
    assert not (control/'success.receipt').exists(), (
        'worker accepted loss of original empty read-info as legitimate absence; '
        'it changed the historical first-seen set and removed pending evidence')


def test_immutable_round_fields_cannot_change_json_type(tmp_path):
    control=tmp_path/'worker'
    emitted=auth('emit-worker',control,'5','60');assert emitted.returncode==0,emitted.stderr
    driver=runpy.run_path(str(control/'driver.py'))
    live=tmp_path/'s';state=live/'_state';state.mkdir(parents=True)
    rd=live/'runA_0';rd.mkdir()
    original={'barcode':'b','round_barcode':'runA_0','immutable_numeric_extension':1}
    changed={**original,'immutable_numeric_extension':True,'read_fate':{},'warnings':[]}
    report=rd/'round_report.json';report.write_text(json.dumps(changed)+'\n')
    (state/'report_history.jsonl').write_text(json.dumps(changed)+'\n')
    fate=[b'',b'',DEMULT.encode()]
    for suffix,value in zip(FATE_NAMES,fate):(state/f'b_{suffix}').write_bytes(value)
    with pytest.raises(RuntimeError,match='stable report fields changed'):
        driver['verify_outputs'](live,[(rd,original)],fate,{}, {str(report):original})
