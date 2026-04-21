#!/usr/bin/env perl
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);
use File::Basename qw(basename dirname);
use File::Spec;

my ($pool_file, $reads_fasta, $min_reads, $min_qscore, $out_prefix, @rest) = @ARGV;
$min_reads = 10 if (!defined $min_reads || $min_reads !~ /^[0-9]+$/);
$min_qscore = 0 if (!defined $min_qscore || $min_qscore !~ /^[0-9]+([.][0-9]+)?$/);
$out_prefix = "selection" if (!defined $out_prefix || $out_prefix eq "");
my $confirm_min_count = 2;

my %opt = (
	assigned_ids => '',
	prune_unassigned => 0,
	keep_unassigned_top => 5,
	skip_prune => 0,
	emit_prune_stats => '',
	dropped_ids => '',
	ranking => 'qscore_first',
	max_selected_reads => '',
);
while (@rest) {
	my $arg = shift @rest;
	if ($arg eq '--assigned-ids') {
		$opt{assigned_ids} = shift(@rest) // '';
	} elsif ($arg eq '--prune-unassigned') {
		$opt{prune_unassigned} = 1;
	} elsif ($arg eq '--keep-unassigned-top') {
		$opt{keep_unassigned_top} = shift(@rest) // '';
	} elsif ($arg eq '--skip-prune') {
		$opt{skip_prune} = 1;
	} elsif ($arg eq '--emit-prune-stats') {
		$opt{emit_prune_stats} = shift(@rest) // '';
	} elsif ($arg eq '--dropped-ids') {
		$opt{dropped_ids} = shift(@rest) // '';
	} elsif ($arg eq '--ranking') {
		$opt{ranking} = shift(@rest) // '';
	} elsif ($arg eq '--max-selected-reads') {
		$opt{max_selected_reads} = shift(@rest) // '';
	} else {
		die "unknown argument: $arg\n";
	}
}
if ($opt{keep_unassigned_top} !~ /^[0-9]+$/) {
	$opt{keep_unassigned_top} = 5;
}
if ($opt{ranking} ne 'qscore_first' && $opt{ranking} ne 'count_then_qscore') {
	die "invalid --ranking '$opt{ranking}'\n";
}
if ($opt{max_selected_reads} ne '' && $opt{max_selected_reads} !~ /^[0-9]+$/) {
	die "invalid --max-selected-reads '$opt{max_selected_reads}'\n";
}
if ($opt{max_selected_reads} ne '' && $opt{max_selected_reads} < $min_reads) {
	die "invalid --max-selected-reads '$opt{max_selected_reads}' (must be >= min_reads $min_reads)\n";
}

my $out_ids      = "${out_prefix}.selected_ids.list";
my $out_reps     = "${out_prefix}.selected_reps.tsv";
my $out_reps_all = "${out_prefix}.reps_all.tsv";
my $out_meta     = "${out_prefix}.meta.tsv";
my $out_rank1    = "${out_prefix}.rank1.fasta";
my @cleanup_files;

my %prune_stats = (
	prune_requested => ($opt{prune_unassigned} ? 1 : 0),
	prune_applied => 0,
	reason => 'prune_disabled',
	total_clusters => 0,
	assigned_clusters => 0,
	unassigned_clusters => 0,
	kept_unassigned => 0,
	dropped_unassigned => 0,
	keep_unassigned_top => 0 + $opt{keep_unassigned_top},
);

END {
	for my $path (@cleanup_files) {
		next if !defined $path || $path eq '' || !-e $path;
		unlink($path);
	}
}

sub make_local_tmp_base {
	my ($prefix, $tag) = @_;
	my $dir = dirname($prefix);
	$dir = "." if !defined $dir || $dir eq "";
	my $name = basename($prefix);
	my $nonce = join(".", $$, time(), int(rand(1_000_000)));
	return File::Spec->catfile($dir, ".$name.$tag.$nonce");
}

sub write_prune_stats {
	my ($path, $stats_ref) = @_;
	return if !defined $path || $path eq '';
	open(my $S, ">", $path) or die "open $path: $!";
	for my $k (qw(prune_requested prune_applied reason total_clusters assigned_clusters unassigned_clusters kept_unassigned dropped_unassigned keep_unassigned_top)) {
		my $v = exists($stats_ref->{$k}) ? $stats_ref->{$k} : '';
		print $S "$k\t$v\n";
	}
	close($S);
}

sub write_empty_outputs {
	my ($meta_path, $ids_path, $reps_path, $reps_all_path, $stats_ref, $stats_path, $dropped_path) = @_;
	open(my $M, ">", $meta_path); print $M "0\tNA\t0\tnone\tNA\t0\n"; close($M);
	open(my $I, ">", $ids_path); close($I);
	open(my $R, ">", $reps_path); close($R);
	open(my $A, ">", $reps_all_path); close($A);
	if (defined $dropped_path && $dropped_path ne '' && (!-e $dropped_path || !-s $dropped_path)) {
		open(my $D, ">", $dropped_path); close($D);
	}
	write_prune_stats($stats_path, $stats_ref);
}

