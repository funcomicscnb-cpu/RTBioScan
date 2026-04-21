#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use JSON::PP qw(encode_json);
use POSIX qw(strftime);
use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";
require "$FindBin::Bin/lib/taxon_util.pl";
*trim_text           = \&TaxonUtil::trim_text;
*is_unassigned_taxon = \&TaxonUtil::is_unassigned_taxon;
*normalize_taxon     = \&TaxonUtil::normalize_taxon;
*marker_from_token   = \&TaxonUtil::marker_from_token;
*canonical_marker_token = \&TaxonUtil::canonical_marker_token;
*marker_filename_token = \&TaxonUtil::marker_filename_token;
*marker_slug = \&TaxonUtil::marker_slug;
*is_numeric_taxid    = \&TaxonUtil::is_numeric_taxid;

my %opt = (
  schema_version => '1.6',
);

GetOptions(
  'run-id=s'                     => \$opt{run_id},
  'state-id=s'                   => \$opt{state_id},
  'barcode=s'                    => \$opt{barcode},
  'round-barcode=s'              => \$opt{round_barcode},
  'targets=s'                    => \$opt{targets},
  'target-taxa=s'                => \$opt{target_taxa},
  'schema-version=s'             => \$opt{schema_version},
  'timestamp-utc=s'              => \$opt{timestamp_utc},
  'out=s'                        => \$opt{out},
  'read-info=s'                  => \$opt{read_info},
  'on-target=s'                  => \$opt{on_target},
  'demult=s'                     => \$opt{demult},
  'read-fate-demult=s'           => \$opt{read_fate_demult},
  'otu-def=s'                    => \$opt{otu_def},
  'blast-otu=s'                  => \$opt{blast_otu},
  'read-fate-blast=s'            => \$opt{read_fate_blast},
  'blast-unassigned-ids=s'       => \$opt{blast_unassigned_ids},
  'read-fate-live-current!'      => \$opt{read_fate_live_current},
  'blast-otu-cumulative=s'       => \$opt{blast_otu_cumulative},
  'blast-noadapter=s'            => \$opt{blast_noadapter},
  'otu-sizes-round=s'            => \$opt{otu_sizes_round},
  'blast-consensus=s'            => \$opt{blast_consensus},
  'consensus-round-provenance=s' => \$opt{consensus_round_provenance},
  'summary=s'                    => \$opt{summary},
  'summary-otu=s'                => \$opt{summary_otu},
  'round-failed-file=s'          => \$opt{round_failed_file},
  'otu-size-streak-stats=s'      => \$opt{otu_size_streak_stats},
  'otu-size-streak=s'            => \$opt{otu_size_streak},
  'otu-size-streak-mode=s'       => \$opt{otu_size_streak_mode},
  'otu-size-streak-min-rounds=s' => \$opt{otu_size_streak_min_rounds},
  'otu-lock-summary=s'           => \$opt{otu_lock_summary},
  'otu-blast-filter-stats=s'     => \$opt{otu_blast_filter_stats},
  'blast-filter-dropped-ids=s'   => \$opt{blast_filter_dropped_ids},
  'blast-filter-mode=s'          => \$opt{blast_filter_mode},
  'otu-blast-min-members=s'      => \$opt{otu_blast_min_members},
  'otu-blast-filter-skip-rounds=s' => \$opt{otu_blast_filter_skip_rounds},
  'otu-blast-unassigned-grace-rounds=s' => \$opt{otu_blast_unassigned_grace_rounds},
  'blast-id-family=s'            => \$opt{blast_id_family},
  'blast-id-genus=s'             => \$opt{blast_id_genus},
  'blast-id-spec=s'              => \$opt{blast_id_spec},
  'otu-members-blastdiag-stats=s'=> \$opt{otu_members_blastdiag_stats},
  'consensus-consolidated-ids=s' => \$opt{consensus_consolidated_ids},
  'blast-consensus-consolidated=s' => \$opt{blast_consensus_consolidated},
  'active-prune-counts=s'        => \$opt{active_prune_counts},
  'round-index-file=s'           => \$opt{round_index_file},
  'spec-basics-metazoa=s'        => \$opt{spec_basics_metazoa},
  'spec-basics-viridiplantae=s'  => \$opt{spec_basics_viridiplantae},
  'debug-otu-out=s'              => \$opt{debug_otu_out},
  'fig-list=s'                   => \$opt{fig_list},
  'fig-dir=s'                    => \$opt{fig_dir},
  'fig-url-prefix=s'             => \$opt{fig_url_prefix},
  'sample-fig-list=s'            => \$opt{sample_fig_list},
  'sample-roster=s'              => \$opt{sample_roster},
  'track-identity=s'             => \$opt{track_identity},
  'identity-mode=s'              => \$opt{identity_mode},
  'sample-fig-dir=s'             => \$opt{sample_fig_dir},
  'sample-fig-url-prefix=s'      => \$opt{sample_fig_url_prefix},
  'asset-snapshot-policy=s'      => \$opt{asset_snapshot_policy},
) or die "invalid arguments\n";

for my $req (qw(run_id barcode round_barcode out)) {
  die "missing required --$req\n" unless defined $opt{$req} && $opt{$req} ne '';
}

$opt{identity_mode} //= 'collapse';
die "invalid --identity-mode '$opt{identity_mode}': must be 'collapse' or 'track'\n"
  unless $opt{identity_mode} eq 'collapse' || $opt{identity_mode} eq 'track';
if ($opt{identity_mode} eq 'track'
    && (!defined $opt{sample_roster} || $opt{sample_roster} eq '')) {
  die "ERROR: --identity-mode track requires --sample-roster\n";
}

my $g_roster_ready = 0;
our %TRACK_UNIT_METRICS;
our %TRACK_UNIT_BY_UNIT_ID;
our %TRACK_UNIT_BY_TRACK_MARKER;
our %TRACK_ROSTER_BY_TRACK_ID;

my @warnings;
my %warned;
my %file_text_cache;
my %_parsed_rows;
my $otu_assignment_thresholds = build_assignment_thresholds(
  $opt{targets},
  $opt{blast_id_family},
  $opt{blast_id_genus},
  $opt{blast_id_spec},
);

sub get_parsed_rows {
  my ($path) = @_;
  return undef if !defined $path || $path eq '';
  return $_parsed_rows{$path} if exists $_parsed_rows{$path};
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    $_parsed_rows{$path} = undef;
    return undef;
  }
  my @rows;
  open my $FH, '<', $path or do { warn_once("open_failed:$path"); $_parsed_rows{$path} = undef; return undef };
  my $header_line = <$FH>;
  unless (defined $header_line) {
    close $FH;
    $_parsed_rows{$path} = undef;
    return undef;
  }
  chomp $header_line;
  $header_line =~ s/\r//g;
  my @cols = split /\t/, $header_line, -1;
  while (my $line = <$FH>) {
    chomp $line;
    $line =~ s/\r//g;
    next if $line eq '';
    next if $line eq $header_line;
    my @vals = split /\t/, $line, -1;
    my %row;
    for my $i (0..$#cols) { $row{$cols[$i]} = $vals[$i] // '' }
    push @rows, \%row;
  }
  close $FH;
  $_parsed_rows{$path} = \@rows;
  return \@rows;
}

sub warn_once {
  my ($msg) = @_;
  return if !defined $msg || $msg eq '';
  if ($msg =~ /^missing_or_empty:(.+)$/) {
    my $path = $1;
    return if defined $opt{otu_lock_summary} && $opt{otu_lock_summary} ne '' && $path eq $opt{otu_lock_summary};
    return if defined $opt{otu_members_blastdiag_stats} && $opt{otu_members_blastdiag_stats} ne '' && $path eq $opt{otu_members_blastdiag_stats};
    return if defined $opt{otu_size_streak} && $opt{otu_size_streak} ne '' && $path eq $opt{otu_size_streak};
  }
  if ($msg =~ /^missing_or_empty_data_rows:(.+)$/) {
    my $path = $1;
    return if defined $opt{consensus_round_provenance} && $opt{consensus_round_provenance} ne ''
      && $path eq $opt{consensus_round_provenance};
  }
  return if $msg eq 'otu_fate_universe_empty:strict_round';
  return if $msg =~ /^size_streak_inputs_missing:/;
  return if $warned{$msg}++;
  push @warnings, $msg;
}

sub cached_file_text {
  my ($path) = @_;
  return undef if !defined $path || $path eq '';
  if (!exists $file_text_cache{$path}) {
    open my $RAW, '<', $path or return undef;
    local $/ = undef;
    my $text = <$RAW>;
    close $RAW;
    $text = '' if !defined $text;
    $text =~ s/\r\n/\n/g;
    $text =~ s/\r/\n/g;
    $file_text_cache{$path} = $text;
  }
  return \$file_text_cache{$path};
}

sub open_cached_text_handle {
  my ($path) = @_;
  my $text_ref = cached_file_text($path);
  return undef if !defined $text_ref;
  open my $FH, '<', $text_ref or return undef;
  return $FH;
}

sub parse_pipe_values {
  my ($raw) = @_;
  return () if !defined $raw || $raw eq '';
  return map { trim_text($_) } split /\|/, $raw, -1;
}

our (@CONFIGURED_MARKERS, %CONFIGURED_MARKER, $CONFIGURED_TARGET_TAX_MAP, $CONFIGURED_MARKER_FILE_TOKENS, $CONFIGURED_MARKER_COLORS);

sub configured_marker_order {
  my @targets = parse_pipe_values($opt{targets});
  my @markers;
  my %seen;
  for my $target (@targets) {
    my $marker = canonical_marker_token($target);
    next if !defined $marker || $marker eq '';
    next if $seen{$marker}++;
    push @markers, $marker;
  }
  return @markers;
}

sub configured_marker_tax_map {
  my @targets = parse_pipe_values($opt{targets});
  my @taxa = parse_pipe_values($opt{target_taxa});
  my %map;
  for my $i (0 .. $#targets) {
    my $marker = canonical_marker_token($targets[$i]);
    next if !defined $marker || $marker eq '';
    my $taxon = $i <= $#taxa ? trim_text($taxa[$i]) : '';
    next if $taxon eq '';
    $map{$marker} = $taxon;
  }
  return \%map;
}

sub configured_marker_file_tokens {
  my %tokens;
  for my $marker (configured_marker_order()) {
    $tokens{$marker} = marker_filename_token($marker);
  }
  return \%tokens;
}

sub marker_color_map {
  my %defaults = (
    COI  => '#2c6e49',
    ITS2 => '#1d4e89',
  );
  my @palette = (
    '#8c564b', '#bcbd22', '#17becf', '#e45756', '#54a24b',
    '#f58518', '#4c78a8', '#9c755f', '#bab0ac', '#ff9da6',
  );
  my %colors;
  my $palette_idx = 0;
  for my $marker (configured_marker_order()) {
    if (exists $defaults{$marker}) {
      $colors{$marker} = $defaults{$marker};
      next;
    }
    $colors{$marker} = $palette[$palette_idx % scalar(@palette)];
    $palette_idx++;
  }
  return \%colors;
}

sub configured_marker_regex_fragment {
  my @quoted = map { quotemeta($_) } @CONFIGURED_MARKERS;
  @quoted = ('COI', 'ITS2') if !@quoted;
  return '(?:' . join('|', @quoted) . ')';
}

sub dynamic_run_figure_specs {
  my ($barcode, $prefix) = @_;
  my @specs;
  my %base = (
    otu => {
      section => 'OTU Definition',
      species_order => 110,
      genus_order => 115,
      circle_order => 118,
      species_prefix => 'otu_tax_spc',
      genus_prefix => 'otu_tax_gns',
      circle_prefix => 'otu_circle_tree',
      species_title => 'OTU Species Treemap',
      genus_title => 'OTU Genus Treemap',
      circle_title => 'OTU Fan Cladogram',
    },
    consensus => {
      section => 'Consensus',
      species_order => 130,
      genus_order => 135,
      circle_order => 138,
      species_prefix => 'consensus_tax_spc',
      genus_prefix => 'consensus_tax_gns',
      circle_prefix => 'consensus_circle_tree',
      species_title => 'Consensus Species Treemap',
      genus_title => 'Consensus Genus Treemap',
      circle_title => 'Consensus Fan Cladogram',
    },
  );
  for my $marker (@CONFIGURED_MARKERS) {
    my $file_token = $CONFIGURED_MARKER_FILE_TOKENS->{$marker} || marker_filename_token($marker);
    my $slug = marker_slug($marker);
    my $legacy_suffix = $marker eq 'COI' ? 'coi' : ($marker eq 'ITS2' ? 'its' : $slug);
    for my $source (qw(otu consensus)) {
      my $cfg = $base{$source};
      push @specs, (
        {
          id => "${source}_spc_treemap_${legacy_suffix}",
          filename => "${barcode}_$cfg->{species_prefix}_${file_token}_treemap.png",
          title => "$cfg->{species_title} ($marker)",
          description => $source eq 'otu' ? 'Species abundance (OTU)' : 'Species abundance (consensus)',
          section => $cfg->{section},
          order => $cfg->{species_order},
        },
        {
          id => "${source}_gns_treemap_${legacy_suffix}",
          filename => "${barcode}_$cfg->{genus_prefix}_${file_token}_treemap.png",
          title => "$cfg->{genus_title} ($marker)",
          description => $source eq 'otu' ? 'Genus abundance (OTU)' : 'Genus abundance (consensus)',
          section => $cfg->{section},
          order => $cfg->{genus_order},
        },
        {
          id => "${source}_circle_tree_${legacy_suffix}",
          filename => "${barcode}_$cfg->{circle_prefix}_${file_token}.png",
          title => "$cfg->{circle_title} ($marker)",
          description => $source eq 'otu' ? 'Weight: reads in OTUs' : 'Weight: consensus count',
          section => $cfg->{section},
          order => $cfg->{circle_order},
        },
      );
    }
    push @specs, (
      {
        id => "consensus_consolidated_spc_treemap_${legacy_suffix}",
        filename => "${barcode}_consensus_consolidated_tax_spc_${file_token}_treemap.png",
        title => "Consolidated Consensus Species Treemap ($marker)",
        description => 'Species abundance (consolidated consensus)',
        section => 'Consensus',
        order => 142,
      },
      {
        id => "consensus_consolidated_gns_treemap_${legacy_suffix}",
        filename => "${barcode}_consensus_consolidated_tax_gns_${file_token}_treemap.png",
        title => "Consolidated Consensus Genus Treemap ($marker)",
        description => 'Genus abundance (consolidated consensus)',
        section => 'Consensus',
        order => 147,
      },
    );
  }
  return @specs;
}

@CONFIGURED_MARKERS = configured_marker_order();
%CONFIGURED_MARKER = map { $_ => 1 } @CONFIGURED_MARKERS;
$CONFIGURED_TARGET_TAX_MAP = configured_marker_tax_map();
$CONFIGURED_MARKER_FILE_TOKENS = configured_marker_file_tokens();
$CONFIGURED_MARKER_COLORS = marker_color_map();

sub build_marker_threshold_map {
  my ($targets_raw, $thresholds_raw) = @_;
  my @targets = parse_pipe_values($targets_raw);
  my @thresholds = parse_pipe_values($thresholds_raw);
  my %map;

  for my $i (0 .. $#thresholds) {
    my $raw = $thresholds[$i];
    next if !defined $raw || $raw eq '' || uc($raw) eq 'NA';
    next if $raw !~ /^(?:\d+(?:\.\d+)?|\.\d+)$/;
    my $value = 0 + $raw;
    next if $value < 0 || $value > 100;

    my $marker = '';
    if ($i <= $#targets) {
      my $target = $targets[$i];
      my $canonical = marker_from_token($target);
      if (defined $canonical && $canonical ne '' && $canonical ne 'OTHER') {
        $marker = $canonical;
      } elsif (defined $target && $target ne '') {
        $marker = uc($target);
      }
    }

    $marker = '_default' if $marker eq '' && @thresholds == 1;
    next if $marker eq '';
    $map{$marker} = $value;
  }

  return \%map;
}

sub build_assignment_thresholds {
  my ($targets_raw, $family_raw, $genus_raw, $species_raw) = @_;
  return {
    family  => build_marker_threshold_map($targets_raw, $family_raw),
    genus   => build_marker_threshold_map($targets_raw, $genus_raw),
    species => build_marker_threshold_map($targets_raw, $species_raw),
  };
}

sub threshold_for_marker {
  my ($threshold_map, $marker_raw) = @_;
  return undef if !defined $threshold_map || ref($threshold_map) ne 'HASH' || !%{$threshold_map};
  my $marker = trim_text($marker_raw // '');
  return $threshold_map->{$marker} if $marker ne '' && exists $threshold_map->{$marker};
  my $canonical = marker_from_token($marker);
  return $threshold_map->{$canonical} if defined $canonical && $canonical ne '' && exists $threshold_map->{$canonical};
  return $threshold_map->{uc($marker)} if $marker ne '' && exists $threshold_map->{uc($marker)};
  return $threshold_map->{_default} if exists $threshold_map->{_default};
  return undef;
}

sub otu_row_qualifies_for_level {
  my ($row, $level, $thresholds_by_level) = @_;
  return 0 if !defined $row || ref($row) ne 'HASH';
  my $taxon = $row->{$level};
  return 0 if !defined $taxon;
  return 1 if !defined $thresholds_by_level || ref($thresholds_by_level) ne 'HASH';
  my $level_thresholds = $thresholds_by_level->{$level};
  return 1 if !defined $level_thresholds || ref($level_thresholds) ne 'HASH' || !%{$level_thresholds};

  my $threshold = threshold_for_marker($level_thresholds, $row->{marker});
  return 1 if !defined $threshold;
  return 0 if !defined $row->{perc_id};
  return $row->{perc_id} >= $threshold ? 1 : 0;
}

sub is_kingdom_consistent {
  my ($kingdom, $marker, $tax_map) = @_;
  return 1 if !defined $tax_map || ref($tax_map) ne 'HASH';
  my $expected = $tax_map->{$marker};
  return 1 if !defined $expected || $expected eq '';  # no constraint → keep
  return 1 if !defined $kingdom  || $kingdom  eq '';  # unknown kingdom → keep
  return lc($kingdom) eq lc($expected) ? 1 : 0;
}

sub clone_threshold_tree {
  my ($root) = @_;
  return {} if !defined $root || ref($root) ne 'HASH';
  my %copy;
  for my $level (keys %{$root}) {
    my $level_map = $root->{$level};
    next if !defined $level_map || ref($level_map) ne 'HASH';
    $copy{$level} = { %{$level_map} };
  }
  return \%copy;
}

sub is_repeated_header_line {
  my ($line, $header_line) = @_;
  return 0 if !defined $line || !defined $header_line;
  return $line eq $header_line;
}

sub load_spec_interest {
  my ($path, $setref) = @_;
  return unless defined $path && $path ne '' && -f $path;
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return;
  }
  my $hdr = <$FH>;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if defined $hdr && is_repeated_header_line($line, $hdr);
    my @tr = split /\t/, $line;
    next unless @tr >= 4;
    my $sp = trim_text($tr[0]);
    next if $sp eq '';
    my $pos = $tr[1];
    my $obs = $tr[2];
    my $hum = $tr[3];
    if ((defined $pos && $pos =~ /^[0-9]+$/ && $pos != 0) ||
        (defined $obs && $obs =~ /^[0-9]+$/ && $obs != 0) ||
        (defined $hum && $hum =~ /^[0-9]+$/ && $hum != 0)) {
      $setref->{$sp} = 1;
    }
  }
  close $FH;
}

sub to_nonneg_int_or_undef {
  my ($v) = @_;
  return undef if !defined $v;
  $v = trim_text($v);
  return undef if $v eq '' || uc($v) eq 'NA';
  return undef if $v !~ /^\d+$/;
  return 0 + $v;
}

sub to_text_or_undef {
  my ($v) = @_;
  return undef if !defined $v;
  $v = trim_text($v);
  return undef if $v eq '' || uc($v) eq 'NA';
  return $v;
}

sub load_kv_tsv {
  my ($path) = @_;
  return undef if !defined $path || $path eq '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my %kv;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($k, $v) = split /\t/, $line, 2;
    next if !defined $k;
    $k = trim_text($k);
    next if $k eq '';
    $kv{$k} = defined $v ? $v : '';
  }
  close $FH;
  return \%kv;
}

sub resolve_reporting_identity {
  my ($label_raw) = @_;
  return $opt{identity_mode} eq 'track'
    ? normalize_track_reporting_identity($label_raw)
    : SampleLabel::normalize_sample_base($label_raw);
}

sub normalize_track_reporting_identity {
  my ($label_raw) = @_;
  my $label = SampleLabel::normalize_sample_label($label_raw);
  return $label if !defined $label || $label eq '' || $label eq 'unknown';
  return $label if SampleLabel::is_no_adapter_label($label);
  my $marker_pattern = SampleLabel::configured_marker_suffix_pattern();
  $label =~ s/${marker_pattern}$//i if defined $marker_pattern && $marker_pattern ne '';
  $label =~ s/_(?:COI|ITS)\d*$//i;
  return trim_text($label);
}

sub ensure_sample_entry {
  my ($sample_metrics, $label_to_id, $id_to_label, $label_raw) = @_;
  # Normalize to reporting identity: collapse mode strips replicate suffix _N so that
  # W_eDNA_1_1, W_eDNA_1_2, ... accumulate into W_eDNA_1; track mode preserves the
  # track unit while dropping only the terminal marker suffix used in demux reports.
  my $label = resolve_reporting_identity($label_raw);
  $label = 'unknown' if !defined $label || $label eq '';
  if ($opt{identity_mode} eq 'track' && $g_roster_ready
      && $label ne 'unknown'
      && !SampleLabel::is_no_adapter_label($label)
      && !exists $label_to_id->{$label}) {
    die "ERROR: identity-mode=track: observed identity '$label_raw' (resolved: '$label')"
      . " is not in the track roster\n";
  }
  if (exists $label_to_id->{$label}) {
    return $label_to_id->{$label};
  }
  my $id = SampleLabel::stable_sample_id_from_label($label);
  if (exists $id_to_label->{$id} && $id_to_label->{$id} ne $label) {
    warn_once("sample_id_collision:$id");
  }
  $label_to_id->{$label} = $id;
  $id_to_label->{$id} = $label;
  if (!exists $sample_metrics->{$id}) {
    $sample_metrics->{$id} = {
      sample_id         => $id,
      label             => $label,
      reads_demux       => undef,
      reads_demux_by_marker => blank_marker_count_map(),
      reads_demux_coi   => undef,
      reads_demux_its2  => undef,
      reads_blast_assigned => undef,
      otu_active        => undef,
      consensus_emitted => undef,
      replicates        => {},
    };
  }
  return $id;
}

# Given a sample entry and the raw per-read label, ensure a per-replicate
# sub-entry exists inside $entry->{replicates} and return a ref to it.
# Returns undef when the raw label normalises to the same base as the sample
# (i.e. the read comes from a non-replicated sample or the base sample itself).
sub ensure_replicate_sub_entry {
  my ($entry, $raw_label) = @_;
  my $rep_label = $opt{identity_mode} eq 'track'
    ? normalize_track_reporting_identity($raw_label)
    : SampleLabel::normalize_sample_label($raw_label);
  $rep_label = 'unknown' if !defined $rep_label || $rep_label eq '';
  return undef if $rep_label eq $entry->{label};
  if (!exists $entry->{replicates}{$rep_label}) {
    $entry->{replicates}{$rep_label} = {
      label                => $rep_label,
      reads_demux          => undef,
      reads_blast_assigned => undef,
      otu_active           => undef,
      consensus_emitted    => undef,
    };
  }
  return $entry->{replicates}{$rep_label};
}

sub increment_sample_demux_marker_counts {
  my ($entry, $marker_raw) = @_;
  return if !defined $entry || ref($entry) ne 'HASH';
  my $marker = canonical_read_fate_marker($marker_raw);
  return if $marker eq '';
  increment_marker_count($entry->{reads_demux_by_marker}, $marker, 1);
  $entry->{reads_demux_coi} = $entry->{reads_demux_by_marker}{COI}
    if exists $entry->{reads_demux_by_marker}{COI};
  $entry->{reads_demux_its2} = $entry->{reads_demux_by_marker}{ITS2}
    if exists $entry->{reads_demux_by_marker}{ITS2};
}

