#!/usr/bin/env perl

use strict;
use warnings;

use Cwd qw(abs_path);
use Digest::SHA qw(sha256_hex);
use Fcntl qw(O_CREAT O_EXCL O_WRONLY);
use File::Path qw(make_path);
use File::Spec;
use Getopt::Long qw(GetOptions);
use Time::HiRes qw(time usleep);

my %opt = (
    policy                 => 'strict',
    lock_wait              => 300,
    profile                => '',
    taxonomy_dir           => '',
    nonncbi_memtax         => '',
    nonncbi_id2lineage     => '',
    verification_cache_dir => '',
    verification_mode      => 'cached',
);

GetOptions(
    'state-dir=s'                 => \$opt{state_dir},
    'reference-manifest=s'        => \$opt{reference_manifest},
    'reference-root=s'            => \$opt{reference_root},
    'taxonomy-data-dir=s'         => \$opt{taxonomy_dir},
    'taxonomy-release-manifest=s' => \$opt{taxonomy_release_manifest},
    'classifier-policy-version=s' => \$opt{classifier_policy_version},
    'scoring-policy-version=s'    => \$opt{scoring_policy_version},
    'targets=s'                   => \$opt{targets},
    'target-taxa=s'               => \$opt{target_taxa},
    'blast-filter-db=s'           => \$opt{blast_filter_db},
    'blast-db-specs=s'            => \$opt{blast_db_specs},
    'blast-taxdb=s'               => \$opt{blast_taxdb},
    'nonncbi-memtax=s'            => \$opt{nonncbi_memtax},
    'nonncbi-id2lineage=s'        => \$opt{nonncbi_id2lineage},
    'profile=s'                   => \$opt{profile},
    'policy=s'                    => \$opt{policy},
    'lock-wait=i'                 => \$opt{lock_wait},
    'verification-cache-dir=s'    => \$opt{verification_cache_dir},
    'verification-mode=s'         => \$opt{verification_mode},
) or die "ERROR: invalid state compatibility options\n";

my %required_option_name = (
    taxonomy_dir              => 'taxonomy-data-dir',
    taxonomy_release_manifest => 'taxonomy-release-manifest',
);
for my $required (
    qw(
        state_dir
        reference_manifest
        reference_root
        taxonomy_dir
        taxonomy_release_manifest
        classifier_policy_version
        scoring_policy_version
        targets
        target_taxa
        blast_filter_db
        blast_db_specs
        blast_taxdb
    )
) {
    my $option_name = $required_option_name{$required} // ($required =~ s/_/-/gr);
    die "ERROR: --$option_name is required\n"
        if !defined $opt{$required} || $opt{$required} eq '';
}

die "ERROR: --policy must be strict or adopt_legacy\n"
    if $opt{policy} ne 'strict' && $opt{policy} ne 'adopt_legacy';
die "ERROR: --lock-wait must be >= 1\n"
    if !defined $opt{lock_wait} || $opt{lock_wait} < 1;
die "ERROR: --verification-mode must be cached or full\n"
    if $opt{verification_mode} ne 'cached' && $opt{verification_mode} ne 'full';

my @owned_lock_dirs;
my @owned_temp_files;
END {
    unlink($_) for grep { defined($_) && -e $_ } reverse @owned_temp_files;
    rmdir($_) for grep { defined($_) && -d $_ } reverse @owned_lock_dirs;
}
$SIG{INT} = sub { die "ERROR: state compatibility validation interrupted by SIGINT\n" };
$SIG{TERM} = sub { die "ERROR: state compatibility validation interrupted by SIGTERM\n" };
$SIG{HUP} = sub { die "ERROR: state compatibility validation interrupted by SIGHUP\n" };

sub absolute_path {
    my ($path) = @_;
    return File::Spec->rel2abs($path);
}

sub file_sha256 {
    my ($path) = @_;
    open(my $fh, '<:raw', $path) or die "ERROR: cannot read '$path': $!\n";
    my $sha = Digest::SHA->new(256);
    $sha->addfile($fh);
    close($fh) or die "ERROR: cannot close '$path': $!\n";
    return $sha->hexdigest;
}

