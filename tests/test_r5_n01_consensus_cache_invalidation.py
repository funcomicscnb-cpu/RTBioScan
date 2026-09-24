"""Round-level regression for the two deliberate consensus-cache invalidations."""

import os
import shutil
import subprocess
from pathlib import Path

from tests.test_consensus_recovery_integrity import _install_stub_tools
from tests.test_consensus_stable_otu_identity_contract import CEDAR, FIR, write_current_evidence


ROOT = Path(__file__).resolve().parents[1]
ASSIGNMENTS = {"OTUB_1-COI": CEDAR, "OTUB_2-COI": FIR}
SAMPLE = "no_adapter"


def _cache(directory):
    return directory / "Consensus" / ".cache" / SAMPLE


def _triplet(directory, biology):
    cache = _cache(directory)
    return {kind: cache / f"{biology.stable}.{kind}"
            for kind in ("consensus.fasta", "meta", "pool.tsv")}


def _inventory(directory, biology):
    return {kind: path.read_bytes() for kind, path in _triplet(directory, biology).items()
            if path.exists()}


def _key_files(directory, biology):
    return {path.name for path in _cache(directory).glob(f"{biology.stable}.*")}


def _pool_owners(directory, biology):
    rows = _triplet(directory, biology)["pool.tsv"].read_text().splitlines()
    return {row.split("\t")[0]: row.split("\t")[1] for row in rows}


def _pool_rows(directory, biology):
    rows = [line.split("\t") for line in _triplet(directory, biology)["pool.tsv"].read_text().splitlines()]
    assert all(len(row) == 4 for row in rows)
    return {row[0]: (row[1], int(row[2]), int(row[3])) for row in rows}


def _assert_ownership_only(directory, biology, public_display):
    paths = _triplet(directory, biology)
    assert not paths["consensus.fasta"].exists()
    assert set(_key_files(directory, biology)) == {paths["meta"].name, paths["pool.tsv"].name}
    lines = paths["meta"].read_text().splitlines()
    assert lines[0] == "0\tNA"
    entries = [line.split("\t") for line in lines[1:]]
    assert all(len(entry) == 2 for entry in entries)
    fields = dict(entries)
    assert len(fields) == len(entries)
    assert fields == {
        "stable_otu_key": biology.stable,
        "representative_id": biology.representative,
        "display_otu_key": public_display + "-no_adapter_1",
        "public_display_key": public_display,
    }
    assert "cache_sha256" not in paths["meta"].read_text()


def _merged(directory):
    return (directory / "Consensus" / SAMPLE / f"{SAMPLE}_Merged_Consensus.fasta").read_bytes()


def _emitted_record(directory, public_display):
    lines = _merged(directory).splitlines(keepends=True)
    records = [lines[index] + lines[index + 1] for index in range(0, len(lines), 2)]
    matches = [record for record in records if f"OTU={public_display}|".encode() in record]
    assert len(matches) <= 1
    return matches[0] if matches else b""


