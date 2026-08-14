# Round-lock operator recovery and activation migration

This procedure preserves an invalid round lock for diagnosis and clears its
runtime pathname without certifying the round as complete. Use it only after
the helper has failed closed on an absent, malformed, or semantically invalid
generation snapshot.

Legacy or malformed locks are not reclaimed automatically. Do not delete,
rename, or edit a lock directory by hand. The separate stopped-world remedy
for a valid generation with structurally unusable subordinate state is
documented below; its confirmation is not interchangeable with
`--confirm-invalid-snapshot`.

## Invalid generation procedure

1. Stop every RTBioScan, Nextflow, scheduler, and helper process that can use
   the affected state directory. Confirm at the scheduler and host level that
   no related task remains. Keep the state directory unchanged while gathering
   the values below.

2. Identify the exact source basename. It must be one of:

   - `.round_inflight.lockdir` for the canonical lock;
   - `.round_inflight.lockdir.reclaim-<64-lowercase-hex>`; or
   - `.round_inflight.lockdir.release-<64-lowercase-hex>`.

   The latter two are active recovery quarantines, not terminal
   `.round_lock_archives/{release,reclaim}-*` or prior `.operator-*` evidence.
   Set explicit shell values and retain them in the incident record:

   ```bash
   RTB_STATE_DIR=/absolute/path/to/the/state-directory
   RTB_SOURCE_NAME=.round_inflight.lockdir
   RTB_SOURCE_PATH="$RTB_STATE_DIR/$RTB_SOURCE_NAME"
   RTB_OPERATOR_LABEL='operator-or-ticket-identity'
   RTB_REASON='concise reason for quarantining this invalid snapshot'
   ```

3. Record the source device and inode with portable Perl `lstat`. This command
   also refuses a symlink or a non-directory:

   ```bash
   perl -e '
     my @st = lstat($ARGV[0]);
     die "lstat failed: $!\n" if !@st;
     die "source is not a real directory\n" if -l _ || !-d _;
     print "$st[0]\t$st[1]\n";
   ' -- "$RTB_SOURCE_PATH"
   ```

   Copy the two printed integers into `RTB_LOCK_DEV` and `RTB_LOCK_INO`.
   Do not recompute them after an interrupted attempt.

4. Generate one 64-character lowercase hexadecimal operation token and retain
   it with the other incident values:

   ```bash
   RTB_OPERATION_TOKEN=$(perl -e '
     open my $fh, "<", "/dev/urandom" or die "open /dev/urandom: $!\n";
     read($fh, my $bytes, 32) == 32 or die "short read from /dev/urandom\n";
     print unpack("H*", $bytes);
   ')
   ```

5. Run the remedy from the repository root. Supply every argument exactly as
   recorded:

   ```bash
   perl bin/round_lock_generation.pl operator-quarantine-invalid \
     --state-dir "$RTB_STATE_DIR" \
     --source-name "$RTB_SOURCE_NAME" \
     --expected-lock-dev "$RTB_LOCK_DEV" \
     --expected-lock-ino "$RTB_LOCK_INO" \
     --operation-token "$RTB_OPERATION_TOKEN" \
     --operator-label "$RTB_OPERATOR_LABEL" \
     --reason "$RTB_REASON" \
     --confirm-invalid-snapshot
   ```

   The command refuses a valid generation, a changed source inode, conflicting
   evidence, or an unrelated source name. It does not write a release receipt
   or a revocation and does not assert that finalization completed. It also
   refuses the shared-path blockers described below, because preserving the
   invalid lock would not by itself make the resumed pipeline runnable.

6. If the command is interrupted, keep all files in place and rerun the exact
   same command with the same operation token, source name, device, inode,
   label, and reason. Never generate a replacement token for the same attempt.

