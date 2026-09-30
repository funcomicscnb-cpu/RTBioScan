#!/usr/bin/env python3
"""Stage B history authority. classify is read-only; write modes require the Stage A FD.

CLI: one state token on stdout, reason/stats on stderr; classifications exit 0.
Usage errors exit 64, refused authority/write errors 73. JSONL uses literal LF.
Numbers are exact Decimal values; -0 equals 0, bool never equals a number.
"""
import argparse
from decimal import Decimal, DecimalException
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import tempfile

SCHEMAS = {'1.0', '1.1', '1.2', '1.3', '1.4', '1.5', '1.6', '2.0', '2.1'}


class Refusal(ValueError):
    pass


def reject_constant(value):
    raise Refusal('non_json_number:' + value)


def pairs(values):
    obj = {}
    for key, value in values:
        if key in obj:
            raise Refusal('duplicate_object_key')
        obj[key] = value
    return obj


def loads(raw):
    return json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw,
                      parse_int=Decimal, parse_float=Decimal,
                      parse_constant=reject_constant, object_pairs_hook=pairs)


def canonical(value):
    if isinstance(value, dict):
        return ('object', tuple(sorted((k, canonical(v)) for k, v in value.items())))
    if isinstance(value, list):
        return ('array', tuple(map(canonical, value)))
    if isinstance(value, bool):
        return ('bool', value)
    if isinstance(value, (Decimal, int)):
        return ('number', Decimal(value))
    if value is None:
        return ('null',)
    if isinstance(value, str):
        return ('string', value)
    raise Refusal('non_exact_json_value')


def equivalent(left, right):
    return canonical(left) == canonical(right)


def regular(path, directory=False, missing=False):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise Refusal('unsafe_path:' + str(path))
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise Refusal('unsafe_ancestor:' + str(parent))
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing:
            return False
        raise Refusal('missing_authority:' + str(path))
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise Refusal('unsafe_path:' + str(path))
    return True


def identity(obj, state, rb=None):
    if not isinstance(obj, dict):
        raise Refusal('non_object')
    for field in ('run_id', 'barcode', 'round_barcode'):
        if not isinstance(obj.get(field), str) or not obj[field] or any(c in obj[field] for c in ('\x00', '\r', '\n', '\t', '/')):
            raise Refusal('invalid_identity:' + field)
    if obj['round_barcode'] in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', obj['barcode']):
        raise Refusal('invalid_identity')
    if obj.get('state_id') != state.name or (rb is not None and obj['round_barcode'] != rb):
        raise Refusal('identity_mismatch')
    if obj.get('schema_version') not in SCHEMAS:
        raise Refusal('unsupported_schema')
    return tuple(obj[k] for k in ('run_id', 'barcode', 'round_barcode'))


def index_mapping(path, current):
    regular(path)
    mapping, used = {}, set()
    with Path(path).open('rb') as stream:
        for raw in stream:
            match = re.fullmatch(rb'([^\t/\x00\r\n]+)\t([1-9][0-9]{0,8})\n', raw)
            if not match:
                raise Refusal('malformed_round_index')
            rb, value = match[1].decode('utf-8'), int(match[2])
            if rb in ('.', '..') or rb in mapping or value in used:
                raise Refusal('duplicate_or_unsafe_round_index')
            mapping[rb] = value
            used.add(value)
    if current is not None and current not in mapping:
        raise Refusal('current_unmapped')
    return mapping


def terminal_round(state_dir, index, current, run_id=None, barcode=None):
    """Latest retained round for the current (run_id, barcode) in index order."""
    mapping = index_mapping(index, current)
    state = Path(state_dir).absolute()
    regular(state, directory=True)
    view = History(state, current, index=index, run_id=run_id, barcode=barcode)
    view.mapping = mapping
    if regular(state / current, directory=True, missing=True) and regular(state / current / 'round_report.json', missing=True):
        current_obj, _ = view.report(current)
        if ((run_id is not None and current_obj['run_id'] != run_id) or
                (barcode is not None and current_obj['barcode'] != barcode)):
            raise Refusal('current_identity_mismatch')
        target = current_obj['run_id'], current_obj['barcode']
    elif run_id is not None and barcode is not None:
        target = run_id, barcode
    else:
        raise Refusal('missing_reporting_identity')
    for rb in sorted(mapping, key=mapping.get, reverse=True):
        if not regular(state / rb, directory=True, missing=True):
            continue
        if regular(state / rb / 'round_report.json', missing=True):
            obj, _ = view.report(rb)
            if (obj['run_id'], obj['barcode']) == target:
                return rb
            view.cache.pop(rb, None)
    raise Refusal('missing_retained_round_report')


def round_order_digest(entries):
    digest = hashlib.sha256()
    for rb, member in entries:
        digest.update(rb.encode('utf-8') + b'\0' + member.encode('utf-8') + b'\0')
    return digest.hexdigest()


def digest_frame(digest, value):
    """Length-prefix each field; identifiers may contain non-LF Unicode separators."""
    data = value if isinstance(value, bytes) else str(value).encode('utf-8')
    digest.update(len(data).to_bytes(8, 'big'))
    digest.update(data)


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def private_context(source, context_path=None):
    try:
        return loads(Path(context_path if context_path else str(source) + '.authority.json').read_bytes())
    except FileNotFoundError:
        raise Refusal('source_run_authority_mismatch') from None


