#!/usr/bin/perl
use warnings;
use FindBin;
use POSIX qw(strftime);
use Time::Piece;
require "$FindBin::Bin/lib/taxon_util.pl";
require "$FindBin::Bin/reporting_contract_sidecar.pl";
require "$FindBin::Bin/reporting_parser_state_transaction.pl";
*is_unassigned_taxon = \&TaxonUtil::is_unassigned_taxon;

sub run_cmd {
    my ($cmd, %opts) = @_;
    my $ret = system($cmd);
    if ($ret != 0) {
        my $sig  = $ret & 127;
        my $exit = $ret >> 8;
        my $msg  = "command failed (exit=$exit sig=$sig): $cmd";
        die "ERROR: $msg\n" if $opts{fatal};
        warn "WARN: $msg\n";
    }
    return $ret;
}

sub count_lines {
    my ($path) = @_;
    return 0 unless $path && -f $path;
    open my $fh, '<', $path or return 0;
    my $count = 0;
    $count++ while <$fh>;
    close $fh;
    return $count;
}

sub count_unique_read_ids {
    my ($path) = @_;
    return 0 unless $path && -f $path;
    open my $fh, '<', $path or return 0;
    my $header = <$fh>;
    return 0 unless defined $header;
    chomp $header;
    my @cols = split(/\t/, $header);
    my %hidx = map { $cols[$_] => $_ } 0..$#cols;
    my $rid_idx = exists $hidx{"read_id"} ? $hidx{"read_id"} : 0;
    my %seen;
    while (my $line = <$fh>) {
        chomp $line;
        next unless $line =~ /\S/;
        my @tr = split(/\t/, $line);
        my $rid = $tr[$rid_idx] // '';
        next unless defined $rid && $rid ne '';
        $rid =~ s/\r$//;
        $seen{$rid} = 1;
    }
    close $fh;
    return scalar(keys %seen);
}

sub count_reads_by_qscore {
    my ($path) = @_;
    return (0,0,0) unless $path && -f $path;
    open my $fh, '<', $path or return (0,0,0);
    my $hdr = <$fh>;
    my %hidx;
    if (defined $hdr) {
        chomp $hdr;
        my @cols = split /	/, $hdr;
        for (my $i=0; $i<@cols; $i++) { $hidx{$cols[$i]} = $i; }
    }
    my ($fast, $hac, $sup) = (0,0,0);
    while (my $l = <$fh>) {
        chomp $l;
        next unless $l ne '';
        my @tr = split /	/, $l;
        $fast++ if exists $hidx{"fast_mean_qscore"} && defined $tr[$hidx{"fast_mean_qscore"}] && $tr[$hidx{"fast_mean_qscore"}] ne "NA";
        $hac++  if exists $hidx{"hac_mean_qscore"}  && defined $tr[$hidx{"hac_mean_qscore"}]  && $tr[$hidx{"hac_mean_qscore"}]  ne "NA";
        $sup++  if exists $hidx{"sup_mean_qscore"}  && defined $tr[$hidx{"sup_mean_qscore"}]  && $tr[$hidx{"sup_mean_qscore"}]  ne "NA";
    }
    close $fh;
    return ($fast, $hac, $sup);
}

sub write_flag_file {
	my ($path, $content) = @_;
	my $tmp = "$path.tmp.$$";
	open my $fh, '>', $tmp or die "ERROR: unable to write $tmp\n";
	print $fh defined($content) ? $content : "";
	close $fh;
	rename $tmp, $path or die "ERROR: unable to replace $path\n";
}

sub build_accumulated_report_tmp {
	my (%args) = @_;
	my $source_report = $args{source_report} // die "ERROR: missing source_report\n";
	my $state_report = $args{state_report} // die "ERROR: missing state_report\n";
	my $tmp_report = $args{tmp_report} // die "ERROR: missing tmp_report\n";

	open my $out_fh, '>', $tmp_report or die "ERROR: unable to write '$tmp_report'\n";
	if (-f $state_report) {
		open my $state_fh, '<', $state_report or die "ERROR: unable to open '$state_report'\n";
		while (my $line = <$state_fh>) {
			print {$out_fh} $line;
		}
		close $state_fh;

		open my $src_fh, '<', $source_report or die "ERROR: unable to open '$source_report'\n";
		<$src_fh>; # skip source header
		while (my $line = <$src_fh>) {
			print {$out_fh} $line;
		}
		close $src_fh;
	} else {
		open my $src_fh, '<', $source_report or die "ERROR: unable to open '$source_report'\n";
		while (my $line = <$src_fh>) {
			print {$out_fh} $line;
		}
		close $src_fh;
	}
	close $out_fh;
}

sub replace_report_pair_atomically {
	my (%args) = @_;
	my $state_report = $args{state_report} // die "ERROR: missing state_report\n";
	my $state_sidecar = $args{state_sidecar} // die "ERROR: missing state_sidecar\n";
	my $tmp_report = $args{tmp_report} // die "ERROR: missing tmp_report\n";
	my $tmp_sidecar = $args{tmp_sidecar} // die "ERROR: missing tmp_sidecar\n";

	my $bak_report = "$state_report.bak.$$";
	my $bak_sidecar = "$state_sidecar.bak.$$";
	my $had_report = -e $state_report ? 1 : 0;
	my $had_sidecar = -e $state_sidecar ? 1 : 0;

	eval {
		rename $state_report, $bak_report or die "ERROR: unable to stage backup '$bak_report'\n"
			if $had_report;
		rename $state_sidecar, $bak_sidecar or die "ERROR: unable to stage backup '$bak_sidecar'\n"
			if $had_sidecar;
		rename $tmp_report, $state_report or die "ERROR: unable to replace '$state_report'\n";
		rename $tmp_sidecar, $state_sidecar or die "ERROR: unable to replace '$state_sidecar'\n";
		1;
	} or do {
		my $err = $@ || "ERROR: unknown transactional replace failure\n";
		unlink $state_report if -e $state_report && !$had_report;
		unlink $state_sidecar if -e $state_sidecar && !$had_sidecar;
		rename $bak_report, $state_report if $had_report && -e $bak_report;
		rename $bak_sidecar, $state_sidecar if $had_sidecar && -e $bak_sidecar;
		unlink $tmp_report if -e $tmp_report;
		unlink $tmp_sidecar if -e $tmp_sidecar;
		die $err;
	};

	unlink $bak_report if -e $bak_report;
	unlink $bak_sidecar if -e $bak_sidecar;
}

sub update_cumulative_report_pair {
	my (%args) = @_;
	my $report_kind = $args{report_kind} // die "ERROR: missing report_kind\n";
	my $context = $args{context} // die "ERROR: missing context\n";
	my $round_report = $args{round_report} // die "ERROR: missing round_report\n";
	my $round_sidecar = $args{round_sidecar} // die "ERROR: missing round_sidecar\n";
	my $state_report = $args{state_report} // die "ERROR: missing state_report\n";
	my $state_sidecar = $args{state_sidecar} // die "ERROR: missing state_sidecar\n";

	die "ERROR: missing current-round ${report_kind} report '$round_report'\n" if !-e $round_report;
	die "ERROR: missing current-round ${report_kind} sidecar '$round_sidecar'\n" if !-e $round_sidecar;
	die "ERROR: accumulated ${report_kind} report/sidecar skew in '$temp_dir'\n"
		if ((-e $state_report) xor (-e $state_sidecar));

	ReportingContractSidecar::validate_report_and_sidecar(
		report_kind => $report_kind,
		context => $context,
		report_path => $round_report,
		sidecar_path => $round_sidecar,
	);

	my $tmp_report = "$state_report.tmp.$$";
	my $tmp_sidecar = "$state_sidecar.tmp.$$";
	my $is_first_seed = !-e $state_report;

	build_accumulated_report_tmp(
		source_report => $round_report,
		state_report => $state_report,
		tmp_report => $tmp_report,
	);
	ReportingContractSidecar::write_sidecar_from_report(
		report_kind => $report_kind,
		context => $context,
		report_path => $tmp_report,
		sidecar_path => $tmp_sidecar,
	);
	replace_report_pair_atomically(
		state_report => $state_report,
		state_sidecar => $state_sidecar,
		tmp_report => $tmp_report,
		tmp_sidecar => $tmp_sidecar,
	);

	return $is_first_seed;
}

$round_dir=$ARGV[0];
$temp_dir=$ARGV[1];
$pod5_file=$ARGV[2];
$barcode_pipeline=$ARGV[3];

# Arguments (current calling convention from main.nf):
#   0 round_dir
#   1 temp_dir
#   2 pod5_file
#   3 barcode_pipeline
#   4 sequencing_time_file (ledger)
#   5 min_reads_sample (optional; numeric)
#   6 metazoa_spc_basics (optional path)
#   7 viridiplantae_spc_basics (optional path)
#   8 local_metazoa_gns (optional path)
#   9 local_viridiplantae_gns (optional path)
# Environment:
#   RTBIOSCAN_DEMUX_IDENTITY_CONTEXT (required explicit boundary context)
#
# Legacy convention (older commented call in main.nf) passed the species/genus files
# before the sequencing_time_file. We keep best-effort compatibility.
my $spc_metazoa = undef;
my $spc_viridiplantae = undef;
my $local_gns_metazoa = undef;
my $local_gns_viridiplantae = undef;
my $min_reads_sample = 0;
my $run_started_utc_file = undef;

$sequencing_time_file=$ARGV[4];
if(!defined $ARGV[5])
{
	# Minimal (current) signature: no display/interest filtering.
	$min_reads_sample = 0;
	$run_started_utc_file = $ARGV[10] if defined $ARGV[10];
}elsif($ARGV[5] =~ /^[0-9]+$/)
{
	$min_reads_sample = int($ARGV[5]);
	$spc_metazoa = $ARGV[6];
	$spc_viridiplantae = $ARGV[7];
	$local_gns_metazoa = $ARGV[8];
	$local_gns_viridiplantae = $ARGV[9];
	$run_started_utc_file = $ARGV[10] if defined $ARGV[10];
}else
{
	# Legacy order:
	#   4 metazoa_spc_basics
	#   5 viridiplantae_spc_basics
	#   6 local_metazoa_gns
	#   7 local_viridiplantae_gns
	#   8 sequencing_time_file
	$spc_metazoa = $ARGV[4];
	$spc_viridiplantae = $ARGV[5];
	$local_gns_metazoa = $ARGV[6];
	$local_gns_viridiplantae = $ARGV[7];
	$sequencing_time_file = $ARGV[8] if defined $ARGV[8];
	$run_started_utc_file = $ARGV[9] if defined $ARGV[9];
}

my $round_demult_report = "$barcode_pipeline\_demult_rpt.txt";
my $round_demult_sidecar = "$barcode_pipeline\_demult_rpt.contract.tsv";
my $round_otu_report = "$barcode_pipeline\_otu_def_rpt.txt";
my $round_otu_sidecar = "$barcode_pipeline\_otu_def_rpt.contract.tsv";
my $state_demult_report = "$temp_dir/$barcode_pipeline\_demult_rpt.txt";
my $state_demult_sidecar = "$temp_dir/$barcode_pipeline\_demult_rpt.contract.tsv";
my $state_otu_report = "$temp_dir/$barcode_pipeline\_otu_def_rpt.txt";
my $state_otu_sidecar = "$temp_dir/$barcode_pipeline\_otu_def_rpt.contract.tsv";
my $demult_bootstrap_sentinel = "$temp_dir/$barcode_pipeline\_demult_bootstrap.seeded";
my $boundary_context = $ENV{RTBIOSCAN_DEMUX_IDENTITY_CONTEXT} // '';
die "ERROR: missing RTBIOSCAN_DEMUX_IDENTITY_CONTEXT for append_reports.pl\n" if $boundary_context eq '';

ReportingParserStateTxn::recover_leftover_transaction_or_die(
	state_dir => $temp_dir,
	barcode => $barcode_pipeline,
	context => $boundary_context,
);
my $round_demult_sidecar_meta = ReportingContractSidecar::read_sidecar($round_demult_sidecar);
die "ERROR: current-round demux sidecar context mismatch in '$round_demult_sidecar'\n"
	if $round_demult_sidecar_meta->{context} ne $boundary_context;
my $round_demult = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'demult_rpt',
	context => $boundary_context,
	report_path => $round_demult_report,
	sidecar_path => $round_demult_sidecar,
);
my $round_otu = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'otu_def_rpt',
	context => $boundary_context,
	report_path => $round_otu_report,
	sidecar_path => $round_otu_sidecar,
);
my $state_demult = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'demult_rpt',
	context => $boundary_context,
	report_path => $state_demult_report,
	sidecar_path => $state_demult_sidecar,
	allow_absent => 1,
);
my $state_otu = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'otu_def_rpt',
	context => $boundary_context,
	report_path => $state_otu_report,
	sidecar_path => $state_otu_sidecar,
	allow_absent => 1,
);

my $state_demult_absent = $state_demult->{state} eq 'absent' ? 1 : 0;
my $state_otu_absent = $state_otu->{state} eq 'absent' ? 1 : 0;
my $sentinel_exists = -e $demult_bootstrap_sentinel ? 1 : 0;

die "ERROR: accumulated demux state exists without accumulated OTU-definition state\n"
	if !$state_demult_absent && $state_otu_absent;
die "ERROR: accumulated OTU-definition state exists without accumulated demux state\n"
	if $state_demult_absent && !$state_otu_absent;
die "ERROR: demux bootstrap sentinel exists without accumulated parser-state pairs\n"
	if $sentinel_exists && ($state_demult_absent || $state_otu_absent);
