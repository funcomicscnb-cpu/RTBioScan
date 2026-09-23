package RTBioScan::OTURefineBlastreport;

use strict;
use warnings;

use Exporter 'import';
use Time::HiRes qw(time);
use JSON::PP ();
use File::Basename qw(dirname);
use Digest::SHA qw(sha256_hex);
require(dirname(__FILE__).'/../taxon_util.pl') unless defined &TaxonUtil::canonical_lineage;

our @EXPORT_OK = qw(
    append_phase_timing_row
    collect_cluster_taxids_from_cluster_file
    collect_cluster_taxids_from_inputs
    collect_entries_from_inputs
    collect_entries_from_cluster_file
    emit_annotated_entries
    load_cluster_taxids_from_file
    load_target_tax_maps
    print_cluster_taxids
    print_pairs_only_from_cluster_taxids_file
    print_full_output
    print_pairs_only
    write_cluster_taxids_with_phase_timings
);

sub is_valid_taxid {
    my ($taxid) = @_;
    return defined $taxid && $taxid ne '' && $taxid ne 'NA';
}

sub normalize_read_key {
    my ($read_id) = @_;
    return '' if !defined $read_id;
    my ($base,$marker)=split /\|/,$read_id;
    return '' unless defined($marker) && $marker ne '';
    return $base.'|'.TaxonUtil::canonical_marker_token($marker);
}

