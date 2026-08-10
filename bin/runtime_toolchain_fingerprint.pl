#!/usr/bin/env perl

use strict;
use warnings;

use Cwd qw(abs_path);
use Digest::SHA qw(sha256_hex);
use File::Basename qw(dirname basename);
use File::Find qw(find);
use File::Spec;
use File::Temp qw(tempfile);
use Getopt::Long qw(GetOptions);

my %opt = (
    runtime_backend        => '',
    runtime_lock_manifest  => '',
    dorado_release_manifest => '',
    dorado_summary_bin      => '',
    dorado_input_mode       => 'file',
    dorado_input_compat_helper => '',
);
my @dorado_model;
my @dorado_args;

GetOptions(
    'policy-manifest=s'        => \$opt{policy_manifest},
    'runtime-backend=s'        => \$opt{runtime_backend},
    'runtime-lock-manifest=s'  => \$opt{runtime_lock_manifest},
    'dorado-bin=s'             => \$opt{dorado_bin},
    'dorado-summary-bin=s'     => \$opt{dorado_summary_bin},
    'dorado-input-mode=s'      => \$opt{dorado_input_mode},
    'dorado-input-compat-helper=s' => \$opt{dorado_input_compat_helper},
    'dorado-release-manifest=s' => \$opt{dorado_release_manifest},
    'dorado-model=s@'          => \@dorado_model,
    'dorado-device=s'          => \$opt{dorado_device},
    'dorado-args=s@'           => \@dorado_args,
    'output=s'                 => \$opt{output},
) or die "ERROR: invalid runtime toolchain fingerprint options\n";

for my $required (qw(policy_manifest dorado_bin dorado_device output)) {
    die "ERROR: --" . ($required =~ s/_/-/gr) . " is required\n"
        if !defined $opt{$required} || $opt{$required} eq '';
}
die "ERROR: --runtime-backend must be host or conda\n"
    if $opt{runtime_backend} ne 'host' && $opt{runtime_backend} ne 'conda';
die "ERROR: --runtime-lock-manifest is required for the conda backend\n"
    if $opt{runtime_backend} eq 'conda' && $opt{runtime_lock_manifest} eq '';
die "ERROR: --runtime-lock-manifest is only valid for the conda backend\n"
    if $opt{runtime_backend} ne 'conda' && $opt{runtime_lock_manifest} ne '';
die "ERROR: --dorado-input-mode must be file or directory\n"
    if $opt{dorado_input_mode} ne 'file' && $opt{dorado_input_mode} ne 'directory';
die "ERROR: --dorado-input-compat-helper is required for directory input mode\n"
    if $opt{dorado_input_mode} eq 'directory' && $opt{dorado_input_compat_helper} eq '';
die "ERROR: --dorado-input-compat-helper is only valid for directory input mode\n"
    if $opt{dorado_input_mode} ne 'directory' && $opt{dorado_input_compat_helper} ne '';

sub trim {
    my ($value) = @_;
    $value = '' if !defined $value;
    $value =~ s/^\s+//;
    $value =~ s/\s+$//;
    return $value;
}

sub file_sha256 {
    my ($path) = @_;
    open(my $fh, '<:raw', $path) or die "ERROR: cannot read '$path': $!\n";
    my $sha = Digest::SHA->new(256);
    $sha->addfile($fh);
    close($fh) or die "ERROR: cannot close '$path': $!\n";
    return $sha->hexdigest;
}