sub walk_qfiltered_reads {
	my ($reads_path, $qscore_ref, $cb) = @_;
	open(my $F, "<", $reads_path) or return 0;
	my ($hdr, $seq) = ("", "");
	my $flush = sub {
		return if $hdr eq "";
		my $name = $hdr;
		$name =~ s/^>//;
		$name =~ s/\s.*$//;
		my ($uuid) = split(/\|/, $name);
		return if !defined $uuid || !exists $qscore_ref->{$uuid};
		my $s = $seq;
		$s =~ s/\s+//g;
		return if $s eq "";
		$cb->($name, $uuid, $s, $qscore_ref->{$uuid});
	};
	while (my $line = <$F>) {
		if ($line =~ /^>/) {
			$flush->();
			$hdr = $line;
			$seq = "";
		} else {
			$seq .= $line;
		}
	}
	$flush->();
	close($F);
	return 1;
}

open(my $P, "<", $pool_file) or do {
	$prune_stats{reason} = 'pool_unreadable';
	write_empty_outputs($out_meta, $out_ids, $out_reps, $out_reps_all, \%prune_stats, $opt{emit_prune_stats}, $opt{dropped_ids});
	exit 0;
};

my %qscore;
while (my $line = <$P>) {
	chomp($line);
	next if $line eq "";
	my @f = split(/\t/, $line);
	my ($uuid, $q);
	if (@f >= 4) { $uuid = $f[0]; $q = $f[3]; }
	elsif (@f == 3) { $uuid = $f[0]; $q = $f[2]; }
	elsif (@f == 2) { $uuid = $f[0]; $q = $f[1]; }
	else { next; }
	next if !defined $uuid || $uuid eq "";
	next if !defined $q || $q eq "" || $q eq "NA";
	$q += 0;
	next if $q < $min_qscore;
	$qscore{$uuid} = $q;
}
close($P);

my %assigned_ids;
if ($opt{prune_unassigned} && !$opt{skip_prune}) {
	if (!defined $opt{assigned_ids} || $opt{assigned_ids} eq '' || !-e $opt{assigned_ids} || !-s $opt{assigned_ids}) {
		$prune_stats{reason} = 'missing_assigned_ids';
		warn "WARN: consensus cluster prune disabled (missing assigned IDs list)\n";
	} else {
		if (open my $AID, '<', $opt{assigned_ids}) {
			while (my $line = <$AID>) {
				chomp $line;
				next if $line =~ /^\s*$/;
				$assigned_ids{$line} = 1;
			}
			close $AID;
			$prune_stats{reason} = 'applied';
		} else {
			$prune_stats{reason} = 'assigned_ids_unreadable';
			warn "WARN: consensus cluster prune disabled (cannot open assigned IDs list: $opt{assigned_ids})\n";
		}
	}
} elsif ($opt{skip_prune}) {
	$prune_stats{reason} = 'skip_prune_flag';
}

my %seqs;
walk_qfiltered_reads($reads_fasta, \%qscore, sub {
	my ($name, $uuid, $s, $q) = @_;
	$seqs{$s}{count} = 0 if !defined $seqs{$s}{count};
	$seqs{$s}{count}++;
	if (%assigned_ids && exists $assigned_ids{$uuid}) {
		$seqs{$s}{assigned} = 1;
	}
	if (!defined $seqs{$s}{rep_q} || $q > $seqs{$s}{rep_q} || ($q == $seqs{$s}{rep_q} && $name lt $seqs{$s}{rep_hdr})) {
		$seqs{$s}{rep_q} = $q;
		$seqs{$s}{rep_hdr} = $name;
	}
}) or do {
	$prune_stats{reason} = 'reads_unreadable' if $prune_stats{reason} eq 'prune_disabled';
	write_empty_outputs($out_meta, $out_ids, $out_reps, $out_reps_all, \%prune_stats, $opt{emit_prune_stats}, $opt{dropped_ids});
	exit 0;
};

if (!%seqs) {
	$prune_stats{reason} = 'no_clusters_after_qfilter' if $prune_stats{reason} eq 'prune_disabled';
	write_empty_outputs($out_meta, $out_ids, $out_reps, $out_reps_all, \%prune_stats, $opt{emit_prune_stats}, $opt{dropped_ids});
	exit 0;
}

