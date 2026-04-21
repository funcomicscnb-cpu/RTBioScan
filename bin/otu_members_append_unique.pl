#!/usr/bin/env perl
use strict;
use warnings;

my ($members_file, $append_file, $seen_index) = @ARGV;
die "usage: $0 members.tsv append.tsv seen_index.tsv\n" unless @ARGV == 3;

sub parse_pair {
    my ($line) = @_;
    return if !defined $line;
    chomp $line;
    return if $line eq '';
    my @f = split /\t/, $line, 3;
    return if @f < 2 || $f[0] eq '' || $f[1] eq '';
    return ($f[0], $f[1]);
}

sub write_index_atomic {
    my ($path, $pairs_ref) = @_;
    my $tmp = "$path.tmp.$$";
    open my $OUT, '>', $tmp or die "open $tmp: $!";
    for my $k (sort keys %{$pairs_ref}) {
        print {$OUT} $k, "\n";
    }
    close $OUT or die "close $tmp: $!";
    rename $tmp, $path or die "rename $tmp -> $path: $!";
}

sub ensure_parent_dir {
    my ($path) = @_;
    my $dir = $path;
    $dir =~ s{/[^/]+$}{};
    return if $dir eq '' || -d $dir;
    my @parts = split m{/+}, $dir;
    my $cur = ($dir =~ m{^/}) ? '/' : '';
    for my $p (@parts) {
        next if $p eq '';
        $cur .= ($cur eq '/' ? '' : '/') . $p;
        mkdir $cur unless -d $cur;
    }
}

sub load_members_pairs {
    my ($path) = @_;
    my %pairs;
    return %pairs unless -f $path && -s $path;
    open my $IN, '<', $path or die "open $path: $!";
    while (my $line = <$IN>) {
        my ($frozen_id, $read_id) = parse_pair($line);
        next unless defined $frozen_id;
        $pairs{"$frozen_id\t$read_id"} = 1;
    }
    close $IN;
    return %pairs;
}

ensure_parent_dir($members_file);
ensure_parent_dir($seen_index);

open my $MBOOT, '>>', $members_file or die "open $members_file: $!";
close $MBOOT;

my %seen;
my $rebuild_index = 0;
if (!-f $seen_index || !-s $seen_index) {
    $rebuild_index = 1;
} else {
    open my $SIN, '<', $seen_index or die "open $seen_index: $!";
    while (my $line = <$SIN>) {
        chomp $line;
        next if $line eq '';
        my @f = split /\t/, $line, 3;
        if (@f < 2 || $f[0] eq '' || $f[1] eq '') {
            $rebuild_index = 1;
            last;
        }
        $seen{"$f[0]\t$f[1]"} = 1;
    }
    close $SIN;
}

if ($rebuild_index) {
    %seen = load_members_pairs($members_file);
    write_index_atomic($seen_index, \%seen);
}

my @append_lines;
if (-f $append_file && -s $append_file) {
    open my $AIN, '<', $append_file or die "open $append_file: $!";
    while (my $line = <$AIN>) {
        my ($frozen_id, $read_id) = parse_pair($line);
        next unless defined $frozen_id;
        my $k = "$frozen_id\t$read_id";
        next if $seen{$k};
        $seen{$k} = 1;
        push @append_lines, $line;
    }
    close $AIN;
}

if (@append_lines) {
    open my $MAPP, '>>', $members_file or die "open $members_file: $!";
    print {$MAPP} @append_lines;
    close $MAPP or die "close $members_file: $!";
    write_index_atomic($seen_index, \%seen);
} elsif ($rebuild_index) {
    # Keep index in sync even when nothing was appended.
    write_index_atomic($seen_index, \%seen);
}

exit 0;
