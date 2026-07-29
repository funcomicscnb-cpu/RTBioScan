#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFEST_REL="docs/internal/public_release_manifest.md"

OUTDIR="${REPO_ROOT}/release/RTBioScan_public"
PROFILES=""
INCLUDE_NEXTFLOW=1
INCLUDE_TESTS=0
STRICT=0
FORCE=0

WARNINGS=0

usage() {
	cat <<'EOF'
Usage: prepare_public_release.sh [options]

Create a clean RTBioScan release directory based on docs/internal/public_release_manifest.md.

Options:
  --outdir DIR         Output directory for the assembled release
                       [default: <repo>/release/RTBioScan_public]
  --profiles LIST      Comma-separated bundled profiles to support explicitly.
                       Supported: barcoding,voucher,xprize,test
  --with-tests         Include tests/, pytest.ini, and Makefile
  --no-nextflow        Do not bundle the local ./nextflow binary
  --strict             Fail if a manifest-required file/prefix/dir is missing
  --force              Remove an existing output directory first
  -h, --help           Show this help

Notes:
  - The script always copies the core runtime files and docs.
  - Platform-specific Dorado binaries/models are never copied; provision them separately.
  - Database prefixes/files are added from the manifest rules plus the selected profiles.
  - By default, missing required items produce warnings and the release is still generated.
EOF
}

warn() {
	printf 'WARN: %s\n' "$*" >&2
	WARNINGS=$((WARNINGS + 1))
}

die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

handle_missing() {
	local message="$1"
	if [ "$STRICT" -eq 1 ]; then
		die "$message"
	fi
	warn "$message"
}

trim_spaces() {
	printf '%s' "$1" | tr -d '[:space:]'
}

profile_enabled() {
	local needle="$1"
	local item
	local old_ifs="$IFS"
	IFS=','
	for item in $PROFILES; do
		[ "$item" = "$needle" ] && IFS="$old_ifs" && return 0
	done
	IFS="$old_ifs"
	return 1
}

copy_path() {
	local rel="$1"
	local src="${REPO_ROOT}/${rel}"
	local dst="${OUTDIR}/${rel}"
	local parent

	if [ ! -e "$src" ] && [ ! -L "$src" ]; then
		handle_missing "required path is missing: ${rel}"
		return 0
	fi

	parent="$(dirname "$dst")"
	mkdir -p "$parent"

	if [ -d "$src" ] && [ ! -L "$src" ]; then
		cp -PRp "$src" "$parent/"
	else
		cp -Pp "$src" "$dst"
	fi
}