die "ERROR: accumulated parser-state pairs exist without demux bootstrap sentinel\n"
	if !$sentinel_exists && !$state_demult_absent && !$state_otu_absent;
die "ERROR: accumulated OTU-definition is nonempty while accumulated demux is empty\n"
	if !$state_demult_absent && !$state_otu_absent && $state_otu->{state} eq 'nonempty' && $state_demult->{state} eq 'empty';
die "ERROR: current-round demux is empty while OTU-definition is nonempty\n"
	if $round_demult->{state} eq 'empty' && $round_otu->{state} eq 'nonempty';

my %warned;
sub warn_once {
	my ($msg) = @_;
	return if !defined $msg || $msg eq '';
	return if $warned{$msg}++;
	warn "WARN: $msg\n";
}

sub trim_text {
	my ($v) = @_;
	$v = '' unless defined $v;
	$v =~ s/^\s+|\s+$//g;
	return $v;
}

sub parse_run_start_epoch {
	my ($path) = @_;
	return (undef, undef) unless defined $path && $path ne '' && -f $path;
	open my $fh, '<', $path or do {
		warn_once("run_started_utc.txt open failed: $path");
		return (undef, undef);
	};
	my $line = <$fh>;
	close $fh;
	return (undef, undef) unless defined $line;
	$line = trim_text($line);
	return (undef, undef) if $line eq '';
	if ($line !~ /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/) {
		warn_once("run_started_utc.txt invalid ISO-8601 UTC: $line");
		return (undef, undef);
	}
	my $epoch = undef;
	eval {
		my $tp = Time::Piece->strptime($line, "%Y-%m-%dT%H:%M:%SZ");
		$epoch = $tp->epoch;
	};
	if (!defined $epoch) {
		warn_once("run_started_utc.txt parse failed: $line");
		return (undef, undef);
	}
	return ($epoch, $line);
}

sub t0_rows_for {
	my ($kind, $run_id, $epoch, $iso) = @_;
	return () unless defined $run_id && defined $epoch;
	if ($kind eq 'reads_time') {
		return (
			"$run_id\t$epoch\trun_start\t0",
			"$run_id\t$epoch\tfast\t0",
			"$run_id\t$epoch\thac\t0",
			"$run_id\t$epoch\tsup\t0",
		);
	}
	if ($kind eq 'reads_cumulative') {
		return ("$run_id\t$epoch\t0\t0\t0\t0\t0\t0\t0\t0\t0");
	}
	if ($kind eq 'tax_time') {
		return (
			"$run_id\t$epoch\tspecies\t0",
			"$run_id\t$epoch\tgenus\t0",
			"$run_id\t$epoch\tfamily\t0",
			"$run_id\t$epoch\tspecies_COI_reads\t0",
			"$run_id\t$epoch\tspecies_ITS2_reads\t0",
			"$run_id\t$epoch\tgenus_COI_reads\t0",
			"$run_id\t$epoch\tgenus_ITS2_reads\t0",
			"$run_id\t$epoch\tfamily_COI_reads\t0",
			"$run_id\t$epoch\tfamily_ITS2_reads\t0",
		);
	}
	return ();
}

sub header_matches {
	my ($path, $expected) = @_;
	return 0 unless defined $path && $path ne '' && -f $path;
	open my $fh, '<', $path or return 0;
	my $hdr = <$fh>;
	close $fh;
	return 0 unless defined $hdr;
	chomp $hdr;
	$hdr =~ s/\r$//;
	return $hdr eq $expected ? 1 : 0;
}

sub ensure_t0_rows {
	my ($path, $header, $rows_ref) = @_;
	return unless defined $path && $path ne '' && -f $path;
	return unless header_matches($path, $header);
	return unless $rows_ref && @$rows_ref;
	my %missing = map { $_ => 1 } @$rows_ref;
	open my $fh, '<', $path or return;
	my $hdr = <$fh>;
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		delete $missing{$line} if exists $missing{$line};
		last if !%missing;
	}
	close $fh;
	return unless %missing;
	open my $out, '>>', $path or do {
		warn_once("t0 append failed: $path");
		return;
	};
	for my $row (@$rows_ref) {
		next unless exists $missing{$row};
		print $out $row, "\n";
	}
	close $out;
}

sub atomic_write_lines {
	my ($path, $lines_ref) = @_;
	my $tmp = "$path.tmp.$$";
	open my $out, '>', $tmp or die "I couldn't open $tmp\n";
	if ($lines_ref) {
		for my $line (@$lines_ref) {
			next unless defined $line;
			print $out $line, "\n";
		}
	}
	close $out;
	rename $tmp, $path or die "I couldn't replace $path\n";
}

sub load_seen_ids_sidecar {
	my ($path) = @_;
	my %seen;
	return \%seen unless defined $path && -f $path;
	open my $fh, '<', $path or die "I couldn't open $path\n";
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line =~ /^\s*$/;
		my ($rid) = split /\t/, $line, 2;
		$rid = trim_text($rid);
		next if $rid eq '';
		$seen{$rid} = 1;
	}
	close $fh;
	return \%seen;
}

sub write_seen_ids_sidecar {
	my ($path, $seen_ref) = @_;
	my @lines = sort grep { defined $_ && $_ ne '' } keys %{ $seen_ref // {} };
	atomic_write_lines($path, \@lines);
}

sub load_on_target_state_sidecar {
	my ($path) = @_;
	my (%state, %barcode);
	return (\%state, \%barcode) unless defined $path && -f $path;
	open my $fh, '<', $path or die "I couldn't open $path\n";
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line =~ /^\s*$/;
		my @tr = split /\t/, $line, -1;
		next if @tr < 3;
		my $rid = trim_text($tr[0]);
		next if $rid eq '';
		$state{$rid} = defined $tr[1] ? trim_text($tr[1]) : '';
		my $bc = defined $tr[2] ? trim_text($tr[2]) : '';
		$barcode{$rid} = $bc if $bc ne '';
	}
	close $fh;
	return (\%state, \%barcode);
}

sub write_on_target_state_sidecar {
	my ($path, $state_ref, $barcode_ref) = @_;
	my @lines;
	for my $rid (sort grep { defined $_ && $_ ne '' } keys %{ $state_ref // {} }) {
		my $status = defined $state_ref->{$rid} ? $state_ref->{$rid} : '';
		my $bc = defined $barcode_ref->{$rid} ? $barcode_ref->{$rid} : '';
		push @lines, join("\t", $rid, $status, $bc);
	}
	atomic_write_lines($path, \@lines);
}

sub is_repeated_header_row {
	my ($fields_ref, $header_ref, $required_indices_ref) = @_;
	return 0 unless $fields_ref && $header_ref && $required_indices_ref;
	for my $idx (@$required_indices_ref) {
		return 0 if !defined $idx;
		return 0 if $idx > $#$fields_ref || $idx > $#$header_ref;
		return 0 if !defined $fields_ref->[$idx] || !defined $header_ref->[$idx];
		return 0 if $fields_ref->[$idx] ne $header_ref->[$idx];
	}
	return 1;
}

sub replay_read_info_seen_ids {
	my ($path, $seen_ref) = @_;
	return unless defined $path && -f $path;
	open my $fh, '<', $path or die "I couldn't open $path\n";
	my @header = ();
	my $read_id_idx = undef;
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line =~ /^\s*$/;
		my @tr = split /\t/, $line, -1;
		if (!defined $read_id_idx) {
			my %idx = map { $tr[$_] => $_ } 0..$#tr;
			next unless exists $idx{'read_id'};
			@header = @tr;
			$read_id_idx = $idx{'read_id'};
			next;
		}
		next if is_repeated_header_row(\@tr, \@header, [$read_id_idx]);
		next if @tr < @header;
		next if $read_id_idx > $#tr;
		my $rid = trim_text($tr[$read_id_idx]);
		next if $rid eq '';
		$seen_ref->{$rid} = 1;
	}
	close $fh;
}

sub apply_on_target_state {
	my ($state_ref, $barcode_ref, $rid, $ont, $bc) = @_;
	return unless defined $rid;
	$rid = trim_text($rid);
	return if $rid eq '';
	$ont = trim_text($ont);
	$bc = trim_text($bc);
	if ($ont eq 'ON_TARGET') {
		$state_ref->{$rid} = 'ON_TARGET';
		$barcode_ref->{$rid} = $bc if $bc ne '';
	} elsif (!exists $state_ref->{$rid}) {
		$state_ref->{$rid} = $ont;
		$barcode_ref->{$rid} = $bc if $bc ne '';
	}
}

sub replay_on_target_state {
	my ($path, $state_ref, $barcode_ref) = @_;
	return unless defined $path && -f $path;
	open my $fh, '<', $path or die "I couldn't open $path\n";
	my @header = ();
	my ($read_id_idx, $barcode_idx, $ont_idx) = (undef, undef, undef);
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line =~ /^\s*$/;
		my @tr = split /\t/, $line, -1;
		if (!defined $read_id_idx) {
			my %idx = map { $tr[$_] => $_ } 0..$#tr;
			next unless exists $idx{'read_id'} && exists $idx{'barcode'} && exists $idx{'on_target_kingdom'};
			@header = @tr;
			$read_id_idx = $idx{'read_id'};
			$barcode_idx = $idx{'barcode'};
			$ont_idx = $idx{'on_target_kingdom'};
			next;
		}
		next if is_repeated_header_row(\@tr, \@header, [$read_id_idx, $barcode_idx, $ont_idx]);
		next if @tr < @header;
		next if $read_id_idx > $#tr || $barcode_idx > $#tr || $ont_idx > $#tr;
		apply_on_target_state($state_ref, $barcode_ref, $tr[$read_id_idx], $tr[$ont_idx], $tr[$barcode_idx]);
	}
	close $fh;
}

my ($run_start_epoc, $run_start_iso) = parse_run_start_epoch($run_started_utc_file);

# Optional pre-classification: species/genus of interest.
# If these files are provided and loadable, we filter taxa shown in treemap/time/read
# summaries to those "of interest". If they are missing, we keep current behavior.
my (%positive, %local_spec, %human_like_spec, %spec_interest, %local_gen);
my ($use_spec_interest, $use_local_gen) = (0, 0);

sub load_spec_basics {
	my ($path) = @_;
	return unless defined $path && $path ne '' && -f $path;
	open my $fh, '<', $path or return;
	my $hdr = <$fh>;
	while(<$fh>)
	{
		chomp;
		next unless $_;
		my @tr = split /\t/;
		next unless @tr >= 4;
		my $sp = $tr[0];
		my $pos = $tr[1];
		my $obs = $tr[2];
		my $hum = $tr[3];
		$positive{$sp} = 1 if defined $pos && $pos =~ /^[0-9]+$/ && $pos != 0;
		$local_spec{$sp} = 1 if defined $obs && $obs =~ /^[0-9]+$/ && $obs != 0;
		$human_like_spec{$sp} = 1 if defined $hum && $hum =~ /^[0-9]+$/ && $hum != 0;
		if(exists($positive{$sp}) || exists($local_spec{$sp}) || exists($human_like_spec{$sp}))
		{
			$spec_interest{$sp} = 1;
		}
	}
	close $fh;
}

sub load_genus_list {
	my ($path) = @_;
	return unless defined $path && $path ne '' && -f $path;
	open my $fh, '<', $path or return;
	while(<$fh>)
	{
		chomp;
		next unless $_;
		$local_gen{$_} = 1;
	}
	close $fh;
}


load_spec_basics($spc_metazoa);
load_spec_basics($spc_viridiplantae);
load_genus_list($local_gns_metazoa);
load_genus_list($local_gns_viridiplantae);

$use_spec_interest = (scalar keys %spec_interest) ? 1 : 0;
$use_local_gen = (scalar keys %local_gen) ? 1 : 0;

my $otu_genus_reads = 0;
my $consensus_genus_reads = 0;
my $otu_genus_reads_demux = 0;
my $otu_genus_reads_noadapter = 0;
my $consensus_genus_reads_demux = 0;
my $consensus_genus_reads_noadapter = 0;
my %otu_reads_rank_marker = ();
my %otu_reads_rank_marker_model = ();
my %cons_reads_rank_marker = ();
my ($fast_reads, $hac_reads, $sup_reads) = (0, 0, 0);
my $proc_reads = 0;

#open FILE, $spc_metazoa or die "I couldn't open $spc_metazoa\n";
#while(<FILE>)
#{
#	chomp;
#	@tr=split/\t/;
#	if($tr[1]){$positive{$tr[0]}=1;}
#	if($tr[2]){$local_spec{$tr[0]}=1;}
#	if($tr[3]){$human_like_spec{$tr[0]}=1;}
#}
#close FILE;

#open FILE, $spc_viridiplantae or die "I couldn't open $spc_viridiplantae\n";
#while(<FILE>)
#{
#	chomp;
#	@tr=split/\t/;
#	if($tr[1]){$positive{$tr[0]}=1;}
#	if($tr[2]){$local_spec{$tr[0]}=1;}
#	if($tr[2]){$human_like_spec{$tr[0]}=1;}
#}
#close FILE;

#open FILE, $local_gns_viridiplantae or die "I couldn't open $spc_metazoa\n";
#while(<FILE>)
#{
#	chomp;
#	$local_gen{$_}=1;
#}
#close FILE;

#open FILE, $local_gns_metazoa or die "I couldn't open $spc_metazoa\n";
#while(<FILE>)
#{
#	chomp;
#	$local_gen{$_}=1;
#}
#close FILE;



