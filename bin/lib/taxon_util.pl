package TaxonUtil;

use strict;
use warnings;

sub configured_marker_tokens {
  my $raw = $ENV{RTBIOSCAN_TARGET_TOKENS};
  return () if !defined $raw || $raw eq '';
  my @tokens;
  for my $part (split /\|/, $raw, -1) {
    my $marker = canonical_marker_token($part);
    push @tokens, $marker if defined $marker && $marker ne '';
  }
  return @tokens;
}

sub trim_text {
  my ($v) = @_;
  $v = '' unless defined $v;
  $v =~ s/^\s+|\s+$//g;
  return $v;
}

sub is_unassigned_taxon {
  my ($v) = @_;
  $v = trim_text($v);
  return 1 if $v eq '' || uc($v) eq 'NA' || lc($v) eq 'unassigned';
  return 0;
}

sub normalize_taxon {
  my ($v) = @_;
  return undef if is_unassigned_taxon($v);
  return trim_text($v);
}

sub canonical_marker_token {
  my ($v) = @_;
  $v = trim_text($v);
  return undef if $v eq '';
  my $candidate = uc($v);
  return 'ITS2' if $candidate eq 'ITS' || $candidate eq 'ITS1' || $candidate eq 'ITS2';
  return $candidate;
}

sub marker_filename_token {
  my ($marker) = @_;
  my $canonical = canonical_marker_token($marker);
  return '' if !defined $canonical || $canonical eq '';
  return 'COI' if $canonical eq 'COI';
  return 'ITS' if $canonical eq 'ITS2';
  $canonical =~ s/[^A-Z0-9._-]+/_/g;
  $canonical =~ s/_+/_/g;
  $canonical =~ s/^_+|_+$//g;
  return $canonical;
}

sub marker_slug {
  my ($marker) = @_;
  my $canonical = canonical_marker_token($marker);
  return '' if !defined $canonical || $canonical eq '';
  my $slug = lc($canonical);
  $slug =~ s/[^a-z0-9._-]+/_/g;
  $slug =~ s/_+/_/g;
  $slug =~ s/^_+|_+$//g;
  return $slug;
}

sub marker_from_token {
  my ($v) = @_;
  $v = trim_text($v);
  return undef if $v eq '';

  my @configured = configured_marker_tokens();
  for my $marker (@configured) {
    next if !defined $marker || $marker eq '';
    my $quoted = quotemeta($marker);
    return $marker if $v =~ /(?:^|[-_|])${quoted}(?:$|[-_|])/i;
  }

  return 'COI'  if $v =~ /-COI\b/i  || $v =~ /\bCOI\b/i;
  return 'ITS2' if $v =~ /-(?:ITS|ITS1|ITS2)\b/i || $v =~ /\b(?:ITS|ITS1|ITS2)\b/i;
  return 'OTHER';
}

# Returns true if $v looks like a valid numeric taxon ID (non-empty, not NA, all digits).
# NOTE: does NOT enforce >0; callers that need that check must add it explicitly.
sub is_numeric_taxid {
  my ($v) = @_;
  $v = trim_text($v);
  return 0 if $v eq '' || uc($v) eq 'NA';
  return $v =~ /^\d+$/ ? 1 : 0;
}

# Returns true if $v is a non-empty, non-NA, non-"unassigned" kingdom/single-taxon string.
sub is_assigned_kingdom {
  my ($v) = @_;
  $v = trim_text($v);
  return 0 if $v eq '' || uc($v) eq 'NA' || lc($v) eq 'unassigned';
  return 1;
}

# Returns true if semicolon-delimited $lineage contains at least one non-unassigned component.
sub is_assigned_lineage {
  my ($v) = @_;
  $v = trim_text($v);
  return 0 if $v eq '' || uc($v) eq 'NA';
  for my $part (split /;/, $v) {
    $part = trim_text($part);
    next if $part eq '';
    next if $part =~ /^[A-Za-z]__unassigned$/i;
    next if lc($part) eq 'unassigned';
    return 1;
  }
  return 0;
}


