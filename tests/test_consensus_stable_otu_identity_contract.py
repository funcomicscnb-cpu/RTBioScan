"""Executable identity contracts; fixture biology is independent of consensus bytes."""
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ROOT / "bin" / "consensus_otu_identity.pl"
CONSENSUS = ROOT / "bin" / "Consensus_simple.sh"


@dataclass(frozen=True)
class Biology:
    name: str
    marker: str
    sequence: str

    @property
    def representative(self):
        return f"fixture-nr-{self.name}|{self.marker}|hac|barcode=no_adapter_1|adapter=no_adapter_1"

    @property
    def stable(self):
        marker = self.marker.upper()
        if marker in {"ITS", "ITS1", "ITS2"}:
            marker = "ITS2"
        return marker + "|" + hashlib.md5(self.sequence.upper().encode()).hexdigest()


CEDAR = Biology("cedar", "COI", "AACCGGTTAACCGGTTAACCGGTT")
FIR = Biology("fir", "COI", "TTGGCCAATTGGCCAATTGGCCAA")
PINE = Biology("pine", "COI", "ACACACACGTGTGTGTACACACAC")

# Explicit global NR representatives for legacy positive-path test fixtures.
# They may be absent from a sample's current read pool, just as a pruned/frozen
# global representative may be absent in a real consensus sample partition.
# These declarations do not authorize any historical cache or lock artifact.
FIXTURE_BIOLOGY = {
    "OTUB_1-COI": CEDAR,
    "OTUB_2-COI": FIR,
    "OTUB_3-COI": PINE,
    "OTUB_4-COI": Biology("elm", "COI", "AGAGAGAGCTCTCTCTAGAGAGAG"),
    "OTUB_5-COI": Biology("birch", "COI", "ATATATATGCGCGCGCATATATAT"),
    "OTUB_7-COI": Biology("ash", "COI", "AAAATTTTCCCCGGGGAAAATTTT"),
    "OTUB_8-COI": Biology("oak", "COI", "CCCCTTTTAAAAGGGGCCCCTTTT"),
    "OTUB_9-COI": Biology("larch", "COI", "GGGGAAAATTTTCCCCGGGGAAAA"),
    "OTUB_12-COI": Biology("alder", "COI", "GATTACAGATTACAGATTACAGA"),
    "OTUB_123-COI": Biology("willow", "COI", "CATTAGACATTAGACATTAGACA"),
    "OTUB_55-COI": Biology("spruce", "COI", "CCGGAATTCCGGAATTCCGGAATT"),
    "OTUB_77-COI": Biology("beech", "COI", "TTAAGGCCTTAAGGCCTTAAGGCC"),
    "OTUB_2-ITS2": Biology("fir-its", "ITS2", FIR.sequence),
    "OTUB_4-ITS2": Biology("elm-its", "ITS2", "AGAGAGAGCTCTCTCTAGAGAGAG"),
    "OTUB_1-18S": Biology("cedar-18s", "18S", CEDAR.sequence),
    "OTUB_6-18S": Biology("yew", "18S", "AATTGGCCTTAACCGGAATTGGCC"),
}


def run_identity(command, **options):
    args = ["perl", str(IDENTITY), command]
    for key, value in options.items():
        args += ["--" + key.replace("_", "-"), str(value)]
    return subprocess.run(args, text=True, capture_output=True)


def write_current_evidence(directory, declarations, members=None, reverse=False):
    """Declarations explicitly bind display, representative ID, and NR sequence."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    nr = directory / "identity_representatives.fasta"
    clstr = directory / "identity_current.clstr"
    hash_map = directory / "identity_nr_hash_map.tsv"
    records = list(declarations.items())
    if reverse:
        records.reverse()
    nr.write_text("".join(f">{rep}\n{seq}\n" for _, (rep, seq) in records))
    groups = []
    for display, (rep, _sequence) in records:
        number = re.fullmatch(r"OTUB_(\d+)-[A-Za-z0-9_.-]+", display).group(1)
        ids = [(rep, True)] + [(m, False) for m in (members or {}).get(display, []) if m != rep]
        if reverse:
            ids.reverse()
        groups.append(f">Cluster {number}\n" + "".join(
            f"{i}\t0nt, >{member}... {'*' if representative else ''}\n"
            for i, (member, representative) in enumerate(ids)
        ))
    clstr.write_text("".join(groups))
    result = subprocess.run([
        "perl", str(ROOT / "bin" / "otu_hash_map_from_fasta.pl"), str(nr),
        str(hash_map), str(directory / "identity_hash_counts.tsv"),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return {"CONSENSUS_OTU_CLSTR": str(clstr), "CONSENSUS_OTU_HASH_MAP": str(hash_map)}


def add_current_fixture_evidence(directory, env, frozen_members=""):
    """Supply only current fixture truth; never inspect a cache to assign biology."""
    directory = Path(directory)
    blast = directory / "blast_report_annotated.txt"
    if not blast.exists():
        return
    targets = {v.upper() for v in env.get("RTBIOSCAN_TARGET_TOKENS", "COI|ITS2").split("|")}
    declarations, membership, uuid_to_display = {}, {}, {}
    for line in blast.read_text().splitlines():
        fields = line.split("\t")
        if not fields or fields[0] == "read_id":
            continue
        tokens = fields[0].split("|")
        labels = [t for t in tokens if t in FIXTURE_BIOLOGY]
        if not labels and len(fields) > 1 and fields[1] in FIXTURE_BIOLOGY:
            labels = [fields[1]]
        for label in labels:
            biology = FIXTURE_BIOLOGY[label]
            if biology.marker not in targets:
                continue
            declarations[label] = (biology.representative, biology.sequence)
            member = "|".join(t for t in tokens if not t.startswith("OTUB_"))
            membership.setdefault(label, []).append(member)
            uuid_to_display[tokens[0]] = label
    # A frozen representative is already explicitly designated by its flag.
    # Resolve its fixture NR sequence by exact ID, or an unambiguous UUID, using
    # the fixture read FASTA, not any consensus output or mutable row position.
    sequences, by_uuid = {}, {}
    fasta = directory / "qced_reads_hq_accumulated.fasta"
    if fasta.exists():
        header = None
        for line in fasta.read_text().splitlines():
            if line.startswith(">"):
                header = line[1:].split()[0]
                sequences[header] = ""
            elif header is not None:
                sequences[header] += line
        for header, sequence in sequences.items():
            by_uuid.setdefault(header.split("|")[0], set()).add(sequence)
    frozen = Path(frozen_members) if frozen_members else None
    if frozen is not None and frozen.is_file():
        for line in frozen.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) < 3 or fields[2] != "1":
                continue
            rep = fields[1]
            uuid = rep.split("|")[0]
            if uuid in uuid_to_display:
                sequence = sequences.get(rep)
                if sequence is None and len(by_uuid.get(uuid, ())) == 1:
                    sequence = next(iter(by_uuid[uuid]))
                assert sequence is not None, "frozen fixture representative needs an explicit NR sequence"
                label = uuid_to_display[uuid]
                declarations[label] = (rep, sequence)
                membership[label] = [m for m in membership[label] if m.split("|")[0] != uuid]
    if declarations:
        env.update(write_current_evidence(directory, declarations, membership))


def add_historical_evidence(directory, env, sample, display, biology, artifacts, representative=None):
    """Explicit artifact-bound proof, used only by adjudicated legacy fixtures."""
    directory = Path(directory)
    evidence = directory / "identity_legacy_ownership.tsv"
    rows = evidence.read_text().splitlines() if evidence.exists() else []
    for artifact in artifacts:
        artifact = Path(artifact)
        rows.append("\t".join([
            sample, display, biology.marker, representative or biology.representative,
            biology.stable.split("|")[1], biology.stable, artifact.name,
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ]))
    evidence.write_text("".join(row + "\n" for row in rows))
    env["CONSENSUS_LEGACY_IDENTITY"] = str(evidence)


def extend_current_evidence(directory, env, declarations):
    """Add explicitly declared global OTUs absent from this sample's reads."""
    directory = Path(directory)
    existing, membership = {}, {}
    if env.get("CONSENSUS_OTU_CLSTR"):
        sequences = {}
        for record in (directory / "identity_representatives.fasta").read_text().split(">")[1:]:
            header, *seq = record.splitlines()
            sequences[header] = "".join(seq)
        cluster = None
        for line in Path(env["CONSENSUS_OTU_CLSTR"]).read_text().splitlines():
            if line.startswith(">Cluster "):
                cluster = line.split()[1]
            else:
                match = re.fullmatch(r"\d+\s+\d+nt, >(.+)\.\.\.\s*(\*?)", line)
                member, flag = match.groups()
                label = f"OTUB_{cluster}-{member.split('|')[1]}"
                if flag:
                    existing[label] = (member, sequences[member])
                else:
                    membership.setdefault(label, []).append(member)
    for label, evidence in declarations.items():
        if label in existing:
            assert existing[label] == evidence, "fixture declarations conflict"
        existing[label] = evidence
    env.update(write_current_evidence(directory, existing, membership))


