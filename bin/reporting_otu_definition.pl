#!/usr/bin/perl

use strict;
use warnings;
use FindBin;

require "$FindBin::Bin/reporting_contract_sidecar.pl";

my $clstr_file = $ARGV[0];
my $demultiplex_qc_report_file = $ARGV[1];
my $round_dir = $ARGV[2];
my $barcode_pipeline = $ARGV[3];
my @allowed_targets = @ARGV[4..$#ARGV];

my $report_file = $barcode_pipeline . "_otu_def_rpt.txt";
my $sidecar_file = $barcode_pipeline . "_otu_def_rpt.contract.tsv";
my $round_members_file = $barcode_pipeline . "_otu_members_round.tsv";
my $round_sizes_file = $barcode_pipeline . "_otu_sizes_round.tsv";
my $context = lc($ENV{"RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"} || '');
die "ERROR: RTBIOSCAN_DEMUX_IDENTITY_CONTEXT is required\n" if $context eq '';

my %allowed_target;
for my $t (@allowed_targets) {
	next if !defined $t;
	$t =~ s/^\s+|\s+$//g;
	next if $t eq '';
	next if lc($t) eq 'null';
	next if lc($t) eq 'na';
	$allowed_target{uc($t)} = 1;
}
my $has_allowed_targets = scalar(keys %allowed_target) ? 1 : 0;

sub is_allowed_target_token {
	my ($tok) = @_;
	return 0 if !defined $tok;
	$tok =~ s/^\s+|\s+$//g;
	return 0 if $tok eq '' || uc($tok) eq 'NA';
	return 0 unless $has_allowed_targets;
	return exists $allowed_target{uc($tok)} ? 1 : 0;
}

sub is_marker_candidate_token {
	my ($tok) = @_;
	return 0 if !defined $tok;
	$tok =~ s/^\s+|\s+$//g;
	return 0 if $tok eq '' || uc($tok) eq 'NA';
	return 0 if $tok =~ /^(sup|hac|fast|hac_fixed|hac2sup)$/i;
	return 0 if $tok =~ /^(barcode|adapter)=/i;
	return 0 if $tok =~ /^OTUB_/i;
	return 0 if $tok =~ /=/;
	return 1;
}

sub first_allowed_target {
	my (@tokens) = @_;
	return ('', 0, 0) unless $has_allowed_targets;
	my $seen = 0;
	for my $tok (@tokens) {
		next unless is_marker_candidate_token($tok);
		$seen = 1;
		return ($tok, $seen, 0) if is_allowed_target_token($tok);
	}
	return ('', $seen, $seen ? 1 : 0);
}

my %read_line;
my $header_flag = 1;
my %header;
my $header_line = ReportingContractSidecar::canonical_header('otu_def_rpt');
open my $demux_fh, '<', $demultiplex_qc_report_file or die "I couldn't open $demultiplex_qc_report_file\n";
while (my $line = <$demux_fh>) {
	chomp $line;
	$line =~ s/\r$//;
	my @tr = split /\t/, $line, -1;
	if ($header_flag) {
		for (my $i = 0; $i < @tr; $i++) {
			$header{$tr[$i]} = $i;
		}
		$header_flag = 0;
		next;
	}
	my $read_id_raw = $tr[$header{"read_id"}];
	my $identity_scope = exists($header{"identity_scope"}) ? $tr[$header{"identity_scope"}] : 'unknown';
	my $identity_value = exists($header{"identity_value"}) ? $tr[$header{"identity_value"}] : 'unknown';
	$identity_scope = 'unknown' if !defined($identity_scope) || $identity_scope eq '';
	$identity_value = 'unknown' if !defined($identity_value) || $identity_value eq '';
	my ($read_id_base) = split /\|/, $read_id_raw;
	if (!exists $read_line{$read_id_base}) {
		$read_line{$read_id_base} = join("\t", @tr[0..7], $identity_scope, $identity_value);
	}
}
close $demux_fh;

open my $out_fh, '>', $report_file or die "I couldn't open $report_file\n";
print $out_fh $header_line, "\n";

my %round_members;
my %round_otu_size;
my $marker_seen = 0;
my $marker_allowed = 0;
my $marker_rejected = 0;
my $otu = 0;

if (-e $clstr_file && -s $clstr_file) {
	open my $clstr_fh, '<', $clstr_file or die "I couldn't open $clstr_file\n";
	while (my $line = <$clstr_fh>) {
		if ($line =~ /\>Cluster (\d+)/) {
			$otu = $1;
			next;
		}
		next if $line !~ /\>(\S+)\.\.\.(?: (\S))?/;
		my $full_id = $1;
		my @parts = split /\|/, $full_id;
		my $read_id = $parts[0];
		my @scan_tokens = ();
		if ($#parts >= 1) {
			@scan_tokens = @parts[1..$#parts];
		}
		my ($target, $seen_marker, $rejected_marker) = first_allowed_target(@scan_tokens);
		my $role = (defined($2) && $2 eq '*') ? 'REPRESENTATIVE' : 'MEMBER';
		my $line_out = exists($read_line{$read_id}) ? $read_line{$read_id} : join("\t", $read_id, 'NA', 'hac', 'no_adapter', 'unknown', 'unknown', 'unknown', 'unknown', 'unknown', 'unknown');
		my $otu_id = "OTUB_$otu";
		if ($seen_marker) {
			$marker_seen++;
			if ($target ne '') {
				$marker_allowed++;
				$otu_id .= "-$target";
			} elsif ($rejected_marker) {
				$marker_rejected++;
			}
		}
		print $out_fh $line_out, "\t", $otu_id, "\t", $role, "\n";
		my $pair = $otu_id . "\t" . $read_id;
		if (!exists $round_members{$pair}) {
			$round_members{$pair} = 1;
			$round_otu_size{$otu_id}++;
		}
	}
	close $clstr_fh;
}
close $out_fh;

open my $members_fh, '>', $round_members_file or die "I couldn't open $round_members_file\n";
print $members_fh "otu_id\tread_id\n";
for my $pair (sort keys %round_members) {
	print $members_fh $pair, "\n";
}
close $members_fh;

open my $sizes_fh, '>', $round_sizes_file or die "I couldn't open $round_sizes_file\n";
print $sizes_fh "otu_id\tsize\n";
for my $otu_id (sort keys %round_otu_size) {
	print $sizes_fh $otu_id, "\t", $round_otu_size{$otu_id}, "\n";
}
close $sizes_fh;

ReportingContractSidecar::write_sidecar_from_report(
	report_kind => 'otu_def_rpt',
	context => $context,
	report_path => $report_file,
	sidecar_path => $sidecar_file,
);

print STDERR "INFO: otu_marker_tokens seen=$marker_seen allowed=$marker_allowed rejected=$marker_rejected\n";
exit 0;
