set -euo pipefail
shopt -s nullglob

MODE="${MODE}"
OUTDIR="${OUTDIR}"
LOCK_WAIT="${LOCK_WAIT}"
RUN_NAME="${RUN_NAME}"
STATE_ID="${STATE_ID}"
FORCE="${FORCE}"
OPERATION_ID="${OPERATION_ID:-}"

if [ -z "$OUTDIR" ]; then
    if [ "$MODE" = "reset" ]; then
        printf 'ERROR: output root does not exist; reset will not create it: <empty>\n' 1>&2
        exit 1
    fi
    exit 0
fi

if [ -z "$STATE_ID" ] || [ "$STATE_ID" = "." ] || [ "$STATE_ID" = ".." ]; then
    printf 'ERROR: restart STATE_ID must be one safe pathname component\n' 1>&2
    exit 1
fi
case "$STATE_ID" in
    *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-]*)
        printf 'ERROR: restart STATE_ID must be one safe pathname component\n' 1>&2
        exit 1
        ;;
esac

SENTINEL="${OUTDIR}/temp/.restart_applied.${STATE_ID}"
LOCKDIR="${SENTINEL}.lockdir"
SENTINEL_TMP="${SENTINEL}.tmp.${OPERATION_ID}"
SENTINEL_TMP_OWNED=0
RESTART_HANDLER_SUCCESS=0

case "$MODE" in
    reset|restore)
        ;;
    *)
        printf 'ERROR: restart MODE must be reset or restore\n' 1>&2
        exit 1
        ;;
esac
if [ "${#OPERATION_ID}" -ne 64 ]; then
    printf 'ERROR: restart OPERATION_ID must be exactly 64 lowercase hexadecimal characters\n' 1>&2
    exit 1
fi
case "$OPERATION_ID" in
    *[!0-9a-f]*)
        printf 'ERROR: restart OPERATION_ID must be exactly 64 lowercase hexadecimal characters\n' 1>&2
        exit 1
        ;;
esac
if [ -z "$RUN_NAME" ]; then
    printf 'ERROR: restart RUN_NAME must be nonempty\n' 1>&2
    exit 1
fi
case "$RUN_NAME" in
    *$'\n'*|*$'\r'*)
        printf 'ERROR: restart RUN_NAME must occupy one record line\n' 1>&2
        exit 1
        ;;
esac

if [ "$MODE" = "reset" ]; then
    if [ "${RTB_RESET_SUPERVISED:-}" != "1" ]; then
        RTB_RESET_USER_OUTDIR="$OUTDIR"
        unset RTB_RESET_SUFFIX_IDS
        # Keep a trailing marker so command substitution preserves even a
        # canonical pathname ending in a newline. Parse the numeric tail.
        reset_root_info="$(perl -MCwd=realpath -MFcntl=:mode -MErrno=ENOENT -e '
            my ($user) = @ARGV;
            my @entry = lstat($user);
            if (!@entry && $! == ENOENT) {
                die "ERROR: output root does not exist; reset will not create it: $user\n";
            }
            die "ERROR: cannot inspect reset output root $user: $!\n" unless @entry;
            my $canonical = realpath($user);
            die "ERROR: cannot resolve reset output root $user: $!\n" unless defined($canonical);
            my @root = lstat($canonical);
            die "ERROR: reset output root is not a directory: $user\n"
                unless @root && S_ISDIR($root[2]);
            print $canonical, "\n", $root[0], "\n", $root[1], ".";
        ' "$RTB_RESET_USER_OUTDIR")" || exit 1
        reset_root_info="${reset_root_info%.}"
        RTB_RESET_ROOT_INO="${reset_root_info##*$'\n'}"
        reset_root_info="${reset_root_info%$'\n'*}"
        RTB_RESET_ROOT_DEV="${reset_root_info##*$'\n'}"
        OUTDIR="${reset_root_info%$'\n'*}"
        export OUTDIR RTB_RESET_USER_OUTDIR RTB_RESET_ROOT_DEV RTB_RESET_ROOT_INO
        exec 8< "$OUTDIR"
    fi
    verify_reset_root() {
        perl -MCwd=realpath -MFcntl=:mode -e '
            my ($user, $canonical, $dev, $ino, $state_id) = @ARGV;
            die "ERROR: missing reset output-root identity\n"
                unless defined($user) && defined($canonical)
                    && defined($dev) && $dev =~ /\A[0-9]+\z/
                    && defined($ino) && $ino =~ /\A[0-9]+\z/;
            open(my $pinned, "<&8") or die "ERROR: missing pinned reset output root\n";
            my $resolved = realpath($user);
            my @held = stat($pinned); my @current = lstat($canonical);
            die "ERROR: reset output root identity changed: $user\n"
                unless defined($resolved) && $resolved eq $canonical
                    && @held && @current && S_ISDIR($current[2])
                    && $held[0] == $dev && $held[1] == $ino
                    && $current[0] == $dev && $current[1] == $ino;
            if (defined($ENV{RTB_RESET_SUFFIX_IDS})) {
                my @ids = split /,/, $ENV{RTB_RESET_SUFFIX_IDS}, -1;
                my @parts = ("temp", "ongoing", "state", $state_id, "_state");
                die "ERROR: missing reset suffix identity\n" unless @ids == @parts;
                my $path = $canonical;
                for my $i (0 .. $#parts) {
                    $path .= "/$parts[$i]";
                    my @st = lstat($path);
                    die "ERROR: reset suffix inode changed: $path\n"
                        unless @st && S_ISDIR($st[2])
                            && "$st[0]:$st[1]" eq $ids[$i];
                }
            }
        ' "$RTB_RESET_USER_OUTDIR" "$OUTDIR" \
            "$RTB_RESET_ROOT_DEV" "$RTB_RESET_ROOT_INO" "$STATE_ID"
    }
    verify_reset_root || exit 1
    SENTINEL="${OUTDIR}/temp/.restart_applied.${STATE_ID}"
    LOCKDIR="${SENTINEL}.lockdir"
    SENTINEL_TMP="${SENTINEL}.tmp.${OPERATION_ID}"