def prepare_proven_fixture(directory, env, sample, display, public_display,
                           representative, sequence, artifacts, frozen_members=""):
    """Opt-in historical truth for individually adjudicated fixtures only."""
    if not env.get("CONSENSUS_OTU_CLSTR"):
        add_current_fixture_evidence(directory, env, frozen_members)
    extend_current_evidence(directory, env, {public_display: (representative, sequence)})
    biology = Biology("historical", representative.split("|")[1], sequence)
    add_historical_evidence(directory, env, sample, display, biology, artifacts, representative)
    return biology.stable


def write_prior_owners(directory, rows):
    """Explicit fixture ownership; rows bind sample, biology, display and records."""
    import json
    path = Path(directory) / "Consensus" / "consensus_ownership.tsv"
    path.write_text("".join("\t".join([sample, biology.stable, display, json.dumps(entries)]) + "\n"
                            for sample, biology, display, entries in rows))


def test_map_follows_representative_flags_across_renumbering(tmp_path):
    observed = []
    for name, display, reverse in [("first", "OTUB_7-COI", False), ("second", "OTUB_2-COI", True)]:
        case = tmp_path / name
        env = write_current_evidence(case, {display: (CEDAR.representative, CEDAR.sequence)},
                                     {display: [FIR.representative]}, reverse)
        hashes = Path(env["CONSENSUS_OTU_HASH_MAP"])
        hashes.write_text(hashes.read_text() + f"{FIR.representative}\t{FIR.stable.split('|')[1]}\n")
        out = case / "map.tsv"
        result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"],
                              hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI|ITS2", out=out)
        assert result.returncode == 0, result.stderr
        fields = out.read_text().strip().split("\t")
        assert fields[0] == display
        assert fields[1] == CEDAR.stable
        assert fields[2] == CEDAR.representative
        assert fields[1] != FIR.stable
        observed.append(fields[1])
    assert observed[0] == observed[1]


@pytest.mark.parametrize("marker", ["COI", "coi", "ITS", "ITS1", "its2"])
def test_marker_normalization_uses_existing_rules(tmp_path, marker):
    biology = Biology("cedar", marker, CEDAR.sequence)
    env = write_current_evidence(tmp_path, {f"OTUB_7-{marker}": (biology.representative, biology.sequence)})
    output = tmp_path / "map.tsv"
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI|ITS2", out=output)
    assert result.returncode == 0, result.stderr
    assert output.read_text().split("\t")[1] == biology.stable


@pytest.mark.parametrize("corruption", ["no_rep", "two_reps", "no_hash", "ambiguous_hash", "no_marker"])
def test_invalid_current_identity_preserves_prior_authority(tmp_path, corruption):
    env = write_current_evidence(tmp_path, {"OTUB_7-COI": (CEDAR.representative, CEDAR.sequence)},
                                 {"OTUB_7-COI": [FIR.representative]})
    cluster = Path(env["CONSENSUS_OTU_CLSTR"])
    hashes = Path(env["CONSENSUS_OTU_HASH_MAP"])
    if corruption == "no_rep":
        cluster.write_text(cluster.read_text().replace("... *", "... "))
    elif corruption == "two_reps":
        cluster.write_text(cluster.read_text().replace("... \n", "... *\n"))
    elif corruption == "no_hash":
        hashes.write_text("")
    elif corruption == "ambiguous_hash":
        hashes.write_text(hashes.read_text() + f"{CEDAR.representative.split('|')[0]}\t{'a'*32}\n")
    else:
        cluster.write_text(cluster.read_text().replace("|COI|", "||"))
    output = tmp_path / "authority.tsv"
    output.write_bytes(b"old complete authority\n")
    result = run_identity("map", clstr=cluster, hash_map=hashes, targets="COI|ITS2", out=output)
    assert result.returncode != 0
    assert output.read_bytes() == b"old complete authority\n"


@pytest.mark.parametrize("bad", ["COI|", "|"+"a"*32, "COI|"+"a"*31, "COI|"+"a"*32+"|OTUB_1", "COI/other|"+"a"*32])
def test_malformed_stable_state_is_not_published(tmp_path, bad):
    source = tmp_path / "old.tsv"
    source.write_text(bad + "\t2\t1\n")
    output = tmp_path / "new.tsv"
    output.write_text("old authority\n")
    result = run_identity("keys", input=source, out=output)
    assert result.returncode != 0
    assert output.read_text() == "old authority\n"


def test_current_mapping_does_not_prove_numeric_lock(tmp_path):
    source = tmp_path / "lock_state.tsv"
    source.write_text("OTUB_7-COI-no_adapter_1\t9\t1\n")
    before = source.read_bytes()
    output = tmp_path / "normalized.tsv"
    result = run_identity("keys", input=source, sample="no_adapter", out=output)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b""
    assert source.read_bytes() == before
    assert "ignored unproven legacy key no_adapter/OTUB_7-COI-no_adapter_1" in result.stderr


def consensus_round(directory, assignments, previous=None, *, reads=True, reverse=False,
                    overrides=None, frozen=True, evidence=True):
    """Real wrapper, real repository identity/hash helpers, bounded tool stubs."""
    import shutil
    from tests.test_consensus_recovery_integrity import _install_stub_tools
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    bindir = _install_stub_tools(directory, emit_consensus=True)
    if previous is not None:
        for name in (".cache", "consensus_ownership.tsv", "consolidated_consensus_ids.txt"):
            source = Path(previous) / "Consensus" / name
            target = directory / "Consensus" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            elif source.exists():
                shutil.copy2(source, target)
    membership, blast, fasta, scores = {}, [], [], []
    for display, biology in assignments.items():
        members = [biology.representative] + [
            f"{biology.name}-read-{i:02d}|{biology.marker}|hac|barcode=no_adapter_1|adapter=no_adapter_1"
            for i in range(11)
        ]
        membership[display] = members
        if reads and (not isinstance(reads, set) or biology.name in reads):
            for member in members:
                sequence = biology.sequence if member == biology.representative else biology.sequence[-8:] * 2
                kingdom = "Metazoa" if biology.marker.upper() == "COI" else "Viridiplantae"
                blast.append(f"{member}|{display}\t{display}\t{kingdom}\t{biology.marker}\n")
                fasta.append(f">{member}\n{sequence}\n")
                scores.append(f"{member.split('|')[0]}\thac\t30\n")
    if reverse:
        blast.reverse(); fasta.reverse(); scores.reverse()
    (directory / "samples.txt").write_text("no_adapter\n")
    (directory / "blast_report_annotated.txt").write_text("".join(blast))
    (directory / "qced_reads_hq_accumulated.fasta").write_text("".join(fasta))
    (directory / "read_qscore.tsv").write_text("".join(scores))
    frozen_file = directory / "otu_frozen_members.tsv"
    frozen_file.write_text("".join(f"FROZEN_{b.stable.split('|')[1]}\t{b.representative}\t1\n"
                                   for b in assignments.values()) if frozen else "")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}",
               RTBIOSCAN_TARGET_TOKENS="COI|ITS2", RTBIOSCAN_TARGET_TAXA="Metazoa|Viridiplantae",
               CONSENSUS_LOCK_ENABLED="1", CONSENSUS_LOCK_MIN_STABLE_ROUNDS="2",
               CONSENSUS_LOCK_MIN_CONS_READS="10", CONSENSUS_ZERO_EMIT_POLICY="warn",
               CONSENSUS_DEBUG="1", CONSENSUS_ROUND_ID=directory.name)
    if previous is not None:
        env["CONSENSUS_LOCK_KEYS_PREV"] = str(Path(previous) / "Consensus" / "otu_consolidated_keys.tsv")
    if evidence:
        env.update(write_current_evidence(directory, {
            d: (b.representative, b.sequence) for d, b in assignments.items()
        }, membership, reverse))
    env.update(overrides or {})
    result = subprocess.run(["/bin/bash", str(CONSENSUS), str(ROOT / "bin"), "98", "1", "50", "15", "20",
                             str(frozen_file), "representative", "4"], cwd=directory, env=env,
                            capture_output=True, text=True)
    return result


def round_state(directory):
    import json
    directory = Path(directory) / "Consensus"
    cache = directory / ".cache" / "no_adapter"
    locks = {r[0]: (int(r[1]), int(r[2])) for r in
             (line.split("\t") for line in (cache / "lock_state.tsv").read_text().splitlines())}
    keys = {tuple(line.split("\t")) for line in (directory / "otu_consolidated_keys.tsv").read_text().splitlines()}
    owners = [line.split("\t") for line in (directory / "consensus_ownership.tsv").read_text().splitlines()]
    records = {(row[0], row[1]): json.loads(row[3]) for row in owners}
    assert len(records) == len(owners), "duplicate (sample, stable OTU) authority"
    return locks, keys, records


