#!/usr/bin/env perl

use strict;
use warnings;

use Digest::SHA qw();
use File::Basename qw(dirname);
use File::Path qw(make_path remove_tree);
use File::Spec;
use Getopt::Long qw(GetOptions);

my %opt;
GetOptions(
    'archive=s'     => \$opt{archive},
    'destination=s' => \$opt{destination},
    'manifest=s'    => \$opt{manifest},
) or die "ERROR: invalid taxonomy release installer options\n";

for my $required (qw(archive destination manifest)) {
    die "ERROR: --" . ($required =~ s/_/-/gr) . " is required\n"
        if !defined $opt{$required} || $opt{$required} eq '';
}

sub file_sha256 {
    my ($path) = @_;
    open(my $fh, '<:raw', $path) or die "ERROR: cannot read '$path': $!\n";
    my $sha = Digest::SHA->new(256);
    $sha->addfile($fh);
    close($fh) or die "ERROR: cannot close '$path': $!\n";
    return $sha->hexdigest;
}

sub read_release_manifest {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "ERROR: cannot read taxonomy release manifest '$path': $!\n";
    my $header = <$fh>;
    die "ERROR: taxonomy release manifest '$path' is empty\n" if !defined $header;
    chomp $header;
    die "ERROR: taxonomy release manifest must start with kind<TAB>artifact<TAB>sha256<TAB>bytes<TAB>role\n"
        if $header ne "kind\tartifact\tsha256\tbytes\trole";

    my %entries;
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        my ($kind, $artifact, $sha256, $bytes, $role, @extra) = split /\t/, $line, -1;
        die "ERROR: malformed taxonomy release manifest row: $line\n"
            if !defined $kind || ($kind ne 'archive' && $kind ne 'data')
            || !defined $artifact || $artifact !~ /\A[A-Za-z0-9_.-]+\z/
            || !defined $sha256 || $sha256 !~ /\A[0-9a-f]{64}\z/
            || !defined $bytes || $bytes !~ /\A[0-9]+\z/
            || !defined $role || $role eq '' || @extra;
        die "ERROR: duplicate taxonomy release artifact: $artifact\n"
            if exists $entries{$artifact};
        $entries{$artifact} = {
            kind   => $kind,
            sha256 => $sha256,
            bytes  => $bytes,
            role   => $role,
        };
    }
    close($fh) or die "ERROR: cannot close taxonomy release manifest '$path': $!\n";

    my @archive = grep { $entries{$_}{kind} eq 'archive' } keys %entries;
    die "ERROR: taxonomy release manifest must contain exactly one archive row\n"
        if @archive != 1;
    for my $required (qw(nodes.dmp names.dmp merged.dmp delnodes.dmp)) {
        die "ERROR: taxonomy release manifest is missing data artifact '$required'\n"
            if !exists $entries{$required} || $entries{$required}{kind} ne 'data';
    }
    my @unexpected_data = grep {
        $entries{$_}{kind} eq 'data'
            && $_ ne 'nodes.dmp'
            && $_ ne 'names.dmp'
            && $_ ne 'merged.dmp'
            && $_ ne 'delnodes.dmp'
    } keys %entries;
    die "ERROR: taxonomy release manifest contains unexpected data artifacts: "
        . join(', ', sort @unexpected_data) . "\n"
        if @unexpected_data;

    return (\%entries, $archive[0]);
}

sub verify_file {
    my ($path, $entry_ref, $label) = @_;
    die "ERROR: $label is not a regular non-symlink file: $path\n"
        if !-f $path || -l $path;
    my $bytes = -s $path;
    die "ERROR: $label size mismatch for '$path': expected=$entry_ref->{bytes} actual=$bytes\n"
        if $bytes != $entry_ref->{bytes};
    my $actual_sha = file_sha256($path);
    die "ERROR: $label checksum mismatch for '$path': expected=$entry_ref->{sha256} actual=$actual_sha\n"
        if $actual_sha ne $entry_ref->{sha256};
}

sub verify_destination {
    my ($destination, $entries_ref) = @_;
    die "ERROR: taxonomy release destination is not a regular directory: $destination\n"
        if !-d $destination || -l $destination;
    for my $artifact (qw(nodes.dmp names.dmp merged.dmp delnodes.dmp)) {
        verify_file(
            File::Spec->catfile($destination, $artifact),
            $entries_ref->{$artifact},
            'taxonomy data artifact'
        );
    }
}

my $archive = File::Spec->rel2abs($opt{archive});
my $manifest = File::Spec->rel2abs($opt{manifest});
my $destination = File::Spec->rel2abs($opt{destination});
die "ERROR: taxonomy archive not found: $archive\n" if !-f $archive || -l $archive;
die "ERROR: taxonomy release manifest not found: $manifest\n" if !-f $manifest || -l $manifest;

my ($entries_ref, $archive_name) = read_release_manifest($manifest);
verify_file($archive, $entries_ref->{$archive_name}, 'taxonomy archive');

if (-e $destination) {
    verify_destination($destination, $entries_ref);
    print "$destination\n";
    exit 0;
}

my $parent = dirname($destination);
make_path($parent, { mode => 0755 }) if !-d $parent;
die "ERROR: taxonomy release destination parent is not a directory: $parent\n" if !-d $parent;

my $tmp = "$destination.tmp.$$";
die "ERROR: temporary taxonomy release path already exists: $tmp\n" if -e $tmp;
make_path($tmp, { mode => 0755 });
my $installed = 0;
END {
    remove_tree($tmp) if !$installed && defined $tmp && -d $tmp;
}

my @artifacts = qw(nodes.dmp names.dmp merged.dmp delnodes.dmp);
my $tar_status = system('tar', '-xzf', $archive, '-C', $tmp, @artifacts);
die "ERROR: failed to extract taxonomy release archive '$archive'\n" if $tar_status != 0;
verify_destination($tmp, $entries_ref);

for my $artifact (@artifacts) {
    chmod(0444, File::Spec->catfile($tmp, $artifact))
        or die "ERROR: cannot make taxonomy artifact read-only: $tmp/$artifact: $!\n";
}
rename($tmp, $destination)
    or die "ERROR: cannot install taxonomy release '$destination': $!\n";
$installed = 1;
chmod(0555, $destination)
    or die "ERROR: cannot make taxonomy release directory read-only: $destination: $!\n";

print "$destination\n";
exit 0;
