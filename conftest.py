import os

# Force C numeric locale for all test subprocesses.
# On Linux with a non-English locale (e.g. es_ES.UTF-8), awk uses commas as
# decimal separators which breaks float comparisons in pipeline scripts, and
# bash $EPOCHREALTIME is emitted as "1234567890,123456" causing arithmetic
# failures in otu_refine_blastreport_parallel.sh.
os.environ["LC_NUMERIC"] = "C"
os.environ["LANG"] = "en_US.UTF-8"
os.environ["LC_ALL"] = ""  # ensure LC_ALL does not override LC_NUMERIC