fi

if [ "$MODE" = "reset" ]; then
    # temp is the first permitted control-plane ancestor. Validate its real
    # inode before the existing restart lockdir is created inside it.
    perl -MFcntl=:mode -MErrno=EEXIST,ENOENT -e '
        my ($outdir, $temp) = @ARGV;
        umask 077;
        my @base = lstat($outdir);
        die "ERROR: reset output root identity changed: $outdir\n"
            unless @base && S_ISDIR($base[2]);
        my @st = lstat($temp);
        if (!@st) {
            die "ERROR: cannot inspect reset ancestor $temp: $!\n" unless $! == ENOENT;
            if (!mkdir($temp, 0700)) {
                die "ERROR: cannot initialize reset ancestor $temp: $!\n" unless $! == EEXIST;
            }
            @st = lstat($temp);
        }
        die "ERROR: unsafe reset ancestor $temp\n"
            unless @st && S_ISDIR($st[2]);
    ' "$OUTDIR" "${OUTDIR}/temp" || exit 1
else
    mkdir -p "${OUTDIR}/temp"
fi

if [ "${RTB_RESTART_DIR_LOCKED:-}" = "1" ]; then
    perl -MFcntl=:flock,:mode -e '
        my ($path) = @ARGV;
        open(my $dir, "<&9") or die "ERROR: missing inherited restart directory lock\n";
        my @a = stat($dir); my @b = lstat($path);
        die "ERROR: invalid inherited restart directory lock\n"
            unless @a && @b && S_ISDIR($b[2]) && $a[0] == $b[0] && $a[1] == $b[1];
        flock($dir, LOCK_EX | LOCK_NB)
            or die "ERROR: inherited restart directory lock is not held\n";
    ' "${OUTDIR}/temp" || exit 1
else
    # Serialize both modes on the existing temp inode. Reset cannot create
    # and remove the old lockdir: that would alter temp mtime on a refusal.
    # This startup lock is separate from the stable _state reset barrier.
    exec 9< "${OUTDIR}/temp"
    perl -MFcntl=:flock -MTime::HiRes=time,sleep -MErrno=EWOULDBLOCK,EAGAIN -e '
        my ($wait) = @ARGV;
        die "ERROR: invalid restart lock timeout\n"
            unless defined($wait) && $wait =~ /\A[0-9]+\z/ && $wait <= 86400;
        open(my $dir, "<&9") or die "ERROR: cannot open restart directory descriptor: $!\n";
        my $deadline = time() + $wait;
        while (!flock($dir, LOCK_EX | LOCK_NB)) {
            die "ERROR: cannot acquire restart directory lock: $!\n"
                unless $! == EWOULDBLOCK || $! == EAGAIN;
            die "ERROR: timed out acquiring restart directory lock\n" if time() >= $deadline;
            sleep 0.05;
        }
    ' "$LOCK_WAIT" || exit 1
    export RTB_RESTART_DIR_LOCKED=1
fi

if [ "$MODE" = "restore" ] &&
   [ "${RTB_JOINT_RESTART_OWNER:-}" != "$$" ]; then
waited=0
while ! mkdir "$LOCKDIR" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= LOCK_WAIT )); then
        echo "ERROR: Failed to acquire restart lock: $LOCKDIR" 1>&2
        exit 1
    fi
done

fi

cleanup_restart_handler() {
    local status="$1"
    trap - EXIT
    set +e
    if [ "$status" -eq 0 ] && [ "$RESTART_HANDLER_SUCCESS" -ne 1 ]; then
        status=1
    fi
    if [ "$SENTINEL_TMP_OWNED" -eq 1 ]; then
        rm -f "$SENTINEL_TMP" 2>/dev/null || true
    fi
    if [ "$MODE" = "restore" ] && [ "${RTB_JOINT_RESTART_OWNER:-}" != "$$" ]; then
        rmdir "$LOCKDIR" 2>/dev/null || true
    fi
    exit "$status"
}
trap 'cleanup_restart_handler "$?"' EXIT

sentinel_take_line() {
    case "$SENTINEL_REST" in
        *$'\n'*)
            SENTINEL_LINE="${SENTINEL_REST%%$'\n'*}"
            SENTINEL_REST="${SENTINEL_REST#*$'\n'}"
            return 0
            ;;
    esac
    return 1
}

valid_record_operation_id() {
    local value="$1"
    [ "${#value}" -eq 64 ] || return 1
    case "$value" in
        *[!0-9a-f]*) return 1 ;;
    esac
    return 0
}

valid_record_run_name() {
    local value="$1"
    [ -n "$value" ] || return 1
    case "$value" in
        *$'\n'*|*$'\r'*) return 1 ;;
    esac
    return 0
}