def run_terminals(state_dir, index, current, run_id, barcode, include_context=False, verify_history=False, history=None):
    """Retained terminals for this run, in authoritative index order."""
    mapping = index_mapping(index, current)
    state = Path(state_dir).absolute()
    regular(state, directory=True)
    reader = History(state, current, history=history, index=index, run_id=run_id, barcode=barcode)
    reader.mapping = mapping
    current_path = state / current / 'round_report.json' if current is not None else None
    if current is not None and regular(state / current, directory=True, missing=True) and regular(current_path, missing=True):
        obj, _ = reader.report(current)
        if ((run_id is not None and obj['run_id'] != run_id) or
                (barcode is not None and obj['barcode'] != barcode)):
            raise Refusal('current_identity_mismatch')
        if run_id is None:
            run_id = obj['run_id']
        if barcode is None:
            barcode = obj['barcode']
    elif run_id is None or (current is not None and barcode is None):
        raise Refusal('missing_reporting_identity')
    terminals = {}
    roster = []
    order_sha = hashlib.sha256() if include_context else None
    authority_sha = hashlib.sha256() if include_context else None
    if include_context:
        authority_sha.update(b'RTB_STAGE_B_FULL_AUTHORITY_V1\0')
        digest_frame(authority_sha, run_id)
    count = 0
    last_round = last_barcode = None
    issues, same_run_order = [], []
    if verify_history:
        order, duplicates = reader.inventory()
        if reader.malformed:
            raise Refusal('malformed_history')
        if duplicates:
            raise Refusal('duplicate_history_identity')
        history_stream = reader.path.open('rb') if reader.path.exists() else None
    else:
        history_stream = None
    try:
        ordered_rounds = sorted(mapping, key=mapping.get)
        for rb in ordered_rounds:
            if not regular(state / rb, directory=True, missing=True):
                continue
            if not regular(state / rb / 'round_report.json', missing=True):
                continue
            obj, _ = reader.report(rb)
            if obj['run_id'] == run_id:
                if include_context:
                    if obj['barcode'] not in terminals:
                        roster.append(obj['barcode'])
                    order_sha.update(rb.encode('utf-8') + b'\0' + obj['barcode'].encode('utf-8') + b'\0')
                    for field in (mapping[rb], rb, obj['barcode'], reader.report_digests[rb]):
                        digest_frame(authority_sha, field)
                    count += 1
                    last_round, last_barcode = rb, obj['barcode']
                terminals[obj['barcode']] = rb
                if verify_history:
                    same_run_order.append(rb)
                    record = reader.records.get(rb)
                    if record is None:
                        issues.append((mapping[rb], run_id, obj['barcode'], rb, 'normalize', 'missing_history_row'))
                    else:
                        if record[3] != identity(obj, state, rb):
                            raise Refusal('identity_mismatch:' + rb)
                        history_stream.seek(record[0])
                        old = loads(history_stream.read(record[1]))
                        if not equivalent(old, obj):
                            issues.append((mapping[rb], run_id, obj['barcode'], rb, 'normalize', 'stale_history_row'))
            reader.cache.pop(rb, None)
            reader.report_digests.pop(rb, None)
    finally:
        if history_stream is not None:
            history_stream.close()
    if verify_history:
        actual_order = [rb for rb in order if reader.records[rb][3][0] == run_id]
        if actual_order != same_run_order:
            rb = next((name for name in same_run_order if name not in actual_order),
                      same_run_order[-1] if same_run_order else current)
            issues.append((mapping.get(rb, 0), run_id, barcode or last_barcode, rb,
                           'normalize', 'history_order_or_membership'))
    if not terminals:
        raise Refusal('missing_retained_run_report')
    if include_context:
        digest_frame(authority_sha, count)
        digest_frame(authority_sha, len(roster))
        for member in roster:
            digest_frame(authority_sha, member)
    context = authority_context(run_id, roster, last_barcode, last_round, count, order_sha.hexdigest(),
                                authority_sha.hexdigest()) if include_context else None
    if verify_history:
        issues.sort()
        return terminals, mapping, context, issues
    return (terminals, mapping, context) if include_context else (terminals, mapping)


def run_completion(state_dir, index, current, run_id, barcode, history=None, include_context=False):
    result = run_terminals(state_dir, index, current, run_id, barcode, include_context,
                           verify_history=include_context, history=history)
    terminals, mapping = result[:2]
    if include_context:
        return terminals, result[3], result[2]
    pending = []
    for member, rb in terminals.items():
        view = History(state_dir, rb, history, index, run_id, member)
        if view.classify() != 'complete':
            pending.append((mapping[rb], run_id, member, rb, view.state, view.reason))
    pending.sort()
    return terminals, pending


def authority_context(run_id, roster, terminal_barcode, terminal_round, count, order_digest, full_digest):
    """Transient validated identity/order supplied to all authoritative publishers."""
    if not roster or not terminal_round:
        raise Refusal('missing_retained_run_report')
    if any(not member for member in roster):
        raise Refusal('empty_authoritative_barcode')
    return {'run_id': run_id, 'barcodes': roster, 'barcode': terminal_barcode,
            'last_round_barcode': terminal_round, 'rounds_count': count,
            'round_order_sha256': order_digest, 'authority_revision': full_digest}


def context_for_run(state_dir, run_id, require_complete=False):
    index = Path(state_dir) / '_state/round_index.tsv'
    result = run_terminals(state_dir, index, None, run_id, None, include_context=True,
                           verify_history=require_complete)
    if require_complete and result[3]:
        _, _, member, rb, state, reason = result[3][0]
        raise Refusal('run_incomplete:' + member + ':' + rb + ':' + state + ':' + reason)
    return result[2]


def run_repair_target(state_dir, index, current, run_id, barcode, history=None):
    terminals, pending = run_completion(state_dir, index, current, run_id, barcode, history)
    if pending:
        return pending[0][1:4]
    if barcode not in terminals:
        raise Refusal('missing_retained_barcode_report')
    return run_id, barcode, terminals[barcode]


