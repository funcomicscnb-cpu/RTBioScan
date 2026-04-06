#!/usr/bin/perl

package ReportingParserStateTxn;

use strict;
use warnings;
use File::Path qw(make_path remove_tree);
use File::Basename qw(dirname);

my $THIS_DIR = dirname(__FILE__);
require "$THIS_DIR/reporting_contract_sidecar.pl";

our @MANIFEST_KEYS = (
	'transaction_version',
	'barcode',
	'demult_report_live',
	'demult_report_tmp',
	'demult_report_bak',
	'demult_sidecar_live',
	'demult_sidecar_tmp',
	'demult_sidecar_bak',
	'otu_report_live',
	'otu_report_tmp',
	'otu_report_bak',
	'otu_sidecar_live',
	'otu_sidecar_tmp',
	'otu_sidecar_bak',
	'bootstrap_sentinel_path',
	'bootstrap_sentinel_in_transaction',
	'publish_phase',
);

our %VALID_PHASE = map { $_ => 1 } qw(prepared staged backed_up published_pairs published_sentinel committed);

sub canonical_paths {
	my (%args) = @_;
	my $state_dir = $args{state_dir} // die "ERROR: missing state_dir\n";
	my $barcode = $args{barcode} // die "ERROR: missing barcode\n";
	my $tx_root = "$state_dir/.parser_state_txn/$barcode";
	my $work_root = "$tx_root/work";
	my $tx_tmp_dir = "$work_root/tmp";
	my $tx_bak_dir = "$work_root/bak";
	return {
		state_dir => $state_dir,
		barcode => $barcode,
		tx_root => $tx_root,
		work_root => $work_root,
		tx_tmp_dir => $tx_tmp_dir,
		tx_bak_dir => $tx_bak_dir,
		manifest_path => "$tx_root/parser_state_boundary_commit.manifest.tsv",
		manifest_tmp_path => "$tx_root/parser_state_boundary_commit.manifest.tsv.tmp",
		demult_report_live => "$state_dir/${barcode}_demult_rpt.txt",
		demult_report_tmp => "$tx_tmp_dir/${barcode}_demult_rpt.txt",
		demult_report_bak => "$tx_bak_dir/${barcode}_demult_rpt.txt",
		demult_sidecar_live => "$state_dir/${barcode}_demult_rpt.contract.tsv",
		demult_sidecar_tmp => "$tx_tmp_dir/${barcode}_demult_rpt.contract.tsv",
		demult_sidecar_bak => "$tx_bak_dir/${barcode}_demult_rpt.contract.tsv",
		otu_report_live => "$state_dir/${barcode}_otu_def_rpt.txt",
		otu_report_tmp => "$tx_tmp_dir/${barcode}_otu_def_rpt.txt",
		otu_report_bak => "$tx_bak_dir/${barcode}_otu_def_rpt.txt",
		otu_sidecar_live => "$state_dir/${barcode}_otu_def_rpt.contract.tsv",
		otu_sidecar_tmp => "$tx_tmp_dir/${barcode}_otu_def_rpt.contract.tsv",
		otu_sidecar_bak => "$tx_bak_dir/${barcode}_otu_def_rpt.contract.tsv",
		bootstrap_sentinel_path => "$state_dir/${barcode}_demult_bootstrap.seeded",
		bootstrap_sentinel_tmp => "$tx_tmp_dir/${barcode}_demult_bootstrap.seeded",
	};
}

sub _ensure_dir {
	my ($dir) = @_;
	return if -d $dir;
	make_path($dir) or die "ERROR: unable to create directory '$dir'\n";
}

