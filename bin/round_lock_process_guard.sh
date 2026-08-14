#!/usr/bin/env bash

# Generation fencing for stateful Nextflow task scripts.
#
# The caller must set:
#   RTBIOSCAN_ROUND_LOCK_STATE_DIR
#   RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE
#   RTBIOSCAN_ROUND_LOCK_SCOPE
#   RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN
#   RTBIOSCAN_ROUND_LOCK_HELPER
#
# A full_round writer must also set RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE to a
# task-attempt-local path (the pipeline includes the task shell PID).  The
# token is installed durably before pin and reused for an exact response-loss
# retry.  A failed task deliberately leaves its pin behind.  Its record names
# the task shell PID, so the helper stops treating it as live after that shell
# exits.  No trap is installed or replaced here.

# Test hooks are accepted in the helper as a foundation testability seam, but
# a production task must never inherit them from the Nextflow launcher.
unset RTBIOSCAN_ROUND_LOCK_FAILPOINT
_rtbioscan_round_lock_test_variables=$(compgen -A variable \
	RTBIOSCAN_ROUND_LOCK_TEST_ || :)
for _rtbioscan_round_lock_test_variable in \
	$_rtbioscan_round_lock_test_variables; do
	unset "$_rtbioscan_round_lock_test_variable"
done
unset _rtbioscan_round_lock_test_variable
unset _rtbioscan_round_lock_test_variables

rtbioscan_round_lock_error() {
	printf 'ERROR: %s\n' "$1" >&2
	return 1
}

rtbioscan_round_lock_validate_token() {
	_rtbioscan_round_lock_token=$1
	case "$_rtbioscan_round_lock_token" in
		''|*[!0-9a-f]*)
			rtbioscan_round_lock_error "invalid round-lock token"
			return 1
			;;
	esac
	if [ "${#_rtbioscan_round_lock_token}" -ne 64 ]; then
		rtbioscan_round_lock_error "invalid round-lock token length"
		return 1
	fi
}

rtbioscan_round_lock_validate_text() {
	_rtbioscan_round_lock_text_label=$1
	_rtbioscan_round_lock_text_value=$2
	if [ -z "$_rtbioscan_round_lock_text_value" ]; then
		rtbioscan_round_lock_error \
			"missing round-lock $_rtbioscan_round_lock_text_label"
		return 1
	fi
	case "$_rtbioscan_round_lock_text_value" in
		*[$'\t\r\n']*)
			rtbioscan_round_lock_error \
				"round-lock $_rtbioscan_round_lock_text_label contains unsupported control characters"
			return 1
			;;
	esac
}