class History:
    def __init__(self, state_dir, current, history=None, index=None, run_id=None, barcode=None):
        self.state_dir = Path(state_dir).absolute()
        self.current = current
        self.path = Path(history) if history else self.state_dir / '_state/report_history.jsonl'
        self.index = Path(index) if index else self.state_dir / '_state/round_index.tsv'
        self.run_id, self.barcode = run_id, barcode
        self.reports_opened = 0
        self.cache = {}
        self.report_digests = {}
        self.records, self.malformed = {}, []
        self.full_scan = False
        self.reason = ''
        self.state = 'invalid_authority'

    def report(self, rb):
        if rb not in self.cache:
            path = self.state_dir / rb / 'round_report.json'
            regular(path)
            self.reports_opened += 1
            raw = path.read_bytes()
            self.report_digests[rb] = hashlib.sha256(raw).digest()
            obj = loads(raw)
            identity(obj, self.state_dir, rb)
            local = path.parent / 'round_index.tsv'
            if regular(local, missing=True) and local.read_bytes() != f'round_barcode\tround_index\n{rb}\t{self.mapping[rb]}\n'.encode():
                raise Refusal('local_index_mismatch:' + rb)
            # Retained production reports have one JSON line; never split Unicode separators.
            if b'\n' in raw.rstrip(b'\n'):
                raise Refusal('multiline_retained_report:' + rb)
            self.cache[rb] = (obj, raw if raw.endswith(b'\n') else raw + b'\n')
        return self.cache[rb]

    def row(self, record):
        offset, size, _, _ = record
        with self.path.open('rb') as stream:
            stream.seek(offset)
            return stream.read(size)

    def inventory(self):
        """Shared literal-LF history framing and identity inventory, without report opens."""
        self.records, self.malformed = {}, []
        order, duplicates, offset = [], False, 0
        self.source_sha = hashlib.sha256()
        if regular(self.path, missing=True):
            with self.path.open('rb') as stream:
                for line, raw in enumerate(stream, 1):
                    start = offset
                    offset += len(raw)
                    self.source_sha.update(raw)
                    if not raw.endswith(b'\n'):
                        self.malformed.append((start, len(raw), line, 'trailing_fragment_without_LF'))
                        continue
                    if not raw.strip(b' \t\r\n'):
                        continue
                    try:
                        obj = loads(raw)
                    except (ValueError, UnicodeError) as error:
                        self.malformed.append((start, len(raw), line, 'malformed_json:' + type(error).__name__))
                        continue
                    try:
                        key = identity(obj, self.state_dir)
                    except Refusal as error:
                        if str(error).startswith(('non_object', 'invalid_identity')):
                            self.malformed.append((start, len(raw), line, str(error)))
                            continue
                        raise
                    rb = key[2]
                    if rb not in self.mapping:
                        raise Refusal('unmapped_history_identity')
                    record = (start, len(raw), line, key)
                    if rb in self.records:
                        duplicates = True
                        if not equivalent(loads(self.row(self.records[rb])), obj):
                            raise Refusal('conflicting_duplicate_identity:' + rb)
                    else:
                        self.records[rb] = record
                    order.append(rb)
        self.source_digest = self.source_sha.hexdigest()
        return order, duplicates

    def classify(self, full=False):
        self.records, self.malformed = {}, []
        try:
            regular(self.state_dir, directory=True)
            self.mapping = index_mapping(self.index, self.current)
            self.prefix = sorted((rb for rb in self.mapping if self.mapping[rb] <= self.mapping[self.current]), key=self.mapping.get)
            # Missing retained authority is never inferred from derived history.
            for rb in self.prefix:
                regular(self.state_dir / rb / 'round_report.json')
            current_obj, _ = self.report(self.current)
            if self.run_id is not None and current_obj['run_id'] != self.run_id or self.barcode is not None and current_obj['barcode'] != self.barcode:
                raise Refusal('current_identity_mismatch')
            order, duplicates = self.inventory()
            future = [rb for rb in self.records if self.mapping[rb] > self.mapping[self.current]]
            if self.malformed:
                # A torn record cannot identify its round. Require every mapped retained
                # report before recovery; never guess that the missing bytes were old.
                future = [rb for rb in self.mapping if self.mapping[rb] > self.mapping[self.current]]
            expected = self.prefix + sorted(future, key=self.mapping.get)
            self.rebuild_rounds = expected
            clean_complete = not duplicates and order == expected
            clean_append = not duplicates and not future and order == self.prefix[:-1]
            self.full_scan = full or bool(self.malformed) or not (clean_complete or clean_append) or bool(future)
            check = expected if self.full_scan else list(dict.fromkeys([self.current] + (order[-1:])))
            mismatch = False
            for rb in check:
                obj, _ = self.report(rb)
                if rb in self.records:
                    row = loads(self.row(self.records[rb]))
                    if identity(row, self.state_dir) != identity(obj, self.state_dir):
                        raise Refusal('identity_mismatch:' + rb)
                    if not equivalent(row, obj):
                        if rb in future:
                            raise Refusal('unverifiable_future_row:' + rb)
                        mismatch = True
            if self.malformed:
                self.state, self.reason = 'blocked_malformed', 'malformed_records'
            elif mismatch or not (clean_complete or clean_append):
                self.state, self.reason = 'normalize', 'content_or_order'
            elif clean_complete:
                self.state, self.reason = 'complete', 'current_present'
            else:
                self.state, self.reason = 'append', 'current_absent'
        except (OSError, ValueError, UnicodeError, DecimalException) as error:
            self.state, self.reason = 'invalid_authority', str(error)
        return self.state

    def validated_rows(self):
        """Full authority validation before any mutation; keep equal rows byte-exact."""
        if self.classify(full=True) == 'invalid_authority':
            raise Refusal(self.reason)
        for rb in self.rebuild_rounds:
            obj, raw = self.report(rb)
            if rb in self.records:
                old = self.row(self.records[rb])
                if equivalent(loads(old), obj):
                    raw = old
            yield raw


def atomic_bytes(path, data):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.tmp.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def lock_identity(state_dir, fd, owner=None):
    if fd is None:
        raise Refusal('inherited_history_lock_required')
    fd = int(fd)
    # Bash command substitutions retain the holder's $$ but add a subshell PID.
    if owner is None and os.environ.get('RTB_HISTORY_LOCK_OWNER'):
        owner = int(os.environ['RTB_HISTORY_LOCK_OWNER'])
    path = Path(state_dir) / '_state/.report_history.lock.flock'
    regular(path)
    st, target = os.fstat(fd), path.stat()
    if (st.st_dev, st.st_ino) != (target.st_dev, target.st_ino):
        raise Refusal('history_lock_inode_mismatch')
    # Query on the inherited open file description, never unlock it.
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    raw = os.pread(fd, 4096, 0)
    match = re.fullmatch(rb'v=2 host=\S+ pid=([0-9]+) ppid=[0-9]+ started=[0-9]+ token=([0-9a-f]+) label=\.report_history.lock boot=\S+\n', raw)
    if not match or int(match[1]) not in ((owner,) if owner is not None else (os.getpid(), os.getppid())):
        raise Refusal('history_lock_owner_mismatch')
    return {'fd': fd, 'identity': [st.st_dev, st.st_ino], 'record': raw.decode()}


