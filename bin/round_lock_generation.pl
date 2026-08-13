#!/usr/bin/env perl

use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use Errno qw(EACCES EAGAIN EEXIST ELOOP ENOENT EPERM EWOULDBLOCK);
use Fcntl qw(:DEFAULT :flock O_NOFOLLOW O_RDONLY);
use File::Basename qw(dirname);
use File::Path qw(make_path);
use File::Spec;
use Getopt::Long qw(GetOptionsFromArray Configure);
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
my $OPERATOR_SCHEMA = '2';
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
  operator-quarantine-invalid
                 preserve an explicitly identified invalid lock outside runtime
                 recovery after an operator has stopped all related tasks
  operator-quarantine-unrecoverable
                 preserve an exact valid generation whose subordinate state is
                 structurally unrecoverable, after every writer is stopped

Runtime options:
  --state-dir DIR --round-barcode NAME --scope full_round|dorado_only
  --token HEX64 --pin-token HEX64 --owner-pid PID --role NAME
  --stale-seconds N --wait-seconds N
  For response-loss recovery, callers must durably preallocate and retain an
  exact distinct --token/--pin-token pair before acquire, or an exact
  --pin-token before pin, then retry the identical request. Tokenless calls
  preserve the legacy interface but cannot recover a response that was lost.
  --best-effort suppresses only an abort/unpin authority miss after the state
  fence is acquired; fence, recovery, and malformed-state errors remain fatal

Operator quarantine options:
  --state-dir DIR [--wait-seconds N]
  --expected-lock-dev N --expected-lock-ino N --operation-token HEX64
  --operator-label TEXT --reason TEXT --confirm-invalid-snapshot
  [--source-name BASENAME]

Stopped-world valid-generation quarantine additionally requires:
  --expected-generation-token HEX64 --confirm-stopped-world
  --confirm-abandon-generation HEX64
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

