plot_style_defaults <- function() {
  list(
    font_family = "Helvetica",
    base_size = 11,
    title_size = 16,
    subtitle_size = 12,
    axis_title_size = 12,
    axis_text_size = 10,
    legend_title_size = 10,
    legend_text_size = 10,
    line_size = 0.8,
    width_in = 7,
    height_in = 4.5,
    dpi = 300
  )
}

plot_style <- plot_style_defaults()

theme_journal <- function() {
  ggplot2::theme_minimal(base_family = plot_style$font_family, base_size = plot_style$base_size) +
    ggplot2::theme(
      plot.title = ggplot2::element_text(size = plot_style$title_size, face = "bold"),
      plot.subtitle = ggplot2::element_text(size = plot_style$subtitle_size),
      axis.title = ggplot2::element_text(size = plot_style$axis_title_size),
      axis.text = ggplot2::element_text(size = plot_style$axis_text_size),
      legend.title = ggplot2::element_text(size = plot_style$legend_title_size),
      legend.text = ggplot2::element_text(size = plot_style$legend_text_size),
      panel.grid.minor = ggplot2::element_blank()
    )
}

plot_palettes <- list(
  reads_series = c(
    sequencing = "#4C78A8",
    processed = "#F58518",
    fast = "#54A24B",
    hac = "#E45756",
    sup = "#72B7B2"
  ),
  tree_gradient = c(
    low = "#dfe7f3",
    high = "#1f4e79"
  ),
  reads_cumulative = c(
    "Total reads" = "#4C78A8",
    "OTU species reads" = "#F58518",
    "Consensus species reads" = "#E45756",
    "OTU genus reads" = "#54A24B",
    "Consensus genus reads" = "#72B7B2",
    "Matched reads (configured markers)" = "#54A24B",
    "Demultiplexed reads" = "#F58518",
    "OTU genus reads (demultiplexed)" = "#B279A2",
    "OTU genus reads (no_adapter)" = "#FF9DA6",
    "Consensus genus reads (demultiplexed)" = "#9C755F",
    "Consensus genus reads (no_adapter)" = "#BAB0AC"
  )
)

plot_palette <- function(name) {
  plot_palettes[[name]]
}

save_plot_placeholder <- function(out_png, title = NULL, message = "No data available") {
  p <- ggplot2::ggplot() +
    ggplot2::theme_void() +
    ggplot2::annotate("text", x = 0, y = 0, label = message, size = 4)
  if (!is.null(title) && nzchar(title)) {
    p <- p + ggplot2::ggtitle(title) + theme_journal()
  }
  save_plot_journal(p, out_png)
}

save_plot_journal <- function(p, out_png, out_pdf = NULL) {
  if (is.null(out_pdf) && !is.null(out_png) && grepl("\\.[Pp][Nn][Gg]$", out_png)) {
    out_pdf <- sub("\\.[Pp][Nn][Gg]$", ".pdf", out_png)
  }
  ggplot2::ggsave(
    filename = out_png,
    plot = p,
    width = plot_style$width_in,
    height = plot_style$height_in,
    dpi = plot_style$dpi,
    units = "in"
  )
  if (!is.null(out_pdf)) {
    ggplot2::ggsave(
      filename = out_pdf,
      plot = p,
      width = plot_style$width_in,
      height = plot_style$height_in,
      units = "in"
    )
  }
}
