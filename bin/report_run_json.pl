#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use JSON::PP qw(encode_json decode_json);
use Time::Local qw(timegm);
use File::Basename qw(dirname);
use File::Temp qw(tempfile);
use FindBin;

my $history = '';
my $out = '';
my $run_id = '';
my $barcode = '';
my $state_id = '';
my $outdir = '';
my $report_rel_path = '';
my $schema_version = '2.0';
my $status = 'running';
my $status_label = '';
my $status_color = '';
my $cadence_seconds;
my $age_seconds;

GetOptions(
    'history=s' => \$history,
    'out=s' => \$out,
    'run-id=s' => \$run_id,
    'barcode=s' => \$barcode,
    'state-id=s' => \$state_id,
    'outdir=s' => \$outdir,
    'report-rel-path=s' => \$report_rel_path,
    'schema-version=s' => \$schema_version,
    'status=s' => \$status,
    'status-label=s' => \$status_label,
    'status-color=s' => \$status_color,
    'run-started-utc-file=s' => \my $run_started_utc_file,
) or die "ERROR: invalid arguments\n";

for my $req (['history', $history], ['out', $out], ['run-id', $run_id]) {
    die "ERROR: missing --$req->[0]\n" if !defined($req->[1]) || $req->[1] eq '';
}

my $rounds_count = 0;
my $started_utc = '';
my $last_round_barcode = '';
my $last_round_ts = '';
my $prev_round_ts = '';
my $last_round_obj;
my @round_candidates;
my $identity_mode = 'collapse';

sub parse_ts {
    my ($s) = @_;
    return undef if !defined $s || $s eq '';
    if ($s =~ /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$/) {
        my ($y,$mo,$d,$h,$mi,$se) = ($1,$2,$3,$4,$5,$6);
        my $epoch = timegm($se, $mi, $h, $d, $mo - 1, $y);
        return $epoch;
    }
    return undef;
}

sub round_num_from_barcode {
    my ($rb) = @_;
    return undef if !defined $rb || $rb eq '';
    return ($rb =~ /(\d+)(?!.*\d)/) ? ($1 + 0) : undef;
}

