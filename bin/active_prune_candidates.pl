#!/usr/bin/env perl
use strict;
use warnings;

use File::Basename qw(dirname);
use File::Path qw(make_path);
use Getopt::Long qw(GetOptions);

# NOTE:
# - Active scope is round-local only.
# - Missing/unreadable round-local inputs yield empty outputs with warnings.

sub usage {
    return <<"USAGE";
usage: $0 \\
  --otu-members-round <file> \\
  --otu-sizes-round <file> \\
  --size-streak-ids <file> \\
  --otu-blast-min-members <int> \\
  --otu-blast-filter-mode <off|observe|enforce> \\
  --otu-blast-filter-skip-rounds <none|all|int>=none \\
  --force-use-filtered <0|1|true|false> \\
  --round-index-file <file> \\
  --round-barcode <id> \\
  --eligible-counts <file> \\
  --effective-mode-helper <path-to-otu_blast_effective_mode.sh> \\
  --out-size-streak <file> \\
  --out-size-candidates <file> \\
  --out-all <file> \\
  --out-counts <file>
USAGE
}

my %opt;
GetOptions(
    'otu-members-round=s'           => \$opt{otu_members_round},
    'otu-sizes-round=s'             => \$opt{otu_sizes_round},
    'size-streak-ids=s'             => \$opt{size_streak_ids},
    'otu-blast-min-members=i'       => \$opt{otu_blast_min_members},
    'otu-blast-filter-mode=s'       => \$opt{otu_blast_filter_mode},
    'otu-blast-filter-skip-rounds=s'=> \$opt{otu_blast_filter_skip_rounds},
    'force-use-filtered=s'          => \$opt{force_use_filtered},
    'round-index-file=s'            => \$opt{round_index_file},
    'round-barcode=s'               => \$opt{round_barcode},
    'eligible-counts=s'             => \$opt{eligible_counts},
    'effective-mode-helper=s'       => \$opt{effective_mode_helper},
    'out-size-streak=s'             => \$opt{out_size_streak},
    'out-size-candidates=s'         => \$opt{out_size_candidates},
    'out-all=s'                     => \$opt{out_all},
    'out-counts=s'                  => \$opt{out_counts},
) or die usage();

for my $required (
    qw(
      otu_members_round otu_sizes_round size_streak_ids
      otu_blast_min_members otu_blast_filter_mode otu_blast_filter_skip_rounds
      round_index_file round_barcode effective_mode_helper
      out_size_streak out_size_candidates out_all out_counts
      )
  )
{
    if (!defined $opt{$required} || $opt{$required} eq q{}) {
        die "missing required --$required\n" . usage();
    }
}

if ($opt{otu_blast_min_members} < 0) {
    die "--otu-blast-min-members must be >= 0\n";
}

sub ensure_parent_dir {
    my ($path) = @_;
    my $dir = dirname($path);
    return if !defined $dir || $dir eq q{} || $dir eq q{.};
    make_path($dir) if !-d $dir;
}

sub write_id_list {
    my ($path, $set_ref) = @_;
    ensure_parent_dir($path);
    open my $out_fh, '>', $path or die "cannot write $path: $!";
    for my $id (sort keys %{$set_ref}) {
        next if !defined $id || $id eq q{};
        print {$out_fh} $id, "\n";
    }
    close $out_fh or die "cannot close $path: $!";
}

sub write_counts {
    my ($path, $pairs_ref) = @_;
    ensure_parent_dir($path);
    open my $out_fh, '>', $path or die "cannot write $path: $!";
    for my $pair (@{$pairs_ref}) {
        print {$out_fh} $pair->[0], "\t", $pair->[1], "\n";
    }
    close $out_fh or die "cannot close $path: $!";
}

sub warn_msg {
    my ($msg) = @_;
    print STDERR "WARN: $msg\n";
}

sub info_msg {
    my ($msg) = @_;
    print STDERR "INFO: $msg\n";
}

