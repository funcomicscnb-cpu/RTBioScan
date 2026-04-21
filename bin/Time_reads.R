# Load necessary libraries
library(ggplot2)
library(dplyr)
library(ggrepel)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

#File input
args <- commandArgs(trailingOnly = TRUE)
file_name <- args[1]
sample_name <- gsub("_reads_time_rpt.txt","",basename(file_name))
data_cumulative <- read.delim(file_name, stringsAsFactors = FALSE)

# The report can be appended across rounds, and older files may contain repeated header
# lines. Also, the table values are per-round (incremental) counts; to plot a true
# cumulative curve we convert them to cumulative sums per data series.
data_cumulative <- data_cumulative %>%
  filter(run_id != "run_id")

data_cumulative$time <- suppressWarnings(as.numeric(data_cumulative$time))
data_cumulative$reads <- suppressWarnings(as.numeric(data_cumulative$reads))
data_cumulative <- data_cumulative %>%
  filter(!is.na(time), !is.na(reads)) %>%
  arrange(time) %>%
  group_by(data) %>%
  mutate(reads = cumsum(reads)) %>%
  ungroup()

if (nrow(data_cumulative) == 0) {
  output_file <- paste0(sample_name, "_reads_time.png")
  save_plot_placeholder(output_file, "Cumulative Reads Over Time")
  quit(status = 0)
}

#Dataprocessing
#data_cumulative <- read.delim("Tucan_3/Tucan_3_reads_time_rpt.txt")
time0 <- min(data_cumulative$time)
data_cumulative$time <- data_cumulative$time-time0
data_cumulative$hours <- data_cumulative$time/3600

data_cumulative$label <- NA
# Function to get the last x value for each group
get_last_x <- function(df) {
  df$label[which(df$hours == max(df$hours))] <- df$data[which(df$hours == max(df$hours))]
  return(df)
}
# Apply the function to each group
data_cumulative <- do.call(rbind, lapply(split(data_cumulative, data_cumulative$data), get_last_x))

#To show better colors:
data_cumulative$data <- factor(data_cumulative$data, levels = c("sequencing", "processed","fast","hac","sup"))
##If finally processed is not shown needs to change to
#data_cumulative$data <- factor(data_cumulative$data, levels = c("sequencing", "fast","hac","sup"))

output_file <- paste0(sample_name,"_reads_time.png")
palette <- plot_palette("reads_series")
p <- ggplot(data_cumulative, aes(x = hours, y = reads, color = data)) +
  geom_line(linewidth = plot_style$line_size) +
  theme_journal() +
  theme(legend.position = "none") +
  scale_color_manual(values = palette, drop = FALSE) +
  geom_text_repel(aes(label = label), size = 3.5, nudge_x = 0.3, na.rm = TRUE) +
  scale_y_continuous(labels = function(x) format(x, big.mark = ",", scientific = FALSE)) +
  labs(y = "Cumulative Reads", x = "Time (h)", title = "Cumulative Reads Over Time")

save_plot_journal(p, output_file)
