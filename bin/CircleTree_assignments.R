# Circle tree plot for assignments (consensus/OTU)
library(ggplot2)
source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

have_ggrepel <- requireNamespace("ggrepel", quietly = TRUE)

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  stop("Usage: CircleTree_assignments.R <input.tsv> <output.png> [title]")
}

in_path <- args[1]
out_path <- args[2]
plot_title <- if (length(args) >= 3) args[3] else ""

read_input <- function(path) {
  if (is.null(path) || path == "" || !file.exists(path) || file.info(path)$size == 0) {
    return(data.frame())
  }
  df <- tryCatch(
    read.delim(path, header = TRUE, sep = "\t", stringsAsFactors = FALSE, check.names = FALSE),
    error = function(e) data.frame()
  )
  if (ncol(df) < 3) {
    return(data.frame())
  }
  df$path <- trimws(df$path)
  df$weight <- suppressWarnings(as.numeric(df$weight))
  df <- df[!is.na(df$weight) & is.finite(df$weight) & df$weight > 0 & df$path != "", , drop = FALSE]
  df
}

ensure_root <- function(parts) {
  parts <- parts[nzchar(parts)]
  if (!length(parts)) return(character())
  if (parts[1] != "Root") {
    parts <- c("Root", parts)
  }
  parts
}

build_tree <- function(df) {
  paths <- strsplit(df$path, ";", fixed = TRUE)
  paths <- lapply(paths, function(p) ensure_root(trimws(p)))
  paths <- paths[sapply(paths, length) > 0]
  if (!length(paths)) return(NULL)

  nodes <- data.frame(id = "Root", name = "Root", parent = NA_character_, depth = 0, stringsAsFactors = FALSE)
  node_index <- setNames(1, "Root")

  for (parts in paths) {
    if (length(parts) < 2) next
    for (i in 2:length(parts)) {
      id <- paste(parts[1:i], collapse = ";")
      if (!is.na(node_index[id])) next
      nodes <- rbind(nodes, data.frame(id = id, name = parts[i], parent = paste(parts[1:(i - 1)], collapse = ";"), depth = i - 1, stringsAsFactors = FALSE))
      node_index[id] <- nrow(nodes)
    }
  }

  leaf_weights <- tapply(df$weight, df$path, sum)
  leaf_weights[is.na(leaf_weights)] <- 0

  node_weight <- setNames(rep(0, nrow(nodes)), nodes$id)
  for (leaf_id in names(leaf_weights)) {
    my_parts <- ensure_root(strsplit(leaf_id, ";", fixed = TRUE)[[1]])
    for (i in seq_along(my_parts)) {
      id <- paste(my_parts[1:i], collapse = ";")
      node_weight[id] <- node_weight[id] + leaf_weights[[leaf_id]]
    }
  }
  nodes$weight <- node_weight[nodes$id]

  children_map <- split(nodes$id, nodes$parent)
  get_leaves <- function(node_id) {
    kids <- children_map[[node_id]]
    if (is.null(kids) || length(kids) == 0) {
      return(node_id)
    }
    kids <- sort(kids)
    unlist(lapply(kids, get_leaves), use.names = FALSE)
  }
  leaves <- get_leaves("Root")
  leaves <- leaves[leaves != "Root"]
  if (!length(leaves)) return(NULL)

  angles <- seq(0, 2 * pi, length.out = length(leaves) + 1)
  angles <- angles[1:length(leaves)]
  leaf_angles <- setNames(angles, leaves)

  node_angles <- setNames(rep(NA_real_, nrow(nodes)), nodes$id)
  node_angles[leaves] <- leaf_angles[leaves]

  calc_angle <- function(node_id) {
    if (!is.na(node_angles[node_id])) return(node_angles[node_id])
    kids <- children_map[[node_id]]
    if (is.null(kids) || length(kids) == 0) return(NA_real_)
    vals <- sapply(kids, calc_angle)
    node_angles[node_id] <<- mean(vals, na.rm = TRUE)
    node_angles[node_id]
  }
  calc_angle("Root")

  nodes$angle <- node_angles[nodes$id]
  nodes$x <- nodes$depth * cos(nodes$angle)
  nodes$y <- nodes$depth * sin(nodes$angle)

  edges <- nodes[!is.na(nodes$parent), , drop = FALSE]
  edges$parent_x <- nodes$x[match(edges$parent, nodes$id)]
  edges$parent_y <- nodes$y[match(edges$parent, nodes$id)]
  list(nodes = nodes, edges = edges)
}

df <- read_input(in_path)
if (!nrow(df)) {
  save_plot_placeholder(out_path, "Circle tree")
  quit(save = "no")
}

tree <- build_tree(df)
if (is.null(tree) || !is.list(tree) || !nrow(tree$nodes)) {
  save_plot_placeholder(out_path, "Circle tree")
  quit(save = "no")
}

nodes <- tree$nodes
edges <- tree$edges
leaf_ids <- setdiff(nodes$id, unique(nodes$parent))
leaves <- nodes[nodes$id %in% leaf_ids, , drop = FALSE]
leaves$leaf_weight <- leaves$weight
leaves <- leaves[is.finite(leaves$leaf_weight) & leaves$leaf_weight > 0, , drop = FALSE]

label_df <- leaves[order(-leaves$leaf_weight), , drop = FALSE]
max_labels <- min(20, nrow(label_df))
if (max_labels > 0) {
  label_df <- label_df[1:max_labels, , drop = FALSE]
}

gradient <- NULL
if (exists("plot_palette")) {
  gradient <- plot_palette("tree_gradient")
}
if (!is.null(gradient) && length(gradient) >= 2) {
  grad_low <- gradient[1]
  grad_high <- gradient[length(gradient)]
} else {
  grad_low <- "#dfe7f3"
  grad_high <- "#1f4e79"
}

p <- ggplot() +
  geom_segment(
    data = edges,
    aes(x = parent_x, y = parent_y, xend = x, yend = y, color = weight),
    linewidth = 0.4
  ) +
  geom_point(
    data = leaves,
    aes(x = x, y = y, size = leaf_weight),
    color = "#2c3e50",
    alpha = 0.85
  ) +
  scale_size(range = c(1, 6)) +
  scale_color_gradient(low = grad_low, high = grad_high) +
  coord_equal() +
  theme_void() +
  theme_journal() +
  theme(
    legend.position = "none",
    plot.title = element_text(hjust = 0.5)
  )

if (nzchar(plot_title)) {
  p <- p + ggtitle(plot_title)
}

if (nrow(label_df) > 0) {
  if (have_ggrepel) {
    p <- p + ggrepel::geom_text_repel(
      data = label_df,
      aes(x = x, y = y, label = name),
      size = 3,
      segment.size = 0.2,
      max.overlaps = Inf
    )
  } else {
    p <- p + geom_text(
      data = label_df,
      aes(x = x, y = y, label = name),
      size = 3
    )
  }
}

save_plot_journal(p, out_path)