sub trim_text {
    my ($v) = @_;
    return q{} if !defined $v;
    $v =~ s/^\s+//;
    $v =~ s/\s+$//;
    return $v;
}

sub header_index {
    my ($cols_ref, @names) = @_;
    my %idx;
    for my $i (0 .. $#{$cols_ref}) {
        $idx{$cols_ref->[$i]} = $i;
    }
    for my $name (@names) {
        return $idx{$name} if exists $idx{$name};
    }
    return undef;
}

sub read_eligible_counts {
    my ($path) = @_;
    my %out;
    return \%out if !defined $path || $path eq q{} || !-s $path;
    open my $fh, '<', $path or do {
        warn_msg("active_prune_candidates: cannot read eligible counts file: $path");
        return \%out;
    };
    my $hdr = <$fh>;
    if (defined $hdr) {
        chomp $hdr;
        my @cols = split /\t/, $hdr, -1;
        my $otu_idx = header_index(\@cols, 'otu_key', 'otu_id', 'OTU_id');
        my $cnt_idx = header_index(\@cols, 'eligible_pool_count', 'eligible_count', 'count');
        if (defined $otu_idx && defined $cnt_idx) {
            while (my $line = <$fh>) {
                chomp $line;
                next if trim_text($line) eq q{};
                my @f = split /\t/, $line, -1;
                next if $otu_idx > $#f || $cnt_idx > $#f;
                my $otu = trim_text($f[$otu_idx]);
                my $cnt = trim_text($f[$cnt_idx]);
                next if $otu eq q{} || uc($otu) eq 'NA';
                next unless $cnt =~ /^\d+$/;
                $out{$otu} = 0 + $cnt;
            }
        }
    }
    close $fh;
    return \%out;
}

sub read_size_streak_ids {
    my ($path) = @_;
    my %ids;
    if (!-s $path) {
        warn_msg("active_prune_candidates: size-streak IDs file missing/empty: $path");
        return \%ids;
    }
    open my $fh, '<', $path or do {
        warn_msg("active_prune_candidates: cannot read size-streak IDs file: $path");
        return \%ids;
    };
    while (my $line = <$fh>) {
        chomp $line;
        next if trim_text($line) eq q{};
        my ($id) = split /\t/, $line, 2;
        $id = trim_text($id);
        next if $id eq q{};
        $ids{$id} = 1;
    }
    close $fh;
    return \%ids;
}

sub read_round_index {
    my ($path, $round_barcode) = @_;
    if (!-s $path) {
        warn_msg("active_prune_candidates: round index file missing/empty: $path");
        return;
    }
    open my $fh, '<', $path or do {
        warn_msg("active_prune_candidates: cannot read round index file: $path");
        return;
    };
    my $idx;
    while (my $line = <$fh>) {
        chomp $line;
        next if trim_text($line) eq q{};
        my ($rb, $value) = split /\t/, $line, 3;
        next if !defined $rb || !defined $value;
        if ($rb eq $round_barcode) {
            $idx = trim_text($value);
            last;
        }
    }
    close $fh;
    if (!defined $idx || $idx !~ /^\d+$/ || $idx < 1) {
        warn_msg(
            "active_prune_candidates: missing/invalid round index for round_barcode=$round_barcode"
        );
        return;
    }
    return $idx;
}

