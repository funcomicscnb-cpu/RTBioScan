#!/usr/bin/perl


$clstr_file = $ARGV[0];
		$demultiplex_qc_report_file = $ARGV[1];
$round_dir=$ARGV[2];
$barcode_pipeline=$ARGV[3];
my @allowed_targets = @ARGV[4..$#ARGV];

$report_file=$barcode_pipeline."_otu_def_rpt.txt";
$round_members_file=$barcode_pipeline."_otu_members_round.tsv";
$round_sizes_file=$barcode_pipeline."_otu_sizes_round.tsv";
$"="\t";

my %allowed_target = ();
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
	return 0 unless scalar(keys %allowed_target);
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

$header_flag=1;
open FILE, $demultiplex_qc_report_file or die "I couldn't open $demultiplex_qc_report_file\n";
while(<FILE>)
{
	chomp;
	
	if ($header_flag)
	{
		
		@header_array=split/\t/;
		for($i=0;$i<@header_array;$i++)
		{
			$header{$header_array[$i]}=$i;
		}
#		$header_line="@header_array[0..3]\t$header_array[5]";
		$header_line="@header_array[0..7]\tidentity_scope\tidentity_value";
		$header_flag=0;
	}else
	{
		@tr=split/\t/;
		my $read_id_raw = $tr[$header{"read_id"}];
		my $identity_scope = exists($header{"identity_scope"}) ? $tr[$header{"identity_scope"}] : 'unknown';
		my $identity_value = exists($header{"identity_value"}) ? $tr[$header{"identity_value"}] : 'unknown';
		if (!defined($identity_scope) || $identity_scope eq '') { $identity_scope = 'unknown'; }
		if (!defined($identity_value) || $identity_value eq '') { $identity_value = 'unknown'; }
		my ($read_id_base) = split /\|/, $read_id_raw;
		# Demultiplex report may include marker/barcode tokens in read_id.
		# Use base UUID as key so it matches OTU member IDs.
		if (!exists $read_line{$read_id_base}) {
			$read_line{$read_id_base}="@tr[0..7]\t$identity_scope\t$identity_value";
		}
	}
}


open OUT_FILE, ">$report_file" or die "I couldn't open $report_file\n";

print OUT_FILE $header_line."\tOTU_id\tOTU_role\n";

open FILE, $clstr_file or die "I couldn't open $clstr_file\n";
$marker_seen = 0;
$marker_allowed = 0;
$marker_rejected = 0;
my %round_members = ();
my %round_otu_size = ();
while(<FILE>)
{
	if(/\>Cluster (\d+)/)
	{
		$otu=$1;
	}
		elsif(/\>(\S+)\.\.\.(?: (\S))?/)
		{
			my $full_id=$1;
			my @parts=split /\|/, $full_id;
			$read_id = $parts[0];
			my @scan_tokens = ();
			if ($#parts >= 1) {
				@scan_tokens = @parts[1..$#parts];
			}
			my ($target, $seen_marker, $rejected_marker) = first_allowed_target(@scan_tokens);
				$role = (defined($2) && $2 eq '*') ? 'REPRESENTATIVE' : 'MEMBER';
					$line = exists($read_line{$read_id}) ? $read_line{$read_id} : "$read_id\tNA\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunknown\tunknown";
					my $otu_id = "OTUB_$otu";
					if ($seen_marker) {
						$marker_seen++;
						if ($target ne '') {
							$marker_allowed++;
							$otu_id = $otu_id . "-" . $target;
						} elsif ($rejected_marker) {
							$marker_rejected++;
						}
					}
					print OUT_FILE $line."\t$otu_id\t$role\n";
					my $pair = $otu_id."\t".$read_id;
					if (!exists $round_members{$pair}) {
						$round_members{$pair}=1;
						$round_otu_size{$otu_id}++;
					}
				}
			}
close FILE;
close OUT_FILE;

open ROUND_MEM, ">$round_members_file" or die "I couldn't open $round_members_file\n";
print ROUND_MEM "otu_id\tread_id\n";
for my $pair (sort keys %round_members) {
	print ROUND_MEM $pair."\n";
}
close ROUND_MEM;

open ROUND_SIZE, ">$round_sizes_file" or die "I couldn't open $round_sizes_file\n";
print ROUND_SIZE "otu_id\tsize\n";
for my $otu_id (sort keys %round_otu_size) {
	print ROUND_SIZE $otu_id."\t".$round_otu_size{$otu_id}."\n";
}
close ROUND_SIZE;

print STDERR "INFO: otu_marker_tokens seen=$marker_seen allowed=$marker_allowed rejected=$marker_rejected\n";
