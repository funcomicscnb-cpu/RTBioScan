# Load necessary libraries
library(ggplot2)
library(dplyr)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

args <- commandArgs(trailingOnly = TRUE)
file_name <- args[1]
#sample_name <- args[2]
sample_name <- gsub("_otu_tax_time_rpt.txt","",basename(file_name))

#data_cumulative <- read.delim("~/XPrize/Plots/Tucan2_new/Tucan2_new_otu_tax_time.rpt")
data_cumulative <- read.delim(file_name)

# Drop repeated header lines if present
data_cumulative <- data_cumulative %>% filter(run_id != "run_id")

# Parse optional marker read rows appended by append_reports.pl
parsed <- data_cumulative %>%
  mutate(
    rank = ifelse(grepl("^(species|genus|family)$", taxon), taxon, sub("^((species|genus|family)).*$", "\\1", taxon)),
    marker = ifelse(grepl("^(species|genus|family)_[A-Z0-9._-]+_reads$", taxon), sub("^(species|genus|family)_([A-Z0-9._-]+)_reads$", "\\2", taxon), NA_character_),
    series = ifelse(grepl("^(species|genus|family)$", taxon), "taxa", ifelse(!is.na(marker), paste(marker, "reads"), "other")),
    identifications = suppressWarnings(as.numeric(identifications)),
    time = suppressWarnings(as.numeric(time))
  ) %>%
  filter(!is.na(time), !is.na(identifications)) %>%
  filter(series != "other")

# Ensure chronological plotting when a t=0 row is injected.
parsed <- parsed %>% arrange(time)

# Suppress read-series that are all-zero
parsed <- parsed %>%
  group_by(rank, series) %>%
  # dplyr::filter() needs a vector; use a single boolean per group.
  filter(!(dplyr::first(series) != "taxa" & max(identifications, na.rm = TRUE) <= 0)) %>%
  ungroup()

if (nrow(parsed) == 0) {
  output_file <- paste0(sample_name, "_otu_tax_time.png")
  save_plot_placeholder(output_file, "Taxons identified over time")
  quit(status = 0)
}

time0 <- min(parsed$time, na.rm = TRUE)
parsed$time <- parsed$time - time0

max_taxa <- max(parsed$identifications[parsed$series == "taxa"], na.rm = TRUE)
max_reads <- max(parsed$identifications[parsed$series != "taxa"], na.rm = TRUE)
scale_factor <- 1
if (is.finite(max_taxa) && is.finite(max_reads) && max_taxa > 0 && max_reads > 0) {
  scale_factor <- max_reads / max_taxa
}
parsed <- parsed %>%
  mutate(y_plot = ifelse(series == "taxa", identifications, identifications / scale_factor))

parsed$rank <- factor(parsed$rank, levels = c("species", "genus", "family"))
series_levels <- c("taxa", sort(unique(parsed$series[parsed$series != "taxa"])))
parsed$series <- factor(parsed$series, levels = series_levels)

output_file <- paste0(sample_name,"_otu_tax_time.png")
# Plot the data
p <- ggplot(parsed, aes(x = time, y = y_plot, color = rank, linetype = series)) +
  geom_line(linewidth = plot_style$line_size) +
  geom_point(size = 1.5, alpha = 0.8) +
  scale_color_brewer(palette = "Dark2") +
  scale_linetype_manual(values = rep(c("solid", "dashed", "dotdash", "twodash", "longdash"), length.out = length(series_levels))) +
  scale_y_continuous(
    name = "Identifications (unique taxa)",
    labels = function(x) format(x, big.mark = ",", scientific = FALSE),
    sec.axis = sec_axis(~ . * scale_factor, name = "Reads")
  ) +
  labs(
    title = "Taxons identified over time",
    x = "Time (s)",
    color = NULL,
    linetype = NULL
  ) +
  theme_journal() +
  theme(legend.position = "bottom")

save_plot_journal(p, output_file)
