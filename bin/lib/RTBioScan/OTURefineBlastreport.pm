package RTBioScan::OTURefineBlastreport;

use strict;
use warnings;

use Exporter 'import';
use Time::HiRes qw(time);

our @EXPORT_OK = qw(
    append_phase_timing_row
    collect_cluster_taxids_from_cluster_file
    collect_cluster_taxids_from_inputs
    collect_entries_from_inputs
    collect_entries_from_cluster_file
    emit_annotated_entries
    load_cluster_taxids_from_file
    load_target_tax_maps
    print_cluster_taxids
    print_pairs_only_from_cluster_taxids_file
    print_full_output
    print_pairs_only
    write_cluster_taxids_with_phase_timings
);

sub is_valid_taxid {
    my ($taxid) = @_;
    return defined $taxid && $taxid ne '' && $taxid ne 'NA';
}

sub normalize_read_key {
    my ($read_id) = @_;
    return '' if !defined $read_id;
    $read_id =~ s/\|.*$//;
    return $read_id;
}

sub choose_cluster_taxid {
    my ($counts_ref) = @_;
    return '' if !defined $counts_ref || !%{$counts_ref};

    my @ranked = sort {
        $counts_ref->{$b} <=> $counts_ref->{$a}
            || (is_valid_taxid($b) <=> is_valid_taxid($a))
            || $a cmp $b
    } keys %{$counts_ref};

    for my $taxid (@ranked) {
        return $taxid if is_valid_taxid($taxid);
    }
    return $ranked[0];
}

sub load_target_tax_maps {
    my ($input1) = @_;

    my (%target_tax, %target_tax_norm);
    open(my $report_fh, '<', $input1) or die "Cannot open $input1: $!";
    while (my $line = <$report_fh>) {
        chomp $line;
        next if $line !~ /\S/;
        my ($seq_id, $taxid) = split /\;/, $line;
        next if !defined $seq_id || !defined $taxid;
        $seq_id =~ s/^\s+|\s+$//g;
        $taxid =~ s/^\s+|\s+$//g;
        $target_tax{$seq_id} = $taxid;
        my $normalized = normalize_read_key($seq_id);
        $target_tax_norm{$normalized} = $taxid if $normalized ne '';
    }
    close $report_fh;

    return (\%target_tax, \%target_tax_norm);
}

sub _append_phase_timing {
    my ($phase_file, $round_id, $phase, $start_ts, $end_ts) = @_;
    return if !defined $round_id || $round_id eq '';

    my $ms = 0;
    my $seconds = 0;
    if (defined $start_ts && defined $end_ts && $end_ts >= $start_ts) {
        $ms = int((($end_ts - $start_ts) * 1000) + 0.5);
        $ms = 0 if $ms < 0;
        $seconds = int($ms / 1000);
        $seconds = 0 if $seconds < 0;
    }

    if (defined $phase_file && $phase_file ne '') {
        if (open(my $phase_fh, '>>', $phase_file)) {
            print {$phase_fh} join("\t", $round_id, $phase, $seconds) . "\n";
            close $phase_fh;
        }
    }

    my $phase_ms_file = $ENV{OTU_REFINE_PHASE_TIMINGS_MS_FILE} // '';
    if ($phase_ms_file ne '') {
        if (open(my $phase_ms_fh, '>>', $phase_ms_file)) {
            print {$phase_ms_fh} join("\t", $round_id, $phase, $seconds, $ms) . "\n";
            close $phase_ms_fh;
        }
    }
}

sub append_phase_timing_row {
    my ($phase_file, $round_id, $phase, $start_ts, $end_ts) = @_;
    _append_phase_timing($phase_file, $round_id, $phase, $start_ts, $end_ts);
}

sub _finalize_cluster_entries {
    my ($cluster_id_ref, $members_ref, $counts_ref, $entries_ref) = @_;
    return if !defined ${$cluster_id_ref};
    return if !@{$members_ref};

    my $cluster_taxid = choose_cluster_taxid($counts_ref);
    for my $member_id (@{$members_ref}) {
        my $marker = '';
        if ($member_id =~ /^[^|]+\|([^|]+)/) {
            $marker = $1 // '';
        }
        my $otu_tag = "OTUB_${$cluster_id_ref}";
        $otu_tag .= "-$marker" if defined $marker && $marker ne '';
        push @{$entries_ref}, [ "$member_id|$otu_tag", $cluster_taxid ];
    }
}