# Print the exact token retained at PATH.  Installation mirrors the helper's
# immutable-record discipline: sync a 0600 temporary file, hard-link it into
# place without replacement, remove only that exact temporary name, then sync
# the parent directory.  A retry adopts only a regular, non-symlink canonical
# file containing exactly one lowercase-hex token line.
rtbioscan_round_lock_prepare_token_file() {
	_rtbioscan_round_lock_token_path=${1-}
	_rtbioscan_round_lock_token_label=${2-}
	if [ -z "$_rtbioscan_round_lock_token_path" ] || \
		[ -z "$_rtbioscan_round_lock_token_label" ]; then
		rtbioscan_round_lock_error "token preparation requires a path and label"
		return 1
	fi

	LC_ALL=C LANG=C LC_CTYPE=C perl - \
		"$_rtbioscan_round_lock_token_path" \
		"$_rtbioscan_round_lock_token_label" <<'RTBIOSCAN_TOKEN_PERL'
use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use Errno qw(EEXIST ENOENT);
use Fcntl qw(:DEFAULT O_NOFOLLOW O_RDONLY);
use File::Basename qw(dirname);
use IO::Handle;
use Time::HiRes qw(time);

die "ERROR: Fcntl O_NOFOLLOW is unavailable or inert for token files\n"
    if !O_NOFOLLOW;

my ($path, $label) = @ARGV;
die "ERROR: invalid token file path\n"
    if !defined($path) || $path eq '' || $path =~ /[\r\n]/;
die "ERROR: invalid token label\n"
    if !defined($label) || $label eq '' || $label =~ /[\t\r\n]/;

my $token_re = qr/\A[0-9a-f]{64}\z/;

sub sync_directory {
    my ($directory) = @_;
    sysopen(my $fh, $directory, O_RDONLY)
        or die "ERROR: cannot open token directory '$directory': $!\n";
    $fh->sync()
        or die "ERROR: cannot sync token directory '$directory': $!\n";
    close($fh)
        or die "ERROR: cannot close token directory '$directory': $!\n";
}

sub read_token {
    my ($candidate) = @_;
    my @before = lstat($candidate);
    die "ERROR: cannot inspect token file '$candidate': $!\n" if !@before;
    die "ERROR: token file is not a regular non-symlink file: $candidate\n"
        if -l _ || !-f _;
    sysopen(my $fh, $candidate, O_RDONLY | O_NOFOLLOW)
        or die "ERROR: cannot open token file '$candidate': $!\n";
    my @opened = stat($fh);
    die "ERROR: cannot inspect open token file '$candidate': $!\n"
        if !@opened;
    die "ERROR: token file changed while opening: $candidate\n"
        if $opened[0] != $before[0] || $opened[1] != $before[1];
    local $/;
    my $content = <$fh>;
    close($fh) or die "ERROR: cannot close token file '$candidate': $!\n";
    die "ERROR: malformed token file: $candidate\n"
        if !defined($content) || $content !~ /\A([0-9a-f]{64})\n\z/;
    return $1;
}

my $parent = dirname($path);
my @parent_before = lstat($parent);
die "ERROR: token parent is not a real directory: $parent\n"
    if !@parent_before || -l _ || !-d _;
sysopen(my $parent_fh, $parent, O_RDONLY)
    or die "ERROR: cannot open token parent '$parent': $!\n";
my @parent_opened = stat($parent_fh);
die "ERROR: cannot inspect token parent '$parent': $!\n"
    if !@parent_opened;
die "ERROR: token parent changed while opening: $parent\n"
    if $parent_opened[0] != $parent_before[0]
    || $parent_opened[1] != $parent_before[1]
    || !-d _;
close($parent_fh)
    or die "ERROR: cannot close token parent '$parent': $!\n";

my @existing = lstat($path);
if (@existing) {
    my $retained = read_token($path);
    # Adopt a canonical name whose installer may have died after link(2) but
    # before the parent-directory durability barrier.
    sync_directory($parent);
    print "$retained\n";
    exit 0;
}
die "ERROR: cannot inspect token file '$path': $!\n" if !$!{ENOENT};

sysopen(my $random_fh, '/dev/urandom', O_RDONLY)
    or die "ERROR: cannot open system CSPRNG /dev/urandom: $!\n";
my $random = '';
while (length($random) < 32) {
    my $read = sysread($random_fh, $random, 32 - length($random), length($random));
    die "ERROR: system CSPRNG returned insufficient data\n"
        if !defined($read) || $read == 0;
}
close($random_fh)
    or die "ERROR: cannot close /dev/urandom: $!\n";
my $token = sha256_hex(join("\0", $random, $label, $$, sprintf('%.9f', time())));
my $temp = "$path.tmp-" . sha256_hex(join("\0", $random, $path, time()));

sysopen(my $temp_fh, $temp, O_WRONLY | O_CREAT | O_EXCL, 0600)
    or die "ERROR: cannot create token candidate '$temp': $!\n";
if (!print {$temp_fh} "$token\n" || !$temp_fh->flush() || !$temp_fh->sync()) {
    my $error = $!;
    close($temp_fh);
    unlink($temp);
    die "ERROR: cannot persist token candidate '$temp': $error\n";
}
if (!close($temp_fh)) {
    my $error = $!;
    unlink($temp);
    die "ERROR: cannot close token candidate '$temp': $error\n";
}

my $linked = link($temp, $path);
my $exists = !$linked && $!{EEXIST};
my $link_error = $linked ? '' : "$!";
unlink($temp)
    or die "ERROR: cannot remove exact token candidate '$temp': $!\n";
sync_directory($parent);
die "ERROR: cannot install token file '$path': $link_error\n"
    if !$linked && !$exists;

my $retained = read_token($path);
die "ERROR: installed token file conflicts: $path\n" if $linked && $retained ne $token;
print "$retained\n";
RTBIOSCAN_TOKEN_PERL
}