sub seed_sample_entries_from_roster {
  my ($path, $sample_metrics, $label_to_id, $id_to_label) = @_;
  return unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    if ($opt{identity_mode} eq 'track') {
      die "ERROR: --identity-mode track: sample roster '$path' is missing or empty\n";
    }
    warn_once("missing_or_empty:$path");
    return;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    if ($opt{identity_mode} eq 'track') {
      die "ERROR: --identity-mode track: cannot open sample roster '$path'\n";
    }
    warn_once("open_failed:$path");
    return;
  }
  if ($opt{identity_mode} eq 'track') {
    # Structured TSV: header row required; track_id column required.
    my $hdr = <$FH>;
    die "ERROR: track roster '$path' has no header row\n" unless defined $hdr;
    chomp $hdr;
    my @cols = split /\t/, $hdr, -1;
    my ($tid_col) = grep { $cols[$_] eq 'track_id' } 0 .. $#cols;
    die "ERROR: track roster '$path' has no 'track_id' column\n" unless defined $tid_col;
    while (my $line = <$FH>) {
      chomp $line;
      next if $line =~ /^\s*$/;
      my @f = split /\t/, $line, -1;
      my $tid = trim_text($f[$tid_col] // '');
      next if $tid eq '';
      ensure_sample_entry($sample_metrics, $label_to_id, $id_to_label, $tid);
    }
  } else {
    # Collapse mode: existing whitespace-split behavior (unchanged).
    while (my $line = <$FH>) {
      chomp $line;
      next if $line =~ /^\s*$/;
      my @f = split /\s+/, $line;
      next unless @f;
      my $base_label = trim_text($f[0]);
      next if $base_label eq '';
      my $sid = ensure_sample_entry($sample_metrics, $label_to_id, $id_to_label, $base_label);
      next if !defined $sid || $sid eq '';
      my $entry = $sample_metrics->{$sid};
      if (defined $f[1] && trim_text($f[1]) ne '') {
        ensure_replicate_sub_entry($entry, $f[1]);
      }
    }
  }
  close $FH;
}

sub canonical_track_marker_label {
  my ($raw) = @_;
  my $marker = canonical_marker_token($raw);
  if (!defined $marker || $marker eq '') {
    $marker = marker_from_token($raw);
  }
  return trim_text($marker // '');
}

sub load_track_roster_lookup {
  my ($path) = @_;
  my %by_track_id;
  return \%by_track_id unless defined $path && $path ne '';
  my $FH = open_cached_text_handle($path);
  return \%by_track_id if !defined $FH;
  my $hdr = <$FH>;
  die "ERROR: track roster '$path' has no header row\n" unless defined $hdr;
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $sample_idx = header_index_fallback(\%idx, 'sample_id');
  my $track_idx = header_index_fallback(\%idx, 'track_id');
  my $repnum_idx = header_index_fallback(\%idx, 'replicate_number');
  die "ERROR: track roster '$path' is missing sample_id, track_id, or replicate_number\n"
    unless defined $sample_idx && defined $track_idx && defined $repnum_idx;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $track_idx > $#f;
    my $track_id = trim_text($f[$track_idx]);
    next if $track_id eq '';
    my $sample_id = ($sample_idx <= $#f) ? trim_text($f[$sample_idx]) : '';
    my $replicate_number = ($repnum_idx <= $#f) ? trim_text($f[$repnum_idx]) : '';
    if (exists $by_track_id{$track_id}) {
      my $prev = $by_track_id{$track_id};
      if (($prev->{sample_id} // '') ne $sample_id
          || ($prev->{replicate_number} // '') ne $replicate_number) {
        die "ERROR: track roster '$path' has conflicting rows for track_id '$track_id'\n";
      }
      next;
    }
    $by_track_id{$track_id} = {
      sample_id => $sample_id,
      track_id => $track_id,
      replicate_number => $replicate_number,
    };
  }
  close $FH;
  return \%by_track_id;
}

sub seed_track_unit_metrics_from_identity {
  my ($identity_path, $roster_path) = @_;
  %TRACK_UNIT_METRICS = ();
  %TRACK_UNIT_BY_UNIT_ID = ();
  %TRACK_UNIT_BY_TRACK_MARKER = ();
  %TRACK_ROSTER_BY_TRACK_ID = ();
  return unless defined $identity_path && $identity_path ne '';
  if (!-e $identity_path || !-s $identity_path) {
    die "ERROR: --track-identity '$identity_path' is missing or empty\n";
  }
  my $roster_ref = load_track_roster_lookup($roster_path);
  %TRACK_ROSTER_BY_TRACK_ID = %{$roster_ref};
  my $FH = open_cached_text_handle($identity_path);
  die "ERROR: cannot open track identity '$identity_path'\n" if !defined $FH;
  my $hdr = <$FH>;
  die "ERROR: track identity '$identity_path' has no header row\n" unless defined $hdr;
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $sample_idx = header_index_fallback(\%idx, 'sample_id');
  my $track_idx = header_index_fallback(\%idx, 'track_id');
  my $repnum_idx = header_index_fallback(\%idx, 'replicate_number');
  my $marker_idx = header_index_fallback(\%idx, 'marker_id');
  my $unit_idx = header_index_fallback(\%idx, 'unit_id_track');
  die "ERROR: track identity '$identity_path' is missing sample_id, track_id, replicate_number, marker_id, or unit_id_track\n"
    unless defined $sample_idx && defined $track_idx && defined $repnum_idx && defined $marker_idx && defined $unit_idx;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    my $unit_id = ($unit_idx <= $#f) ? trim_text($f[$unit_idx]) : '';
    next if $unit_id eq '';
    my $track_id = ($track_idx <= $#f) ? trim_text($f[$track_idx]) : '';
    die "ERROR: track identity '$identity_path' contains empty track_id for unit '$unit_id'\n"
      if $track_id eq '';
    my $marker_id = ($marker_idx <= $#f) ? trim_text($f[$marker_idx]) : '';
    my $marker_label = canonical_track_marker_label($marker_id);
    my $roster = $TRACK_ROSTER_BY_TRACK_ID{$track_id};
    die "ERROR: track identity '$identity_path' references track_id '$track_id' that is absent from roster '$roster_path'\n"
      if !defined $roster;
    my $sample_label = trim_text($roster->{sample_id});
    $sample_label = ($sample_idx <= $#f) ? trim_text($f[$sample_idx]) : '' if $sample_label eq '';
    my $replicate_number = trim_text($roster->{replicate_number});
    $replicate_number = ($repnum_idx <= $#f) ? trim_text($f[$repnum_idx]) : '' if $replicate_number eq '';
    my $sample_replicate_label = '';
    if ($sample_label ne '' && $replicate_number ne '') {
      $sample_replicate_label = $sample_label . '_' . $replicate_number;
    } else {
      $sample_replicate_label = $track_id;
    }
    if (exists $TRACK_UNIT_METRICS{$unit_id}) {
      my $prev = $TRACK_UNIT_METRICS{$unit_id};
      if (($prev->{track_replicate_id} // '') ne $track_id
          || ($prev->{track_primer_label} // '') ne $marker_label) {
        die "ERROR: track identity '$identity_path' has conflicting rows for unit_id_track '$unit_id'\n";
      }
      next;
    }
    $TRACK_UNIT_METRICS{$unit_id} = {
      track_unit_id => $unit_id,
      track_sample_label => $sample_label,
      track_replicate_id => $track_id,
      track_replicate_number => ($replicate_number =~ /^\d+$/ ? 0 + $replicate_number : ($replicate_number ne '' ? $replicate_number : undef)),
      track_replicate_label => $track_id,
      track_sample_replicate_label => $sample_replicate_label,
      track_primer_label => $marker_label,
      reads_demux => undef,
      reads_demux_by_marker => blank_marker_count_map(),
      reads_demux_coi => undef,
      reads_demux_its2 => undef,
      reads_blast_assigned => undef,
      otu_active => undef,
      consensus_emitted => undef,
      figures => [],
    };
    $TRACK_UNIT_BY_UNIT_ID{$unit_id} = $unit_id;
    if ($marker_label ne '') {
      if (exists $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_label}
          && $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_label} ne $unit_id) {
        die "ERROR: track identity '$identity_path' maps track_id '$track_id' and marker '$marker_label' to multiple unit_id_track values\n";
      }
      $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_label} = $unit_id;
      if ($marker_id ne '') {
        if (exists $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_id}
            && $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_id} ne $unit_id) {
          die "ERROR: track identity '$identity_path' maps track_id '$track_id' and marker '$marker_id' to multiple unit_id_track values\n";
        }
        $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_id} = $unit_id;
      }
    }
  }
  close $FH;
}

sub resolve_track_unit_metrics_entry {
  my ($raw_sample, $sample_label, $marker_raw) = @_;
  return undef if $opt{identity_mode} ne 'track' || !%TRACK_UNIT_METRICS;
  my $marker_label = canonical_track_marker_label($marker_raw);
  for my $candidate ($raw_sample, $sample_label) {
    my $value = trim_text($candidate // '');
    next if $value eq '';
    if (exists $TRACK_UNIT_BY_UNIT_ID{$value}) {
      return $TRACK_UNIT_METRICS{$TRACK_UNIT_BY_UNIT_ID{$value}};
    }
  }
  for my $track_candidate ($sample_label, $raw_sample) {
    my $track_id = trim_text($track_candidate // '');
    next if $track_id eq '';
    next if !exists $TRACK_UNIT_BY_TRACK_MARKER{$track_id};
    if ($marker_label ne '' && exists $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_label}) {
      my $unit_id = $TRACK_UNIT_BY_TRACK_MARKER{$track_id}{$marker_label};
      return $TRACK_UNIT_METRICS{$unit_id} if exists $TRACK_UNIT_METRICS{$unit_id};
    }
  }
  return undef;
}

sub add_track_identity_fields {
  my ($row, $track_entry) = @_;
  return unless defined $row && ref($row) eq 'HASH';
  return unless defined $track_entry && ref($track_entry) eq 'HASH';
  $row->{track_unit_id} = $track_entry->{track_unit_id};
  $row->{track_sample_label} = $track_entry->{track_sample_label};
  $row->{track_replicate_id} = $track_entry->{track_replicate_id};
  $row->{track_replicate_number} = $track_entry->{track_replicate_number};
  $row->{track_replicate_label} = $track_entry->{track_replicate_label};
  $row->{track_sample_replicate_label} = $track_entry->{track_sample_replicate_label};
  $row->{track_primer_label} = $track_entry->{track_primer_label};
}

sub header_index_fallback {
  my ($idx, @names) = @_;
  for my $name (@names) {
    return $idx->{$name} if exists $idx->{$name};
  }
  return undef;
}

sub count_rows {
  my ($path, $has_header) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    return scalar @{$_parsed_rows{$path}};
  }
  if (!-e $path || !-s $path) {
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $count = 0;
  my $first = 1;
  my $hdr = undef;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    if ($has_header && $first) {
      $hdr = $line;
      $first = 0;
      next;
    }
    next if defined $hdr && is_repeated_header_line($line, $hdr);
    $first = 0;
    $count++;
  }
  close $FH;
  return $count;
}

sub count_non_na_in_column {
  my ($path, $header_name) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my $count = 0;
    for my $row (@{$_parsed_rows{$path}}) {
      my $v = $row->{$header_name};
      $count++ if defined $v && $v ne '' && uc($v) ne 'NA';
    }
    return $count;
  }
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    warn_once("missing_or_empty_header:$path");
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  if (!exists $idx{$header_name}) {
    close $FH;
    warn_once("missing_column:$path:$header_name");
    return undef;
  }
  my $target_idx = $idx{$header_name};
  my $count = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $target_idx > $#f;
    my $v = $f[$target_idx];
    next if !defined $v || $v eq '' || uc($v) eq 'NA';
    $count++;
  }
  close $FH;
  return $count;
}

sub count_value_in_column {
  my ($path, $header_name, $wanted_value) = @_;
  return undef unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    warn_once("missing_or_empty_header:$path");
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  if (!exists $idx{$header_name}) {
    close $FH;
    return undef;
  }
  my $target_idx = $idx{$header_name};
  my $wanted = defined($wanted_value) ? uc($wanted_value) : '';
  my $count = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $target_idx > $#f;
    my $v = defined($f[$target_idx]) ? uc($f[$target_idx]) : '';
    $count++ if $v eq $wanted;
  }
  close $FH;
  return $count;
}

sub count_unique_column_fallback {
  my ($path, @column_names) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my $rows = $_parsed_rows{$path};
    return 0 unless @$rows;
    my $target_col = '';
    for my $cand (@column_names) {
      if (exists $rows->[0]{$cand}) {
        $target_col = $cand;
        last;
      }
    }
    if ($target_col eq '') {
      warn_once("missing_column:$path:" . join('_or_', @column_names));
      return undef;
    }
    my %seen;
    for my $row (@$rows) {
      my $v = $row->{$target_col};
      next if !defined $v || $v eq '' || uc($v) eq 'NA';
      $seen{$v} = 1;
    }
    return scalar keys %seen;
  }
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $target_col = '';
  for my $cand (@column_names) {
    if (exists $idx{$cand}) {
      $target_col = $cand;
      last;
    }
  }
  if ($target_col eq '') {
    close $FH;
    warn_once("missing_column:$path:" . join('_or_', @column_names));
    return undef;
  }
  my $target_idx = $idx{$target_col};
  my %seen;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $target_idx > $#f;
    my $v = $f[$target_idx];
    next if !defined $v || $v eq '' || uc($v) eq 'NA';
    $seen{$v} = 1;
  }
  close $FH;
  return scalar keys %seen;
}

sub normalize_read_id {
  my ($v) = @_;
  $v = trim_text($v);
  return '' if !defined $v || $v eq '' || uc($v) eq 'NA';
  $v =~ s/\s.*$//;
  $v =~ s/\|.*$//;
  return $v;
}

sub classify_sample_bucket {
  my ($label_raw) = @_;
  my $raw = trim_text($label_raw);
  return undef if !defined $raw || $raw eq '';
  my $label = SampleLabel::normalize_sample_label($raw);
  return undef if !defined $label || $label eq '';
  return 'no_adapter' if lc($label) eq 'no_adapter';
  return 'adapter';
}

sub merge_sample_class {
  my ($old, $new) = @_;
  return $old if !defined $new || $new eq '';
  return $new if !defined $old || $old eq '';
  return 'adapter' if $old eq 'adapter' || $new eq 'adapter';
  return 'no_adapter' if $old eq 'no_adapter' || $new eq 'no_adapter';
  return $old;
}

sub blast_row_is_assigned {
  my ($f_ref, $idx_ref) = @_;
  my @f = @{$f_ref};
  my $tax_idx = $idx_ref->{tax_idx};
  my $family_idx = $idx_ref->{family_idx};
  my $genus_idx = $idx_ref->{genus_idx};
  my $species_idx = $idx_ref->{species_idx};
  if (defined $tax_idx && $tax_idx <= $#f) {
    my $tax = trim_text($f[$tax_idx]);
    return 1 if $tax ne '' && uc($tax) ne 'NA' && $tax =~ /^[0-9]+$/ && $tax > 0;
  }
  for my $idx_field ($family_idx, $genus_idx, $species_idx) {
    next unless defined $idx_field && $idx_field <= $#f;
    my $val = trim_text($f[$idx_field]);
    next if $val eq '' || uc($val) eq 'NA';
    return 1 if !is_unassigned_taxon($val);
  }
  return 0;
}