my @pod5_stat = stat($pod5_file);
if (@pod5_stat) {
	$file_date_secs = $pod5_stat[9];
	$file_date = strftime "%Y-%m-%dT%H:%M:%SZ", gmtime($file_date_secs);
} else {
	$file_date = "";
	$file_date_secs = "";
}
$pod5_file=~s/\.pod5//;

$datestring = strftime "%FT%TZ", gmtime;
$epoc = time();
	if(!-f "$temp_dir/$barcode_pipeline\_round_time_stamp_rpt.txt")
	{
		open TIME_STAMP, ">$temp_dir/$barcode_pipeline\_round_time_stamp_rpt.txt" or die "$temp_dir/$barcode_pipeline\_round_time_stamp_rpt.txt";
		print TIME_STAMP "file\tprocessing_time(YYY-MM-DDTHH:MM:SSZ)\tprocessing_time(epoc secs)\n";
	}else
	{
		open TIME_STAMP, ">>$temp_dir/$barcode_pipeline\_round_time_stamp_rpt.txt" or die "$temp_dir/$barcode_pipeline\_round_time_stamp_rpt.txt";
	}
print TIME_STAMP "$round_dir\t$datestring\t$epoc\n";
close TIME_STAMP;

if (-f "$temp_dir/$barcode_pipeline\_read_info_rpt.txt")
{
	# Avoid grep exit status=1 when the file only contains a header line.
	system "tail -n +2 $barcode_pipeline\\_read_info_rpt.txt >> $temp_dir/$barcode_pipeline\\_read_info_rpt.txt || true";
}else
{
	run_cmd("cp $barcode_pipeline\\_read_info_rpt.txt $temp_dir/$barcode_pipeline\\_read_info_rpt.txt");
}
if (-f "$temp_dir/$barcode_pipeline\_on_target_rpt.txt")
{
	# Avoid grep exit status=1 when the file only contains a header line.
	system "tail -n +2 $barcode_pipeline\\_on_target_rpt.txt >> $temp_dir/$barcode_pipeline\\_on_target_rpt.txt || true";
}else
{
	run_cmd("cp $barcode_pipeline\\_on_target_rpt.txt $temp_dir/$barcode_pipeline\\_on_target_rpt.txt");
}
$header_flag=1;
if (-f "$temp_dir/$barcode_pipeline\_on_target_rpt.txt")
{
	open FILE, "$temp_dir/$barcode_pipeline\_on_target_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_on_target_rpt.txt\n";
	my @rolling_on_target_header = ();
	while(<FILE>)
	{
		chomp;
		s/\r$//;
		next if /^\s*$/;
		@tr=split/\t/;

		if ($header_flag)
		{
			for($i=0;$i<@tr;$i++)
			{
				$header{$tr[$i]}=$i;
			}
			@rolling_on_target_header = @tr;
			$header_flag=0;
		}else
		{
			next if is_repeated_header_row(\@tr, \@rolling_on_target_header, [ $header{"read_id"}, $header{"barcode"}, $header{"on_target_kingdom"} ]);
			next if @tr < @rolling_on_target_header;
			my $rid = $tr[$header{"read_id"}];
			my $barcode = $tr[$header{"barcode"}];
			my $ont = $tr[$header{"on_target_kingdom"}];
			# Cumulative semantics: once a read is ON_TARGET, keep it ON_TARGET in rolling state.
			if (defined $ont && $ont eq "ON_TARGET") {
				$read_ontarget{$rid} = "ON_TARGET";
				$read_barcode{$rid} = $barcode if defined $barcode && $barcode ne '';
			} elsif (!exists $read_ontarget{$rid}) {
				$read_ontarget{$rid} = $ont;
				$read_barcode{$rid} = $barcode if defined $barcode && $barcode ne '';
			}
		}	
	}
	close FILE;
}

$header_flag=1;

open FILE_OUT, ">$temp_dir/$barcode_pipeline\_read_info_on_target_barcode_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_read_info_on_target_barcode_rpt.txt\n";
open FILE, "$temp_dir/$barcode_pipeline\_read_info_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_read_info_rpt.txt\n";
my @rolling_read_info_header = ();
while(<FILE>)
{
	chomp;
	$line = $_;
	$line =~ s/\r$//;
	next if $line =~ /^\s*$/;
	@tr=split(/\t/, $line, -1);

	if ($header_flag)
	{
		print FILE_OUT "$line\tbarcode\n";
		for($i=0;$i<@tr;$i++)
		{
			$header{$tr[$i]}=$i;
		}
		@rolling_read_info_header = @tr;
		$header_flag=0;
	}else
	{
		next if is_repeated_header_row(\@tr, \@rolling_read_info_header, [ $header{"read_id"} ]);
		next if @tr < @rolling_read_info_header;

		if(exists($read_barcode{$tr[$header{"read_id"}]}) && ($read_ontarget{$tr[$header{"read_id"}]} eq "ON_TARGET")){print FILE_OUT "$line\t".$read_barcode{$tr[$header{"read_id"}]}."\n";}
		
	}	
}
close FILE;
close FILE_OUT;

my $seen_ids_sidecar = "$temp_dir/$barcode_pipeline\_seen_read_ids.tsv";
my $on_target_state_sidecar = "$temp_dir/$barcode_pipeline\_on_target_state.tsv";
my $rolling_read_info = "$temp_dir/$barcode_pipeline\_read_info_rpt.txt";
my $rolling_on_target = "$temp_dir/$barcode_pipeline\_on_target_rpt.txt";
my $round_read_info = "$barcode_pipeline\_read_info_rpt.txt";
my $round_on_target = "$barcode_pipeline\_on_target_rpt.txt";
my $seen_ids_ref;
my ($on_target_state_ref, $on_target_barcode_ref);

if (!-e $seen_ids_sidecar || !-e $on_target_state_sidecar) {
	$seen_ids_ref = {};
	$on_target_state_ref = {};
	$on_target_barcode_ref = {};
	replay_read_info_seen_ids($rolling_read_info, $seen_ids_ref);
	replay_on_target_state($rolling_on_target, $on_target_state_ref, $on_target_barcode_ref);
} else {
	$seen_ids_ref = load_seen_ids_sidecar($seen_ids_sidecar);
	($on_target_state_ref, $on_target_barcode_ref) = load_on_target_state_sidecar($on_target_state_sidecar);
	replay_read_info_seen_ids($round_read_info, $seen_ids_ref);
	replay_on_target_state($round_on_target, $on_target_state_ref, $on_target_barcode_ref);
}

write_seen_ids_sidecar($seen_ids_sidecar, $seen_ids_ref);
write_on_target_state_sidecar($on_target_state_sidecar, $on_target_state_ref, $on_target_barcode_ref);

# Count per-round reads (prefer current round file over rolling temp)
if (-f $round_read_info) {
	($fast_reads, $hac_reads, $sup_reads) = count_reads_by_qscore($round_read_info);
} else {
	($fast_reads, $hac_reads, $sup_reads) = count_reads_by_qscore("$temp_dir/$barcode_pipeline\_read_info_rpt.txt");
}

$proc_reads = $sup_reads;
	$proc_reads = $hac_reads if !$proc_reads;
	$proc_reads = $fast_reads if !$proc_reads;

	my $reads_time_rpt = "$temp_dir/$barcode_pipeline\_reads_time_rpt.txt";
	my $reads_time_hdr = "run_id\ttime\tdata\treads";
	my %reads_time_seen = ();
	my @reads_time_out = ();

	sub _reads_time_ingest {
		my ($path) = @_;
		return unless defined $path && $path ne '' && -f $path;
		open my $fh, '<', $path or die "I couldn't open $path\n";
		while (my $l = <$fh>) {
			chomp $l;
			next unless $l ne '';
			next if $l =~ /^run_id\t/;  # header
			next if exists $reads_time_seen{$l};
			$reads_time_seen{$l} = 1;
			push @reads_time_out, $l;
		}
		close $fh;
	}

	# Merge existing report lines + persistent ledger lines (if different file), then append this round.
	_reads_time_ingest($reads_time_rpt);
	if (defined $sequencing_time_file && $sequencing_time_file ne '' && -f $sequencing_time_file && $sequencing_time_file ne $reads_time_rpt) {
		_reads_time_ingest($sequencing_time_file);
	}

	for my $l (
		"$barcode_pipeline\t$epoc\tprocessed\t$fast_reads",
		"$barcode_pipeline\t$epoc\tfast\t$fast_reads",
		"$barcode_pipeline\t$epoc\thac\t$hac_reads",
		"$barcode_pipeline\t$epoc\tsup\t$sup_reads",
	) {
		next if exists $reads_time_seen{$l};
		$reads_time_seen{$l} = 1;
		push @reads_time_out, $l;
	}

	open my $rt_out, '>', "$reads_time_rpt.tmp" or die "I couldn't open $reads_time_rpt.tmp\n";
	print $rt_out $reads_time_hdr, "\n";
	print $rt_out join("\n", @reads_time_out), "\n" if @reads_time_out;
	close $rt_out;
	rename "$reads_time_rpt.tmp", $reads_time_rpt or die "I couldn't replace $reads_time_rpt\n";

my $skip_parser_state_publish =
	$state_demult_absent &&
	$state_otu_absent &&
	!$sentinel_exists &&
	$round_demult->{state} eq 'empty' &&
	$round_otu->{state} eq 'empty';

if (!$skip_parser_state_publish) {
	my $tx_paths = ReportingParserStateTxn::canonical_paths(
		state_dir => $temp_dir,
		barcode => $barcode_pipeline,
	);
	my $bootstrap_in_transaction = ($state_demult_absent && $state_otu_absent) ? 1 : 0;
	ReportingParserStateTxn::prepare_transaction_workspace(
		paths => $tx_paths,
		bootstrap_sentinel_in_transaction => $bootstrap_in_transaction,
	);

	build_accumulated_report_tmp(
		source_report => $round_demult_report,
		state_report => $state_demult_report,
		tmp_report => $tx_paths->{demult_report_tmp},
	);
	ReportingContractSidecar::write_sidecar_from_report(
		report_kind => 'demult_rpt',
		context => $boundary_context,
		report_path => $tx_paths->{demult_report_tmp},
		sidecar_path => $tx_paths->{demult_sidecar_tmp},
	);

	build_accumulated_report_tmp(
		source_report => $round_otu_report,
		state_report => $state_otu_report,
		tmp_report => $tx_paths->{otu_report_tmp},
	);
	ReportingContractSidecar::write_sidecar_from_report(
		report_kind => 'otu_def_rpt',
		context => $boundary_context,
		report_path => $tx_paths->{otu_report_tmp},
		sidecar_path => $tx_paths->{otu_sidecar_tmp},
	);

	ReportingParserStateTxn::publish_boundary_commit_or_die(
		state_dir => $temp_dir,
		barcode => $barcode_pipeline,
		context => $boundary_context,
		bootstrap_sentinel_in_transaction => $bootstrap_in_transaction,
	);
}

#if (-f "$temp_dir/$barcode_pipeline\_demult_qc_rpt.txt")
#{
#	system "grep -v read_id $barcode_pipeline\_demult_qc_rpt.txt >> $temp_dir/$barcode_pipeline\_demult_qc_rpt.txt";
#}else
#{
#	system "cp $barcode_pipeline\_demult_qc_rpt.txt $temp_dir/$barcode_pipeline\_demult_qc_rpt.txt";
#}


#system "cp $barcode_pipeline\_otu_def_rpt.txt $temp_dir/$barcode_pipeline\_otu_def_rpt.txt";
#while( !-r "$barcode_pipeline\_blast_otu_pretax_rpt.txt")
#{
#	sleep 2;
#}
run_cmd("cp $barcode_pipeline\\_blast_otu_pretax_rpt.txt $temp_dir/$barcode_pipeline\\_blast_otu_pretax_rpt.txt", fatal => 1);

run_cmd("cp $barcode_pipeline\\_blast_consensus_tax_rpt.txt $temp_dir/$barcode_pipeline\\_blast_consensus_tax_rpt.txt", fatal => 1);


#####	PLOTS BASED ON OTu TAXONOMICAL PREASSIGNMENTS