sub stat_signature {
    my ($path) = @_;
    my @st = Time::HiRes::stat($path);
    die "ERROR: cannot stat '$path': $!\n" if !@st;
    return {
        dev   => $st[0],
        ino   => $st[1],
        mode  => sprintf('%04o', $st[2] & 07777),
        uid   => $st[4],
        gid   => $st[5],
        size  => $st[7],
        mtime => $st[9],
        ctime => $st[10],
    };
}

sub signatures_equal {
    my ($left, $right) = @_;
    for my $key (qw(dev ino mode uid gid size mtime ctime)) {
        return 0 if !defined $left->{$key} || !defined $right->{$key};
        return 0 if "$left->{$key}" ne "$right->{$key}";
    }
    return 1;
}

sub hash_stable_file {
    my ($path) = @_;
    my $before = stat_signature($path);
    my $sha = file_sha256($path);
    my $after = stat_signature($path);
    die "ERROR: file changed while being verified: $path\n"
        if !signatures_equal($before, $after);
    return ($sha, $after);
}

sub trim {
    my ($value) = @_;
    $value = '' if !defined $value;
    $value =~ s/^\s+//;
    $value =~ s/\s+$//;
    return $value;
}

sub profile_uses_unsupported_container {
    my ($profile) = @_;
    for my $token (split /,/, lc(trim($profile // ''))) {
        $token = trim($token);
        return 1 if $token eq 'docker' || $token eq 'singularity';
    }
    return 0;
}

sub read_reference_manifest {
    my ($manifest_path, $reference_root) = @_;
    open(my $fh, '<', $manifest_path)
        or die "ERROR: cannot read reference manifest '$manifest_path': $!\n";
    my $header = <$fh>;
    die "ERROR: reference manifest '$manifest_path' is empty\n" if !defined $header;
    chomp $header;
    die "ERROR: reference manifest '$manifest_path' must start with artifact<TAB>sha256<TAB>role\n"
        if $header ne "artifact\tsha256\trole";

    my $row_count = 0;
    my %artifacts;
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        my ($artifact, $expected_sha, $role, @extra) = split /\t/, $line, -1;
        die "ERROR: malformed reference manifest row: $line\n"
            if !defined $artifact || $artifact eq ''
            || !defined $expected_sha || $expected_sha !~ /\A[0-9a-f]{64}\z/
            || !defined $role || $role eq '' || @extra;
        die "ERROR: absolute artifact paths are not allowed in reference manifest: $artifact\n"
            if File::Spec->file_name_is_absolute($artifact);
        die "ERROR: parent traversal is not allowed in reference manifest: $artifact\n"
            if grep { $_ eq '..' } File::Spec->splitdir($artifact);
        die "ERROR: duplicate artifact in reference manifest: $artifact\n"
            if exists $artifacts{$artifact};
        my $artifact_path = File::Spec->catfile($reference_root, $artifact);
        die "ERROR: reference artifact not found: $artifact_path\n" if !-f $artifact_path;
        $artifacts{$artifact} = {
            expected_sha => $expected_sha,
            role         => $role,
            path         => absolute_path($artifact_path),
        };
        $row_count++;
    }
    close($fh);
    die "ERROR: reference manifest '$manifest_path' contains no artifacts\n" if $row_count == 0;
    return \%artifacts;
}

sub read_taxonomy_release_manifest {
    my ($manifest_path) = @_;
    open(my $fh, '<', $manifest_path)
        or die "ERROR: cannot read taxonomy release manifest '$manifest_path': $!\n";
    my $header = <$fh>;
    die "ERROR: taxonomy release manifest '$manifest_path' is empty\n" if !defined $header;
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
    close($fh) or die "ERROR: cannot close taxonomy release manifest '$manifest_path': $!\n";

    my @archive = grep { $entries{$_}{kind} eq 'archive' } keys %entries;
    die "ERROR: taxonomy release manifest must contain exactly one archive row\n"
        if @archive != 1;
    my %required = map { $_ => 1 } qw(nodes.dmp names.dmp merged.dmp delnodes.dmp);
    for my $artifact (sort keys %required) {
        die "ERROR: taxonomy release manifest is missing data artifact '$artifact'\n"
            if !exists $entries{$artifact} || $entries{$artifact}{kind} ne 'data';
    }
    my @unexpected_data = grep {
        $entries{$_}{kind} eq 'data' && !$required{$_}
    } keys %entries;
    die "ERROR: taxonomy release manifest contains unexpected data artifacts: "
        . join(', ', sort @unexpected_data) . "\n"
        if @unexpected_data;
    return \%entries;
}

sub manifest_relative_path {
    my ($declared_path, $reference_root) = @_;
    my $path = trim($declared_path);
    my $relative = File::Spec->file_name_is_absolute($path)
        ? File::Spec->abs2rel($path, $reference_root)
        : $path;
    $relative = File::Spec->canonpath($relative);
    die "ERROR: configured reference path is outside the reference root: $declared_path\n"
        if grep { $_ eq '..' } File::Spec->splitdir($relative);
    $relative =~ s{\\}{/}g;
    $relative =~ s{^\./}{};
    return $relative;
}

sub require_manifest_exact {
    my ($artifacts_ref, $reference_root, $declared_path, $label) = @_;
    my $relative = manifest_relative_path($declared_path, $reference_root);
    die "ERROR: $label is not covered by the reference manifest: $declared_path\n"
        if !exists $artifacts_ref->{$relative};
}

sub require_manifest_components {
    my ($artifacts_ref, $reference_root, $declared_path, $extensions_ref, $label) = @_;
    my $relative = manifest_relative_path($declared_path, $reference_root);
    my @missing = grep { !exists $artifacts_ref->{$relative . $_} } @{$extensions_ref};
    die "ERROR: incomplete $label in reference manifest for '$declared_path'; missing: "
        . join(', ', @missing) . "\n"
        if @missing;
}

sub read_contract {
    my ($path) = @_;
    my %values;
    open(my $fh, '<', $path) or die "ERROR: cannot read state contract '$path': $!\n";
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        my ($key, $value) = split /\t/, $line, 2;
        next if !defined $key || $key eq '';
        $value = '' if !defined $value;
        $values{$key} = $value;
    }
    close($fh);
    return \%values;
}

