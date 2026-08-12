#!/usr/bin/env perl

use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use Errno qw(EEXIST ELOOP ENOENT EPERM);
use Fcntl qw(:DEFAULT O_NOFOLLOW O_RDONLY);
use File::Basename qw(dirname);
use File::Path qw(make_path remove_tree);
use File::Spec;
use Getopt::Long qw(GetOptionsFromArray);
use IO::Handle;
use POSIX qw(strftime);
use Sys::Hostname qw(hostname);
use Time::HiRes qw(time usleep);

# Records are opened with O_NOFOLLOW so a symlink substituted for a record path
# is refused rather than followed. Its value is platform-specific, and on a
# platform that defines it as zero the flag would be silently inert, so this
# fails closed instead of degrading to an ordinary follow-the-path open.
BEGIN {
    die "ERROR: Fcntl O_NOFOLLOW is unavailable or inert; refusing to read "
        . "lock records without symlink protection\n"
        if !O_NOFOLLOW;
}

my $SCHEMA = '1';
my $TOKEN_RE = qr/\A[0-9a-f]{64}\z/;

sub usage {
    die <<'USAGE';
Usage: round_lock_generation.pl <command> --state-dir DIR [options]

Commands:
  acquire        acquire a generation and print generation/pin tokens
  inflight       publish the token-bound round_inflight.txt diagnostic
  handoff        publish full_round handoff and end the acquisition pin
  pin            acquire a process pin for a handed-off full_round generation
  guard-pin      assert an exact live process pin
  unpin          end an exact process pin
  verify-release  authenticate an idempotent release receipt for resume
  early-release  create a dorado_only marker and release with the acquisition pin
  finish         release full_round, or clean the exact dorado_only marker
  abort          best-effort pre-handoff release with the acquisition pin

Common options:
  --state-dir DIR --round-barcode NAME --scope full_round|dorado_only
  --token HEX64 --pin-token HEX64 --owner-pid PID --role NAME
  --stale-seconds N --wait-seconds N
USAGE
}

sub normalized_host {
    my $value = eval { hostname() } // '';
    $value =~ s/^\s+|\s+$//g;
    $value = lc($value);
    $value =~ s/\.\z//;
    return 'unknown' if $value eq '' || $value =~ /[\t\r\n]/;
    return $value;
}

my $THIS_HOST = normalized_host();

sub validate_text {
    my ($label, $value) = @_;
    die "ERROR: missing $label\n" if !defined($value) || $value eq '';
    die "ERROR: $label contains unsupported tab/newline characters\n"
        if $value =~ /[\t\r\n]/;
    return $value;
}

sub validate_uint {
    my ($label, $value) = @_;
    die "ERROR: invalid $label; expected integer >= 0\n"
        if !defined($value) || $value !~ /\A[0-9]+\z/;
    return int($value);
}

sub validate_pid {
    my ($value) = @_;
    validate_uint('owner-pid', $value);
    die "ERROR: owner-pid must be >= 1\n" if $value < 1;
    return int($value);
}

sub validate_scope {
    my ($scope) = @_;
    die "ERROR: invalid scope; expected full_round or dorado_only\n"
        if !defined($scope) || ($scope ne 'full_round' && $scope ne 'dorado_only');
    return $scope;
}

sub validate_token {
    my ($token) = @_;
    die "ERROR: invalid generation token\n"
        if !defined($token) || $token !~ $TOKEN_RE;
    return $token;
}

sub validate_role {
    my ($role) = @_;
    die "ERROR: invalid pin role\n"
        if !defined($role) || $role !~ /\A[A-Za-z0-9][A-Za-z0-9_.-]*\z/;
    return $role;
}

sub new_token {
    my ($context) = @_;
    sysopen(my $fh, '/dev/urandom', O_RDONLY)
        or die "ERROR: cannot open system CSPRNG /dev/urandom: $!\n";
    my $random = '';
    my $read = sysread($fh, $random, 32);
    close($fh) or die "ERROR: cannot close /dev/urandom: $!\n";
    die "ERROR: system CSPRNG returned insufficient data\n"
        if !defined($read) || $read != 32;
    return sha256_hex(join("\0", $random, $context, $$, $THIS_HOST, sprintf('%.9f', time())));
}

