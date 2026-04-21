#!/usr/bin/env Rscript
suppressPackageStartupMessages({
  have_ape <- requireNamespace("ape", quietly = TRUE)
})

source(file.path(dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[1]))), "plot_style.R"))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  stop("Usage: TreeFan_cladogram.R <input.tsv> <output.png> [title] [subtitle]")
}

in_path <- args[1]
out_path <- args[2]
plot_title <- if (length(args) >= 3) args[3] else ""
plot_subtitle <- if (length(args) >= 4) args[4] else ""

safe_placeholder <- function(message) {
  save_plot_placeholder(out_path, plot_title, message)
}

if (!have_ape) {
  safe_placeholder("ape not installed")
  quit(save = "no")
}

suppressPackageStartupMessages({
  library(ape)
})

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

build_nodes <- function(paths) {
  nodes <- data.frame(id = "Root", name = "Root", parent = NA_character_, stringsAsFactors = FALSE)
  node_index <- setNames(1, "Root")

  for (parts in paths) {
    if (length(parts) < 2) next
    for (i in 2:length(parts)) {
      id <- paste(parts[1:i], collapse = ";")
      if (!is.na(node_index[id])) next
      nodes <- rbind(
        nodes,
        data.frame(
          id = id,
          name = parts[i],
          parent = paste(parts[1:(i - 1)], collapse = ";"),
          stringsAsFactors = FALSE
        )
      )
      node_index[id] <- nrow(nodes)
    }
  }
  nodes
}

build_phylo <- function(nodes) {
  children_map <- split(nodes$id, nodes$parent)
  is_tip <- !(nodes$id %in% names(children_map))
  tip_ids <- nodes$id[is_tip]
  tip_ids <- tip_ids[tip_ids != "Root"]
  if (!length(tip_ids)) return(NULL)

  internal_ids <- nodes$id[!is_tip]
  if (!("Root" %in% internal_ids)) {
    internal_ids <- c(internal_ids, "Root")
  }

  tip_ids <- sort(tip_ids)
  internal_ids <- sort(unique(internal_ids))

  tip_index <- setNames(seq_along(tip_ids), tip_ids)
  internal_index <- setNames(seq_along(internal_ids) + length(tip_ids), internal_ids)
  node_index <- c(tip_index, internal_index)

  edges <- list()
  for (i in seq_len(nrow(nodes))) {
    my_id <- nodes$id[i]
    parent_id <- nodes$parent[i]
    if (is.na(parent_id) || parent_id == "") {
      next
    }
    if (!is.na(node_index[my_id]) && !is.na(node_index[parent_id])) {
      edges[[length(edges) + 1]] <- c(node_index[parent_id], node_index[my_id])
    }
  }
  edge_mat <- do.call(rbind, edges)
  if (is.null(edge_mat)) return(NULL)

  tree <- list(
    edge = edge_mat,
    tip.label = tip_ids,
    Nnode = length(internal_ids)
  )
  tree$node.label <- vapply(internal_ids, extract_label, character(1))
  tree$edge.length <- rep(1, nrow(tree$edge))
  class(tree) <- "phylo"
  tree <- ape::reorder.phylo(tree, "cladewise")
  tree
}

calc_node_weights <- function(paths, weights) {
  node_weight <- list()
  for (i in seq_along(paths)) {
    parts <- ensure_root(paths[[i]])
    w <- weights[[i]]
    for (j in seq_along(parts)) {
      id <- paste(parts[1:j], collapse = ";")
      node_weight[[id]] <- (node_weight[[id]] %||% 0) + w
    }
  }
  node_weight
}

`%||%` <- function(a, b) if (!is.null(a)) a else b

extract_label <- function(path) {
  if (is.na(path) || path == "") return("")
  parts <- strsplit(path, ";", fixed = TRUE)[[1]]
  parts <- parts[nzchar(parts)]
  if (!length(parts)) return("")
  parts[length(parts)]
}

make_taxon_color_map <- function(labels) {
  labels <- sort(unique(labels[nzchar(labels) & labels != "Root"]))
  if (!length(labels)) {
    return(setNames(character(), character()))
  }
  pal <- grDevices::hcl.colors(max(length(labels), 3L), palette = "Dark 3")
  setNames(pal[seq_along(labels)], labels)
}

make_label_colors <- function(labels, taxon_colors, default = "#4d5b48") {
  vapply(labels, function(label) {
    if (!is.null(label) && nzchar(label) && !is.na(taxon_colors[label])) {
      unname(taxon_colors[label])
    } else {
      default
    }
  }, character(1))
}

