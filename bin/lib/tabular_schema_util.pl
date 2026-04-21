package TabularSchemaUtil;

use strict;
use warnings;

sub trim_text {
  my ($v) = @_;
  $v = '' unless defined $v;
  $v =~ s/^\s+|\s+$//g;
  return $v;
}

sub read_required_header {
  my ($fh, $source, @required) = @_;
  my $hdr = <$fh>;
  die "missing header row in $source\n" unless defined $hdr;
  chomp $hdr;
  $hdr =~ s/\r$//;

  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $key = trim_text($cols[$i]);
    $idx{$key} = $i if $key ne '';
  }

  for my $name (@required) {
    die "missing required column '$name' in $source\n" if !exists $idx{$name};
  }

  return (\@cols, \%idx);
}

1;
