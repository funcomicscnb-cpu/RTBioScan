#!/usr/bin/perl

use FindBin;
require "$FindBin::Bin/lib/sample_label.pl";

use strict;
use warnings;

my $keep_ids_path = shift @ARGV;
my %keep_ids;
if (defined $keep_ids_path && $keep_ids_path ne '') {
    open my $KEEP, '<', $keep_ids_path or die "open $keep_ids_path: $!";
    while (my $line = <$KEEP>) {
        chomp $line;
        next if $line =~ /^\s*$/;
        $keep_ids{$line} = 1;
    }
    close $KEEP;
}

my $emit_current = 0;
while (my $line = <>) {
    if ($line =~ /^>/) {
        my $adapter = SampleLabel::extract_adapter_from_read_id(substr($line, 1));
        my $read_id = substr($line, 1);
        chomp $read_id;
        $read_id =~ s/\s.*$//;
        $read_id =~ s/[|].*$//;
        if (exists $keep_ids{$read_id}) {
            $emit_current = 1;
        } else {
            $emit_current =
                (index($line, '|sup|') >= 0
                && (
                    !defined($adapter)
                    || !SampleLabel::is_no_adapter_label($adapter)
                )) ? 1 : 0;
        }
        print $line if $emit_current;
        next;
    }

    print $line if $emit_current;
}
