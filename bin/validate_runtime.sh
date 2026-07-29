#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage:
  bin/validate_runtime.sh \
    --taxonomy-data-dir DIR \
    --last-index PREFIX

Validates the pinned RTBioScan command-line runtime, the LAST-index builder
version, representative BLAST/SeqKit/Cutadapt behavior, and the TaxonKit
superkingdom/kingdom semantics used by the taxonomy classifier.
EOF
}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

taxonomy_dir=""
last_index=""
script_dir="$(cd "$(dirname "$0")" && pwd -P)"
repo_root="$(cd "${script_dir}/.." && pwd -P)"
routing_fixture="${repo_root}/conf/runtime_validation/fast_routing_endosymbionts.fa"
routing_expected="${repo_root}/conf/runtime_validation/fast_routing_endosymbionts.expected.tsv"
while [ "$#" -gt 0 ]; do
	case "$1" in
		--taxonomy-data-dir)
			[ "$#" -ge 2 ] || die "--taxonomy-data-dir requires a value"
			taxonomy_dir="$2"
			shift 2
			;;
		--last-index)
			[ "$#" -ge 2 ] || die "--last-index requires a value"
			last_index="$2"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			die "unknown argument: $1"
			;;
	esac
done

[ -n "$taxonomy_dir" ] || die "--taxonomy-data-dir is required"
[ -d "$taxonomy_dir" ] || die "taxonomy directory not found: $taxonomy_dir"
[ -n "$last_index" ] || die "--last-index is required"
[ -f "${last_index}.prj" ] || die "LAST project file not found: ${last_index}.prj"
[ -r "$routing_fixture" ] || die "FAST routing fixture not found: $routing_fixture"
[ -r "$routing_expected" ] || die "FAST routing expectation not found: $routing_expected"

for taxonomy_file in nodes.dmp names.dmp merged.dmp delnodes.dmp; do
	[ -r "${taxonomy_dir}/${taxonomy_file}" ] \
		|| die "taxonomy artifact is not readable: ${taxonomy_dir}/${taxonomy_file}"
done

for tool in blastn makeblastdb lastal seqkit taxonkit cutadapt; do
	command -v "$tool" >/dev/null 2>&1 || die "required runtime tool not found: $tool"
done

blast_version="$(blastn -version 2>&1 | awk 'NR == 1 { sub(/^blastn:[[:space:]]*/, ""); sub(/[+].*$/, ""); print; exit }')"
last_version="$(lastal --version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) { print $i; exit } }')"
seqkit_version="$(seqkit version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^v?[0-9]+[.][0-9]+[.][0-9]+$/) { sub(/^v/, "", $i); print $i; exit } }')"
taxonkit_version="$(taxonkit version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^v?[0-9]+[.][0-9]+[.][0-9]+$/) { sub(/^v/, "", $i); print $i; exit } }')"
cutadapt_version="$(cutadapt --version 2>&1 | awk 'NR == 1 { print $1; exit }')"

[ "$blast_version" = "2.15.0" ] || die "blastn version mismatch: expected 2.15.0, found ${blast_version:-unknown}"
[ "$last_version" = "1542" ] || die "lastal version mismatch: expected 1542, found ${last_version:-unknown}"
[ "$seqkit_version" = "2.6.1" ] || die "seqkit version mismatch: expected 2.6.1, found ${seqkit_version:-unknown}"
[ "$taxonkit_version" = "0.14.2" ] || die "taxonkit version mismatch: expected 0.14.2, found ${taxonkit_version:-unknown}"
[ "$cutadapt_version" = "4.6" ] || die "cutadapt version mismatch: expected 4.6, found ${cutadapt_version:-unknown}"

index_version="$(awk -F= '$1 == "version" { print $2; exit }' "${last_index}.prj")"
[ -n "$index_version" ] || die "LAST project file has no version field: ${last_index}.prj"
[ "$index_version" = "$last_version" ] \
	|| die "LAST index/runtime mismatch: index=${index_version}, lastal=${last_version}"

runtime_tmp="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan-runtime.XXXXXX")" \
	|| die "cannot create temporary runtime-validation directory"
cleanup() {
	rm -rf "$runtime_tmp"
}
trap cleanup EXIT HUP INT TERM

lastal "$last_index" "$routing_fixture" -f BlastTab -P 1 \
	| awk '!/^#/ && !seen[$1]++ {
		print $1 "\t" $2 "\t" $4 "\t" $5 "\t" $6 "\t" \
			$7 "\t" $8 "\t" $9 "\t" $10
	}' \
	> "${runtime_tmp}/last-routing.tsv" \
	|| die "lastal ${last_version} could not read index '${last_index}'"
