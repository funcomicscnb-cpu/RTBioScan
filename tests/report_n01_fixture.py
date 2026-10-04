"""Manufactured N-01 reporter-table fixture, sealed design 20261003T224122Z.

S1R-003 projection: 192 unique reads, eight OTUs, two samples/replicates/markers,
three rounds. This exercises reporter contracts only, not the S1 pipeline campaign.
Independent group/minimum oracles are reproduced from the sealed design sources.
"""
import csv
import json
import re
import shutil
from pathlib import Path


def tsv(path, header, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(header)
        writer.writerows(rows)


def make_fixture(ROOT):
 B=ROOT/'bundle';B.mkdir()
 # The exact composed cell identities and input multiset are retained below.
 c={'plate_identity_design': {'composed_cells': CELLS}}

 # Table-level diagnostic only; no sequence, model, engine, S1 fixture or executable constructed.
 inputs=ROOT/'inputs';inputs.mkdir(exist_ok=True);members=[]
 for rnd in range(1,4):
  for cell in c['plate_identity_design']['composed_cells']:
   for marker in ['COI','ITS2']:
    for otu in ['A','B']:
     for ri in range(1,5):
      sample=cell['sample'];rep=int(cell['replicate'][1:]);unit=f'{sample}_{marker}_{rep}';oid=f'OTUB_{sample}_{otu}-{marker}';rid=f'stateA_round{rnd}_{sample}_R{rep}_{cell["plate"]}_{cell["well"]}_{marker}_{otu}_{ri}'
      members.append(dict(read_id=rid,sample=sample,replicate=rep,plate=cell['plate'],well=cell['well'],marker=marker,round=rnd,otu=oid,unit=unit))
 assert len(members)==192 and len({m['read_id'] for m in members})==192
 (B/'INPUT_MULTISET.json').write_text(json.dumps(members,indent=2)+'\n')
 expected={}
 for m in members:
  e=expected.setdefault(m['otu'],{}).setdefault(m['sample'],{});label=f'rep_{m["replicate"]}';e[label]=e.get(label,0)+1
 expected={otu:{sam:[dict(count=n,label=label) for label,n in sorted(reps.items(),key=lambda x:(int(x[0].rsplit('_',1)[1]),x[0]))] for sam,reps in samples.items()} for otu,samples in expected.items()}
 (B/'EXPECTED_REPLICATE_CELL.json').write_text(json.dumps(expected,sort_keys=True,separators=(',',':'))+'\n')
 (B/'EXPECTED_STATE.json').write_text(json.dumps(dict(state_id='stateA',run_id='N01_diagnostic',round_barcode='round3',identity_mode='collapse',members=192,final_per_sample_marker=48,per_sample_marker_replicate=24,otu_count=8),indent=2)+'\n')
 for order in [(1,2),(2,1)]:
  d=inputs/('order'+''.join(map(str,order)));d.mkdir(exist_ok=True)
  ms=sorted(members,key=lambda m:order.index(m['replicate']));current=[m for m in ms if m['round']==3]
  tsv(d/'members.tsv',['read_id','barcode_by_homology','sample','OTU_id','OTU_role'],[[m['read_id'],m['marker'],m['unit'],m['otu'],'MEMBER'] for m in ms])
  bh=['read_id','barcode_by_homology','basecalling_model','sample','hit_id','taxid','aln_length','perc_id','otu_id','otu_taxid','otu_kingdom','otu_phylum','otu_class','otu_order','otu_family','otu_genus','otu_species']
  def row(m):
   n=1 if m['marker']=='COI' else 2;otu=m['otu'].split('_')[-1].split('-')[0]
   return [m['read_id'],m['marker'],'sup',m['unit'],'hit_'+m['marker']+'_'+otu,100+n,320,100,m['otu'],100+n,'Metazoa' if n==1 else 'Viridiplantae','P'+str(n),'C'+str(n),'O'+str(n),'F'+str(n),'G'+str(n),'Sp'+str(1 if otu=='A' else 2)]
  tsv(d/'blast.tsv',bh,[row(m) for m in ms]);tsv(d/'blast_current.tsv',bh,[row(m) for m in current])
  tsv(d/'sizes.tsv',['otu_id','size'],[[k,24] for k in sorted(expected)])
  tsv(d/'lock.tsv',['otu_key','effective_consolidated','is_frozen'],[[k,0,0] for k in sorted(expected)])
  (d/'roster.tsv').write_text('S1\nS2\n')
  units=sorted({(m['sample'],m['marker'],m['replicate'],m['unit']) for m in ms})
  tsv(d/'identity.tsv',['sample_id','marker_id','suffix_resolution_mode','unit_suffix_current','unit_id_collapse'],[[s,m,'fallback',m+'_'+str(r),u] for s,m,r,u in units])
  tsv(d/'read_info.tsv',['read_id','filename','run_id','barcode','fast_length','fast_mean_qscore','sup_length','sup_mean_qscore'],[[m['read_id'],'diagnostic','N01_diagnostic','RTBioScan',320,30,320,30] for m in current])
  tsv(d/'on_target.tsv',['read_id','barcode_by_homology','basecalling_model'],[[m['read_id'],m['marker'],'sup'] for m in current])
  tsv(d/'demult.tsv',['read_id','barcode_by_homology','sample','basecalling_model'],[[m['read_id'],m['marker'],m['unit'],'sup'] for m in current])
  tsv(d/'consensus.tsv',['consensus_id','sample','barcode_by_homology','number_of_reads','perc_id','aln_length','consensus_family','consensus_genus','consensus_species'],[[f'cons_{s}_{m}',s,m,24,100,320,'F'+str(1 if m=='COI' else 2),'G'+str(1 if m=='COI' else 2),'Sp1'] for s in ['S1','S2'] for m in ['COI','ITS2']])
  tsv(d/'round_index.tsv',['round_barcode','round_index'],[['round3',3]])
  (d/'empty.list').write_text('')


CELLS = [{'plate': 'P1', 'replicate': 'R1', 'sample': 'S1', 'well': 'A01'}, {'plate': 'P2', 'replicate': 'R2', 'sample': 'S1', 'well': 'A01'}, {'plate': 'P2', 'replicate': 'R1', 'sample': 'S2', 'well': 'B01'}, {'plate': 'P1', 'replicate': 'R2', 'sample': 'S2', 'well': 'B01'}]

CASES={
 'species_original_conflict':{'level':'species','A':('Parent_F1','Parent_G1','Shared_Species'),'B':('Parent_F9','Parent_G9','Shared_Species')},
 'genus_original_conflict':{'level':'genus','A':('Parent_F1','Shared_Genus','Sp1'),'B':('Parent_F9','Shared_Genus','Sp2')},
 'species_crossed_lineage':{'level':'species','A':('Parent_F1','Parent_G9','Shared_Species'),'B':('Parent_F9','Parent_G1','Shared_Species')},
 'species_missing_before_populated':{'level':'species','A':('', 'Parent_G1','Shared_Species'),'B':('Parent_F9','','Shared_Species')},
 'species_family_missing':{'level':'species','A':('', 'Parent_G9','Shared_Species'),'B':('', 'Parent_G1','Shared_Species')},
 'species_all_ancestors_missing':{'level':'species','A':('', '', 'Shared_Species'),'B':('', '', 'Shared_Species')},
 'genus_missing_family':{'level':'genus','A':('', 'Shared_Genus','Sp1'),'B':('Parent_F9','Shared_Genus','Sp2')},
 'species_equal_parents':{'level':'species','A':('Parent_F1','Parent_G1','Shared_Species'),'B':('Parent_F1','Parent_G1','Shared_Species')},
}
def scalar(x):return x if x not in (None,'') else None
def sortfield(v):return (v in (None,''), (v or '').encode('utf8'))
def oracle(records,level):
 fields=['family'] if level=='genus' else ['family','genus'];selected=min(records,key=lambda r:tuple(sortfield(r.get(f)) for f in fields)+(r['id'].encode('utf8'),));return selected

def make_inputs(root,case,order,active):
 shutil.rmtree(active,ignore_errors=True);shutil.copytree(root/'inputs'/('order'+order),active)
 for fn in ['blast.tsv','blast_current.tsv']:
  f=active/fn;rd=csv.DictReader(f.open(),delimiter='\t');cols=rd.fieldnames;rows=list(rd)
  for row in rows:
   if row['sample'].startswith('S1_COI_'):
    tag='B' if '_B-' in row['otu_id'] else 'A';fam,gen,spe=case[tag];row['otu_family']=fam;row['otu_genus']=gen;row['otu_species']=spe;row['taxid']=row['otu_taxid']='901' if tag=='B' else '101'
  tsv(f,cols,[[x[c] for c in cols] for x in rows])
 rows=[]
 for sample in ['S1','S2']:
  for marker,n in [('COI',1),('ITS2',2)]:
   for tag,sp in [('A','Sp1'),('B','Sp2')]:
    fam,gen,spe=case[tag] if sample=='S1' and marker=='COI' else ('F'+str(n),'G'+str(n),sp)
    rows.append([f'cons_{sample}_{marker}_{tag}',sample,marker,24,100,320,fam,gen,spe,100+n])
 if order=='21':rows.reverse()
 tsv(active/'consensus.tsv',['consensus_id','sample','barcode_by_homology','number_of_reads','perc_id','aln_length','consensus_family','consensus_genus','consensus_species','taxid'],rows)

def input_groups(active,source):
 groups={}
 if source=='otu':
  rd=list(csv.DictReader((active/'blast.tsv').open(),delimiter='\t'));items={}
  for row in rd:
   sample=re.sub(r'_(COI|ITS2)_\d+$','',row['sample']);key=(row['otu_id'],sample,row['barcode_by_homology']);it=items.setdefault(key,dict(id=row['otu_id'],sample=sample,marker=row['barcode_by_homology'],family=scalar(row['otu_family']),genus=scalar(row['otu_genus']),species=scalar(row['otu_species']),members=set()));it['members'].add(row['read_id'])
  records=list(items.values())
 else:
  rd=list(csv.DictReader((active/'consensus.tsv').open(),delimiter='\t'));records=[dict(id=row['consensus_id'],sample=row['sample'],marker=row['barcode_by_homology'],family=scalar(row['consensus_family']),genus=scalar(row['consensus_genus']),species=scalar(row['consensus_species']),weight=int(row['number_of_reads'])) for row in rd]
 for row in records:
  for lev in ['family','genus','species']:
   if row[lev] is None:continue
   key=(lev,row[lev],row['sample'],row['marker']);groups.setdefault(key,[]).append(row)
 return groups

def validate_obj(obj,active,expected_replicates):
 assert obj['state_id']=='stateA' and obj['round_barcode']=='round3' and obj['reads']['total']==obj['reads']['on_target']==64
 assert obj['otu']['replicate_reads']==expected_replicates
 evidence={}
 for src in ['otu','consensus']:
  groups=input_groups(active,src);checks=[]
  for lev in ['family','genus','species']:
   rows=obj[src]['assignments_by_level'][lev];assert len(rows)==sum(k[0]==lev for k in groups)
   for row in rows:
    key=(lev,row['taxon'],row['sample'],row['marker']);members=groups[key];cnt=len({x['id'] for x in members});weight=len(set().union(*(x['members'] for x in members))) if src=='otu' else sum(x['weight'] for x in members)
    assert row['otu_count' if src=='otu' else 'consensus_count']==cnt and row['reads_total']==weight
    assert row['frozen_otu_count' if src=='otu' else 'consolidated_consensus_count']==0
    if lev in ['genus','species']:
     chosen=oracle(members,lev);assert row['family']==chosen['family'],(key,row,chosen)
     if lev=='species':assert row['genus']==chosen['genus'],(key,row,chosen)
    for field in (['genus','species'] if lev=='family' else ['species'] if lev=='genus' else []):
     vals=[x[field] for x in members if x[field] not in (None,'')];expect=min(vals,key=lambda x:x.encode('utf8')) if vals else None;assert row[field]==expect,(key,row,field,expect)
    checks.append({'group_key':key,'count':cnt,'reads':weight,'selected_source':oracle(members,lev)['id'] if lev in ['genus','species'] else None})
  evidence[src]=checks
 return evidence
