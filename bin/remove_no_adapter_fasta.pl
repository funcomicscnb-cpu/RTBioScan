#!/usr/bin/perl

use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";

use strict;
use warnings;

my $emit_current = 0;
while (my $line = <STDIN>) {
    if ($line =~ /^>/) {
        my $adapter = SampleLabel::extract_adapter_from_read_id(substr($line, 1));
        $emit_current = (!defined($adapter) || !SampleLabel::is_no_adapter_label($adapter)) ? 1 : 0;
        print $line if $emit_current;
        next;
    }

    print $line if $emit_current;
}