@pytest.mark.parametrize("reverse", [False, True])
def test_real_round_renumbering_preserves_cache_counter_and_one_owner(tmp_path, reverse):
    first, second, third = [tmp_path / n for n in ("round 1", "round 2", "round 3")]
    result = consensus_round(first, {"OTUB_7-COI": CEDAR}, reverse=reverse)
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(first)
    assert locks == {CEDAR.stable: (1, 1)}
    assert keys == set() and owners == {}
    prior_cache = first / "Consensus" / ".cache" / "no_adapter" / f"{CEDAR.stable}.consensus.fasta"
    cache_bytes = prior_cache.read_bytes()
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, first, reverse=not reverse)
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(second)
    assert locks == {CEDAR.stable: (2, 1)}
    assert keys == {("no_adapter", CEDAR.stable)}
    assert set(owners) == {("no_adapter", CEDAR.stable)}
    assert len(owners[("no_adapter", CEDAR.stable)]) == 1
    assert (second / "Consensus" / ".cache" / "no_adapter" / prior_cache.name).read_bytes() == cache_bytes
    merged = (second / "Consensus" / "no_adapter" / "no_adapter_Merged_Consensus.fasta").read_text()
    assert "|OTU=OTUB_2-COI|" in merged and "OTUB_7" not in merged
    result = consensus_round(third, {"OTUB_9-COI": CEDAR}, second)
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(third)
    assert locks == {CEDAR.stable: (2, 1)}
    assert len(owners[("no_adapter", CEDAR.stable)]) == 1
    assert "OTUB_9" in owners[("no_adapter", CEDAR.stable)][0][1]


@pytest.mark.parametrize("other", [FIR, Biology("cedar-its", "ITS2", CEDAR.sequence)])
def test_real_numeric_alias_and_cross_marker_do_not_inherit(tmp_path, other):
    first, second = tmp_path / "round1", tmp_path / "round2"
    result = consensus_round(first, {"OTUB_1-COI": CEDAR}, overrides={"CONSENSUS_LOCK_MIN_STABLE_ROUNDS": "1"})
    assert result.returncode == 0, result.stderr
    result = consensus_round(second, {f"OTUB_1-{other.marker}": other}, first)
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(second)
    assert locks[other.stable] == (1, 1)
    assert ("no_adapter", other.stable) not in keys
    assert ("no_adapter", other.stable) not in owners
    cache = second / "Consensus" / ".cache" / "no_adapter"
    assert (cache / f"{CEDAR.stable}.consensus.fasta").read_bytes() != (cache / f"{other.stable}.consensus.fasta").read_bytes()


def test_real_swap_follows_each_biology(tmp_path):
    first, second = tmp_path / "round1", tmp_path / "round2"
    result = consensus_round(first, {"OTUB_1-COI": CEDAR, "OTUB_2-COI": FIR})
    assert result.returncode == 0, result.stderr
    result = consensus_round(second, {"OTUB_2-COI": CEDAR, "OTUB_1-COI": FIR}, first, reverse=True)
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(second)
    assert locks == {CEDAR.stable: (2, 1), FIR.stable: (2, 1)}
    assert keys == {("no_adapter", CEDAR.stable), ("no_adapter", FIR.stable)}
    assert "OTUB_2" in owners[("no_adapter", CEDAR.stable)][0][1]
    assert "OTUB_1" in owners[("no_adapter", FIR.stable)][0][1]


class ConsensusOracle:
    """Biology-only transition model for successful, fixed-depth fixture rounds."""
    def __init__(self, threshold=2):
        self.threshold = threshold
        self.counters = {}
        self.locked = set()
        self.cache_owners = set()

    def advance(self, biology, *, reads=True, reset=()):
        reset = {b.stable for b in reset}
        self.locked -= reset
        for key in reset:
            self.counters.pop(key, None)
        for item in biology:
            key = item.stable
            if reads:
                self.cache_owners.add(key)
                if key not in self.locked:
                    self.counters[key] = self.counters.get(key, 0) + 1
                if self.counters[key] >= self.threshold:
                    self.locked.add(key)

    def assert_round(self, directory):
        locks, keys, owners = round_state(directory)
        assert locks == {key: (count, 1) for key, count in self.counters.items()}
        assert keys == {("no_adapter", key) for key in self.locked}
        assert set(owners) == keys
        assert all(len(records) == 1 for records in owners.values())
        cache = Path(directory) / "Consensus" / ".cache" / "no_adapter"
        assert {p.name.removesuffix(".consensus.fasta") for p in cache.glob("*.consensus.fasta")} == self.cache_owners


def test_oracle_cache_only_counter_continuity_and_display(tmp_path):
    oracle = ConsensusOracle()
    previous = None
    for i, (display, reads) in enumerate([("OTUB_7-COI", True), ("OTUB_2-COI", True),
                                         ("OTUB_9-COI", False), ("OTUB_1-COI", True)]):
        current = tmp_path / f"round {i}"
        result = consensus_round(current, {display: CEDAR}, previous, reads=reads)
        assert result.returncode == 0, result.stderr
        oracle.advance([CEDAR], reads=reads)
        oracle.assert_round(current)
        merged = (current / "Consensus" / "no_adapter" / "no_adapter_Merged_Consensus.fasta").read_text()
        assert f"|OTU={display}|" in merged
        previous = current


@pytest.mark.parametrize("reset", [CEDAR.stable, FIR.stable, "ITS2|" + CEDAR.stable.split("|")[1], "OTUB_7-COI-no_adapter_1"])
def test_reset_targets_only_proven_exact_stable_owner(tmp_path, reset):
    first, locked, second = [tmp_path / name for name in ("round1", "locked", "round2")]
    result = consensus_round(first, {"OTUB_7-COI": CEDAR})
    assert result.returncode == 0, result.stderr
    result = consensus_round(locked, {"OTUB_7-COI": CEDAR}, first)
    assert result.returncode == 0, result.stderr
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, locked,
                             overrides={"CONSENSUS_LOCK_RESET_KEYS": reset})
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(second)
    assert locks[CEDAR.stable] == ((1, 1) if reset == CEDAR.stable else (2, 1))
    assert (("no_adapter", CEDAR.stable) in keys) == (reset != CEDAR.stable)
    assert (("no_adapter", CEDAR.stable) in owners) == (reset != CEDAR.stable)
    if reset.startswith("OTUB_"):
        assert "ignored unproven legacy key" in result.stderr


@pytest.mark.parametrize("proof", ["exact", "none", "wrong_hash", "wrong_marker", "ambiguous", "missing_artifact"])
def test_legacy_cache_ownership_matrix(tmp_path, proof):
    env = write_current_evidence(tmp_path, {"OTUB_7-COI": (CEDAR.representative, CEDAR.sequence)})
    mapping = tmp_path / "map.tsv"
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI", out=mapping)
    assert result.returncode == 0, result.stderr
    cache = tmp_path / "cache with spaces"; cache.mkdir()
    legacy = cache / "OTUB_7-COI-no_adapter_1.consensus.fasta"
    legacy.write_text(">no_adapter|OTUB_7|COI|reads-10|OTU=OTUB_7-COI\nGATTACA\n")
    original = legacy.read_bytes()
    if proof != "none":
        biology = FIR if proof == "wrong_hash" else Biology("cedar", "ITS2", CEDAR.sequence) if proof == "wrong_marker" else CEDAR
        add_historical_evidence(tmp_path, env, "no_adapter", "OTUB_7-COI-no_adapter_1", biology, [legacy])
        if proof == "ambiguous":
            add_historical_evidence(tmp_path, env, "no_adapter", "OTUB_7-COI-no_adapter_1", FIR, [legacy])
        if proof == "missing_artifact":
            path = Path(env["CONSENSUS_LEGACY_IDENTITY"])
            path.write_text(path.read_text().replace(legacy.name, "different-artifact.fasta"))
    valid = tmp_path / "valid.tsv"
    result = run_identity("cache", map=mapping, cache=cache, sample="no_adapter", legacy=env.get("CONSENSUS_LEGACY_IDENTITY", ""), out=valid)
    assert legacy.read_bytes() == original
    if proof == "ambiguous":
        assert result.returncode != 0 and "ambiguous historical ownership" in result.stderr
        assert not (cache / f"{CEDAR.stable}.consensus.fasta").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert valid.read_text() == (f"{CEDAR.stable}\tconsensus.fasta\n" if proof == "exact" else "")
        assert (cache / f"{CEDAR.stable}.consensus.fasta").exists() == (proof == "exact")
        assert "WARN: consensus identity:" in result.stderr


@pytest.mark.parametrize("legacy_display", ["OTUB_7-COI-no_adapter_1", "OTUB_7-COI", "OTUB_7"])
def test_unproven_numeric_cache_not_emitted_counted_deleted_or_adopted(tmp_path, legacy_display):
    directory = tmp_path / "round with spaces"
    cache = directory / "Consensus" / ".cache" / "no_adapter"
    cache.mkdir(parents=True)
    source = cache / f"{legacy_display}.consensus.fasta"
    source.write_text(">no_adapter|OTUB_7|COI|reads-50|OTU=OTUB_7-COI|consolidated=1\nGGGG\n")
    original = source.read_bytes()
    result = consensus_round(directory, {"OTUB_7-COI": CEDAR}, reads=False)
    assert result.returncode == 0, result.stderr
    status = dict(line.split("\t") for line in (directory / "Consensus" / "consolidated_ids_status.tsv").read_text().splitlines())
    assert status["merged_input_headers_total"] == status["emitted_consensus_count"] == "0"
    assert round_state(directory) == ({}, set(), {})
    assert source.read_bytes() == original
    assert "ignored unproven legacy cache" in result.stderr


