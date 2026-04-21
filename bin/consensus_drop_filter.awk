BEGIN {
	FS = OFS = "\t"
	mode = MODE
	drop_file = DROP
	global_suffix_mode = ID_GLOBAL_SUFFIX_MODE
	if (global_suffix_mode == "") global_suffix_mode = "strict"
	if (mode != "keys" && mode != "ids") {
		print "ERROR: consensus_drop_filter.awk MODE must be keys or ids" > "/dev/stderr"
		exit 2
	}
	if (global_suffix_mode != "strict" && global_suffix_mode != "heuristic") {
		print "ERROR: consensus_drop_filter.awk ID_GLOBAL_SUFFIX_MODE must be strict or heuristic" > "/dev/stderr"
		exit 2
	}
	if (drop_file == "") {
		print "ERROR: consensus_drop_filter.awk DROP file path is required" > "/dev/stderr"
		exit 2
	}
	expected_argc = (mode == "keys" ? 3 : 4)
	if (ARGC != expected_argc) {
		print "ERROR: consensus_drop_filter.awk invalid input count for MODE=" mode \
		      " (expected " expected_argc - 1 " input files after script, got " ARGC - 1 ")" > "/dev/stderr"
		print "USAGE keys: awk -v MODE=keys -v DROP=<drop.tsv> -f consensus_drop_filter.awk <drop.tsv> <keys.tsv>" > "/dev/stderr"
		print "USAGE ids : awk -v MODE=ids  -v DROP=<drop.tsv> -f consensus_drop_filter.awk <drop.tsv> <ids.tsv> <ids.tsv>" > "/dev/stderr"
		exit 2
	}
	if (ARGV[1] != drop_file) {
		print "ERROR: consensus_drop_filter.awk DROP input must be the first file argument and must match DROP exactly" > "/dev/stderr"
		exit 2
	}
	file_no = 0
}

function trim_cr(v) {
	sub(/\r$/, "", v)
	return v
}

function derive_consensus_id(hdr,    n, a) {
	n = split(hdr, a, "|")
	if (n >= 2) return a[2] "_" a[1]
	return ""
}

function parse_otu_key(hdr,    n, a, i, tok, otu_explicit, otu_token, barcode, adapter, otu, suffix) {
	n = split(hdr, a, "|")
	otu_explicit = ""
	otu_token = ""
	barcode = ""
	adapter = ""
	for (i = 1; i <= n; i++) {
		tok = a[i]
		if (otu_explicit == "" && tok ~ /^OTU=/) {
			otu_explicit = substr(tok, 5)
		} else if (otu_token == "" && tok ~ /^OTUB_/) {
			otu_token = tok
		} else if (barcode == "" && tok ~ /^barcode=/) {
			barcode = substr(tok, 9)
		} else if (adapter == "" && tok ~ /^adapter=/) {
			adapter = substr(tok, 9)
		}
	}
	if (otu_explicit != "") return otu_explicit
	otu = otu_token
	if (otu == "") return ""
	if (adapter != "" && adapter != "NA" && adapter != "barcode") {
		barcode = adapter
	}
	if (barcode != "" && barcode != "NA" && barcode != "barcode") {
		suffix = "-" barcode
		if (length(otu) <= length(suffix) || substr(otu, length(otu) - length(suffix) + 1) != suffix) {
			otu = otu suffix
		}
	}
	return otu
}

function add_drop_entry(sample, otu) {
	if (otu == "") return
	if (sample != "") {
		drop_pair[sample FS otu] = 1
	} else {
		drop_global_otu[otu] = 1
	}
	add_headerless_fallback_ids(sample, otu)
}

function add_headerless_fallback_ids(sample, otu,    n, a, otu_short) {
	fallback_drop[otu] = 1
	n = split(otu, a, "-")
	otu_short = (n >= 1 ? a[1] : "")
	if (sample != "") {
		fallback_drop[otu "_" sample] = 1
		if (otu_short != "") {
			fallback_drop[otu_short "_" sample] = 1
		}
	} else if (global_suffix_mode == "heuristic" && otu_short != "") {
		fallback_prefix[otu_short "_"] = 1
	}
}

function should_drop_for_ids(sample, otu_key) {
	if (otu_key == "") return 0
	if (otu_key in drop_global_otu) return 1
	if ((sample FS otu_key) in drop_pair) return 1
	return 0
}

function is_id_like(v) {
	return (v ~ /^OTUB_[A-Za-z0-9._-]+(_[A-Za-z0-9._-]+)?$/)
}

function matches_global_fallback_prefix(v,    p) {
	for (p in fallback_prefix) {
		if (index(v, p) == 1 && length(v) > length(p)) return 1
	}
	return 0
}

FNR == 1 {
	file_no++
}

file_no == 1 {
	s = trim_cr($1)
	if (NF >= 2) {
		o = trim_cr($2)
		if (s != "" && o != "") add_drop_entry(s, o)
	} else {
		o = trim_cr($1)
		if (o != "") add_drop_entry("", o)
	}
	next
}

mode == "keys" {
	if (NF >= 2) {
		if (($1 FS $2) in drop_pair || ($2 in drop_global_otu)) next
		print
		next
	}
	if (NF == 1) {
		if ($1 in drop_global_otu) next
		print
	}
	next
}

file_no == 2 {
	line = trim_cr($0)
	if (line == "" || substr(line, 1, 1) != ">") next
	header = substr(line, 2)
	split(header, f, "|")
	sample = (f[1] != "" ? f[1] : "")
	otu_key = parse_otu_key(header)
	if (should_drop_for_ids(sample, otu_key)) {
		drop_header[line] = 1
		cid = derive_consensus_id(header)
		if (cid != "") drop_consensus_id[cid] = 1
	}
	next
}

file_no == 3 {
	line = trim_cr($0)
	if (line == "") next
	if (line in drop_header) next
	if (line in drop_consensus_id) next
	if (is_id_like(line) && (line in fallback_drop || matches_global_fallback_prefix(line))) next
	print
}