my @all_clusters = keys %seqs;
$prune_stats{total_clusters} = scalar @all_clusters;
my @assigned_clusters = grep { $seqs{$_}{assigned} } @all_clusters;
my @unassigned_clusters = grep { !$seqs{$_}{assigned} } @all_clusters;
$prune_stats{assigned_clusters} = scalar @assigned_clusters;
$prune_stats{unassigned_clusters} = scalar @unassigned_clusters;

my $prune_apply = ($opt{prune_unassigned} && !$opt{skip_prune} && $prune_stats{reason} eq 'applied') ? 1 : 0;
my @dropped_unassigned = ();
my %dropped_uuid = ();
my %dropped_cluster = ();
if ($prune_apply) {
	$prune_stats{prune_applied} = 1;
	my @ordered_unassigned = sort {
		$seqs{$b}{count} <=> $seqs{$a}{count}
		|| $seqs{$b}{rep_q} <=> $seqs{$a}{rep_q}
		|| $a cmp $b
	} @unassigned_clusters;
	my $keep_k = $opt{keep_unassigned_top};
	$keep_k = 0 if $keep_k < 0;
	if ($keep_k > @ordered_unassigned) {
		$keep_k = scalar @ordered_unassigned;
	}
	if ($keep_k <= $#ordered_unassigned) {
		@dropped_unassigned = @ordered_unassigned[$keep_k .. $#ordered_unassigned];
	}
	%dropped_cluster = map { $_ => 1 } @dropped_unassigned;
	my %keep = map { $_ => 1 } @assigned_clusters;
	for my $i (0 .. $keep_k - 1) {
		$keep{$ordered_unassigned[$i]} = 1 if defined $ordered_unassigned[$i];
	}
	for my $s (@all_clusters) {
		delete $seqs{$s} unless $keep{$s};
	}
	$prune_stats{kept_unassigned} = $keep_k;
	$prune_stats{dropped_unassigned} = scalar(@unassigned_clusters) - $keep_k;
}

if (!%seqs) {
	if (defined $opt{dropped_ids} && $opt{dropped_ids} ne '' && %dropped_cluster) {
		walk_qfiltered_reads($reads_fasta, \%qscore, sub {
			my ($name, $uuid, $s) = @_;
			$dropped_uuid{$uuid} = 1 if $dropped_cluster{$s};
		});
		open(my $D, ">", $opt{dropped_ids}) or die "open $opt{dropped_ids}: $!";
		for my $uuid (sort keys %dropped_uuid) {
			print $D "$uuid\n";
		}
		close($D);
	}
	$prune_stats{reason} = 'all_clusters_pruned' if $prune_apply;
	write_empty_outputs($out_meta, $out_ids, $out_reps, $out_reps_all, \%prune_stats, $opt{emit_prune_stats}, $opt{dropped_ids});
	exit 0;
}

for my $s (keys %seqs) {
	$seqs{$s}{hash} = md5_hex($s);
}

my @ordered = sort {
	($opt{ranking} eq 'count_then_qscore'
		? ($seqs{$b}{count} <=> $seqs{$a}{count}
			|| $seqs{$b}{rep_q} <=> $seqs{$a}{rep_q}
			|| $seqs{$a}{hash} cmp $seqs{$b}{hash})
		: ($seqs{$b}{rep_q} <=> $seqs{$a}{rep_q}
			|| $seqs{$a}{hash} cmp $seqs{$b}{hash}))
} keys %seqs;

my %rank;
my $idx = 0;
for my $s (@ordered) {
	$idx++;
	$rank{$s} = $idx;
	$seqs{$s}{rank} = $idx;
}

my @selected;
my $selected_total_reads = 0;
my $rank1_only = 0;
my $mode = "ranked_until_10";

my $rank1 = $ordered[0];
my $rank1_count = $seqs{$rank1}{count};
if ($rank1_count >= $min_reads) {
	@selected = ($rank1);
	$selected_total_reads = $rank1_count;
	$rank1_only = 1;
	$mode = "rank1_direct";
} else {
	for my $s (@ordered) {
		push @selected, $s;
		$selected_total_reads += $seqs{$s}{count};
		last if $selected_total_reads >= $min_reads;
	}
}

open(my $R, ">", $out_reps);
for my $s (@selected) {
	my $rep = $seqs{$s}{rep_hdr};
	my $rq  = $seqs{$s}{rep_q};
	my $rk  = $seqs{$s}{rank};
	my $cnt = $seqs{$s}{count};
	my $h   = $seqs{$s}{hash};
	print $R join("\t", $h, $rep, $rq, $rk, $cnt) . "\n";
}
close($R);

my %selected_hash = map { $seqs{$_}{hash} => 1 } @selected;
my %selected_cluster = map { $_ => 1 } @selected;
open(my $A, ">", $out_reps_all);
for my $s (@ordered) {
	my $rep = $seqs{$s}{rep_hdr};
	my $rq  = $seqs{$s}{rep_q};
	my $rk  = $seqs{$s}{rank};
	my $cnt = $seqs{$s}{count};
	my $h   = $seqs{$s}{hash};
	my $sel = $selected_hash{$h} ? 1 : 0;
	print $A join("\t", $h, $rep, $rq, $rk, $cnt, $sel) . "\n";
}
close($A);

my $selected_id_tmp_base = make_local_tmp_base($out_prefix, "selected_ids");
my (%selected_id_paths, %selected_id_fhs);
my @selected_id_lru = ();
my $selected_id_max_open = 32;
for my $s (@selected) {
	my $rk = $seqs{$s}{rank};
	my $tmp = sprintf("%s.%06d.tmp", $selected_id_tmp_base, $rk);
	$selected_id_paths{$s} = $tmp;
	push @cleanup_files, $tmp;
}

sub get_selected_id_append_fh {
	my ($cluster_key, $path_ref, $fh_ref, $lru_ref, $max_open) = @_;
	if (exists $fh_ref->{$cluster_key}) {
		@$lru_ref = grep { $_ ne $cluster_key } @$lru_ref;
		push @$lru_ref, $cluster_key;
		return $fh_ref->{$cluster_key};
	}
	if (@$lru_ref >= $max_open) {
		my $evict = shift @$lru_ref;
		if (defined $evict && exists $fh_ref->{$evict}) {
			close($fh_ref->{$evict});
			delete $fh_ref->{$evict};
		}
	}
	my $path = $path_ref->{$cluster_key};
	open(my $FH, ">>", $path) or die "open $path: $!";
	$fh_ref->{$cluster_key} = $FH;
	push @$lru_ref, $cluster_key;
	return $FH;
}

walk_qfiltered_reads($reads_fasta, \%qscore, sub {
	my ($name, $uuid, $s) = @_;
	if ($selected_cluster{$s}) {
		my $FH = get_selected_id_append_fh($s, \%selected_id_paths, \%selected_id_fhs, \@selected_id_lru, $selected_id_max_open);
		print {$FH} "$name\n";
	}
	if (defined $opt{dropped_ids} && $opt{dropped_ids} ne '' && $dropped_cluster{$s}) {
		$dropped_uuid{$uuid} = 1;
	}
}) or die "open $reads_fasta: $!";

for my $s (keys %selected_id_fhs) {
	close($selected_id_fhs{$s});
}

open(my $I, ">", $out_ids) or die "open $out_ids: $!";
my %emitted_counts;
my $selected_reads_remaining = ($opt{max_selected_reads} ne '' ? $opt{max_selected_reads} : '');
for my $s (@selected) {
	my $emit_count = 0;
	my $tmp_path = $selected_id_paths{$s};
	if ($selected_reads_remaining eq '' || $selected_reads_remaining > 0) {
		open(my $TMP, "<", $tmp_path) or die "open $tmp_path: $!";
		while (my $line = <$TMP>) {
			last if $selected_reads_remaining ne '' && $selected_reads_remaining <= 0;
			print $I $line;
			$emit_count++;
			$selected_reads_remaining-- if $selected_reads_remaining ne '';
		}
		close($TMP);
	}
	$emitted_counts{$s} = $emit_count;
	unlink($tmp_path);
}
close($I);

if (defined $opt{dropped_ids} && $opt{dropped_ids} ne '') {
	open(my $D, ">", $opt{dropped_ids}) or die "open $opt{dropped_ids}: $!";
	for my $uuid (sort keys %dropped_uuid) {
		print $D "$uuid\n";
	}
	close($D);
}

my $emitted_total_reads = 0;
my $min_repq;
my $min_repq_confirmed;
my $confirmed_seqs = 0;
for my $s (@selected) {
	my $cnt = $emitted_counts{$s} // 0;
	next if $cnt <= 0;
	my $rq = $seqs{$s}{rep_q};
	$emitted_total_reads += $cnt;
	$min_repq = $rq if !defined $min_repq || $rq < $min_repq;
	next if $cnt < $confirm_min_count;
	$min_repq_confirmed = $rq if !defined $min_repq_confirmed || $rq < $min_repq_confirmed;
	$confirmed_seqs++;
}

if ($rank1_only) {
	open(my $O, ">", $out_rank1);
	print $O ">rank1_seq\n$rank1\n";
	close($O);
}

my $min_repq_out = defined $min_repq ? $min_repq : "NA";
open(my $M, ">", $out_meta);
my $min_repq_conf_out = defined $min_repq_confirmed ? $min_repq_confirmed : "NA";
print $M join("\t", $emitted_total_reads, $min_repq_out, $rank1_only, $mode, $min_repq_conf_out, $confirmed_seqs) . "\n";
close($M);
write_prune_stats($opt{emit_prune_stats}, \%prune_stats);