# Parse the complete record rather than extracting one agreeable field.  This
# accepts the historical exact two-line applied sentinel and the schema-2
# applying/applied records, while rejecting partial, extended, or reordered
# records.
parse_restart_sentinel() {
    SENTINEL_REST="$1"
    SENTINEL_KIND=""
    SENTINEL_MODE=""
    SENTINEL_OPERATION_ID=""

    sentinel_take_line || return 1
    local first="$SENTINEL_LINE"
    if [ "$first" = "mode=reset" ] || [ "$first" = "mode=restore" ]; then
        SENTINEL_MODE="${first#mode=}"
        sentinel_take_line || return 1
        case "$SENTINEL_LINE" in
            run_name=?*)
                valid_record_run_name "${SENTINEL_LINE#run_name=}" || return 1
                ;;
            *) return 1 ;;
        esac
        [ -z "$SENTINEL_REST" ] || return 1
        SENTINEL_KIND="legacy-applied"
        return 0
    fi

    [ "$first" = "schema=2" ] || return 1
    sentinel_take_line || return 1
    local status_line="$SENTINEL_LINE"
    sentinel_take_line || return 1
    case "$SENTINEL_LINE" in
        operation_id=*) SENTINEL_OPERATION_ID="${SENTINEL_LINE#operation_id=}" ;;
        *) return 1 ;;
    esac
    valid_record_operation_id "$SENTINEL_OPERATION_ID" || return 1
    sentinel_take_line || return 1
    local mode_line="$SENTINEL_LINE"
    sentinel_take_line || return 1
    case "$SENTINEL_LINE" in
        run_name=?*)
            valid_record_run_name "${SENTINEL_LINE#run_name=}" || return 1
            ;;
        *) return 1 ;;
    esac
    [ -z "$SENTINEL_REST" ] || return 1

    if [ "$status_line" = "status=applying" ]; then
        case "$mode_line" in
            requested_mode=reset|requested_mode=restore)
                SENTINEL_MODE="${mode_line#requested_mode=}"
                SENTINEL_KIND="applying"
                return 0
                ;;
        esac
        return 1
    fi
    if [ "$status_line" = "status=applied" ]; then
        case "$mode_line" in
            mode=reset|mode=restore)
                SENTINEL_MODE="${mode_line#mode=}"
                SENTINEL_KIND="applied"
                return 0
                ;;
        esac
    fi
    return 1
}

write_restart_sentinel() {
    local status="$1"
    if [ -e "$SENTINEL_TMP" ] || [ -L "$SENTINEL_TMP" ]; then
        printf 'ERROR: restart transition temp path already exists: %s\n' \
            "$SENTINEL_TMP" 1>&2
        return 1
    fi

    SENTINEL_TMP_OWNED=1
    if ! (
        set -C
        umask 077
        if [ "$status" = "applying" ]; then
            {
                printf 'schema=2\n'
                printf 'status=applying\n'
                printf 'operation_id=%s\n' "$OPERATION_ID"
                printf 'requested_mode=%s\n' "$MODE"
                printf 'run_name=%s\n' "$RUN_NAME"
            } > "$SENTINEL_TMP"
        else
            {
                printf 'schema=2\n'
                printf 'status=applied\n'
                printf 'operation_id=%s\n' "$OPERATION_ID"
                printf 'mode=%s\n' "$MODE"
                printf 'run_name=%s\n' "$RUN_NAME"
            } > "$SENTINEL_TMP"
        fi
    ); then
        rm -f "$SENTINEL_TMP" 2>/dev/null || true
        SENTINEL_TMP_OWNED=0
        printf 'ERROR: failed to write restart transition temp: %s\n' \
            "$SENTINEL_TMP" 1>&2
        return 1
    fi
    if ! mv -f "$SENTINEL_TMP" "$SENTINEL"; then
        printf 'ERROR: failed to install restart transition record: %s\n' \
            "$SENTINEL" 1>&2
        return 1
    fi
    SENTINEL_TMP_OWNED=0
    if [ "$status" = "applied" ]; then
        RESTART_HANDLER_SUCCESS=1
    fi
}

# If restart has already been applied in the same mode, preserve its operation
# epoch on an ordinary resume.  An applying record is safe to recover by
# repeating the idempotent reset/restore transaction under a new operation ID.
if [ -L "$SENTINEL" ]; then
    printf 'ERROR: restart sentinel is a symlink: %s\n' "$SENTINEL" 1>&2
    exit 1
fi
if [ -e "$SENTINEL" ]; then
    if [ ! -f "$SENTINEL" ]; then
        printf 'ERROR: restart sentinel is not a regular file: %s\n' "$SENTINEL" 1>&2
        exit 1
    fi
    if ! SENTINEL_RAW="$({
        LC_ALL=C perl -MEncode=decode,FB_CROAK -e '
            local $/;
            my $bytes = <>;
            die "missing restart sentinel bytes\n" if !defined($bytes);
            die "NUL in restart sentinel\n" if index($bytes, "\0") >= 0;
            my $validation_copy = $bytes;
            decode("UTF-8", $validation_copy, FB_CROAK);
            print $bytes;
        ' "$SENTINEL" 2>/dev/null || exit 1
        printf x
    })"; then
        printf 'ERROR: failed to read restart sentinel: %s\n' "$SENTINEL" 1>&2
        exit 1
    fi
    SENTINEL_RAW="${SENTINEL_RAW%x}"
    if ! parse_restart_sentinel "$SENTINEL_RAW"; then
        printf 'ERROR: malformed restart sentinel: %s\n' "$SENTINEL" 1>&2
        exit 1
    fi
    if [ "$FORCE" != "1" ] && \
       { [ "$SENTINEL_KIND" = "applied" ] || [ "$SENTINEL_KIND" = "legacy-applied" ]; } && \
       [ "$SENTINEL_MODE" = "$MODE" ]; then
        RESTART_HANDLER_SUCCESS=1
        exit 0
    fi
fi