@pytest.mark.parametrize("overrides", [{}, {"CONSENSUS_LOCK_RESET_KEYS": "OTUB_7-COI-no_adapter_1"},
                                       {"CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS": "1"}])
def test_unproven_cache_cannot_block_current_recomputation_or_claim_pool(tmp_path, overrides):
    cache = tmp_path / "Consensus" / ".cache" / "no_adapter"; cache.mkdir(parents=True)
    legacy = cache / "OTUB_7-COI-no_adapter_1.consensus.fasta"
    legacy.write_text(">no_adapter|OTUB_7|COI|reads-999|consolidated=1\nGGGG\n")
    pool = cache / "OTUB_7-COI-no_adapter_1.pool.tsv"
    pool.write_text("poison\tpoison|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\t3\t99\n")
    before = {p: p.read_bytes() for p in (legacy, pool)}
    result = consensus_round(tmp_path, {"OTUB_7-COI": CEDAR}, overrides=overrides)
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert result.returncode == 0, result.stderr
    assert round_state(tmp_path)[0] == {CEDAR.stable: (1, 1)}
    assert "poison" not in (cache / f"{CEDAR.stable}.pool.tsv").read_text()
    assert b"GGGG\n" not in (cache / f"{CEDAR.stable}.consensus.fasta").read_bytes()


def test_missing_current_representative_evidence_declines_even_stable_cache(tmp_path):
    first, second = tmp_path / "round1", tmp_path / "round2"
    result = consensus_round(first, {"OTUB_7-COI": CEDAR})
    assert result.returncode == 0, result.stderr
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, first, evidence=False)
    assert result.returncode != 0
    assert "missing current identity" in result.stderr
    assert (second / "Consensus" / ".cache" / "no_adapter" / f"{CEDAR.stable}.consensus.fasta").read_bytes() == (
        first / "Consensus" / ".cache" / "no_adapter" / f"{CEDAR.stable}.consensus.fasta").read_bytes()


def test_active_to_frozen_promotion_preserves_one_owner(tmp_path):
    first, second, third = [tmp_path / name for name in ("active", "promoted", "frozen")]
    result = consensus_round(first, {"OTUB_7-COI": CEDAR}, frozen=False)
    assert result.returncode == 0, result.stderr
    assert round_state(first)[0] == {CEDAR.stable: (0, 0)}
    assert (first / "Consensus" / ".cache" / "no_adapter" / f"{CEDAR.stable}.consensus.fasta").exists()
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, first)
    assert result.returncode == 0, result.stderr
    assert round_state(second)[0] == {CEDAR.stable: (1, 1)}
    result = consensus_round(third, {"OTUB_9-COI": CEDAR}, second)
    assert result.returncode == 0, result.stderr
    assert round_state(third)[0] == {CEDAR.stable: (2, 1)}
    assert len(round_state(third)[2][("no_adapter", CEDAR.stable)]) == 1


@pytest.mark.parametrize("cadence", [2, 3])
def test_revalidation_follows_stable_biology_after_renumbering(tmp_path, cadence):
    first, second = tmp_path / "first", tmp_path / "second"
    options = {"CONSENSUS_LOCK_MIN_STABLE_ROUNDS": "1", "CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS": str(cadence)}
    result = consensus_round(first, {"OTUB_7-COI": CEDAR}, overrides=options)
    assert result.returncode == 0, result.stderr
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, first, overrides=options)
    assert result.returncode == 0, result.stderr
    debug = (second / "Consensus" / "consensus_debug.log").read_text()
    assert ("OTU=OTUB_2-COI-no_adapter_1 locked_cache_revalidation_triggered" in debug) == (cadence == 2)
    assert round_state(second)[1] == {("no_adapter", CEDAR.stable)}
    assert set(round_state(second)[2]) == {("no_adapter", CEDAR.stable)}


def test_proven_missing_cache_writes_only_stable_zero_default(tmp_path):
    first, second = tmp_path / "previous", tmp_path / "current"
    (first / "Consensus" / ".cache" / "no_adapter").mkdir(parents=True)
    (first / "Consensus" / "otu_consolidated_keys.tsv").write_text(f"no_adapter\t{CEDAR.stable}\n")
    result = consensus_round(second, {"OTUB_2-COI": CEDAR, "OTUB_1-COI": FIR}, first, reads={FIR.name})
    assert result.returncode == 0, result.stderr
    locks, keys, owners = round_state(second)
    assert locks == {CEDAR.stable: (0, 0), FIR.stable: (1, 1)}
    assert ("no_adapter", CEDAR.stable) not in keys | set(owners)
    assert "locked but cache missing during carry-forward" in result.stderr
    assert "OTUB_2-COI-no_adapter_1\tno_adapter\t0\tNA\t1\t0" in (
        second / "Consensus" / "no_adapter" / "otu_meta.tsv").read_text()


def test_real_stable_cache_lazy_hydration(tmp_path):
    first, second = tmp_path / "previous", tmp_path / "current"
    result = consensus_round(first, {"OTUB_7-COI": CEDAR}, overrides={"CONSENSUS_LOCK_MIN_STABLE_ROUNDS": "1"})
    assert result.returncode == 0, result.stderr
    source = first / "Consensus" / ".cache"
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, reads=False, overrides={
        "CONSENSUS_CACHE_STATE_ROOT": str(source),
        "CONSENSUS_CACHE_SYNC_SCRIPT": str(ROOT / "bin" / "sync_dir_atomic.sh"),
        "CONSENSUS_LOCK_KEYS_PREV": str(first / "Consensus" / "otu_consolidated_keys.tsv"),
    })
    assert result.returncode == 0, result.stderr
    destination = second / "Consensus" / ".cache" / "no_adapter" / f"{CEDAR.stable}.consensus.fasta"
    assert destination.read_bytes() == (source / "no_adapter" / destination.name).read_bytes()
    rows = (second / "Consensus" / "cache_hydration_stats.tsv").read_text().splitlines()
    row = dict(zip(rows[0].split("\t"), rows[1].split("\t")))
    assert row["hydrated"] == "1" and int(row["restored_files"]) > 0
    assert round_state(second)[1] == {("no_adapter", CEDAR.stable)}


def test_atomic_interrupted_identity_publication_retry_converges(tmp_path):
    import resource
    source, authority, uninterrupted = [tmp_path / name for name in ("input.tsv", "authority.tsv", "uninterrupted.tsv")]
    source.write_text("".join(f"COI|{hashlib.md5(str(i).encode()).hexdigest()}\n" for i in range(5000)))
    before = f"{CEDAR.stable}\n".encode()
    authority.write_bytes(before)
    old_stat = authority.stat()
    def interrupt_write():
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
    interrupted = subprocess.run(["perl", str(IDENTITY), "publish-keys", "--input", str(source), "--out", str(authority)],
                                 capture_output=True, preexec_fn=interrupt_write)
    assert interrupted.returncode != 0
    assert authority.read_bytes() == before
    assert authority.stat().st_mtime_ns == old_stat.st_mtime_ns
    assert authority.stat().st_ctime_ns == old_stat.st_ctime_ns
    retry = run_identity("publish-keys", input=source, out=authority)
    normal = run_identity("publish-keys", input=source, out=uninterrupted)
    assert retry.returncode == normal.returncode == 0
    assert authority.read_bytes() == uninterrupted.read_bytes() == source.read_bytes()


def test_exact_tab_key_with_display_alongside_and_prefix_adversaries(tmp_path):
    source, target = tmp_path / "source.tsv", tmp_path / "target.tsv"
    source.write_text(f"sample\t{CEDAR.stable}\tOTUB_7-COI\n")
    result = run_identity("keys", column=2, input=source, out=target)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == source.read_bytes()
    source.write_text(f"COI|{CEDAR.stable.split('|')[1][:-1]}\n")
    before = target.read_bytes()
    result = run_identity("keys", input=source, out=target)
    assert result.returncode != 0 and target.read_bytes() == before


def test_unproven_legacy_lock_never_transfers_to_current_numeric_alias(tmp_path):
    cache = tmp_path / "Consensus" / ".cache" / "no_adapter"; cache.mkdir(parents=True)
    legacy_lock = b"OTUB_7-COI-no_adapter_1\t9\t1\n"
    (cache / "lock_state.tsv").write_bytes(legacy_lock)
    previous = tmp_path / "previous_keys.tsv"
    previous.write_text("no_adapter\tOTUB_7-COI-no_adapter_1\n")
    before = previous.read_bytes()
    result = consensus_round(tmp_path, {"OTUB_7-COI": FIR}, overrides={"CONSENSUS_LOCK_KEYS_PREV": str(previous)})
    assert result.returncode == 0, result.stderr
    assert round_state(tmp_path) == ({FIR.stable: (1, 1)}, set(), {})
    assert previous.read_bytes() == before
    assert (cache / ("lock_state.tsv.legacy." + hashlib.sha256(legacy_lock).hexdigest())).read_bytes() == legacy_lock
    assert "ignored unproven legacy key no_adapter/OTUB_7-COI-no_adapter_1" in result.stderr


