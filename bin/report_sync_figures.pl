#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use File::Copy qw(copy);
use File::Path qw(make_path);

my %opt;
GetOptions(
  'fig-list=s' => \$opt{fig_list},
  'barcode=s'  => \$opt{barcode},
  'src-dir=s'  => \$opt{src_dir},
  'out-dir=s'  => \$opt{out_dir},
  'asset-dir=s'=> \$opt{asset_dir},
) or die "invalid arguments\n";

for my $req (qw(barcode src_dir)) {
  die "missing required --$req\n" unless defined $opt{$req} && $opt{$req} ne '';
}
die "missing required --out-dir or --asset-dir\n"
  unless (defined $opt{out_dir} && $opt{out_dir} ne '') || (defined $opt{asset_dir} && $opt{asset_dir} ne '');

my $fig_list = $opt{fig_list};
my $barcode  = $opt{barcode};
my $src_dir  = $opt{src_dir};
my $out_dir  = $opt{out_dir};
my $asset_dir = (defined $opt{asset_dir} && $opt{asset_dir} ne '') ? $opt{asset_dir} : "$out_dir/report_assets";
my $manifest = "$asset_dir/.copied_manifest.tsv";
my $MF;

make_path($asset_dir);

sub expand_name {
  my ($pattern) = @_;
  my $name = $pattern;
  $name =~ s/\{barcode\}/$barcode/g;
  return $name;
}

sub copy_asset {
  my ($filename) = @_;
  return 0 unless defined $filename && $filename ne '';
  my $src = "$src_dir/$filename";
  my $dst = "$asset_dir/$filename";
  return 0 unless -f $src;
  if (!copy($src, $dst)) {
    warn "WARN: failed to copy $src -> $dst\n";
    return 0;
  }
  print {$MF} "$filename\n";
  return 1;
}

my %copied;
open $MF, '>', $manifest or die "open $manifest: $!";

if (defined $fig_list && $fig_list ne '' && -s $fig_list) {
  open my $IN, '<', $fig_list or die "open $fig_list: $!";
  while (my $line = <$IN>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if $line =~ /^\s*#/;
    my ($id, $pattern, $title, $desc, $section, $order) = split /\t/, $line, 6;
    next unless defined $pattern && $pattern ne '';
    my $filename = expand_name($pattern);
    if (copy_asset($filename)) {
      $copied{$filename} = 1;
      my $pdf_name = $filename;
      $pdf_name =~ s/\.png$/.pdf/i;
      copy_asset($pdf_name) if $pdf_name ne $filename;
    }
  }
  close $IN;
}

opendir my $DH, $src_dir or exit 0;
while (my $entry = readdir $DH) {
  next unless $entry =~ /\.(png|pdf)$/i;
  next if $copied{$entry};
  copy_asset($entry);
}
closedir $DH;
close $MF;

exit 0;