sub _finalize_cluster_taxid {
    my ($cluster_id_ref, $counts_ref, $cluster_taxids_ref) = @_;
    return if !defined ${$cluster_id_ref};

    my $cluster_taxid = choose_cluster_taxid($counts_ref);
    push @{$cluster_taxids_ref}, [ ${$cluster_id_ref}, $cluster_taxid ];
}

sub collect_cluster_taxids_from_cluster_file {
    my ($cluster_file, $target_tax_ref, $target_tax_norm_ref) = @_;

    my @cluster_taxids;
    my $current_cluster_id;
    my %current_tax_counts;

    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            _finalize_cluster_taxid(
                \$current_cluster_id,
                \%current_tax_counts,
                \@cluster_taxids,
            );
            $current_cluster_id = $1;
            %current_tax_counts = ();
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $ref_id = $1;
        my $taxid = $target_tax_ref->{$ref_id};
        if (!defined $taxid || $taxid eq '') {
            my $normalized = normalize_read_key($ref_id);
            $taxid = $target_tax_norm_ref->{$normalized} if $normalized ne '';
            $target_tax_ref->{$ref_id} = $taxid if defined $taxid && $taxid ne '';
        }
        $current_tax_counts{$taxid}++ if defined $taxid && $taxid ne '';
    }
    close $cluster_fh;

    _finalize_cluster_taxid(
        \$current_cluster_id,
        \%current_tax_counts,
        \@cluster_taxids,
    );

    return \@cluster_taxids;
}

sub collect_entries_from_cluster_file {
    my ($cluster_file, $target_tax_ref, $target_tax_norm_ref) = @_;

    my @entries;
    my $current_cluster_id;
    my @current_members;
    my %current_tax_counts;

    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            _finalize_cluster_entries(
                \$current_cluster_id,
                \@current_members,
                \%current_tax_counts,
                \@entries,
            );
            $current_cluster_id = $1;
            @current_members = ();
            %current_tax_counts = ();
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $ref_id = $1;
        push @current_members, $ref_id;

        my $taxid = $target_tax_ref->{$ref_id};
        if (!defined $taxid || $taxid eq '') {
            my $normalized = normalize_read_key($ref_id);
            $taxid = $target_tax_norm_ref->{$normalized} if $normalized ne '';
            $target_tax_ref->{$ref_id} = $taxid if defined $taxid && $taxid ne '';
        }
        $current_tax_counts{$taxid}++ if defined $taxid && $taxid ne '';
    }
    close $cluster_fh;

    _finalize_cluster_entries(
        \$current_cluster_id,
        \@current_members,
        \%current_tax_counts,
        \@entries,
    );

    return \@entries;
}

sub collect_entries_from_inputs {
    my ($input1, $cluster_file) = @_;
    my ($target_tax_ref, $target_tax_norm_ref) = load_target_tax_maps($input1);
    return collect_entries_from_cluster_file($cluster_file, $target_tax_ref, $target_tax_norm_ref);
}

sub collect_cluster_taxids_from_inputs {
    my ($input1, $cluster_file) = @_;
    my ($target_tax_ref, $target_tax_norm_ref) = load_target_tax_maps($input1);
    return collect_cluster_taxids_from_cluster_file($cluster_file, $target_tax_ref, $target_tax_norm_ref);
}

sub load_cluster_taxids_from_file {
    my ($cluster_taxids_file) = @_;

    my %cluster_taxids;
    open(my $cluster_taxids_fh, '<', $cluster_taxids_file)
        or die "Cannot open $cluster_taxids_file: $!";
    while (my $line = <$cluster_taxids_fh>) {
        chomp $line;
        next if $line !~ /\S/;
        my ($cluster_id, $taxid) = split /\t/, $line, 2;
        next if !defined $cluster_id || $cluster_id eq '';
        $taxid = '' if !defined $taxid;
        $cluster_taxids{$cluster_id} = $taxid;
    }
    close $cluster_taxids_fh;

    return \%cluster_taxids;
}

sub _load_lineage_overrides {
    my ($lineage_file) = @_;
    my %id2lineage;

    open(my $lineage_fh, '<', $lineage_file) or die "I couldn't open $lineage_file\n";
    while (my $line = <$lineage_fh>) {
        chomp $line;
        next if $line !~ /\S/;
        my ($taxid, $lineage) = split /\t/, $line, 2;
        next if !defined $taxid || !defined $lineage;
        $id2lineage{$taxid} = $lineage;
    }
    close $lineage_fh;

    return \%id2lineage;
}

