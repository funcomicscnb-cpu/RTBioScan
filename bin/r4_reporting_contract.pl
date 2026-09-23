#!/usr/bin/perl
# R4-D reporting contract: public OTU propagation, explicit denominators and
# the current cumulative canonical-membership snapshot.
#
# Population unit: one unique canonical NR sequence-to-OTU membership relation
# (`uuid|MARKER` inside one `OTUB_N-MARKER` projection). The authoritative
# population is R3 canonical membership (otu_members_round.tsv); classification
# is the validated R4-B sidecar; direct-hit metrics are the sealed R4-A
# all-evidence state. Nothing here re-derives R4-A/R4-B scientific selection.
package RTBioScan::R4D;

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib";
use Digest::SHA qw(sha256_hex);
use File::Basename qw(dirname basename);
use File::Temp qw(tempfile);
use IO::Handle ();
use JSON::PP ();
use Getopt::Long qw(GetOptionsFromArray);

# Load the shared taxon helpers by their canonical path first so every later
# module sees them already defined (their guards skip a second compilation).
require "$FindBin::Bin/lib/taxon_util.pl" unless defined &TaxonUtil::canonical_lineage;
require RTBioScan::OTURefineBlastreport;
require "$FindBin::Bin/reporting_identity_contract.pl";
require "$FindBin::Bin/lib/RTBioScan/R4DCumulative.pm" unless defined &RTBioScan::R4DCumulative::resolve;
# The R4-A helper declares no package; load it the way R4-B does so its subs
# live in RTBioScan::R4A (sealed-evidence reader, canonical number display).
{
    package RTBioScan::R4A;
    require "$FindBin::Bin/cache_blast_by_hash.pl" unless defined &RTBioScan::R4A::evidence_read;
}

our $VERSION = 1;
our $CONTRACT = 'R4-D-v1';
our @COLUMNS = qw(
    canonical_member uuid marker read_id barcode_by_homology basecalling_model sample
    display_otu_key stable_otu_key otu_status otu_taxid otu_depth otu_origin
    kingdom phylum class order family genus species
    member_count blast_eligible direct_hit
    read_status read_taxid read_depth read_origin read_reason
    hit_id hit_taxid aln_length perc_id evalue bitscore candidate_count source_taxids
);
our @PUBLIC_COLUMNS = qw(read_id barcode_by_homology basecalling_model sample hit_id taxid aln_length perc_id
    otu_id otu_taxid otu_kingdom otu_phylum otu_class otu_order otu_family otu_genus otu_species);
our @RANKS = qw(kingdom phylum class order family genus species);
our @STATUSES = qw(ASSIGNED AMBIGUOUS_TIE NO_HIT FILTERED_INELIGIBLE REFERENCE_UNRESOLVED REFERENCE_INCONSISTENT COMPUTATION_FAILED);
our %LEVEL_DEPTH = (family => 4, genus => 5, species => 6);
our @LEVELS = qw(family genus species);
our @NOADAPTER_UNASSIGNED = ('Unassigned') x 7;

