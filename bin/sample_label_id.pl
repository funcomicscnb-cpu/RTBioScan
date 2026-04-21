#!/usr/bin/env perl

use strict;
use warnings;
use FindBin;

require "$FindBin::Bin/lib/sample_label.pl";

my $label = join(' ', @ARGV);
$label = '' unless defined $label;
print SampleLabel::stable_sample_id_from_label($label), "\n";

