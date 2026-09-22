"""R4-D reporting contract: public OTU propagation, explicit denominators and the
current cumulative canonical-membership snapshot.

The fixtures fabricate the sealed upstream artifacts (R3 canonical membership,
the validated R4-B taxonomy sidecar and R4-A all-evidence) from one declarative
model, and an independent Python oracle derives every expected count from that
model without importing any production aggregation logic.
"""
import copy
import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"
HELPER = BIN / "r4_reporting_contract.pl"
REPORTING_BLAST_OTU = BIN / "reporting_blast_otu.pl"
REPORT_ROUND_JSON = BIN / "report_round_json.pl"
APPEND_REPORTS = BIN / "append_reports.pl"
MAIN_NF = REPO_ROOT / "main.nf"
BARCODE = "RTBioScan"
CONTEXT = "full_collapse"
ENV = {
    "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": CONTEXT,
    "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2",
    "RTBIOSCAN_TARGET_TAXA": "Metazoa|Viridiplantae",
}
USABLE = {"ASSIGNED", "AMBIGUOUS_TIE"}
STATUSES = [
    "ASSIGNED",
    "AMBIGUOUS_TIE",
    "NO_HIT",
    "FILTERED_INELIGIBLE",
    "REFERENCE_UNRESOLVED",
    "REFERENCE_INCONSISTENT",
    "COMPUTATION_FAILED",
]
LEVELS = {"family": 4, "genus": 5, "species": 6}
R4B_COLUMNS = (
    "read_id otu_id marker taxid lineage status depth origin member_count votes support "
    "winner_count stable_key read_status read_taxid read_lineage read_depth read_origin "
    "read_reason source_taxids status_counts canonical_member"
).split()
PUBLIC_HEADER = (
    "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\t"
    "otu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species"
)
NA7 = ["NA"] * 7
UNASSIGNED7 = ["Unassigned"] * 7
METAZOA = ["Metazoa", "Arthropoda", "Insecta", "Coleoptera", "Carabidae", "Galerita", "Galerita bicolor"]
PLANT = ["Viridiplantae", "Streptophyta", "Jungermanniopsida", "Porellales", "Porellaceae", "Porella", "Porella arborisvitae"]
SIG = "a" * 64


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lineage(ranks):
    return ";".join(f"{prefix}__{value}" for prefix, value in zip("Kpcofgs", ranks))


def depth(ranks):
    for index in range(6, -1, -1):
        if ranks[index] != "NA":
            return index
    return -1


def hash_of(marker, uuid, seq_tag="seq"):
    return hashlib.md5(f"{marker}:{uuid}:{seq_tag}".encode("utf-8")).hexdigest()


def sealed(kind, sig, rows):
    body = "".join(row + "\n" for row in rows)
    return f"#RTB-R4-{kind}\t1\t{sig}\n{body}#END\t{len(rows)}\t{sha256(body)}\n"


def member(uuid, read_status="ASSIGNED", taxid="12345", ranks=None, origin="DIRECT", reason="NA",
           evidence="default", model="hac", adapter="alpha", sources=None, seq_tag="seq"):
    """One canonical member. `evidence` is a list of candidate dicts, [] for a
    sealed NO_HIT query, or None when the member was never queried."""
    if ranks is None:
        ranks = METAZOA if read_status in USABLE else NA7
    if evidence == "default":
        evidence = [] if read_status == "NO_HIT" else (
            None if reason == "missing_sealed_query" else [candidate(taxid if taxid != "NA" else "12345")]
        )
    if sources is None:
        sources = sorted({c["taxid"] for c in (evidence or [])}, key=lambda v: (len(v), v)) if evidence else []
    return {
        "uuid": uuid, "model": model, "adapter": adapter, "seq_tag": seq_tag,
        "read": {"status": read_status, "taxid": taxid if read_status in USABLE else "NA",
                 "ranks": ranks if read_status in USABLE else NA7,
                 "origin": origin if read_status in USABLE else "NONE",
                 "reason": reason if read_status in USABLE or reason != "NA" else ("no_hit" if read_status == "NO_HIT" else "NA")},
        "evidence": evidence, "sources": sources,
    }


def candidate(taxid="12345", subject=None, evalue="1e-50", length="500", pident="99.5", bitscore="900"):
    subject = subject or f"ACC{taxid.replace('-', 'N')}|kraken:taxid|{taxid}"
    return {"subject": subject, "taxid": taxid, "evalue": evalue, "length": length, "pident": pident, "bitscore": bitscore}


def otu(n, marker="COI", status="ASSIGNED", taxid="12345", ranks=None, origin="DIRECT", stable=True, members=(),
        winner_count=1, eligible=True):
    if ranks is None:
        ranks = METAZOA if status in USABLE else NA7
    return {
        "n": n, "marker": marker, "status": status, "taxid": taxid if status in USABLE else "NA",
        "ranks": ranks if status in USABLE else NA7, "origin": origin if status in USABLE else "NONE",
        "stable": hash_of(marker, f"rep{n}") if stable else None, "members": list(members),
        "winner_count": winner_count if status in USABLE else 0, "eligible": eligible,
    }