7. After a zero exit, run the exact command once more as an idempotency and
   record-validation check. Then set the expected destination and event paths
   and verify their filesystem identities without following symlinks:

   ```bash
   RTB_OPERATOR_ARCHIVE="$RTB_STATE_DIR/.round_inflight.lockdir.operator-$RTB_OPERATION_TOKEN"
   RTB_INTENT_RECORD="$RTB_STATE_DIR/.round_lock_operator_events/$RTB_OPERATION_TOKEN.intent.tsv"
   RTB_COMPLETE_RECORD="$RTB_STATE_DIR/.round_lock_operator_events/$RTB_OPERATION_TOKEN.complete.tsv"
   RTB_PENDING_RECORD="$RTB_STATE_DIR/.round_lock_operator_pending/$RTB_OPERATION_TOKEN.tsv"

   perl -MErrno=ENOENT -e '
     my ($source, $archive, $dev, $ino, @records) = @ARGV;
     my @source_st = lstat($source);
     die "source still exists\n" if @source_st;
     die "cannot inspect source: $!\n" if $! != ENOENT;
     my @archive_st = lstat($archive);
     die "archive is not a real directory\n"
       if !@archive_st || -l _ || !-d _;
     die "archive identity changed\n"
       if $archive_st[0] != $dev || $archive_st[1] != $ino;
     for my $record (@records) {
       my @record_st = lstat($record);
       die "event is not a real regular file: $record\n"
         if !@record_st || -l _ || !-f _;
     }
   ' -- "$RTB_SOURCE_PATH" "$RTB_OPERATOR_ARCHIVE" \
     "$RTB_LOCK_DEV" "$RTB_LOCK_INO" \
     "$RTB_INTENT_RECORD" "$RTB_COMPLETE_RECORD"

   test ! -e "$RTB_PENDING_RECORD" && test ! -L "$RTB_PENDING_RECORD"
   ```

   The successful replay validates the checksums and contents; the `lstat`
   check above validates the preserved filesystem objects. Together they show
   that:

   - the original source pathname is absent;
   - `.round_inflight.lockdir.operator-$RTB_OPERATION_TOKEN` is a real
     directory with the recorded device and inode;
   - `.round_lock_operator_events/$RTB_OPERATION_TOKEN.intent.tsv` and
     `.round_lock_operator_events/$RTB_OPERATION_TOKEN.complete.tsv` are real
     regular files; and
   - the operation is absent from the active `.round_lock_operator_pending`
     namespace; and
   - the operator archive and both event records remain preserved.

   Do not delete or repurpose the `.operator-*` directory or its audit records.
   Preserve the command, its zero exit, and the recorded values in the incident
   log.

8. Resume the pipeline only after the verification above succeeds and no
   related process has restarted early. If the replay conflicts or any identity
   check changes, stop and investigate the state rather than forcing cleanup.

## Structurally unrecoverable valid generation

`operator-quarantine-unrecoverable` is a separate break-glass operation for a
generation whose `generation.tsv` is valid but whose exact subordinate or
generation/transition-bound finalization state prevents both normal progress
and normal fenced recovery. It uses
the same state-directory flock, identity check, intent, whole-tree rename, and
completion mechanism as the invalid-snapshot command. It never uses age, TTL,
PID liveness, hostname, or a second check-then-rename sequence as authority.

For the canonical `.round_inflight.lockdir`, the closed eligibility classes
are:

- `pins/` is absent, a symlink, or not a directory;
- an exact `ready.<64-lowercase-hex>.tsv` record is malformed or does not bind
  to the valid generation;
- `transition.tsv` is parse-invalid or semantically invalid for the valid
  generation;
- `transition.tsv` is absent while the exact generation handoff-marker pathname
  is occupied by an invalid record; or
- a valid release transition has unusable immutable authority: a reason/scope
  conflict, missing/invalid/conflicting handoff marker, missing/invalid allowed
  pin, or wrong allowed-pin role; or
- after all local transition authority is sound, one of the exact artifacts
  that the pending transition must consume or install conflicts: the
  generation-bound inflight record, its replaceable compatibility diagnostic,
  an occupied generation-bound finish disposition (which cannot legitimately
  precede terminal transition finalization),
  the action-selected release/revocation receipt, an occupied opposite-action
  receipt, the transition event, or the terminal archive destination.

