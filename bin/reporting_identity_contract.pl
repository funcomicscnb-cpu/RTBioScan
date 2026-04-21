#!/usr/bin/perl

package ReportingIdentityContract;

use strict;
use warnings;
use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";

sub _normalize_model {
	my ($token) = @_;
	return '' if !defined $token || $token eq '';
	return 'sup' if $token =~ /^sup$/i;
	return 'fast' if $token =~ /^fast$/i;
	return 'hac' if $token =~ /^hac(?:_fixed|2sup)?$/i;
	return '';
}

sub _extract_keyed_token {
	my ($tokens_ref, $key) = @_;
	for my $tok (@{$tokens_ref}) {
		next if !defined $tok;
		if ($tok =~ /^\Q$key\E=(.*)$/) {
			return defined($1) ? $1 : '';
		}
	}
	return '';
}

sub _free_tokens {
	my ($tokens_ref) = @_;
	my @free = ();
	for my $tok (@{$tokens_ref}) {
		next if !defined $tok || $tok eq '';
		next if _normalize_model($tok) ne '';
		next if $tok =~ /^[^=]+=/;
		push @free, $tok;
	}
	return @free;
}

sub _is_supported_compatibility_target {
	my ($token) = @_;
	return 0 if !defined $token;
	my $targets_raw = $ENV{"RTBIOSCAN_TARGET_TOKENS"} || '';
	return 0 if $targets_raw eq '';
	for my $target (split /\|/, $targets_raw, -1) {
		next if !defined $target;
		$target =~ s/^\s+|\s+$//g;
		next if $target eq '';
		return 1 if $token eq $target;
	}
	return 0;
}

sub _is_supported_nonkeyed_family {
	my ($tokens_ref, $free_tokens_ref) = @_;
	return 0 if !defined $tokens_ref || !defined $free_tokens_ref;
	return 0 if @{$free_tokens_ref} != 1;
	return 0 if !_is_supported_compatibility_target($free_tokens_ref->[0]);
	for my $tok (@{$tokens_ref}) {
		next if !defined $tok || $tok eq '';
		next if _normalize_model($tok) ne '';
		return 0 if $tok ne $free_tokens_ref->[0];
	}
	return 1;
}

sub _sample_dimensions {
	my ($sample, $context) = @_;
	return ('unknown', 'unknown', 'unknown', 'unknown')
		if !defined $sample || $sample eq '' || $sample eq 'no_adapter' || $context eq 'full_track';
	my @parts = split /_/, $sample, -1;
	return (
		defined($parts[0]) && $parts[0] ne '' ? $parts[0] : 'unknown',
		defined($parts[1]) && $parts[1] ne '' ? $parts[1] : 'unknown',
		defined($parts[2]) && $parts[2] ne '' ? $parts[2] : 'unknown',
		defined($parts[3]) && $parts[3] ne '' ? $parts[3] : 'unknown',
	);
}

sub parse_header {
	my ($raw_header, $context) = @_;
	return undef if !defined $raw_header;
	my $header = $raw_header;
	chomp $header;
	$header =~ s/\r$//;
	$header =~ s/^[>@]//;
	$header =~ s/\s.*$//;
	return undef if $header eq '';
	my @tokens = split /\|/, $header, -1;
	return undef if !@tokens;
	my $read_id = shift @tokens;
	return undef if !defined $read_id || $read_id eq '';

	my $otu_id = '';
	while (@tokens) {
		my $last = $tokens[-1];
		last if !defined $last;
		if ($last =~ /^(OTUB_[^|]+)$/) {
			$otu_id = $1 if $otu_id eq '';
			pop @tokens;
			next;
		}
		if ($last =~ /^OTU=(.+)$/) {
			$otu_id = $1 if $otu_id eq '';
			pop @tokens;
			next;
		}
		last;
	}

	my $model = '';
	for my $tok (@tokens) {
		my $norm = _normalize_model($tok);
		if ($norm ne '') {
			$model = $norm;
			last;
		}
	}
	$model = 'hac' if $model eq '';

	my $barcode = _extract_keyed_token(\@tokens, 'barcode');
	my $adapter = _extract_keyed_token(\@tokens, 'adapter');
	my @free_tokens = _free_tokens(\@tokens);
	my $sample = SampleLabel::normalize_sample_label(
		(defined $adapter && $adapter ne '') ? $adapter : 'no_adapter'
	);
	if ($barcode eq '' && $adapter ne '') {
		my $target_token = '';
		for my $tok (@free_tokens) {
			next if !defined $tok || $tok eq '';
			if (_is_supported_compatibility_target($tok)) {
				$target_token = $tok;
				last;
			}
		}
		$barcode = $target_token ne '' ? $target_token : 'no_adapter_1';
	}
	my ($platform, $sampling_method, $subsample, $replicate) = _sample_dimensions($sample, $context);

	if ($context eq 'off') {
		return {
			read_id => $read_id,
			barcode_by_homology => 'no_adapter_1',
			basecalling_model => $model,
			sample => 'no_adapter',
			platform => 'unknown',
			sampling_method => 'unknown',
			subsample => 'unknown',
			replicate => 'unknown',
			identity_scope => 'unknown',
			identity_value => 'no_adapter',
			otu_id => ($otu_id ne '' ? $otu_id : 'NA'),
		};
	}

	if ($barcode eq '' && $adapter eq '' && _is_supported_nonkeyed_family(\@tokens, \@free_tokens)) {
		return {
			read_id => $read_id,
			barcode_by_homology => 'no_adapter_1',
			basecalling_model => $model,
			sample => 'no_adapter',
			platform => 'unknown',
			sampling_method => 'unknown',
			subsample => 'unknown',
			replicate => 'unknown',
			identity_scope => 'unknown',
			identity_value => 'no_adapter',
			otu_id => ($otu_id ne '' ? $otu_id : 'NA'),
		};
	}

	return undef if $barcode eq '' || $adapter eq '';

	my $identity_scope = 'unknown';
	if ($context eq 'full_track') {
		$identity_scope = 'unit';
	} elsif ($context eq 'full_collapse') {
		$identity_scope = 'sample';
	} elsif ($context eq 'primers_only') {
		$identity_scope = 'primer';
	} else {
		die "ERROR: unsupported demux context '$context'\n";
	}

	return {
		read_id => $read_id,
		barcode_by_homology => $barcode,
		basecalling_model => $model,
		sample => $sample,
		platform => $platform,
		sampling_method => $sampling_method,
		subsample => $subsample,
		replicate => $replicate,
		identity_scope => $identity_scope,
		identity_value => $sample,
		otu_id => ($otu_id ne '' ? $otu_id : 'NA'),
	};
}

1;