sub parse_effective_mode {
    my ($helper_path, $configured_mode, $skip_rounds, $round_index) = @_;
    my %kv = (
        effective_mode => 'off',
        reason         => 'unavailable',
    );
    if (!-f $helper_path) {
        warn_msg("active_prune_candidates: effective mode helper not found: $helper_path");
        return \%kv;
    }
    my @cmd = ('bash', $helper_path, $configured_mode, $skip_rounds, "$round_index");
    open my $fh, '-|', @cmd or do {
        warn_msg("active_prune_candidates: failed to run effective mode helper");
        return \%kv;
    };
    while (my $line = <$fh>) {
        chomp $line;
        next if trim_text($line) eq q{};
        my ($k, $v) = split /\t/, $line, 2;
        next if !defined $k;
        $kv{$k} = defined $v ? $v : q{};
    }
    close $fh;
    if ($? != 0) {
        warn_msg("active_prune_candidates: effective mode helper returned non-zero exit status");
        $kv{effective_mode} = 'off';
        $kv{reason}         = 'helper_failed';
    }
    if (
        !defined $kv{effective_mode}
        || $kv{effective_mode} !~ /^(?:off|observe|enforce)$/
      )
    {
        warn_msg("active_prune_candidates: invalid effective mode from helper");
        $kv{effective_mode} = 'off';
        $kv{reason}         = 'invalid_helper_output';
    }
    return \%kv;
}

sub normalize_skip_rounds {
    my ($raw) = @_;
    my $v = trim_text($raw);
    $v = lc $v;
    return 'none' if $v eq q{} || $v eq '0';
    return $v;
}

sub resolve_size_streak_policy {
    my (%args) = @_;
    my $skip_rounds = normalize_skip_rounds($args{skip_rounds});
    my $round_index = read_round_index($args{round_index_file}, $args{round_barcode});

    my $effective_mode = 'off';
    my $effective_reason = 'missing_round_index';
    my $in_size_streak_window = 0;

    if (defined $round_index) {
        my $mode_ref = parse_effective_mode(
            $args{effective_mode_helper},
            $args{configured_mode},
            $skip_rounds,
            $round_index,
        );
        $effective_mode = $mode_ref->{effective_mode};
        $effective_reason = defined $mode_ref->{reason} ? $mode_ref->{reason} : 'unknown';
        $in_size_streak_window = (
            $effective_mode eq 'off'
                && ($effective_reason eq 'within_skip_window' || $effective_reason eq 'skip_all_rounds')
        ) ? 1 : 0;
    } elsif ($skip_rounds eq 'all') {
        # `all` does not need round index; treat as always inside skip window.
        $effective_mode = 'off';
        $effective_reason = 'skip_all_rounds_no_index';
        $in_size_streak_window = 1;
    }

    if ($args{otu_blast_min_members} <= 0) {
        $in_size_streak_window = 0;
    }

    if ($args{force_use_filtered} && $effective_mode ne 'off') {
        $effective_mode = 'enforce';
        $effective_reason = 'force_use_filtered';
        $in_size_streak_window = 0;
    }

    my $size_streak_disabled = $in_size_streak_window ? 0 : 1;
    my $round_index_out = defined $round_index ? $round_index : 'NA';

    return {
        effective_mode      => $effective_mode,
        effective_reason    => $effective_reason,
        in_size_streak_window => $in_size_streak_window,
        size_streak_disabled  => $size_streak_disabled,
        round_index         => $round_index_out,
        skip_rounds         => $skip_rounds,
    };
}

