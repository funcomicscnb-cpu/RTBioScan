#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: consensus_threshold_mask.sh <levels.tsv> <tax_join.tsv>" 1>&2
  exit 2
fi

levels="$1"
tax_join="$2"

# levels.tsv:   qseqid<TAB>level  (level in {species,genus,family,unassigned})
# tax_join.tsv: idx<TAB>qseqid<TAB>sseqid<TAB>taxid<TAB>kingdom<TAB>phylum<TAB>class<TAB>order<TAB>family<TAB>genus<TAB>species
#
# Masking rules (strict — unknown/missing level => unassigned, no leak path):
#   species    => keep all ranks
#   genus      => mask species
#   family     => mask genus + species
#   unassigned => mask family + genus + species
#   anything else (including "", numeric) => treat as unassigned
awk -F'\t' -v OFS='\t' -v _lf="$levels" '
	FILENAME==_lf { lev[$1]=$2; next }
	{
		q=$2; s=$3;
		k=$5; p=$6; c=$7; o=$8; f=$9; g=$10; sp=$11;
		l=(q in lev) ? lev[q] : "";
		if      (l == "genus")    { sp="Unassigned"; }
		else if (l == "family")   { g="Unassigned"; sp="Unassigned"; }
		else if (l != "species")  { f="Unassigned"; g="Unassigned"; sp="Unassigned"; }
		print q, s, k, p, c, o, f, g, sp;
	}
' "$levels" "$tax_join"