rtbioscan_round_lock_validate_context() {
	if [ -z "${RTBIOSCAN_ROUND_LOCK_STATE_DIR-}" ] || \
		[ -z "${RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE-}" ] || \
		[ -z "${RTBIOSCAN_ROUND_LOCK_SCOPE-}" ] || \
		[ -z "${RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN-}" ] || \
		[ -z "${RTBIOSCAN_ROUND_LOCK_HELPER-}" ]; then
		rtbioscan_round_lock_error "incomplete round-lock process context"
		return 1
	fi
	case "$RTBIOSCAN_ROUND_LOCK_SCOPE" in
		full_round|dorado_only) ;;
		*)
			rtbioscan_round_lock_error \
				"invalid round-lock scope: $RTBIOSCAN_ROUND_LOCK_SCOPE"
			return 1
			;;
	esac
	rtbioscan_round_lock_validate_text state-directory \
		"$RTBIOSCAN_ROUND_LOCK_STATE_DIR" || return $?
	rtbioscan_round_lock_validate_text round-barcode \
		"$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE" || return $?
	rtbioscan_round_lock_validate_text helper-path \
		"$RTBIOSCAN_ROUND_LOCK_HELPER" || return $?
	rtbioscan_round_lock_validate_token \
		"$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN" || return $?
	if [ ! -d "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" ] || \
		[ -L "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" ]; then
		rtbioscan_round_lock_error \
			"round-lock state directory is not a real directory: $RTBIOSCAN_ROUND_LOCK_STATE_DIR"
		return 1
	fi
	if [ ! -r "$RTBIOSCAN_ROUND_LOCK_HELPER" ] || \
		[ ! -f "$RTBIOSCAN_ROUND_LOCK_HELPER" ] || \
		[ -L "$RTBIOSCAN_ROUND_LOCK_HELPER" ]; then
		rtbioscan_round_lock_error \
			"round-lock helper is not a readable regular file: $RTBIOSCAN_ROUND_LOCK_HELPER"
		return 1
	fi
}

rtbioscan_round_lock_validate_role() {
	_rtbioscan_round_lock_role=${1-}
	case "$_rtbioscan_round_lock_role" in
		''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*)
			rtbioscan_round_lock_error \
				"invalid round-lock pin role: $_rtbioscan_round_lock_role"
			return 1
			;;
	esac
}

rtbioscan_round_lock_verify() {
	_rtbioscan_round_lock_verify_command=$1
	LC_ALL=C LANG=C LC_CTYPE=C perl "$RTBIOSCAN_ROUND_LOCK_HELPER" \
		"$_rtbioscan_round_lock_verify_command" \
		--state-dir "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" \
		--round-barcode "$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE" \
		--scope "$RTBIOSCAN_ROUND_LOCK_SCOPE" \
		--token "$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN"
}

