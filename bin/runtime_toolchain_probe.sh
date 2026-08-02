#!/usr/bin/env bash
set -eu

if [ "$#" -ne 11 ]; then
	echo "ERROR: runtime_toolchain_probe.sh requires 11 resolved executables" >&2
	exit 2
fi

blastn_bin="$1"
lastal_bin="$2"
taxonkit_bin="$3"
seqkit_bin="$4"
cutadapt_bin="$5"
vsearch_bin="$6"
cdhit_bin="$7"
samtools_bin="$8"
seqtk_bin="$9"
rscript_bin="${10}"
dorado_bin="${11}"

blastn_version="$("$blastn_bin" -version 2>&1 | awk 'NR == 1 { sub(/^blastn:[[:space:]]*/, ""); sub(/[+].*$/, ""); print; exit }')"
lastal_version="$("$lastal_bin" --version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) { print $i; exit } }')"
taxonkit_version="$("$taxonkit_bin" version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^v?[0-9]+[.][0-9]+[.][0-9]+$/) { sub(/^v/, "", $i); print $i; exit } }')"
seqkit_version="$("$seqkit_bin" version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^v?[0-9]+[.][0-9]+[.][0-9]+$/) { sub(/^v/, "", $i); print $i; exit } }')"
cutadapt_version="$("$cutadapt_bin" --version 2>&1 | awk 'NR == 1 { print $1; exit }')"
vsearch_version="$("$vsearch_bin" --version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^v?[0-9]+[.][0-9]+[.][0-9]+/) { sub(/^v/, "", $i); sub(/[^0-9.].*$/, "", $i); print $i; exit } }')"
cdhit_version="$("$cdhit_bin" -h 2>&1 | awk '{ for (i = 1; i <= NF; i++) if ($i == "version" && (i + 1) <= NF) { print $(i + 1); exit } }')"
samtools_version="$("$samtools_bin" --version 2>&1 | awk 'NR == 1 { print $2; exit }')"
seqtk_version="$("$seqtk_bin" 2>&1 | awk '$1 == "Version:" { sub(/-.*$/, "", $2); print $2; exit }')"
dorado_version="$("$dorado_bin" --version 2>&1 | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+[.][0-9]+[.][0-9]+/) { print $i; exit } }')"

for required_value in \
	"$blastn_version" "$lastal_version" "$taxonkit_version" "$seqkit_version" \
	"$cutadapt_version" "$vsearch_version" "$cdhit_version" "$samtools_version" \
	"$seqtk_version" "$dorado_version"; do
	if [ -z "$required_value" ]; then
		echo "ERROR: one or more runtime versions could not be parsed" >&2
		exit 1
	fi
done

printf 'tool_blastn\t%s\n' "$blastn_version"
printf 'tool_lastal\t%s\n' "$lastal_version"
printf 'tool_taxonkit\t%s\n' "$taxonkit_version"
printf 'tool_seqkit\t%s\n' "$seqkit_version"
printf 'tool_cutadapt\t%s\n' "$cutadapt_version"
printf 'tool_vsearch\t%s\n' "$vsearch_version"
printf 'tool_cd-hit-est\t%s\n' "$cdhit_version"
printf 'tool_samtools\t%s\n' "$samtools_version"
printf 'tool_seqtk\t%s\n' "$seqtk_version"
printf 'dorado_dorado\t%s\n' "$dorado_version"

"$rscript_bin" -e '
cat("tool_Rscript\t", as.character(getRversion()), "\n", sep="")
for (p in c("DECIPHER", "Biostrings")) {
    if (!requireNamespace(p, quietly=TRUE)) quit(status=3)
    cat("r_package_", p, "\t", as.character(packageVersion(p)), "\n", sep="")
}
'