sub prepare_transaction_workspace {
	my (%args) = @_;
	my $paths = $args{paths} // die "ERROR: missing paths\n";
	my $bootstrap_in_transaction = $args{bootstrap_sentinel_in_transaction} ? 1 : 0;
	my $barcode = $paths->{barcode} // die "ERROR: missing barcode in paths\n";
	_ensure_dir($paths->{tx_root});
	die "ERROR: parser-state transaction manifest already exists before prepare\n"
		if -e $paths->{manifest_path};
	die "ERROR: parser-state transaction manifest temp already exists before prepare\n"
		if -e $paths->{manifest_tmp_path};
	my %manifest = (
		transaction_version => '1',
		barcode => $barcode,
		demult_report_live => $paths->{demult_report_live},
		demult_report_tmp => $paths->{demult_report_tmp},
		demult_report_bak => $paths->{demult_report_bak},
		demult_sidecar_live => $paths->{demult_sidecar_live},
		demult_sidecar_tmp => $paths->{demult_sidecar_tmp},
		demult_sidecar_bak => $paths->{demult_sidecar_bak},
		otu_report_live => $paths->{otu_report_live},
		otu_report_tmp => $paths->{otu_report_tmp},
		otu_report_bak => $paths->{otu_report_bak},
		otu_sidecar_live => $paths->{otu_sidecar_live},
		otu_sidecar_tmp => $paths->{otu_sidecar_tmp},
		otu_sidecar_bak => $paths->{otu_sidecar_bak},
		bootstrap_sentinel_path => $paths->{bootstrap_sentinel_path},
		bootstrap_sentinel_in_transaction => $bootstrap_in_transaction ? 'true' : 'false',
		publish_phase => 'prepared',
	);
	_write_manifest($paths, \%manifest);
	_ensure_dir($paths->{tx_tmp_dir});
	_ensure_dir($paths->{tx_bak_dir});
}

sub _atomic_write_lines {
	my ($path, $lines_ref) = @_;
	my $tmp = "$path.tmp.$$";
	open my $fh, '>', $tmp or die "ERROR: unable to write '$tmp'\n";
	for my $line (@{$lines_ref}) {
		print {$fh} $line;
	}
	close $fh or die "ERROR: unable to close '$tmp'\n";
	rename $tmp, $path or die "ERROR: unable to replace '$path'\n";
}

sub _write_manifest {
	my ($paths, $meta) = @_;
	my @lines = ();
	for my $key (@MANIFEST_KEYS) {
		die "ERROR: manifest missing key '$key'\n" if !exists $meta->{$key};
		my $value = defined $meta->{$key} ? $meta->{$key} : '';
		push @lines, "$key\t$value\n";
	}
	open my $fh, '>', $paths->{manifest_tmp_path} or die "ERROR: unable to write '$paths->{manifest_tmp_path}'\n";
	for my $line (@lines) {
		print {$fh} $line;
	}
	close $fh or die "ERROR: unable to close '$paths->{manifest_tmp_path}'\n";
	rename $paths->{manifest_tmp_path}, $paths->{manifest_path}
		or die "ERROR: unable to replace '$paths->{manifest_path}'\n";
}

sub _read_manifest_partial {
	my ($manifest_path) = @_;
	open my $fh, '<', $manifest_path or die "ERROR: unable to open manifest '$manifest_path'\n";
	my %meta;
	my $malformed = 0;
	while (my $line = <$fh>) {
		chomp $line;
		$line =~ s/\r$//;
		next if $line eq '';
		if ($line !~ /\A([^\t]+)\t([^\t]*)\z/) {
			$malformed = 1;
			next;
		}
		my ($key, $value) = ($1, $2);
		if (exists $meta{$key}) {
			$malformed = 1;
			next;
		}
		$meta{$key} = $value;
	}
	close $fh;
	return (\%meta, $malformed);
}

sub _validate_manifest_core_or_die {
	my ($meta, $paths, $barcode) = @_;
	die "ERROR: parser-state transaction version mismatch\n"
		if !exists($meta->{transaction_version}) || $meta->{transaction_version} ne '1';
	die "ERROR: parser-state transaction barcode mismatch\n"
		if !exists($meta->{barcode}) || $meta->{barcode} ne $barcode;
	my %expected = (
		demult_report_live => $paths->{demult_report_live},
		demult_report_tmp => $paths->{demult_report_tmp},
		demult_report_bak => $paths->{demult_report_bak},
		demult_sidecar_live => $paths->{demult_sidecar_live},
		demult_sidecar_tmp => $paths->{demult_sidecar_tmp},
		demult_sidecar_bak => $paths->{demult_sidecar_bak},
		otu_report_live => $paths->{otu_report_live},
		otu_report_tmp => $paths->{otu_report_tmp},
		otu_report_bak => $paths->{otu_report_bak},
		otu_sidecar_live => $paths->{otu_sidecar_live},
		otu_sidecar_tmp => $paths->{otu_sidecar_tmp},
		otu_sidecar_bak => $paths->{otu_sidecar_bak},
		bootstrap_sentinel_path => $paths->{bootstrap_sentinel_path},
	);
	for my $key (keys %expected) {
		die "ERROR: parser-state transaction missing key '$key'\n" if !exists $meta->{$key};
		die "ERROR: parser-state transaction noncanonical path for '$key'\n"
			if $meta->{$key} ne $expected{$key};
	}
	die "ERROR: parser-state transaction missing bootstrap_sentinel_in_transaction\n"
		if !exists $meta->{bootstrap_sentinel_in_transaction};
	die "ERROR: parser-state transaction invalid bootstrap_sentinel_in_transaction\n"
		if $meta->{bootstrap_sentinel_in_transaction} ne 'true' && $meta->{bootstrap_sentinel_in_transaction} ne 'false';
	die "ERROR: parser-state transaction missing publish_phase\n"
		if !exists $meta->{publish_phase};
	die "ERROR: parser-state transaction invalid publish_phase '$meta->{publish_phase}'\n"
			if !$VALID_PHASE{$meta->{publish_phase}};
}

