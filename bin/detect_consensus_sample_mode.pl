#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;

require "$FindBin::Bin/lib/sample_label.pl";

my $program_name = $ENV{DETECT_CONSENSUS_SAMPLE_MODE_PROG} || $0;

sub usage {
    print STDERR "usage: $program_name <blast_report_annotated.txt> <samples_out> [default_samples]\n";
    exit 2;
}

@ARGV >= 2 && @ARGV <= 3 or usage();

my ($blast_report, $samples_out, $default_samples) = @ARGV;
my $tmp_candidates = "${samples_out}.candidates.tmp.$$";
my $tmp_out = "${samples_out}.tmp.$$";
my $identity_mode = lc(($ENV{RTBIOSCAN_EFFECTIVE_IDENTITY_MODE} || 'collapse'));

END {
    unlink $tmp_candidates if defined $tmp_candidates && -e $tmp_candidates;
    unlink $tmp_out if defined $tmp_out && -e $tmp_out;
}

sub trim_text {
    my ($text) = @_;
    $text = '' unless defined $text;
    $text =~ s/^[[:space:]]+//;
    $text =~ s/[[:space:]]+$//;
    return $text;
}

sub extract_adapter_from_read_id {
    my ($read_id) = @_;
    return undef unless defined $read_id;
    return $1 if $read_id =~ /adapter=([^\s|]+)/;
    return undef;
}

sub is_header_read_id {
    my ($read_id) = @_;
    return 1 if !defined $read_id;
    my $value = trim_text($read_id);
    return 1 if $value eq '';
    return 1 if $value =~ /^#/;
    return 1 if $value eq 'read_id' || $value eq 'seq_id' || $value eq 'long_read_id';
    return 0;
}

sub is_no_adapter_adapter {
    my ($adapter) = @_;
    return SampleLabel::is_no_adapter_label($adapter) ? 1 : 0;
}

sub normalize_sample_base {
    my ($sample) = @_;
    my $normalized = SampleLabel::normalize_sample_base($sample);
    return undef if !defined($normalized) || $normalized eq '';
    return $normalized;
}

sub valid_barcoded_sample {
    my ($adapter) = @_;
    return undef if is_no_adapter_adapter($adapter);
    my $normalized = normalize_sample_base($adapter);
    return undef if !defined($normalized) || $normalized eq '' || $normalized eq 'no_adapter';
    return $normalized;
}

sub append_normalized_sample {
    my ($fh, $raw) = @_;
    my $normalized = valid_barcoded_sample($raw);
    return 0 unless defined $normalized;
    print {$fh} "$normalized\n" or die "failed to write candidate sample: $!\n";
    return 1;
}

sub read_nonempty_lines {
    my ($path) = @_;
    open my $fh, '<', $path or die "failed to read $path: $!\n";
    my @lines;
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        push @lines, $line;
    }
    close $fh or die "failed to close $path: $!\n";
    return @lines;
}

sub read_trimmed_nonempty_lines {
    my ($path) = @_;
    open my $fh, '<', $path or die "failed to read $path: $!\n";
    my @lines;
    while (my $line = <$fh>) {
        chomp $line;
        $line = trim_text($line);
        next if $line eq '';
        push @lines, $line;
    }
    close $fh or die "failed to close $path: $!\n";
    return @lines;
}

sub write_lines_atomic {
    my ($path, @lines) = @_;
    open my $out_fh, '>', $tmp_out or die "failed to write $tmp_out: $!\n";
    for my $line (@lines) {
        print {$out_fh} "$line\n" or die "failed to write $tmp_out: $!\n";
    }
    close $out_fh or die "failed to close $tmp_out: $!\n";
    rename $tmp_out, $path or die "failed to rename $tmp_out to $path: $!\n";
}

sub write_sorted_unique_candidates {
    my ($path, $append_no_adapter) = @_;
    my %seen;
    for my $line (read_nonempty_lines($tmp_candidates)) {
        $seen{$line} = 1;
    }
    my @lines = sort { $a cmp $b } keys %seen;
    push @lines, 'no_adapter' if $append_no_adapter;
    write_lines_atomic($path, @lines);
}