def _round(directory, *, previous=None, counts=(12, 12), min_reads=1,
           revalidate=False, drop=False, omit_b=False, hydrate=False,
           low_quality_b=False, b_upgrade=False, debug=True):
    directory.mkdir(parents=True)
    bindir = _install_stub_tools(directory, emit_consensus=True)
    if previous is not None:
        for name in ("consensus_ownership.tsv", "consolidated_consensus_ids.txt"):
            source = previous / "Consensus" / name
            if source.exists():
                target = directory / "Consensus" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if not hydrate:
            shutil.copytree(previous / "Consensus" / ".cache", directory / "Consensus" / ".cache")

    blast, fasta, scores, membership = [], [], [], {}
    for (display, biology), count in zip(ASSIGNMENTS.items(), counts):
        members = ([biology.representative] + [
            f"{biology.name}-read-{i:02d}|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1"
            for i in range(count - 1)
        ]) if count else []
        if biology is FIR and b_upgrade:
            members[1:] = [member.replace("|hac|", "|sup|") for member in members[1:]]
        membership[display] = members
        for index, member in enumerate(members):
            blast.append(f"{member}|{display}\t{display}\tMetazoa\tCOI\n")
            sequence = biology.sequence
            if biology is FIR and index:
                sequence = biology.sequence[:-4] + "".join(
                    "ACGT"[(index >> (2 * shift)) & 3] for shift in range(4)
                )
            fasta.append(f">{member}\n{sequence}\n")
            quality = 0 if low_quality_b and biology is FIR and index >= 2 else 35 if biology is FIR and b_upgrade and index == 1 else 30
            model = "sup" if biology is FIR and b_upgrade and index else "hac"
            scores.append(f"{member.split('|')[0]}\t{model}\t{quality}\n")
    (directory / "samples.txt").write_text(SAMPLE + "\n")
    (directory / "blast_report_annotated.txt").write_text("".join(blast))
    (directory / "qced_reads_hq_accumulated.fasta").write_text("".join(fasta))
    (directory / "read_qscore.tsv").write_text("".join(scores))
    frozen = directory / "otu_frozen_members.tsv"
    frozen.write_text("")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}",
               RTBIOSCAN_TARGET_TOKENS="COI|ITS2",
               RTBIOSCAN_TARGET_TAXA="Metazoa|Viridiplantae",
               CONSENSUS_LOCK_ENABLED="1", CONSENSUS_ZERO_EMIT_POLICY="warn",
               CONSENSUS_DEBUG="1" if debug else "0", CONSENSUS_ROUND_ID=directory.name)
    env.update(write_current_evidence(directory, {
        display: (biology.representative, biology.sequence)
        for display, biology in ASSIGNMENTS.items()
    }, membership))
    if previous is not None:
        env["CONSENSUS_LOCK_KEYS_PREV"] = str(previous / "Consensus" / "otu_consolidated_keys.tsv")
    if hydrate:
        env["CONSENSUS_CACHE_STATE_ROOT"] = str(previous / "Consensus" / ".cache")
    if revalidate:
        env["CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS"] = "1"
    if drop:
        env["CONSENSUS_CACHE_BELOW_MIN_POLICY"] = "drop"
    if omit_b:
        # Model an N-rich consensus rejected by the R worker while retaining
        # the real shell cache-update and missing-output branches.
        worker = directory / "n_rich_worker.sh"
        worker.write_text(
            '#!/bin/bash\n'
            f'"{bindir / "Rscript"}" "$@"\n'
            'if [ "$1" != "-e" ]; then\n'
            '  rm -f Consensus/no_adapter/OTUB_2-COI-no_adapter_1_consensus.fasta\n'
            '  awk \'/^>/{keep=($0 !~ /OTUB_2/)} keep\' '
            'Consensus/no_adapter/no_adapter_consensus.fasta > '
            'Consensus/no_adapter/no_adapter_consensus.filtered\n'
            '  mv Consensus/no_adapter/no_adapter_consensus.filtered '
            'Consensus/no_adapter/no_adapter_consensus.fasta\n'
            'fi\n'
        )
        worker.chmod(0o755)
        env["RTBIOSCAN_RSCRIPT"] = str(worker)
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "bin" / "Consensus_simple.sh"), str(ROOT / "bin"),
         "98", str(min_reads), "50", "15", "20", str(frozen), "representative", "4"],
        cwd=directory, env=env, capture_output=True, text=True,
    )
    return result


def _seed_locked_b(directory):
    cache = _cache(directory)
    (cache / "lock_state.tsv").write_text(f"{FIR.stable}\t1\t1\n")
    (directory / "Consensus" / "otu_consolidated_keys.tsv").write_text(
        f"{SAMPLE}\t{FIR.stable}\n"
    )


def _seed_revalidation_payload(directory):
    canonical = _triplet(directory, FIR)["consensus.fasta"]
    (canonical.parent / (canonical.name + ".revalidate")).write_bytes(
        b">stale-revalidation\nTTTT\n"
    )


