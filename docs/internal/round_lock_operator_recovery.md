# Invalid round-lock operator recovery

This procedure preserves an invalid round lock for diagnosis and clears its
runtime pathname without certifying the round as complete. Use it only after
the helper has failed closed on an absent, malformed, or semantically invalid
generation snapshot.

Legacy or malformed locks are not reclaimed automatically. Do not delete,
rename, or edit a lock directory by hand.

## Procedure

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
   or a revocation and does not assert that finalization completed.

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

## Trust boundary

The local operation token, record checksums, and filesystem identity checks
provide correlation and accidental-corruption detection; they do not
authenticate who created or modified the files. Same-principal tampering and
cross-host ownership are outside this helper-foundation procedure and require
an explicit activation policy before the helper is wired into production.
The state-directory fence is advisory between cooperating helper processes;
activation also requires every runtime writer of this namespace to use that
fence, or for legacy direct writers to be proven unreachable.
