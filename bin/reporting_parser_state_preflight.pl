#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/reporting_contract_sidecar.pl";
require "$FindBin::Bin/reporting_parser_state_transaction.pl";

my ($state_dir, $barcode, $phase, $context, $round_demult_report, $round_demult_sidecar, $round_otu_report, $round_otu_sidecar) = @ARGV;
die "usage: $0 <state_dir> <barcode> <phase> <context> <round_demult_report> <round_demult_sidecar> <round_otu_report> <round_otu_sidecar>\n"
	if !defined $round_otu_sidecar;
die "ERROR: unsupported phase '$phase'\n" if $phase ne 'full_reports';

my $state_demult_report = "$state_dir/${barcode}_demult_rpt.txt";
my $state_demult_sidecar = "$state_dir/${barcode}_demult_rpt.contract.tsv";
my $state_otu_report = "$state_dir/${barcode}_otu_def_rpt.txt";
my $state_otu_sidecar = "$state_dir/${barcode}_otu_def_rpt.contract.tsv";
my $demult_bootstrap_sentinel = "$state_dir/${barcode}_demult_bootstrap.seeded";

ReportingParserStateTxn::recover_leftover_transaction_or_die(
	state_dir => $state_dir,
	barcode => $barcode,
	context => $context,
);

my $round_demult = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'demult_rpt',
	context => $context,
	report_path => $round_demult_report,
	sidecar_path => $round_demult_sidecar,
);
my $round_otu = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'otu_def_rpt',
	context => $context,
	report_path => $round_otu_report,
	sidecar_path => $round_otu_sidecar,
);

my $state_demult = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'demult_rpt',
	context => $context,
	report_path => $state_demult_report,
	sidecar_path => $state_demult_sidecar,
	allow_absent => 1,
);
my $state_otu = ReportingContractSidecar::validate_report_and_sidecar(
	report_kind => 'otu_def_rpt',
	context => $context,
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

if ($state_demult_absent && $state_otu_absent && !$sentinel_exists) {
	exit 0;
}

if (!$state_demult_absent && !$state_otu_absent && $sentinel_exists) {
	exit 0;
}

die "ERROR: full_reports boundary is in an invalid parser-state configuration\n";

exit 0;