$header_flag=1;
open FILE, "$temp_dir/$barcode_pipeline\_blast_otu_pretax_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_blast_otu_pretax_rpt.txt\n";
while(<FILE>)
{
	chomp;
	@tr=split/\t/;

	if ($header_flag)
	{
		for($i=0;$i<@tr;$i++)
		{
			$header{$tr[$i]}=$i;
		}
		$header_flag=0;
		}else
		{
			$sample=$tr[$header{"sample"}];
			my $is_no_adapter = ($sample =~ /^no_adapter/i) ? 1 : 0;
			# Robust parsing: allow arbitrary sample names and avoid stale $1/$2 when regex doesn't match.
			($sample_joined, $replicate) = ($sample, 1);
			if ($sample =~ /^(.*)_(\d+)$/) {
				($sample_joined, $replicate) = ($1, $2);
			}
			$samples_joined{$sample_joined}=1;
			$otu_id=$tr[$header{"otu_id"}];
			my $marker = "";
			if (exists($header{"barcode_by_homology"})) {
				$marker = $tr[$header{"barcode_by_homology"}] // "";
			}
			$marker = "COI"  if $marker =~ /^COI/i;
			$marker = "ITS2" if $marker =~ /^ITS/i;
			my $bc_model = "";
			if (exists($header{"basecalling_model"})) {
				$bc_model = lc($tr[$header{"basecalling_model"}] // "");
			}
			# Normalize to the expected categories; anything else is ignored for the model-specific plot.
			$bc_model = "fast" if $bc_model =~ /fast/;
			$bc_model = "hac"  if $bc_model =~ /hac/;
			$bc_model = "sup"  if $bc_model =~ /sup/;
			$bc_model = "" if $bc_model !~ /^(fast|hac|sup)$/;
		
		if($tr[$header{"otu_kingdom"}] eq "Metazoa")
		{
			if($tr[$header{"otu_class"}])
			{
				$class{$tr[$header{"otu_class"}]}++;
#				if($tr[$header{"otu_species"}] && exists($local_spec{$tr[$header{"otu_species"}]}))
				if($tr[$header{"otu_species"}])
				{
					$species{$tr[$header{"otu_species"}]}++;
					if($marker ne "")
					{
						$otu_reads_rank_marker{"species"}{$marker}++;
						$otu_reads_rank_marker_model{"species"}{$marker}{$bc_model}++ if $bc_model ne "";
					}
					$species_otu_sample{$tr[$header{"otu_species"}]}{$otu_id}{$sample_joined}++;
					if($species_otu_sample{$tr[$header{"otu_species"}]}{$otu_id}{$sample_joined} == 5){$species_otu_sample5{$tr[$header{"otu_species"}]}=1;}
						if($species_otu_sample{$tr[$header{"otu_species"}]}{$otu_id}{$sample_joined}>9)
						{
						$detected_species{$sample_joined}{$tr[$header{"otu_species"}]}=1;
					}
					$species_per_sample{$sample_joined}{$tr[$header{"otu_species"}]}++;
					$class_species_metazoa{$tr[$header{"otu_class"}]}{$tr[$header{"otu_species"}]}=1;
				}
				if($tr[$header{"otu_genus"}])
#				if($tr[$header{"otu_genus"}] && exists($local_gen{$tr[$header{"otu_genus"}]}))
				{
					$genus{$tr[$header{"otu_genus"}]}++;
					if($marker ne "")
					{
						$otu_reads_rank_marker{"genus"}{$marker}++;
						$otu_reads_rank_marker_model{"genus"}{$marker}{$bc_model}++ if $bc_model ne "";
					}
					$genus_otu_sample{$tr[$header{"otu_genus"}]}{$otu_id}{$sample_joined}++;
					if($is_no_adapter){ $otu_genus_reads_noadapter++; } else { $otu_genus_reads_demux++; }
					if($genus_otu_sample{$tr[$header{"otu_genus"}]}{$otu_id}{$sample_joined} == 5){$genus_otu_sample5{$tr[$header{"otu_genus"}]}=1;}
					if($genus_otu_sample{$tr[$header{"otu_genus"}]}{$otu_id}{$sample_joined}>9)
					{
						$detected_genus{$sample_joined}{$tr[$header{"otu_genus"}]}=1;
					}
					$genus_per_sample{$sample_joined}{$tr[$header{"otu_genus"}]}++;
					$class_genus_metazoa{$tr[$header{"otu_class"}]}{$tr[$header{"otu_genus"}]}=1;
				}
				if($tr[$header{"otu_family"}])
				{
					$family{$tr[$header{"otu_family"}]}++;
					if($marker ne "")
					{
						$otu_reads_rank_marker{"family"}{$marker}++;
						$otu_reads_rank_marker_model{"family"}{$marker}{$bc_model}++ if $bc_model ne "";
					}
					$family_otu_sample{$tr[$header{"otu_family"}]}{$otu_id}{$sample_joined}++;
					if($family_otu_sample{$tr[$header{"otu_family"}]}{$otu_id}{$sample_joined} == 5){$family_otu_sample5{$tr[$header{"otu_family"}]}=1;}
					if($family_otu_sample{$tr[$header{"otu_family"}]}{$otu_id}{$sample_joined}>9)
					{
						$detected_family{$sample_joined}{$tr[$header{"otu_family"}]}=1;
					}
					$family_per_sample{$sample_joined}{$tr[$header{"otu_family"}]}++;
					$class_family_metazoa{$tr[$header{"otu_class"}]}{$tr[$header{"otu_family"}]}=1;
				}
			
			}
		}elsif($tr[$header{"otu_kingdom"}] eq "Viridiplantae")
		{
			if($tr[$header{"otu_class"}])
			{
				$class{$tr[$header{"otu_class"}]}++;
#				if($tr[$header{"otu_species"}] && exists($local_spec{$tr[$header{"otu_species"}]}))
				if($tr[$header{"otu_species"}])
				{
					$species{$tr[$header{"otu_species"}]}++;
					if($marker ne "")
					{
						$otu_reads_rank_marker{"species"}{$marker}++;
						$otu_reads_rank_marker_model{"species"}{$marker}{$bc_model}++ if $bc_model ne "";
					}
					$species_otu_sample{$tr[$header{"otu_species"}]}{$otu_id}{$sample_joined}++;
					if($species_otu_sample{$tr[$header{"otu_species"}]}{$otu_id}{$sample_joined} == 5){$species_otu_sample5{$tr[$header{"otu_species"}]}=1;}
					if($species_otu_sample{$tr[$header{"otu_species"}]}{$otu_id}{$sample_joined}>9)
					{
						$detected_species{$sample_joined}{$tr[$header{"otu_species"}]}=1;
					}
					$species_per_sample{$sample_joined}{$tr[$header{"otu_species"}]}++;
					$class_species_viridiplantae{$tr[$header{"otu_class"}]}{$tr[$header{"otu_species"}]}=1;
				}
#				if($tr[$header{"otu_genus"}] && exists($local_gen{$tr[$header{"otu_genus"}]}))
					if($tr[$header{"otu_genus"}])
					{
						$genus{$tr[$header{"otu_genus"}]}++;
						if($marker ne "")
						{
							$otu_reads_rank_marker{"genus"}{$marker}++;
							$otu_reads_rank_marker_model{"genus"}{$marker}{$bc_model}++ if $bc_model ne "";
						}
						$genus_otu_sample{$tr[$header{"otu_genus"}]}{$otu_id}{$sample_joined}++;
						if($is_no_adapter){ $otu_genus_reads_noadapter++; } else { $otu_genus_reads_demux++; }
						if($genus_otu_sample{$tr[$header{"otu_genus"}]}{$otu_id}{$sample_joined} == 5){$genus_otu_sample5{$tr[$header{"otu_genus"}]}=1;}
						if($genus_otu_sample{$tr[$header{"otu_genus"}]}{$otu_id}{$sample_joined}>9)
					{
						$detected_genus{$sample_joined}{$tr[$header{"otu_genus"}]}=1;
					}
					$genus_per_sample{$sample_joined}{$tr[$header{"otu_genus"}]}++;
					$class_genus_viridiplantae{$tr[$header{"otu_class"}]}{$tr[$header{"otu_genus"}]}=1;
				}
					if($tr[$header{"otu_family"}])
					{
						$family{$tr[$header{"otu_family"}]}++;
						if($marker ne "")
						{
							$otu_reads_rank_marker{"family"}{$marker}++;
							$otu_reads_rank_marker_model{"family"}{$marker}{$bc_model}++ if $bc_model ne "";
						}
						$family_otu_sample{$tr[$header{"otu_family"}]}{$otu_id}{$sample_joined}++;
						if($family_otu_sample{$tr[$header{"otu_family"}]}{$otu_id}{$sample_joined} == 5){$family_otu_sample5{$tr[$header{"otu_family"}]}=1;}
						if($family_otu_sample{$tr[$header{"otu_family"}]}{$otu_id}{$sample_joined}>9)
						{
						$detected_family{$sample_joined}{$tr[$header{"otu_family"}]}=1;
					}
					$family_per_sample{$sample_joined}{$tr[$header{"otu_family"}]}++;
					$class_family_viridiplantae{$tr[$header{"otu_class"}]}{$tr[$header{"otu_family"}]}=1;
				}
			}
		}
	}	
}
close FILE;

@species_uniq=sort keys(%species);
@genus_uniq=sort keys(%genus);
@family_uniq=sort keys(%family);

# Filter taxa for tables/plots:
# - min_reads_sample controls which taxa are shown (based on total reads supporting the taxon).
# - when pre-classification files are provided, restrict taxa to those deemed "of interest".
if($min_reads_sample && $min_reads_sample > 0)
{
	my $otu_support_min_reads = ($min_reads_sample > 5) ? $min_reads_sample : 5;
	@species_uniq = grep { defined $species{$_} && $species{$_} >= $otu_support_min_reads } @species_uniq;
	@genus_uniq   = grep { defined $genus{$_}   && $genus{$_}   >= $otu_support_min_reads } @genus_uniq;
	@family_uniq  = grep { defined $family{$_}  && $family{$_}  >= $otu_support_min_reads } @family_uniq;
}else
{
	my $otu_support_min_reads = 5;
	@species_uniq = grep { defined $species{$_} && $species{$_} >= $otu_support_min_reads } @species_uniq;
	@genus_uniq   = grep { defined $genus{$_}   && $genus{$_}   >= $otu_support_min_reads } @genus_uniq;
	@family_uniq  = grep { defined $family{$_}  && $family{$_}  >= $otu_support_min_reads } @family_uniq;
}
if($use_spec_interest)
{
	@species_uniq = grep { exists($spec_interest{$_}) } @species_uniq;
}
if($use_local_gen)
{
	@genus_uniq = grep { exists($local_gen{$_}) } @genus_uniq;
}
@class_metazoa=sort keys(%class_species_metazoa);
@class_viridiplantae=sort keys(%class_species_viridiplantae);
@a_samples_joined=sort keys(%samples_joined);

$otu_genus_reads = $otu_genus_reads_demux + $otu_genus_reads_noadapter;

@species_otu_sample5_uniq=sort keys(%species_otu_sample5);
@genus_otu_sample5_uniq=sort keys(%genus_otu_sample5);
@family_otu_sample5_uniq=sort keys(%family_otu_sample5);


#####	ABUNDANCE HEATMAPS

#if(!-f "$temp_dir/$barcode_pipeline\_otu_tax_detect_abundance_rpt.txt")
#{
#	open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_detect_abundance_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_detect_abundance_rpt.txt\n";
#	print FILE "sample\tfamily\tgenus\tspecies\n";
#}else
#{
#	open FILE, ">>$temp_dir/$barcode_pipeline\_otu_tax_detect_abundance_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_detect_abundance_rpt.txt\n";
#}
#print FILE "$round_dir\t$datestring\t$epoc\t".@family_uniq."\t".@genus_uniq."\t".@species_uniq."\n";
#close FILE;





if(!-f "$temp_dir/$barcode_pipeline\_otu_tax_time_rpt.txt")
{
	open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_time_rpt.txt\n";
	print FILE "run_id\ttime\ttaxon\tidentifications\n";
}else
{
	open FILE, ">>$temp_dir/$barcode_pipeline\_otu_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_time_rpt.txt\n";
	
}

	print FILE "$barcode_pipeline\t$epoc\tspecies\t".@species_uniq."\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus\t".@genus_uniq."\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily\t".@family_uniq."\n";
	my $otu_sp_coi  = $otu_reads_rank_marker{"species"}{"COI"}  // 0;
	my $otu_sp_its2 = $otu_reads_rank_marker{"species"}{"ITS2"} // 0;
	my $otu_gn_coi  = $otu_reads_rank_marker{"genus"}{"COI"}    // 0;
	my $otu_gn_its2 = $otu_reads_rank_marker{"genus"}{"ITS2"}   // 0;
	my $otu_fm_coi  = $otu_reads_rank_marker{"family"}{"COI"}   // 0;
	my $otu_fm_its2 = $otu_reads_rank_marker{"family"}{"ITS2"}  // 0;
	print FILE "$barcode_pipeline\t$epoc\tspecies_COI_reads\t$otu_sp_coi\n";
	print FILE "$barcode_pipeline\t$epoc\tspecies_ITS2_reads\t$otu_sp_its2\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus_COI_reads\t$otu_gn_coi\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus_ITS2_reads\t$otu_gn_its2\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily_COI_reads\t$otu_fm_coi\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily_ITS2_reads\t$otu_fm_its2\n";
	# Optional: basecalling-model breakdown for marker read support (fast/hac/sup).
	for my $rk (qw(species genus family))
	{
		for my $mk (qw(COI ITS2))
		{
			for my $bm (qw(fast hac sup))
			{
				my $n = $otu_reads_rank_marker_model{$rk}{$mk}{$bm} // 0;
				print FILE "$barcode_pipeline\t$epoc\t${rk}_${mk}_${bm}_reads\t$n\n";
			}
		}
	}
	close FILE;

if(!-f "$temp_dir/$barcode_pipeline\_otu_tax_reads_rpt.txt")
{
	open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_reads_rpt.txt\n";
	print FILE "run_id\treads\ttaxon\tidentifications\n";
}else
{
	open FILE, ">>$temp_dir/$barcode_pipeline\_otu_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_reads_rpt.txt\n";
	
}

print FILE "$barcode_pipeline\t$proc_reads\tspecies\t".@species_uniq."\n";
print FILE "$barcode_pipeline\t$proc_reads\tgenus\t".@genus_uniq."\n";
print FILE "$barcode_pipeline\t$proc_reads\tfamily\t".@family_uniq."\n";
close FILE;



if(!-f "$temp_dir/$barcode_pipeline\_otu_sample5_tax_time_rpt.txt")
{
	open FILE, ">$temp_dir/$barcode_pipeline\_otu_sample5_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_sample5_tax_time_rpt.txt\n";
	print FILE "run_id\ttime\ttaxon\tidentifications\n";
}else
{
	open FILE, ">>$temp_dir/$barcode_pipeline\_otu_sample5_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_sample5_tax_time_rpt.txt\n";
	
}
print FILE "$barcode_pipeline\t$epoc\tspecies\t".@species_otu_sample5_uniq."\n";
print FILE "$barcode_pipeline\t$epoc\tgenus\t".@genus_otu_sample5_uniq."\n";
print FILE "$barcode_pipeline\t$epoc\tfamily\t".@family_otu_sample5_uniq."\n";
close FILE;



#####	TREEMAPS

open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_gns_metazoa_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_gns_metazoa_treemap_rpt.txt\n";
print FILE "otu_class\totu_genus\t#reads\n";
	foreach $each_class(sort keys(%class_genus_metazoa))
	{
		foreach $each_genus(sort keys(%{$class_genus_metazoa{$each_class}}))
		{
			next if is_unassigned_taxon($each_genus);
			my $n = $genus{$each_genus} // 0;
			next if ($use_local_gen && !exists($local_gen{$each_genus}));
			print FILE "$each_class\t$each_genus\t$n\n";
		}
	}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_gns_viridiplantae_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_gns_viridiplantae_treemap_rpt.txt\n";
print FILE "otu_class\totu_genus\t#reads\n";
	foreach $each_class(sort keys(%class_genus_viridiplantae))
	{
		foreach $each_genus(sort keys(%{$class_genus_viridiplantae{$each_class}}))
		{
			next if is_unassigned_taxon($each_genus);
			my $n = $genus{$each_genus} // 0;
			next if ($use_local_gen && !exists($local_gen{$each_genus}));
			print FILE "$each_class\t$each_genus\t$n\n";
		}
	}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_spc_metazoa_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_spc_metazoa_treemap_rpt.txt\n";
print FILE "otu_class\totu_species\t#reads\n";
	foreach $each_class(sort keys(%class_species_metazoa))
	{
		foreach $each_species(sort keys(%{$class_species_metazoa{$each_class}}))
		{
			next if is_unassigned_taxon($each_species);
			my $n = $species{$each_species} // 0;
			print FILE "$each_class\t$each_species\t$n\n";
		}
	}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_otu_tax_spc_viridiplantae_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_otu_tax_spc_viridiplantae_treemap_rpt.txt\n";
print FILE "otu_class\totu_species\t#reads\n";
	foreach $each_class(sort keys(%class_species_viridiplantae))
	{
		foreach $each_species(sort keys(%{$class_species_viridiplantae{$each_class}}))
		{
			next if is_unassigned_taxon($each_species);
			my $n = $species{$each_species} // 0;
			print FILE "$each_class\t$each_species\t$n\n";
		}
	}
close FILE;

my $consolidated_has_rows = 0;
#####	CONSOLIDATED CONSENSUS REPORTS / PLOTS (high-quality cached consensus)
if (-f "$temp_dir/$barcode_pipeline\_blast_consensus_tax_consolidated_rpt.txt") {
	$header_flag=1;
	undef %header;
	undef %consensus_id;
	undef %samples_joined;
	undef %class;
	undef %species;
	undef %species_consensus_sample;
	undef %detected_species;
	undef %species_per_sample;
	undef %class_species_metazoa;
	undef %class_species_viridiplantae;
	undef %species_clusters;
	undef %class_species_clusters_metazoa;
	undef %class_species_clusters_viridiplantae;
	undef %genus;
	undef %genus_consensus_sample;
	undef %detected_genus;
	undef %genus_per_sample;
	undef %class_genus_metazoa;
	undef %class_genus_viridiplantae;
	undef %genus_clusters;
	undef %class_genus_clusters_metazoa;
	undef %class_genus_clusters_viridiplantae;
	undef %family;
	undef %family_consensus_sample;
	undef %detected_family;
	undef %family_per_sample;
	undef %class_family_metazoa;
	undef %class_family_viridiplantae;
	undef %class_reads_metazoa;
	undef %class_reads_viridiplantae;
	undef %class_clusters_metazoa;
	undef %class_clusters_viridiplantae;
	undef %cons_reads_rank_marker;
	$consensus_genus_reads_demux = 0;
	$consensus_genus_reads_noadapter = 0;

	open FILE, "$temp_dir/$barcode_pipeline\_blast_consensus_tax_consolidated_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_blast_consensus_tax_consolidated_rpt.txt\n";
	while(<FILE>)
	{
		my $line = $_;
		chomp $line;
		@tr=split/\t/,$line;

		if ($header_flag)
		{
			for($i=0;$i<@tr;$i++)
			{
				$header{$tr[$i]}=$i;
			}
			$header_flag=0;
		}else
		{
			$consolidated_has_rows = 1;
			$sample=$tr[$header{"sample"}];
			my $is_no_adapter = ($sample =~ /^no_adapter/i) ? 1 : 0;
			($sample_joined, $replicate) = ($sample, 1);
			if ($sample =~ /^(.*)_(\d+)$/) {
				($sample_joined, $replicate) = ($1, $2);
			}

			my $nreads = 1;
			if (exists($header{"number_of_reads"})) {
				my $v = $tr[$header{"number_of_reads"}];
				$v =~ s/\r$// if defined $v;
				if (defined $v && $v =~ /^[0-9]+$/) {
					$nreads = int($v);
				}
			}
			
			$samples_joined{$sample_joined}=1;
			$consensus_id=$tr[$header{"consensus_id"}];
			my $marker = "";
			if (exists($header{"barcode_by_homology"})) {
				$marker = $tr[$header{"barcode_by_homology"}] // "";
			}
			$marker = "COI"  if $marker =~ /^COI/i;
			$marker = "ITS2" if $marker =~ /^ITS/i;

			if($tr[$header{"consensus_kingdom"}] eq "Metazoa")
			{
				if($tr[$header{"consensus_class"}])
				{
					$class{$tr[$header{"consensus_class"}]} += $nreads;
					$class_reads_metazoa{$tr[$header{"consensus_class"}]} += $nreads;
					$class_clusters_metazoa{$tr[$header{"consensus_class"}]}++;
					if($tr[$header{"consensus_species"}] && !is_unassigned_taxon($tr[$header{"consensus_species"}]))
					{
						$species{$tr[$header{"consensus_species"}]} += $nreads;
						$cons_reads_rank_marker{"species"}{$marker} += $nreads if $marker ne "";
						$species_clusters{$tr[$header{"consensus_species"}]}++;
						$class_species_clusters_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}++;
						$species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined} += $nreads;
						$species_per_sample{$sample_joined}{$tr[$header{"consensus_species"}]} += $nreads;
						$class_species_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}=1;
					}
					if($tr[$header{"consensus_genus"}] && !is_unassigned_taxon($tr[$header{"consensus_genus"}]))
					{
						$genus{$tr[$header{"consensus_genus"}]} += $nreads;
						$cons_reads_rank_marker{"genus"}{$marker} += $nreads if $marker ne "";
						$genus_clusters{$tr[$header{"consensus_genus"}]}++;
						$class_genus_clusters_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}++;
						$genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined} += $nreads;
						if($is_no_adapter){ $consensus_genus_reads_noadapter += $nreads; } else { $consensus_genus_reads_demux += $nreads; }
						$genus_per_sample{$sample_joined}{$tr[$header{"consensus_genus"}]} += $nreads;
						$class_genus_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}=1;
					}
					if($tr[$header{"consensus_family"}] && !is_unassigned_taxon($tr[$header{"consensus_family"}]))
					{
						$family{$tr[$header{"consensus_family"}]} += $nreads;
						$cons_reads_rank_marker{"family"}{$marker} += $nreads if $marker ne "";
						$family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined} += $nreads;
						$family_per_sample{$sample_joined}{$tr[$header{"consensus_family"}]} += $nreads;
						$class_family_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_family"}]}=1;
					}
				}
			}elsif($tr[$header{"consensus_kingdom"}] eq "Viridiplantae")
			{
				if($tr[$header{"consensus_class"}])
				{
					$class{$tr[$header{"consensus_class"}]} += $nreads;
					$class_reads_viridiplantae{$tr[$header{"consensus_class"}]} += $nreads;
					$class_clusters_viridiplantae{$tr[$header{"consensus_class"}]}++;
					if($tr[$header{"consensus_species"}] && !is_unassigned_taxon($tr[$header{"consensus_species"}]))
					{
						$species{$tr[$header{"consensus_species"}]} += $nreads;
						$cons_reads_rank_marker{"species"}{$marker} += $nreads if $marker ne "";
						$species_clusters{$tr[$header{"consensus_species"}]}++;
						$class_species_clusters_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}++;
						$species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined} += $nreads;
						$species_per_sample{$sample_joined}{$tr[$header{"consensus_species"}]} += $nreads;
						$class_species_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}=1;
					}
					if($tr[$header{"consensus_genus"}] && !is_unassigned_taxon($tr[$header{"consensus_genus"}]))
					{
						$genus{$tr[$header{"consensus_genus"}]} += $nreads;
						$cons_reads_rank_marker{"genus"}{$marker} += $nreads if $marker ne "";
						$genus_clusters{$tr[$header{"consensus_genus"}]}++;
						$class_genus_clusters_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}++;
						$genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined} += $nreads;
						if($is_no_adapter){ $consensus_genus_reads_noadapter += $nreads; } else { $consensus_genus_reads_demux += $nreads; }
						$genus_per_sample{$sample_joined}{$tr[$header{"consensus_genus"}]} += $nreads;
						$class_genus_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}=1;
					}
					if($tr[$header{"consensus_family"}] && !is_unassigned_taxon($tr[$header{"consensus_family"}]))
					{
						$family{$tr[$header{"consensus_family"}]} += $nreads;
						$cons_reads_rank_marker{"family"}{$marker} += $nreads if $marker ne "";
						$family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined} += $nreads;
						$family_per_sample{$sample_joined}{$tr[$header{"consensus_family"}]} += $nreads;
						$class_family_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_family"}]}=1;
					}
				}
			}
		}
	}
	close FILE;
}
if (-f "$temp_dir/$barcode_pipeline\_blast_consensus_tax_consolidated_rpt.txt" && !$consolidated_has_rows) {
	# Preserve consolidated ID state files and time/reads history; only clear per-round derived visuals.
	for my $f (glob("$temp_dir/${barcode_pipeline}_consensus_consolidated_*_treemap_rpt.txt")) {
		unlink $f;
	}
	for my $f (glob("$temp_dir/${barcode_pipeline}_consensus_consolidated_*_clusters_rpt.txt")) {
		unlink $f;
	}
	for my $f (glob("$temp_dir/${barcode_pipeline}_consensus_consolidated_*.png")) {
		unlink $f;
	}
}