sub _resolve_taxid_lineages {
    my ($entries_ref, $id2lineage_ref) = @_;
    my %needed_taxids;

    for my $entry_ref (@{$entries_ref}) {
        my $taxid = $entry_ref->[1];
        $needed_taxids{$taxid} = 1 if defined $taxid && $taxid ne '';
    }

    my %taxid_lineage;
    my @pending = sort { $a <=> $b } grep { defined($_) && $_ =~ /^\d+$/ } keys %needed_taxids;
    while (@pending) {
        my @batch = splice @pending, 0, 100000;
        my $input = join("\n", @batch) . "\n";
        my $output = `printf '%s' "$input" | taxonkit lineage 2>/dev/null | taxonkit reformat -f "{K};{p};{c};{o};{f};{g};{s}" -P | cut -f3`;
        my @lines = split /\n/, $output;
        for my $idx (0 .. $#batch) {
            my $taxid = $batch[$idx];
            my $lineage = $lines[$idx] // '';
            $lineage =~ s/\s+$//;
            $taxid_lineage{$taxid} = $lineage if $lineage ne '';
        }
    }

    return \%taxid_lineage;
}

sub emit_annotated_entries {
    my ($entries_ref, $lineage_file, $out_fh, $stats_ref) = @_;
    my $header = "#seq_id\ttax_id\tlineage\n";
    print {$out_fh} $header;
    if (defined $stats_ref && ref($stats_ref) eq 'HASH') {
        $stats_ref->{annotated_rows} = 0 if !defined $stats_ref->{annotated_rows};
        $stats_ref->{annotated_bytes} = 0 if !defined $stats_ref->{annotated_bytes};
        $stats_ref->{annotated_bytes} += length($header);
    }
    return if !defined $lineage_file || !-e $lineage_file;

    my $phase_file = $ENV{OTU_REFINE_PHASE_TIMINGS_FILE} // '';
    my $round_id = $ENV{OTU_REFINE_ROUND_ID} // '';
    my $t_lineage_start = time();
    my $id2lineage_ref = _load_lineage_overrides($lineage_file);
    my $taxid_lineage_ref = _resolve_taxid_lineages($entries_ref, $id2lineage_ref);
    my $t_lineage_end = time();
    _append_phase_timing($phase_file, $round_id, 'lineage_resolution', $t_lineage_start, $t_lineage_end);
    my %lineage_cache;
    my $fallback_lineage = 'K__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned';

    my $t_emit_start = time();
    for my $entry_ref (@{$entries_ref}) {
        my ($seq_id, $otu_taxid) = @{$entry_ref};
        $otu_taxid = '' if !defined $otu_taxid;
        $otu_taxid =~ s/^\s+//;
        $otu_taxid =~ s/\s+$//;

        my $lineage = $lineage_cache{$otu_taxid};
        if (!defined $lineage || $lineage eq '') {
            if ($otu_taxid eq '' || $otu_taxid eq 'NA') {
                $lineage = $fallback_lineage;
            } elsif (exists $id2lineage_ref->{$otu_taxid}) {
                $lineage = $id2lineage_ref->{$otu_taxid};
            } elsif (exists $taxid_lineage_ref->{$otu_taxid}) {
                $lineage = $taxid_lineage_ref->{$otu_taxid};
            } else {
                $lineage = $fallback_lineage;
            }
            $lineage_cache{$otu_taxid} = $lineage;
        }

        my $row = "$seq_id\t$otu_taxid\t$lineage\n";
        print {$out_fh} $row;
        if (defined $stats_ref && ref($stats_ref) eq 'HASH') {
            $stats_ref->{annotated_rows}++;
            $stats_ref->{annotated_bytes} += length($row);
        }
    }
    my $t_emit_end = time();
    _append_phase_timing($phase_file, $round_id, 'annotated_emit', $t_emit_start, $t_emit_end);
}

sub print_pairs_only {
    my ($input1, $cluster_file, $out_fh) = @_;
    my $entries_ref = collect_entries_from_inputs($input1, $cluster_file);
    for my $entry_ref (@{$entries_ref}) {
        print {$out_fh} join("\t", @{$entry_ref}) . "\n";
    }
}

sub print_cluster_taxids {
    my ($input1, $cluster_file, $out_fh) = @_;
    my $cluster_taxids_ref = collect_cluster_taxids_from_inputs($input1, $cluster_file);
    for my $entry_ref (@{$cluster_taxids_ref}) {
        my ($cluster_id, $taxid) = @{$entry_ref};
        $taxid = '' if !defined $taxid;
        print {$out_fh} "$cluster_id\t$taxid\n";
    }
}

