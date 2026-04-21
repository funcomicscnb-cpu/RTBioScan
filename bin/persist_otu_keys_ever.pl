#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(:flock);

my ($round_keys_path, $ever_keys_path, $stats_path) = @ARGV;
if (!defined $round_keys_path || !defined $ever_keys_path) {
    die "usage: persist_otu_keys_ever.pl <round_otu_keys.list> <ever_otu_keys.list> [stats_out]\n";
}

sub trim_text {
    my ($v) = @_;
    $v = '' unless defined $v;
    $v =~ s/^\s+|\s+$//g;
    return $v;
}

my %merged;
my $round_count = 0;
my $lock_path = "$ever_keys_path.lock";

open my $LOCK, '>>', $lock_path or die "open $lock_path: $!";
flock($LOCK, LOCK_EX) or die "flock $lock_path: $!";

if (-e $ever_keys_path && -s $ever_keys_path) {
    open my $EK, '<', $ever_keys_path or die "open $ever_keys_path: $!";
    while (my $line = <$EK>) {
        chomp $line;
        my $otu_key = trim_text($line);
        next if $otu_key eq '';
        $merged{$otu_key} = 1;
    }
    close $EK;
}

if (-e $round_keys_path && -s $round_keys_path) {
    open my $RK, '<', $round_keys_path or die "open $round_keys_path: $!";
    while (my $line = <$RK>) {
        chomp $line;
        my $otu_key = trim_text($line);
        next if $otu_key eq '';
        $merged{$otu_key} = 1;
        $round_count++;
    }
    close $RK;
}

my $tmp = "$ever_keys_path.tmp.$$";
open my $OUT, '>', $tmp or die "open $tmp: $!";
for my $otu_key (sort keys %merged) {
    print {$OUT} "$otu_key\n";
}
close $OUT;
rename $tmp, $ever_keys_path or die "rename $tmp -> $ever_keys_path: $!";

if (defined $stats_path && $stats_path ne '') {
    open my $ST, '>', $stats_path or die "open $stats_path: $!";
    print {$ST} "round_otu_keys_count\t$round_count\n";
    print {$ST} "protected_otu_keys_ever_count\t" . (scalar keys %merged) . "\n";
    close $ST;
}

close $LOCK;

exit 0;
