library(ggplot2)
library(tidyr)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

args <- commandArgs(trailingOnly = TRUE)
file_name <- args[1]
sample_filter <- if (length(args) >= 2) args[2] else ""
output_prefix <- if (length(args) >= 3) args[3] else ""
#sample_name <- args[2]
sample_name <- gsub("_summary_demult_rpt.txt","",basename(file_name))

#sample_info <- read.delim("Tucan_3_summary_demult_rpt.txt")
sample_info <- read.delim(file_name)
if (!("sample_name" %in% colnames(sample_info))) {
  stop(
    sprintf(
      "summary file %s is missing required sample_name column; regenerate the summary with the corrected schema",
      file_name
    ),
    call. = FALSE
  )
}
has_track_cols <- all(c("track_sample_label", "track_replicate_suffix", "track_replicate_number", "track_plate_label") %in% colnames(sample_info))
group_col <- "sample_name"
if (has_track_cols && any(nzchar(as.character(sample_info$track_sample_label)))) {
  group_col <- "track_sample_label"
}
sample_info <- sample_info[!grepl("^no_adapter", as.character(sample_info[[group_col]])), ]
sample_info <- subset(sample_info, basecalling_model == "hac")
if (!is.null(sample_filter) && nzchar(sample_filter)) {
  sample_info <- sample_info[as.character(sample_info[[group_col]]) == sample_filter, ]
}
# Backward-compatible default output names unless an explicit prefix is provided.
if (!is.null(output_prefix) && nzchar(output_prefix)) {
  sample_name <- output_prefix
}
# Keep output contract stable even when filtered input has no rows.
if (nrow(sample_info) == 0) {
  output_file <- paste0(sample_name,"_reads_per_barcode.png")
  save_plot_placeholder(output_file, "Reads per Barcode per Sample replicate", "No sample read-count data")

  output_file <- paste0(sample_name,"_reads_per_sample_log.png")
  save_plot_placeholder(output_file, "Reads per Sample (log scale)", "No sample read-count data")

  output_file <- paste0(sample_name,"_reads_per_sample.png")
  save_plot_placeholder(output_file, "Reads per Sample", "No sample read-count data")
  quit(save = "no")
}
track_mode <- has_track_cols &&
  any(
    nzchar(as.character(sample_info$track_sample_label)) &
    nzchar(as.character(sample_info$track_replicate_suffix))
  )
if (track_mode) {
  replicate_order <- unique(
    sample_info[, c("track_replicate_suffix", "track_replicate_number", "track_plate_label"), drop = FALSE]
  )
  replicate_order$track_replicate_number_num <- suppressWarnings(as.numeric(as.character(replicate_order$track_replicate_number)))
  replicate_order$track_replicate_number_num[is.na(replicate_order$track_replicate_number_num)] <- Inf
  replicate_order$track_plate_label <- as.character(replicate_order$track_plate_label)
  replicate_order <- replicate_order[order(
    replicate_order$track_replicate_number_num,
    replicate_order$track_plate_label
  ), , drop = FALSE]
  x_levels <- unique(as.character(replicate_order$track_replicate_suffix))
  sample_info$x_label <- factor(as.character(sample_info$track_replicate_suffix), levels = x_levels)
} else {
  sample_info$x_label <- factor(as.character(sample_info$replicate))
}
# sample_info$target_barcode <- gsub("MPnew_", "", sample_info$target_barcode)
# sample_info$target_barcode <- gsub("MP1_", "", sample_info$target_barcode)
# sample_info$target_barcode <- gsub("LR1_", "", sample_info$target_barcode)
# sample_info$target_barcode <- gsub("COI2", "COI", sample_info$target_barcode)


output_file <- paste0(sample_name,"_reads_per_barcode.png")
p <- ggplot(sample_info, aes(x = x_label, y = read_count, fill = barcode_by_homology)) +
  geom_col(alpha = 0.8) +
  theme_journal() +
  theme(legend.title = element_blank()) +
  scale_fill_brewer(palette = "Dark2") +
  labs(title = "Reads per Replicate",
       x = "Replicate",
       y = "Read count")
save_plot_journal(p, output_file)


output_file <- paste0(sample_name,"_reads_per_sample_log.png")
p <- ggplot(sample_info, aes(x = x_label, y = read_count, fill = barcode_by_homology)) +
  geom_col(alpha = 0.8) +
  scale_y_log10() + annotation_logticks(sides = "l") +
  theme_journal() +
  theme(legend.title = element_blank()) +
  scale_fill_brewer(palette = "Dark2") +
  labs(x = "Replicate", y = "Read count (log scale)")
save_plot_journal(p, output_file)

output_file <- paste0(sample_name,"_reads_per_sample.png")
p <- ggplot(sample_info, aes(x = x_label, y = read_count, fill = barcode_by_homology)) +
  geom_col(alpha = 0.8) +
  theme_journal() +
  theme(legend.title = element_blank()) +
  scale_fill_brewer(palette = "Dark2") +
  labs(x = "Replicate", y = "Read count")
save_plot_journal(p, output_file)





















# ggplot(sample_info, aes(x = sample_name, y = read_count, fill = barcode_by_homology)) +
#   geom_point( alpha = 0.8) + facet_grid(~sample_name) +
#   coord_flip()# +
#   #geom_boxplot(width=0.1, aes(fill=barcode)) + 
#   labs(title = "Read Length Quality Distribution",
#        x = "Qscore Type",
#        y = "Qscore") +
#   theme_minimal() + #theme(legend.position = "none") +
#   theme(legend.title = element_blank()) + 
#   scale_fill_brewer(palette = "Dark2") + 
#   theme(
#     title = element_text(size = 18),    # Title
#     axis.title = element_text(size = 16),    # X and Y axis titles
#     axis.text = element_text(size = 14),     # X and Y axis text
#     legend.title = element_text(size = 16),  # Legend title
#     legend.text = element_text(size = 14)    # Legend text
#   )
