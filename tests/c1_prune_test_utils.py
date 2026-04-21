import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PRUNE_HELPER = REPO_ROOT / "bin" / "otu_c1_prune_ids.sh"


def run_c1_prune_helper(
    policy: str,
    fasta: Path,
    out_ids: Path,
    *,
    frozen_members: Path | None = None,
    consolidated_keys: Path | None = None,
    samples_file: Path | None = None,
    mixed_mode: str = "sample_scoped_only",
    noadapter_hint: int = 0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(PRUNE_HELPER),
            policy,
            str(fasta),
            str(out_ids),
            str(frozen_members or ""),
            str(consolidated_keys or ""),
            str(samples_file or ""),
            mixed_mode,
            str(noadapter_hint),
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def read_pruned_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