sub compare_round_candidates {
    my ($a, $b) = @_;
    my $a_num = round_num_from_barcode($a->{round_barcode});
    my $b_num = round_num_from_barcode($b->{round_barcode});
    my $a_ts = $a->{ts_epoch};
    my $b_ts = $b->{ts_epoch};

    if (defined $a_num && defined $b_num) {
        return $a_num <=> $b_num
            || (($a->{round_barcode} // '') cmp ($b->{round_barcode} // ''));
    }
    if (defined $a_ts && defined $b_ts) {
        return $a_ts <=> $b_ts
            || (($a->{round_barcode} // '') cmp ($b->{round_barcode} // ''));
    }
    if (defined $a_ts || defined $b_ts) {
        return ((defined $a_ts ? 1 : 0) <=> (defined $b_ts ? 1 : 0))
            || (($a->{round_barcode} // '') cmp ($b->{round_barcode} // ''));
    }
    if (defined $a_num || defined $b_num) {
        return ((defined $a_num ? 1 : 0) <=> (defined $b_num ? 1 : 0))
            || (($a->{round_barcode} // '') cmp ($b->{round_barcode} // ''));
    }
    return (($a->{round_barcode} // '') cmp ($b->{round_barcode} // ''));
}

if (defined $run_started_utc_file && $run_started_utc_file ne '') {
    if (open(my $fh, '<', $run_started_utc_file)) {
        my $line = <$fh>; close $fh;
        if (defined $line) {
            $line =~ s/\s+\z//;
            my $epoch = parse_ts($line);
            if (defined $epoch) {
                $started_utc = $line;  # round loop won't lower this; run-start < any round ts
            } else {
                warn "Warning: run_started_utc.txt '$line' is not valid ISO-8601 UTC; ignoring\n";
            }
        }
    }
    # Missing file is silently ignored — best-effort, non-fatal.
}

if (-s $history) {
    open my $H, '<', $history or die "ERROR: open history: $!";
    while (my $line = <$H>) {
        chomp $line;
        next if $line =~ /^\s*$/;
        my $obj = eval { decode_json($line) };
        next if $@ || !defined $obj || ref($obj) ne 'HASH';
        next if defined($obj->{run_id}) && $obj->{run_id} ne $run_id;
        $rounds_count++;
        my $ts = defined($obj->{timestamp_utc}) ? $obj->{timestamp_utc} : '';
        my $rb = defined($obj->{round_barcode}) ? $obj->{round_barcode} : '';
        my $ts_epoch = parse_ts($ts);
        if (defined $ts_epoch) {
            if ($started_utc eq '' || $ts lt $started_utc) {
                $started_utc = $ts;
            }
        }
        push @round_candidates, {
            obj => $obj,
            round_barcode => $rb,
            timestamp_utc => $ts,
            ts_epoch => $ts_epoch,
        };
    }
    close $H;
}

if (@round_candidates) {
    my @sorted = sort { compare_round_candidates($a, $b) } @round_candidates;
    my $picked = $sorted[-1];
    $last_round_obj = $picked->{obj};
    $last_round_barcode = $picked->{round_barcode} if defined $picked->{round_barcode};
    $last_round_ts = $picked->{timestamp_utc} // '';
    if (@sorted > 1) {
        for (my $i = $#sorted - 1; $i >= 0; $i--) {
            my $prev = $sorted[$i];
            next if !defined($prev->{timestamp_utc}) || $prev->{timestamp_utc} eq '';
            $prev_round_ts = $prev->{timestamp_utc} // '';
            last;
        }
    }
}

if (defined $last_round_obj && ref($last_round_obj) eq 'HASH') {
    if ((!defined $barcode || $barcode eq '')
        && defined $last_round_obj->{barcode}
        && $last_round_obj->{barcode} ne '') {
        $barcode = $last_round_obj->{barcode};
    }
    if ((!defined $state_id || $state_id eq '')
        && defined $last_round_obj->{state_id}
        && $last_round_obj->{state_id} ne '') {
        $state_id = $last_round_obj->{state_id};
    }
    if (defined $last_round_obj->{identity_mode}
        && $last_round_obj->{identity_mode} eq 'track') {
        $identity_mode = 'track';
    }
}

my ($sec,$min,$hour,$mday,$mon,$year) = gmtime();
my $now_utc = sprintf("%04d-%02d-%02dT%02d:%02d:%02dZ",
    $year + 1900, $mon + 1, $mday, $hour, $min, $sec);

my $record = {
    schema_version => $schema_version,
    run_id => $run_id,
    barcode => $barcode,
    state_id => $state_id,
    identity_mode => $identity_mode,
    outdir => $outdir,
    report_rel_path => $report_rel_path,
    report_url => $report_rel_path,
    started_utc => $started_utc,
    last_round_barcode => $last_round_barcode,
    last_round_timestamp_utc => $last_round_ts,
    last_updated_utc => $now_utc,
    rounds_count => $rounds_count,
    status => $status,
};

if ($rounds_count == 0) {
    $record->{last_round_barcode} = '0';
    $record->{last_updated_utc} = $started_utc ne '' ? $started_utc : $now_utc;
    $record->{status_label} = 'Fresh';
    $record->{status_color} = 'green';
    $record->{report_rel_path} = '';
    $record->{report_url} = '';
}

if ($identity_mode eq 'track' && defined $report_rel_path && $report_rel_path ne '') {
    $record->{report_views} = [
        {
            view_id => 'sample',
            label => 'Run Info',
            report_rel_path => $report_rel_path,
            report_url => $report_rel_path,
            is_primary => JSON::PP::true,
        },
        {
            view_id => 'track_detail',
            label => 'Replicate Comparison',
            report_rel_path => "runs/${run_id}/report_replicates_primers.html",
            report_url => "runs/${run_id}/report_replicates_primers.html",
            is_primary => JSON::PP::false,
        },
        {
            view_id => 'replicate',
            label => 'Primer Comparison',
            report_rel_path => "runs/${run_id}/report_replicates.html",
            report_url => "runs/${run_id}/report_replicates.html",
            is_primary => JSON::PP::false,
        },
    ];
}

if (defined $last_round_obj && ref($last_round_obj) eq 'HASH') {
    my $last_round_status = defined($last_round_obj->{round_status}) && $last_round_obj->{round_status} ne ''
        ? $last_round_obj->{round_status}
        : 'ok';
    $record->{last_round_status} = $last_round_status;
    if ($last_round_status eq 'failed') {
        $record->{last_round_failure_reason} = $last_round_obj->{failure_reason}
            if defined $last_round_obj->{failure_reason} && $last_round_obj->{failure_reason} ne '';
    }
}

sub extract_run_summary {
    my ($round) = @_;
    return undef if !defined $round || ref($round) ne 'HASH';
    my $reads = (ref($round->{reads}) eq 'HASH') ? $round->{reads} : {};
    my $read_fate = (ref($round->{read_fate}) eq 'HASH') ? $round->{read_fate} : {};
    my $otu = (ref($round->{otu}) eq 'HASH') ? $round->{otu} : {};
    my $consensus = (ref($round->{consensus}) eq 'HASH') ? $round->{consensus} : {};
    return {
        reads => decode_json(encode_json($reads)),
        read_fate => decode_json(encode_json($read_fate)),
        otu => decode_json(encode_json($otu)),
        consensus => decode_json(encode_json($consensus)),
    };
}

sub build_run_status_read_fate {
    my ($history_path, $run_id, $barcode_value, $schema_version_value, $last_round_barcode_value, $last_round_obj_value) = @_;
    return undef if !defined $history_path || $history_path eq '';
    return undef if !defined $barcode_value || $barcode_value eq '';
    return undef if !defined $last_round_obj_value || ref($last_round_obj_value) ne 'HASH';

    my $markers = (ref($last_round_obj_value->{markers}) eq 'HASH') ? $last_round_obj_value->{markers} : {};
    my $order = (ref($markers->{order}) eq 'ARRAY') ? $markers->{order} : [];
    my $target_taxa = (ref($markers->{target_taxa_by_marker}) eq 'HASH') ? $markers->{target_taxa_by_marker} : {};
    my @targets = grep { defined $_ && $_ ne '' } @{$order};
    return undef if !@targets;
    my @taxa = map { defined($target_taxa->{$_}) ? $target_taxa->{$_} : '' } @targets;
    return undef if grep { !defined($_) || $_ eq '' } @taxa;

    my $state_dir = dirname($history_path);
    my $read_info = "$state_dir/${barcode_value}_read_info_rpt.txt";
    my $on_target = "$state_dir/${barcode_value}_on_target_rpt.txt";
    my $demux_cache = "$state_dir/${barcode_value}_demux_annotation_cache.tsv";
    my $blast_otu_cumulative = "$state_dir/${barcode_value}_blast_otu_pretax_rpt.txt";
    my $blast_unassigned_current = "$state_dir/${barcode_value}_blast_unassigned_current.list";

    return undef if !-s $read_info || !-s $on_target || !-s $demux_cache || !-s $blast_otu_cumulative;
    return undef if !-e $blast_unassigned_current;

    my ($tmpfh, $tmpout) = tempfile('report_run_status_read_fate.XXXXXX', SUFFIX => '.json', UNLINK => 1);
    close $tmpfh;

    my @cmd = (
        'perl',
        "$FindBin::Bin/report_round_json.pl",
        '--run-id', $run_id,
        '--barcode', $barcode_value,
        '--round-barcode', ($last_round_barcode_value || $run_id),
        '--schema-version', $schema_version_value,
        '--targets', join('|', @targets),
        '--target-taxa', join('|', @taxa),
        '--out', $tmpout,
        '--read-info', $read_info,
        '--on-target', $on_target,
        '--demult', $demux_cache,
        '--blast-otu', $blast_otu_cumulative,
        '--blast-unassigned-ids', $blast_unassigned_current,
        '--read-fate-live-current',
    );

    my $ok = system(@cmd);
    if ($ok != 0 || !-s $tmpout) {
        unlink $tmpout if -e $tmpout;
        return undef;
    }

    open my $fh, '<', $tmpout or do {
        unlink $tmpout if -e $tmpout;
        return undef;
    };
    local $/;
    my $raw = <$fh>;
    close $fh;
    unlink $tmpout if -e $tmpout;
    return undef if !defined $raw || $raw =~ /^\s*$/;
    my $obj = eval { decode_json($raw) };
    return undef if $@ || !defined $obj || ref($obj) ne 'HASH';
    return undef if ref($obj->{read_fate}) ne 'HASH';
    return decode_json(encode_json($obj->{read_fate}));
}

sub make_marker_split_warning_counts {
    return {
        cross_source_disagreement => {
            demult_over_blast => 0,
            demult_over_sample_label => 0,
            blast_over_sample_label => 0,
        },
    };
}

sub make_marker_split_fatal_counts {
    return {
        same_source_conflict => {
            demult => 0,
            blast => 0,
            sample_label => 0,
        },
        bucket_conflict => 0,
        bucket_unresolved => 0,
        unresolved_marker => 0,
        demux_stage_absent => 0,
        blast_stage_absent => 0,
        missing_or_unusable_demux_read_id => 0,
        missing_or_unusable_demux_sample => 0,
        missing_or_unusable_blast_read_id => 0,
        missing_required_global_total => 0,
        missing_required_stage_total => 0,
        demux_disabled => 0,
        invalid_stage_order_on_target_gt_total => 0,
        invalid_stage_order_demux_gt_on_target => 0,
        invalid_stage_order_blast_seen_gt_demux => 0,
        invalid_stage_order_assigned_gt_seen => 0,
        invalid_stage_order_unassigned_gt_seen => 0,
        invalid_stage_order_assigned_plus_unassigned_ne_seen => 0,
        invariant_no_adapter_gt_demux => 0,
        invariant_demux_split_mismatch => 0,
        invariant_blast_seen_split_mismatch => 0,
        invariant_blast_assigned_split_mismatch => 0,
        invariant_blast_unassigned_split_mismatch => 0,
        chart_negative_skipped_coi => 0,
        chart_negative_skipped_its2 => 0,
        chart_negative_on_target_not_demultiplexed => 0,
        chart_negative_off_target => 0,
        chart_total_mismatch => 0,
    };
}

sub make_empty_read_fate_summary {
    return {
        demux_total_reads => undef,
        no_adapter_reads => undef,
        demux_enabled => undef,
        blast_seen_reads => undef,
        blast_assigned_reads => undef,
        blast_unassigned_reads => undef,
        blast_seen_reads_unbucketed => undef,
        marker_split_status => 'invalid',
        data_reason_codes => [],
        chart_reason_codes => [],
        marker_split_invalid_read_count => 0,
        marker_split_warning_counts => make_marker_split_warning_counts(),
        marker_split_fatal_counts => make_marker_split_fatal_counts(),
        demux_total_reads_coi => undef,
        demux_total_reads_its2 => undef,
        blast_seen_reads_coi => undef,
        blast_seen_reads_its2 => undef,
        blast_assigned_reads_coi => undef,
        blast_assigned_reads_its2 => undef,
        blast_unassigned_reads_coi => undef,
        blast_unassigned_reads_its2 => undef,
        chart_blast_assigned_coi => undef,
        chart_blast_assigned_its2 => undef,
        chart_blast_unassigned_coi => undef,
        chart_blast_unassigned_its2 => undef,
        chart_blast_skipped_coi => undef,
        chart_blast_skipped_its2 => undef,
        chart_on_target_not_demultiplexed => undef,
        chart_off_target => undef,
    };
}

sub add_numeric {
    my ($sumref, $key, $val) = @_;
    return 0 if !defined $val;
    return 0 if ref($val) ne '' && ref($val) ne 'SCALAR';
    return 0 if $val !~ /^-?\d+(?:\.\d+)?$/;
    $sumref->{$key} += $val;
    return 1;
}

sub add_nested_numeric_counts {
    my ($sumref, $valsref) = @_;
    return if ref($sumref) ne 'HASH' || ref($valsref) ne 'HASH';
    for my $key (keys %{$sumref}) {
        if (ref($sumref->{$key}) eq 'HASH') {
            add_nested_numeric_counts($sumref->{$key}, $valsref->{$key});
            next;
        }
        my $val = (ref($valsref) eq 'HASH') ? $valsref->{$key} : undef;
        next if !defined $val;
        next if ref($val) ne '' && ref($val) ne 'SCALAR';
        next if $val !~ /^-?\d+(?:\.\d+)?$/;
        $sumref->{$key} += $val;
    }
}

sub aggregate_run_summary {
    my ($candidates) = @_;
    return undef if !defined $candidates || ref($candidates) ne 'ARRAY' || !@$candidates;
    my %reads_sum = ( total => 0, on_target => 0 );
    my $fate_sum = make_empty_read_fate_summary();
    my @retained_numeric = qw(
        demux_total_reads
        no_adapter_reads
        blast_seen_reads
        blast_assigned_reads
        blast_unassigned_reads
        blast_seen_reads_unbucketed
    );
    my @marker_numeric = qw(
        demux_total_reads_coi
        demux_total_reads_its2
        blast_seen_reads_coi
        blast_seen_reads_its2
        blast_assigned_reads_coi
        blast_assigned_reads_its2
        blast_unassigned_reads_coi
        blast_unassigned_reads_its2
    );
    my @chart_numeric = qw(
        chart_blast_assigned_coi
        chart_blast_assigned_its2
        chart_blast_unassigned_coi
        chart_blast_unassigned_its2
        chart_blast_skipped_coi
        chart_blast_skipped_its2
        chart_on_target_not_demultiplexed
        chart_off_target
    );
    my %retained_can_sum = map { $_ => 1 } @retained_numeric;
    my %marker_can_sum = map { $_ => 1 } @marker_numeric;
    my %chart_can_sum = map { $_ => 1 } @chart_numeric;
    my $all_rounds_ok = 1;
    my %invalid_data_reasons;
    my %invalid_chart_reasons;
    for my $c (@$candidates) {
        next if !defined $c || ref($c) ne 'HASH';
        my $round = $c->{obj};
        next if !defined $round || ref($round) ne 'HASH';
        my $reads = (ref($round->{reads}) eq 'HASH') ? $round->{reads} : {};
        my $fate = (ref($round->{read_fate}) eq 'HASH') ? $round->{read_fate} : {};
        add_numeric(\%reads_sum, 'total', $reads->{total});
        add_numeric(\%reads_sum, 'on_target', $reads->{on_target});
        for my $field (@retained_numeric) {
            if (!add_numeric($fate_sum, $field, $fate->{$field})) {
                $retained_can_sum{$field} = 0;
            }
        }
        for my $field (@marker_numeric) {
            if (!add_numeric($fate_sum, $field, $fate->{$field})) {
                $marker_can_sum{$field} = 0;
            }
        }
        for my $field (@chart_numeric) {
            if (!add_numeric($fate_sum, $field, $fate->{$field})) {
                $chart_can_sum{$field} = 0;
            }
        }
        add_nested_numeric_counts($fate_sum->{marker_split_warning_counts}, $fate->{marker_split_warning_counts});
        add_nested_numeric_counts($fate_sum->{marker_split_fatal_counts}, $fate->{marker_split_fatal_counts});
        add_numeric($fate_sum, 'marker_split_invalid_read_count', $fate->{marker_split_invalid_read_count});
        my $status = $fate->{marker_split_status} // 'invalid';
        if ($status ne 'ok') {
            $all_rounds_ok = 0;
            if (ref($fate->{data_reason_codes}) eq 'ARRAY') {
                $invalid_data_reasons{$_} = 1 for grep { defined($_) && $_ ne '' } @{$fate->{data_reason_codes}};
            }
            if (ref($fate->{chart_reason_codes}) eq 'ARRAY') {
                $invalid_chart_reasons{$_} = 1 for grep { defined($_) && $_ ne '' } @{$fate->{chart_reason_codes}};
            }
        }
    }
    for my $field (@retained_numeric) {
        $fate_sum->{$field} = undef if !$retained_can_sum{$field};
    }
    for my $field (@marker_numeric) {
        $fate_sum->{$field} = undef if !$all_rounds_ok || !$marker_can_sum{$field};
    }
    for my $field (@chart_numeric) {
        $fate_sum->{$field} = undef if !$all_rounds_ok || !$chart_can_sum{$field};
    }
    $fate_sum->{demux_enabled} = undef;
    $fate_sum->{marker_split_status} = $all_rounds_ok ? 'ok' : 'invalid';
    $fate_sum->{data_reason_codes} = $all_rounds_ok ? [] : [sort keys %invalid_data_reasons];
    $fate_sum->{chart_reason_codes} = $all_rounds_ok ? [] : [sort keys %invalid_chart_reasons];
    return {
        reads => \%reads_sum,
        read_fate => $fate_sum,
    };
}

if (defined $last_round_obj) {
    my $summary = extract_run_summary($last_round_obj);
    my $totals = aggregate_run_summary(\@round_candidates);
    my $run_status_read_fate = build_run_status_read_fate(
        $history,
        $run_id,
        $barcode,
        $schema_version,
        $last_round_barcode,
        $last_round_obj,
    );
    $record->{run_summary} = $summary if defined $summary;
    $record->{run_totals} = $totals if defined $totals;
    $record->{run_status_read_fate} = $run_status_read_fate if defined $run_status_read_fate;
    $record->{run_summary_source_round} = $last_round_barcode if $last_round_barcode ne '';
}

my $now_epoch = parse_ts($now_utc);
my $last_epoch = parse_ts($last_round_ts);
my $prev_epoch = parse_ts($prev_round_ts);
my $start_epoch = parse_ts($started_utc);

if (defined $now_epoch && defined $last_epoch) {
    $age_seconds = $now_epoch - $last_epoch;
    if (defined $prev_epoch && $last_epoch > $prev_epoch) {
        $cadence_seconds = $last_epoch - $prev_epoch;
    } elsif (defined $start_epoch && $last_epoch > $start_epoch) {
        $cadence_seconds = $last_epoch - $start_epoch;
    }
}

if (defined $age_seconds && defined $cadence_seconds && $cadence_seconds > 0) {
    my $ratio = $age_seconds / $cadence_seconds;
    if ($ratio <= 1.5) {
        $status_label = 'Fresh';
        $status_color = 'green';
    } elsif ($ratio <= 3.0) {
        $status_label = 'Aging';
        $status_color = 'orange';
    } else {
        $status_label = 'Stale';
        $status_color = 'red';
    }
    $record->{status_label} = $status_label;
    $record->{status_color} = $status_color;
    $record->{status_age_seconds} = int($age_seconds);
    $record->{status_cadence_seconds} = int($cadence_seconds);
}

open my $O, '>', $out or die "ERROR: open out: $!";
print {$O} encode_json($record), "\n";
close $O;

exit 0;