sub _validate_manifest_committed_minimal_or_die {
	my ($meta, $paths, $barcode) = @_;
	die "ERROR: parser-state transaction version mismatch\n"
		if !exists($meta->{transaction_version}) || $meta->{transaction_version} ne '1';
	die "ERROR: parser-state transaction barcode mismatch\n"
		if !exists($meta->{barcode}) || $meta->{barcode} ne $barcode;
	my %expected = (
		demult_report_live => $paths->{demult_report_live},
		demult_sidecar_live => $paths->{demult_sidecar_live},
		otu_report_live => $paths->{otu_report_live},
		otu_sidecar_live => $paths->{otu_sidecar_live},
		bootstrap_sentinel_path => $paths->{bootstrap_sentinel_path},
	);
	for my $key (keys %expected) {
		die "ERROR: parser-state transaction missing key '$key'\n" if !exists $meta->{$key};
		die "ERROR: parser-state transaction noncanonical path for '$key'\n"
			if $meta->{$key} ne $expected{$key};
	}
	die "ERROR: parser-state transaction missing bootstrap_sentinel_in_transaction\n"
		if !exists $meta->{bootstrap_sentinel_in_transaction};
	die "ERROR: parser-state transaction invalid bootstrap_sentinel_in_transaction\n"
		if $meta->{bootstrap_sentinel_in_transaction} ne 'true' && $meta->{bootstrap_sentinel_in_transaction} ne 'false';
	die "ERROR: parser-state transaction missing publish_phase\n"
		if !exists $meta->{publish_phase};
	die "ERROR: parser-state transaction invalid publish_phase '$meta->{publish_phase}'\n"
		if $meta->{publish_phase} ne 'committed';
}

sub _manifest_has_full_schema {
	my ($meta) = @_;
	for my $key (@MANIFEST_KEYS) {
		return 0 if !exists $meta->{$key};
	}
	return 1;
}

sub _list_tx_root_entries {
	my ($tx_root) = @_;
	return () if !-d $tx_root;
	opendir my $dh, $tx_root or die "ERROR: unable to open transaction directory '$tx_root'\n";
	my @entries = grep { $_ ne '.' && $_ ne '..' } readdir $dh;
	closedir $dh;
	return @entries;
}

sub _work_has_artifacts {
	my ($dir) = @_;
	return 0 if !defined $dir || !-d $dir;
	opendir my $dh, $dir or die "ERROR: unable to open transaction work directory '$dir'\n";
	while (my $entry = readdir $dh) {
		next if $entry eq '.' || $entry eq '..';
		my $path = "$dir/$entry";
		if (-f $path || -l $path) {
			closedir $dh;
			return 1;
		}
		if (-d $path && _work_has_artifacts($path)) {
			closedir $dh;
			return 1;
		}
	}
	closedir $dh;
	return 0;
}

sub _validate_tx_root_entries_or_die {
	my ($paths) = @_;
	for my $entry (_list_tx_root_entries($paths->{tx_root})) {
		my $path = "$paths->{tx_root}/$entry";
		next if $entry eq 'parser_state_boundary_commit.manifest.tsv' && !-d $path;
		next if $entry eq 'parser_state_boundary_commit.manifest.tsv.tmp' && !-d $path;
		next if $entry eq 'work' && -d $path;
		die "ERROR: unexpected parser-state transaction root path '$path'\n";
	}
}

