#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);

my ($in, $out_map, $out_counts) = @ARGV;
die "usage: $0 in.fasta out_hash_map.tsv out_hash_counts.tsv\n" unless @ARGV == 3;

open my $IN, '<', $in or die "open $in: $!";
open my $OM, '>', $out_map or die "open $out_map: $!";
open my $OC, '>', $out_counts or die "open $out_counts: $!";

my (%counts, $hdr, $seq);
($hdr, $seq) = ("", "");
while (<$IN>) {
  chomp;
  if (/^>/) {
    if ($hdr && $seq ne "") {
      my $h = md5_hex(uc($seq));
      my $id = $hdr;
      $id =~ s/^>//;
      $id =~ s/\s.*$//;
      # Keep full FASTA identifier; downstream member tables use full headers.
      print $OM join("\t", $id, $h), "\n";
      $counts{$h}++;
    }
    $hdr = $_;
    $seq = "";
  } else {
    $seq .= $_;
  }
}
if ($hdr && $seq ne "") {
  my $h = md5_hex(uc($seq));
  my $id = $hdr;
  $id =~ s/^>//;
  $id =~ s/\s.*$//;
  # Keep full FASTA identifier; downstream member tables use full headers.
  print $OM join("\t", $id, $h), "\n";
  $counts{$h}++;
}

for my $h (sort keys %counts) {
  print $OC join("\t", $h, $counts{$h}), "\n";
}

close $IN;
close $OM;
close $OC;