sub safe_relative_path {
    my ($path) = @_;
    return 0 if !defined $path || $path eq '' || File::Spec->file_name_is_absolute($path);
    return 0 if $path =~ m{(?:\A|/)\.\.(?:/|\z)};
    return 0 if $path =~ m{//} || $path =~ m{\A\./};
    return $path =~ m{\A[A-Za-z0-9_.@+/-]+\z} ? 1 : 0;
}

sub directory_content_sha256 {
    my ($root) = @_;
    my @files;
    find(
        {
            no_chdir => 1,
            wanted   => sub {
                my $path = $File::Find::name;
                die "ERROR: symlink is not allowed in an unqualified Dorado model: $path\n"
                    if -l $path;
                push @files, $path if -f $path;
            },
        },
        $root,
    );
    die "ERROR: Dorado model directory contains no files: $root\n" if !@files;
    my $canonical = '';
    for my $path (sort @files) {
        my $relative = File::Spec->abs2rel($path, $root);
        $relative =~ s{\\}{/}g;
        die "ERROR: tab/newline is not allowed in Dorado model paths\n"
            if $relative =~ /[\t\r\n]/;
        $canonical .= "$relative\t" . file_sha256($path) . "\n";
    }
    return sha256_hex($canonical);
}

sub require_regular_file {
    my ($path, $label) = @_;
    my $absolute = abs_path($path);
    die "ERROR: $label is not a regular non-symlink file: $path\n"
        if !defined $absolute || !-f $absolute || -l $path;
    return $absolute;
}

sub resolve_executable {
    my ($name) = @_;
    if ($name =~ m{/}) {
        my $absolute = abs_path($name);
        die "ERROR: runtime executable not found: $name\n"
            if !defined $absolute || !-f $absolute;
        die "ERROR: runtime executable is not executable: $absolute\n" if !-x $absolute;
        return $absolute;
    }
    for my $dir (split /:/, ($ENV{PATH} // '')) {
        $dir = '.' if $dir eq '';
        my $candidate = File::Spec->catfile($dir, $name);
        next if !-f $candidate || !-x $candidate;
        my $absolute = abs_path($candidate);
        return $absolute if defined $absolute;
    }
    die "ERROR: required runtime tool not found or not executable: $name\n";
}

sub run_capture {
    my (@command) = @_;
    my ($out_fh, $out_path) = tempfile('rtbioscan-tool-out-XXXXXX', TMPDIR => 1, UNLINK => 1);
    my ($err_fh, $err_path) = tempfile('rtbioscan-tool-err-XXXXXX', TMPDIR => 1, UNLINK => 1);
    close($out_fh);
    close($err_fh);
    my $pid = fork();
    die "ERROR: cannot fork runtime version probe: $!\n" if !defined $pid;
    if ($pid == 0) {
        open(STDOUT, '>', $out_path) or die "ERROR: cannot redirect tool output: $!\n";
        open(STDERR, '>', $err_path) or die "ERROR: cannot redirect tool errors: $!\n";
        exec { $command[0] } @command;
        die "ERROR: cannot execute '$command[0]': $!\n";
    }
    waitpid($pid, 0);
    my $status = $?;
    open(my $read_out, '<', $out_path) or die "ERROR: cannot read tool output: $!\n";
    open(my $read_err, '<', $err_path) or die "ERROR: cannot read tool error output: $!\n";
    local $/;
    my $output = (<$read_out> // '') . (<$read_err> // '');
    close($read_out);
    close($read_err);
    return ($status, $output);
}

sub read_policy_manifest {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "ERROR: cannot read toolchain policy '$path': $!\n";
    my $header = <$fh>;
    die "ERROR: toolchain policy '$path' is empty\n" if !defined $header;
    chomp $header;
    die "ERROR: toolchain policy must start with kind<TAB>name<TAB>expected_version\n"
        if $header ne "kind\tname\texpected_version";
    my %policy;
    while (my $line = <$fh>) {
        chomp $line;
        next if $line eq '';
        my ($kind, $name, $version, @extra) = split /\t/, $line, -1;
        die "ERROR: malformed toolchain policy row: $line\n"
            if !defined $kind || ($kind ne 'tool' && $kind ne 'r_package' && $kind ne 'dorado')
            || !defined $name || $name !~ /\A[A-Za-z0-9_.+-]+\z/
            || !defined $version || $version !~ /\A[A-Za-z0-9_.+-]+\z/
            || @extra;
        my $key = "$kind:$name";
        die "ERROR: duplicate toolchain policy entry: $key\n" if exists $policy{$key};
        $policy{$key} = $version;
    }
    close($fh);
    return \%policy;
}

sub parse_named_values {
    my ($values_ref, $label, $required_ref) = @_;
    my %result;
    for my $value (@{$values_ref}) {
        my ($name, $payload) = split /=/, $value, 2;
        die "ERROR: malformed --$label value '$value'; expected NAME=VALUE\n"
            if !defined $name || $name !~ /\A[A-Za-z0-9_]+\z/ || !defined $payload || $payload eq '';
        die "ERROR: duplicate --$label name '$name'\n" if exists $result{$name};
        $result{$name} = $payload;
    }
    for my $name (@{$required_ref}) {
        die "ERROR: missing --$label $name=VALUE\n" if !exists $result{$name};
    }
    return \%result;
}

sub read_dorado_release_manifest {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "ERROR: cannot read Dorado release manifest '$path': $!\n";
    my %meta;
    my %entry;
    my $header_seen = 0;
    while (my $line = <$fh>) {
        chomp $line;
        if (!$header_seen && $line =~ /\A# ([a-z_]+)=(.+)\z/) {
            die "ERROR: duplicate Dorado release metadata key '$1'\n"
                if exists $meta{$1};
            $meta{$1} = $2;
            next;
        }
        next if !$header_seen && $line eq '';
        if (!$header_seen) {
            die "ERROR: malformed Dorado release manifest header\n"
                if $line ne "kind\tartifact\tsha256\tbytes\tmode\trole";
            $header_seen = 1;
            next;
        }
        next if $line eq '';
        my ($kind, $artifact, $sha, $bytes, $mode, $role, @extra) = split /\t/, $line, -1;
        die "ERROR: malformed Dorado release manifest row: $line\n"
            if !defined $kind || ($kind ne 'archive' && $kind ne 'runtime' && $kind ne 'model')
            || !safe_relative_path($artifact)
            || !defined $sha || $sha !~ /\A[0-9a-f]{64}\z/
            || !defined $bytes || $bytes !~ /\A[0-9]+\z/
            || !defined $mode || ($mode ne '-' && $mode !~ /\A0[0-7]{3}\z/)
            || !defined $role || $role eq '' || @extra;
        die "ERROR: duplicate Dorado release artifact: $artifact\n"
            if exists $entry{$artifact};
        die "ERROR: archive artifact must be a basename: $artifact\n"
            if $kind eq 'archive' && $artifact =~ m{/};
        die "ERROR: model artifact must be installed below models/: $artifact\n"
            if $kind eq 'model' && $artifact !~ m{\Amodels/};
        $entry{$artifact} = {
            kind   => $kind,
            sha256 => $sha,
            bytes  => $bytes,
            mode   => $mode,
            role   => $role,
        };
    }
    close($fh);
    die "ERROR: Dorado release manifest has no artifact header\n" if !$header_seen;
    for my $required (qw(release_id platform expected_version source_url)) {
        die "ERROR: Dorado release manifest is missing metadata '$required'\n"
            if !defined $meta{$required} || $meta{$required} eq '';
    }
    my @archives = grep { $entry{$_}{kind} eq 'archive' } keys %entry;
    die "ERROR: Dorado release manifest must contain exactly one archive row\n"
        if @archives != 1;
    die "ERROR: Dorado release manifest is missing runtime artifact 'bin/dorado'\n"
        if !exists $entry{'bin/dorado'} || $entry{'bin/dorado'}{kind} ne 'runtime';
    return (\%meta, \%entry);
}

sub verify_qualified_release_artifact {
    my ($release_root, $artifact, $entry_ref) = @_;
    my $path = File::Spec->catfile($release_root, split m{/}, $artifact);
    my $absolute = abs_path($path);
    die "ERROR: qualified Dorado release artifact is not a regular non-symlink file: $artifact\n"
        if !defined $absolute || !-f $absolute || -l $path;
    die "ERROR: qualified Dorado release artifact resolves outside its declared layout: $artifact\n"
        if $absolute ne $path;
    my $actual_bytes = -s $absolute;
    die "ERROR: qualified Dorado release artifact size mismatch for '$artifact': "
        . "manifest=$entry_ref->{bytes} actual=$actual_bytes\n"
        if $actual_bytes != $entry_ref->{bytes};
    my $actual_sha = file_sha256($absolute);
    die "ERROR: qualified Dorado release artifact checksum mismatch for '$artifact': "
        . "manifest=$entry_ref->{sha256} actual=$actual_sha\n"
        if $actual_sha ne $entry_ref->{sha256};
}

my $policy_path = require_regular_file($opt{policy_manifest}, 'toolchain policy manifest');
my $policy_ref = read_policy_manifest($policy_path);
my @tool_names = qw(blastn lastal taxonkit seqkit cutadapt vsearch cd-hit-est samtools seqtk);
for my $required (
    (map { "tool:$_" } @tool_names),
    'tool:Rscript',
    'r_package:DECIPHER',
    'r_package:Biostrings',
    'dorado:dorado',
) {
    die "ERROR: toolchain policy is missing '$required'\n" if !exists $policy_ref->{$required};
}

my %fingerprint = (
    fingerprint_schema_version    => '1',
    runtime_backend               => $opt{runtime_backend},
    policy_manifest_sha256        => file_sha256($policy_path),
);

if ($opt{runtime_lock_manifest} ne '') {
    my $lock_path = require_regular_file($opt{runtime_lock_manifest}, 'runtime lock manifest');
    $fingerprint{runtime_lock_manifest_sha256} = file_sha256($lock_path);
} else {
    $fingerprint{runtime_lock_manifest_sha256} = '';
}

my $dorado_bin = resolve_executable($opt{dorado_bin});
my $dorado_summary_bin = $opt{dorado_summary_bin} eq ''
    ? $dorado_bin
    : resolve_executable($opt{dorado_summary_bin});
my (
    $qualified_manifest_path,
    $qualified_release_meta_ref,
    $qualified_release_entries_ref,
    $qualified_release_root,
);
if ($opt{dorado_release_manifest} ne '') {
    $qualified_manifest_path = require_regular_file(
        $opt{dorado_release_manifest}, 'Dorado release manifest'
    );
    ($qualified_release_meta_ref, $qualified_release_entries_ref) =
        read_dorado_release_manifest($qualified_manifest_path);
    $qualified_release_root = dirname(dirname($dorado_bin));
    my $expected_binary = File::Spec->catfile(
        $qualified_release_root, 'bin', 'dorado'
    );
    die "ERROR: selected Dorado binary is not in the qualified release layout\n"
        if $expected_binary ne $dorado_bin;
    for my $artifact (sort keys %{$qualified_release_entries_ref}) {
        next if $qualified_release_entries_ref->{$artifact}{kind} eq 'archive';
        verify_qualified_release_artifact(
            $qualified_release_root,
            $artifact,
            $qualified_release_entries_ref->{$artifact},
        );
    }
}
my %resolved_tool = map { $_ => resolve_executable($_) } @tool_names;
my $rscript = resolve_executable('Rscript');
my $probe_script = require_regular_file(
    File::Spec->catfile(dirname(abs_path($0)), 'runtime_toolchain_probe.sh'),
    'runtime toolchain probe'
);
my ($probe_status, $probe_output) = run_capture(
    $probe_script,
    @resolved_tool{@tool_names},
    $rscript,
    $dorado_bin,
);
die "ERROR: runtime toolchain version probe failed: " . trim($probe_output) . "\n"
    if $probe_status != 0;
my %probed;
for my $line (split /\r?\n/, $probe_output) {
    next if $line eq '';
    my ($key, $value, @extra) = split /\t/, $line, -1;
    die "ERROR: malformed runtime toolchain probe output: $line\n"
        if !defined $key || !defined $value || @extra || exists $probed{$key};
    $probed{$key} = $value;
}
for my $name (@tool_names, 'Rscript') {
    my $key = "tool_$name";
    die "ERROR: runtime toolchain probe did not report $key\n" if !exists $probed{$key};
    my $expected = $policy_ref->{"tool:$name"};
    die "ERROR: $name version mismatch: expected $expected, found $probed{$key}\n"
        if $probed{$key} ne $expected;
    $fingerprint{"${key}_version"} = $probed{$key};
}
for my $package (qw(DECIPHER Biostrings)) {
    my $key = "r_package_$package";
    die "ERROR: runtime toolchain probe did not report $key\n" if !exists $probed{$key};
    my $expected = $policy_ref->{"r_package:$package"};
    die "ERROR: R package $package version mismatch: expected $expected, found $probed{$key}\n"
        if $probed{$key} ne $expected;
    $fingerprint{"${key}_version"} = $probed{$key};
}
my $dorado_version = $probed{dorado_dorado};
die "ERROR: runtime toolchain probe did not report dorado_dorado\n"
    if !defined $dorado_version;
my $expected_dorado = $policy_ref->{'dorado:dorado'};
die "ERROR: Dorado version mismatch: expected $expected_dorado, found $dorado_version\n"
    if $dorado_version ne $expected_dorado;
$fingerprint{dorado_version} = $dorado_version;
$fingerprint{dorado_device} = trim($opt{dorado_device});
if ($opt{dorado_release_manifest} eq '') {
    $fingerprint{dorado_binary_sha256} = file_sha256($dorado_bin);
}
if ($dorado_summary_bin ne $dorado_bin) {
    my ($summary_status, $summary_output) =
        run_capture($dorado_summary_bin, '--version');
    die "ERROR: Dorado summary version probe failed: " . trim($summary_output) . "\n"
        if $summary_status != 0;
    my ($summary_version) =
        trim($summary_output) =~ /(\d+\.\d+\.\d+(?:[+][A-Za-z0-9._-]+)?)/;
    die "ERROR: Dorado summary version could not be parsed\n"
        if !defined $summary_version;
    my $expected_summary = $policy_ref->{'dorado:summary'};
    die "ERROR: toolchain policy must declare dorado:summary when a separate summary binary is selected\n"
        if !defined $expected_summary;
    die "ERROR: Dorado summary version mismatch: expected $expected_summary, found $summary_version\n"
        if $summary_version ne $expected_summary;
    $fingerprint{dorado_summary_version} = $summary_version;
    $fingerprint{dorado_summary_binary_sha256} = file_sha256($dorado_summary_bin);
}
if ($opt{dorado_input_mode} eq 'directory') {
    my $compat_helper = require_regular_file(
        $opt{dorado_input_compat_helper}, 'Dorado input compatibility helper'
    );
    die "ERROR: Dorado input compatibility helper is not executable: $compat_helper\n"
        if !-x $compat_helper;
    $fingerprint{dorado_input_mode} = 'directory';
    $fingerprint{dorado_input_compat_helper_sha256} = file_sha256($compat_helper);
}

my $model_ref = parse_named_values(\@dorado_model, 'dorado-model', [qw(fast hac sup)]);
my $args_ref = parse_named_values(\@dorado_args, 'dorado-args', [qw(fast hac sup)]);
my %model_path;
for my $stage (qw(fast hac sup)) {
    my $path = abs_path($model_ref->{$stage});
    die "ERROR: Dorado $stage model directory not found: $model_ref->{$stage}\n"
        if !defined $path || !-d $path || -l $model_ref->{$stage};
    my $config = require_regular_file(File::Spec->catfile($path, 'config.toml'), "Dorado $stage model config");
    $model_path{$stage} = $path;
    $fingerprint{"dorado_${stage}_model"} = basename($path);
    $fingerprint{"dorado_${stage}_model_config_sha256"} = file_sha256($config);
    $fingerprint{"dorado_${stage}_args"} = trim($args_ref->{$stage});
}

if ($opt{dorado_release_manifest} ne '') {
    my $manifest_path = $qualified_manifest_path;
    my $release_meta_ref = $qualified_release_meta_ref;
    my $release_entries_ref = $qualified_release_entries_ref;
    my $release_root = $qualified_release_root;
    die "ERROR: selected Dorado version does not match release manifest: "
        . "$dorado_version != $release_meta_ref->{expected_version}\n"
        if $dorado_version ne $release_meta_ref->{expected_version};
    $fingerprint{dorado_binary_sha256} =
        $release_entries_ref->{'bin/dorado'}{sha256};
    for my $stage (qw(fast hac sup)) {
        my $artifact = "models/$fingerprint{\"dorado_${stage}_model\"}/config.toml";
        my $expected_model = abs_path(
            File::Spec->catdir(
                $release_root,
                'models',
                $fingerprint{"dorado_${stage}_model"},
            )
        );
        die "ERROR: selected Dorado $stage model is outside the qualified release layout\n"
            if !defined $expected_model || $expected_model ne $model_path{$stage};
        die "ERROR: selected Dorado $stage model is not declared by the release manifest\n"
            if !exists $release_entries_ref->{$artifact}
            || $release_entries_ref->{$artifact}{kind} ne 'model';
        die "ERROR: selected Dorado $stage model config does not match the release manifest\n"
            if $fingerprint{"dorado_${stage}_model_config_sha256"}
                ne $release_entries_ref->{$artifact}{sha256};
        my $model_prefix = "models/$fingerprint{\"dorado_${stage}_model\"}/";
        my @declared_model_artifacts =
            sort grep { index($_, $model_prefix) == 0 } keys %{$release_entries_ref};
        die "ERROR: Dorado release manifest declares no files for the selected $stage model\n"
            if !@declared_model_artifacts;
        my $model_canonical = '';
        for my $declared (@declared_model_artifacts) {
            $model_canonical .=
                "$declared\t$release_entries_ref->{$declared}{sha256}\n";
        }
        $fingerprint{"dorado_${stage}_model_content_sha256"} =
            sha256_hex($model_canonical);
    }
    $fingerprint{dorado_release_status} = 'qualified_manifest';
    $fingerprint{dorado_release_id} = $release_meta_ref->{release_id};
    $fingerprint{dorado_release_platform} = $release_meta_ref->{platform};
    $fingerprint{dorado_release_manifest_sha256} = file_sha256($manifest_path);
} else {
    warn "WARNING: no Dorado release manifest was supplied; binding the explicit binary "
        . "and model configurations as an unqualified runtime\n";
    $fingerprint{dorado_release_status} = 'unqualified_explicit';
    $fingerprint{dorado_release_id} = '';
    $fingerprint{dorado_release_platform} = '';
    $fingerprint{dorado_release_manifest_sha256} = '';
    for my $stage (qw(fast hac sup)) {
        $fingerprint{"dorado_${stage}_model_content_sha256"} =
            directory_content_sha256($model_path{$stage});
    }
}

for my $key (keys %fingerprint) {
    die "ERROR: invalid tab/newline in toolchain fingerprint field '$key'\n"
        if $key =~ /[\t\r\n]/ || $fingerprint{$key} =~ /[\t\r\n]/;
}
my $canonical = join('', map { "$_\t$fingerprint{$_}\n" } sort keys %fingerprint);
my $fingerprint_id = sha256_hex($canonical);
my $output = File::Spec->rel2abs($opt{output});
my $tmp = "$output.tmp.$$";
open(my $fh, '>', $tmp) or die "ERROR: cannot write toolchain fingerprint '$tmp': $!\n";
print {$fh} $canonical;
print {$fh} "fingerprint_id\t$fingerprint_id\n";
close($fh) or die "ERROR: cannot close toolchain fingerprint '$tmp': $!\n";
rename($tmp, $output) or die "ERROR: cannot install toolchain fingerprint '$output': $!\n";
print "$fingerprint_id\n";