sub _remove_work_contents_or_die {
	my ($paths) = @_;
	return if !-d $paths->{work_root};
	remove_tree($paths->{work_root}, { error => \my $err });
	if ($err && @{$err}) {
		die "ERROR: unable to clean parser-state transaction work directory '$paths->{work_root}'\n";
	}
}

sub _rmdir_if_empty {
	my ($dir) = @_;
	return if !defined $dir || !-d $dir;
	opendir my $dh, $dir or return;
	my @entries = grep { $_ ne '.' && $_ ne '..' } readdir $dh;
	closedir $dh;
	return if @entries;
	rmdir $dir or return;
}

sub _finish_cleanup_or_die {
	my ($paths) = @_;
	_remove_work_contents_or_die($paths);
	_unlink_if_exists($paths->{manifest_tmp_path});
	_unlink_if_exists($paths->{manifest_path});
	_rmdir_if_empty($paths->{tx_root});
	_rmdir_if_empty(dirname($paths->{tx_root}));
}

sub _unlink_if_exists {
	my ($path) = @_;
	return if !defined $path || !-e $path;
	unlink $path or die "ERROR: unable to remove '$path'\n";
}

sub _restore_backup_to_live {
	my ($bak, $live) = @_;
	return if !-e $bak;
	_unlink_if_exists($live);
	rename $bak, $live or die "ERROR: unable to restore '$live'\n";
}

sub _validate_committed_live_state_or_die {
	my (%args) = @_;
	my $paths = $args{paths} // die "ERROR: missing paths\n";
	my $context = $args{context} // die "ERROR: missing context\n";
	my $meta = $args{meta} // die "ERROR: missing meta\n";

	ReportingContractSidecar::validate_report_and_sidecar(
		report_kind => 'demult_rpt',
		context => $context,
		report_path => $paths->{demult_report_live},
		sidecar_path => $paths->{demult_sidecar_live},
	);
	ReportingContractSidecar::validate_report_and_sidecar(
		report_kind => 'otu_def_rpt',
		context => $context,
		report_path => $paths->{otu_report_live},
		sidecar_path => $paths->{otu_sidecar_live},
	);
	die "ERROR: parser-state transaction committed state is missing demux bootstrap sentinel\n"
		if !-e $paths->{bootstrap_sentinel_path};
	if ($meta->{bootstrap_sentinel_in_transaction} eq 'true' && !-e $paths->{bootstrap_sentinel_path}) {
		die "ERROR: parser-state transaction committed bootstrap sentinel is missing\n";
	}
}