copy_tree_filtered() {
	local root_rel="$1"
	local path rel
	local old_pwd

	[ -d "${REPO_ROOT}/${root_rel}" ] || {
		handle_missing "required directory is missing: ${root_rel}"
		return 0
	}

	old_pwd="$(pwd -P)"
	cd "${REPO_ROOT}/${root_rel}"
	while IFS= read -r path; do
		rel="${root_rel}/${path}"
		case "$rel" in
			*/.DS_Store|*/__pycache__/*|*.pyc)
				continue
				;;
		esac
		copy_path "$rel"
	done < <(find . \( -type f -o -type l \) -print | sed 's#^\./##' | sort)
	cd "$old_pwd"
}

should_copy_bin() {
	local rel="$1"
	case "$rel" in
		*/.DS_Store|*/__pycache__/*|*.pyc|*.pre_*)
			return 1
			;;
		bin/dorado/*)
			# Dorado is a platform-specific, independently licensed/provisioned
			# runtime. Never copy arbitrary local bytes into a public release.
			return 1
			;;
		*)
			return 0
			;;
	esac
}

copy_bin_filtered() {
	local path rel
	[ -d "${REPO_ROOT}/bin" ] || die "bin/ is missing"
	while IFS= read -r path; do
		rel="${path#${REPO_ROOT}/}"
		if should_copy_bin "$rel"; then
			copy_path "$rel"
		fi
	done < <(find "${REPO_ROOT}/bin" \( -type f -o -type l \) -print | sort)
}

copy_prefix_family() {
	local prefix_rel="$1"
	local prefix_abs="${REPO_ROOT}/${prefix_rel}"
	local match_count=0
	local item
	local old_nullglob

	old_nullglob="$(shopt -p nullglob || true)"
	shopt -s nullglob

	for item in "${prefix_abs}".*; do
		match_count=$((match_count + 1))
		copy_path "${item#${REPO_ROOT}/}"
	done

	eval "$old_nullglob" 2>/dev/null || true

	if [ "$match_count" -eq 0 ]; then
		handle_missing "required database prefix has no matching files: ${prefix_rel}.*"
	fi
}

not_recorded_yet() {
	local value="$1"
	local haystack="$2"
	case "$haystack" in
		*$'\n'"$value"$'\n'*)
			return 1
			;;
		*)
			return 0
			;;
	esac
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--outdir)
			OUTDIR="${2:-}"
			shift 2
			;;
		--profiles)
			PROFILES="$(trim_spaces "${2:-}")"
			shift 2
			;;
		--with-tests)
			INCLUDE_TESTS=1
			shift
			;;
		--no-nextflow)
			INCLUDE_NEXTFLOW=0
			shift
			;;
		--strict)
			STRICT=1
			shift
			;;
		--force)
			FORCE=1
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			die "unknown argument: $1"
			;;
	esac
done

[ -n "$OUTDIR" ] || die "--outdir must not be empty"
[ -f "${REPO_ROOT}/${MANIFEST_REL}" ] || die "manifest not found: ${MANIFEST_REL}"

case ",${PROFILES}," in
	*,barcoding,*|*,voucher,*|*,xprize,*|*,test,*|*,,*)
		:
		;;
	*)
		# Validation happens below for unknown tokens.
		:
		;;
esac

if [ -n "$PROFILES" ]; then
	local_profiles="$PROFILES"
	old_ifs="$IFS"
	IFS=','
	for _p in $local_profiles; do
		case "$_p" in
			barcoding|voucher|xprize|test)
				:
				;;
			*)
				IFS="$old_ifs"
				die "unsupported profile '${_p}'. Supported: barcoding,voucher,xprize,test"
				;;
		esac
	done
	IFS="$old_ifs"
fi

if [ -e "$OUTDIR" ]; then
	if [ "$FORCE" -ne 1 ]; then
		die "output directory already exists: ${OUTDIR} (use --force to replace it)"
	fi
	rm -rf "$OUTDIR"
fi

mkdir -p "$OUTDIR"

CORE_TOP=(
	README.md
	LICENSE
	RTBioScan.sh
	main.nf
	nextflow.config
	environment.yml
	conda-lock-linux-64.yml
	conda-lock-osx-64.yml
)

CORE_CONF=(
	conf/base.config
	conf/barcoding.config
	conf/voucher.config
	conf/xprize.config
	conf/state_compatibility/reference_manifest_legacy_v1.tsv
	conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv
	conf/runtime_compatibility/dorado_release_0.7.0_osx-arm64.tsv
	conf/runtime_validation/fast_routing_endosymbionts.fa
	conf/runtime_validation/fast_routing_endosymbionts.expected.tsv
)

CORE_LIB=(
	lib/ChannelUtils.groovy
	lib/DemuxConfig.groovy
)

CORE_DOCS=(
	docs/README.md
	docs/installation.md
	docs/usage.md
	docs/output.md
	docs/report_schema.md
	docs/pipeline.md
	docs/concepts.md
	docs/assets/pipeline_overview_public.svg
	docs/assets/pipeline_overview_public.png
	docs/params_reference.json
)

CORE_ASSETS=(
	assets/report/template.html
	assets/report/run_template.html
	assets/report/report.css
	assets/report/report.js
	assets/report/figures.tsv
	assets/report/figures_sample.tsv
	assets/readme/pod5.html
	assets/readme/sample_info.html
	assets/readme/state.html
	assets/readme/run_config.html
)

for rel in "${CORE_TOP[@]}"; do
	copy_path "$rel"
done

for rel in "${CORE_CONF[@]}"; do
	copy_path "$rel"
done

if profile_enabled test; then
	copy_path "conf/test.config"
fi

for rel in "${CORE_LIB[@]}"; do
	copy_path "$rel"
done

for rel in "${CORE_DOCS[@]}"; do
	copy_path "$rel"
done

for rel in "${CORE_ASSETS[@]}"; do
	copy_path "$rel"
done

if [ "$INCLUDE_NEXTFLOW" -eq 1 ] && [ -x "${REPO_ROOT}/nextflow" ]; then
	copy_path "nextflow"
fi

copy_bin_filtered

if [ "$INCLUDE_TESTS" -eq 1 ]; then
	copy_tree_filtered "tests"
	copy_path "pytest.ini"
	copy_path "Makefile"
fi

DB_PREFIXES=(
	"db/COInr98_2024Jun_RioNegro_Brazil"
	"db/ITS2nr98_2024Jun_RioNegro_Brazil"
	"db/targets_All_tagged_nr95"
)

DB_FILES=(
	"db/COInr_2024Jun_metazoa_memtax1.txt"
	"db/ITS2nr_2024Jun_viridiplantae_memtax2.txt"
	"db/DBnr_2024Jun_id2lineage.txt"
)

DB_DIRS=(
	"db/taxdb"
	"db/taxonomy/releases/ncbi-taxdump-2024-06-24"
)

if profile_enabled barcoding; then
	DB_FILES+=( "db/ITS2nr_2024Jun_viridiplantae_memtax2.txt" )
fi

if profile_enabled voucher; then
	DB_PREFIXES+=( "db/targets_All_tagged_nr95" )
	DB_FILES+=( "db/ITS2nr_2024Jun_viridiplantae_memtax2.txt" )
fi

if profile_enabled xprize; then
	DB_PREFIXES+=( "db/targets_All_tagged_nr95" )
	DB_FILES+=( "db/ITS2nr_2024Jun_viridiplantae_memtax2.txt" )
	DB_FILES+=( "db/metazoa_spec_basics.txt" )
	DB_FILES+=( "db/viridiplantae_spec_basics.txt" )
	DB_FILES+=( "db/GBIF_iNAturalist_2024Jun_RioNegro_metazoa_gns.txt" )
	DB_FILES+=( "db/GBIF_iNAturalist_2024Jun_RioNegro_viridiplantae_gns.txt" )
fi

if profile_enabled test; then
	DB_PREFIXES+=( "db/targets_All_tagged_nr95" )
	DB_PREFIXES+=( "db/COInr_nr99_lca" )
	DB_PREFIXES+=( "db/ITS2_nr99_lca" )
fi

copied_prefixes=$'\n'
for prefix in "${DB_PREFIXES[@]}"; do
	if not_recorded_yet "$prefix" "$copied_prefixes"; then
		copied_prefixes="${copied_prefixes}${prefix}"$'\n'
		copy_prefix_family "$prefix"
	fi
done

copied_files=$'\n'
for rel in "${DB_FILES[@]}"; do
	if not_recorded_yet "$rel" "$copied_files"; then
		copied_files="${copied_files}${rel}"$'\n'
		copy_path "$rel"
	fi
done

copied_dirs=$'\n'
for rel in "${DB_DIRS[@]}"; do
	if not_recorded_yet "$rel" "$copied_dirs"; then
		copied_dirs="${copied_dirs}${rel}"$'\n'
		copy_path "$rel"
	fi
done

printf 'Release directory prepared at: %s\n' "$OUTDIR"
if [ -n "$PROFILES" ]; then
	printf 'Bundled profiles: %s\n' "$PROFILES"
else
	printf 'Bundled profiles: core runtime only\n'
fi
printf 'Warnings: %s\n' "$WARNINGS"