sub canonical_read_fate_marker {
  my ($raw) = @_;
  my $marker = marker_from_token(trim_text($raw // ''));
  return '' if !defined $marker || $marker eq '' || $marker eq 'OTHER';
  return $marker;
}

sub blank_marker_count_map {
  my %counts = map { $_ => undef } @CONFIGURED_MARKERS;
  return \%counts;
}

sub increment_marker_count {
  my ($counts_ref, $marker, $delta) = @_;
  return if !defined $counts_ref || ref($counts_ref) ne 'HASH';
  return if !defined $marker || $marker eq '' || $marker eq 'OTHER';
  $delta = 1 if !defined $delta;
  $counts_ref->{$marker} = 0 unless defined $counts_ref->{$marker};
  $counts_ref->{$marker} += $delta;
}

sub set_legacy_marker_aliases {
  my ($dst_ref, $prefix, $counts_ref) = @_;
  return if !defined $dst_ref || ref($dst_ref) ne 'HASH';
  return if !defined $counts_ref || ref($counts_ref) ne 'HASH';
  for my $marker (qw(COI ITS2 OTHER)) {
    my $suffix = lc($marker);
    my $value = exists $counts_ref->{$marker} ? $counts_ref->{$marker} : undef;
    $dst_ref->{"${prefix}_${suffix}"} = $value;
  }
}

sub sample_label_marker_for_read_fate {
  my ($raw) = @_;
  return canonical_read_fate_marker(SampleLabel::extract_marker_from_label($raw));
}

sub classify_read_fate_bucket_label {
  my ($label_raw) = @_;
  my $raw = trim_text($label_raw);
  return undef if !defined $raw || $raw eq '' || uc($raw) eq 'NA';
  my $label = SampleLabel::normalize_sample_label($raw);
  return undef if !defined $label || $label eq '' || lc($label) eq 'unknown';
  return 'no_adapter' if SampleLabel::is_no_adapter_label($label);
  my $base = SampleLabel::normalize_sample_base($label);
  return undef if !defined $base || $base eq '';
  return undef if lc($base) eq 'unknown' || lc($base) eq 'no_adapter';
  return 'adapter';
}

sub make_marker_split_warning_counts {
  return {
    cross_source_disagreement => {
      demult_over_blast => 0,
      demult_over_sample_label => 0,
      blast_over_sample_label => 0,
    },
  };
}

sub make_marker_split_fatal_counts {
  return {
    same_source_conflict => {
      demult => 0,
      blast => 0,
      sample_label => 0,
    },
    bucket_conflict => 0,
    bucket_unresolved => 0,
    unresolved_marker => 0,
    demux_stage_absent => 0,
    blast_stage_absent => 0,
    missing_or_unusable_demux_read_id => 0,
    missing_or_unusable_demux_sample => 0,
    missing_or_unusable_blast_read_id => 0,
    missing_required_global_total => 0,
    missing_required_stage_total => 0,
    demux_disabled => 0,
    invalid_stage_order_on_target_gt_total => 0,
    invalid_stage_order_demux_gt_on_target => 0,
    invalid_stage_order_blast_seen_gt_demux => 0,
    invalid_stage_order_assigned_gt_seen => 0,
    invalid_stage_order_unassigned_gt_seen => 0,
    invalid_stage_order_assigned_plus_unassigned_ne_seen => 0,
    invariant_no_adapter_gt_demux => 0,
    invariant_demux_split_mismatch => 0,
    invariant_blast_seen_split_mismatch => 0,
    invariant_blast_assigned_split_mismatch => 0,
    invariant_blast_unassigned_split_mismatch => 0,
    chart_negative_skipped_coi => 0,
    chart_negative_skipped_its2 => 0,
    chart_negative_on_target_not_demultiplexed => 0,
    chart_negative_off_target => 0,
    chart_total_mismatch => 0,
  };
}

sub read_fate_reason_group {
  my ($code) = @_;
  return 'chart' if defined $code && $code =~ /^chart_/;
  return 'chart' if defined $code && $code eq 'chart_total_mismatch';
  return 'data';
}

sub add_read_fate_reason {
  my ($data_ref, $chart_ref, $code) = @_;
  return if !defined $code || $code eq '';
  if (read_fate_reason_group($code) eq 'chart') {
    $chart_ref->{$code} = 1;
  } else {
    $data_ref->{$code} = 1;
  }
}

sub build_public_marker_split_read_fate {
  my ($demult_path, $blast_path, $blast_unassigned_ids_path, $reads_total, $reads_on_target, $live_current_mode) = @_;
  my $first_seen_read_fate_mode = (
    (defined $demult_path && $demult_path =~ /read_fate_demult_first_seen[.]tsv(?:[.]gz)?$/)
    || (defined $blast_path && $blast_path =~ /read_fate_blast_first_seen[.]tsv(?:[.]gz)?$/)
  ) ? 1 : 0;

  my %read_fate = (
    demux_total_reads => undef,
    no_adapter_reads => undef,
    demux_enabled => undef,
    blast_seen_reads => undef,
    blast_assigned_reads => undef,
    blast_unassigned_reads => undef,
    blast_seen_reads_unbucketed => undef,
    marker_split_status => 'invalid',
    data_reason_codes => [],
    chart_reason_codes => [],
    marker_split_invalid_read_count => 0,
    marker_split_warning_counts => make_marker_split_warning_counts(),
    marker_split_fatal_counts => make_marker_split_fatal_counts(),
    demux_total_reads_coi => undef,
    demux_total_reads_its2 => undef,
    blast_seen_reads_coi => undef,
    blast_seen_reads_its2 => undef,
    blast_assigned_reads_coi => undef,
    blast_assigned_reads_its2 => undef,
    blast_unassigned_reads_coi => undef,
    blast_unassigned_reads_its2 => undef,
    chart_blast_assigned_coi => undef,
    chart_blast_assigned_its2 => undef,
    chart_blast_unassigned_coi => undef,
    chart_blast_unassigned_its2 => undef,
    chart_blast_skipped_coi => undef,
    chart_blast_skipped_its2 => undef,
    chart_on_target_not_demultiplexed => undef,
    chart_off_target => undef,
    marker_counts => {
      demux_total_reads => blank_marker_count_map(),
      blast_seen_reads => blank_marker_count_map(),
      blast_assigned_reads => blank_marker_count_map(),
      blast_unassigned_reads => blank_marker_count_map(),
      chart_blast_assigned => blank_marker_count_map(),
      chart_blast_unassigned => blank_marker_count_map(),
      chart_blast_skipped => blank_marker_count_map(),
    },
  );

  my %data_reasons;
  my %chart_reasons;
  my %invalid_read_ids;
  my %bucket_conflict_reads;
  my %bucket_unresolved_reads;
  my %marker_unresolved_reads;
  my %same_source_conflict_seen;
  my %warning_seen;
  my %demux_reads;
  my %blast_reads;

  my $demux_stage = 'absent';
  my $blast_stage = 'absent';
  my $blast_has_assignment_columns = 0;
  my $read_level_marker_failure = 0;
  my $demux_bucket_invalid = 0;
  my $missing_global_total = 0;
  my $first_seen_stage_lag = 0;

  if (!defined $demult_path || $demult_path eq '' || !-e $demult_path || !-s $demult_path) {
    $read_fate{marker_split_fatal_counts}{demux_stage_absent}++;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'demux_stage_absent');
    $read_fate{demux_total_reads} = 0;
    $read_fate{no_adapter_reads} = 0;
    $read_fate{demux_enabled} = JSON::PP::false();
  } else {
    my $FH = open_cached_text_handle($demult_path);
    if (defined $FH) {
      my $hdr = <$FH>;
      if (defined $hdr) {
        chomp $hdr;
        my @cols = split /\t/, $hdr, -1;
        my %idx;
        for my $i (0 .. $#cols) {
          $idx{$cols[$i]} = $i;
        }
        my $read_idx = header_index_fallback(\%idx, 'read_id');
        my $sample_idx = header_index_fallback(\%idx, 'sample');
        my $bchom_idx = header_index_fallback(\%idx, 'barcode_by_homology');
        my $usable_id_rows = 0;
        my $usable_bucket_rows = 0;
        if (!defined $read_idx) {
          $demux_stage = 'broken';
          $read_fate{marker_split_fatal_counts}{missing_or_unusable_demux_read_id}++;
          add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_demux_read_id');
        } elsif (!defined $sample_idx) {
          $demux_stage = 'broken';
          $read_fate{marker_split_fatal_counts}{missing_or_unusable_demux_sample}++;
          add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_demux_sample');
        } else {
          while (my $line = <$FH>) {
            chomp $line;
            next if $line =~ /^\s*$/;
            next if is_repeated_header_line($line, $hdr);
            my @f = split /\t/, $line, -1;
            next if $read_idx > $#f;
            my $rid = normalize_read_id($f[$read_idx]);
            next if $rid eq '';
            $usable_id_rows++;
            my $sample_raw = ($sample_idx <= $#f) ? $f[$sample_idx] : '';
            my $bucket = classify_read_fate_bucket_label($sample_raw);
            $usable_bucket_rows++ if defined $bucket;
            my $st = $demux_reads{$rid} ||= {
              bucket_labels => {},
              bucket_unresolved => 0,
              source_markers => {
                demult => {},
                blast => {},
                sample_label => {},
              },
            };
            if (defined $bucket) {
              $st->{bucket_labels}{$bucket} = 1;
            } else {
              $st->{bucket_unresolved} = 1;
            }
            my $sample_marker = sample_label_marker_for_read_fate($sample_raw);
            $st->{source_markers}{sample_label}{$sample_marker} = 1 if $sample_marker ne '';
            if (defined $bchom_idx && $bchom_idx <= $#f) {
              my $demult_marker = canonical_read_fate_marker($f[$bchom_idx]);
              $st->{source_markers}{demult}{$demult_marker} = 1 if $demult_marker ne '';
            }
          }
          if (!$usable_id_rows) {
            $demux_stage = 'broken';
            $read_fate{marker_split_fatal_counts}{missing_or_unusable_demux_read_id}++;
            add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_demux_read_id');
          } elsif (!$usable_bucket_rows) {
            $demux_stage = 'broken';
            $read_fate{marker_split_fatal_counts}{missing_or_unusable_demux_sample}++;
            add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_demux_sample');
          } else {
            $demux_stage = 'usable';
            $read_fate{demux_total_reads} = scalar keys %demux_reads;
          }
        }
      }
      close $FH;
    } else {
      $demux_stage = 'broken';
      $read_fate{marker_split_fatal_counts}{missing_or_unusable_demux_read_id}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_demux_read_id');
    }
    if ($demux_stage eq 'broken') {
      $read_fate{demux_total_reads} = undef;
      $read_fate{no_adapter_reads} = undef;
      $read_fate{demux_enabled} = undef;
    }
  }

  if (!defined $blast_path || $blast_path eq '' || !-e $blast_path || !-s $blast_path) {
    $read_fate{marker_split_fatal_counts}{blast_stage_absent}++;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'blast_stage_absent');
    $read_fate{blast_seen_reads} = undef;
    $read_fate{blast_assigned_reads} = undef;
    $read_fate{blast_unassigned_reads} = undef;
    $read_fate{blast_seen_reads_unbucketed} = undef;
  } else {
    my $FH = open_cached_text_handle($blast_path);
    if (defined $FH) {
      my $hdr = <$FH>;
      if (defined $hdr) {
        chomp $hdr;
        my @cols = split /\t/, $hdr, -1;
        my %idx;
        for my $i (0 .. $#cols) {
          $idx{$cols[$i]} = $i;
        }
        my $read_idx = header_index_fallback(\%idx, 'read_id');
        my $sample_idx = header_index_fallback(\%idx, 'sample');
        my $bchom_idx = header_index_fallback(\%idx, 'barcode_by_homology');
        my $tax_idx = header_index_fallback(\%idx, 'otu_taxid', 'taxid');
        my $family_idx = header_index_fallback(\%idx, 'otu_family', 'family');
        my $genus_idx = header_index_fallback(\%idx, 'otu_genus', 'genus');
        my $species_idx = header_index_fallback(\%idx, 'otu_species', 'species');
        my $usable_id_rows = 0;
        $blast_has_assignment_columns = (defined $tax_idx || defined $family_idx || defined $genus_idx || defined $species_idx) ? 1 : 0;
        if (!defined $read_idx) {
          $blast_stage = 'broken';
          $read_fate{marker_split_fatal_counts}{missing_or_unusable_blast_read_id}++;
          add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_blast_read_id');
        } else {
          while (my $line = <$FH>) {
            chomp $line;
            next if $line =~ /^\s*$/;
            next if is_repeated_header_line($line, $hdr);
            my @f = split /\t/, $line, -1;
            next if $read_idx > $#f;
            my $rid = normalize_read_id($f[$read_idx]);
            next if $rid eq '';
            $usable_id_rows++;
            my $st;
            if ($live_current_mode) {
              $st = {
                bucket_labels => {},
                bucket_unresolved => 0,
                source_markers => {
                  demult => {},
                  blast => {},
                  sample_label => {},
                },
                assigned => 0,
              };
              $blast_reads{$rid} = $st;
            } else {
              $st = $blast_reads{$rid} ||= {
                bucket_labels => {},
                bucket_unresolved => 0,
                source_markers => {
                  demult => {},
                  blast => {},
                  sample_label => {},
                },
                assigned => 0,
              };
            }
            my $sample_raw = (defined $sample_idx && $sample_idx <= $#f) ? $f[$sample_idx] : '';
            my $bucket = classify_read_fate_bucket_label($sample_raw);
            if (defined $bucket) {
              $st->{bucket_labels}{$bucket} = 1;
            } else {
              $st->{bucket_unresolved} = 1;
            }
            my $sample_marker = sample_label_marker_for_read_fate($sample_raw);
            $st->{source_markers}{sample_label}{$sample_marker} = 1 if $sample_marker ne '';
            if (defined $bchom_idx && $bchom_idx <= $#f) {
              my $blast_marker = canonical_read_fate_marker($f[$bchom_idx]);
              $st->{source_markers}{blast}{$blast_marker} = 1 if $blast_marker ne '';
            }
            if ($blast_has_assignment_columns) {
              $st->{assigned} = 1 if blast_row_is_assigned(
                \@f,
                {
                  tax_idx => $tax_idx,
                  family_idx => $family_idx,
                  genus_idx => $genus_idx,
                  species_idx => $species_idx,
                }
              );
            }
          }
          if (!$usable_id_rows) {
            $blast_stage = 'broken';
            $read_fate{marker_split_fatal_counts}{missing_or_unusable_blast_read_id}++;
            add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_blast_read_id');
          } else {
            $blast_stage = 'usable';
          }
        }
      }
      close $FH;
    } else {
      $blast_stage = 'broken';
      $read_fate{marker_split_fatal_counts}{missing_or_unusable_blast_read_id}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_or_unusable_blast_read_id');
    }
    if ($blast_stage eq 'broken') {
      $read_fate{blast_seen_reads} = undef;
      $read_fate{blast_assigned_reads} = undef;
      $read_fate{blast_unassigned_reads} = undef;
      $read_fate{blast_seen_reads_unbucketed} = undef;
    }
  }

  if (defined $blast_unassigned_ids_path && $blast_unassigned_ids_path ne '' && -e $blast_unassigned_ids_path && -s $blast_unassigned_ids_path) {
    if (open my $UFH, '<', $blast_unassigned_ids_path) {
      while (my $line = <$UFH>) {
        chomp $line;
        next if $line =~ /^\s*$/;
        my $rid = normalize_read_id($line);
        next if $rid eq '';
        next if !$live_current_mode && exists $blast_reads{$rid};
        next if !$live_current_mode && !exists $demux_reads{$rid};
        # Live current-run status uses a cumulative unassigned-ID sidecar. If that
        # sidecar contains an ID that cannot be resolved through demux metadata,
        # skip it rather than making the whole run-status bar invalid.
        next if $live_current_mode && !exists $demux_reads{$rid};

        my %bucket_labels = ();
        my $bucket_unresolved = $live_current_mode ? 1 : 0;
        my %demult_markers = ();
        my %sample_label_markers = ();
        if (exists $demux_reads{$rid}) {
          %bucket_labels = %{ $demux_reads{$rid}{bucket_labels} || {} };
          $bucket_unresolved = $demux_reads{$rid}{bucket_unresolved} ? 1 : 0;
          %demult_markers = %{ $demux_reads{$rid}{source_markers}{demult} || {} };
          %sample_label_markers = %{ $demux_reads{$rid}{source_markers}{sample_label} || {} };
        }
        next if $live_current_mode && !%demult_markers && !%sample_label_markers;

        my $synthetic = {
          bucket_labels => { %bucket_labels },
          bucket_unresolved => $bucket_unresolved,
          source_markers => {
            demult => { %demult_markers },
            blast => {},
            sample_label => { %sample_label_markers },
          },
          assigned => 0,
        };
        if ($live_current_mode) {
          $blast_reads{$rid} = $synthetic;
        } else {
          $blast_reads{$rid} = $synthetic;
        }
      }
      close $UFH;
    } else {
      warn_once("open_failed:$blast_unassigned_ids_path");
    }
  }

  my %all_read_ids;
  $all_read_ids{$_} = 1 for (keys %demux_reads, keys %blast_reads);
  my %resolved_marker_by_read;
  my %demux_bucket_state_by_read;
  my %blast_bucket_state_by_read;

  for my $rid (keys %all_read_ids) {
    my %source_values = (
      demult => {},
      blast => {},
      sample_label => {},
    );
    if (exists $demux_reads{$rid}) {
      for my $src (qw(demult sample_label)) {
        for my $marker (keys %{$demux_reads{$rid}{source_markers}{$src} || {}}) {
          $source_values{$src}{$marker} = 1;
        }
      }
    }
    if (exists $blast_reads{$rid}) {
      for my $src (qw(blast sample_label)) {
        for my $marker (keys %{$blast_reads{$rid}{source_markers}{$src} || {}}) {
          $source_values{$src}{$marker} = 1;
        }
      }
    }

    for my $src (qw(demult blast sample_label)) {
      my @markers = sort keys %{$source_values{$src}};
      if (@markers > 1) {
        my $seen_key = "$rid\t$src";
        next if $same_source_conflict_seen{$seen_key}++;
        $read_level_marker_failure = 1;
        $invalid_read_ids{$rid} = 1;
        $read_fate{marker_split_fatal_counts}{same_source_conflict}{$src}++;
        add_read_fate_reason(\%data_reasons, \%chart_reasons, 'same_source_conflict');
      }
    }

    my $demux_bucket_state = '';
    if (exists $demux_reads{$rid}) {
      my $st = $demux_reads{$rid};
      my $has_adapter = $st->{bucket_labels}{adapter} ? 1 : 0;
      my $has_no_adapter = $st->{bucket_labels}{no_adapter} ? 1 : 0;
      if ($has_adapter && $has_no_adapter) {
        $demux_bucket_state = 'conflict';
      } elsif ($st->{bucket_unresolved}) {
        $demux_bucket_state = 'unresolved';
      } elsif ($has_adapter) {
        $demux_bucket_state = 'adapter';
      } elsif ($has_no_adapter) {
        $demux_bucket_state = 'no_adapter';
      } else {
        $demux_bucket_state = 'unresolved';
      }
      $demux_bucket_state_by_read{$rid} = $demux_bucket_state;
      if ($demux_bucket_state eq 'conflict') {
        $bucket_conflict_reads{$rid} = 1;
        $invalid_read_ids{$rid} = 1;
        $demux_bucket_invalid = 1;
      } elsif ($demux_bucket_state eq 'unresolved') {
        $bucket_unresolved_reads{$rid} = 1;
        $invalid_read_ids{$rid} = 1;
        $demux_bucket_invalid = 1;
      }
    }

    my $blast_bucket_state = '';
    if (exists $blast_reads{$rid}) {
      my $st = $blast_reads{$rid};
      my $has_adapter = $st->{bucket_labels}{adapter} ? 1 : 0;
      my $has_no_adapter = $st->{bucket_labels}{no_adapter} ? 1 : 0;
      if ($has_adapter && $has_no_adapter) {
        $blast_bucket_state = 'conflict';
      } elsif ($st->{bucket_unresolved}) {
        $blast_bucket_state = 'unresolved';
      } elsif ($has_adapter) {
        $blast_bucket_state = 'adapter';
      } elsif ($has_no_adapter) {
        $blast_bucket_state = 'no_adapter';
      } else {
        $blast_bucket_state = 'unresolved';
      }
      $blast_bucket_state_by_read{$rid} = $blast_bucket_state;
      if ($blast_bucket_state eq 'conflict') {
        $bucket_conflict_reads{$rid} = 1;
        $invalid_read_ids{$rid} = 1;
      } elsif ($blast_bucket_state eq 'unresolved') {
        $bucket_unresolved_reads{$rid} = 1;
        $invalid_read_ids{$rid} = 1;
      }
    }

    next if exists $same_source_conflict_seen{"$rid\tdemult"} || exists $same_source_conflict_seen{"$rid\tblast"} || exists $same_source_conflict_seen{"$rid\tsample_label"};

    my $winner_source = '';
    my $winner_marker = '';
    for my $src (qw(demult blast sample_label)) {
      my @markers = sort keys %{$source_values{$src}};
      if (@markers == 1) {
        $winner_source = $src;
        $winner_marker = $markers[0];
        last;
      }
    }
    if ($winner_marker eq '') {
      $read_level_marker_failure = 1;
      $invalid_read_ids{$rid} = 1;
      $marker_unresolved_reads{$rid} = 1;
      next;
    }
    $resolved_marker_by_read{$rid} = $winner_marker;
    if ($winner_source eq 'demult') {
      my @blast_markers = sort keys %{$source_values{blast}};
      if (@blast_markers == 1 && $blast_markers[0] ne $winner_marker && !$warning_seen{"$rid\tdemult_over_blast"}++) {
        $read_fate{marker_split_warning_counts}{cross_source_disagreement}{demult_over_blast}++;
      }
      my @sample_markers = sort keys %{$source_values{sample_label}};
      if (@sample_markers == 1 && $sample_markers[0] ne $winner_marker && !$warning_seen{"$rid\tdemult_over_sample_label"}++) {
        $read_fate{marker_split_warning_counts}{cross_source_disagreement}{demult_over_sample_label}++;
      }
    } elsif ($winner_source eq 'blast') {
      my @sample_markers = sort keys %{$source_values{sample_label}};
      if (@sample_markers == 1 && $sample_markers[0] ne $winner_marker && !$warning_seen{"$rid\tblast_over_sample_label"}++) {
        $read_fate{marker_split_warning_counts}{cross_source_disagreement}{blast_over_sample_label}++;
      }
    }
  }

  if (%bucket_conflict_reads) {
    $read_fate{marker_split_fatal_counts}{bucket_conflict} = scalar keys %bucket_conflict_reads;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'bucket_conflict');
  }
  if (%bucket_unresolved_reads) {
    $read_fate{marker_split_fatal_counts}{bucket_unresolved} = scalar keys %bucket_unresolved_reads;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'bucket_unresolved');
  }
  if (%marker_unresolved_reads) {
    $read_level_marker_failure = 1;
    $read_fate{marker_split_fatal_counts}{unresolved_marker} = scalar keys %marker_unresolved_reads;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'unresolved_marker');
  }

  if ($blast_stage eq 'usable') {
    my $seen_total = scalar keys %blast_reads;
    $read_fate{blast_seen_reads} = $seen_total;
    $read_fate{blast_seen_reads_unbucketed} = scalar grep {
      ($blast_bucket_state_by_read{$_} || '') eq 'unresolved' || ($blast_bucket_state_by_read{$_} || '') eq 'conflict'
    } keys %blast_reads;
    if ($blast_has_assignment_columns) {
      my $assigned_total = 0;
      for my $rid (keys %blast_reads) {
        $assigned_total++ if $blast_reads{$rid}{assigned};
      }
      $read_fate{blast_assigned_reads} = $assigned_total;
      $read_fate{blast_unassigned_reads} = $seen_total - $assigned_total;
    } else {
      $read_fate{marker_split_fatal_counts}{missing_required_stage_total}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_required_stage_total');
      $read_fate{blast_assigned_reads} = undef;
      $read_fate{blast_unassigned_reads} = undef;
    }
  }

  if ($demux_stage eq 'usable') {
    my $no_adapter_count = scalar grep { ($demux_bucket_state_by_read{$_} || '') eq 'no_adapter' } keys %demux_reads;
    if ($demux_bucket_invalid) {
      $read_fate{no_adapter_reads} = undef;
      $read_fate{demux_enabled} = undef;
    } else {
      $read_fate{no_adapter_reads} = $no_adapter_count;
    }
  }

  if (!defined $reads_total || !defined $reads_on_target) {
    $missing_global_total = 1;
    $read_fate{marker_split_fatal_counts}{missing_required_global_total}++;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'missing_required_global_total');
  }

  if ($demux_stage eq 'usable' && !$demux_bucket_invalid && !$missing_global_total) {
    my $has_adapter_demux = scalar grep { ($demux_bucket_state_by_read{$_} || '') eq 'adapter' } keys %demux_reads;
    if (($read_fate{demux_total_reads} // 0) > 0 && $has_adapter_demux > 0) {
      $read_fate{demux_enabled} = JSON::PP::true();
    } else {
      $read_fate{demux_enabled} = JSON::PP::false();
      $read_fate{marker_split_fatal_counts}{demux_disabled}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'demux_disabled');
    }
  }

  if (!$missing_global_total && defined $reads_total && defined $reads_on_target && $reads_on_target > $reads_total) {
    $read_fate{marker_split_fatal_counts}{invalid_stage_order_on_target_gt_total}++;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invalid_stage_order_on_target_gt_total');
  }
  if ($demux_stage eq 'usable' && !$missing_global_total && defined $read_fate{demux_total_reads} && defined $reads_on_target && $read_fate{demux_total_reads} > $reads_on_target) {
    $read_fate{marker_split_fatal_counts}{invalid_stage_order_demux_gt_on_target}++;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invalid_stage_order_demux_gt_on_target');
  }
  if ($demux_stage eq 'usable' && $blast_stage eq 'usable'
      && defined $read_fate{demux_total_reads} && defined $read_fate{blast_seen_reads}
      && !$missing_global_total) {
    if ($read_fate{blast_seen_reads} > $read_fate{demux_total_reads}) {
      if ($first_seen_read_fate_mode) {
        $first_seen_stage_lag = 1;
      } else {
        $read_fate{marker_split_fatal_counts}{invalid_stage_order_blast_seen_gt_demux}++;
        add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invalid_stage_order_blast_seen_gt_demux');
      }
    }
    if (defined $read_fate{blast_assigned_reads} && defined $read_fate{blast_seen_reads}
        && $read_fate{blast_assigned_reads} > $read_fate{blast_seen_reads}) {
      $read_fate{marker_split_fatal_counts}{invalid_stage_order_assigned_gt_seen}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invalid_stage_order_assigned_gt_seen');
    }
    if (defined $read_fate{blast_unassigned_reads} && defined $read_fate{blast_seen_reads}
        && $read_fate{blast_unassigned_reads} > $read_fate{blast_seen_reads}) {
      $read_fate{marker_split_fatal_counts}{invalid_stage_order_unassigned_gt_seen}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invalid_stage_order_unassigned_gt_seen');
    }
    if (defined $read_fate{blast_assigned_reads} && defined $read_fate{blast_unassigned_reads}
        && defined $read_fate{blast_seen_reads}
        && ($read_fate{blast_assigned_reads} + $read_fate{blast_unassigned_reads}) != $read_fate{blast_seen_reads}) {
      $read_fate{marker_split_fatal_counts}{invalid_stage_order_assigned_plus_unassigned_ne_seen}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invalid_stage_order_assigned_plus_unassigned_ne_seen');
    }
  }
  if (defined $read_fate{no_adapter_reads} && defined $read_fate{demux_total_reads}
      && $read_fate{no_adapter_reads} > $read_fate{demux_total_reads}) {
    $read_fate{marker_split_fatal_counts}{invariant_no_adapter_gt_demux}++;
    add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invariant_no_adapter_gt_demux');
  }

  my %marker_splits = (
    demux_total_reads => blank_marker_count_map(),
    blast_seen_reads => blank_marker_count_map(),
    blast_assigned_reads => blank_marker_count_map(),
    blast_unassigned_reads => blank_marker_count_map(),
  );
  for my $rid (keys %demux_reads) {
    next unless defined $resolved_marker_by_read{$rid};
    increment_marker_count($marker_splits{demux_total_reads}, $resolved_marker_by_read{$rid}, 1);
  }
  for my $rid (keys %blast_reads) {
    next unless defined $resolved_marker_by_read{$rid};
    my $marker = $resolved_marker_by_read{$rid};
    next if !defined $marker || $marker eq '' || $marker eq 'OTHER';
    increment_marker_count($marker_splits{blast_seen_reads}, $marker, 1);
    if ($blast_has_assignment_columns) {
      if ($blast_reads{$rid}{assigned}) {
        increment_marker_count($marker_splits{blast_assigned_reads}, $marker, 1);
      } else {
        increment_marker_count($marker_splits{blast_unassigned_reads}, $marker, 1);
      }
    }
  }

  if (!$read_level_marker_failure) {
    my $demux_marker_total = 0;
    $demux_marker_total += $_ // 0 for values %{$marker_splits{demux_total_reads}};
    if (defined $read_fate{demux_total_reads}
        && $demux_marker_total != $read_fate{demux_total_reads}) {
      $read_fate{marker_split_fatal_counts}{invariant_demux_split_mismatch}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invariant_demux_split_mismatch');
    }
    my $blast_seen_marker_total = 0;
    $blast_seen_marker_total += $_ // 0 for values %{$marker_splits{blast_seen_reads}};
    if (defined $read_fate{blast_seen_reads}
        && $blast_seen_marker_total != $read_fate{blast_seen_reads}) {
      $read_fate{marker_split_fatal_counts}{invariant_blast_seen_split_mismatch}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invariant_blast_seen_split_mismatch');
    }
    my $blast_assigned_marker_total = 0;
    $blast_assigned_marker_total += $_ // 0 for values %{$marker_splits{blast_assigned_reads}};
    if (defined $read_fate{blast_assigned_reads}
        && $blast_assigned_marker_total != $read_fate{blast_assigned_reads}) {
      $read_fate{marker_split_fatal_counts}{invariant_blast_assigned_split_mismatch}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invariant_blast_assigned_split_mismatch');
    }
    my $blast_unassigned_marker_total = 0;
    $blast_unassigned_marker_total += $_ // 0 for values %{$marker_splits{blast_unassigned_reads}};
    if (defined $read_fate{blast_unassigned_reads}
        && $blast_unassigned_marker_total != $read_fate{blast_unassigned_reads}) {
      $read_fate{marker_split_fatal_counts}{invariant_blast_unassigned_split_mismatch}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'invariant_blast_unassigned_split_mismatch');
    }
  }

  my %chart_values;
  if (!$read_level_marker_failure
      && !$missing_global_total
      && $demux_stage eq 'usable'
      && $blast_stage eq 'usable'
      && defined $read_fate{blast_assigned_reads}
      && defined $read_fate{blast_unassigned_reads}) {
    my @chart_markers = @CONFIGURED_MARKERS ? @CONFIGURED_MARKERS : qw(COI ITS2);
    my %chart_assigned = (COI => 0, ITS2 => 0);
    my %chart_unassigned = (COI => 0, ITS2 => 0);
    my %chart_skipped = (COI => 0, ITS2 => 0);
    for my $marker (@chart_markers) {
      $chart_assigned{$marker} = $marker_splits{blast_assigned_reads}{$marker} // 0;
      $chart_unassigned{$marker} = $marker_splits{blast_unassigned_reads}{$marker} // 0;
      my $raw_skipped = ($marker_splits{demux_total_reads}{$marker} // 0) - ($marker_splits{blast_seen_reads}{$marker} // 0);
      if ($raw_skipped < 0 && $first_seen_read_fate_mode) {
        $first_seen_stage_lag = 1;
        $chart_skipped{$marker} = 0;
      } else {
        $chart_skipped{$marker} = $raw_skipped;
      }
    }
    %chart_values = (
      chart_blast_assigned_coi => $chart_assigned{COI},
      chart_blast_assigned_its2 => $chart_assigned{ITS2},
      chart_blast_unassigned_coi => $chart_unassigned{COI},
      chart_blast_unassigned_its2 => $chart_unassigned{ITS2},
      chart_blast_skipped_coi => $chart_skipped{COI},
      chart_blast_skipped_its2 => $chart_skipped{ITS2},
      chart_on_target_not_demultiplexed => $reads_on_target - ($read_fate{demux_total_reads} // 0),
      chart_off_target => $reads_total - $reads_on_target,
    );
    $read_fate{marker_counts}{chart_blast_assigned} = \%chart_assigned;
    $read_fate{marker_counts}{chart_blast_unassigned} = \%chart_unassigned;
    $read_fate{marker_counts}{chart_blast_skipped} = \%chart_skipped;
    for my $key (keys %chart_values) {
      if ($chart_values{$key} < 0) {
        my $reason = $key;
        $reason =~ s/^chart_/chart_negative_/;
        $read_fate{marker_split_fatal_counts}{$reason}++;
        add_read_fate_reason(\%data_reasons, \%chart_reasons, $reason);
      }
    }
    my $chart_total = 0;
    $chart_total += $chart_values{$_} for keys %chart_values;
    if ($chart_total != $reads_total && !$first_seen_stage_lag) {
      $read_fate{marker_split_fatal_counts}{chart_total_mismatch}++;
      add_read_fate_reason(\%data_reasons, \%chart_reasons, 'chart_total_mismatch');
    }
  }

  $read_fate{marker_split_invalid_read_count} = scalar keys %invalid_read_ids;
  $read_fate{marker_counts}{demux_total_reads} = $marker_splits{demux_total_reads};
  $read_fate{marker_counts}{blast_seen_reads} = $marker_splits{blast_seen_reads};
  $read_fate{marker_counts}{blast_assigned_reads} = $marker_splits{blast_assigned_reads};
  $read_fate{marker_counts}{blast_unassigned_reads} = $marker_splits{blast_unassigned_reads};
  set_legacy_marker_aliases(\%read_fate, 'demux_total_reads', $marker_splits{demux_total_reads});
  set_legacy_marker_aliases(\%read_fate, 'blast_seen_reads', $marker_splits{blast_seen_reads});
  set_legacy_marker_aliases(\%read_fate, 'blast_assigned_reads', $marker_splits{blast_assigned_reads});
  set_legacy_marker_aliases(\%read_fate, 'blast_unassigned_reads', $marker_splits{blast_unassigned_reads});
  if (!%data_reasons && !%chart_reasons) {
    $read_fate{marker_split_status} = 'ok';
    if (%chart_values) {
      for my $key (sort keys %chart_values) {
        $read_fate{$key} = $chart_values{$key};
      }
    }
  } else {
    $read_fate{marker_split_status} = 'invalid';
  }

  $read_fate{data_reason_codes} = [sort keys %data_reasons];
  $read_fate{chart_reason_codes} = [sort keys %chart_reasons];

  return \%read_fate;
}

sub collect_blast_read_fate_and_sample_metrics {
  my ($path, $sample_metrics, $label_to_id, $id_to_label, $track_unit_metrics) = @_;
  my %out = (
    enabled => 0,
    assignment_status => undef,
    seen => {
      total => 0,
      adapter => 0,
      no_adapter => 0,
      unbucketed => 0,
    },
    assigned => {
      total => undef,
      adapter => undef,
      no_adapter => undef,
    },
    unassigned => {
      total => undef,
      adapter => undef,
      no_adapter => undef,
    },
  );
  return \%out unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return \%out;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return \%out;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    warn_once("missing_or_empty_header:$path");
    return \%out;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $read_idx = header_index_fallback(\%idx, 'read_id');
  my $sample_idx = header_index_fallback(\%idx, 'sample');
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $tax_idx = header_index_fallback(\%idx, 'otu_taxid', 'taxid');
  my $family_idx = header_index_fallback(\%idx, 'otu_family', 'family');
  my $genus_idx = header_index_fallback(\%idx, 'otu_genus', 'genus');
  my $species_idx = header_index_fallback(\%idx, 'otu_species', 'species');
  if (!defined $read_idx) {
    close $FH;
    warn_once("missing_column:$path:read_id");
    return \%out;
  }
  if (!defined $sample_idx) {
    warn_once("missing_column:$path:sample");
  }
  if (!defined $otu_idx) {
    warn_once("missing_column:$path:otu_id_or_OTU_id");
  }
  my $has_assignment_columns = (defined $tax_idx || defined $family_idx || defined $genus_idx || defined $species_idx) ? 1 : 0;
  if (!$has_assignment_columns) {
    warn_once("missing_column:$path:taxid_or_taxon");
  }

  my %read_state;
  my %seen_sample_otu;
  my %seen_sample_assigned_read;
  my %seen_rep_otu;
  my %seen_rep_assigned;
  my %seen_track_unit_otu;
  my %seen_track_unit_assigned_read;
  $out{assignment_status} = $has_assignment_columns ? 'classified' : 'unknown';
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $read_idx > $#f;
    my $rid = normalize_read_id($f[$read_idx]);
    next if $rid eq '';
    my $is_assigned = 0;
    if ($has_assignment_columns) {
      $is_assigned = blast_row_is_assigned(
        \@f,
        {
          tax_idx => $tax_idx,
          family_idx => $family_idx,
          genus_idx => $genus_idx,
          species_idx => $species_idx,
        }
      );
    }
    my $sample_bucket = undef;
    if (defined $sample_idx && $sample_idx <= $#f) {
      $sample_bucket = classify_sample_bucket($f[$sample_idx]);
    }
    if (!exists $read_state{$rid}) {
      $read_state{$rid} = {
        assigned => ($is_assigned ? 1 : 0),
        sample_class => $sample_bucket,
      };
    } else {
      $read_state{$rid}{assigned} = 1 if $is_assigned;
      $read_state{$rid}{sample_class} = merge_sample_class($read_state{$rid}{sample_class}, $sample_bucket);
    }

    next unless defined $sample_idx && $sample_idx <= $#f;
    my $raw_sample = $f[$sample_idx];
    my $sid = ensure_sample_entry($sample_metrics, $label_to_id, $id_to_label, $raw_sample);
    my $entry = $sample_metrics->{$sid};
    my $rep   = ensure_replicate_sub_entry($entry, $raw_sample);
    my $sample_label = defined $entry ? ($entry->{label} // '') : '';
    my $marker_raw = (defined $marker_idx && $marker_idx <= $#f) ? $f[$marker_idx] : ((defined $otu_idx && $otu_idx <= $#f) ? $f[$otu_idx] : '');
    my $track_entry = defined $track_unit_metrics ? resolve_track_unit_metrics_entry($raw_sample, $sample_label, $marker_raw) : undef;
    if ($has_assignment_columns) {
      $entry->{reads_blast_assigned} = 0 unless defined $entry->{reads_blast_assigned};
      if (defined $track_entry) {
        $track_entry->{reads_blast_assigned} = 0 unless defined $track_entry->{reads_blast_assigned};
      }
    }

    if (defined $otu_idx && $otu_idx <= $#f) {
      my $otu = trim_text($f[$otu_idx]);
      if ($otu ne '' && uc($otu) ne 'NA') {
        $entry->{otu_active} = 0 unless defined $entry->{otu_active};
        my $otu_key = "$sid\t$otu";
        if (!$seen_sample_otu{$otu_key}) {
          $entry->{otu_active}++;
          $seen_sample_otu{$otu_key} = 1;
        }
        if (defined $rep) {
          $rep->{otu_active} = 0 unless defined $rep->{otu_active};
          my $rep_otu_key = "$sid\t$rep->{label}\t$otu";
          if (!$seen_rep_otu{$rep_otu_key}) {
            $rep->{otu_active}++;
            $seen_rep_otu{$rep_otu_key} = 1;
          }
        }
        if (defined $track_entry) {
          $track_entry->{otu_active} = 0 unless defined $track_entry->{otu_active};
          my $track_otu_key = $track_entry->{track_unit_id} . "\t" . $otu;
          if (!$seen_track_unit_otu{$track_otu_key}) {
            $track_entry->{otu_active}++;
            $seen_track_unit_otu{$track_otu_key} = 1;
          }
        }
      }
    }

    if ($has_assignment_columns && $is_assigned) {
      my $assign_key = "$sid\t$rid";
      if (!$seen_sample_assigned_read{$assign_key}) {
        $entry->{reads_blast_assigned}++;
        $seen_sample_assigned_read{$assign_key} = 1;
      }
      if (defined $rep) {
        $rep->{reads_blast_assigned} = 0 unless defined $rep->{reads_blast_assigned};
        my $rep_assign_key = "$sid\t$rep->{label}\t$rid";
        if (!$seen_rep_assigned{$rep_assign_key}) {
          $rep->{reads_blast_assigned}++;
          $seen_rep_assigned{$rep_assign_key} = 1;
        }
      }
      if (defined $track_entry) {
        my $track_assign_key = $track_entry->{track_unit_id} . "\t" . $rid;
        if (!$seen_track_unit_assigned_read{$track_assign_key}) {
          $track_entry->{reads_blast_assigned}++;
          $seen_track_unit_assigned_read{$track_assign_key} = 1;
        }
      }
    }
  }
  close $FH;

  my %seen_counts = (
    total => 0,
    adapter => 0,
    no_adapter => 0,
    unbucketed => 0,
  );
  my %assigned_counts = (
    total => 0,
    adapter => 0,
    no_adapter => 0,
  );
  for my $rid (keys %read_state) {
    my $st = $read_state{$rid};
    $seen_counts{total}++;
    if (defined $st->{sample_class} && exists $seen_counts{$st->{sample_class}}) {
      $seen_counts{$st->{sample_class}}++;
    } else {
      $seen_counts{unbucketed}++;
    }
    if ($has_assignment_columns && $st->{assigned}) {
      $assigned_counts{total}++;
      if (defined $st->{sample_class} && exists $assigned_counts{$st->{sample_class}}) {
        $assigned_counts{$st->{sample_class}}++;
      }
    }
  }
  $out{enabled} = 1;
  $out{seen}{total} = $seen_counts{total};
  $out{seen}{adapter} = $seen_counts{adapter};
  $out{seen}{no_adapter} = $seen_counts{no_adapter};
  $out{seen}{unbucketed} = $seen_counts{unbucketed};
  if ($has_assignment_columns) {
    my %unassigned_counts = (
      total => $seen_counts{total} - $assigned_counts{total},
      adapter => $seen_counts{adapter} - $assigned_counts{adapter},
      no_adapter => $seen_counts{no_adapter} - $assigned_counts{no_adapter},
    );
    for my $k (qw(total adapter no_adapter)) {
      $unassigned_counts{$k} = 0 if $unassigned_counts{$k} < 0;
    }
    $out{assigned}{total} = $assigned_counts{total};
    $out{assigned}{adapter} = $assigned_counts{adapter};
    $out{assigned}{no_adapter} = $assigned_counts{no_adapter};
    $out{unassigned}{total} = $unassigned_counts{total};
    $out{unassigned}{adapter} = $unassigned_counts{adapter};
    $out{unassigned}{no_adapter} = $unassigned_counts{no_adapter};
  }
  return \%out;
}

sub sum_consensus_round_reads {
  my ($path) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my $rows = $_parsed_rows{$path};
    return 0 unless @$rows;
    unless (exists $rows->[0]{reads_used_round}) {
      warn_once("missing_column:$path:reads_used_round");
      return undef;
    }
    my $sum = 0;
    my $has_numeric = 0;
    for my $row (@$rows) {
      my $v = trim_text($row->{reads_used_round});
      next if $v eq '' || uc($v) eq 'NA';
      if ($v =~ /^\d+$/) {
        $has_numeric = 1;
        $sum += 0 + $v;
      } else {
        warn_once("invalid_value:$path:reads_used_round:$v");
      }
    }
    unless ($has_numeric) {
      warn_once("missing_numeric_reads_used_round:$path");
      return undef;
    }
    return $sum;
  }
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return 0;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $reads_idx = header_index_fallback(\%idx, 'reads_used_round');
  if (!defined $reads_idx) {
    close $FH;
    warn_once("missing_column:$path:reads_used_round");
    return undef;
  }
  my $sum = 0;
  my $data_rows = 0;
  my $has_numeric = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    $data_rows++;
    my @f = split /\t/, $line, -1;
    next if $reads_idx > $#f;
    my $v = trim_text($f[$reads_idx]);
    next if $v eq '' || uc($v) eq 'NA';
    if ($v =~ /^\d+$/) {
      $has_numeric = 1;
      $sum += 0 + $v;
    } else {
      warn_once("invalid_value:$path:reads_used_round:$v");
    }
  }
  close $FH;
  return 0 if $data_rows == 0;
  if (!$has_numeric) {
    warn_once("missing_numeric_reads_used_round:$path");
    return undef;
  }
  return $sum;
}

