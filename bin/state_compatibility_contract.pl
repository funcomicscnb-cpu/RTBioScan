#!/usr/bin/env perl

use strict;
use warnings;

use Cwd qw(abs_path);
use Digest::SHA qw(sha256_hex);
use Fcntl qw(O_CREAT O_EXCL O_WRONLY);
use File::Basename qw(dirname);
use File::Path qw(make_path);
use File::Spec;
use Getopt::Long qw(GetOptions);
use IO::Handle ();
use POSIX qw(strftime);
use Sys::Hostname qw(hostname);
use Time::HiRes qw(time usleep);

my %opt = (
    policy                 => 'strict',
    lock_wait              => 300,
    lock_stale_seconds     => 300,
    profile                => '',
    taxonomy_dir           => '',
    nonncbi_memtax         => '',
    nonncbi_id2lineage     => '',
    verification_cache_dir => '',
    verification_mode      => 'cached',
    contract_migration     => 'strict',
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
    'lock-stale-seconds=i'        => \$opt{lock_stale_seconds},
    'verification-cache-dir=s'    => \$opt{verification_cache_dir},
    'verification-mode=s'         => \$opt{verification_mode},
    'toolchain-fingerprint=s'     => \$opt{toolchain_fingerprint},
    'contract-migration=s'        => \$opt{contract_migration},
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
        toolchain_fingerprint
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
die "ERROR: --lock-stale-seconds must be >= 0\n"
    if !defined $opt{lock_stale_seconds} || $opt{lock_stale_seconds} < 0;
die "ERROR: --verification-mode must be cached or full\n"
    if $opt{verification_mode} ne 'cached' && $opt{verification_mode} ne 'full';
die "ERROR: --contract-migration must be strict or attest_v1\n"
    if $opt{contract_migration} ne 'strict' && $opt{contract_migration} ne 'attest_v1';

my @owned_lock_dirs;
my %owned_lock_token;
my %owned_lock_kind;
my @owned_temp_files;
END {
    unlink($_) for grep { defined($_) && -e $_ } reverse @owned_temp_files;
    for my $lock_dir (reverse @owned_lock_dirs) {
        eval { release_lock_dir($lock_dir, 1); 1 };
    }
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

sub read_toolchain_fingerprint {
    my ($path) = @_;
    open(my $fh, '<', $path)
        or die "ERROR: cannot read runtime toolchain fingerprint '$path': $!\n";
    my %values;
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        my ($key, $value, @extra) = split /\t/, $line, -1;
        die "ERROR: malformed runtime toolchain fingerprint row: $line\n"
            if !defined $key || $key !~ /\A[A-Za-z0-9_+-]+\z/
            || !defined $value || @extra;
        die "ERROR: duplicate runtime toolchain fingerprint field: $key\n"
            if exists $values{$key};
        $values{$key} = $value;
    }
    close($fh)
        or die "ERROR: cannot close runtime toolchain fingerprint '$path': $!\n";
    die "ERROR: runtime toolchain fingerprint schema is not supported\n"
        if ($values{fingerprint_schema_version} // '') ne '1';
    my $declared_id = delete $values{fingerprint_id};
    die "ERROR: runtime toolchain fingerprint has no valid fingerprint_id\n"
        if !defined $declared_id || $declared_id !~ /\A[0-9a-f]{64}\z/;
    my $canonical = join('', map { "$_\t$values{$_}\n" } sort keys %values);
    my $actual_id = sha256_hex($canonical);
    die "ERROR: runtime toolchain fingerprint checksum mismatch: "
        . "declared=$declared_id actual=$actual_id\n"
        if $declared_id ne $actual_id;
    $values{fingerprint_id} = $declared_id;
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
        next if $entry =~ /\A\.state_compatibility\.lockdir\.reclaim-[0-9a-f]{64}\z/;
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

sub normalized_lock_host {
    my $value = eval { hostname() } // '';
    $value = lc(trim($value));
    $value =~ s/\.\z//;
    return $value eq '' || $value =~ /[\t\r\n]/ ? 'unknown' : $value;
}

my $this_lock_host = normalized_lock_host();

sub process_start_identity {
    my ($pid) = @_;
    return '' if !defined $pid || $pid !~ /\A[0-9]+\z/ || $pid < 1;
    my $proc_stat = "/proc/$pid/stat";
    if (-r $proc_stat && open(my $proc_fh, '<', $proc_stat)) {
        my $line = <$proc_fh> // '';
        close($proc_fh);
        if ($line =~ /\A[0-9]+\s+\(.*\)\s+(.*)\z/s) {
            my @field = split /\s+/, trim($1);
            if (@field > 19 && $field[19] =~ /\A[0-9]+\z/) {
                my $boot_id = '';
                my $boot_id_path = '/proc/sys/kernel/random/boot_id';
                if (-r $boot_id_path && open(my $boot_fh, '<', $boot_id_path)) {
                    $boot_id = lc(trim(<$boot_fh> // ''));
                    close($boot_fh);
                    $boot_id = '' if $boot_id !~ /\A[0-9a-f-]+\z/;
                }
                return $boot_id eq ''
                    ? "proc:$field[19]"
                    : "proc:$boot_id:$field[19]";
            }
        }
    }
    return '';
}

sub new_lock_token {
    my ($lock_dir) = @_;
    return sha256_hex(join(
        "\0",
        $$,
        $this_lock_host,
        $lock_dir,
        sprintf('%.9f', time()),
        rand(),
    ));
}

sub parsed_process_start_identity {
    my ($value) = @_;
    return undef if !defined $value;
    return { ticks => $1, boot_id => undef }
        if $value =~ /\Aproc:([0-9]+)\z/;
    return { ticks => $2, boot_id => $1 }
        if $value =~ /\Aproc:([0-9a-f-]+):([0-9]+)\z/;
    return undef;
}

sub process_start_relationship {
    my ($recorded, $current) = @_;
    my $recorded_ref = parsed_process_start_identity($recorded);
    my $current_ref = parsed_process_start_identity($current);
    return 'unverifiable' if !defined $recorded_ref || !defined $current_ref;
    return 'reused' if $recorded_ref->{ticks} ne $current_ref->{ticks};
    return 'unverifiable'
        if !defined $recorded_ref->{boot_id} || !defined $current_ref->{boot_id};
    return $recorded_ref->{boot_id} eq $current_ref->{boot_id}
        ? 'same'
        : 'reused';
}

sub lock_owner_path {
    my ($lock_dir, $token) = @_;
    return File::Spec->catfile($lock_dir, ".owner-$token.tsv");
}

sub lock_dir_snapshot {
    my ($lock_dir, $expected_kind) = @_;
    my @dir_stat = lstat($lock_dir);
    if (!@dir_stat) {
        return undef if $!{ENOENT};
        die "ERROR: cannot inspect lock '$lock_dir': $!\n";
    }
    die "ERROR: lock path is not a regular directory: $lock_dir\n"
        if !-d _ || -l _;
    my $newest_epoch = $dir_stat[9];
    my $dh;
    if (!opendir($dh, $lock_dir)) {
        return undef if $!{ENOENT};
        die "ERROR: cannot inspect lock '$lock_dir': $!\n";
    }
    my @owner_name = sort grep { /\A\.owner-[0-9a-f]{64}\.tsv\z/ } readdir($dh);
    closedir($dh);
    for my $name (@owner_name) {
        my @owner_stat = lstat(File::Spec->catfile($lock_dir, $name));
        $newest_epoch = $owner_stat[9]
            if @owner_stat && $owner_stat[9] > $newest_epoch;
    }
    my $snapshot = {
        dev          => $dir_stat[0],
        ino          => $dir_stat[1],
        newest_epoch => $newest_epoch,
        valid        => 0,
    };
    return $snapshot if @owner_name != 1;

    my $owner_path = File::Spec->catfile($lock_dir, $owner_name[0]);
    return $snapshot if !-f $owner_path || -l $owner_path;
    open(my $fh, '<', $owner_path) or return $snapshot;
    my %value;
    my $valid = 1;
    while (my $line = <$fh>) {
        chomp $line;
        my ($key, $payload, @extra) = split /\t/, $line, -1;
        if (
            !defined $key || !defined $payload || @extra
            || exists $value{$key}
        ) {
            $valid = 0;
            last;
        }
        $value{$key} = $payload;
    }
    close($fh);
    (my $filename_token = $owner_name[0]) =~ s/\A\.owner-//;
    $filename_token =~ s/\.tsv\z//;
    $valid = 0
        if keys(%value) != 7
        || ($value{schema} // '') ne '1'
        || ($value{token} // '') ne $filename_token
        || $filename_token !~ /\A[0-9a-f]{64}\z/
        || ($value{pid} // '') !~ /\A[0-9]+\z/
        || ($value{pid} // 0) < 1
        || ($value{host} // '') eq ''
        || ($value{host} // '') =~ /[\t\r\n]/
        || ($value{process_start} // '') eq ''
        || ($value{process_start} // '') =~ /[\t\r\n]/
        || ($value{started_epoch} // '') !~ /\A[0-9]+\z/
        || ($value{kind} // '') ne $expected_kind;
    if ($valid) {
        $snapshot->{valid} = 1;
        $snapshot->{owner_path} = $owner_path;
        @{$snapshot}{qw(token pid host process_start started_epoch kind)} =
            @value{qw(token pid host process_start started_epoch kind)};
    }
    return $snapshot;
}

sub lock_stale_reason {
    my ($snapshot, $stale_seconds) = @_;
    my $same_host_unverifiable = 0;
    if (
        $snapshot->{valid}
        && $this_lock_host ne 'unknown'
        && $snapshot->{host} eq $this_lock_host
    ) {
        my $alive = kill(0, $snapshot->{pid}) || $!{EPERM};
        return "dead pid=$snapshot->{pid} host=$snapshot->{host}" if !$alive;
        my $current_start = process_start_identity($snapshot->{pid});
        my $relationship = process_start_relationship(
            $snapshot->{process_start}, $current_start
        );
        if ($relationship eq 'reused') {
            return "reused pid=$snapshot->{pid} host=$snapshot->{host}";
        }
        if ($relationship eq 'same') {
            return undef;
        }
        $same_host_unverifiable = 1;
    }
    return undef if $stale_seconds == 0;
    my $now = int(time());
    my $started = int($snapshot->{newest_epoch} // 0);
    return undef if $started < 1 || $started > $now;
    my $age = $now - $started;
    return undef if $age < $stale_seconds;
    return "same-host live pid=$snapshot->{pid} has unverifiable process "
        . "identity age=${age}s ttl=${stale_seconds}s"
        if $same_host_unverifiable;
    return $snapshot->{valid}
        ? "foreign owner host=$snapshot->{host} age=${age}s ttl=${stale_seconds}s"
        : "missing or malformed owner metadata age=${age}s ttl=${stale_seconds}s";
}

sub reclaim_generation_path {
    my ($lock_dir) = @_;
    return File::Spec->catfile($lock_dir, '.reclaim-generation.tsv');
}

sub read_reclaim_generation {
    my ($lock_dir, $expected_kind) = @_;
    my $path = reclaim_generation_path($lock_dir);
    my @path_stat = lstat($path);
    if (!@path_stat) {
        return undef if $!{ENOENT};
        die "ERROR: cannot inspect reclaim generation '$path': $!\n";
    }
    die "ERROR: reclaim generation is not a regular file: $path\n"
        if !-f _ || -l _;
    my $fh;
    if (!open($fh, '<', $path)) {
        return undef if $!{ENOENT};
        die "ERROR: cannot read reclaim generation '$path': $!\n";
    }
    my %value;
    while (my $line = <$fh>) {
        chomp $line;
        my ($key, $payload, @extra) = split /\t/, $line, -1;
        die "ERROR: malformed reclaim generation '$path'\n"
            if !defined $key || !defined $payload || @extra
            || exists $value{$key};
        $value{$key} = $payload;
    }
    close($fh)
        or die "ERROR: cannot close reclaim generation '$path': $!\n";
    die "ERROR: malformed reclaim generation '$path'\n"
        if keys(%value) != 3
        || ($value{schema} // '') ne '1'
        || ($value{token} // '') !~ /\A[0-9a-f]{64}\z/
        || ($value{kind} // '') ne $expected_kind;
    return {
        path  => $path,
        token => $value{token},
        kind  => $value{kind},
    };
}

sub install_reclaim_generation {
    my ($lock_dir, $snapshot, $lock_kind) = @_;
    my $existing_ref = read_reclaim_generation($lock_dir, $lock_kind);
    return $existing_ref if defined $existing_ref;

    my $token = new_lock_token("$lock_dir:reclaim-generation");
    my $claim_path = reclaim_generation_path($lock_dir);
    my $tmp = File::Spec->catfile(
        $lock_dir, ".reclaim-generation-$token.tmp"
    );
    my $content = join(
        '',
        "schema\t1\n",
        "token\t$token\n",
        "kind\t$lock_kind\n",
    );
    my $fh;
    if (!sysopen($fh, $tmp, O_WRONLY | O_CREAT | O_EXCL, 0600)) {
        return undef if $!{ENOENT};
        die "ERROR: cannot create reclaim generation '$tmp': $!\n";
    }
    if (!print {$fh} $content) {
        my $error = $!;
        close($fh);
        unlink($tmp);
        die "ERROR: cannot write reclaim generation '$tmp': $error\n";
    }
    if (!$fh->flush()) {
        my $error = $!;
        close($fh);
        unlink($tmp);
        die "ERROR: cannot flush reclaim generation '$tmp': $error\n";
    }
    if (!$fh->sync()) {
        my $error = $!;
        close($fh);
        unlink($tmp);
        die "ERROR: cannot sync reclaim generation '$tmp': $error\n";
    }
    if (!close($fh)) {
        my $error = $!;
        unlink($tmp);
        die "ERROR: cannot close reclaim generation '$tmp': $error\n";
    }

    my @current_stat = lstat($lock_dir);
    my $current_missing = !@current_stat && $!{ENOENT};
    my $current_error = @current_stat ? '' : "$!";
    if (
        !@current_stat
        || $current_stat[0] != $snapshot->{dev}
        || $current_stat[1] != $snapshot->{ino}
    ) {
        unlink($tmp);
        return undef if $current_missing;
        return undef if @current_stat;
        die "ERROR: cannot inspect stale lock '$lock_dir': $current_error\n";
    }

    my $linked = link($tmp, $claim_path);
    my $link_exists = !$linked && $!{EEXIST};
    my $link_missing = !$linked && $!{ENOENT};
    my $link_error = $linked ? '' : "$!";
    unlink($tmp);
    die "ERROR: cannot install reclaim generation '$claim_path': $link_error\n"
        if !$linked && !$link_exists && !$link_missing;
    return undef if !$linked && $link_missing;
    return read_reclaim_generation($lock_dir, $lock_kind);
}

sub same_reclaim_generation {
    my ($left_ref, $right_ref) = @_;
    return 0 if !defined $left_ref || !defined $right_ref;
    return $left_ref->{token} eq $right_ref->{token};
}

sub reclaim_stale_lock {
    my ($lock_dir, $stale_seconds, $lock_kind) = @_;
    my $snapshot = lock_dir_snapshot($lock_dir, $lock_kind);
    return 0 if !defined $snapshot;

    my $generation_ref = read_reclaim_generation($lock_dir, $lock_kind);
    my $reason;
    if (defined $generation_ref) {
        $reason = "recorded reclaim generation=$generation_ref->{token}";
    } else {
        $reason = lock_stale_reason($snapshot, $stale_seconds);
        return 0 if !defined $reason;
        $generation_ref = install_reclaim_generation(
            $lock_dir, $snapshot, $lock_kind
        );
        return 0 if !defined $generation_ref;
    }

    my @source_stat = lstat($lock_dir);
    return 0 if !@source_stat && $!{ENOENT};
    die "ERROR: cannot inspect stale lock '$lock_dir': $!\n"
        if !@source_stat;
    die "ERROR: lock path is not a regular directory: $lock_dir\n"
        if !-d _ || -l _;
    return 0
        if $source_stat[0] != $snapshot->{dev}
        || $source_stat[1] != $snapshot->{ino};
    my $current_generation_ref = read_reclaim_generation(
        $lock_dir, $lock_kind
    );
    return 0
        if !same_reclaim_generation(
            $generation_ref, $current_generation_ref
        );

    my $quarantine = "$lock_dir.reclaim-$generation_ref->{token}";
    if (rename($lock_dir, $quarantine)) {
        my @moved_stat = lstat($quarantine);
        die "ERROR: cannot inspect stale lock quarantine '$quarantine': $!\n"
            if !@moved_stat;
        die "ERROR: stale lock changed while being quarantined: $lock_dir\n"
            if $moved_stat[0] != $snapshot->{dev}
            || $moved_stat[1] != $snapshot->{ino};
        my $moved_generation_ref = read_reclaim_generation(
            $quarantine, $lock_kind
        );
        die "ERROR: stale lock quarantine lost its reclaim generation: "
            . "$quarantine\n"
            if !same_reclaim_generation(
                $generation_ref, $moved_generation_ref
            );
        warn "WARNING: reclaiming stale $lock_kind ($reason) at $lock_dir\n";
        return 1;
    }

    my $rename_error = "$!";
    my $completed_ref = read_reclaim_generation($quarantine, $lock_kind);
    if (same_reclaim_generation($generation_ref, $completed_ref)) {
        return -e $lock_dir ? 0 : 1;
    }
    my @current_stat = lstat($lock_dir);
    return 0 if !@current_stat && $!{ENOENT};
    return 0
        if @current_stat
        && ($current_stat[0] != $snapshot->{dev}
            || $current_stat[1] != $snapshot->{ino});
    die "ERROR: cannot quarantine stale lock '$lock_dir': $rename_error\n";
}

sub write_lock_owner {
    my ($lock_dir, $token, $lock_kind) = @_;
    my $owner_path = lock_owner_path($lock_dir, $token);
    my $owner_tmp = "$owner_path.tmp";
    my $process_start = process_start_identity($$);
    $process_start = 'unavailable' if $process_start eq '';
    my $content = join(
        '',
        "schema\t1\n",
        "token\t$token\n",
        "pid\t$$\n",
        "host\t$this_lock_host\n",
        "process_start\t$process_start\n",
        "started_epoch\t", int(time()), "\n",
        "kind\t$lock_kind\n",
    );
    sysopen(my $fh, $owner_tmp, O_WRONLY | O_CREAT | O_EXCL, 0600)
        or die "ERROR: cannot create lock owner metadata '$owner_tmp': $!\n";
    if (!print {$fh} $content) {
        my $error = $!;
        close($fh);
        unlink($owner_tmp);
        die "ERROR: cannot write lock owner metadata '$owner_tmp': $error\n";
    }
    if (!close($fh)) {
        my $error = $!;
        unlink($owner_tmp);
        die "ERROR: cannot close lock owner metadata '$owner_tmp': $error\n";
    }
    rename($owner_tmp, $owner_path)
        or do {
            my $error = $!;
            unlink($owner_tmp);
            die "ERROR: cannot install lock owner metadata '$owner_path': $error\n";
        };
}

sub acquire_lock_dir {
    my ($lock_dir, $wait_seconds, $stale_seconds, $lock_kind) = @_;
    my $waited_ms = 0;
    my $wait_limit_ms = $wait_seconds * 1000;
    while (1) {
        if (mkdir($lock_dir, 0700)) {
            my $token = new_lock_token($lock_dir);
            eval { write_lock_owner($lock_dir, $token, $lock_kind); 1 } or do {
                my $error = $@ || "ERROR: cannot initialize lock '$lock_dir'\n";
                my $owner_path = lock_owner_path($lock_dir, $token);
                unlink($owner_path);
                unlink("$owner_path.tmp");
                rmdir($lock_dir);
                die $error;
            };
            $owned_lock_token{$lock_dir} = $token;
            $owned_lock_kind{$lock_dir} = $lock_kind;
            push @owned_lock_dirs, $lock_dir;
            return $token;
        }
        my $lock_exists = $!{EEXIST};
        my $mkdir_error = "$!";
        die "ERROR: cannot create lock '$lock_dir': $mkdir_error\n"
            if !$lock_exists;
        next if reclaim_stale_lock($lock_dir, $stale_seconds, $lock_kind);
        die "ERROR: timed out waiting for lock '$lock_dir'\n"
            if $waited_ms >= $wait_limit_ms;
        usleep(100_000);
        $waited_ms += 100;
    }
}

sub assert_lock_owned {
    my ($lock_dir) = @_;
    my $token = $owned_lock_token{$lock_dir};
    my $lock_kind = $owned_lock_kind{$lock_dir};
    die "ERROR: current process does not own lock '$lock_dir'\n"
        if !defined $token || !defined $lock_kind;
    my $reclaim_path = reclaim_generation_path($lock_dir);
    my @reclaim_stat = lstat($reclaim_path);
    die "ERROR: ownership of $lock_kind was lost at '$lock_dir'\n"
        if @reclaim_stat;
    die "ERROR: cannot inspect reclaim generation '$reclaim_path': $!\n"
        if !$!{ENOENT};
    my $snapshot = lock_dir_snapshot($lock_dir, $lock_kind);
    die "ERROR: ownership of $lock_kind was lost at '$lock_dir'\n"
        if !defined $snapshot || !$snapshot->{valid}
        || $snapshot->{token} ne $token || $snapshot->{pid} != $$;
    return $token;
}

sub release_lock_dir {
    my ($lock_dir, $best_effort) = @_;
    my $token = $owned_lock_token{$lock_dir};
    return if !defined $token && $best_effort;
    die "ERROR: current process does not own lock '$lock_dir'\n"
        if !defined $token;
    my $ok = eval { assert_lock_owned($lock_dir); 1 };
    if (!$ok) {
        my $error = $@ || "ERROR: ownership of lock '$lock_dir' was lost\n";
        delete $owned_lock_token{$lock_dir};
        delete $owned_lock_kind{$lock_dir};
        @owned_lock_dirs = grep { $_ ne $lock_dir } @owned_lock_dirs;
        return if $best_effort;
        die $error;
    }
    my @remaining_temp;
    for my $tmp (@owned_temp_files) {
        if (dirname($tmp) eq $lock_dir) {
            unlink($tmp) if -e $tmp;
            next;
        }
        push @remaining_temp, $tmp;
    }
    @owned_temp_files = @remaining_temp;
    my $owner_path = lock_owner_path($lock_dir, $token);
    unlink($owner_path)
        or die "ERROR: cannot remove lock owner metadata '$owner_path': $!\n";
    rmdir($lock_dir) or die "ERROR: cannot remove lock '$lock_dir': $!\n";
    delete $owned_lock_token{$lock_dir};
    delete $owned_lock_kind{$lock_dir};
    @owned_lock_dirs = grep { $_ ne $lock_dir } @owned_lock_dirs;
}

sub write_attestation_cache_atomic {
    my ($path, $meta_ref, $entries_ref, $lock_wait, $lock_stale_seconds) = @_;
    my $lock_dir = "$path.lockdir";
    my $lock_token = acquire_lock_dir(
        $lock_dir,
        $lock_wait,
        $lock_stale_seconds,
        'attestation cache lock',
    );

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
    my $tmp = File::Spec->catfile(
        $lock_dir, ".attestation-$lock_token.tmp"
    );
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
    assert_lock_owned($lock_dir);
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
    my $lock_stale_seconds = $args{lock_stale_seconds};
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
        write_attestation_cache_atomic(
            $cache_path,
            $meta_ref,
            \%verified,
            $lock_wait,
            $lock_stale_seconds,
        );
    }
    return \%verified;
}

sub write_contract_atomic {
    my ($path, $values_ref, $adopted_legacy, $lock_dir) = @_;
    my $lock_token = assert_lock_owned($lock_dir);
    my $tmp = File::Spec->catfile(
        $lock_dir, ".contract-$lock_token.tmp"
    );
    open(my $fh, '>', $tmp) or die "ERROR: cannot write '$tmp': $!\n";
    push @owned_temp_files, $tmp;
    for my $key (sort keys %{$values_ref}) {
        print {$fh} "$key\t$values_ref->{$key}\n";
    }
    print {$fh} "legacy_adopted\t", ($adopted_legacy ? 1 : 0), "\n";
    close($fh) or die "ERROR: failed to close '$tmp': $!\n";
    assert_lock_owned($lock_dir);
    rename($tmp, $path) or die "ERROR: cannot install state contract '$path': $!\n";
    @owned_temp_files = grep { $_ ne $tmp } @owned_temp_files;
}

my $state_dir = absolute_path($opt{state_dir});
my $reference_manifest = absolute_path($opt{reference_manifest});
my $taxonomy_release_manifest = absolute_path($opt{taxonomy_release_manifest});
my $toolchain_fingerprint_path = absolute_path($opt{toolchain_fingerprint});
my $reference_root = abs_path($opt{reference_root});
die "ERROR: reference manifest not found: $reference_manifest\n"
    if !-f $reference_manifest;
die "ERROR: taxonomy release manifest not found: $taxonomy_release_manifest\n"
    if !-f $taxonomy_release_manifest;
die "ERROR: reference root not found: $opt{reference_root}\n"
    if !defined $reference_root || !-d $reference_root;
die "ERROR: runtime toolchain fingerprint not found: $toolchain_fingerprint_path\n"
    if !-f $toolchain_fingerprint_path;
my $manifest_sha256 = file_sha256($reference_manifest);
my $taxonomy_release_manifest_sha256 = file_sha256($taxonomy_release_manifest);
my $manifest_artifacts_ref = read_reference_manifest($reference_manifest, $reference_root);
my $taxonomy_release_entries_ref = read_taxonomy_release_manifest($taxonomy_release_manifest);
my $toolchain_fingerprint_ref = read_toolchain_fingerprint($toolchain_fingerprint_path);

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
    lock_stale_seconds => $opt{lock_stale_seconds},
    cache_allowed => $cache_allowed,
);
my %taxonomy_hash = map {
    $_ => $verified_ref->{$taxonomy_path{$_}}{verified_sha}
} @taxonomy_files;

my %legacy_identity = (
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
    die "ERROR: $key must not be empty\n" if $legacy_identity{$key} eq '';
}

my %identity = (
    %legacy_identity,
    schema_version           => '2',
    toolchain_fingerprint_id => $toolchain_fingerprint_ref->{fingerprint_id},
);
my $canonical = join('', map { "$_\t$identity{$_}\n" } sort keys %identity);
my $legacy_canonical =
    join('', map { "$_\t$legacy_identity{$_}\n" } sort keys %legacy_identity);
my $legacy_contract_id = sha256_hex($legacy_canonical);
my %contract = (
    %identity,
    reference_manifest_path => $reference_manifest,
    taxonomy_release_manifest_path   => $taxonomy_release_manifest,
    taxonomy_release_manifest_sha256 => $taxonomy_release_manifest_sha256,
    taxonomy_data_dir                => $taxonomy_dir,
    taxonomy_mode                    => $taxonomy_mode,
    execution_profile                => trim($opt{profile}),
);
for my $key (sort keys %{$toolchain_fingerprint_ref}) {
    $contract{"toolchain_$key"} = $toolchain_fingerprint_ref->{$key};
}
$contract{contract_id} = sha256_hex($canonical);

make_path($state_dir) if !-d $state_dir;
my $lock_dir = File::Spec->catdir($state_dir, '.state_compatibility.lockdir');
acquire_lock_dir(
    $lock_dir,
    $opt{lock_wait},
    $opt{lock_stale_seconds},
    'state compatibility lock',
);

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
        my $existing_schema = $existing_ref->{schema_version} // '';
        die "ERROR: unsupported rolling-state contract schema '$existing_schema' at '$state_dir'\n"
            if $existing_schema ne '1' && $existing_schema ne '2';

        if ($existing_schema eq '1') {
            my @legacy_changed;
            for my $key (sort(keys(%legacy_identity))) {
                my $old = $existing_ref->{$key} // '';
                my $new = $legacy_identity{$key};
                push @legacy_changed, "$key: '$old' -> '$new'" if $old ne $new;
            }
            my $old_contract_id = $existing_ref->{contract_id} // '';
            push @legacy_changed,
                "contract_id: '$old_contract_id' -> '$legacy_contract_id'"
                if $old_contract_id ne $legacy_contract_id;
            if (@legacy_changed) {
                die "ERROR: schema-v1 rolling state is incompatible with the legacy "
                    . "reference/taxonomy/classifier baseline at '$state_dir'.\n"
                    . join("\n", map { "  $_" } @legacy_changed)
                    . "\nUse a new --state_id, --restart_mode reset, or the matching legacy inputs.\n";
            }
            if ($opt{contract_migration} ne 'attest_v1') {
                die "ERROR: schema-v1 rolling state requires explicit toolchain migration at '$state_dir'.\n"
                    . "The historical runtime was not recorded and cannot be cryptographically proven. "
                    . "After verifying that this state was produced by the supported legacy host/locked "
                    . "runtime and not the inherited NanoRTax container, re-run once with "
                    . "--state_contract_migration attest_v1. Otherwise use a new --state_id or "
                    . "--restart_mode reset.\n";
            }
            my $old_taxonomy_dir = $existing_ref->{taxonomy_data_dir} // '';
            my $migrated_from = $existing_ref->{taxonomy_migrated_from_data_dir} // '';
            $migrated_from = $old_taxonomy_dir
                if $migrated_from eq '' && $old_taxonomy_dir ne ''
                && $old_taxonomy_dir ne $contract{taxonomy_data_dir};
            $contract{taxonomy_migrated_from_data_dir} = $migrated_from
                if $migrated_from ne '';
            $contract{migrated_from_contract_id} = $old_contract_id;
            $contract{toolchain_migration_attested} = '1';
            $contract{toolchain_migration_attested_utc} =
                strftime('%Y-%m-%dT%H:%M:%SZ', gmtime());
            $contract{toolchain_migration_limitation} =
                'historical_schema_v1_runtime_not_cryptographically_provable';
            my $legacy_adopted = ($existing_ref->{legacy_adopted} // '') eq '1' ? 1 : 0;
            write_contract_atomic(
                $contract_path, \%contract, $legacy_adopted, $lock_dir
            );
            warn "WARNING: migrated schema-v1 rolling state to schema v2 under an "
                . "operator-attested legacy runtime assumption; historical toolchain identity "
                . "was not recorded and cannot be cryptographically proven\n";
        } else {
            my @changed;
            for my $key (sort(keys(%identity)), 'contract_id') {
                my $old = $existing_ref->{$key} // '';
                my $new = $contract{$key};
                push @changed, "$key: '$old' -> '$new'" if $old ne $new;
            }
            if (@changed) {
                die "ERROR: incompatible rolling state at '$state_dir'.\n"
                    . join("\n", map { "  $_" } @changed)
                    . "\nUse a new --state_id, --restart_mode reset, or the matching reference/taxonomy/classifier/toolchain versions.\n";
            }
            my $old_taxonomy_dir = $existing_ref->{taxonomy_data_dir} // '';
            my $old_taxonomy_manifest_sha =
                $existing_ref->{taxonomy_release_manifest_sha256} // '';
            if (
                $old_taxonomy_dir ne $contract{taxonomy_data_dir}
                || $old_taxonomy_manifest_sha ne $contract{taxonomy_release_manifest_sha256}
            ) {
                my $migrated_from = $existing_ref->{taxonomy_migrated_from_data_dir} // '';
                $migrated_from = $old_taxonomy_dir
                    if $migrated_from eq '' && $old_taxonomy_dir ne ''
                    && $old_taxonomy_dir ne $contract{taxonomy_data_dir};
                $contract{taxonomy_migrated_from_data_dir} = $migrated_from
                    if $migrated_from ne '';
                for my $key (
                    qw(
                        migrated_from_contract_id
                        toolchain_migration_attested
                        toolchain_migration_attested_utc
                        toolchain_migration_limitation
                    )
                ) {
                    $contract{$key} = $existing_ref->{$key}
                        if defined $existing_ref->{$key};
                }
                my $legacy_adopted =
                    ($existing_ref->{legacy_adopted} // '') eq '1' ? 1 : 0;
                write_contract_atomic(
                    $contract_path, \%contract, $legacy_adopted, $lock_dir
                );
            }
        }
    } else {
        my $has_material = state_has_material($state_dir);
        if ($has_material && $opt{policy} ne 'adopt_legacy') {
            die "ERROR: legacy rolling state exists without a compatibility manifest at '$state_dir'.\n"
                . "Re-run once with --state_compatibility_policy adopt_legacy only after confirming that the state was produced by the current reference, taxonomy, classifier, and supported host runtime baseline. Never adopt state known or suspected to have been produced with the inherited docker/singularity NanoRTax image; use a new --state_id or --restart_mode reset instead.\n";
        }
        print STDERR "WARNING: adopting legacy rolling state at '$state_dir' under the current compatibility contract\n"
            if $has_material;
        write_contract_atomic(
            $contract_path, \%contract, $has_material ? 1 : 0, $lock_dir
        );
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
