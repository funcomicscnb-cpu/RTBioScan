#Treemap plot
#Genus and species abundance in terms of reads

library(ggplot2)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

have_treemapify <- requireNamespace("treemapify", quietly = TRUE)

args <- commandArgs(trailingOnly = TRUE)

read_treemap_df <- function(path, cols) {
  if (is.null(path) || !file.exists(path) || file.info(path)$size == 0) {
    return(data.frame())
  }
  df <- tryCatch(
    read.delim(path, header = TRUE, sep = "\t", stringsAsFactors = FALSE, check.names = FALSE),
    error = function(e) data.frame()
  )
  if (ncol(df) < 3) {
    return(data.frame())
  }
  df <- df[, 1:3]
  colnames(df) <- cols
  df[[3]] <- suppressWarnings(as.numeric(df[[3]]))
  df <- df[is.finite(df[[3]]) & df[[3]] > 0, , drop = FALSE]
  df
}

write_treemap <- function(df, out_path, label_col) {
  if (nrow(df) == 0) {
    save_plot_placeholder(out_path, "Treemap")
    return(invisible(NULL))
  }
  if (!have_treemapify) {
    save_plot_placeholder(out_path, "Treemap", "R package 'treemapify' is not available")
    return(invisible(NULL))
  }
  p <- ggplot(df, aes(area = Abundance, fill = Class, label = .data[[label_col]], subgroup = Class)) +
    treemapify::geom_treemap() +
    treemapify::geom_treemap_subgroup_border(colour = "white", size = 5) +
    treemapify::geom_treemap_subgroup_text(place = "centre", grow = TRUE,
                               alpha = 0.25, colour = "black",
                               fontface = "italic") +
    treemapify::geom_treemap_text(colour = "white", place = "centre",
                      size = 15, grow = TRUE) +
    scale_fill_brewer(palette = "Dark2") +
    theme_journal() +
    theme(
      axis.title = element_blank(),
      axis.text = element_blank(),
      axis.ticks = element_blank(),
      panel.grid = element_blank()
    )
  save_plot_journal(p, out_path)
  invisible(NULL)
}

process_pair <- function(file_name, output_file) {
  df <- read_treemap_df(file_name, c("Class", "Genus", "Abundance"))
  write_treemap(df, output_file, "Genus")
}

if (length(args) == 2 && grepl("_treemap_rpt\\.txt$", args[2])) {
  file_name <- args[1]
  sample_name <- gsub("_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt","",basename(file_name))
  process_pair(file_name, paste0(sample_name, "_consensus_consolidated_tax_gns_COI_treemap.png"))

  file_name <- args[2]
  sample_name <- gsub("_consensus_consolidated_tax_gns_viridiplantae_treemap_rpt.txt","",basename(file_name))
  process_pair(file_name, paste0(sample_name, "_consensus_consolidated_tax_gns_ITS_treemap.png"))
} else if (length(args) >= 2 && length(args) %% 2 == 0) {
  for (i in seq(1, length(args), by = 2)) {
    process_pair(args[i], args[i + 1])
  }
} else {
  stop("Usage: Treemap_consensus_abundance_genus_consolidated.R <legacy_file1> <legacy_file2> OR repeated <input> <output_png> pairs")
}
