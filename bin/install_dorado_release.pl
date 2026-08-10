#!/usr/bin/env perl

use strict;
use warnings;

use Digest::SHA qw();
use File::Basename qw(dirname);
use File::Copy qw(copy);
use File::Find qw(find);
use File::Path qw(make_path remove_tree);
use File::Spec;
use Getopt::Long qw(GetOptions);

my %opt;
GetOptions(
    'archive=s'          => \$opt{archive},
    'destination=s'      => \$opt{destination},
    'manifest=s'         => \$opt{manifest},
    'model-source-dir=s' => \$opt{model_source_dir},
) or die "ERROR: invalid Dorado release installer options\n";

for my $required (qw(destination manifest)) {
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

sub safe_relative_path {
    my ($path) = @_;
    return 0 if !defined $path || $path eq '' || File::Spec->file_name_is_absolute($path);
    return 0 if $path =~ m{(?:\A|/)\.\.(?:/|\z)};
    return 0 if $path =~ m{//} || $path =~ m{\A\./};
    return $path =~ m{\A[A-Za-z0-9_.@+/-]+\z} ? 1 : 0;
}

sub read_release_manifest {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "ERROR: cannot read Dorado release manifest '$path': $!\n";

    my %metadata;
    my $header;
    while (my $line = <$fh>) {
        chomp $line;
        if ($line =~ /\A# ([a-z_]+)=(.+)\z/) {
            die "ERROR: duplicate Dorado release metadata key '$1'\n"
                if exists $metadata{$1};
            $metadata{$1} = $2;
            next;
        }
        next if $line eq '';
        $header = $line;
        last;
    }
    die "ERROR: Dorado release manifest '$path' has no artifact header\n"
        if !defined $header;
    die "ERROR: Dorado release manifest must start with kind<TAB>artifact<TAB>sha256<TAB>bytes<TAB>mode<TAB>role\n"
        if $header ne "kind\tartifact\tsha256\tbytes\tmode\trole";

    for my $required (qw(release_id platform expected_version source_url)) {
        die "ERROR: Dorado release manifest is missing metadata '$required'\n"
            if !defined $metadata{$required} || $metadata{$required} eq '';
    }

    my %entries;
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        my ($kind, $artifact, $sha256, $bytes, $mode, $role, @extra) =
            split /\t/, $line, -1;
        die "ERROR: malformed Dorado release manifest row: $line\n"
            if !defined $kind || ($kind ne 'archive' && $kind ne 'runtime' && $kind ne 'model')
            || !safe_relative_path($artifact)
            || !defined $sha256 || $sha256 !~ /\A[0-9a-f]{64}\z/
            || !defined $bytes || $bytes !~ /\A[0-9]+\z/
            || !defined $mode || ($mode ne '-' && $mode !~ /\A0[0-7]{3}\z/)
            || !defined $role || $role eq '' || @extra;
        die "ERROR: duplicate Dorado release artifact: $artifact\n"
            if exists $entries{$artifact};
        die "ERROR: archive artifact must be a basename: $artifact\n"
            if $kind eq 'archive' && $artifact =~ m{/};
        die "ERROR: model artifact must be installed below models/: $artifact\n"
            if $kind eq 'model' && $artifact !~ m{\Amodels/};
        $entries{$artifact} = {
            kind   => $kind,
            sha256 => $sha256,
            bytes  => $bytes,
            mode   => $mode,
            role   => $role,
        };
    }
    close($fh) or die "ERROR: cannot close Dorado release manifest '$path': $!\n";

    my @archives = grep { $entries{$_}{kind} eq 'archive' } keys %entries;
    die "ERROR: Dorado release manifest must contain exactly one archive row\n"
        if @archives != 1;
    die "ERROR: Dorado release manifest is missing runtime artifact 'bin/dorado'\n"
        if !exists $entries{'bin/dorado'} || $entries{'bin/dorado'}{kind} ne 'runtime';

    for my $model (
        qw(
            dna_r10.4.1_e8.2_400bps_fast@v5.0.0
            dna_r10.4.1_e8.2_400bps_hac@v5.0.0
            dna_r10.4.1_e8.2_400bps_sup@v4.3.0
        )
    ) {
        my $config = "models/$model/config.toml";
        die "ERROR: Dorado release manifest is missing required model configuration '$config'\n"
            if !exists $entries{$config} || $entries{$config}{kind} ne 'model';
    }

    return (\%metadata, \%entries, $archives[0]);
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

sub manifest_data_artifacts {
    my ($entries_ref) = @_;
    return sort grep { $entries_ref->{$_}{kind} ne 'archive' } keys %{$entries_ref};
}

sub verify_destination {
    my ($destination, $entries_ref, $manifest_path) = @_;
    die "ERROR: Dorado release destination is not a regular directory: $destination\n"
        if !-d $destination || -l $destination;

    my %allowed = map { $_ => 1 } manifest_data_artifacts($entries_ref);
    $allowed{'release_manifest.tsv'} = 1;
    my @unexpected;
    find(
        {
            no_chdir => 1,
            wanted   => sub {
                my $path = $File::Find::name;
                if (-l $path) {
                    push @unexpected, File::Spec->abs2rel($path, $destination);
                    return;
                }
                return if $path eq $destination || -d $path;
                my $rel = File::Spec->abs2rel($path, $destination);
                push @unexpected, $rel if !$allowed{$rel};
            },
        },
        $destination
    );
    die "ERROR: Dorado release contains unexpected files: " . join(', ', sort @unexpected) . "\n"
        if @unexpected;

    verify_file(
        File::Spec->catfile($destination, 'release_manifest.tsv'),
        {
            sha256 => file_sha256($manifest_path),
            bytes  => -s $manifest_path,
        },
        'installed Dorado release manifest'
    );
    for my $artifact (manifest_data_artifacts($entries_ref)) {
        verify_file(
            File::Spec->catfile($destination, split m{/}, $artifact),
            $entries_ref->{$artifact},
            'Dorado release artifact'
        );
    }
}

sub copy_declared_file {
    my ($source, $destination, $entry_ref) = @_;
    verify_file($source, $entry_ref, 'Dorado source artifact');
    my $parent = dirname($destination);
    make_path($parent, { mode => 0755 }) if !-d $parent;
    copy($source, $destination)
        or die "ERROR: cannot copy Dorado artifact '$source' to '$destination': $!\n";
    if ($entry_ref->{mode} ne '-') {
        chmod(oct($entry_ref->{mode}), $destination)
            or die "ERROR: cannot set Dorado artifact mode on '$destination': $!\n";
    }
}

my $manifest = File::Spec->rel2abs($opt{manifest});
my $destination = File::Spec->rel2abs($opt{destination});
die "ERROR: Dorado release manifest not found: $manifest\n"
    if !-f $manifest || -l $manifest;

my ($metadata_ref, $entries_ref, $archive_name) = read_release_manifest($manifest);

if (-e $destination) {
    verify_destination($destination, $entries_ref, $manifest);
    print "$destination\n";
    exit 0;
}

for my $required (qw(archive model_source_dir)) {
    die "ERROR: --" . ($required =~ s/_/-/gr) . " is required for a new Dorado release installation\n"
        if !defined $opt{$required} || $opt{$required} eq '';
}

my $archive = File::Spec->rel2abs($opt{archive});
my $model_source_dir = File::Spec->rel2abs($opt{model_source_dir});
die "ERROR: Dorado archive not found: $archive\n" if !-f $archive || -l $archive;
die "ERROR: Dorado model source directory not found: $model_source_dir\n"
    if !-d $model_source_dir || -l $model_source_dir;
die "ERROR: Dorado archive name mismatch: manifest=$archive_name actual=" . (File::Basename::basename($archive)) . "\n"
    if File::Basename::basename($archive) ne $archive_name;
verify_file($archive, $entries_ref->{$archive_name}, 'Dorado archive');

my $parent = dirname($destination);
make_path($parent, { mode => 0755 }) if !-d $parent;
die "ERROR: Dorado release destination parent is not a directory: $parent\n" if !-d $parent;

my $tmp = "$destination.tmp.$$";
die "ERROR: temporary Dorado release path already exists: $tmp\n" if -e $tmp;
my $unpack = File::Spec->catdir($tmp, 'unpack');
my $release = File::Spec->catdir($tmp, 'release');
make_path($unpack, { mode => 0755 });
make_path($release, { mode => 0755 });
my $installed = 0;
END {
    remove_tree($tmp) if !$installed && defined $tmp && -d $tmp;
}

my $extract_status;
if ($archive =~ /\.zip\z/i) {
    $extract_status = system('unzip', '-q', $archive, '-d', $unpack);
} elsif ($archive =~ /\.(?:tar\.gz|tgz)\z/i) {
    $extract_status = system('tar', '-xzf', $archive, '-C', $unpack);
} else {
    die "ERROR: unsupported Dorado archive format: $archive\n";
}
die "ERROR: failed to extract Dorado release archive '$archive'\n"
    if $extract_status != 0;

opendir(my $unpack_dh, $unpack)
    or die "ERROR: cannot inspect extracted Dorado archive '$unpack': $!\n";
my @top = grep { $_ ne '.' && $_ ne '..' } readdir($unpack_dh);
closedir($unpack_dh);
die "ERROR: Dorado archive must contain exactly one top-level directory\n"
    if @top != 1;
my $archive_root = File::Spec->catdir($unpack, $top[0]);
die "ERROR: Dorado archive top-level entry is not a regular directory: $archive_root\n"
    if !-d $archive_root || -l $archive_root;

for my $artifact (manifest_data_artifacts($entries_ref)) {
    my $entry_ref = $entries_ref->{$artifact};
    my $source;
    if ($entry_ref->{kind} eq 'runtime') {
        $source = File::Spec->catfile($archive_root, split m{/}, $artifact);
    } else {
        (my $model_rel = $artifact) =~ s{\Amodels/}{};
        $source = File::Spec->catfile($model_source_dir, split m{/}, $model_rel);
    }
    my $target = File::Spec->catfile($release, split m{/}, $artifact);
    copy_declared_file($source, $target, $entry_ref);
}

copy($manifest, File::Spec->catfile($release, 'release_manifest.tsv'))
    or die "ERROR: cannot copy Dorado release manifest into '$release': $!\n";
chmod(0444, File::Spec->catfile($release, 'release_manifest.tsv'))
    or die "ERROR: cannot make installed Dorado manifest read-only: $!\n";
verify_destination($release, $entries_ref, $manifest);

if (!rename($release, $destination)) {
    my $rename_error = $!;
    if (-d $destination) {
        verify_destination($destination, $entries_ref, $manifest);
        $installed = 1;
        remove_tree($tmp);
        print "$destination\n";
        exit 0;
    }
    die "ERROR: cannot install Dorado release '$destination' from '$release' "
        . "(source_exists=" . (-d $release ? 1 : 0)
        . ", parent_exists=" . (-d $parent ? 1 : 0)
        . "): $rename_error\n";
}
$installed = 1;
remove_tree($tmp);

find(
    {
        no_chdir => 1,
        wanted   => sub {
            return if !-d $File::Find::name;
            chmod(0555, $File::Find::name)
                or die "ERROR: cannot make Dorado release directory read-only: $File::Find::name: $!\n";
        },
    },
    $destination
);

print "$destination\n";
exit 0;