sub state_has_material {
    my ($state_dir) = @_;
    return 0 if !-d $state_dir;
    opendir(my $dh, $state_dir) or die "ERROR: cannot inspect state directory '$state_dir': $!\n";
    my %ignored = map { $_ => 1 } qw(
        .
        ..
        .state_compatibility.lockdir
        state_compatibility_manifest.tsv
        run_started_utc.txt
    );
    while (my $entry = readdir($dh)) {
        next if $ignored{$entry};
        next if $entry eq '.DS_Store' || $entry =~ /^\._/;
        closedir($dh);
        return 1;
    }
    closedir($dh);
    return 0;
}

sub private_cache_dir_ok {
    my ($path) = @_;
    return 0 if !-d $path || -l $path;
    my @st = lstat($path);
    return 0 if !@st || $st[4] != $<;
    return (($st[2] & 0777) == 0700) ? 1 : 0;
}

sub private_cache_file_ok {
    my ($path) = @_;
    return 0 if !-f $path || -l $path;
    my @st = lstat($path);
    return 0 if !@st || $st[4] != $<;
    return (($st[2] & 0777) == 0600) ? 1 : 0;
}

sub reference_boundary_allows_cache {
    my ($paths_ref) = @_;
    # Intermediate-directory replacement changes file identity and is caught by
    # the cached signature; reference artifacts are then rehashed against their
    # manifest SHA-256 before reuse.
    for my $path (@{$paths_ref}) {
        my $signature = stat_signature($path);
        return 0 if (oct($signature->{mode}) & 0022) != 0;
    }
    return 1;
}

