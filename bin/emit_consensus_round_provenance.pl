#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use File::Glob qw(bsd_glob GLOB_NOSORT);
use FindBin;
require "$FindBin::Bin/lib/consensus_id_util.pl";

my %opt;
GetOptions(
  'consensus-dir=s' => \$opt{consensus_dir},
  'round-barcode=s' => \$opt{round_barcode},
  'out=s'           => \$opt{out},
) or die "invalid arguments\n";

for my $req (qw(consensus_dir round_barcode out)) {
  die "missing required --$req\n" unless defined $opt{$req} && $opt{$req} ne '';
}

my %warned;
sub warn_once {
  my ($msg) = @_;
  return if !defined $msg || $msg eq '';
  return if $warned{$msg}++;
  warn "$msg\n";
}

sub trim_text {
  my ($v) = @_;
  $v = '' unless defined $v;
  $v =~ s/^\s+|\s+$//g;
  return $v;
}

sub parse_consensus_headers {
  my ($fasta) = @_;
  my @rows;

  open my $FH, '<', $fasta or return @rows;
  while (my $line = <$FH>) {
    next unless $line =~ /^>/;
    chomp $line;
    $line =~ s/^>//;
    $line =~ s/\r$//;

    my $parsed = ConsensusIdUtil::parse_consensus_long_seq_id($line);
    next unless defined $parsed;

    my $sample = $parsed->{sample};
    my $cons_token = $parsed->{cons_token};

    my $otu_key = 'NA';
    if ($line =~ /\|OTU=([^|]+)/) {
      $otu_key = trim_text($1);
      $otu_key = 'NA' if $otu_key eq '';
    }

    push @rows, {
      sample       => $sample,
      otu_key      => $otu_key,
      consensus_id => $parsed->{consensus_id},
      cons_token   => $cons_token,
    };
  }
  close $FH;
  return @rows;
}

sub count_unique_reads {
  my ($path) = @_;
  if (!defined $path || $path eq '' || !-e $path) {
    warn_once("consensus_provenance_missing_reads_list:$path");
    return undef;
  }
  if (!-s $path) {
    warn_once("consensus_provenance_empty_reads_list:$path");
    return undef;
  }

  open my $FH, '<', $path or do {
    warn_once("consensus_provenance_open_failed:$path");
    return undef;
  };
  my %seen;
  while (my $line = <$FH>) {
    chomp $line;
    $line = trim_text($line);
    next if $line eq '';
    my ($read_id) = split /\|/, $line, 2;
    $read_id = trim_text($read_id);
    next if $read_id eq '';
    $seen{$read_id} = 1;
  }
  close $FH;

  return scalar keys %seen;
}

my $header = "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n";

my $tmp_out = $opt{out} . '.tmp';
open my $OUT, '>', $tmp_out or die "open $tmp_out: $!";
print {$OUT} $header;

if (-d $opt{consensus_dir}) {
  my @fasta_files = sort { $a cmp $b } bsd_glob("$opt{consensus_dir}/*/*_Merged_Consensus.fasta", GLOB_NOSORT);
  my %rows;

  for my $fasta (@fasta_files) {
    next unless -s $fasta;
    my ($sample_dir) = $fasta =~ m{^(.*?)/[^/]+_Merged_Consensus\.fasta$};
    next unless defined $sample_dir && $sample_dir ne '';

    my @cons_rows = parse_consensus_headers($fasta);
    for my $row (@cons_rows) {
      my $reads_list = "$sample_dir/OriginalReads/$row->{cons_token}_reads.list";
      my $reads_used_round = count_unique_reads($reads_list);
      my $reads_value = defined($reads_used_round) ? $reads_used_round : 'NA';
      my $key = join("\t", $row->{sample}, $row->{otu_key}, $row->{consensus_id});
      if (exists $rows{$key}) {
        my $prev = $rows{$key};
        if ($reads_value ne $prev) {
          warn_once("consensus_provenance_duplicate_key:$key:prev=$prev:new=$reads_value");
        } else {
          warn_once("consensus_provenance_duplicate_key:$key");
        }
      }
      if (!exists $rows{$key}) {
        $rows{$key} = $reads_value;
      } else {
        my $prev = $rows{$key};
        if ($prev eq 'NA') {
          $rows{$key} = $reads_value;
        } elsif ($reads_value eq 'NA') {
          # keep previous numeric value
        } elsif ($reads_value > $prev) {
          $rows{$key} = $reads_value;
        }
      }
    }
  }

  for my $key (sort keys %rows) {
    print {$OUT} join("\t", $opt{round_barcode}, $key, $rows{$key}), "\n";
  }
}

close $OUT;
rename $tmp_out, $opt{out} or die "rename $tmp_out -> $opt{out}: $!";

exit 0;
