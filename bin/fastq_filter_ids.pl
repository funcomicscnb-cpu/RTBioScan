#!/usr/bin/env perl
use strict;
use warnings;

my ($in_fastq, $ids_file, $out_fastq, $out_stats) = @ARGV;
die "usage: $0 in.fastq ids.txt out.fastq out_stats.tsv\n" unless @ARGV == 4;

my %blocked;
if (defined $ids_file && -s $ids_file) {
	open my $ID, '<', $ids_file or die "open $ids_file: $!";
	while (my $line = <$ID>) {
		chomp $line;
		$line =~ s/^\s+|\s+$//g;
		next if $line eq '';
		my ($base) = split /\|/, $line;
		$base = '' unless defined $base;
		next if $base eq '';
		$blocked{$base} = 1;
	}
	close $ID;
}

my $reads_total = 0;
my $reads_pruned = 0;
my $reads_kept = 0;

open my $OUT, '>', $out_fastq or die "open $out_fastq: $!";

if (defined $in_fastq && -s $in_fastq) {
	open my $IN, '<', $in_fastq or die "open $in_fastq: $!";
	while (1) {
		my $h = <$IN>;
		last unless defined $h;
		my $s = <$IN>;
		my $p = <$IN>;
		my $q = <$IN>;
		last unless defined $q;
		chomp $h;
		my $rid = $h;
		$rid =~ s/^@//;
		$rid =~ s/\s.*$//;
		my ($base) = split /\|/, $rid;
		$base = '' unless defined $base;
		$reads_total++;
		if ($base ne '' && exists $blocked{$base}) {
			$reads_pruned++;
			next;
		}
		$reads_kept++;
		print {$OUT} $h, "\n";
		print {$OUT} $s if defined $s;
		print {$OUT} $p if defined $p;
		print {$OUT} $q if defined $q;
	}
	close $IN;
}

close $OUT;

open my $ST, '>', $out_stats or die "open $out_stats: $!";
print {$ST} "reads_total\t$reads_total\n";
print {$ST} "reads_pruned\t$reads_pruned\n";
print {$ST} "reads_kept\t$reads_kept\n";
print {$ST} "block_ids_total\t" . scalar(keys %blocked) . "\n";
close $ST;

exit 0;
