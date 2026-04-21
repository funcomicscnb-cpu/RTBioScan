library(ggplot2)
library(tidyr)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

args <- commandArgs(trailingOnly = TRUE)
file_name <- args[1]
#sample_name <- args[2]
sample_name <- gsub("_read_info_rpt.txt","",basename(file_name))

#read_info <- read.delim("Tucan_3/Tucan_3_read_info_rpt.txt",row.names = NULL,nrows = 100000)
#read_info <- read.delim("Tucan_4/Tucan_4_fast_read_info_on_target_barcode_rpt.txt",row.names = NULL,nrows = 100000)
#This is limited to 100k reads as otherwise it is too difficult to run:
read_info <- read.delim(file_name,row.names = NULL,nrows = 100000)

#Not sure if this would be better for for now not done
#read_info <- na.omit(read_info)

#Convert the data to plot into a longer format
data_long <- pivot_longer(read_info, 
                          cols = c(fast_mean_qscore, hac_mean_qscore, sup_mean_qscore), 
                          names_to = "qscore_type", 
                          values_to = "qscore",
                          values_drop_na = TRUE)

output_file <- paste0(sample_name,"_violin_quality_read_info.png")
p <- ggplot(data_long, aes(x = qscore_type, y = qscore)) +
  geom_violin(aes(fill=barcode), alpha = 0.8) +
  #facet_wrap(~barcode) +
  #geom_boxplot(width=0.1, aes(fill=barcode)) + 
  labs(title = "Quality Distribution by Barcode",
       x = "Basecalling Model",
       y = "Qscore") +
  theme_journal() +
  scale_fill_brewer(palette = "Dark2") +
  theme(legend.title = element_blank())
save_plot_journal(p, output_file)


data_long$read_length <- data_long$fast_length
data_long[data_long$qscore_type == "hac_mean_qscore","read_length"] <- data_long[data_long$qscore_type == "hac_mean_qscore","hac_length"]
data_long[data_long$qscore_type == "sup_mean_qscore","read_length"] <- data_long[data_long$qscore_type == "sup_mean_qscore","sup_length"]

length_without_unmatched <- data_long[data_long$barcode != "unmatched", ]
length_with_unmatched <- data_long[data_long$barcode != "unmatched" | (data_long$barcode == "unmatched" & data_long$qscore_type == "fast_mean_qscore"), ]

output_file <- paste0(sample_name,"_violin_length_read_info.png")
p <- ggplot(length_without_unmatched, aes(x = qscore_type, y = read_length)) +
  geom_violin(aes(fill=barcode), alpha = 0.8) +
  labs(title = "Read Length by Barcode",
       x = "Basecalling model",
       y = "Read length") +
  theme_journal() +
  scale_fill_brewer(palette = "Dark2") +
  theme(legend.title = element_blank())
save_plot_journal(p, output_file)

output_file <- paste0(sample_name,"_violin_length_read_info_log.png")
p <- ggplot(length_with_unmatched, aes(x = qscore_type, y = read_length)) +
  geom_violin(aes(fill=barcode), alpha = 0.8) +
  scale_y_log10() + annotation_logticks(sides= "l") +
  labs(title = "Read Length by Barcode (log scale)",
       x = "Basecalling model",
       y = "Read length") +
  theme_journal() +
  scale_fill_brewer(palette = "Dark2") +
  theme(legend.title = element_blank())
save_plot_journal(p, output_file)

output_file <- paste0(sample_name,"_density_read_info.png")
p <- ggplot(length_without_unmatched, aes(x=read_length, y=qscore )) +
  stat_density_2d(aes(fill = after_stat(level)),geom = "polygon") + scale_fill_viridis_c() +
  theme_journal()  +
  facet_wrap(~qscore_type) + theme(legend.position = "none") +
  theme(strip.text = element_text(size = plot_style$axis_text_size)) +
  labs(x = "Read Length (bp)")
save_plot_journal(p, output_file)

output_file <- paste0(sample_name,"_density_read_info_with_unmatched.png")
p <- ggplot(length_with_unmatched, aes(x=read_length, y=qscore )) +
  stat_density_2d(aes(fill = after_stat(level)),geom = "polygon") + scale_fill_viridis_c() +
  theme_journal()  +
  facet_wrap(~qscore_type) + theme(legend.position = "none") +
  theme(strip.text = element_text(size = plot_style$axis_text_size)) +
  labs(x = "Read Length (bp)")
save_plot_journal(p, output_file)
  


# ggplot(data_long[data_long$barcode == "unmatched",], aes(x=read_length)) + 
#   geom_density() + theme_bw() + 
#   xlim(c(0,2000))# +
#   scale_y_log10() + annotation_logticks(sides= "b") 
