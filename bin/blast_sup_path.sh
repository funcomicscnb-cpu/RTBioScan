#!/usr/bin/env bash

sup_summary_write_header() {
    local out_tsv="$1"
    printf "%b" "$DORADO_SUMMARY_HEADER" > "$out_tsv"
}

sup_summary_copy_header() {
    local src_tsv="$1"
    local out_tsv="$2"
    if [ -s "$src_tsv" ]; then
        head -n 1 "$src_tsv" > "$out_tsv" 2>/dev/null || sup_summary_write_header "$out_tsv"
    else
        sup_summary_write_header "$out_tsv"
    fi
}

sup_summary_id_list() {
    local summary_in="$1"
    local out_ids="$2"

    perl -e '
        use strict;
        use warnings;
        my ($sum_f) = @ARGV;
        open my $SUM, "<", $sum_f or die "open summary: $!";
        my $header = <$SUM>;
        defined $header or die "missing summary header\n";
        chomp $header;
        my @h = split /\t/, $header, -1;
        my $read_id_idx = -1;
        for my $i (0 .. $#h) {
            if ($h[$i] eq "read_id") {
                $read_id_idx = $i;
                last;
            }
        }
        die "summary header missing read_id column\n" if $read_id_idx < 0;
        my %seen;
        while (<$SUM>) {
            chomp;
            next unless /\S/;
            my @f = split /\t/, $_, -1;
            my $id = $f[$read_id_idx] // "";
            next unless $id ne "";
            next if $seen{$id}++;
            print "$id\n";
        }
        close $SUM;
    ' "$summary_in" | LC_ALL=C sort -u > "$out_ids"
}

sup_count_nonempty_lines() {
    local path="$1"
    awk 'NF{c++} END{print c+0}' "$path" 2>/dev/null || echo 0
}

sup_count_summary_rows() {
    local path="$1"
    awk 'NR>1 && NF{c++} END{print c+0}' "$path" 2>/dev/null || echo 0
}

sup_emit_zero_timing() {
    local phase="$1"
    append_sup_path_timing "$phase" 0 0
}

sup_unique_ids_in_order() {
    local ids_in="$1"
    local ids_out="$2"

    awk 'NF && !seen[$0]++{print $0}' "$ids_in" > "$ids_out" 2>/dev/null || : > "$ids_out"
}

sup_fasta_extract_ordered() {
    local ids_file="$1"
    local fasta_in="$2"
    local fasta_out="$3"
    local missing_out="${4:-}"

    perl -e '
        use strict;
        use warnings;
        my ($ids_f, $fasta_f, $out_f, $miss_f) = @ARGV;
        open my $IDS, "<", $ids_f or die "open ids: $!";
        my (@ids, %want);
        while (<$IDS>) {
            chomp;
            next unless /\S/;
            push @ids, $_;
            $want{$_} = 1;
        }
        close $IDS;

        my (%records, $cur_id, $keep);
        if (open my $FA, "<", $fasta_f) {
            while (<$FA>) {
                if (/^>/) {
                    my $hdr = $_;
                    (my $id = $hdr) =~ s/^>//;
                    $id =~ s/\s.*//;
                    chomp $id;
                    $cur_id = $id;
                    $keep = exists $want{$id} && !exists $records{$id};
                    $records{$id} = $hdr if $keep;
                    next;
                }
                next unless $keep && defined $cur_id;
                $records{$cur_id} .= $_;
            }
            close $FA;
        }

        open my $OUT, ">", $out_f or die "open out: $!";
        my $MISS;
        if (defined $miss_f && $miss_f ne "") {
            open $MISS, ">", $miss_f or die "open miss: $!";
        }
        my ($count, $missing) = (0, 0);
        for my $id (@ids) {
            if (exists $records{$id}) {
                print {$OUT} $records{$id};
                $count++;
            } else {
                print {$MISS} "$id\n" if $MISS;
                $missing++;
            }
        }
        close $OUT;
        close $MISS if $MISS;
        print "$count\t$missing\n";
    ' "$ids_file" "$fasta_in" "$fasta_out" "$missing_out"
}

