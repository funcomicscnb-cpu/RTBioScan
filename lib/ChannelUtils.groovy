// Channel join helpers — no Nextflow script-context dependencies, safe to live in lib/.
// Note: tuple() is a Nextflow script-context function; [a, b] list literals are used
// instead (semantically identical in all Nextflow channel contexts).
class ChannelUtils {

    static def normalizeRoundJoinRow(row, side, label = null) {
        def values = row instanceof List ? new ArrayList(row) : [row]
        def joinContext = label ? " for ${label}" : ""
        if (values.size() < 2) {
            throw new IllegalArgumentException("strictRoundJoin${joinContext} requires tuples with at least 2 key fields on ${side} channel: ${values}")
        }
        def barcodeKey = values[0]?.toString()?.trim()
        def roundKey = values[1]?.toString()?.trim()
        if (!barcodeKey || !roundKey) {
            throw new IllegalArgumentException("strictRoundJoin${joinContext} requires non-blank barcode and round_barcode on ${side} channel: ${values}")
        }
        [barcodeKey, roundKey] + values.subList(2, values.size())
    }

    static def strictRoundJoin(left, right, label = null) {
        def leftKeyed = left.map { row ->
            def values = normalizeRoundJoinRow(row, 'left', label)
            [[values[0], values[1]], values]
        }
        def rightKeyed = right.map { row ->
            def values = normalizeRoundJoinRow(row, 'right', label)
            [[values[0], values[1]], values]
        }
        leftKeyed
            .join(rightKeyed, by: 0, failOnMismatch: true, failOnDuplicate: true)
            .map { _key, leftValues, rightValues ->
                def merged = []
                merged.addAll(leftValues)
                if (rightValues.size() > 2) {
                    merged.addAll(rightValues.subList(2, rightValues.size()))
                }
                merged
            }
    }

    static def isStrictRoundJoinChannelLike(ch) {
        // Use the read-channel capability strictRoundJoin actually consumes instead of
        // pinning the helper to one concrete runtime class name.
        ((ch?.metaClass?.respondsTo(ch, 'getValAsync')) ?: []).size() > 0
    }

    static def strictRoundJoinAll(channels, label = null) {
        if (channels == null) {
            throw new IllegalArgumentException('strictRoundJoinAll requires at least one channel')
        }
        // Accept list/array-style channel bundles while still failing fast if a caller
        // passes something that is not an actual Nextflow channel.
        def channelList
        if (channels instanceof Iterable) {
            channelList = channels.toList()
        } else if (channels.getClass().isArray()) {
            channelList = channels.toList()
        } else {
            throw new IllegalArgumentException('strictRoundJoinAll requires an iterable of channels')
        }
        if (channelList.isEmpty()) {
            throw new IllegalArgumentException('strictRoundJoinAll requires at least one channel')
        }
        // Validate the folded inputs up front so misuse fails as a helper-contract error
        // instead of as a later operator-method exception.
        channelList.eachWithIndex { ch, idx ->
            if (!isStrictRoundJoinChannelLike(ch)) {
                throw new IllegalArgumentException("strictRoundJoinAll requires channel-like inputs; element ${idx + 1} was ${ch?.getClass()?.name ?: 'null'}")
            }
        }
        // Carry a label forward through the fold so malformed-row errors identify the
        // specific multi-channel join step that failed.
        channelList.tail().withIndex().inject(channelList[0]) { acc, entry ->
            def ch = entry[0]
            def idx = entry[1]
            def joinLabel = label ? "${label}[${idx + 2}]" : null
            strictRoundJoin(acc, ch, joinLabel)
        }
    }
}
