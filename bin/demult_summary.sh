#!/bin/bash

demult_file=$1
barcode=$2



echo -ne "read_count\t$(head -1 ${demult_file} |cut -f2-8)\tsample_name\n" > ${barcode}_summary_demult_rpt.txt

tail -n +2 ${demult_file} | cut -f2-8 | sort | uniq -c | sed 's/^[[:space:]]*//'|sed 's/[[:space:]]/\t/'| awk '{print $0"\t"$5"_"$6"_"$7}' >> ${barcode}_summary_demult_rpt.txt