if ! diff -u "$routing_expected" "${runtime_tmp}/last-routing.tsv"; then
	die "LAST FAST-routing fixture diverged from the pinned 1542 baseline"
fi

cat > "${runtime_tmp}/blast-ref.fa" <<'EOF'
>animal_ref
TAGCCTCCTTATTCGAGCCGAGCTGGGCCAGCCAGGCAACCTTCTAGGTAACGACCACATCTACAACGTT
>bacteria_ref
GGTGCTGCTTATTCGATCCGAATTGGACCTGCTCGTCAACCTGTTAGGTAATGATCATATTTATAATGTT
EOF
cat > "${runtime_tmp}/blast-query.fa" <<'EOF'
>bacterial_probe
GGTGCTGCTTATTCGATCCGAATTGGACCTGCTCGTCAACCTGTTAGGTAATGATCATATTTATAATGTT
EOF
makeblastdb -in "${runtime_tmp}/blast-ref.fa" -dbtype nucl \
	-out "${runtime_tmp}/blast-ref" >/dev/null 2>&1
blastn -task megablast -dust no -query "${runtime_tmp}/blast-query.fa" \
	-db "${runtime_tmp}/blast-ref" -num_threads 1 \
	-perc_identity 92 -evalue 11 -max_hsps 50 -max_target_seqs 1 \
	-word_size 50 -qcov_hsp_perc 50 -mt_mode 2 \
	-outfmt '6 qseqid sseqid pident length bitscore' \
	> "${runtime_tmp}/blast.out"
blast_best="$(awk 'NR == 1 { print $2; exit }' "${runtime_tmp}/blast.out")"
[ "$blast_best" = "bacteria_ref" ] \
	|| die "BLAST behavioral probe selected '${blast_best:-no hit}', expected bacteria_ref"

cat > "${runtime_tmp}/seqkit-input.fa" <<'EOF'
>keep
ACGT--AC
>drop
AC
EOF
seqkit seq -g -m 4 -M 8 "${runtime_tmp}/seqkit-input.fa" \
	> "${runtime_tmp}/seqkit.out"
grep -q '^>keep$' "${runtime_tmp}/seqkit.out" \
	|| die "SeqKit behavioral probe lost the expected retained sequence"
if grep -q '^>drop$' "${runtime_tmp}/seqkit.out"; then
	die "SeqKit behavioral probe retained a below-minimum sequence"
fi
grep -q '^ACGTAC$' "${runtime_tmp}/seqkit.out" \
	|| die "SeqKit behavioral probe did not remove gap characters"

cat > "${runtime_tmp}/adapters.fa" <<'EOF'
>COI
ACGT
EOF
cat > "${runtime_tmp}/cutadapt-input.fastq" <<'EOF'
@read1
ACGTTTGG
+
IIIIIIII
EOF
cutadapt -g "file:${runtime_tmp}/adapters.fa" --discard-untrimmed \
	-o "${runtime_tmp}/cutadapt.out.fastq" "${runtime_tmp}/cutadapt-input.fastq" \
	> "${runtime_tmp}/cutadapt.log"
cutadapt_sequence="$(awk 'NR == 2 { print; exit }' "${runtime_tmp}/cutadapt.out.fastq")"
[ "$cutadapt_sequence" = "TTGG" ] \
	|| die "Cutadapt behavioral probe produced '${cutadapt_sequence:-no read}', expected TTGG"

printf '9606\n3702\n562\n1386\n2157\n' \
	| TAXONKIT_DB="$taxonomy_dir" taxonkit lineage \
	| TAXONKIT_DB="$taxonomy_dir" taxonkit reformat -f 'k__{k};K__{K}' \
	> "${runtime_tmp}/taxonomy.out"

awk -F '\t' '
	$1 == "9606" && $NF == "k__Eukaryota;K__Metazoa" { human = 1 }
	$1 == "3702" && $NF == "k__Eukaryota;K__Viridiplantae" { plant = 1 }
	$1 == "562"  && $NF == "k__Bacteria;K__" { ecoli = 1 }
	$1 == "1386" && $NF == "k__Bacteria;K__" { bacillus = 1 }
	$1 == "2157" && $NF == "k__Archaea;K__" { archaea = 1 }
	END {
		if (!(human && plant && ecoli && bacillus && archaea)) exit 1
	}
' "${runtime_tmp}/taxonomy.out" \
	|| die "TaxonKit {k}/{K} semantic matrix did not match the pinned baseline"

echo "OK: RTBioScan runtime validation passed"
echo "  blastn=${blast_version}"
echo "  lastal=${last_version} (index=${index_version})"
echo "  seqkit=${seqkit_version}"
echo "  taxonkit=${taxonkit_version}"
echo "  cutadapt=${cutadapt_version}"
