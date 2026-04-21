#get argument from bash script (plate name)
args <- commandArgs(trailingOnly = TRUE)
sample_name <- args[1]
#wd <- args[2]
min_reads <- 5
if (length(args) >= 2 && grepl("^[0-9]+$", args[2])) {
  min_reads <- as.integer(args[2])
}
max_N <- 4
if (length(args) >= 3 && grepl("^[0-9]+$", args[3])) {
  max_N <- as.integer(args[3])
}

if (length(sample_name) == 0) {
  stop("Error: Missing input argument (sample_name).\n", call. = FALSE)
}

#set working directory based on plate's folder, load required libraries
setwd(paste0("Consensus/",sample_name))
library("Biostrings")
library(DECIPHER)
library(parallel)

#sample files
pattern <- "*_reads_sup.fasta"
file_list <- grep(pattern, list.files(), value = TRUE)

# Process one OTU FASTA: align reads and return consensus DNAStringSet, or NULL.
# Also writes per-OTU consensus file as a side-effect (safe across parallel workers
# because each OTU writes a distinct filename).
process_otu <- function(file) {
  otuname_raw <- gsub("_reads_sup.fasta", "", file)
  otuname     <- gsub("-", "|", otuname_raw)
  input <- readDNAStringSet(file)
  len   <- length(input)
  if (len < min_reads) return(NULL)

  align <- DNAStringSet(AlignSeqs(input, verbose = FALSE))
  cons  <- DNAStringSet(ConsensusSequence(align,
            ignoreNonBases = FALSE, noConsensusChar = "N",
            threshold = 0.75, minInformation = 0.25))
  names(cons) <- sprintf("%s|%s|reads-%s", sample_name, otuname, len)

  output <- DNAStringSet(gsub(pattern = "-", replacement = "", x = cons))
  degen  <- paste0("[^", paste(c("A","T","C","G","N"), collapse = ""), "]")
  output <- DNAStringSet(gsub(pattern = degen, "N", output))

  if (letterFrequency(output, letters = "N") < max_N) {
    # Per-OTU consensus output for caching/reuse
    writeXStringSet(output, sprintf("%s_consensus.fasta", otuname_raw))
    return(output)
  }
  return(NULL)
}

# Use RSCRIPT_WORKERS parallel workers (set by Consensus_simple.sh based on
# CPU budget and number of samples); fall back to sequential for small batches.
workers <- local({
  v <- suppressWarnings(as.integer(Sys.getenv("RSCRIPT_WORKERS", "1")))
  if (is.na(v) || v < 1L) 1L else v
})
nc <- if (length(file_list) >= workers) workers else 1L

results <- mclapply(file_list, process_otu, mc.cores = nc)

# Collect non-NULL results into a single DNAStringSet
outputall <- do.call(c, Filter(Negate(is.null), results))
if (is.null(outputall)) outputall <- DNAStringSet()

#output consensus sequences as a single FASTA file
writeXStringSet(outputall, sprintf("%s_consensus.fasta", sample_name))
