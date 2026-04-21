#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(:flock);

my ($round_ids_path, $ever_ids_path, $stats_path) = @ARGV;
if (!defined $round_ids_path || !defined $ever_ids_path) {
    die "usage: persist_read_ids_ever.pl <round_ids.list> <ever_ids.list> [stats_out]\n";
}

sub trim_text {
    my ($v) = @_;
    $v = '' unless defined $v;
    $v =~ s/^\s+|\s+$//g;
    return $v;
}

my %merged;
my $round_count = 0;
my $lock_path = "$ever_ids_path.lock";

open my $LOCK, '>>', $lock_path or die "open $lock_path: $!";
flock($LOCK, LOCK_EX) or die "flock $lock_path: $!";

if (-e $ever_ids_path && -s $ever_ids_path) {
    open my $EI, '<', $ever_ids_path or die "open $ever_ids_path: $!";
    while (my $line = <$EI>) {
        chomp $line;
        my $read_id = trim_text($line);
        next if $read_id eq '';
        my ($base) = split /\|/, $read_id, 2;
        next if !defined $base || $base eq '';
        $merged{$base} = 1;
    }
    close $EI;
}

if (-e $round_ids_path && -s $round_ids_path) {
    open my $RI, '<', $round_ids_path or die "open $round_ids_path: $!";
    while (my $line = <$RI>) {
        chomp $line;
        my $read_id = trim_text($line);
        next if $read_id eq '';
        my ($base) = split /\|/, $read_id, 2;
        next if !defined $base || $base eq '';
        $merged{$base} = 1;
        $round_count++;
    }
    close $RI;
}

my $tmp = "$ever_ids_path.tmp.$$";
open my $OUT, '>', $tmp or die "open $tmp: $!";
for my $read_id (sort keys %merged) {
    print {$OUT} "$read_id\n";
}
close $OUT;
rename $tmp, $ever_ids_path or die "rename $tmp -> $ever_ids_path: $!";

if (defined $stats_path && $stats_path ne '') {
    open my $ST, '>', $stats_path or die "open $stats_path: $!";
    print {$ST} "round_read_ids_count\t$round_count\n";
    print {$ST} "protected_read_ids_ever_count\t" . (scalar keys %merged) . "\n";
    close $ST;
}

close $LOCK;

exit 0;