def r4b_rows(model, reverse=False):
    rows = []
    for o in model:
        members = o["members"]
        read_statuses = Counter(m["read"]["status"] for m in members)
        votes = sum(1 for m in members if m["read"]["status"] in USABLE)
        winner_count = o["winner_count"] if votes else 0
        support = (votes // winner_count) if winner_count else 0
        status_counts = json.dumps(dict(read_statuses), sort_keys=True, separators=(",", ":"))
        display = f"OTUB_{o['n']}-{o['marker']}"
        for m in members:
            read = m["read"]
            rows.append({
                "read_id": f"{m['uuid']}|{o['marker']}|{m['model']}|barcode=|adapter={m['adapter']}|{display}",
                "otu_id": display, "marker": o["marker"], "taxid": o["taxid"], "lineage": lineage(o["ranks"]),
                "status": o["status"], "depth": depth(o["ranks"]), "origin": o["origin"],
                "member_count": len(members), "votes": votes, "support": support, "winner_count": winner_count,
                "stable_key": f"{o['marker']}|{o['stable']}" if o["stable"] else "NA",
                "read_status": read["status"], "read_taxid": read["taxid"], "read_lineage": lineage(read["ranks"]),
                "read_depth": depth(read["ranks"]), "read_origin": read["origin"], "read_reason": read["reason"],
                "source_taxids": ",".join(m["sources"]) if m["sources"] else "NA",
                "status_counts": status_counts, "canonical_member": f"{m['uuid']}|{o['marker']}",
            })
    if reverse:
        rows.reverse()
    return rows


def r4b_text(rows, sig=SIG):
    body = "".join("\t".join(str(row[c]) for c in R4B_COLUMNS) + "\n" for row in rows)
    return f"#RTB-R4B-TAXONOMY\t1\t{sig}\n#columns\t" + "\t".join(R4B_COLUMNS) + f"\n{body}#END\t{len(rows)}\t{sha256(body)}\n"


def evidence_rows(model, extra_observations=(), reverse=False):
    by_marker = {}
    for o in model:
        for m in o["members"]:
            if m["evidence"] is None:
                continue
            qid = f"{m['uuid']}|{o['marker']}|{m['model']}|barcode=|adapter={m['adapter']}"
            h = hash_of(o["marker"], m["uuid"], m["seq_tag"])
            by_marker.setdefault(o["marker"], []).extend(evidence_lines(qid, h, m["evidence"]))
    for obs in extra_observations:
        by_marker.setdefault(obs["marker"], []).extend(evidence_lines(obs["id"], obs["hash"], obs["evidence"]))
    if reverse:
        by_marker = {k: list(reversed(v)) for k, v in by_marker.items()}
    return by_marker


def evidence_lines(qid, h, cands):
    if not cands:
        return ["\t".join([qid, h] + ["NA"] * 11 + ["NO_HIT"])]
    status = "DEFERRED_TIE" if len(cands) > 1 else "UNIQUE"
    lines = []
    for c in sorted(cands, key=lambda c: c["subject"]):
        length = int(c["length"])
        lines.append("\t".join([qid, h, c["subject"], c["taxid"], c["evalue"], c["length"], c["pident"], c["bitscore"],
                                "1", str(length), "1", str(length), str(length + 20), status]))
    return lines


def membership_text(model):
    pairs = sorted(f"OTUB_{o['n']}-{o['marker']}\t{m['uuid']}" for o in model for m in o["members"])
    return "otu_id\tread_id\n" + "".join(p + "\n" for p in pairs)


def write_inputs(root: Path, model, extra_observations=(), reverse=False, sidecar=True, eligible=True):
    root.mkdir(parents=True, exist_ok=True)
    state = root / "_state"
    state.mkdir(exist_ok=True)
    (root / "otu_members_round.tsv").write_text(membership_text(model), encoding="utf-8")
    if sidecar:
        (root / f"{BARCODE}_blast_otu_taxonomy_v1.tsv").write_text(r4b_text(r4b_rows(model, reverse=reverse)), encoding="utf-8")
    for marker, lines in evidence_rows(model, extra_observations, reverse=reverse).items():
        (state / f"otu_blast_evidence_{marker}.tsv").write_text(sealed("EVIDENCE", SIG, lines), encoding="utf-8")
    if eligible:
        (root / "kept_otus.tsv").write_text("".join(f"CLUST_{o['n']}\n" for o in model if o["eligible"]), encoding="utf-8")
    return root


def run_build(root: Path, extra_args=(), check=True):
    cmd = [
        "perl", str(HELPER), "--build",
        "--sidecar", str(root / f"{BARCODE}_blast_otu_taxonomy_v1.tsv"),
        "--members", str(root / "otu_members_round.tsv"),
        "--evidence-dir", str(root / "_state"),
        "--context", CONTEXT,
        "--out-public", str(root / f"{BARCODE}_blast_otu_pretax_rpt.txt"),
        "--out-reporting", str(root / f"{BARCODE}_blast_otu_reporting_v1.tsv"),
    ]
    if (root / "kept_otus.tsv").exists():
        cmd += ["--eligible", str(root / "kept_otus.tsv")]
    cmd += list(extra_args)
    result = subprocess.run(cmd, cwd=root, env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def run_helper(args, cwd, check=True):
    result = subprocess.run(["perl", str(HELPER), *args], cwd=cwd, env={**os.environ, **ENV},
                            capture_output=True, text=True, check=False)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def metrics_of(root: Path):
    result = run_helper(["--metrics", "--reporting", str(root / f"{BARCODE}_blast_otu_reporting_v1.tsv"), "--full"], root)
    return json.loads(result.stdout)


def public_rows(root: Path, name=f"{BARCODE}_blast_otu_pretax_rpt.txt"):
    lines = (root / name).read_text(encoding="utf-8").splitlines()
    assert lines[0] == PUBLIC_HEADER
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


def reporting_rows(root: Path):
    lines = (root / f"{BARCODE}_blast_otu_reporting_v1.tsv").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("#RTB-R4D-REPORTING\t1\t")
    columns = lines[1].split("\t")[1:]
    return [dict(zip(columns, line.split("\t"))) for line in lines[2:-1]]


# ---------------------------------------------------------------------------
# Independent oracle: derives every expected quantity from the declarative model.
def oracle(model, extra_observations=()):
    members = [(o, m) for o in model for m in o["members"]]
    out = {
        "canonical_member_count": len(members),
        "blast_eligible_read_support": len(members) + len(extra_observations),
        "canonical_direct_hit_count": sum(1 for _, m in members if m["evidence"]),
        "canonical_direct_attribution_count": sum(1 for _, m in members if m["read"]["status"] in USABLE),
        "status_counts": {s: 0 for s in STATUSES},
        "read_status_counts": {s: 0 for s in STATUSES},
        "otu_count": len({(o["n"], o["marker"]) for o in model}),
        "stable_otu_count": len({o["stable"] for o in model if o["stable"]}),
        "canonical_assigned_count": {}, "canonical_unassigned_count": {}, "assigned_otu_count": {},
        "assigned_stable_otu_count": {}, "taxon_member_counts": {}, "rank_gap_count": {},
    }
    for o, m in members:
        out["status_counts"][o["status"]] += 1
        out["read_status_counts"][m["read"]["status"]] += 1
    for level, need in LEVELS.items():
        assigned = [(o, m) for o, m in members if o["status"] in USABLE and depth(o["ranks"]) >= need]
        out["canonical_assigned_count"][level] = len(assigned)
        out["canonical_unassigned_count"][level] = len(members) - len(assigned)
        out["assigned_otu_count"][level] = len({(o["n"], o["marker"]) for o, _ in assigned})
        out["assigned_stable_otu_count"][level] = len({o["stable"] for o, _ in assigned if o["stable"]})
        taxa = Counter(o["ranks"][need] for o, _ in assigned)
        out["taxon_member_counts"][level] = dict(taxa)
        out["rank_gap_count"][level] = taxa.get("NA", 0)
    return out


def assert_metrics_match(metrics, expected):
    for key in ("canonical_member_count", "canonical_direct_hit_count", "canonical_direct_attribution_count",
                "otu_count", "stable_otu_count"):
        assert metrics[key] == expected[key], key
    assert metrics["status_counts"] == expected["status_counts"]
    assert metrics["read_status_counts"] == expected["read_status_counts"]
    for level in LEVELS:
        assert metrics["canonical_assigned_count"][level] == expected["canonical_assigned_count"][level], level
        assert metrics["canonical_unassigned_count"][level] == expected["canonical_unassigned_count"][level], level
        assert metrics["assigned_otu_count"][level] == expected["assigned_otu_count"][level], level
        assert metrics["assigned_stable_otu_count"][level] == expected["assigned_stable_otu_count"][level], level
        if "taxon_member_counts" in metrics:  # full map only in --metrics --full output
            assert metrics["taxon_member_counts"][level] == expected["taxon_member_counts"][level], level
        assert metrics["taxon_count"][level] == len(expected["taxon_member_counts"][level]), level
        assert metrics["taxon_member_count_sum"][level] == sum(expected["taxon_member_counts"][level].values()), level
        assert metrics["rank_gap_count"][level] == expected["rank_gap_count"][level], level
        # Reconciliation identities of the typed contract.
        assert metrics["canonical_assigned_count"][level] + metrics["canonical_unassigned_count"][level] == metrics["canonical_member_count"]
        if "taxon_member_counts" in metrics:
            assert sum(metrics["taxon_member_counts"][level].values()) == metrics["canonical_assigned_count"][level]
        assert metrics["taxon_member_count_sum"][level] == metrics["canonical_assigned_count"][level]
        fraction = metrics["assigned_fraction"][level]
        assert fraction["numerator"] == metrics["canonical_assigned_count"][level]
        assert fraction["denominator"] == metrics["canonical_member_count"]
        if fraction["denominator"] == 0:
            assert fraction["fraction"] is None
        else:
            assert abs(fraction["fraction"] - fraction["numerator"] / fraction["denominator"]) < 1e-6
    assert sum(metrics["status_counts"].values()) == metrics["canonical_member_count"]
    assert sum(metrics["read_status_counts"].values()) == metrics["canonical_member_count"]


# ---------------------------------------------------------------------------
def core_model():
    dup_hash = hash_of("COI", "m0")
    model = [otu(0, members=[
        member("m0"), member("m1", model="sup", adapter="alpha"), member("m2", adapter="beta"),
        member("div", read_status="NO_HIT", adapter="alpha"),
    ])]
    dup = {"marker": "COI", "id": "dup|COI|hac|barcode=|adapter=alpha", "hash": dup_hash, "evidence": [candidate("12345")]}
    return model, [dup]


def test_core_denominator_fixture_4_5_3_4(tmp_path):
    model, extra = core_model()
    root = write_inputs(tmp_path / "fwd", model, extra)
    run_build(root)
    expected = oracle(model, extra)
    assert (expected["canonical_member_count"], expected["blast_eligible_read_support"],
            expected["canonical_direct_hit_count"], expected["canonical_assigned_count"]["species"]) == (4, 5, 3, 4)
    metrics = metrics_of(root)
    assert_metrics_match(metrics, expected)
    assert metrics["by_marker"]["COI"]["canonical_member_count"] == 4
    assert metrics["blast_eligible_otu_count"] == 1
    rows = public_rows(root)
    assert len(rows) == 4
    assert {r["read_id"].split("|")[0] for r in rows} == {"m0", "m1", "m2", "div"}
    assert not any(r["read_id"].startswith("dup|") for r in rows)
    by_uuid = {r["read_id"].split("|")[0]: r for r in rows}
    div = by_uuid["div"]
    assert (div["hit_id"], div["taxid"], div["aln_length"], div["perc_id"]) == ("NA", "NA", "NA", "NA")
    assert [div[f"otu_{r}"] for r in "kingdom phylum class order family genus species".split()] == METAZOA
    assert div["otu_taxid"] == "12345" and div["otu_id"] == "OTUB_0-COI"
    m0 = by_uuid["m0"]
    assert (m0["hit_id"], m0["taxid"], m0["aln_length"], m0["perc_id"]) == ("ACC12345", "12345", "500", "99.5")
    assert by_uuid["m1"]["basecalling_model"] == "sup" and by_uuid["m1"]["barcode_by_homology"] == "COI"
    assert by_uuid["m2"]["sample"] == "beta"
    # One stable OTU regardless of member count; taxon member counts sum to 4.
    assert metrics["stable_otu_count"] == 1 and metrics["otu_count"] == 1
    assert metrics["taxon_member_counts"]["species"] == {"Galerita bicolor": 4}
    internal = reporting_rows(root)
    assert [r["direct_hit"] for r in sorted(internal, key=lambda r: r["uuid"])] == ["0", "1", "1", "1"]
    assert all(r["blast_eligible"] == "1" for r in internal)

    # Reverse input order and repeated raw duplicates: byte- and count-identical.
    dup_again = dict(extra[0], id="dup2|COI|hac|barcode=|adapter=beta")
    root_rev = write_inputs(tmp_path / "rev", model, [extra[0], dup_again], reverse=True)
    run_build(root_rev)
    for name in (f"{BARCODE}_blast_otu_pretax_rpt.txt", f"{BARCODE}_blast_otu_reporting_v1.tsv"):
        assert (root / name).read_bytes() == (root_rev / name).read_bytes(), name


def test_zero_denominator_and_empty_membership(tmp_path):
    root = write_inputs(tmp_path / "zero", [], sidecar=False, eligible=False)
    run_build(root)
    assert (root / f"{BARCODE}_blast_otu_pretax_rpt.txt").read_text(encoding="utf-8") == PUBLIC_HEADER + "\n"
    metrics = metrics_of(root)
    assert metrics["canonical_member_count"] == 0
    for level in LEVELS:
        assert metrics["assigned_fraction"][level] == {"numerator": 0, "denominator": 0, "fraction": None}
        assert metrics["canonical_assigned_count"][level] == 0
    assert metrics["blast_eligible_otu_count"] is None
    # Members without a sidecar is a hard failure, not a silent zero.
    model, extra = core_model()
    root2 = write_inputs(tmp_path / "nosidecar", model, extra, sidecar=False)
    result = run_build(root2, check=False)
    assert result.returncode != 0 and "sidecar missing" in result.stderr
    assert not (root2 / f"{BARCODE}_blast_otu_pretax_rpt.txt").exists()


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("level_depth", [4, 5, 6])
def test_status_depth_matrix(tmp_path, status, level_depth):
    ranks = METAZOA[: level_depth + 1] + ["NA"] * (6 - level_depth)
    origin = "LCA" if status == "AMBIGUOUS_TIE" else "DIRECT"
    read_status = "ASSIGNED" if status in USABLE else status
    if status == "FILTERED_INELIGIBLE" or status == "COMPUTATION_FAILED":
        read_status = status
    members = [
        member("a", read_status=read_status, ranks=ranks, evidence=None if status in ("FILTERED_INELIGIBLE", "COMPUTATION_FAILED", "REFERENCE_UNRESOLVED") else "default",
               reason="missing_sealed_query" if status == "REFERENCE_UNRESOLVED" else "NA"),
        member("b", read_status="NO_HIT"),
    ]
    if status == "REFERENCE_INCONSISTENT":
        members[0] = member("a", read_status="REFERENCE_INCONSISTENT", evidence=[candidate("777")], reason="777:wrong_kingdom")
    model = [otu(3, status=status, ranks=ranks, origin=origin, members=members, taxid="12345" if status == "ASSIGNED" else "NA")]
    root = write_inputs(tmp_path, model)
    run_build(root)
    expected = oracle(model)
    metrics = metrics_of(root)
    assert_metrics_match(metrics, expected)
    rows = public_rows(root)
    assert len(rows) == 2
    for row in rows:
        public_ranks = [row[f"otu_{r}"] for r in "kingdom phylum class order family genus species".split()]
        if status in USABLE:
            assert public_ranks == ranks
            assert row["otu_taxid"] == ("12345" if status == "ASSIGNED" else "NA")
        else:
            assert public_ranks == UNASSIGNED7 and row["otu_taxid"] == "NA"
    for level, need in LEVELS.items():
        want = 2 if (status in USABLE and level_depth >= need) else 0
        assert metrics["canonical_assigned_count"][level] == want, (status, level_depth, level)


def test_synthetic_numeric_parity_and_marker_collision(tmp_path):
    model = [
        otu(0, taxid="12345", members=[member("p1"), member("p2", read_status="NO_HIT")]),
        otu(1, taxid="-1156", ranks=METAZOA, members=[member("s1", taxid="-1156"), member("s2", read_status="NO_HIT")]),
        otu(2, marker="ITS2", taxid="-1156", ranks=PLANT, members=[member("v1", taxid="-1156", ranks=PLANT), member("v2", read_status="NO_HIT")]),
        otu(3, status="REFERENCE_INCONSISTENT", members=[member("w1", read_status="REFERENCE_INCONSISTENT", evidence=[candidate("9606")], reason="9606:wrong_kingdom")]),
        otu(4, status="REFERENCE_UNRESOLVED", members=[member("u1", read_status="REFERENCE_UNRESOLVED", evidence=[candidate("-99")], reason="-99:missing_marker_lineage")]),
        otu(5, status="REFERENCE_UNRESOLVED", members=[member("c1", read_status="REFERENCE_UNRESOLVED", evidence=[candidate("-77")], reason="-77:contradictory_populated_ranks:genus")]),
        otu(6, taxid="555", ranks=METAZOA[:5] + ["NA", "NA"], members=[member("f1", taxid="555", ranks=METAZOA[:5] + ["NA", "NA"])]),
    ]
    root = write_inputs(tmp_path, model)
    run_build(root)
    metrics = metrics_of(root)
    assert_metrics_match(metrics, oracle(model))
    by = {r["read_id"].split("|")[0]: r for r in public_rows(root)}
    # Negative synthetic taxids are first-class in both public taxid fields.
    assert by["s1"]["taxid"] == "-1156" and by["s1"]["otu_taxid"] == "-1156"
    assert by["s1"]["otu_kingdom"] == "Metazoa" and by["v1"]["otu_kingdom"] == "Viridiplantae"
    assert by["v1"]["otu_id"] == "OTUB_2-ITS2" and by["v1"]["barcode_by_homology"] == "ITS2"
    assert by["s1"]["hit_id"] == "ACCN1156"
    # Wrong kingdom / missing / contradictory authority never become assignments; evidence stays visible.
    for uuid in ("w1", "u1", "c1"):
        assert by[uuid]["otu_kingdom"] == "Unassigned" and by[uuid]["taxid"] == "NA"
        assert by[uuid]["hit_id"] != "NA"
    assert metrics["status_counts"]["REFERENCE_INCONSISTENT"] == 1
    assert metrics["status_counts"]["REFERENCE_UNRESOLVED"] == 2
    assert metrics["read_reason_counts"] == {"9606:wrong_kingdom": 1, "-99:missing_marker_lineage": 1,
                                             "-77:contradictory_populated_ranks:genus": 1, "no_hit": 3}
    assert metrics["canonical_assigned_count"] == {"family": 7, "genus": 6, "species": 6}
    assert metrics["by_marker"]["ITS2"]["canonical_assigned_count"]["species"] == 2
    assert metrics["by_marker"]["COI"]["canonical_assigned_count"]["species"] == 4
    assert by["f1"]["otu_genus"] == "NA" and by["f1"]["otu_family"] == "Carabidae"


def test_model_aliases_and_cross_marker_shared_uuid(tmp_path):
    """One canonical member may carry several model aliases in the R4-B sidecar
    (hac and sup ids of the same read); it stays one relation and displays the
    best model. The same uuid under two markers is two distinct relations."""
    model = [
        otu(0, members=[member("m0"), member("shared", adapter="alpha")]),
        otu(1, marker="ITS2", taxid="-1156", ranks=PLANT, members=[member("shared", taxid="-1156", ranks=PLANT, adapter="alpha")]),
    ]
    root = write_inputs(tmp_path, model)
    # Inject a second (sup) alias row for m0 into the R4-B sidecar with identical read-level fields.
    rows = r4b_rows(model)
    m0_row = next(r for r in rows if r["canonical_member"] == "m0|COI")
    alias = dict(m0_row, read_id=m0_row["read_id"].replace("|hac|", "|sup|"))
    rows.insert(0, alias)
    (root / f"{BARCODE}_blast_otu_taxonomy_v1.tsv").write_text(r4b_text(rows), encoding="utf-8")
    # The sup alias is a separate sealed query of the same sequence hash.
    ev = root / "_state" / "otu_blast_evidence_COI.tsv"
    lines = ev.read_text(encoding="utf-8").splitlines()[1:-1]
    m0_lines = [l for l in lines if l.startswith("m0|COI|hac|")]
    lines += [l.replace("m0|COI|hac|", "m0|COI|sup|", 1) for l in m0_lines]
    ev.write_text(sealed("EVIDENCE", SIG, lines), encoding="utf-8")
    run_build(root)
    rows_out = public_rows(root)
    assert len(rows_out) == 3
    by = {(r["read_id"].split("|")[0], r["barcode_by_homology"]): r for r in rows_out}
    assert by[("m0", "COI")]["basecalling_model"] == "sup" and by[("m0", "COI")]["read_id"].startswith("m0|COI|sup|")
    assert by[("shared", "COI")]["otu_id"] == "OTUB_0-COI" and by[("shared", "ITS2")]["otu_id"] == "OTUB_1-ITS2"
    assert by[("shared", "ITS2")]["otu_taxid"] == "-1156" and by[("shared", "COI")]["otu_taxid"] == "12345"
    metrics = metrics_of(root)
    assert metrics["canonical_member_count"] == 3 and metrics["by_marker"]["ITS2"]["canonical_member_count"] == 1
    assert_metrics_match(metrics, oracle(model))


def test_ties_project_na_accession_but_keep_metrics(tmp_path):
    tie = [candidate("100", subject="ACCA|kraken:taxid|100"), candidate("100", subject="ACCB|kraken:taxid|100")]
    model = [otu(0, taxid="100", members=[member("t1", taxid="100", evidence=tie, sources=["100"])])]
    root = write_inputs(tmp_path, model)
    run_build(root)
    row = public_rows(root)[0]
    assert row["hit_id"] == "NA" and row["aln_length"] == "500" and row["perc_id"] == "99.5" and row["taxid"] == "100"
    internal = reporting_rows(root)[0]
    assert internal["candidate_count"] == "2" and internal["direct_hit"] == "1" and internal["hit_taxid"] == "NA"


def test_membership_identity_cases(tmp_path):
    model, extra = core_model()
    root = write_inputs(tmp_path / "base", model, extra)
    run_build(root)
    base_rows = reporting_rows(root)
    # OTUB renumbering: identical biology under another display number keeps one row per member.
    renumbered = copy.deepcopy(model)
    renumbered[0]["n"] = 7
    root2 = write_inputs(tmp_path / "renum", renumbered, extra)
    run_build(root2)
    rows2 = reporting_rows(root2)
    assert [(r["canonical_member"], r["stable_otu_key"]) for r in base_rows] == [(r["canonical_member"], r["stable_otu_key"]) for r in rows2]
    assert {r["display_otu_key"] for r in rows2} == {"OTUB_7-COI"}
    # OTUB reuse by another stable OTU: distinct stable keys, no cross-talk.
    reused = copy.deepcopy(model)
    reused[0]["stable"] = hash_of("COI", "other-rep")
    root3 = write_inputs(tmp_path / "reuse", reused, extra)
    run_build(root3)
    assert {r["stable_otu_key"] for r in reporting_rows(root3)} == {f"COI|{hash_of('COI', 'other-rep')}"}
    # Zero-representative projection: stable key NA is preserved, never invented.
    norep = copy.deepcopy(model)
    norep[0]["stable"] = None
    root4 = write_inputs(tmp_path / "norep", norep, extra)
    run_build(root4)
    assert {r["stable_otu_key"] for r in reporting_rows(root4)} == {"NA"}
    metrics4 = metrics_of(root4)
    assert metrics4["stable_otu_count"] == 0 and metrics4["otu_without_stable_key_count"] == 1 and metrics4["otu_count"] == 1
    # Same member in two stable OTUs: rejected.
    twice = copy.deepcopy(model) + [otu(1, members=[member("m0", adapter="alpha")])]
    root5 = write_inputs(tmp_path / "twice", twice, extra)
    result = run_build(root5, check=False)
    assert result.returncode != 0
    # Membership/sidecar disagreement (member missing from R3 membership): rejected.
    root6 = write_inputs(tmp_path / "disagree", model, extra)
    text = (root6 / "otu_members_round.tsv").read_text(encoding="utf-8")
    (root6 / "otu_members_round.tsv").write_text("".join(l + "\n" for l in text.splitlines() if not l.endswith("\tdiv")), encoding="utf-8")
    result = run_build(root6, check=False)
    assert result.returncode != 0 and "disagree" in result.stderr
    # Duplicate identical membership relation is tolerated; conflicting evidence is rejected.
    root7 = write_inputs(tmp_path / "dupe", model, extra)
    (root7 / "otu_members_round.tsv").write_text(text + "OTUB_0-COI\tm0\n", encoding="utf-8")
    run_build(root7)
    assert reporting_rows(root7) == base_rows


def test_evidence_generation_mismatch_fails_closed(tmp_path):
    model, extra = core_model()
    root = write_inputs(tmp_path, model, extra)
    # R4-B recorded a hit for m0, but the sealed evidence says NO_HIT: stale generation.
    lines = (root / "_state" / "otu_blast_evidence_COI.tsv").read_text(encoding="utf-8").splitlines()[1:-1]
    # m0 and the raw duplicate share one sequence hash, so both queries flip to
    # NO_HIT together (the sealed file stays internally consistent).
    fixed = ["\t".join([l.split("\t")[0], l.split("\t")[1]] + ["NA"] * 11 + ["NO_HIT"]) if l.startswith(("m0|", "dup|")) else l for l in lines]
    (root / "_state" / "otu_blast_evidence_COI.tsv").write_text(sealed("EVIDENCE", SIG, fixed), encoding="utf-8")
    result = run_build(root, check=False)
    assert result.returncode != 0 and "disagree on direct evidence" in result.stderr


def test_corrupt_sidecar_fails_before_any_output(tmp_path):
    model, extra = core_model()
    root = write_inputs(tmp_path, model, extra)
    path = root / f"{BARCODE}_blast_otu_taxonomy_v1.tsv"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = "#END\t4\t" + "0" * 64
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    result = run_build(root, check=False)
    assert result.returncode != 0
    assert not (root / f"{BARCODE}_blast_otu_pretax_rpt.txt").exists()
    assert not (root / f"{BARCODE}_blast_otu_reporting_v1.tsv").exists()
    assert not list(root.glob(".r4d-publish-*"))


def publish(root: Path, state: Path, noadapter="0", reporting=None, check=True):
    return run_helper(["--publish-cumulative", "--reporting", str(reporting or root / f"{BARCODE}_blast_otu_reporting_v1.tsv"),
                       "--state-dir", str(state), "--barcode", BARCODE, "--noadapter-enabled", noadapter], root, check=check)


def test_cumulative_snapshot_replace_retry_legacy_and_failure(tmp_path):
    model, extra = core_model()
    model[0]["members"].append(member("na1", read_status="NO_HIT", adapter="no_adapter_1"))
    root = write_inputs(tmp_path, model, extra)
    run_build(root)
    state = tmp_path / "state"
    state.mkdir()
    legacy = state / f"{BARCODE}_blast_otu_pretax_rpt.txt"
    legacy.write_text(PUBLIC_HEADER + "\n" + "old|COI|hac|barcode=|adapter=alpha|OTUB_9-COI\tCOI\thac\talpha\th\t1\t1\t1\tOTUB_9-COI\t1\t" + "\t".join(METAZOA) + "\n" * 1, encoding="utf-8")
    legacy_bytes = legacy.read_bytes()
    publish(root, state, noadapter="1")
    snapshot = state / f"{BARCODE}_blast_otu_reporting_v1.tsv"
    assert snapshot.read_bytes() == (root / f"{BARCODE}_blast_otu_reporting_v1.tsv").read_bytes()
    assert legacy.read_bytes() == (root / f"{BARCODE}_blast_otu_pretax_rpt.txt").read_bytes()
    assert legacy.read_bytes() != legacy_bytes
    noadapter = state / f"{BARCODE}_blast_otu_noadapter_rpt.txt"
    na_rows = public_rows(state, name=noadapter.name)
    assert [r["read_id"].split("|")[0] for r in na_rows] == ["na1"] and na_rows[0]["sample"] == "no_adapter"
    assert not list(state.glob("*.bak.*")) and not list(state.glob(".r4d-publish-*"))
    first = {p.name: p.read_bytes() for p in state.iterdir()}
    # Retry / replay: byte-identical, no growth.
    publish(root, state, noadapter="1")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == first
    # No-adapter split disabled: header-only projection, other files unchanged.
    publish(root, state, noadapter="0")
    assert noadapter.read_text(encoding="utf-8") == PUBLIC_HEADER + "\n"
    assert snapshot.read_bytes() == first[snapshot.name]
    # Failed publication (corrupt round sidecar) preserves the previous snapshot byte for byte.
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    corrupt = root / "corrupt.tsv"
    lines = (root / f"{BARCODE}_blast_otu_reporting_v1.tsv").read_text(encoding="utf-8").splitlines()
    lines[-1] = "#END\t5\t" + "0" * 64
    corrupt.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    result = publish(root, state, reporting=corrupt, check=False)
    assert result.returncode != 0
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before
    # Interruption residue (stale temp and backup files) never blocks a later publication.
    (state / ".r4d-publish-stale").write_text("x", encoding="utf-8")
    (state / f"{BARCODE}_blast_otu_pretax_rpt.txt.bak.999").write_text("y", encoding="utf-8")
    publish(root, state, noadapter="1")
    assert snapshot.read_bytes() == first[snapshot.name]
    # No-new / cache-only round: an identical input generation republishes identical bytes.
    root2 = write_inputs(tmp_path / "again", model, extra)
    run_build(root2)
    assert (root2 / f"{BARCODE}_blast_otu_reporting_v1.tsv").read_bytes() == snapshot.read_bytes()
    # Removal from current membership removes the row from the snapshot.
    smaller = copy.deepcopy(model)
    smaller[0]["members"] = [m for m in smaller[0]["members"] if m["uuid"] != "m2"]
    root3 = write_inputs(tmp_path / "smaller", smaller, extra)
    run_build(root3)
    publish(root3, state, noadapter="1")
    assert {r["read_id"].split("|")[0] for r in public_rows(state)} == {"m0", "m1", "div", "na1"}


# ---------------------------------------------------------------------------
# Transactional publication fault injection (F-01). A disposable Perl wrapper
# loads the require-safe helper and overrides one filesystem primitive
# deterministically; production code carries no fault-injection interface.
FAULT_WRAPPER = r"""
use strict;
use warnings;
BEGIN {
    *CORE::GLOBAL::rename = sub {
        my ($from, $to) = @_;
        my $fault = $ENV{R4D_FAULT} // '';
        my $arg = $ENV{R4D_FAULT_ARG} // '';
        # A backup fault matches the `.bak.<pid>` destination; a replacement fault
        # matches only renames whose source is a transaction temp file, so the
        # rollback's own restore rename (backup -> target) is never blocked.
        if ($fault eq 'rename' && $arg ne '' && $to =~ /\Q$arg\E[0-9]*\z/
            && ($to =~ /\.bak\.[0-9]+\z/ || $from =~ /\.r4d-publish-/)) { $! = 13; return 0; }
        return CORE::rename($from, $to);
    };
}
use FindBin ();
use File::Basename ();
my ($helper, $reporting, $state_dir) = @ARGV;
$FindBin::Bin = File::Basename::dirname($helper);   # helper resolves bin/lib via FindBin
require $helper;
{
    no warnings 'redefine';
    my $orig = \&RTBioScan::R4D::tempfile;
    my $calls = 0;
    *RTBioScan::R4D::tempfile = sub {
        $calls++;
        my $fault = $ENV{R4D_FAULT} // '';
        my $n = $ENV{R4D_FAULT_ARG} // 0;
        die "injected tempfile failure at call $calls\n" if $fault eq 'tempfile_die' && $calls == $n;
        my @r = $orig->(@_);
        if ($fault eq 'tempfile_readonly' && $calls == $n) {
            close $r[0];
            open my $ro, '<', $r[1] or die "reopen $r[1]: $!";
            return ($ro, $r[1]);
        }
        return @r;
    };
}
my $n = RTBioScan::R4D::publish_cumulative(
    reporting => $reporting,
    state_reporting => "$state_dir/RTBioScan_blast_otu_reporting_v1.tsv",
    state_public => "$state_dir/RTBioScan_blast_otu_pretax_rpt.txt",
    state_noadapter => "$state_dir/RTBioScan_blast_otu_noadapter_rpt.txt",
    noadapter_enabled => 1,
);
print "published $n\n";
"""
STATE_NAMES = (f"{BARCODE}_blast_otu_noadapter_rpt.txt", f"{BARCODE}_blast_otu_pretax_rpt.txt", f"{BARCODE}_blast_otu_reporting_v1.tsv")


def _fault_publish(tmp_path: Path, root: Path, state: Path, fault: str, arg: str):
    wrapper = tmp_path / "fault_wrapper.pl"
    wrapper.write_text(FAULT_WRAPPER, encoding="utf-8")
    env = {**os.environ, **ENV, "R4D_FAULT": fault, "R4D_FAULT_ARG": arg}
    return subprocess.run(["perl", str(wrapper), str(HELPER), str(root / f"{BARCODE}_blast_otu_reporting_v1.tsv"), str(state)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, check=False)


def _state_snapshot(state: Path):
    return {p.name: (p.read_bytes(), p.stat().st_ino) for p in state.iterdir() if p.is_file()}


def _assert_no_residue(state: Path):
    assert not list(state.glob(".r4d-publish-*")), list(state.iterdir())
    assert not list(state.glob("*.bak.*")), list(state.iterdir())


def _seed_state(state: Path, names=STATE_NAMES):
    state.mkdir(parents=True, exist_ok=True)
    for name in names:
        (state / name).write_bytes(f"previous authoritative {name}\n".encode("utf-8"))
    return _state_snapshot(state)


def _expected_publication(tmp_path: Path, root: Path):
    control = tmp_path / "control-state"
    control.mkdir()
    publish(root, control, noadapter="1")
    return {p.name: p.read_bytes() for p in control.iterdir()}


@pytest.mark.parametrize("fault,arg", [("tempfile_die", "1"), ("tempfile_die", "3"), ("tempfile_readonly", "2")])
def test_transaction_failure_before_backup_staging_preserves_snapshot(tmp_path, fault, arg):
    model, extra = core_model()
    root = write_inputs(tmp_path / "round", model, extra)
    run_build(root)
    state = tmp_path / "state"
    before = _seed_state(state)
    result = _fault_publish(tmp_path, root, state, fault, arg)
    assert result.returncode != 0
    assert "published" not in result.stdout
    assert ("injected tempfile failure" in result.stderr) if fault == "tempfile_die" else ("R4-D: write" in result.stderr), result.stderr
    after = _state_snapshot(state)
    assert set(after) == set(STATE_NAMES), after.keys()
    for name in STATE_NAMES:
        assert after[name][0] == before[name][0], name
        assert after[name][1] == before[name][1], f"inode changed for {name}"
    _assert_no_residue(state)
    # Successful control and replay converge to the expected outputs without growth.
    expected = _expected_publication(tmp_path, root)
    publish(root, state, noadapter="1")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == expected
    publish(root, state, noadapter="1")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == expected
    _assert_no_residue(state)


@pytest.mark.parametrize("failing", ["noadapter_rpt.txt", "pretax_rpt.txt", "reporting_v1.tsv"])
def test_transaction_backup_rename_failure_restores_every_target(tmp_path, failing):
    model, extra = core_model()
    root = write_inputs(tmp_path / "round", model, extra)
    run_build(root)
    state = tmp_path / "state"
    before = _seed_state(state)
    result = _fault_publish(tmp_path, root, state, "rename", f"{failing}.bak.")
    assert result.returncode != 0 and "stage backup" in result.stderr
    after = _state_snapshot(state)
    assert set(after) == set(STATE_NAMES), after.keys()
    for name in STATE_NAMES:
        assert after[name][0] == before[name][0], name
        assert after[name][1] == before[name][1], f"inode changed for {name}"
    _assert_no_residue(state)
    expected = _expected_publication(tmp_path, root)
    publish(root, state, noadapter="1")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == expected
    publish(root, state, noadapter="1")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == expected
    _assert_no_residue(state)


@pytest.mark.parametrize("fault,arg", [("rename", "reporting_v1.tsv.bak."), ("rename", f"{BARCODE}_blast_otu_pretax_rpt.txt")])
def test_transaction_failure_keeps_absent_target_absent(tmp_path, fault, arg):
    """One target (no-adapter table) does not exist before the transaction: on
    failure the two pre-existing targets are preserved and the absent one stays
    absent, whether the failure happens during backup staging or after the new
    file was already renamed into place."""
    model, extra = core_model()
    root = write_inputs(tmp_path / "round", model, extra)
    run_build(root)
    state = tmp_path / "state"
    present = (STATE_NAMES[1], STATE_NAMES[2])
    before = _seed_state(state, present)
    assert not (state / STATE_NAMES[0]).exists()
    result = _fault_publish(tmp_path, root, state, fault, arg)
    assert result.returncode != 0 and "published" not in result.stdout
    assert ("stage backup" in result.stderr) if arg.endswith(".bak.") else ("replace" in result.stderr), result.stderr
    after = _state_snapshot(state)
    assert set(after) == set(present), after.keys()
    for name in present:
        assert after[name][0] == before[name][0], name
        assert after[name][1] == before[name][1], f"inode changed for {name}"
    assert not (state / STATE_NAMES[0]).exists()
    _assert_no_residue(state)
    expected = _expected_publication(tmp_path, root)
    publish(root, state, noadapter="1")
    assert {p.name: p.read_bytes() for p in state.iterdir()} == expected
    _assert_no_residue(state)


def test_reporting_blast_otu_contract_mode_and_legacy_warning(tmp_path):
    model, extra = core_model()
    root = write_inputs(tmp_path, model, extra)
    (root / "summary.tsv").write_text("read_id\tsequence_length_template\tmean_qscore_template\nm0\t500\t12.5\n", encoding="utf-8")
    (root / "pre_read_info.tsv").write_text("read_id\nm0\nm1\n", encoding="utf-8")
    (root / "blast.csv").write_text("qseqid,sseqid,evalue,length,pident\n", encoding="utf-8")
    (root / "preferred.txt").write_text("", encoding="utf-8")
    env = {**os.environ, **ENV,
           "RTBIOSCAN_R4D_SIDECAR": str(root / f"{BARCODE}_blast_otu_taxonomy_v1.tsv"),
           "RTBIOSCAN_R4D_MEMBERS": str(root / "otu_members_round.tsv"),
           "RTBIOSCAN_R4D_EVIDENCE_DIR": str(root / "_state"),
           "RTBIOSCAN_R4D_ELIGIBLE": str(root / "kept_otus.tsv")}
    cmd = ["perl", str(REPORTING_BLAST_OTU), "summary.tsv", "blast.csv", "preferred.txt", "pre_read_info.tsv", BARCODE]
    result = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "r4d_reporting canonical_members=4" in result.stderr
    assert len(public_rows(root)) == 4
    assert (root / f"{BARCODE}_read_info_rpt.txt").read_text(encoding="utf-8").splitlines()[1] == "m0\t500\t12.5"
    assert (root / f"{BARCODE}_blast_otu_reporting_v1.tsv").exists()
    # Legacy invocation stays available for the standalone interface but warns deterministically.
    legacy_env = {k: v for k, v in env.items() if not k.startswith("RTBIOSCAN_R4D_")}
    result = subprocess.run(cmd, cwd=root, env=legacy_env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "legacy hit-row projection" in result.stderr
    assert (root / f"{BARCODE}_blast_otu_pretax_rpt.txt").read_text(encoding="utf-8") == PUBLIC_HEADER + "\n"


# ---------------------------------------------------------------------------
OTU_DEF_HEADER = ("read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\t"
                  "replicate\tidentity_scope\tidentity_value\tOTU_id\tOTU_role")


def write_otu_def(root: Path, model):
    lines = [OTU_DEF_HEADER]
    sizes = ["otu_id\tsize"]
    lock = ["otu_key\teffective_consolidated\tis_frozen"]
    for o in model:
        display = f"OTUB_{o['n']}-{o['marker']}"
        for index, m in enumerate(o["members"]):
            lines.append("\t".join([m["uuid"], o["marker"], m["model"], m["adapter"], "nanopore", "grab", "sub1", "1",
                                    "sample", m["adapter"], display, "REPRESENTATIVE" if index == 0 else "MEMBER"]))
        sizes.append(f"{display}\t{len(o['members'])}")
        lock.append(f"{display}\t0\t0")
    (root / "otu_def.tsv").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    (root / "otu_sizes_round.tsv").write_text("".join(l + "\n" for l in sizes), encoding="utf-8")
    (root / "lock.tsv").write_text("".join(l + "\n" for l in lock), encoding="utf-8")


def run_round_json(root: Path, extra=(), out="round_report.json", cumulative=True):
    cmd = ["perl", str(REPORT_ROUND_JSON), "--run-id", "runA", "--barcode", BARCODE, "--round-barcode", "round_1",
           "--targets", "COI|ITS2", "--target-taxa", "Metazoa|Viridiplantae",
           "--timestamp-utc", "2026-09-22T00:00:00Z", "--out", str(root / out),
           "--blast-otu", str(root / f"{BARCODE}_blast_otu_pretax_rpt.txt"),
           "--blast-otu-reporting", str(root / f"{BARCODE}_blast_otu_reporting_v1.tsv"),
           "--otu-def", str(root / "otu_def.tsv"), "--otu-sizes-round", str(root / "otu_sizes_round.tsv"),
           "--otu-lock-summary", str(root / "lock.tsv"),
           "--blast-id-family", "92", "--blast-id-genus", "95", "--blast-id-spec", "98"]
    if cumulative:
        cmd += ["--blast-otu-cumulative", str(root / f"{BARCODE}_blast_otu_pretax_rpt.txt"),
                "--blast-otu-reporting-cumulative", str(root / f"{BARCODE}_blast_otu_reporting_v1.tsv")]
    cmd += list(extra)
    result = subprocess.run(cmd, cwd=root, env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads((root / out).read_text(encoding="utf-8")), result


def test_report_round_json_taxonomy_assignment_section(tmp_path):
    model = [
        otu(0, members=[member("m0"), member("m1", model="sup"), member("m2", adapter="beta"), member("div", read_status="NO_HIT")]),
        otu(1, taxid="-1156", members=[member("s1", taxid="-1156"), member("s2", read_status="NO_HIT")]),
        otu(2, taxid="555", ranks=METAZOA[:5] + ["NA", "NA"], members=[member("f1", taxid="555", ranks=METAZOA[:5] + ["NA", "NA"])]),
        otu(3, status="NO_HIT", members=[member("n1", read_status="NO_HIT"), member("n2", read_status="NO_HIT")]),
        otu(4, marker="ITS2", taxid="-1156", ranks=PLANT, members=[member("v1", taxid="-1156", ranks=PLANT)]),
    ]
    root = write_inputs(tmp_path, model)
    run_build(root)
    write_otu_def(root, model)
    data, result = run_round_json(root)
    assert data["schema_version"] == "2.1"
    section = data["taxonomy_assignment"]
    assert section["schema"] == "r4d-v1"
    assert_metrics_match(section["round"], oracle(model))
    assert section["cumulative_equals_round"] is True
    assert section["round"]["canonical_assigned_count"] == {"family": 8, "genus": 7, "species": 7}
    for level in LEVELS:
        recon = section["reconciliation"][level]
        assert recon["consistent"] is True, (level, recon)
        assert recon["canonical_assigned_count"] == section["round"]["canonical_assigned_count"][level]
    # Synthetic signed OTUs count as assigned; hit-less members inherit their OTU.
    species_rows = data["otu"]["assignments_by_level"]["species"]
    by_taxon_sample = {(r["taxon"], r["sample"], r["marker"]): r for r in species_rows}
    assert by_taxon_sample[("Galerita bicolor", "alpha", "COI")]["reads_total"] == 5  # m0, m1, div, s1, s2
    assert by_taxon_sample[("Galerita bicolor", "beta", "COI")]["reads_total"] == 1
    assert by_taxon_sample[("Porella arborisvitae", "alpha", "ITS2")]["reads_total"] == 1
    # Family-only OTU appears at family, not genus/species.
    assert any(r["taxon"] == "Carabidae" and r["otu_count"] == 3 for r in data["otu"]["assignments_by_level"]["family"])
    assert not any(r["otu_count"] > 2 for r in species_rows)
    sample_alpha = next(e for e in data["sample_metrics"].values() if e["label"] == "alpha")
    assert sample_alpha["reads_blast_assigned"] == 7  # m0 m1 div s1 s2 f1 v1 (n1/n2 unassigned)
    # Informative OTU split counts the synthetic and family-only OTUs as assigned.
    active = data["otu"]["active_by_marker_taxon"]
    assert (active["coi_assigned"], active["its2_assigned"]) == (3, 1)
    # Deprecated inputs: deterministic warning, byte-identical JSON.
    (root / "summary.tsv").write_text("x\n", encoding="utf-8")
    data2, result2 = run_round_json(root, extra=["--summary", str(root / "summary.tsv"), "--summary-otu", str(root / "summary.tsv")], out="second.json")
    assert "--summary is deprecated and ignored" in result2.stderr and "--summary-otu is deprecated and ignored" in result2.stderr
    assert json.loads((root / "round_report.json").read_text(encoding="utf-8")) == data2
    # Without any sidecar the additive section is null and the schema version is still declared.
    data3, _ = run_round_json(root, extra=["--blast-otu-reporting", str(root / "missing.tsv")], out="third.json", cumulative=False)
    assert data3["taxonomy_assignment"] is None and data3["schema_version"] == "2.1"


def test_report_round_json_legacy_table_signed_taxid_without_lineage_columns(tmp_path):
    """Legacy public tables (no otu_* lineage columns, no sidecar) still count a
    signed synthetic OTU taxid as assigned; taxid positivity is not a criterion."""
    root = tmp_path
    header = "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid"
    (root / "legacy.tsv").write_text(header + "\n" + "r1|COI|hac|barcode=|adapter=alpha|OTUB_0-COI\tCOI\thac\talpha\tACC\t-1156\t500\t99.5\tOTUB_0-COI\t-1156\n"
                                     + "r2|COI|hac|barcode=|adapter=alpha|OTUB_1-COI\tCOI\thac\talpha\tNA\tNA\tNA\tNA\tOTUB_1-COI\tNA\n", encoding="utf-8")
    (root / "otu_def.tsv").write_text(OTU_DEF_HEADER + "\n" + "\t".join(["r1", "COI", "hac", "alpha", "nanopore", "grab", "sub1", "1", "sample", "alpha", "OTUB_0-COI", "REPRESENTATIVE"]) + "\n"
                                      + "\t".join(["r2", "COI", "hac", "alpha", "nanopore", "grab", "sub1", "1", "sample", "alpha", "OTUB_1-COI", "REPRESENTATIVE"]) + "\n", encoding="utf-8")
    (root / "otu_sizes_round.tsv").write_text("otu_id\tsize\nOTUB_0-COI\t1\nOTUB_1-COI\t1\n", encoding="utf-8")
    (root / "lock.tsv").write_text("otu_key\teffective_consolidated\tis_frozen\nOTUB_0-COI\t0\t0\nOTUB_1-COI\t0\t0\n", encoding="utf-8")
    cmd = ["perl", str(REPORT_ROUND_JSON), "--run-id", "runA", "--barcode", BARCODE, "--round-barcode", "round_1",
           "--targets", "COI|ITS2", "--target-taxa", "Metazoa|Viridiplantae", "--timestamp-utc", "2026-09-22T00:00:00Z",
           "--out", str(root / "out.json"), "--blast-otu", str(root / "legacy.tsv"), "--otu-def", str(root / "otu_def.tsv"),
           "--otu-sizes-round", str(root / "otu_sizes_round.tsv"), "--otu-lock-summary", str(root / "lock.tsv")]
    result = subprocess.run(cmd, cwd=root, env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    data = json.loads((root / "out.json").read_text(encoding="utf-8"))
    assert data["taxonomy_assignment"] is None
    assert data["otu"]["active_by_marker_taxon"]["coi_assigned"] == 1
    alpha = next(e for e in data["sample_metrics"].values() if e["label"] == "alpha")
    assert alpha["reads_blast_assigned"] == 1


def test_report_round_json_rejects_malformed_sidecar(tmp_path):
    model, extra = core_model()
    root = write_inputs(tmp_path, model, extra)
    run_build(root)
    write_otu_def(root, model)
    path = root / f"{BARCODE}_blast_otu_reporting_v1.tsv"
    path.write_text(path.read_text(encoding="utf-8").replace("#END\t4\t", "#END\t3\t"), encoding="utf-8")
    cmd = ["perl", str(REPORT_ROUND_JSON), "--run-id", "runA", "--barcode", BARCODE, "--round-barcode", "round_1",
           "--out", str(root / "out.json"), "--blast-otu", str(root / f"{BARCODE}_blast_otu_pretax_rpt.txt"),
           "--blast-otu-reporting", str(path)]
    result = subprocess.run(cmd, cwd=root, env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "R4-D" in result.stderr


def write_contract_sidecar(path: Path, report_kind: str, header, row_count: int, context: str):
    header_line = "\t".join(header)
    meta = {
        "contract_version": "1", "report_kind": report_kind, "context": context, "row_count": str(row_count),
        "empty_contract": "genuinely_empty" if row_count == 0 else "nonempty",
        "header_sha1": hashlib.sha1(header_line.encode("utf-8")).hexdigest(),
    }
    path.write_text("".join(f"{k}\t{meta[k]}\n" for k in ("contract_version", "report_kind", "context", "row_count", "empty_contract", "header_sha1")), encoding="utf-8")


def test_append_reports_counts_canonical_members_not_hits(tmp_path):
    members = [member(f"a{i}") for i in range(3)] + [member(f"h{i}", read_status="NO_HIT") for i in range(3)]
    unassigned = [member(f"u{i}", read_status="NO_HIT") for i in range(6)]
    model = [otu(0, members=members), otu(1, status="NO_HIT", members=unassigned)]
    root = write_inputs(tmp_path, model)
    run_build(root)
    temp_dir = root / "temp"
    temp_dir.mkdir()
    (root / "round_001").mkdir()
    read_info_header = "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore"
    reads = [m["uuid"] for o in model for m in o["members"]]
    (root / f"{BARCODE}_read_info_rpt.txt").write_text(read_info_header + "\n" + "".join(f"{r}\tround.pod5\trun001\talpha\t100\t10\t100\t12\t100\t15\n" for r in reads), encoding="utf-8")
    (root / f"{BARCODE}_on_target_rpt.txt").write_text("read_id\tbarcode\ton_target_kingdom\n" + "".join(f"{r}\talpha\tON_TARGET\n" for r in reads), encoding="utf-8")
    consensus_header = "read_id\tsample\tconsensus_id\tnumber_of_reads\tconsensus_kingdom\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species"
    (root / f"{BARCODE}_blast_consensus_tax_rpt.txt").write_text(consensus_header + "\n", encoding="utf-8")
    demult_header = "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value"
    demult_rows = [f"{r}\tCOI\thac\talpha\tnanopore\tgrab\tsub1\t1\tsample\talpha" for r in reads]
    (root / f"{BARCODE}_demult_rpt.txt").write_text(demult_header + "\n" + "".join(x + "\n" for x in demult_rows), encoding="utf-8")
    write_contract_sidecar(root / f"{BARCODE}_demult_rpt.contract.tsv", "demult_rpt", demult_header.split("\t"), len(demult_rows), CONTEXT)
    otu_rows = [f"{row}\tOTUB_{0 if r.startswith(('a', 'h')) else 1}-COI\tMEMBER" for row, r in zip(demult_rows, reads)]
    (root / f"{BARCODE}_otu_def_rpt.txt").write_text(OTU_DEF_HEADER + "\n" + "".join(x + "\n" for x in otu_rows), encoding="utf-8")
    write_contract_sidecar(root / f"{BARCODE}_otu_def_rpt.contract.tsv", "otu_def_rpt", OTU_DEF_HEADER.split("\t"), len(otu_rows), CONTEXT)
    (root / "sequencing_template.tsv").write_text(f"run_id\ttime\tstage\treads\n{BARCODE}\t0\tfast\t0\n", encoding="utf-8")
    (root / "round_001.pod5").write_bytes(b"test")
    cmd = ["perl", str(APPEND_REPORTS), "round_001", str(temp_dir), str(root / "round_001.pod5"), BARCODE, str(root / "sequencing_template.tsv")]
    result = subprocess.run(cmd, cwd=root, env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    # The cumulative public snapshot is owned by _reporting_blast_pretax; append_reports never (re)creates it.
    assert not (temp_dir / f"{BARCODE}_blast_otu_pretax_rpt.txt").exists()
    tax_time = (temp_dir / f"{BARCODE}_otu_tax_time_rpt.txt").read_text(encoding="utf-8").splitlines()
    identifications = {line.split("\t")[2]: int(line.split("\t")[3]) for line in tax_time[1:4]}
    assert identifications == {"species": 1, "genus": 1, "family": 1}  # 6 canonical members incl. 3 hit-less; Unassigned never counts
    treemap = (temp_dir / f"{BARCODE}_otu_tax_spc_metazoa_treemap_rpt.txt").read_text(encoding="utf-8").splitlines()
    assert treemap[1:] == ["Insecta\tGalerita bicolor\t6"]
    reads_rank = [line for line in tax_time if "species" in line and "COI" in line]
    assert any(line.endswith("\t6") for line in reads_rank), reads_rank


def test_main_nf_wiring_retires_dead_state_and_uses_contract():
    text = MAIN_NF.read_text(encoding="utf-8")
    reporting_block = text.split("process _reporting_blast_pretax {", 1)[1].split("process consensus {", 1)[0]
    blast_block = text.split("process blast_OTU_pretax {", 1)[1].split("process _reporting_blast_pretax {", 1)[0]
    hac_block = text.split("process _reporting_hac_basecalling {", 1)[1].split("process demultiplexing_hq_reads {", 1)[0]
    assert "tail -n +2 ${barcode}_blast_otu_pretax_rpt.txt >>" not in reporting_block
    assert "tail -n +2 ${barcode}_blast_otu_noadapter_rpt.txt >>" not in reporting_block
    assert "_state/${barcode}_sup.tsv" not in reporting_block
    assert "_state/${barcode}_hac.tsv" not in hac_block
    assert 'r4_reporting_contract.pl --publish-cumulative' in reporting_block
    assert 'r4_reporting_contract.pl --project-noadapter' in reporting_block
    assert 'export RTBIOSCAN_R4D_SIDECAR="${ongoingStateDir}/${round_barcode}/${barcode}_blast_otu_taxonomy_v1.tsv"' in reporting_block
    assert 'export RTBIOSCAN_R4D_MEMBERS="${ongoingStateDir}/${round_barcode}/otu_members_round.tsv"' in reporting_block
    assert 'if [ "\\$ROUND_FAILED" -eq 0 ] && [ -s "\\$R4D_REPORTING_ROUND" ]; then' in reporting_block
    assert 'cache_blast_by_hash.pl" --merge' not in blast_block
    assert 'cp "\\${STATE_DIR}/blastreport.txt"' not in blast_block
    assert 'mv "\\$STATE_BLASTREPORT_TMP" "\\${STATE_DIR}/blastreport.txt"' not in blast_block
    assert 'BLAST_STATE_INIT_MARKER="\\${STATE_DIR}/blastreport_initialized_v1.txt"' in blast_block
    assert 'if [ -f "\\$BLAST_STATE_INIT_MARKER" ] || [ -f "\\${STATE_DIR}/blastreport.txt" ]; then' in blast_block
    assert 'if [ "\\$STATE_BLASTREPORT_EXISTS" -eq 1 ]; then' in blast_block
    assert blast_block.index('STATE_BLASTREPORT_EXISTS=1') < blast_block.index('printf \'RTB-R4D-BLAST-STATE')
    assert 'copy_soft "\\$BLAST_FILTER_KEPT_OTUS" "\\$ROUND_DIR/${barcode}_blast_filter_kept_otus.tsv"' in blast_block
    summary_block = text.split("process getting_run_summary {", 1)[1]
    assert '--summary "${summary}"' not in summary_block and '--summary-otu "${summary_otu}"' not in summary_block
    assert '--schema-version "2.1" \\' in summary_block
    assert '--blast-otu-reporting "\\$ROUND_DIR/${barcode}_blast_otu_reporting_v1.tsv" \\' in summary_block
    assert '--blast-otu-reporting-cumulative "${ongoingStateDir}/_state/${barcode}_blast_otu_reporting_v1.tsv" \\' in summary_block