sub write_cluster_taxids_with_phase_timings {
    my ($input1, $cluster_file, $cluster_taxids_file, $arg4, $arg5, $arg6) = @_;

    my $cluster_sizes_file = '';
    my $phase_file = $arg4;
    my $round_id = $arg5;
    if (defined $arg6) {
        $cluster_sizes_file = $arg4 // '';
        $phase_file = $arg5;
        $round_id = $arg6;
    }

    my $t_load_start = time();
    my ($target_tax_ref, $target_tax_norm_ref) = load_target_tax_maps($input1);
    my $t_load_end = time();
    _append_phase_timing($phase_file, $round_id, 'load_blast_tax_map', $t_load_start, $t_load_end);

    my $t_cluster_start = time();
    open(my $cluster_taxids_fh, '>', $cluster_taxids_file)
        or die "Cannot open $cluster_taxids_file for write: $!";
    my $cluster_sizes_fh;
    if (defined $cluster_sizes_file && $cluster_sizes_file ne '') {
        open($cluster_sizes_fh, '>', $cluster_sizes_file)
            or die "Cannot open $cluster_sizes_file for write: $!";
    }

    my $current_cluster_id;
    my %current_tax_counts;
    my $current_cluster_records = 0;
    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            if (defined $current_cluster_id) {
                my $cluster_taxid = choose_cluster_taxid(\%current_tax_counts);
                $cluster_taxid = '' if !defined $cluster_taxid;
                print {$cluster_taxids_fh} "$current_cluster_id\t$cluster_taxid\n";
                print {$cluster_sizes_fh} "$current_cluster_id\t$current_cluster_records\n"
                    if defined $cluster_sizes_fh;
            }
            $current_cluster_id = $1;
            %current_tax_counts = ();
            $current_cluster_records = 0;
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $ref_id = $1;
        $current_cluster_records++;
        my $taxid = $target_tax_ref->{$ref_id};
        if (!defined $taxid || $taxid eq '') {
            my $normalized = normalize_read_key($ref_id);
            $taxid = $target_tax_norm_ref->{$normalized} if $normalized ne '';
            $target_tax_ref->{$ref_id} = $taxid if defined $taxid && $taxid ne '';
        }
        $current_tax_counts{$taxid}++ if defined $taxid && $taxid ne '';
    }
    close $cluster_fh;

    if (defined $current_cluster_id) {
        my $cluster_taxid = choose_cluster_taxid(\%current_tax_counts);
        $cluster_taxid = '' if !defined $cluster_taxid;
        print {$cluster_taxids_fh} "$current_cluster_id\t$cluster_taxid\n";
        print {$cluster_sizes_fh} "$current_cluster_id\t$current_cluster_records\n"
            if defined $cluster_sizes_fh;
    }
    close $cluster_taxids_fh;
    close $cluster_sizes_fh if defined $cluster_sizes_fh;

    my $t_cluster_end = time();
    _append_phase_timing($phase_file, $round_id, 'cluster_taxid_pass', $t_cluster_start, $t_cluster_end);
}

sub print_pairs_only_from_cluster_taxids_file {
    my ($cluster_taxids_file, $cluster_file, $out_fh) = @_;
    my $cluster_taxids_ref = load_cluster_taxids_from_file($cluster_taxids_file);
    my $current_cluster_id;
    my $current_cluster_taxid = '';

    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            $current_cluster_id = $1;
            die "Missing cluster taxid for cluster $current_cluster_id\n"
                if !exists $cluster_taxids_ref->{$current_cluster_id};
            $current_cluster_taxid = $cluster_taxids_ref->{$current_cluster_id};
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $member_id = $1;
        my $marker = '';
        if ($member_id =~ /^[^|]+\|([^|]+)/) {
            $marker = $1 // '';
        }
        my $otu_tag = "OTUB_${current_cluster_id}";
        $otu_tag .= "-$marker" if defined $marker && $marker ne '';
        print {$out_fh} "$member_id|$otu_tag\t$current_cluster_taxid\n";
    }
    close $cluster_fh;
}

sub print_full_output {
    my ($input1, $cluster_file, $lineage_file, $out_fh) = @_;
    my $entries_ref = collect_entries_from_inputs($input1, $cluster_file);
    emit_annotated_entries($entries_ref, $lineage_file, $out_fh);
}

1;