def quarantine(history):
    if not history.malformed:
        return
    q = history.path.parent / 'report_history.quarantine.jsonl'
    audit = history.path.parent / 'report_history.quarantine.log'
    for path in (q, audit):
        regular(path, missing=True)
    chunks = [history.row((*r[:3], None)) for r in history.malformed]
    data = b''.join(chunks)
    repair_id = hashlib.sha256(data + repr(history.malformed).encode()).hexdigest()
    old_q = q.read_bytes() if q.exists() else b''
    old_log = audit.read_bytes() if audit.exists() else b''
    records = [json.loads(row) for row in old_log.split(b'\n') if row]
    found = [r for r in records if r['repair_id'] == repair_id]
    if found:
        entry = found[0]
        offset = entry['quarantine_offset']
        if entry['sha256'] != hashlib.sha256(data).hexdigest():
            raise Refusal('quarantine_audit_conflict')
    else:
        offset = len(old_q)
        entry = dict(repair_id=repair_id, source=str(history.path), source_sha256=history.source_digest, reason='malformed_history',
                     current_round=history.current, state=history.state_dir.name,
                     quarantine_offset=offset, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                     records=[dict(byte_offset=r[0], size=r[1], line=r[2], reason=r[3]) for r in history.malformed])
        # Intent first: an interruption before archive publication is retryable without duplication.
        atomic_bytes(audit, old_log + json.dumps(entry, sort_keys=True).encode() + b'\n')
    if len(old_q) == offset:
        atomic_bytes(q, old_q + data)
    elif old_q[offset:offset+len(data)] != data:
        raise Refusal('quarantine_archive_conflict')
    print('WARN: REPORT_HISTORY_QUARANTINED repair=' + repair_id + ' file=' + str(q), file=sys.stderr)


def reconcile(state_dir, current, history=None, index=None, run_id=None, barcode=None, lock_fd=None, owner=None, outdir=None, full=False):
    lock_identity(state_dir, lock_fd, owner)
    view = History(state_dir, current, history, index, run_id, barcode)
    state = view.classify(full=full)
    if state == 'invalid_authority':
        raise Refusal(view.reason)
    if state == 'complete':
        return view
    if state == 'append':
        # Existing atomic whole-file publication model; no new in-place append protocol.
        old = view.path.read_bytes() if view.path.exists() else b''
        data = old + view.report(current)[1]
    else:
        # A new view avoids reusing inventory while validating a complete repair plan.
        view = History(state_dir, current, history, index, run_id, barcode)
        data = b''.join(view.validated_rows())
    if view.path.exists() and hashlib.sha256(view.path.read_bytes()).hexdigest() != view.source_digest:
        raise Refusal('history_changed_before_publication')
    mark_pending(view, outdir)
    quarantine(view)
    atomic_bytes(view.path, data)
    return view


# Reporting markers and revision tokens are derived/advisory, never classification inputs.
def output_root(state, outdir=None):
    if outdir:
        return Path(outdir).absolute()
    state = Path(state)
    if state.parent.name == 'state' and state.parent.parent.name == 'ongoing' and state.parent.parent.parent.name == 'temp':
        return state.parents[3]
    return None


def mark_pending(view, outdir=None, reason='reconciliation'):
    out = output_root(view.state_dir, outdir)
    if out is None:
        return
    runs = {view.report(view.current)[0]['run_id']}
    for run in runs:
        if run in ('.', '..'):
            raise Refusal('unsafe_run_id')
        directory = out / 'report_html/runs' / run
        directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(directory / '.report_history_pending',
                     f'state={view.state_dir.name} run={run} round={view.current} reason={reason}\n'.encode())


def revision(view, context=None):
    if context is None:
        _, _, context = run_terminals(view.state_dir, view.index, view.current,
                                      view.run_id, view.barcode, include_context=True)
    digest = hashlib.sha256()
    digest.update(b'RTB_STAGE_B_REPORT_REVISION_V2\0')
    digest_frame(digest, bytes.fromhex(context['authority_revision']))
    for path in (view.path, view.index):
        regular(path)
        content = hashlib.sha256()
        size = 0
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(block)
                content.update(block)
        digest_frame(digest, size)
        digest_frame(digest, content.digest())
    return digest.hexdigest()


def bounded(command, timeout, env=None, capture=False):
    import signal
    import subprocess
    output = tempfile.TemporaryFile() if capture else None
    child = subprocess.Popen(command, env=env, start_new_session=True, stdout=output)
    try:
        rc = child.wait(timeout=max(0.1, timeout))
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        if output is not None:
            output.close()
        raise Refusal('publication_deadline')
    if rc:
        if output is not None:
            output.close()
        raise Refusal('publication_exit:' + str(rc))
    if output is not None:
        output.seek(0)
        data = output.read().decode()
        output.close()
        return data


# This is the existing render-lock protocol, including its legacy stale helper.
# History descriptors are never passed into artifact publication subprocesses.
ARTIFACT_LOCK = r'''
set -euo pipefail
bindir=$1; target=$2; wait_limit=$3; shift 3
source "$bindir/lib/stale_lock_utils.sh"
lock_dir="${target}.lockdir"
host=$(hostname 2>/dev/null || uname -n)
cleanup_stale() { rm -f "$lock_dir/meta.env" && rmdir "$lock_dir"; }
waited=0
while ! mkdir "$lock_dir" 2>/dev/null; do
    status=0
    stale_lock_maybe_reclaim "$lock_dir" "$lock_dir/meta.env" "$host" 21600 'report lock' cleanup_stale 0 || status=$?
    [ "$status" -ne 2 ] && [ "$status" -ne 11 ] || exit 73
    [ "$status" -ne 10 ] || continue
    [ "$waited" -lt "$wait_limit" ] || exit 73
    sleep 1; waited=$((waited + 1))
done
trap 'rm -f "$lock_dir/meta.env"; rmdir "$lock_dir" 2>/dev/null || true' EXIT
printf 'pid=%s\nhost=%s\nstarted_epoch=%s\n' "$$" "$host" "$(date +%s)" > "$lock_dir/meta.env"
"$@"
'''


def artifact_command(target, command, wait=0, timeout=120):
    bindir = Path(__file__).resolve().parent
    bounded(['/bin/bash', '-c', ARTIFACT_LOCK, 'report-artifact', str(bindir), str(target), str(wait), *map(str, command)], timeout)


