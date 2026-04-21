#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use FindBin;
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text           = \&TaxonUtil::trim_text;
*is_unassigned_taxon = \&TaxonUtil::is_unassigned_taxon;
*normalize_taxon     = \&TaxonUtil::normalize_taxon;
*marker_from_token   = \&TaxonUtil::marker_from_token;

my %opt = (
  top => 200,
);

GetOptions(
  \%opt,
  'mode=s',
  'blast=s',
  'otu-sizes-round=s',
  'out=s',
  'marker=s',
  'top=i',
) or die "Usage: $0 --mode consensus|otu --blast <path> --out <path> [--otu-sizes-round <path>] [--top N]\n";

my $mode = $opt{mode} // '';
my $blast_path = $opt{blast} // '';
my $out_path = $opt{out} // '';
my $top_n = defined $opt{top} ? $opt{top} + 0 : 200;
my $marker_filter = $opt{marker} // '';
if (defined $marker_filter && $marker_filter ne '') {
  $marker_filter = marker_from_token($marker_filter);
}

if ($mode ne 'consensus' && $mode ne 'otu') {
  die "--mode must be consensus or otu\n";
}
if ($blast_path eq '' || $out_path eq '') {
  die "--blast and --out are required\n";
}

sub header_index_fallback {
  my ($idx_ref, @names) = @_;
  for my $name (@names) {
    my $key = lc(trim_text($name));
    return $idx_ref->{$key} if exists $idx_ref->{$key};
  }
  return undef;
}

sub build_path {
  my (@ranks) = @_;
  my @parts;
  for my $rank (@ranks) {
    next if !defined $rank || is_unassigned_taxon($rank);
    push @parts, $rank;
  }
  return undef if !@parts;
  return join(';', 'Root', @parts);
}

sub write_output {
  my ($rows_ref, $path, $marker_filter) = @_;
  open my $OUT, '>', $path or die "Cannot write $path: $!\n";
  print $OUT "marker\tpath\tweight\n";
  for my $row (@{$rows_ref}) {
    if (defined $marker_filter && $marker_filter ne '' && $row->{marker} ne $marker_filter) {
      next;
    }
    print $OUT join("\t", $row->{marker}, $row->{path}, $row->{weight}), "\n";
  }
  close $OUT;
}

sub load_otu_sizes_round {
  my ($path) = @_;
  my %sizes;
  return \%sizes if !defined $path || $path eq '' || !-e $path || !-s $path;
  open my $FH, '<', $path or return \%sizes;
  my $header = <$FH>;
  if (!defined $header) {
    close $FH;
    return \%sizes;
  }
  chomp $header;
  my @cols = split /\t/, $header, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $size_idx = header_index_fallback(\%idx, 'size', 'reads', 'read_count');
  if (!defined $otu_idx || !defined $size_idx) {
    close $FH;
    return \%sizes;
  }
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f || $size_idx > $#f;
    my $otu = trim_text($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';
    my $size = trim_text($f[$size_idx]);
    next unless $size =~ /^\d+$/;
    $sizes{$otu} = 0 + $size;
  }
  close $FH;
  return \%sizes;
}