For a `.reclaim-*` or `.release-*` recovery orphan, eligibility instead follows
the authority normal orphan recovery actually consumes: the orphan has no
valid transition, its action/operation token conflicts with its basename, or a
release orphan's exact release authority is unusable. Missing or malformed
unrelated pins do not authorize abandoning an otherwise recoverable orphan. A
correctly named orphan whose local authority is sound is also eligible when its
exact finish disposition, release/revocation receipt, transition event, or
terminal archive destination conflicts with finalization.

The command refuses a healthy generation, including a zero-second lease owned
by a dead PID. It also refuses a correctly recoverable reclaim/release orphan,
a generation merely blocked by a live pin, foreign/unknown-host ownership,
inert candidate/temp entries, and `ready.*` lookalikes outside the exact ready
record namespace. These are not structural abandonment authority.

External-finalization eligibility is deliberately exact, not a namespace scan.
The helper derives the old generation token, action, and operation token from
the validated snapshot and transition, then examines only their exact finish,
release and revocation paths, event, and archive destination. For a canonical
source carrying an exact generation-bound inflight record, it also examines
that record and the corresponding compatibility diagnostic. A finish path must
be absent until terminal transition finalization has completed; any occupation
is an impossible-state conflict. Another absent artifact or an exact canonical
action-selected record is recoverable and does not authorize abandonment; any
occupied opposite-action outcome is a conflict. An unsafe
global `.round_lock_events` or `.round_lock_archives` directory is
outside this command. A directory-valued `round_inflight.txt` is also outside
the command: moving only the lock tree cannot make a later inflight publication
replace that directory. These shared-path checks are non-authorizing vetoes for
both operator commands, even when the lock itself has a separately eligible
defect. Preserve those paths and stop for a separately reviewed storage-
administrator recovery; do not delete, rename, or overwrite them as part of
this procedure.

After completing steps 1–4 above, independently read the valid generation
token from the preserved source and retain it as `RTB_GENERATION_TOKEN`. Use an
independent record reader or incident tooling; do not use a token guessed from
a filename. Then run:

```bash
perl bin/round_lock_generation.pl operator-quarantine-unrecoverable \
  --state-dir "$RTB_STATE_DIR" \
  --source-name "$RTB_SOURCE_NAME" \
  --expected-lock-dev "$RTB_LOCK_DEV" \
  --expected-lock-ino "$RTB_LOCK_INO" \
  --expected-generation-token "$RTB_GENERATION_TOKEN" \
  --operation-token "$RTB_OPERATION_TOKEN" \
  --operator-label "$RTB_OPERATOR_LABEL" \
  --reason "$RTB_REASON" \
  --confirm-stopped-world \
  --confirm-abandon-generation "$RTB_GENERATION_TOKEN"
```

Both generation-token arguments must equal the valid record under the state
fence. The stopped-world assertion means every cooperating and legacy writer
on every host is stopped; it is not inferred from TTL or process inspection.
On interruption, replay this exact command. Verify the destination, audit, and
pending paths exactly as in steps 6–7. The schema-3 intent and completion bind
the operation kind, expected generation, structural basis, external marker,
the recorded root device/inode, and a recursive no-follow digest of every
descendant in the preserved lock tree. Root timestamps and other rename-
affected root metadata are not used as replay authority. When finalization is
the blocker, the records also bind a domain-separated digest of the exact
generation/transition-derived external artifacts described above. Those
external artifacts are observed evidence; they are not moved into the operator
archive. Perform the required exact-command replay and evidence verification
before any writer resumes, because a later round may legitimately replace the
shared compatibility diagnostic.

This action is abandonment, not completion. It writes no release receipt or
revocation, does not synthesize or change a transition reason, and does not
certify cached/output state. Pre-existing finish dispositions, receipts, and
events retain their own wire meaning: quarantine neither endorses nor revokes
them. Record and reconcile every such artifact. A fresh acquisition may rerun
unfinished work, but do not assume a rerun when an exact finish disposition or
release receipt already exists;
reconcile accumulated state and published outputs before resuming.
An old uncooperative worker that restarts after abandonment can overlap the new
owner, so this procedure is never an online force-unlock.