if ($identity_mode eq 'track') {
    die "track mode requires track_active_units.txt path\n"
        if !defined($default_samples) || $default_samples eq '';
    die "track mode requires readable track_active_units.txt: $default_samples\n"
        if !-f $default_samples || !-r $default_samples;
    my @track_units = read_trimmed_nonempty_lines($default_samples);

    my %active_track_unit = ();
    for my $track_unit (@track_units) {
        if (is_no_adapter_adapter($track_unit)) {
            die "track mode track_active_units.txt must not contain no_adapter entry: $track_unit\n";
        }
        $active_track_unit{$track_unit} = 1;
    }

    my $observed_no_adapter = 0;
    if (-s $blast_report) {
        open my $blast_fh, '<', $blast_report or die "failed to read $blast_report: $!\n";
        while (my $line = <$blast_fh>) {
            chomp $line;
            next if $line =~ /^\s*$/;
            my ($read_id) = split /\t/, $line, 2;
            next if is_header_read_id($read_id);
            my $adapter = extract_adapter_from_read_id($read_id);
            die "track mode requires adapter= token in blast row: $read_id\n"
                if !defined($adapter) || trim_text($adapter) eq '';
            if (is_no_adapter_adapter($adapter)) {
                $observed_no_adapter = 1;
                next;
            }
            die "track mode observed adapter not present in track_active_units.txt: $adapter\n"
                if !$active_track_unit{$adapter};
        }
        close $blast_fh or die "failed to close $blast_report: $!\n";
    }

    my @output_lines = @track_units;
    if ($observed_no_adapter) {
        push @output_lines, 'no_adapter';
    }
    if (!@output_lines) {
        @output_lines = ('no_adapter');
    }
    write_lines_atomic($samples_out, @output_lines);

    my $mode = 'barcoded';
    my $source = 'default_samples';
    if (!@track_units) {
        $mode = 'no_adapter_only';
        $source = 'default_no_adapter';
    }

    print "mode\t$mode\n";
    print "source\t$source\n";
    print "observed_no_adapter\t$observed_no_adapter\n";
    exit 0;
}

open my $tmp_fh, '>', $tmp_candidates or die "failed to write $tmp_candidates: $!\n";

my $observed_barcoded = 0;
my $observed_no_adapter = 0;

if (-s $blast_report) {
    open my $blast_fh, '<', $blast_report or die "failed to read $blast_report: $!\n";
    while (my $line = <$blast_fh>) {
        chomp $line;
        next if $line eq '';
        my ($read_id) = split /\t/, $line, 2;
        next if is_header_read_id($read_id);
        my $adapter = extract_adapter_from_read_id($read_id);
        next if !defined($adapter) || $adapter eq '';
        if (is_no_adapter_adapter($adapter)) {
            $observed_no_adapter = 1;
            next;
        }
        if (append_normalized_sample($tmp_fh, $adapter)) {
            $observed_barcoded = 1;
        }
    }
    close $blast_fh or die "failed to close $blast_report: $!\n";
}

close $tmp_fh or die "failed to close $tmp_candidates: $!\n";

my $mode = 'no_adapter_only';
my $source = 'default_no_adapter';

if ($observed_barcoded) {
    write_sorted_unique_candidates($samples_out, $observed_no_adapter);
    $mode = 'barcoded';
    $source = 'observed_adapters';
} elsif ($observed_no_adapter) {
    write_lines_atomic($samples_out, 'no_adapter');
    $mode = 'no_adapter_only';
    $source = 'observed_no_adapter';
} elsif (defined($default_samples) && $default_samples ne '' && -s $default_samples) {
    open my $defaults_fh, '<', $default_samples or die "failed to read $default_samples: $!\n";
    open my $defaults_tmp_fh, '>', $tmp_candidates or die "failed to rewrite $tmp_candidates: $!\n";
    while (my $sample = <$defaults_fh>) {
        chomp $sample;
        next if $sample eq '';
        append_normalized_sample($defaults_tmp_fh, $sample);
    }
    close $defaults_tmp_fh or die "failed to close $tmp_candidates: $!\n";
    close $defaults_fh or die "failed to close $default_samples: $!\n";
    if (-s $tmp_candidates) {
        write_sorted_unique_candidates($samples_out, 0);
        $mode = 'barcoded_fallback';
        $source = 'default_samples';
    } else {
        write_lines_atomic($samples_out, 'no_adapter');
    }
} else {
    write_lines_atomic($samples_out, 'no_adapter');
}

print "mode\t$mode\n";
print "source\t$source\n";
print "observed_no_adapter\t$observed_no_adapter\n";