sub prep_consensus {
  my ($path) = @_;
  my %by_marker_path_ids;
  return [] if !-e $path || !-s $path;

  open my $FH, '<', $path or return [];
  my $header = <$FH>;
  if (!defined $header) {
    close $FH;
    return [];
  }
  chomp $header;
  my @cols = split /\t/, $header, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $cons_idx = header_index_fallback(\%idx, 'consensus_id');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $kingdom_idx = header_index_fallback(\%idx, 'kingdom', 'consensus_kingdom', 'otu_kingdom');
  my $phylum_idx = header_index_fallback(\%idx, 'phylum', 'consensus_phylum', 'otu_phylum');
  my $class_idx = header_index_fallback(\%idx, 'class', 'consensus_class', 'otu_class');
  my $order_idx = header_index_fallback(\%idx, 'order', 'consensus_order', 'otu_order');
  my $family_idx = header_index_fallback(\%idx, 'family', 'consensus_family', 'otu_family');
  my $genus_idx = header_index_fallback(\%idx, 'genus', 'consensus_genus', 'otu_genus');
  my $species_idx = header_index_fallback(\%idx, 'species', 'consensus_species', 'otu_species');

  return [] if !defined $cons_idx;

  my %seen;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    next if $cons_idx > $#f;
    my $cons_id = trim_text($f[$cons_idx]);
    next if $cons_id eq '' || $seen{$cons_id}++;

    my $marker_token = (defined $marker_idx && $marker_idx <= $#f) ? $f[$marker_idx] : $cons_id;
    my $marker = marker_from_token($marker_token);
    my $kingdom = (defined $kingdom_idx && $kingdom_idx <= $#f) ? normalize_taxon($f[$kingdom_idx]) : undef;
    my $phylum = (defined $phylum_idx && $phylum_idx <= $#f) ? normalize_taxon($f[$phylum_idx]) : undef;
    my $class = (defined $class_idx && $class_idx <= $#f) ? normalize_taxon($f[$class_idx]) : undef;
    my $order = (defined $order_idx && $order_idx <= $#f) ? normalize_taxon($f[$order_idx]) : undef;
    my $family = (defined $family_idx && $family_idx <= $#f) ? normalize_taxon($f[$family_idx]) : undef;
    my $genus = (defined $genus_idx && $genus_idx <= $#f) ? normalize_taxon($f[$genus_idx]) : undef;
    my $species = (defined $species_idx && $species_idx <= $#f) ? normalize_taxon($f[$species_idx]) : undef;
    my $path_str = build_path($kingdom, $phylum, $class, $order, $family, $genus, $species);
    next if !defined $path_str;
    $by_marker_path_ids{$marker}{$path_str}{$cons_id} = 1;
  }
  close $FH;

  my @rows;
  for my $marker (sort keys %by_marker_path_ids) {
    my @paths;
    for my $path (keys %{$by_marker_path_ids{$marker}}) {
      my $weight = scalar keys %{$by_marker_path_ids{$marker}{$path}};
      push @paths, { marker => $marker, path => $path, weight => $weight };
    }
    @paths = sort {
      $b->{weight} <=> $a->{weight} || $a->{path} cmp $b->{path}
    } @paths;
    if (@paths > $top_n) {
      @paths = @paths[0 .. ($top_n - 1)];
    }
    push @rows, @paths;
  }
  return \@rows;
}

sub prep_otu {
  my ($path, $otu_sizes_path) = @_;
  my $size_map = load_otu_sizes_round($otu_sizes_path);
  my %best;
  my %best_perc;
  my %read_ids;

  return [] if !-e $path || !-s $path;
  open my $FH, '<', $path or return [];
  my $header = <$FH>;
  if (!defined $header) {
    close $FH;
    return [];
  }
  chomp $header;
  my @cols = split /\t/, $header, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $perc_idx = header_index_fallback(\%idx, 'perc_id');
  my $kingdom_idx = header_index_fallback(\%idx, 'otu_kingdom', 'kingdom');
  my $phylum_idx = header_index_fallback(\%idx, 'otu_phylum', 'phylum');
  my $class_idx = header_index_fallback(\%idx, 'otu_class', 'class');
  my $order_idx = header_index_fallback(\%idx, 'otu_order', 'order');
  my $family_idx = header_index_fallback(\%idx, 'otu_family', 'family');
  my $genus_idx = header_index_fallback(\%idx, 'otu_genus', 'genus');
  my $species_idx = header_index_fallback(\%idx, 'otu_species', 'species');
  my $read_idx = header_index_fallback(\%idx, 'read_id');

  return [] if !defined $otu_idx;

  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = trim_text($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';

    my $perc = (defined $perc_idx && $perc_idx <= $#f) ? trim_text($f[$perc_idx]) : '';
    my $perc_num = ($perc ne '' ? $perc + 0 : undef);
    my $prev = $best_perc{$otu};
    if (!defined $prev || (defined $perc_num && $perc_num > $prev)) {
      my $marker_token = (defined $marker_idx && $marker_idx <= $#f) ? $f[$marker_idx] : $otu;
      my $marker = marker_from_token($marker_token);
      my $kingdom = (defined $kingdom_idx && $kingdom_idx <= $#f) ? normalize_taxon($f[$kingdom_idx]) : undef;
      my $phylum = (defined $phylum_idx && $phylum_idx <= $#f) ? normalize_taxon($f[$phylum_idx]) : undef;
      my $class = (defined $class_idx && $class_idx <= $#f) ? normalize_taxon($f[$class_idx]) : undef;
      my $order = (defined $order_idx && $order_idx <= $#f) ? normalize_taxon($f[$order_idx]) : undef;
      my $family = (defined $family_idx && $family_idx <= $#f) ? normalize_taxon($f[$family_idx]) : undef;
      my $genus = (defined $genus_idx && $genus_idx <= $#f) ? normalize_taxon($f[$genus_idx]) : undef;
      my $species = (defined $species_idx && $species_idx <= $#f) ? normalize_taxon($f[$species_idx]) : undef;
      $best{$otu} = {
        marker => $marker,
        kingdom => $kingdom,
        phylum => $phylum,
        class => $class,
        order => $order,
        family => $family,
        genus => $genus,
        species => $species,
      };
      $best_perc{$otu} = defined $perc_num ? $perc_num : -1;
    }

    if (!defined $size_map->{$otu} && defined $read_idx && $read_idx <= $#f) {
      my $rid = trim_text($f[$read_idx]);
      $rid =~ s/\|.*$//;
      $read_ids{$otu}{$rid} = 1 if $rid ne '';
    }
  }
  close $FH;

  my %agg;
  for my $otu (keys %best) {
    my $weight = defined $size_map->{$otu}
      ? $size_map->{$otu}
      : (exists $read_ids{$otu} ? scalar keys %{$read_ids{$otu}} : undef);
    next if !defined $weight || $weight <= 0;
    my $row = $best{$otu};
    my $path_str = build_path(
      $row->{kingdom},
      $row->{phylum},
      $row->{class},
      $row->{order},
      $row->{family},
      $row->{genus},
      $row->{species}
    );
    next if !defined $path_str;
    my $marker = $row->{marker} // 'OTHER';
    $agg{$marker}{$path_str} += $weight;
  }

  my @rows;
  for my $marker (sort keys %agg) {
    my @paths;
    for my $path (keys %{$agg{$marker}}) {
      my $weight = $agg{$marker}{$path} // 0;
      next if $weight <= 0;
      push @paths, { marker => $marker, path => $path, weight => $weight };
    }
    @paths = sort {
      $b->{weight} <=> $a->{weight} || $a->{path} cmp $b->{path}
    } @paths;
    if (@paths > $top_n) {
      @paths = @paths[0 .. ($top_n - 1)];
    }
    push @rows, @paths;
  }
  return \@rows;
}

my $rows_ref = $mode eq 'consensus'
  ? prep_consensus($blast_path)
  : prep_otu($blast_path, $opt{'otu-sizes-round'});

write_output($rows_ref, $out_path, $marker_filter);

exit 0;