sub fail { die "R4-D: @_\n" }
sub json { return JSON::PP->new->canonical; }
sub usable { my ($s) = @_; return defined($s) && ($s eq 'ASSIGNED' || $s eq 'AMBIGUOUS_TIE') ? 1 : 0; }
sub is_no_adapter_sample {
    my ($sample) = @_;
    return 0 unless defined $sample;
    return SampleLabel::is_no_adapter_label($sample) ? 1 : 0 if defined &SampleLabel::is_no_adapter_label;
    return $sample =~ /^no_adapter/i ? 1 : 0;
}
sub model_rank {
    my ($model) = @_;
    $model = lc($model // '');
    return 4 if $model eq 'sup';
    return 3 if $model eq 'hac2sup';
    return 2 if $model =~ /^hac/;
    return 1 if $model eq 'fast';
    return 0;
}
sub file_sha256 {
    my ($path) = @_;
    return 'NA' unless defined($path) && $path ne '' && -f $path;
    return Digest::SHA->new(256)->addfile($path, 'b')->hexdigest;
}
sub public_header { return join("\t", @PUBLIC_COLUMNS); }

# ---------------------------------------------------------------------------
# R3 canonical membership (otu_members_round.tsv: otu_id \t read_id)
sub read_membership {
    my ($path) = @_;
    my (%pairs, %by_member, %otus);
    fail("missing canonical membership $path") unless defined($path) && -f $path;
    open my $f, '<', $path or fail("read canonical membership $path: $!");
    my $head = <$f>;
    fail('empty canonical membership file') unless defined $head;
    $head =~ s/\r?\n\z//;
    fail('unexpected canonical membership header') unless $head eq "otu_id\tread_id";
    while (my $line = <$f>) {
        fail('truncated canonical membership row') unless $line =~ s/\r?\n\z//;
        next if $line eq '';
        my @v = split /\t/, $line, -1;
        fail('malformed canonical membership row') unless @v == 2 && $v[0] ne '' && $v[1] ne '';
        fail('canonical membership read id is not a base identifier') if $v[1] =~ /[|\s]/;
        my ($otu, $uuid) = @v;
        fail("canonical membership OTU without marker projection: $otu") unless $otu =~ /\AOTUB_([0-9]+)-([^|\s-]+)\z/;
        my $marker = $2;
        my $member = "$uuid|$marker";
        my $key = "$otu\t$uuid";
        next if $pairs{$key}++;
        fail("canonical member in two projections of one marker: $member") if exists($by_member{$member}) && $by_member{$member} ne $otu;
        $by_member{$member} = $otu;
        $otus{$otu}++;
    }
    close $f or fail('close canonical membership');
    return { pairs => \%pairs, by_member => \%by_member, otus => \%otus };
}

sub read_kept_otus {
    my ($path) = @_;
    return undef unless defined($path) && $path ne '' && -f $path;
    my %kept;
    open my $f, '<', $path or fail("read eligible OTU list $path: $!");
    while (my $line = <$f>) {
        $line =~ s/\r?\n\z//;
        next if $line eq '';
        fail("malformed eligible OTU id: $line") unless $line =~ /\ACLUST_([0-9]+)\z/;
        $kept{$1} = 1;
    }
    close $f or fail('close eligible OTU list');
    return \%kept;
}

sub r4b_signature_of {
    my ($path) = @_;
    open my $f, '<', $path or fail("read R4-B sidecar $path: $!");
    my $head = <$f> // '';
    close $f;
    my ($sig,$provenance,$version)=RTBioScan::OTURefineBlastreport::r4b_header($head);
    fail('legacy R4-B sidecar (v1) lacks R4-A evidence provenance') if $version==1;
    return ($sig,$provenance);
}

# ---------------------------------------------------------------------------
# Sealed R4-A all-evidence, streamed through the R4-A envelope/checksum reader
# and per-HSP validator, keeping only the canonical members' rows. Memory is
# proportional to the current membership, never to the evidence history.
sub load_evidence {
    my ($dir, $markers, $wanted, $provenance) = @_;
    fail('R4-B evidence provenance marker scope mismatch') unless join("\t",sort keys %$markers) eq join("\t",sort keys %$provenance);
    my (%by_key, %sigs);
    for my $marker (sort keys %$markers) {
        my $path = "$dir/otu_blast_evidence_$marker.tsv";
        fail("missing R4-A evidence for $marker: $path") unless -f $path;
        my (%group, %seen, %subject_tax);
        my ($sig,$version,$digest,$count) = RTBioScan::R4A::scan_sealed($path, 'EVIDENCE', sub {
            my ($line) = @_;
            my @v = split /\t/, $line, -1;
            fail('wrong evidence field count') unless @v == 14;
            my $id = shift @v;
            my $status = pop @v;
            my @tok = split /\|/, $id;
            return unless @tok > 1 && $tok[1] eq $marker;
            my $key = "$tok[0]|$marker";
            return unless $wanted->{$key};
            RTBioScan::R4A::query_id($id);
            RTBioScan::R4A::hash_id($v[0]);
            my $g = $group{$id} //= { hash => $v[0], id => $id, key => $key, status => $status, candidates => [] };
            fail('conflicting query/hash evidence') if $g->{hash} ne $v[0];
            fail('mixed evidence status for one query') if $g->{status} ne $status;
            my $dup = "$id\t$v[1]";
            fail('conflicting duplicate evidence') if exists($seen{$dup}) && $seen{$dup} ne $line;
            return if exists $seen{$dup};
            $seen{$dup} = $line;
            if ($status eq 'NO_HIT') {
                fail('malformed NO_HIT') unless join("\t", @v[1 .. 11]) eq join("\t", ('NA') x 11);
                return;
            }
            fail('unknown evidence status') unless $status eq 'UNIQUE' || $status eq 'DEFERRED_TIE';
            RTBioScan::R4A::hsp(\@v);
            fail('conflicting subject taxonomy') if exists($subject_tax{ $v[1] }) && $subject_tax{ $v[1] } ne $v[2];
            $subject_tax{ $v[1] } = $v[2];
            push @{ $g->{candidates} }, [ @v[1 .. 6] ];
        });
        my $p=$provenance->{$marker};
        fail("R4-A evidence provenance mismatch for $marker") unless $sig eq $p->{signature} && $count==$p->{rows} && $digest eq $p->{body_sha256};
        $sigs{$marker} = $sig;
        my %by_hash;
        for my $id (sort keys %group) {
            my $g = $group{$id};
            fail('invalid tie cardinality') if ($g->{status} eq 'DEFERRED_TIE') != (@{ $g->{candidates} } > 1);
            my $identity = join("\n", sort map { join("\t", @$_) } @{ $g->{candidates} });
            fail('conflicting attribution for one sequence hash') if exists($by_hash{ $g->{hash} }) && $by_hash{ $g->{hash} } ne $identity;
            $by_hash{ $g->{hash} } = $identity;
            push @{ $by_key{ $g->{key} } }, { hash => $g->{hash}, id => $g->{id}, candidates => $g->{candidates} };
        }
    }
    return (\%by_key, \%sigs);
}

sub accession_of {
    my ($subject) = @_;
    my $acc = $subject;
    $acc =~ s/\|kraken:taxid\|-?[0-9]+\z//;
    return $acc;
}

# ---------------------------------------------------------------------------
sub build {
    my (%o) = @_;
    my $context = lc($o{context} // '');
    fail('demux identity context required') if $context eq '';
    my $membership = read_membership($o{members});
    my $kept = read_kept_otus($o{eligible});
    my %markers;
    for my $otu (keys %{ $membership->{otus} }) { $otu =~ /\AOTUB_[0-9]+-(.+)\z/ and $markers{$1} = 1; }

    my $member_total = scalar keys %{ $membership->{by_member} };
    my @rows;
    my ($r4b_sig, $evidence_sigs) = ('NA', {});
    if (!defined($o{sidecar}) || $o{sidecar} eq '' || !-e $o{sidecar}) {
        fail("R4-B sidecar missing for non-empty canonical membership ($member_total members)") if $member_total > 0;
    } else {
        my $provenance;
        ($r4b_sig,$provenance) = r4b_signature_of($o{sidecar});
        my $r4b = RTBioScan::OTURefineBlastreport::read_status_sidecar($o{sidecar});
        my (%by_member, %seen_pair);
        for my $r (@$r4b) {
            my $member = $r->{canonical_member};
            my ($uuid) = split /\|/, $member;
            $seen_pair{"$r->{otu_id}\t$uuid"} = 1;
            my $rid = $r->{read_id};
            my $id = $rid;
            $id =~ s/\|\Q$r->{otu_id}\E\z// or fail("R4-B read id lacks its projection suffix: $rid");
            my $model = (split /\|/, $id)[2] // '';
            my $entry = $by_member{$member} //= { row => $r, ids => [] };
            push @{ $entry->{ids} }, [ $id, $rid, $model ];
        }
        my @missing = grep { !$seen_pair{$_} } sort keys %{ $membership->{pairs} };
        my @extra = grep { !$membership->{pairs}{$_} } sort keys %seen_pair;
        if (@missing || @extra) {
            my $m = join(',', map { s/\t/:/r } @missing[0 .. ($#missing < 2 ? $#missing : 2)]);
            my $e = join(',', map { s/\t/:/r } @extra[0 .. ($#extra < 2 ? $#extra : 2)]);
            fail(sprintf('canonical membership and R4-B sidecar disagree (missing=%d [%s]; extra=%d [%s])', scalar(@missing), $m, scalar(@extra), $e));
        }
        my ($evidence, $sigs) = load_evidence($o{evidence_dir}, \%markers, $membership->{by_member}, $provenance);
        $evidence_sigs = $sigs;
        my %lineage_cache;   # one OTU lineage text is shared by all its members
        for my $member (keys %by_member) {
            my $entry = $by_member{$member};
            my $r = $entry->{row};
            my ($best) = sort { model_rank($b->[2]) <=> model_rank($a->[2]) || $a->[0] cmp $b->[0] } @{ $entry->{ids} };
            my $parsed = ReportingIdentityContract::parse_header($best->[1], $context)
                or fail("unrecognized or context-incompatible member id: $best->[1]");
            fail("member id parses to a different read: $best->[1]") unless $parsed->{read_id} eq (split /\|/, $member)[0];
            my ($uuid, $marker) = split /\|/, $member;
            my $groups = $evidence->{$member} // [];
            my %hashes = map { $_->{hash} => 1 } @$groups;
            my %taxids;
            my $hits = 0;
            for my $g (@$groups) { for my $c (@{ $g->{candidates} }) { $taxids{ $c->[1] } = 1; $hits++; } }
            my $observed = keys(%taxids) ? join(',', sort keys %taxids) : 'NA';
            my $declared = $r->{source_taxids} eq 'NA' ? 'NA' : join(',', sort split /,/, $r->{source_taxids});
            fail("sealed evidence and R4-B sidecar disagree on direct evidence for $member (evidence=$observed sidecar=$declared)")
                unless $observed eq $declared;
            my @cands;
            if (keys(%hashes) == 1) {
                @cands = sort { $a->[0] cmp $b->[0] } map { @{ $_->{candidates} } } @$groups;
                my %unique = map { join("\t", @$_) => $_ } @cands;
                @cands = sort { $a->[0] cmp $b->[0] } values %unique;
            }
            my $direct_hit = $hits ? 1 : 0;
            my ($hit_id, $hit_taxid, $aln, $perc, $evalue, $bits) = ('NA') x 6;
            if (@cands) {
                $aln = $cands[0][3];
                $perc = RTBioScan::R4A::display_identity($cands[0][4]);
                $evalue = RTBioScan::R4A::display_number($cands[0][2]);
                $bits = RTBioScan::R4A::display_number($cands[0][5]);
                if (@cands == 1) { $hit_id = accession_of($cands[0][0]); $hit_taxid = $cands[0][1]; }
            }
            my ($otub) = $r->{otu_id} =~ /\AOTUB_([0-9]+)-/;
            my $eligible = defined($kept) ? ($kept->{$otub} ? 1 : 0) : 'NA';
            my $lr = $lineage_cache{ $r->{lineage} } //= (TaxonUtil::canonical_lineage($r->{lineage}) || 0)
                or fail("noncanonical R4-B lineage for $member");
            my %row = (
                canonical_member => $member, uuid => $uuid, marker => $marker,
                read_id => $best->[1], barcode_by_homology => $parsed->{barcode_by_homology},
                basecalling_model => $parsed->{basecalling_model}, sample => $parsed->{sample},
                display_otu_key => $r->{otu_id}, stable_otu_key => $r->{stable_key},
                otu_status => $r->{status}, otu_taxid => $r->{taxid}, otu_depth => $r->{depth}, otu_origin => $r->{origin},
                member_count => $r->{member_count}, blast_eligible => $eligible, direct_hit => $direct_hit,
                read_status => $r->{read_status}, read_taxid => $r->{read_taxid}, read_depth => $r->{read_depth},
                read_origin => $r->{read_origin}, read_reason => $r->{read_reason},
                hit_id => $hit_id, hit_taxid => $hit_taxid, aln_length => $aln, perc_id => $perc, evalue => $evalue,
                bitscore => $bits, candidate_count => scalar(@cands), source_taxids => $r->{source_taxids},
            );
            @row{@RANKS} = @$lr;
            push @rows, \%row;
        }
        my %count_by_otu;
        $count_by_otu{ $_->{display_otu_key} }++ for @rows;
        for my $row (@rows) {
            fail("R4-B member_count disagrees with canonical membership for $row->{display_otu_key}")
                unless $count_by_otu{ $row->{display_otu_key} } == $row->{member_count};
        }
    }
    @rows = sort_rows(@rows);
    validate_rows(\@rows);
    my $signature = sha256_hex(json()->encode({
        contract => $CONTRACT, columns => \@COLUMNS, context => $context,
        r4b_signature => $r4b_sig, membership_sha256 => file_sha256($o{members}),
        eligible_sha256 => file_sha256($o{eligible}), evidence => $evidence_sigs,
    }));
    return ($signature, \@rows);
}

sub sort_rows {
    for my $r (@_) { $r->{_otub} //= 0 + (($r->{display_otu_key} =~ /\AOTUB_([0-9]+)/)[0] // 0); }
    return sort {
        $a->{marker} cmp $b->{marker}
            || $a->{_otub} <=> $b->{_otub}
            || $a->{canonical_member} cmp $b->{canonical_member}
    } @_;
}

# ---------------------------------------------------------------------------
sub reporting_text {
    my ($sig, $rows) = @_;
    my $body = join('', map { join("\t", map { defined($_) && $_ ne '' ? $_ : 'NA' } @$_{@COLUMNS}) . "\n" } @$rows);
    return "#RTB-R4D-REPORTING\t$VERSION\t$sig\n#columns\t" . join("\t", @COLUMNS) . "\n" . $body
        . "#END\t" . scalar(@$rows) . "\t" . sha256_hex($body) . "\n";
}

sub validate_row {
    my ($r) = @_;
    fail('empty or control reporting field') if grep { !defined($_) || $_ eq '' || /[\r\n\x00]/ } @$r{@COLUMNS};
    fail('invalid canonical member') unless $r->{canonical_member} eq "$r->{uuid}|$r->{marker}" && $r->{uuid} !~ /[|\s]/;
    fail('invalid projection identity') unless $r->{display_otu_key} =~ /\AOTUB_[0-9]+-\Q$r->{marker}\E\z/;
    fail('invalid stable key') unless $r->{stable_otu_key} eq 'NA' || $r->{stable_otu_key} =~ /\A\Q$r->{marker}\E\|[0-9a-f]{32}\z/;
    fail('invalid member relationship') unless $r->{read_id} =~ /\A\Q$r->{uuid}\E\|\Q$r->{marker}\E\|.*\|\Q$r->{display_otu_key}\E\z/;
    for my $prefix ('otu_', 'read_') {
        my ($status, $tax, $depth, $origin) = @$r{ map { $prefix . $_ } qw(status taxid depth origin) };
        fail('unknown assignment status') unless TaxonUtil::valid_assignment_status($status);
        fail('invalid signed taxid') unless $tax eq 'NA' || $tax =~ /\A-?[1-9][0-9]*\z/;
        fail('invalid depth') unless $depth =~ /\A(?:-1|[0-6])\z/;
        fail('invalid origin') unless $origin =~ /\A(?:DIRECT|LCA|NONE)\z/;
        if (usable($status)) { fail('usable status without lineage depth') unless $depth >= 0; }
        else { fail('rejected status carries assignment') unless $tax eq 'NA' && $depth == -1 && $origin eq 'NONE'; }
    }
    fail('otu depth disagrees with ranks') unless TaxonUtil::lineage_depth([ @$r{@RANKS} ]) == $r->{otu_depth};
    for (qw(member_count candidate_count)) { fail('invalid count') unless $r->{$_} =~ /\A(?:0|[1-9][0-9]*)\z/; }
    fail('invalid direct-hit flag') unless $r->{direct_hit} =~ /\A[01]\z/;
    fail('invalid eligibility flag') unless $r->{blast_eligible} =~ /\A(?:NA|[01])\z/;
    fail('direct-hit flag disagrees with evidence') if $r->{direct_hit} eq '1' xor $r->{source_taxids} ne 'NA';
    fail('hit metrics without direct hit') if !$r->{direct_hit} && grep { $r->{$_} ne 'NA' } qw(hit_id hit_taxid aln_length perc_id evalue bitscore);
    fail('single-candidate hit without accession') if $r->{candidate_count} eq '1' && $r->{hit_id} eq 'NA';
    fail('multi-candidate hit with accession') if $r->{candidate_count} > 1 && $r->{hit_id} ne 'NA';
    fail('invalid diagnostic signed ids') unless $r->{source_taxids} eq 'NA' || $r->{source_taxids} =~ /\A-?[1-9][0-9]*(?:,-?[1-9][0-9]*)*\z/;
    return join("\t", @$r{qw(marker stable_otu_key otu_status otu_taxid otu_depth otu_origin member_count blast_eligible)}, @$r{@RANKS});
}

# Whole-generation checks on validated rows: unique members, one projection per
# display key, per-projection cardinality, canonical order.
sub validate_rows {
    my ($rows) = @_;
    my (%seen, %projection, %otu_members);
    for my $r (@$rows) {
        my $common = validate_row($r);
        fail('duplicate canonical member') if $seen{ $r->{canonical_member} }++;
        fail('conflicting projection rows') if exists($projection{ $r->{display_otu_key} }) && $projection{ $r->{display_otu_key} } ne $common;
        $projection{ $r->{display_otu_key} } = $common;
        $otu_members{ $r->{display_otu_key} }++;
    }
    for my $otu (keys %otu_members) {
        my ($count) = (split /\t/, $projection{$otu})[6];
        fail("projection cardinality mismatch for $otu") unless $otu_members{$otu} == $count;
    }
    my @sorted = sort_rows(@$rows);
    for my $i (0 .. $#$rows) { fail('reporting rows are not in canonical order') unless $rows->[$i] == $sorted[$i]; }
    return 1;
}

sub read_reporting {
    my ($path, $expected) = @_;
    open my $f, '<', $path or fail("read reporting sidecar $path: $!");
    my $head = <$f> // '';
    fail('unsupported reporting schema') unless $head =~ /\A#RTB-R4D-REPORTING\t1\t([0-9a-f]{64})\n\z/;
    my $sig = $1;
    fail('stale reporting signature') if defined($expected) && $sig ne $expected;
    fail('invalid reporting columns') unless (<$f> // '') eq "#columns\t" . join("\t", @COLUMNS) . "\n";
    my $sha = Digest::SHA->new(256);
    my ($count, $end) = (0, 0);
    my @rows;
    while (my $line = <$f>) {
        fail('truncated reporting row') unless $line =~ /\n\z/;
        if ($line =~ /^#END\t/) {
            fail('incomplete reporting envelope') unless $line eq "#END\t$count\t" . $sha->hexdigest . "\n";
            fail('data after reporting envelope') if defined(<$f>);
            $end = 1;
            last;
        }
        $sha->add($line);
        $count++;
        chomp $line;
        my @v = split /\t/, $line, -1;
        fail('reporting field count') unless @v == @COLUMNS;
        fail('empty or control reporting field') if grep { $_ eq '' || /[\r\n\x00]/ } @v;
        my %r;
        @r{@COLUMNS} = @v;
        push @rows, \%r;
    }
    close $f or fail('close reporting sidecar');
    fail('missing reporting footer') unless $end;
    validate_rows(\@rows);
    return ($sig, \@rows);
}

sub public_row {
    my ($r) = @_;
    my @lineage = usable($r->{otu_status}) ? @$r{@RANKS} : @NOADAPTER_UNASSIGNED;
    return join("\t", @$r{qw(read_id barcode_by_homology basecalling_model sample hit_id read_taxid aln_length perc_id display_otu_key otu_taxid)}, @lineage);
}
sub public_text {
    my ($rows, $filter) = @_;
    my $text = public_header() . "\n";
    for my $r (@$rows) { next if $filter && !$filter->($r); $text .= public_row($r) . "\n"; }
    return $text;
}
sub noadapter_text { my ($rows) = @_; return public_text($rows, sub { is_no_adapter_sample($_[0]{sample}) }); }

sub atomic_text {
    my ($path, $text) = @_;
    my ($f, $tmp) = tempfile('.r4d-publish-XXXXXX', DIR => dirname($path), UNLINK => 0);
    my $ok = eval { print {$f} $text or fail("write $tmp: $!"); close $f or fail("close $tmp: $!"); rename $tmp, $path or fail("rename $path: $!"); 1 };
    my $err = $@;
    unlink $tmp if -e $tmp;
    die $err unless $ok;
}

# ---------------------------------------------------------------------------
# Cumulative snapshot publication (R4-I2; protocol in RTBioScan::R4DCumulative
# and docs/output.md). The three state products form one generation named by
# the sealed commit record:
#   1. immutable members `<name>.gen-<generation>` are written and made durable;
#   2. the record is replaced by one rename -- the commit point;
#   3. every public name that changes is retracted (renamed to
#      `<name>.bak.<pid>`) before any is projected onto its committed member,
#      so the public names present at any instant hold one generation.
# Process death before the commit leaves the previous generation
# authoritative, after it the new one; readers resolving the record never see
# a mixture. A synchronous failure restores the previous state exactly (names,
# bytes, inodes and record). Old members are released only after the commit,
# and the previous generation is kept for readers that resolved it just
# before. One publisher per barcode holds the lock. A record-less authentic
# pre-R4-D snapshot (an outdir written before the upgrade) is first sealed as a
# legacy generation under that lock (RTBioScan::R4DCumulative::seal_pre_r4d),
# so it stays authoritative until the new generation commits; a record-less
# `_state` that is not authentic is never written
# (RTBioScan::R4DCumulative::classify_publication).
sub sync_directory { RTBioScan::R4DCumulative::sync_directory(@_) }
sub acquire_publication_lock { RTBioScan::R4DCumulative::acquire_lock(@_) }
sub release_publication_lock { RTBioScan::R4DCumulative::release_lock(@_) }

sub write_durable {
    my ($dir, $bc, $text) = @_;
    my ($f, $t) = tempfile(".r4d-publish-$bc-XXXXXX", DIR => $dir, UNLINK => 0);
    my $ok = eval {
        print {$f} $text or fail("write $t: $!");
        $f->flush or fail("write $t: $!");
        $f->sync or fail("sync $t: $!");
        close $f or fail("close $t: $!");
        1;
    };
    return $t if $ok;
    my $err = $@;
    unlink $t;
    die $err;
}

# Hard links make a member and its public name one file. On a filesystem
# without hard links (RTBioScan::R4DCumulative::hardlinks_unsupported) the name
# is a durable byte copy instead; any other link failure is an error.
sub link_or_copy {
    my ($from, $to, $bc) = @_;
    return 1 if link($from, $to);
    my $errno = $! + 0;
    if (!RTBioScan::R4DCumulative::hardlinks_unsupported(dirname($to), $errno)) { $! = $errno; return 0; }
    my $text = do { open my $f, '<:raw', $from or return 0; local $/; my $t = <$f>; close $f; $t // '' };
    my $t = write_durable(dirname($to), $bc, $text);
    return 1 if rename $t, $to;
    my $err = $!;
    unlink $t;
    $! = $err;
    return 0;
}

# A public name already holds a committed member: the same file, or (without
# hard links) the same bytes -- compared by size first, then SHA-256.
sub same_content {
    my ($copy, $name, $size, $sha) = @_;
    return 1 if RTBioScan::R4DCumulative::same_file($copy, $name);
    return RTBioScan::R4DCumulative::file_matches($name, $size, $sha);
}

# A same-inode rollback copy of the current record, `<record>.bak.<pid>`.
# link(2) cannot replace a name, so one left under the same pid by a killed
# publisher is replaced by rename(2).
sub stage_backup {
    my ($path, $bc, $scratch) = @_;
    my $bak = "$path.bak.$$";
    my $t = dirname($path) . "/.r4d-publish-$bc-bak-" . basename($path) . ".$$";
    unlink $t if lstat $t;
    push @$scratch, $t;
    link_or_copy($path, $t, $bc) or fail("stage backup $bak: $!");
    rename $t, $bak or fail("stage backup $bak: $!");
    return $bak;
}

# Remove non-authoritative residue after a successful publication: temps and
# rollback copies of this barcode, legacy R4-D temps and backups, and members
# of every generation except the committed one and its predecessor -- only
# while the record still names this commit (a restore may have replaced the
# state meanwhile).
sub cleanup_residue {
    my ($dir, $bc, $keep) = @_;
    return unless RTBioScan::R4DCumulative::record_names($dir, $bc, $keep);
    my $names = RTBioScan::R4DCumulative::names($bc);
    my $names_re = join('|', map { quotemeta } sort values %$names);
    my $record = basename(RTBioScan::R4DCumulative::record_path($dir, $bc));
    for my $e (sort(RTBioScan::R4DCumulative::dir_entries($dir))) {
        my $stale = $e =~ /\A\.r4d-publish-(?:\Q$bc\E-|[A-Za-z0-9_]{6}\z)/
            || $e =~ /\A(?:(?:$names_re)|\Q$record\E)\.bak\./
            || ($e =~ /\A(?:$names_re)\.gen-(.*)\z/s && !$keep->{$1});
        unlink "$dir/$e" if $stale;
    }
}

sub publish_generation {
    my ($dir, $bc, $live, $texts) = @_;
    my @order = sort { $live->{$a} cmp $live->{$b} } keys %$live;
    my %want = map { $_ => [ length($texts->{$_}), sha256_hex($texts->{$_}) ] } @order;
    my $record = RTBioScan::R4DCumulative::record_path($dir, $bc);
    my $lock_path = RTBioScan::R4DCumulative::lock_path($dir, $bc);
    my $state_dir = RTBioScan::R4DCumulative::is_state_dir($dir);
    if ($state_dir) {
        # A state being reset or restored is never written; a record-less state
        # is classified read-only before the lock is taken, so a refused one --
        # legacy-like tables that are not authentic, residue no publication
        # leaves -- is left exactly as it is.
        RTBioScan::R4DCumulative::restart_fence($dir);
        # (a record committed meanwhile by a concurrent sealing is handled under the lock)
        eval { RTBioScan::R4DCumulative::classify_publication($dir, $bc); 1 } or do { my $e = $@; die $e unless lstat $record; }
            unless lstat $record;
    }
    my $lock = acquire_publication_lock($lock_path);
    my (%tmp, %created, %retracted, %projected, @scratch, $record_bak, $committed, $keep);
    my $ok = eval {
        my $current;
        if (lstat $record) {
            $current = eval { RTBioScan::R4DCumulative::parse_record($record, $bc) };
            print STDERR "WARN: R4-D cumulative: replacing unreadable commit record: $@" unless $current;
            # An earlier candidate's backup record restored from a results snapshot
            # has no reporting member: it names no complete generation of this
            # state. A legacy backup is the whole legacy generation.
            $current = undef if $current && $current->{kind} eq 'backup';
        } elsif ($state_dir) {
            # Classified again under the lock. Upgrade: an authentic pre-R4-D
            # snapshot is sealed first, so it stays authoritative until this
            # generation commits; a complete R4-D snapshot, a pristine state and
            # an interrupted first publication are superseded.
            my ($class, $validated) = RTBioScan::R4DCumulative::classify_publication($dir, $bc);
            $current = RTBioScan::R4DCumulative::seal_pre_r4d($dir, $bc, locked => 1, validated => $validated)
                if $class eq 'pre-r4d';
        }
        my (undef, $generation) = RTBioScan::R4DCumulative::record_text($bc, \%want, undef);
        my %gen = map { $_ => RTBioScan::R4DCumulative::generation_path($dir, basename($live->{$_}), $generation) } @order;
        my %valid = map { $_ => RTBioScan::R4DCumulative::file_matches($gen{$_}, @{ $want{$_} }) } @order;
        my $previous = $current ? ($current->{generation} ne $generation ? $current->{generation} : $current->{previous}) : undef;
        # Retry, replay or unchanged round: the generation is already committed
        # and only lagging public names are reconciled below.
        if (!($current && $current->{generation} eq $generation && !grep { !$valid{$_} } @order)) {
            # 1. Immutable members of the new generation. A product unchanged
            #    since the committed generation is linked, not rewritten.
            for my $p (@order) {
                next if $valid{$p};
                my $source;
                if ($current && $current->{entries}{$p}) {
                    my $e = $current->{entries}{$p};
                    my $old = RTBioScan::R4DCumulative::generation_path($dir, $e->{name}, $current->{generation});
                    $source = $old if $e->{sha} eq $want{$p}[1] && RTBioScan::R4DCumulative::file_matches($old, @{ $want{$p} });
                }
                if (!defined $source) {
                    $tmp{$p} = write_durable($dir, $bc, $texts->{$p});
                    $source = $tmp{$p};
                }
                my $fresh = !lstat $gen{$p};
                unlink $gen{$p} or fail("replace damaged copy $gen{$p}: $!") unless $fresh;
                link_or_copy($source, $gen{$p}, $bc) or fail("link $gen{$p}: $!");
                $created{ $gen{$p} } = RTBioScan::R4DCumulative::identity($gen{$p}) if $fresh;
            }
            $record_bak = stage_backup($record, $bc, \@scratch) if lstat $record;
            sync_directory($dir);
            # 2. Commit.
            my ($record_text) = RTBioScan::R4DCumulative::record_text($bc, \%want, $previous);
            my $t = write_durable($dir, $bc, $record_text);
            push @scratch, $t;
            rename $t, $record or fail("commit $record: $!");
            $committed = 1;
            sync_directory($dir);
        }
        # 3. Retract every public name that does not hold its committed member
        #    before projecting any: the names present never mix generations.
        my @lag = grep { !same_content($gen{$_}, $live->{$_}, @{ $want{$_} }) } @order;
        for my $p (@lag) {
            next unless lstat $live->{$p};
            my $bak = "$live->{$p}.bak.$$";
            unlink $bak if lstat $bak;
            rename $live->{$p}, $bak or fail("stage backup $bak: $!");
            $retracted{$p} = $bak;
        }
        for my $p (@lag) {
            if (!defined $tmp{$p}) {
                my $t = "$dir/.r4d-publish-$bc-link-$p.$$";
                unlink $t if lstat $t;
                push @scratch, $t;
                link_or_copy($gen{$p}, $t, $bc) or fail("link $t: $!");
                $tmp{$p} = $t;
            }
            rename $tmp{$p}, $live->{$p} or fail("replace $live->{$p}: $!");
            delete $tmp{$p};
            $projected{$p} = 1;
        }
        $keep = { $generation => 1, (defined($previous) ? ($previous => 1) : ()) };
        1;
    };
    my $err = $@;
    if (!$ok) {
        # Undo the projection, then restore the retracted names, and only then
        # the previous record: while the new record exists it names a complete
        # generation, and the public names never mix generations. If a step
        # fails the committed generation stays authoritative.
        my $restored = 1;
        for my $p (reverse @order) {
            next unless $projected{$p};
            unlink($live->{$p}) or $restored = 0;
        }
        if ($restored) {
            for my $p (@order) {
                next unless defined $retracted{$p};
                rename($retracted{$p}, $live->{$p}) or $restored = 0;
            }
        }
        if ($committed && $restored) {
            if (defined $record_bak) { rename($record_bak, $record) or $restored = 0; }
            else { unlink($record) or $restored = 0; }
        }
        if (!$restored) {
            $err .= "R4-D: the committed cumulative generation stays authoritative (previous state not fully restorable)\n";
        } else {
            RTBioScan::R4DCumulative::unlink_own(\%created);
            unlink $record_bak if defined($record_bak) && lstat $record_bak;
        }
        unlink $_ for grep { defined && lstat $_ } values(%tmp), @scratch;
        release_publication_lock($lock, $lock_path);
        die $err;
    }
    cleanup_residue($dir, $bc, $keep);
    release_publication_lock($lock, $lock_path);
}

sub publish_cumulative {
    my (%o) = @_;
    my ($sig, $rows) = read_reporting($o{reporting});
    my $text = reporting_text($sig, $rows);
    my $expected = do { local $/; open my $f, '<', $o{reporting} or fail("read $o{reporting}: $!"); my $t = <$f>; close $f; $t };
    fail('reporting sidecar bytes are not canonical') unless $text eq $expected;
    my %live = (reporting => $o{state_reporting}, public => $o{state_public}, noadapter => $o{state_noadapter});
    my ($bc) = basename($o{state_reporting} // '') =~ /\A(.+)_blast_otu_reporting_v1\.tsv\z/;
    fail('cumulative state reporting path must be <dir>/<barcode>_blast_otu_reporting_v1.tsv') unless defined $bc;
    my $dir = dirname($o{state_reporting});
    my $names = RTBioScan::R4DCumulative::names($bc);
    for my $p (sort keys %$names) {
        fail('cumulative state paths must be the three products of one barcode in one directory')
            unless defined($live{$p}) && $live{$p} eq "$dir/$names->{$p}";
    }
    publish_generation($dir, $bc, \%live, {
        reporting => $text,
        public => public_text($rows),
        noadapter => $o{noadapter_enabled} ? noadapter_text($rows) : public_header() . "\n",
    });
    return scalar @$rows;
}

# ---------------------------------------------------------------------------
sub blank_counts { return { map { $_ => 0 } @STATUSES }; }
sub metrics {
    my ($rows, %o) = @_;
    my $bucket = sub {
        return {
            canonical_member_count => 0, canonical_direct_hit_count => 0, canonical_direct_attribution_count => 0,
            canonical_assigned_count => { map { $_ => 0 } @LEVELS }, canonical_unassigned_count => { map { $_ => 0 } @LEVELS },
            status_counts => blank_counts(), read_status_counts => blank_counts(), read_reason_counts => {},
            otu_count => 0, stable_otu_count => 0, otu_without_stable_key_count => 0,
            assigned_otu_count => { map { $_ => 0 } @LEVELS }, assigned_stable_otu_count => { map { $_ => 0 } @LEVELS },
            blast_eligible_otu_count => 0, blast_eligible_known => 0,
            taxon_count => { map { $_ => 0 } @LEVELS }, taxon_member_count_sum => { map { $_ => 0 } @LEVELS },
            rank_gap_count => { map { $_ => 0 } @LEVELS },
            _otus => {}, _stable => {}, _eligible => {}, _taxa => { map { $_ => {} } @LEVELS },
        };
    };
    my $total = $bucket->();
    my (%by_marker, %by_sample_marker);
    my $add = sub {
        my ($b, $r) = @_;
        $b->{canonical_member_count}++;
        $b->{canonical_direct_hit_count}++ if $r->{direct_hit};
        $b->{canonical_direct_attribution_count}++ if usable($r->{read_status});
        $b->{status_counts}{ $r->{otu_status} }++;
        $b->{read_status_counts}{ $r->{read_status} }++;
        $b->{read_reason_counts}{ $r->{read_reason} }++ if !usable($r->{read_status}) && $r->{read_reason} ne 'NA';
        my $ok = usable($r->{otu_status});
        for my $level (@LEVELS) {
            if ($ok && $r->{otu_depth} >= $LEVEL_DEPTH{$level}) {
                $b->{canonical_assigned_count}{$level}++;
                my $taxon = $r->{$level};
                $b->{_taxa}{$level}{$taxon}++;
                $b->{rank_gap_count}{$level}++ if $taxon eq 'NA';
            } else { $b->{canonical_unassigned_count}{$level}++; }
        }
        my $otu = $r->{display_otu_key};
        if (!$b->{_otus}{$otu}++) {
            $b->{otu_count}++;
            if ($r->{stable_otu_key} eq 'NA') { $b->{otu_without_stable_key_count}++; }
            if ($r->{blast_eligible} ne 'NA') { $b->{blast_eligible_known}++; $b->{blast_eligible_otu_count}++ if $r->{blast_eligible}; }
            for my $level (@LEVELS) { $b->{assigned_otu_count}{$level}++ if $ok && $r->{otu_depth} >= $LEVEL_DEPTH{$level}; }
        }
        if ($r->{stable_otu_key} ne 'NA' && !$b->{_stable}{ $r->{stable_otu_key} }++) {
            $b->{stable_otu_count}++;
            for my $level (@LEVELS) { $b->{assigned_stable_otu_count}{$level}++ if $ok && $r->{otu_depth} >= $LEVEL_DEPTH{$level}; }
        }
    };
    for my $r (@$rows) {
        $add->($total, $r);
        $add->($by_marker{ $r->{marker} } //= $bucket->(), $r);
        $add->($by_sample_marker{"$r->{sample}\t$r->{marker}"} //= $bucket->(), $r);
    }
    my $finish = sub {
        my ($b) = @_;
        for my $level (@LEVELS) {
            my $taxa = $b->{_taxa}{$level};
            $b->{taxon_count}{$level} = scalar keys %$taxa;
            my $sum = 0; $sum += $_ for values %$taxa;
            $b->{taxon_member_count_sum}{$level} = $sum;
            $b->{taxon_member_counts}{$level} = { %$taxa } if $o{full};
            my $den = $b->{canonical_member_count};
            $b->{assigned_fraction}{$level} = {
                numerator => $b->{canonical_assigned_count}{$level}, denominator => $den,
                fraction => $den > 0 ? 0 + sprintf('%.6f', $b->{canonical_assigned_count}{$level} / $den) : undef,
            };
        }
        $b->{blast_eligible_otu_count} = undef unless $b->{blast_eligible_known};
        delete @$b{qw(_otus _stable _eligible _taxa blast_eligible_known)};
        return $b;
    };
    my %out = ( contract => 'r4d-v1', %{ $finish->($total) } );
    $out{by_marker} = { map { $_ => $finish->($by_marker{$_}) } keys %by_marker };
    $out{by_sample_marker} = [ map { my ($s, $m) = split /\t/, $_; { sample => $s, marker => $m, %{ $finish->($by_sample_marker{$_}) } } } sort keys %by_sample_marker ];
    return \%out;
}

# ---------------------------------------------------------------------------
sub main {
    my @args = @ARGV;
    my $mode = shift @args // '';
    my %o;
    if ($mode eq '--build') {
        GetOptionsFromArray(\@args, \%o, 'sidecar=s', 'members=s', 'evidence-dir=s', 'eligible=s', 'context=s',
            'out-public=s', 'out-reporting=s') or fail('invalid --build options');
        $o{context} //= $ENV{RTBIOSCAN_DEMUX_IDENTITY_CONTEXT};
        fail('--members, --evidence-dir, --out-public and --out-reporting are required')
            unless defined($o{members}) && defined($o{'evidence-dir'}) && defined($o{'out-public'}) && defined($o{'out-reporting'});
        my ($sig, $rows) = build(sidecar => $o{sidecar}, members => $o{members}, evidence_dir => $o{'evidence-dir'},
            eligible => $o{eligible}, context => $o{context});
        my $text = reporting_text($sig, $rows);
        atomic_text($o{'out-reporting'}, $text);
        fail('written reporting sidecar does not match generated bytes') unless file_sha256($o{'out-reporting'}) eq sha256_hex($text);
        atomic_text($o{'out-public'}, public_text($rows));
        print STDERR "INFO: r4d_reporting canonical_members=" . scalar(@$rows) . " signature=$sig\n";
    } elsif ($mode eq '--publish-cumulative') {
        GetOptionsFromArray(\@args, \%o, 'reporting=s', 'state-dir=s', 'barcode=s', 'noadapter-enabled=s') or fail('invalid --publish-cumulative options');
        fail('--reporting, --state-dir and --barcode are required') unless defined($o{reporting}) && defined($o{'state-dir'}) && defined($o{barcode});
        fail('--noadapter-enabled must be 0 or 1') if defined($o{'noadapter-enabled'}) && $o{'noadapter-enabled'} !~ /\A[01]\z/;
        my $n = publish_cumulative(reporting => $o{reporting},
            state_reporting => "$o{'state-dir'}/$o{barcode}_blast_otu_reporting_v1.tsv",
            state_public => "$o{'state-dir'}/$o{barcode}_blast_otu_pretax_rpt.txt",
            state_noadapter => "$o{'state-dir'}/$o{barcode}_blast_otu_noadapter_rpt.txt",
            noadapter_enabled => $o{'noadapter-enabled'} // 0);
        print STDERR "INFO: r4d_cumulative_snapshot canonical_members=$n\n";
    } elsif ($mode eq '--project-noadapter') {
        my ($reporting, $out) = @args;
        fail('usage: --project-noadapter <reporting.tsv> <out.txt>') unless defined($reporting) && defined($out);
        my (undef, $rows) = read_reporting($reporting);
        atomic_text($out, noadapter_text($rows));
    } elsif ($mode eq '--project-public') {
        my ($reporting, $out) = @args;
        fail('usage: --project-public <reporting.tsv> <out.txt>') unless defined($reporting) && defined($out);
        my (undef, $rows) = read_reporting($reporting);
        atomic_text($out, public_text($rows));
    } elsif ($mode eq '--metrics') {
        GetOptionsFromArray(\@args, \%o, 'reporting=s', 'full') or fail('invalid --metrics options');
        fail('--reporting is required') unless defined $o{reporting};
        my (undef, $rows) = read_reporting($o{reporting});
        print json()->pretty->encode(metrics($rows, full => $o{full}));
    } elsif ($mode eq '--validate') {
        my ($reporting) = @args;
        fail('usage: --validate <reporting.tsv>') unless defined $reporting;
        my ($sig, $rows) = read_reporting($reporting);
        print "valid\t$sig\t" . scalar(@$rows) . "\n";
    } else {
        fail('usage: r4_reporting_contract.pl --build|--publish-cumulative|--project-noadapter|--project-public|--metrics|--validate ...');
    }
}

main() unless caller;
1;