def capture_run_context(command, state_dir, current, destination):
    """Freeze only the existing run-JSON inputs; this is transient publication input."""
    import shutil
    state = Path(state_dir) / '_state'
    root = Path(str(destination) + '.context')
    shadow = root / '_state'
    shadow.mkdir(parents=True)
    barcode = command[command.index('--barcode') + 1]
    run_id = command[command.index('--run-id') + 1]
    try:
        authority = context_for_run(state_dir, run_id, require_complete=True)
    except Refusal:
        # A private RF-PIN witness may be produced while another barcode is
        # pending. It must not claim an authoritative public roster.
        authority = None
    status_barcode = authority['barcode'] if authority else barcode
    names = {'report_history.jsonl', 'round_index.tsv', 'done_pod5.txt', 'run_started_utc.txt'}
    for member in {barcode, status_barcode}:
        names.update(member + '_' + suffix for suffix in
                     ('read_info_rpt.txt', 'on_target_rpt.txt', 'demux_annotation_cache.tsv', 'blast_unassigned_current.list'))
    names.update(p.name for p in state.iterdir() if '_blast_otu_' in p.name or p.name.startswith('.r4d-publish-'))
    for name in sorted(names):
        source = state / name
        if regular(source, missing=True):
            shutil.copyfile(source, shadow / name)
    for rb in index_mapping(state / 'round_index.tsv', current):
        directory = root / rb
        directory.mkdir()
        for member in {barcode, status_barcode}:
            for suffix in ('blast_otu_pretax_rpt.txt', 'blast_otu_noadapter_rpt.txt'):
                source = state.parent / rb / (member + '_' + suffix)
                if source.parent.exists() and regular(source, missing=True):
                    shutil.copyfile(source, directory / source.name)
    captured = list(command)
    captured[captured.index('--history') + 1] = str(shadow / 'report_history.jsonl')
    if authority:
        context_path = str(destination) + '.authority.json'
        authority['report_revision'] = revision(History(state_dir, current, run_id=run_id, barcode=barcode), authority)
        atomic_bytes(context_path, json.dumps(authority, ensure_ascii=False,
                     separators=(',', ':')).encode() + b'\n')
        captured.extend(['--authority-context', context_path])
    if '--run-started-utc-file' in captured:
        captured[captured.index('--run-started-utc-file') + 1] = str(shadow / 'run_started_utc.txt')
    atomic_bytes(str(destination) + '.command.json', json.dumps(captured).encode() + b'\n')
    return captured


def acquire_history_fd(state_dir, wait=0):
    """Use the landed fd_lock.pl API; policy, validation and fences remain there."""
    import subprocess
    target = str(Path(state_dir) / '_state/.report_history.lock')
    helper = str(Path(__file__).parent / 'lib/fd_lock.pl')
    root_id = subprocess.check_output(['perl', helper, 'preflight', target], text=True).strip()
    barrier = os.open(str(Path(state_dir) / '_state/.rtbioscan_state_reset.flock'), os.O_RDONLY)
    fd = None
    try:
        subprocess.run(['perl', helper, 'barrier', str(barrier), str(wait), target, root_id],
                       pass_fds=(barrier,), check=True, timeout=wait + 10)
        fd = os.open(target + '.flock', os.O_RDWR | os.O_NOFOLLOW)
        subprocess.run(['perl', helper, 'lock', str(fd), str(barrier), str(wait), target, str(os.getpid()), root_id],
                       pass_fds=(fd, barrier), check=True, timeout=wait + 10)
        return fd, barrier
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.close(barrier)
        raise


def publish_run(source, state, outdir, run_id, current, barcode, snapshot, source_context=None, wait=0):
    expected = guard_private_source(state, current, run_id, barcode, outdir, snapshot, source, source_context)
    bindir = Path(__file__).resolve().parent
    command = [sys.executable, '-B', '-c',
               'import runpy,sys; api=runpy.run_path(sys.argv[1]); api["atomic_bytes"](sys.argv[2], open(sys.argv[3],"rb").read())',
               str(bindir / 'report_history_state.py'),
               str(Path(outdir) / 'report_html/runs' / run_id / 'run_report.json'), str(source)]
    target = Path(outdir) / 'report_html/runs' / run_id
    target.mkdir(parents=True, exist_ok=True)
    artifact_command(Path(state) / '_state/.report_render.lock', command, wait)
    return expected