sub read_attestation_cache {
    my ($path, $expected_meta_ref) = @_;
    return undef if !private_cache_file_ok($path);
    open(my $fh, '<:raw', $path) or return undef;
    my @lines = <$fh>;
    close($fh);
    return undef if !@lines;

    my $checksum_line = pop @lines;
    chomp $checksum_line;
    my ($checksum_key, $stored_checksum) = split /\t/, $checksum_line, 2;
    return undef if !defined $stored_checksum || $checksum_key ne 'cache_sha256';
    my $body = join('', @lines);
    return undef if sha256_hex($body) ne $stored_checksum;

    my %meta;
    my %entries;
    for my $line (@lines) {
        chomp $line;
        my @fields = split /\t/, $line, -1;
        if ($fields[0] eq 'meta' && @fields == 3) {
            return undef if exists $meta{$fields[1]};
            $meta{$fields[1]} = $fields[2];
            next;
        }
        if ($fields[0] eq 'entry' && @fields == 15) {
            my (
                undef, $kind, $entry_path, $expected_sha, $verified_sha,
                $dev, $ino, $mode, $uid, $gid, $size, $mtime, $ctime,
                $verified_at, $role
            ) = @fields;
            return undef if exists $entries{$entry_path};
            $entries{$entry_path} = {
                kind         => $kind,
                path         => $entry_path,
                expected_sha => $expected_sha,
                verified_sha => $verified_sha,
                signature    => {
                    dev   => $dev,
                    ino   => $ino,
                    mode  => $mode,
                    uid   => $uid,
                    gid   => $gid,
                    size  => $size,
                    mtime => $mtime,
                    ctime => $ctime,
                },
                verified_at => $verified_at,
                role        => $role,
            };
            next;
        }
        return undef;
    }

    for my $key (keys %{$expected_meta_ref}) {
        return undef if !defined $meta{$key} || $meta{$key} ne $expected_meta_ref->{$key};
    }
    return {
        meta    => \%meta,
        entries => \%entries,
    };
}

sub acquire_lock_dir {
    my ($lock_dir, $wait_seconds) = @_;
    my $waited_ms = 0;
    my $wait_limit_ms = $wait_seconds * 1000;
    while (!mkdir($lock_dir, 0700)) {
        if ($!{EEXIST}) {
            die "ERROR: timed out waiting for lock '$lock_dir'\n"
                if $waited_ms >= $wait_limit_ms;
            usleep(100_000);
            $waited_ms += 100;
            next;
        }
        die "ERROR: cannot create lock '$lock_dir': $!\n";
    }
    push @owned_lock_dirs, $lock_dir;
}

sub release_lock_dir {
    my ($lock_dir) = @_;
    rmdir($lock_dir) or die "ERROR: cannot remove lock '$lock_dir': $!\n";
    @owned_lock_dirs = grep { $_ ne $lock_dir } @owned_lock_dirs;
}

sub write_attestation_cache_atomic {
    my ($path, $meta_ref, $entries_ref, $lock_wait) = @_;
    my $lock_dir = "$path.lockdir";
    acquire_lock_dir($lock_dir, $lock_wait);

    for my $entry (values %{$entries_ref}) {
        my $current = stat_signature($entry->{path});
        if (!signatures_equal($current, $entry->{signature})) {
            warn "WARNING: attestation cache not updated because a verified file changed: $entry->{path}\n";
            release_lock_dir($lock_dir);
            return;
        }
    }

    my $body = '';
    for my $key (sort keys %{$meta_ref}) {
        $body .= "meta\t$key\t$meta_ref->{$key}\n";
    }
    for my $entry_path (sort keys %{$entries_ref}) {
        my $entry = $entries_ref->{$entry_path};
        die "ERROR: tab or newline is not allowed in attestation paths\n"
            if $entry_path =~ /[\t\r\n]/;
        my $sig = $entry->{signature};
        $body .= join(
            "\t",
            'entry',
            $entry->{kind},
            $entry_path,
            $entry->{expected_sha},
            $entry->{verified_sha},
            @{$sig}{qw(dev ino mode uid gid size mtime ctime)},
            $entry->{verified_at},
            $entry->{role},
        ) . "\n";
    }
    my $content = $body . "cache_sha256\t" . sha256_hex($body) . "\n";
    my $tmp = "$path.tmp.$$";
    my $old_umask = umask(0077);
    sysopen(my $fh, $tmp, O_WRONLY | O_CREAT | O_EXCL, 0600) or do {
        my $error = $!;
        umask($old_umask);
        die "ERROR: cannot write attestation cache '$tmp': $error\n";
    };
    push @owned_temp_files, $tmp;
    if (!print {$fh} $content) {
        my $error = $!;
        close($fh);
        umask($old_umask);
        die "ERROR: cannot write attestation cache '$tmp': $error\n";
    }
    if (!close($fh)) {
        my $error = $!;
        umask($old_umask);
        die "ERROR: cannot close attestation cache '$tmp': $error\n";
    }
    umask($old_umask);
    rename($tmp, $path) or die "ERROR: cannot install attestation cache '$path': $!\n";
    @owned_temp_files = grep { $_ ne $tmp } @owned_temp_files;
    release_lock_dir($lock_dir);
}