sub recover_leftover_transaction_or_die {
	my (%args) = @_;
	my $state_dir = $args{state_dir} // die "ERROR: missing state_dir\n";
	my $barcode = $args{barcode} // die "ERROR: missing barcode\n";
	my $context = $args{context} // die "ERROR: missing context\n";
	my $paths = canonical_paths(state_dir => $state_dir, barcode => $barcode);
	my $manifest_path = $paths->{manifest_path};
	my $manifest_tmp_path = $paths->{manifest_tmp_path};

	_validate_tx_root_entries_or_die($paths);

	if (!-e $manifest_path) {
		if (-e $manifest_tmp_path) {
			die "ERROR: leftover parser-state transaction artifacts without manifest\n"
				if _work_has_artifacts($paths->{work_root});
			_unlink_if_exists($manifest_tmp_path);
			_rmdir_if_empty($paths->{work_root});
			_rmdir_if_empty($paths->{tx_root});
			_rmdir_if_empty(dirname($paths->{tx_root}));
			return 1;
		}
		die "ERROR: leftover parser-state transaction artifacts without manifest\n"
			if _work_has_artifacts($paths->{work_root});
		_rmdir_if_empty($paths->{work_root});
		_rmdir_if_empty($paths->{tx_root});
		_rmdir_if_empty(dirname($paths->{tx_root}));
		return 0;
	}

	_unlink_if_exists($manifest_tmp_path);

	my ($meta, $malformed) = _read_manifest_partial($manifest_path);
	if ($malformed) {
		die "ERROR: malformed pre-committed parser-state transaction manifest\n"
			if !exists($meta->{publish_phase}) || $meta->{publish_phase} ne 'committed';
		die "ERROR: malformed parser-state transaction manifest missing committed recovery proof\n"
			if !exists($meta->{transaction_version}) ||
			   !exists($meta->{barcode}) ||
			   !exists($meta->{demult_report_live}) ||
			   !exists($meta->{demult_sidecar_live}) ||
			   !exists($meta->{otu_report_live}) ||
			   !exists($meta->{otu_sidecar_live}) ||
			   !exists($meta->{bootstrap_sentinel_path}) ||
			   !exists($meta->{bootstrap_sentinel_in_transaction});
		_validate_manifest_committed_minimal_or_die($meta, $paths, $barcode);
		_validate_committed_live_state_or_die(paths => $paths, context => $context, meta => $meta);
		_finish_cleanup_or_die($paths);
		return 1;
	}

	if (exists($meta->{publish_phase}) && $meta->{publish_phase} eq 'committed') {
		if (_manifest_has_full_schema($meta)) {
			_validate_manifest_core_or_die($meta, $paths, $barcode);
		} else {
			_validate_manifest_committed_minimal_or_die($meta, $paths, $barcode);
		}
		_validate_committed_live_state_or_die(paths => $paths, context => $context, meta => $meta);
		_finish_cleanup_or_die($paths);
		return 1;
	}

	for my $key (@MANIFEST_KEYS) {
		die "ERROR: parser-state transaction manifest missing key '$key'\n"
			if !exists $meta->{$key};
	}
	_validate_manifest_core_or_die($meta, $paths, $barcode);

	if ($meta->{publish_phase} eq 'prepared' || $meta->{publish_phase} eq 'staged') {
		_remove_work_contents_or_die($paths);
		_unlink_if_exists($manifest_tmp_path);
		_unlink_if_exists($manifest_path);
		_rmdir_if_empty($paths->{tx_root});
		_rmdir_if_empty(dirname($paths->{tx_root}));
		return 1;
	}

	my @rollback_pairs = (
		['demult report', $paths->{demult_report_bak}, $paths->{demult_report_live}],
		['demult sidecar', $paths->{demult_sidecar_bak}, $paths->{demult_sidecar_live}],
		['OTU report', $paths->{otu_report_bak}, $paths->{otu_report_live}],
		['OTU sidecar', $paths->{otu_sidecar_bak}, $paths->{otu_sidecar_live}],
	);
	my $first_seed_transaction = $meta->{bootstrap_sentinel_in_transaction} eq 'true' ? 1 : 0;
	if ($first_seed_transaction) {
		for my $pair (@rollback_pairs) {
			my ($label, $bak, undef) = @{$pair};
			die "ERROR: first-seed parser-state rollback found unexpected backup for $label\n"
				if -e $bak;
		}
		for my $pair (@rollback_pairs) {
			my (undef, undef, $live) = @{$pair};
			_unlink_if_exists($live);
		}
		_unlink_if_exists($paths->{bootstrap_sentinel_path});
	} else {
		for my $pair (@rollback_pairs) {
			my ($label, $bak, undef) = @{$pair};
			die "ERROR: parser-state rollback is impossible because backup for $label is missing\n"
				if !-e $bak;
		}
		for my $pair (@rollback_pairs) {
			my (undef, $bak, $live) = @{$pair};
			_restore_backup_to_live($bak, $live);
		}
	}

	_finish_cleanup_or_die($paths);
	return 1;
}

sub _maybe_inject_fault {
	my ($phase) = @_;
	my $fault_phase = $ENV{RTBIOSCAN_PARSER_STATE_TXN_FAULT_PHASE} // '';
	return if $fault_phase eq '' || $fault_phase ne $phase;
	my $fault_exit = $ENV{RTBIOSCAN_PARSER_STATE_TXN_FAULT_EXIT} // '28';
	die "__PARSER_STATE_TXN_FAULT_EXIT__:$fault_exit\n";
}