# R4-B predicates are separate from the legacy report/streak primitives above.
sub canonical_lineage {
  my ($text) = @_;
  $text = trim_text($text);
  return undef if is_unassigned_taxon($text);
  my @parts = split /;/, $text, -1;
  my @r = ('NA') x 7;
  if ($text !~ /(?:^|;)\s*[A-Za-z]__/) {
    return undef unless @parts == 7;
    for (0..6) { $r[$_] = normalize_taxon($parts[$_]) // 'NA'; }
  } else {
    my $last = -1;
    for my $part (@parts) {
      $part = trim_text($part);
      return undef unless $part =~ /\A([kpcofgs])__(.*)\z/i;
      my ($prefix,$name) = (lc($1),$2);
      my $i = index('kpcofgs',$prefix);
      return undef if $i <= $last;
      $last = $i; $r[$i] = normalize_taxon($name) // 'NA';
    }
  }
  return undef if grep { /[\t\r\n\x00]/ } @r;
  return \@r;
}
sub lineage_text {
  my ($r) = @_;
  my @prefix = qw(K p c o f g s);
  return join(';',map { $prefix[$_].'__'.$r->[$_] } 0..6);
}
sub lineage_depth {
  my ($r) = @_;
  return -1 unless $r;
  for (reverse 0..6) { return $_ if $r->[$_] ne 'NA'; }
  return -1;
}
sub valid_assignment_status {
  my ($s) = @_;
  return defined($s) && $s =~ /\A(?:ASSIGNED|AMBIGUOUS_TIE|NO_HIT|FILTERED_INELIGIBLE|REFERENCE_UNRESOLVED|REFERENCE_INCONSISTENT|COMPUTATION_FAILED)\z/;
}
sub assignment_from_row {
  my ($row,$level) = @_;
  die "invalid assignment level\n" unless $level =~ /\A(?:family|genus|species)\z/;
  if (exists $row->{status}) {
    die "unknown assignment status: $row->{status}\n" unless valid_assignment_status($row->{status});
    return 0 unless $row->{status} eq 'ASSIGNED' || $row->{status} eq 'AMBIGUOUS_TIE';
  }
  my @id = split /\|/, $row->{read_id} // '';
  my $marker = @id > 1 ? canonical_marker_token($id[1]) : '';
  my $otu = $row->{otu_id} // '';
  $otu = $1 if !$otu && ($row->{read_id} // '') =~ /\|(OTUB_[^|]+)/;
  return 0 if $otu =~ /\AOTUB_[^-]+-(.+)\z/ && $marker && canonical_marker_token($1) ne $marker;
  return 0 if exists($row->{marker}) && $marker && $row->{marker} ne $marker;
  my $r;
  if (exists $row->{lineage}) { $r = canonical_lineage($row->{lineage}); }
  else { $r = [map { normalize_taxon($row->{$_}) // 'NA' } qw(kingdom phylum class order family genus species)]; }
  return 0 unless $r;
  my %minimum = (family=>4,genus=>5,species=>6);
  return lineage_depth($r) >= $minimum{$level} ? 1 : 0;
}
sub assignment_rows {
  my ($path) = @_;
  return [] unless -e $path && -s $path;
  open my $f,'<',$path or die "open $path: $!\n";
  my $first=<$f>; close $f;
  if ($first =~ /^#RTB-R4B-TAXONOMY\t/) {
    require File::Basename;
    require File::Spec;
    my $module=File::Spec->catfile(File::Basename::dirname(__FILE__),'RTBioScan','OTURefineBlastreport.pm');
    require $module;
    return RTBioScan::OTURefineBlastreport::read_status_sidecar($path);
  }
  open $f,'<',$path or die "open $path: $!\n";
  $first=<$f>; $first =~ s/\r?\n\z//;
  my $delimiter = $first =~ /;/ && $first !~ /\t/ ? ';' : "\t";
  my @cols=split /\Q$delimiter\E/,$first,-1;
  my $header=$cols[0] =~ /\A#?(?:read_id|seq_id|long_read_id)\z/;
  my %alias=(seq_id=>'read_id',long_read_id=>'read_id',tax_id=>'taxid',otu_taxid=>'taxid',otu=>'otu_id',assignment_status=>'status');
  my @names=$header ? map { my $k=lc trim_text($_);$k=~s/^#+//;$k=~s/^otu_(kingdom|phylum|class|order|family|genus|species|lineage)$/$1/;$alias{$k}//$k } @cols : ('read_id','taxid',($delimiter eq "\t" ? 'lineage' : 'evalue'));
  my @rows;
  my $consume=sub {
    my ($line)=@_;$line =~ s/\r?\n\z//;return if $line !~ /\S/;
    my @v=split /\Q$delimiter\E/,$line,-1;my %row;
    for my $i (0..$#names) { $row{$names[$i]}=trim_text($v[$i]) if $i<@v; }
    push @rows,\%row if defined($row{read_id}) && $row{read_id} ne '';
  };
  $consume->($first) unless $header;
  while (my $line=<$f>) { $consume->($line); }
  close $f or die "close $path: $!\n";
  return \@rows;
}
sub read_assignment_sets {
  my ($path,$level)=@_;my (%seen,%assigned);
  for my $row (@{assignment_rows($path)}) {
    my ($base)=split /\|/,$row->{read_id};next unless defined($base) && $base ne '';
    $seen{$base}=1; $assigned{$base}=1 if assignment_from_row($row,$level);
  }
  return (\%seen,\%assigned);
}
sub atomic_text {
  my ($path,$text)=@_;
  require File::Temp;require File::Basename;
  my ($f,$tmp)=File::Temp::tempfile('.r4b-publish-XXXXXX',DIR=>File::Basename::dirname($path),UNLINK=>0);
  my $ok=eval { print {$f} $text or die "write $tmp: $!\n";close $f or die "close $tmp: $!\n";rename $tmp,$path or die "rename $path: $!\n";1 };
  my $err=$@;unlink $tmp if -e $tmp;die $err unless $ok;
}

1;
