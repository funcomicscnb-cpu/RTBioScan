// Pure data class for demultiplexing configuration.
// No Nextflow script-context dependencies — safe to live in lib/.
class DemuxConfig {
    final boolean enabled;
    final String mode;
    final String indexes;
    final String primers;
    DemuxConfig(boolean enabled, String mode, String indexes, String primers) {
        this.enabled = enabled;
        this.mode = mode;
        this.indexes = indexes;
        this.primers = primers;
    }
    String toShell() {
        return """DO_DEMUX=${enabled ? 1 : 0}
DEMUX_MODE=\"${mode ?: 'off'}\"
INDEXES_PATH=\"${indexes ?: ''}\"
PRIMERS_PATH=\"${primers ?: ''}\"
"""
    }
}