sub prepare_private_cache_dir {
    my ($base_dir) = @_;
    return undef if !defined $base_dir || trim($base_dir) eq '';
    my $user_dir = File::Spec->catdir(absolute_path($base_dir), "uid-$<");
    if (!-e $user_dir) {
        my $old_umask = umask(0077);
        eval { make_path($user_dir, { mode => 0700 }); 1 } or do {
            my $error = $@ || $!;
            umask($old_umask);
            warn "WARNING: cannot create private attestation cache '$user_dir': $error; using full verification\n";
            return undef;
        };
        umask($old_umask);
    }
    if (!private_cache_dir_ok($user_dir)) {
        warn "WARNING: attestation cache is not a private user-owned 0700 directory: $user_dir; using full verification\n";
        return undef;
    }
    return $user_dir;
}

sub verify_with_attestation {
    my (%args) = @_;
    my $subjects_ref = $args{subjects};
    my $mode = $args{mode};
    my $cache_dir = $args{cache_dir};
    my $meta_ref = $args{meta};
    my $lock_wait = $args{lock_wait};
    my $cache_allowed = $args{cache_allowed};

    my $cache_path;
    my $cached_ref;
    my $cache_needs_write = ($mode eq 'full') ? 1 : 0;
    if ($cache_allowed && defined $cache_dir) {
        my $cache_key = sha256_hex(join("\0", map { "$_=$meta_ref->{$_}" } sort keys %{$meta_ref}));
        $cache_path = File::Spec->catfile($cache_dir, "attestation-$cache_key.tsv");
        $cached_ref = read_attestation_cache($cache_path, $meta_ref) if $mode eq 'cached';
    }

    my %verified;
    for my $subject (@{$subjects_ref}) {
        my $path = $subject->{path};
        my $expected_sha = $subject->{expected_sha} // '';
        my $expected_size = $subject->{expected_size} // '';
        my $cached_entry = $cached_ref ? $cached_ref->{entries}{$path} : undef;
        my $current_signature = stat_signature($path);
        my ($verified_sha, $verified_signature, $verified_at);

        if ($expected_size ne '' && "$current_signature->{size}" ne "$expected_size") {
            die "ERROR: $subject->{kind} artifact size mismatch for '$path': "
                . "manifest=$expected_size actual=$current_signature->{size}\n";
        }
        if (
            $mode eq 'cached'
            && $cached_entry
            && $cached_entry->{kind} eq $subject->{kind}
            && $cached_entry->{expected_sha} eq $expected_sha
            && signatures_equal($cached_entry->{signature}, $current_signature)
        ) {
            $verified_sha = $cached_entry->{verified_sha};
            $verified_signature = $current_signature;
            $verified_at = $cached_entry->{verified_at};
        } else {
            ($verified_sha, $verified_signature) = hash_stable_file($path);
            $verified_at = sprintf('%.6f', time());
            $cache_needs_write = 1;
        }

        if ($expected_sha ne '' && $verified_sha ne $expected_sha) {
            die "ERROR: $subject->{kind} artifact checksum mismatch for '$path': "
                . "manifest=$expected_sha actual=$verified_sha\n";
        }
        $verified{$path} = {
            %{$subject},
            verified_sha => $verified_sha,
            signature    => $verified_signature,
            verified_at  => $verified_at,
        };
    }

    if ($cache_allowed && defined $cache_path && $cache_needs_write) {
        write_attestation_cache_atomic($cache_path, $meta_ref, \%verified, $lock_wait);
    }
    return \%verified;
}

