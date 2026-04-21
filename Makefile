.PHONY: serve-report serve-report-open report report-run report-all

REPORT_DIR ?= results
REPORT_HOST ?= 127.0.0.1
REPORT_PORT ?= 8000
REPORT_FILE ?= report.html
REPORT_URL_PREFIX ?= /
REPORT_REFRESH_SECONDS ?= 15

serve-report:
	@bash bin/serve_report.sh --dir "$(REPORT_DIR)" --host "$(REPORT_HOST)" --port "$(REPORT_PORT)" --report "$(REPORT_FILE)" --quiet
# Example:
#   make serve-report REPORT_DIR=/path/to/outdir

serve-report-open:
	@bash bin/serve_report.sh --dir "$(REPORT_DIR)" --host "$(REPORT_HOST)" --port "$(REPORT_PORT)" --report "$(REPORT_FILE)" --open --quiet
# Auto-open browser if supported (open/xdg-open)

REPORT_OPEN_LAST ?= 10

serve-report-open-all:
	@bash bin/serve_report.sh --dir "$(REPORT_DIR)" --host "$(REPORT_HOST)" --port "$(REPORT_PORT)" --report "$(REPORT_FILE)" --open --open-all --quiet

serve-report-open-last:
	@bash bin/serve_report.sh --dir "$(REPORT_DIR)" --host "$(REPORT_HOST)" --port "$(REPORT_PORT)" --report "$(REPORT_FILE)" --open --open-last "$(REPORT_OPEN_LAST)" --quiet

report:
	@bash bin/report_rebuild.sh --outdir "$(REPORT_DIR)" --url-prefix "$(REPORT_URL_PREFIX)" --refresh-seconds "$(REPORT_REFRESH_SECONDS)"

report-run:
	@bash bin/report_rebuild.sh --outdir "$(REPORT_DIR)" --url-prefix "$(REPORT_URL_PREFIX)" --refresh-seconds "$(REPORT_REFRESH_SECONDS)" --run-id "$(RUN_ID)"

report-all:
	@bash bin/report_rebuild.sh --outdir "$(REPORT_DIR)" --url-prefix "$(REPORT_URL_PREFIX)" --refresh-seconds "$(REPORT_REFRESH_SECONDS)" --all-runs