sub process_start_identity {
    my ($pid) = @_;
    return '' if !defined($pid) || $pid !~ /\A[0-9]+\z/ || $pid < 1;
    my $proc_stat = "/proc/$pid/stat";
    if (-r $proc_stat && open(my $proc_fh, '<', $proc_stat)) {
        my $line = <$proc_fh> // '';
        close($proc_fh);
        if ($line =~ /\A[0-9]+\s+\(.*\)\s+(.*)\z/s) {
            my @field = split(/\s+/, $1);
            if (@field > 19 && $field[19] =~ /\A[0-9]+\z/) {
                my $boot_id = '';
                my $boot_path = '/proc/sys/kernel/random/boot_id';
                if (-r $boot_path && open(my $boot_fh, '<', $boot_path)) {
                    $boot_id = lc(<$boot_fh> // '');
                    close($boot_fh);
                    $boot_id =~ s/^\s+|\s+$//g;
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

sub parsed_process_start {
    my ($value) = @_;
    return { ticks => $1, boot_id => undef }
        if defined($value) && $value =~ /\Aproc:([0-9]+)\z/;
    return { ticks => $2, boot_id => $1 }
        if defined($value) && $value =~ /\Aproc:([0-9a-f-]+):([0-9]+)\z/;
    return undef;
}

sub process_start_relationship {
    my ($recorded, $current) = @_;
    my $left = parsed_process_start($recorded);
    my $right = parsed_process_start($current);
    return 'unverifiable' if !defined($left) || !defined($right);
    return 'reused' if $left->{ticks} ne $right->{ticks};
    return 'unverifiable'
        if !defined($left->{boot_id}) || !defined($right->{boot_id});
    return $left->{boot_id} eq $right->{boot_id} ? 'same' : 'reused';
}

sub lock_dir { return File::Spec->catdir($_[0], '.round_inflight.lockdir'); }
sub generation_path { return File::Spec->catfile($_[0], 'generation.tsv'); }
sub transition_path { return File::Spec->catfile($_[0], 'transition.tsv'); }
sub pins_dir { return File::Spec->catdir($_[0], 'pins'); }
sub pin_candidate_path { return File::Spec->catfile(pins_dir($_[0]), "candidate.$_[1].tsv"); }
sub pin_ready_path { return File::Spec->catfile(pins_dir($_[0]), "ready.$_[1].tsv"); }
sub marker_path { return File::Spec->catfile($_[0], ".round_lock_handoff.$_[1].tsv"); }
sub release_path { return File::Spec->catfile($_[0], ".round_lock_release.$_[1].tsv"); }
sub revocation_path { return File::Spec->catfile($_[0], ".round_lock_revocation.$_[1].tsv"); }
sub inflight_generation_path { return File::Spec->catfile($_[0], ".round_inflight.$_[1].tsv"); }
sub inflight_compat_path { return File::Spec->catfile($_[0], 'round_inflight.txt'); }
sub events_dir { return File::Spec->catdir($_[0], '.round_lock_events'); }

sub sync_directory {
    my ($path) = @_;
    sysopen(my $fh, $path, O_RDONLY)
        or die "ERROR: cannot open directory for sync '$path': $!\n";
    $fh->sync()
        or die "ERROR: cannot sync directory '$path': $!\n";
    close($fh) or die "ERROR: cannot close directory '$path': $!\n";
}

sub ensure_real_directory {
    my ($path, $mode) = @_;
    my @st = lstat($path);
    if (@st) {
        die "ERROR: expected a real directory, not a symlink: $path\n"
            if -l _ || !-d _;
        return;
    }
    die "ERROR: cannot inspect directory '$path': $!\n" if !$!{ENOENT};
    if (mkdir($path, $mode)) {
        sync_directory(dirname($path));
        return;
    }
    # Capture both before any later syscall resets errno.
    my $lost_creation_race = $!{EEXIST};
    my $mkdir_error = "$!";
    die "ERROR: cannot create directory '$path': $mkdir_error\n"
        if !$lost_creation_race;
    # A concurrent first acquisition created the directory between our lstat
    # and our mkdir. That is benign, but only once the winner is confirmed to
    # have installed a real directory rather than a file or a symlink.
    @st = lstat($path);
    die "ERROR: cannot inspect directory '$path' after concurrent creation: $!\n"
        if !@st;
    die "ERROR: expected a real directory, not a symlink: $path\n"
        if -l _ || !-d _;
    # Sync the parent on this path too. The winner may have died between its
    # mkdir and its own sync, so observing the entry does not establish that it
    # is durable; without this the loser proceeds on an unflushed directory.
    sync_directory(dirname($path));
    return;
}

sub canonical_body {
    my ($values_ref, $order_ref) = @_;
    return join('', map { "$_\t$values_ref->{$_}\n" } @{$order_ref});
}

sub checksummed_content {
    my ($values_ref, $order_ref) = @_;
    my $body = canonical_body($values_ref, $order_ref);
    return $body . "record_sha256\t" . sha256_hex($body) . "\n";
}

sub write_temp_file {
    my ($path, $content) = @_;
    sysopen(my $fh, $path, O_WRONLY | O_CREAT | O_EXCL, 0600)
        or die "ERROR: cannot create '$path': $!\n";
    if (!print {$fh} $content) {
        my $error = $!;
        close($fh);
        unlink($path);
        die "ERROR: cannot write '$path': $error\n";
    }
    if (!$fh->flush()) {
        my $error = $!;
        close($fh);
        unlink($path);
        die "ERROR: cannot flush '$path': $error\n";
    }
    if (!$fh->sync()) {
        my $error = $!;
        close($fh);
        unlink($path);
        die "ERROR: cannot sync '$path': $error\n";
    }
    if (!close($fh)) {
        my $error = $!;
        unlink($path);
        die "ERROR: cannot close '$path': $error\n";
    }
}

sub install_immutable {
    my ($path, $content) = @_;
    my $tmp = "$path.tmp-" . new_token($path);
    write_temp_file($tmp, $content);
    my $linked = link($tmp, $path);
    my $exists = !$linked && $!{EEXIST};
    my $error = $linked ? '' : "$!";
    unlink($tmp);
    sync_directory(dirname($path)) if $linked;
    die "ERROR: cannot install immutable record '$path': $error\n"
        if !$linked && !$exists;
    return $linked ? 1 : 0;
}

sub replace_record {
    my ($path, $content) = @_;
    my $tmp = "$path.tmp-" . new_token($path);
    write_temp_file($tmp, $content);
    if (!rename($tmp, $path)) {
        my $error = $!;
        unlink($tmp);
        die "ERROR: cannot install record '$path': $error\n";
    }
    sync_directory(dirname($path));
}

sub parse_record {
    my ($path, $required_ref) = @_;
    my @st = lstat($path);
    return undef if !@st && $!{ENOENT};
    die "ERROR: cannot inspect record '$path': $!\n" if !@st;
    die "ERROR: record is not a regular non-symlink file: $path\n"
        if !-f _ || -l _;
    # Test-only fault injection for the window between the lstat above and the
    # open below. Scoped to one named ready pin so a test can target either a
    # concurrently unpinned worker (benign, skipped) or the caller's own
    # authorization pin (lost authority, fail closed).
    my $failpoint = $ENV{RTBIOSCAN_ROUND_LOCK_FAILPOINT} // '';
    if ($failpoint =~ /\Aunlink-ready-pin-before-open:([0-9a-f]{64})\z/
        && $path =~ m{/ready\.\Q$1\E\.tsv\z}) {
        unlink($path);
    }
    if ($failpoint =~ /\Areplace-ready-pin-with-file:([0-9a-f]{64})\z/
        && $path =~ m{/ready\.\Q$1\E\.tsv\z}) {
        # Substitute a different regular file, which the dev/ino check rejects.
        my $decoy = "$path.decoy";
        if (open(my $decoy_fh, '>', $decoy)) {
            print {$decoy_fh} "decoy\n";
            close($decoy_fh);
            rename($decoy, $path);
        }
    }
    if ($failpoint =~ /\Areplace-ready-pin-with-symlink:([0-9a-f]{64})\z/
        && $path =~ m{/ready\.\Q$1\E\.tsv\z}) {
        # Substitute a symlink to the candidate hard link. Both names share one
        # inode, so this defeats a dev/ino comparison and is refused only by
        # O_NOFOLLOW.
        (my $candidate = $path) =~ s{/ready\.}{/candidate.};
        unlink($path);
        symlink($candidate, $path);
    }
    # O_NOFOLLOW is what actually enforces the non-symlink half of the record
    # contract. A dev/ino comparison alone cannot: ready.<token>.tsv and
    # candidate.<token>.tsv are hard links to one inode, so a symlink swapped in
    # for the ready path resolves to that same inode and would compare equal.
    my $fh;
    if (!sysopen($fh, $path, O_RDONLY | O_NOFOLLOW)) {
        # Removed between the lstat above and this open. Report absence exactly
        # as for a record that was already gone, so each caller keeps its own
        # meaning for absence: blocking_pins skips a concurrently unpinned
        # worker, while guard_pin treats a missing authorization pin as lost
        # authority and fails closed.
        return undef if $!{ENOENT};
        # O_NOFOLLOW refuses a symlink with ELOOP. That is substitution, not a
        # readable record, and must never be parsed.
        die "ERROR: record path is a symlink: $path\n" if $!{ELOOP};
        die "ERROR: cannot read '$path': $!\n";
    }
    binmode($fh, ':raw')
        or die "ERROR: cannot set raw mode on '$path': $!\n";
    # Secondary check: O_NOFOLLOW rejects a substituted symlink, and this
    # rejects a substituted regular file with a different identity.
    my @fst = stat($fh);
    die "ERROR: cannot inspect open record '$path': $!\n" if !@fst;
    die "ERROR: record is not a regular file: $path\n" if !-f _;
    die "ERROR: record was replaced while being opened: $path\n"
        if $fst[0] != $st[0] || $fst[1] != $st[1];
    my @lines = <$fh>;
    close($fh) or die "ERROR: cannot close '$path': $!\n";
    my %value;
    my $body = '';
    my $checksum;
    my $checksum_seen = 0;
    for my $line (@lines) {
        chomp($line);
        my ($key, $payload, @extra) = split(/\t/, $line, -1);
        die "ERROR: malformed record '$path'\n"
            if !defined($key) || !defined($payload) || @extra || exists($value{$key});
        if ($key eq 'record_sha256') {
            die "ERROR: malformed record '$path'\n" if $checksum_seen;
            $checksum = $payload;
            $checksum_seen = 1;
            next;
        }
        $value{$key} = $payload;
        $body .= "$key\t$payload\n";
    }
    die "ERROR: malformed record '$path'\n"
        if !defined($checksum) || $checksum !~ $TOKEN_RE || sha256_hex($body) ne $checksum;
    for my $key (@{$required_ref}) {
        die "ERROR: malformed record '$path': missing $key\n"
            if !exists($value{$key});
    }
    die "ERROR: malformed record '$path': unexpected fields\n"
        if scalar(keys(%value)) != scalar(@{$required_ref});
    return \%value;
}

my @GENERATION_ORDER = qw(schema token round_barcode scope pid host process_start started_epoch effective_ttl_seconds lock_dev lock_ino);
my @PIN_ORDER = qw(schema token pin_token round_barcode scope role pid host process_start created_epoch lock_dev lock_ino);
my @TRANSITION_ORDER = qw(schema action operation_token owner_token round_barcode scope reason effective_ttl_seconds lock_dev lock_ino allowed_pin_token started_epoch);
my @MARKER_ORDER = qw(schema token round_barcode scope outcome created_epoch);
my @RELEASE_ORDER = qw(schema token round_barcode scope outcome reason effective_ttl_seconds release_transition_epoch lock_dev lock_ino);
my @REVOCATION_ORDER = qw(schema token round_barcode scope outcome reason effective_ttl_seconds revoked_epoch lock_dev lock_ino operation_token);
my @EVENT_ORDER = qw(schema event_id generation_token round_barcode scope event outcome effective_ttl_seconds event_epoch lock_dev lock_ino);
my @INFLIGHT_ORDER = qw(round_barcode started_utc read_file generation_token scope lock_dev lock_ino);

sub record_for {
    my ($path, $required_ref) = @_;
    return parse_record($path, $required_ref);
}

sub generation_record {
    my ($dir) = @_;
    return record_for(generation_path($dir), \@GENERATION_ORDER);
}

sub transition_record {
    my ($dir) = @_;
    return record_for(transition_path($dir), \@TRANSITION_ORDER);
}

sub lock_snapshot {
    my ($dir) = @_;
    my @dir_st = lstat($dir);
    return undef if !@dir_st && $!{ENOENT};
    die "ERROR: cannot inspect round lock '$dir': $!\n" if !@dir_st;
    die "ERROR: round lock is not a regular directory: $dir\n"
        if !-d _ || -l _;
    my $snapshot = {
        dev => $dir_st[0], ino => $dir_st[1], newest_epoch => $dir_st[9],
        valid => 0,
    };
    my $generation;
    my $ok = eval { $generation = generation_record($dir); 1 };
    if (!$ok) {
        $snapshot->{malformed_error} = $@;
        return $snapshot;
    }
    return $snapshot if !defined($generation);
    my @generation_st = lstat(generation_path($dir));
    $snapshot->{newest_epoch} = $generation_st[9]
        if @generation_st && $generation_st[9] > $snapshot->{newest_epoch};
    my $valid =
        $generation->{schema} eq $SCHEMA
        && $generation->{token} =~ $TOKEN_RE
        && $generation->{round_barcode} ne ''
        && $generation->{round_barcode} !~ /[\t\r\n]/
        && $generation->{scope} =~ /\A(?:full_round|dorado_only)\z/
        && $generation->{pid} =~ /\A[0-9]+\z/ && $generation->{pid} > 0
        && $generation->{host} ne '' && $generation->{host} !~ /[\t\r\n]/
        && $generation->{process_start} ne '' && $generation->{process_start} !~ /[\t\r\n]/
        && $generation->{started_epoch} =~ /\A[0-9]+\z/
        && $generation->{effective_ttl_seconds} =~ /\A[0-9]+\z/
        && $generation->{lock_dev} =~ /\A[0-9]+\z/
        && $generation->{lock_ino} =~ /\A[0-9]+\z/
        && $generation->{lock_dev} == $dir_st[0]
        && $generation->{lock_ino} == $dir_st[1];
    if ($valid) {
        $snapshot->{valid} = 1;
        $snapshot->{generation} = $generation;
        my @pins_st = lstat(pins_dir($dir));
        $snapshot->{newest_epoch} = $pins_st[9]
            if @pins_st && $pins_st[9] > $snapshot->{newest_epoch};
        my @marker_st = lstat(marker_path(dirname($dir), $generation->{token}));
        $snapshot->{newest_epoch} = $marker_st[9]
            if @marker_st && $marker_st[9] > $snapshot->{newest_epoch};
    }
    return $snapshot;
}

sub same_identity {
    my ($snapshot, $dir) = @_;
    my @st = lstat($dir);
    return 0 if !@st;
    return $st[0] == $snapshot->{dev} && $st[1] == $snapshot->{ino};
}

sub write_event {
    my (%arg) = @_;
    my $dir = events_dir($arg{state_dir});
    ensure_real_directory($dir, 0700);
    my $event_id = $arg{event_id}
        // new_token(join(':', $arg{event}, $arg{generation_token}));
    my %value = (
        schema => $SCHEMA,
        event_id => $event_id,
        generation_token => $arg{generation_token},
        round_barcode => $arg{round_barcode},
        scope => $arg{scope},
        event => $arg{event},
        outcome => $arg{outcome},
        effective_ttl_seconds => $arg{effective_ttl_seconds},
        event_epoch => $arg{event_epoch} // int(time()),
        lock_dev => $arg{lock_dev},
        lock_ino => $arg{lock_ino},
    );
    my $path = File::Spec->catfile(
        $dir, join('.', $arg{generation_token}, $arg{event}, $event_id) . '.tsv'
    );
    my $content = checksummed_content(\%value, \@EVENT_ORDER);
    my $installed = install_immutable($path, $content);
    if (!$installed) {
        my $existing = record_for($path, \@EVENT_ORDER);
        die "ERROR: immutable event conflicts at '$path'\n"
            if canonical_body($existing, \@EVENT_ORDER)
                ne canonical_body(\%value, \@EVENT_ORDER);
    }
}

sub install_generation {
    my ($dir, $generation) = @_;
    my $path = generation_path($dir);
    die "ERROR: generation record already exists: $path\n"
        if !install_immutable($path, checksummed_content($generation, \@GENERATION_ORDER));
}

sub assert_generation {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = lock_snapshot($dir);
    die "ERROR: round lock generation was lost: $arg{token}\n"
        if !defined($snapshot) || !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    die "ERROR: round lock generation was displaced: $arg{token}\n"
        if $generation->{token} ne $arg{token}
        || $generation->{round_barcode} ne $arg{round_barcode}
        || $generation->{scope} ne $arg{scope};
    my $transition = transition_record($dir);
    die "ERROR: round lock generation is transitioning: $arg{token}\n"
        if defined($transition) && !$arg{allow_transition};
    return ($snapshot, $generation, $transition);
}

sub pin_values {
    my (%arg) = @_;
    return {
        schema => $SCHEMA, token => $arg{token}, pin_token => $arg{pin_token},
        round_barcode => $arg{round_barcode}, scope => $arg{scope}, role => $arg{role},
        pid => $arg{owner_pid}, host => $THIS_HOST,
        process_start => process_start_identity($arg{owner_pid}) || 'unavailable',
        created_epoch => int(time()), lock_dev => $arg{snapshot}->{dev},
        lock_ino => $arg{snapshot}->{ino},
    };
}

sub validate_pin_for_snapshot {
    my ($pin, $snapshot) = @_;
    return 0 if !defined($pin) || !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    return $pin->{schema} eq $SCHEMA
        && $pin->{token} eq $generation->{token}
        && $pin->{pin_token} =~ $TOKEN_RE
        && $pin->{round_barcode} eq $generation->{round_barcode}
        && $pin->{scope} eq $generation->{scope}
        && $pin->{role} =~ /\A[A-Za-z0-9][A-Za-z0-9_.-]*\z/
        && $pin->{pid} =~ /\A[0-9]+\z/ && $pin->{pid} > 0
        && $pin->{host} ne '' && $pin->{host} !~ /[\t\r\n]/
        && $pin->{process_start} ne '' && $pin->{process_start} !~ /[\t\r\n]/
        && $pin->{created_epoch} =~ /\A[0-9]+\z/
        && $pin->{lock_dev} =~ /\A[0-9]+\z/
        && $pin->{lock_ino} =~ /\A[0-9]+\z/
        && $pin->{lock_dev} == $snapshot->{dev}
        && $pin->{lock_ino} == $snapshot->{ino};
}

sub read_ready_pin {
    my ($dir, $pin_token, $snapshot) = @_;
    my $path = pin_ready_path($dir, $pin_token);
    my $pin = record_for($path, \@PIN_ORDER);
    return undef if !defined($pin);
    die "ERROR: process pin does not match the active generation\n"
        if !validate_pin_for_snapshot($pin, $snapshot)
        || $pin->{pin_token} ne $pin_token;
    return $pin;
}

sub pin_is_blocking {
    my ($pin) = @_;
    # Callers must resolve a missing pin before asking whether it blocks. An
    # undefined record would otherwise compare an undefined host against this
    # host and be misread as a foreign-host pin.
    die "ERROR: internal: pin_is_blocking requires a pin record\n"
        if !defined($pin);
    return 1 if $pin->{host} ne $THIS_HOST;
    my $alive = kill(0, $pin->{pid}) || $!{EPERM};
    return 0 if !$alive;
    my $relationship = process_start_relationship(
        $pin->{process_start}, process_start_identity($pin->{pid})
    );
    return 0 if $relationship eq 'reused';
    return 1;
}

sub blocking_pins {
    my ($dir, $snapshot, $allowed_pin) = @_;
    my $pin_dir = pins_dir($dir);
    my @st = lstat($pin_dir);
    die "ERROR: generation pin directory is missing or unsafe: $pin_dir\n"
        if !@st || -l _ || !-d _;
    opendir(my $dh, $pin_dir)
        or die "ERROR: cannot inspect generation pins '$pin_dir': $!\n";
    my @entries = sort grep { /\Aready\.([0-9a-f]{64})\.tsv\z/ } readdir($dh);
    closedir($dh);
    my @blocking;
    for my $entry (@entries) {
        my ($pin_token) = $entry =~ /\Aready\.([0-9a-f]{64})\.tsv\z/;
        next if defined($allowed_pin) && $pin_token eq $allowed_pin;
        my $pin = read_ready_pin($dir, $pin_token, $snapshot);
        # A concurrent legitimate unpin can remove the record between readdir
        # and this read. read_ready_pin returns undef only when the file is
        # absent; malformed records die inside parse_record. A pin that no
        # longer exists holds nothing and must be skipped, because
        # pin_is_blocking would otherwise read an undefined host and classify
        # it as a foreign-host pin, blocking release indefinitely.
        next if !defined($pin);
        push(@blocking, $pin) if pin_is_blocking($pin);
    }
    return @blocking;
}

sub install_pin {
    my (%arg) = @_;
    my ($snapshot) = assert_generation(%arg);
    my $dir = lock_dir($arg{state_dir});
    my $pin_token = $arg{pin_token} // new_token(join(':', $arg{token}, $arg{role}));
    validate_token($pin_token);
    my $values = pin_values(%arg, pin_token => $pin_token, snapshot => $snapshot);
    my $content = checksummed_content($values, \@PIN_ORDER);
    my $candidate = pin_candidate_path($dir, $pin_token);
    my $ready = pin_ready_path($dir, $pin_token);
    my $installed = install_immutable($candidate, $content);
    if (!$installed) {
        my $existing = record_for($candidate, \@PIN_ORDER);
        die "ERROR: immutable pin candidate conflicts: $pin_token\n"
            if canonical_body($existing, \@PIN_ORDER)
                ne canonical_body($values, \@PIN_ORDER);
    }
    assert_generation(%arg);
    if (!link($candidate, $ready)) {
        my $exists = $!{EEXIST};
        my $error = "$!";
        die "ERROR: cannot ready process pin '$pin_token': $error\n" if !$exists;
        my $existing = record_for($ready, \@PIN_ORDER);
        die "ERROR: ready process pin conflicts: $pin_token\n"
            if canonical_body($existing, \@PIN_ORDER)
                ne canonical_body($values, \@PIN_ORDER);
    } else {
        sync_directory(pins_dir($dir));
    }
    my $post_ok = eval { assert_generation(%arg); 1 };
    if (!$post_ok) {
        if (same_identity($snapshot, $dir) && -e $ready) {
            unlink($ready);
            sync_directory(pins_dir($dir));
        }
        die $@;
    }
    return $pin_token;
}

sub guard_pin {
    my (%arg) = @_;
    my ($snapshot) = assert_generation(%arg, allow_transition => 1);
    my $pin = read_ready_pin(lock_dir($arg{state_dir}), $arg{pin_token}, $snapshot);
    die "ERROR: process pin is absent: $arg{pin_token}\n" if !defined($pin);
    die "ERROR: process pin role mismatch\n"
        if defined($arg{role}) && $pin->{role} ne $arg{role};
    return ($snapshot, $pin);
}

sub unpin_generation {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = lock_snapshot($dir);
    return 0 if !defined($snapshot) || !$snapshot->{valid};
    return 0 if $snapshot->{generation}->{token} ne $arg{token};
    my $pin = read_ready_pin($dir, $arg{pin_token}, $snapshot);
    return 0 if !defined($pin);
    my $path = pin_ready_path($dir, $arg{pin_token});
    unlink($path) or die "ERROR: cannot end process pin '$arg{pin_token}': $!\n";
    sync_directory(pins_dir($dir));
    return 1;
}

sub marker_values {
    my (%arg) = @_;
    return {
        schema => $SCHEMA, token => $arg{token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, outcome => 'handoff', created_epoch => int(time()),
    };
}

sub install_marker {
    my (%arg) = @_;
    my $path = marker_path($arg{state_dir}, $arg{token});
    my $expected = marker_values(%arg);
    my $installed = install_immutable($path, checksummed_content($expected, \@MARKER_ORDER));
    if (!$installed) {
        validate_marker(%arg);
    }
    return $path;
}

sub validate_marker {
    my (%arg) = @_;
    my $path = marker_path($arg{state_dir}, $arg{token});
    my $marker = record_for($path, \@MARKER_ORDER);
    die "ERROR: missing handoff marker for generation $arg{token}\n"
        if !defined($marker);
    die "ERROR: handoff marker does not match generation $arg{token}\n"
        if $marker->{schema} ne $SCHEMA
        || $marker->{token} ne $arg{token}
        || $marker->{round_barcode} ne $arg{round_barcode}
        || $marker->{scope} ne $arg{scope}
        || $marker->{outcome} ne 'handoff'
        || $marker->{created_epoch} !~ /\A[0-9]+\z/;
    return $path;
}

sub release_record {
    my (%arg) = @_;
    my $path = release_path($arg{state_dir}, $arg{token});
    my $record = record_for($path, \@RELEASE_ORDER);
    return undef if !defined($record);
    die "ERROR: release receipt does not match generation $arg{token}\n"
        if $record->{schema} ne $SCHEMA
        || $record->{token} ne $arg{token}
        || $record->{round_barcode} ne $arg{round_barcode}
        || $record->{scope} ne $arg{scope}
        || $record->{outcome} ne 'released'
        || $record->{reason} !~ /\A(?:full_round_released|dorado_only_early|pre_handoff_abort)\z/
        || $record->{effective_ttl_seconds} !~ /\A[0-9]+\z/
        || $record->{release_transition_epoch} !~ /\A[0-9]+\z/
        || $record->{lock_dev} !~ /\A[0-9]+\z/
        || $record->{lock_ino} !~ /\A[0-9]+\z/;
    return $record;
}

sub revocation_record {
    my (%arg) = @_;
    my $path = revocation_path($arg{state_dir}, $arg{token});
    my $record = record_for($path, \@REVOCATION_ORDER);
    return undef if !defined($record);
    die "ERROR: revocation receipt does not match generation $arg{token}\n"
        if $record->{schema} ne $SCHEMA
        || $record->{token} ne $arg{token}
        || $record->{round_barcode} ne $arg{round_barcode}
        || $record->{scope} ne $arg{scope}
        || $record->{outcome} ne 'revoked'
        || $record->{reason} eq '' || $record->{reason} =~ /[\t\r\n]/
        || $record->{effective_ttl_seconds} !~ /\A[0-9]+\z/
        || $record->{revoked_epoch} !~ /\A[0-9]+\z/
        || $record->{lock_dev} !~ /\A[0-9]+\z/
        || $record->{lock_ino} !~ /\A[0-9]+\z/
        || $record->{operation_token} !~ $TOKEN_RE;
    return $record;
}

sub install_transition {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $existing = transition_record($dir);
    return $existing if defined($existing);
    my $operation_token = new_token(join(':', $arg{action}, $arg{owner_token}));
    my %value = (
        schema => $SCHEMA, action => $arg{action}, operation_token => $operation_token,
        owner_token => $arg{owner_token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, reason => $arg{reason},
        effective_ttl_seconds => $arg{effective_ttl_seconds},
        lock_dev => $arg{snapshot}->{dev}, lock_ino => $arg{snapshot}->{ino},
        allowed_pin_token => $arg{allowed_pin_token} // 'none',
        started_epoch => int(time()),
    );
    my $tmp = File::Spec->catfile($dir, ".transition-$operation_token.tmp");
    write_temp_file($tmp, checksummed_content(\%value, \@TRANSITION_ORDER));
    if (!same_identity($arg{snapshot}, $dir)) {
        unlink($tmp);
        return undef;
    }
    my $linked = link($tmp, transition_path($dir));
    my $exists = !$linked && $!{EEXIST};
    my $missing = !$linked && $!{ENOENT};
    my $error = $linked ? '' : "$!";
    unlink($tmp);
    sync_directory($dir) if $linked;
    die "ERROR: cannot install round-lock transition: $error\n"
        if !$linked && !$exists && !$missing;
    return undef if $missing;
    return transition_record($dir);
}

sub validate_transition_for_snapshot {
    my ($transition, $snapshot) = @_;
    return 0 if !defined($transition);
    my $shape_ok = $transition->{schema} eq $SCHEMA
        && $transition->{action} =~ /\A(?:reclaim|release)\z/
        && $transition->{operation_token} =~ $TOKEN_RE
        && ($transition->{owner_token} eq 'legacy'
            || $transition->{owner_token} =~ $TOKEN_RE)
        && $transition->{round_barcode} ne ''
        && $transition->{round_barcode} !~ /[\t\r\n]/
        && $transition->{scope} =~ /\A(?:full_round|dorado_only|legacy)\z/
        && $transition->{reason} ne ''
        && $transition->{reason} !~ /[\t\r\n]/
        && $transition->{effective_ttl_seconds} =~ /\A[0-9]+\z/
        && $transition->{lock_dev} =~ /\A[0-9]+\z/
        && $transition->{lock_ino} =~ /\A[0-9]+\z/
        && ($transition->{allowed_pin_token} eq 'none'
            || $transition->{allowed_pin_token} =~ $TOKEN_RE)
        && $transition->{started_epoch} =~ /\A[0-9]+\z/
        && $transition->{lock_dev} == $snapshot->{dev}
        && $transition->{lock_ino} == $snapshot->{ino};
    return 0 if !$shape_ok;
    if ($snapshot->{valid}) {
        my $generation = $snapshot->{generation};
        return 0 if $transition->{owner_token} ne $generation->{token}
            || $transition->{round_barcode} ne $generation->{round_barcode}
            || $transition->{scope} ne $generation->{scope}
            || $transition->{effective_ttl_seconds}
                ne $generation->{effective_ttl_seconds};
        return 0 if $transition->{action} eq 'reclaim'
            && $transition->{allowed_pin_token} ne 'none';
        return 0 if $transition->{action} eq 'release'
            && ($transition->{allowed_pin_token} !~ $TOKEN_RE
                || $transition->{reason}
                    !~ /\A(?:full_round_released|dorado_only_early|pre_handoff_abort)\z/);
        return 1;
    }
    return $transition->{action} eq 'reclaim'
        && $transition->{owner_token} eq 'legacy'
        && $transition->{round_barcode} eq 'legacy'
        && $transition->{scope} eq 'legacy'
        && $transition->{allowed_pin_token} eq 'none';
}

sub install_release {
    my (%arg) = @_;
    my %value = (
        schema => $SCHEMA, token => $arg{token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, outcome => 'released', reason => $arg{reason},
        effective_ttl_seconds => $arg{effective_ttl_seconds},
        release_transition_epoch => $arg{event_epoch} // int(time()),
        lock_dev => $arg{lock_dev}, lock_ino => $arg{lock_ino},
    );
    my $path = release_path($arg{state_dir}, $arg{token});
    my $installed = install_immutable($path, checksummed_content(\%value, \@RELEASE_ORDER));
    if (!$installed) {
        my $existing = release_record(%arg);
        die "ERROR: immutable release receipt conflicts: $arg{token}\n"
            if canonical_body($existing, \@RELEASE_ORDER)
                ne canonical_body(\%value, \@RELEASE_ORDER);
    }
}

sub install_revocation {
    my (%arg) = @_;
    return if $arg{token} !~ $TOKEN_RE;
    my %value = (
        schema => $SCHEMA, token => $arg{token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, outcome => 'revoked', reason => $arg{reason},
        effective_ttl_seconds => $arg{effective_ttl_seconds},
        revoked_epoch => $arg{event_epoch} // int(time()),
        lock_dev => $arg{lock_dev}, lock_ino => $arg{lock_ino},
        operation_token => $arg{operation_token},
    );
    my $path = revocation_path($arg{state_dir}, $arg{token});
    my $installed = install_immutable(
        $path, checksummed_content(\%value, \@REVOCATION_ORDER),
    );
    if (!$installed) {
        my $existing = revocation_record(%arg);
        die "ERROR: immutable revocation receipt conflicts: $arg{token}\n"
            if canonical_body($existing, \@REVOCATION_ORDER)
                ne canonical_body(\%value, \@REVOCATION_ORDER);
    }
}

sub record_transition_outcome {
    my (%arg) = @_;
    my $snapshot = $arg{snapshot};
    my $transition = $arg{transition};
    my $generation = $snapshot->{valid} ? $snapshot->{generation} : undef;
    my $token = $generation ? $generation->{token} : 'legacy';
    my $round = $generation ? $generation->{round_barcode} : 'legacy';
    my $scope = $generation ? $generation->{scope} : 'legacy';
    my $ttl = $generation ? $generation->{effective_ttl_seconds} : $transition->{effective_ttl_seconds};
    if ($transition->{action} eq 'reclaim') {
        install_revocation(
            state_dir => $arg{state_dir}, token => $token, round_barcode => $round,
            scope => $scope, reason => $arg{reason}, effective_ttl_seconds => $ttl,
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            operation_token => $transition->{operation_token},
            event_epoch => $transition->{started_epoch},
        );
        write_event(
            state_dir => $arg{state_dir}, generation_token => $token,
            round_barcode => $round, scope => $scope, event => 'reclaim',
            outcome => 'quarantined', effective_ttl_seconds => $ttl,
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            event_id => $transition->{operation_token},
            event_epoch => $transition->{started_epoch},
        );
    } else {
        install_release(
            state_dir => $arg{state_dir}, token => $token, round_barcode => $round,
            scope => $scope, reason => $arg{reason}, effective_ttl_seconds => $ttl,
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            event_epoch => $transition->{started_epoch},
        );
        write_event(
            state_dir => $arg{state_dir}, generation_token => $token,
            round_barcode => $round, scope => $scope, event => 'release',
            outcome => $arg{reason}, effective_ttl_seconds => $ttl,
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            event_id => $transition->{operation_token},
            event_epoch => $transition->{started_epoch},
        );
    }
}

sub release_pin_role {
    my ($reason) = @_;
    return 'backup_update_and_clean' if $reason eq 'full_round_released';
    return 'fast_acquisition'
        if $reason eq 'dorado_only_early' || $reason eq 'pre_handoff_abort';
    die "ERROR: unsupported release reason: $reason\n";
}

sub validate_release_transition_authority {
    my (%arg) = @_;
    my $snapshot = $arg{snapshot};
    my $transition = $arg{transition};
    my $generation = $snapshot->{generation};
    my $reason = $transition->{reason};
    my $reason_matches_scope = $generation->{scope} eq 'full_round'
        ? ($reason eq 'full_round_released' || $reason eq 'pre_handoff_abort')
        : ($reason eq 'dorado_only_early' || $reason eq 'pre_handoff_abort');
    die "ERROR: pending release reason does not match generation scope: $generation->{token}\n"
        if !$reason_matches_scope;
    my %generation_arg = (
        state_dir => $arg{state_dir}, token => $generation->{token},
        round_barcode => $generation->{round_barcode}, scope => $generation->{scope},
    );
    if ($reason eq 'full_round_released' || $reason eq 'dorado_only_early') {
        validate_marker(%generation_arg);
    } elsif (-e marker_path($arg{state_dir}, $generation->{token})) {
        die "ERROR: pre-handoff release conflicts with an existing handoff marker\n";
    }
    my $allowed_pin = read_ready_pin(
        $arg{lock_path}, $transition->{allowed_pin_token}, $snapshot,
    );
    die "ERROR: pending release lost its authenticated process pin\n"
        if !defined($allowed_pin);
    my $required_role = release_pin_role($reason);
    die "ERROR: pending release process pin role mismatch\n"
        if $allowed_pin->{role} ne $required_role;
}

sub remove_release_quarantine {
    my (%arg) = @_;
    return if $arg{transition}->{action} ne 'release';
    my $path = $arg{path};
    my @st = lstat($path);
    return if !@st && $!{ENOENT};
    die "ERROR: cannot inspect completed release quarantine '$path': $!\n"
        if !@st;
    die "ERROR: completed release quarantine is not a real directory: $path\n"
        if -l _ || !-d _;
    die "ERROR: completed release quarantine identity changed: $path\n"
        if $st[0] != $arg{snapshot}->{dev} || $st[1] != $arg{snapshot}->{ino};

    my $errors;
    remove_tree($path, { error => \$errors });
    if (defined($errors) && @{$errors}) {
        my @messages;
        for my $item (@{$errors}) {
            for my $failed_path (sort keys(%{$item})) {
                push(@messages, "$failed_path: $item->{$failed_path}");
            }
        }
        die "ERROR: cannot remove completed release quarantine '$path': "
            . join('; ', @messages) . "\n";
    }
    my @remaining = lstat($path);
    die "ERROR: completed release quarantine remains after cleanup: $path\n"
        if @remaining;
    die "ERROR: cannot verify completed release quarantine cleanup '$path': $!\n"
        if !$!{ENOENT};
    sync_directory($arg{state_dir});
}

sub finalize_transition {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = $arg{snapshot};
    my $transition = $arg{transition};
    if (($ENV{RTBIOSCAN_ROUND_LOCK_FAILPOINT} // '') eq 'after-transition-install') {
        die "ERROR: injected failure after transition install\n";
    }
    return 0 if !same_identity($snapshot, $dir);
    return 0 if !validate_transition_for_snapshot($transition, $snapshot);
    validate_release_transition_authority(
        state_dir => $arg{state_dir}, lock_path => $dir,
        snapshot => $snapshot, transition => $transition,
    ) if $transition->{action} eq 'release';
    if ($snapshot->{valid}) {
        my $allowed = $transition->{action} eq 'release'
            ? $transition->{allowed_pin_token}
            : undef;
        my @blocking = blocking_pins($dir, $snapshot, $allowed);
        return 0 if @blocking;
        remove_inflight_for_snapshot(
            state_dir => $arg{state_dir}, snapshot => $snapshot,
        );
    }
    my $suffix = $transition->{action} eq 'reclaim' ? 'reclaim' : 'release';
    my $quarantine = "$dir.$suffix-$transition->{operation_token}";
    if (!rename($dir, $quarantine)) {
        my $rename_error = "$!";
        my $moved = transition_record($quarantine);
        if (defined($moved)
            && $moved->{operation_token} eq $transition->{operation_token}) {
            my $moved_snapshot = lock_snapshot($quarantine);
            die "ERROR: cannot inspect completed round-lock quarantine\n"
                if !defined($moved_snapshot)
                || $moved_snapshot->{dev} != $snapshot->{dev}
                || $moved_snapshot->{ino} != $snapshot->{ino}
                || !validate_transition_for_snapshot($moved, $moved_snapshot);
            record_transition_outcome(
                state_dir => $arg{state_dir}, snapshot => $moved_snapshot,
                transition => $moved, reason => $moved->{reason},
            );
            remove_release_quarantine(
                state_dir => $arg{state_dir}, path => $quarantine,
                snapshot => $moved_snapshot, transition => $moved,
            );
            return 1;
        }
        my @current = lstat($dir);
        return 0 if !@current && $!{ENOENT};
        return 0 if @current && ($current[0] != $snapshot->{dev} || $current[1] != $snapshot->{ino});
        die "ERROR: cannot quarantine round lock '$dir': $rename_error\n";
    }
    my @moved_st = lstat($quarantine);
    die "ERROR: cannot inspect round-lock quarantine '$quarantine': $!\n" if !@moved_st;
    die "ERROR: wrong round-lock generation was quarantined\n"
        if $moved_st[0] != $snapshot->{dev} || $moved_st[1] != $snapshot->{ino};
    sync_directory($arg{state_dir});
    my $moved_transition = transition_record($quarantine);
    die "ERROR: round-lock quarantine lost its transition claim\n"
        if !defined($moved_transition)
        || $moved_transition->{operation_token} ne $transition->{operation_token};

    if (($ENV{RTBIOSCAN_ROUND_LOCK_FAILPOINT} // '') eq 'after-quarantine-rename') {
        die "ERROR: injected failure after quarantine rename\n";
    }

    record_transition_outcome(%arg);
    remove_release_quarantine(
        state_dir => $arg{state_dir}, path => $quarantine,
        snapshot => $snapshot, transition => $transition,
    );
    return 1;
}

sub recover_quarantines {
    my ($state_dir) = @_;
    opendir(my $dh, $state_dir)
        or die "ERROR: cannot inspect state directory '$state_dir': $!\n";
    my @entries = sort grep {
        /\A\.round_inflight\.lockdir\.(?:reclaim|release)-[0-9a-f]{64}\z/
    } readdir($dh);
    closedir($dh);
    for my $entry (@entries) {
        my $path = File::Spec->catdir($state_dir, $entry);
        my $snapshot = lock_snapshot($path);
        next if !defined($snapshot);
        my $transition = transition_record($path);
        die "ERROR: quarantine lacks a valid transition: $path\n"
            if !validate_transition_for_snapshot($transition, $snapshot);
        my $expected = $transition->{action} eq 'reclaim' ? 'reclaim' : 'release';
        die "ERROR: quarantine name/action mismatch: $path\n"
            if $entry ne ".round_inflight.lockdir.$expected-$transition->{operation_token}";
        validate_release_transition_authority(
            state_dir => $state_dir, lock_path => $path,
            snapshot => $snapshot, transition => $transition,
        ) if $transition->{action} eq 'release';
        record_transition_outcome(
            state_dir => $state_dir, snapshot => $snapshot, transition => $transition,
            reason => $transition->{reason},
        );
        remove_release_quarantine(
            state_dir => $state_dir, path => $path,
            snapshot => $snapshot, transition => $transition,
        );
    }
}

sub recover_pending_release {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = lock_snapshot($dir);
    return 0 if !defined($snapshot) || !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    return 0 if $generation->{token} ne $arg{token}
        || $generation->{round_barcode} ne $arg{round_barcode}
        || $generation->{scope} ne $arg{scope};
    my $transition = transition_record($dir);
    return 0 if !defined($transition);
    die "ERROR: canonical generation has an invalid release transition: $arg{token}\n"
        if !validate_transition_for_snapshot($transition, $snapshot)
        || $transition->{action} ne 'release'
        || $transition->{owner_token} ne $arg{token};
    validate_release_transition_authority(
        state_dir => $arg{state_dir}, lock_path => $dir,
        snapshot => $snapshot, transition => $transition,
    );
    my $finalized = finalize_transition(
        state_dir => $arg{state_dir}, snapshot => $snapshot,
        transition => $transition, reason => $transition->{reason},
    );
    die "ERROR: pending release is blocked by another live generation pin\n"
        if !$finalized;
    return 1;
}

sub stale_reason {
    my ($state_dir, $snapshot, $fallback_ttl) = @_;
    my $ttl = $fallback_ttl;
    if ($snapshot->{valid}) {
        my $generation = $snapshot->{generation};
        $ttl = int($generation->{effective_ttl_seconds});
        my @blocking = blocking_pins(lock_dir($state_dir), $snapshot, undef);
        return undef if @blocking;
        my $handoff = marker_path($state_dir, $generation->{token});
        if (!-e $handoff && $generation->{host} eq $THIS_HOST) {
            my $alive = kill(0, $generation->{pid}) || $!{EPERM};
            return "dead pid=$generation->{pid} host=$generation->{host}" if !$alive;
            my $relationship = process_start_relationship(
                $generation->{process_start}, process_start_identity($generation->{pid})
            );
            return "reused pid=$generation->{pid} host=$generation->{host}"
                if $relationship eq 'reused';
            return undef;
        }
    }
    return undef if $ttl == 0;
    my $now = int(time());
    return undef if $snapshot->{newest_epoch} < 1 || $snapshot->{newest_epoch} > $now;
    my $age = $now - $snapshot->{newest_epoch};
    return undef if $age < $ttl;
    return $snapshot->{valid}
        ? "leased generation age=${age}s ttl=${ttl}s"
        : "legacy or malformed lock age=${age}s ttl=${ttl}s";
}

sub reclaim_if_stale {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = lock_snapshot($dir);
    return 0 if !defined($snapshot);
    my $transition = transition_record($dir);
    my $reason;
    if (defined($transition)) {
        die "ERROR: malformed transition identity in '$dir'\n"
            if !validate_transition_for_snapshot($transition, $snapshot);
        $reason = $transition->{reason};
    } else {
        $reason = stale_reason($arg{state_dir}, $snapshot, $arg{stale_seconds});
        return 0 if !defined($reason);
        my $owner_token = $snapshot->{valid} ? $snapshot->{generation}->{token} : 'legacy';
        my $round = $snapshot->{valid} ? $snapshot->{generation}->{round_barcode} : 'legacy';
        my $scope = $snapshot->{valid} ? $snapshot->{generation}->{scope} : 'legacy';
        my $effective_ttl = $snapshot->{valid}
            ? $snapshot->{generation}->{effective_ttl_seconds}
            : $arg{stale_seconds};
        $transition = install_transition(
            state_dir => $arg{state_dir}, snapshot => $snapshot, action => 'reclaim',
            owner_token => $owner_token, round_barcode => $round, scope => $scope,
            reason => $reason, effective_ttl_seconds => $effective_ttl,
        );
        return 0 if !defined($transition);
    }
    return finalize_transition(
        state_dir => $arg{state_dir}, snapshot => $snapshot,
        transition => $transition, reason => $reason,
    );
}

sub acquire_generation {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    ensure_real_directory(events_dir($arg{state_dir}), 0700);
    recover_quarantines($arg{state_dir});
    my $waited_ms = 0;
    my $limit_ms = $arg{wait_seconds} * 1000;
    while (1) {
        if (mkdir($dir, 0700)) {
            sync_directory($arg{state_dir});
            my @st = lstat($dir);
            die "ERROR: cannot inspect newly-created round lock: $!\n" if !@st;
            my $token = new_token(join(':', $dir, $arg{round_barcode}, $arg{scope}));
            my %generation = (
                schema => $SCHEMA, token => $token, round_barcode => $arg{round_barcode},
                scope => $arg{scope}, pid => $arg{owner_pid}, host => $THIS_HOST,
                process_start => process_start_identity($arg{owner_pid}) || 'unavailable',
                started_epoch => int(time()),
                effective_ttl_seconds => $arg{stale_seconds},
                lock_dev => $st[0], lock_ino => $st[1],
            );
            my $pin_token;
            my $ok = eval {
                mkdir(pins_dir($dir), 0700)
                    or die "ERROR: cannot create generation pin directory: $!\n";
                sync_directory($dir);
                install_generation($dir, \%generation);
                $pin_token = install_pin(
                    %arg, token => $token, pin_token => new_token("acquire:$token"),
                    role => 'fast_acquisition', snapshot => undef,
                );
                write_event(
                    state_dir => $arg{state_dir}, generation_token => $token,
                    round_barcode => $arg{round_barcode}, scope => $arg{scope},
                    event => 'acquire', outcome => 'acquired',
                    effective_ttl_seconds => $arg{stale_seconds},
                    lock_dev => $st[0], lock_ino => $st[1],
                );
                1;
            };
            if (!$ok) {
                my $error = $@ || "ERROR: cannot initialize round lock\n";
                my $failed = "$dir.failed-acquire-$token";
                if (-d $dir && !rename($dir, $failed)) {
                    die "$error" . "ERROR: failed acquisition also could not be quarantined: $!\n";
                }
                sync_directory($arg{state_dir}) if -d $failed;
                die $error;
            }
            return ($token, $pin_token);
        }
        my $exists = $!{EEXIST};
        my $error = "$!";
        die "ERROR: cannot create round lock '$dir': $error\n" if !$exists;
        next if reclaim_if_stale(%arg);
        die "ERROR: timed out waiting for round lock '$dir'\n"
            if $limit_ms > 0 && $waited_ms >= $limit_ms;
        usleep(100_000);
        $waited_ms += 100;
    }
}

sub release_generation {
    my (%arg) = @_;
    recover_quarantines($arg{state_dir});
    my $existing = release_record(%arg);
    my $revoked = revocation_record(%arg);
    die "ERROR: generation has both release and revocation receipts: $arg{token}\n"
        if defined($existing) && defined($revoked);
    if (defined($existing)) {
        die "ERROR: release reason conflicts for generation $arg{token}\n"
            if $existing->{reason} ne $arg{reason};
        my $current = lock_snapshot(lock_dir($arg{state_dir}));
        die "ERROR: released generation is still the canonical lock: $arg{token}\n"
            if defined($current) && $current->{valid}
            && $current->{generation}->{token} eq $arg{token};
        return 1;
    }
    if (defined($revoked)) {
        return 0 if $arg{best_effort};
        die "ERROR: cannot release revoked generation $arg{token}\n";
    }
    die "ERROR: missing pin-token\n" if !defined($arg{pin_token});
    my $required_role = release_pin_role($arg{reason});
    my ($snapshot, $generation);
    my $ok = eval {
        ($snapshot) = guard_pin(%arg, role => $required_role);
        $generation = $snapshot->{generation};
        validate_marker(%arg) if $arg{reason} eq 'full_round_released';
        install_marker(%arg) if $arg{reason} eq 'dorado_only_early';
        1;
    };
    if (!$ok) {
        return 0 if $arg{best_effort};
        die $@;
    }
    if ($arg{unless_handoff}) {
        my $marker = marker_path($arg{state_dir}, $arg{token});
        if (-e $marker) {
            unpin_generation(%arg);
            return 1;
        }
    }
    my $transition = install_transition(
        state_dir => $arg{state_dir}, snapshot => $snapshot, action => 'release',
        owner_token => $arg{token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, reason => $arg{reason},
        effective_ttl_seconds => $generation->{effective_ttl_seconds},
        allowed_pin_token => $arg{pin_token},
    );
    if (!defined($transition)
        || !validate_transition_for_snapshot($transition, $snapshot)
        || $transition->{action} ne 'release'
        || $transition->{owner_token} ne $arg{token}
        || $transition->{reason} ne $arg{reason}
        || $transition->{allowed_pin_token} ne $arg{pin_token}) {
        return 0 if $arg{best_effort};
        die "ERROR: release lost transition race for generation $arg{token}\n";
    }
    my $finalized = finalize_transition(
        state_dir => $arg{state_dir}, snapshot => $snapshot,
        transition => $transition, reason => $arg{reason},
    );
    if (!$finalized) {
        return 0 if $arg{best_effort};
        die "ERROR: release is blocked by another live generation pin\n";
    }
    return 1;
}

sub assert_inflight_for_snapshot {
    my ($record, $arg_ref, $snapshot) = @_;
    die "ERROR: immutable inflight record conflicts: $arg_ref->{token}\n"
        if $record->{round_barcode} ne $arg_ref->{round_barcode}
        || $record->{started_utc}
            !~ /\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\z/
        || $record->{read_file} ne $arg_ref->{read_file}
        || $record->{generation_token} ne $arg_ref->{token}
        || $record->{scope} ne $arg_ref->{scope}
        || $record->{lock_dev} ne "$snapshot->{dev}"
        || $record->{lock_ino} ne "$snapshot->{ino}";
}

sub write_inflight {
    my (%arg) = @_;
    my ($snapshot) = guard_pin(%arg);
    my $generation_path = inflight_generation_path($arg{state_dir}, $arg{token});
    my $existing = record_for($generation_path, \@INFLIGHT_ORDER);
    my %value;
    if (defined($existing)) {
        assert_inflight_for_snapshot($existing, \%arg, $snapshot);
        %value = %{$existing};
    } else {
        %value = (
            round_barcode => $arg{round_barcode},
            started_utc => strftime('%Y-%m-%dT%H:%M:%SZ', gmtime()),
            read_file => $arg{read_file}, generation_token => $arg{token},
            scope => $arg{scope}, lock_dev => $snapshot->{dev},
            lock_ino => $snapshot->{ino},
        );
        my $installed = install_immutable(
            $generation_path, checksummed_content(\%value, \@INFLIGHT_ORDER),
        );
        if (!$installed) {
            $existing = record_for($generation_path, \@INFLIGHT_ORDER);
            die "ERROR: immutable inflight record disappeared: $arg{token}\n"
                if !defined($existing);
            assert_inflight_for_snapshot($existing, \%arg, $snapshot);
            %value = %{$existing};
        }
    }
    guard_pin(%arg);
    my $compat_body = join(
        '',
        "round_barcode=$value{round_barcode}\n",
        "started_utc=$value{started_utc}\n",
        "read_file=$value{read_file}\n",
        "generation_token=$value{generation_token}\n",
        "scope=$value{scope}\n",
        "lock_dev=$value{lock_dev}\n",
        "lock_ino=$value{lock_ino}\n",
    );
    my $compat_content = $compat_body
        . "record_sha256=" . sha256_hex($compat_body) . "\n";
    my $dir = lock_dir($arg{state_dir});
    my $compat_tmp = File::Spec->catfile(
        $dir,
        ".round_inflight.$arg{pin_token}." . new_token("inflight:$arg{token}") . '.tmp',
    );
    my $temp_created = 0;
    my $ok = eval {
        write_temp_file($compat_tmp, $compat_content);
        $temp_created = 1;
        guard_pin(%arg);
        if (($ENV{RTBIOSCAN_ROUND_LOCK_FAILPOINT} // '') eq 'before-compat-publish') {
            die "ERROR: injected failure before compatibility inflight publish\n";
        }
        rename($compat_tmp, inflight_compat_path($arg{state_dir}))
            or die "ERROR: cannot publish generation-bound inflight diagnostic: $!\n";
        $temp_created = 0;
        sync_directory($arg{state_dir});
        guard_pin(%arg);
        1;
    };
    if (!$ok) {
        my $error = $@ || "ERROR: cannot publish generation-bound inflight diagnostic\n";
        unlink($compat_tmp) if $temp_created;
        die $error;
    }
}

sub remove_inflight_for_snapshot {
    my (%arg) = @_;
    my $snapshot = $arg{snapshot};
    return if !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    my $path = inflight_generation_path(
        $arg{state_dir}, $generation->{token},
    );
    my $record = record_for($path, \@INFLIGHT_ORDER);
    return if !defined($record);
    die "ERROR: generation-bound inflight record does not match the active lock\n"
        if $record->{generation_token} ne $generation->{token}
        || $record->{round_barcode} ne $generation->{round_barcode}
        || $record->{scope} ne $generation->{scope}
        || $record->{lock_dev} ne "$snapshot->{dev}"
        || $record->{lock_ino} ne "$snapshot->{ino}";
    my $compat_body = join(
        '',
        "round_barcode=$record->{round_barcode}\n",
        "started_utc=$record->{started_utc}\n",
        "read_file=$record->{read_file}\n",
        "generation_token=$record->{generation_token}\n",
        "scope=$record->{scope}\n",
        "lock_dev=$record->{lock_dev}\n",
        "lock_ino=$record->{lock_ino}\n",
    );
    my $expected_compat = $compat_body
        . "record_sha256=" . sha256_hex($compat_body) . "\n";
    my $compat_path = inflight_compat_path($arg{state_dir});
    if (-e $compat_path || -l $compat_path) {
        my @compat_st = lstat($compat_path);
        die "ERROR: cannot inspect compatibility inflight diagnostic: $!\n"
            if !@compat_st;
        die "ERROR: compatibility inflight diagnostic is not a regular file\n"
            if -l _ || !-f _;
        open(my $compat_fh, '<', $compat_path)
            or die "ERROR: cannot read compatibility inflight diagnostic: $!\n";
        local $/;
        my $content = <$compat_fh> // '';
        close($compat_fh)
            or die "ERROR: cannot close compatibility inflight diagnostic: $!\n";
        die "ERROR: compatibility inflight diagnostic does not match the active generation\n"
            if $content ne $expected_compat;
        unlink($compat_path)
            or die "ERROR: cannot remove compatibility inflight diagnostic: $!\n";
    }
    unlink($path)
        or die "ERROR: cannot remove generation-bound inflight diagnostic '$path': $!\n";
    sync_directory($arg{state_dir});
}

sub remove_exact_marker {
    my (%arg) = @_;
    my $path = marker_path($arg{state_dir}, $arg{token});
    return if !-e $path;
    validate_marker(%arg);
    unlink($path) or die "ERROR: cannot remove exact handoff marker '$path': $!\n";
    sync_directory($arg{state_dir});
}

my $command = shift(@ARGV) // '';
usage() if $command eq '' || $command eq '--help' || $command eq '-h';

my %opt = (
    wait_seconds => 0,
    stale_seconds => 0,
    best_effort => 0,
);
GetOptionsFromArray(
    \@ARGV,
    'state-dir=s' => \$opt{state_dir},
    'round-barcode=s' => \$opt{round_barcode},
    'scope=s' => \$opt{scope},
    'token=s' => \$opt{token},
    'pin-token=s' => \$opt{pin_token},
    'role=s' => \$opt{role},
    'owner-pid=s' => \$opt{owner_pid},
    'wait-seconds=s' => \$opt{wait_seconds},
    'stale-seconds=s' => \$opt{stale_seconds},
    'read-file=s' => \$opt{read_file},
    'best-effort!' => \$opt{best_effort},
) or usage();
die "ERROR: unexpected arguments: @ARGV\n" if @ARGV;

$opt{state_dir} = validate_text('state-dir', $opt{state_dir});
make_path($opt{state_dir}, { mode => 0700 }) if !-d $opt{state_dir};
die "ERROR: state-dir is not a directory: $opt{state_dir}\n" if !-d $opt{state_dir};
$opt{round_barcode} = validate_text('round-barcode', $opt{round_barcode})
    if $command ne 'acquire' || defined($opt{round_barcode});
$opt{scope} = validate_scope($opt{scope}) if defined($opt{scope});
$opt{wait_seconds} = validate_uint('wait-seconds', $opt{wait_seconds});
$opt{stale_seconds} = validate_uint('stale-seconds', $opt{stale_seconds});
$opt{owner_pid} = validate_pid($opt{owner_pid}) if defined($opt{owner_pid});
$opt{token} = validate_token($opt{token}) if defined($opt{token});
$opt{pin_token} = validate_token($opt{pin_token}) if defined($opt{pin_token});
$opt{role} = validate_role($opt{role}) if defined($opt{role});

if ($command eq 'acquire') {
    $opt{round_barcode} = validate_text('round-barcode', $opt{round_barcode});
    $opt{scope} = validate_scope($opt{scope});
    $opt{owner_pid} = validate_pid($opt{owner_pid});
    my ($token, $pin_token) = acquire_generation(%opt);
    print "generation_token=$token\npin_token=$pin_token\n";
    exit 0;
}

for my $required (qw(round_barcode scope token)) {
    die "ERROR: missing $required\n" if !defined($opt{$required});
}

if ($command eq 'inflight') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    $opt{read_file} = validate_text('read-file', $opt{read_file});
    write_inflight(%opt);
} elsif ($command eq 'handoff') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    die "ERROR: handoff is only valid for full_round\n" if $opt{scope} ne 'full_round';
    recover_quarantines($opt{state_dir});
    my $marker = marker_path($opt{state_dir}, $opt{token});
    if (-e $marker) {
        validate_marker(%opt);
        my $receipt = release_record(%opt);
        my $revoked = revocation_record(%opt);
        die "ERROR: generation has both release and revocation receipts: $opt{token}\n"
            if defined($receipt) && defined($revoked);
        die "ERROR: cannot hand off revoked generation $opt{token}\n"
            if defined($revoked);
        if (defined($receipt)) {
            die "ERROR: handoff release reason conflicts for generation $opt{token}\n"
                if $receipt->{reason} ne 'full_round_released';
            my $current = lock_snapshot(lock_dir($opt{state_dir}));
            die "ERROR: released generation is still the canonical lock: $opt{token}\n"
                if defined($current) && $current->{valid}
                && $current->{generation}->{token} eq $opt{token};
        } else {
            my ($snapshot) = assert_generation(%opt);
            my $pin = read_ready_pin(
                lock_dir($opt{state_dir}), $opt{pin_token}, $snapshot
            );
			if (defined($pin)) {
				die "ERROR: handoff pin role mismatch\n"
					if $pin->{role} ne 'fast_acquisition';
                unpin_generation(%opt);
            }
        }
    } else {
        my ($snapshot, $pin) = guard_pin(%opt, role => 'fast_acquisition');
        install_marker(%opt);
        write_event(
            state_dir => $opt{state_dir}, generation_token => $opt{token},
            round_barcode => $opt{round_barcode}, scope => $opt{scope},
            event => 'handoff', outcome => 'published',
            effective_ttl_seconds => $snapshot->{generation}->{effective_ttl_seconds},
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
        );
        unpin_generation(%opt);
    }
} elsif ($command eq 'pin') {
    $opt{owner_pid} = validate_pid($opt{owner_pid});
    $opt{role} = validate_role($opt{role});
    die "ERROR: pin is only valid for full_round\n" if $opt{scope} ne 'full_round';
    validate_marker(%opt);
    my $pin_token = install_pin(%opt);
    print "$pin_token\n";
} elsif ($command eq 'guard-pin') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    guard_pin(%opt);
} elsif ($command eq 'unpin') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    my $ok = eval { unpin_generation(%opt); 1 };
    die $@ if !$ok && !$opt{best_effort};
} elsif ($command eq 'verify-release') {
    recover_quarantines($opt{state_dir});
    recover_pending_release(%opt);
    my $receipt = release_record(%opt);
    my $revoked = revocation_record(%opt);
    die "ERROR: generation has both release and revocation receipts: $opt{token}\n"
        if defined($receipt) && defined($revoked);
	die "ERROR: missing authenticated release receipt for generation $opt{token}\n"
		if !defined($receipt);
	die "ERROR: pre-handoff abort is not a resumable release for generation $opt{token}\n"
		if $receipt->{reason} eq 'pre_handoff_abort';
	print "$receipt->{reason}\n";
} elsif ($command eq 'early-release') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    die "ERROR: early-release is only valid for dorado_only\n"
        if $opt{scope} ne 'dorado_only';
    release_generation(%opt, reason => 'dorado_only_early', unless_handoff => 0);
} elsif ($command eq 'finish') {
    if ($opt{scope} eq 'full_round') {
        release_generation(%opt, reason => 'full_round_released', unless_handoff => 0);
        remove_exact_marker(%opt);
    } else {
        recover_quarantines($opt{state_dir});
        my $receipt = release_record(%opt);
        die "ERROR: dorado_only generation lacks authenticated early-release receipt\n"
            if !defined($receipt) || $receipt->{reason} ne 'dorado_only_early';
        die "ERROR: dorado_only generation was revoked\n"
            if defined(revocation_record(%opt));
        remove_exact_marker(%opt);
    }
} elsif ($command eq 'abort') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    release_generation(
        %opt, reason => 'pre_handoff_abort', unless_handoff => 1, best_effort => 1,
    );
} else {
    usage();
}

exit 0;