sub write_contract_atomic {
    my ($path, $values_ref, $adopted_legacy) = @_;
    my $tmp = "$path.tmp.$$";
    open(my $fh, '>', $tmp) or die "ERROR: cannot write '$tmp': $!\n";
    push @owned_temp_files, $tmp;
    for my $key (sort keys %{$values_ref}) {
        print {$fh} "$key\t$values_ref->{$key}\n";
    }
    print {$fh} "legacy_adopted\t", ($adopted_legacy ? 1 : 0), "\n";
    close($fh) or die "ERROR: failed to close '$tmp': $!\n";
    rename($tmp, $path) or die "ERROR: cannot install state contract '$path': $!\n";
    @owned_temp_files = grep { $_ ne $tmp } @owned_temp_files;
}

my $state_dir = absolute_path($opt{state_dir});
my $reference_manifest = absolute_path($opt{reference_manifest});
my $taxonomy_release_manifest = absolute_path($opt{taxonomy_release_manifest});
my $reference_root = abs_path($opt{reference_root});
die "ERROR: reference manifest not found: $reference_manifest\n"
    if !-f $reference_manifest;
die "ERROR: taxonomy release manifest not found: $taxonomy_release_manifest\n"
    if !-f $taxonomy_release_manifest;
die "ERROR: reference root not found: $opt{reference_root}\n"
    if !defined $reference_root || !-d $reference_root;
my $manifest_sha256 = file_sha256($reference_manifest);
my $taxonomy_release_manifest_sha256 = file_sha256($taxonomy_release_manifest);
my $manifest_artifacts_ref = read_reference_manifest($reference_manifest, $reference_root);
my $taxonomy_release_entries_ref = read_taxonomy_release_manifest($taxonomy_release_manifest);

my @last_extensions = qw(.prj .bck .des .sds .ssp .suf .tis);
my @blast_v5_extensions = qw(.ndb .nhr .nin .njs .not .nsq .ntf .nto);
require_manifest_components(
    $manifest_artifacts_ref, $reference_root, $opt{blast_filter_db}, \@last_extensions,
    'FAST/LAST filter database'
);
for my $prefix (split /\|/, $opt{blast_db_specs}, -1) {
    die "ERROR: --blast-db-specs contains an empty database prefix\n" if trim($prefix) eq '';
    require_manifest_components(
        $manifest_artifacts_ref, $reference_root, $prefix, \@blast_v5_extensions,
        'marker BLAST database'
    );
}
my $blast_taxdb_prefix = File::Spec->catfile(trim($opt{blast_taxdb}), 'taxdb');
require_manifest_components(
    $manifest_artifacts_ref, $reference_root, $blast_taxdb_prefix, [qw(.btd .bti)],
    'BLAST taxonomy database'
);
for my $taxonomy_file (split /\|/, $opt{nonncbi_memtax}, -1) {
    next if trim($taxonomy_file) eq '';
    require_manifest_exact(
        $manifest_artifacts_ref, $reference_root, $taxonomy_file,
        'taxonomy memory seed'
    );
}
if (trim($opt{nonncbi_id2lineage}) ne '') {
    require_manifest_exact(
        $manifest_artifacts_ref, $reference_root, $opt{nonncbi_id2lineage},
        'synthetic lineage map'
    );
}