@pytest.mark.parametrize("corruption", ["wrong_key", "partial_identity", "pool_without_identity"])
def test_conflicting_cache_identity_fails_closed_without_replacing_authority(tmp_path, corruption):
    first, second = tmp_path / "first", tmp_path / "second"
    result = consensus_round(first, {"OTUB_7-COI": CEDAR})
    assert result.returncode == 0, result.stderr
    cache = first / "Consensus" / ".cache" / "no_adapter"
    metadata = cache / f"{CEDAR.stable}.meta"
    if corruption == "wrong_key":
        metadata.write_text(metadata.read_text().replace(CEDAR.stable, FIR.stable))
    elif corruption == "partial_identity":
        metadata.write_text(f"11\tNA\nstable_otu_key\t{CEDAR.stable}\n")
    else:
        metadata.write_text("11\tNA\n")
    before = {p.name: p.read_bytes() for p in cache.iterdir() if p.is_file()}
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, first)
    assert result.returncode != 0
    assert "consensus identity:" in result.stderr
    after = second / "Consensus" / ".cache" / "no_adapter"
    for name in (metadata.name, f"{CEDAR.stable}.consensus.fasta", "lock_state.tsv"):
        assert (after / name).read_bytes() == before[name]


def test_shared_current_fixture_never_proves_numeric_history(tmp_path):
    from tests.test_consensus_recovery_integrity import _install_stub_tools, _run_consensus
    bindir = _install_stub_tools(tmp_path, emit_consensus=True)
    read = "current|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1"
    (tmp_path / "samples.txt").write_text("no_adapter\n")
    (tmp_path / "blast_report_annotated.txt").write_text(f"{read}|OTUB_1-COI\tOTUB_1-COI\tMetazoa\tCOI\n")
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(f">{read}\nACGTACGT\n")
    (tmp_path / "read_qscore.tsv").write_text("current\thac\t30\n")
    cache = tmp_path / "Consensus" / ".cache" / "no_adapter"; cache.mkdir(parents=True)
    legacy = cache / "OTUB_1-COI-no_adapter_1.consensus.fasta"
    legacy.write_text(">no_adapter|OTUB_1|COI|reads-99|OTU=OTUB_1-COI|consolidated=1\nGGGGGGGG\n")
    before = legacy.read_bytes()
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}", CONSENSUS_DEBUG="1")
    result = _run_consensus(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert "ignored unproven legacy cache" in result.stderr
    assert legacy.read_bytes() == before
    assert "GGGGGGGG" not in (tmp_path / "Consensus" / "no_adapter" / "no_adapter_Merged_Consensus.fasta").read_text()
    assert "CONSENSUS_LEGACY_IDENTITY" not in env


@pytest.mark.parametrize("flags", [(1,), (1, 1), (1, 0), (0,), ("NA",)])
def test_sup_blocking_projects_proven_owner_to_current_biology(tmp_path, flags):
    import json
    env = write_current_evidence(tmp_path, {"OTUB_2-COI": (CEDAR.representative, CEDAR.sequence),
                                             "OTUB_1-COI": (FIR.representative, FIR.sequence)})
    mapping, owners, projected = [tmp_path / name for name in ("map.tsv", "owners.tsv", "projected.tsv")]
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI", out=mapping)
    assert result.returncode == 0, result.stderr
    entries = [[f"Consensus{i}_no_adapter", f"no_adapter|Consensus{i}|COI|OTU=OTUB_1-COI|consolidated={flag}"] for i, flag in enumerate(flags)]
    owners.write_text(f"no_adapter\t{CEDAR.stable}\tOTUB_1-COI\t{json.dumps(entries)}\n")
    result = run_identity("consolidated-display", map=mapping, prior=owners, out=projected)
    assert result.returncode == 0, result.stderr
    expected = "no_adapter\tOTUB_2-COI\n" if all(flag == 1 for flag in flags) else ""
    assert projected.read_text() == expected
    frozen, blast = tmp_path / "frozen.ids", tmp_path / "blast.tsv"
    frozen.write_text("fixture-nr-cedar\nfixture-nr-fir\n")
    blast.write_text(f"{CEDAR.representative}|OTUB_2-COI\tMetazoa\n{FIR.representative}|OTUB_1-COI\tMetazoa\n")
    result = subprocess.run(["perl", str(ROOT / "bin/build_blocked_otu.pl"), str(projected), str(frozen), str(blast)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected.replace("no_adapter\t", "no_adapter_1\t")


def test_pruning_uses_current_membership_and_stable_consolidation(tmp_path):
    env = write_current_evidence(tmp_path, {"OTUB_2-COI": (CEDAR.representative, CEDAR.sequence),
                                             "OTUB_1-COI": (FIR.representative, FIR.sequence)})
    mapping, stable_keys, display_keys, fasta, projected, keep = [tmp_path / name for name in
        ("map.tsv", "stable.tsv", "display.tsv", "accumulated.fasta", "current.fasta", "keep.ids")]
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI", out=mapping)
    assert result.returncode == 0, result.stderr
    stable_keys.write_text(f"no_adapter\t{CEDAR.stable}\n")
    # Accumulated display tags still describe the previous round, with a swap.
    fasta.write_text(f">{CEDAR.representative}|OTUB_1-COI\n{CEDAR.sequence}\n>{FIR.representative}|OTUB_2-COI\n{FIR.sequence}\n")
    result = run_identity("display-keys", map=mapping, input=stable_keys, out=display_keys)
    assert result.returncode == 0, result.stderr
    result = run_identity("prune-view", map=mapping, clstr=env["CONSENSUS_OTU_CLSTR"], input=fasta, out=projected)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(["/bin/bash", str(ROOT / "bin/otu_c1_prune_ids.sh"), "until_consolidated", str(projected), str(keep), "", str(display_keys), "", "sample_scoped_only", "1"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert {line.split("|")[0] for line in keep.read_text().splitlines()} == {FIR.representative.split("|")[0]}
    assert fasta.read_text().count("OTUB_1-COI") == 1  # public accumulated input was not rewritten


def test_zero_emission_filter_removes_stable_owner_after_display_swap(tmp_path):
    import json
    declarations = {"OTUB_9-COI": (CEDAR.representative, CEDAR.sequence), "OTUB_2-COI": (FIR.representative, FIR.sequence)}
    env = write_current_evidence(tmp_path, declarations)
    mapping, prior, source, drop, emitted, filtered, new, ids = [tmp_path / name for name in
        ("map.tsv", "owners.tsv", "ids.txt", "drop.tsv", "emitted.tsv", "filtered.txt", "new.tsv", "new_ids.txt")]
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI", out=mapping)
    assert result.returncode == 0, result.stderr
    rows = [(CEDAR, "OTUB_2-COI", "Consensus2_no_adapter"), (FIR, "OTUB_9-COI", "Consensus9_no_adapter")]
    prior.write_text("".join(f"no_adapter\t{b.stable}\t{d}\t{json.dumps([[i, 'no_adapter|'+i.removesuffix('_no_adapter')+'|COI|OTU='+d+'|consolidated=1']])}\n" for b, d, i in rows))
    source.write_text("Consensus2_no_adapter\nConsensus9_no_adapter\n")
    drop.write_text(f"no_adapter\t{CEDAR.stable}\n")
    emitted.write_text("")
    result = run_identity("filter-ids", map=mapping, prior=prior, input=source, drop=drop, out=filtered)
    assert result.returncode == 0, result.stderr
    assert filtered.read_text().splitlines() == ["Consensus9_no_adapter"]
    result = run_identity("owners", map=mapping, prior=prior, input=emitted, drop=drop, out=new, ids=ids)
    assert result.returncode == 0, result.stderr
    owners = [row.split("\t") for row in new.read_text().splitlines()]
    assert [(row[0], row[1]) for row in owners] == [("no_adapter", FIR.stable)]
    assert json.loads(owners[0][3])[0][0] == "Consensus9_no_adapter"
    assert ids.read_bytes() == b""


@pytest.mark.parametrize("temptation", ["consensus_sequence", "dummy_accumulated_sequence"])
def test_sequence_bytes_cannot_prove_historical_numeric_ownership(tmp_path, temptation):
    # Even exact representative bytes in a legacy consensus are not provenance.
    from tests.test_consensus_recovery_integrity import _install_stub_tools
    cache = tmp_path / "Consensus/.cache/no_adapter"; cache.mkdir(parents=True)
    legacy = cache / "OTUB_7-COI.consensus.fasta"
    legacy.write_text(f">no_adapter|OTUB_7|COI|reads-50|OTU=OTUB_7-COI|consolidated=1\n{CEDAR.sequence}\n")
    before = legacy.read_bytes()
    result = consensus_round(tmp_path, {"OTUB_7-COI": CEDAR}, reads=False)
    assert result.returncode == 0, result.stderr
    if temptation == "dummy_accumulated_sequence":
        (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(f">dummy|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1\n{CEDAR.sequence}\n")
        bindir = _install_stub_tools(tmp_path, emit_consensus=True)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}", RTBIOSCAN_TARGET_TOKENS="COI|ITS2", RTBIOSCAN_TARGET_TAXA="Metazoa|Viridiplantae", CONSENSUS_OTU_CLSTR=str(tmp_path / "identity_current.clstr"), CONSENSUS_OTU_HASH_MAP=str(tmp_path / "identity_nr_hash_map.tsv"))
        result = subprocess.run(["/bin/bash", str(CONSENSUS), str(ROOT / "bin"), "98", "1", "50", "15", "20", str(tmp_path / "otu_frozen_members.tsv"), "representative", "4"], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    status = dict(line.split("\t") for line in (tmp_path / "Consensus/consolidated_ids_status.tsv").read_text().splitlines())
    assert status["merged_input_headers_total"] == status["emitted_consensus_count"] == "0"
    assert legacy.read_bytes() == before
    assert not (cache / f"{CEDAR.stable}.consensus.fasta").exists()
    assert "ignored unproven legacy cache" in result.stderr


@pytest.mark.parametrize("reverse", [False, True])
def test_owner_keeps_all_records_without_changing_per_record_consolidation(tmp_path, reverse):
    import json
    env = write_current_evidence(tmp_path, {"OTUB_2-COI": (CEDAR.representative, CEDAR.sequence)})
    mapping, emitted, owners, ids, projected = [tmp_path / name for name in ("map.tsv", "emitted.tsv", "owners.tsv", "ids.txt", "blocked.tsv")]
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"], targets="COI", out=mapping)
    assert result.returncode == 0, result.stderr
    rows = [f"no_adapter\tOTUB_2-COI\tConsensus{i}_no_adapter\tno_adapter|Consensus{i}|COI|OTU=OTUB_2-COI|consolidated={flag}\t{flag}\t0\n" for i, flag in [(1, 1), (2, 0)]]
    emitted.write_text("".join(reversed(rows) if reverse else rows))
    result = run_identity("owners", map=mapping, input=emitted, out=owners, ids=ids)
    assert result.returncode == 0, result.stderr
    records = owners.read_text().splitlines()
    assert len(records) == 1
    assert records[0].split("\t")[1] == CEDAR.stable
    assert len(json.loads(records[0].split("\t")[3])) == 2
    assert ids.read_text().splitlines() == ["Consensus1_no_adapter", "no_adapter|Consensus1|COI|OTU=OTUB_2-COI|consolidated=1"]
    result = run_identity("consolidated-display", map=mapping, prior=owners, out=projected)
    assert result.returncode == 0 and projected.read_bytes() == b""


@pytest.mark.parametrize("rule", ["lock", "fraction", "top_two_gap"])
@pytest.mark.parametrize("change", ["renumber", "other_hash", "other_marker", "missing_evidence"])
def test_cluster_and_signature_decisions_follow_biology(tmp_path, rule, change):
    first, second = tmp_path / "first", tmp_path / "second"
    options = {"CONSENSUS_OTU_CONSOLIDATION_MODE": "lock" if rule == "lock" else "significant_clusters",
               "CONSENSUS_SIG_RULE": "fraction" if rule == "lock" else rule,
               "CONSENSUS_SIG_MIN_STABLE_ROUNDS": "2"}
    result = consensus_round(first, {"OTUB_7-COI": CEDAR}, overrides=options)
    assert result.returncode == 0, result.stderr
    assert round_state(first)[0] == {CEDAR.stable: (1, 1)}
    assert round_state(first)[1] == set()
    biology = FIR if change == "other_hash" else Biology("cedar-its", "ITS2", CEDAR.sequence) if change == "other_marker" else CEDAR
    display = f"OTUB_{2 if change == 'renumber' else 7}-{biology.marker}"
    if rule == "top_two_gap":
        options.update(CONSENSUS_SIG_MIN_POOL_FRACTION="0.45", CONSENSUS_SIG_MIN_TOP_FRACTION="0.75")
    result = consensus_round(second, {display: biology}, first, overrides=options, evidence=change != "missing_evidence")
    if change == "missing_evidence":
        assert result.returncode != 0 and "missing current identity" in result.stderr
        assert not (second / "Consensus/otu_consolidated_keys.tsv").exists()
    else:
        assert result.returncode == 0, result.stderr
        locks, keys, owners = round_state(second)
        assert locks[biology.stable] == ((2, 1) if change == "renumber" else (1, 1))
        assert (("no_adapter", biology.stable) in keys) == (change == "renumber")
        assert (("no_adapter", biology.stable) in owners) == (change == "renumber")
        assert f"|OTU={display}|" in (second / "Consensus/no_adapter/no_adapter_Merged_Consensus.fasta").read_text()


def test_interrupted_stable_cache_hydration_preserves_authority_and_retry_bytes(tmp_path):
    import resource
    first = tmp_path / "first"
    result = consensus_round(first, {"OTUB_7-COI": CEDAR})
    assert result.returncode == 0, result.stderr
    source = first / "Consensus/.cache/no_adapter"
    destination, normal, shims = tmp_path / "persisted cache", tmp_path / "normal cache", tmp_path / "shims"
    destination.mkdir(); shims.mkdir()
    (destination / "old_complete.meta").write_bytes(b"old complete cache authority\n")
    (shims / "rsync").write_text("#!/bin/bash\nexit 1\n"); (shims / "rsync").chmod(0o755)
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    def interrupt_copy():
        resource.setrlimit(resource.RLIMIT_FSIZE, (128, 128))
    env = dict(os.environ, PATH=f"{shims}:{os.environ.get('PATH', '')}")
    command = ["/bin/bash", str(ROOT / "bin/sync_dir_atomic.sh"), str(source)]
    result = subprocess.run(command + [str(destination)], env=env, capture_output=True, preexec_fn=interrupt_copy)
    assert result.returncode != 0
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before
    for output in (destination, normal):
        result = subprocess.run(command + [str(output)], env=env, capture_output=True)
        assert result.returncode == 0, result.stderr
    expected = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == expected
    assert {p.name: p.read_bytes() for p in normal.iterdir()} == expected


def test_real_active_frozen_merge_keeps_representative_ownership_through_promotion(tmp_path):
    # Exercise the actual merged-cluster producer, including frozen UUID hashes.
    observed = []
    for round_number, frozen_names in enumerate([set(), {CEDAR.name}, {CEDAR.name, FIR.name}]):
        directory = tmp_path / f"round {round_number}"; directory.mkdir()
        active, frozen, merged, nr, hashes, mapping = [directory / name for name in
            ("active.tsv", "frozen.tsv", "merged.clstr", "active_nr.fasta", "nr_hash_map.tsv", "identity.tsv")]
        rows = {b.name: f"0\tmember-{b.name}|COI|hac\t0\n0\t{b.representative}\t1\n" for b in (CEDAR, FIR)}
        # IDs are tier-local; promotion changes final OTUB ordering.
        active.write_text("".join(rows[b.name].replace("0\t", f"{b.name}\t") for b in (CEDAR, FIR) if b.name not in frozen_names))
        frozen.write_text("".join(rows[b.name].replace("0\t", f"FROZEN_{b.name}\t") for b in (CEDAR, FIR) if b.name in frozen_names))
        result = subprocess.run(["perl", str(ROOT / "bin/otu_merge_clstr.pl"), str(frozen), str(active), str(merged)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        nr.write_text("".join(f">{b.representative}\n{b.sequence}\n" for b in (CEDAR, FIR) if b.name not in frozen_names))
        result = subprocess.run(["perl", str(ROOT / "bin/otu_hash_map_from_fasta.pl"), str(nr), str(hashes), str(directory / "counts.tsv")], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        # Frozen metadata's canonical hash is independent oracle truth; the
        # actual main.nf producer projects its representative full ID to UUID.
        metadata = directory / "frozen_meta.tsv"
        metadata.write_text("".join(f"FROZEN_{b.name}\t{b.representative}\t{b.stable.split('|')[1]}\n" for b in (CEDAR, FIR) if b.name in frozen_names))
        main = (ROOT / "main.nf").read_text()
        start = main.index("awk -F'\\t' 'NF>=3 && length(\\$2)>0")
        end = main.index("\n", start)
        shell = main[start:end].replace("\\$", "$" )
        result = subprocess.run(["/bin/bash", "-euc", shell], env=dict(os.environ, FROZEN_META=str(metadata), NR_HASH_MAP_FILE=str(hashes)), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        result = run_identity("map", clstr=merged, hash_map=hashes, targets="COI", out=mapping)
        assert result.returncode == 0, result.stderr
        rows = [row.split("\t") for row in mapping.read_text().splitlines()]
        assert {row[1] for row in rows} == {CEDAR.stable, FIR.stable}
        assert len(rows) == 2
        assert {row[2] for row in rows} == {CEDAR.representative, FIR.representative}
        observed.append({row[1]: row[0] for row in rows})
    assert observed[0][CEDAR.stable] != observed[1][CEDAR.stable]
    assert observed[1][CEDAR.stable] != observed[2][CEDAR.stable]


REPORT_HEADER = (
    "consensus_id\totu_key\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\t"
    "taxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
    "consensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species"
)


def real_reporting(directory, headers, ids):
    """Independent public consumer: production reporting code, no parser stub."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    blast, taxonomy = directory / "blast.csv", directory / "taxonomy.tsv"
    blast.write_text("".join(f"{h},123,0,16,99\n" for h in headers))
    taxonomy.write_text("long_seq_id\ttaxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n" +
                        "".join(f"{h}\t123\tMetazoa\tP\tC\tO\tF\tG\tS\n" for h in headers))
    result = subprocess.run(["perl", str(ROOT / "bin/reporting_blast_consensus.pl"), str(blast),
                             str(taxonomy), "oracle", str(ids)], cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    general = (directory / "oracle_blast_consensus_tax_rpt.txt").read_text().splitlines()
    assert general[0] == REPORT_HEADER
    assert len(general) == len(headers) + 1
    consolidated = directory / "oracle_blast_consensus_tax_consolidated_rpt.txt"
    rows = []
    if consolidated.exists():
        lines = consolidated.read_text().splitlines()
        assert lines[0] == REPORT_HEADER
        rows = [line.split("\t") for line in lines[1:]]
    assert len({tuple(row) for row in rows}) == len(rows), "duplicate consolidated report row"
    return rows


def public_header(sample="no_adapter", display="OTUB_1-COI", marker="COI", number=0, flag=0):
    return f"{sample}|Consensus{number}|{marker}|reads-11|OTU={display}|consolidated={flag}"


def projection_case(directory, biology=CEDAR, display="OTUB_1-COI", *, sample="no_adapter",
                    flag=0, header_flag=None, prior_sample="no_adapter", prior_id=None,
                    current_id=None, prior_biology=CEDAR, corrupt=None, emitted=True, drop=False):
    """Explicit old/current owners; neither owner comes from a consensus sequence."""
    import json
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    env = write_current_evidence(directory, {display: (biology.representative, biology.sequence)})
    mapping = directory / "map.tsv"
    result = run_identity("map", clstr=env["CONSENSUS_OTU_CLSTR"], hash_map=env["CONSENSUS_OTU_HASH_MAP"],
                          targets="COI|ITS2", out=mapping)
    assert result.returncode == 0, result.stderr
    old_display = f"OTUB_1-{prior_biology.marker}"
    old_header = public_header(prior_sample, old_display, prior_biology.marker, flag=1)
    header = public_header(sample, display, biology.marker, flag=flag if header_flag is None else header_flag)
    prior_id = prior_id or f"Consensus0_{prior_sample}"
    current_id = current_id or f"Consensus0_{sample}"
    row = "\t".join([prior_sample, prior_biology.stable, old_display, json.dumps([[prior_id, old_header]])]) + "\n"
    if corrupt == "malformed": row = "no_adapter\tmalformed\tOTUB_1-COI\t[]\n"
    if corrupt == "duplicate": row += row
    if corrupt == "conflicting":
        row += "\t".join([prior_sample, FIR.stable, old_display, json.dumps([[prior_id, old_header]])]) + "\n"
    if corrupt == "missing": row = ""
    if corrupt == "marker": row = row.replace(prior_biology.stable, f"ITS2|{prior_biology.stable.split('|')[1]}")
    prior, previous, records, drops = [directory / n for n in ("prior.tsv", "previous.txt", "emitted.tsv", "drop.tsv")]
    prior.write_text(row); previous.write_text(prior_id + "\n" + old_header + "\n")
    records.write_text(f"{sample}\t{display}\t{current_id}\t{header}\t{flag}\t0\n" if emitted else "")
    drops.write_text(f"{prior_sample}\t{prior_biology.stable}\n" if drop else "")
    authority, current, projected = [directory / n for n in ("owners.next.tsv", "current.txt", "projected.txt")]
    args = dict(map=mapping, prior=prior, input=records, drop=drops, out=authority, ids=current,
                previous_ids=previous, projected_ids=projected)
    return args, header


def projection_reporting(directory, args, headers):
    """Apply the unchanged shell choice to owner-checked projections."""
    result = run_identity("owners", **args)
    assert result.returncode == 0, result.stderr
    chosen = args["ids"] if args["ids"].stat().st_size else args["projected_ids"]
    return real_reporting(Path(directory) / "reporting", headers, chosen)


@pytest.mark.parametrize("case", ["unchanged", "renumber", "hash_alias", "marker_alias", "absent_alias"])
def test_real_reporting_public_id_owner_matrix(tmp_path, case):
    first, second = tmp_path / "previous", tmp_path / "current"
    result = consensus_round(first, {"OTUB_1-COI": CEDAR}, overrides={"CONSENSUS_LOCK_MIN_STABLE_ROUNDS": "1"})
    assert result.returncode == 0, result.stderr
    biology = FIR if case in {"hash_alias", "absent_alias"} else Biology("cedar-its", "ITS2", CEDAR.sequence) if case == "marker_alias" else CEDAR
    display = f"OTUB_{9 if case in {'renumber', 'absent_alias'} else 1}-{biology.marker}"
    result = consensus_round(second, {display: biology}, first)
    assert result.returncode == 0, result.stderr
    headers = [line[1:] for line in (second / "Consensus/no_adapter/no_adapter_Merged_Consensus.fasta").read_text().splitlines() if line.startswith(">")]
    assert len(headers) == 1
    assert headers[0].split("|")[:3] == ["no_adapter", "Consensus0", biology.marker]
    expected = case in {"unchanged", "renumber"}
    ids = second / "Consensus/consolidated_consensus_ids.txt"
    rows = real_reporting(second / "reporting", headers, ids)
    assert len(rows) == int(expected)
    if expected:
        assert rows[0] == ["Consensus0_no_adapter", display, biology.marker, "consensus", "11", "no_adapter", "123", "123", "16", "99", "Metazoa", "P", "C", "O", "F", "G", "S"]
        assert set(ids.read_text().splitlines()) == {"Consensus0_no_adapter", headers[0]}
    else:
        assert "|consolidated=0" in headers[0]
        assert ids.read_bytes() == b""
    locks, keys, owners = round_state(second)
    assert (("no_adapter", biology.stable) in owners) == expected
    assert (("no_adapter", biology.stable) in keys) == expected
    assert locks[biology.stable] == ((2, 1) if expected else (1, 1))
    status = dict(line.split("\t") for line in (second / "Consensus/consolidated_ids_status.tsv").read_text().splitlines())
    assert status["reason"] == ("emitted" if expected else "emitted_no_prev")
    map_rows = (second / "Consensus/consensus_otu_map.tsv").read_text().splitlines()
    assert len(map_rows) == 2
    fields = map_rows[1].split("\t")
    assert fields[1:3] == [display, "no_adapter"]
    assert fields[-1] == str(int(expected))


@pytest.mark.parametrize("biology,display", [(CEDAR, "OTUB_1-COI"), (CEDAR, "OTUB_9-COI"),
                                             (FIR, "OTUB_1-COI"), (Biology("cedar-its", "ITS2", CEDAR.sequence), "OTUB_1-ITS2")])
def test_public_projection_uses_recorded_owner_for_unconsolidated_current(tmp_path, biology, display):
    args, header = projection_case(tmp_path, biology, display)
    rows = projection_reporting(tmp_path, args, [header])
    expected = biology == CEDAR
    assert len(rows) == int(expected)
    if expected:
        assert rows[0][:6] == ["Consensus0_no_adapter", display, biology.marker, "consensus", "11", "no_adapter"]
    else:
        assert args["ids"].read_bytes() == args["projected_ids"].read_bytes() == b""
        assert args["out"].read_bytes() == b""


@pytest.mark.parametrize("mode", ["different_sample", "same_id_different_sample", "missing", "unrecorded_id", "header_only"])
def test_public_projection_requires_sample_record_and_authority(tmp_path, mode):
    options = {}
    if mode in {"different_sample", "same_id_different_sample"}: options["prior_sample"] = "other"
    if mode == "same_id_different_sample": options["prior_id"] = "Consensus0_no_adapter"
    if mode == "missing": options["corrupt"] = "missing"
    if mode == "unrecorded_id": options["current_id"] = "Consensus7_no_adapter"
    if mode == "header_only": options.update(corrupt="missing", header_flag=1)
    args, header = projection_case(tmp_path, **options)
    if mode == "unrecorded_id":
        header = header.replace("Consensus0", "Consensus7")
        args["input"].write_text(args["input"].read_text().replace("|Consensus0|", "|Consensus7|"))
    if mode == "same_id_different_sample":
        result = run_identity("owners", **args)
        assert result.returncode != 0 and "conflicting public ownership sample or ID" in result.stderr
    else:
        rows = projection_reporting(tmp_path, args, [header])
        assert rows == []
        assert args["ids"].read_bytes() == b""


def test_real_reporting_legitimate_cache_only_owner(tmp_path):
    first, second = tmp_path / "first", tmp_path / "cache-only"
    result = consensus_round(first, {"OTUB_7-COI": CEDAR}, overrides={"CONSENSUS_LOCK_MIN_STABLE_ROUNDS": "1"})
    assert result.returncode == 0, result.stderr
    result = consensus_round(second, {"OTUB_2-COI": CEDAR}, first, reads=False)
    assert result.returncode == 0, result.stderr
    headers = [line[1:] for line in (second / "Consensus/no_adapter/no_adapter_Merged_Consensus.fasta").read_text().splitlines() if line.startswith(">")]
    rows = real_reporting(second / "reporting", headers, second / "Consensus/consolidated_consensus_ids.txt")
    assert len(rows) == 1
    assert rows[0][:6] == ["Consensus0_no_adapter", "OTUB_2-COI", "COI", "consensus", "11", "no_adapter"]
    assert set(round_state(second)[2]) == {("no_adapter", CEDAR.stable)}


@pytest.mark.parametrize("corrupt", ["malformed", "duplicate", "conflicting", "marker"])
def test_public_projection_invalid_ownership_preserves_complete_outputs(tmp_path, corrupt):
    args, header = projection_case(tmp_path, corrupt=corrupt)
    outputs = [args[key] for key in ("out", "ids", "projected_ids")]
    before = b"old complete authority\n"
    for output in outputs: output.write_bytes(before)
    result = run_identity("owners", **args)
    assert result.returncode != 0
    assert all(output.read_bytes() == before for output in outputs)
    valid, _ = projection_case(tmp_path)
    rows = projection_reporting(tmp_path, valid, [header])
    assert len(rows) == 1 and rows[0][0] == "Consensus0_no_adapter"


@pytest.mark.parametrize("drop", [False, True])
def test_real_reporting_zero_emission_owner_drop_policy(tmp_path, drop):
    args, header = projection_case(tmp_path, emitted=False, drop=drop)
    result = run_identity("owners", **args)
    assert result.returncode == 0, result.stderr
    filtered = tmp_path / "filtered.txt"
    result = run_identity("filter-ids", prior=args["prior"], input=args["projected_ids"], drop=args["drop"], out=filtered)
    assert result.returncode == 0, result.stderr
    rows = real_reporting(tmp_path / "reporting", [header.replace("consolidated=0", "consolidated=1")], filtered)
    assert len(rows) == int(not drop)
    assert bool(args["out"].read_bytes()) == (not drop)
    if not drop:
        assert filtered.read_bytes() == args["previous_ids"].read_bytes()
        assert rows[0][:6] == ["Consensus0_no_adapter", "OTUB_1-COI", "COI", "consensus", "11", "no_adapter"]


@pytest.mark.parametrize("fault", ["create", "partial", "rename", "term_before", "term_after"])
def test_public_projection_interruption_and_retry(tmp_path, fault):
    import resource
    args, header = projection_case(tmp_path, FIR)
    directory = tmp_path / "publication"; directory.mkdir()
    authority = directory / "projection.txt"; args["projected_ids"] = authority
    # Noncolliding legacy lines exercise a long projection, while the collision
    # must disappear. Keep the old authority independently complete and safe.
    suffix = "".join(f"Consensus{i}_historical\n" for i in range(1000, 4000))
    args["previous_ids"].write_text(args["previous_ids"].read_text() + suffix)
    before = b"Consensus999_old_complete\n"; authority.write_bytes(before)
    module = tmp_path / "ProjectionFault.pm"
    module.write_text(r'''package ProjectionFault;
use strict; use warnings;
BEGIN {
    require File::Temp;
    my $original = \&File::Temp::tempfile;
    no warnings 'redefine';
    *File::Temp::tempfile = sub {
        die "injected temporary creation failure\n" if $ENV{RTB_FAULT} eq 'create' && grep { defined($_) && $_ eq $ENV{RTB_PUBLISH_DIR} } @_;
        return $original->(@_);
    };
    *CORE::GLOBAL::rename = sub {
        my ($from, $to) = @_;
        return CORE::rename($from, $to) unless $to eq $ENV{RTB_AUTHORITY};
        if ($ENV{RTB_FAULT} eq 'rename') { $! = 13; return 0; }
        kill 'TERM', $$ if $ENV{RTB_FAULT} eq 'term_before';
        my $ok = CORE::rename($from, $to);
        kill 'TERM', $$ if $ENV{RTB_FAULT} eq 'term_after';
        return $ok;
    };
}
1;
''')
    command = ["perl", str(IDENTITY), "owners"]
    for key, value in args.items(): command += ["--" + key.replace("_", "-"), str(value)]
    env = dict(os.environ, PERL5OPT="-MProjectionFault", PERL5LIB=str(tmp_path), RTB_FAULT=fault,
               RTB_AUTHORITY=str(authority), RTB_PUBLISH_DIR=str(directory))
    def partial(): resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            preexec_fn=partial if fault == "partial" else None)
    assert result.returncode != 0
    assert authority.read_bytes() == (suffix.encode() if fault == "term_after" else before)
    assert real_reporting(tmp_path / "interrupted-report", [header], authority) == []
    result = run_identity("owners", **args)
    assert result.returncode == 0, result.stderr
    assert authority.read_text() == suffix
    assert real_reporting(tmp_path / "retry-report", [header], authority) == []
    assert args["ids"].read_bytes() == b""
    assert args["out"].read_bytes() == b""


@pytest.mark.parametrize("problem", ["sample", "duplicate", "conflicting"])
def test_public_projection_rejects_invalid_current_records(tmp_path, problem):
    args, _ = projection_case(tmp_path)
    row = args["input"].read_text()
    if problem == "sample":
        args["input"].write_text(row.replace("no_adapter\t", "other\t", 1))
    elif problem == "duplicate":
        args["input"].write_text(row + row)
    else:
        args["input"].write_text(row + row.replace("\t0\t0\n", "\t1\t0\n"))
    outputs = [args[key] for key in ("out", "ids", "projected_ids")]
    for path in outputs: path.write_bytes(b"old complete\n")
    result = run_identity("owners", **args)
    assert result.returncode != 0
    assert all(path.read_bytes() == b"old complete\n" for path in outputs)


@pytest.mark.parametrize("biology", [CEDAR, FIR])
def test_public_projection_uses_existing_reporting_sample_normalization(tmp_path, biology):
    args, header = projection_case(tmp_path, biology, current_id="Consensus0_no_adapter_1")
    header = header.replace("no_adapter|", "no_adapter_1|", 1)
    args["input"].write_text(args["input"].read_text().replace("no_adapter|", "no_adapter_1|", 1))
    rows = projection_reporting(tmp_path, args, [header])
    assert len(rows) == int(biology == CEDAR)
    if rows:
        assert rows[0][:6] == ["Consensus0_no_adapter", "OTUB_1-COI", "COI", "consensus", "11", "no_adapter"]
    else:
        assert args["projected_ids"].read_bytes() == b""


def test_real_reporting_owner_returns_after_public_id_alias(tmp_path):
    first, alias, returned = [tmp_path / n for n in ("original", "alias", "returned")]
    options = {"CONSENSUS_LOCK_MIN_STABLE_ROUNDS": "1"}
    result = consensus_round(first, {"OTUB_1-COI": CEDAR}, overrides=options)
    assert result.returncode == 0, result.stderr
    result = consensus_round(alias, {"OTUB_1-COI": FIR}, first)
    assert result.returncode == 0, result.stderr
    assert (alias / "Consensus/consolidated_consensus_ids.txt").read_bytes() == b""
    result = consensus_round(returned, {"OTUB_9-COI": CEDAR}, alias, reads=False, overrides=options)
    assert result.returncode == 0, result.stderr
    headers = [line[1:] for line in (returned / "Consensus/no_adapter/no_adapter_Merged_Consensus.fasta").read_text().splitlines() if line.startswith(">")]
    rows = real_reporting(returned / "reporting", headers, returned / "Consensus/consolidated_consensus_ids.txt")
    assert len(rows) == 1
    assert rows[0][:6] == ["Consensus0_no_adapter", "OTUB_9-COI", "COI", "consensus", "11", "no_adapter"]
    assert set(round_state(returned)[2]) == {("no_adapter", CEDAR.stable)}


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_public_projection_preserves_noncolliding_bytes_and_filters_reporting_whitespace(tmp_path, ending):
    args, header = projection_case(tmp_path, FIR)
    # Match the real consumer's whitespace and optional FASTA-prefix handling.
    collision = b"  Consensus0_no_adapter  \r\n\t>" + args["previous_ids"].read_bytes().splitlines()[1] + b"  \n"
    preserved = b"  Consensus999_historical  " + ending
    args["previous_ids"].write_bytes(collision + preserved)
    rows = projection_reporting(tmp_path, args, [header])
    assert rows == []
    assert args["projected_ids"].read_bytes() == preserved
    assert args["previous_ids"].read_bytes() == collision + preserved


def test_public_projection_changed_id_replaces_one_stable_owner(tmp_path):
    import json
    args, header = projection_case(tmp_path, display="OTUB_9-COI", flag=1, current_id="Consensus7_no_adapter")
    header = header.replace("Consensus0", "Consensus7")
    args["input"].write_text(args["input"].read_text().replace("|Consensus0|", "|Consensus7|"))
    rows = projection_reporting(tmp_path, args, [header])
    assert len(rows) == 1
    assert rows[0][:6] == ["Consensus7_no_adapter", "OTUB_9-COI", "COI", "consensus", "11", "no_adapter"]
    owners = [line.split("\t") for line in args["out"].read_text().splitlines()]
    assert len(owners) == 1, "one sample/stable owner despite changed display and public ID"
    assert owners[0][:3] == ["no_adapter", CEDAR.stable, "OTUB_9-COI"]
    assert json.loads(owners[0][3]) == [["Consensus7_no_adapter", header]]
    # The newly published ownership must remain valid when it becomes prior state.
    args["prior"].write_bytes(args["out"].read_bytes())
    args["previous_ids"].write_bytes(args["ids"].read_bytes())
    assert projection_reporting(tmp_path / "retry", args, [header]) == rows