# Convert a raw adapter label (e.g. GAG1_COI_1) to a short rep label (rep_1).
# Falls back to the raw label when no trailing _N suffix is present.
sub adapter_to_rep_label {
  my ($raw) = @_;
  return ($raw =~ /_(\d+)$/) ? "rep_$1" : $raw;
}

# Convert a { rep_label => count } hash to a sorted [ { label, count }, ... ] arrayref.
# Returns undef when fewer than 2 distinct labels (nothing to break down).
sub _rep_reads_array {
  my ($h) = @_;
  return undef unless defined $h && scalar(keys %$h) > 1;
  my @sorted = map { { label => $_, count => $h->{$_} } }
    sort {
      my ($na, $nb) = (0, 0);
      $na = 0 + $1 if $a =~ /_(\d+)$/;
      $nb = 0 + $1 if $b =~ /_(\d+)$/;
      $na <=> $nb || $a cmp $b;
    } keys %$h;
  return \@sorted;
}

# Build { OTU_id => { collapsed_sample => { rep_label => count } } } from
# otu_def rows (current round). Scoped per collapsed sample so that replicates
# from different samples never mix under the same rep_N label.
# Only meaningful in collapse mode; returns {} otherwise.
sub load_otu_replicate_reads {
  return {} unless $opt{identity_mode} eq 'collapse';
  return {} unless defined $opt{otu_def} && $opt{otu_def} ne '';
  my $rows = $_parsed_rows{$opt{otu_def}};
  return {} unless defined $rows && @$rows;
  my %map;  # { OTU_id => { collapsed_sample => { rep_label => count } } }
  for my $row (@$rows) {
    my $otu = trim_text($row->{OTU_id} // '');
    next if $otu eq '' || uc($otu) eq 'NA';
    my $raw = trim_text($row->{sample} // '');
    next if $raw eq '' || SampleLabel::is_no_adapter_label($raw);
    my $collapsed = resolve_reporting_identity($raw);
    next if $collapsed eq '';
    my $rep = adapter_to_rep_label($raw);
    $map{$otu}{$collapsed}{$rep}++;
  }
  # Drop sample entries with only one distinct replicate label — no breakdown to show.
  for my $otu (keys %map) {
    for my $smp (keys %{$map{$otu}}) {
      delete $map{$otu}{$smp} if scalar(keys %{$map{$otu}{$smp}}) <= 1;
    }
    delete $map{$otu} unless %{$map{$otu}};
  }
  return \%map;
}

sub load_otu_sizes_round {
  my ($path) = @_;
  my %sizes;
  return \%sizes if !defined $path || $path eq '' || !-e $path || !-s $path;
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return \%sizes;
  }
  my $header = <$FH>;
  if (!defined $header) {
    close $FH;
    return \%sizes;
  }
  chomp $header;
  my @cols = split /\t/, $header, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $size_idx = header_index_fallback(\%idx, 'size', 'reads', 'read_count');
  if (!defined $otu_idx || !defined $size_idx) {
    warn_once("missing_column:$path:otu_id_or_size");
    close $FH;
    return \%sizes;
  }
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $header);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f || $size_idx > $#f;
    my $otu = trim_text($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';
    my $size = trim_text($f[$size_idx]);
    next unless $size =~ /^\d+$/;
    $sizes{$otu} = 0 + $size;
  }
  close $FH;
  return \%sizes;
}