my $taxonomy_dir = trim($opt{taxonomy_dir});
my $taxonomy_mode = 'pinned_explicit';
my $taxonomy_candidate = absolute_path($taxonomy_dir);
$taxonomy_dir = abs_path($taxonomy_candidate);
die "ERROR: TaxonKit taxonomy directory not found: $taxonomy_candidate ($taxonomy_mode)\n"
    if !defined $taxonomy_dir || !-d $taxonomy_dir;

my @taxonomy_files = qw(nodes.dmp names.dmp merged.dmp delnodes.dmp);
my @subjects;
for my $artifact (sort keys %{$manifest_artifacts_ref}) {
    my $entry = $manifest_artifacts_ref->{$artifact};
    push @subjects, {
        kind         => 'reference',
        path         => $entry->{path},
        expected_sha => $entry->{expected_sha},
        role         => $entry->{role},
    };
}
my %taxonomy_path;
for my $name (@taxonomy_files) {
    my $path = absolute_path(File::Spec->catfile($taxonomy_dir, $name));
    die "ERROR: required TaxonKit taxonomy file not found: $path\n" if !-f $path;
    $taxonomy_path{$name} = $path;
    push @subjects, {
        kind          => 'taxonomy',
        path          => $path,
        expected_sha  => $taxonomy_release_entries_ref->{$name}{sha256},
        expected_size => $taxonomy_release_entries_ref->{$name}{bytes},
        role          => $taxonomy_release_entries_ref->{$name}{role},
    };
}

my $cache_dir = prepare_private_cache_dir($opt{verification_cache_dir});
my @boundary_paths = ($reference_root, $taxonomy_dir, map { $_->{path} } @subjects);
my $cache_allowed = reference_boundary_allows_cache(\@boundary_paths);
if (!$cache_allowed && $opt{verification_mode} eq 'cached') {
    warn "WARNING: reference or taxonomy files are group/world writable; using full verification without attestation reuse\n";
}
my %attestation_meta = (
    schema_version                      => '1',
    reference_manifest_sha256           => $manifest_sha256,
    taxonomy_release_manifest_sha256 => $taxonomy_release_manifest_sha256,
    reference_root                      => $reference_root,
    taxonomy_data_dir                   => $taxonomy_dir,
);
my $verified_ref = verify_with_attestation(
    subjects      => \@subjects,
    mode          => $opt{verification_mode},
    cache_dir     => $cache_dir,
    meta          => \%attestation_meta,
    lock_wait     => $opt{lock_wait},
    cache_allowed => $cache_allowed,
);
my %taxonomy_hash = map {
    $_ => $verified_ref->{$taxonomy_path{$_}}{verified_sha}
} @taxonomy_files;

my %identity = (
    schema_version             => '1',
    reference_manifest_sha256 => $manifest_sha256,
    taxonomy_nodes_sha256      => $taxonomy_hash{'nodes.dmp'},
    taxonomy_names_sha256      => $taxonomy_hash{'names.dmp'},
    taxonomy_merged_sha256     => $taxonomy_hash{'merged.dmp'},
    taxonomy_delnodes_sha256   => $taxonomy_hash{'delnodes.dmp'},
    classifier_policy_version  => trim($opt{classifier_policy_version}),
    scoring_policy_version     => trim($opt{scoring_policy_version}),
    targets                    => trim($opt{targets}),
    target_taxa                => trim($opt{target_taxa}),
    blast_filter_db            => trim($opt{blast_filter_db}),
    blast_db_specs             => trim($opt{blast_db_specs}),
    blast_taxdb                => trim($opt{blast_taxdb}),
    nonncbi_memtax             => trim($opt{nonncbi_memtax}),
    nonncbi_id2lineage         => trim($opt{nonncbi_id2lineage}),
);

for my $key (qw(classifier_policy_version scoring_policy_version)) {
    die "ERROR: $key must not be empty\n" if $identity{$key} eq '';
}