#####	CONSOLIDATED CONSENSUS DERIVED REPORTS / PLOTS (separate from standard consensus)
if (-f "$temp_dir/$barcode_pipeline\_blast_consensus_tax_consolidated_rpt.txt" && $consolidated_has_rows) {
	undef @species_uniq;
	undef @genus_uniq;
	undef @family_uniq;
	undef @class_metazoa;
	undef @class_viridiplantae;
	undef @a_samples_joined;

	@species_uniq=sort keys(%species);
	@genus_uniq=sort keys(%genus);
	@family_uniq=sort keys(%family);

	# Apply the same "show taxon" filters to consolidated consensus-derived summaries.
	if($min_reads_sample && $min_reads_sample > 0)
	{
		@species_uniq = grep { defined $species{$_} && $species{$_} >= $min_reads_sample } @species_uniq;
		@genus_uniq   = grep { defined $genus{$_}   && $genus{$_}   >= $min_reads_sample } @genus_uniq;
		@family_uniq  = grep { defined $family{$_}  && $family{$_}  >= $min_reads_sample } @family_uniq;
	}
	if($use_spec_interest)
	{
		@species_uniq = grep { exists($spec_interest{$_}) } @species_uniq;
	}
	if($use_local_gen)
	{
		@genus_uniq = grep { exists($local_gen{$_}) } @genus_uniq;
	}
	@class_metazoa=sort keys(%class_species_metazoa);
	@class_viridiplantae=sort keys(%class_species_viridiplantae);
	@a_samples_joined=sort keys(%samples_joined);

	my $consensus_consolidated_genus_reads = $consensus_genus_reads_demux + $consensus_genus_reads_noadapter;

	if(!-f "$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt")
	{
		open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt\n";
		print FILE "run_id\ttime\ttaxon\tidentifications\n";
	}else
	{
		open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt\n";
	}
		print FILE "$barcode_pipeline\t$epoc\tspecies\t".@species_uniq."\n";
		print FILE "$barcode_pipeline\t$epoc\tgenus\t".@genus_uniq."\n";
		print FILE "$barcode_pipeline\t$epoc\tfamily\t".@family_uniq."\n";
		my $cs_sp_coi  = $cons_reads_rank_marker{"species"}{"COI"}  // 0;
		my $cs_sp_its2 = $cons_reads_rank_marker{"species"}{"ITS2"} // 0;
		my $cs_gn_coi  = $cons_reads_rank_marker{"genus"}{"COI"}    // 0;
		my $cs_gn_its2 = $cons_reads_rank_marker{"genus"}{"ITS2"}   // 0;
		my $cs_fm_coi  = $cons_reads_rank_marker{"family"}{"COI"}   // 0;
		my $cs_fm_its2 = $cons_reads_rank_marker{"family"}{"ITS2"}  // 0;
		print FILE "$barcode_pipeline\t$epoc\tspecies_COI_reads\t$cs_sp_coi\n";
		print FILE "$barcode_pipeline\t$epoc\tspecies_ITS2_reads\t$cs_sp_its2\n";
		print FILE "$barcode_pipeline\t$epoc\tgenus_COI_reads\t$cs_gn_coi\n";
		print FILE "$barcode_pipeline\t$epoc\tgenus_ITS2_reads\t$cs_gn_its2\n";
		print FILE "$barcode_pipeline\t$epoc\tfamily_COI_reads\t$cs_fm_coi\n";
		print FILE "$barcode_pipeline\t$epoc\tfamily_ITS2_reads\t$cs_fm_its2\n";
		close FILE;

	if(!-f "$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt")
	{
		open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt\n";
		print FILE "run_id\treads\ttaxon\tidentifications\n";
	}else
	{
		open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt\n";
	}
	print FILE "$barcode_pipeline\t$proc_reads\tspecies\t".@species_uniq."\n";
	print FILE "$barcode_pipeline\t$proc_reads\tgenus\t".@genus_uniq."\n";
	print FILE "$barcode_pipeline\t$proc_reads\tfamily\t".@family_uniq."\n";
	close FILE;
} elsif (-f "$temp_dir/$barcode_pipeline\_blast_consensus_tax_consolidated_rpt.txt") {
	# No consolidated rows: append zero entries for this round to keep series continuity.
	if(!-f "$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt")
	{
		open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt\n";
		print FILE "run_id\ttime\ttaxon\tidentifications\n";
	}else
	{
		open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt\n";
	}
	print FILE "$barcode_pipeline\t$epoc\tspecies\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tspecies_COI_reads\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tspecies_ITS2_reads\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus_COI_reads\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus_ITS2_reads\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily_COI_reads\t0\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily_ITS2_reads\t0\n";
	close FILE;

	if(!-f "$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt")
	{
		open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt\n";
		print FILE "run_id\treads\ttaxon\tidentifications\n";
	}else
	{
		open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_reads_rpt.txt\n";
	}
	print FILE "$barcode_pipeline\t$proc_reads\tspecies\t0\n";
	print FILE "$barcode_pipeline\t$proc_reads\tgenus\t0\n";
	print FILE "$barcode_pipeline\t$proc_reads\tfamily\t0\n";
	close FILE;
}

