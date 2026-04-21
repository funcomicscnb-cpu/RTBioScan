#!/usr/bin/perl

$blast_ann_otu=$ARGV[0];
$reads2consensus=$ARGV[1];
my $keep_no_adapter = 0;
my $pident_map = "";
my $blocked_otus = "";
if (defined $ARGV[2] && $ARGV[2] ne "") {
	my $flag = lc $ARGV[2];
	if ($flag eq 'keep_no_adapter' || $flag eq 'keep') {
		$keep_no_adapter = 1;
		$pident_map = $ARGV[3] // "";
		$blocked_otus = $ARGV[4] // "";
	} else {
		$pident_map = $ARGV[2];
		$blocked_otus = $ARGV[3] // "";
	}
}

my %pident;
if ($pident_map && -s $pident_map) {
	open my $PM, "<", $pident_map or die "I couldn't open $pident_map\n";
	while (<$PM>) {
		chomp;
		next unless /\S/;
		my ($id, $pid) = split /\t/;
		next unless defined $id && defined $pid;
		$pid += 0;
		$pident{$id} = $pid if !exists $pident{$id} || $pid > $pident{$id};
	}
	close $PM;
}

my %blocked;
if ($blocked_otus && -s $blocked_otus) {
	open my $BL, "<", $blocked_otus or die "I couldn't open $blocked_otus\n";
	while (<$BL>) {
		chomp;
		next unless /\S/;
		$blocked{$_} = 1;
	}
	close $BL;
}

open FILE, $blast_ann_otu or die "I couldn't open $blast_ann_otu\n";
my @lines;
my %groups;
while (<FILE>) {
	if (/^#/) {
		push @lines, {raw => $_, header => 1};
		next;
	} elsif (/no_adapter/ && !$keep_no_adapter) { next; }
	chomp;
	my $line = $_;
	my @tr = split /\t/;
	my @tr2 = split /\|/, $tr[0];

	my $read_id = $tr2[0];
	my $target  = $tr2[1] // 'NA';
	my $otu_id  = 'OTUB_NA';
	foreach my $f (@tr2){ if($f =~ /^OTUB_/){ $otu_id = $f; last; } }
	if ($otu_id =~ /^OTUB_[^-]+$/ && defined $target && $target ne '' && $target ne 'NA') {
		$otu_id = $otu_id . "-" . $target;
	}

	# Determine quality/model
	my $qual = 'hac';
	foreach my $f (@tr2){ if($f =~ /^(?:hac|hac_normal|hac_fixed|hac2sup|sup)$/){ $qual = $f; last; } }

	# Determine sample (adapter=...) if present, else set to no_adapter_1
	my $sample_val = 'no_adapter_1';
	foreach my $f (@tr2){
		if($f =~ /^adapter=(\S*)/){
			$sample_val = $1 eq '' ? 'no_adapter_1' : $1;
			last;
		}
	}

	my $pid = exists $pident{$read_id} ? $pident{$read_id} : 0;
	my $key = "$sample_val|$otu_id";
	my $idx = scalar @lines;
	push @lines, {
		raw => $line,
		tr => \@tr,
		tr2 => \@tr2,
		read_id => $read_id,
		target => $target,
		otu_id => $otu_id,
		sample => $sample_val,
		qual => $qual,
		pident => $pid,
		new_qual => $qual,
	};
	push @{ $groups{$key} }, $idx;
}
close FILE;

# Decide which reads to promote to sup per sample|OTU using pident
for my $key (keys %groups) {
	my ($sample_val, $otu_id) = split /\|/, $key, 2;
	my $sample_base = $sample_val;
	if ($sample_base =~ /^(.*)_\d+$/) { $sample_base = $1; }
	if ($blocked{"$sample_val\t$otu_id"} || $blocked{"$sample_base\t$otu_id"}) {
		for my $idx (@{ $groups{$key} }) {
			my $q = $lines[$idx]{qual} // '';
			if ($q eq "sup" || $q eq "hac2sup" || $q eq "hac_fixed") {
				$lines[$idx]{new_qual} = $q;
			} else {
				$lines[$idx]{new_qual} = "hac_fixed";
			}
		}
		next;
	}
	my @cand = grep {
		$lines[$_]{qual} eq "hac" || $lines[$_]{qual} eq "hac_normal"
	} @{ $groups{$key} };
	@cand = sort {
		$lines[$b]{pident} <=> $lines[$a]{pident}
		|| $lines[$a]{read_id} cmp $lines[$b]{read_id}
	} @cand;
	my $limit = $reads2consensus || 0;
	my %chosen;
	for (my $i=0; $i<@cand && $i<$limit; $i++) { $chosen{$cand[$i]} = 1; }
	for my $idx (@cand) {
		if ($chosen{$idx}) {
			$lines[$idx]{new_qual} = "hac2sup";
		} else {
			$lines[$idx]{new_qual} = "hac_fixed";
		}
	}
}

for my $rec (@lines) {
	if ($rec->{header}) { print $rec->{raw}; next; }
	my @tr = @{ $rec->{tr} };
	my @tr2 = @{ $rec->{tr2} };
	my $read_id = $rec->{read_id};
	my $target  = $rec->{target};
	my $otu_id  = $rec->{otu_id};
	my $sample_val = $rec->{sample};
	my $qual = $rec->{new_qual};

	# Recompose header preserving barcode/adapter tags and OTUB id
	my @tags = ();
	my $has_adapter = 0;
	foreach my $f (@tr2){
		if($f =~ /^(?:barcode=|adapter=)/){
			push @tags, $f;
			$has_adapter = 1 if $f =~ /^adapter=/;
		}
	}
	if(!$has_adapter){ push @tags, "adapter=$sample_val"; }
	my @new = ($read_id, $target, $qual, @tags);
	my $has_otu = 0; foreach my $f (@tr2){ $has_otu = 1 if $f =~ /^OTUB_/; }
	push @new, $otu_id if $has_otu;

	$"='|';
	$tr[0] = "@new";
	$"="\t";
	print "@tr\n";
}
