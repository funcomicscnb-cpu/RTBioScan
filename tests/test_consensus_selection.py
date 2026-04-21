import subprocess
from pathlib import Path


def test_consolidated_first_selection(tmp_path: Path) -> None:
    cluster = tmp_path / "cluster.fa"
    cluster.write_text(
        ">sample|OTUB_1|COI|reads-10|OTU=OTUB_1-COI|minQ=10|consolidated=0\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-5|OTU=OTUB_2-COI|minQ=30|consolidated=1\n"
        "TTTT\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "consolidated.list"
    consolidated.write_text("", encoding="utf-8")

    awk_script = r"""
BEGIN{
    max_reads=-1; best_cons=-1; best_minq=-1e18; best_otu=""; best_h="";
    any_cons=0; rec_n=0;
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
    if (cons == 1) any_cons=1;
    rec_n++;
    rec_h[rec_n]=h; rec_otu[rec_n]=otu; rec_reads[rec_n]=reads; rec_cons[rec_n]=cons; rec_minq[rec_n]=minq;
}
END{
    best_set=0;
    if (any_cons) {
        for (i=1; i<=rec_n; i++) {
            if (rec_cons[i] == 1) {
                best_h=rec_h[i]; best_otu=rec_otu[i]; best_cons=rec_cons[i]; best_minq=rec_minq[i]; max_reads=rec_reads[i];
                best_set=1;
                break;
            }
        }
    }
    if (!best_set && rec_n >= 1) {
        best_h=rec_h[1]; best_otu=rec_otu[1]; best_cons=rec_cons[1]; best_minq=rec_minq[1]; max_reads=rec_reads[1];
        best_set=1;
    }
    for (i=1; i<=rec_n; i++) {
        if (any_cons && rec_cons[i] != 1) continue;
        reads=rec_reads[i]; cons=rec_cons[i]; minq=rec_minq[i]; otu=rec_otu[i]; h=rec_h[i];
        if (!best_set || reads > max_reads \
         || (reads == max_reads && cons > best_cons) \
         || (reads == max_reads && cons == best_cons && minq > best_minq) \
         || (reads == max_reads && cons == best_cons && minq == best_minq && (best_otu == "" || otu < best_otu))) {
            max_reads=reads; best_cons=cons; best_minq=minq; best_otu=otu; best_h=h;
            best_set=1;
        }
    }
    print best_otu;
}
"""

    result = subprocess.run(
        ["awk", "-v", f"ck={consolidated}", awk_script, str(cluster)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "OTUB_2-COI"


def test_reads_zero_cluster_selects_entry(tmp_path: Path) -> None:
    cluster = tmp_path / "cluster.fa"
    cluster.write_text(
        ">sample|OTUB_1|COI|reads-0|OTU=OTUB_1-COI|minQ=NA|consolidated=0\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-0|OTU=OTUB_2-COI|minQ=NA|consolidated=0\n"
        "TTTT\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "consolidated.list"
    consolidated.write_text("", encoding="utf-8")

    awk_script = r"""
BEGIN{
    max_reads=-1; best_cons=-1; best_minq=-1e18; best_otu=""; best_h="";
    any_cons=0; rec_n=0;
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
    if (cons == 1) any_cons=1;
    rec_n++;
    rec_h[rec_n]=h; rec_otu[rec_n]=otu; rec_reads[rec_n]=reads; rec_cons[rec_n]=cons; rec_minq[rec_n]=minq;
}
END{
    if (rec_n >= 1) {
        best_h=rec_h[1]; best_otu=rec_otu[1]; best_cons=rec_cons[1]; best_minq=rec_minq[1]; max_reads=rec_reads[1];
    }
    for (i=1; i<=rec_n; i++) {
        if (any_cons && rec_cons[i] != 1) continue;
        reads=rec_reads[i]; cons=rec_cons[i]; minq=rec_minq[i]; otu=rec_otu[i]; h=rec_h[i];
        if (reads > max_reads \
         || (reads == max_reads && cons > best_cons) \
         || (reads == max_reads && cons == best_cons && minq > best_minq) \
         || (reads == max_reads && cons == best_cons && minq == best_minq && (best_otu == "" || otu < best_otu))) {
            max_reads=reads; best_cons=cons; best_minq=minq; best_otu=otu; best_h=h;
        }
    }
    print best_h;
}
"""

    result = subprocess.run(
        ["awk", "-v", f"ck={consolidated}", awk_script, str(cluster)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip().startswith("sample|OTUB_")