# Namespace rolling state by STATE_ID to avoid cross-run contamination.
ONGOING="${OUTDIR}/temp/ongoing/state/${STATE_ID}"
LEGACY_CURRENT="${OUTDIR}/temp/current/state/${STATE_ID}"
CURRENT_ROOT="${OUTDIR}/current/state/${STATE_ID}"
ONGOING_STATE="${ONGOING}/_state"

case "${BASH_SOURCE[0]}" in
    /*) RESTART_HANDLER_DIR="${BASH_SOURCE[0]%/*}" ;;
    */*) RESTART_HANDLER_DIR="$PWD/${BASH_SOURCE[0]%/*}" ;;
    *) RESTART_HANDLER_DIR="$PWD" ;;
esac
case "${BASH_SOURCE[0]}" in
    /*) RESET_HANDLER_PATH="${BASH_SOURCE[0]}" ;;
    *) RESET_HANDLER_PATH="$PWD/${BASH_SOURCE[0]}" ;;
esac

# Mandatory Stage A contract: a writer first opens and takes a shared lock on
# this exact barrier, before opening, creating, adopting, or modifying any
# per-state lock or fence. It retains the shared descriptor through every
# writing descendant, then takes its per-state kernel lock, compatibility
# fence, and only then mutates protected state.
# Reset takes the exclusive barrier and retains it with every governed lock
# descriptor through the complete wipe and applied-sentinel publication.
# Any future A2 or Stage B stable kernel-lock name must be added to the two
# declared lists and the scan allow-list in the same reviewed candidate.
RESET_BARRIER="$ONGOING_STATE/.rtbioscan_state_reset.flock"
if [ "$MODE" = "reset" ]; then
    if [ "${RTB_RESET_SUPERVISED:-}" = "1" ]; then
        # A caller cannot bypass supervision by setting the environment flag:
        # every inherited descriptor must still lock the expected stable inode.
        perl -MFcntl=:flock,:mode -e '
            my ($root, $fds) = @ARGV;
            my @names = (".rtbioscan_state_reset.flock",
                ".dorado.lock.flock", ".blastreport.lock.flock",
                ".blastreport_sup.lock.flock", ".qced_reads.lock.flock",
                ".otu_size_streak.lock.flock", ".sup_basecall_cache.lock.flock",
                ".done_pod5.lock.flock", ".report_history.lock.flock");
            my @fds = split /,/, $fds, -1;
            die "ERROR: missing reset lock descriptors\n" unless @fds == @names;
            for my $i (0 .. $#names) {
                die "ERROR: invalid reset lock descriptor\n" unless $fds[$i] =~ /^[0-9]+$/;
                open(my $fh, "<&$fds[$i]") or die "ERROR: missing reset lock descriptor\n";
                my @a = stat($fh); my @b = lstat("$root/$names[$i]");
                die "ERROR: reset control inode changed: $names[$i]\n"
                    unless @a && @b && S_ISREG($b[2]) && $a[0] == $b[0] && $a[1] == $b[1]
                        && $b[3] <= 1 && ($i != 0 || $b[7] == 0);
                flock($fh, LOCK_EX | LOCK_NB)
                    or die "ERROR: reset control descriptor is not locked: $names[$i]\n";
            }
        ' "$ONGOING_STATE" "${RTB_RESET_FDS:-}" || exit 1
    else
        # Perl owns the stable open descriptions, then execs this script. The
        # outer shell keeps the existing restart serialization until it returns.
        if perl - "$OUTDIR" "$STATE_ID" "$RESET_HANDLER_PATH" "$LOCK_WAIT" <<'RESET_PERL'
use strict;
use warnings;
use Fcntl qw(:DEFAULT :flock :mode F_SETFD);
use Errno qw(EEXIST ENOENT EWOULDBLOCK EAGAIN);
use Time::HiRes qw(time sleep);
use Cwd qw(realpath);
my ($outdir, $state_id, $script, $wait) = @ARGV;
die "ERROR: invalid reset barrier timeout\n" unless defined($wait) && $wait =~ /\A[0-9]+\z/ && $wait <= 86400;
umask 077;
sub verify_root {
    my $user = $ENV{RTB_RESET_USER_OUTDIR};
    my $dev = $ENV{RTB_RESET_ROOT_DEV};
    my $ino = $ENV{RTB_RESET_ROOT_INO};
    die "ERROR: missing reset output-root identity\n"
        unless defined($user) && defined($dev) && $dev =~ /\A[0-9]+\z/
            && defined($ino) && $ino =~ /\A[0-9]+\z/;
    open(my $pinned, "<&8") or die "ERROR: missing pinned reset output root\n";
    my $resolved = realpath($user);
    my @held = stat($pinned); my @current = lstat($outdir);
    die "ERROR: reset output root identity changed: $user\n"
        unless defined($resolved) && $resolved eq $outdir
            && @held && @current && S_ISDIR($current[2])
            && $held[0] == $dev && $held[1] == $ino
            && $current[0] == $dev && $current[1] == $ino;
}
verify_root();
my $root = "$outdir/temp";
my @suffix_ids;
my @temp_st = lstat($root);
die "ERROR: unsafe reset ancestor $root\n" unless @temp_st && S_ISDIR($temp_st[2]);
push @suffix_ids, "$temp_st[0]:$temp_st[1]";
for my $part ('ongoing', 'state', $state_id, '_state') {
    $root .= "/$part";
    my @st = lstat($root);
    if (!@st) {
        die "ERROR: cannot inspect reset state root $root: $!\n" unless $! == ENOENT;
        if (!mkdir($root, 0700)) {
            die "ERROR: cannot initialize reset state root $root: $!\n" unless $! == EEXIST;
        }
        @st = lstat($root);
    }
    die "ERROR: round-lock state-fence path is a symlink: $root\n"
        if $part eq '_state' && @st && S_ISLNK($st[2]);
    die "ERROR: unsafe reset state root $root\n"
        unless @st && S_ISDIR($st[2]);
    push @suffix_ids, "$st[0]:$st[1]";
}
sub verify_suffix {
    my $path = "$outdir";
    my @parts = ('temp', 'ongoing', 'state', $state_id, '_state');
    for my $i (0 .. $#parts) {
        $path .= "/$parts[$i]";
        my @st = lstat($path);
        die "ERROR: reset suffix inode changed: $path\n"
            unless @st && S_ISDIR($st[2])
                && "$st[0]:$st[1]" eq $suffix_ids[$i];
    }
}
my @names = ('.rtbioscan_state_reset.flock',
    '.dorado.lock.flock', '.blastreport.lock.flock',
    '.blastreport_sup.lock.flock', '.qced_reads.lock.flock',
    '.otu_size_streak.lock.flock', '.sup_basecall_cache.lock.flock',
    '.done_pod5.lock.flock', '.report_history.lock.flock');
my @handles;
sub stable_open {
    my ($path, $is_barrier) = @_;
    my @before = lstat($path);
    my $fh;
    if (!@before) {
        die "ERROR: cannot inspect reset control $path: $!\n" unless $! == ENOENT;
        if (!sysopen($fh, $path, O_RDWR | O_CREAT | O_EXCL, 0600)) {
            die "ERROR: cannot initialize reset control $path: $!\n" unless $! == EEXIST;
            undef $fh;
        }
    }
    @before = lstat($path);
    die "ERROR: unsafe reset control $path\n"
        unless @before && S_ISREG($before[2]) && $before[3] <= 1
            && (!$is_barrier || $before[7] == 0);
    sysopen($fh, $path, O_RDONLY) or die "ERROR: cannot open reset control $path: $!\n"
        unless defined($fh);
    my @opened = stat($fh);
    my @current = lstat($path);
    die "ERROR: reset control inode changed: $path\n"
        unless @opened && @current && S_ISREG($current[2])
            && $opened[0] == $before[0] && $opened[1] == $before[1]
            && $opened[0] == $current[0] && $opened[1] == $current[1];
    fcntl($fh, F_SETFD, 0) or die "ERROR: cannot retain reset control descriptor: $path: $!\n";
    return $fh;
}
my $barrier = stable_open("$root/$names[0]", 1);
my $deadline = time() + $wait;
while (!flock($barrier, LOCK_EX | LOCK_NB)) {
    die "ERROR: cannot acquire exclusive reset barrier $root/$names[0]: $!\n"
        unless $! == EWOULDBLOCK || $! == EAGAIN;
    die "ERROR: timed out acquiring exclusive reset barrier $root/$names[0]\n"
        if time() >= $deadline;
    sleep 0.05;
}
push @handles, $barrier;
verify_root();
verify_suffix();
for my $i (1 .. $#names) {
    my $path = "$root/$names[$i]";
    my $fh = stable_open($path, 0);
    die "ERROR: active governed reset lock $path\n"
        unless flock($fh, LOCK_EX | LOCK_NB);
    push @handles, $fh;
}
for my $i (0 .. $#names) {
    my $path = "$root/$names[$i]";
    my @a = stat($handles[$i]); my @b = lstat($path);
    die "ERROR: reset control inode changed: $path\n"
        unless @a && @b && S_ISREG($b[2]) && $a[0] == $b[0] && $a[1] == $b[1];
}
$ENV{RTB_RESET_SUPERVISED} = '1';
$ENV{RTB_RESET_FDS} = join(',', map { fileno($_) } @handles);
$ENV{RTB_RESET_SUFFIX_IDS} = join(',', @suffix_ids);
exec '/bin/bash', $script or die "ERROR: cannot exec supervised reset: $!\n";
RESET_PERL
        then
            RESTART_HANDLER_SUCCESS=1
            exit 0
        else
            exit "$?"
        fi
    fi
fi

# The cumulative BLAST OTU generation is restored with its authority, never as
# bare public tables (RTBioScan::R4DCumulative::restore_cli): `prepare`
# authenticates the snapshot's generation read-only, `upgrade` completes a
# proven older backup in place while the live state that proves it still
# exists, `install` writes the generation, sidecar and record into `_state`.
r4d_restore() {
    perl -e 'require $ARGV[0]; exit RTBioScan::R4DCumulative::restore_cli(@ARGV[1 .. $#ARGV]);' \
        "$RESTART_HANDLER_DIR/lib/RTBioScan/R4DCumulative.pm" "$1" "$ONGOING_STATE" "$CURRENT_ROOT" "$LEGACY_CURRENT"
}

# Governed cumulative names of any barcode (tables, sidecar, members, record and
# residue): installed by `r4d_restore install`, never copied by the snapshot copy.
# The authoritative rolling state (accumulated reads, round map, ever-lists,
# prune barrier/archive, streak state, the §6 tables) and its completed-round
# ledger are restored only from a snapshot root's sealed completeness record
# (bin/state_snapshot_authority.pl): verified read-only before the first
# mutation, installed into `_state` after every compatibility copy.
state_authority() {
    perl "$RESTART_HANDLER_DIR/state_snapshot_authority.pl" "$@"
}

state_authority_refuse() {
    printf 'ERROR: restart_mode=restore refuses snapshot %s: %s. It records completed rounds but cannot prove that its accumulated reads, round map, ever-lists, prune and streak state are complete (a snapshot written before this check, or an interrupted backup). It cannot be restored safely: use restart_mode=reset and replay the input POD5 files, or restore from a snapshot a later completed backup has sealed. Reset is eligible only without protected round-lock evidence in affected state or snapshot locations; restore still requires every safety check. Preserve the original state and replay inputs.\n' \
        "$1" "$2" 1>&2
    exit 1
}

restore_skips_governed_name() {
    case "$1" in
        *_blast_otu_pretax_rpt.txt|*_blast_otu_noadapter_rpt.txt|*_blast_otu_reporting_v1.tsv|\
        *_blast_otu_pretax_rpt.txt.*|*_blast_otu_noadapter_rpt.txt.*|*_blast_otu_reporting_v1.tsv.*|\
        *_blast_otu_cumulative.*)
            return 0
            ;;
    esac
    state_authority governed "$1"
}

wipe_dir_contents() {
    local dir="$1"
    [ -d "$dir" ] || return 0
    local items=( "$dir"/* )
    if (( ${#items[@]} )); then
        rm -rf "${items[@]}"
    fi
    # Parser-state transaction artifacts are excluded explicitly; do not rely on hidden-dir glob omission.
    rm -rf "$dir/.parser_state_txn"
}

# Keep the state directory itself and its stable control inodes. The parent
# reset removed _state as one item; deleting that directory would unlink a
# locked inode and allow a second writer to lock a replacement pathname.
wipe_reset_ongoing() {
    local item name dotglob_was_set=0
    local items=()
    [ -d "$ONGOING" ] || return 0
    items=( "$ONGOING"/* )
    for item in ${items[@]+"${items[@]}"}; do
        [ "$item" = "$ONGOING_STATE" ] && continue
        rm -rf "$item"
    done
    if [ -e "$ONGOING/.parser_state_txn" ] || [ -L "$ONGOING/.parser_state_txn" ]; then
        rm -rf "$ONGOING/.parser_state_txn"
    fi
    if shopt -q dotglob; then dotglob_was_set=1; else shopt -s dotglob; fi
    items=( "$ONGOING_STATE"/* )
    if [ "$dotglob_was_set" -eq 0 ]; then shopt -u dotglob; fi
    for item in ${items[@]+"${items[@]}"}; do
        name="${item##*/}"
        case "$name" in
            .rtbioscan_state_reset.flock|*.flock|.rtbioscan_lock_host_v1|.rtbioscan_lock_host_v1.guard|.lock_migration.log)
                continue ;;
        esac
        rm -rf "$item"
    done
}