if ($consolidated_has_rows) {
##### TREEMAPS (consolidated consensus read abundance)
	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt\n";
	print FILE "consensus_class\tconsensus_genus\t#reads\n";
			foreach $each_class(sort keys(%class_reads_metazoa))
			{
				if (exists($class_genus_metazoa{$each_class})) {
				foreach $each_genus(sort keys(%{$class_genus_metazoa{$each_class}}))
				{
					next if is_unassigned_taxon($each_genus);
					if(exists($genus{$each_genus}))
					{
						my $n = $genus{$each_genus} // 0;
					next if ($use_local_gen && !exists($local_gen{$each_genus}));
						print FILE "$each_class\t$each_genus\t$n\n";
					}
				}
				}
		}
	close FILE;

	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_viridiplantae_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_viridiplantae_treemap_rpt.txt\n";
	print FILE "consensus_class\tconsensus_genus\t#reads\n";
			foreach $each_class(sort keys(%class_reads_viridiplantae))
			{
				if (exists($class_genus_viridiplantae{$each_class})) {
				foreach $each_genus(sort keys(%{$class_genus_viridiplantae{$each_class}}))
				{
					next if is_unassigned_taxon($each_genus);
					if(exists($genus{$each_genus}))
					{
						my $n = $genus{$each_genus} // 0;
					next if ($use_local_gen && !exists($local_gen{$each_genus}));
						print FILE "$each_class\t$each_genus\t$n\n";
					}
				}
				}
		}
	close FILE;

	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_metazoa_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_metazoa_treemap_rpt.txt\n";
	print FILE "consensus_class\tconsensus_species\t#reads\n";
			foreach $each_class(sort keys(%class_reads_metazoa))
			{
				if (exists($class_species_metazoa{$each_class})) {
				foreach $each_species(sort keys(%{$class_species_metazoa{$each_class}}))
				{
					next if is_unassigned_taxon($each_species);
					if(exists($species{$each_species}))
					{
						my $n = $species{$each_species} // 0;
						print FILE "$each_class\t$each_species\t$n\n";
					}
				}
				}
		}
	close FILE;

	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_viridiplantae_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_viridiplantae_treemap_rpt.txt\n";
	print FILE "consensus_class\tconsensus_species\t#reads\n";
			foreach $each_class(sort keys(%class_reads_viridiplantae))
			{
				if (exists($class_species_viridiplantae{$each_class})) {
				foreach $each_species(sort keys(%{$class_species_viridiplantae{$each_class}}))
				{
					next if is_unassigned_taxon($each_species);
					if(exists($species{$each_species}))
					{
						my $n = $species{$each_species} // 0;
						print FILE "$each_class\t$each_species\t$n\n";
					}
				}
				}
		}
	close FILE;

##### TREEMAPS (CONSOLIDATED CONSENSUS CLUSTERS; counts of consensus sequences, not reads)
	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_metazoa_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_metazoa_treemap_clusters_rpt.txt\n";
	print FILE "consensus_class\tconsensus_genus\t#clusters\n";
			foreach $each_class(sort keys(%class_clusters_metazoa))
			{
				my $class_with_genus_clusters = 0;
				if (exists($class_genus_clusters_metazoa{$each_class})) {
				foreach $each_genus(sort keys(%{$class_genus_clusters_metazoa{$each_class}}))
				{
					next if is_unassigned_taxon($each_genus);
					next if ($use_local_gen && !exists($local_gen{$each_genus}));
					my $c = $class_genus_clusters_metazoa{$each_class}{$each_genus} // 0;
					next unless $c && $c > 0;
					$class_with_genus_clusters += $c;
					print FILE "$each_class\t$each_genus\t$c\n";
				}
				}
			}
	close FILE;

	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_viridiplantae_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_gns_viridiplantae_treemap_clusters_rpt.txt\n";
	print FILE "consensus_class\tconsensus_genus\t#clusters\n";
			foreach $each_class(sort keys(%class_clusters_viridiplantae))
			{
				my $class_with_genus_clusters = 0;
				if (exists($class_genus_clusters_viridiplantae{$each_class})) {
				foreach $each_genus(sort keys(%{$class_genus_clusters_viridiplantae{$each_class}}))
				{
					next if is_unassigned_taxon($each_genus);
					next if ($use_local_gen && !exists($local_gen{$each_genus}));
					my $c = $class_genus_clusters_viridiplantae{$each_class}{$each_genus} // 0;
					next unless $c && $c > 0;
					$class_with_genus_clusters += $c;
					print FILE "$each_class\t$each_genus\t$c\n";
				}
				}
			}
	close FILE;

	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_metazoa_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_metazoa_treemap_clusters_rpt.txt\n";
	print FILE "consensus_class\tconsensus_species\t#clusters\n";
			foreach $each_class(sort keys(%class_clusters_metazoa))
			{
				my $class_with_species_clusters = 0;
				if (exists($class_species_clusters_metazoa{$each_class})) {
				foreach $each_species(sort keys(%{$class_species_clusters_metazoa{$each_class}}))
				{
					next if is_unassigned_taxon($each_species);
					my $c = $class_species_clusters_metazoa{$each_class}{$each_species} // 0;
					next unless $c && $c > 0;
					$class_with_species_clusters += $c;
					print FILE "$each_class\t$each_species\t$c\n";
				}
				}
			}
	close FILE;

	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_viridiplantae_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_consolidated_tax_spc_viridiplantae_treemap_clusters_rpt.txt\n";
	print FILE "consensus_class\tconsensus_species\t#clusters\n";
			foreach $each_class(sort keys(%class_clusters_viridiplantae))
			{
				my $class_with_species_clusters = 0;
				if (exists($class_species_clusters_viridiplantae{$each_class})) {
				foreach $each_species(sort keys(%{$class_species_clusters_viridiplantae{$each_class}}))
				{
					next if is_unassigned_taxon($each_species);
					my $c = $class_species_clusters_viridiplantae{$each_class}{$each_species} // 0;
					next unless $c && $c > 0;
					$class_with_species_clusters += $c;
					print FILE "$each_class\t$each_species\t$c\n";
				}
				}
			}
	close FILE;
}

	undef @species_uniq;
	undef @genus_uniq;
	undef @family_uniq;
	undef @class_metazoa;
	undef @class_viridiplantae;
	undef @a_samples_joined;

	#####	PLOTS BASED ON CONSENSUS TAXONOMICAL PREASSIGNMENTS

$header_flag=1;
undef %header;
undef %consensus_id;
undef %samples_joined;
undef %class;
undef %species;
undef %species_consensus_sample;
undef %detected_species;
undef %species_per_sample;
undef %class_species_metazoa;
undef %class_species_viridiplantae;
undef %species_clusters;
undef %class_species_clusters_metazoa;
undef %class_species_clusters_viridiplantae;
undef %genus;
undef %genus_consensus_sample;
undef %detected_genus;
undef %genus_per_sample;
undef %class_genus_metazoa;
undef %class_genus_viridiplantae;
undef %genus_clusters;
undef %class_genus_clusters_metazoa;
undef %class_genus_clusters_viridiplantae;
undef %family;
undef %family_consensus_sample;
undef %detected_family;
undef %family_per_sample;
undef %class_family_metazoa;
undef %class_family_viridiplantae;
undef %class_reads_metazoa;
undef %class_reads_viridiplantae;
undef %class_clusters_metazoa;
undef %class_clusters_viridiplantae;

# Reset consensus counters before standard consensus parsing to avoid contamination
$consensus_genus_reads_demux = 0;
$consensus_genus_reads_noadapter = 0;
undef %cons_reads_rank_marker;