# Compatibility CLI: unresolved ties remain unassigned. The sealed production
# path below has the lineage authority required to resolve the full LCA.
sub choose_cluster_taxid {
    my ($counts_ref) = @_;
    my @valid=grep { is_valid_taxid($_) } keys %{$counts_ref // {}};
    return exists($counts_ref->{NA}) ? 'NA' : '' unless @valid;
    my $max=0; for (@valid) { $max=$counts_ref->{$_} if $counts_ref->{$_}>$max; }
    my @winners=grep { $counts_ref->{$_}==$max } @valid;
    return @winners==1 ? $winners[0] : 'NA';
}
sub _record_member_vote {
    my ($counts,$id,$taxid)=@_;
    my ($base,$marker)=split /\|/,$id;
    $marker=TaxonUtil::canonical_marker_token($marker // '') // '';
    $counts->{$marker}{$base}{$taxid}=1 if defined($taxid) && $taxid ne '';
}
sub _projection_taxids {
    my ($members)=@_;my %out;
    for my $marker (keys %$members) {
        my %counts;
        for my $base (keys %{$members->{$marker}}) {
            my @ids=keys %{$members->{$marker}{$base}};
            $counts{@ids==1 ? $ids[0] : 'NA'}++;
        }
        $out{$marker}=choose_cluster_taxid(\%counts);
    }
    return \%out;
}
sub _projection_taxid {
    my ($value,$id)=@_;
    return $value unless ref($value) eq 'HASH';
    my (undef,$marker)=split /\|/,$id;
    return $value->{TaxonUtil::canonical_marker_token($marker // '') // ''} // '';
}

sub load_target_tax_maps {
    my ($input1) = @_;

    my (%target_tax, %target_tax_norm);
    open(my $report_fh, '<', $input1) or die "Cannot open $input1: $!";
    while (my $line = <$report_fh>) {
        chomp $line;
        next if $line !~ /\S/;
        my ($seq_id, $taxid) = split /\;/, $line;
        next if !defined $seq_id || !defined $taxid;
        $seq_id =~ s/^\s+|\s+$//g;
        $taxid =~ s/^\s+|\s+$//g;
        $target_tax{$seq_id} = exists($target_tax{$seq_id}) && $target_tax{$seq_id} ne $taxid ? 'NA' : $taxid;
        my $normalized = normalize_read_key($seq_id);
        $target_tax_norm{$normalized} = exists($target_tax_norm{$normalized}) && $target_tax_norm{$normalized} ne $taxid ? 'NA' : $taxid if $normalized ne '';
    }
    close $report_fh;

    return (\%target_tax, \%target_tax_norm);
}

sub _append_phase_timing {
    my ($phase_file, $round_id, $phase, $start_ts, $end_ts) = @_;
    return if !defined $round_id || $round_id eq '';

    my $ms = 0;
    my $seconds = 0;
    if (defined $start_ts && defined $end_ts && $end_ts >= $start_ts) {
        $ms = int((($end_ts - $start_ts) * 1000) + 0.5);
        $ms = 0 if $ms < 0;
        $seconds = int($ms / 1000);
        $seconds = 0 if $seconds < 0;
    }

    if (defined $phase_file && $phase_file ne '') {
        if (open(my $phase_fh, '>>', $phase_file)) {
            print {$phase_fh} join("\t", $round_id, $phase, $seconds) . "\n";
            close $phase_fh;
        }
    }

    my $phase_ms_file = $ENV{OTU_REFINE_PHASE_TIMINGS_MS_FILE} // '';
    if ($phase_ms_file ne '') {
        if (open(my $phase_ms_fh, '>>', $phase_ms_file)) {
            print {$phase_ms_fh} join("\t", $round_id, $phase, $seconds, $ms) . "\n";
            close $phase_ms_fh;
        }
    }
}

sub append_phase_timing_row {
    my ($phase_file, $round_id, $phase, $start_ts, $end_ts) = @_;
    _append_phase_timing($phase_file, $round_id, $phase, $start_ts, $end_ts);
}

sub _finalize_cluster_entries {
    my ($cluster_id_ref, $members_ref, $counts_ref, $entries_ref) = @_;
    return if !defined ${$cluster_id_ref};
    return if !@{$members_ref};

    my $cluster_taxid = _projection_taxids($counts_ref);
    for my $member_id (@{$members_ref}) {
        my $marker = '';
        if ($member_id =~ /^[^|]+\|([^|]+)/) {
            $marker = $1 // '';
        }
        my $otu_tag = "OTUB_${$cluster_id_ref}";
        $otu_tag .= "-$marker" if defined $marker && $marker ne '';
        push @{$entries_ref}, [ "$member_id|$otu_tag", _projection_taxid($cluster_taxid,$member_id) ];
    }
}

sub _finalize_cluster_taxid {
    my ($cluster_id_ref, $counts_ref, $cluster_taxids_ref) = @_;
    return if !defined ${$cluster_id_ref};

    my $cluster_taxid = _projection_taxids($counts_ref);
    push @{$cluster_taxids_ref}, [ ${$cluster_id_ref}, $cluster_taxid ];
}

sub collect_cluster_taxids_from_cluster_file {
    my ($cluster_file, $target_tax_ref, $target_tax_norm_ref) = @_;

    my @cluster_taxids;
    my $current_cluster_id;
    my %current_tax_counts;

    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            _finalize_cluster_taxid(
                \$current_cluster_id,
                \%current_tax_counts,
                \@cluster_taxids,
            );
            $current_cluster_id = $1;
            %current_tax_counts = ();
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $ref_id = $1;
        my $taxid = $target_tax_ref->{$ref_id};
        if (!defined $taxid || $taxid eq '') {
            my $normalized = normalize_read_key($ref_id);
            $taxid = $target_tax_norm_ref->{$normalized} if $normalized ne '';
            $target_tax_ref->{$ref_id} = $taxid if defined $taxid && $taxid ne '';
        }
        _record_member_vote(\%current_tax_counts,$ref_id,$taxid);
    }
    close $cluster_fh;

    _finalize_cluster_taxid(
        \$current_cluster_id,
        \%current_tax_counts,
        \@cluster_taxids,
    );

    return \@cluster_taxids;
}

sub collect_entries_from_cluster_file {
    my ($cluster_file, $target_tax_ref, $target_tax_norm_ref) = @_;

    my @entries;
    my $current_cluster_id;
    my @current_members;
    my %current_tax_counts;

    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            _finalize_cluster_entries(
                \$current_cluster_id,
                \@current_members,
                \%current_tax_counts,
                \@entries,
            );
            $current_cluster_id = $1;
            @current_members = ();
            %current_tax_counts = ();
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $ref_id = $1;
        push @current_members, $ref_id;

        my $taxid = $target_tax_ref->{$ref_id};
        if (!defined $taxid || $taxid eq '') {
            my $normalized = normalize_read_key($ref_id);
            $taxid = $target_tax_norm_ref->{$normalized} if $normalized ne '';
            $target_tax_ref->{$ref_id} = $taxid if defined $taxid && $taxid ne '';
        }
        _record_member_vote(\%current_tax_counts,$ref_id,$taxid);
    }
    close $cluster_fh;

    _finalize_cluster_entries(
        \$current_cluster_id,
        \@current_members,
        \%current_tax_counts,
        \@entries,
    );

    return \@entries;
}

sub collect_entries_from_inputs {
    my ($input1, $cluster_file) = @_;
    my ($target_tax_ref, $target_tax_norm_ref) = load_target_tax_maps($input1);
    return collect_entries_from_cluster_file($cluster_file, $target_tax_ref, $target_tax_norm_ref);
}

sub collect_cluster_taxids_from_inputs {
    my ($input1, $cluster_file) = @_;
    my ($target_tax_ref, $target_tax_norm_ref) = load_target_tax_maps($input1);
    return collect_cluster_taxids_from_cluster_file($cluster_file, $target_tax_ref, $target_tax_norm_ref);
}

sub load_cluster_taxids_from_file {
    my ($cluster_taxids_file) = @_;

    my %cluster_taxids;
    open(my $cluster_taxids_fh, '<', $cluster_taxids_file)
        or die "Cannot open $cluster_taxids_file: $!";
    while (my $line = <$cluster_taxids_fh>) {
        chomp $line;
        next if $line !~ /\S/;
        my ($cluster_id, $taxid) = split /\t/, $line, 2;
        next if !defined $cluster_id || $cluster_id eq '';
        $taxid = '' if !defined $taxid;
        $cluster_taxids{$cluster_id} = $taxid =~ /^\{/ ? JSON::PP->new->decode($taxid) : $taxid;
    }
    close $cluster_taxids_fh;

    return \%cluster_taxids;
}

sub _load_lineage_overrides {
    my ($lineage_file) = @_;
    my %id2lineage;

    open(my $lineage_fh, '<', $lineage_file) or die "I couldn't open $lineage_file\n";
    while (my $line = <$lineage_fh>) {
        chomp $line;
        next if $line !~ /\S/;
        my ($taxid, $lineage) = split /\t/, $line, 2;
        next if !defined $taxid || !defined $lineage;
        $id2lineage{$taxid} = $lineage;
    }
    close $lineage_fh;

    return \%id2lineage;
}

sub _resolve_taxid_lineages {
    my ($entries_ref, $id2lineage_ref) = @_;
    my %needed_taxids;

    for my $entry_ref (@{$entries_ref}) {
        my $taxid = $entry_ref->[1];
        $needed_taxids{$taxid} = 1 if defined $taxid && $taxid ne '';
    }

    my %taxid_lineage;
    my @pending = sort { $a <=> $b } grep { defined($_) && $_ =~ /^\d+$/ } keys %needed_taxids;
    while (@pending) {
        my @batch = splice @pending, 0, 100000;
        my $input = join("\n", @batch) . "\n";
        my $output = `printf '%s' "$input" | taxonkit lineage 2>/dev/null | taxonkit reformat -f "{K};{p};{c};{o};{f};{g};{s}" -P | cut -f3`;
        my @lines = split /\n/, $output;
        for my $idx (0 .. $#batch) {
            my $taxid = $batch[$idx];
            my $lineage = $lines[$idx] // '';
            $lineage =~ s/\s+$//;
            $taxid_lineage{$taxid} = $lineage if $lineage ne '';
        }
    }

    return \%taxid_lineage;
}

sub emit_annotated_entries {
    my ($entries_ref, $lineage_file, $out_fh, $stats_ref) = @_;
    my $header = "#seq_id\ttax_id\tlineage\n";
    print {$out_fh} $header;
    if (defined $stats_ref && ref($stats_ref) eq 'HASH') {
        $stats_ref->{annotated_rows} = 0 if !defined $stats_ref->{annotated_rows};
        $stats_ref->{annotated_bytes} = 0 if !defined $stats_ref->{annotated_bytes};
        $stats_ref->{annotated_bytes} += length($header);
    }
    return if !defined $lineage_file || !-e $lineage_file;

    my $phase_file = $ENV{OTU_REFINE_PHASE_TIMINGS_FILE} // '';
    my $round_id = $ENV{OTU_REFINE_ROUND_ID} // '';
    my $t_lineage_start = time();
    my $id2lineage_ref = _load_lineage_overrides($lineage_file);
    my $taxid_lineage_ref = _resolve_taxid_lineages($entries_ref, $id2lineage_ref);
    my $t_lineage_end = time();
    _append_phase_timing($phase_file, $round_id, 'lineage_resolution', $t_lineage_start, $t_lineage_end);
    my %lineage_cache;
    my $fallback_lineage = 'K__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned';

    my $t_emit_start = time();
    for my $entry_ref (@{$entries_ref}) {
        my ($seq_id, $otu_taxid) = @{$entry_ref};
        $otu_taxid = '' if !defined $otu_taxid;
        $otu_taxid =~ s/^\s+//;
        $otu_taxid =~ s/\s+$//;

        my $lineage = $lineage_cache{$otu_taxid};
        if (!defined $lineage || $lineage eq '') {
            if ($otu_taxid eq '' || $otu_taxid eq 'NA') {
                $lineage = $fallback_lineage;
            } elsif (exists $id2lineage_ref->{$otu_taxid}) {
                $lineage = $id2lineage_ref->{$otu_taxid};
            } elsif (exists $taxid_lineage_ref->{$otu_taxid}) {
                $lineage = $taxid_lineage_ref->{$otu_taxid};
            } else {
                $lineage = $fallback_lineage;
            }
            $lineage_cache{$otu_taxid} = $lineage;
        }

        my $row = "$seq_id\t$otu_taxid\t$lineage\n";
        print {$out_fh} $row;
        if (defined $stats_ref && ref($stats_ref) eq 'HASH') {
            $stats_ref->{annotated_rows}++;
            $stats_ref->{annotated_bytes} += length($row);
        }
    }
    my $t_emit_end = time();
    _append_phase_timing($phase_file, $round_id, 'annotated_emit', $t_emit_start, $t_emit_end);
}

sub print_pairs_only {
    my ($input1, $cluster_file, $out_fh) = @_;
    my $entries_ref = collect_entries_from_inputs($input1, $cluster_file);
    for my $entry_ref (@{$entries_ref}) {
        print {$out_fh} join("\t", @{$entry_ref}) . "\n";
    }
}

sub print_cluster_taxids {
    my ($input1, $cluster_file, $out_fh) = @_;
    my $cluster_taxids_ref = collect_cluster_taxids_from_inputs($input1, $cluster_file);
    for my $entry_ref (@{$cluster_taxids_ref}) {
        my ($cluster_id, $taxid) = @{$entry_ref};
        $taxid = '' if !defined $taxid;
        print {$out_fh} "$cluster_id\t".(ref($taxid) ? JSON::PP->new->canonical->encode($taxid) : $taxid)."\n";
    }
}

sub write_cluster_taxids_with_phase_timings {
    my ($input1, $cluster_file, $cluster_taxids_file, $arg4, $arg5, $arg6) = @_;

    my $cluster_sizes_file = '';
    my $phase_file = $arg4;
    my $round_id = $arg5;
    if (defined $arg6) {
        $cluster_sizes_file = $arg4 // '';
        $phase_file = $arg5;
        $round_id = $arg6;
    }

    my $t_load_start = time();
    my ($target_tax_ref, $target_tax_norm_ref) = load_target_tax_maps($input1);
    my $t_load_end = time();
    _append_phase_timing($phase_file, $round_id, 'load_blast_tax_map', $t_load_start, $t_load_end);

    my $t_cluster_start = time();
    open(my $cluster_taxids_fh, '>', $cluster_taxids_file)
        or die "Cannot open $cluster_taxids_file for write: $!";
    my $cluster_sizes_fh;
    if (defined $cluster_sizes_file && $cluster_sizes_file ne '') {
        open($cluster_sizes_fh, '>', $cluster_sizes_file)
            or die "Cannot open $cluster_sizes_file for write: $!";
    }

    my $current_cluster_id;
    my %current_tax_counts;
    my $current_cluster_records = 0;
    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            if (defined $current_cluster_id) {
                my $cluster_taxid = JSON::PP->new->canonical->encode(_projection_taxids(\%current_tax_counts));
                $cluster_taxid = '' if !defined $cluster_taxid;
                print {$cluster_taxids_fh} "$current_cluster_id\t$cluster_taxid\n";
                print {$cluster_sizes_fh} "$current_cluster_id\t$current_cluster_records\n"
                    if defined $cluster_sizes_fh;
            }
            $current_cluster_id = $1;
            %current_tax_counts = ();
            $current_cluster_records = 0;
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $ref_id = $1;
        $current_cluster_records++;
        my $taxid = $target_tax_ref->{$ref_id};
        if (!defined $taxid || $taxid eq '') {
            my $normalized = normalize_read_key($ref_id);
            $taxid = $target_tax_norm_ref->{$normalized} if $normalized ne '';
            $target_tax_ref->{$ref_id} = $taxid if defined $taxid && $taxid ne '';
        }
        _record_member_vote(\%current_tax_counts,$ref_id,$taxid);
    }
    close $cluster_fh;

    if (defined $current_cluster_id) {
        my $cluster_taxid = JSON::PP->new->canonical->encode(_projection_taxids(\%current_tax_counts));
        $cluster_taxid = '' if !defined $cluster_taxid;
        print {$cluster_taxids_fh} "$current_cluster_id\t$cluster_taxid\n";
        print {$cluster_sizes_fh} "$current_cluster_id\t$current_cluster_records\n"
            if defined $cluster_sizes_fh;
    }
    close $cluster_taxids_fh;
    close $cluster_sizes_fh if defined $cluster_sizes_fh;

    my $t_cluster_end = time();
    _append_phase_timing($phase_file, $round_id, 'cluster_taxid_pass', $t_cluster_start, $t_cluster_end);
}

sub print_pairs_only_from_cluster_taxids_file {
    my ($cluster_taxids_file, $cluster_file, $out_fh) = @_;
    my $cluster_taxids_ref = load_cluster_taxids_from_file($cluster_taxids_file);
    my $current_cluster_id;
    my $current_cluster_taxid = '';

    open(my $cluster_fh, '<', $cluster_file) or die "Cannot open $cluster_file: $!";
    while (my $line = <$cluster_fh>) {
        chomp $line;
        if ($line =~ /^\>Cluster (\d+)$/) {
            $current_cluster_id = $1;
            die "Missing cluster taxid for cluster $current_cluster_id\n"
                if !exists $cluster_taxids_ref->{$current_cluster_id};
            $current_cluster_taxid = $cluster_taxids_ref->{$current_cluster_id};
            next;
        }
        next if $line !~ /\>(\S+)\.\.\./;
        next if !defined $current_cluster_id;

        my $member_id = $1;
        my $marker = '';
        if ($member_id =~ /^[^|]+\|([^|]+)/) {
            $marker = $1 // '';
        }
        my $otu_tag = "OTUB_${current_cluster_id}";
        $otu_tag .= "-$marker" if defined $marker && $marker ne '';
        print {$out_fh} "$member_id|$otu_tag\t"._projection_taxid($current_cluster_taxid,$member_id)."\n";
    }
    close $cluster_fh;
}

sub print_full_output {
    my ($input1, $cluster_file, $lineage_file, $out_fh) = @_;
    my $entries_ref = collect_entries_from_inputs($input1, $cluster_file);
    emit_annotated_entries($entries_ref, $lineage_file, $out_fh);
}

# R4-B strict production path. The producer's readers remain byte-identical.
{
    package RTBioScan::R4A;
    require(File::Basename::dirname(__FILE__).'/../../cache_blast_by_hash.pl');
}
sub r4b_fail { die "R4-B: @_\n" }
sub r4b_json { return JSON::PP->new->canonical; }
sub r4b_marker {
    my ($m)=@_;
    r4b_fail('invalid canonical marker') unless defined($m) && $m =~ /\A[A-Z0-9][A-Z0-9_.-]*\z/ && TaxonUtil::canonical_marker_token($m) eq $m;
    return $m;
}
sub r4b_missing { return [('NA')x7]; }
sub r4b_result {
    my ($status,$reason,$taxid,$r,$origin)=@_;
    return {status=>$status,reason=>$reason // 'NA',taxid=>$taxid // 'NA',ranks=>$r // r4b_missing(),origin=>$origin // 'NONE'};
}
sub r4b_usable { return $_[0]{status} eq 'ASSIGNED' || $_[0]{status} eq 'AMBIGUOUS_TIE'; }
sub r4b_trim {
    my ($r,$depth)=@_;return [map { $_<=$depth ? $r->[$_] : 'NA' } 0..6];
}
sub r4b_identity {
    my ($r)=@_;return join("\t",$r->{taxid},TaxonUtil::lineage_text($r->{ranks}));
}
sub r4b_diagnostic_name { my ($n)=@_;$n =~ tr/_/ /;return $n; }
sub r4b_compare {
    my ($a,$b)=@_;my (@conflict,@missing);
    for (0..6) {
        if ($a->[$_] eq 'NA' || $b->[$_] eq 'NA') { push @missing,$_ if $a->[$_] ne $b->[$_]; }
        elsif (r4b_diagnostic_name($a->[$_]) ne r4b_diagnostic_name($b->[$_])) { push @conflict,$_; }
    }
    return (\@conflict,\@missing);
}
sub r4b_lineage_authority {
    my ($file,$targets)=@_;my (%rows,%bad,%kingdoms);my $count=0;
    for my $t (@$targets) { $kingdoms{$t->{kingdom}}=1; }
    RTBioScan::R4A::each_line($file,sub {
        my ($line)=@_;my ($id,$text,@extra)=split /\t/,$line,-1;
        r4b_fail('malformed lineage identity row') unless defined($text) && !@extra && $id =~ /\A-[1-9][0-9]*\z/;
        $count++;my $r=TaxonUtil::canonical_lineage($text);
        if (!$r || $r->[0] eq 'NA') { $bad{$id}=1;return; }
        $rows{$id}{$r->[0]}{TaxonUtil::lineage_text($r)}=$r;
    });
    my %scoped;
    for my $t (@$targets) {
        for my $id (keys %rows,keys %bad) {
            my @r=values %{$rows{$id}{$t->{kingdom}} // {}};
            $scoped{$t->{marker}}{$id}=$bad{$id} ? r4b_result('REFERENCE_UNRESOLVED','malformed_configured_lineage')
                : @r==1 ? r4b_result('ASSIGNED','NA',$id,$r[0],'DIRECT')
                : r4b_result('REFERENCE_UNRESOLVED',@r ? 'conflicting_marker_lineages' : 'missing_marker_lineage');
        }
    }
    my @collisions=sort grep { keys(%{$rows{$_}})>1 } keys %rows;
    return (\%scoped,{rows=>$count,collisions=>\@collisions,malformed=>[sort keys %bad]});
}
sub r4b_metadata {
    my ($t,$needed)=@_;my (%rows,%accessions,%issues);my ($f,$pipe);my $count=0;
    if (defined $t->{metadata}) { open $f,'<',$t->{metadata} or r4b_fail('read explicit reference metadata'); }
    else { open $f,'-|','blastdbcmd','-db',$t->{database},'-entry','all','-outfmt',"%a\t%T\t%t" or r4b_fail('start blastdbcmd');$pipe=1; }
    my $sha=Digest::SHA->new(256);
    while (my $line=<$f>) {
        $sha->add($line);$count++;r4b_fail('truncated reference metadata') unless $line =~ s/\r?\n\z//;
        my ($accession,$native,$title,@extra)=split /\t/,$line,-1;
        r4b_fail('malformed reference metadata') unless defined($title) && !@extra;
        my ($id,$subject,$marker,$text);
        if ($title =~ /\A(\S+)\|kraken:taxid\|(-?[0-9]+)\s+(\S+)\s+(.*)\z/) { ($subject,$id,$marker,$text)=($1,$2,$3,$4); }
        else { $subject=$accession;$id=$native; }
        RTBioScan::R4A::taxid($id);
        $issues{conflicting_accession}{$subject}=1 if exists($accessions{$subject}) && $accessions{$subject} ne $id;
        $accessions{$subject}=$id;
        next unless $id<0 || !$needed || $needed->{$id};
        if (!defined($text)) { $issues{missing_title_lineage}{$id}=1;next; }
        my $r=TaxonUtil::canonical_lineage($text);
        if (!$r) { $issues{malformed_title_lineage}{$id}=1;next; }
        if ($marker ne $t->{marker} || $r->[0] ne $t->{kingdom}) { $issues{wrong_kingdom_or_marker}{$id}=1; }
        $rows{$id}{TaxonUtil::lineage_text($r)}=$r;
    }
    close $f or r4b_fail($pipe ? 'blastdbcmd failed' : 'close reference metadata');
    return (\%rows,\%issues,{rows=>$count,sha256=>$sha->hexdigest});
}
sub r4b_numeric_lineages {
    my ($ids,$taxdir)=@_;my %out;
    return \%out unless %$ids;
    r4b_fail('pinned taxonomy unavailable') unless -d $taxdir;
    require File::Temp;
    my ($f,$in)=File::Temp::tempfile('r4b-taxids-XXXXXX',TMPDIR=>1,UNLINK=>1);
    print {$f} "$_\n" for sort { $a<=>$b } keys %$ids;close $f or r4b_fail('write numeric IDs');
    my ($lf,$lp)=File::Temp::tempfile('r4b-lineages-XXXXXX',TMPDIR=>1,UNLINK=>1);
    local $ENV{TAXONKIT_DB}=$taxdir;
    open my $p,'-|','taxonkit','lineage',$in or r4b_fail('start numeric lineage batch');
    while (my $line=<$p>) { print {$lf} $line or r4b_fail('write numeric lineages'); }
    close $p or r4b_fail('numeric lineage batch failed');close $lf or r4b_fail('close numeric lineages');
    open $p,'-|','taxonkit','reformat','-f','{K};{p};{c};{o};{f};{g};{s}','-P',$lp or r4b_fail('start numeric reformat batch');
    while (my $line=<$p>) {
        chomp $line;my @v=split /\t/,$line,-1;
        r4b_fail('unexpected numeric lineage response') unless @v==3 && $ids->{$v[0]} && !exists($out{$v[0]});
        $out{$v[0]}=TaxonUtil::canonical_lineage($v[2]) // r4b_missing();
    }
    close $p or r4b_fail('numeric reformat failed');
    r4b_fail('incomplete numeric lineage batch') unless keys(%out)==keys(%$ids);
    return \%out;
}
sub r4b_signature {
    my ($t,$taxdir)=@_;my $text='';
    open my $capture,'>',\$text or r4b_fail('capture reference signature');
    { local *STDOUT=$capture;
      RTBioScan::R4A::signature($t->{database},$taxdir,"idfam=$t->{family}","idgen=$t->{genus}","idspec=$t->{species}","evalue=$t->{evalue}","maxhsps=$t->{max_hsps}",'word=50','qcov=50',"target=$t->{marker}","seed=$t->{seed}"); }
    close $capture;return (split /\t/,$text)[0];
}
sub r4b_resolve_candidate {
    my ($t,$row,$memory,$authority,$metadata,$issues,$numeric,$identities)=@_;
    my $id=$row->[2];
    return r4b_result('REFERENCE_INCONSISTENT',"$id:wrong_kingdom_or_marker") if $issues->{wrong_kingdom_or_marker}{$id};
    return r4b_result('REFERENCE_UNRESOLVED',"$id:malformed_reference_title") if $issues->{malformed_title_lineage}{$id};
    my $a=$id<0 ? ($authority->{$id} // r4b_result('REFERENCE_UNRESOLVED','missing_marker_lineage'))
        : r4b_result('ASSIGNED','NA',$id,$numeric->{$id} // r4b_missing(),'DIRECT');
    return r4b_result($a->{status},"$id:$a->{reason}") unless r4b_usable($a);
    my $r=$a->{ranks};
    return r4b_result('REFERENCE_UNRESOLVED',"$id:missing_lineage") if TaxonUtil::lineage_depth($r)<0;
    return r4b_result('REFERENCE_INCONSISTENT',"$id:wrong_kingdom") if $r->[0] ne $t->{kingdom};
    my %notes;
    for my $title (values %{$metadata->{$id} // {}}) {
        my ($conflict,$missing)=r4b_compare($r,$title);
        return r4b_result('REFERENCE_UNRESOLVED',"$id:contradictory_populated_ranks:".join(',',@$conflict)) if @$conflict;
        $notes{"$id:completeness_only"}=1 if @$missing;
    }
    return r4b_result('REFERENCE_UNRESOLVED',"$id:missing_deployed_validation") if $id<0 && !exists($metadata->{$id});
    my $rank=$memory->{$id};
    return r4b_result('REFERENCE_UNRESOLVED',"$id:missing_marker_memtax") unless $rank;
    my $pid=RTBioScan::R4A::decimal_key($row->[5]);
    my $start=RTBioScan::R4A::decimal_cmp($pid,RTBioScan::R4A::decimal_key($t->{genus}))<0 ? 2
        : RTBioScan::R4A::decimal_cmp($pid,RTBioScan::R4A::decimal_key($t->{species}))<0 ? 1 : 0;
    my ($tax,$depth);
    for my $i ($start..3) { if ($rank->[$i] ne 'NA') { ($tax,$depth)=($rank->[$i],6-$i);last; } }
    return r4b_result('REFERENCE_UNRESOLVED',"$id:unresolved_identity_tier") unless defined($tax);
    my $resolved=r4b_trim($r,$depth);
    return r4b_result('REFERENCE_UNRESOLVED',"$id:missing_tier_lineage") if TaxonUtil::lineage_depth($resolved)<0;
    # A numeric ancestor has independent pinned authority; never fill missing
    # configured ranks from it, and never retain its ID against conflicting names.
    if ($tax>0 && $id<0) {
        my $nr=$numeric->{$tax};
        return r4b_result('REFERENCE_UNRESOLVED',"$id:missing_numeric_ancestor") unless $nr && $nr->[$depth] ne 'NA';
        my ($conflict)=r4b_compare($resolved,r4b_trim($nr,$depth));
        return r4b_result('REFERENCE_UNRESOLVED',"$id:contradictory_numeric_ancestor") if @$conflict;
    }
    # Record only explicitly supplied rank identities at their actual depth.
    for my $i (0..3) {
        my $d=6-$i;next if $rank->[$i] eq 'NA' || $r->[$d] eq 'NA';
        my $ancestor=$rank->[$i];
        if ($ancestor>0) {
            my $nr=$numeric->{$ancestor};
            my ($conflict)=$nr ? r4b_compare(r4b_trim($r,$d),r4b_trim($nr,$d)) : ([1]);
            if (!$nr || $nr->[$d] eq 'NA' || @$conflict) {
                $notes{"$id:unvalidated_numeric_ancestor:$ancestor"}=1;next;
            }
        }
        $identities->{TaxonUtil::lineage_text(r4b_trim($r,$d))}{$ancestor}=1;
    }
    $tax='NA' if $resolved->[$depth] eq 'NA';
    return r4b_result('ASSIGNED',keys(%notes) ? join(',',sort keys %notes) : 'NA',$tax,$resolved,'DIRECT');
}
sub r4b_lca {
    my ($results,$identities)=@_;
    my @r=('NA')x7;my $depth=-1;
    for my $i (0..6) {
        my %names=map { $_->{ranks}[$i]=>1 } @$results;
        # A populated disagreement in an ancestor invalidates a shared lower
        # homonym; missing ancestors are retained as NA, never filled in.
        my @populated=grep { $_ ne 'NA' } keys %names;
        last if @populated>1;
        if (keys(%names)==1 && !exists($names{NA})) { $r[$i]=$populated[0];$depth=$i; }
    }
    return r4b_result('REFERENCE_UNRESOLVED','no_common_lineage') if $depth<0;
    my @tax=keys %{$identities->{TaxonUtil::lineage_text(\@r)} // {}};
    my %reasons=(lineage_lca=>1);
    for (@$results) { $reasons{$_->{reason}}=1 if defined($_->{reason}) && $_->{reason} ne 'NA'; }
    return r4b_result('AMBIGUOUS_TIE',join(',',sort keys %reasons),@tax==1 ? $tax[0] : 'NA',\@r,'LCA');
}
sub r4b_resolve_group {
    my ($t,$group,$memory,$authority,$metadata,$issues,$numeric,$identities)=@_;
    return r4b_result('NO_HIT','no_hit') if $group->[0][1] eq 'NO_HIT';
    my %local_identities;
    my @resolved=map { r4b_resolve_candidate($t,$_->[0],$memory,$authority,$metadata,$issues,$numeric,\%local_identities) } @$group;
    for my $lineage (keys %local_identities) { $identities->{$lineage}{$_}=1 for keys %{$local_identities{$lineage}}; }
    for my $status ('REFERENCE_INCONSISTENT','REFERENCE_UNRESOLVED') {
        my @bad=grep { $_->{status} eq $status } @resolved;
        return r4b_result($status,join(',',sort map { $_->{reason} } @bad)) if @bad;
    }
    my %unique;
    for my $i (0..$#resolved) {
        my $r=$resolved[$i];my $key=r4b_identity($r);
        # NA is absence of an identity, not a shared taxid that can collapse
        # distinct selected candidates before their lineage LCA is recorded.
        $key.="\tsource=".$group->[$i][0][2] if $r->{taxid} eq 'NA';
        $unique{$key}=$r;
    }
    return (values %unique)[0] if keys(%unique)==1;
    return r4b_lca([values %unique],\%local_identities);
}
sub r4b_vote {
    my ($members,$identities)=@_;my (%counts,%values,%ambiguous,%statuses);
    for my $r (@$members) {
        $statuses{$r->{status}}++;next unless r4b_usable($r);
        my $key=r4b_identity($r);$counts{$key}++;$values{$key}=$r;
        $ambiguous{$key}=1 if $r->{origin} eq 'LCA';
    }
    my $max=0;for (values %counts) { $max=$_ if $_>$max; }
    my @winners=grep { $counts{$_}==$max } keys %counts;
    my $out;
    if (@winners==1) {
        my $r=$values{$winners[0]};
        $out=r4b_result($ambiguous{$winners[0]} ? 'AMBIGUOUS_TIE' : 'ASSIGNED','plurality',$r->{taxid},$r->{ranks},$ambiguous{$winners[0]} ? 'LCA' : 'DIRECT');
    } elsif (@winners>1) { $out=r4b_lca([map { $values{$_} } @winners],$identities); }
    else {
        my ($status)=grep { $statuses{$_} } qw(REFERENCE_INCONSISTENT REFERENCE_UNRESOLVED COMPUTATION_FAILED FILTERED_INELIGIBLE NO_HIT);
        $out=r4b_result($status // 'NO_HIT','no_usable_votes');
    }
    $out->{support}=$max;$out->{winner_count}=scalar @winners;$out->{votes}=0;$out->{votes}+=$_ for values %counts;
    $out->{member_count}=scalar @$members;$out->{status_counts}=\%statuses;
    return $out;
}
my @R4B_COLUMNS=qw(read_id otu_id marker taxid lineage status depth origin member_count votes support winner_count stable_key read_status read_taxid read_lineage read_depth read_origin read_reason source_taxids status_counts canonical_member);
sub r4b_status_text {
    my ($sig,$rows,$provenance)=@_;
    my $body=join('',map { join("\t",map { defined($_) ? $_ : 'NA' } @$_{@R4B_COLUMNS})."\n" } @$rows);
    return "#RTB-R4B-TAXONOMY\t2\t$sig\t".r4b_json()->encode($provenance)."\n#columns\t".join("\t",@R4B_COLUMNS)."\n".$body."#END\t".scalar(@$rows)."\t".sha256_hex($body)."\n";
}
sub r4b_header {
    my ($head)=@_;
    r4b_fail('unsupported status schema') unless $head =~ /\A#RTB-R4B-TAXONOMY\t([12])\t([0-9a-f]{64})(?:\t([^\r\n]*))?\n\z/;
    my ($version,$sig,$json)=($1,$2,$3);
    if ($version==1) { r4b_fail('invalid legacy status header') if defined $json; return ($sig,undef,1); }
    r4b_fail('missing R4-B evidence provenance') unless defined $json;
    my $provenance=eval { r4b_json()->decode($json) };
    r4b_fail('invalid R4-B evidence provenance') unless ref($provenance) eq 'HASH';
    my %canonical;
    for my $marker (keys %$provenance) {
        r4b_marker($marker);
        my $p=$provenance->{$marker};
        r4b_fail('invalid R4-B evidence provenance fields') unless ref($p) eq 'HASH'
            && keys(%$p)==3 && exists($p->{signature}) && exists($p->{rows}) && exists($p->{body_sha256});
        r4b_fail('invalid R4-B evidence provenance values') unless !ref($p->{signature}) && !ref($p->{body_sha256}) && !ref($p->{rows})
            && $p->{signature}=~/\A[0-9a-f]{64}\z/ && $p->{body_sha256}=~/\A[0-9a-f]{64}\z/
            && $p->{rows}=~/\A(?:0|[1-9][0-9]*)\z/;
        $canonical{$marker}={signature=>$p->{signature},rows=>0+$p->{rows},body_sha256=>$p->{body_sha256}};
    }
    r4b_fail('noncanonical R4-B evidence provenance') unless r4b_json()->encode(\%canonical) eq $json;
    return ($sig,\%canonical,2);
}
sub read_status_sidecar {
    my ($path,$expected)=@_;my @rows;my (%seen,%projection,%members,%canonical,%observed_status,%row_markers);
    open my $f,'<',$path or r4b_fail("read status $path");
    my ($sig,$provenance)=r4b_header(<$f>//'');
    r4b_fail('stale status signature') if defined($expected) && $sig ne $expected;
    r4b_fail('invalid status columns') unless (<$f>//'') eq "#columns\t".join("\t",@R4B_COLUMNS)."\n";
    my $sha=Digest::SHA->new(256);my ($count,$end)=(0,0);
    while (my $line=<$f>) {
        r4b_fail('truncated status row') unless $line =~ /\n\z/;
        if ($line =~ /^#END\t/) {
            r4b_fail('incomplete status envelope') unless $line eq "#END\t$count\t".$sha->hexdigest."\n";
            r4b_fail('data after status envelope') if defined(<$f>);$end=1;last;
        }
        $sha->add($line);$count++;chomp $line;
        my @v=split /\t/,$line,-1;r4b_fail('status field count') unless @v==@R4B_COLUMNS;
        my %r;@r{@R4B_COLUMNS}=@v;
        r4b_fail('empty or control status field') if grep { $_ eq '' || /[\r\n\x00]/ } @v;
        r4b_marker($r{marker});
        $row_markers{$r{marker}}=1;
        r4b_fail('invalid projection identity') unless $r{otu_id} =~ /\AOTUB_[0-9]+-\Q$r{marker}\E\z/;
        r4b_fail('invalid member relationship') unless $r{read_id} =~ /\A([^|\s]+)\|\Q$r{marker}\E\|.*\|\Q$r{otu_id}\E\z/ && $r{canonical_member} eq "$1|$r{marker}";
        r4b_fail('duplicate status member') if $seen{$r{read_id}}++;
        for my $prefix ('','read_') {
            my ($status,$tax,$lineage,$depth,$origin)=@r{map { $prefix.$_ } qw(status taxid lineage depth origin)};
            r4b_fail('unknown assignment status') unless TaxonUtil::valid_assignment_status($status);
            r4b_fail('invalid signed status taxid') unless $tax eq 'NA' || $tax =~ /\A-?[1-9][0-9]*\z/;
            my $lr=TaxonUtil::canonical_lineage($lineage);
            r4b_fail('noncanonical status lineage') unless $lr && TaxonUtil::lineage_text($lr) eq $lineage;
            r4b_fail('status depth mismatch') unless $depth =~ /\A(?:-1|[0-6])\z/ && $depth==TaxonUtil::lineage_depth($lr);
            r4b_fail('invalid assignment origin') unless $origin =~ /\A(?:DIRECT|LCA|NONE)\z/;
            if ($status eq 'ASSIGNED' || $status eq 'AMBIGUOUS_TIE') {
                r4b_fail('empty assigned lineage') unless $depth>=0;
                r4b_fail('assignment origin mismatch') unless $origin eq ($status eq 'ASSIGNED' ? 'DIRECT' : 'LCA');
            } else { r4b_fail('rejected status carries assignment') unless $tax eq 'NA' && $depth==-1 && $origin eq 'NONE'; }
        }
        for (qw(member_count votes support winner_count)) { r4b_fail('invalid count') unless $r{$_} =~ /\A(?:0|[1-9][0-9]*)\z/; }
        r4b_fail('inconsistent vote counts') unless $r{member_count}>0 && $r{votes}<=$r{member_count} && $r{support}<=$r{votes} && $r{support}*$r{winner_count}<=$r{votes};
        r4b_fail('invalid stable key') unless $r{stable_key} eq 'NA' || $r{stable_key} =~ /\A\Q$r{marker}\E\|[0-9a-f]{32}\z/;
        my $counts=eval { r4b_json()->decode($r{status_counts}) };
        r4b_fail('invalid status accounting') unless ref($counts) eq 'HASH' && r4b_json()->encode($counts) eq $r{status_counts};
        my $n=0;for (keys %$counts) { r4b_fail('invalid status count') unless TaxonUtil::valid_assignment_status($_) && $counts->{$_}=~/\A(?:0|[1-9][0-9]*)\z/;$n+=$counts->{$_}; }
        r4b_fail('status accounting mismatch') unless $n==$r{member_count} && ($counts->{ASSIGNED}//0)+($counts->{AMBIGUOUS_TIE}//0)==$r{votes};
        r4b_fail('invalid diagnostic signed IDs') unless $r{source_taxids} eq 'NA' || $r{source_taxids} =~ /\A-?[1-9][0-9]*(?:,-?[1-9][0-9]*)*\z/;
        my $key=$r{otu_id};my $common=join("\t",@r{qw(marker taxid lineage status depth origin member_count votes support winner_count stable_key status_counts)});
        r4b_fail('conflicting projection rows') if exists($projection{$key}) && $projection{$key} ne $common;
        $projection{$key}=$common;$members{$key}{$r{canonical_member}}=1;
        $observed_status{$key}{$r{canonical_member}}=$r{read_status};
        my $member=join("\t",$key,@r{qw(read_status read_taxid read_lineage read_depth read_origin read_reason source_taxids)});
        r4b_fail('conflicting canonical member') if exists($canonical{$r{canonical_member}}) && $canonical{$r{canonical_member}} ne $member;
        $canonical{$r{canonical_member}}=$member;
        $r{_r4b_validated}=1;push @rows,\%r;
    }
    close $f or r4b_fail('close status');r4b_fail('missing status footer') unless $end;
    if (defined $provenance) {
        r4b_fail('R4-B evidence provenance marker scope mismatch') unless join("\t",sort keys %$provenance) eq join("\t",sort keys %row_markers);
    }
    my %checked;
    for my $r (@rows) {
        next if $checked{$r->{otu_id}}++;
        r4b_fail('projection cardinality mismatch') unless keys(%{$members{$r->{otu_id}}})==$r->{member_count};
        my %counts; $counts{$_}++ for values %{$observed_status{$r->{otu_id}}};
        r4b_fail('per-member status accounting mismatch') unless r4b_json()->encode(\%counts) eq $r->{status_counts};
    }
    return \@rows;
}
sub r4b_config {
    my ($path)=@_;
    my $cfg;
    if (defined($path) && $path ne '') {
        open my $f,'<',$path or r4b_fail('read contract');local $/;$cfg=r4b_json()->decode(<$f>);close $f;
    } else {
        my @names=qw(marker kingdom database seed family genus species);
        my @env=qw(RTB_R4B_TARGETS RTB_R4B_KINGDOMS RTB_R4B_DATABASES RTB_R4B_SEEDS RTB_R4B_FAMILY RTB_R4B_GENUS RTB_R4B_SPECIES);
        my @parts=map { [split /\|/,$ENV{$_}//'',-1] } @env;
        # R4-A treats an omitted/empty seed list as no configured seed.
        $parts[3]=[('') x @{$parts[0]}] if ($ENV{RTB_R4B_SEEDS}//'') eq '';
        my @targets;
        for my $i (0..$#{$parts[0]}) {
            my %t;for my $j (0..$#names) { r4b_fail('target configuration cardinality') unless @{$parts[$j]}==@{$parts[0]};$t{$names[$j]}=$parts[$j][$i]; }
            $t{database}=($ENV{RTB_R4B_DB_ROOT}//'').$t{database};
            $t{seed}=($ENV{RTB_R4B_BASE}//'').'/'.$t{seed} if $t{seed} ne '' && $t{seed} ne 'null' && $t{seed}!~m{^/};
            $t{evidence}=($ENV{RTB_R4B_STATE}//'')."/otu_blast_evidence_$t{marker}.tsv";
            $t{memtax}=($ENV{RTB_R4B_STATE}//'').'/memtax'.($i+1).'.txt';
            $t{evalue}=$ENV{RTB_R4B_EVALUE};$t{max_hsps}=$ENV{RTB_R4B_MAX_HSPS};push @targets,\%t;
        }
        $cfg={targets=>\@targets,taxonomy_dir=>$ENV{TAXONKIT_DB},hash_map=>$ENV{RTB_R4B_HASH_MAP},sidecar=>$ENV{RTB_R4B_SIDECAR}};
    }
    r4b_fail('invalid contract') unless ref($cfg) eq 'HASH' && ref($cfg->{targets}) eq 'ARRAY' && @{$cfg->{targets}};
    my %seen;
    for my $t (@{$cfg->{targets}}) {
        $t->{seed}='' unless defined $t->{seed};
        r4b_marker($t->{marker});r4b_fail('duplicate configured marker') if $seen{$t->{marker}}++;
        r4b_fail('missing expected kingdom') unless defined($t->{kingdom}) && $t->{kingdom} ne '' && $t->{kingdom} !~ /[;\t\r\n]/;
    }
    return $cfg;
}
sub run_marker_contract {
    my ($clstr,$lineage,$config,$out)=@_;my $cfg=r4b_config($config);my $started=time;my %cluster_records;
    my (%targets,%clusters,%member_cluster,%hashes);my @cluster_order;
    for (@{$cfg->{targets}}) { $targets{$_->{marker}}=$_; }
    my $cid;
    RTBioScan::R4A::each_line($clstr,sub {
        my ($line)=@_;
        if ($line =~ /^>Cluster (\d+)$/) { $cid=$1;r4b_fail('duplicate cluster') if exists($clusters{$cid});$clusters{$cid}={};push @cluster_order,$cid;return; }
        r4b_fail('malformed canonical cluster member') unless defined($cid) && $line =~ />([^\s]+)\.\.\.(?:\s|$)/;
        my $id=$1;my ($base,$marker)=split /\|/,$id;
        r4b_marker($marker);r4b_fail('member marker not configured') unless $targets{$marker};
        my $key="$base|$marker";r4b_fail('canonical member in multiple clusters') if exists($member_cluster{$key}) && $member_cluster{$key} ne $cid;
        $member_cluster{$key}=$cid;$clusters{$cid}{$marker}{$key}{ids}{$id}=1;$cluster_records{$cid}++;
        $clusters{$cid}{$marker}{$key}{representative}=1 if $line =~ /\*\s*$/;
    });
    if (defined($cfg->{hash_map}) && -s $cfg->{hash_map}) {
        RTBioScan::R4A::each_line($cfg->{hash_map},sub {
            my ($line)=@_;my ($base,$hash,@extra)=split /\t/,$line,-1;
            r4b_fail('invalid representative map') unless defined($hash) && $hash=~/\A[0-9a-f]{32}\z/ && !@extra;
            ($base)=split /\|/,$base;r4b_fail('conflicting representative hash') if exists($hashes{$base}) && $hashes{$base} ne $hash;$hashes{$base}=$hash;
        });
    }
    my ($authorities,$inventory)=r4b_lineage_authority($lineage,$cfg->{targets});
    my (%read_results,%identities,%sources,%digests,%provenance);
    $digests{lineage}=RTBioScan::R4A::file_digest($lineage);$digests{clusters}=RTBioScan::R4A::file_digest($clstr);
    $digests{hash_map}=RTBioScan::R4A::file_digest($cfg->{hash_map}) if defined($cfg->{hash_map}) && -e $cfg->{hash_map};
    for my $t (@{$cfg->{targets}}) {
        my $marker=$t->{marker};my @members=grep { /\|\Q$marker\E\z/ } keys %member_cluster;
        next unless @members;
        for (qw(family genus species evalue)) { RTBioScan::R4A::number($t->{$_},0,$_ eq 'evalue' ? undef : 100); }
        RTBioScan::R4A::uint($t->{max_hsps});
        my $sig=r4b_signature($t,$cfg->{taxonomy_dir});
        my ($stored,$groups,$evidence_provenance)=RTBioScan::R4A::evidence_read($t->{evidence});r4b_fail('stale R4-A evidence') unless $stored eq $sig;
        my $count=$evidence_provenance->{rows};
        $provenance{$marker}={signature=>$evidence_provenance->{signature},rows=>0+$count,body_sha256=>$evidence_provenance->{body_sha256}};
        my ($msig,$version)=RTBioScan::R4A::scan_sealed($t->{memtax},'MEMTAX',sub {});
        r4b_fail('stale or unsupported R4-A MEMTAX') unless $msig eq $sig && $version==2;
        my $memory=RTBioScan::R4A::memtax_read($t->{memtax},$sig);
        my $seed=RTBioScan::R4A::seed_read($t->{seed},$marker);
        for my $id (grep { /^-/ } keys %$memory) {
            my $expected=$seed->{$id} // [('NA')x5];
            r4b_fail('MEMTAX disagrees with configured marker seed') unless join("\t",@{$memory->{$id}}) eq join("\t",@$expected);
        }
        my %needed;
        for my $id (keys %$groups) {
            my (undef,$m)=split /\|/,$id;r4b_fail('R4-A evidence marker mismatch') unless $m eq $marker;
            for (@{$groups->{$id}}) { next if $_->[1] eq 'NO_HIT';$needed{$_->[0][2]}=1; }
        }
        my %numeric=map { $_=>1 } grep { $_>0 } keys %needed;
        for my $id (keys %needed) { for (@{$memory->{$id}//[]}[0..3]) { $numeric{$_}=1 if defined($_) && $_ ne 'NA' && $_>0; } }
        my $numeric=r4b_numeric_lineages(\%numeric,$cfg->{taxonomy_dir});
        my ($metadata,$issues,$meta_info)=r4b_metadata($t,\%needed);
        r4b_fail('conflicting reference accession identities') if keys %{$issues->{conflicting_accession}//{}};
        $digests{"$marker:evidence"}=$evidence_provenance;$digests{"$marker:memtax"}=RTBioScan::R4A::file_digest($t->{memtax});
        $digests{"$marker:signature"}=$sig;$digests{"$marker:metadata"}=$meta_info->{sha256};
        my (%aliases,%resolved_by_hash);
        for my $id (sort keys %$groups) {
            my ($base)=split /\|/,$id;my $key="$base|$marker";next unless exists $member_cluster{$key};
            my $hash=$groups->{$id}[0][0][0];
            $resolved_by_hash{$hash}//=r4b_resolve_group($t,$groups->{$id},$memory,$authorities->{$marker},$metadata,$issues,$numeric,$identities{$marker}//={});
            $aliases{$key}{$hash}=$resolved_by_hash{$hash};
            for (@{$groups->{$id}}) { $sources{$key}{$_->[0][2]}=1 unless $_->[1] eq 'NO_HIT'; }
        }
        for my $key (@members) {
            my @r=values %{$aliases{$key}//{}};
            $read_results{$key}=@r==1 ? $r[0] : r4b_result('REFERENCE_UNRESOLVED',@r ? 'conflicting_canonical_aliases' : 'missing_sealed_query');
        }
    }
    my @rows;my @public=("#seq_id\ttax_id\tlineage\n");
    for my $cluster (@cluster_order) {
        for my $marker (sort keys %{$clusters{$cluster}}) {
            my $members=$clusters{$cluster}{$marker};my @keys=sort keys %$members;
            my $vote=r4b_vote([map { $read_results{$_} } @keys],$identities{$marker}//{});
            my %rep;
            for my $key (@keys) { my ($base)=split /\|/,$key;$rep{$hashes{$base}}=1 if $members->{$key}{representative} && exists($hashes{$base}); }
            r4b_fail('multiple stable representatives in one projection') if keys(%rep)>1;
            my $stable=keys(%rep) ? $marker.'|'.(keys %rep)[0] : 'NA';
            warn "WARN: no stable representative for OTUB_$cluster-$marker; no persistent display fallback\n" if $stable eq 'NA';
            for my $key (@keys) {
                my $read=$read_results{$key};
                for my $id (sort keys %{$members->{$key}{ids}}) {
                    my $otu="OTUB_$cluster-$marker";my $rid="$id|$otu";
                    my %r=(read_id=>$rid,otu_id=>$otu,marker=>$marker,taxid=>$vote->{taxid},lineage=>TaxonUtil::lineage_text($vote->{ranks}),status=>$vote->{status},depth=>TaxonUtil::lineage_depth($vote->{ranks}),origin=>$vote->{origin},stable_key=>$stable,
                        read_status=>$read->{status},read_taxid=>$read->{taxid},read_lineage=>TaxonUtil::lineage_text($read->{ranks}),read_depth=>TaxonUtil::lineage_depth($read->{ranks}),read_origin=>$read->{origin},read_reason=>$read->{reason},source_taxids=>keys(%{$sources{$key}//{}}) ? join(',',sort keys %{$sources{$key}}) : 'NA',status_counts=>r4b_json()->encode($vote->{status_counts}),canonical_member=>$key);
                    @r{qw(member_count votes support winner_count)}=@$vote{qw(member_count votes support winner_count)};
                    # Only the legacy public boundary uses Unassigned placeholders.
                    # Keep partial/LCA lineages and the sealed canonical NA fields intact.
                    my $public_lineage=r4b_usable($vote) ? $r{lineage}
                        : 'K__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned';
                    push @rows,\%r;push @public,join("\t",$rid,$r{taxid},$public_lineage)."\n";
                }
            }
        }
    }
    my $sig=sha256_hex(r4b_json()->encode({contract=>'R4-B-v1',configuration=>$cfg,sources=>\%digests}));
    my $sidecar=$cfg->{sidecar};r4b_fail('status sidecar path required') unless defined($sidecar) && $sidecar ne '';
    my $text=r4b_status_text($sig,\@rows,\%provenance);
    # Validate the whole generation before replacing its externally visible name.
    require File::Temp;my ($f,$tmp)=File::Temp::tempfile('.r4b-status-XXXXXX',DIR=>dirname($sidecar),UNLINK=>0);
    my $ok=eval {
        print {$f} $text or r4b_fail('write status');close $f or r4b_fail('close status');read_status_sidecar($tmp,$sig);
        for (@public) { print {$out} $_ or r4b_fail('write annotated output'); }
        require IO::Handle;IO::Handle::flush($out) or r4b_fail('flush annotated output');
        rename $tmp,$sidecar or r4b_fail('publish status');1;
    };
    my $err=$@;unlink $tmp if -e $tmp;die $err unless $ok;
    if (defined($ENV{OTU_REFINE_WORKLOAD_STATS_FILE}) && $ENV{OTU_REFINE_WORKLOAD_STATS_FILE} ne '') {
        my @names=qw(cluster_count cluster_records blastreport_rows cluster_taxids_rows worker_count shard_count shard_scheduler_mode target_records_per_shard smallest_shard_records median_shard_records largest_shard_records largest_shard_fraction max_single_cluster_records max_single_cluster_fraction merged_pairs_rows merged_pairs_bytes annotated_rows annotated_bytes annotated_file_bytes);
        my %stats=map { $_=>0 } @names;my $records=0;my $largest=0;
        for (values %cluster_records) { $records+=$_;$largest=$_ if $_>$largest; }
        my $legacy=$ENV{RTB_R4B_LEGACY_REPORT};
        RTBioScan::R4A::each_line($legacy,sub { $stats{blastreport_rows}++ if $_[0]=~/\S/; }) if defined($legacy) && -f $legacy;
        @stats{qw(cluster_count cluster_records cluster_taxids_rows worker_count shard_scheduler_mode max_single_cluster_records max_single_cluster_fraction annotated_rows annotated_bytes annotated_file_bytes)}=
            (scalar(@cluster_order),$records,scalar(@cluster_order),@rows ? 1 : 0,'single_pass',$largest,$records ? $largest/$records : 0,scalar(@rows),length(join('',@public))-length($public[0]),length(join('',@public)));
        # Shard and merged-pair counters remain zero: the strict pass emits
        # directly, without constructing those legacy intermediate files.
        # Preserve the wrapper's nonfatal diagnostic-write behavior.
        eval { TaxonUtil::atomic_text($ENV{OTU_REFINE_WORKLOAD_STATS_FILE},"key\tvalue\n".join('',map { "$_\t$stats{$_}\n" } @names));1; }
            or warn "WARN: unable to publish OTU refinement workload diagnostics: $@";
        append_phase_timing_row($ENV{OTU_REFINE_PHASE_TIMINGS_FILE},$ENV{OTU_REFINE_ROUND_ID},'wrapper_total',$started,time);
    }
    return \@rows;
}

sub publish_status_sidecar {
    my ($source,$destination)=@_;
    require File::Temp;
    my ($out,$tmp)=File::Temp::tempfile('.r4b-status-XXXXXX',DIR=>dirname($destination),UNLINK=>0);
    my $ok=eval {
        open my $in,'<',$source or r4b_fail('read publication source');
        while (my $line=<$in>) { print {$out} $line or r4b_fail('write publication'); }
        close $in or r4b_fail('close publication source');close $out or r4b_fail('close publication');
        read_status_sidecar($tmp);rename $tmp,$destination or r4b_fail('publish status sidecar');1;
    };
    my $err=$@;unlink $tmp if -e $tmp;die $err unless $ok;
}
sub validate_marker_references {
    my ($contract)=@_;my $cfg=r4b_config($contract);
    r4b_fail('lineage path required') unless defined($cfg->{lineage});
    my ($authority,$inventory)=r4b_lineage_authority($cfg->{lineage},$cfg->{targets});
    my %sources;my @problems;my @rows;my %markers;
    my $source=sub { my ($path)=@_;$sources{$path}={sha256=>RTBioScan::R4A::file_digest($path),bytes=>0+(-s $path)}; };
    $source->($contract);$source->($cfg->{lineage});
    for my $t (@{$cfg->{targets}}) {
        my $marker=$t->{marker};my ($meta,$issues,$stream)=r4b_metadata($t,undef);
        if (defined($t->{metadata})) { $source->($t->{metadata}); }
        else { $source->($_) for RTBioScan::R4A::reference_files($t->{database},{}); }
        if (defined($t->{seed}) && $t->{seed} ne '') { RTBioScan::R4A::seed_read($t->{seed},$marker);$source->($t->{seed}); }
        my (@missing,@conflicting,@incomplete,@malformed,@wrong,@complete_only);
        for my $id (sort keys %{$authority->{$marker}}) {
            my $a=$authority->{$marker}{$id};
            push @malformed,$id if $a->{reason} eq 'malformed_configured_lineage';
            push @conflicting,$id if $a->{reason} eq 'conflicting_marker_lineages';
            next unless r4b_usable($a);
            push @incomplete,$id if grep { $_ eq 'NA' } @{$a->{ranks}};
            push @rows,join("\t",$marker,$id,TaxonUtil::lineage_text($a->{ranks}));
        }
        for my $id (sort keys %$meta) {
            next unless $id<0;
            my $a=$authority->{$marker}{$id};
            if (!$a || $a->{reason} eq 'missing_marker_lineage') { push @missing,$id;next; }
            next unless r4b_usable($a);
            for my $r (values %{$meta->{$id}}) {
                my ($conflict,$missing)=r4b_compare($a->{ranks},$r);
                if (@$conflict) { push @conflicting,$id; }
                elsif (@$missing) { push @complete_only,$id; }
            }
        }
        push @wrong,keys %{$issues->{wrong_kingdom_or_marker}//{}};
        for my $kind (sort keys %$issues) { push @problems,map { {marker=>$marker,identity=>$_,reason=>$kind} } sort keys %{$issues->{$kind}}; }
        for my $pair (['missing_configured_lineage',\@missing],['contradictory_lineage',\@conflicting],['malformed_configured_lineage',\@malformed]) {
            my %seen;push @problems,map { {marker=>$marker,identity=>$_,reason=>$pair->[0]} } sort grep { !$seen{$_}++ } @{$pair->[1]};
        }
        my %unique;my @synthetic=grep { $_<0 } keys %$meta;
        $markers{$marker}={expected_kingdom=>$t->{kingdom},metadata_rows=>$stream->{rows},metadata_sha256=>$stream->{sha256},synthetic_taxids=>scalar(@synthetic),incomplete_ranks=>\@incomplete,completeness_only=>[sort grep { !$unique{$_}++ } @complete_only],wrong_kingdom=>[sort @wrong]};
    }
    @rows=sort @rows;
    my $payload=join('',map { "$_\n" } @rows);
    return {schema=>'RTB-R4B-REFERENCE-VALIDATION',version=>1,valid=>@problems ? JSON::PP::false : JSON::PP::true,issues=>\@problems,inventory=>$inventory,markers=>\%markers,sources=>\%sources,rows=>\@rows,row_count=>scalar(@rows),output_sha256=>sha256_hex($payload)};
}
sub marker_reference_cli {
    my ($mode,$contract,$output)=@_;
    my $report=validate_marker_references($contract);
    if ($mode eq '--validate-marker-view') {
        r4b_fail('view path required') unless defined($output);
        open my $f,'<',$output or r4b_fail('read marker view');local $/;my $view=r4b_json()->decode(<$f>);close $f;
        r4b_fail('marker view/source hash mismatch') unless r4b_json()->encode($view) eq r4b_json()->encode($report) && $report->{valid};
    } elsif ($mode eq '--write-marker-view') {
        r4b_fail('new output path required') unless defined($output) && $output ne '';
        r4b_fail('reference validation failed; no view written') unless $report->{valid};
        r4b_fail('output already exists') if -e $output || -l $output;
        # A single JSON bundle includes the canonical TSV payload and its manifest.
        # Hard-link publication is atomic and cannot replace an existing path.
        require File::Temp;
        my ($f,$tmp)=File::Temp::tempfile('.r4b-view-XXXXXX',DIR=>dirname($output),UNLINK=>0);
        my $ok=eval { print {$f} r4b_json()->encode($report)."\n" or r4b_fail('write marker view');close $f or r4b_fail('close marker view');link $tmp,$output or r4b_fail('new output publication refused');1 };
        my $err=$@;unlink $tmp if -e $tmp;die $err unless $ok;
    } elsif ($mode ne '--validate-marker-lineages') { r4b_fail('unknown reference mode'); }
    my %summary=%$report;delete $summary{rows};
    print r4b_json()->pretty->encode(\%summary);
    return $report->{valid} ? 0 : 2;
}

1;