rtbioscan_round_lock_pin() {
	_rtbioscan_round_lock_role=${1-}
	rtbioscan_round_lock_validate_context || return $?
	rtbioscan_round_lock_validate_role "$_rtbioscan_round_lock_role" || return $?

	RTBIOSCAN_ROUND_LOCK_PIN_TOKEN=
	RTBIOSCAN_ROUND_LOCK_PIN_ROLE=
	RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED=0
	export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN
	export RTBIOSCAN_ROUND_LOCK_PIN_ROLE
	export RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED

	if [ "$RTBIOSCAN_ROUND_LOCK_SCOPE" = "dorado_only" ]; then
		if _rtbioscan_round_lock_result=$(rtbioscan_round_lock_verify verify-finish 2>&1); then
			if [ "$_rtbioscan_round_lock_role" != "backup_update_and_clean" ]; then
				rtbioscan_round_lock_error \
					"dorado_only generation is already finished"
				return 1
			fi
			if [ "$_rtbioscan_round_lock_result" != "dorado_only_early" ]; then
				rtbioscan_round_lock_error \
					"unexpected dorado_only finish reason: $_rtbioscan_round_lock_result"
				return 1
			fi
			RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED=1
			export RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED
			return 0
		else
			_rtbioscan_round_lock_status=$?
			_rtbioscan_round_lock_expected="ERROR: missing authenticated finish receipt for generation $RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN"
			if [ "$_rtbioscan_round_lock_result" != "$_rtbioscan_round_lock_expected" ]; then
				printf '%s\n' "$_rtbioscan_round_lock_result" >&2
				return "$_rtbioscan_round_lock_status"
			fi
		fi
		if _rtbioscan_round_lock_result=$(rtbioscan_round_lock_verify verify-release 2>&1); then
			:
		else
			_rtbioscan_round_lock_status=$?
			printf '%s\n' "$_rtbioscan_round_lock_result" >&2
			return "$_rtbioscan_round_lock_status"
		fi
		if [ "$_rtbioscan_round_lock_result" != "dorado_only_early" ]; then
			rtbioscan_round_lock_error \
				"unexpected dorado_only release reason: $_rtbioscan_round_lock_result"
			return 1
		fi
		return 0
	fi

	if [ "$_rtbioscan_round_lock_role" = "backup_update_and_clean" ]; then
		if _rtbioscan_round_lock_result=$(rtbioscan_round_lock_verify verify-finish 2>&1); then
			if [ "$_rtbioscan_round_lock_result" != "full_round_released" ]; then
				rtbioscan_round_lock_error \
					"unexpected full_round finish reason: $_rtbioscan_round_lock_result"
				return 1
			fi
			RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED=1
			export RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED
			return 0
		else
			_rtbioscan_round_lock_status=$?
			_rtbioscan_round_lock_missing_release="ERROR: missing authenticated release receipt for generation $RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN"
			_rtbioscan_round_lock_missing_finish="ERROR: missing authenticated finish receipt for generation $RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN"
			if [ "$_rtbioscan_round_lock_result" = "$_rtbioscan_round_lock_missing_finish" ]; then
				# Release is terminal and authenticated; finish can only resume
				# exact marker cleanup and installation of its finish receipt.
				if _rtbioscan_round_lock_result=$(LC_ALL=C LANG=C LC_CTYPE=C \
					perl "$RTBIOSCAN_ROUND_LOCK_HELPER" finish \
					--state-dir "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" \
					--round-barcode "$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE" \
					--scope "$RTBIOSCAN_ROUND_LOCK_SCOPE" \
					--token "$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN" 2>&1); then
					:
				else
					_rtbioscan_round_lock_status=$?
					printf '%s\n' "$_rtbioscan_round_lock_result" >&2
					return "$_rtbioscan_round_lock_status"
				fi
				if _rtbioscan_round_lock_result=$(rtbioscan_round_lock_verify verify-finish 2>&1); then
					:
				else
					_rtbioscan_round_lock_status=$?
					printf '%s\n' "$_rtbioscan_round_lock_result" >&2
					return "$_rtbioscan_round_lock_status"
				fi
				if [ "$_rtbioscan_round_lock_result" != "full_round_released" ]; then
					rtbioscan_round_lock_error \
						"unexpected full_round finish reason: $_rtbioscan_round_lock_result"
					return 1
				fi
				RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED=1
				export RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED
				return 0
			fi
			if [ "$_rtbioscan_round_lock_result" != "$_rtbioscan_round_lock_missing_release" ]; then
				printf '%s\n' "$_rtbioscan_round_lock_result" >&2
				return "$_rtbioscan_round_lock_status"
			fi
		fi
	fi

	if [ -z "${RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE-}" ]; then
		rtbioscan_round_lock_error "missing round-lock pin-token file path"
		return 1
	fi
	RTBIOSCAN_ROUND_LOCK_PIN_TOKEN=$(rtbioscan_round_lock_prepare_token_file \
		"$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE" \
		"pin:$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN:$_rtbioscan_round_lock_role:$$") \
		|| return $?
	rtbioscan_round_lock_validate_token "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" || return $?
	RTBIOSCAN_ROUND_LOCK_PIN_ROLE=$_rtbioscan_round_lock_role
	export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN
	export RTBIOSCAN_ROUND_LOCK_PIN_ROLE

	_rtbioscan_round_lock_pin_attempt=1
	while [ "$_rtbioscan_round_lock_pin_attempt" -le 2 ]; do
		if _rtbioscan_round_lock_result=$(LC_ALL=C LANG=C LC_CTYPE=C \
			perl "$RTBIOSCAN_ROUND_LOCK_HELPER" pin \
			--state-dir "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" \
			--round-barcode "$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE" \
			--scope "$RTBIOSCAN_ROUND_LOCK_SCOPE" \
			--token "$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN" \
			--pin-token "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" \
			--owner-pid "$$" \
			--role "$_rtbioscan_round_lock_role" 2>&1); then
			if [ "$_rtbioscan_round_lock_result" != "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" ]; then
				rtbioscan_round_lock_error \
					"round-lock pin response did not match retained token"
				return 1
			fi
			return 0
		else
			_rtbioscan_round_lock_status=$?
		fi
		_rtbioscan_round_lock_pin_attempt=$((_rtbioscan_round_lock_pin_attempt + 1))
	done
	printf '%s\n' "$_rtbioscan_round_lock_result" >&2
	return "$_rtbioscan_round_lock_status"
}

