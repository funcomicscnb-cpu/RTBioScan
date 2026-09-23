package RTBioScan::R4DCumulative;
# R4-I2 cumulative reporting-state generations (protocol: docs/output.md).
#
# The three cumulative BLAST OTU products of one barcode form one generation:
#   <bc>_blast_otu_reporting_v1.tsv  <bc>_blast_otu_pretax_rpt.txt
#   <bc>_blast_otu_noadapter_rpt.txt
# Its members are immutable files `<name>.gen-<generation>` next to the public
# names. The sealed commit record `<bc>_blast_otu_cumulative.commit`, replaced
# by a single rename, is the only authority naming the current generation:
# readers open only the members it names, after checking their size and
# SHA-256. The public names are compatibility projections. A publication
# retracts every public name that changes before it projects any new one, so
# the public names present at any instant hold one generation (a name can be
# briefly absent, never mixed); no portable operation switches three names at
# once, so they are never the authority.
#
# A backup destination (results/ongoing/state/<id>, results/current/state/<id>/tables)
# holds a replica of a generation, published by publish_backup with the same
# protocol: the same record kind and generation id, every member (the
# reporting sidecar included), and projections of the two public tables only.
# A restore (restart_mode=restore, restore_cli) installs the snapshot's
# generation into `_state` with its authority; an older, incomplete backup is
# first proven and upgraded in place, or the restore is refused.
#
# Without a record, only three states of a pipeline state directory (`_state`)
# are legitimate: a pristine first run (no public name, no member, no governed
# residue), a complete, content-consistent pre-I2 (R4-D) snapshot, and an
# authentic pre-R4-D snapshot, which is sealed on first sight (below). Anything
# else -- a subset of the public names, a generation member, temporary,
# journal, rollback or lock residue of an interrupted, in-progress or abandoned
# publication or copy (governed_residue), a deleted record -- fails closed, and
# the publisher refuses to write over it (classify_publication). Other
# directories are governed once they hold an R4-I2 artifact; before that, their
# tables are ordinary files. While a restart (reset or restore) of the state
# is applied (or was interrupted), its `_state` fails closed (restart_fence).
#
# Pre-R4-D compatibility (an existing outdir after the upgrade). The pre-R4-D
# pipeline (R4-C 111c1667; main 71c8de1 is identical here) creates the public
# and no-adapter tables in `_state` from the first reported round's tables and
# appends every later round's rows, after copying each round's own tables into
# its round directory `<state root>/<round_barcode>/`; `_state/round_index.tsv`
# orders the rounds and `_state/done_pod5.txt` lists the completed ones. When a
# record-less `_state` is exactly that layout and its tables are the ordered
# concatenation of the round tables of one prefix of the ledger (every
# completed round included), it is sealed once, under the publication lock:
# members `<name>.gen-<L>` (hard links; the legacy bytes are never rewritten)
# and a legacy record `#RTB-PRE-R4D-LEGACY` binding their sizes and SHA-256,
# committed by one rename. From then on it is a committed generation without a
# reporting sidecar (R4-D metrics unavailable); the first complete publication
# replaces it by one rename and keeps it as its predecessor for one further
# commit. A backup of it is a replica of that legacy generation (its record
# and both members), which a restore brings back into `_state`; an earlier
# candidate's `#RTB-PRE-R4D-LEGACY-BACKUP` generation is also the whole legacy
# generation. No other record-less `_state` is ever sealed, and results copies
# establish no authority by themselves: a restore proves them against the live
# round ledger and round tables before it wipes them (restore_cli).
use strict;
use warnings;
use Digest::SHA ();
use Errno ();
use Fcntl qw(O_RDONLY O_RDWR O_CREAT LOCK_EX);
use File::Basename ();
use File::Spec ();
use File::Temp ();
use IO::Handle ();
use POSIX ();

our @PRODUCTS = qw(reporting public noadapter);
our @BACKUP_PRODUCTS = qw(public noadapter);
# Record kinds: header, the products its entries name (in order) and the
# products it has members for. A legacy generation never had a reporting
# sidecar. Backups are replicas of the source's kind. `backup` (entries for the
# three products, members for the two public tables only) and `legacy-backup`
# are the backup formats of earlier candidates: still read, and a
# `legacy-backup` generation (complete: a legacy generation has two products)
# is replicated as it is, but no backup is written as `backup` again; such a
# generation is completed from its bound sidecar bytes before it is trusted as
# state (adopt_backup_record, restore_cli).
our %KIND = (
    state           => { header => "#RTB-R4D-CUMULATIVE\t1\n",        products => [@PRODUCTS],        members => [@PRODUCTS] },
    backup          => { header => "#RTB-R4D-CUMULATIVE-BACKUP\t1\n", products => [@PRODUCTS],        members => [@BACKUP_PRODUCTS] },
    legacy          => { header => "#RTB-PRE-R4D-LEGACY\t1\n",        products => [@BACKUP_PRODUCTS], members => [@BACKUP_PRODUCTS] },
    'legacy-backup' => { header => "#RTB-PRE-R4D-LEGACY-BACKUP\t1\n", products => [@BACKUP_PRODUCTS], members => [@BACKUP_PRODUCTS] },
);
our %HEADER = map { $_ => $KIND{$_}{header} } keys %KIND;
my %SUFFIX = (
    reporting => '_blast_otu_reporting_v1.tsv',
    public    => '_blast_otu_pretax_rpt.txt',
    noadapter => '_blast_otu_noadapter_rpt.txt',
);
# bin/ of this checkout, for the lazily loaded R4-D helper (legacy validation).
our $BIN = File::Spec->rel2abs(File::Basename::dirname(__FILE__) . '/../..');

sub fail { die "R4-D cumulative: @_\n" }

sub names {
    my ($bc) = @_;
    return { map { $_ => "$bc$SUFFIX{$_}" } @PRODUCTS };
}
sub record_path { my ($dir, $bc) = @_; return "$dir/${bc}_blast_otu_cumulative.commit"; }
sub lock_path   { my ($dir, $bc) = @_; return "$dir/${bc}_blast_otu_cumulative.lock"; }
sub generation_path { my ($dir, $name, $generation) = @_; return "$dir/$name.gen-$generation"; }

# A public cumulative path -> (dir, barcode, product); empty otherwise.
sub split_live_path {
    my ($path) = @_;
    return unless defined($path) && $path ne '';
    my ($dir, $base) = $path =~ m{\A(.*)/([^/]+)\z} ? ($1 eq '' ? '/' : $1, $2) : ('.', $path);
    for my $p (@PRODUCTS) {
        return ($dir, $1, $p) if $base =~ /\A(.+)\Q$SUFFIX{$p}\E\z/;
    }
    return;
}

sub same_file {
    my ($left, $right) = @_;
    my @x = lstat $left or return 0;
    my @y = lstat $right or return 0;
    return $x[0] == $y[0] && $x[1] == $y[1] ? 1 : 0;
}

sub sha_of { my ($path) = @_; return eval { Digest::SHA->new(256)->addfile($path, 'b')->hexdigest }; }

sub file_matches {
    my ($path, $size, $sha) = @_;
    my @st = lstat $path;
    return 0 unless @st && -f _ && $st[7] == $size;
    my $got = sha_of($path);
    return defined($got) && $got eq $sha ? 1 : 0;
}