open FILE, "$temp_dir/$barcode_pipeline\_blast_consensus_tax_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_blast_consensus_tax_rpt.txt\n";
my $consensus_header_line = "";
my %rep_best_species_line = ();
my %rep_best_species_reads = ();
my %rep_best_species_genus = ();
my %rep_best_species_pid = ();
my %rep_best_species_aln = ();
my %rep_best_genus_line = ();
my %rep_best_genus_reads = ();
my %rep_best_genus_pid = ();
my %rep_best_genus_aln = ();
while(<FILE>)
{
	my $line = $_;
	chomp $line;
	@tr=split/\t/,$line;

	if ($header_flag)
	{
		$consensus_header_line = $line;
		for($i=0;$i<@tr;$i++)
		{
			$header{$tr[$i]}=$i;
		}
		$header_flag=0;
			}else
			{
					$sample=$tr[$header{"sample"}];
					my $is_no_adapter = ($sample =~ /^no_adapter/i) ? 1 : 0;
					# Robust parsing: allow arbitrary sample names and avoid stale $1/$2 when regex doesn't match.
					($sample_joined, $replicate) = ($sample, 1);
					if ($sample =~ /^(.*)_(\d+)$/) {
						($sample_joined, $replicate) = ($1, $2);
					}

				# For consensus reports, each line corresponds to a consensus sequence, which has an
				# associated number_of_reads. Use that as the weight so `min_reads_sample` reflects
				# underlying read support rather than consensus-sequence counts.
				my $nreads = 1;
				if (exists($header{"number_of_reads"})) {
					my $v = $tr[$header{"number_of_reads"}];
					$v =~ s/\r$// if defined $v;
					if (defined $v && $v =~ /^[0-9]+$/) {
						$nreads = int($v);
					}
				}
				
					$samples_joined{$sample_joined}=1;
						$consensus_id=$tr[$header{"consensus_id"}];
						my $marker = "";
						if (exists($header{"barcode_by_homology"})) {
							$marker = $tr[$header{"barcode_by_homology"}] // "";
						}
						$marker = "COI"  if $marker =~ /^COI/i;
						$marker = "ITS2" if $marker =~ /^ITS/i;

					# Representative consensus IDs (best by number_of_reads) for species/genus.
					# This reproduces the old "representative-only" consensus table, but is now derived
					# from the full consensus table that includes all clusters.
					my $rep_sample = $tr[$header{"sample"}] // $sample;
					my $rep_species = $tr[$header{"consensus_species"}] // '';
					my $rep_genus = $tr[$header{"consensus_genus"}] // '';
					my $rep_aln = 0;
					my $rep_pid = 0;
					if (exists($header{"aln_length"})) {
						my $v = $tr[$header{"aln_length"}];
						$rep_aln = $v if defined $v && $v =~ /^[0-9]+$/;
					}
					if (exists($header{"perc_id"})) {
						my $v = $tr[$header{"perc_id"}];
						$rep_pid = $v if defined $v && $v =~ /^[0-9.]+$/;
					}
					my $is_better = sub {
						my ($new_reads, $new_pid, $new_aln, $old_reads, $old_pid, $old_aln) = @_;
						return 1 if !defined($old_reads);
						return 1 if $new_reads > $old_reads;
						return 0 if $new_reads < $old_reads;
						# tie-breakers (best BLAST quality)
						return 1 if $new_pid > ($old_pid // 0);
						return 0 if $new_pid < ($old_pid // 0);
						return 1 if $new_aln > ($old_aln // 0);
						return 0;
					};
					if ($rep_species ne '') {
						my $k = "$rep_sample\t$rep_species";
						if ($is_better->($nreads, $rep_pid, $rep_aln, $rep_best_species_reads{$k}, ($rep_best_species_pid{$k} // 0), ($rep_best_species_aln{$k} // 0))) {
							$rep_best_species_line{$k} = $line;
							$rep_best_species_reads{$k} = $nreads;
							$rep_best_species_genus{$k} = $rep_genus;
							$rep_best_species_pid{$k} = $rep_pid;
							$rep_best_species_aln{$k} = $rep_aln;
						}
					} elsif ($rep_genus ne '') {
						my $k = "$rep_sample\t$rep_genus";
						if ($is_better->($nreads, $rep_pid, $rep_aln, $rep_best_genus_reads{$k}, ($rep_best_genus_pid{$k} // 0), ($rep_best_genus_aln{$k} // 0))) {
							$rep_best_genus_line{$k} = $line;
							$rep_best_genus_reads{$k} = $nreads;
							$rep_best_genus_pid{$k} = $rep_pid;
							$rep_best_genus_aln{$k} = $rep_aln;
						}
					}
				
				
					if($tr[$header{"consensus_kingdom"}] eq "Metazoa")
					{
					if($tr[$header{"consensus_class"}])
					{
						$class{$tr[$header{"consensus_class"}]} += $nreads;
						$class_reads_metazoa{$tr[$header{"consensus_class"}]} += $nreads;
						$class_clusters_metazoa{$tr[$header{"consensus_class"}]}++;
		#				if($tr[$header{"consensus_species"}] && exists($local_spec{$tr[$header{"consensus_species"}]}))
						if($tr[$header{"consensus_species"}] && !is_unassigned_taxon($tr[$header{"consensus_species"}]))				{
							$species{$tr[$header{"consensus_species"}]} += $nreads;
							$cons_reads_rank_marker{"species"}{$marker} += $nreads if $marker ne "";
							$species_clusters{$tr[$header{"consensus_species"}]}++;
							$class_species_clusters_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}++;
							$species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined} += $nreads;
						if($species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined} == 5){$species_consensus_sample5{$tr[$header{"consensus_species"}]}=1;}
						if($species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined}>9)
						{
							$detected_species{$sample_joined}{$tr[$header{"consensus_species"}]}=1;
						}
						$species_per_sample{$sample_joined}{$tr[$header{"consensus_species"}]} += $nreads;
						$class_species_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}=1;
					}
		#				if($tr[$header{"consensus_genus"}] && exists($local_gen{$tr[$header{"consensus_genus"}]}))
							if($tr[$header{"consensus_genus"}] && !is_unassigned_taxon($tr[$header{"consensus_genus"}]))
							{
								$genus{$tr[$header{"consensus_genus"}]} += $nreads;
								$cons_reads_rank_marker{"genus"}{$marker} += $nreads if $marker ne "";
								$genus_clusters{$tr[$header{"consensus_genus"}]}++;
								$class_genus_clusters_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}++;
								$genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined} += $nreads;
							if($is_no_adapter){ $consensus_genus_reads_noadapter += $nreads; } else { $consensus_genus_reads_demux += $nreads; }
						if($genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined} == 5){$genus_consensus_sample5{$tr[$header{"consensus_genus"}]}=1;}
						if($genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined}>9)
						{
							$detected_genus{$sample_joined}{$tr[$header{"consensus_genus"}]}=1;
						}
						$genus_per_sample{$sample_joined}{$tr[$header{"consensus_genus"}]} += $nreads;
						$class_genus_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}=1;
					}
						if($tr[$header{"consensus_family"}] && !is_unassigned_taxon($tr[$header{"consensus_family"}]))
						{
							$family{$tr[$header{"consensus_family"}]} += $nreads;
							$cons_reads_rank_marker{"family"}{$marker} += $nreads if $marker ne "";
							$family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined} += $nreads;
							if($family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined} == 5){$family_consensus_sample5{$tr[$header{"consensus_family"}]}=1;}
							if($family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined}>9)
							{
							$detected_family{$sample_joined}{$tr[$header{"consensus_family"}]}=1;
						}
						$family_per_sample{$sample_joined}{$tr[$header{"consensus_family"}]} += $nreads;
						$class_family_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_family"}]}=1;
					}
				
				}
				}elsif($tr[$header{"consensus_kingdom"}] eq "Viridiplantae")
				{
					if($tr[$header{"consensus_class"}])
					{
						$class{$tr[$header{"consensus_class"}]} += $nreads;
						$class_reads_viridiplantae{$tr[$header{"consensus_class"}]} += $nreads;
						$class_clusters_viridiplantae{$tr[$header{"consensus_class"}]}++;
		#				if($tr[$header{"consensus_species"}] && exists($local_spec{$tr[$header{"consensus_species"}]}))
						if($tr[$header{"consensus_species"}] && !is_unassigned_taxon($tr[$header{"consensus_species"}]))
						{
							$species{$tr[$header{"consensus_species"}]} += $nreads;
							$cons_reads_rank_marker{"species"}{$marker} += $nreads if $marker ne "";
							$species_clusters{$tr[$header{"consensus_species"}]}++;
							$class_species_clusters_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}++;
							$species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined} += $nreads;
						if($species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined} == 5){$species_consensus_sample5{$tr[$header{"consensus_species"}]}=1;}
						if($species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined}>9)
						{
							$detected_species{$sample_joined}{$tr[$header{"consensus_species"}]}=1;
						}
						$species_per_sample{$sample_joined}{$tr[$header{"consensus_species"}]} += $nreads;
						$class_species_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}=1;
					}
		#				if($tr[$header{"consensus_genus"}] && exists($local_gen{$tr[$header{"consensus_genus"}]}))
							if($tr[$header{"consensus_genus"}] && !is_unassigned_taxon($tr[$header{"consensus_genus"}]))
							{
								$genus{$tr[$header{"consensus_genus"}]} += $nreads;
								$cons_reads_rank_marker{"genus"}{$marker} += $nreads if $marker ne "";
								$genus_clusters{$tr[$header{"consensus_genus"}]}++;
								$class_genus_clusters_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}++;
								$genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined} += $nreads;
							if($is_no_adapter){ $consensus_genus_reads_noadapter += $nreads; } else { $consensus_genus_reads_demux += $nreads; }
						if($genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined} == 5){$genus_consensus_sample5{$tr[$header{"consensus_genus"}]}=1;}
						if($genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined}>9)
						{
							$detected_genus{$sample_joined}{$tr[$header{"consensus_genus"}]}=1;
						}
						$genus_per_sample{$sample_joined}{$tr[$header{"consensus_genus"}]} += $nreads;
						$class_genus_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}=1;
					}
						if($tr[$header{"consensus_family"}] && !is_unassigned_taxon($tr[$header{"consensus_family"}]))
						{
							$family{$tr[$header{"consensus_family"}]} += $nreads;
							$cons_reads_rank_marker{"family"}{$marker} += $nreads if $marker ne "";
							$family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined} += $nreads;
							if($family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined} == 5){$family_consensus_sample5{$tr[$header{"consensus_family"}]}=1;}
							if($family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined}>9)
							{
							$detected_family{$sample_joined}{$tr[$header{"consensus_family"}]}=1;
						}
						$family_per_sample{$sample_joined}{$tr[$header{"consensus_family"}]} += $nreads;
						$class_family_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_family"}]}=1;
					}
				}
			}
		
		
		
		
#		if($tr[$header{"consensus_kingdom"}] eq "Metazoa")
#		{
#			if($tr[$header{"consensus_class"}])
#			{
#				$class{$tr[$header{"consensus_class"}]}++;
#				if($tr[$header{"consensus_species"}] && exists($local_spec{$tr[$header{"consensus_species"}]}))
#				{
#					$species{$tr[$header{"consensus_species"}]}++;
#					$species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined}++;
#					if($species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined}>9)
#					{
#						$detected_species{$sample_joined}{$tr[$header{"consensus_species"}]}=1;
#					}
#					$species_per_sample{$sample_joined}{$tr[$header{"consensus_species"}]}++;
#					$class_species_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}=1;
#				}
#				if($tr[$header{"consensus_genus"}] && exists($local_gen{$tr[$header{"consensus_genus"}]}))
#				{
#					$genus{$tr[$header{"consensus_genus"}]}++;
#					$genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined}++;
#					if($genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined}>9)
#					{
#						$detected_genus{$sample_joined}{$tr[$header{"consensus_genus"}]}=1;
#					}
#					$genus_per_sample{$sample_joined}{$tr[$header{"consensus_genus"}]}++;
#					$class_genus_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}=1;
#				}
#				if($tr[$header{"consensus_family"}])
#				{
#					$family{$tr[$header{"consensus_family"}]}++;
#					$family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined}++;
#					if($family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined}>9)
#					{
#						$detected_family{$sample_joined}{$tr[$header{"consensus_family"}]}=1;
#					}
#					$family_per_sample{$sample_joined}{$tr[$header{"consensus_family"}]}++;
#					$class_family_metazoa{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_family"}]}=1;
#				}
#			
#			}
#		}elsif($tr[$header{"consensus_kingdom"}] eq "Viridiplantae")
#		{
#			if($tr[$header{"consensus_class"}])
#			{
#				$class{$tr[$header{"consensus_class"}]}++;
#				if($tr[$header{"consensus_species"}] && exists($local_spec{$tr[$header{"consensus_species"}]}))
#				{
#					$species{$tr[$header{"consensus_species"}]}++;
#					$species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined}++;
#					if($species_consensus_sample{$tr[$header{"consensus_species"}]}{$consensus_id}{$sample_joined}>9)
#					{
#						$detected_species{$sample_joined}{$tr[$header{"consensus_species"}]}=1;
#					}
#					$species_per_sample{$sample_joined}{$tr[$header{"consensus_species"}]}++;
#					$class_species_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_species"}]}=1;
#				}
#				if($tr[$header{"consensus_genus"}] && exists($local_gen{$tr[$header{"consensus_genus"}]}))
#				{
#					$genus{$tr[$header{"consensus_genus"}]}++;
#					$genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined}++;
#					if($genus_consensus_sample{$tr[$header{"consensus_genus"}]}{$consensus_id}{$sample_joined}>9)
#					{
#						$detected_genus{$sample_joined}{$tr[$header{"consensus_genus"}]}=1;
#					}
#					$genus_per_sample{$sample_joined}{$tr[$header{"consensus_genus"}]}++;
#					$class_genus_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_genus"}]}=1;
#				}
#				if($tr[$header{"consensus_family"}])
#				{
#					$family{$tr[$header{"consensus_family"}]}++;
#					$family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined}++;
#					if($family_consensus_sample{$tr[$header{"consensus_family"}]}{$consensus_id}{$sample_joined}>9)
#					{
#						$detected_family{$sample_joined}{$tr[$header{"consensus_family"}]}=1;
#					}
#					$family_per_sample{$sample_joined}{$tr[$header{"consensus_family"}]}++;
#					$class_family_viridiplantae{$tr[$header{"consensus_class"}]}{$tr[$header{"consensus_family"}]}=1;
#				}
#			}
#		}
	}	
}
close FILE;

# Write a "representative-only" consensus table (best consensus per species, then per genus when
# no species-level representative exists for that genus in that sample).
my $rep_file = "$temp_dir/$barcode_pipeline\_blast_consensus_tax_representative_rpt.txt";
open my $REP, '>', $rep_file or die "I couldn't open $rep_file\n";
if ($consensus_header_line ne '') {
	print $REP $consensus_header_line, "\n";
} else {
	print $REP "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\ttaxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n";
}
my %rep_found_genus = ();
foreach my $k (sort keys %rep_best_species_line)
{
	print $REP $rep_best_species_line{$k}, "\n";
	my $g = $rep_best_species_genus{$k} // '';
	if ($g ne '') {
		my ($samp) = split(/\t/, $k, 2);
		$rep_found_genus{"$samp\t$g"} = 1;
	}
}
foreach my $k (sort keys %rep_best_genus_line)
{
	next if exists($rep_found_genus{$k});
	print $REP $rep_best_genus_line{$k}, "\n";
}
close $REP;


undef @species_uniq;
undef @genus_uniq;
undef @family_uniq;
undef @class_metazoa;
undef @class_viridiplantae;
undef @a_samples_joined;

	@species_uniq=sort keys(%species);
	@genus_uniq=sort keys(%genus);
	@family_uniq=sort keys(%family);

	# Apply the same "show taxon" filters to consensus-derived summaries.
	if($min_reads_sample && $min_reads_sample > 0)
	{
		@species_uniq = grep { defined $species{$_} && $species{$_} >= $min_reads_sample } @species_uniq;
		@genus_uniq   = grep { defined $genus{$_}   && $genus{$_}   >= $min_reads_sample } @genus_uniq;
		@family_uniq  = grep { defined $family{$_}  && $family{$_}  >= $min_reads_sample } @family_uniq;
	}
	if($use_spec_interest)
	{
		@species_uniq = grep { exists($spec_interest{$_}) } @species_uniq;
	}
	if($use_local_gen)
	{
		@genus_uniq = grep { exists($local_gen{$_}) } @genus_uniq;
	}
	@class_metazoa=sort keys(%class_species_metazoa);
	@class_viridiplantae=sort keys(%class_species_viridiplantae);
	@a_samples_joined=sort keys(%samples_joined);

$consensus_genus_reads = $consensus_genus_reads_demux + $consensus_genus_reads_noadapter;

