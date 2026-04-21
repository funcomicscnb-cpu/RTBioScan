#!/usr/bin/env Rscript

# Treemap plot
# Genus abundance in terms of consensus clusters (count of consensus sequences)

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
  # Hide 'unassigned' when any assigned taxa exist to avoid plots dominated by that category.
  assigned_df <- df[df[[label_col]] != "unassigned", , drop = FALSE]
  if (nrow(assigned_df) > 0) {
    df <- assigned_df
  }
  if (!have_treemapify) {
    save_plot_placeholder(out_path, "Treemap", "R package 'treemapify' is not available")
    return(invisible(NULL))
  }
  p <- ggplot(df, aes(area = Clusters, fill = Class, label = .data[[label_col]], subgroup = Class)) +
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

file_name <- args[1]
sample_name <- gsub("_consensus_tax_gns_metazoa_treemap_clusters_rpt.txt", "", basename(file_name))
df <- read_treemap_df(file_name, c("Class", "Genus", "Clusters"))
output_file <- paste0(sample_name, "_consensus_tax_gns_COI_treemap_clusters.png")
write_treemap(df, output_file, "Genus")

file_name <- args[2]
sample_name <- gsub("_consensus_tax_gns_viridiplantae_treemap_clusters_rpt.txt", "", basename(file_name))
df <- read_treemap_df(file_name, c("Class", "Genus", "Clusters"))
output_file <- paste0(sample_name, "_consensus_tax_gns_ITS_treemap_clusters.png")
write_treemap(df, output_file, "Genus")