sub read_two_col_tsv_with_status {
    my ($path, $key_name, $value_name) = @_;
    if (!defined $path || trim_text($path) eq q{}) {
        return { status => 'missing', rows => [] };
    }
    if (!-e $path) {
        return { status => 'missing', rows => [] };
    }
    if (!-r $path) {
        return { status => 'unreadable', rows => [] };
    }

    open my $fh, '<', $path or return { status => 'unreadable', rows => [] };
    my $first = <$fh>;
    if (!defined $first) {
        close $fh;
        return { status => 'empty', rows => [] };
    }
    chomp $first;
    my @first_cols = split /\t/, $first, -1;
    my $has_header = 0;
    my ($ki, $vi) = (0, 1);
    my %hidx;
    for my $i (0 .. $#first_cols) {
        my $k = trim_text($first_cols[$i]);
        $k = lc $k;
        $hidx{$k} = $i;
    }
    if (exists $hidx{lc $key_name} && exists $hidx{lc $value_name}) {
        $has_header = 1;
        $ki = $hidx{lc $key_name};
        $vi = $hidx{lc $value_name};
    }
    my @rows = ();
    if (!$has_header) {
        if (@first_cols >= 2) {
            push @rows, [ @first_cols[ 0, 1 ] ];
        } elsif (trim_text($first) ne q{}) {
            close $fh;
            return { status => 'invalid', rows => [] };
        }
    }
    while (my $line = <$fh>) {
        chomp $line;
        next if trim_text($line) eq q{};
        my @f = split /\t/, $line, -1;
        if ($has_header) {
            next if $ki > $#f || $vi > $#f;
            push @rows, [ $f[$ki], $f[$vi] ];
        } else {
            if ($#f < 1) {
                close $fh;
                return { status => 'invalid', rows => [] };
            }
            push @rows, [ $f[0], $f[1] ];
        }
    }
    close $fh;
    if (!@rows) {
        return { status => 'empty', rows => [] };
    }
    return { status => 'ok', rows => \@rows };
}

sub read_active_reads_from_members_rows {
    my ($rows_ref) = @_;
    my %ids;
    for my $row (@{$rows_ref}) {
        my (undef, $rid) = @{$row};
        $rid = trim_text($rid);
        next if $rid eq q{};
        $ids{$rid} = 1;
    }
    return \%ids;
}

sub select_scope_inputs {
    my (%args) = @_;
    my $round_members_ref = read_two_col_tsv_with_status(
        $args{otu_members_round}, 'otu_id', 'read_id'
    );
    my $round_sizes_ref = read_two_col_tsv_with_status(
        $args{otu_sizes_round}, 'otu_id', 'size'
    );
    if ($round_members_ref->{status} eq 'ok' || $round_members_ref->{status} eq 'empty') {
        if ($round_sizes_ref->{status} ne 'ok' && $round_sizes_ref->{status} ne 'empty') {
            warn_msg(
                "active_prune_candidates: round-local sizes unavailable (status=$round_sizes_ref->{status}); using empty size rows in round scope"
            );
        }
        return {
            active_scope      => 'round',
            active_scope_reason => ($round_members_ref->{status} eq 'ok' ? 'round_local_ok' : 'round_local_empty'),
            members_rows_ref  => $round_members_ref->{rows},
            sizes_rows_ref    => ($round_sizes_ref->{status} eq 'ok' ? $round_sizes_ref->{rows} : []),
            size_streak_input_status => $round_sizes_ref->{status},
            active_reads_ref  => read_active_reads_from_members_rows($round_members_ref->{rows}),
        };
    }

    warn_msg(
        "active_prune_candidates: round-local members unavailable (status=$round_members_ref->{status}); using empty round-local scope"
    );
    info_msg(
        "active_prune_candidates: round-local members missing/unreadable; outputs will be empty for this round"
    );
    return {
        active_scope      => 'round',
        active_scope_reason => 'round_local_unavailable',
        members_rows_ref  => [],
        sizes_rows_ref    => [],
        size_streak_input_status => $round_sizes_ref->{status},
        active_reads_ref  => {},
    };
}

my $scope_ref = select_scope_inputs(
    otu_members_round    => $opt{otu_members_round},
    otu_sizes_round      => $opt{otu_sizes_round},
);
my $active_reads_ref = $scope_ref->{active_reads_ref};
my $size_streak_raw_ref = read_size_streak_ids($opt{size_streak_ids});
my $eligible_counts_ref = read_eligible_counts($opt{eligible_counts});

my %size_streak_active = map { $_ => 1 } grep { exists $active_reads_ref->{$_} } keys %{$size_streak_raw_ref};