measure_label_box <- function(label, x, y, cex, srt = 0, pad = 0.01) {
  width <- strwidth(label, cex = cex, units = "user")
  height <- strheight(label, cex = cex, units = "user")
  theta <- abs(srt) * pi / 180
  dx <- abs(width * cos(theta)) + abs(height * sin(theta))
  dy <- abs(width * sin(theta)) + abs(height * cos(theta))
  list(
    xmin = x - dx / 2 - pad,
    xmax = x + dx / 2 + pad,
    ymin = y - dy / 2 - pad,
    ymax = y + dy / 2 + pad
  )
}

boxes_overlap <- function(a, b) {
  !(a$xmax < b$xmin || a$xmin > b$xmax || a$ymax < b$ymin || a$ymin > b$ymax)
}

draw_filtered_labels <- function(candidates) {
  if (!length(candidates)) {
    return(invisible(NULL))
  }
  occupied <- list()
  for (candidate in candidates) {
    if (!nzchar(candidate$label)) {
      next
    }
    box <- measure_label_box(candidate$label, candidate$x, candidate$y, candidate$cex, candidate$srt, candidate$pad)
    overlap <- any(vapply(occupied, boxes_overlap, logical(1), b = box))
    if (overlap) {
      next
    }
    text(
      candidate$x,
      candidate$y,
      labels = candidate$label,
      col = candidate$color,
      cex = candidate$cex,
      font = candidate$font,
      srt = candidate$srt,
      adj = candidate$adj,
      xpd = NA
    )
    occupied[[length(occupied) + 1]] <- box
  }
  invisible(NULL)
}

build_tip_label_candidates <- function(x, y, labels, colors, weights, cex, offset) {
  keep <- which(nzchar(labels) & is.finite(weights) & weights > 0)
  if (!length(keep)) {
    return(list())
  }
  ord <- keep[order(weights[keep], decreasing = TRUE)]
  out <- vector("list", length(ord))
  for (idx in seq_along(ord)) {
    i <- ord[idx]
    radius <- sqrt(x[i]^2 + y[i]^2)
    if (!is.finite(radius) || radius <= 0) {
      next
    }
    lx <- x[i] + (x[i] / radius) * offset
    ly <- y[i] + (y[i] / radius) * offset
    angle <- atan2(y[i], x[i]) * 180 / pi
    if (angle < -90) angle <- angle + 180
    if (angle > 90) angle <- angle - 180
    out[[idx]] <- list(
      label = labels[i],
      x = lx,
      y = ly,
      color = colors[i],
      cex = cex,
      font = 1,
      srt = angle,
      adj = c(if (x[i] >= 0) 0 else 1, 0.5),
      pad = offset * 0.18
    )
  }
  Filter(Negate(is.null), out)
}

build_internal_label_candidates <- function(x, y, labels, colors, weights, cex, offset) {
  keep <- which(nzchar(labels) & is.finite(weights) & weights > 0)
  if (!length(keep)) {
    return(list())
  }
  ord <- keep[order(weights[keep], decreasing = TRUE)]
  out <- vector("list", length(ord))
  for (idx in seq_along(ord)) {
    i <- ord[idx]
    radius <- sqrt(x[i]^2 + y[i]^2)
    if (!is.finite(radius) || radius <= 0) {
      next
    }
    lx <- x[i] + (x[i] / radius) * offset
    ly <- y[i] + (y[i] / radius) * offset
    out[[idx]] <- list(
      label = labels[i],
      x = lx,
      y = ly,
      color = colors[i],
      cex = cex,
      font = 2,
      srt = 0,
      adj = c(0.5, 0.5),
      pad = offset * 0.12
    )
  }
  Filter(Negate(is.null), out)
}

