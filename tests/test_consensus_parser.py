import subprocess
from pathlib import Path


def test_consensus_parser_uses_last_metadata_block(tmp_path: Path) -> None:
    cluster = tmp_path / "cluster.fa"
    cluster.write_text(
        ">sample|OTUB_1|COI|reads-5|OTU=OTUB_1-COI|minQ=20|consolidated=0|minQ=25|consolidated=1\n"
        "AAAA\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "consolidated.list"
    consolidated.write_text("", encoding="utf-8")

    awk_script = r"""
BEGIN{
    max_reads=-1; best_cons=-1; best_minq=-1e18; best_otu=""; best_h="";
    while ((getline k < ck) > 0) {
        gsub(/\r/, "", k);
        if (k ~ /\S/) consk[k]=1;
    }
    close(ck);
}
/^>/{
    h=$0; sub(/^>/,"",h);
    reads=0;
    if (h ~ /reads-[0-9]+/) {
        tmp=h; sub(/.*reads-/,"",tmp); gsub(/[^0-9].*/,"",tmp); reads=tmp+0;
    }
    otu="";
    if (match(h, /\|OTU=[^|]+/)) {
        otu=substr(h, RSTART+5, RLENGTH-5);
    } else {
        n=split(h, p, "|");
        if (n>=2) {
            otu=p[2];
            if (n>=3 && p[3] != "" && otu !~ ("-" p[3] "$")) otu=otu "-" p[3];
        }
    }
    cons=-1;
    tmp=h;
    while (match(tmp, /\|consolidated=[01]/)) {
        cons=substr(tmp, RSTART+14, 1)+0;
        tmp=substr(tmp, RSTART+RLENGTH);
    }
    if (cons < 0) {
        if (otu != "" && (otu in consk)) { cons=1; } else { cons=0; }
    }
    minq=-1e18;
    tmp=h;
    while (match(tmp, /\|minQ=[^|]+/)) {
        mq=substr(tmp, RSTART+6, RLENGTH-6);
        tmp=substr(tmp, RSTART+RLENGTH);
        if (mq != "NA" && mq ~ /^-?[0-9]+([.][0-9]+)?$/) minq=mq+0;
    }
    if (reads > max_reads \
     || (reads == max_reads && cons > best_cons) \
     || (reads == max_reads && cons == best_cons && minq > best_minq) \
     || (reads == max_reads && cons == best_cons && minq == best_minq && (best_otu == "" || otu < best_otu))) {
        max_reads=reads; best_cons=cons; best_minq=minq; best_otu=otu; best_h=h;
    }
}
END{
    print best_cons;
}
"""

    result = subprocess.run(
        [
            "awk",
            "-v",
            f"ck={consolidated}",
            awk_script,
            str(cluster),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "1"


def test_consensus_parser_uses_last_otu_tag(tmp_path: Path) -> None:
    cluster = tmp_path / "cluster.fa"
    cluster.write_text(
        ">sample|OTUB_1|COI|reads-5|OTU=Consensus0-COI|OTU=OTUB_1-COI\n"
        "AAAA\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "consolidated.list"
    consolidated.write_text("", encoding="utf-8")

    awk_script = r"""
BEGIN{
    max_reads=-1; best_cons=-1; best_minq=-1e18; best_otu=""; best_h="";
    while ((getline k < ck) > 0) {
        gsub(/\r/, "", k);
        if (k ~ /\S/) consk[k]=1;
    }
    close(ck);
}
/^>/{
    h=$0; sub(/^>/,"",h);
    reads=0;
    if (h ~ /reads-[0-9]+/) {
        tmp=h; sub(/.*reads-/,"",tmp); gsub(/[^0-9].*/,"",tmp); reads=tmp+0;
    }
    otu="";
    tmp=h;
    while (match(tmp, /\|OTU=[^|]+/)) {
        otu=substr(tmp, RSTART+5, RLENGTH-5);
        tmp=substr(tmp, RSTART+RLENGTH);
    }
    if (otu == "") {
        n=split(h, p, "|");
        if (n>=2) {
            otu=p[2];
            if (n>=3 && p[3] != "" && otu !~ ("-" p[3] "$")) otu=otu "-" p[3];
        }
    }
    if (reads > max_reads || (reads == max_reads && (best_otu == "" || otu < best_otu))) {
        max_reads=reads; best_otu=otu; best_h=h;
    }
}
END{
    print best_otu;
}
"""

    result = subprocess.run(
        [
            "awk",
            "-v",
            f"ck={consolidated}",
            awk_script,
            str(cluster),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "OTUB_1-COI"