rtbioscan_round_lock_unpin() {
	rtbioscan_round_lock_validate_context || return $?
	if [ "$RTBIOSCAN_ROUND_LOCK_SCOPE" = "dorado_only" ] || \
		[ "${RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED:-0}" -eq 1 ]; then
		return 0
	fi
	rtbioscan_round_lock_validate_token \
		"${RTBIOSCAN_ROUND_LOCK_PIN_TOKEN-}" || return $?
	rtbioscan_round_lock_validate_role \
		"${RTBIOSCAN_ROUND_LOCK_PIN_ROLE-}" || return $?
	LC_ALL=C LANG=C LC_CTYPE=C perl "$RTBIOSCAN_ROUND_LOCK_HELPER" guard-pin \
		--state-dir "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" \
		--round-barcode "$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE" \
		--scope "$RTBIOSCAN_ROUND_LOCK_SCOPE" \
		--token "$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN" \
		--pin-token "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" \
		--role "$RTBIOSCAN_ROUND_LOCK_PIN_ROLE" || return $?
	LC_ALL=C LANG=C LC_CTYPE=C perl "$RTBIOSCAN_ROUND_LOCK_HELPER" unpin \
		--state-dir "$RTBIOSCAN_ROUND_LOCK_STATE_DIR" \
		--round-barcode "$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE" \
		--scope "$RTBIOSCAN_ROUND_LOCK_SCOPE" \
		--token "$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN" \
		--pin-token "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" \
		--best-effort || return $?
	RTBIOSCAN_ROUND_LOCK_PIN_TOKEN=
	RTBIOSCAN_ROUND_LOCK_PIN_ROLE=
	export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN
	export RTBIOSCAN_ROUND_LOCK_PIN_ROLE
}
