#!/usr/bin/env bash
set -euo pipefail

policy="${1:-}"
fasta="${2:-}"
out_ids="${3:-}"
frozen_members="${4:-}"
consolidated_keys="${5:-}"
samples_file="${6:-}"
mixed_mode="${7:-sample_scoped_only}"
noadapter_hint="${8:-0}"
identity_mode="$(printf '%s' "${RTBIOSCAN_EFFECTIVE_IDENTITY_MODE:-collapse}" | tr '[:upper:]' '[:lower:]')"
track_active_units="${RTBIOSCAN_TRACK_ACTIVE_UNITS:-}"
track_identity_tsv="${RTBIOSCAN_TRACK_IDENTITY_TSV:-}"

if [ -z "$policy" ] || [ -z "$fasta" ] || [ -z "$out_ids" ]; then
	echo "Usage: $0 <policy> <fasta> <out_ids> [frozen_members] [consolidated_keys] [samples_file] [mixed_mode] [noadapter_hint]" 1>&2
	exit 2
fi
if [ ! -s "$fasta" ]; then
	: > "$out_ids"
	exit 0
fi

print_all_ids() {
	awk '/^>/{id=substr($0,2); sub(/ .*/, "", id); if (id!="") print id}' "$fasta"
}

case "$policy" in
	never)
		print_all_ids > "$out_ids"
		;;
	always)
		if [ -s "$frozen_members" ]; then
			awk 'BEGIN{FS=OFS="\t"}
				FNR==NR { if ($2!=""){split($2,a,"|"); f[a[1]]=1} next }
				/^>/{
					id=substr($0,2);
					sub(/ .*/, "", id);
					u=id; split(u,b,"|"); u=b[1];
					if (!(u in f) && id!="") print id;
				}
			' "$frozen_members" "$fasta" > "$out_ids"
		else
			print_all_ids > "$out_ids"
		fi
		;;
	until_consolidated)
		if [ -s "$consolidated_keys" ]; then
			if [ "$identity_mode" = "track" ]; then
				if [ -n "${samples_file:-}" ] && [ "$samples_file" != "$track_active_units" ]; then
					echo "ERROR: --otu_prune_samples_file is not supported in track mode; prune must use track_active_units.txt" 1>&2
					exit 1
				fi
				if [ -z "$track_active_units" ] || [ ! -r "$track_active_units" ]; then
					echo "ERROR: RTBIOSCAN_TRACK_ACTIVE_UNITS is required and must be readable in track mode" 1>&2
					exit 1
				fi
				if [ -z "$track_identity_tsv" ] || [ ! -r "$track_identity_tsv" ]; then
					echo "ERROR: RTBIOSCAN_TRACK_IDENTITY_TSV is required and must be readable in track mode" 1>&2
					exit 1
				fi
				awk -v CONS="$consolidated_keys" -v ACTIVE="$track_active_units" -v IDENTITY="$track_identity_tsv" -v MIXED_MODE="$mixed_mode" '
					function trim(v) {
						gsub(/\r/, "", v);
						sub(/^[ \t]+/, "", v);
						sub(/[ \t]+$/, "", v);
						return v;
					}
					function is_unusable(v) {
						return (v=="" || v=="NA" || v=="barcode");
					}
					function is_no_adapter(v, t) {
						t = tolower(trim(v));
						return (t ~ /^no_adapter(_[0-9]+)?$/);
					}
					function extract_adapter(id,   n, f, i, adapter) {
						n=split(id, f, "|");
						adapter="";
						for (i=1; i<=n; i++) {
							if (f[i] ~ /^adapter=/) {
								adapter=substr(f[i], 9);
								break;
							}
						}
						return adapter;
					}
					function extract_barcode(id,   n, f, i, barcode) {
						n=split(id, f, "|");
						barcode="";
						for (i=1; i<=n; i++) {
							if (f[i] ~ /^barcode=/) {
								barcode=substr(f[i], 9);
								break;
							}
						}
						return barcode;
					}
					function extract_otu(id,   n, f, i, otu) {
						n=split(id, f, "|");
						otu="";
						for (i=1; i<=n; i++) {
							if (f[i] ~ /^OTUB_/) otu=f[i];
							else if (f[i] ~ /^OTU=/) otu=substr(f[i], 5);
						}
						return otu;
					}
					function fatal(msg) {
						print msg > "/dev/stderr";
						exit 2;
					}
					BEGIN{
						FS=OFS="\t";
						mode=MIXED_MODE;
						if (mode=="") mode="sample_scoped_only";
						has_sample_scope=0;
						has_global=0;
						while ((getline line < CONS) > 0) {
							line=trim(line);
							if (line=="") continue;
							n=split(line, a, FS);
							if (n>=2) {
								sample=trim(a[1]); otu=trim(a[2]);
								if (sample != "" && otu != "") {
									cons_sample[sample "\t" otu]=1;
									has_sample_scope=1;
								}
							} else if (n==1) {
								otu=trim(a[1]);
								if (otu != "") {
									cons_global[otu]=1;
									has_global=1;
								}
							}
						}
						close(CONS);
						if (has_sample_scope && has_global) {
							if (mode=="fail") {
								fatal("ERROR: Mixed consolidated key formats detected in " CONS " (sample-scoped + global).");
							}
							if (mode=="warn_and_sample_scoped") {
								print "WARN: Mixed consolidated key formats detected in " CONS "; using sample-scoped keys only." > "/dev/stderr";
							}
						}
						while ((getline sline < ACTIVE) > 0) {
							sample_key=trim(sline);
							if (sample_key == "") continue;
							if (is_no_adapter(sample_key)) {
								fatal("ERROR: track_active_units.txt must not contain no_adapter entry: " sample_key);
							}
							active_sample[sample_key]=1;
						}
						close(ACTIVE);
						header_ready=0;
						while ((getline iline < IDENTITY) > 0) {
							iline=trim(iline);
							if (iline=="") continue;
							if (!header_ready) {
								n=split(iline, a, FS);
								for (i=1; i<=n; i++) {
									if (trim(a[i])=="unit_id_track") {
										unit_col=i;
									}
								}
								if (!unit_col) fatal("ERROR: track_identity.tsv is missing unit_id_track header");
								header_ready=1;
								continue;
							}
							n=split(iline, a, FS);
							unit_id=trim(a[unit_col]);
							if (unit_id == "") fatal("ERROR: track_identity.tsv contains empty unit_id_track");
							valid_sample[unit_id]=1;
						}
						close(IDENTITY);
					}
					/^>/{
						id=substr($0,2);
						sub(/ .*/, "", id);
						otu=extract_otu(id);
						adapter=extract_adapter(id);
						barcode=extract_barcode(id);
						effective="";
						if (!is_unusable(adapter)) {
							if (is_no_adapter(adapter)) {
								print id;
								next;
							}
							effective=adapter;
						} else {
							if (is_unusable(barcode)) {
								print "WARN: track mode skipping unannotated read (no usable adapter or barcode): " id > "/dev/stderr";
								next;
							}
							if (is_no_adapter(barcode)) {
								fatal("ERROR: barcode=no_adapter does not qualify for track-mode no-adapter handling for read " id);
							}
							effective=barcode;
						}
						if (!(effective in valid_sample)) {
							fatal("ERROR: track mode observed identity not present in track_identity.tsv: " effective);
						}
						if (!(effective in active_sample)) {
							fatal("ERROR: track mode observed identity not present in track_active_units.txt: " effective);
						}
						keep=1;
						if (otu != "") {
							if (has_sample_scope) {
								if ((effective "\t" otu) in cons_sample) keep=0;
							} else if (otu in cons_global) {
								keep=0;
							}
						}
						if (keep) print id;
					}
				' "$fasta" > "$out_ids"
			else
			awk -v CONS="$consolidated_keys" -v FASTA="$fasta" -v SAMPLES="$samples_file" -v MIXED_MODE="$mixed_mode" -v NOADAPTER_HINT="$noadapter_hint" '
				function trim(v) {
					gsub(/\r/, "", v);
					return v;
				}
				function is_unusable(v) {
					return (v=="" || v=="NA" || v=="barcode");
				}
				function normalize_key(v,   n, parts, j, outv, last) {
					outv=v;
					n=split(v, parts, "_");
					if (n>1) {
						last=parts[n];
						if (last != "" && last !~ /[^0-9]/) {
							outv=parts[1];
							for (j=2; j<n; j++) outv=outv "_" parts[j];
						}
					}
					return outv;
				}
				function extract_sample_key(id,   n, f, i, adapter, barcode, key) {
					n=split(id, f, "|");
					adapter=""; barcode="";
					for (i=1; i<=n; i++) {
						if (f[i] ~ /^adapter=/) adapter=substr(f[i], 9);
						else if (f[i] ~ /^barcode=/) barcode=substr(f[i], 9);
					}
					key=adapter;
					if (is_unusable(key)) key="";
					if (key == "" && !is_unusable(barcode)) key=barcode;
					return key;
				}
				function extract_otu(id,   n, f, i, otu) {
					n=split(id, f, "|");
					otu="";
					for (i=1; i<=n; i++) {
						if (f[i] ~ /^OTUB_/) otu=f[i];
						else if (f[i] ~ /^OTU=/) otu=substr(f[i], 5);
					}
					return otu;
				}
					BEGIN{
						FS=OFS="\t";
						mode=MIXED_MODE;
						if (mode=="") mode="sample_scoped_only";
						noadapter_hint=NOADAPTER_HINT+0;
						samples_mode=0;
						has_sample_scope=0;
						has_global=0;
					while ((getline line < CONS) > 0) {
						if (line=="") continue;
						n=split(line, a, FS);
						if (n>=2) {
							sample=trim(a[1]); otu=trim(a[2]);
							if (sample != "" && otu != "") {
								cons_sample[sample "\t" otu]=1;
								cons_sample_name[sample]=1;
								has_sample_scope=1;
							}
						} else if (n==1) {
							otu=trim(a[1]);
							if (otu != "") {
								cons_global[otu]=1;
								has_global=1;
							}
						}
					}
					close(CONS);
					if (has_sample_scope && has_global) {
						if (mode=="fail") {
							print "ERROR: Mixed consolidated key formats detected in " CONS " (sample-scoped + global)." > "/dev/stderr";
							exit 2;
						}
						if (mode=="warn_and_sample_scoped") {
							print "WARN: Mixed consolidated key formats detected in " CONS "; using sample-scoped keys only." > "/dev/stderr";
						}
					}
						known_count=0;
						samples_count=0;
						if (SAMPLES != "") {
							while ((getline sline < SAMPLES) > 0) {
								sample_key=trim(sline);
								sub(/^[ \t]+/, "", sample_key);
								sub(/[ \t]+$/, "", sample_key);
								if (sample_key != "" && !(sample_key in known_sample)) {
									known_sample[sample_key]=1;
									known_count++;
									samples_count++;
								}
							}
							close(SAMPLES);
						}
						if (samples_count > 0) {
							samples_mode=1;
						}
						if (samples_mode==0 && noadapter_hint==1 && ("no_adapter" in cons_sample_name) && !("no_adapter" in known_sample)) {
							known_sample["no_adapter"]=1;
							known_count++;
						}
						if (samples_mode == 0 && FASTA != "") {
							while ((getline hline < FASTA) > 0) {
								if (hline !~ /^>/) continue;
								id=substr(hline,2);
							sub(/ .*/, "", id);
							sample_key=extract_sample_key(id);
							if (sample_key != "" && !(sample_key in known_sample)) {
								known_sample[sample_key]=1;
								known_count++;
							}
						}
						close(FASTA);
					}
				}
				/^>/{
					id=substr($0,2);
					sub(/ .*/, "", id);
					otu=extract_otu(id);
					raw_sample_key=extract_sample_key(id);
					norm_sample_key=normalize_key(raw_sample_key);
						allow_norm=0;
						if (norm_sample_key != "" && norm_sample_key != raw_sample_key) {
							if (samples_mode==1) {
								if (!(raw_sample_key in known_sample) && (norm_sample_key in known_sample)) allow_norm=1;
							} else if (norm_sample_key=="no_adapter" && noadapter_hint==1 && (norm_sample_key in cons_sample_name)) {
								allow_norm=1;
							}
					}
					keep=1;
					if (otu != "") {
						if (has_sample_scope) {
							if (raw_sample_key != "" && ((raw_sample_key "\t" otu) in cons_sample)) keep=0;
							else if (allow_norm && ((norm_sample_key "\t" otu) in cons_sample)) keep=0;
						} else {
							if (otu in cons_global) keep=0;
						}
					}
					if (keep) print id;
				}
			' "$fasta" > "$out_ids"
			fi
		else
			print_all_ids > "$out_ids"
		fi
		;;
	*)
		echo "ERROR: invalid policy '$policy' (allowed: always, until_consolidated, never)" 1>&2
		exit 2
		;;
esac
