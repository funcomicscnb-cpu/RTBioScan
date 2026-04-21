#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);

my ($in, $seen, $out_new, $out_hash) = @ARGV;
die "usage: $0 in.fasta seen.tsv out_new.fasta out_new_hashes.tsv\n" unless @ARGV == 4;

my %seen;
if (-s $seen) {
  open my $S, "<", $seen or die "open $seen: $!";
  while (<$S>) {
    chomp;
    next unless length;
    my ($h) = split /\t/;
    $seen{$h} = 1 if defined $h && length $h;
  }
  close $S;
}

open my $IN,  "<", $in       or die "open $in: $!";
open my $OUT, ">", $out_new  or die "open $out_new: $!";
open my $H,   ">", $out_hash or die "open $out_hash: $!";

my ($hdr, $seq) = ("", "");
while (<$IN>) {
  chomp;
  if (/^>/) {
    if ($hdr && $seq ne "") {
      my $h = md5_hex(uc($seq));
      if (!$seen{$h}) {
        print $OUT $hdr, "\n", $seq, "\n";
        print $H $h, "\n";
        $seen{$h} = 1;
      }
    }
    $hdr = $_;
    $seq = "";
  } else {
    $seq .= $_;
  }
}
if ($hdr && $seq ne "") {
  my $h = md5_hex(uc($seq));
  if (!$seen{$h}) {
    print $OUT $hdr, "\n", $seq, "\n";
    print $H $h, "\n";
  }
}

close $IN;
close $OUT;
close $H;