def test_missing_recompute_output_preserves_authenticated_pool_and_replays(tmp_path):
    first, second, next_round, replay = (tmp_path / name for name in
                                         ("v1", "v2", "v3", "v3-replay"))
    result = _round(first)
    assert result.returncode == 0, result.stderr
    a_bytes, b_bytes = _inventory(first, CEDAR), _inventory(first, FIR)
    assert set(a_bytes) == set(b_bytes) == {"consensus.fasta", "meta", "pool.tsv"}
    assert len(_key_files(first, CEDAR)) == len(_key_files(first, FIR)) == 3
    assert set(_pool_owners(first, CEDAR)).isdisjoint(_pool_owners(first, FIR))
    assert all("cedar" in row for row in _pool_owners(first, CEDAR).values())
    assert all("fir" in row for row in _pool_owners(first, FIR).values())
    prior_rows = _pool_rows(first, FIR)
    a_output = _emitted_record(first, "OTUB_1-COI")
    assert prior_rows["fir-read-00"][1:] == (2, 30)
    _seed_locked_b(first)
    _seed_revalidation_payload(first)

    result = _round(second, previous=first, counts=(12, 14), revalidate=True,
                    omit_b=True, b_upgrade=True)
    assert result.returncode == 0, result.stderr
    assert _inventory(second, CEDAR) == a_bytes
    assert _emitted_record(second, "OTUB_1-COI") == a_output
    assert _emitted_record(second, "OTUB_2-COI") == b""
    assert b"OTUB_1" in _merged(second) and b"OTUB_2" not in _merged(second)
    assert "pool lacks stable ownership metadata" not in result.stderr
    assert "locked_cache_revalidation_triggered" in (
        second / "Consensus" / "consensus_debug.log").read_text()
    _assert_ownership_only(second, FIR, "OTUB_2-COI")
    current_rows = _pool_rows(second, FIR)
    assert set(prior_rows) < set(current_rows)
    assert current_rows["fir-read-00"][1:] == (3, 35)
    assert all(current_rows[key][1:] == (3, 30)
               for key in prior_rows if key not in ("fir-read-00", "fixture-nr-fir"))
    assert current_rows["fixture-nr-fir"] == prior_rows["fixture-nr-fir"]

    for target in (next_round, replay):
        result = _round(target, previous=second, counts=(12, 16), hydrate=True,
                        b_upgrade=True)
        assert result.returncode == 0, result.stderr
        assert "pool lacks stable ownership metadata" not in result.stderr
        assert _inventory(target, CEDAR) == a_bytes
        assert _emitted_record(target, "OTUB_1-COI") == a_output
        assert set(_inventory(target, FIR)) == {"consensus.fasta", "meta", "pool.tsv"}
        assert all("fir" in row for row in _pool_owners(target, FIR).values())
        assert set(current_rows) < set(_pool_rows(target, FIR))
        assert _pool_rows(target, FIR)["fir-read-00"][1:] == (3, 35)
        assert _emitted_record(target, "OTUB_2-COI")
        assert b"OTUB_1" in _merged(target) and b"OTUB_2" in _merged(target)
    assert _merged(next_round) == _merged(replay)
    assert _inventory(next_round, FIR) == _inventory(replay, FIR)


def test_locked_revalidation_cannot_emit_stale_canonical_consensus(tmp_path):
    first, second = tmp_path / "v1", tmp_path / "v2"
    result = _round(first)
    assert result.returncode == 0, result.stderr
    prior_pool = _triplet(first, FIR)["pool.tsv"].read_bytes()
    a_bytes = _inventory(first, CEDAR)
    _seed_locked_b(first)
    result = _round(second, previous=first, revalidate=True, omit_b=True)
    assert result.returncode == 0, result.stderr
    _assert_ownership_only(second, FIR, "OTUB_2-COI")
    assert _triplet(second, FIR)["pool.tsv"].read_bytes() == prior_pool
    assert _emitted_record(second, "OTUB_2-COI") == b""
    assert _inventory(second, CEDAR) == a_bytes
    assert _emitted_record(second, "OTUB_1-COI") == _emitted_record(first, "OTUB_1-COI")


