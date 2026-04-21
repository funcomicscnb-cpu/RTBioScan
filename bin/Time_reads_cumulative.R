library(ggplot2)
library(dplyr)
library(tidyr)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

args <- commandArgs(trailingOnly = TRUE)
file_name <- args[1]
sample_name <- gsub("_reads_cumulative_rpt.txt","",basename(file_name))

data <- read.delim(file_name)
data <- data %>% arrange(time)
data$hours <- (data$time - min(data$time)) / 3600

series_map <- c(
  total_reads = "Total reads",
  otu_species_reads = "OTU species reads",
  consensus_species_reads = "Consensus species reads",
  otu_genus_reads = "OTU genus reads",
  consensus_genus_reads = "Consensus genus reads",
  matched_reads = "Matched reads (configured markers)",
  demultiplexed_reads = "Demultiplexed reads",
  otu_genus_reads_demux = "OTU genus reads (demultiplexed)",
  otu_genus_reads_noadapter = "OTU genus reads (no_adapter)",
  consensus_genus_reads_demux = "Consensus genus reads (demultiplexed)",
  consensus_genus_reads_noadapter = "Consensus genus reads (no_adapter)"
)

available_series <- intersect(names(series_map), colnames(data))

plot_data <- data %>%
  select(hours, all_of(available_series)) %>%
  pivot_longer(-hours,
               names_to = "series",
               values_to = "value")

plot_data$series <- factor(
  plot_data$series,
  levels = available_series,
  labels = series_map[available_series]
)

output_file <- paste0(sample_name,"_reads_cumulative_log.png")
p <- ggplot(plot_data, aes(x = hours, y = value, color = series)) +
  geom_line(linewidth = plot_style$line_size)

# With a single timepoint, a line won't be visible; show points so round 1 isn't "blank".
if (nrow(data) == 1) {
  p <- p + geom_point(size = 2)
}

# Label each series at its last point (right side of plot). Keep labels inside the image.
x_rng <- max(plot_data$hours, na.rm = TRUE) - min(plot_data$hours, na.rm = TRUE)
label_nudge <- ifelse(is.finite(x_rng) && x_rng > 0, -0.02 * x_rng, 0)
end_labels <- plot_data %>%
  group_by(series) %>%
  filter(hours == max(hours, na.rm = TRUE)) %>%
  arrange(desc(value)) %>%
  slice(1) %>%
  ungroup() %>%
  # Avoid trying to draw labels at non-positive values on a log10 axis
  mutate(value = ifelse(is.finite(value) & value > 0, value, NA_real_))

p <- p +
  scale_y_log10(labels = scales::comma) +
  scale_color_manual(values = plot_palette("reads_cumulative"), drop = FALSE) +
  labs(
    title = "Cumulative Reads Over Time",
    x = "Time (h)",
    y = "Cumulative reads (log scale)",
    color = NULL
  ) +
  theme_journal() +
  theme(
    legend.position = "none",
    plot.margin = margin(5.5, 20, 5.5, 5.5)
  ) +
  geom_text(
    data = end_labels,
    aes(label = series),
    hjust = 1,
    nudge_x = label_nudge,
    size = 3.5,
    show.legend = FALSE,
    na.rm = TRUE
  )

save_plot_journal(p, output_file)