plot_tree <- function(tree, tip_weights, internal_weights, tip_colors, node_colors, edge_colors, title, subtitle) {
  side_in <- max(plot_style$width_in, plot_style$height_in, 11.44)
  preview <- ape::plot.phylo(
    tree,
    type = "fan",
    edge.color = edge_colors,
    edge.width = 3,
    show.tip.label = FALSE,
    no.margin = FALSE,
    plot = FALSE
  )
  expand_limits <- function(lim, factor = 1.28) {
    center <- mean(lim, na.rm = TRUE)
    half <- diff(lim) * 0.5 * factor
    c(center - half, center + half)
  }
  plot_xlim <- expand_limits(preview$x.lim)
  plot_ylim <- expand_limits(preview$y.lim)
  grDevices::png(
    filename = out_path,
    width = side_in,
    height = side_in,
    units = "in",
    res = plot_style$dpi
  )
  on.exit(grDevices::dev.off(), add = TRUE)

  par(mar = c(3.4, 3.4, 4.9, 3.4), xpd = NA)
  ape::plot.phylo(
    tree,
    type = "fan",
    edge.color = edge_colors,
    edge.width = 3,
    show.tip.label = FALSE,
    no.margin = FALSE,
    x.lim = plot_xlim,
    y.lim = plot_ylim
  )

  if (nzchar(title) || nzchar(subtitle)) {
    title(main = title, sub = subtitle, cex.main = 1.4, cex.sub = 1.0)
  }

  plot_env <- get("last_plot.phylo", envir = .PlotPhyloEnv)
  span_x <- diff(range(plot_env$xx, na.rm = TRUE))
  span_y <- diff(range(plot_env$yy, na.rm = TRUE))
  span <- max(span_x, span_y)
  tip_offset <- span * 0.075
  node_offset <- span * 0.04

  if (length(tip_weights) > 0) {
    tip_labels <- vapply(tree$tip.label, extract_label, character(1))
    tip_candidates <- build_tip_label_candidates(
      plot_env$xx[seq_along(tree$tip.label)],
      plot_env$yy[seq_along(tree$tip.label)],
      tip_labels,
      tip_colors,
      tip_weights,
      cex = 1.17,
      offset = tip_offset
    )
    draw_filtered_labels(tip_candidates)
  }

  if (length(tree$node.label) > 0) {
    node_numbers <- seq_along(tree$node.label) + length(tree$tip.label)
    internal_labels <- tree$node.label
    internal_labels[internal_labels == "Root"] <- ""
    node_candidates <- build_internal_label_candidates(
      plot_env$xx[node_numbers],
      plot_env$yy[node_numbers],
      internal_labels,
      node_colors,
      internal_weights,
      cex = 1.07,
      offset = node_offset
    )
    draw_filtered_labels(node_candidates)
  }
}

tryCatch({
  df <- read_input(in_path)
  if (!nrow(df)) {
    safe_placeholder("No data available")
    quit(save = "no")
  }

  paths <- strsplit(df$path, ";", fixed = TRUE)
  paths <- lapply(paths, function(p) ensure_root(trimws(p)))
  keep_idx <- sapply(paths, length) > 1
  paths <- paths[keep_idx]
  df <- df[keep_idx, , drop = FALSE]
  if (!length(paths)) {
    safe_placeholder("No data available")
    quit(save = "no")
  }

  nodes <- build_nodes(paths)
  if (!nrow(nodes)) {
    safe_placeholder("No data available")
    quit(save = "no")
  }

  tree <- build_phylo(nodes)
  if (is.null(tree)) {
    safe_placeholder("No data available")
    quit(save = "no")
  }

  leaf_weights <- tapply(df$weight, df$path, sum)
  leaf_weights[is.na(leaf_weights)] <- 0
  node_weights <- calc_node_weights(paths, as.numeric(df$weight))

  internal_ids <- sort(unique(nodes$id[!(nodes$id %in% tree$tip.label)]))
  index_to_id <- c(tree$tip.label, internal_ids)
  edge_child_ids <- index_to_id[tree$edge[, 2]]
  edge_weights <- vapply(edge_child_ids, function(id) node_weights[[id]] %||% 0, numeric(1))

  missing_tips <- setdiff(tree$tip.label, names(leaf_weights))
  if (length(missing_tips)) {
    leaf_weights[missing_tips] <- 0
  }

  tip_labels <- vapply(tree$tip.label, extract_label, character(1))
  node_labels <- tree$node.label
  node_labels[node_labels == "Root"] <- ""
  taxon_colors <- make_taxon_color_map(c(tip_labels, node_labels))
  tip_colors <- make_label_colors(tip_labels, taxon_colors)
  node_colors <- make_label_colors(node_labels, taxon_colors)
  edge_taxa <- vapply(edge_child_ids, extract_label, character(1))
  edge_colors <- make_label_colors(edge_taxa, taxon_colors, default = "#6c7a64")
  internal_weights <- vapply(internal_ids, function(id) node_weights[[id]] %||% 0, numeric(1))

  tip_weights <- as.numeric(leaf_weights[tree$tip.label])
  plot_tree(tree, tip_weights, internal_weights, tip_colors, node_colors, edge_colors, plot_title, plot_subtitle)
}, error = function(e) {
  safe_placeholder("Tree render failed")
})