def test_below_min_drop_preserves_pool_ownership_during_revalidation(tmp_path):
    first, second, next_round = (tmp_path / name for name in ("v1", "v2", "v3"))
    result = _round(first, counts=(12, 2))
    assert result.returncode == 0, result.stderr
    a_bytes = _inventory(first, CEDAR)
    assert set(_inventory(first, FIR)) == {"consensus.fasta", "meta", "pool.tsv"}
    _seed_locked_b(first)
    _seed_revalidation_payload(first)
    result = _round(second, previous=first, counts=(12, 12), min_reads=5,
                    revalidate=True, drop=True, low_quality_b=True)
    assert result.returncode == 0, result.stderr
    assert _inventory(second, CEDAR) == a_bytes
    assert _emitted_record(second, "OTUB_1-COI") == _emitted_record(first, "OTUB_1-COI")
    assert _emitted_record(second, "OTUB_2-COI") == b""
    assert b"OTUB_2" not in _merged(second)
    log = (second / "Consensus" / "consensus_debug.log").read_text()
    assert "locked_cache_revalidation_triggered" in log
    assert "cache_drop_below_min policy=drop" in log
    _assert_ownership_only(second, FIR, "OTUB_2-COI")
    preserved_rows = _pool_rows(second, FIR)
    assert set(_pool_rows(first, FIR)) <= set(preserved_rows)
    result = _round(next_round, previous=second, counts=(12, 0), hydrate=True)
    assert result.returncode == 0, result.stderr
    assert "pool lacks stable ownership metadata" not in result.stderr
    assert _inventory(next_round, CEDAR) == a_bytes
    assert _emitted_record(next_round, "OTUB_1-COI") == _emitted_record(first, "OTUB_1-COI")
    assert _emitted_record(next_round, "OTUB_2-COI") == b""
    _assert_ownership_only(next_round, FIR, "OTUB_2-COI")
    assert _pool_rows(next_round, FIR) == preserved_rows


def test_default_below_minimum_keep_preserves_cache(tmp_path):
    first, second = tmp_path / "v1", tmp_path / "v2"
    result = _round(first, counts=(12, 2))
    assert result.returncode == 0, result.stderr
    before_a, before_b = _inventory(first, CEDAR), _inventory(first, FIR)
    result = _round(second, previous=first, counts=(12, 12), min_reads=5,
                    low_quality_b=True)
    assert result.returncode == 0, result.stderr
    assert _inventory(second, CEDAR) == before_a
    assert _triplet(second, FIR)["consensus.fasta"].read_bytes() == before_b["consensus.fasta"]
    assert _triplet(second, FIR)["meta"].read_bytes() == before_b["meta"]
    assert _emitted_record(second, "OTUB_1-COI") == _emitted_record(first, "OTUB_1-COI")


def test_malformed_external_orphan_still_fails_closed(tmp_path):
    directory = tmp_path / "external"
    directory.mkdir()
    orphan = _triplet(directory, FIR)["pool.tsv"]
    orphan.parent.mkdir(parents=True)
    orphan.write_text("foreign\tforeign|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1\t2\t30\n")
    before = orphan.read_bytes()
    result = _round(directory / "run", previous=None)
    # The external artifact is independent of the round fixture and is never normalized.
    assert orphan.read_bytes() == before
    cache = _cache(directory / "run")
    (cache / orphan.name).write_bytes(before)
    for kind in ("meta", "consensus.fasta"):
        _triplet(directory / "run", FIR)[kind].unlink()
    result = _round(directory / "retry", previous=directory / "run")
    assert result.returncode != 0
    assert "pool lacks stable ownership metadata" in result.stderr