is_round_lock_namespace() {
    local name="$1"
    case "$name" in
        .round_inflight.*|round_inflight.txt|\
        .round_lock_handoff|.round_lock_handoff.*|\
        .round_lock_release|.round_lock_release.*|\
        .round_lock_finish|.round_lock_finish.*|\
        .round_lock_revocation|.round_lock_revocation.*|\
        .round_lock_events|.round_lock_events.*|\
        .round_lock_operator_events|.round_lock_operator_events.*|\
        .round_lock_operator_pending|.round_lock_operator_pending.*|\
        .round_lock_archives|.round_lock_archives.*)
            return 0
            ;;
    esac
    return 1
}

restart_refuse_path() {
    local path="$1"
    local reason="$2"
    printf "ERROR: restart_mode=%s refuses to mutate round-lock state: %s: %s. Stop related writers and preserve the original state; do not delete, edit, or fabricate round-lock records. For protected completed history, use a fresh namespace and reanalyse or a separately reviewed recovery procedure. restart_force does not bypass this check.\n" \
        "$MODE" "$reason" "$path" 1>&2
    return 1
}

# Reset and restore predate the fenced round-lock protocol.  They must never
# erase, copy, or follow that protocol's live state or durable evidence.
# Completed rounds retain protected history, so force cannot make them eligible.
# Scan with dotglob enabled only while collecting each directory's entries so that
# hidden protocol names are inspected without changing wipe/copy semantics.
scan_restart_tree() {
    local root="$1"
    local purpose="$2"
    local reject_symlinks="$3"
    local ignored_presentation_tree="${4:-}"
    local dotglob_was_set=0
    local entries=()
    local entry
    local name

    if [ -L "$root" ]; then
        restart_refuse_path "$root" "unsafe symlink at ${purpose} root"
        return 1
    fi
    [ -e "$root" ] || return 0
    [ -d "$root" ] || return 0

    if shopt -q dotglob; then
        dotglob_was_set=1
    else
        shopt -s dotglob
    fi
    entries=( "$root"/* )
    if [ "$dotglob_was_set" -eq 0 ]; then
        shopt -u dotglob
    fi

    for entry in ${entries[@]+"${entries[@]}"}; do
        name="${entry##*/}"
        if [ "$MODE" = "restore" ] && [ "$root" = "$CURRENT_ROOT" ] &&
            { [ -d "$CURRENT_ROOT/tables" ] || [ -d "$CURRENT_ROOT/plots" ]; }; then
            case "$entry" in
                "$CURRENT_ROOT/live_round"|"$CURRENT_ROOT/.live_round_payloads")
                    # These direct current-state entries are presentation-only.
                    # Restore neither inspects nor copies their contents.
                    continue
                    ;;
            esac
        fi
        if is_round_lock_namespace "$name"; then
            restart_refuse_path "$entry" "protected round-lock namespace in ${purpose}"
            return 1
        fi
        if [ "$MODE" = "reset" ]; then
            case "$name" in
                *.lockdir|*.lockdir.*)
                    restart_refuse_path "$entry" "compatibility or legacy lock fence in ${purpose}"
                    return 1 ;;
                *.flock|.rtbioscan_lock_host_v1|.rtbioscan_lock_host_v1.guard)
                    if [ -L "$entry" ] || [ ! -f "$entry" ]; then
                        restart_refuse_path "$entry" "unsafe stable control in ${purpose}"
                        return 1
                    fi
                    case "$entry" in
                        "$ONGOING_STATE/.rtbioscan_state_reset.flock"|\
                        "$ONGOING_STATE/.dorado.lock.flock"|\
                        "$ONGOING_STATE/.blastreport.lock.flock"|\
                        "$ONGOING_STATE/.blastreport_sup.lock.flock"|\
                        "$ONGOING_STATE/.qced_reads.lock.flock"|\
                        "$ONGOING_STATE/.otu_size_streak.lock.flock"|\
                        "$ONGOING_STATE/.sup_basecall_cache.lock.flock"|\
                        "$ONGOING_STATE/.done_pod5.lock.flock"|\
                        "$ONGOING_STATE/.report_history.lock.flock"|\
                        "$ONGOING_STATE/.rtbioscan_lock_host_v1"|\
                        "$ONGOING_STATE/.rtbioscan_lock_host_v1.guard") ;;
                        *)
                            restart_refuse_path "$entry" "undeclared or misplaced stable control"
                            return 1 ;;
                    esac ;;
            esac
        fi
        if [ -L "$entry" ]; then
            if [ "$reject_symlinks" -eq 1 ]; then
                if [ "$MODE" = restore ] && [ "$purpose" = "restore snapshot state" ] &&
                    state_authority presentation-leaf "$CURRENT_ROOT" "$entry"; then
                    continue
                fi
                if [ -n "$ignored_presentation_tree" ]; then
                    case "$entry" in
                        "$ignored_presentation_tree"/*)
                            # backup_update_and_clean creates this derived link tree.
                            # Restore never copies it, so inspecting a target would
                            # add authority without restoring any rolling state.
                            continue
                            ;;
                    esac
                fi
                restart_refuse_path "$entry" "unsafe symlink in ${purpose}"
                return 1
            fi
            continue
        fi
        if [ -d "$entry" ]; then
            scan_restart_tree "$entry" "$purpose" "$reject_symlinks" \
                "$ignored_presentation_tree" || return 1
        fi
    done
}

# Keep the existing restart lock while the child retains the two existing
# snapshot flock descriptors through admission and installation. The child
# rechecks all paths and the sentinel; it does not create snapshot roots.
if [ "$MODE" = restore ] && [ "${RTB_JOINT_RESTART_OWNER:-}" != "$$" ]; then
    if state_authority restore-supervise "$STATE_ID" "$ONGOING" "$LEGACY_CURRENT" "$CURRENT_ROOT" \
        /bin/bash "$RESTART_HANDLER_DIR/restart_handler.sh"; then
        RESTART_HANDLER_SUCCESS=1
        exit 0
    else
        exit "$?"
    fi
fi

# Complete every destructive preflight before the first wipe.  The live state
# tree may contain ordinary symlinks (rm removes the link itself), but restore
# snapshots may not: their copy paths could otherwise traverse or reproduce an
# entry whose contents were not inspected.  The `_state` pathname is also the
# helper's flock fence, so a symlink there is never a valid mutation target.
scan_restart_tree "$ONGOING" "live ongoing state" 0
if [ -L "$ONGOING_STATE" ]; then
    restart_refuse_path "$ONGOING_STATE" "round-lock state-fence path is a symlink"
fi

if [ "$MODE" = "reset" ]; then
    scan_restart_tree "$LEGACY_CURRENT" "reset snapshot state" 0
else
    scan_restart_tree "$LEGACY_CURRENT" "restore snapshot state" 1
    scan_restart_tree "$CURRENT_ROOT" "restore snapshot state" 1 "$CURRENT_ROOT/tables/to_figures"
    authority_choice="$(state_authority select "$STATE_ID" "$ONGOING" "$LEGACY_CURRENT" "$CURRENT_ROOT")"
    case "$authority_choice" in
        0) STATE_AUTHORITY_ROOT="" ;;
        1) STATE_AUTHORITY_ROOT="$LEGACY_CURRENT" ;;
        2) STATE_AUTHORITY_ROOT="$CURRENT_ROOT" ;;
        *) state_authority_refuse "$CURRENT_ROOT" "invalid joint source selection" ;;
    esac
    r4d_restore prepare
fi

# Publish the process-crash-visible operation epoch only after every read-only
# refusal check has passed, and before the first destructive state mutation.
if [ "$MODE" = "reset" ]; then
    verify_reset_root || exit 1
fi
write_restart_sentinel applying

if [ "$MODE" = "reset" ]; then
    wipe_reset_ongoing
    wipe_dir_contents "$LEGACY_CURRENT"
    write_restart_sentinel applied
    exit 0
fi

# MODE=restore
# Complete a proven older backup in place before the live state that proves it
# is wiped.
r4d_restore upgrade
mkdir -p "$ONGOING"
wipe_dir_contents "$ONGOING"
mkdir -p "$ONGOING_STATE"
rm -rf "$ONGOING_STATE/.parser_state_txn"

restore_from_root() {
    local root="$1"
    local scope="${2:-all}"   # all | tables_plots | sequences
    RESTORE_FROM_ROOT_RESULT="absent"
    [ -d "$root" ] || return 0

    # Prefer the structured snapshot layout used by backup_update_and_clean.
    local restored=0
    local structured_layout_seen=0
    local subs=()
    local items=()
    local copy_items=()
    local item
    if [ "$scope" = "tables_plots" ]; then
        subs=( tables plots )
    elif [ "$scope" = "sequences" ]; then
        subs=( sequences )
    else
        subs=( tables plots sequences )
    fi
    for sub in ${subs[@]+"${subs[@]}"}; do
        local src="${root}/${sub}"
        if [ -d "$src" ]; then
            structured_layout_seen=1
            items=( "$src"/* )
            copy_items=()
            for item in ${items[@]+"${items[@]}"}; do
                # `tables/to_figures` contains derived absolute symlinks for
                # rendering.  The real table files are restored separately.
                if [ "$sub" = "tables" ] && [ "${item##*/}" = "to_figures" ]; then
                    continue
                fi
                if restore_skips_governed_name "${item##*/}"; then
                    continue
                fi
                copy_items+=( "$item" )
            done
            if (( ${#copy_items[@]} )); then
                for item in "${copy_items[@]}"; do
                    state_authority overlay-copy "$root" "$item" "$ONGOING_STATE/${item##*/}"
                done
                restored=1
            fi
        fi
    done

    # Backward-compatible: if the root doesn't have the structured layout,
    # treat it as a flat snapshot and copy its top-level contents.
    if [ "$structured_layout_seen" -eq 0 ]; then
        items=( "$root"/* )
        copy_items=()
        for item in ${items[@]+"${items[@]}"}; do
            if restore_skips_governed_name "${item##*/}"; then
                continue
            fi
            if [ "${item##*/}" = "state_authority" ]; then
                continue
            fi
            copy_items+=( "$item" )
        done
        if (( ${#copy_items[@]} )); then
            for item in "${copy_items[@]}"; do
                state_authority overlay-copy "$root" "$item" "$ONGOING_STATE/${item##*/}"
            done
            restored=1
        fi
    fi

    # Always try to restore done_pod5 tracking if present.
    if [ -z "$STATE_AUTHORITY_ROOT" ] && [ -f "${root}/done_pod5.txt" ]; then
        mkdir -p "${ONGOING_STATE}"
        cp -f "${root}/done_pod5.txt" "${ONGOING_STATE}/done_pod5.txt"
    fi

    rm -rf "${ONGOING_STATE}/.parser_state_txn"

    if [ "$restored" -eq 1 ]; then
        RESTORE_FROM_ROOT_RESULT="restored"
    fi
}

# Restore is a merge:
# - `temp/current` has uncompressed per-round + sequence snapshots (eg. qced_reads_hq_accumulated.fasta)
# - `current` has the rolling aggregate reports (often gzipped) required for cumulative plots
restored_any=0
restore_from_root "$LEGACY_CURRENT" all
if [ "$RESTORE_FROM_ROOT_RESULT" = "restored" ]; then
    restored_any=1
fi
restore_from_root "$CURRENT_ROOT" tables_plots
if [ "$RESTORE_FROM_ROOT_RESULT" = "restored" ]; then
    restored_any=1
fi
# If only current/state is available, also restore consensus sequences if present.
if [ -d "$CURRENT_ROOT/sequences/Consensus" ]; then
    mkdir -p "$ONGOING_STATE/Consensus"
    state_authority compat-copy "$CURRENT_ROOT/sequences/Consensus" "$ONGOING_STATE/Consensus" 0 0
    restored_any=1
fi
r4d_install_log="$(r4d_restore install)"
printf '%s\n' "$r4d_install_log"
case "$r4d_install_log" in
    *"restore install: installed 0 generation(s)") ;;
    *) restored_any=1 ;;
esac
if [ -n "$STATE_AUTHORITY_ROOT" ]; then
    restored_any=1
fi
if [ "$restored_any" -ne 1 ]; then
    write_restart_sentinel applied
    echo "WARN: restart_mode=restore requested but no snapshot found at $LEGACY_CURRENT or $CURRENT_ROOT; skipping restore" 1>&2
    exit 0
fi

# If we restored gzipped rolling reports, make sure the expected uncompressed files exist.
# Most of the reporting scripts read/write `*_rpt.txt` directly under `temp/ongoing/`.
for gz in "$ONGOING_STATE"/*.txt.gz "$ONGOING_STATE"/*.tsv.gz "$ONGOING_STATE"/*.csv.gz "$ONGOING_STATE"/*_rpt.txt.gz; do
    [ -f "$gz" ] || continue
    out="${gz%.gz}"
    if restore_skips_governed_name "${out##*/}"; then
        continue
    fi
    gunzip -c "$gz" > "$out"
done

# Restore consensus artifacts if present in sequence snapshots.
if [ -d "$ONGOING_STATE/single_exp/Consensus" ]; then
    mkdir -p "$ONGOING/Consensus"
    state_authority compat-copy "$ONGOING_STATE/single_exp/Consensus" "$ONGOING/Consensus" 0 0
fi
if [ -d "$ONGOING_STATE/Consensus" ]; then
    mkdir -p "$ONGOING/Consensus"
    state_authority compat-copy "$ONGOING_STATE/Consensus" "$ONGOING/Consensus" 0 0
fi
rm -rf "$ONGOING_STATE/.parser_state_txn" "$ONGOING/.parser_state_txn"

# Last, so no per-round or compatibility copy of a governed name
# (sequences/qced_reads_hq_accumulated.fasta, tables/round_index.tsv,
# done_pod5.txt) overrides the sealed state.
if [ -n "$STATE_AUTHORITY_ROOT" ]; then
    state_authority install "$STATE_AUTHORITY_ROOT" "$ONGOING_STATE"
fi

write_restart_sentinel applied