def generate_run(args):
    import subprocess
    context = loads(Path(args.snapshot + '.authority.json').read_bytes())
    if (not valid_digest(getattr(args, 'guard_revision', None)) or
            args.guard_revision != context.get('report_revision')):
        raise Refusal('missing_or_mismatched_publication_guard')
    bindir = Path(__file__).resolve().parent
    context_path = args.snapshot + '.authority.json'
    if not Path(context_path).exists():
        # Interrupted transient snapshot publication is recoverable from retained authority.
        atomic_bytes(context_path, json.dumps(context_for_run(args.state_dir, args.run_id, require_complete=True),
                     ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
    directory = Path(args.outdir) / 'report_html/runs' / args.run_id
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.run_report.json.tmp.', dir=directory)
    os.close(fd)
    try:
        command = ['perl', str(bindir / 'report_run_json.pl'), '--history', args.snapshot, '--out', temp,
                   '--run-id', args.run_id, '--barcode', args.barcode, '--state-id', Path(args.state_dir).name,
                   '--authority-context', context_path,
                   '--outdir', args.outdir, '--schema-version', '2.0', '--report-rel-path', f'runs/{args.run_id}/report.html',
                   '--run-started-utc-file', str(Path(args.state_dir) / '_state/run_started_utc.txt')]
        subprocess.run(command, check=True, timeout=60)
        data = Path(temp).read_bytes()
        obj = loads(data)
        if obj.get('run_id') != args.run_id or obj.get('state_id') != Path(args.state_dir).name:
            raise Refusal('run_identity_mismatch')
        os.replace(temp, directory / 'run_report.json')
        # Expected generated bytes are transient evidence for the post-publication check.
        atomic_bytes(Path(args.snapshot + '.run.json'), data)
        subprocess.run(['/bin/bash', str(bindir / 'report_run_index_update.sh'), str(directory / 'run_report.json'),
                        str(Path(args.outdir) / 'report_html/runs_index.jsonl'), str(Path(args.outdir) / '.runs_index.lock')],
                       check=True, timeout=60, env={**os.environ, 'LOCK_WAIT': str(args.lock_wait)})
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def derived_current(args):
    import subprocess
    directory = Path(args.outdir) / 'report_html/runs' / args.run_id
    expected = loads(Path(args.snapshot + '.run.json').read_bytes())
    volatile = {'last_updated_utc', 'status_age_seconds', 'status_label', 'status_color'}
    stable = lambda obj: {k: v for k, v in obj.items() if k not in volatile}
    if not equivalent(stable(loads((directory / 'run_report.json').read_bytes())), stable(expected)):
        return False
    matches = []
    with (Path(args.outdir) / 'report_html/runs_index.jsonl').open('rb') as stream:
        for raw in stream:
            if not raw.strip(b' \t\r\n'):
                continue
            obj = loads(raw)
            if obj.get('run_id') == args.run_id:
                matches.append(obj)
    if len(matches) != 1 or not equivalent(stable(matches[0]), stable(expected)):
        return False
    if args.html:
        checks = [('report.html', 'report_state.json', 'sample')]
        if args.identity_mode == 'track':
            checks += [('report_replicates.html', 'report_replicates_state.json', 'replicate'),
                       ('report_replicates_primers.html', 'report_replicates_primers_state.json', 'track_detail')]
        for html, state, view in checks:
            if subprocess.run([sys.executable, '-B', str(Path(__file__).parent / 'report_publication_check.py'),
                               '--report-html', str(directory / html), '--report-state', str(directory / state),
                               '--history', args.snapshot, '--run-id', args.run_id, '--expected-report-view', view,
                               '--round-index-file', str(Path(args.state_dir) / '_state/round_index.tsv'),
                               '--current-round-barcode', args.current_round_barcode],
                              timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                return False
        # Root HTML also carries the existing history revision and run-index payload.
        root = Path(args.outdir) / 'report_html'
        text = (root / 'report.html').read_text(encoding='utf-8')
        embedded = {}
        for label in ('REPORT_META', 'REPORT_PAYLOAD'):
            match = re.search(r'window\.' + label + r' =\s*(\{.*?\});', text, re.S)
            if not match:
                return False
            embedded[label] = loads(match[1])
        digest = hashlib.sha256(Path(args.snapshot).read_bytes()).hexdigest()
        if embedded['REPORT_META'].get('report_revision') != digest or loads((root / 'report_state.json').read_bytes()).get('report_revision') != digest:
            return False
        payload = embedded['REPORT_PAYLOAD']
        entries = [r for r in payload.get('run_index', []) if r.get('run_id') == args.run_id]
        if payload.get('view_scope') != 'index' or len(entries) != 1 or not equivalent(stable(entries[0]), stable(expected)):
            return False
    return True


def snapshot(args):
    view = reconcile(args.state_dir, args.current_round_barcode, args.history, args.round_index_file,
                     args.run_id, args.barcode, args.lock_fd, outdir=args.outdir, full=args.full)
    if view.state != 'complete':
        view = History(args.state_dir, args.current_round_barcode, args.history, args.round_index_file,
                       args.run_id, args.barcode)
        if view.classify(full=args.full) != 'complete':
            raise Refusal(view.reason)
    mark_pending(view, args.outdir)
    _, pending, context = run_completion(view.state_dir, view.index, view.current, args.run_id, args.barcode,
                                         view.path, include_context=True)
    if pending:
        _, _, member, rb, state, reason = pending[0]
        raise Refusal('run_incomplete:' + member + ':' + rb + ':' + state + ':' + reason)
    token = revision(view, context)
    context['report_revision'] = token
    atomic_bytes(args.snapshot, view.path.read_bytes())
    atomic_bytes(args.snapshot + '.authority.json', json.dumps(context,
                 ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
    return token


def current_publication_context(state_dir, current, run_id, barcode):
    """One fresh authority traversal under H; no downstream lock is held."""
    fd, barrier = acquire_history_fd(state_dir, 0)
    try:
        view = History(state_dir, current, run_id=run_id, barcode=barcode)
        _, pending, context = run_completion(state_dir, view.index, current, run_id, barcode,
                                              view.path, include_context=True)
        if pending:
            raise Refusal('source_history_not_complete:' + pending[0][5])
        context['report_revision'] = revision(view, context)
        return context
    finally:
        os.close(fd)
        os.close(barrier)


def same_publication_context(left, right):
    fields = ('run_id', 'barcodes', 'barcode', 'last_round_barcode', 'rounds_count',
              'round_order_sha256', 'authority_revision', 'report_revision')
    return (isinstance(left, dict) and isinstance(right, dict) and
            valid_digest(left.get('authority_revision')) and valid_digest(left.get('report_revision')) and
            all(left.get(field) == right.get(field) for field in fields))


def _guard_private_source(state_dir, current, run_id, barcode, snapshot, source, source_context=None):
    """Compare private source, optional snapshot, and one fresh locked authority view."""
    private = private_context(source, source_context)
    if not isinstance(private, dict):
        raise Refusal('source_run_authority_mismatch')
    record = loads(Path(source).read_bytes())
    if not isinstance(record, dict):
        raise Refusal('source_run_authority_mismatch')
    actual = current_publication_context(state_dir, current, run_id, barcode)
    if snapshot is not None:
        expected = loads(Path(str(snapshot) + '.authority.json').read_bytes())
        if not same_publication_context(expected, actual):
            raise Refusal('source_run_authority_changed')
    else:
        expected = actual
    if not same_publication_context(private, expected):
        raise Refusal('source_run_authority_mismatch')
    if any(record.get(field) != expected[field] for field in
           ('run_id', 'barcodes', 'barcode', 'last_round_barcode', 'rounds_count')):
        raise Refusal('source_run_authority_mismatch')
    return expected


def guard_private_source(state_dir, current, run_id, barcode, outdir, snapshot, source, source_context=None):
    try:
        return _guard_private_source(state_dir, current, run_id, barcode, snapshot, source, source_context)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        from types import SimpleNamespace
        pending_failure(SimpleNamespace(state_dir=str(state_dir), outdir=str(outdir), run_id=run_id),
                        'source_guard:' + str(error))
        raise


def guard_snapshot(state_dir, current, run_id, barcode, outdir, snapshot):
    try:
        expected = loads(Path(str(snapshot) + '.authority.json').read_bytes())
        actual = current_publication_context(state_dir, current, run_id, barcode)
        if not same_publication_context(expected, actual):
            raise Refusal('source_run_authority_changed')
        return expected
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        from types import SimpleNamespace
        pending_failure(SimpleNamespace(state_dir=str(state_dir), outdir=str(outdir), run_id=run_id),
                        'source_guard:' + str(error))
        raise


def publish_private_source(state_dir, current, run_id, barcode, outdir, snapshot, source, command):
    """Guard outside the artifact lock, then use the existing publisher lock."""
    expected = guard_private_source(state_dir, current, run_id, barcode, outdir, snapshot, source)
    artifact_command(Path(state_dir) / '_state/.report_render.lock',
                     [*command, '--guard-revision', expected['report_revision']], 0)


def recheck(args, completion=None):
    lock_identity(args.state_dir, args.lock_fd)
    view = History(args.state_dir, args.current_round_barcode, args.history, args.round_index_file, args.run_id, args.barcode)
    _, pending, context = completion if completion is not None else run_completion(
        view.state_dir, view.index, view.current, args.run_id, args.barcode, view.path,
        include_context=True)
    if pending:
        _, _, member, rb, state, reason = pending[0]
        pending_failure(args, 'run_incomplete:' + member + ':' + rb + ':' + state + ':' + reason)
        return False
    if revision(view, context) == args.revision and derived_current(args):
        marker = Path(args.outdir) / 'report_html/runs' / args.run_id / '.report_history_pending'
        marker.unlink(missing_ok=True)
        (marker.parent / '.report_render_pending').unlink(missing_ok=True)
        return True
    mark_pending(view, args.outdir, 'revision_or_artifact_changed')
    return False


HISTORY_COMMAND = r'''
set -euo pipefail
bindir=$1; state=$2; wait=$3; shift 3
source "$bindir/lib/lock_utils.sh"
init_lock_helpers
LOCK_WAIT=$wait acquire_lock "$state/_state/.report_history.lock" || exit 73
fd=${acquired_lock_fds[$((${#acquired_lock_fds[@]}-1))]}
export RTB_HISTORY_LOCK_OWNER=$$
"$@" --lock-fd "$fd"
'''


def pending_failure(args, reason):
    # A failed lock attempt has no authority to mutate history, but may set advisory state.
    if not args.outdir or not args.run_id or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', args.run_id):
        return
    directory = Path(args.outdir) / 'report_html/runs' / args.run_id
    try:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(directory / '.report_history_pending',
                     ('state=' + Path(args.state_dir).name + ' reason=' + reason.replace('\n', ' ') + '\n').encode())
    except OSError as error:
        print('WARN: pending marker publication failed: ' + str(error), file=sys.stderr)


def finalize(args):
    import subprocess
    import shlex
    bindir = Path(__file__).resolve().parent
    name = None
    command = [sys.executable, '-B', str(bindir / 'report_history_state.py')]
    try:
        target_run, target_barcode, target = run_repair_target(
            args.state_dir, getattr(args, 'round_index_file', None) or Path(args.state_dir) / '_state/round_index.tsv',
            args.current_round_barcode, args.run_id, args.barcode, getattr(args, 'history', None))
        args.run_id, args.barcode = target_run, target_barcode
        args.current_round_barcode = target
        common = ['--state-dir', args.state_dir, '--current-round-barcode', target,
                  '--outdir', args.outdir, '--run-id', args.run_id, '--barcode', args.barcode,
                  '--html', str(args.html), '--identity-mode', args.identity_mode,
                  '--lock-wait', str(args.lock_wait), '--url-prefix', args.url_prefix,
                  '--auto-refresh', args.auto_refresh, '--refresh-seconds', args.refresh_seconds,
                  '--sample-plot-max', args.sample_plot_max]
        fd, name = tempfile.mkstemp(prefix='.report_history.snapshot.', suffix='.jsonl', dir=Path(args.state_dir) / '_state')
        os.close(fd)
        args.snapshot = name
        common += ['--snapshot', name, '--full']
        token = bounded(['/bin/bash', '-c', HISTORY_COMMAND, 'history-snapshot', str(bindir), args.state_dir,
                         str(args.lock_wait), *command, 'snapshot', *common], args.lock_wait + 10, capture=True)
        # Artifact locks are acquired only after all history descriptors close.
        guard_snapshot(args.state_dir, target, args.run_id, args.barcode, args.outdir, name)
        artifact_command(Path(args.state_dir) / '_state/.report_render.lock',
                         [*command, 'publish-derived', *common, '--guard-revision', token.strip()], 0, 120)
        if args.html:
            rebuild = ['/bin/bash', str(bindir / 'report_rebuild.sh'), '--outdir', args.outdir,
                       '--state-id', Path(args.state_dir).name, '--history', name, '--skip-run-json',
                       '--url-prefix', args.url_prefix, '--auto-refresh', args.auto_refresh, '--refresh-seconds', args.refresh_seconds,
                       '--sample-plot-max', args.sample_plot_max]
            artifact_command(Path(args.outdir) / '.report_root_render.lock', rebuild, 0, 120)
        bounded(['/bin/bash', '-c', HISTORY_COMMAND, 'history-recheck', str(bindir), args.state_dir, '0',
                 *command, 'recheck', *common, '--revision', token.strip()], 15)
        print('REPORT_HISTORY_COMPLETE run=' + args.run_id)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        pending_failure(args, str(error))
        print('WARN: REPORT_HISTORY_PENDING reason=' + str(error), file=sys.stderr)
        repair = [*command, 'finalize', '--state-dir', args.state_dir, '--current-round-barcode', args.current_round_barcode,
                  '--outdir', args.outdir, '--run-id', args.run_id, '--barcode', args.barcode,
                  '--html', str(args.html), '--identity-mode', args.identity_mode, '--lock-wait', '0',
                  '--url-prefix', args.url_prefix, '--auto-refresh', args.auto_refresh,
                  '--refresh-seconds', args.refresh_seconds, '--sample-plot-max', args.sample_plot_max]
        print('Offline repair: ' + shlex.join(repair), file=sys.stderr)
        return 73
    finally:
        for path in ([Path(name), Path(name + '.run.json'), Path(name + '.authority.json')] if name is not None else []):
            path.unlink(missing_ok=True)


def publish_derived(args):
    import subprocess
    context = loads(Path(args.snapshot + '.authority.json').read_bytes())
    if (not valid_digest(getattr(args, 'guard_revision', None)) or
            args.guard_revision != context.get('report_revision')):
        raise Refusal('missing_or_mismatched_publication_guard')
    if args.source:
        context = loads(Path(args.snapshot + '.authority.json').read_bytes())
        private = private_context(args.source)
        data = Path(args.source).read_bytes()
        record = loads(data)
        if any(not isinstance(value, dict) for value in (context, private, record)):
            raise Refusal('source_run_authority_mismatch')
        fields = ('run_id', 'barcodes', 'barcode', 'last_round_barcode', 'rounds_count',
                  'round_order_sha256', 'authority_revision', 'report_revision')
        if (not valid_digest(context.get('report_revision')) or
                not valid_digest(context.get('authority_revision')) or
                not valid_digest(private.get('authority_revision')) or
                any(private.get(field) != context.get(field) for field in fields) or
                context.get('run_id') != args.run_id or any(record.get(field) != context.get(field) for field in
                ('run_id', 'barcodes', 'barcode', 'last_round_barcode', 'rounds_count'))):
            raise Refusal('source_run_authority_mismatch')
        directory = Path(args.outdir) / 'report_html/runs' / args.run_id
        directory.mkdir(parents=True, exist_ok=True)
        atomic_bytes(directory / 'run_report.json', data)
        atomic_bytes(args.snapshot + '.run.json', data)
        subprocess.run(['/bin/bash', str(Path(__file__).parent / 'report_run_index_update.sh'),
                        str(directory / 'run_report.json'), str(Path(args.outdir) / 'report_html/runs_index.jsonl'),
                        str(Path(args.outdir) / '.runs_index.lock')], check=True, timeout=60,
                       env={**os.environ, 'LOCK_WAIT': '0'})
    else:
        generate_run(args)
    if args.html:
        for view in (['sample', 'replicate', 'track_detail'] if args.identity_mode == 'track' else ['sample']):
            subprocess.run(['/bin/bash', str(Path(__file__).parent / 'report_rebuild.sh'), '--outdir', args.outdir,
                            '--state-id', Path(args.state_dir).name, '--history', args.snapshot, '--run-id', args.run_id,
                            '--skip-root-report', '--skip-run-json', '--group-view', view,
                            '--url-prefix', args.url_prefix, '--auto-refresh', args.auto_refresh,
                            '--refresh-seconds', args.refresh_seconds, '--sample-plot-max', args.sample_plot_max],
                           check=True, timeout=90)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, message + '\n')


def main():
    parser = Parser(description=__doc__)
    parser.add_argument('mode', choices=['classify', 'terminal', 'run-target', 'context', 'presentation-history', 'rebuild-history', 'snapshot', 'recheck', 'finalize', 'publish-derived', 'generate-run', 'publish-run', 'guard-snapshot'])
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--current-round-barcode', required=True)
    parser.add_argument('--history')
    parser.add_argument('--round-index-file')
    parser.add_argument('--run-id')
    parser.add_argument('--barcode')
    parser.add_argument('--lock-fd', type=int)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--outdir')
    parser.add_argument('--snapshot')
    parser.add_argument('--revision')
    parser.add_argument('--source')
    parser.add_argument('--source-context')
    parser.add_argument('--guard-revision')
    parser.add_argument('--lock-wait', type=int, default=0)
    parser.add_argument('--html', type=int, choices=[0, 1], default=1)
    parser.add_argument('--identity-mode', choices=['sample', 'collapse', 'track'], default='sample')
    parser.add_argument('--url-prefix', default='')
    parser.add_argument('--auto-refresh', default='1')
    parser.add_argument('--refresh-seconds', default='15')
    parser.add_argument('--sample-plot-max', default='-1')
    args = parser.parse_args()
    if args.identity_mode == 'collapse':
        args.identity_mode = 'sample'
    if args.lock_wait < 0:
        parser.error('--lock-wait must be nonnegative')
    required = {'terminal': ('run_id', 'barcode'), 'run-target': ('run_id', 'barcode'),
                'snapshot': ('outdir', 'run_id', 'barcode', 'snapshot'),
                'recheck': ('outdir', 'run_id', 'barcode', 'snapshot', 'revision'),
                'finalize': ('outdir', 'run_id', 'barcode'),
                'publish-derived': ('outdir', 'run_id', 'barcode', 'snapshot'),
                'generate-run': ('outdir', 'run_id', 'barcode', 'snapshot'),
                'guard-snapshot': ('outdir', 'run_id', 'barcode', 'snapshot'),
                'publish-run': ('outdir', 'source', 'run_id', 'barcode')}
    for field in required.get(args.mode, ()):
        if not getattr(args, field):
            parser.error('--' + field.replace('_', '-') + ' is required for ' + args.mode)
    values = [args.state_dir, args.current_round_barcode, args.history, args.round_index_file, args.run_id, args.barcode]
    try:
        if args.mode == 'context':
            if not args.run_id:
                raise Refusal('missing_run_id')
            data = json.dumps(context_for_run(args.state_dir, args.run_id, require_complete=True), ensure_ascii=False,
                              separators=(',', ':')).encode() + b'\n'
            if args.source:
                atomic_bytes(args.source, data)
            else:
                sys.stdout.buffer.write(data)
            return 0
        if args.mode == 'terminal':
            print(terminal_round(args.state_dir, args.round_index_file or Path(args.state_dir) / '_state/round_index.tsv',
                                 args.current_round_barcode, args.run_id, args.barcode))
            return 0
        if args.mode == 'run-target':
            print('\t'.join(run_repair_target(args.state_dir,
                args.round_index_file or Path(args.state_dir) / '_state/round_index.tsv',
                args.current_round_barcode, args.run_id, args.barcode, args.history)))
            return 0
        if args.mode == 'presentation-history':
            # Scientific snapshot admission may use validated presentation hints, never
            # malformed derived history. Missing report authority supplies no hints.
            view = History(*values)
            status = view.classify()
            if status == 'invalid_authority':
                print('WARN: REPORT_HISTORY_PENDING presentation_hints_unavailable=' + view.reason, file=sys.stderr)
                return 0
            if status in ('complete', 'append'):
                if view.path.exists():
                    with view.path.open('rb') as stream:
                        for row in stream:
                            if row.strip(b' \t\r\n'):
                                sys.stdout.buffer.write(row)
                if status == 'append':
                    sys.stdout.buffer.write(view.report(view.current)[1])
            else:
                for row in view.validated_rows():
                    sys.stdout.buffer.write(row)
            return 0
        if args.mode == 'finalize':
            return finalize(args)
        if args.mode == 'snapshot':
            print(snapshot(args))
            return 0
        if args.mode == 'recheck':
            return 0 if recheck(args) else 73
        if args.mode == 'publish-derived':
            publish_derived(args)
            return 0
        if args.mode == 'guard-snapshot':
            print(guard_snapshot(args.state_dir, args.current_round_barcode, args.run_id,
                                 args.barcode, args.outdir, args.snapshot)['report_revision'])
            return 0
        if args.mode == 'generate-run':
            generate_run(args)
            return 0
        if args.mode == 'publish-run':
            publish_run(args.source, args.state_dir, args.outdir, args.run_id,
                        args.current_round_barcode, args.barcode, args.snapshot,
                        args.source_context, args.lock_wait)
            return 0
        if args.mode == 'rebuild-history':
            reconcile(*values, lock_fd=args.lock_fd, outdir=args.outdir)
        view = History(*values)
        print(view.classify(full=args.full))
        print(f'reason={view.reason} reports_opened={view.reports_opened} full_scan={int(view.full_scan)}', file=sys.stderr)
        return 0 if args.mode == 'classify' or view.state == 'complete' else 73
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('ERROR: REPORT_HISTORY_PENDING reason=' + str(error), file=sys.stderr)
        return 73


if __name__ == '__main__':
    raise SystemExit(main())
