package CollapseIdentityMap;

use strict;
use warnings;

sub load {
  my ($path) = @_;
  die "ERROR: collapse identity path is empty\n" unless defined $path && $path ne '';
  die "ERROR: collapse identity '$path' is missing, empty, or unreadable\n"
    unless -f $path && -r $path && -s $path;
  open my $fh, '<', $path or die "ERROR: cannot open collapse identity '$path': $!\n";
  my $header = <$fh>;
  die "ERROR: collapse identity '$path' has no header\n" unless defined $header;
  chomp $header;
  my @columns = split /\t/, $header, -1;
  my %index;
  for my $i (0 .. $#columns) {
    die "ERROR: collapse identity '$path' has duplicate header '$columns[$i]'\n"
      if exists $index{$columns[$i]};
    $index{$columns[$i]} = $i;
  }
  for my $required (qw(sample_id marker_id suffix_resolution_mode unit_suffix_current unit_id_collapse)) {
    die "ERROR: collapse identity '$path' is missing $required header\n"
      unless exists $index{$required};
  }
  my %by_unit;
  my $line_number = 1;
  while (my $line = <$fh>) {
    ++$line_number;
    chomp $line;
    die "ERROR: collapse identity '$path' has malformed row $line_number\n"
      if $line =~ /\r$/ || $line eq '';
    my @fields = split /\t/, $line, -1;
    die "ERROR: collapse identity '$path' has malformed row $line_number\n"
      unless @fields == @columns;
    my ($sample, $marker, $mode, $suffix, $unit) =
      @fields[@index{qw(sample_id marker_id suffix_resolution_mode unit_suffix_current unit_id_collapse)}];
    for my $pair (['sample_id', $sample], ['unit_id_collapse', $unit]) {
      die "ERROR: collapse identity '$path' has empty or unsafe $pair->[0] at row $line_number\n"
        if $pair->[1] eq '' || $pair->[1] =~ /[\x00-\x20\x7f|\/]/
        || $pair->[1] =~ /^no_adapter(?:_\d+)?$/i;
    }
    die "ERROR: collapse identity '$path' has empty or unsafe marker/suffix at row $line_number\n"
      if $marker !~ /^[A-Za-z0-9_.-]+$/ || $suffix !~ /^[A-Za-z0-9_.-]+$/;
    die "ERROR: collapse identity '$path' has marker/suffix mismatch at row $line_number\n"
      if ($mode ne 'marker' && $mode ne 'fallback') || ($mode eq 'marker' && $marker ne $suffix);
    die "ERROR: collapse identity '$path' has unit/identity mismatch at row $line_number\n"
      unless $unit eq "${sample}_${suffix}";
    if (exists $by_unit{$unit}) {
      my $prior = $by_unit{$unit};
      die "ERROR: collapse identity '$path' has conflicting rows for unit '$unit'\n"
      if $prior->{sample} ne $sample || $prior->{marker} ne $marker
        || $prior->{mode} ne $mode || $prior->{suffix} ne $suffix;
      next;
    }
    $by_unit{$unit} = { sample => $sample, marker => $marker, mode => $mode, suffix => $suffix };
  }
  close $fh or die "ERROR: cannot close collapse identity '$path': $!\n";
  die "ERROR: collapse identity '$path' has no units\n" unless %by_unit;
  return \%by_unit;
}

sub sample_for_unit {
  my ($map, $unit) = @_;
  die "ERROR: collapse identity has no entry for unit '$unit'\n"
    unless exists $map->{$unit};
  return $map->{$unit}{sample};
}

1;