# entries: product => [size, sha256] for the products of the kind. The
# generation id is the digest of the product lines, so identical content always
# yields the same id, for a generation and for its backups alike. A legacy
# record also seals the round provenance it was validated against.
sub record_text {
    my ($bc, $entries, $previous, $kind, $provenance) = @_;
    my $spec = $KIND{ $kind // 'state' } or fail("unknown commit record kind $kind");
    my $n = names($bc);
    my $products = join('', map { join("\t", $_, $n->{$_}, @{ $entries->{$_} }) . "\n" } @{ $spec->{products} });
    my $generation = Digest::SHA::sha256_hex($products);
    my $head = $spec->{header} . "generation\t$generation\nprevious\t" . ($previous // 'NA') . "\n" . $products
        . (($kind // '') eq 'legacy' ? "provenance\t$provenance->{rounds}\t$provenance->{sha}\n" : '');
    return ($head . "#END\t" . Digest::SHA::sha256_hex($head) . "\n", $generation);
}

sub parse_record_text {
    my ($text, $bc, $path) = @_;
    my $n = names($bc);
    my ($kind) = defined($text) && $text =~ /\A([^\n]*\n)/ ? grep { $KIND{$_}{header} eq $1 } sort keys %KIND : ();
    fail("invalid commit record $path") unless defined $kind;
    my $count = @{ $KIND{$kind}{products} };
    my $provenance = $kind eq 'legacy' ? "provenance\t([1-9][0-9]{0,8})\t([0-9a-f]{64})\n" : '()()';
    fail("invalid commit record $path")
        unless $text =~ /\A(\Q$KIND{$kind}{header}\Egeneration\t([0-9a-f]{64})\nprevious\t([0-9a-f]{64}|NA)\n((?:[^\n]*\n){$count})$provenance)#END\t([0-9a-f]{64})\n\z/;
    my ($head, $generation, $previous, $products, $rounds, $prov, $seal) = ($1, $2, $3, $4, $5, $6, $7);
    fail("commit record $path fails its seal") unless Digest::SHA::sha256_hex($head) eq $seal;
    fail("commit record $path names a generation that does not match its entries")
        unless Digest::SHA::sha256_hex($products) eq $generation;
    my @lines = split /\n/, $products;
    my @want = @{ $KIND{$kind}{products} };
    my %entries;
    for my $i (0 .. $#want) {
        my @v = split /\t/, $lines[$i], -1;
        fail("invalid commit record entry in $path")
            unless @v == 4 && $v[0] eq $want[$i] && $v[1] eq $n->{ $v[0] }
                && $v[2] =~ /\A(?:0|[1-9][0-9]{0,17})\z/ && $v[3] =~ /\A[0-9a-f]{64}\z/;
        $entries{ $v[0] } = { name => $v[1], size => 0 + $v[2], sha => $v[3] };
    }
    return { kind => $kind, generation => $generation, previous => ($previous eq 'NA' ? undef : $previous),
             entries => \%entries, text => $text,
             ($kind eq 'legacy' ? (provenance => { rounds => 0 + $rounds, sha => $prov }) : ()) };
}

sub parse_record {
    my ($path, $bc) = @_;
    open my $f, '<:raw', $path or fail("cannot read commit record $path: $!");
    my $text = do { local $/; <$f> };
    close $f;
    return parse_record_text($text, $bc, $path);
}

sub dir_entries {
    my ($dir) = @_;
    opendir(my $dh, $dir) or return ();
    my @entries = grep { $_ ne '.' && $_ ne '..' } readdir $dh;
    closedir $dh;
    return @entries;
}

# R4-I2 artifacts of one barcode: its record family (record, lock, rollback
# copies), generation members of its three names and its transaction temps.
# They place any directory under the authority.
sub i2_artifacts {
    my ($bc, $entries) = @_;
    my $n = names($bc);
    my $names_re = join('|', map { quotemeta } sort values %$n);
    return sort grep {
        /\A\Q$bc\E_blast_otu_cumulative\./ || /\A(?:$names_re)\.gen-/ || /\A\.r4d-publish-\Q$bc\E-/
    } @$entries;
}

# Governed residue (pristine absence requires none): every transaction
# artifact of the barcode's namespace -- the R4-I2 artifacts, the former
# sequential R4-D protocol's temps, and the table-named temporaries, journals,
# rollback copies and partial projections a writer or copier leaves while it
# replaces a governed name (`<name>.tmp`, `<name>.journal`, `<name>.bak.<pid>`,
# `.<name>.XXXXXX`, ...). Matching is bounded to the exact governed basenames
# followed by a closed set of transaction suffixes, so no other file of the
# directory ever counts.
my $RESIDUE_SUFFIX = 'tmp|temp|journal|bak|part|partial|new|old|orig';
sub governed_residue {
    my ($bc, $entries) = @_;
    my $n = names($bc);
    my $names_re = join('|', map { quotemeta } sort values %$n);
    my @residue = (i2_artifacts($bc, $entries), grep {
        /\A\.r4d-publish-[A-Za-z0-9_]{6}\z/
            || /\A(?:$names_re)(?:\.(?:$RESIDUE_SUFFIX)(?:[.~_-].*)?|~)\z/s
            || /\A\.(?:$names_re)\.[A-Za-z0-9]{6}\z/
    } @$entries);
    my %seen;
    return grep { !$seen{$_}++ } sort @residue;
}

sub is_state_dir { my ($dir) = @_; return File::Basename::basename($dir) eq '_state' ? 1 : 0; }

# A restart (bin/restart_handler.sh, reset or restore) of the rolling state
# `<outdir>/temp/ongoing/state/<id>` records `status=applying` in
# `<outdir>/temp/.restart_applied.<id>` before it first changes that state and
# `status=applied` after its last change. While that record says anything but a
# completed restart (applying: running or interrupted; unreadable; malformed;
# not a regular file), the `_state` fails closed, for readers and publishers
# alike, so nothing reads or writes a half-restored state. The handler's lock
# directory alone does not fence: the state is intact until `applying`, and
# complete once `applied`.
sub restart_fence {
    my ($dir) = @_;
    return unless is_state_dir($dir);
    my $root = File::Basename::dirname(File::Spec->rel2abs($dir));
    my $state = File::Basename::dirname($root);
    my $ongoing = File::Basename::dirname($state);
    my $temp = File::Basename::dirname($ongoing);
    return unless File::Basename::basename($state) eq 'state' && File::Basename::basename($ongoing) eq 'ongoing'
        && File::Basename::basename($temp) eq 'temp';
    my $sentinel = "$temp/.restart_applied." . File::Basename::basename($root);
    my $unknown = "the state of a restart (restart_mode reset or restore) of $root is unknown";
    unless (lstat $sentinel) {
        return if $!{ENOENT} || $!{ENOTDIR};
        fail("cannot inspect the restart record $sentinel: $!; $unknown");
    }
    fail("the restart record $sentinel is not a regular file; $unknown") unless -f _;
    my $text = '';
    open(my $fh, '<:raw', $sentinel) or fail("cannot read the restart record $sentinel: $!; $unknown");
    my $got = read($fh, $text, 4097);
    fail("cannot read the restart record $sentinel: $!; $unknown") unless defined $got;
    close $fh;
    return if $text =~ /\Aschema=2\nstatus=applied\n/ || $text =~ /\Amode=(?:reset|restore)\nrun_name=[^\n]+\n\z/;
    fail("a restart (restart_mode reset or restore) of $root is in progress, or was interrupted ($sentinel "
        . 'records status=applying): retry when it has finished, or rerun the interrupted restart to complete it')
        if $text =~ /\Aschema=2\nstatus=applying\n/;
    fail("the restart record $sentinel is malformed; $unknown");
}

# The R4-D (pre-I2) helper: its sealed-sidecar reader and projections are the
# only definition of a consistent legacy snapshot.
sub load_r4d {
    return if defined &RTBioScan::R4D::read_reporting;
    require FindBin;
    local $FindBin::Bin = $BIN;
    require "$BIN/r4_reporting_contract.pl";
}

sub slurp {
    my ($path) = @_;
    open my $f, '<:raw', $path or fail("cannot read $path: $!");
    my $text = do { local $/; <$f> };
    close $f;
    return $text // '';
}

# A complete legacy snapshot is exactly one R4-D publication: a valid sealed
# reporting sidecar, the public table projected from it, and its no-adapter
# projection (or the header-only table of a round without the split).
sub validate_legacy {
    my ($dir, $bc, $paths) = @_;
    load_r4d();
    my ($rows) = eval { (RTBioScan::R4D::read_reporting($paths->{reporting}))[1] };
    if (!$rows) {
        (my $why = $@ || 'unreadable') =~ s/\s+\z//;
        fail("legacy cumulative snapshot in $dir is not a valid R4-D publication ($paths->{reporting}: $why)");
    }
    fail("legacy cumulative snapshot in $dir is inconsistent: $paths->{public} is not the projection of "
        . "$paths->{reporting}")
        unless slurp($paths->{public}) eq RTBioScan::R4D::public_text($rows);
    my $noadapter = slurp($paths->{noadapter});
    fail("legacy cumulative snapshot in $dir is inconsistent: $paths->{noadapter} is not the no-adapter projection "
        . "of $paths->{reporting}")
        unless $noadapter eq RTBioScan::R4D::noadapter_text($rows) || $noadapter eq RTBioScan::R4D::public_header() . "\n";
    return 1;
}

# ---------------------------------------------------------------------------
# Pre-R4-D snapshots (see the header). The exact header of every pre-R4-D
# BLAST OTU table (reporting_blast_otu.pl, main.nf BLAST_HEADER and the
# failed-round placeholders of R4-C all write it) and its row width.
our $PRE_R4D_HEADER = join("\t", qw(read_id barcode_by_homology basecalling_model sample hit_id taxid aln_length
    perc_id otu_id otu_taxid otu_kingdom otu_phylum otu_class otu_order otu_family otu_genus otu_species)) . "\n";
my $PRE_R4D_FIELDS = 17;

# The body (rows) of a pre-R4-D table: the exact header, then complete rows.
sub read_pre_r4d_table {
    my ($path, $where) = @_;
    my @st = lstat $path;
    fail("$where: $path is not a regular file") unless @st && -f _;
    my $text = slurp($path);
    fail("$where: $path does not start with the pre-R4-D BLAST OTU header (wrong header)")
        unless substr($text, 0, length $PRE_R4D_HEADER) eq $PRE_R4D_HEADER;
    my $body = substr($text, length $PRE_R4D_HEADER);
    fail("$where: $path ends with a truncated row") if $body ne '' && substr($body, -1) ne "\n";
    my $line = 1;
    while ($body =~ /\G([^\n]*)\n/gc) {
        $line++;
        my $fields = ($1 =~ tr/\t//) + 1;
        fail("$where: $path line $line has $fields fields, not $PRE_R4D_FIELDS (malformed row)")
            unless $fields == $PRE_R4D_FIELDS && $1 !~ /[\r\0]/;
    }
    return $body;
}

# The round ledger `_state/round_index.tsv` (round_index_assign.sh):
# `<round_barcode>\t<index>` lines, unique on both, in index order.
sub read_round_ledger {
    my ($path, $where) = @_;
    my @st = lstat $path;
    fail("$where: its provenance cannot be established: no round ledger $path") unless @st && -f _;
    my $text = slurp($path);
    fail("$where: round ledger $path ends with a truncated line") if $text ne '' && substr($text, -1) ne "\n";
    my (@rounds, %rb, %idx);
    my $line = 0;
    for my $row (split /\n/, $text) {
        $line++;
        fail("$where: round ledger $path line $line is malformed")
            unless $row =~ /\A([^\t\/]+)\t([1-9][0-9]{0,8})\z/ && $1 ne '.' && $1 ne '..' && !$rb{$1}++ && !$idx{$2}++;
        push @rounds, { rb => $1, index => 0 + $2 };
    }
    return sort { $a->{index} <=> $b->{index} } @rounds;
}

# Completed rounds (the backup's `done_pod5.txt`): the round barcode is the
# recorded POD5 basename without `.pod5`. No ledger: no round completed.
sub read_done_rounds {
    my ($path, $where) = @_;
    return {} unless defined $path;
    my @st = lstat $path;
    if (!@st) {
        fail("$where: cannot inspect $path: $!") unless $!{ENOENT};
        return {};
    }
    fail("$where: completed-round ledger $path is not a regular file") unless -f _;
    my %done;
    for my $row (split /\n/, slurp($path)) {
        my $base = (split /\t/, $row, -1)[-1] // '';
        $done{$1} = 1 if $base =~ /\A(.+)\.pod5\z/;
    }
    return \%done;
}

# The shape of a pre-R4-D `_state` (validate_pre_r4d decides): a public or a
# no-adapter table, no reporting sidecar, and no governed residue except what
# an interrupted sealing leaves (its lock, and under it its temps and members).
sub pre_r4d_candidate {
    my ($dir, $bc, $entries) = @_;
    my $n = names($bc);
    my %present = map { $_ => 1 } @$entries;
    return 0 if $present{ $n->{reporting} } || !($present{ $n->{public} } || $present{ $n->{noadapter} });
    my $lock = File::Basename::basename(lock_path($dir, $bc));
    my $pn = join('|', map { quotemeta } @$n{@BACKUP_PRODUCTS});
    for my $e (governed_residue($bc, $entries)) {
        next if $e eq $lock;
        return 0 unless $present{$lock} && ($e =~ /\A(?:$pn)\.gen-/ || $e =~ /\A\.r4d-publish-\Q$bc\E-/);
    }
    return 1;
}

# The provenance proof of a pair of pre-R4-D tables: each is the exact header
# followed by the rows of the round tables of the first k rounds of the round
# ledger (the same k for both tables), every completed round included. The
# live `_state` proves its own tables (validate_pre_r4d); a restore proves the
# copies in its results snapshot against the live ledger and round
# directories before it wipes them (restore_cli). Arguments: tables =>
# {public, noadapter} paths, ledger => the round ledger, round_root => the
# directory of the round directories, done => the completed-round ledger (or
# undef), where => the diagnostic subject. Returns { generation, entries =>
# {product => [size, sha]}, provenance => {rounds, sha}, provenance_text }.
sub prove_pre_r4d_tables {
    my (%a) = @_;
    my ($bc, $where, $root) = @a{qw(bc where round_root)};
    my $n = names($bc);
    my (%body, %entries);
    for my $p (@BACKUP_PRODUCTS) {
        $body{$p} = read_pre_r4d_table($a{tables}{$p}, $where);
        $entries{$p} = [ length($PRE_R4D_HEADER) + length($body{$p}),
                         Digest::SHA->new(256)->add($PRE_R4D_HEADER)->add($body{$p})->hexdigest ];
    }
    my @rounds = read_round_ledger($a{ledger}, $where);
    my $done = read_done_rounds($a{done}, $where);
    # One pass per table over the ledger's rounds, one round table in memory at a
    # time: the k (number of leading rounds) for which the table is the header
    # followed by the rows of the first k round tables form a range [first, last].
    my (%has, %len, %digest, %range);
    for my $p (@BACKUP_PRODUCTS) {
        my ($pos, $matching, @ks) = (0, 1);
        push @ks, 0 if length($body{$p}) == 0;
        for my $i (0 .. $#rounds) {
            my $t = "$root/$rounds[$i]{rb}/$n->{$p}";
            my $b = '';
            if (lstat $t) {
                $b = read_pre_r4d_table($t, $where);
                $has{$p}[$i] = 1;
            } else {
                fail("$where: cannot inspect $t: $!") unless $!{ENOENT};
            }
            $len{$p}[$i] = length $b;
            $digest{$p}[$i] = $has{$p}[$i] ? Digest::SHA::sha256_hex($b) : '-';
            next unless $matching;
            if (substr($body{$p}, $pos, length $b) eq $b) {
                $pos += length $b;
                push @ks, $i + 1 if $pos == length $body{$p};
            } else {
                $matching = 0;
            }
        }
        $range{$p} = @ks ? [ $ks[0], $ks[-1] ] : undef;
    }
    for my $i (0 .. $#rounds) {
        fail("$where: its provenance cannot be established: completed round $rounds[$i]{rb} (done_pod5.txt) has no "
            . "round table in $root/$rounds[$i]{rb}") if $done->{ $rounds[$i]{rb} } && grep { !$has{$_}[$i] } @BACKUP_PRODUCTS;
    }
    for my $p (@BACKUP_PRODUCTS) {
        fail("$where: $n->{$p} is not the ordered concatenation of the round tables of round_index.tsv "
            . '(rows no round accounts for: appended, duplicated, truncated, reordered or copied from other state)')
            unless $range{$p};
    }
    my ($lo) = sort { $b <=> $a } map { $range{$_}[0] } @BACKUP_PRODUCTS;
    my ($hi) = sort { $a <=> $b } map { $range{$_}[1] } @BACKUP_PRODUCTS;
    fail("$where: $n->{public} holds the first $range{public}[0] to $range{public}[1] rounds of round_index.tsv but "
        . "$n->{noadapter} the first $range{noadapter}[0] to $range{noadapter}[1] (tables of different rounds)") if $lo > $hi;
    fail("$where: its provenance cannot be established: no round of round_index.tsv produced these tables")
        unless grep { my $i = $_; !grep { !$has{$_}[$i] } @BACKUP_PRODUCTS } 0 .. $hi - 1;
    for my $i ($hi .. $#rounds) {
        fail("$where: completed round $rounds[$i]{rb} (done_pod5.txt) is missing from the cumulative tables (truncated)")
            if $done->{ $rounds[$i]{rb} } && grep { $len{$_}[$i] } @BACKUP_PRODUCTS;
    }
    my $provenance_text = join('', map {
        my $i = $_;
        join("\t", $i + 1, $rounds[$i]{rb}, $rounds[$i]{index}, map { $digest{$_}[$i] } @BACKUP_PRODUCTS) . "\n"
    } 0 .. $hi - 1);
    my (undef, $generation) = record_text($bc, \%entries, undef, 'legacy', { rounds => $hi, sha => '0' x 64 });
    return { generation => $generation, entries => \%entries,
             provenance => { rounds => $hi, sha => Digest::SHA::sha256_hex($provenance_text) },
             provenance_text => $provenance_text };
}

# Complete validation of an authentic pre-R4-D `_state`; dies with a precise
# diagnostic. Returns what prove_pre_r4d_tables returns.
sub validate_pre_r4d {
    my ($dir, $bc) = @_;
    my $n = names($bc);
    my $where = "pre-R4-D cumulative snapshot in $dir";
    my @entries = dir_entries($dir);
    my %present = map { $_ => 1 } @entries;
    my $lock = File::Basename::basename(lock_path($dir, $bc));
    my @foreign = sort grep { /_blast_otu_(?:pretax_rpt\.txt|noadapter_rpt\.txt|reporting_v1\.tsv|cumulative\.)/ && !/\A\.?\Q$bc\E_blast_otu_/ } @entries;
    fail("$where mixes barcodes: " . join(', ', @foreign)) if @foreign;
    my @missing = grep { !$present{ $n->{$_} } } @BACKUP_PRODUCTS;
    fail("incomplete $where: " . join(', ', map { $n->{$_} } @missing) . ' missing (only one of the two pre-R4-D '
        . 'cumulative tables: a partial or interrupted round, or a partial copy)') if @missing;
    fail("$where holds $n->{reporting} (an R4-D sidecar next to pre-R4-D tables)") if $present{ $n->{reporting} };
    my $pn = join('|', map { quotemeta } @$n{@BACKUP_PRODUCTS});
    my @members = sort grep { /\A(?:$pn)\.gen-/ } @entries;
    my @temps = sort grep { /\A\.r4d-publish-\Q$bc\E-/ } @entries;
    my %sealing = map { $_ => 1 } $lock, @members, @temps;
    my @unexpected = sort grep {
        !$sealing{$_} && ((/\A\Q$bc\E_blast_otu_/ && !/\A(?:$pn)\z/) || /\A\.r4d-publish-/)
    } @entries;
    push @unexpected, grep { !$sealing{$_} } governed_residue($bc, \@entries);
    my %u; @unexpected = grep { !$u{$_}++ } sort @unexpected;
    fail("$where holds entries no pre-R4-D pipeline writes (unexpected or transaction residue): " . join(', ', @unexpected))
        if @unexpected;
    fail("$where holds transaction residue without valid authority (no sealing in progress): " . join(', ', @members, @temps))
        if (@members || @temps) && !$present{$lock};
    my $v = prove_pre_r4d_tables(bc => $bc, where => $where, round_root => File::Basename::dirname($dir),
        tables => { map { $_ => "$dir/$n->{$_}" } @BACKUP_PRODUCTS },
        ledger => "$dir/round_index.tsv", done => "$dir/done_pod5.txt");
    for my $m (@members) {
        my ($p) = grep { index($m, "$n->{$_}.gen-") == 0 } @BACKUP_PRODUCTS;
        fail("$where holds transaction residue without valid authority: $m is not a member of its sealing")
            unless $m eq "$n->{$p}.gen-$v->{generation}"
                && (same_file("$dir/$m", "$dir/$n->{$p}") || file_matches("$dir/$m", @{ $v->{entries}{$p} }));
    }
    return $v;
}

# Seal an authentic pre-R4-D snapshot under the publication lock: validate it
# completely, add its members (hard links, or verified durable byte copies
# without hard links), commit the legacy record by one rename. Idempotent: a
# record that exists meanwhile is returned as it is. A snapshot that fails
# validation is never touched: a reader validates it read-only before it takes
# the lock (and again under the lock), and a pre-existing lock name is kept.
# opt: locked => 1 when the caller already holds the lock; validated => the
# result of validate_pre_r4d taken under that lock; prevalidated => 1 when the
# caller has just validated it read-only (it is validated again under the lock).
sub seal_pre_r4d {
    my ($dir, $bc, %opt) = @_;
    my $record = record_path($dir, $bc);
    my $lock_path = lock_path($dir, $bc);
    my $had_lock = lstat($lock_path) ? 1 : 0;
    validate_pre_r4d($dir, $bc) unless $opt{locked} || $opt{prevalidated};
    my $lock = $opt{locked} ? undef : acquire_lock($lock_path);
    my (%created, $parsed);
    my $ok = eval {
        if (lstat $record) {
            $parsed = parse_record($record, $bc);
            return 1;
        }
        my $v = $opt{validated} // validate_pre_r4d($dir, $bc);
        my $n = names($bc);
        for my $p (@BACKUP_PRODUCTS) {
            my $gen = generation_path($dir, $n->{$p}, $v->{generation});
            next if lstat $gen;  # validated: the matching member of an interrupted sealing
            backup_link_or_copy("$dir/$n->{$p}", $gen, $bc);
            $created{$gen} = identity($gen);
            fail("sealed member $gen does not match $dir/$n->{$p}") unless file_matches($gen, @{ $v->{entries}{$p} });
        }
        sync_directory($dir);
        my ($text) = record_text($bc, $v->{entries}, undef, 'legacy', $v->{provenance});
        my $t = backup_write_durable($dir, $bc, $text);
        if (!rename $t, $record) {
            my $err = "$!";
            unlink $t;
            fail("commit legacy record $record: $err");
        }
        sync_directory($dir);
        $parsed = parse_record($record, $bc);
        1;
    };
    my $err = $@;
    if ($ok) {
        if ($lock) {
            unlink "$dir/$_" for grep { /\A\.r4d-publish-\Q$bc\E-/ } dir_entries($dir);  # an interrupted sealing's temps
            release_lock($lock, $lock_path);
        }
        return $parsed;
    }
    unlink_own(\%created);
    if ($lock) { $had_lock ? close($lock) : release_lock($lock, $lock_path); }
    die $err;
}

# The publisher's classification of a record-less `_state` (r4_reporting_contract.pl
# calls it before it takes the lock -- read-only, so a refused state is never
# touched -- and again under the lock). Returns
#   'pristine'  nothing published and no governed residue;
#   'residue'   no public table, only what an interrupted first publication of
#               this barcode leaves (generation members, its temporaries, the
#               lock): the retry supersedes it;
#   ('pre-r4d', $validated)  an authentic pre-R4-D snapshot, sealed first;
#   'r4d'       a complete, content-consistent R4-D snapshot (with, at most,
#               the residue of an interrupted first publication over it);
# and dies before any write for everything else: legacy-like tables that fail
# their ledger, schema or provenance validation, an incomplete snapshot, or
# residue no publisher of this barcode leaves. No new generation replaces such
# a state, so its evidence survives; it needs explicit remediation.
sub classify_publication {
    my ($dir, $bc) = @_;
    my $n = names($bc);
    my @entries = dir_entries($dir);
    my %present = map { $_ => 1 } @entries;
    my $lock = File::Basename::basename(lock_path($dir, $bc));
    my $names_re = join('|', map { quotemeta } sort values %$n);
    my @residue = governed_residue($bc, \@entries);
    my @foreign = grep { $_ ne $lock && !/\A(?:$names_re)\.gen-/ && !/\A\.r4d-publish-\Q$bc\E-/ } @residue;
    my $refuse = sub {
        (my $why = $_[0]) =~ s/\s+\z//;
        $why =~ s/\AR4-D cumulative: //;
        fail("publication refused: $why; no new generation replaces a cumulative snapshot that is not authentic, "
            . 'so its evidence is kept: remediate it explicitly (restore an authentic backup, or reset the state)');
    };
    my @tables = grep { $present{ $n->{$_} } } @PRODUCTS;
    if (!@tables) {
        $refuse->("$dir holds governed residue that no publication of barcode $bc leaves (" . join(', ', @foreign) . ')')
            if @foreign;
        return @residue ? 'residue' : 'pristine';
    }
    if (!$present{ $n->{reporting} }) {
        # pre-R4-D tables (no reporting sidecar): authentic, or refused with the reason
        my $v = eval { validate_pre_r4d($dir, $bc) };
        $refuse->($@) unless $v;
        return ('pre-r4d', $v);
    }
    $refuse->("$dir holds governed residue that no publication of barcode $bc leaves (" . join(', ', @foreign) . ')')
        if @foreign;
    my @missing = grep { !$present{ $n->{$_} } } @PRODUCTS;
    $refuse->("incomplete legacy cumulative snapshot in $dir: " . join(', ', map { $n->{$_} } @missing) . ' missing')
        if @missing;
    my %paths = map { $_ => "$dir/$n->{$_}" } @PRODUCTS;
    for my $p (@PRODUCTS) {
        my @st = lstat $paths{$p};
        $refuse->("public cumulative table is not a regular file: $paths{$p}") unless @st && -f _;
    }
    $refuse->($@) unless eval { validate_legacy($dir, $bc, \%paths); 1 };
    return 'r4d';
}

# A `backup` record (an earlier candidate's two-member backup) that a restore
# by that candidate left in `_state` names the reporting sidecar only by size
# and SHA-256. When exactly those bytes are in `_state` -- as the generation's
# reporting member, or at the sidecar's public name, where that restore copied
# the round's sidecar -- and both public members verify, the generation is
# completed under the lock: the reporting member is linked (or copied) and a
# state record of the same generation replaces the backup record by one
# rename. Returns 1 when the record is (now) a state record, 0 when the bytes
# are not there (the caller fails closed); nothing is synthesized.
sub adopt_backup_record {
    my ($dir, $bc, $r) = @_;
    return 0 unless is_state_dir($dir);
    my $n = names($bc);
    for my $p (@BACKUP_PRODUCTS) {
        my $e = $r->{entries}{$p};
        return 0 unless file_matches(generation_path($dir, $e->{name}, $r->{generation}), $e->{size}, $e->{sha});
    }
    my $e = $r->{entries}{reporting};
    my $member = generation_path($dir, $n->{reporting}, $r->{generation});
    my ($source) = grep { file_matches($_, $e->{size}, $e->{sha}) } ($member, "$dir/$n->{reporting}");
    return 0 unless defined $source;
    my %want = map { $_ => [ $r->{entries}{$_}{size}, $r->{entries}{$_}{sha} ] } @PRODUCTS;
    my ($text, $generation) = record_text($bc, \%want, undef, 'state');
    fail("backup record $r->{generation} and its state record disagree") unless $generation eq $r->{generation};
    my $record = record_path($dir, $bc);
    my $lock_path = lock_path($dir, $bc);
    my $lock = acquire_lock($lock_path);
    my $ok = eval {
        my $now = eval { parse_record($record, $bc) };
        if ($now && $now->{kind} eq 'backup' && $now->{text} eq $r->{text}) {
            if ($source ne $member) {
                unlink $member if lstat $member;
                backup_link_or_copy($source, $member, $bc);
                fail("adopted member $member does not match its record") unless file_matches($member, $e->{size}, $e->{sha});
            }
            sync_directory($dir);
            my $t = backup_write_durable($dir, $bc, $text);
            if (!rename $t, $record) {
                my $err = "$!";
                unlink $t;
                fail("commit state record $record: $err");
            }
            sync_directory($dir);
        }
        1;
    };
    my $err = $@;
    release_lock($lock, $lock_path);
    die $err unless $ok;
    return 1;
}

# Advice of a fail-closed diagnostic: what the publisher does with the state.
our $REPUBLISH = 'rerun the reporting task to republish the cumulative snapshot';
our $REMEDIATE = 'the publisher refuses to replace it, so its evidence is kept: remediate it explicitly '
    . '(restore an authentic backup, or reset the state)';

# Resolve the cumulative snapshot of one barcode in a directory. Returns
#   { mode => committed|legacy|absent|ungoverned, kind, generation, entries,
#     paths => {product => path}, record, ident }
# and dies (fail closed) when the authority is damaged or ambiguous.
#   committed   the record's generation; paths are its verified members
#               (kind state: all three; kinds backup, legacy and legacy-backup:
#               public and no-adapter -- a legacy generation has no reporting
#               sidecar, so its R4-D metrics are unavailable)
#   legacy      `_state` without a record holding a complete, consistent R4-D
#               snapshot; paths are the public names
#   absent      pristine `_state`: nothing was ever published, no residue
#   ungoverned  a directory other than `_state` holding no R4-I2 artifact;
#               paths are whatever public names exist (ordinary files)
# An authentic pre-R4-D `_state` is sealed here first (seal_pre_r4d) and then
# resolved through its legacy record. A `_state` whose rolling state is being
# reset or restored fails closed (restart_fence).
# expect => 'state' (default) never serves a `backup` record (an earlier
# candidate's two-member backup, restored into `_state` by that candidate): it
# is completed from its bound sidecar bytes first (adopt_backup_record), or
# fails closed. A restored legacy backup is the whole legacy generation.
sub resolve {
    my ($dir, $bc, %opt) = @_;
    my $expect = $opt{expect} // 'state';
    restart_fence($dir);
    my $record = record_path($dir, $bc);
    my $n = names($bc);
    my %res = (dir => $dir, bc => $bc, record => $record, paths => {}, ident => {});
    if (lstat $record) {
        fail("commit record is not a regular file: $record") unless -f _;
        my $r = parse_record($record, $bc);
        if ($expect eq 'state' && $r->{kind} eq 'backup') {
            fail("$record is an earlier candidate's backup record (restored from a results snapshot), and the reporting "
                . "sidecar it binds ($r->{entries}{reporting}{size} bytes, SHA-256 $r->{entries}{reporting}{sha}) is not in "
                . "$dir; $REPUBLISH") unless adopt_backup_record($dir, $bc, $r);
            return resolve($dir, $bc, %opt);
        }
        for my $p (@{ $KIND{ $r->{kind} }{members} }) {
            my $e = $r->{entries}{$p};
            my $g = generation_path($dir, $e->{name}, $r->{generation});
            my @st = lstat $g;
            fail("committed generation $r->{generation} is unavailable: $g is missing or does not match $record; $REPUBLISH")
                unless @st && -f _ && file_matches($g, $e->{size}, $e->{sha});
            $res{paths}{$p} = $g;
            $res{ident}{$g} = join(':', @st[0, 1, 7]);
        }
        return { %res, mode => 'committed', kind => $r->{kind}, generation => $r->{generation},
                 previous => $r->{previous}, entries => $r->{entries},
                 ($r->{provenance} ? (provenance => $r->{provenance}) : ()) };
    }
    fail("cannot inspect commit record $record: $!") unless $!{ENOENT};
    my @entries = dir_entries($dir);
    # A record committed meanwhile (a sealing or a publication that finished
    # after the check above) is the authority.
    return resolve($dir, $bc, %opt) if lstat $record;
    my @artifacts = i2_artifacts($bc, \@entries);
    if (!is_state_dir($dir) && !@artifacts) {
        for my $p (@PRODUCTS) {
            my $live = "$dir/$n->{$p}";
            $res{paths}{$p} = $live if -e $live;
        }
        return { %res, mode => 'ungoverned' };
    }
    # Every fail-closed answer below is re-checked against a record committed
    # meanwhile (a concurrent sealing or publication): that record is the answer.
    my $fail = sub { return resolve($dir, $bc, %opt) if lstat $record; fail(@_); };
    if (is_state_dir($dir) && pre_r4d_candidate($dir, $bc, \@entries)) {
        my $v = eval { validate_pre_r4d($dir, $bc) };
        if (!$v) {
            (my $why = $@) =~ s/\s+\z//;
            $why =~ s/\AR4-D cumulative: //;
            return $fail->("$why; not sealed as a legacy generation, and $REMEDIATE");
        }
        if (!eval { seal_pre_r4d($dir, $bc, prevalidated => 1); 1 }) {
            (my $why = $@) =~ s/\s+\z//;
            $why =~ s/\AR4-D cumulative: //;
            return $fail->("sealing the authentic pre-R4-D snapshot in $dir failed: $why; the snapshot is intact: retry");
        }
        return resolve($dir, $bc, %opt);
    }
    my @residue = governed_residue($bc, \@entries);
    if (@residue) {
        # the advice is the publisher's own decision (classify_publication, read-only)
        my $advice = !is_state_dir($dir) ? 'the next copy into it replaces it'
            : eval { classify_publication($dir, $bc); 1 } ? $REPUBLISH : $REMEDIATE;
        return $fail->("no commit record in $dir, but governed residue is present (" . join(', ', @residue)
            . "): an interrupted, in-progress or abandoned publication or copy, or a deleted commit record; $advice");
    }
    for my $p (@PRODUCTS) {
        my $live = "$dir/$n->{$p}";
        my @st = lstat $live;
        if (!@st) {
            return $fail->("cannot inspect $live: $!") unless $!{ENOENT};
            next;
        }
        return $fail->("public cumulative table is not a regular file: $live; $REMEDIATE") unless -f _;
        $res{paths}{$p} = $live;
        $res{ident}{$live} = join(':', @st[0, 1, 7, 9]);
    }
    my $present = keys %{ $res{paths} };
    return { %res, mode => 'absent' } if $present == 0;
    return $fail->("incomplete legacy cumulative snapshot in $dir: " . join(', ', map { $n->{$_} } grep { !$res{paths}{$_} } @PRODUCTS)
        . " missing (a partial or interrupted publication or copy); $REMEDIATE") if $present < @PRODUCTS;
    if (!eval { validate_legacy($dir, $bc, $res{paths}); 1 }) {
        (my $why = $@) =~ s/\s+\z//;
        $why =~ s/\AR4-D cumulative: //;
        return $fail->("$why; $REMEDIATE");
    }
    my %entries = map { $_ => { name => $n->{$_}, size => (lstat $res{paths}{$_})[7], sha => sha_of($res{paths}{$_}) } } @PRODUCTS;
    return { %res, mode => 'legacy', entries => \%entries };
}

# True while what a reader resolved is still what it read; checked before the
# reader publishes derived output. Committed members are immutable and kept
# for one further commit, so they need only still be the same files; a legacy
# or absent resolution is invalidated by any commit and by any change of the
# public names. Beyond the retention window the reader fails and is rerun.
sub still_current {
    my ($res) = @_;
    return 1 if $res->{mode} eq 'ungoverned';
    if ($res->{mode} ne 'committed') {
        return 0 if lstat $res->{record};
        my $n = names($res->{bc});
        return 0 if grep { !$res->{paths}{$_} && lstat("$res->{dir}/$n->{$_}") } @PRODUCTS;
    }
    for my $path (keys %{ $res->{ident} }) {
        my @st = lstat $path;
        return 0 unless @st && -f _;
        my $now = join(':', $res->{mode} eq 'committed' ? @st[0, 1, 7] : @st[0, 1, 7, 9]);
        return 0 unless $now eq $res->{ident}{$path};
    }
    return 1;
}

# ---------------------------------------------------------------------------
# Publication primitives shared by the state publisher (r4_reporting_contract.pl)
# and the backup publisher below.

sub sync_directory {
    my ($dir) = @_;
    sysopen(my $d, $dir, O_RDONLY) or fail("open directory $dir: $!");
    $d->sync or fail("sync directory $dir: $!");
    close $d;
}

# One publisher per barcode and directory. The lock name is removed on
# release, so a lock taken on a name released meanwhile is retried.
sub acquire_lock {
    my ($path) = @_;
    while (1) {
        sysopen(my $fh, $path, O_RDWR | O_CREAT, 0600) or fail("open publication lock $path: $!");
        flock($fh, LOCK_EX) or fail("lock $path: $!");
        my @held = stat $fh;
        my @named = lstat $path;
        return $fh if @named && $named[0] == $held[0] && $named[1] == $held[1];
        close $fh;
    }
}
# The name is removed only while it is still the lock this holder holds (a
# restart may have replaced the whole directory meanwhile).
sub release_lock {
    my ($fh, $path) = @_;
    my @held = stat $fh;
    my @named = lstat $path;
    unlink $path if @held && @named && $held[0] == $named[0] && $held[1] == $named[1];
    close $fh;
}

# Identity (device:inode) of a path, and removal of the paths that are still the
# files a writer created: a rollback never removes a file another writer (a
# restore replacing the directory, say) put under the same name meanwhile.
sub identity { my @st = lstat $_[0]; return @st ? "$st[0]:$st[1]" : ''; }
sub unlink_own {
    my ($ids) = @_;
    for my $path (sort keys %$ids) { unlink $path if identity($path) eq $ids->{$path}; }
}

# True while the directory's record still names one of the generations a
# writer just committed or kept: only then may it release other members.
sub record_names {
    my ($dir, $bc, $keep) = @_;
    my $r = eval { parse_record(record_path($dir, $bc), $bc) };
    return $r && $keep->{ $r->{generation} } ? 1 : 0;
}

# link(2) failed with $errno: true only when the filesystem has no hard links
# -- an unsupported-operation errno, or EPERM/EMLINK on a filesystem that
# declares a link limit of 1 (exFAT, FAT). Every other error (EIO, ENOSPC,
# EACCES, EXDEV, EPERM on a linking filesystem, ...) is a real failure and
# never selects the byte-copy fallback.
sub hardlinks_unsupported {
    my ($dir, $errno) = @_;
    local $! = $errno;
    return 1 if $!{ENOTSUP} || $!{EOPNOTSUPP} || $!{ENOSYS};
    return 0 unless $!{EPERM} || $!{EMLINK};
    my $max = POSIX::pathconf($dir, POSIX::_PC_LINK_MAX());
    return defined($max) && $max <= 1 ? 1 : 0;
}

# ---------------------------------------------------------------------------
# Backup generations. publish_backup replicates the generation resolved in a
# source directory (a `_state`, or another backup destination) into a
# destination directory:
#   1. every member `<name>.gen-<generation>` of the source's kind -- the
#      reporting sidecar included -- is written durably from bytes verified
#      against the source's record (size and SHA-256);
#   2. the destination's record, of the source's kind, is replaced by one
#      rename (the commit);
#   3. every lagging public table name is retracted before any is projected
#      (a destination projects the two public tables only).
# Death before the commit leaves the previous generation authoritative, after
# it the new one; destination readers resolve the record. A synchronous
# failure before the commit leaves the destination untouched; after it, the
# committed generation stays authoritative. The destination keeps the current
# and the previous generation. A backup therefore holds everything a restore
# needs: no restart cleanup can remove its authority or its sidecar.

sub backup_write_durable {
    my ($dir, $bc, $bytes, $stamp) = @_;
    my ($f, $t) = File::Temp::tempfile(".r4d-publish-$bc-XXXXXX", DIR => $dir, UNLINK => 0);
    my $ok = eval {
        binmode $f;
        print {$f} $bytes or fail("write $t: $!");
        $f->flush or fail("write $t: $!");
        $f->sync or fail("sync $t: $!");
        close $f or fail("close $t: $!");
        utime($stamp->[8], $stamp->[9], $t) or fail("set times of $t: $!") if $stamp && @$stamp;
        1;
    };
    return $t if $ok;
    my $err = $@;
    unlink $t;
    die $err;
}

sub backup_link_or_copy {
    my ($from, $to, $bc) = @_;
    return 1 if link($from, $to);
    my ($errno, $why) = ($! + 0, "$!");
    fail("link $to: $why") unless hardlinks_unsupported(File::Basename::dirname($to), $errno);
    my @st = lstat $from;
    my $t = backup_write_durable(File::Basename::dirname($to), $bc, slurp($from), \@st);
    chmod($st[2] & 07777, $t) if @st;  # a copy keeps the linked file's mode where the mount allows it
    return 1 if rename $t, $to;
    my $err = "$!";
    unlink $t;
    fail("link $to: $err");
}

# Read a source member, verifying its bytes against its record entry.
sub read_verified {
    my ($path, $size, $sha) = @_;
    my $bytes = slurp($path);
    fail("backup source $path does not match its committed generation (size or SHA-256); "
        . 'it was republished or damaged while it was copied')
        unless length($bytes) == $size && Digest::SHA::sha256_hex($bytes) eq $sha;
    return $bytes;
}

sub public_current {
    my ($member, $live, $size, $sha) = @_;
    return 1 if same_file($member, $live);
    return file_matches($live, $size, $sha);
}

sub cleanup_backup {
    my ($dir, $bc, $keep) = @_;
    return unless record_names($dir, $bc, $keep);
    my $n = names($bc);
    my $names_re = join('|', map { quotemeta } sort values %$n);
    for my $e (dir_entries($dir)) {
        my $stale = $e =~ /\A\.r4d-publish-\Q$bc\E-/
            || $e =~ /\A(?:$names_re)\.bak\./
            || ($e =~ /\A(?:$names_re)\.gen-(.*)\z/s && !$keep->{$1});
        unlink "$dir/$e" if $stale;
    }
}

sub publish_backup_generation {
    my ($dest, $bc, $kind, $entries, $sources, $provenance) = @_;
    my $n = names($bc);
    my @members = @{ $KIND{$kind}{members} };
    my %want = map { $_ => [ $entries->{$_}{size}, $entries->{$_}{sha} ] } @{ $KIND{$kind}{products} };
    my (undef, $generation) = record_text($bc, \%want, undef, $kind, $provenance);
    my $record = record_path($dest, $bc);
    my $lock_path = lock_path($dest, $bc);
    my $lock = acquire_lock($lock_path);
    my (%created, @temps, %proj, $committed, $keep);
    my $ok = eval {
        my $current;
        if (lstat $record) {
            $current = eval { parse_record($record, $bc) };
            print STDERR "WARN: R4-D cumulative: replacing unreadable backup record: $@" unless $current;
        }
        my %gen = map { $_ => generation_path($dest, $n->{$_}, $generation) } @members;
        my %valid = map { $_ => file_matches($gen{$_}, @{ $want{$_} }) } @members;
        my $previous = $current ? ($current->{generation} ne $generation ? $current->{generation} : $current->{previous}) : undef;
        # Replay: the same generation, of the same kind, with every member intact.
        if (!($current && $current->{kind} eq $kind && $current->{generation} eq $generation && !grep { !$valid{$_} } @members)) {
            for my $p (@members) {
                next if $valid{$p};
                my $bytes = read_verified($sources->{$p}, @{ $want{$p} });
                my @src = lstat $sources->{$p};
                my $t = backup_write_durable($dest, $bc, $bytes, \@src);
                # the source's mode, as the per-file copy keeps it (a mount with
                # fixed modes, exFAT say, keeps its own)
                chmod($src[2] & 07777, $t) if @src;
                push @temps, $t;
                if (lstat $gen{$p}) { unlink $gen{$p} or fail("replace damaged member $gen{$p}: $!"); }
                backup_link_or_copy($t, $gen{$p}, $bc);
                $created{ $gen{$p} } = identity($gen{$p});
                $proj{$p} = $t;
            }
            sync_directory($dest);
            my ($text) = record_text($bc, \%want, $previous, $kind, $provenance);
            my $t = backup_write_durable($dest, $bc, $text);
            push @temps, $t;
            rename $t, $record or fail("commit backup record $record: $!");
            $committed = 1;
            sync_directory($dest);
        }
        # Retract every lagging public table name before projecting any.
        my @lag = grep { !public_current($gen{$_}, "$dest/$n->{$_}", @{ $want{$_} }) } @BACKUP_PRODUCTS;
        for my $p (@lag) {
            my $live = "$dest/$n->{$p}";
            next unless lstat $live;
            unlink $live or fail("retract $live: $!");
        }
        for my $p (@lag) {
            my $t = $proj{$p};
            if (!defined $t) {
                $t = "$dest/.r4d-publish-$bc-link-$p.$$";
                unlink $t if lstat $t;
                push @temps, $t;
                backup_link_or_copy($gen{$p}, $t, $bc);
            }
            rename $t, "$dest/$n->{$p}" or fail("project $dest/$n->{$p}: $!");
            delete $proj{$p};
        }
        $keep = { $generation => 1, (defined($previous) ? ($previous => 1) : ()) };
        1;
    };
    my $err = $@;
    unlink $_ for grep { lstat $_ } @temps;
    if (!$ok) {
        if ($committed) { $err .= "R4-D cumulative: the committed backup generation $generation stays authoritative in $dest\n"; }
        else { unlink_own(\%created); }
        release_lock($lock, $lock_path);
        die $err;
    }
    cleanup_backup($dest, $bc, $keep);
    release_lock($lock, $lock_path);
    return $generation;
}

# An ungoverned source: its tables are ordinary files, copied one by one
# (atomic per file, mode and times kept) like the other backed-up tables.
sub copy_plain {
    my ($dest, $bc, $path) = @_;
    my @st = stat $path or fail("cannot read $path: $!");
    my $t = backup_write_durable($dest, $bc, slurp($path), \@st);
    chmod($st[2] & 07777, $t) or fail("chmod $t: $!");
    my $to = "$dest/" . File::Basename::basename($path);
    return 1 if rename $t, $to;
    my $err = "$!";
    unlink $t;
    fail("copy $to: $err");
}

# Back up the cumulative generation of one barcode of a source directory. The
# source's authority decides (resolve), never the presence of its public
# names: a publication that has just retracted them changes nothing here.
sub publish_backup {
    my ($dest, $src_dir, $bc) = @_;
    my $res = resolve($src_dir, $bc, expect => is_state_dir($src_dir) ? 'state' : 'any');
    if ($res->{mode} eq 'ungoverned') {
        my @plain = grep { defined $res->{paths}{$_} } @BACKUP_PRODUCTS;
        fail("$dest holds a backup generation; the ordinary files of $src_dir are not copied over it")
            if @plain && lstat record_path($dest, $bc);
        copy_plain($dest, $bc, $res->{paths}{$_}) for @plain;
        return;
    }
    return if $res->{mode} eq 'absent';
    # A complete R4-D snapshot (a record-less `_state`) is a state generation.
    my $kind = $res->{mode} eq 'legacy' ? 'state' : $res->{kind};
    fail("$res->{record} holds an earlier candidate's backup generation $res->{generation}, which has no reporting "
        . "sidecar member; it is not replicated as a complete generation (back up the live state into $src_dir first)")
        if $kind eq 'backup';
    return publish_backup_generation($dest, $bc, $kind, $res->{entries}, $res->{paths}, $res->{provenance});
}

# ---------------------------------------------------------------------------
# Restore (bin/restart_handler.sh, restart_mode=restore). The restart handler
# replaces the live rolling state with the results snapshot; the cumulative
# generation is restored with its authority, never as bare public tables:
#   prepare  read-only, before the handler changes anything: classifies the
#            snapshot's cumulative generation of every barcode it holds (see
#            classify_snapshot); a generation it cannot authenticate refuses
#            the whole restore, and no byte anywhere changes;
#   upgrade  after the handler recorded the restart as applying, before it
#            wipes the live state: an older backup proven by the retained
#            evidence is completed in place into a full generation (the backup
#            protocol, under the destination's lock) while that evidence still
#            exists, so provenance is never reconstructed after its source is
#            gone and a repeated restore needs no evidence;
#   install  after the wipe and the snapshot copy (which skips every governed
#            name): each complete generation is installed into `_state` --
#            verified member copies, its record committed by one rename, then
#            its public names -- so an I2 generation resolves at once, with its
#            reporting sidecar, and a legacy generation as sealed.
# The cumulative source of a snapshot root is what the handler copies: the
# structured current root's `tables/`, or a flat root; the temp/current root's
# structured `tables/` holds round copies (backup section 7), never a
# cumulative generation.
sub restore_sources {
    my ($current_root, $legacy_current) = @_;
    my @src;
    if (defined($current_root) && -d $current_root) {
        push @src, (-d "$current_root/tables" || -d "$current_root/plots") ? "$current_root/tables" : $current_root;
    }
    if (defined($legacy_current) && -d $legacy_current && !grep { -d "$legacy_current/$_" } qw(tables plots sequences)) {
        push @src, $legacy_current;
    }
    return grep { -d $_ } @src;
}

my $GOVERNED_NAME = qr/_blast_otu_(?:cumulative\.commit|pretax_rpt\.txt|noadapter_rpt\.txt|reporting_v1\.tsv)(?:\.gen-.*)?\z/s;
# A compressed copy of a cumulative table: no pipeline version writes one, and
# the snapshot copy skips it; a restore refuses it rather than drop it.
my $COMPRESSED_NAME = qr/_blast_otu_(?:pretax_rpt\.txt|noadapter_rpt\.txt|reporting_v1\.tsv)\.gz\z/;

sub restore_barcodes {
    my (@dirs) = @_;
    my %bc;
    for my $d (@dirs) {
        for my $e (dir_entries($d)) {
            if ($e =~ /\A(.+)$GOVERNED_NAME/ || $e =~ /\A(.+)$COMPRESSED_NAME/) {
                $bc{$1} = 1;
            } elsif ($e =~ /\A\.?(.+)_blast_otu_(?:cumulative\.commit|pretax_rpt\.txt|noadapter_rpt\.txt|reporting_v1\.tsv)(?:\..*|~)\z/s) {
                my $barcode = $1;
                $bc{$barcode} = 1 if governed_residue($barcode, [$e]);
            } elsif ($e =~ /\A\.r4d-publish-(.+)-(?:[A-Za-z0-9_]{6}|link-(?:reporting|public|noadapter)\.[0-9]+)\z/) {
                $bc{$1} = 1;
            }
        }
    }
    return sort keys %bc;
}

# Retained artifacts that may hold the bytes of a sidecar: the snapshot's own
# sidecar name and members, the round copy in temp/current (backup section 7),
# the live `_state` (its public name and members) and the live round
# directories.
sub sidecar_candidates {
    my ($src, $bc, $ev) = @_;
    my $name = names($bc)->{reporting};
    my $members = sub { my ($d) = @_; map { "$d/$_" } sort grep { index($_, "$name.gen-") == 0 } dir_entries($d) };
    my @c = ("$src/$name", $members->($src));
    push @c, "$ev->{temp_current_tables}/$name" if defined $ev->{temp_current_tables};
    push @c, "$ev->{live_state}/$name", $members->($ev->{live_state}) if defined $ev->{live_state};
    push @c, map { "$ev->{round_root}/$_/$name" } sort grep { $_ ne '_state' && !/\A\./ } dir_entries($ev->{round_root})
        if defined $ev->{round_root};
    my %seen;
    return grep { !$seen{$_}++ && do { my @st = lstat $_; @st && -f _ } } @c;
}

# The snapshot's cumulative generation of one barcode:
#   { class => 'none' }                      nothing of it in the snapshot;
#   { class => 'complete', record }          a sealed record of kind state,
#                                            legacy or legacy-backup whose
#                                            members all verify;
#   { class => 'upgrade', kind, entries, sources, provenance, proof }
#                                            an older backup proven by retained
#                                            evidence (below);
# and dies -- refusing the restore -- for anything else: a corrupt record, a
# missing or damaged member, members or residue without a record, only one of
# the two tables, tables no evidence authenticates, compressed tables. Proofs, from retained
# artifacts only (nothing is synthesized):
#   * an earlier candidate's `backup` record (two members): its reporting
#     sidecar, found by the size and SHA-256 the record binds;
#   * record-less R4-D-family tables (R4-D, the first I2 candidate): a valid
#     sealed R4-D sidecar whose projections are exactly those tables;
#   * record-less pre-R4-D tables: the live round ledger and round tables,
#     with the snapshot's own completed-round ledger (prove_pre_r4d_tables).
sub classify_snapshot {
    my ($src, $bc, $ev) = @_;
    my $n = names($bc);
    my $where = "results snapshot $src";
    my @compressed = sort grep { /\A\Q$bc\E$COMPRESSED_NAME/ } dir_entries($src);
    fail("$where: compressed cumulative tables of barcode $bc (" . join(', ', @compressed) . ') cannot be authenticated '
        . '(no pipeline version writes them); decompress or remove them explicitly') if @compressed;
    my $record = record_path($src, $bc);
    if (lstat $record) {
        fail("$where: commit record is not a regular file: $record") unless -f _;
        my $r = eval { parse_record($record, $bc) };
        if (!$r) { (my $why = $@) =~ s/\s+\z//; fail("$where: unreadable backup record ($why)"); }
        for my $p (@{ $KIND{ $r->{kind} }{members} }) {
            my $e = $r->{entries}{$p};
            my $g = generation_path($src, $e->{name}, $r->{generation});
            fail("$where: backup generation $r->{generation} is incomplete: $g is missing or does not match its record")
                unless file_matches($g, $e->{size}, $e->{sha});
        }
        return { class => 'complete', record => $r } unless $r->{kind} eq 'backup';
        my $e = $r->{entries}{reporting};
        my ($found) = grep { file_matches($_, $e->{size}, $e->{sha}) } sidecar_candidates($src, $bc, $ev);
        fail("$where: backup generation $r->{generation} (an earlier candidate's format) binds a reporting sidecar "
            . "($e->{size} bytes, SHA-256 $e->{sha}) that no retained artifact holds; it cannot be restored as a complete "
            . 'generation') unless defined $found;
        my %sources = map { $_ => generation_path($src, $r->{entries}{$_}{name}, $r->{generation}) } @BACKUP_PRODUCTS;
        $sources{reporting} = $found;
        return { class => 'upgrade', kind => 'state', entries => $r->{entries}, sources => \%sources,
                 proof => "its reporting sidecar, bound by SHA-256, retained at $found" };
    }
    my @entries = dir_entries($src);
    my %present = map { $_ => 1 } @entries;
    my @tables = grep { $present{ $n->{$_} } } @BACKUP_PRODUCTS;
    my @stray = grep { $_ ne File::Basename::basename(lock_path($src, $bc)) } governed_residue($bc, \@entries);
    if (!@tables) {
        return { class => 'none' } unless $present{ $n->{reporting} } || @stray;
        my @partial = ($present{ $n->{reporting} } ? $n->{reporting} : (), @stray);
        fail("$where: incomplete backup of barcode $bc: governed members or transaction residue without a commit "
            . 'record (' . join(', ', @partial) . ')');
    }
    fail("$where: incomplete backup of barcode $bc: only " . join(', ', map { $n->{$_} } @tables)
        . ' of the two cumulative tables') if @tables < 2;
    for my $p (@BACKUP_PRODUCTS) {
        my @st = lstat "$src/$n->{$p}";
        fail("$where: $src/$n->{$p} is not a regular file") unless @st && -f _;
    }
    my %t = map { $_ => slurp("$src/$n->{$_}") } @BACKUP_PRODUCTS;
    load_r4d();
    for my $cand ($present{ $n->{reporting} } ? "$src/$n->{reporting}" : sidecar_candidates($src, $bc, $ev)) {
        my $rows = eval { (RTBioScan::R4D::read_reporting($cand))[1] } or next;
        next unless RTBioScan::R4D::public_text($rows) eq $t{public};
        next unless $t{noadapter} eq RTBioScan::R4D::noadapter_text($rows)
            || $t{noadapter} eq RTBioScan::R4D::public_header() . "\n";
        my $rep = slurp($cand);
        my %entries = (reporting => { name => $n->{reporting}, size => length($rep), sha => Digest::SHA::sha256_hex($rep) },
                       map { $_ => { name => $n->{$_}, size => length($t{$_}), sha => Digest::SHA::sha256_hex($t{$_}) } } @BACKUP_PRODUCTS);
        return { class => 'upgrade', kind => 'state', entries => \%entries,
                 sources => { reporting => $cand, map { $_ => "$src/$n->{$_}" } @BACKUP_PRODUCTS },
                 proof => "the valid R4-D reporting sidecar $cand projects exactly to its two tables" };
    }
    fail("$where: reporting sidecar $n->{reporting} does not authenticate its cumulative tables")
        if $present{ $n->{reporting} };
    my $v = eval {
        prove_pre_r4d_tables(bc => $bc, where => $where, round_root => $ev->{round_root}, done => $ev->{done},
            tables => { map { $_ => "$src/$n->{$_}" } @BACKUP_PRODUCTS }, ledger => "$ev->{live_state}/round_index.tsv");
    };
    if (!$v) {
        (my $why = $@) =~ s/\s+\z//;
        $why =~ s/\AR4-D cumulative: //;
        fail("$where: its cumulative tables of barcode $bc cannot be authenticated -- no retained valid R4-D reporting "
            . "sidecar projects to them, and as pre-R4-D tables: $why");
    }
    return { class => 'upgrade', kind => 'legacy', provenance => $v->{provenance},
             entries => { map { $_ => { name => $n->{$_}, size => $v->{entries}{$_}[0], sha => $v->{entries}{$_}[1] } } @BACKUP_PRODUCTS },
             sources => { map { $_ => "$src/$n->{$_}" } @BACKUP_PRODUCTS },
             proof => "the live round ledger and round tables ($v->{provenance}{rounds} rounds)" };
}

# Install a complete generation of the snapshot into the (wiped) `_state`.
sub install_generation {
    my ($state, $bc, $src, $r) = @_;
    my $n = names($bc);
    my $kind = $r->{kind};
    my %want = map { $_ => [ $r->{entries}{$_}{size}, $r->{entries}{$_}{sha} ] } @{ $KIND{$kind}{products} };
    my $record = record_path($state, $bc);
    my $lock_path = lock_path($state, $bc);
    my $lock = acquire_lock($lock_path);
    my (%created, @temps, $committed);
    my $ok = eval {
        fail("$state already holds a commit record of barcode $bc: the live state changed during the restore") if lstat $record;
        for my $p (@{ $KIND{$kind}{members} }) {
            my $e = $r->{entries}{$p};
            my $from = generation_path($src, $e->{name}, $r->{generation});
            my $to = generation_path($state, $e->{name}, $r->{generation});
            my @st = lstat $from;
            my $t = backup_write_durable($state, $bc, read_verified($from, $e->{size}, $e->{sha}), \@st);
            chmod($st[2] & 07777, $t) if @st;
            push @temps, $t;
            if (lstat $to) { unlink $to or fail("replace $to: $!"); }
            rename $t, $to or fail("install member $to: $!");
            $created{$to} = identity($to);
        }
        sync_directory($state);
        my ($text) = record_text($bc, \%want, undef, $kind, $r->{provenance});
        my $t = backup_write_durable($state, $bc, $text);
        push @temps, $t;
        rename $t, $record or fail("commit restored record $record: $!");
        $committed = 1;
        sync_directory($state);
        for my $p (@{ $KIND{$kind}{members} }) {
            my $name = "$state/$n->{$p}";
            my $gen = generation_path($state, $n->{$p}, $r->{generation});
            next if lstat($name) && public_current($gen, $name, @{ $want{$p} });
            my $l = "$state/.r4d-publish-$bc-link-$p.$$";
            unlink $l if lstat $l;
            push @temps, $l;
            backup_link_or_copy($gen, $l, $bc);
            rename $l, $name or fail("project $name: $!");
        }
        1;
    };
    my $err = $@;
    unlink $_ for grep { lstat $_ } @temps;
    unlink_own(\%created) unless $committed;
    release_lock($lock, $lock_path);
    die $err unless $ok;
    return $r->{generation};
}

# restore <prepare|upgrade|install> <_state> <current root> <temp/current root>
sub restore_cli {
    my ($phase, $state, $current_root, $legacy_current) = @_;
    fail('usage: restore <prepare|upgrade|install> <_state> <current root> <temp/current root>')
        unless defined($legacy_current) && defined($phase) && $phase =~ /\A(?:prepare|upgrade|install)\z/;
    my @sources = restore_sources($current_root, $legacy_current);
    for my $src (@sources) {
        my @residue = grep { /\A\.r4d-publish-[A-Za-z0-9_]{6}\z/ } dir_entries($src);
        fail("results snapshot $src: governed transaction residue without a commit record (" . join(', ', @residue) . ')') if @residue;
    }
    my $ev = { live_state => $state, round_root => File::Basename::dirname($state),
               temp_current_tables => (-d "$legacy_current/tables" ? "$legacy_current/tables" : undef),
               done => scalar((grep { -f $_ } "$current_root/done_pod5.txt", "$legacy_current/done_pod5.txt")[0]) };
    my $installed = 0;
    for my $bc (restore_barcodes(@sources)) {
        my ($src) = grep { my $d = $_; grep {
            /\A\Q$bc\E(?:$GOVERNED_NAME|$COMPRESSED_NAME)/ || governed_residue($bc, [$_])
        } dir_entries($d) } @sources;
        my $plan = classify_snapshot($src, $bc, $ev);
        if ($plan->{class} eq 'none') {
            print "restore $phase: barcode $bc: no cumulative generation in $src\n";
        } elsif ($plan->{class} eq 'upgrade') {
            fail("restore install: the backup of barcode $bc in $src was not completed before the wipe") if $phase eq 'install';
            my $g = $phase eq 'upgrade'
                ? publish_backup_generation($src, $bc, $plan->{kind}, $plan->{entries}, $plan->{sources}, $plan->{provenance})
                : (record_text($bc, { map { $_ => [ $plan->{entries}{$_}{size}, $plan->{entries}{$_}{sha} ] } @{ $KIND{ $plan->{kind} }{products} } },
                               undef, $plan->{kind}, $plan->{provenance}))[1];
            print "restore $phase: barcode $bc: $plan->{kind} generation $g of $src, proven by $plan->{proof}\n";
        } else {
            my $r = $plan->{record};
            if ($phase eq 'install') { install_generation($state, $bc, $src, $r); $installed++; }
            my $record_name = File::Basename::basename(record_path($src, $bc));
            my @extra = grep {
                $_ ne $record_name && !(/\.gen-([0-9a-f]{64})\z/ && ($1 eq $r->{generation} || ($r->{previous} // '') eq $1))
            } governed_residue($bc, [ dir_entries($src) ]);
            if ($phase eq 'upgrade' && @extra) {
                # residue of an interrupted backup or upgrade (beyond the record): its replay
                # completes it -- names reconciled, temporaries and stale members removed
                publish_backup_generation($src, $bc, $r->{kind}, $r->{entries},
                    { map { $_ => generation_path($src, $r->{entries}{$_}{name}, $r->{generation}) } @{ $KIND{ $r->{kind} }{members} } },
                    $r->{provenance});
            }
            print "restore $phase: barcode $bc: $r->{kind} generation $r->{generation} of $src\n";
        }
    }
    print "restore $phase: installed $installed generation(s)\n" if $phase eq 'install';
    return 0;
}

# ---------------------------------------------------------------------------
# Command-line entry points for bash (bin/lib/backup_sync.sh) and Python
# (bin/report_render.py); one process per call.

# backup <dest> <source public table>...: publish the generation of every
# source directory into dest.
sub backup_cli {
    my ($dest, @sources) = @_;
    my %groups;
    for my $src (@sources) {
        my ($dir, $bc) = split_live_path($src);
        $groups{"$dir\0$bc"} = [ $dir, $bc ] if defined $bc;
    }
    publish_backup($dest, @{ $groups{$_} }) for sort keys %groups;
    return 0;
}

# backup-generation <dest> <source dir> <barcode>: publish the source's
# generation of that barcode into dest (backup_update_and_clean names the
# source and barcode explicitly: no glob or existence test decides).
sub backup_generation_cli {
    my ($dest, $src_dir, $bc) = @_;
    fail('usage: backup-generation <dest> <source dir> <barcode>') unless defined($bc) && $bc ne '';
    publish_backup($dest, $src_dir, $bc);
    return 0;
}

# resolve <barcode> (<dir> <state|any>)...: one JSON object per directory, in
# order, as one JSON array; a failed resolution is {ok: false, error}.
sub resolve_cli {
    my ($bc, @pairs) = @_;
    require JSON::PP;
    my @out;
    while (my ($dir, $expect) = splice(@pairs, 0, 2)) {
        my $res = eval { resolve($dir, $bc, expect => $expect // 'state') };
        push @out, $res
            ? { ok => JSON::PP::true(), map { $_ => $res->{$_} } qw(mode kind generation paths record ident) }
            : { ok => JSON::PP::false(), error => ($@ =~ s/\s+\z//r) };
    }
    print JSON::PP->new->canonical->encode(\@out), "\n";
    return 0;
}

1;