my $canonical = join('', map { "$_\t$identity{$_}\n" } sort keys %identity);
my %contract = (
    %identity,
    reference_manifest_path => $reference_manifest,
    taxonomy_release_manifest_path   => $taxonomy_release_manifest,
    taxonomy_release_manifest_sha256 => $taxonomy_release_manifest_sha256,
    taxonomy_data_dir                => $taxonomy_dir,
    taxonomy_mode                    => $taxonomy_mode,
    execution_profile                => trim($opt{profile}),
);
$contract{contract_id} = sha256_hex($canonical);

make_path($state_dir) if !-d $state_dir;
my $lock_dir = File::Spec->catdir($state_dir, '.state_compatibility.lockdir');
acquire_lock_dir($lock_dir, $opt{lock_wait});

my $contract_path = File::Spec->catfile($state_dir, 'state_compatibility_manifest.tsv');
my $exit_code = 0;
my $error = '';
eval {
    if (-f $contract_path) {
        my $existing_ref = read_contract($contract_path);
        my $existing_profile = $existing_ref->{execution_profile} // '';
        if (profile_uses_unsupported_container($existing_profile)) {
            die "ERROR: rolling state at '$state_dir' was produced with unsupported "
                . "docker/singularity execution profile '$existing_profile'.\n"
                . "The inherited hecrp/nanortax image belonged to a different pipeline, "
                . "so this state cannot be resumed or migrated. Use a new --state_id or "
                . "--restart_mode reset and reanalyse with a supported runtime.\n";
        }
        my @changed;
        for my $key (sort(keys(%identity)), 'contract_id') {
            my $old = $existing_ref->{$key} // '';
            my $new = $contract{$key};
            push @changed, "$key: '$old' -> '$new'" if $old ne $new;
        }
        if (@changed) {
            die "ERROR: incompatible rolling state at '$state_dir'.\n"
                . join("\n", map { "  $_" } @changed)
                . "\nUse a new --state_id, --restart_mode reset, or the matching reference/taxonomy/classifier versions.\n";
        }
        my $old_taxonomy_dir = $existing_ref->{taxonomy_data_dir} // '';
        my $old_taxonomy_manifest_sha = $existing_ref->{taxonomy_release_manifest_sha256} // '';
        if (
            $old_taxonomy_dir ne $contract{taxonomy_data_dir}
            || $old_taxonomy_manifest_sha ne $contract{taxonomy_release_manifest_sha256}
        ) {
            my $migrated_from = $existing_ref->{taxonomy_migrated_from_data_dir} // '';
            $migrated_from = $old_taxonomy_dir
                if $migrated_from eq '' && $old_taxonomy_dir ne '' && $old_taxonomy_dir ne $contract{taxonomy_data_dir};
            $contract{taxonomy_migrated_from_data_dir} = $migrated_from if $migrated_from ne '';
            my $legacy_adopted = ($existing_ref->{legacy_adopted} // '') eq '1' ? 1 : 0;
            write_contract_atomic($contract_path, \%contract, $legacy_adopted);
        }
    } else {
        my $has_material = state_has_material($state_dir);
        if ($has_material && $opt{policy} ne 'adopt_legacy') {
            die "ERROR: legacy rolling state exists without a compatibility manifest at '$state_dir'.\n"
                . "Re-run once with --state_compatibility_policy adopt_legacy only after confirming that the state was produced by the current reference, taxonomy, classifier, and supported host runtime baseline. Never adopt state known or suspected to have been produced with the inherited docker/singularity NanoRTax image; use a new --state_id or --restart_mode reset instead.\n";
        }
        print STDERR "WARNING: adopting legacy rolling state at '$state_dir' under the current compatibility contract\n"
            if $has_material;
        write_contract_atomic($contract_path, \%contract, $has_material ? 1 : 0);
    }
    1;
} or do {
    $error = $@ || "ERROR: unknown state compatibility failure\n";
    $exit_code = 1;
};

eval { release_lock_dir($lock_dir); 1 } or do {
    if ($exit_code == 0) {
        $error = $@ || "ERROR: cannot remove state compatibility lock '$lock_dir'\n";
        $exit_code = 1;
    }
};

if ($exit_code != 0) {
    print STDERR $error;
    exit $exit_code;
}

print "$contract{contract_id}\n";
exit 0;