sup_restore_cached_payloads() {
    local restore_stats

    if ! gzip -dc "$SUP_CACHE_READS_GZ" > "${barcode}_sup_cache_reads_all.fastq" 2>/dev/null; then
        return 1
    fi
    restore_stats=$(sup_fastq_extract_ordered \
        "${barcode}_blastreport_hac_cached.list" \
        "${barcode}_sup_cache_reads_all.fastq" \
        "${barcode}_blastreport_sup_cached.fastq" \
        "${barcode}_sup_cache_missing_fastq_ids.list") || {
        rm -f "${barcode}_sup_cache_reads_all.fastq"
        return 1
    }
    sup_cache_restored_fastq_reads=${restore_stats%%$'\t'*}
    sup_cache_restore_missing_fastq_ids=${restore_stats#*$'\t'}
    rm -f "${barcode}_sup_cache_reads_all.fastq"

    restore_stats=$(sup_summary_extract_ordered \
        "${barcode}_blastreport_hac_cached.list" \
        "$SUP_CACHE_SUMMARY" \
        "${barcode}_round_sup_cached.tsv" \
        "${barcode}_sup_cache_missing_summary_ids.list") || return 1
    sup_cache_restored_summary_rows=${restore_stats%%$'\t'*}
    sup_cache_restore_missing_summary_ids=${restore_stats#*$'\t'}
    return 0
}

sup_fastq_extract_ordered() {
    local ids_file="$1"
    local fastq_in="$2"
    local fastq_out="$3"
    local missing_out="$4"

    perl -e '
        use strict;
        use warnings;
        my ($ids_f, $fastq_f, $out_f, $miss_f) = @ARGV;
        open my $IDS, "<", $ids_f or die "open ids: $!";
        my (@ids, %want);
        while (<$IDS>) {
            chomp;
            next unless /\S/;
            push @ids, $_;
            $want{$_} = 1;
        }
        close $IDS;

        my %records;
        if (open my $FQ, "<", $fastq_f) {
            while (defined(my $h = <$FQ>)) {
                my $seq  = <$FQ>;
                my $plus = <$FQ>;
                my $qual = <$FQ>;
                last unless defined $qual;
                (my $id = $h) =~ s/^@//;
                $id =~ s/\s.*//;
                chomp $id;
                next unless exists $want{$id};
                next if exists $records{$id};
                $records{$id} = $h . $seq . $plus . $qual;
            }
            close $FQ;
        }

        open my $OUT, ">", $out_f or die "open out: $!";
        open my $MISS, ">", $miss_f or die "open miss: $!";
        my ($count, $missing) = (0, 0);
        for my $id (@ids) {
            if (exists $records{$id}) {
                print {$OUT} $records{$id};
                $count++;
            } else {
                print {$MISS} "$id\n";
                $missing++;
            }
        }
        close $OUT;
        close $MISS;
        print "$count\t$missing\n";
    ' "$ids_file" "$fastq_in" "$fastq_out" "$missing_out"
}

sup_summary_extract_ordered() {
    local ids_file="$1"
    local summary_in="$2"
    local summary_out="$3"
    local missing_out="$4"

    sup_summary_copy_header "$summary_in" "$summary_out"
    perl -e '
        use strict;
        use warnings;
        my ($ids_f, $sum_f, $out_f, $miss_f) = @ARGV;
        open my $IDS, "<", $ids_f or die "open ids: $!";
        my (@ids, %want);
        while (<$IDS>) {
            chomp;
            next unless /\S/;
            push @ids, $_;
            $want{$_} = 1;
        }
        close $IDS;

        my %rows;
        if (open my $SUM, "<", $sum_f) {
            my $header = <$SUM>;
            my $read_id_idx = -1;
            if (defined $header) {
                chomp $header;
                my @h = split /\t/, $header, -1;
                for my $i (0 .. $#h) {
                    if ($h[$i] eq "read_id") {
                        $read_id_idx = $i;
                        last;
                    }
                }
            }
            die "summary header missing read_id column\n" if $read_id_idx < 0;
            while (<$SUM>) {
                chomp;
                next unless /\S/;
                my @f = split /\t/, $_, -1;
                my $id = $f[$read_id_idx] // "";
                next unless $id ne "" && exists $want{$id};
                next if exists $rows{$id};
                $rows{$id} = $_ . "\n";
            }
            close $SUM;
        }

        open my $OUT, ">>", $out_f or die "open out: $!";
        open my $MISS, ">", $miss_f or die "open miss: $!";
        my ($count, $missing) = (0, 0);
        for my $id (@ids) {
            if (exists $rows{$id}) {
                print {$OUT} $rows{$id};
                $count++;
            } else {
                print {$MISS} "$id\n";
                $missing++;
            }
        }
        close $OUT;
        close $MISS;
        print "$count\t$missing\n";
    ' "$ids_file" "$summary_in" "$summary_out" "$missing_out"
}

sup_cache_init_paths() {
    SUP_CACHE_DIR="${STATE_DIR}/sup_basecall_cache"
    SUP_CACHE_META="${SUP_CACHE_DIR}/meta.tsv"
    SUP_CACHE_MANIFEST="${SUP_CACHE_DIR}/manifest.tsv"
    SUP_CACHE_SUMMARY="${SUP_CACHE_DIR}/summary.tsv"
    SUP_CACHE_READS_GZ="${SUP_CACHE_DIR}/reads.fastq.gz"
}

sup_cache_meta_value_locked() {
    local key="$1"
    awk -v k="$key" 'index($0, k "\t") == 1 { sub(/^[^\t]*\t/, "", $0); print; exit }' "$SUP_CACHE_META" 2>/dev/null || true
}

sup_cache_write_meta_locked() {
    {
        printf 'key\tvalue\n'
        printf 'cache_schema_version\t%s\n' "$SUP_CACHE_SCHEMA_VERSION"
        printf 'restart_token\t%s\n' "$SUP_CACHE_RESTART_TOKEN"
        printf 'dorado_model\t%s\n' "$SUP_CACHE_DORADO_MODEL"
        printf 'dorado_args\t%s\n' "$SUP_CACHE_DORADO_ARGS"
        printf 'min_qscore\t%s\n' "$SUP_CACHE_MIN_QSCORE"
    } > "$SUP_CACHE_META"
}

sup_cache_reset_locked() {
    local tmp_dir="${SUP_CACHE_DIR}.reset.$$"
    local old_meta="${SUP_CACHE_META}"
    local old_manifest="${SUP_CACHE_MANIFEST}"
    local old_summary="${SUP_CACHE_SUMMARY}"
    local old_reads_gz="${SUP_CACHE_READS_GZ}"

    rm -rf "$tmp_dir" || return 1
    mkdir -p "$tmp_dir" || return 1

    SUP_CACHE_META="${tmp_dir}/meta.tsv"
    SUP_CACHE_MANIFEST="${tmp_dir}/manifest.tsv"
    SUP_CACHE_SUMMARY="${tmp_dir}/summary.tsv"
    SUP_CACHE_READS_GZ="${tmp_dir}/reads.fastq.gz"

    if ! printf '' | gzip -c > "$SUP_CACHE_READS_GZ"; then
        rm -rf "$tmp_dir" || true
        SUP_CACHE_META="$old_meta"
        SUP_CACHE_MANIFEST="$old_manifest"
        SUP_CACHE_SUMMARY="$old_summary"
        SUP_CACHE_READS_GZ="$old_reads_gz"
        return 1
    fi
    if ! sup_summary_write_header "$SUP_CACHE_SUMMARY"; then
        rm -rf "$tmp_dir" || true
        SUP_CACHE_META="$old_meta"
        SUP_CACHE_MANIFEST="$old_manifest"
        SUP_CACHE_SUMMARY="$old_summary"
        SUP_CACHE_READS_GZ="$old_reads_gz"
        return 1
    fi
    if ! : > "$SUP_CACHE_MANIFEST"; then
        rm -rf "$tmp_dir" || true
        SUP_CACHE_META="$old_meta"
        SUP_CACHE_MANIFEST="$old_manifest"
        SUP_CACHE_SUMMARY="$old_summary"
        SUP_CACHE_READS_GZ="$old_reads_gz"
        return 1
    fi
    if ! sup_cache_write_meta_locked; then
        rm -rf "$tmp_dir" || true
        SUP_CACHE_META="$old_meta"
        SUP_CACHE_MANIFEST="$old_manifest"
        SUP_CACHE_SUMMARY="$old_summary"
        SUP_CACHE_READS_GZ="$old_reads_gz"
        return 1
    fi

    SUP_CACHE_META="$old_meta"
    SUP_CACHE_MANIFEST="$old_manifest"
    SUP_CACHE_SUMMARY="$old_summary"
    SUP_CACHE_READS_GZ="$old_reads_gz"

    rm -rf "$SUP_CACHE_DIR" || {
        rm -rf "$tmp_dir" || true
        return 1
    }
    mv "$tmp_dir" "$SUP_CACHE_DIR" || {
        rm -rf "$tmp_dir" || true
        return 1
    }
    return 0
}

sup_cache_prepare_locked() {
    sup_cache_init_paths
    local reset=0
    if [ ! -d "$SUP_CACHE_DIR" ] || [ ! -f "$SUP_CACHE_META" ] || [ ! -f "$SUP_CACHE_MANIFEST" ] || [ ! -f "$SUP_CACHE_SUMMARY" ] || [ ! -f "$SUP_CACHE_READS_GZ" ]; then
        reset=1
    elif [ "$(sup_cache_meta_value_locked cache_schema_version)" != "$SUP_CACHE_SCHEMA_VERSION" ] \
      || [ "$(sup_cache_meta_value_locked restart_token)" != "$SUP_CACHE_RESTART_TOKEN" ] \
      || [ "$(sup_cache_meta_value_locked dorado_model)" != "$SUP_CACHE_DORADO_MODEL" ] \
      || [ "$(sup_cache_meta_value_locked dorado_args)" != "$SUP_CACHE_DORADO_ARGS" ] \
      || [ "$(sup_cache_meta_value_locked min_qscore)" != "$SUP_CACHE_MIN_QSCORE" ]; then
        reset=1
    fi
    if [ "$reset" = "1" ]; then
        sup_cache_reset_locked || return 1
    fi
    return 0
}

sup_candidate_extract() {
    local t_start t_end
    t_start=$(now_ms)
    : > "${barcode}_blastreport_hac.list"
    : > "${barcode}_blastreport_hac_unique.list"
    grep -F "|hac2sup|" blast_report_annotated_otu.txt | cut -f1 -d"|" > "${barcode}_blastreport_hac.list" || :
    awk 'NF && !seen[$0]++{print $0}' "${barcode}_blastreport_hac.list" > "${barcode}_blastreport_hac_unique.list" 2>/dev/null || : > "${barcode}_blastreport_hac_unique.list"
    hac2sup_candidate_rows=$(grep -cF "|hac2sup|" blast_report_annotated_otu.txt 2>/dev/null || echo 0)
    hac2sup_candidate_unique_read_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_unique.list")
    : > "${barcode}_qced_reads_hq_hac2sup.fasta"
    t_end=$(now_ms)
    append_sup_path_timing "sup_candidate_extract" "$t_start" "$t_end"

    : > "${barcode}_qced_reads_hq_hac2sup_sup.fasta"
}

sup_shared_candidate_extract() {
    local t_start t_end

    shared_extract_union_ids=0
    shared_extract_hac2sup_ids=0
    shared_extract_hac_fixed_ids=0
    shared_extract_fasta_reads=0

    : > "${barcode}_qced_reads_hq_hac2sup.fasta"
    : > "${barcode}_qced_reads_hq_hac_fixed.fasta"
    : > "hac_fixed_readids.unique.list"
    : > "${barcode}_shared_extract_union_ids.list"
    : > "${barcode}_shared_extract_union.fasta"

    if [ -s "hac_fixed_readids.list" ]; then
        sup_unique_ids_in_order "hac_fixed_readids.list" "hac_fixed_readids.unique.list"
    fi

    shared_extract_hac2sup_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_unique.list")
    shared_extract_hac_fixed_ids=$(sup_count_nonempty_lines "hac_fixed_readids.unique.list")
    cat "${barcode}_blastreport_hac_unique.list" "hac_fixed_readids.unique.list" \
        | awk 'NF && !seen[$0]++{print $0}' > "${barcode}_shared_extract_union_ids.list" 2>/dev/null || : > "${barcode}_shared_extract_union_ids.list"
    shared_extract_union_ids=$(sup_count_nonempty_lines "${barcode}_shared_extract_union_ids.list")

    if [ "$shared_extract_union_ids" -eq 0 ]; then
        sup_emit_zero_timing "shared_candidate_extract"
        rm -f "${barcode}_shared_extract_union_ids.list" "${barcode}_shared_extract_union.fasta" "hac_fixed_readids.unique.list"
        return 0
    fi

    t_start=$(now_ms)
    _seqkit_fai_src="${STATE_DIR}/$(basename "${SUP_FASTA_HQ_QCED}").seqkit.fai"
    [ -f "$_seqkit_fai_src" ] && ln -sf "$_seqkit_fai_src" "${SUP_FASTA_HQ_QCED}.seqkit.fai" 2>/dev/null || true
    seqkit faidx -j "${SUP_TASK_CPUS}" -l "${barcode}_shared_extract_union_ids.list" -r "${SUP_FASTA_HQ_QCED}" > "${barcode}_shared_extract_union.fasta" || : > "${barcode}_shared_extract_union.fasta"
    shared_extract_fasta_reads=$(awk '/^>/{c++} END{print c+0}' "${barcode}_shared_extract_union.fasta" 2>/dev/null || echo 0)
    if [ "$shared_extract_hac2sup_ids" -gt 0 ]; then
        sup_fasta_extract_ordered \
            "${barcode}_blastreport_hac_unique.list" \
            "${barcode}_shared_extract_union.fasta" \
            "${barcode}_qced_reads_hq_hac2sup.fasta" >/dev/null || : > "${barcode}_qced_reads_hq_hac2sup.fasta"
    fi
    if [ "$shared_extract_hac_fixed_ids" -gt 0 ]; then
        sup_fasta_extract_ordered \
            "hac_fixed_readids.unique.list" \
            "${barcode}_shared_extract_union.fasta" \
            "${barcode}_qced_reads_hq_hac_fixed.fasta" >/dev/null || : > "${barcode}_qced_reads_hq_hac_fixed.fasta"
    fi
    t_end=$(now_ms)
    append_sup_path_timing "shared_candidate_extract" "$t_start" "$t_end"

    rm -f "${barcode}_shared_extract_union_ids.list" "${barcode}_shared_extract_union.fasta" "hac_fixed_readids.unique.list"
}

sup_cache_lookup() {
    local restore_stats cache_restore_failed=0 cache_prepare_failed=0
    SUP_CACHE_SKIP_PERSIST="${SUP_CACHE_SKIP_PERSIST:-0}"
    sup_cache_hit_ids=0
    sup_cache_miss_ids=0
    sup_cache_restored_fastq_reads=0
    sup_cache_restored_summary_rows=0
    sup_cache_restore_missing_fastq_ids=0
    sup_cache_restore_missing_summary_ids=0
    dorado_sup_reads_requested=0

    sup_cache_init_paths
    : > "${barcode}_blastreport_hac_unique.list"
    if [ -s "${barcode}_blastreport_hac.list" ]; then
        awk 'NF && !seen[$0]++{print $0}' "${barcode}_blastreport_hac.list" > "${barcode}_blastreport_hac_unique.list" 2>/dev/null || : > "${barcode}_blastreport_hac_unique.list"
    fi
    hac2sup_candidate_unique_read_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_unique.list")
    : > "${barcode}_blastreport_hac_cached.list"
    : > "${barcode}_blastreport_hac_missing.list"
    : > "${barcode}_blastreport_sup_cached.fastq"
    sup_summary_write_header "${barcode}_round_sup_cached.tsv"
    : > "${barcode}_sup_cache_missing_fastq_ids.list"
    : > "${barcode}_sup_cache_missing_summary_ids.list"

    if [ ! -s "${barcode}_blastreport_hac_unique.list" ]; then
        sup_emit_zero_timing "sup_cache_lookup"
        sup_emit_zero_timing "sup_cache_restore_fastq"
        sup_emit_zero_timing "sup_cache_restore_summary"
        return 0
    fi

    local t_start t_end
    t_start=$(now_ms)
    if acquire_lock "$SUP_CACHE_LOCK"; then
        if ! sup_cache_prepare_locked; then
            cache_prepare_failed=1
            SUP_CACHE_SKIP_PERSIST=1
        else
            awk -v manifest="$SUP_CACHE_MANIFEST" 'BEGIN{while ((getline < manifest) > 0) if ($1 != "") cached[$1]=1; close(manifest)} NF && ($1 in cached){print $1}' \
                "${barcode}_blastreport_hac_unique.list" > "${barcode}_blastreport_hac_cached.list"
            awk -v manifest="$SUP_CACHE_MANIFEST" 'BEGIN{while ((getline < manifest) > 0) if ($1 != "") cached[$1]=1; close(manifest)} NF && !($1 in cached){print $1}' \
                "${barcode}_blastreport_hac_unique.list" > "${barcode}_blastreport_hac_missing.list"
            sup_cache_hit_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_cached.list")
            sup_cache_miss_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_missing.list")
            dorado_sup_reads_requested="$sup_cache_miss_ids"
        fi
        t_end=$(now_ms)
        append_sup_path_timing "sup_cache_lookup" "$t_start" "$t_end"

        if [ "$cache_prepare_failed" = "0" ] && [ -s "${barcode}_blastreport_hac_cached.list" ]; then
            t_start=$(now_ms)
            if ! sup_restore_cached_payloads; then
                cache_restore_failed=1
            fi
            if [ "$cache_restore_failed" = "0" ] && { [ "${sup_cache_restore_missing_fastq_ids:-0}" -gt 0 ] || [ "${sup_cache_restore_missing_summary_ids:-0}" -gt 0 ]; }; then
                cat "${barcode}_sup_cache_missing_fastq_ids.list" "${barcode}_sup_cache_missing_summary_ids.list" \
                    | awk 'NF' | LC_ALL=C sort -u > "${barcode}_sup_cache_provisional_missing_ids.list"
                cat "${barcode}_blastreport_hac_missing.list" "${barcode}_sup_cache_provisional_missing_ids.list" \
                    | awk 'NF' | LC_ALL=C sort -u > "${barcode}_blastreport_hac_missing.list.new"
                mv "${barcode}_blastreport_hac_missing.list.new" "${barcode}_blastreport_hac_missing.list"
                awk 'NR==FNR{bad[$1]=1; next} NF && !($1 in bad){print $1}' \
                    "${barcode}_sup_cache_provisional_missing_ids.list" \
                    "${barcode}_blastreport_hac_cached.list" > "${barcode}_blastreport_hac_cached.list.new"
                mv "${barcode}_blastreport_hac_cached.list.new" "${barcode}_blastreport_hac_cached.list"
                sup_cache_hit_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_cached.list")
                sup_cache_miss_ids=$(sup_count_nonempty_lines "${barcode}_blastreport_hac_missing.list")
                dorado_sup_reads_requested="$sup_cache_miss_ids"
                echo "WARN: reclassifying incomplete SUP cache payloads as Dorado misses for this round" 1>&2
                : > "${barcode}_blastreport_sup_cached.fastq"
                sup_summary_write_header "${barcode}_round_sup_cached.tsv"
                : > "${barcode}_sup_cache_missing_fastq_ids.list"
                : > "${barcode}_sup_cache_missing_summary_ids.list"
                sup_cache_restored_fastq_reads=0
                sup_cache_restored_summary_rows=0
                sup_cache_restore_missing_fastq_ids=0
                sup_cache_restore_missing_summary_ids=0
                if [ -s "${barcode}_blastreport_hac_cached.list" ]; then
                    if ! sup_restore_cached_payloads; then
                        cache_restore_failed=1
                    fi
                fi
            fi
            t_end=$(now_ms)
            append_sup_path_timing "sup_cache_restore_fastq" "$t_start" "$t_end"
            append_sup_path_timing "sup_cache_restore_summary" "$t_start" "$t_end"
        else
            sup_emit_zero_timing "sup_cache_restore_fastq"
            sup_emit_zero_timing "sup_cache_restore_summary"
        fi

        if [ "$cache_prepare_failed" = "1" ]; then
            echo "WARN: SUP cache reset/invalidation failed; falling back to full Dorado for this round" 1>&2
            : > "${barcode}_blastreport_hac_cached.list"
            cp "${barcode}_blastreport_hac_unique.list" "${barcode}_blastreport_hac_missing.list"
            : > "${barcode}_blastreport_sup_cached.fastq"
            sup_summary_write_header "${barcode}_round_sup_cached.tsv"
            : > "${barcode}_sup_cache_missing_fastq_ids.list"
            : > "${barcode}_sup_cache_missing_summary_ids.list"
            sup_cache_hit_ids=0
            sup_cache_miss_ids="$hac2sup_candidate_unique_read_ids"
            sup_cache_restored_fastq_reads=0
            sup_cache_restored_summary_rows=0
            sup_cache_restore_missing_fastq_ids=0
            sup_cache_restore_missing_summary_ids=0
            dorado_sup_reads_requested="$sup_cache_miss_ids"
        elif [ "$cache_restore_failed" = "1" ]; then
            echo "WARN: SUP cache restore failed; resetting cache and falling back to full Dorado for this round" 1>&2
            if ! sup_cache_reset_locked; then
                echo "WARN: SUP cache reset failed after restore error; disabling cache persistence for this round" 1>&2
                SUP_CACHE_SKIP_PERSIST=1
            fi
            : > "${barcode}_blastreport_hac_cached.list"
            cp "${barcode}_blastreport_hac_unique.list" "${barcode}_blastreport_hac_missing.list"
            : > "${barcode}_blastreport_sup_cached.fastq"
            sup_summary_write_header "${barcode}_round_sup_cached.tsv"
            : > "${barcode}_sup_cache_missing_fastq_ids.list"
            : > "${barcode}_sup_cache_missing_summary_ids.list"
            sup_cache_hit_ids=0
            sup_cache_miss_ids="$hac2sup_candidate_unique_read_ids"
            sup_cache_restored_fastq_reads=0
            sup_cache_restored_summary_rows=0
            sup_cache_restore_missing_fastq_ids=0
            sup_cache_restore_missing_summary_ids=0
            dorado_sup_reads_requested="$sup_cache_miss_ids"
        fi
        release_lock "$SUP_CACHE_LOCK"
    else
        echo "WARN: failed to acquire SUP cache lock; falling back to full Dorado for this round" 1>&2
        cp "${barcode}_blastreport_hac_unique.list" "${barcode}_blastreport_hac_missing.list"
        sup_cache_hit_ids=0
        sup_cache_miss_ids="$hac2sup_candidate_unique_read_ids"
        dorado_sup_reads_requested="$sup_cache_miss_ids"
        append_sup_path_timing "sup_cache_lookup" "$t_start" "$(now_ms)"
        sup_emit_zero_timing "sup_cache_restore_fastq"
        sup_emit_zero_timing "sup_cache_restore_summary"
    fi

    if [ $(( sup_cache_hit_ids + sup_cache_miss_ids )) -ne "${hac2sup_candidate_unique_read_ids:-0}" ]; then
        echo "ERROR: SUP cache hit/miss accounting drift detected after cache correction" 1>&2
        return 1
    fi
    if [ "${sup_cache_restore_missing_fastq_ids:-0}" -gt 0 ]; then
        echo "WARN: SUP cache missing FASTQ entries for ${sup_cache_restore_missing_fastq_ids} provisional hit IDs; those reads will be re-basecalled" 1>&2
    fi
    if [ "${sup_cache_restore_missing_summary_ids:-0}" -gt 0 ]; then
        echo "WARN: SUP cache missing summary rows for ${sup_cache_restore_missing_summary_ids} provisional hit IDs; those reads will be re-basecalled" 1>&2
    fi
}

sup_build_new_summary() {
    sup_summary_write_header "${barcode}_round_sup_new.tsv"
    if [ -s "${barcode}_blastreport_sup.sam" ] && grep -q '^@' "${barcode}_blastreport_sup.sam"; then
        if "${SUP_DORADO_BIN}" summary "${barcode}_blastreport_sup.sam" > "${barcode}_round_sup_new.tsv.tmp" 2>/dev/null && [ -s "${barcode}_round_sup_new.tsv.tmp" ]; then
            mv "${barcode}_round_sup_new.tsv.tmp" "${barcode}_round_sup_new.tsv"
        else
            rm -f "${barcode}_round_sup_new.tsv.tmp"
        fi
    fi
    dorado_sup_summary_rows_new=$(sup_count_summary_rows "${barcode}_round_sup_new.tsv")
}

sup_merge_outputs() {
    local t_start t_end merge_stats

    : > "${barcode}_blastreport_sup_pre.fastq"
    sup_summary_copy_header "${barcode}_round_sup_cached.tsv" "${barcode}_round_sup.tsv"
    : > "${barcode}_sup_merge_missing_fastq_ids.list"
    : > "${barcode}_sup_merge_missing_summary_ids.list"
    cat "${barcode}_blastreport_sup_cached.fastq" "${barcode}_blastreport_sup_new.fastq" > "${barcode}_blastreport_sup_combined.fastq" 2>/dev/null || : > "${barcode}_blastreport_sup_combined.fastq"
    if [ "$(sup_count_summary_rows "${barcode}_round_sup_cached.tsv")" -gt 0 ]; then
        sup_summary_copy_header "${barcode}_round_sup_cached.tsv" "${barcode}_round_sup_combined.tsv"
    else
        sup_summary_copy_header "${barcode}_round_sup_new.tsv" "${barcode}_round_sup_combined.tsv"
    fi
    tail -n +2 "${barcode}_round_sup_cached.tsv" >> "${barcode}_round_sup_combined.tsv" 2>/dev/null || true
    tail -n +2 "${barcode}_round_sup_new.tsv" >> "${barcode}_round_sup_combined.tsv" 2>/dev/null || true

    t_start=$(now_ms)
    merge_stats=$(sup_fastq_extract_ordered \
        "${barcode}_blastreport_hac.list" \
        "${barcode}_blastreport_sup_combined.fastq" \
        "${barcode}_blastreport_sup_pre.fastq" \
        "${barcode}_sup_merge_missing_fastq_ids.list")
    t_end=$(now_ms)
    append_sup_path_timing "sup_fastq_merge" "$t_start" "$t_end"
    sup_pre_fastq_reads_merged=${merge_stats%%$'\t'*}
    dorado_sup_fastq_reads="$sup_pre_fastq_reads_merged"

    t_start=$(now_ms)
    merge_stats=$(sup_summary_extract_ordered \
        "${barcode}_blastreport_hac.list" \
        "${barcode}_round_sup_combined.tsv" \
        "${barcode}_round_sup.tsv" \
        "${barcode}_sup_merge_missing_summary_ids.list")
    t_end=$(now_ms)
    append_sup_path_timing "sup_summary_merge" "$t_start" "$t_end"
    sup_summary_rows_merged=${merge_stats%%$'\t'*}

    rm -f "${barcode}_blastreport_sup_combined.fastq" "${barcode}_round_sup_combined.tsv"

    local final_fastq_missing final_summary_missing
    final_fastq_missing=${merge_stats#*$'\t'}
    final_summary_missing=$(sup_count_nonempty_lines "${barcode}_sup_merge_missing_summary_ids.list")
    final_fastq_missing=$(sup_count_nonempty_lines "${barcode}_sup_merge_missing_fastq_ids.list")
    if [ "$final_fastq_missing" -gt 0 ]; then
        echo "WARN: missing ${final_fastq_missing} requested SUP FASTQ records after cache/new merge" 1>&2
    fi
    if [ "$final_summary_missing" -gt 0 ]; then
        echo "WARN: missing ${final_summary_missing} requested SUP summary rows after cache/new merge" 1>&2
    fi
}

sup_cache_persist() {
    local t_start t_end
    local cacheable_count=0 cache_persist_failed=0
    SUP_CACHE_SKIP_PERSIST="${SUP_CACHE_SKIP_PERSIST:-0}"

    if [ "$SUP_CACHE_SKIP_PERSIST" = "1" ]; then
        sup_emit_zero_timing "sup_cache_persist"
        return 0
    fi

    if [ ! -s "${barcode}_blastreport_sup_new.fastq" ] || [ "$(sup_count_summary_rows "${barcode}_round_sup_new.tsv")" -eq 0 ]; then
        sup_emit_zero_timing "sup_cache_persist"
        return 0
    fi

    awk 'NR%4==1{ id=substr($0,2); sub(/ .*/, "", id); print id }' "${barcode}_blastreport_sup_new.fastq" | LC_ALL=C sort -u > "${barcode}_sup_cache_new_fastq_ids.list"
    if ! sup_summary_id_list "${barcode}_round_sup_new.tsv" "${barcode}_sup_cache_new_summary_ids.list"; then
        echo "WARN: SUP cache persist skipped because summary header lacks read_id; continuing without cache update for this round" 1>&2
        SUP_CACHE_SKIP_PERSIST=1
        rm -f \
            "${barcode}_sup_cache_new_fastq_ids.list" \
            "${barcode}_sup_cache_new_summary_ids.list" \
            "${barcode}_sup_cache_cacheable_ids.list" \
            "${barcode}_sup_cache_append.fastq" \
            "${barcode}_sup_cache_append.tsv" \
            "${barcode}_sup_cache_append_missing_fastq.list" \
            "${barcode}_sup_cache_append_missing_summary.list"
        sup_emit_zero_timing "sup_cache_persist"
        return 0
    fi
    comm -12 "${barcode}_sup_cache_new_fastq_ids.list" "${barcode}_sup_cache_new_summary_ids.list" > "${barcode}_sup_cache_cacheable_ids.list" || : > "${barcode}_sup_cache_cacheable_ids.list"
    cacheable_count=$(sup_count_nonempty_lines "${barcode}_sup_cache_cacheable_ids.list")

    t_start=$(now_ms)
    if [ "$cacheable_count" -gt 0 ] && acquire_lock "$SUP_CACHE_LOCK"; then
        if ! sup_cache_prepare_locked; then
            echo "WARN: SUP cache prepare failed during persist; skipping cache update for this round" 1>&2
            SUP_CACHE_SKIP_PERSIST=1
            append_sup_path_timing "sup_cache_persist" "$t_start" "$(now_ms)"
            release_lock "$SUP_CACHE_LOCK"
            rm -f \
                "${barcode}_sup_cache_new_fastq_ids.list" \
                "${barcode}_sup_cache_new_summary_ids.list" \
                "${barcode}_sup_cache_cacheable_ids.list" \
                "${barcode}_sup_cache_append.fastq" \
                "${barcode}_sup_cache_append.tsv" \
                "${barcode}_sup_cache_append_missing_fastq.list" \
                "${barcode}_sup_cache_append_missing_summary.list"
                return 0
        fi
        if [ -s "$SUP_CACHE_MANIFEST" ]; then
            awk -v manifest="$SUP_CACHE_MANIFEST" 'BEGIN{while ((getline < manifest) > 0) if ($1 != "") seen[$1]=1; close(manifest)} NF && !($1 in seen){print $1}' \
                "${barcode}_sup_cache_cacheable_ids.list" > "${barcode}_sup_cache_cacheable_ids.filtered"
            mv "${barcode}_sup_cache_cacheable_ids.filtered" "${barcode}_sup_cache_cacheable_ids.list"
        fi
        cacheable_count=$(sup_count_nonempty_lines "${barcode}_sup_cache_cacheable_ids.list")
        if [ "$cacheable_count" -eq 0 ]; then
            append_sup_path_timing "sup_cache_persist" "$t_start" "$(now_ms)"
            release_lock "$SUP_CACHE_LOCK"
            rm -f \
                "${barcode}_sup_cache_new_fastq_ids.list" \
                "${barcode}_sup_cache_new_summary_ids.list" \
                "${barcode}_sup_cache_cacheable_ids.list" \
                "${barcode}_sup_cache_append.fastq" \
                "${barcode}_sup_cache_append.tsv" \
                "${barcode}_sup_cache_append_missing_fastq.list" \
                "${barcode}_sup_cache_append_missing_summary.list"
            return 0
        fi
        if ! sup_fastq_extract_ordered \
            "${barcode}_sup_cache_cacheable_ids.list" \
            "${barcode}_blastreport_sup_new.fastq" \
            "${barcode}_sup_cache_append.fastq" \
            "${barcode}_sup_cache_append_missing_fastq.list" >/dev/null; then
            cache_persist_failed=1
        fi
        if [ "$cache_persist_failed" = "0" ] && ! sup_summary_extract_ordered \
            "${barcode}_sup_cache_cacheable_ids.list" \
            "${barcode}_round_sup_new.tsv" \
            "${barcode}_sup_cache_append.tsv" \
            "${barcode}_sup_cache_append_missing_summary.list" >/dev/null; then
            cache_persist_failed=1
        fi
        if [ "$cache_persist_failed" = "0" ] && ! {
            gzip -dc "$SUP_CACHE_READS_GZ" 2>/dev/null || true
            cat "${barcode}_sup_cache_append.fastq"
        } | gzip -c > "${SUP_CACHE_READS_GZ}.new"; then
            cache_persist_failed=1
        fi
        if [ "$cache_persist_failed" = "0" ] && ! mv "${SUP_CACHE_READS_GZ}.new" "$SUP_CACHE_READS_GZ"; then
            cache_persist_failed=1
        fi
        if [ "$cache_persist_failed" = "0" ] && [ "$(sup_count_summary_rows "$SUP_CACHE_SUMMARY")" -eq 0 ]; then
            if ! sup_summary_copy_header "${barcode}_round_sup_new.tsv" "${SUP_CACHE_SUMMARY}.new"; then
                cache_persist_failed=1
            elif ! mv "${SUP_CACHE_SUMMARY}.new" "$SUP_CACHE_SUMMARY"; then
                cache_persist_failed=1
            fi
        fi
        if [ "$cache_persist_failed" = "0" ] && ! tail -n +2 "${barcode}_sup_cache_append.tsv" >> "$SUP_CACHE_SUMMARY" 2>/dev/null; then
            cache_persist_failed=1
        fi
        if [ "$cache_persist_failed" = "0" ] && ! awk -v rb="$round_barcode" 'NF{print $1"\t"rb}' "${barcode}_sup_cache_cacheable_ids.list" >> "$SUP_CACHE_MANIFEST"; then
            cache_persist_failed=1
        fi
        if [ "$cache_persist_failed" = "1" ]; then
            echo "WARN: SUP cache persist failed; resetting cache and continuing without reuse persistence" 1>&2
            SUP_CACHE_SKIP_PERSIST=1
            if ! sup_cache_reset_locked; then
                echo "WARN: SUP cache reset failed after persist failure; continuing without cache reuse persistence" 1>&2
            fi
        fi
        release_lock "$SUP_CACHE_LOCK"
    elif [ "$cacheable_count" -gt 0 ]; then
        echo "WARN: failed to acquire SUP cache lock for persist; skipping cache update" 1>&2
    fi
    t_end=$(now_ms)
    append_sup_path_timing "sup_cache_persist" "$t_start" "$t_end"

    rm -f \
        "${barcode}_sup_cache_new_fastq_ids.list" \
        "${barcode}_sup_cache_new_summary_ids.list" \
        "${barcode}_sup_cache_cacheable_ids.list" \
        "${barcode}_sup_cache_append.fastq" \
        "${barcode}_sup_cache_append.tsv" \
        "${barcode}_sup_cache_append_missing_fastq.list" \
        "${barcode}_sup_cache_append_missing_summary.list"
}

sup_post_dorado() {
    local t_start t_end

    if [ -s "${barcode}_blastreport_sup_pre.fastq" ] && [ -s "${barcode}_blastreport_hac.list" ]; then
        t_start=$(now_ms)
        seqtk subseq "${barcode}_blastreport_sup_pre.fastq" "${barcode}_blastreport_hac.list" > "${barcode}_hac2sup_sup.fastq" || : > "${barcode}_hac2sup_sup.fastq"
        t_end=$(now_ms)
        append_sup_path_timing "hac2sup_sup_fastq_subseq" "$t_start" "$t_end"

        t_start=$(now_ms)
        seqtk seq -a "${barcode}_hac2sup_sup.fastq" > "${barcode}_hac2sup_sup.fasta" || : > "${barcode}_hac2sup_sup.fasta"
        t_end=$(now_ms)
        append_sup_path_timing "hac2sup_sup_fasta_convert" "$t_start" "$t_end"

        t_start=$(now_ms)
        awk -F'|' '/^>/{h=substr($0,2); n=split(h,a,"|"); if(n>=5){id=a[1]; hdr=a[1]"|"a[2]"|hac|"a[4]"|"a[5]; print id"\t"hdr}}' "${SUP_FASTA_HQ_QCED}" | LC_ALL=C sort -u > "${barcode}_hac2sup_hdrmap.tsv" || : > "${barcode}_hac2sup_hdrmap.tsv"
        awk 'BEGIN{FS="\t"; while((getline<ARGV[1])>0){m[$1]=$2} ARGV[1]=""; skip=0} /^>/{id=substr($0,2); sub(/ .*/,"",id); if(id in m){print ">"m[id]; skip=0} else skip=1; next} !skip{print}' "${barcode}_hac2sup_hdrmap.tsv" "${barcode}_hac2sup_sup.fasta" > "${barcode}_qced_reads_hq_hac2sup_sup.fasta" || : > "${barcode}_qced_reads_hq_hac2sup_sup.fasta"
        t_end=$(now_ms)
        append_sup_path_timing "hac2sup_header_remap" "$t_start" "$t_end"

        hac2sup_sup_fasta_reads=$(awk '/^>/{c++} END{print c+0}' "${barcode}_qced_reads_hq_hac2sup_sup.fasta" 2>/dev/null || echo 0)
        rm -f "${barcode}_hac2sup_sup.fastq" "${barcode}_hac2sup_sup.fasta" "${barcode}_hac2sup_hdrmap.tsv"
    fi

    t_start=$(now_ms)
    cut -f1,2 -d"|" "${barcode}_blastreport_round.txt" > "${barcode}_blastreport_sup.list"
    sup_annotation_input_rows=$(sup_count_nonempty_lines "${barcode}_blastreport_sup.list")
    perl "${SUP_BASEDIR}/bin/fastq_add_annotations2ids.pl" "${barcode}_blastreport_sup.list" "${barcode}_blastreport_sup_pre.fastq" > "${barcode}_blastreport_sup_annotated_pre.fastq"
    gzip -c "${barcode}_blastreport_sup_annotated_pre.fastq" > "${barcode}_blastreport_sup_annotated_pre.fastq.gz"
    t_end=$(now_ms)
    append_sup_path_timing "sup_annotation_prepare_and_emit" "$t_start" "$t_end"
}

sup_write_stats() {
    if ! {
        printf 'key\tvalue\n'
        printf 'hac2sup_candidate_rows\t%s\n' "$hac2sup_candidate_rows"
        printf 'hac2sup_candidate_unique_read_ids\t%s\n' "$hac2sup_candidate_unique_read_ids"
        printf 'sup_annotation_input_rows\t%s\n' "$sup_annotation_input_rows"
        printf 'dorado_sup_sam_records\t%s\n' "$dorado_sup_sam_records"
        printf 'dorado_sup_fastq_reads\t%s\n' "$dorado_sup_fastq_reads"
        printf 'hac2sup_sup_fasta_reads\t%s\n' "$hac2sup_sup_fasta_reads"
        printf 'sup_cache_hit_ids\t%s\n' "$sup_cache_hit_ids"
        printf 'sup_cache_miss_ids\t%s\n' "$sup_cache_miss_ids"
        printf 'sup_cache_restored_fastq_reads\t%s\n' "$sup_cache_restored_fastq_reads"
        printf 'sup_cache_restored_summary_rows\t%s\n' "$sup_cache_restored_summary_rows"
        printf 'dorado_sup_reads_requested\t%s\n' "$dorado_sup_reads_requested"
        printf 'dorado_sup_sam_records_new\t%s\n' "$dorado_sup_sam_records_new"
        printf 'dorado_sup_fastq_reads_new\t%s\n' "$dorado_sup_fastq_reads_new"
        printf 'dorado_sup_summary_rows_new\t%s\n' "$dorado_sup_summary_rows_new"
        printf 'sup_pre_fastq_reads_merged\t%s\n' "$sup_pre_fastq_reads_merged"
        printf 'sup_summary_rows_merged\t%s\n' "$sup_summary_rows_merged"
        printf 'sup_cache_restore_missing_fastq_ids\t%s\n' "$sup_cache_restore_missing_fastq_ids"
        printf 'sup_cache_restore_missing_summary_ids\t%s\n' "$sup_cache_restore_missing_summary_ids"
        printf 'shared_extract_union_ids\t%s\n' "$shared_extract_union_ids"
        printf 'shared_extract_hac2sup_ids\t%s\n' "$shared_extract_hac2sup_ids"
        printf 'shared_extract_hac_fixed_ids\t%s\n' "$shared_extract_hac_fixed_ids"
        printf 'shared_extract_fasta_reads\t%s\n' "$shared_extract_fasta_reads"
    } > "$SUP_PATH_STATS_FILE" 2>/dev/null; then
        :
    fi
}