sub write_stdout {
    my ($content) = @_;
    local $SIG{PIPE} = 'IGNORE';
    my $offset = 0;
    while ($offset < length($content)) {
        my $written = syswrite(
            STDOUT, $content, length($content) - $offset, $offset,
        );
        die "ERROR: cannot write stdout: $!\n"
            if !defined($written) || $written == 0;
        $offset += $written;
    }
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
sub operator_events_dir { return File::Spec->catdir($_[0], '.round_lock_operator_events'); }
sub operator_pending_dir { return File::Spec->catdir($_[0], '.round_lock_operator_pending'); }
sub terminal_archive_dir { return File::Spec->catdir($_[0], '.round_lock_archives'); }

sub sync_directory {
    my ($path) = @_;
    sysopen(my $fh, $path, O_RDONLY)
        or die "ERROR: cannot open directory for sync '$path': $!\n";
    $fh->sync()
        or die "ERROR: cannot sync directory '$path': $!\n";
    close($fh) or die "ERROR: cannot close directory '$path': $!\n";
}

sub acquire_state_fence {
    my ($state_dir, $nonblocking) = @_;
    my @before = lstat($state_dir);
    die "ERROR: cannot inspect round-lock state directory '$state_dir': $!\n"
        if !@before;
    die "ERROR: round-lock state directory is not a real directory: $state_dir\n"
        if -l _ || !-d _;
    sysopen(my $fh, $state_dir, O_RDONLY)
        or die "ERROR: cannot open round-lock state fence '$state_dir': $!\n";
    my @opened = stat($fh);
    die "ERROR: cannot inspect round-lock state fence '$state_dir': $!\n"
        if !@opened;
    die "ERROR: round-lock state directory changed while opening its fence\n"
        if $opened[0] != $before[0] || $opened[1] != $before[1] || !-d _;
    if (!flock($fh, LOCK_EX | ($nonblocking ? LOCK_NB : 0))) {
        my $contended = $nonblocking && ($!{EAGAIN} || $!{EWOULDBLOCK});
        my $error = "$!";
        close($fh);
        return undef if $contended;
        die "ERROR: cannot acquire round-lock state fence '$state_dir': $error\n";
    }
    my @current = lstat($state_dir);
    die "ERROR: round-lock state directory changed while waiting for its fence\n"
        if !@current || -l _ || !-d _
        || $current[0] != $opened[0] || $current[1] != $opened[1];
    my $archive_dir = terminal_archive_dir($state_dir);
    my @archive_st = lstat($archive_dir);
    if (@archive_st) {
        die "ERROR: terminal round-lock archive is not a real directory: $archive_dir\n"
            if -l _ || !-d _;
        # A terminal archive rename can be visible before the archive-parent
        # fsync.  Every later cooperating command adopts that visible state by
        # repeating the missing durability barrier while it owns the fence.
        sync_directory($archive_dir);
    } elsif (!$!{ENOENT}) {
        die "ERROR: cannot inspect terminal round-lock archive '$archive_dir': $!\n";
    }
    # Re-establish parent-directory durability for a visible state transition
    # whose previous owner may have died between rename/link and its fsync.  A
    # terminal rename is adopted destination-first so a second crash cannot
    # persist source removal without the archived destination name.
    sync_directory($state_dir);
    return $fh;
}

sub release_state_fence {
    my ($fh) = @_;
    return if !defined($fh);
    flock($fh, LOCK_UN)
        or die "ERROR: cannot release round-lock state fence: $!\n";
    close($fh) or die "ERROR: cannot close round-lock state fence: $!\n";
}

sub acquire_state_fence_with_wait {
    my ($state_dir, $wait_seconds) = @_;
    my $waited_ms = 0;
    my $limit_ms = $wait_seconds * 1000;
    while (1) {
        my $fh = acquire_state_fence($state_dir, 1);
        return $fh if defined($fh);
        die "ERROR: timed out waiting for round-lock state fence\n"
            if $limit_ms > 0 && $waited_ms >= $limit_ms;
        usleep(100_000);
        $waited_ms += 100;
    }
}

sub configured_failpoint {
    return $ENV{RTBIOSCAN_ROUND_LOCK_FAILPOINT} // '';
}

sub validate_failpoint_configuration {
    my $name = configured_failpoint();
    return if $name eq '';
    return if $name =~ /\A(?:unlink-ready-pin-before-open|replace-ready-pin-with-file|replace-ready-pin-with-symlink):[0-9a-f]{64}\z/;
    my %known = map { $_ => 1 } qw(
        after-lock-mkdir-before-stat
        after-lock-stat-before-pins
        after-pins-sync-before-generation
        after-generation-install
        after-transition-temp-partial-write
        after-transition-temp-write-before-link
        after-transition-link-before-temp-unlink
        after-transition-temp-unlink-before-sync
        after-operator-intent
        after-operator-intent-link-before-sync
        after-operator-rename-before-sync
        after-operator-sync-before-complete
        after-operator-complete-link-before-sync
        after-transition-install
        after-quarantine-rename
        before-quarantine-recovery-outcome
        after-transition-receipt-before-event
        after-transition-outcome-before-archive
        after-terminal-archive-rename-before-sync
        before-compat-publish
    );
    die "ERROR: unknown round-lock failpoint: $name\n" if !$known{$name};
}

sub test_pause {
    my ($name) = @_;
    return if configured_failpoint() ne $name;
    my $ready = validate_text(
        'RTBIOSCAN_ROUND_LOCK_TEST_READY',
        $ENV{RTBIOSCAN_ROUND_LOCK_TEST_READY},
    );
    my $release = validate_text(
        'RTBIOSCAN_ROUND_LOCK_TEST_RELEASE',
        $ENV{RTBIOSCAN_ROUND_LOCK_TEST_RELEASE},
    );
    my $timeout = $ENV{RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS} // '30';
    $timeout = validate_uint('RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS', $timeout);
    sysopen(my $ready_fh, $ready, O_WRONLY | O_CREAT | O_EXCL, 0600)
        or die "ERROR: cannot publish failpoint readiness '$ready': $!\n";
    print {$ready_fh} "$name\n"
        or die "ERROR: cannot write failpoint readiness '$ready': $!\n";
    $ready_fh->flush()
        or die "ERROR: cannot flush failpoint readiness '$ready': $!\n";
    $ready_fh->sync()
        or die "ERROR: cannot sync failpoint readiness '$ready': $!\n";
    close($ready_fh)
        or die "ERROR: cannot close failpoint readiness '$ready': $!\n";
    my $deadline = time() + $timeout;
    while (1) {
        my @st = lstat($release);
        if (@st) {
            die "ERROR: failpoint release is not a regular non-symlink file: $release\n"
                if -l _ || !-f _;
            last;
        }
        die "ERROR: cannot inspect failpoint release '$release': $!\n"
            if !$!{ENOENT};
        die "ERROR: timed out waiting for failpoint release: $name\n"
            if time() >= $deadline;
        usleep(10_000);
    }
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

sub path_occupied_nofollow {
    my ($path, $description) = @_;
    my @st = lstat($path);
    return 1 if @st;
    return 0 if $!{ENOENT};
    die "ERROR: cannot inspect $description '$path': $!\n";
}

sub assert_fenced_directory_move_ready {
    my (%arg) = @_;
    my @source = lstat($arg{source});
    die "ERROR: cannot inspect fenced directory move source '$arg{source}': $!\n"
        if !@source;
    die "ERROR: fenced directory move source is not a real directory: $arg{source}\n"
        if -l _ || !-d _;
    die "ERROR: fenced directory move source identity changed: $arg{source}\n"
        if $source[0] != $arg{expected_dev}
        || $source[1] != $arg{expected_ino};
    my @destination = lstat($arg{destination});
    die "ERROR: fenced directory move destination already exists: $arg{destination}\n"
        if @destination;
    die "ERROR: cannot inspect fenced directory move destination "
        . "'$arg{destination}': $!\n"
        if !$!{ENOENT};
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
    my $is_transition_temp = $path =~ m{/\.transition-[0-9a-f]{64}\.tmp\z};
    my $written = 1;
    if ($is_transition_temp
        && configured_failpoint() eq 'after-transition-temp-partial-write') {
        my $length = int(length($content) / 2) || 1;
        $written = print {$fh} substr($content, 0, $length);
        if ($written) {
            $fh->flush()
                or die "ERROR: cannot flush partial transition '$path': $!\n";
            test_pause('after-transition-temp-partial-write');
            $written = print {$fh} substr($content, $length);
        }
    } else {
        $written = print {$fh} $content;
    }
    if (!$written) {
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
    my ($path, $content, $after_link_failpoint) = @_;
    my $tmp = "$path.tmp-" . new_token($path);
    write_temp_file($tmp, $content);
    my $linked = link($tmp, $path);
    my $exists = !$linked && $!{EEXIST};
    my $error = $linked ? '' : "$!";
    unlink($tmp);
    test_pause($after_link_failpoint)
        if $linked && defined($after_link_failpoint);
    # An existing immutable name may have been linked by a process that died
    # before syncing its parent. Adoption repeats the same durability barrier.
    sync_directory(dirname($path)) if $linked || $exists;
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
    # Every mutation below aborts with a distinct setup error if it cannot
    # establish the requested interleaving. Injection that silently fails would
    # let the code under test run unperturbed while the test still reports the
    # outcome it was asserting, which is a false green.
    my $failpoint = $ENV{RTBIOSCAN_ROUND_LOCK_FAILPOINT} // '';
    if ($failpoint =~ /\Aunlink-ready-pin-before-open:([0-9a-f]{64})\z/
        && $path =~ m{/ready\.\Q$1\E\.tsv\z}) {
        unlink($path)
            or die "ERROR: failpoint setup failed: cannot remove '$path': $!\n";
    }
    if ($failpoint =~ /\Areplace-ready-pin-with-file:([0-9a-f]{64})\z/
        && $path =~ m{/ready\.\Q$1\E\.tsv\z}) {
        # Substitute a different regular file, which the dev/ino check rejects.
        my $decoy = "$path.decoy";
        open(my $decoy_fh, '>', $decoy)
            or die "ERROR: failpoint setup failed: cannot create decoy "
                . "'$decoy': $!\n";
        print {$decoy_fh} "decoy\n"
            or die "ERROR: failpoint setup failed: cannot write decoy "
                . "'$decoy': $!\n";
        close($decoy_fh)
            or die "ERROR: failpoint setup failed: cannot close decoy "
                . "'$decoy': $!\n";
        rename($decoy, $path)
            or die "ERROR: failpoint setup failed: cannot install decoy at "
                . "'$path': $!\n";
    }
    if ($failpoint =~ /\Areplace-ready-pin-with-symlink:([0-9a-f]{64})\z/
        && $path =~ m{/ready\.\Q$1\E\.tsv\z}) {
        # Substitute a symlink to the candidate hard link. Both names share one
        # inode, so this defeats a dev/ino comparison and is refused only by
        # O_NOFOLLOW.
        (my $candidate = $path) =~ s{/ready\.}{/candidate.};
        unlink($path)
            or die "ERROR: failpoint setup failed: cannot remove '$path': $!\n";
        symlink($candidate, $path)
            or die "ERROR: failpoint setup failed: cannot symlink '$path' -> "
                . "'$candidate': $!\n";
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
my @RELEASE_ORDER = qw(schema token round_barcode scope outcome reason effective_ttl_seconds release_transition_epoch lock_dev lock_ino operation_token);
my @REVOCATION_ORDER = qw(schema token round_barcode scope outcome reason effective_ttl_seconds reclaim_transition_epoch lock_dev lock_ino operation_token);
my @EVENT_ORDER = qw(schema event_id generation_token round_barcode scope event outcome effective_ttl_seconds event_epoch lock_dev lock_ino);
my @INFLIGHT_ORDER = qw(round_barcode started_utc read_file generation_token scope lock_dev lock_ino);
my @OPERATOR_EVENT_ORDER = qw(schema operation_token operation_kind expected_generation_token recovery_basis phase operator_label reason source_name destination_name lock_dev lock_ino tree_sha256 generation_status generation_entry_kind generation_sha256 generation_entry_fingerprint pins_status pins_entry_kind pins_sha256 pins_entry_fingerprint transition_status transition_entry_kind transition_sha256 transition_entry_fingerprint marker_status marker_entry_kind marker_sha256 marker_entry_fingerprint release_authority_status finalization_status finalization_sha256 event_epoch outcome);
my @OPERATOR_EVIDENCE_FIELDS = qw(
    recovery_basis tree_sha256
    generation_status generation_entry_kind generation_sha256
    generation_entry_fingerprint pins_status pins_entry_kind pins_sha256
    pins_entry_fingerprint transition_status transition_entry_kind
    transition_sha256 transition_entry_fingerprint marker_status
    marker_entry_kind marker_sha256 marker_entry_fingerprint
    release_authority_status finalization_status finalization_sha256
);

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

sub operator_evidence_entry_shallow {
    my ($path) = @_;
    my @before = lstat($path);
    return {
        entry_kind => 'absent', sha256 => 'none', fingerprint => 'none',
    } if !@before && $!{ENOENT};
    die "ERROR: cannot inspect operator evidence '$path': $!\n" if !@before;
    my $kind = -l _ ? 'symlink'
        : -f _ ? 'regular'
        : -d _ ? 'directory'
        : -p _ ? 'fifo'
        : 'other';
    my $link_target = $kind eq 'symlink' ? readlink($path) : '';
    die "ERROR: cannot read operator evidence link '$path': $!\n"
        if $kind eq 'symlink' && !defined($link_target);
    my $metadata = join(
        "\0", $kind, @before[0 .. 7], @before[9 .. 10], $link_target,
    );
    my $fingerprint = sha256_hex($metadata);
    return {
        entry_kind => $kind, sha256 => 'none', fingerprint => $fingerprint,
    } if $kind ne 'regular';
    my $fh;
    if (!sysopen($fh, $path, O_RDONLY | O_NOFOLLOW)) {
        return {
            entry_kind => 'regular-unreadable', sha256 => 'none',
            fingerprint => $fingerprint,
        } if $!{EACCES} || $!{EPERM};
        die "ERROR: cannot read operator evidence '$path': $!\n";
    }
    binmode($fh, ':raw')
        or die "ERROR: cannot set raw mode on operator evidence '$path': $!\n";
    my @opened = stat($fh);
    die "ERROR: cannot inspect open operator evidence '$path': $!\n"
        if !@opened;
    die "ERROR: operator evidence changed while opening: $path\n"
        if $opened[0] != $before[0] || $opened[1] != $before[1] || !-f _;
    my $digest = Digest::SHA->new(256);
    $digest->addfile($fh);
    close($fh) or die "ERROR: cannot close operator evidence '$path': $!\n";
    return {
        entry_kind => 'regular', sha256 => $digest->hexdigest(),
        fingerprint => $fingerprint,
    };
}

sub operator_directory_manifest_add {
    my ($digest, $directory, $prefix) = @_;
    # Adopt every visible descendant namespace before treating its names as
    # durable operator evidence. A writer may have died after linking a child
    # and before syncing this particular directory.
    sync_directory($directory);
    opendir(my $dh, $directory)
        or die "ERROR: cannot inspect operator evidence directory '$directory': $!\n";
    my @entries = sort grep { $_ ne '.' && $_ ne '..' } readdir($dh);
    closedir($dh)
        or die "ERROR: cannot close operator evidence directory '$directory': $!\n";
    for my $entry (@entries) {
        my $path = File::Spec->catfile($directory, $entry);
        my $relative = $prefix eq '' ? $entry : "$prefix/$entry";
        my $evidence = operator_evidence_entry_shallow($path);
        for my $value (
            $relative, $evidence->{entry_kind}, $evidence->{sha256},
            $evidence->{fingerprint},
        ) {
            $digest->add(pack('N', length($value)), $value);
        }
        operator_directory_manifest_add($digest, $path, $relative)
            if $evidence->{entry_kind} eq 'directory';
    }
}

sub operator_directory_manifest_sha256 {
    my ($directory) = @_;
    my $digest = Digest::SHA->new(256);
    operator_directory_manifest_add($digest, $directory, '');
    return $digest->hexdigest();
}

sub operator_evidence_entry {
    my ($path) = @_;
    my $evidence = operator_evidence_entry_shallow($path);
    $evidence->{sha256} = operator_directory_manifest_sha256($path)
        if $evidence->{entry_kind} eq 'directory';
    return $evidence;
}

sub lock_snapshot {
    my ($dir) = @_;
    my @dir_st = lstat($dir);
    return undef if !@dir_st && $!{ENOENT};
    die "ERROR: cannot inspect round lock '$dir': $!\n" if !@dir_st;
    die "ERROR: round lock is not a regular directory: $dir\n"
        if !-d _ || -l _;
    # A visible nested generation/transition entry may have been linked by a
    # process that died before syncing this directory. Snapshot adoption
    # repeats that durability barrier before trusting either name.
    sync_directory($dir);
    my $snapshot = {
        dev => $dir_st[0], ino => $dir_st[1], newest_epoch => $dir_st[9],
        valid => 0, generation_status => 'absent',
    };
    my $generation;
    my $ok = eval { $generation = generation_record($dir); 1 };
    if (!$ok) {
        $snapshot->{generation_status} = 'parse-invalid';
        return $snapshot;
    }
    return $snapshot if !defined($generation);
    $snapshot->{generation_status} = 'semantic-invalid';
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
        $snapshot->{generation_status} = 'valid';
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

sub invalid_snapshot_error {
    my ($path, $snapshot) = @_;
    my $status = $snapshot->{generation_status} // 'invalid';
    return "ERROR: round lock has $status generation state and requires "
        . "operator quarantine: $path\n";
}

sub operator_transition_status {
    my ($dir, $entry, $snapshot) = @_;
    my $path = transition_path($dir);
    return 'absent' if $entry->{entry_kind} eq 'absent';
    my $record;
    my $ok = eval { $record = transition_record($dir); 1 };
    return 'parse-invalid' if !$ok || !defined($record);
    return 'semantic-invalid'
        if !defined($snapshot) || !$snapshot->{valid}
        || !validate_transition_for_snapshot($record, $snapshot);
    return $record->{action} eq 'reclaim'
        ? 'valid-reclaim' : 'valid-release';
}

sub operator_pins_evidence {
    my ($dir, $snapshot) = @_;
    my $path = pins_dir($dir);
    my $entry_evidence = operator_evidence_entry($path);
    return {
        status => 'missing', %{$entry_evidence},
    } if $entry_evidence->{entry_kind} eq 'absent';
    return {
        status => 'unsafe', %{$entry_evidence},
    } if $entry_evidence->{entry_kind} ne 'directory';
    sync_directory($path);
    opendir(my $dh, $path)
        or die "ERROR: cannot inspect generation pins '$path': $!\n";
    my @entries = sort grep { $_ ne '.' && $_ ne '..' } readdir($dh);
    closedir($dh)
        or die "ERROR: cannot close generation pins '$path': $!\n";
    my $status = 'healthy';
    my @ready_tokens;
    for my $entry (@entries) {
        next if $entry !~ /\Aready\.([0-9a-f]{64})\.tsv\z/;
        my $pin_token = $1;
        my $pin;
        my $ok = eval {
            $pin = record_for(
                pin_ready_path($dir, $pin_token), \@PIN_ORDER,
            );
            1;
        };
        $status = 'record-invalid'
            if !$ok || !defined($pin)
            || !validate_pin_for_snapshot($pin, $snapshot)
            || $pin->{pin_token} ne $pin_token;
        push(@ready_tokens, $pin_token)
            if $ok && defined($pin)
            && validate_pin_for_snapshot($pin, $snapshot)
            && $pin->{pin_token} eq $pin_token;
    }
    return {
        status => $status,
        entry_kind => 'directory',
        sha256 => operator_directory_manifest_sha256($path),
        fingerprint => $entry_evidence->{fingerprint},
        ready_tokens => \@ready_tokens,
    };
}

sub operator_marker_evidence {
    my ($state_dir, $snapshot) = @_;
    return {
        status => 'not-applicable', entry_kind => 'absent',
        sha256 => 'none', fingerprint => 'none',
    } if !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    my $path = marker_path($state_dir, $generation->{token});
    my $evidence = operator_evidence_entry($path);
    return { status => 'absent', %{$evidence} }
        if $evidence->{entry_kind} eq 'absent';
    my $marker;
    my $ok = eval { $marker = record_for($path, \@MARKER_ORDER); 1 };
    my $valid = $ok && defined($marker)
        && $marker->{schema} eq $SCHEMA
        && $marker->{token} eq $generation->{token}
        && $marker->{round_barcode} eq $generation->{round_barcode}
        && $marker->{scope} eq $generation->{scope}
        && $marker->{outcome} eq 'handoff'
        && $marker->{created_epoch} =~ /\A[0-9]+\z/;
    return { status => $valid ? 'valid' : 'invalid', %{$evidence} };
}

sub operator_release_authority_status {
    my (%arg) = @_;
    return 'not-applicable'
        if $arg{transition_status} ne 'valid-release';
    my $snapshot = $arg{snapshot};
    my $generation = $snapshot->{generation};
    my $transition = transition_record($arg{source_path});
    my $reason = $transition->{reason};
    my $reason_matches_scope = $generation->{scope} eq 'full_round'
        ? ($reason eq 'full_round_released' || $reason eq 'pre_handoff_abort')
        : ($reason eq 'dorado_only_early' || $reason eq 'pre_handoff_abort');
    return 'reason-scope-invalid' if !$reason_matches_scope;
    if ($reason eq 'pre_handoff_abort') {
        return 'marker-conflict' if $arg{marker_status} ne 'absent';
    } else {
        return 'marker-missing' if $arg{marker_status} eq 'absent';
        return 'marker-invalid' if $arg{marker_status} ne 'valid';
    }
    return 'pin-invalid'
        if $arg{pins_status} eq 'missing' || $arg{pins_status} eq 'unsafe';
    my $pin_path = pin_ready_path(
        $arg{source_path}, $transition->{allowed_pin_token},
    );
    my $pin;
    my $ok = eval { $pin = record_for($pin_path, \@PIN_ORDER); 1 };
    return 'pin-missing' if $ok && !defined($pin);
    return 'pin-invalid'
        if !$ok || !validate_pin_for_snapshot($pin, $snapshot)
        || $pin->{pin_token} ne $transition->{allowed_pin_token};
    return 'pin-role-invalid'
        if $pin->{role} ne release_pin_role($reason);
    return 'valid';
}

sub operator_recovery_basis {
    my (%arg) = @_;
    my $snapshot = $arg{snapshot};
    return 'generation-invalid' if !$snapshot->{valid};
    my $transition_status = $arg{transition_status};
    if ($arg{source_name}
        =~ /\A\.round_inflight\.lockdir\.(reclaim|release)-([0-9a-f]{64})\z/) {
        my ($name_action, $name_token) = ($1, $2);
        return 'transition-orphan-invalid'
            if $transition_status eq 'absent'
            || $transition_status eq 'parse-invalid'
            || $transition_status eq 'semantic-invalid';
        my $transition = transition_record($arg{source_path});
        return 'transition-orphan-invalid'
            if !defined($transition)
            || $transition->{action} ne $name_action
            || $transition->{operation_token} ne $name_token;
        return 'release-authority-invalid'
            if $transition_status eq 'valid-release'
            && $arg{release_authority_status} ne 'valid';
        return 'healthy';
    }
    return 'release-authority-invalid'
        if $transition_status eq 'valid-release'
        && $arg{release_authority_status} ne 'valid';
    return 'marker-invalid'
        if $transition_status eq 'absent'
        && $arg{marker_status} eq 'invalid';
    my $pins_status = $arg{pins_status};
    return 'pins-missing' if $pins_status eq 'missing';
    return 'pins-unsafe' if $pins_status eq 'unsafe';
    return 'pins-record-invalid' if $pins_status eq 'record-invalid';
    return 'transition-parse-invalid'
        if $transition_status eq 'parse-invalid';
    return 'transition-semantic-invalid'
        if $transition_status eq 'semantic-invalid';
    return 'healthy';
}

sub operator_named_evidence_sha256 {
    my (@items) = @_;
    my $digest = Digest::SHA->new(256);
    my $domain = 'RTBioScan-round-lock-operator-finalization-v1';
    $digest->add(pack('N', length($domain)), $domain);
    for my $item (@items) {
        my ($label, $evidence) = @{$item};
        for my $value (
            $label, $evidence->{entry_kind}, $evidence->{sha256},
            $evidence->{fingerprint},
        ) {
            $digest->add(pack('N', length($value)), $value);
        }
    }
    return $digest->hexdigest();
}

sub operator_existing_record_matches {
    my ($path, $order_ref, $expected_ref, $evidence) = @_;
    return 1 if $evidence->{entry_kind} eq 'absent';
    return 0 if $evidence->{entry_kind} ne 'regular';
    my $record;
    my $ok = eval { $record = record_for($path, $order_ref); 1 };
    return 0 if !$ok || !defined($record);
    return canonical_body($record, $order_ref)
        eq canonical_body($expected_ref, $order_ref);
}

sub assert_operator_abandonment_global_paths_safe {
    my ($state_dir) = @_;
    my $compat_path = inflight_compat_path($state_dir);
    my @compat_st = lstat($compat_path);
    if (@compat_st) {
        die "ERROR: operator quarantine cannot restore liveness while the "
            . "compatibility inflight diagnostic is a directory: $compat_path\n"
            if !-l _ && -d _;
    } elsif (!$!{ENOENT}) {
        die "ERROR: cannot inspect compatibility inflight diagnostic "
            . "'$compat_path': $!\n";
    }

    my $event_dir = events_dir($state_dir);
    my @event_dir_st = lstat($event_dir);
    if (@event_dir_st) {
        die "ERROR: operator quarantine cannot restore liveness with an "
            . "unsafe round-lock event directory: $event_dir\n"
            if -l _ || !-d _;
        sync_directory($event_dir);
    } elsif (!$!{ENOENT}) {
        die "ERROR: cannot inspect round-lock event directory '$event_dir': $!\n";
    }
}

sub operator_finalization_evidence {
    my (%arg) = @_;
    return { status => 'not-applicable', sha256 => 'none' }
        if $arg{local_basis} ne 'healthy'
        || $arg{transition_status}
            !~ /\A(?:valid-reclaim|valid-release)\z/;
    my $transition = transition_record($arg{source_path});
    return { status => 'not-applicable', sha256 => 'none' }
        if !defined($transition);
    my $is_canonical = $arg{source_name} eq '.round_inflight.lockdir';
    if (!$is_canonical) {
        return { status => 'not-applicable', sha256 => 'none' }
            if $arg{source_name}
                !~ /\A\.round_inflight\.lockdir\.(reclaim|release)-([0-9a-f]{64})\z/
            || $transition->{action} ne $1
            || $transition->{operation_token} ne $2;
    }
    return { status => 'not-applicable', sha256 => 'none' }
        if $transition->{action} eq 'release'
        && $arg{release_authority_status} ne 'valid';
    if ($is_canonical) {
        return { status => 'not-applicable', sha256 => 'none' }
            if $arg{pins_status} ne 'healthy';
        my @ready = @{$arg{ready_tokens} // []};
        if ($transition->{action} eq 'release') {
            return { status => 'not-applicable', sha256 => 'none' }
                if grep { $_ ne $transition->{allowed_pin_token} } @ready;
        } else {
            return { status => 'not-applicable', sha256 => 'none' }
                if @ready;
        }
    }

    my $snapshot = $arg{snapshot};
    my $generation = $snapshot->{generation};
    my @items;
    my $status = 'ready';
    if ($is_canonical) {
        my $inflight_path = inflight_generation_path(
            $arg{state_dir}, $generation->{token},
        );
        my $inflight_evidence = operator_evidence_entry($inflight_path);
        push(@items, [
            ".round_inflight.$generation->{token}.tsv", $inflight_evidence,
        ]);
        if ($inflight_evidence->{entry_kind} ne 'absent') {
            my $inflight;
            my $ok = eval {
                $inflight = record_for($inflight_path, \@INFLIGHT_ORDER);
                1;
            };
            my $valid = $ok && defined($inflight)
                && $inflight->{generation_token} eq $generation->{token}
                && $inflight->{round_barcode} eq $generation->{round_barcode}
                && $inflight->{scope} eq $generation->{scope}
                && $inflight->{lock_dev} eq "$snapshot->{dev}"
                && $inflight->{lock_ino} eq "$snapshot->{ino}";
            $status = 'inflight-invalid' if !$valid;
            my $compat_path = inflight_compat_path($arg{state_dir});
            my $compat_evidence = operator_evidence_entry($compat_path);
            push(@items, ['round_inflight.txt', $compat_evidence]);
            $status = 'unsupported'
                if $compat_evidence->{entry_kind} eq 'directory';
            if ($valid && $compat_evidence->{entry_kind} ne 'absent') {
                my $compat_body = join(
                    '',
                    "round_barcode=$inflight->{round_barcode}\n",
                    "started_utc=$inflight->{started_utc}\n",
                    "read_file=$inflight->{read_file}\n",
                    "generation_token=$inflight->{generation_token}\n",
                    "scope=$inflight->{scope}\n",
                    "lock_dev=$inflight->{lock_dev}\n",
                    "lock_ino=$inflight->{lock_ino}\n",
                );
                my $expected_compat = $compat_body
                    . 'record_sha256=' . sha256_hex($compat_body) . "\n";
                $status = 'compat-invalid'
                    if $status eq 'ready'
                    && ($compat_evidence->{entry_kind} ne 'regular'
                        || $compat_evidence->{sha256}
                            ne sha256_hex($expected_compat));
            }
        }
    }

    my ($receipt_path, $receipt_order, %receipt);
    if ($transition->{action} eq 'release') {
        $receipt_path = release_path(
            $arg{state_dir}, $generation->{token},
        );
        $receipt_order = \@RELEASE_ORDER;
        %receipt = (
            schema => $SCHEMA, token => $generation->{token},
            round_barcode => $generation->{round_barcode},
            scope => $generation->{scope}, outcome => 'released',
            reason => $transition->{reason},
            effective_ttl_seconds => $generation->{effective_ttl_seconds},
            release_transition_epoch => $transition->{started_epoch},
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            operation_token => $transition->{operation_token},
        );
    } else {
        $receipt_path = revocation_path(
            $arg{state_dir}, $generation->{token},
        );
        $receipt_order = \@REVOCATION_ORDER;
        %receipt = (
            schema => $SCHEMA, token => $generation->{token},
            round_barcode => $generation->{round_barcode},
            scope => $generation->{scope}, outcome => 'revoked',
            reason => $transition->{reason},
            effective_ttl_seconds => $generation->{effective_ttl_seconds},
            reclaim_transition_epoch => $transition->{started_epoch},
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            operation_token => $transition->{operation_token},
        );
    }
    my $receipt_evidence = operator_evidence_entry($receipt_path);
    my $receipt_relative = $transition->{action} eq 'release'
        ? ".round_lock_release.$generation->{token}.tsv"
        : ".round_lock_revocation.$generation->{token}.tsv";
    push(@items, [$receipt_relative, $receipt_evidence]);

    my $opposite_path = $transition->{action} eq 'release'
        ? revocation_path($arg{state_dir}, $generation->{token})
        : release_path($arg{state_dir}, $generation->{token});
    my $opposite_relative = $transition->{action} eq 'release'
        ? ".round_lock_revocation.$generation->{token}.tsv"
        : ".round_lock_release.$generation->{token}.tsv";
    my $opposite_evidence = operator_evidence_entry($opposite_path);
    push(@items, [$opposite_relative, $opposite_evidence]);
    $status = 'opposite-outcome-conflict'
        if $status eq 'ready'
        && $opposite_evidence->{entry_kind} ne 'absent';
    $status = 'receipt-invalid'
        if $status eq 'ready'
        && !operator_existing_record_matches(
            $receipt_path, $receipt_order, \%receipt, $receipt_evidence,
        );

    my $event_dir = events_dir($arg{state_dir});
    my @event_dir_st = lstat($event_dir);
    if (@event_dir_st) {
        die "ERROR: round-lock event directory is not a real directory: $event_dir\n"
            if -l _ || !-d _;
        sync_directory($event_dir);
    } elsif (!$!{ENOENT}) {
        die "ERROR: cannot inspect round-lock event directory '$event_dir': $!\n";
    }
    my $event_action = $transition->{action};
    my $event_path = File::Spec->catfile(
        $event_dir,
        join(
            '.', $generation->{token}, $event_action,
            $transition->{operation_token},
        ) . '.tsv',
    );
    my %event = (
        schema => $SCHEMA, event_id => $transition->{operation_token},
        generation_token => $generation->{token},
        round_barcode => $generation->{round_barcode},
        scope => $generation->{scope}, event => $event_action,
        outcome => $transition->{action} eq 'reclaim'
            ? 'quarantined' : $transition->{reason},
        effective_ttl_seconds => $generation->{effective_ttl_seconds},
        event_epoch => $transition->{started_epoch},
        lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
    );
    my $event_evidence = operator_evidence_entry($event_path);
    my $event_relative = join(
        '/', '.round_lock_events',
        join(
            '.', $generation->{token}, $event_action,
            $transition->{operation_token},
        ) . '.tsv',
    );
    push(@items, [$event_relative, $event_evidence]);
    $status = 'event-invalid'
        if $status eq 'ready'
        && !operator_existing_record_matches(
            $event_path, \@EVENT_ORDER, \%event, $event_evidence,
        );

    my $archive_path = File::Spec->catdir(
        terminal_archive_dir($arg{state_dir}),
        "$transition->{action}-$transition->{operation_token}",
    );
    my $archive_evidence = operator_evidence_entry($archive_path);
    push(@items, [
        ".round_lock_archives/$transition->{action}-$transition->{operation_token}",
        $archive_evidence,
    ]);
    $status = 'archive-collision'
        if $status eq 'ready'
        && $archive_evidence->{entry_kind} ne 'absent';
    return {
        status => $status,
        sha256 => operator_named_evidence_sha256(@items),
    };
}

sub operator_snapshot_matches_policy {
    my ($snapshot, $basis, $arg_ref) = @_;
    if ($arg_ref->{operation_kind} eq 'invalid_snapshot') {
        return !$snapshot->{valid} && $basis eq 'generation-invalid';
    }
    return $snapshot->{valid}
        && $snapshot->{generation}->{token}
            eq $arg_ref->{expected_generation_token}
        && $basis ne 'healthy'
        && $basis ne 'generation-invalid';
}

sub operator_policy_error {
    my ($arg_ref, $basis) = @_;
    return "ERROR: operator quarantine refuses a valid generation\n"
        if $arg_ref->{operation_kind} eq 'invalid_snapshot';
    return "ERROR: operator quarantine generation token does not match confirmation\n"
        if $basis eq 'generation-token-mismatch';
    return "ERROR: operator quarantine refuses a structurally recoverable generation\n";
}

sub operator_event_path {
    my ($state_dir, $operation_token, $phase) = @_;
    return File::Spec->catfile(
        operator_events_dir($state_dir), "$operation_token.$phase.tsv",
    );
}

sub operator_pending_path {
    my ($state_dir, $operation_token) = @_;
    return File::Spec->catfile(
        operator_pending_dir($state_dir), "$operation_token.tsv",
    );
}

sub validate_operator_events_directory_if_present {
    my ($state_dir) = @_;
    my $path = operator_events_dir($state_dir);
    my @st = lstat($path);
    return if !@st && $!{ENOENT};
    die "ERROR: cannot inspect operator event directory '$path': $!\n"
        if !@st;
    die "ERROR: operator event directory is not a real directory: $path\n"
        if -l _ || !-d _;
}

sub validate_operator_pending_directory_if_present {
    my ($state_dir) = @_;
    my $path = operator_pending_dir($state_dir);
    my @st = lstat($path);
    return if !@st && $!{ENOENT};
    die "ERROR: cannot inspect operator pending directory '$path': $!\n"
        if !@st;
    die "ERROR: operator pending directory is not a real directory: $path\n"
        if -l _ || !-d _;
}

sub operator_event_record {
    my ($state_dir, $operation_token, $phase) = @_;
    return record_for(
        operator_event_path($state_dir, $operation_token, $phase),
        \@OPERATOR_EVENT_ORDER,
    );
}

sub operator_pending_record {
    my ($state_dir, $operation_token) = @_;
    return record_for(
        operator_pending_path($state_dir, $operation_token),
        \@OPERATOR_EVENT_ORDER,
    );
}

sub install_operator_pending {
    my ($state_dir, $intent) = @_;
    ensure_real_directory(operator_pending_dir($state_dir), 0700);
    my $path = operator_pending_path(
        $state_dir, $intent->{operation_token},
    );
    my $audit_path = operator_event_path(
        $state_dir, $intent->{operation_token}, 'intent',
    );
    my @audit_st = lstat($audit_path);
    die "ERROR: operator intent is not a regular non-symlink file\n"
        if !@audit_st || -l _ || !-f _;
    my $linked = link($audit_path, $path);
    my $exists = !$linked && $!{EEXIST};
    my $error = $linked ? '' : "$!";
    die "ERROR: cannot install operator pending intent: $error\n"
        if !$linked && !$exists;
    # Repeat the pending-directory barrier when adopting an existing link.
    sync_directory(operator_pending_dir($state_dir));
    my $existing = operator_pending_record(
        $state_dir, $intent->{operation_token},
    );
    my @pending_st = lstat($path);
    die "ERROR: operator pending intent conflicts: $intent->{operation_token}\n"
        if !defined($existing) || !@pending_st || -l _ || !-f _
        || $pending_st[0] != $audit_st[0] || $pending_st[1] != $audit_st[1]
        || canonical_body($existing, \@OPERATOR_EVENT_ORDER)
            ne canonical_body($intent, \@OPERATOR_EVENT_ORDER);
}

sub remove_operator_pending {
    my ($state_dir, $intent) = @_;
    my $path = operator_pending_path(
        $state_dir, $intent->{operation_token},
    );
    my $existing = operator_pending_record(
        $state_dir, $intent->{operation_token},
    );
    return if !defined($existing);
    die "ERROR: operator pending intent conflicts: $intent->{operation_token}\n"
        if canonical_body($existing, \@OPERATOR_EVENT_ORDER)
        ne canonical_body($intent, \@OPERATOR_EVENT_ORDER);
    unlink($path)
        or die "ERROR: cannot retire completed operator pending intent '$path': $!\n";
    sync_directory(operator_pending_dir($state_dir));
}

sub operator_pending_tokens {
    my ($state_dir) = @_;
    validate_operator_pending_directory_if_present($state_dir);
    my $path = operator_pending_dir($state_dir);
    my @st = lstat($path);
    return () if !@st && $!{ENOENT};
    # Adopt a pending link (or removal) left visible by a process killed before
    # its own parent-directory barrier, before enumeration makes it authority.
    sync_directory($path);
    opendir(my $dh, $path)
        or die "ERROR: cannot inspect operator pending directory '$path': $!\n";
    my @tokens;
    for my $entry (readdir($dh)) {
        next if $entry eq '.' || $entry eq '..';
        if ($entry =~ /\A([0-9a-f]{64})\.tsv\z/) {
            push(@tokens, $1);
            next;
        }
        die "ERROR: unexpected entry in operator pending directory: $entry\n";
    }
    closedir($dh)
        or die "ERROR: cannot close operator pending directory '$path': $!\n";
    return sort @tokens;
}

sub install_operator_event {
    my (%arg) = @_;
    ensure_real_directory(operator_events_dir($arg{state_dir}), 0700);
    my $path = operator_event_path(
        $arg{state_dir}, $arg{operation_token}, $arg{phase},
    );
    my $after_link_failpoint = $arg{phase} eq 'intent'
        ? 'after-operator-intent-link-before-sync'
        : 'after-operator-complete-link-before-sync';
    my $installed = install_immutable(
        $path, checksummed_content(\%arg, \@OPERATOR_EVENT_ORDER),
        $after_link_failpoint,
    );
    if (!$installed) {
        my $existing = operator_event_record(
            $arg{state_dir}, $arg{operation_token}, $arg{phase},
        );
        die "ERROR: immutable operator event conflicts: $arg{operation_token}\n"
            if canonical_body($existing, \@OPERATOR_EVENT_ORDER)
            ne canonical_body(\%arg, \@OPERATOR_EVENT_ORDER);
        # Observing a matching immutable name does not prove the process that
        # linked it reached its parent-directory durability barrier.
        sync_directory(operator_events_dir($arg{state_dir}));
    }
}

sub valid_operator_entry_evidence {
    my ($record, $prefix) = @_;
    my $kind = $record->{"${prefix}_entry_kind"} // '';
    my $sha = $record->{"${prefix}_sha256"} // '';
    my $fingerprint = $record->{"${prefix}_entry_fingerprint"} // '';
    return $sha eq 'none' && $fingerprint eq 'none'
        if $kind eq 'absent';
    return $sha =~ $TOKEN_RE && $fingerprint =~ $TOKEN_RE
        if $kind eq 'regular';
    return $sha =~ $TOKEN_RE && $fingerprint =~ $TOKEN_RE
        if $kind eq 'directory';
    return $sha eq 'none' && $fingerprint =~ $TOKEN_RE
        if $kind =~ /\A(?:regular-unreadable|symlink|fifo|other)\z/;
    return 0;
}

sub valid_operator_pins_evidence {
    my ($record) = @_;
    my $status = $record->{pins_status} // '';
    my $kind = $record->{pins_entry_kind} // '';
    my $sha = $record->{pins_sha256} // '';
    my $fingerprint = $record->{pins_entry_fingerprint} // '';
    return $kind eq 'absent' && $sha eq 'none' && $fingerprint eq 'none'
        if $status eq 'missing';
    return $kind ne 'absent' && $kind ne 'directory'
        && valid_operator_entry_evidence($record, 'pins')
        if $status eq 'unsafe';
    return $kind eq 'directory' && $sha =~ $TOKEN_RE
        && $fingerprint =~ $TOKEN_RE
        if $status eq 'healthy' || $status eq 'record-invalid';
    return 0;
}

sub validate_operator_event_shape {
    my ($record, $operation_token, $phase) = @_;
    return 0 if !defined($record);
    return $record->{schema} eq $OPERATOR_SCHEMA
        && $record->{operation_token} eq $operation_token
        && $record->{operation_kind}
            =~ /\A(?:invalid_snapshot|abandon_unrecoverable_generation)\z/
        && (($record->{operation_kind} eq 'invalid_snapshot'
                && $record->{expected_generation_token} eq 'none'
                && $record->{recovery_basis} eq 'generation-invalid')
            || ($record->{operation_kind} eq 'abandon_unrecoverable_generation'
                && $record->{expected_generation_token} =~ $TOKEN_RE
                && $record->{recovery_basis}
                    =~ /\A(?:pins-missing|pins-unsafe|pins-record-invalid|transition-parse-invalid|transition-semantic-invalid|transition-orphan-invalid|marker-invalid|release-authority-invalid|finalization-invalid)\z/))
        && $record->{phase} eq $phase
        && $record->{operator_label} ne ''
        && $record->{operator_label} !~ /[\t\r\n]/
        && $record->{reason} ne ''
        && $record->{reason} !~ /[\t\r\n]/
        && $record->{source_name}
            =~ /\A\.round_inflight\.lockdir(?:\.(?:reclaim|release)-[0-9a-f]{64})?\z/
        && $record->{destination_name}
            eq ".round_inflight.lockdir.operator-$operation_token"
        && $record->{lock_dev} =~ /\A[0-9]+\z/
        && $record->{lock_ino} =~ /\A[0-9]+\z/
        && $record->{tree_sha256} =~ $TOKEN_RE
        && $record->{generation_status}
            =~ /\A(?:absent|parse-invalid|semantic-invalid|valid)\z/
        && (($record->{operation_kind} eq 'invalid_snapshot'
                && $record->{generation_status} ne 'valid')
            || ($record->{operation_kind}
                    eq 'abandon_unrecoverable_generation'
                && $record->{generation_status} eq 'valid'))
        && valid_operator_entry_evidence($record, 'generation')
        && (($record->{generation_status} eq 'absent')
            == ($record->{generation_entry_kind} eq 'absent'))
        && ($record->{generation_status} ne 'semantic-invalid'
            || $record->{generation_entry_kind} eq 'regular')
        && ($record->{generation_status} ne 'valid'
            || $record->{generation_entry_kind} eq 'regular')
        && $record->{pins_status}
            =~ /\A(?:missing|unsafe|healthy|record-invalid)\z/
        && valid_operator_pins_evidence($record)
        && $record->{transition_status}
            =~ /\A(?:absent|parse-invalid|semantic-invalid|valid-reclaim|valid-release)\z/
        && valid_operator_entry_evidence($record, 'transition')
        && (($record->{transition_status} eq 'absent')
            == ($record->{transition_entry_kind} eq 'absent'))
        && ($record->{transition_status}
                !~ /\A(?:semantic-invalid|valid-reclaim|valid-release)\z/
            || $record->{transition_entry_kind} eq 'regular')
        && $record->{marker_status}
            =~ /\A(?:not-applicable|absent|valid|invalid)\z/
        && valid_operator_entry_evidence($record, 'marker')
        && (($record->{marker_status}
                =~ /\A(?:not-applicable|absent)\z/)
            == ($record->{marker_entry_kind} eq 'absent'))
        && ($record->{marker_status} ne 'valid'
            || $record->{marker_entry_kind} eq 'regular')
        && $record->{release_authority_status}
            =~ /\A(?:not-applicable|valid|reason-scope-invalid|marker-missing|marker-invalid|marker-conflict|pin-missing|pin-invalid|pin-role-invalid)\z/
        && (($record->{transition_status} eq 'valid-release')
            == ($record->{release_authority_status} ne 'not-applicable'))
        && ($record->{recovery_basis} ne 'finalization-invalid'
            || ($record->{transition_status}
                    =~ /\A(?:valid-reclaim|valid-release)\z/
                && ($record->{source_name} ne '.round_inflight.lockdir'
                    || $record->{pins_status} eq 'healthy')
                && $record->{release_authority_status}
                    =~ /\A(?:not-applicable|valid)\z/))
        && ($record->{recovery_basis} ne 'release-authority-invalid'
            || $record->{release_authority_status}
                =~ /\A(?:reason-scope-invalid|marker-missing|marker-invalid|marker-conflict|pin-missing|pin-invalid|pin-role-invalid)\z/)
        && $record->{finalization_status}
            =~ /\A(?:not-applicable|inflight-invalid|compat-invalid|receipt-invalid|opposite-outcome-conflict|event-invalid|archive-collision)\z/
        && (($record->{finalization_status} eq 'not-applicable'
                && $record->{finalization_sha256} eq 'none')
            || ($record->{finalization_status} ne 'not-applicable'
                && $record->{finalization_sha256} =~ $TOKEN_RE))
        && (($record->{recovery_basis} eq 'finalization-invalid')
            == ($record->{finalization_status}
                =~ /\A(?:inflight-invalid|compat-invalid|receipt-invalid|opposite-outcome-conflict|event-invalid|archive-collision)\z/))
        && $record->{event_epoch} =~ /\A[0-9]+\z/
        && $record->{outcome} eq ($phase eq 'intent' ? 'prepared' : 'quarantined');
}

sub operator_events_match {
    my ($intent, $complete) = @_;
    return 0 if !defined($intent) || !defined($complete);
    for my $field (@OPERATOR_EVENT_ORDER) {
        next if $field eq 'phase' || $field eq 'event_epoch'
            || $field eq 'outcome';
        return 0 if $intent->{$field} ne $complete->{$field};
    }
    return $complete->{event_epoch} >= $intent->{event_epoch};
}

sub validate_operator_event_for_args {
    my ($record, $arg_ref, $phase) = @_;
    return 0 if !validate_operator_event_shape(
        $record, $arg_ref->{operation_token}, $phase,
    );
    return $record->{operator_label} eq $arg_ref->{operator_label}
        && $record->{operation_kind} eq $arg_ref->{operation_kind}
        && $record->{expected_generation_token}
            eq $arg_ref->{expected_generation_token}
        && $record->{reason} eq $arg_ref->{reason}
        && $record->{source_name} eq $arg_ref->{source_name}
        && $record->{destination_name} eq $arg_ref->{destination_name}
        && $record->{lock_dev} eq "$arg_ref->{expected_lock_dev}"
        && $record->{lock_ino} eq "$arg_ref->{expected_lock_ino}";
}

sub operator_evidence_values {
    my (%arg) = @_;
    my $snapshot = $arg{snapshot};
    assert_operator_abandonment_global_paths_safe($arg{state_dir});
    my $tree = operator_evidence_entry($arg{source_path});
    die "ERROR: operator quarantine source evidence is not a directory\n"
        if $tree->{entry_kind} ne 'directory';
    my $generation = operator_evidence_entry(
        generation_path($arg{source_path}),
    );
    my $transition = operator_evidence_entry(
        transition_path($arg{source_path}),
    );
    my $transition_status = operator_transition_status(
        $arg{source_path}, $transition, $snapshot,
    );
    my $pins = operator_pins_evidence($arg{source_path}, $snapshot);
    my $marker = operator_marker_evidence($arg{state_dir}, $snapshot);
    my $release_authority_status = operator_release_authority_status(
        %arg, snapshot => $snapshot,
        transition_status => $transition_status,
        pins_status => $pins->{status}, marker_status => $marker->{status},
    );
    my $local_basis = operator_recovery_basis(
        %arg, snapshot => $snapshot, pins_status => $pins->{status},
        transition_status => $transition_status,
        marker_status => $marker->{status},
        release_authority_status => $release_authority_status,
    );
    my $finalization = operator_finalization_evidence(
        %arg, snapshot => $snapshot, local_basis => $local_basis,
        transition_status => $transition_status,
        pins_status => $pins->{status},
        ready_tokens => $pins->{ready_tokens},
        release_authority_status => $release_authority_status,
    );
    my $recovery_basis = $local_basis;
    $recovery_basis = 'finalization-invalid'
        if $local_basis eq 'healthy'
        && $finalization->{status}
            =~ /\A(?:inflight-invalid|compat-invalid|receipt-invalid|opposite-outcome-conflict|event-invalid|archive-collision)\z/;
    return (
        recovery_basis => $recovery_basis,
        tree_sha256 => $tree->{sha256},
        generation_status => $snapshot->{generation_status},
        generation_entry_kind => $generation->{entry_kind},
        generation_sha256 => $generation->{sha256},
        generation_entry_fingerprint => $generation->{fingerprint},
        pins_status => $pins->{status},
        pins_entry_kind => $pins->{entry_kind},
        pins_sha256 => $pins->{sha256},
        pins_entry_fingerprint => $pins->{fingerprint},
        transition_status => $transition_status,
        transition_entry_kind => $transition->{entry_kind},
        transition_sha256 => $transition->{sha256},
        transition_entry_fingerprint => $transition->{fingerprint},
        marker_status => $marker->{status},
        marker_entry_kind => $marker->{entry_kind},
        marker_sha256 => $marker->{sha256},
        marker_entry_fingerprint => $marker->{fingerprint},
        release_authority_status => $release_authority_status,
        finalization_status => $finalization->{status},
        finalization_sha256 => $finalization->{sha256},
    );
}

sub assert_operator_operations_complete {
    my ($state_dir) = @_;
    my $pending_dir = operator_pending_dir($state_dir);
    for my $operation_token (operator_pending_tokens($state_dir)) {
        validate_operator_events_directory_if_present($state_dir);
        my $intent = operator_pending_record(
            $state_dir, $operation_token,
        );
        my $audit_intent = operator_event_record(
            $state_dir, $operation_token, 'intent',
        );
        my $complete = operator_event_record(
            $state_dir, $operation_token, 'complete',
        );
        die "ERROR: malformed operator quarantine intent: $operation_token\n"
            if !validate_operator_event_shape(
                $intent, $operation_token, 'intent',
            ) || !defined($audit_intent)
            || canonical_body($intent, \@OPERATOR_EVENT_ORDER)
                ne canonical_body($audit_intent, \@OPERATOR_EVENT_ORDER);
        my @pending_st = lstat(operator_pending_path(
            $state_dir, $operation_token,
        ));
        my @audit_st = lstat(operator_event_path(
            $state_dir, $operation_token, 'intent',
        ));
        die "ERROR: operator pending intent is not the immutable audit intent\n"
            if !@pending_st || !@audit_st
            || $pending_st[0] != $audit_st[0]
            || $pending_st[1] != $audit_st[1];
        my $replay_command = $intent->{operation_kind} eq 'invalid_snapshot'
            ? 'operator-quarantine-invalid'
            : 'operator-quarantine-unrecoverable';
        die "ERROR: incomplete operator quarantine $operation_token; rerun "
            . "$replay_command with the original arguments\n"
            if !defined($complete);
        die "ERROR: malformed operator quarantine completion: $operation_token\n"
            if !validate_operator_event_shape(
                $complete, $operation_token, 'complete',
            ) || !operator_events_match($intent, $complete);
        my $source = File::Spec->catdir(
            $state_dir, $intent->{source_name},
        );
        my $destination = File::Spec->catdir(
            $state_dir, $intent->{destination_name},
        );
        my @source_st = lstat($source);
        die "ERROR: completed operator quarantine source reused the preserved identity: $source\n"
            if @source_st
            && $source_st[0] == $intent->{lock_dev}
            && $source_st[1] == $intent->{lock_ino};
        die "ERROR: cannot inspect completed operator quarantine source '$source': $!\n"
            if !@source_st && !$!{ENOENT};
        my @destination_st = lstat($destination);
        die "ERROR: completed operator quarantine destination is missing or changed\n"
            if !@destination_st || -l _ || !-d _
            || $destination_st[0] != $intent->{lock_dev}
            || $destination_st[1] != $intent->{lock_ino};
        my $snapshot = lock_snapshot($destination);
        die "ERROR: completed operator quarantine destination is absent\n"
            if !defined($snapshot);
        my %evidence = operator_evidence_values(
            state_dir => $state_dir, source_path => $destination,
            source_name => $intent->{source_name}, snapshot => $snapshot,
            operation_kind => $intent->{operation_kind},
        );
        die "ERROR: completed operator quarantine destination no longer matches policy\n"
            if !operator_snapshot_matches_policy(
                $snapshot, $evidence{recovery_basis}, $intent,
            );
        for my $field (@OPERATOR_EVIDENCE_FIELDS) {
            die "ERROR: completed operator quarantine evidence changed\n"
                if $intent->{$field} ne $evidence{$field}
                || $complete->{$field} ne $evidence{$field};
        }
        sync_directory(operator_events_dir($state_dir));
        sync_directory($state_dir);
        remove_operator_pending($state_dir, $intent);
    }
}

sub operator_quarantine_snapshot {
    my (%arg) = @_;
    validate_operator_events_directory_if_present($arg{state_dir});
    my @other = grep {
        $_ ne $arg{operation_token}
    } operator_pending_tokens($arg{state_dir});
    die "ERROR: another operator quarantine is incomplete; replay it first\n"
        if @other;
    my $source = File::Spec->catdir($arg{state_dir}, $arg{source_name});
    my $destination = File::Spec->catdir(
        $arg{state_dir}, $arg{destination_name},
    );
    my $intent = operator_event_record(
        $arg{state_dir}, $arg{operation_token}, 'intent',
    );
    my $complete = operator_event_record(
        $arg{state_dir}, $arg{operation_token}, 'complete',
    );

    if (defined($complete)) {
        die "ERROR: completed operator quarantine does not match this request\n"
            if !validate_operator_event_for_args($complete, \%arg, 'complete');
        die "ERROR: completed operator quarantine lacks a matching intent\n"
            if !defined($intent)
            || !validate_operator_event_for_args($intent, \%arg, 'intent');
        die "ERROR: completed operator quarantine lacks exact intent binding\n"
            if !operator_events_match($intent, $complete);
        sync_directory(operator_events_dir($arg{state_dir}));
        sync_directory($arg{state_dir});
        my @destination_st = lstat($destination);
        die "ERROR: completed operator quarantine destination is missing or changed\n"
            if !@destination_st || -l _ || !-d _
            || $destination_st[0] != $arg{expected_lock_dev}
            || $destination_st[1] != $arg{expected_lock_ino};
        my $destination_snapshot = lock_snapshot($destination);
        die "ERROR: completed operator quarantine destination is absent\n"
            if !defined($destination_snapshot);
        my %current_evidence = operator_evidence_values(
            %arg, source_path => $destination,
            snapshot => $destination_snapshot,
        );
        die "ERROR: completed operator quarantine destination no longer matches policy\n"
            if !operator_snapshot_matches_policy(
                $destination_snapshot, $current_evidence{recovery_basis}, \%arg,
            );
        for my $field (@OPERATOR_EVIDENCE_FIELDS) {
            die "ERROR: completed operator quarantine evidence changed\n"
                if $complete->{$field} ne $current_evidence{$field}
                || $intent->{$field} ne $current_evidence{$field};
        }
        remove_operator_pending($arg{state_dir}, $intent);
        return;
    }

    my @destination_before = lstat($destination);
    die "ERROR: cannot inspect operator quarantine destination '$destination': $!\n"
        if !@destination_before && !$!{ENOENT};
    my @source_before = lstat($source);
    die "ERROR: cannot inspect operator quarantine source '$source': $!\n"
        if !@source_before && !$!{ENOENT};
    die "ERROR: operator quarantine source and destination both exist\n"
        if @source_before && @destination_before;
    die "ERROR: operator quarantine destination lacks a durable matching intent\n"
        if @destination_before && !defined($intent);
    my $evidence_source = @destination_before ? $destination : $source;
    my $snapshot = lock_snapshot($evidence_source);
    die "ERROR: operator quarantine source is absent\n" if !defined($snapshot);
    die "ERROR: operator quarantine lock identity does not match confirmation\n"
        if $snapshot->{dev} != $arg{expected_lock_dev}
        || $snapshot->{ino} != $arg{expected_lock_ino};
    my %evidence = operator_evidence_values(
        %arg, source_path => $evidence_source, snapshot => $snapshot,
    );
    my $policy_basis = $evidence{recovery_basis};
    $policy_basis = 'generation-token-mismatch'
        if $arg{operation_kind} eq 'abandon_unrecoverable_generation'
        && (!$snapshot->{valid}
            || $snapshot->{generation}->{token}
                ne $arg{expected_generation_token});
    die operator_policy_error(\%arg, $policy_basis)
        if !operator_snapshot_matches_policy(
            $snapshot, $evidence{recovery_basis}, \%arg,
        );

    if (defined($intent)) {
        die "ERROR: operator quarantine intent does not match this request\n"
            if !validate_operator_event_for_args($intent, \%arg, 'intent')
            || grep {
                $intent->{$_} ne $evidence{$_}
            } @OPERATOR_EVIDENCE_FIELDS;
        sync_directory(operator_events_dir($arg{state_dir}));
    } else {
        my %value = (
            schema => $OPERATOR_SCHEMA,
            operation_token => $arg{operation_token},
            operation_kind => $arg{operation_kind},
            expected_generation_token => $arg{expected_generation_token},
            phase => 'intent',
            operator_label => $arg{operator_label},
            reason => $arg{reason},
            source_name => $arg{source_name},
            destination_name => $arg{destination_name},
            lock_dev => $arg{expected_lock_dev},
            lock_ino => $arg{expected_lock_ino},
            %evidence,
            event_epoch => int(time()),
            outcome => 'prepared',
        );
        die "ERROR: internal: operator intent does not satisfy its wire contract\n"
            if !validate_operator_event_shape(
                \%value, $arg{operation_token}, 'intent',
            );
        install_operator_event(%arg, %value);
        $intent = operator_event_record(
            $arg{state_dir}, $arg{operation_token}, 'intent',
        );
    }
    install_operator_pending($arg{state_dir}, $intent);
    test_pause('after-operator-intent');

    my @source_st = lstat($source);
    if (@source_st) {
        die "ERROR: operator quarantine source is not a real directory\n"
            if -l _ || !-d _;
        die "ERROR: operator quarantine source identity changed\n"
            if $source_st[0] != $arg{expected_lock_dev}
            || $source_st[1] != $arg{expected_lock_ino};
        assert_fenced_directory_move_ready(
            source => $source, destination => $destination,
            expected_dev => $arg{expected_lock_dev},
            expected_ino => $arg{expected_lock_ino},
        );
        rename($source, $destination)
            or die "ERROR: cannot preserve invalid lock for operator review: $!\n";
        test_pause('after-operator-rename-before-sync');
    } elsif (!$!{ENOENT}) {
        die "ERROR: cannot inspect operator quarantine source '$source': $!\n";
    }
    # Repeat this barrier on replay even when the source is already absent. A
    # previous process may have died after rename and before syncing the parent.
    sync_directory($arg{state_dir});
    my @destination_st = lstat($destination);
    die "ERROR: operator quarantine destination is missing or changed\n"
        if !@destination_st || -l _ || !-d _
        || $destination_st[0] != $arg{expected_lock_dev}
        || $destination_st[1] != $arg{expected_lock_ino};
    my $moved_snapshot = lock_snapshot($destination);
    die "ERROR: operator quarantine destination is absent\n"
        if !defined($moved_snapshot);
    my %moved_evidence = operator_evidence_values(
        %arg, source_path => $destination, snapshot => $moved_snapshot,
    );
    die "ERROR: operator quarantine destination no longer matches policy\n"
        if !operator_snapshot_matches_policy(
            $moved_snapshot, $moved_evidence{recovery_basis}, \%arg,
        );
    for my $field (@OPERATOR_EVIDENCE_FIELDS) {
        die "ERROR: operator quarantine evidence changed during preservation\n"
            if $moved_evidence{$field} ne $evidence{$field};
    }
    test_pause('after-operator-sync-before-complete');

    my $complete_epoch = int(time());
    $complete_epoch = $intent->{event_epoch}
        if $complete_epoch < $intent->{event_epoch};
    my %complete_value = (
        %{$intent}, phase => 'complete',
        event_epoch => $complete_epoch,
        outcome => 'quarantined',
    );
    die "ERROR: internal: operator completion does not satisfy its wire contract\n"
        if !validate_operator_event_shape(
            \%complete_value, $arg{operation_token}, 'complete',
        );
    install_operator_event(%arg, %complete_value);
    remove_operator_pending($arg{state_dir}, $intent);
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
        if !defined($snapshot);
    die invalid_snapshot_error($dir, $snapshot) if !$snapshot->{valid};
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

sub pin_matches_request {
    my ($pin, $snapshot, $arg_ref, $pin_token) = @_;
    my $process_start = process_start_identity($arg_ref->{owner_pid})
        || 'unavailable';
    return validate_pin_for_snapshot($pin, $snapshot)
        && $pin->{pin_token} eq $pin_token
        && $pin->{token} eq $arg_ref->{token}
        && $pin->{round_barcode} eq $arg_ref->{round_barcode}
        && $pin->{scope} eq $arg_ref->{scope}
        && $pin->{role} eq $arg_ref->{role}
        && $pin->{pid} == $arg_ref->{owner_pid}
        && $pin->{host} eq $THIS_HOST
        && $pin->{process_start} eq $process_start;
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
    my $pin_dir = pins_dir($dir);
    my @pin_dir_st = lstat($pin_dir);
    die "ERROR: process pin directory is not a real directory\n"
        if !@pin_dir_st || -l _ || !-d _;
    # Likewise adopt a ready hard link only after re-establishing its parent
    # directory durability.
    sync_directory($pin_dir);
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
    # A failed hostname lookup must never make two unrelated hosts compare as
    # the same literal "unknown" host and authorize local PID reasoning.
    return 1 if $THIS_HOST eq 'unknown' || $pin->{host} eq 'unknown';
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
    # A previous helper may have died after adding or removing the last ready
    # name but before syncing pins/.  Adopt that visible namespace before an
    # empty enumeration is allowed to authorize release or reclaim.
    sync_directory($pin_dir);
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
            if !pin_matches_request($existing, $snapshot, \%arg, $pin_token);
        $values = $existing;
        my @ready_st = lstat($ready);
        die "ERROR: immutable pin candidate has no live ready pin: $pin_token\n"
            if !@ready_st && $!{ENOENT};
        die "ERROR: cannot inspect ready process pin '$pin_token': $!\n"
            if !@ready_st;
    }
    assert_generation(%arg);
    if (!link($candidate, $ready)) {
        my $exists = $!{EEXIST};
        my $error = "$!";
        die "ERROR: cannot ready process pin '$pin_token': $error\n" if !$exists;
        my $existing = record_for($ready, \@PIN_ORDER);
        die "ERROR: ready process pin conflicts: $pin_token\n"
            if !pin_matches_request($existing, $snapshot, \%arg, $pin_token);
        my @candidate_st = lstat($candidate);
        my @ready_st = lstat($ready);
        die "ERROR: ready process pin is not the immutable candidate: $pin_token\n"
            if !@candidate_st || !@ready_st
            || $candidate_st[0] != $ready_st[0]
            || $candidate_st[1] != $ready_st[1];
        sync_directory(pins_dir($dir));
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
    return 0 if !defined($snapshot);
    die invalid_snapshot_error($dir, $snapshot) if !$snapshot->{valid};
    return 0 if $snapshot->{generation}->{token} ne $arg{token};
    my $transition = transition_record($dir);
    if (defined($transition)) {
        die "ERROR: malformed transition identity in '$dir'\n"
            if !validate_transition_for_snapshot($transition, $snapshot);
        die "ERROR: cannot remove the process pin authorizing a pending release\n"
            if $transition->{action} eq 'release'
            && $transition->{allowed_pin_token} eq $arg{pin_token};
    }
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
        || $record->{lock_ino} !~ /\A[0-9]+\z/
        || $record->{operation_token} !~ $TOKEN_RE;
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
        || $record->{reclaim_transition_epoch} !~ /\A[0-9]+\z/
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
    test_pause('after-transition-temp-write-before-link');
    if (!same_identity($arg{snapshot}, $dir)) {
        unlink($tmp);
        return undef;
    }
    my $linked = link($tmp, transition_path($dir));
    my $exists = !$linked && $!{EEXIST};
    my $missing = !$linked && $!{ENOENT};
    my $error = $linked ? '' : "$!";
    test_pause('after-transition-link-before-temp-unlink') if $linked;
    unlink($tmp);
    test_pause('after-transition-temp-unlink-before-sync') if $linked;
    sync_directory($dir) if $linked || $exists;
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
    return 0 if !$snapshot->{valid};
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

sub transition_matches_release_request {
    my ($transition, $snapshot, $token, $reason, $pin_token) = @_;
    return defined($transition)
        && validate_transition_for_snapshot($transition, $snapshot)
        && $transition->{action} eq 'release'
        && $transition->{owner_token} eq $token
        && $transition->{reason} eq $reason
        && $transition->{allowed_pin_token} eq $pin_token;
}

sub install_release {
    my (%arg) = @_;
    my %value = (
        schema => $SCHEMA, token => $arg{token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, outcome => 'released', reason => $arg{reason},
        effective_ttl_seconds => $arg{effective_ttl_seconds},
        release_transition_epoch => $arg{event_epoch} // int(time()),
        lock_dev => $arg{lock_dev}, lock_ino => $arg{lock_ino},
        operation_token => $arg{operation_token},
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
    validate_token($arg{token});
    my %value = (
        schema => $SCHEMA, token => $arg{token}, round_barcode => $arg{round_barcode},
        scope => $arg{scope}, outcome => 'revoked', reason => $arg{reason},
        effective_ttl_seconds => $arg{effective_ttl_seconds},
        reclaim_transition_epoch => $arg{event_epoch} // int(time()),
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
    die "ERROR: internal: transition outcome requires a valid generation snapshot\n"
        if !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    my $token = $generation->{token};
    my $round = $generation->{round_barcode};
    my $scope = $generation->{scope};
    my $ttl = $generation->{effective_ttl_seconds};
    my $opposite_path = $transition->{action} eq 'reclaim'
        ? release_path($arg{state_dir}, $token)
        : revocation_path($arg{state_dir}, $token);
    die "ERROR: pending $transition->{action} conflicts with an opposite "
        . "terminal outcome for generation $token\n"
        if path_occupied_nofollow($opposite_path, 'opposite terminal outcome');
    if ($transition->{action} eq 'reclaim') {
        install_revocation(
            state_dir => $arg{state_dir}, token => $token, round_barcode => $round,
            scope => $scope, reason => $arg{reason}, effective_ttl_seconds => $ttl,
            lock_dev => $snapshot->{dev}, lock_ino => $snapshot->{ino},
            operation_token => $transition->{operation_token},
            event_epoch => $transition->{started_epoch},
        );
        test_pause('after-transition-receipt-before-event');
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
            operation_token => $transition->{operation_token},
        );
        test_pause('after-transition-receipt-before-event');
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
    } elsif (path_occupied_nofollow(
        marker_path($arg{state_dir}, $generation->{token}),
        'pre-handoff marker',
    )) {
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

sub archive_release_quarantine {
    my (%arg) = @_;
    return if $arg{transition}->{action} ne 'release';
    my $path = $arg{path};
    my $operation_token = $arg{transition}->{operation_token};
    my $archive_parent = terminal_archive_dir($arg{state_dir});
    ensure_real_directory($archive_parent, 0700);
    my $archive = File::Spec->catdir(
        $archive_parent, "release-$operation_token",
    );
    assert_fenced_directory_move_ready(
        source => $path, destination => $archive,
        expected_dev => $arg{snapshot}->{dev},
        expected_ino => $arg{snapshot}->{ino},
    );
    if (!rename($path, $archive)) {
        my $error = "$!";
        my $existing = transition_record($archive);
        die "ERROR: cannot archive completed release quarantine '$path': $error\n"
            if !defined($existing)
            || $existing->{operation_token} ne $operation_token;
        my $archived_snapshot = lock_snapshot($archive);
        die "ERROR: completed release archive identity changed: $archive\n"
            if !defined($archived_snapshot)
            || $archived_snapshot->{dev} != $arg{snapshot}->{dev}
            || $archived_snapshot->{ino} != $arg{snapshot}->{ino};
        my @source = lstat($path);
        die "ERROR: release quarantine and archive both exist: $path\n"
            if @source;
        die "ERROR: cannot inspect release quarantine after archive race '$path': $!\n"
            if !$!{ENOENT};
        # A prior process may have died after the rename and before either
        # parent-directory barrier.  Adopting the exact archive repeats both
        # barriers before treating the move as durable.
        sync_directory($archive_parent);
        sync_directory($arg{state_dir});
        return;
    }
    test_pause('after-terminal-archive-rename-before-sync');
    my @archived = lstat($archive);
    die "ERROR: cannot inspect completed release archive '$archive': $!\n"
        if !@archived;
    die "ERROR: wrong release quarantine was archived\n"
        if $archived[0] != $arg{snapshot}->{dev}
        || $archived[1] != $arg{snapshot}->{ino};
    sync_directory($archive_parent);
    sync_directory($arg{state_dir});
}

sub archive_reclaim_quarantine {
    my (%arg) = @_;
    return if $arg{transition}->{action} ne 'reclaim';
    my $path = $arg{path};
    my $operation_token = $arg{transition}->{operation_token};
    my $archive_parent = terminal_archive_dir($arg{state_dir});
    ensure_real_directory($archive_parent, 0700);
    my $archive = File::Spec->catdir(
        $archive_parent, "reclaim-$operation_token",
    );
    assert_fenced_directory_move_ready(
        source => $path, destination => $archive,
        expected_dev => $arg{snapshot}->{dev},
        expected_ino => $arg{snapshot}->{ino},
    );
    if (!rename($path, $archive)) {
        my $error = "$!";
        my $existing = transition_record($archive);
        die "ERROR: cannot archive completed reclaim quarantine '$path': $error\n"
            if !defined($existing)
            || $existing->{operation_token} ne $operation_token;
        my $archived_snapshot = lock_snapshot($archive);
        die "ERROR: completed reclaim archive identity changed: $archive\n"
            if !defined($archived_snapshot)
            || $archived_snapshot->{dev} != $arg{snapshot}->{dev}
            || $archived_snapshot->{ino} != $arg{snapshot}->{ino};
        my @source = lstat($path);
        die "ERROR: reclaim quarantine and archive both exist: $path\n"
            if @source;
        die "ERROR: cannot inspect reclaim quarantine after archive race '$path': $!\n"
            if !$!{ENOENT};
        # See archive_release_quarantine: replay must establish durability for
        # an archive name that another process may only have linked visibly.
        sync_directory($archive_parent);
        sync_directory($arg{state_dir});
        return;
    }
    test_pause('after-terminal-archive-rename-before-sync');
    my @archived = lstat($archive);
    die "ERROR: cannot inspect completed reclaim archive '$archive': $!\n"
        if !@archived;
    die "ERROR: wrong reclaim quarantine was archived\n"
        if $archived[0] != $arg{snapshot}->{dev}
        || $archived[1] != $arg{snapshot}->{ino};
    sync_directory($archive_parent);
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
    assert_fenced_directory_move_ready(
        source => $dir, destination => $quarantine,
        expected_dev => $snapshot->{dev}, expected_ino => $snapshot->{ino},
    );
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
            test_pause('after-transition-outcome-before-archive');
            archive_reclaim_quarantine(
                state_dir => $arg{state_dir}, path => $quarantine,
                snapshot => $moved_snapshot, transition => $moved,
            );
            archive_release_quarantine(
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
    test_pause('after-transition-outcome-before-archive');
    archive_reclaim_quarantine(
        state_dir => $arg{state_dir}, path => $quarantine,
        snapshot => $snapshot, transition => $transition,
    );
    archive_release_quarantine(
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
        die invalid_snapshot_error($path, $snapshot) if !$snapshot->{valid};
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
        test_pause('before-quarantine-recovery-outcome');
        record_transition_outcome(
            state_dir => $state_dir, snapshot => $snapshot, transition => $transition,
            reason => $transition->{reason},
        );
        test_pause('after-transition-outcome-before-archive');
        archive_release_quarantine(
            state_dir => $state_dir, path => $path,
            snapshot => $snapshot, transition => $transition,
        );
        archive_reclaim_quarantine(
            state_dir => $state_dir, path => $path,
            snapshot => $snapshot, transition => $transition,
        ) if $transition->{action} eq 'reclaim';
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

sub acquire_generation_matches_request {
    my ($snapshot, $arg_ref) = @_;
    return 0 if !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    my $process_start = process_start_identity($arg_ref->{owner_pid})
        || 'unavailable';
    return $generation->{token} eq $arg_ref->{token}
        && $generation->{round_barcode} eq $arg_ref->{round_barcode}
        && $generation->{scope} eq $arg_ref->{scope}
        && $generation->{pid} == $arg_ref->{owner_pid}
        && $generation->{host} eq $THIS_HOST
        && $generation->{process_start} eq $process_start
        && $generation->{effective_ttl_seconds} eq "$arg_ref->{stale_seconds}";
}

sub assert_paths_absent_for_explicit_acquire {
    my ($label, @paths) = @_;
    for my $path (@paths) {
        my @st = lstat($path);
        die "ERROR: explicit acquire token has durable $label history: $path\n"
            if @st;
        die "ERROR: cannot inspect explicit acquire history '$path': $!\n"
            if !$!{ENOENT};
    }
}

sub assert_no_acquire_event_history {
    my ($state_dir, $token) = @_;
    my $dir = events_dir($state_dir);
    my @dir_st = lstat($dir);
    return if !@dir_st && $!{ENOENT};
    die "ERROR: acquire event history directory is missing or unsafe\n"
        if !@dir_st || -l _ || !-d _;
    sync_directory($dir);
    opendir(my $dh, $dir)
        or die "ERROR: cannot inspect acquire event history '$dir': $!\n";
    my @matches = sort grep {
        /\A\Q$token\E\.acquire\.[0-9a-f]{64}\.tsv\z/
    } readdir($dh);
    closedir($dh)
        or die "ERROR: cannot close acquire event history '$dir': $!\n";
    die "ERROR: explicit acquire generation token has durable acquire history: "
        . "$matches[0]\n" if @matches;
}

sub assert_explicit_acquire_token_unused {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    assert_paths_absent_for_explicit_acquire(
        'conflicting',
        marker_path($arg{state_dir}, $arg{token}),
        release_path($arg{state_dir}, $arg{token}),
        revocation_path($arg{state_dir}, $arg{token}),
        inflight_generation_path($arg{state_dir}, $arg{token}),
        "$dir.failed-acquire-$arg{token}",
    );
    assert_no_acquire_event_history($arg{state_dir}, $arg{token});
}

sub validate_explicit_acquire_replay_pin {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = $arg{snapshot};
    my %pin_arg = (%arg, role => 'fast_acquisition');
    my $candidate_path = pin_candidate_path($dir, $arg{pin_token});
    my $ready_path = pin_ready_path($dir, $arg{pin_token});
    my $candidate = record_for($candidate_path, \@PIN_ORDER);
    my $ready = read_ready_pin($dir, $arg{pin_token}, $snapshot);
    die "ERROR: explicit acquire replay is missing its durable process pin\n"
        if !defined($candidate) || !defined($ready);
    die "ERROR: explicit acquire replay process pin conflicts\n"
        if !pin_matches_request(
            $candidate, $snapshot, \%pin_arg, $arg{pin_token},
        )
        || !pin_matches_request($ready, $snapshot, \%pin_arg, $arg{pin_token});
    my @candidate_st = lstat($candidate_path);
    my @ready_st = lstat($ready_path);
    die "ERROR: explicit acquire replay pin is not the immutable candidate\n"
        if !@candidate_st || !@ready_st
        || $candidate_st[0] != $ready_st[0]
        || $candidate_st[1] != $ready_st[1];
}

sub validate_explicit_acquire_replay_event {
    my (%arg) = @_;
    my $snapshot = $arg{snapshot};
    my $generation = $snapshot->{generation};
    my $event_dir = events_dir($arg{state_dir});
    my @event_dir_st = lstat($event_dir);
    die "ERROR: explicit acquire replay event directory is missing or unsafe\n"
        if !@event_dir_st || -l _ || !-d _;
    sync_directory($event_dir);
    my $path = File::Spec->catfile(
        $event_dir,
        "$arg{token}.acquire.$arg{pin_token}.tsv",
    );
    my $event = record_for($path, \@EVENT_ORDER);
    die "ERROR: explicit acquire replay is missing its durable acquire event\n"
        if !defined($event);
    my %expected = (
        schema => $SCHEMA,
        event_id => $arg{pin_token},
        generation_token => $arg{token},
        round_barcode => $arg{round_barcode},
        scope => $arg{scope},
        event => 'acquire',
        outcome => 'acquired',
        effective_ttl_seconds => $generation->{effective_ttl_seconds},
        event_epoch => $generation->{started_epoch},
        lock_dev => $snapshot->{dev},
        lock_ino => $snapshot->{ino},
    );
    die "ERROR: explicit acquire replay event conflicts\n"
        if canonical_body($event, \@EVENT_ORDER)
            ne canonical_body(\%expected, \@EVENT_ORDER);
}

sub replay_explicit_acquire {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = $arg{snapshot};
    die "ERROR: explicit acquire request conflicts with generation $arg{token}\n"
        if !acquire_generation_matches_request($snapshot, \%arg);
    my $transition = transition_record($dir);
    die "ERROR: explicit acquire request conflicts with a pending transition\n"
        if defined($transition);
    assert_paths_absent_for_explicit_acquire(
        'progressed',
        marker_path($arg{state_dir}, $arg{token}),
        release_path($arg{state_dir}, $arg{token}),
        revocation_path($arg{state_dir}, $arg{token}),
        inflight_generation_path($arg{state_dir}, $arg{token}),
    );
    validate_explicit_acquire_replay_pin(%arg);
    validate_explicit_acquire_replay_event(%arg);
    return ($arg{token}, $arg{pin_token});
}

sub stale_reason {
    my ($state_dir, $snapshot, $fallback_ttl) = @_;
    die "ERROR: internal: stale_reason requires a valid generation snapshot\n"
        if !$snapshot->{valid};
    my $generation = $snapshot->{generation};
    die "ERROR: round lock host identity is unverifiable; refusing automatic reclaim\n"
        if $THIS_HOST eq 'unknown' || $generation->{host} eq 'unknown';
    my $ttl = int($generation->{effective_ttl_seconds});
    my @blocking = blocking_pins(lock_dir($state_dir), $snapshot, undef);
    return undef if @blocking;
    my $handoff = marker_path($state_dir, $generation->{token});
    my $has_handoff = path_occupied_nofollow($handoff, 'handoff marker');
    validate_marker(
        state_dir => $state_dir, token => $generation->{token},
        round_barcode => $generation->{round_barcode},
        scope => $generation->{scope},
    ) if $has_handoff;
    if (!$has_handoff && $generation->{host} eq $THIS_HOST) {
        my $alive = kill(0, $generation->{pid}) || $!{EPERM};
        return "dead pid=$generation->{pid} host=$generation->{host}" if !$alive;
        my $relationship = process_start_relationship(
            $generation->{process_start}, process_start_identity($generation->{pid})
        );
        return "reused pid=$generation->{pid} host=$generation->{host}"
            if $relationship eq 'reused';
        return undef;
    }
    return undef if $ttl == 0;
    my $now = int(time());
    return undef if $snapshot->{newest_epoch} < 1 || $snapshot->{newest_epoch} > $now;
    my $age = $now - $snapshot->{newest_epoch};
    return undef if $age < $ttl;
    return "leased generation age=${age}s ttl=${ttl}s";
}

sub reclaim_if_stale {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
    my $snapshot = lock_snapshot($dir);
    return 0 if !defined($snapshot);
    die invalid_snapshot_error($dir, $snapshot) if !$snapshot->{valid};
    my $transition = transition_record($dir);
    my $reason;
    if (defined($transition)) {
        die "ERROR: malformed transition identity in '$dir'\n"
            if !validate_transition_for_snapshot($transition, $snapshot);
        $reason = $transition->{reason};
    } else {
        $reason = stale_reason($arg{state_dir}, $snapshot, $arg{stale_seconds});
        return 0 if !defined($reason);
        my $owner_token = $snapshot->{generation}->{token};
        my $round = $snapshot->{generation}->{round_barcode};
        my $scope = $snapshot->{generation}->{scope};
        my $effective_ttl = $snapshot->{generation}->{effective_ttl_seconds};
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
    my $explicit_tokens = defined($arg{token});
    my $waited_ms = 0;
    my $limit_ms = $arg{wait_seconds} * 1000;
    while (1) {
        my $state_fence = acquire_state_fence($arg{state_dir}, 1);
        if (!defined($state_fence)) {
            die "ERROR: timed out waiting for round-lock state fence\n"
                if $limit_ms > 0 && $waited_ms >= $limit_ms;
            usleep(100_000);
            $waited_ms += 100;
            next;
        }
        assert_operator_operations_complete($arg{state_dir});
        recover_quarantines($arg{state_dir});
        my $existing_snapshot = lock_snapshot($dir);
        die invalid_snapshot_error($dir, $existing_snapshot)
            if defined($existing_snapshot) && !$existing_snapshot->{valid};
        if ($explicit_tokens && defined($existing_snapshot)
            && $existing_snapshot->{generation}->{token} eq $arg{token}) {
            my @replayed = replay_explicit_acquire(
                %arg, snapshot => $existing_snapshot,
            );
            release_state_fence($state_fence);
            return @replayed;
        }
        assert_explicit_acquire_token_unused(%arg) if $explicit_tokens;
        ensure_real_directory(events_dir($arg{state_dir}), 0700);
        if (mkdir($dir, 0700)) {
            sync_directory($arg{state_dir});
            test_pause('after-lock-mkdir-before-stat');
            my @st = lstat($dir);
            die "ERROR: cannot inspect newly-created round lock: $!\n" if !@st;
            my $token = $arg{token}
                // new_token(join(':', $dir, $arg{round_barcode}, $arg{scope}));
            my %generation = (
                schema => $SCHEMA, token => $token, round_barcode => $arg{round_barcode},
                scope => $arg{scope}, pid => $arg{owner_pid}, host => $THIS_HOST,
                process_start => process_start_identity($arg{owner_pid}) || 'unavailable',
                started_epoch => int(time()),
                effective_ttl_seconds => $arg{stale_seconds},
                lock_dev => $st[0], lock_ino => $st[1],
            );
            test_pause('after-lock-stat-before-pins');
            my $pin_token;
            my $ok = eval {
                mkdir(pins_dir($dir), 0700)
                    or die "ERROR: cannot create generation pin directory: $!\n";
                sync_directory($dir);
                test_pause('after-pins-sync-before-generation');
                install_generation($dir, \%generation);
                test_pause('after-generation-install');
                $pin_token = install_pin(
                    %arg, token => $token,
                    pin_token => $arg{pin_token} // new_token("acquire:$token"),
                    role => 'fast_acquisition', snapshot => undef,
                );
                my %event_arg = (
                    state_dir => $arg{state_dir}, generation_token => $token,
                    round_barcode => $arg{round_barcode}, scope => $arg{scope},
                    event => 'acquire', outcome => 'acquired',
                    effective_ttl_seconds => $arg{stale_seconds},
                    lock_dev => $st[0], lock_ino => $st[1],
                );
                if ($explicit_tokens) {
                    $event_arg{event_id} = $pin_token;
                    $event_arg{event_epoch} = $generation{started_epoch};
                }
                write_event(%event_arg);
                1;
            };
            if (!$ok) {
                my $error = $@ || "ERROR: cannot initialize round lock\n";
                my $failed = "$dir.failed-acquire-$token";
                my $owned = { dev => $st[0], ino => $st[1] };
                if (same_identity($owned, $dir)) {
                    my $move_ok = eval {
                        assert_fenced_directory_move_ready(
                            source => $dir, destination => $failed,
                            expected_dev => $st[0], expected_ino => $st[1],
                        );
                        rename($dir, $failed)
                            or die "ERROR: failed acquisition also could not be quarantined: $!\n";
                        1;
                    };
                    die "$error" . ($@ || "ERROR: failed acquisition quarantine failed\n")
                        if !$move_ok;
                }
                if (-e $dir || -l $dir) {
                    die "$error" . "ERROR: failed acquisition lost ownership of the canonical lock; replacement preserved\n";
                }
                sync_directory($arg{state_dir}) if -d $failed;
                die $error;
            }
            release_state_fence($state_fence);
            return ($token, $pin_token);
        }
        my $exists = $!{EEXIST};
        my $error = "$!";
        die "ERROR: cannot create round lock '$dir': $error\n" if !$exists;
        my $reclaimed = reclaim_if_stale(%arg);
        release_state_fence($state_fence);
        next if $reclaimed;
        die "ERROR: timed out waiting for round lock '$dir'\n"
            if $limit_ms > 0 && $waited_ms >= $limit_ms;
        usleep(100_000);
        $waited_ms += 100;
    }
}

sub release_generation {
    my (%arg) = @_;
    my $dir = lock_dir($arg{state_dir});
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
    # A retry may be resuming this exact release transition.  The immutable
    # transition is validated below against action, generation, reason, and
    # authorizing pin before it can be finalized.
    my ($snapshot, $generation, $pending_transition) = assert_generation(
        %arg, allow_transition => 1,
    );
    my $pin = read_ready_pin($dir, $arg{pin_token}, $snapshot);
    return 0 if !defined($pin) && $arg{best_effort};
    die "ERROR: process pin is absent: $arg{pin_token}\n"
        if !defined($pin);
    if ($pin->{role} ne $required_role) {
        return 0 if $arg{best_effort};
        die "ERROR: process pin role mismatch\n";
    }
    die "ERROR: release lost transition race for generation $arg{token}\n"
        if defined($pending_transition)
        && !transition_matches_release_request(
            $pending_transition, $snapshot, $arg{token}, $arg{reason},
            $arg{pin_token},
        );
    validate_marker(%arg) if $arg{reason} eq 'full_round_released';
    install_marker(%arg) if $arg{reason} eq 'dorado_only_early';
    if ($arg{unless_handoff}) {
        my $marker = marker_path($arg{state_dir}, $arg{token});
        if (path_occupied_nofollow($marker, 'pre-handoff marker')) {
            validate_marker(%arg);
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
    if (!transition_matches_release_request(
        $transition, $snapshot, $arg{token}, $arg{reason}, $arg{pin_token},
    )) {
        die "ERROR: release lost transition race for generation $arg{token}\n";
    }
    my $finalized = finalize_transition(
        state_dir => $arg{state_dir}, snapshot => $snapshot,
        transition => $transition, reason => $arg{reason},
    );
    if (!$finalized) {
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
    return if !path_occupied_nofollow($path, 'handoff marker');
    validate_marker(%arg);
    unlink($path) or die "ERROR: cannot remove exact handoff marker '$path': $!\n";
    sync_directory($arg{state_dir});
}

my $command = shift(@ARGV) // '';
usage() if $command eq '' || $command eq '--help' || $command eq '-h';
my %VALID_COMMAND = map { $_ => 1 } qw(
    acquire inflight handoff pin guard-pin unpin verify-release early-release
    finish abort operator-quarantine-invalid operator-quarantine-unrecoverable
);
usage() if !$VALID_COMMAND{$command};
Configure(qw(no_auto_abbrev no_getopt_compat no_bundling no_ignore_case));

my %opt = (
    wait_seconds => 0,
    stale_seconds => 0,
    best_effort => 0,
    confirm_invalid_snapshot => 0,
    confirm_stopped_world => 0,
);
my @common_option_spec = (
    'state-dir=s' => \$opt{state_dir},
    'wait-seconds=s' => \$opt{wait_seconds},
);
my @operator_option_spec = (
    @common_option_spec,
    'expected-lock-dev=s' => \$opt{expected_lock_dev},
    'expected-lock-ino=s' => \$opt{expected_lock_ino},
    'operation-token=s' => \$opt{operation_token},
    'operator-label=s' => \$opt{operator_label},
    'reason=s' => \$opt{reason},
    'source-name=s' => \$opt{source_name},
    'confirm-invalid-snapshot!' => \$opt{confirm_invalid_snapshot},
);
my @unrecoverable_operator_option_spec = (
    @common_option_spec,
    'expected-lock-dev=s' => \$opt{expected_lock_dev},
    'expected-lock-ino=s' => \$opt{expected_lock_ino},
    'expected-generation-token=s' => \$opt{expected_generation_token},
    'operation-token=s' => \$opt{operation_token},
    'operator-label=s' => \$opt{operator_label},
    'reason=s' => \$opt{reason},
    'source-name=s' => \$opt{source_name},
    'confirm-stopped-world!' => \$opt{confirm_stopped_world},
    'confirm-abandon-generation=s' => \$opt{confirm_abandon_generation},
);
my @runtime_option_spec = (
    @common_option_spec,
    'round-barcode=s' => \$opt{round_barcode},
    'scope=s' => \$opt{scope},
    'token=s' => \$opt{token},
    'pin-token=s' => \$opt{pin_token},
    'role=s' => \$opt{role},
    'owner-pid=s' => \$opt{owner_pid},
    'stale-seconds=s' => \$opt{stale_seconds},
    'read-file=s' => \$opt{read_file},
    'best-effort!' => \$opt{best_effort},
);
GetOptionsFromArray(
    \@ARGV,
    $command eq 'operator-quarantine-invalid'
        ? @operator_option_spec
        : $command eq 'operator-quarantine-unrecoverable'
        ? @unrecoverable_operator_option_spec
        : @runtime_option_spec,
) or usage();
die "ERROR: unexpected arguments: @ARGV\n" if @ARGV;
die "ERROR: --best-effort is valid only for abort or unpin\n"
    if $opt{best_effort} && $command ne 'abort' && $command ne 'unpin';

validate_failpoint_configuration();
$opt{state_dir} = validate_text('state-dir', $opt{state_dir});
my @state_st = lstat($opt{state_dir});
my $operator_command = $command eq 'operator-quarantine-invalid'
    || $command eq 'operator-quarantine-unrecoverable';
if (!@state_st && $!{ENOENT} && !$operator_command) {
    make_path($opt{state_dir}, { mode => 0700 });
    @state_st = lstat($opt{state_dir});
}
die "ERROR: state-dir is not a real directory: $opt{state_dir}\n"
    if !@state_st || -l _ || !-d _;

if ($operator_command) {
    for my $required (qw(expected_lock_dev expected_lock_ino operation_token operator_label reason)) {
        die "ERROR: missing $required\n" if !defined($opt{$required});
    }
    if ($command eq 'operator-quarantine-invalid') {
        die "ERROR: operator quarantine requires --confirm-invalid-snapshot\n"
            if !$opt{confirm_invalid_snapshot};
        $opt{operation_kind} = 'invalid_snapshot';
        $opt{expected_generation_token} = 'none';
    } else {
        die "ERROR: missing expected_generation_token\n"
            if !defined($opt{expected_generation_token});
        die "ERROR: missing confirm_abandon_generation\n"
            if !defined($opt{confirm_abandon_generation});
        die "ERROR: stopped-world quarantine requires --confirm-stopped-world\n"
            if !$opt{confirm_stopped_world};
        $opt{expected_generation_token} = validate_token(
            $opt{expected_generation_token},
        );
        $opt{confirm_abandon_generation} = validate_token(
            $opt{confirm_abandon_generation},
        );
        die "ERROR: abandonment confirmation does not match expected generation\n"
            if $opt{confirm_abandon_generation}
                ne $opt{expected_generation_token};
        $opt{operation_kind} = 'abandon_unrecoverable_generation';
    }
    $opt{expected_lock_dev} = validate_uint(
        'expected-lock-dev', $opt{expected_lock_dev},
    );
    $opt{expected_lock_ino} = validate_uint(
        'expected-lock-ino', $opt{expected_lock_ino},
    );
    $opt{operation_token} = validate_token($opt{operation_token});
    $opt{operator_label} = validate_text('operator-label', $opt{operator_label});
    $opt{reason} = validate_text('reason', $opt{reason});
    $opt{wait_seconds} = validate_uint('wait-seconds', $opt{wait_seconds});
    $opt{source_name} = $opt{source_name} // '.round_inflight.lockdir';
    die "ERROR: invalid operator quarantine source name\n"
        if $opt{source_name}
            !~ /\A\.round_inflight\.lockdir(?:\.(?:reclaim|release)-[0-9a-f]{64})?\z/;
    $opt{destination_name} =
        ".round_inflight.lockdir.operator-$opt{operation_token}";
    my $operator_fence = acquire_state_fence_with_wait(
        $opt{state_dir}, $opt{wait_seconds},
    );
    operator_quarantine_snapshot(%opt);
    release_state_fence($operator_fence);
    exit 0;
}

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
    die "ERROR: acquire requires --token and --pin-token together\n"
        if defined($opt{token}) != defined($opt{pin_token});
    die "ERROR: acquire generation and pin tokens must differ\n"
        if defined($opt{token}) && $opt{token} eq $opt{pin_token};
    my ($token, $pin_token) = acquire_generation(%opt);
    write_stdout("generation_token=$token\npin_token=$pin_token\n");
    exit 0;
}

for my $required (qw(round_barcode scope token)) {
    die "ERROR: missing $required\n" if !defined($opt{$required});
}

my $state_fence = acquire_state_fence_with_wait(
    $opt{state_dir}, $opt{wait_seconds},
);
assert_operator_operations_complete($opt{state_dir});
if ($command eq 'inflight') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    $opt{read_file} = validate_text('read-file', $opt{read_file});
    write_inflight(%opt);
} elsif ($command eq 'handoff') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    die "ERROR: handoff is only valid for full_round\n" if $opt{scope} ne 'full_round';
    recover_quarantines($opt{state_dir});
    my $marker = marker_path($opt{state_dir}, $opt{token});
    if (path_occupied_nofollow($marker, 'handoff marker')) {
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
        # A pending release transition is durable authority.  Handoff must not
        # create a marker or event that changes that transition's meaning.
        my ($snapshot) = assert_generation(%opt);
        my $pin = read_ready_pin(
            lock_dir($opt{state_dir}), $opt{pin_token}, $snapshot,
        );
        die "ERROR: process pin is absent: $opt{pin_token}\n"
            if !defined($pin);
        die "ERROR: process pin role mismatch\n"
            if $pin->{role} ne 'fast_acquisition';
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
    die "ERROR: generation and pin tokens must differ\n"
        if defined($opt{pin_token}) && $opt{pin_token} eq $opt{token};
    validate_marker(%opt);
    my $pin_token = install_pin(%opt);
    release_state_fence($state_fence);
    write_stdout("$pin_token\n");
    exit 0;
} elsif ($command eq 'guard-pin') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    guard_pin(%opt);
} elsif ($command eq 'unpin') {
    die "ERROR: missing pin-token\n" if !defined($opt{pin_token});
    unpin_generation(%opt);
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
        %opt, reason => 'pre_handoff_abort', unless_handoff => 1,
        best_effort => 1,
    );
} else {
    usage();
}

release_state_fence($state_fence);
exit 0;