my $total_reads_cumulative = scalar(keys %{ $seen_ids_ref // {} });

my $matched_reads_cumulative = 0;
for my $rid (keys %{ $on_target_state_ref // {} }) {
	next unless exists $seen_ids_ref->{$rid};
	next unless defined $on_target_state_ref->{$rid} && $on_target_state_ref->{$rid} eq 'ON_TARGET';
	next unless exists $on_target_barcode_ref->{$rid};
	next unless defined $on_target_barcode_ref->{$rid} && $on_target_barcode_ref->{$rid} ne '';
	$matched_reads_cumulative++;
}

my $demux_reads_cumulative = 0;
if (-f "$temp_dir/$barcode_pipeline\_demult_rpt.txt") {
	open my $DEMULT, "<", "$temp_dir/$barcode_pipeline\_demult_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_demult_rpt.txt\n";
	my $header = <$DEMULT>;
	my $count_idx = 1;
	if (defined $header) {
		chomp $header;
		my @cols = split(/\t/, $header);
		my %idx = map { $cols[$_] => $_ } 0..$#cols;
		$count_idx = $idx{'read_count'} if exists $idx{'read_count'};
	}
	while (my $line = <$DEMULT>) {
		chomp $line;
		next unless $line =~ /\S/;
		my @vals = split(/\t/, $line);
		my $val = $vals[$count_idx] // 0;
		$val =~ s/\r$//;
		$demux_reads_cumulative += $val if $val =~ /^[0-9]+$/;
	}
	close $DEMULT;
}

my $cumulative_file = "$temp_dir/$barcode_pipeline\_reads_cumulative_rpt.txt";
my $new_cumulative = !-f $cumulative_file;
open my $CUM, ($new_cumulative ? '>' : '>>'), $cumulative_file or die "I couldn't open $cumulative_file\n";
print $CUM "run_id\ttime\ttotal_reads\tmatched_reads\tdemultiplexed_reads\totu_genus_reads\tconsensus_genus_reads\totu_genus_reads_demux\totu_genus_reads_noadapter\tconsensus_genus_reads_demux\tconsensus_genus_reads_noadapter\n" if $new_cumulative;
print $CUM join("\t", $barcode_pipeline, $epoc, $total_reads_cumulative, $matched_reads_cumulative, $demux_reads_cumulative, $otu_genus_reads, $consensus_genus_reads, $otu_genus_reads_demux, $otu_genus_reads_noadapter, $consensus_genus_reads_demux, $consensus_genus_reads_noadapter), "\n";
close $CUM;

if (defined $run_start_epoc) {
	my @reads_time_t0 = t0_rows_for('reads_time', $barcode_pipeline, $run_start_epoc, $run_start_iso);
	my @reads_cum_t0 = t0_rows_for('reads_cumulative', $barcode_pipeline, $run_start_epoc, $run_start_iso);
	my @tax_t0 = t0_rows_for('tax_time', $barcode_pipeline, $run_start_epoc, $run_start_iso);
	ensure_t0_rows(
		"$temp_dir/$barcode_pipeline\_reads_time_rpt.txt",
		"run_id\ttime\tdata\treads",
		\@reads_time_t0
	);
	ensure_t0_rows(
		"$temp_dir/$barcode_pipeline\_reads_cumulative_rpt.txt",
		"run_id\ttime\ttotal_reads\tmatched_reads\tdemultiplexed_reads\totu_genus_reads\tconsensus_genus_reads\totu_genus_reads_demux\totu_genus_reads_noadapter\tconsensus_genus_reads_demux\tconsensus_genus_reads_noadapter",
		\@reads_cum_t0
	);
	ensure_t0_rows(
		"$temp_dir/$barcode_pipeline\_otu_tax_time_rpt.txt",
		"run_id\ttime\ttaxon\tidentifications",
		\@tax_t0
	);
	ensure_t0_rows(
		"$temp_dir/$barcode_pipeline\_otu_sample5_tax_time_rpt.txt",
		"run_id\ttime\ttaxon\tidentifications",
		\@tax_t0
	);
	ensure_t0_rows(
		"$temp_dir/$barcode_pipeline\_consensus_consolidated_tax_time_rpt.txt",
		"run_id\ttime\ttaxon\tidentifications",
		\@tax_t0
	);
}


#####	ABUNDANCE HEATMAPS


#if(!-f "$temp_dir/$barcode_pipeline\_consensus_tax_detect_abundance_rpt.txt")
#{
#	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_detect_abundance_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_detect_abundance_rpt.txt\n";
#	print FILE "sample\tprocessing_time(YYY-MM-DDTHH:MM:SSZ)\tprocessing_time(epoc secs)\tfamily\tgenus\tspecies\n";
#}else
#{
#	open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_tax_detect_abundance_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_detect_abundance_rpt.txt\n";
#}
#print FILE "$round_dir\t$datestring\t$epoc\t".@family_uniq."\t".@genus_uniq."\t".@species_uniq."\n";
#close FILE;



if(!-f "$temp_dir/$barcode_pipeline\_consensus_tax_time_rpt.txt")
{
	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_time_rpt.txt\n";
	print FILE "run_id\ttime\ttaxon\tidentifications\n";
}else
{
	open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_tax_time_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_time_rpt.txt\n";
}
	print FILE "$barcode_pipeline\t$epoc\tspecies\t".@species_uniq."\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus\t".@genus_uniq."\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily\t".@family_uniq."\n";
	my $cs_sp_coi  = $cons_reads_rank_marker{"species"}{"COI"}  // 0;
	my $cs_sp_its2 = $cons_reads_rank_marker{"species"}{"ITS2"} // 0;
	my $cs_gn_coi  = $cons_reads_rank_marker{"genus"}{"COI"}    // 0;
	my $cs_gn_its2 = $cons_reads_rank_marker{"genus"}{"ITS2"}   // 0;
	my $cs_fm_coi  = $cons_reads_rank_marker{"family"}{"COI"}   // 0;
	my $cs_fm_its2 = $cons_reads_rank_marker{"family"}{"ITS2"}  // 0;
	print FILE "$barcode_pipeline\t$epoc\tspecies_COI_reads\t$cs_sp_coi\n";
	print FILE "$barcode_pipeline\t$epoc\tspecies_ITS2_reads\t$cs_sp_its2\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus_COI_reads\t$cs_gn_coi\n";
	print FILE "$barcode_pipeline\t$epoc\tgenus_ITS2_reads\t$cs_gn_its2\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily_COI_reads\t$cs_fm_coi\n";
	print FILE "$barcode_pipeline\t$epoc\tfamily_ITS2_reads\t$cs_fm_its2\n";
	close FILE;
if (defined $run_start_epoc) {
	my @_cons_tax_t0 = t0_rows_for('tax_time', $barcode_pipeline, $run_start_epoc, $run_start_iso);
	ensure_t0_rows(
		"$temp_dir/$barcode_pipeline\_consensus_tax_time_rpt.txt",
		"run_id\ttime\ttaxon\tidentifications",
		\@_cons_tax_t0
	);
}

if(!-f "$temp_dir/$barcode_pipeline\_consensus_tax_reads_rpt.txt")
{
	open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_reads_rpt.txt\n";
	print FILE "run_id\treads\ttaxon\tidentifications\n";
}else
{
	open FILE, ">>$temp_dir/$barcode_pipeline\_consensus_tax_reads_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_reads_rpt.txt\n";
}
print FILE "$barcode_pipeline\t$proc_reads\tspecies\t".@species_uniq."\n";
print FILE "$barcode_pipeline\t$proc_reads\tgenus\t".@genus_uniq."\n";
print FILE "$barcode_pipeline\t$proc_reads\tfamily\t".@family_uniq."\n";
close FILE;



##### TREEMAPS
open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_gns_metazoa_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_gns_metazoa_treemap_rpt.txt\n";
print FILE "consensus_class\tconsensus_genus\t#reads\n";
		foreach $each_class(sort keys(%class_reads_metazoa))
		{
			if (exists($class_genus_metazoa{$each_class})) {
			foreach $each_genus(sort keys(%{$class_genus_metazoa{$each_class}}))
			{
				next if is_unassigned_taxon($each_genus);
				if(exists($genus{$each_genus}))
				{
					my $n = $genus{$each_genus} // 0;
				next if ($use_local_gen && !exists($local_gen{$each_genus}));
					print FILE "$each_class\t$each_genus\t$n\n";
				}
			}
			}
	}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_gns_viridiplantae_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_gns_viridiplantae_treemap_rpt.txt\n";
print FILE "consensus_class\tconsensus_genus\t#reads\n";
		foreach $each_class(sort keys(%class_reads_viridiplantae))
		{
			if (exists($class_genus_viridiplantae{$each_class})) {
			foreach $each_genus(sort keys(%{$class_genus_viridiplantae{$each_class}}))
			{
				next if is_unassigned_taxon($each_genus);
				if(exists($genus{$each_genus}))
				{
					my $n = $genus{$each_genus} // 0;
				next if ($use_local_gen && !exists($local_gen{$each_genus}));
					print FILE "$each_class\t$each_genus\t$n\n";
				}
			}
			}
	}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_spc_metazoa_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_spc_metazoa_treemap_rpt.txt\n";
print FILE "consensus_class\tconsensus_species\t#reads\n";
		foreach $each_class(sort keys(%class_reads_metazoa))
		{
			if (exists($class_species_metazoa{$each_class})) {
			foreach $each_species(sort keys(%{$class_species_metazoa{$each_class}}))
			{
				next if is_unassigned_taxon($each_species);
				if(exists($species{$each_species}))
				{
					my $n = $species{$each_species} // 0;
					print FILE "$each_class\t$each_species\t$n\n";
				}
			}
			}
	}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_spc_viridiplantae_treemap_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_spc_viridiplantae_treemap_rpt.txt\n";
print FILE "consensus_class\tconsensus_species\t#reads\n";
		foreach $each_class(sort keys(%class_reads_viridiplantae))
		{
			if (exists($class_species_viridiplantae{$each_class})) {
			foreach $each_species(sort keys(%{$class_species_viridiplantae{$each_class}}))
			{
				next if is_unassigned_taxon($each_species);
				if(exists($species{$each_species}))
				{
					my $n = $species{$each_species} // 0;
					print FILE "$each_class\t$each_species\t$n\n";
				}
			}
			}
	}
close FILE;

##### TREEMAPS (CONSENSUS CLUSTERS; counts of consensus sequences, not reads)
#
# These tables are parallel to the *_treemap_rpt.txt read-abundance tables, but the value is the
# number of consensus clusters contributing to each assignment. Taxa inclusion filtering (e.g.
# min_reads_sample / local genus list / species-of-interest) is still based on read support, to
# keep "shown taxa" consistent with the read-based tables.

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_gns_metazoa_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_gns_metazoa_treemap_clusters_rpt.txt\n";
print FILE "consensus_class\tconsensus_genus\t#clusters\n";
		foreach $each_class(sort keys(%class_clusters_metazoa))
		{
			my $class_with_genus_clusters = 0;
			# Cluster treemaps show #clusters per taxon. Do not gate by read counts (`min_reads_sample`),
			# otherwise this plot can be empty even when consensus assignments exist.
			if (exists($class_genus_clusters_metazoa{$each_class})) {
			foreach $each_genus(sort keys(%{$class_genus_clusters_metazoa{$each_class}}))
			{
				next if is_unassigned_taxon($each_genus);
				next if ($use_local_gen && !exists($local_gen{$each_genus}));
				my $c = $class_genus_clusters_metazoa{$each_class}{$each_genus} // 0;
				next unless $c && $c > 0;
				$class_with_genus_clusters += $c;
				print FILE "$each_class\t$each_genus\t$c\n";
			}
			}
		}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_gns_viridiplantae_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_gns_viridiplantae_treemap_clusters_rpt.txt\n";
print FILE "consensus_class\tconsensus_genus\t#clusters\n";
		foreach $each_class(sort keys(%class_clusters_viridiplantae))
		{
			my $class_with_genus_clusters = 0;
			if (exists($class_genus_clusters_viridiplantae{$each_class})) {
			foreach $each_genus(sort keys(%{$class_genus_clusters_viridiplantae{$each_class}}))
			{
				next if is_unassigned_taxon($each_genus);
				next if ($use_local_gen && !exists($local_gen{$each_genus}));
				my $c = $class_genus_clusters_viridiplantae{$each_class}{$each_genus} // 0;
				next unless $c && $c > 0;
				$class_with_genus_clusters += $c;
				print FILE "$each_class\t$each_genus\t$c\n";
			}
			}
		}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_spc_metazoa_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_spc_metazoa_treemap_clusters_rpt.txt\n";
print FILE "consensus_class\tconsensus_species\t#clusters\n";
		foreach $each_class(sort keys(%class_clusters_metazoa))
		{
			my $class_with_species_clusters = 0;
			if (exists($class_species_clusters_metazoa{$each_class})) {
			foreach $each_species(sort keys(%{$class_species_clusters_metazoa{$each_class}}))
			{
				next if is_unassigned_taxon($each_species);
				my $c = $class_species_clusters_metazoa{$each_class}{$each_species} // 0;
				next unless $c && $c > 0;
				$class_with_species_clusters += $c;
				print FILE "$each_class\t$each_species\t$c\n";
			}
			}
		}
close FILE;

open FILE, ">$temp_dir/$barcode_pipeline\_consensus_tax_spc_viridiplantae_treemap_clusters_rpt.txt" or die "I couldn't open $temp_dir/$barcode_pipeline\_consensus_tax_spc_viridiplantae_treemap_clusters_rpt.txt\n";
print FILE "consensus_class\tconsensus_species\t#clusters\n";
		foreach $each_class(sort keys(%class_clusters_viridiplantae))
		{
			my $class_with_species_clusters = 0;
			if (exists($class_species_clusters_viridiplantae{$each_class})) {
			foreach $each_species(sort keys(%{$class_species_clusters_viridiplantae{$each_class}}))
			{
				next if is_unassigned_taxon($each_species);
				my $c = $class_species_clusters_viridiplantae{$each_class}{$each_species} // 0;
				next unless $c && $c > 0;
				$class_with_species_clusters += $c;
				print FILE "$each_class\t$each_species\t$c\n";
			}
			}
		}
close FILE;