my $policy_ref = resolve_size_streak_policy(
    configured_mode      => $opt{otu_blast_filter_mode},
    skip_rounds          => $opt{otu_blast_filter_skip_rounds},
    round_index_file     => $opt{round_index_file},
    round_barcode        => $opt{round_barcode},
    effective_mode_helper=> $opt{effective_mode_helper},
    otu_blast_min_members=> $opt{otu_blast_min_members},
    force_use_filtered   => (defined $opt{force_use_filtered} && $opt{force_use_filtered} =~ /^(?:1|true|TRUE|yes|YES)$/) ? 1 : 0,
);

my %size_round_active;
my $size_possible = 0;
my $size_round_candidates = 'NA';
my $size_eligible_candidates = 'NA';
if (defined $opt{otu_blast_min_members} && $opt{otu_blast_min_members} =~ /^[0-9]+$/) {
    if ($opt{otu_blast_min_members} >= 2 && $scope_ref->{size_streak_input_status} eq 'ok') {
        $size_possible = 1;
    }
}
if ($size_possible) {
    my $sizes_rows_ref = $scope_ref->{sizes_rows_ref};
    my %size_streak_otus;
    $size_round_candidates = 0;
    $size_eligible_candidates = 0;
    for my $row (@{$sizes_rows_ref}) {
        my ($otu, $size) = @{$row};
        $otu = trim_text($otu);
        $size = trim_text($size);
        next if $otu eq q{} || $size !~ /^\d+$/;
        if ($size < $opt{otu_blast_min_members}) {
            $size_streak_otus{$otu} = 1;
            $size_round_candidates++;
            if (!(exists $eligible_counts_ref->{$otu} && $eligible_counts_ref->{$otu} >= $opt{otu_blast_min_members})) {
                $size_eligible_candidates++;
            }
        }
    }
    my $members_rows_ref = $scope_ref->{members_rows_ref};
    for my $row (@{$members_rows_ref}) {
        my ($otu, $rid) = @{$row};
        $otu = trim_text($otu);
        $rid = trim_text($rid);
        next if $otu eq q{} || $rid eq q{};
        next if !exists $size_streak_otus{$otu};
        next if !exists $active_reads_ref->{$rid};
        $size_round_active{$rid} = 1;
    }
}

if (
    !$policy_ref->{in_size_streak_window}
    && (
        $policy_ref->{effective_mode} eq 'enforce'
        || !defined $policy_ref->{round_index}
        || $policy_ref->{round_index} eq 'NA'
    )
) {
    %size_round_active = ();
}

my %all_active = (%size_streak_active, %size_round_active);

my $size_streak_possible = $size_possible ? 1 : 0;
my $size_streak_applied = ($size_streak_possible && $policy_ref->{effective_mode} eq 'enforce' && !$policy_ref->{in_size_streak_window}) ? 1 : 0;

write_id_list($opt{out_size_streak}, \%size_streak_active);
write_id_list($opt{out_size_candidates}, \%size_round_active);
write_id_list($opt{out_all}, \%all_active);

my @count_pairs = (
    [ 'active_scope', $scope_ref->{active_scope} ],
    [ 'active_scope_reason', $scope_ref->{active_scope_reason} ],
    [ 'size_streak_input_status', $scope_ref->{size_streak_input_status} ],
    [ 'active_total', scalar keys %{$active_reads_ref} ],
    [ 'size_streak_active', scalar keys %size_streak_active ],
    [ 'size_streak_candidates', scalar keys %size_round_active ],
    [ 'union', scalar keys %all_active ],
    [ 'size_streak_possible', $size_streak_possible ],
    [ 'size_streak_applied', $size_streak_applied ],
    [ 'size_streak_disabled', $policy_ref->{size_streak_disabled} ],
    [ 'effective_mode', $policy_ref->{effective_mode} ],
    [ 'effective_reason', $policy_ref->{effective_reason} ],
    [ 'round_index', $policy_ref->{round_index} ],
    [ 'size_streak_round_candidates', $size_round_candidates ],
    [ 'size_streak_eligible_candidates', $size_eligible_candidates ],
);
write_counts($opt{out_counts}, \@count_pairs);

exit 0;