sub collect_otu_assignments_by_level {
  my ($blast_otu_path, $otu_sizes_path, $spec_interest_ref, $spec_interest_enabled, $lock_frozen_ref, $thresholds_by_level) = @_;
  my %by_level = (species => [], genus => [], family => []);
  my %otu_rep_reads;           # { otu => { collapsed_sample => { rep_label => count } } }
  my %sample_marker_rep_reads; # { collapsed_sample => { marker => { rep_label => count } } }
  return (\%by_level, \%otu_rep_reads, \%sample_marker_rep_reads) if !defined $blast_otu_path || $blast_otu_path eq '' || !-e $blast_otu_path || !-s $blast_otu_path;

  my $size_map = load_otu_sizes_round($otu_sizes_path);
  my %counts_fallback;
  my %best;
  my %otu_seen;
  my %raw_otu_sample_rep;    # { otu => { raw_sample => count } } — for OTU-level rep breakdown
  my %raw_sample_marker_rep; # { raw_sample => { marker => count } } — for consensus-level rep breakdown

  my $FH = open_cached_text_handle($blast_otu_path);
  if (!defined $FH) {
    warn_once("open_failed:$blast_otu_path");
    return (\%by_level, \%otu_rep_reads, \%sample_marker_rep_reads);
  }
  my $header = <$FH>;
  if (!defined $header) {
    close $FH;
    return (\%by_level, \%otu_rep_reads, \%sample_marker_rep_reads);
  }
  chomp $header;
  my @cols = split /\t/, $header, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $sample_idx = header_index_fallback(\%idx, 'sample');
  my $taxid_idx = header_index_fallback(\%idx, 'otu_taxid', 'taxid');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $hit_idx = header_index_fallback(\%idx, 'hit_id', 'blast_hit');
  my $perc_idx = header_index_fallback(\%idx, 'perc_id');
  my $aln_idx = header_index_fallback(\%idx, 'aln_length');
  my $family_idx = header_index_fallback(\%idx, 'otu_family');
  my $genus_idx = header_index_fallback(\%idx, 'otu_genus');
  my $species_idx = header_index_fallback(\%idx, 'otu_species');
  my $kingdom_idx = header_index_fallback(\%idx, 'otu_kingdom');
  my $read_idx = header_index_fallback(\%idx, 'read_id');

  if (!defined $otu_idx) {
    warn_once("missing_column:$blast_otu_path:otu_id_or_OTU_id");
    close $FH;
    return (\%by_level, \%otu_rep_reads, \%sample_marker_rep_reads);
  }

  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $header);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = trim_text($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';
    my $_raw_sample_col = (defined $sample_idx && $sample_idx <= $#f) ? trim_text($f[$sample_idx]) : '';
    my $sample = resolve_reporting_identity($_raw_sample_col);
    my $marker = (defined $marker_idx && $marker_idx <= $#f) ? marker_from_token($f[$marker_idx]) : marker_from_token($otu);
    # Accumulate per-OTU-per-raw-sample and per-marker-per-raw-sample counts
    # for replicate breakdown (collapse mode only).
    if ($opt{identity_mode} eq 'collapse'
        && $_raw_sample_col ne ''
        && !SampleLabel::is_no_adapter_label($_raw_sample_col)) {
      $raw_otu_sample_rep{$otu}{$_raw_sample_col}++;
      my $_mk = $marker ne '' ? $marker : 'OTHER';
      $raw_sample_marker_rep{$_raw_sample_col}{$_mk}++;
    }
    my $kingdom = (defined $kingdom_idx && $kingdom_idx <= $#f) ? trim_text($f[$kingdom_idx]) : '';
    next unless is_kingdom_consistent($kingdom, $marker, $CONFIGURED_TARGET_TAX_MAP);
    my $key = join("\t", $sample, $marker, $otu);

    if (!defined $size_map->{$otu}) {
      if (defined $read_idx && $read_idx <= $#f) {
        my $rid = trim_text($f[$read_idx]);
        $rid =~ s/\|.*$//;
        if ($rid ne '') {
          $counts_fallback{$key}{$rid} = 1;
        }
      }
    }

    my $taxid = (defined $taxid_idx && $taxid_idx <= $#f) ? trim_text($f[$taxid_idx]) : '';
    my $hit = (defined $hit_idx && $hit_idx <= $#f) ? trim_text($f[$hit_idx]) : '';
    my $perc = (defined $perc_idx && $perc_idx <= $#f) ? trim_text($f[$perc_idx]) : '';
    my $aln = (defined $aln_idx && $aln_idx <= $#f) ? trim_text($f[$aln_idx]) : '';
    my $family = (defined $family_idx && $family_idx <= $#f) ? normalize_taxon($f[$family_idx]) : undef;
    my $genus = (defined $genus_idx && $genus_idx <= $#f) ? normalize_taxon($f[$genus_idx]) : undef;
    my $species = (defined $species_idx && $species_idx <= $#f) ? normalize_taxon($f[$species_idx]) : undef;

    my $_perc_raw = ($perc ne '' ? $perc + 0 : undef);
    my $_aln_raw  = ($aln  ne '' ? $aln  + 0 : undef);
    # Sanity-check: perc_id must be in [0,100], aln_length must be >= 1.
    # Old pipeline versions swapped aln_length (stored evalue) and perc_id (stored length);
    # discard rows where values fall outside valid ranges so they don't corrupt aggregates.
    my $_perc_valid = defined $_perc_raw && $_perc_raw >= 0 && $_perc_raw <= 100;
    my $_aln_valid  = defined $_aln_raw  && $_aln_raw  >= 1;
    my $row = {
      otu_id => $otu,
      sample => $sample,
      marker => $marker,
      taxid => ($taxid ne '' ? $taxid : undef),
      blast_hit => ($hit ne '' ? $hit : undef),
      perc_id => ($_perc_valid ? $_perc_raw : undef),
      aln_length => ($_aln_valid  ? $_aln_raw  : undef),
      family => $family,
      genus => $genus,
      species => $species,
    };

    if (!exists $best{$key}) {
      $best{$key} = $row;
    } else {
      my $prev = $best{$key};
      my $prev_perc = defined $prev->{perc_id} ? $prev->{perc_id} : -1;
      my $new_perc = defined $row->{perc_id} ? $row->{perc_id} : -1;
      if ($new_perc > $prev_perc) {
        $best{$key} = $row;
      }
    }
    $otu_seen{$key} = 1;
  }
  close $FH;

  # Convert raw-sample counts to { otu => { collapsed_sample => { rep_label => count } } }.
  for my $otu (keys %raw_otu_sample_rep) {
    for my $raw (keys %{$raw_otu_sample_rep{$otu}}) {
      my $collapsed = resolve_reporting_identity($raw);
      next if $collapsed eq '';
      my $rep = adapter_to_rep_label($raw);
      $otu_rep_reads{$otu}{$collapsed}{$rep} += $raw_otu_sample_rep{$otu}{$raw};
    }
  }
  # Prune entries with ≤1 distinct replicate label per sample — no breakdown to show.
  for my $otu (keys %otu_rep_reads) {
    for my $smp (keys %{$otu_rep_reads{$otu}}) {
      delete $otu_rep_reads{$otu}{$smp}
        if scalar(keys %{$otu_rep_reads{$otu}{$smp}}) <= 1;
    }
    delete $otu_rep_reads{$otu} unless %{$otu_rep_reads{$otu}};
  }

  # Build { collapsed_sample => { marker => { rep_label => count } } } for consensus-level breakdown.
  for my $raw (keys %raw_sample_marker_rep) {
    my $collapsed = resolve_reporting_identity($raw);
    next if $collapsed eq '';
    my $rep = adapter_to_rep_label($raw);
    for my $mk (keys %{$raw_sample_marker_rep{$raw}}) {
      $sample_marker_rep_reads{$collapsed}{$mk}{$rep} += $raw_sample_marker_rep{$raw}{$mk};
    }
  }
  # Prune markers where ≤1 distinct rep label exists — no breakdown to show.
  for my $smp (keys %sample_marker_rep_reads) {
    for my $mk (keys %{$sample_marker_rep_reads{$smp}}) {
      delete $sample_marker_rep_reads{$smp}{$mk}
        if scalar(keys %{$sample_marker_rep_reads{$smp}{$mk}}) <= 1;
    }
    delete $sample_marker_rep_reads{$smp} unless %{$sample_marker_rep_reads{$smp}};
  }

  my @rows;
  for my $key (keys %best) {
    my $row = $best{$key};
    my $otu = $row->{otu_id};
    my $reads = defined $size_map->{$otu}
      ? $size_map->{$otu}
      : (exists $counts_fallback{$key} ? scalar(keys %{$counts_fallback{$key}}) : undef);
    $row->{reads} = defined $reads ? $reads : undef;
    push @rows, $row;
  }

  my %levels = (
    species => 'species',
    genus => 'genus',
    family => 'family',
  );
  for my $level (keys %levels) {
    my $field = $levels{$level};
    my %groups;
    for my $row (@rows) {
      next if !otu_row_qualifies_for_level($row, $level, $thresholds_by_level);
      my $taxon = $row->{$field};
      next if !defined $taxon;
      my $sample = $row->{sample} // '';
      my $marker = $row->{marker} // '';
      my $gkey = join("\t", $taxon, $sample, $marker);
      my $g = $groups{$gkey} ||= {
        taxon => $taxon,
        sample => $sample,
        marker => $marker,
        family => undef,
        genus => undef,
        species => undef,
        otu_ids => {},
        reads_total => 0,
        reads_any => 0,
        frozen_reads_total => 0,
        frozen_reads_any => 0,
        perc_min => undef,
        perc_max => undef,
        aln_min => undef,
        aln_max => undef,
      };
      $g->{family} = $row->{family} if defined $row->{family} && (!defined $g->{family} || $g->{family} eq '');
      $g->{genus} = $row->{genus} if defined $row->{genus} && (!defined $g->{genus} || $g->{genus} eq '');
      $g->{species} = $row->{species} if defined $row->{species} && (!defined $g->{species} || $g->{species} eq '');
      $g->{otu_ids}{$row->{otu_id}} = 1 if defined $row->{otu_id};
      if (defined $row->{otu_id} && exists $otu_rep_reads{$row->{otu_id}}) {
        my $smp = $row->{sample} // '';
        if (exists $otu_rep_reads{$row->{otu_id}}{$smp}) {
          my $rmap = $otu_rep_reads{$row->{otu_id}}{$smp};
          $g->{rep_reads}{$_} += $rmap->{$_} for keys %$rmap;
        }
      }
      if (defined $row->{reads}) {
        $g->{reads_total} += $row->{reads};
        $g->{reads_any} = 1;
        my $lock_key = normalize_lock_otu_key($row->{otu_id});
        if ($lock_key ne '' && defined $lock_frozen_ref && exists $lock_frozen_ref->{$lock_key}) {
          $g->{frozen_reads_total} += $row->{reads};
          $g->{frozen_reads_any} = 1;
        }
      }
      if (defined $row->{perc_id}) {
        $g->{perc_min} = $row->{perc_id} if !defined $g->{perc_min} || $row->{perc_id} < $g->{perc_min};
        $g->{perc_max} = $row->{perc_id} if !defined $g->{perc_max} || $row->{perc_id} > $g->{perc_max};
      }
      if (defined $row->{aln_length}) {
        $g->{aln_min} = $row->{aln_length} if !defined $g->{aln_min} || $row->{aln_length} < $g->{aln_min};
        $g->{aln_max} = $row->{aln_length} if !defined $g->{aln_max} || $row->{aln_length} > $g->{aln_max};
      }
    }
    my @agg;
    for my $gkey (keys %groups) {
      my $g = $groups{$gkey};
      my $otu_count = scalar keys %{$g->{otu_ids}};
      my $frozen_otu_n = 0;
      for my $otu_id (keys %{$g->{otu_ids}}) {
        my $key = normalize_lock_otu_key($otu_id);
        next if $key eq '';
        $frozen_otu_n++ if defined $lock_frozen_ref && exists $lock_frozen_ref->{$key};
      }
      my $_otu_rep_arr = _rep_reads_array($g->{rep_reads});
      my $row_out = {
        taxon => $g->{taxon},
        sample => $g->{sample},
        marker => $g->{marker},
        family => ($g->{family} // ($level eq 'family' ? $g->{taxon} : undef)),
        genus => ($g->{genus} // ($level eq 'genus' ? $g->{taxon} : undef)),
        species => ($g->{species} // ($level eq 'species' ? $g->{taxon} : undef)),
        otu_count => $otu_count,
        frozen_otu_count => $frozen_otu_n,
        frozen_otu_reads_total => ($g->{reads_any} ? $g->{frozen_reads_total} : undef),
        reads_total => ($g->{reads_any} ? $g->{reads_total} : undef),
        perc_id_min => $g->{perc_min},
        perc_id_max => $g->{perc_max},
        aln_length_min => $g->{aln_min},
        aln_length_max => $g->{aln_max},
        (defined $_otu_rep_arr ? (replicate_reads => $_otu_rep_arr) : ()),
      };
      if ($level eq 'species' && $spec_interest_enabled) {
        $row_out->{species_interest} = (defined $spec_interest_ref && $spec_interest_ref->{$g->{taxon}})
          ? JSON::PP::true
          : JSON::PP::false;
      }
      if ($opt{identity_mode} eq 'track') {
        my $track_entry = resolve_track_unit_metrics_entry($g->{sample}, $g->{sample}, $g->{marker});
        add_track_identity_fields($row_out, $track_entry);
      }
      push @agg, $row_out;
    }
    @agg = sort {
      (defined($b->{reads_total}) ? $b->{reads_total} : -1) <=> (defined($a->{reads_total}) ? $a->{reads_total} : -1)
        || ($b->{otu_count} // -1) <=> ($a->{otu_count} // -1)
        || ($a->{taxon} // '') cmp ($b->{taxon} // '')
        || ($a->{sample} // '') cmp ($b->{sample} // '')
        || ($a->{marker} // '') cmp ($b->{marker} // '')
    } @agg;
    if (@agg > 200) {
      @agg = @agg[0..199];
    }
    $by_level{$level} = \@agg;
  }

  $by_level{species_interest_enabled} = $spec_interest_enabled ? JSON::PP::true : JSON::PP::false;
  return (\%by_level, \%otu_rep_reads, \%sample_marker_rep_reads);
}

sub load_id_set {
  my ($path) = @_;
  my %set;
  return \%set unless defined $path && $path ne '' && -s $path;
  my $fh = open_cached_text_handle($path);
  return \%set if !defined $fh;
  while (my $line = <$fh>) {
    chomp $line;
    $line = trim_text($line);
    next if $line eq '';
    $set{$line} = 1;
  }
  close $fh;
  return \%set;
}

sub collect_consensus_assignments_by_level {
  my ($blast_consensus_path, $spec_interest_ref, $spec_interest_enabled, $consolidated_cons_ref, $identity_mode, $sample_marker_rep_reads_ref) = @_;
  $identity_mode //= 'collapse';
  my %by_level = (species => [], genus => [], family => []);
  return \%by_level if !defined $blast_consensus_path || $blast_consensus_path eq '' || !-e $blast_consensus_path || !-s $blast_consensus_path;

  my $FH = open_cached_text_handle($blast_consensus_path);
  if (!defined $FH) {
    warn_once("open_failed:$blast_consensus_path");
    return \%by_level;
  }
  my $header = <$FH>;
  if (!defined $header) {
    close $FH;
    return \%by_level;
  }
  chomp $header;
  my @cols = split /\t/, $header, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $cons_idx = header_index_fallback(\%idx, 'consensus_id');
  my $sample_idx = header_index_fallback(\%idx, 'sample');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $reads_idx = header_index_fallback(\%idx, 'number_of_reads', 'reads');
  my $perc_idx = header_index_fallback(\%idx, 'perc_id');
  my $aln_idx = header_index_fallback(\%idx, 'aln_length');
  my $family_idx = header_index_fallback(\%idx, 'consensus_family', 'otu_family');
  my $genus_idx = header_index_fallback(\%idx, 'consensus_genus', 'otu_genus');
  my $species_idx = header_index_fallback(\%idx, 'consensus_species', 'otu_species');
  my $kingdom_idx = header_index_fallback(\%idx, 'consensus_kingdom', 'otu_kingdom');

  if (!defined $cons_idx) {
    warn_once("missing_column:$blast_consensus_path:consensus_id");
    close $FH;
    return \%by_level;
  }

  my @rows;
  my %seen_cons;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $header);
    my @f = split /\t/, $line, -1;
    next if $cons_idx > $#f;
    my $cons_id = trim_text($f[$cons_idx]);
    next if $cons_id eq '' || $seen_cons{$cons_id}++;
    my $raw_sample = (defined $sample_idx && $sample_idx <= $#f) ? trim_text($f[$sample_idx]) : '';
    my $sample = resolve_reporting_identity($raw_sample);
    my $marker = (defined $marker_idx && $marker_idx <= $#f) ? marker_from_token($f[$marker_idx]) : marker_from_token($cons_id);
    my $kingdom = (defined $kingdom_idx && $kingdom_idx <= $#f) ? trim_text($f[$kingdom_idx]) : '';
    next unless is_kingdom_consistent($kingdom, $marker, $CONFIGURED_TARGET_TAX_MAP);
    my $reads = (defined $reads_idx && $reads_idx <= $#f) ? trim_text($f[$reads_idx]) : '';
    $reads = ($reads =~ /^\d+$/) ? 0 + $reads : undef;
    my $_perc_raw2 = do { my $v = (defined $perc_idx && $perc_idx <= $#f) ? trim_text($f[$perc_idx]) : ''; $v ne '' ? $v + 0 : undef };
    my $_aln_raw2  = do { my $v = (defined $aln_idx  && $aln_idx  <= $#f) ? trim_text($f[$aln_idx])  : ''; $v ne '' ? $v + 0 : undef };
    my $perc = (defined $_perc_raw2 && $_perc_raw2 >= 0 && $_perc_raw2 <= 100) ? $_perc_raw2 : undef;
    my $aln  = (defined $_aln_raw2  && $_aln_raw2  >= 1) ? $_aln_raw2  : undef;
    my $family = (defined $family_idx && $family_idx <= $#f) ? normalize_taxon($f[$family_idx]) : undef;
    my $genus = (defined $genus_idx && $genus_idx <= $#f) ? normalize_taxon($f[$genus_idx]) : undef;
    my $species = (defined $species_idx && $species_idx <= $#f) ? normalize_taxon($f[$species_idx]) : undef;
    push @rows, {
      consensus_id => $cons_id,
      raw_sample => $raw_sample,
      sample => $sample,
      marker => $marker,
      reads => $reads,
      perc_id => $perc,
      aln_length => $aln,
      family => $family,
      genus => $genus,
      species => $species,
    };
  }
  close $FH;

  my %levels = (
    species => 'species',
    genus => 'genus',
    family => 'family',
  );
  for my $level (keys %levels) {
    my $field = $levels{$level};
    my %groups;
    for my $row (@rows) {
      my $taxon = $row->{$field};
      next if !defined $taxon;
      my $sample = $row->{sample} // '';
      my $marker = $row->{marker} // '';
      my $gkey = join("\t", $taxon, $sample, $marker);
      my $g = $groups{$gkey} ||= {
        taxon => $taxon,
        sample => $sample,
        marker => $marker,
        family => undef,
        genus => undef,
        species => undef,
        cons_ids => {},
        reads_total => 0,
        reads_any => 0,
        consolidated_reads_total => 0,
        consolidated_reads_any => 0,
        perc_min => undef,
        perc_max => undef,
        aln_min => undef,
        aln_max => undef,
      };
      $g->{family} = $row->{family} if defined $row->{family} && (!defined $g->{family} || $g->{family} eq '');
      $g->{genus} = $row->{genus} if defined $row->{genus} && (!defined $g->{genus} || $g->{genus} eq '');
      $g->{species} = $row->{species} if defined $row->{species} && (!defined $g->{species} || $g->{species} eq '');
      $g->{cons_ids}{$row->{consensus_id}} = 1 if defined $row->{consensus_id};
      if (defined $row->{reads}) {
        $g->{reads_total} += $row->{reads};
        $g->{reads_any} = 1;
        if (defined $row->{consensus_id} && defined $consolidated_cons_ref && exists $consolidated_cons_ref->{$row->{consensus_id}}) {
          $g->{consolidated_reads_total} += $row->{reads};
          $g->{consolidated_reads_any} = 1;
        }
      }
      if (defined $row->{perc_id}) {
        $g->{perc_min} = $row->{perc_id} if !defined $g->{perc_min} || $row->{perc_id} < $g->{perc_min};
        $g->{perc_max} = $row->{perc_id} if !defined $g->{perc_max} || $row->{perc_id} > $g->{perc_max};
      }
      if (defined $row->{aln_length}) {
        $g->{aln_min} = $row->{aln_length} if !defined $g->{aln_min} || $row->{aln_length} < $g->{aln_min};
        $g->{aln_max} = $row->{aln_length} if !defined $g->{aln_max} || $row->{aln_length} > $g->{aln_max};
      }
    }
    my @agg;
    for my $gkey (keys %groups) {
      my $g = $groups{$gkey};
      my $cons_count = scalar keys %{$g->{cons_ids}};
      my $consolidated_cons_n = 0;
      for my $cid (keys %{$g->{cons_ids}}) {
        $consolidated_cons_n++ if defined $consolidated_cons_ref && exists $consolidated_cons_ref->{$cid};
      }
      # Look up per-replicate read counts from the OTU-blast-derived map (keyed by collapsed sample + marker).
      # Consensus sequences are built from collapsed reads, so we use the OTU-level replicate distribution
      # for the same (sample, marker) as a proxy.
      my $_cons_rep_map;
      if ($identity_mode eq 'collapse' && defined $sample_marker_rep_reads_ref) {
        my $_mk = $g->{marker} // 'OTHER';
        $_cons_rep_map = $sample_marker_rep_reads_ref->{$g->{sample}}{$_mk};
      }
      my $_cons_rep_arr = _rep_reads_array($_cons_rep_map);
      my $row_out = {
        taxon => $g->{taxon},
        sample => $g->{sample},
        marker => $g->{marker},
        family => ($g->{family} // ($level eq 'family' ? $g->{taxon} : undef)),
        genus => ($g->{genus} // ($level eq 'genus' ? $g->{taxon} : undef)),
        species => ($g->{species} // ($level eq 'species' ? $g->{taxon} : undef)),
        consensus_count => $cons_count,
        consolidated_consensus_count => $consolidated_cons_n,
        consolidated_consensus_reads_total => ($g->{reads_any} ? $g->{consolidated_reads_total} : undef),
        reads_total => ($g->{reads_any} ? $g->{reads_total} : undef),
        perc_id_min => $g->{perc_min},
        perc_id_max => $g->{perc_max},
        aln_length_min => $g->{aln_min},
        aln_length_max => $g->{aln_max},
        (defined $_cons_rep_arr ? (replicate_reads => $_cons_rep_arr) : ()),
      };
      if ($level eq 'species' && $spec_interest_enabled) {
        $row_out->{species_interest} = (defined $spec_interest_ref && $spec_interest_ref->{$g->{taxon}})
          ? JSON::PP::true
          : JSON::PP::false;
      }
      if ($opt{identity_mode} eq 'track') {
        my $track_entry = resolve_track_unit_metrics_entry($g->{sample}, $g->{sample}, $g->{marker});
        add_track_identity_fields($row_out, $track_entry);
      }
      push @agg, $row_out;
    }
    @agg = sort {
      (defined($b->{reads_total}) ? $b->{reads_total} : -1) <=> (defined($a->{reads_total}) ? $a->{reads_total} : -1)
        || ($b->{consensus_count} // -1) <=> ($a->{consensus_count} // -1)
        || ($a->{taxon} // '') cmp ($b->{taxon} // '')
        || ($a->{sample} // '') cmp ($b->{sample} // '')
        || ($a->{marker} // '') cmp ($b->{marker} // '')
    } @agg;
    if (@agg > 200) {
      @agg = @agg[0..199];
    }
    $by_level{$level} = \@agg;
  }

  $by_level{species_interest_enabled} = $spec_interest_enabled ? JSON::PP::true : JSON::PP::false;
  return \%by_level;
}

sub consensus_emitted_by_marker_from_provenance {
  my ($path) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my %seen;
    my %counts;
    for my $row (@{$_parsed_rows{$path}}) {
      my $cons_id = trim_text($row->{consensus_id} // '');
      next if $cons_id eq '';
      next if $seen{$cons_id}++;
      my $marker = marker_from_token($row->{otu_key} // '');
      $marker = marker_from_token($cons_id) unless defined $marker;
      $marker = 'OTHER' unless defined $marker;
      $counts{$marker}++;
    }
    return \%counts;
  }
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_key');
  my $cons_idx = header_index_fallback(\%idx, 'consensus_id');
  if (!defined $cons_idx) {
    close $FH;
    warn_once("missing_column:$path:consensus_id");
    return undef;
  }
  my %seen;
  my %counts;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $cons_idx > $#f;
    my $cons_id = trim_text($f[$cons_idx]);
    next if $cons_id eq '';
    next if $seen{$cons_id}++;
    my $marker = undef;
    if (defined $otu_idx && $otu_idx <= $#f) {
      $marker = marker_from_token($f[$otu_idx]);
    }
    if (!defined $marker) {
      $marker = marker_from_token($cons_id);
    }
    $marker = 'OTHER' if !defined $marker;
    $counts{$marker}++;
  }
  close $FH;
  return \%counts;
}

sub consensus_assigned_by_marker_from_report {
  my ($path) = @_;
  return undef unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $cons_idx = header_index_fallback(\%idx, 'consensus_id');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $tax_idx = header_index_fallback(\%idx, 'taxid');
  if (!defined $cons_idx || !defined $tax_idx) {
    close $FH;
    warn_once("missing_column:$path:consensus_id_or_taxid");
    return undef;
  }
  my %seen;
  my %counts;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $cons_idx > $#f || $tax_idx > $#f;
    my $cons_id = trim_text($f[$cons_idx]);
    next if $cons_id eq '';
    my $taxid = trim_text($f[$tax_idx]);
    next if !is_numeric_taxid($taxid) || $taxid <= 0;
    next if $seen{$cons_id}++;
    my $marker = undef;
    if (defined $marker_idx && $marker_idx <= $#f) {
      $marker = marker_from_token($f[$marker_idx]);
    }
    if (!defined $marker) {
      $marker = marker_from_token($cons_id);
    }
    $marker = 'OTHER' if !defined $marker;
    $counts{$marker}++;
  }
  close $FH;
  return \%counts;
}

sub otu_active_by_marker_from_otu_def {
  my ($path, $filter_set) = @_;
  return undef unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'OTU_id', 'otu_id');
  if (!defined $otu_idx) {
    close $FH;
    warn_once("missing_column:$path:otu_id");
    return undef;
  }
  my %seen;
  my %counts;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = trim_text($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';
    if (defined $filter_set && ref($filter_set) eq 'HASH') {
      my $norm = normalize_otu_key($otu);
      next if $norm eq '' || !exists $filter_set->{$norm};
    }
    next if $seen{$otu}++;
    my $marker = marker_from_token($otu);
    $marker = 'OTHER' if !defined $marker;
    $counts{$marker}++;
  }
  close $FH;
  return \%counts;
}

sub otu_assigned_by_marker_from_blast_otu {
  my ($path, $filter_set) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my %seen;
    my %counts;
    for my $row (@{$_parsed_rows{$path}}) {
      my $otu = trim_text($row->{otu_id} // $row->{OTU_id} // '');
      next if $otu eq '' || uc($otu) eq 'NA';
      if (defined $filter_set && ref($filter_set) eq 'HASH') {
        my $norm = normalize_otu_key($otu);
        next if $norm eq '' || !exists $filter_set->{$norm};
      }
      my $taxid = trim_text($row->{taxid} // '');
      next if !is_numeric_taxid($taxid) || $taxid <= 0;
      next if $seen{$otu}++;
      my $marker = marker_from_token($otu);
      if (!defined $marker) {
        my $bchom = $row->{barcode_by_homology} // '';
        $marker = marker_from_token($bchom) if $bchom ne '';
      }
      $marker = 'OTHER' unless defined $marker;
      $counts{$marker}++;
    }
    return \%counts;
  }
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $tax_idx = header_index_fallback(\%idx, 'taxid');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  if (!defined $otu_idx || !defined $tax_idx) {
    close $FH;
    warn_once("missing_column:$path:otu_id_or_taxid");
    return undef;
  }
  my %seen;
  my %counts;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f || $tax_idx > $#f;
    my $otu = trim_text($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';
    if (defined $filter_set && ref($filter_set) eq 'HASH') {
      my $norm = normalize_otu_key($otu);
      next if $norm eq '' || !exists $filter_set->{$norm};
    }
    my $taxid = trim_text($f[$tax_idx]);
    next if !is_numeric_taxid($taxid) || $taxid <= 0;
    next if $seen{$otu}++;
    my $marker = marker_from_token($otu);
    if (!defined $marker && defined $marker_idx && $marker_idx <= $#f) {
      $marker = marker_from_token($f[$marker_idx]);
    }
    $marker = 'OTHER' if !defined $marker;
    $counts{$marker}++;
  }
  close $FH;
  return \%counts;
}

sub otu_counts_by_marker_from_set {
  my ($set) = @_;
  return undef unless defined $set && ref($set) eq 'HASH';
  my %counts;
  for my $otu (keys %{$set}) {
    my $otu_key = normalize_otu_key($otu);
    next if $otu_key eq '';
    my $marker = marker_from_token($otu_key);
    $marker = 'OTHER' if !defined $marker;
    $counts{$marker}++;
  }
  return \%counts;
}

sub kv_value {
  my ($path, $key, $quiet_missing) = @_;
  return undef unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path") unless $quiet_missing;
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $val;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($k, $v) = split /\t/, $line, 2;
    next unless defined $k && defined $v;
    if ($k eq $key) {
      $val = $v;
      last;
    }
  }
  close $FH;
  return $val;
}

sub to_nonneg_int {
  my ($v) = @_;
  return undef unless defined $v;
  return undef unless $v =~ /^-?[0-9]+$/;
  my $n = 0 + $v;
  return ($n < 0) ? 0 : $n;
}

sub normalize_otu_key {
  my ($otu) = @_;
  $otu = trim_text($otu);
  return '' if !defined $otu || $otu eq '';
  return '' if uc($otu) eq 'NA';
  return $otu;
}

sub normalize_lock_otu_key {
  my ($otu) = @_;
  $otu = normalize_otu_key($otu);
  return '' if $otu eq '';
  # Lock summaries can append sample suffixes (e.g. OTUB_10-COI-no_adapter_1).
  # Normalize only this known producer format to avoid collapsing valid OTU ids.
  my $marker_pat = configured_marker_regex_fragment();
  if ($otu =~ /^(OTUB_[^-]+-$marker_pat)-.+$/) {
    return $1;
  }
  return $otu;
}

sub load_blast_otu_flags {
  my ($path) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my %flags;
    for my $row (@{$_parsed_rows{$path}}) {
      my $otu = normalize_otu_key($row->{otu_id} // $row->{OTU_id} // '');
      next if $otu eq '';
      my $marker_raw = $row->{barcode_by_homology} // '';
      my $marker = marker_from_token($marker_raw);
      $marker = marker_from_token($otu) if !defined $marker || $marker eq '';
      $marker = 'OTHER' if !defined $marker || $marker eq '';
      my $taxid = trim_text($row->{taxid} // '');
      my $taxid_ok = ($taxid ne '' && uc($taxid) ne 'NA') ? 1 : 0;
      my $family = normalize_taxon($row->{otu_family} // '');
      my $genus  = normalize_taxon($row->{otu_genus}  // '');
      my $species = normalize_taxon($row->{otu_species} // '');
      my $text_ok = 0;
      if (defined $family && $family ne '' && !is_unassigned_taxon($family)) {
        $text_ok = 1;
      } elsif (defined $genus && $genus ne '' && !is_unassigned_taxon($genus)) {
        $text_ok = 1;
      } elsif (defined $species && $species ne '' && !is_unassigned_taxon($species)) {
        $text_ok = 1;
      }
      my $ref = $flags{$otu};
      if (!defined $ref) {
        $flags{$otu} = { marker => $marker, taxid => $taxid_ok, text => $text_ok };
      } else {
        $ref->{taxid} ||= $taxid_ok;
        $ref->{text}  ||= $text_ok;
        if (!defined $ref->{marker} || $ref->{marker} eq '') {
          $ref->{marker} = $marker;
        }
      }
    }
    return \%flags;
  }
  return undef if !-e $path || !-s $path;
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    my $k = trim_text($cols[$i]);
    $idx{lc($k)} = $i if $k ne '';
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $taxid_idx = header_index_fallback(\%idx, 'taxid');
  my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
  my $family_idx = header_index_fallback(\%idx, 'otu_family');
  my $genus_idx = header_index_fallback(\%idx, 'otu_genus');
  my $species_idx = header_index_fallback(\%idx, 'otu_species');
  if (!defined $otu_idx) {
    close $FH;
    warn_once("missing_column:$path:otu_id_or_OTU_id");
    return undef;
  }
  my %flags;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = normalize_otu_key($f[$otu_idx]);
    next if $otu eq '';
    my $marker_raw = (defined $marker_idx && $marker_idx <= $#f) ? $f[$marker_idx] : '';
    my $marker = marker_from_token($marker_raw);
    $marker = marker_from_token($otu) if !defined $marker || $marker eq '';
    $marker = 'OTHER' if !defined $marker || $marker eq '';
    my $taxid = (defined $taxid_idx && $taxid_idx <= $#f) ? trim_text($f[$taxid_idx]) : '';
    my $taxid_ok = ($taxid ne '' && uc($taxid) ne 'NA') ? 1 : 0;
    my $family = (defined $family_idx && $family_idx <= $#f) ? normalize_taxon($f[$family_idx]) : undef;
    my $genus = (defined $genus_idx && $genus_idx <= $#f) ? normalize_taxon($f[$genus_idx]) : undef;
    my $species = (defined $species_idx && $species_idx <= $#f) ? normalize_taxon($f[$species_idx]) : undef;
    my $text_ok = 0;
    if (defined $family && $family ne '' && !is_unassigned_taxon($family)) {
      $text_ok = 1;
    } elsif (defined $genus && $genus ne '' && !is_unassigned_taxon($genus)) {
      $text_ok = 1;
    } elsif (defined $species && $species ne '' && !is_unassigned_taxon($species)) {
      $text_ok = 1;
    }
    my $ref = $flags{$otu};
    if (!defined $ref) {
      $flags{$otu} = { marker => $marker, taxid => $taxid_ok, text => $text_ok };
    } else {
      $ref->{taxid} ||= $taxid_ok;
      $ref->{text} ||= $text_ok;
      if (!defined $ref->{marker} || $ref->{marker} eq '') {
        $ref->{marker} = $marker;
      }
    }
  }
  close $FH;
  return \%flags;
}

sub otu_lock_sets {
  my ($path) = @_;
  my %out = (
    counts => {
      consolidated => undef,
      frozen_not_consolidated => undef,
      active_not_frozen => undef,
    },
    sets => {
      consolidated => {},
      frozen_not_consolidated => {},
      active_not_frozen => {},
    },
  );
  return \%out unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return \%out unless defined $_parsed_rows{$path};
    my $rows = $_parsed_rows{$path};
    unless (@$rows) {
      $out{counts}{consolidated}           = 0;
      $out{counts}{frozen_not_consolidated} = 0;
      $out{counts}{active_not_frozen}       = 0;
      return \%out;
    }
    my $cons_col = (exists $rows->[0]{effective_consolidated}) ? 'effective_consolidated'
                 : (exists $rows->[0]{should_consolidate})     ? 'should_consolidate'
                 : '';
    my $otu_col  = (exists $rows->[0]{otu_key})  ? 'otu_key'
                 : (exists $rows->[0]{OTU_id})   ? 'OTU_id'
                 : (exists $rows->[0]{otu_id})   ? 'otu_id'
                 : '';
    if ($otu_col eq '' || $cons_col eq '') {
      warn_once("missing_column:$path:otu_key_or_effective_consolidated");
      return \%out;
    }
    my $has_frozen_col = exists $rows->[0]{is_frozen} ? 1 : 0;
    my $warned_unrecognized_lock_key = 0;
    for my $row (@$rows) {
      my $raw_otu = normalize_otu_key($row->{$otu_col} // '');
      my $otu = normalize_lock_otu_key($raw_otu);
      my $marker_pat = configured_marker_regex_fragment();
      if (!$warned_unrecognized_lock_key && $raw_otu ne ''
          && $raw_otu =~ /^OTUB_[^-]+-[^-]+-.+/
          && $raw_otu !~ /^(OTUB_[^-]+-$marker_pat)-.+$/) {
        warn_once("otu_lock_key_unrecognized_format:$path");
        $warned_unrecognized_lock_key = 1;
      }
      next if $otu eq '';
      my $cons_v = $row->{$cons_col} // '';
      my $is_cons = ($cons_v eq '1' || lc($cons_v) eq 'true') ? 1 : 0;
      if ($is_cons) {
        $out{sets}{consolidated}{$otu} = 1;
        next;
      }
      my $is_frozen = 0;
      if ($has_frozen_col) {
        my $fv = $row->{is_frozen} // '';
        $is_frozen = ($fv eq '1' || lc($fv) eq 'true') ? 1 : 0;
      }
      if ($is_frozen) {
        $out{sets}{frozen_not_consolidated}{$otu} = 1;
      } else {
        $out{sets}{active_not_frozen}{$otu} = 1;
      }
    }
    unless ($has_frozen_col) {
      warn_once("missing_column:$path:is_frozen");
    }
    $out{counts}{consolidated}           = scalar keys %{ $out{sets}{consolidated} };
    $out{counts}{frozen_not_consolidated} = scalar keys %{ $out{sets}{frozen_not_consolidated} };
    $out{counts}{active_not_frozen}       = scalar keys %{ $out{sets}{active_not_frozen} };
    return \%out;
  }
  if (!-e $path || !-s $path) {
    return \%out;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return \%out;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return \%out;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_key', 'OTU_id', 'otu_id');
  my $cons_col = exists $idx{effective_consolidated} ? 'effective_consolidated'
              : exists $idx{should_consolidate} ? 'should_consolidate'
              : '';
  if (!defined $otu_idx || $cons_col eq '') {
    close $FH;
    warn_once("missing_column:$path:otu_key_or_effective_consolidated");
    return \%out;
  }
  my $cons_i = $idx{$cons_col};
  my $frozen_i = exists $idx{is_frozen} ? $idx{is_frozen} : undef;
  my $warned_unrecognized_lock_key = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $cons_i > $#f || $otu_idx > $#f;
    my $raw_otu = normalize_otu_key($f[$otu_idx]);
    my $otu = normalize_lock_otu_key($raw_otu);
    my $marker_pat = configured_marker_regex_fragment();
    if (
      !$warned_unrecognized_lock_key
      && $raw_otu ne ''
      && $raw_otu =~ /^OTUB_[^-]+-[^-]+-.+/
      && $raw_otu !~ /^(OTUB_[^-]+-$marker_pat)-.+$/
    ) {
      warn_once("otu_lock_key_unrecognized_format:$path");
      $warned_unrecognized_lock_key = 1;
    }
    next if $otu eq '';
    my $is_cons = (($f[$cons_i] // '') eq '1' || lc($f[$cons_i] // '') eq 'true') ? 1 : 0;
    if ($is_cons) {
      $out{sets}{consolidated}{$otu} = 1;
      next;
    }
    my $is_frozen = 0;
    if (defined $frozen_i && $frozen_i <= $#f) {
      $is_frozen = (($f[$frozen_i] // '') eq '1' || lc($f[$frozen_i] // '') eq 'true') ? 1 : 0;
    }
    if ($is_frozen) {
      $out{sets}{frozen_not_consolidated}{$otu} = 1;
    } else {
      $out{sets}{active_not_frozen}{$otu} = 1;
    }
  }
  close $FH;
  if (!defined $frozen_i) {
    warn_once("missing_column:$path:is_frozen");
  }
  $out{counts}{consolidated} = scalar keys %{ $out{sets}{consolidated} };
  $out{counts}{frozen_not_consolidated} = scalar keys %{ $out{sets}{frozen_not_consolidated} };
  $out{counts}{active_not_frozen} = scalar keys %{ $out{sets}{active_not_frozen} };
  return \%out;
}

sub otu_lock_breakdown {
  my ($path) = @_;
  my $sets = otu_lock_sets($path);
  return $sets->{counts};
}

sub load_round_index {
  my ($path) = @_;
  return undef unless defined $path && $path ne '';
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return undef;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $idx_round = header_index_fallback(\%idx, 'round_index');
  if (!defined $idx_round) {
    close $FH;
    warn_once("missing_column:$path:round_index");
    return undef;
  }
  my $val;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $idx_round > $#f;
    my $v = trim_text($f[$idx_round]);
    next if $v eq '' || uc($v) eq 'NA';
    if ($v =~ /^[0-9]+$/) {
      $val = 0 + $v;
      last;
    }
  }
  close $FH;
  if (!defined $val) {
    warn_once("missing_round_index_value:$path");
  }
  return $val;
}

sub load_size_streak_sets {
  my ($path, $min_rounds, $enabled) = @_;
  my %out = (
    enabled => 0,
    candidate => {},
    pruned => {},
    set => {},
    status => 'missing',
  );
  return \%out unless $enabled;
  return \%out unless defined $min_rounds && $min_rounds =~ /^[0-9]+$/ && $min_rounds >= 1;
  return \%out unless defined $path && $path ne '';
  if (!-e $path) {
    $out{status} = 'missing';
    return \%out;
  }
  if (!-s $path) {
    $out{status} = 'empty';
    return \%out;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    $out{status} = 'unreadable';
    warn_once("open_failed:$path");
    return \%out;
  }
  my $rows = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    my ($key, $marker, $rid, $otu, $streak) = split /\t/, $line, 5;
    $otu = normalize_otu_key($otu);
    next unless defined $otu && defined $streak;
    next unless $streak =~ /^[0-9]+$/;
    $rows++;
    if ($streak >= $min_rounds) {
      $out{pruned}{$otu} = 1;
    } else {
      $out{candidate}{$otu} = 1;
    }
  }
  close $FH;
  $out{status} = $rows > 0 ? 'ok' : 'empty';
  $out{enabled} = 1;
  $out{set} = { %{ $out{pruned} }, %{ $out{candidate} } };
  return \%out;
}

sub load_otu_set_from_sizes_round {
  my ($path) = @_;
  my %out = (
    enabled => 0,
    set => {},
    status => 'missing',
  );
  return \%out unless defined $path && $path ne '';
  if (!-e $path) {
    return \%out;
  }
  if (!-s $path) {
    $out{status} = 'empty';
    return \%out;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    $out{status} = 'unreadable';
    return \%out;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    $out{status} = 'empty';
    return \%out;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  if (!defined $otu_idx) {
    close $FH;
    $out{status} = 'invalid';
    return \%out;
  }
  my $rows = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = normalize_otu_key($f[$otu_idx]);
    next if $otu eq '';
    $rows++;
    $out{set}{$otu} = 1;
  }
  close $FH;
  if ($rows > 0) {
    $out{enabled} = 1;
    $out{status} = 'ok';
  } else {
    $out{status} = 'empty';
  }
  return \%out;
}

sub load_otu_set_from_otu_def {
  my ($path) = @_;
  my %out = (
    enabled => 0,
    set => {},
    status => 'missing',
  );
  return \%out unless defined $path && $path ne '';
  if (!-e $path) {
    return \%out;
  }
  if (!-s $path) {
    $out{status} = 'empty';
    return \%out;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    $out{status} = 'unreadable';
    return \%out;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    $out{status} = 'empty';
    return \%out;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'OTU_id', 'otu_id');
  if (!defined $otu_idx) {
    close $FH;
    $out{status} = 'invalid';
    return \%out;
  }
  my $rows = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = normalize_otu_key($f[$otu_idx]);
    next if $otu eq '';
    $rows++;
    $out{set}{$otu} = 1;
  }
  close $FH;
  if ($rows > 0) {
    $out{enabled} = 1;
    $out{status} = 'ok';
  } else {
    $out{status} = 'empty';
  }
  return \%out;
}

sub universe_status_allows_lock_fallback {
  my ($status) = @_;
  return 0 unless defined $status;
  return 1 if $status eq 'missing';
  return 1 if $status eq 'unreadable';
  return 1 if $status eq 'invalid';
  return 0;
}

sub select_otu_fate_universe {
  my ($otu_sizes_round_path, $otu_def_path, $lock_active_set) = @_;
  my $sizes = load_otu_set_from_sizes_round($otu_sizes_round_path);
  my %sel = (
    set => undef,
    source => undef,
    reason => undef,
    sizes_status => $sizes->{status},
    otu_def_status => undef,
    strict_empty_round => 0,
  );
  if ($sizes->{status} eq 'ok') {
    $sel{set} = $sizes->{set};
    $sel{source} = 'otu_sizes_round';
    $sel{reason} = 'primary';
    return \%sel;
  }
  my $otu_def = load_otu_set_from_otu_def($otu_def_path);
  $sel{otu_def_status} = $otu_def->{status};
  if ($otu_def->{status} eq 'ok') {
    warn_once('otu_fate_universe_fallback:otu_def');
    $sel{set} = $otu_def->{set};
    $sel{source} = 'otu_def';
    $sel{reason} = 'fallback_to_otu_def';
    return \%sel;
  }
  if ($sizes->{status} eq 'empty' && $otu_def->{status} eq 'empty') {
    $sel{set} = {};
    $sel{source} = 'none';
    $sel{reason} = 'strict_empty_round';
    $sel{strict_empty_round} = 1;
    return \%sel;
  }
  my $allow_lock_fallback = universe_status_allows_lock_fallback($sizes->{status})
    || universe_status_allows_lock_fallback($otu_def->{status});
  if ($allow_lock_fallback && defined $lock_active_set && scalar(keys %{$lock_active_set}) > 0) {
    warn_once('otu_fate_universe_fallback:lock_summary');
    $sel{set} = $lock_active_set;
    $sel{source} = 'lock_summary';
    $sel{reason} = 'fallback_to_lock';
    return \%sel;
  }
  return \%sel;
}

sub load_blast_unassigned_otus {
  my ($path) = @_;
  my %out = (
    enabled => 0,
    assigned => {},
    seen => {},
  );
  return \%out unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return \%out unless defined $_parsed_rows{$path};
    my %assigned;
    my %seen;
    for my $row (@{$_parsed_rows{$path}}) {
      my $otu = normalize_otu_key($row->{otu_id} // $row->{OTU_id} // '');
      next if $otu eq '' || uc($otu) eq 'NA';
      $seen{$otu} = 1;
      my $is_assigned = 0;
      my $tax = trim_text($row->{otu_taxid} // $row->{taxid} // '');
      if ($tax ne '' && uc($tax) ne 'NA' && $tax =~ /^[0-9]+$/ && $tax > 0) {
        $is_assigned = 1;
      }
      if (!$is_assigned) {
        for my $col (qw(otu_family family otu_genus genus otu_species species)) {
          my $v = trim_text($row->{$col} // '');
          next if $v eq '' || uc($v) eq 'NA';
          if (!is_unassigned_taxon($v)) {
            $is_assigned = 1;
            last;
          }
        }
      }
      $assigned{$otu} = 1 if $is_assigned;
    }
    $out{assigned} = \%assigned;
    $out{seen}     = \%seen;
    $out{enabled}  = 1;
    return \%out;
  }
  if (!-e $path || !-s $path) {
    warn_once("missing_or_empty:$path");
    return \%out;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return \%out;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return \%out;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $otu_idx = header_index_fallback(\%idx, 'otu_id', 'OTU_id');
  my $tax_idx = header_index_fallback(\%idx, 'otu_taxid', 'taxid');
  my $family_idx = header_index_fallback(\%idx, 'otu_family', 'family');
  my $genus_idx = header_index_fallback(\%idx, 'otu_genus', 'genus');
  my $species_idx = header_index_fallback(\%idx, 'otu_species', 'species');
  if (!defined $otu_idx) {
    close $FH;
    warn_once("missing_column:$path:otu_id");
    return \%out;
  }
  if (!defined $tax_idx && !defined $family_idx && !defined $genus_idx && !defined $species_idx) {
    close $FH;
    warn_once("missing_column:$path:taxid_or_taxon");
    return \%out;
  }
  my %assigned;
  my %seen;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $otu_idx > $#f;
    my $otu = normalize_otu_key($f[$otu_idx]);
    next if $otu eq '' || uc($otu) eq 'NA';
    $seen{$otu} = 1;
    my $is_assigned = 0;
    if (defined $tax_idx && $tax_idx <= $#f) {
      my $tax = trim_text($f[$tax_idx]);
      if ($tax ne '' && uc($tax) ne 'NA' && $tax =~ /^[0-9]+$/ && $tax > 0) {
        $is_assigned = 1;
      }
    }
    if (!$is_assigned) {
      for my $idx_field ($family_idx, $genus_idx, $species_idx) {
        next unless defined $idx_field && $idx_field <= $#f;
        my $val = trim_text($f[$idx_field]);
        next if $val eq '' || uc($val) eq 'NA';
        if (!is_unassigned_taxon($val)) {
          $is_assigned = 1;
          last;
        }
      }
    }
    if ($is_assigned) {
      $assigned{$otu} = 1;
    }
  }
  close $FH;
  $out{assigned} = \%assigned;
  $out{seen} = \%seen;
  $out{enabled} = 1;
  return \%out;
}

sub classify_active_otu_fate {
  my ($otu, $ctx) = @_;
  my $otu_key = normalize_otu_key($otu);
  return undef unless defined $otu_key && $otu_key ne '';
  if ($ctx->{blast_enabled}) {
    if (exists $ctx->{blast_seen}{$otu_key} && !exists $ctx->{blast_assigned}{$otu_key}) {
      return $ctx->{blast_unassigned_grace} ? 'prune_candidates' : 'blast_unassigned';
    }
  }
  if ($ctx->{size_enabled} && exists $ctx->{size_pruned}{$otu_key}) {
    if ($ctx->{size_prune_applied}) {
      return $ctx->{size_grace} ? 'prune_candidates' : 'size_streak';
    }
    return 'prune_candidates';
  }
  return 'informative_dynamic';
}

sub skip_rounds_in_grace {
  my ($round_index, $skip_rounds_raw) = @_;
  return 0 if !defined $skip_rounds_raw || $skip_rounds_raw eq '';
  my $v = lc(trim_text($skip_rounds_raw));
  return 0 if $v eq '' || $v eq 'none' || $v eq '0';
  return 1 if $v eq 'all';
  if ($v =~ /^[0-9]+$/) {
    return 0 unless defined $round_index;
    return ($round_index <= $v) ? 1 : 0;
  }
  return 0;
}

sub grace_rounds_active {
  my ($round_index, $grace_rounds) = @_;
  return 0 unless defined $grace_rounds && $grace_rounds =~ /^[0-9]+$/;
  return 0 if $grace_rounds <= 0;
  return 0 unless defined $round_index;
  return ($round_index <= $grace_rounds) ? 1 : 0;
}

sub build_prune_policy_ctx {
  my (%args) = @_;
  my $size_streak_mode = defined($args{size_streak_mode}) ? lc(trim_text($args{size_streak_mode})) : '';
  my $size_min_members = to_nonneg_int($args{size_min_members});
  my $size_enabled = ($size_streak_mode ne '' && $size_streak_mode ne 'off') ? 1 : 0;
  my $size_sets = $args{size_sets} || {};
  my $size_possible = ($size_sets->{status} && $size_sets->{status} eq 'ok') ? 1 : 0;
  my $size_grace_active = $size_enabled ? (skip_rounds_in_grace($args{round_index}, $args{size_grace_raw}) ? 1 : 0) : 0;
  my $size_applied = ($size_enabled && $size_possible && !$size_grace_active && $size_streak_mode eq 'enforce') ? 1 : 0;

  my $blast_enabled = ($args{blast_sets} && $args{blast_sets}{enabled}) ? 1 : 0;
  my $blast_grace_active = grace_rounds_active($args{round_index}, $args{blast_grace_raw}) ? 1 : 0;

  return {
    size_enabled => $size_enabled,
    size_possible => $size_possible,
    size_grace_active => $size_grace_active,
    size_applied => $size_applied,
    size_grace_source => 'otu_blast_filter_skip_rounds',
    blast_enabled => $blast_enabled,
    blast_grace_active => $blast_grace_active,
  };
}

sub count_lock_consolidated {
  my ($path) = @_;
  return undef unless defined $path && $path ne '';
  if (exists $_parsed_rows{$path}) {
    return undef unless defined $_parsed_rows{$path};
    my $rows = $_parsed_rows{$path};
    return 0 unless @$rows;
    my $target = (exists $rows->[0]{effective_consolidated}) ? 'effective_consolidated'
               : (exists $rows->[0]{should_consolidate})     ? 'should_consolidate'
               : '';
    if ($target eq '') {
      warn_once("missing_column:$path:effective_consolidated_or_should_consolidate");
      return undef;
    }
    my $count = 0;
    for my $row (@$rows) {
      my $v = $row->{$target} // '';
      $count++ if $v eq '1' || lc($v) eq 'true';
    }
    return $count;
  }
  if (!-e $path || !-s $path) {
    return undef;
  }
  my $FH = open_cached_text_handle($path);
  if (!defined $FH) {
    warn_once("open_failed:$path");
    return undef;
  }
  my $hdr = <$FH>;
  if (!defined $hdr) {
    close $FH;
    return 0;
  }
  chomp $hdr;
  my @cols = split /\t/, $hdr, -1;
  my %idx;
  for my $i (0 .. $#cols) {
    $idx{$cols[$i]} = $i;
  }
  my $target = exists $idx{effective_consolidated} ? 'effective_consolidated'
             : exists $idx{should_consolidate}     ? 'should_consolidate'
             : '';
  if ($target eq '') {
    close $FH;
    warn_once("missing_column:$path:effective_consolidated_or_should_consolidate");
    return undef;
  }
  my $ti = $idx{$target};
  my $count = 0;
  while (my $line = <$FH>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if is_repeated_header_line($line, $hdr);
    my @f = split /\t/, $line, -1;
    next if $ti > $#f;
    my $v = $f[$ti] // '';
    $count++ if $v eq '1' || lc($v) eq 'true';
  }
  close $FH;
  return $count;
}

sub build_figures_from_list {
  my ($fig_list, $fig_dir, $fig_url_prefix, $repl) = @_;
  my @figures;
  return @figures unless defined $fig_list && $fig_list ne '';
  if (!-e $fig_list || !-s $fig_list) {
    warn_once("missing_or_empty:$fig_list");
    return @figures;
  }
  my $FL = open_cached_text_handle($fig_list);
  if (!defined $FL) {
    warn_once("open_failed:$fig_list");
    return @figures;
  }
  while (my $line = <$FL>) {
    chomp $line;
    next if $line =~ /^\s*$/;
    next if $line =~ /^\s*#/;
    my ($id, $pattern, $title, $desc, $section, $order) = split /\t/, $line, 6;
    next unless defined $pattern && $pattern ne '';
    my $filename = $pattern;
    for my $k (keys %{$repl}) {
      my $v = defined($repl->{$k}) ? $repl->{$k} : '';
      $filename =~ s/\{$k\}/$v/g;
    }
    $section = defined($section) ? $section : '';
    $section =~ s/^\s+|\s+$//g;
    $order = defined($order) ? $order : '';
    $order =~ s/^\s+|\s+$//g;
    my $rel_path = $filename;
    my $fs_path = $filename;
    if (defined $fig_dir && $fig_dir ne '') {
      $fs_path = "$fig_dir/$filename";
    }
    if (defined $fig_url_prefix && $fig_url_prefix ne '') {
      my $pfx = $fig_url_prefix;
      $pfx =~ s{/\z}{};
      $rel_path = "$pfx/$filename";
    }
    my $exists = (-s $fs_path) ? JSON::PP::true() : JSON::PP::false();
    my $pdf_rel_path;
    my $pdf_exists = JSON::PP::false();
    if ($filename =~ /\.png$/i) {
      my $pdf_filename = $filename;
      $pdf_filename =~ s/\.png$/.pdf/i;
      my $pdf_fs_path = $pdf_filename;
      if (defined $fig_dir && $fig_dir ne '') {
        $pdf_fs_path = "$fig_dir/$pdf_filename";
      }
      if (-s $pdf_fs_path) {
        $pdf_exists = JSON::PP::true();
        $pdf_rel_path = $pdf_filename;
        if (defined $fig_url_prefix && $fig_url_prefix ne '') {
          my $pfx = $fig_url_prefix;
          $pfx =~ s{/\z}{};
          $pdf_rel_path = "$pfx/$pdf_filename";
        }
      }
    }
    push @figures, {
      id => $id,
      title => (defined $title && $title ne '') ? $title : $id,
      description => (defined $desc && $desc ne '') ? $desc : undef,
      path => $rel_path,
      pdf_path => $pdf_rel_path,
      pdf_exists => $pdf_exists,
      section => ($section ne '') ? $section : 'Other',
      order => ($order ne '' && $order =~ /^\d+$/) ? 0 + $order : 999,
      exists => $exists,
    };
  }
  close $FL;
  return @figures;
}

my $timestamp_utc = (defined $opt{timestamp_utc} && $opt{timestamp_utc} ne '')
  ? $opt{timestamp_utc}
  : strftime('%Y-%m-%dT%H:%M:%SZ', gmtime());
my $round_status = 'ok';
my $failure_reason;
my $failure_stage;
if (defined $opt{round_failed_file} && $opt{round_failed_file} ne '' && -s $opt{round_failed_file}) {
  $round_status = 'failed';
  my $raw = cached_file_text($opt{round_failed_file});
  if (defined $raw) {
    $failure_reason = $$raw;
    $failure_reason =~ s/\s+\z//;
    $failure_reason = undef if $failure_reason eq '';
  }
  $failure_reason //= 'Round failed';
  $failure_stage = 'fast_on_target_detection';
}

# Warm parsed-row cache for inputs read multiple times per invocation.
get_parsed_rows($opt{read_info});
get_parsed_rows($opt{on_target});
get_parsed_rows($opt{otu_def});
get_parsed_rows($opt{blast_otu});
get_parsed_rows($opt{demult});

my $reads_total    = count_rows($opt{read_info}, 1);
my $reads_on_target= count_value_in_column($opt{on_target}, 'on_target_kingdom', 'ON_TARGET');
if (!defined $reads_on_target) {
  # Backward-compatible fallback for legacy on_target reports that only include target hits.
  $reads_on_target = count_rows($opt{on_target}, 1);
  if (defined $reads_on_target && defined $opt{on_target} && $opt{on_target} ne '') {
    warn_once("on_target_count_fallback_rows:$opt{on_target}");
  }
}
my $reads_hac      = count_non_na_in_column($opt{read_info}, 'hac_length');
my $reads_sup      = count_non_na_in_column($opt{read_info}, 'sup_length');

my $round_index = load_round_index($opt{round_index_file});
my $otu_lock_sets   = otu_lock_sets($opt{otu_lock_summary});
my $otu_lock_break  = $otu_lock_sets->{counts};
my $lock_cons_set = $otu_lock_sets->{sets}{consolidated} || {};
my $lock_frozen_set = $otu_lock_sets->{sets}{frozen_not_consolidated} || {};
my $otu_lock_cons_raw   = defined($otu_lock_break->{consolidated})
  ? $otu_lock_break->{consolidated}
  : count_lock_consolidated($opt{otu_lock_summary});
my $otu_lock_frozen_raw = $otu_lock_break->{frozen_not_consolidated};
my $otu_lock_active_raw = $otu_lock_break->{active_not_frozen};
my $otu_fate_universe = select_otu_fate_universe(
  $opt{otu_sizes_round},
  $opt{otu_def},
  ($otu_lock_sets->{sets}{active_not_frozen} || {}),
);
my $otu_fate_universe_source = $otu_fate_universe->{source};
my $otu_fate_universe_reason = $otu_fate_universe->{reason};
my $otu_fate_universe_sizes_status = $otu_fate_universe->{sizes_status};
my $otu_fate_universe_otu_def_status = $otu_fate_universe->{otu_def_status};
my $otu_fate_strict_empty_round = $otu_fate_universe->{strict_empty_round} ? 1 : 0;
my $otu_universe_set = $otu_fate_universe->{set};
my $otu_active = defined($otu_universe_set)
  ? scalar(keys %{$otu_universe_set})
  : count_unique_column_fallback($opt{otu_def}, 'OTU_id', 'otu_id');
my $otu_lock_cons = undef;
my $otu_lock_frozen = undef;
my $otu_active_not_frozen = undef;
my %otu_fate_universe_set = ();
if (defined $otu_universe_set) {
  %otu_fate_universe_set = %{$otu_universe_set};
  my $cons_n = 0;
  for my $otu (keys %{$lock_cons_set}) {
    $cons_n++ if exists $otu_fate_universe_set{$otu};
  }
  my $frozen_n = 0;
  for my $otu (keys %{$lock_frozen_set}) {
    $frozen_n++ if exists $otu_fate_universe_set{$otu};
  }
  $otu_lock_cons = $cons_n;
  $otu_lock_frozen = $frozen_n;
  for my $otu (keys %{$lock_cons_set}) {
    delete $otu_fate_universe_set{$otu};
  }
  for my $otu (keys %{$lock_frozen_set}) {
    delete $otu_fate_universe_set{$otu};
  }
  $otu_active_not_frozen = scalar keys %otu_fate_universe_set;
} elsif (defined $otu_lock_active_raw) {
  %otu_fate_universe_set = %{ $otu_lock_sets->{sets}{active_not_frozen} || {} };
  $otu_active_not_frozen = scalar keys %otu_fate_universe_set;
  $otu_lock_cons = $otu_lock_cons_raw;
  $otu_lock_frozen = $otu_lock_frozen_raw;
}
my $blast_f_reads = kv_value($opt{otu_blast_filter_stats}, 'kept_reads');
my $blast_f_otus  = kv_value($opt{otu_blast_filter_stats}, 'kept_otus');
my $blast_missing_policy = kv_value($opt{otu_blast_filter_stats}, 'missing_policy');
my $blast_mode    = (defined($opt{blast_filter_mode}) && $opt{blast_filter_mode} ne '') ? $opt{blast_filter_mode} : undef;

my $cons_emitted       = count_rows($opt{blast_consensus}, 1);
my $cons_consolidated  = count_rows($opt{consensus_consolidated_ids}, 0);

my $diag_rows_total = kv_value($opt{otu_members_blastdiag_stats}, 'rows_total', 1);
my $diag_otu_total  = kv_value($opt{otu_members_blastdiag_stats}, 'otu_total', 1);
my $blast_assigned_otu_total = count_unique_column_fallback($opt{blast_otu}, 'otu_id', 'OTU_id');
my $size_streak_mode = defined $opt{otu_size_streak_mode}
  ? lc(trim_text($opt{otu_size_streak_mode}))
  : '';
my $size_streak_min_rounds = to_nonneg_int($opt{otu_size_streak_min_rounds});
if (defined $size_streak_min_rounds && $size_streak_min_rounds < 1) {
  $size_streak_min_rounds = undef;
}
my $size_min_members = to_nonneg_int($opt{otu_blast_min_members});
my $size_streak_enabled = (defined($size_streak_mode) && $size_streak_mode ne '' && $size_streak_mode ne 'off') ? 1 : 0;
my $size_streak_sets = load_size_streak_sets($opt{otu_size_streak}, $size_streak_min_rounds, $size_streak_enabled);
my $blast_unassigned_sets = load_blast_unassigned_otus($opt{blast_otu});
my $size_sets = {
  enabled => $size_streak_sets->{enabled},
  status => $size_streak_sets->{status},
  set => ($size_streak_sets->{set} || {}),
};
my $policy_ctx = build_prune_policy_ctx(
  round_index => $round_index,
  size_min_members => $size_min_members,
  size_streak_mode => $size_streak_mode,
  size_sets => $size_sets,
  size_grace_raw => $opt{otu_blast_filter_skip_rounds},
  blast_sets => $blast_unassigned_sets,
  blast_grace_raw => to_nonneg_int($opt{otu_blast_unassigned_grace_rounds}),
);
my $blast_unassigned_grace = $policy_ctx->{blast_grace_active} ? 1 : 0;
my $size_grace = $policy_ctx->{size_grace_active} ? 1 : 0;
if (!defined $round_index && defined $opt{otu_blast_filter_skip_rounds} && $opt{otu_blast_filter_skip_rounds} =~ /^[0-9]+$/) {
  warn_once("missing_round_index:otu_blast_filter_skip_rounds");
}
if (!defined $round_index && defined $opt{otu_blast_unassigned_grace_rounds} && $opt{otu_blast_unassigned_grace_rounds} =~ /^[0-9]+$/ && $opt{otu_blast_unassigned_grace_rounds} > 0) {
  warn_once("missing_round_index:otu_blast_unassigned_grace_rounds");
}
my %otu_fate_counts = (
  informative_dynamic => undef,
  prune_candidates => undef,
  size_streak => undef,
  blast_unassigned => undef,
);
my $otu_fate_conservation_ok = undef;
my $otu_fate_conservation_delta = undef;
my %otu_informative_dynamic_set;
if (defined $otu_active_not_frozen) {
  my $size_enabled = $policy_ctx->{size_enabled} ? 1 : 0;
  my $blast_enabled = $policy_ctx->{blast_enabled} ? 1 : 0;
  my $blast_assigned_set = $blast_unassigned_sets->{assigned} || {};
  my $blast_seen_set = $blast_unassigned_sets->{seen} || {};
  my %counts = (
    informative_dynamic => 0,
    prune_candidates => 0,
    size_streak => 0,
    blast_unassigned => 0,
  );
  my %ctx = (
    blast_enabled => $blast_enabled,
    blast_assigned => $blast_assigned_set,
    blast_seen => $blast_seen_set,
    blast_unassigned_grace => $blast_unassigned_grace ? 1 : 0,
    size_enabled => $size_enabled,
    size_pruned => ($size_sets->{set} || {}),
    size_prune_applied => $policy_ctx->{size_applied} ? 1 : 0,
    size_grace => $size_grace ? 1 : 0,
  );
  for my $otu (keys %otu_fate_universe_set) {
    my $label = classify_active_otu_fate($otu, \%ctx);
    next unless defined $label;
    $counts{$label}++;
    if ($label eq 'informative_dynamic') {
      $otu_informative_dynamic_set{$otu} = 1;
    }
  }
  $otu_fate_counts{informative_dynamic} = $counts{informative_dynamic};
  $otu_fate_counts{prune_candidates} = ($size_enabled || $blast_enabled)
    ? $counts{prune_candidates}
    : undef;
  if ($size_enabled) {
    $otu_fate_counts{size_streak} = $policy_ctx->{size_applied} ? $counts{size_streak} : 0;
  } else {
    $otu_fate_counts{size_streak} = undef;
  }
  $otu_fate_counts{blast_unassigned} = $blast_enabled ? $counts{blast_unassigned} : undef;
  my $fate_sum = ($counts{informative_dynamic} || 0)
    + ($counts{prune_candidates} || 0)
    + ($counts{size_streak} || 0)
    + ($counts{blast_unassigned} || 0);
  $otu_fate_conservation_ok = JSON::PP::true();
  if ($fate_sum != $otu_active_not_frozen) {
    $otu_fate_conservation_ok = JSON::PP::false();
    $otu_fate_conservation_delta = $fate_sum - $otu_active_not_frozen;
    warn_once("otu_fate_conservation_mismatch:${otu_active_not_frozen}:${fate_sum}");
  }
}
if ($otu_fate_strict_empty_round) {
  $otu_active = 0;
  $otu_lock_cons = 0 unless defined $otu_lock_cons;
  $otu_lock_frozen = 0 unless defined $otu_lock_frozen;
  $otu_active_not_frozen = 0;
  $otu_fate_counts{informative_dynamic} = 0;
  $otu_fate_counts{prune_candidates} = 0;
  $otu_fate_counts{size_streak} = 0;
  $otu_fate_counts{blast_unassigned} = 0;
  $otu_fate_conservation_ok = JSON::PP::true();
  $otu_fate_conservation_delta = undef;
}

my %sample_metrics;
my %sample_label_to_id;
my %sample_id_to_label;

seed_sample_entries_from_roster(
  $opt{sample_roster},
  \%sample_metrics,
  \%sample_label_to_id,
  \%sample_id_to_label,
);
if ($opt{identity_mode} eq 'track' && defined $opt{track_identity} && $opt{track_identity} ne '') {
  seed_track_unit_metrics_from_identity($opt{track_identity}, $opt{sample_roster});
}
$g_roster_ready = 1;
my $demux_total_reads = 0;
my $no_adapter_reads = 0;
my $blast_assignment_status = undef;
my $blast_seen_reads_total = undef;
my $blast_assigned_reads_total = undef;
my $blast_unassigned_reads_total = undef;
my $blast_seen_reads_adapter = undef;
my $blast_seen_reads_no_adapter = undef;
my $blast_seen_reads_unbucketed = undef;
my $blast_assigned_reads_adapter = undef;
my $blast_assigned_reads_no_adapter = undef;
my $blast_unassigned_reads_adapter = undef;
my $blast_unassigned_reads_no_adapter = undef;
my $consensus_used_reads_total = sum_consensus_round_reads($opt{consensus_round_provenance});
my $cons_emitted_by_marker = consensus_emitted_by_marker_from_provenance($opt{consensus_round_provenance});
my $cons_assigned_by_marker = consensus_assigned_by_marker_from_report($opt{blast_consensus});

my %spec_interest;
load_spec_interest($opt{spec_basics_metazoa}, \%spec_interest);
load_spec_interest($opt{spec_basics_viridiplantae}, \%spec_interest);
my $spec_interest_enabled = scalar keys %spec_interest ? 1 : 0;

my $informative_source_available = defined($otu_active_not_frozen) || defined($otu_lock_cons_raw) || defined($otu_lock_frozen_raw);
my %otu_informative_set = ();
if ($informative_source_available) {
  for my $otu (keys %otu_informative_dynamic_set) {
    my $key = normalize_otu_key($otu);
    $otu_informative_set{$key} = 1 if $key ne '';
  }
  for my $otu (keys %{$lock_cons_set}) {
    my $key = normalize_otu_key($otu);
    $otu_informative_set{$key} = 1 if $key ne '';
  }
  for my $otu (keys %{$lock_frozen_set}) {
    my $key = normalize_otu_key($otu);
    $otu_informative_set{$key} = 1 if $key ne '';
  }
}

my $otu_informative_by_marker = $informative_source_available
  ? otu_counts_by_marker_from_set(\%otu_informative_set)
  : undef;
my $_blast_otu_for_sunburst = (defined $opt{blast_otu_cumulative} && $opt{blast_otu_cumulative} ne '' && -s $opt{blast_otu_cumulative})
  ? $opt{blast_otu_cumulative} : $opt{blast_otu};
my $otu_informative_assigned_by_marker = $informative_source_available
  ? otu_assigned_by_marker_from_blast_otu($_blast_otu_for_sunburst, \%otu_informative_set)
  : undef;
my $consolidated_cons_set = load_id_set($opt{consensus_consolidated_ids});
my ($otu_assignments_by_level, $otu_rep_reads, $sample_marker_rep_reads) = collect_otu_assignments_by_level($_blast_otu_for_sunburst, $opt{otu_sizes_round}, \%spec_interest, $spec_interest_enabled, $lock_frozen_set, $otu_assignment_thresholds);
my $consensus_assignments_by_level = collect_consensus_assignments_by_level($opt{blast_consensus}, \%spec_interest, $spec_interest_enabled, $consolidated_cons_set, $opt{identity_mode}, $sample_marker_rep_reads);
my %otu_active_by_marker_taxon = (
  assigned => blank_marker_count_map(),
  unassigned => blank_marker_count_map(),
  coi_assigned => undef,
  its2_assigned => undef,
  other_assigned => undef,
  coi_unassigned => undef,
  its2_unassigned => undef,
  other_unassigned => undef,
);
if (defined $otu_informative_by_marker || defined $otu_informative_assigned_by_marker) {
  my @_anf_markers = @CONFIGURED_MARKERS ? @CONFIGURED_MARKERS : qw(COI ITS2);
  for my $marker (@_anf_markers, 'OTHER') {
    my $total = defined $otu_informative_by_marker
      ? (exists $otu_informative_by_marker->{$marker} ? $otu_informative_by_marker->{$marker} : 0)
      : undef;
    my $assigned = defined $otu_informative_assigned_by_marker
      ? (exists $otu_informative_assigned_by_marker->{$marker} ? $otu_informative_assigned_by_marker->{$marker} : 0)
      : undef;
    $otu_active_by_marker_taxon{assigned}{$marker} =
      defined($assigned) ? 0 + $assigned : undef;
    if (defined $total && defined $assigned) {
      my $unassigned = $total - $assigned;
      if ($unassigned < 0) {
        warn_once("inconsistent_otu_marker_counts:${marker}_assigned_exceeds_active");
        $unassigned = 0;
      }
      $otu_active_by_marker_taxon{unassigned}{$marker} = $unassigned;
    } else {
      $otu_active_by_marker_taxon{unassigned}{$marker} = undef;
    }
  }
  for my $m (qw(COI ITS2 OTHER)) {
    my $mk = lc($m);
    $otu_active_by_marker_taxon{"${mk}_assigned"}   = $otu_active_by_marker_taxon{assigned}{$m};
    $otu_active_by_marker_taxon{"${mk}_unassigned"} = $otu_active_by_marker_taxon{unassigned}{$m};
  }
}

my $debug_otu_out = $opt{debug_otu_out};
if (defined $debug_otu_out && $debug_otu_out ne '') {
  if (open my $DBG, '>', $debug_otu_out) {
    my $flags = load_blast_otu_flags($opt{blast_otu});
    my $otu_total = defined($flags) ? scalar keys %{$flags} : undef;
    my ($otu_assigned_taxid, $otu_assigned_text, $otu_assigned_text_no_taxid) = (undef, undef, undef);
    if (defined $flags) {
      $otu_assigned_taxid = 0;
      $otu_assigned_text = 0;
      $otu_assigned_text_no_taxid = 0;
      for my $k (keys %{$flags}) {
        my $f = $flags->{$k};
        $otu_assigned_taxid++ if $f->{taxid};
        if ($f->{text}) {
          $otu_assigned_text++;
          $otu_assigned_text_no_taxid++ if !$f->{taxid};
        }
      }
    }

    my $info_total = $informative_source_available ? scalar keys %otu_informative_set : undef;
    my ($info_in_blast, $info_missing, $info_assigned_taxid, $info_assigned_text, $info_text_no_taxid) = (undef, undef, undef, undef, undef);
    my %info_assigned_text_marker = ();
    if ($informative_source_available) {
      $info_in_blast = 0;
      $info_missing = 0;
      $info_assigned_text = 0;
      $info_text_no_taxid = 0;
      for my $otu (keys %otu_informative_set) {
        if (defined $flags && exists $flags->{$otu}) {
          $info_in_blast++;
          my $f = $flags->{$otu};
          if ($f->{text}) {
            $info_assigned_text++;
            $info_text_no_taxid++ if !$f->{taxid};
            my $m = $f->{marker};
            $m = marker_from_token($otu) if !defined $m || $m eq '';
            $m = 'OTHER' if !defined $m || $m eq '';
            $info_assigned_text_marker{$m}++;
          }
        } else {
          $info_missing++;
        }
      }
      # informative_assigned_taxid: use cumulative blast count so it is
      # consistent with informative_assigned_taxid_{marker} and the barplot.
      if (defined $otu_informative_assigned_by_marker) {
        $info_assigned_taxid = 0;
        $info_assigned_taxid += $_ for values %{$otu_informative_assigned_by_marker};
      }
    }

    my $write_kv = sub {
      my ($k, $v) = @_;
      $v = 'NA' if !defined $v;
      print $DBG $k, "\t", $v, "\n";
    };

    $write_kv->('debug_version', '1');
    $write_kv->('blast_otu_total', $otu_total);
    $write_kv->('blast_otu_assigned_taxid', $otu_assigned_taxid);
    $write_kv->('blast_otu_assigned_text', $otu_assigned_text);
    $write_kv->('blast_otu_assigned_text_no_taxid', $otu_assigned_text_no_taxid);
    $write_kv->('informative_total', $info_total);
    $write_kv->('informative_in_blast_otu', $info_in_blast);
    $write_kv->('informative_missing_in_blast_otu', $info_missing);
    $write_kv->('informative_assigned_taxid', $info_assigned_taxid);
    $write_kv->('informative_assigned_text', $info_assigned_text);
    $write_kv->('informative_assigned_text_no_taxid', $info_text_no_taxid);

    for my $marker (@CONFIGURED_MARKERS, 'OTHER') {
      my $total = defined $otu_informative_by_marker
        ? (exists $otu_informative_by_marker->{$marker} ? $otu_informative_by_marker->{$marker} : 0)
        : undef;
      my $assigned_taxid = defined $otu_informative_assigned_by_marker
        ? (exists $otu_informative_assigned_by_marker->{$marker} ? $otu_informative_assigned_by_marker->{$marker} : 0)
        : undef;
      my $assigned_text = exists $info_assigned_text_marker{$marker} ? $info_assigned_text_marker{$marker} : undef;
      $write_kv->("informative_total_" . lc($marker), $total);
      $write_kv->("informative_assigned_taxid_" . lc($marker), $assigned_taxid);
      $write_kv->("informative_assigned_text_" . lc($marker), $assigned_text);
    }

    close $DBG;
  } else {
    warn_once("open_failed:$debug_otu_out");
  }
}

my %consensus_emitted_by_marker_taxon = (
  assigned => blank_marker_count_map(),
  unassigned => blank_marker_count_map(),
  coi_assigned => undef,
  its2_assigned => undef,
  other_assigned => undef,
  coi_unassigned => undef,
  its2_unassigned => undef,
  other_unassigned => undef,
);
if (defined $cons_emitted_by_marker || defined $cons_assigned_by_marker) {
  my @_cons_markers = @CONFIGURED_MARKERS ? @CONFIGURED_MARKERS : qw(COI ITS2);
  for my $marker (@_cons_markers, 'OTHER') {
    my $total = defined $cons_emitted_by_marker
      ? (exists $cons_emitted_by_marker->{$marker} ? $cons_emitted_by_marker->{$marker} : 0)
      : undef;
    my $assigned = defined $cons_assigned_by_marker
      ? (exists $cons_assigned_by_marker->{$marker} ? $cons_assigned_by_marker->{$marker} : 0)
      : undef;
    $consensus_emitted_by_marker_taxon{assigned}{$marker} =
      defined($assigned) ? 0 + $assigned : undef;
    if (defined $total && defined $assigned) {
      my $unassigned = $total - $assigned;
      if ($unassigned < 0) {
        warn_once("inconsistent_consensus_counts:${marker}_assigned_exceeds_total");
        $unassigned = 0;
      }
      $consensus_emitted_by_marker_taxon{unassigned}{$marker} = $unassigned;
    } else {
      $consensus_emitted_by_marker_taxon{unassigned}{$marker} = undef;
    }
  }
  for my $m (qw(COI ITS2 OTHER)) {
    my $mk = lc($m);
    $consensus_emitted_by_marker_taxon{"${mk}_assigned"}   = $consensus_emitted_by_marker_taxon{assigned}{$m};
    $consensus_emitted_by_marker_taxon{"${mk}_unassigned"} = $consensus_emitted_by_marker_taxon{unassigned}{$m};
  }
}

my $prune_counts = load_kv_tsv($opt{active_prune_counts});
my $prune_candidates_round = {
  active_total         => undef,
  size_streak_active   => undef,
  size_streak_candidates => undef,
  union                => undef,
  active_scope         => undef,
  active_scope_reason  => undef,
  size_streak_input_status => undef,
  size_streak_possible => undef,
  size_streak_applied  => undef,
  size_streak_disabled => undef,
  effective_mode       => undef,
  effective_reason     => undef,
  round_index          => undef,
  size_streak_round_candidates => undef,
  size_streak_eligible_candidates => undef,
  blast_unassigned_candidates         => undef,
  blast_unassigned_status             => undef,
  otu_unassigned_streak_candidates    => undef,
  consensus_unassigned_candidates     => undef,
};
if (defined $prune_counts) {
  $prune_candidates_round = {
    active_total         => to_nonneg_int_or_undef($prune_counts->{active_total}),
    size_streak_active   => to_nonneg_int_or_undef($prune_counts->{size_streak_active}),
    size_streak_candidates => to_nonneg_int_or_undef($prune_counts->{size_streak_candidates}),
    union                => to_nonneg_int_or_undef($prune_counts->{union}),
    active_scope         => to_text_or_undef($prune_counts->{active_scope}),
    active_scope_reason  => to_text_or_undef($prune_counts->{active_scope_reason}),
    size_streak_input_status => to_text_or_undef($prune_counts->{size_streak_input_status}),
    size_streak_possible => to_nonneg_int_or_undef($prune_counts->{size_streak_possible}),
    size_streak_applied  => to_nonneg_int_or_undef($prune_counts->{size_streak_applied}),
    size_streak_disabled => to_nonneg_int_or_undef($prune_counts->{size_streak_disabled}),
    effective_mode       => to_text_or_undef($prune_counts->{effective_mode}),
    effective_reason     => to_text_or_undef($prune_counts->{effective_reason}),
    round_index          => to_nonneg_int_or_undef($prune_counts->{round_index}),
    size_streak_round_candidates => to_nonneg_int_or_undef($prune_counts->{size_streak_round_candidates}),
    size_streak_eligible_candidates => to_nonneg_int_or_undef($prune_counts->{size_streak_eligible_candidates}),
    blast_unassigned_candidates      => to_nonneg_int_or_undef($prune_counts->{blast_unassigned_candidates}),
    blast_unassigned_status          => to_text_or_undef($prune_counts->{blast_unassigned_status}),
    otu_unassigned_streak_candidates => to_nonneg_int_or_undef($prune_counts->{otu_unassigned_streak_candidates}),
    consensus_unassigned_candidates  => to_nonneg_int_or_undef($prune_counts->{consensus_unassigned_candidates}),
  };
}
my $demux_enabled = undef;

# Per-sample metric extraction (single source of truth in collector)
# reads_demux: rows in demultiplex report grouped by sample.
if (defined $opt{demult} && $opt{demult} ne '') {
  if (exists $_parsed_rows{$opt{demult}}) {
    if (defined $_parsed_rows{$opt{demult}}) {
      my $dmx_rows = $_parsed_rows{$opt{demult}};
      my $has_sample_col = @$dmx_rows && exists $dmx_rows->[0]{sample};
      if (!$has_sample_col && @$dmx_rows) {
        warn_once("missing_column:$opt{demult}:sample");
      }
      if ($has_sample_col) {
        for my $row (@$dmx_rows) {
          my $sample_val = $row->{sample} // '';
          next if $sample_val eq '';
          my $sid = ensure_sample_entry(\%sample_metrics, \%sample_label_to_id, \%sample_id_to_label, $sample_val);
          my $entry = $sample_metrics{$sid};
          $entry->{reads_demux} = 0 unless defined $entry->{reads_demux};
          $entry->{reads_demux}++;
          my $_dmx_marker = SampleLabel::extract_marker_from_label($sample_val);
          # For no_adapter rows, marker is encoded in barcode_by_homology as the target token
          # (e.g. "COI" or "ITS2") placed there by the second cutadapt primer-detection pass.
          if ($_dmx_marker eq '' && SampleLabel::is_no_adapter_label($sample_val)) {
            my $bchom = $row->{barcode_by_homology} // '';
            if ($bchom ne '' && uc($bchom) ne 'NA') {
              $_dmx_marker = marker_from_token($bchom);
              $_dmx_marker = '' if !defined $_dmx_marker || $_dmx_marker eq 'OTHER';
            }
          }
          increment_sample_demux_marker_counts($entry, $_dmx_marker);
          my $track_entry = resolve_track_unit_metrics_entry($sample_val, $entry->{label}, $_dmx_marker);
          if (defined $track_entry) {
            $track_entry->{reads_demux} = 0 unless defined $track_entry->{reads_demux};
            $track_entry->{reads_demux}++;
            increment_sample_demux_marker_counts($track_entry, $_dmx_marker);
          }
          my $rep_dmx = ensure_replicate_sub_entry($entry, $sample_val);
          if (defined $rep_dmx) {
            $rep_dmx->{reads_demux} = 0 unless defined $rep_dmx->{reads_demux};
            $rep_dmx->{reads_demux}++;
          }
          $demux_total_reads++;
        }
      }
    }
  } elsif (!-e $opt{demult} || !-s $opt{demult}) {
    warn_once("missing_or_empty:$opt{demult}");
  } else {
    my $FH = open_cached_text_handle($opt{demult});
    if (defined $FH) {
      my $hdr = <$FH>;
      if (defined $hdr) {
        chomp $hdr;
        my @cols = split /\t/, $hdr, -1;
        my %idx;
        for my $i (0 .. $#cols) {
          $idx{$cols[$i]} = $i;
        }
        my $sample_idx  = header_index_fallback(\%idx, 'sample');
        my $bchom_idx   = header_index_fallback(\%idx, 'barcode_by_homology');
        if (!defined $sample_idx) {
          warn_once("missing_column:$opt{demult}:sample");
        } else {
          while (my $line = <$FH>) {
            chomp $line;
            next if $line =~ /^\s*$/;
            next if is_repeated_header_line($line, $hdr);
            my @f = split /\t/, $line, -1;
            next if $sample_idx > $#f;
            my $sid = ensure_sample_entry(\%sample_metrics, \%sample_label_to_id, \%sample_id_to_label, $f[$sample_idx]);
            my $entry = $sample_metrics{$sid};
            $entry->{reads_demux} = 0 unless defined $entry->{reads_demux};
            $entry->{reads_demux}++;
            my $_dmx_marker = SampleLabel::extract_marker_from_label($f[$sample_idx]);
            # For no_adapter rows, marker is encoded in barcode_by_homology as the target token
            # (e.g. "COI" or "ITS2") placed there by the second cutadapt primer-detection pass.
            if ($_dmx_marker eq '' && SampleLabel::is_no_adapter_label($f[$sample_idx])
                && defined $bchom_idx && $bchom_idx <= $#f) {
              my $bchom = $f[$bchom_idx];
              if (defined $bchom && $bchom ne '' && uc($bchom) ne 'NA') {
                $_dmx_marker = marker_from_token($bchom);
                $_dmx_marker = '' if !defined $_dmx_marker || $_dmx_marker eq 'OTHER';
              }
            }
            increment_sample_demux_marker_counts($entry, $_dmx_marker);
            my $track_entry = resolve_track_unit_metrics_entry($f[$sample_idx], $entry->{label}, $_dmx_marker);
            if (defined $track_entry) {
              $track_entry->{reads_demux} = 0 unless defined $track_entry->{reads_demux};
              $track_entry->{reads_demux}++;
              increment_sample_demux_marker_counts($track_entry, $_dmx_marker);
            }
            my $rep_dmx = ensure_replicate_sub_entry($entry, $f[$sample_idx]);
            if (defined $rep_dmx) {
              $rep_dmx->{reads_demux} = 0 unless defined $rep_dmx->{reads_demux};
              $rep_dmx->{reads_demux}++;
            }
            $demux_total_reads++;
          }
        }
      }
      close $FH;
    } else {
      warn_once("open_failed:$opt{demult}");
    }
  }
}

# read fate + sample reads_blast_assigned + sample otu_active from blast OTU report.
my $blast_read_fate = collect_blast_read_fate_and_sample_metrics(
  $opt{blast_otu},
  \%sample_metrics,
  \%sample_label_to_id,
  \%sample_id_to_label,
  ((scalar keys %TRACK_UNIT_METRICS) ? \%TRACK_UNIT_METRICS : undef),
);
if ($blast_read_fate->{enabled}) {
  $blast_assignment_status = $blast_read_fate->{assignment_status};
  $blast_seen_reads_total = $blast_read_fate->{seen}{total};
  $blast_seen_reads_adapter = $blast_read_fate->{seen}{adapter};
  $blast_seen_reads_no_adapter = $blast_read_fate->{seen}{no_adapter};
  $blast_seen_reads_unbucketed = $blast_read_fate->{seen}{unbucketed};
  $blast_assigned_reads_total = $blast_read_fate->{assigned}{total};
  $blast_assigned_reads_adapter = $blast_read_fate->{assigned}{adapter};
  $blast_assigned_reads_no_adapter = $blast_read_fate->{assigned}{no_adapter};
  $blast_unassigned_reads_total = $blast_read_fate->{unassigned}{total};
  $blast_unassigned_reads_adapter = $blast_read_fate->{unassigned}{adapter};
  $blast_unassigned_reads_no_adapter = $blast_read_fate->{unassigned}{no_adapter};
}

# consensus_emitted: unique consensus IDs grouped by sample.
if (defined $opt{blast_consensus} && $opt{blast_consensus} ne '') {
  if (!-e $opt{blast_consensus} || !-s $opt{blast_consensus}) {
    warn_once("missing_or_empty:$opt{blast_consensus}");
  } else {
    my $FH = open_cached_text_handle($opt{blast_consensus});
    if (defined $FH) {
      my $hdr = <$FH>;
      if (defined $hdr) {
        chomp $hdr;
        my @cols = split /\t/, $hdr, -1;
        my %idx;
        for my $i (0 .. $#cols) {
          $idx{$cols[$i]} = $i;
        }
        my $sample_idx = header_index_fallback(\%idx, 'sample');
        my $cons_idx = header_index_fallback(\%idx, 'consensus_id', 'long_seq_id');
        my $marker_idx = header_index_fallback(\%idx, 'barcode_by_homology');
        if (!defined $sample_idx) {
          warn_once("missing_column:$opt{blast_consensus}:sample");
        } elsif (!defined $cons_idx) {
          warn_once("missing_column:$opt{blast_consensus}:consensus_id_or_long_seq_id");
        } else {
          my %seen_sample_consensus;
          my %seen_track_unit_consensus;
          while (my $line = <$FH>) {
            chomp $line;
            next if $line =~ /^\s*$/;
            next if is_repeated_header_line($line, $hdr);
            my @f = split /\t/, $line, -1;
            next if $sample_idx > $#f || $cons_idx > $#f;
            my $sid = ensure_sample_entry(\%sample_metrics, \%sample_label_to_id, \%sample_id_to_label, $f[$sample_idx]);
            my $cons_id = trim_text($f[$cons_idx]);
            next if $cons_id eq '' || uc($cons_id) eq 'NA';
            my $entry = $sample_metrics{$sid};
            $entry->{consensus_emitted} = 0 unless defined $entry->{consensus_emitted};
            my $k = "$sid\t$cons_id";
            if (!$seen_sample_consensus{$k}) {
              $entry->{consensus_emitted}++;
              $seen_sample_consensus{$k} = 1;
            }
            my $marker_raw = (defined $marker_idx && $marker_idx <= $#f) ? $f[$marker_idx] : $cons_id;
            my $track_entry = resolve_track_unit_metrics_entry($f[$sample_idx], $entry->{label}, $marker_raw);
            if (defined $track_entry) {
              $track_entry->{consensus_emitted} = 0 unless defined $track_entry->{consensus_emitted};
              my $track_k = $track_entry->{track_unit_id} . "\t" . $cons_id;
              if (!$seen_track_unit_consensus{$track_k}) {
                $track_entry->{consensus_emitted}++;
                $seen_track_unit_consensus{$track_k} = 1;
              }
            }
            my $rep_cons = ensure_replicate_sub_entry($entry, $f[$sample_idx]);
            if (defined $rep_cons) {
              $rep_cons->{consensus_emitted} = 0 unless defined $rep_cons->{consensus_emitted};
              my $rep_k = "$sid\t$rep_cons->{label}\t$cons_id";
              if (!$seen_sample_consensus{$rep_k}) {
                $rep_cons->{consensus_emitted}++;
                $seen_sample_consensus{$rep_k} = 1;
              }
            }
          }
        }
      }
      close $FH;
    } else {
      warn_once("open_failed:$opt{blast_consensus}");
    }
  }
}

# consensus_consolidated: unique consensus IDs per sample from cumulative consolidated state file.
if (defined $opt{blast_consensus_consolidated} && $opt{blast_consensus_consolidated} ne '' && -s $opt{blast_consensus_consolidated}) {
  my $FC = open_cached_text_handle($opt{blast_consensus_consolidated});
  if (defined $FC) {
    my $chdr = <$FC>;
    if (defined $chdr) {
      chomp $chdr;
      my @ccols = split /\t/, $chdr, -1;
      my %cidx;
      for my $i (0 .. $#ccols) { $cidx{$ccols[$i]} = $i; }
      my $csample_idx = header_index_fallback(\%cidx, 'sample');
      my $ccons_idx   = header_index_fallback(\%cidx, 'consensus_id');
      if (defined $csample_idx && defined $ccons_idx) {
        my %seen_cons;
        while (my $cline = <$FC>) {
          chomp $cline;
          next if $cline =~ /^\s*$/;
          next if is_repeated_header_line($cline, $chdr);
          my @cf = split /\t/, $cline, -1;
          next if $csample_idx > $#cf || $ccons_idx > $#cf;
          my $csid = ensure_sample_entry(\%sample_metrics, \%sample_label_to_id, \%sample_id_to_label, $cf[$csample_idx]);
          my $ccons_id = trim_text($cf[$ccons_idx]);
          next if $ccons_id eq '' || uc($ccons_id) eq 'NA';
          my $ck = "$csid\t$ccons_id";
          unless ($seen_cons{$ck}) {
            $sample_metrics{$csid}{consensus_consolidated} = 0 unless defined $sample_metrics{$csid}{consensus_consolidated};
            $sample_metrics{$csid}{consensus_consolidated}++;
            $seen_cons{$ck} = 1;
          }
        }
      } else {
        warn_once("missing_column:$opt{blast_consensus_consolidated}:sample_or_consensus_id");
      }
    }
    close $FC;
  } else {
    warn_once("open_failed:$opt{blast_consensus_consolidated}");
  }
}

# otu_total: unique OTU count per sample from cumulative blast (frozen + consolidated + active).
if (defined $opt{blast_otu_cumulative} && $opt{blast_otu_cumulative} ne '' && -s $opt{blast_otu_cumulative}) {
  my %cum_metrics;
  collect_blast_read_fate_and_sample_metrics(
    $opt{blast_otu_cumulative},
    \%cum_metrics,
    \%sample_label_to_id,
    \%sample_id_to_label,
  );
  for my $csid (keys %cum_metrics) {
    next unless defined $cum_metrics{$csid}{otu_active};
    $sample_metrics{$csid}{otu_total} = $cum_metrics{$csid}{otu_active};
  }
}

# consensus_total: emitted (this round) + consolidated (previous rounds cached).
for my $sid (keys %sample_metrics) {
  my $entry = $sample_metrics{$sid};
  if (defined $entry->{consensus_emitted} || defined $entry->{consensus_consolidated}) {
    $entry->{consensus_total} = ($entry->{consensus_emitted} // 0) + ($entry->{consensus_consolidated} // 0);
  }
}

for my $sid (keys %sample_metrics) {
  my $entry = $sample_metrics{$sid};
  if (defined $entry->{reads_demux}) {
    my $label = $entry->{label} // '';
    if (lc($label) eq 'no_adapter') {
      $no_adapter_reads += $entry->{reads_demux};
    }
  }
}

if (defined $opt{demult} && $opt{demult} ne '' && -s $opt{demult}) {
  my $observed_demux_sample_count = 0;
  for my $sid (keys %sample_metrics) {
    my $entry = $sample_metrics{$sid};
    next unless defined $entry->{reads_demux} && $entry->{reads_demux} > 0;
    $observed_demux_sample_count++;
  }
  if ($demux_total_reads > 0) {
    if ($demux_total_reads == $no_adapter_reads && $observed_demux_sample_count == 1) {
      $demux_enabled = JSON::PP::false();
    } else {
      $demux_enabled = JSON::PP::true();
    }
  } else {
    $demux_enabled = JSON::PP::false();
  }
} else {
  $demux_enabled = JSON::PP::false();
}

my $read_fate_demult_path = $opt{demult};
if (defined $opt{read_fate_demult} && $opt{read_fate_demult} ne '' && -e $opt{read_fate_demult} && -s $opt{read_fate_demult}) {
  $read_fate_demult_path = $opt{read_fate_demult};
}
my $read_fate_blast_path = $opt{blast_otu};
if (defined $opt{read_fate_blast} && $opt{read_fate_blast} ne '' && -e $opt{read_fate_blast} && -s $opt{read_fate_blast}) {
  $read_fate_blast_path = $opt{read_fate_blast};
}

my $public_read_fate = build_public_marker_split_read_fate(
  $read_fate_demult_path,
  $read_fate_blast_path,
  $opt{blast_unassigned_ids},
  $reads_total,
  $reads_on_target,
  $opt{read_fate_live_current} ? 1 : 0,
);

# Sample-specific figures: manifest is resolved per sample_id under
# <sample_fig_dir>/<sample_id>/<filename>.
for my $sid (keys %sample_metrics) {
  my $sample_entry = $sample_metrics{$sid};
  my $sample_fig_dir = '';
  my $sample_fig_url_prefix = '';
  if (defined $opt{sample_fig_dir} && $opt{sample_fig_dir} ne '') {
    $sample_fig_dir = "$opt{sample_fig_dir}/$sid";
  }
  if (defined $opt{sample_fig_url_prefix} && $opt{sample_fig_url_prefix} ne '') {
    my $pfx = $opt{sample_fig_url_prefix};
    $pfx =~ s{/\z}{};
    $sample_fig_url_prefix = "$pfx/$sid";
  }
  my @sample_figures = build_figures_from_list(
    $opt{sample_fig_list},
    $sample_fig_dir,
    $sample_fig_url_prefix,
    {
      barcode => $opt{barcode},
      sample_id => $sid,
    },
  );
  $sample_entry->{figures} = \@sample_figures;
}
if ($opt{identity_mode} eq 'track' && %TRACK_UNIT_METRICS) {
  for my $unit_id (keys %TRACK_UNIT_METRICS) {
    my $track_entry = $TRACK_UNIT_METRICS{$unit_id};
    my $replicate_label = $track_entry->{track_replicate_label} // '';
    next if $replicate_label eq '';
    next if !exists $sample_label_to_id{$replicate_label};
    my $sample_sid = $sample_label_to_id{$replicate_label};
    my $sample_entry = $sample_metrics{$sample_sid};
    my @sample_figures = ();
    if (defined $sample_entry && ref($sample_entry->{figures}) eq 'ARRAY') {
      @sample_figures = @{$sample_entry->{figures}};
    }
    $track_entry->{figures} = \@sample_figures;
  }
}

my @figures;
my $prefix = defined($opt{fig_url_prefix}) ? $opt{fig_url_prefix} : 'report_assets';
$prefix =~ s{/\z}{};
my %fig_seen;
my %copied_ok;
if (defined $opt{fig_dir} && $opt{fig_dir} ne '') {
  my $manifest = "$opt{fig_dir}/.copied_manifest.tsv";
  if (-s $manifest) {
    my $MF = open_cached_text_handle($manifest);
    if (defined $MF) {
      while (my $line = <$MF>) {
        chomp $line;
        next if $line =~ /^\s*$/;
        $copied_ok{$line} = 1;
      }
      close $MF;
    }
  }
}
if (defined $opt{fig_list} && $opt{fig_list} ne '' && -s $opt{fig_list}) {
  my $FL = open_cached_text_handle($opt{fig_list});
  if (!defined $FL) {
    warn_once("open_failed:$opt{fig_list}");
  } else {
    while (my $line = <$FL>) {
      chomp $line;
      next if $line =~ /^\s*$/;
      next if $line =~ /^\s*#/;
      my ($id, $pattern, $title, $desc, $section, $order) = split /\t/, $line, 6;
      next unless defined $pattern && $pattern ne '';
      my $filename = $pattern;
      $filename =~ s/\{barcode\}/$opt{barcode}/g;
      my $path = $prefix ne '' ? "$prefix/$filename" : $filename;
      my $pdf_path;
      my $pdf_exists = JSON::PP::false();
      my $exists = 0;
      if (%copied_ok) {
        $exists = $copied_ok{$filename} ? 1 : 0;
      } elsif (defined $opt{fig_dir} && $opt{fig_dir} ne '') {
        my $fs_path = "$opt{fig_dir}/$filename";
        $exists = (-s $fs_path) ? 1 : 0;
      }
      if ($filename =~ /\.png$/i && defined $opt{fig_dir} && $opt{fig_dir} ne '') {
        my $pdf_filename = $filename;
        $pdf_filename =~ s/\.png$/.pdf/i;
        my $pdf_fs_path = "$opt{fig_dir}/$pdf_filename";
        if (-s $pdf_fs_path) {
          $pdf_exists = JSON::PP::true();
          $pdf_path = $prefix ne '' ? "$prefix/$pdf_filename" : $pdf_filename;
        }
      }
      $fig_seen{$filename} = 1;
      $section = defined($section) ? $section : '';
      $section =~ s/^\s+|\s+$//g;
      $order = defined($order) ? $order : '';
      $order =~ s/^\s+|\s+$//g;
      push @figures, {
        id => $id,
        title => (defined $title && $title ne '') ? $title : $id,
        description => (defined $desc && $desc ne '') ? $desc : undef,
        path => $path,
        pdf_path => $pdf_path,
        pdf_exists => $pdf_exists,
        section => ($section ne '') ? $section : 'Other',
        order => ($order ne '' && $order =~ /^\d+$/) ? 0 + $order : 999,
        exists => $exists ? JSON::PP::true() : JSON::PP::false(),
      };
    }
    close $FL;
  }
}
for my $spec (dynamic_run_figure_specs($opt{barcode}, $prefix)) {
  next if $fig_seen{$spec->{filename}}++;
  my $path = $prefix ne '' ? "$prefix/$spec->{filename}" : $spec->{filename};
  my $pdf_path;
  my $pdf_exists = JSON::PP::false();
  my $exists = 0;
  if (%copied_ok) {
    $exists = $copied_ok{$spec->{filename}} ? 1 : 0;
  } elsif (defined $opt{fig_dir} && $opt{fig_dir} ne '') {
    my $fs_path = "$opt{fig_dir}/$spec->{filename}";
    $exists = (-s $fs_path) ? 1 : 0;
  }
  if ($spec->{filename} =~ /\.png$/i && defined $opt{fig_dir} && $opt{fig_dir} ne '') {
    my $pdf_filename = $spec->{filename};
    $pdf_filename =~ s/\.png$/.pdf/i;
    my $pdf_fs_path = "$opt{fig_dir}/$pdf_filename";
    if (-s $pdf_fs_path) {
      $pdf_exists = JSON::PP::true();
      $pdf_path = $prefix ne '' ? "$prefix/$pdf_filename" : $pdf_filename;
    }
  }
  push @figures, {
    id => $spec->{id},
    title => $spec->{title},
    description => $spec->{description},
    path => $path,
    pdf_path => $pdf_path,
    pdf_exists => $pdf_exists,
    section => $spec->{section},
    order => $spec->{order},
    exists => $exists ? JSON::PP::true() : JSON::PP::false(),
  };
}
if (defined $opt{fig_dir} && $opt{fig_dir} ne '') {
  if (opendir my $DH, $opt{fig_dir}) {
    while (my $entry = readdir $DH) {
      next unless $entry =~ /\.png$/i;
      next if $fig_seen{$entry};
      my $fs_path = "$opt{fig_dir}/$entry";
      my $exists = 0;
      if (%copied_ok) {
        $exists = $copied_ok{$entry} ? 1 : 0;
      } else {
        $exists = (-s $fs_path) ? 1 : 0;
      }
      my $title = $entry;
      $title =~ s/\.[^.]+$//;
      $title =~ s/_/ /g;
      my $path = $prefix ne '' ? "$prefix/$entry" : $entry;
      my $pdf_path;
      my $pdf_exists = JSON::PP::false();
      if ($entry =~ /\.png$/i) {
        my $pdf_entry = $entry;
        $pdf_entry =~ s/\.png$/.pdf/i;
        my $pdf_fs_path = "$opt{fig_dir}/$pdf_entry";
        if (-s $pdf_fs_path) {
          $pdf_exists = JSON::PP::true();
          $pdf_path = $prefix ne '' ? "$prefix/$pdf_entry" : $pdf_entry;
        }
      }
      push @figures, {
        id => $entry,
        title => $title,
        description => undef,
        path => $path,
        pdf_path => $pdf_path,
        pdf_exists => $pdf_exists,
        section => 'Other',
        order => 999,
        exists => $exists ? JSON::PP::true() : JSON::PP::false(),
      };
    }
    closedir $DH;
  }
}

# Convert { OTU_id => { collapsed_sample => {rep=>count} } } to
# { OTU_id => { collapsed_sample => [{label,count},...] } } for JSON serialisation.
my %_otu_rep_reads_json;
for my $_otu_key (keys %$otu_rep_reads) {
  for my $_smp (keys %{$otu_rep_reads->{$_otu_key}}) {
    my $_arr = _rep_reads_array($otu_rep_reads->{$_otu_key}{$_smp});
    $_otu_rep_reads_json{$_otu_key}{$_smp} = $_arr if defined $_arr;
  }
}

my $obj = {
  schema_version => $opt{schema_version},
  run_id         => $opt{run_id},
  state_id       => (defined($opt{state_id}) ? $opt{state_id} : undef),
  barcode        => $opt{barcode},
  round_barcode  => $opt{round_barcode},
  timestamp_utc  => $timestamp_utc,
  asset_snapshot_policy => (defined($opt{asset_snapshot_policy}) && $opt{asset_snapshot_policy} ne '' ? $opt{asset_snapshot_policy} : undef),
  round_status   => $round_status,
  failure_reason => $failure_reason,
  failure_stage  => $failure_stage,
  markers => {
    order => \@CONFIGURED_MARKERS,
    target_taxa_by_marker => $CONFIGURED_TARGET_TAX_MAP,
    file_token_by_marker => $CONFIGURED_MARKER_FILE_TOKENS,
    color_by_marker => $CONFIGURED_MARKER_COLORS,
  },
  reads => {
    total     => $reads_total,
    on_target => $reads_on_target,
    hac       => $reads_hac,
    sup       => $reads_sup,
  },
  otu => {
    canonical => {
      active                    => $otu_active,
      consolidated              => $otu_lock_cons,
      frozen_not_consolidated   => $otu_lock_frozen,
      active_not_frozen         => $otu_active_not_frozen,
      informative_dynamic       => $otu_fate_counts{informative_dynamic},
    },
    pruned => {
      prune_candidates   => $otu_fate_counts{prune_candidates},
      size_streak        => $otu_fate_counts{size_streak},
      blast_unassigned   => $otu_fate_counts{blast_unassigned},
    },
    diagnostic => {
      blast_rows_total => (defined($diag_rows_total) ? 0 + $diag_rows_total : undef),
      blast_otu_total  => (defined($diag_otu_total) ? 0 + $diag_otu_total : undef),
      lock_active_not_frozen => (defined($otu_lock_active_raw) ? 0 + $otu_lock_active_raw : undef),
      size_streak_possible => $policy_ctx->{size_possible} ? JSON::PP::true() : JSON::PP::false(),
      size_streak_applied => $policy_ctx->{size_applied} ? JSON::PP::true() : JSON::PP::false(),
      size_streak_grace_active => $policy_ctx->{size_grace_active} ? JSON::PP::true() : JSON::PP::false(),
      size_streak_grace_source => $policy_ctx->{size_grace_source},
      fate_universe_source => $otu_fate_universe_source,
      fate_universe_reason => $otu_fate_universe_reason,
      fate_universe_sizes_status => $otu_fate_universe_sizes_status,
      fate_universe_otu_def_status => $otu_fate_universe_otu_def_status,
      fate_conservation_ok => $otu_fate_conservation_ok,
      fate_conservation_delta => $otu_fate_conservation_delta,
    },
    prune_candidates_round => $prune_candidates_round,
    active_by_marker_taxon => \%otu_active_by_marker_taxon,
    assignments_by_level   => $otu_assignments_by_level,
    (%_otu_rep_reads_json ? (replicate_reads => \%_otu_rep_reads_json) : ()),
  },
  blast => {
    filtered_reads => (defined($blast_f_reads) ? 0 + $blast_f_reads : undef),
    filtered_otus  => (defined($blast_f_otus) ? 0 + $blast_f_otus : undef),
    mode           => $blast_mode,
    missing_policy => $blast_missing_policy,
  },
  consensus => {
    emitted      => $cons_emitted,
    consolidated => $cons_consolidated,
    emitted_by_marker_taxon => \%consensus_emitted_by_marker_taxon,
    assignments_by_level => $consensus_assignments_by_level,
  },
  read_fate => $public_read_fate,
  assignment_thresholds => {
    otu => clone_threshold_tree($otu_assignment_thresholds),
  },
  identity_mode => $opt{identity_mode},
  sample_metrics => \%sample_metrics,
  (%TRACK_UNIT_METRICS ? (track_unit_metrics => \%TRACK_UNIT_METRICS) : ()),
  figures => \@figures,
  warnings => \@warnings,
};

open my $OUT, '>', $opt{out} or die "open $opt{out}: $!";
print {$OUT} encode_json($obj), "\n";
close $OUT;

exit 0;
