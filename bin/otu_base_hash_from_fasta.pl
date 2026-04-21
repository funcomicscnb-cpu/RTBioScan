#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);

my ($in_fasta, $out_tsv) = @ARGV;
die "usage: $0 in.fasta out_base_hash.tsv\n" unless @ARGV == 2;

open my $IN,  '<', $in_fasta or die "open $in_fasta: $!";
open my $OUT, '>', $out_tsv  or die "open $out_tsv: $!";

my ($hdr, $seq) = ('', '');

sub emit_row {
  my ($h, $s, $fh) = @_;
  return unless defined $h && defined $s && length $h && length $s;
  my $id = $h;
  $id =~ s/^>//;
  $id =~ s/\s.*$//;
  my $base = $id;
  $base =~ s/\|.*$//;
  return unless length $base;
  print $fh $base, "\t", md5_hex(uc($s)), "\n";
}

while (<$IN>) {
  chomp;
  s/\r$//;
  if (/^>/) {
    emit_row($hdr, $seq, $OUT) if length $hdr && length $seq;
    $hdr = $_;
    $seq = '';
  } else {
    $seq .= $_ if length $hdr;
  }
}
emit_row($hdr, $seq, $OUT) if length $hdr && length $seq;

close $IN;
close $OUT;

exit 0;