Evidence collection requires read/traverse access to every real directory in
the preserved tree. If it fails with `EACCES`/`EPERM`, stop. Record the original
metadata and have the storage administrator restore only the minimum original-
owner read/traverse permission needed for evidence collection; do not change
contents or substitute weaker evidence. Then recapture device/inode and begin a
new reviewed attempt.

## Activation and migration decision

Runtime completion now has two distinct durable records. The helper first
installs `.round_lock_release.<generation>.tsv` when generation authority is
released. The final backup boundary then installs
`.round_lock_finish.<generation>.tsv` before removing that generation's exact
handoff marker. `verify-release` authenticates only the release boundary;
`verify-finish` authenticates the completed helper-mediated backup
finalization/release boundary and adopts an interrupted exact marker removal.
That boundary is generation-pin protected in `full_round` and based on the
authenticated early release in `dorado_only`; it does not certify later heavy
publication work in the backup process. In `dorado_only`, FAST failure after early
release uses `cancel-handoff`. FAST arms that cleanup before `early-release`;
the helper reconciles either side of a lost release response under the state
fence, then records an immutable abandoned disposition before removing the
exact marker. It can never verify as a finished round. Do not delete, edit, or
infer these records from pathname
absence. Resume logic must distinguish an early-release receipt from a finish
receipt so `backup_update_and_clean` is not skipped before it has run.

Silent activation over an existing state directory is not acceptable. The
fail-closed policy is approved only with a stopped-world cutover gate:

1. Stop every old and new writer on every host and retain a state-directory
   backup/snapshot according to the deployment procedure.
2. Require the canonical `.round_inflight.lockdir` and active `.reclaim-*` /
   `.release-*` namespaces to be absent at cutover. Let a healthy old owner
   finish with its matching helper. Preserve legacy/invalid state with
   `operator-quarantine-invalid`; use the valid-generation command only for a
   closed structural class above.
3. Require `.round_lock_operator_pending` to be absent or empty. Operator-event
   schemas 1 and 2 predate this helper's finish-disposition evidence. Any
   pre-schema-3 pending operation must be finished with the exact helper
   revision that created it before this schema-3 helper is installed.
   There is no silent dual-parser migration.
4. Inventory and preserve any `.round_lock_handoff.*`,
   `.round_lock_release.*`, `.round_lock_finish.*`, `.round_lock_revocation.*`,
   `.round_inflight.*.tsv`, event, operator, and archive evidence. A reset or
   restore now refuses these namespaces; it does not glob-delete or copy them.
5. Resume only after the state is clear and every writer of round-owned state
   has been switched to the generation/pin protocol. The state fence itself is
   a short-lived helper-internal serialization mechanism, not a mutex held by
   process bodies. The first resumed round may legitimately rerun work
   abandoned during migration.

This preserves resume semantics as an explicit migration, not by TTL-reclaiming
unknown authority. Deployment must enforce the gate; encountering legacy state
after activation remains a deliberate fail-closed operator stop.

## Caller and test-hook activation policy

Callers that need recovery from a lost token response must durably generate and
retain an exact distinct `--token`/`--pin-token` pair before `acquire`, or an
exact `--pin-token` before `pin`. After an acquire error, `replay-acquire`
validates only that identical retained tuple; it never waits, creates, or
reclaims. Pin response replay retries the identical pin request. Tokenless calls
retain the legacy interface but cannot recover a lost response.