sub publish_boundary_commit_or_die {
	my (%args) = @_;
	my $state_dir = $args{state_dir} // die "ERROR: missing state_dir\n";
	my $barcode = $args{barcode} // die "ERROR: missing barcode\n";
	my $context = $args{context} // die "ERROR: missing context\n";
	my $bootstrap_in_transaction = $args{bootstrap_sentinel_in_transaction} ? 1 : 0;
	my $paths = canonical_paths(state_dir => $state_dir, barcode => $barcode);
	die "ERROR: parser-state transaction manifest is missing before publish\n"
		if !-e $paths->{manifest_path};
	die "ERROR: parser-state transaction manifest temp exists before publish\n"
		if -e $paths->{manifest_tmp_path};

	for my $required ($paths->{demult_report_tmp}, $paths->{demult_sidecar_tmp}, $paths->{otu_report_tmp}, $paths->{otu_sidecar_tmp}) {
		die "ERROR: missing staged parser-state file '$required'\n" if !-e $required;
	}

	for my $bak_path ($paths->{demult_report_bak}, $paths->{demult_sidecar_bak}, $paths->{otu_report_bak}, $paths->{otu_sidecar_bak}) {
		die "ERROR: parser-state transaction backup path already exists before publish\n"
			if -e $bak_path;
	}

	if ($bootstrap_in_transaction) {
		my $tmp = $paths->{bootstrap_sentinel_tmp};
		open my $fh, '>', $tmp or die "ERROR: unable to write '$tmp'\n";
		print {$fh} "seeded\n";
		close $fh or die "ERROR: unable to close '$tmp'\n";
	}

	my ($manifest, $malformed) = _read_manifest_partial($paths->{manifest_path});
	die "ERROR: malformed parser-state transaction manifest before publish\n" if $malformed;
	for my $key (@MANIFEST_KEYS) {
		die "ERROR: parser-state transaction manifest missing key '$key'\n"
			if !exists $manifest->{$key};
	}
	_validate_manifest_core_or_die($manifest, $paths, $barcode);
	die "ERROR: parser-state transaction bootstrap flag mismatch before publish\n"
		if $manifest->{bootstrap_sentinel_in_transaction} ne ($bootstrap_in_transaction ? 'true' : 'false');
	die "ERROR: parser-state transaction invalid pre-publish phase '$manifest->{publish_phase}'\n"
		if $manifest->{publish_phase} ne 'prepared';
	$manifest->{publish_phase} = 'staged';
	_write_manifest($paths, $manifest);

	my $error = undef;
	eval {
		for my $pair (
			[$paths->{demult_report_live}, $paths->{demult_report_bak}],
			[$paths->{demult_sidecar_live}, $paths->{demult_sidecar_bak}],
			[$paths->{otu_report_live}, $paths->{otu_report_bak}],
			[$paths->{otu_sidecar_live}, $paths->{otu_sidecar_bak}],
		) {
			my ($live, $bak) = @{$pair};
			if (-e $live) {
				_unlink_if_exists($bak);
				rename $live, $bak or die "ERROR: unable to backup '$live'\n";
			}
		}
		$manifest->{publish_phase} = 'backed_up';
		_write_manifest($paths, $manifest);
		_maybe_inject_fault('after_backed_up');

		rename $paths->{demult_report_tmp}, $paths->{demult_report_live}
			or die "ERROR: unable to publish '$paths->{demult_report_live}'\n";
		rename $paths->{demult_sidecar_tmp}, $paths->{demult_sidecar_live}
			or die "ERROR: unable to publish '$paths->{demult_sidecar_live}'\n";
		rename $paths->{otu_report_tmp}, $paths->{otu_report_live}
			or die "ERROR: unable to publish '$paths->{otu_report_live}'\n";
		rename $paths->{otu_sidecar_tmp}, $paths->{otu_sidecar_live}
			or die "ERROR: unable to publish '$paths->{otu_sidecar_live}'\n";

		$manifest->{publish_phase} = 'published_pairs';
		_write_manifest($paths, $manifest);
		_maybe_inject_fault('after_published_pairs');

		if ($bootstrap_in_transaction) {
			rename $paths->{bootstrap_sentinel_tmp}, $paths->{bootstrap_sentinel_path}
				or die "ERROR: unable to publish '$paths->{bootstrap_sentinel_path}'\n";
			$manifest->{publish_phase} = 'published_sentinel';
			_write_manifest($paths, $manifest);
			_maybe_inject_fault('after_published_sentinel');
		}

		$manifest->{publish_phase} = 'committed';
		_write_manifest($paths, $manifest);
		_maybe_inject_fault('after_committed');
		1;
	} or do {
		$error = $@ || "ERROR: unknown parser-state boundary publish failure\n";
	};

	if (defined $error) {
		recover_leftover_transaction_or_die(
			state_dir => $state_dir,
			barcode => $barcode,
			context => $context,
		);
		if ($error =~ /__PARSER_STATE_TXN_FAULT_EXIT__:(\d+)/) {
			exit $1;
		}
		die $error;
	}

	recover_leftover_transaction_or_die(
		state_dir => $state_dir,
		barcode => $barcode,
		context => $context,
	);
	return 1;
}

1;
