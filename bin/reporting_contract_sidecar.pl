#!/usr/bin/perl

package ReportingContractSidecar;

use strict;
use warnings;
use Digest::SHA qw(sha1_hex);

our %CANONICAL_HEADERS = (
	demult_rpt => "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value",
	otu_def_rpt => "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\tOTU_id\tOTU_role",
);

sub canonical_header {
	my ($report_kind) = @_;
	die "ERROR: unknown report_kind '$report_kind'\n" if !exists $CANONICAL_HEADERS{$report_kind};
	return $CANONICAL_HEADERS{$report_kind};
}

sub canonical_header_sha1 {
	my ($report_kind) = @_;
	return sha1_hex(canonical_header($report_kind));
}

sub read_report_header {
	my ($report_path) = @_;
	open my $fh, '<', $report_path or die "ERROR: unable to open report '$report_path'\n";
	my $header = <$fh>;
	close $fh;
	die "ERROR: report '$report_path' is empty\n" if !defined $header;
	chomp $header;
	$header =~ s/\r$//;
	return $header;
}

sub count_report_rows {
	my ($report_path) = @_;
	open my $fh, '<', $report_path or die "ERROR: unable to open report '$report_path'\n";
	my $header = <$fh>;
	die "ERROR: report '$report_path' is empty\n" if !defined $header;
	my $count = 0;
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line eq '';
		$count++;
	}
	close $fh;
	return $count;
}

sub read_sidecar {
	my ($sidecar_path) = @_;
	open my $fh, '<', $sidecar_path or die "ERROR: unable to open sidecar '$sidecar_path'\n";
	my %meta;
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line eq '';
		die "ERROR: malformed sidecar row in '$sidecar_path'\n" if $line !~ /\A([^\t]+)\t([^\t]*)\z/;
		my ($key, $value) = ($1, $2);
		die "ERROR: duplicate sidecar key '$key' in '$sidecar_path'\n" if exists $meta{$key};
		$meta{$key} = $value;
	}
	close $fh;
	my @expected = qw(contract_version report_kind context row_count empty_contract header_sha1);
	for my $key (@expected) {
		die "ERROR: sidecar '$sidecar_path' missing key '$key'\n" if !exists $meta{$key};
	}
	for my $key (keys %meta) {
		die "ERROR: sidecar '$sidecar_path' contains unexpected key '$key'\n"
			if !grep { $_ eq $key } @expected;
	}
	return \%meta;
}

sub build_meta_from_report {
	my (%args) = @_;
	my $report_kind = $args{report_kind} // die "ERROR: missing report_kind\n";
	my $context = $args{context} // die "ERROR: missing context\n";
	my $report_path = $args{report_path} // die "ERROR: missing report_path\n";
	my $header = read_report_header($report_path);
	my $canonical = canonical_header($report_kind);
	die "ERROR: noncanonical header for '$report_path'\n" if $header ne $canonical;
	my $row_count = count_report_rows($report_path);
	return {
		contract_version => '1',
		report_kind => $report_kind,
		context => $context,
		row_count => "$row_count",
		empty_contract => $row_count == 0 ? 'genuinely_empty' : 'nonempty',
		header_sha1 => sha1_hex($header),
	};
}

sub write_sidecar_from_report {
	my (%args) = @_;
	my $sidecar_path = $args{sidecar_path} // die "ERROR: missing sidecar_path\n";
	my $meta = build_meta_from_report(%args);
	my $tmp = "$sidecar_path.tmp.$$";
	open my $fh, '>', $tmp or die "ERROR: unable to write sidecar '$tmp'\n";
	for my $key (qw(contract_version report_kind context row_count empty_contract header_sha1)) {
		print $fh $key, "\t", $meta->{$key}, "\n";
	}
	close $fh;
	rename $tmp, $sidecar_path or die "ERROR: unable to replace sidecar '$sidecar_path'\n";
	return $meta;
}

sub validate_report_and_sidecar {
	my (%args) = @_;
	my $report_kind = $args{report_kind} // die "ERROR: missing report_kind\n";
	my $context = $args{context} // die "ERROR: missing context\n";
	my $report_path = $args{report_path} // die "ERROR: missing report_path\n";
	my $sidecar_path = $args{sidecar_path} // die "ERROR: missing sidecar_path\n";
	my $allow_absent = $args{allow_absent} ? 1 : 0;
	my $report_exists = -e $report_path;
	my $sidecar_exists = -e $sidecar_path;
	if (!$report_exists && !$sidecar_exists) {
		die "ERROR: required report '$report_path' is missing\n" if !$allow_absent;
		return { state => 'absent' };
	}
	die "ERROR: report/sidecar skew for '$report_path' and '$sidecar_path'\n" if !$report_exists || !$sidecar_exists;
	my $meta = read_sidecar($sidecar_path);
	my $actual = build_meta_from_report(
		report_kind => $report_kind,
		context => $context,
		report_path => $report_path,
	);
	for my $key (qw(contract_version report_kind context row_count empty_contract header_sha1)) {
		die "ERROR: sidecar mismatch for '$report_path' key '$key'\n"
			if $meta->{$key} ne $actual->{$key};
	}
	die "ERROR: header hash mismatch for '$report_path'\n"
		if $meta->{header_sha1} ne canonical_header_sha1($report_kind);
	return {
		state => ($actual->{row_count} eq '0') ? 'empty' : 'nonempty',
		%{$actual},
	};
}

1;

package main;

use strict;
use warnings;

unless (caller) {
	my ($report_kind, $context, $report_path, $sidecar_path) = @ARGV;
	die "usage: $0 <report_kind> <context> <report_path> <sidecar_path>\n"
		if !defined $sidecar_path;
	ReportingContractSidecar::write_sidecar_from_report(
		report_kind => $report_kind,
		context => $context,
		report_path => $report_path,
		sidecar_path => $sidecar_path,
	);
}