The pipeline's process guard retains each caller-supplied token in a
task-attempt-local, PID-qualified file before invoking the helper. FAST makes
one bounded acquire attempt and then only the non-progressing replay check;
`full_round` state writers hold exact process pins; `dorado_only` writers
instead authenticate the exact early-release receipt. A keyed drain of every
upstream generation-bound writer precedes cumulative summary mutation, so
failed-round placeholder rows cannot advance finalization around live writers.
Generation and scope travel as Nextflow values so a replacement generation
changes every state-writer task hash. FAST remains cacheable during an ordinary
`-resume` of the same state namespace: its declared cache input binds the
selected namespace, state-compatibility contract, applied restart epoch, and a
SHA-256 contract over the exact round-lock helper and guard bytes. This input
does not use `workflow.runName`, which Nextflow may change on resume, so a cached
task re-emits the exact handed-off generation and unfinished writers can
authenticate and continue it. If no explicit `--state_id` was supplied, the
existing run-name default selects a different namespace on a newly named run;
use an explicit state ID when continuity across invocations is required. Each
actually applied reset or restore, including a forced same-mode operation,
records a new random operation epoch. Exact `.` and `..` state identifiers are
rejected before any restart path is constructed.
The restart handler atomically records `status=applying` after read-only
preflight and before its first destructive mutation, then records
`status=applied` after success. A mode-off launch fails closed on an incomplete
or malformed record. A material copy, decompression, or cleanup failure after
`applying` returns nonzero, releases the restart lock, and leaves that record
for an explicit reset/restore replay. Abrupt termination is different: the
mkdir-based restart lock can survive SIGKILL. Under the stopped-world policy,
first verify that no restart handler or pipeline launcher remains alive, capture
the lock pathname and metadata, require the exact
`.restart_applied.<state_id>.lockdir` to be a real empty directory, and remove
only that directory with `rmdir`; never glob or TTL-reclaim it. Then replay the
explicit reset/restore operation, which reconciles the surviving `applying`
record. An occupied, nonempty, symlinked, or otherwise unexpected restart-lock
path is an operator stop, not cleanup authority. Legacy two-line restart
sentinels remain readable through an exact-byte digest. A changed namespace,
compatibility contract, applied restart epoch, or
round-lock runtime byte contract reruns FAST; only after the helper's authority
checks can it create a new generation, which deliberately invalidates the
state-writer task hashes. A helper or guard byte change while an older handoff
exists therefore requires the same stopped-world completion or reconciliation
used for other protocol changes; do not rely on cache invalidation to reclaim
that authority. The `applying` record gives a process-crash-visible recovery
intent, but restart-lock removal after abrupt death remains the manual
stopped-world step above. Physical-power-loss durability is not claimed: the
restart handler does not fsync the sentinel and every mutated state path in a
structural durability order.
Backup remains uncached and idempotent. Do not move generation identity to an
undeclared file, environment variable, or mutable "current generation" lookup;
a cached task could then skip the helper and cross a replacement-generation
boundary. Failed worker pins
remain durable evidence and become nonblocking only through the helper's
same-host dead-process rules; the guard does not replace existing task traps.

The nominal helper cost is per scheduled round-task attempt, never per read or
OTU. A fresh, fully executed `full_round` path runs the helper 39 times plus 16
durable token-file Perl launches; `dorado_only` runs it 28 times plus 4
token-file launches, before retries or error cleanup. A transcript-recorded,
3-warm-up/30-iteration synthetic lifecycle benchmark on local APFS with 272 to
340 `_state` root entries measured cumulative p50/p95 times of 1.675/1.782
seconds for `full_round`, and 1.075/1.092 seconds for `dorado_only`. This is not
an end-to-end or contention benchmark. Its raw timing samples were not
retained, and local APFS does not predict fsync latency on shared storage.
Repeat the lifecycle measurement on
the deployment filesystem before cutover when `_state` is network- or
shared-filesystem backed.

The failpoint pause hooks are accepted as a foundation-only testability seam:
unknown names are fatal; pause names are exact; both path environment variables
are validated; and the wait is bounded. Production launchers must clear every
`RTBIOSCAN_ROUND_LOCK_FAILPOINT` and `RTBIOSCAN_ROUND_LOCK_TEST_*` variable.
Any future hook or relaxation requires a separate policy review.

## Trust boundary

The accepted model is cooperative same-principal writers, including writers on
different hosts that share this state directory. The local operation token,
recursive evidence digests, record checksums, and filesystem identity checks
provide correlation and accidental-corruption detection; they do not
authenticate who created or modified the files. The state-directory fence is
advisory, so activation requires every runtime writer of this namespace to use
the generation/pin protocol mediated by that fence, or for legacy direct
writers to be proven unreachable. A hostile
or non-cooperating same-UID writer requires OS isolation or keyed
authentication; another checksum or TTL check cannot establish authority.
