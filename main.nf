// ============================================================
// PREAMBLE — Groovy helpers, params normalization, channel bootstrap
// ============================================================
// --- PREAMBLE §1: Groovy helper classes and closures ---
// DemuxConfig class → lib/DemuxConfig.groovy (auto-loaded by Nextflow)
// hasFastaHeader(), getDemuxConfig() → bottom of this file (hoisted methods)
// parseBool(), parseBoolStrict() → bottom of this file (hoisted methods)
// ChannelUtils (strictRoundJoin family) → lib/ChannelUtils.groovy (auto-loaded by Nextflow)

// Compute demultiplexing enablement once and reuse it everywhere.
// This avoids mismatches where params.demultiplex_mode='auto' or 'on' but the files are missing/empty.
def demuxCfg = getDemuxConfig()
def demuxEnabledForRun = false
try {
    def idxOk = hasFastaHeader(demuxCfg.indexes)
    def priOk = hasFastaHeader(demuxCfg.primers)
    if (demuxCfg.mode == 'primers_only') {
        demuxEnabledForRun = demuxCfg.enabled && priOk
    } else if (demuxCfg.mode == 'full') {
        demuxEnabledForRun = demuxCfg.enabled && idxOk && priOk
    } else {
        demuxEnabledForRun = false
    }
} catch (Exception e) {
    demuxEnabledForRun = false
}
def demuxEnabledInt = demuxEnabledForRun ? 1 : 0
def fullDemuxBranchWillRun = demuxEnabledForRun &&
    demuxCfg.mode == 'full' &&
    isRegularFilePath(demuxCfg.indexes) &&
    isRegularFilePath(demuxCfg.primers)
if ((params.replicate_mode?.toString()?.trim()?.toLowerCase() ?: 'collapse') == 'track' && fullDemuxBranchWillRun) {
    def trackArtifacts = getTrackArtifactPaths()
    requireTrackArtifactPath(trackArtifacts.trackRoster, 'track_roster.tsv', 'when entering the full-demux branch')
    requireTrackArtifactPath(trackArtifacts.trackActiveUnits, 'track_active_units.txt', 'when entering the full-demux branch')
    requireTrackArtifactPath(trackArtifacts.trackIdentity, 'track_identity.tsv', 'when entering the full-demux branch')
}

custom_runName = params.name
run_name = params.name
if (!(workflow.runName ==~ /[a-z]+_[a-z]+/)) {
    custom_runName = workflow.runName
}
if (!run_name) {
    run_name = custom_runName ?: workflow.runName
}

	def available_cpus = Runtime.runtime.availableProcessors()

	// Header log info (matches main_barcoding.nf behavior)
	log.info nfcoreHeader()

	def summary = [
	    'Run Name'          : workflow.runName,
	    'Launch dir'        : workflow.launchDir,
	    'Working dir'       : workflow.workDir,
    'Script dir'        : workflow.projectDir,
    'User'              : workflow.userName,
    'Config Profile'    : workflow.profile
]

def reads

// strictRoundJoin family → lib/ChannelUtils.groovy; call via ChannelUtils.strictRoundJoin(...)
// --- PREAMBLE §2: Input channel construction ---
def watchEnabled = parseBool(params.watch, true)
def runModeRaw = params.run_mode?.toString()?.trim()?.toLowerCase()
if (!runModeRaw) {
    runModeRaw = 'realtime'
}
if (!(runModeRaw in ['batch', 'realtime'])) {
    exit 1, "Invalid --run_mode '${params.run_mode}'. Allowed values: batch, realtime"
}
def runMode = runModeRaw
params.run_mode = runMode
def activeProfiles = (workflow.profile ?: '').tokenize(',')*.trim().findAll { it }
def usingDockerProfile = activeProfiles.contains('docker')
def usingCondaProfile = activeProfiles.contains('conda')

def readsProvided = params.reads?.toString()?.trim()
def readsRtProvided = params.reads_rt?.toString()?.trim()
def readsFlagProvided = (workflow.commandLine ?: '') ==~ /(?s).*(^|\\s)--reads(=|\\s).*/
def deriveGlobRoot = { pattern ->
    try {
        def s = pattern?.toString()
        if (!s) return null
        def idx = s.length()
        ['*','?','[','{'].each { ch ->
            def i = s.indexOf(ch)
            if (i >= 0 && i < idx) idx = i
        }
        def prefix = (idx == s.length()) ? s : s.substring(0, idx)
        if (!prefix) return null
        def explicitDir = prefix.endsWith('/') || prefix.endsWith(File.separator)
        def cleaned = prefix.replaceAll(/[\\\\/]+$/, '')
        if (!cleaned) return new File(".").getPath()
        def f = new File(cleaned)
        def p = (explicitDir || idx == s.length()) ? f.getPath() : f.getParent()
        if (!p) p = new File(".").getPath()
        return p
    } catch (Exception e) {
        return null
    }
}
if (readsRtProvided) {
    if (runMode != 'realtime') {
        exit 1, "--reads_rt is only supported with --run_mode realtime"
    }
    if (readsFlagProvided && readsProvided && readsProvided != readsRtProvided) {
        log.warn "Both --reads and deprecated --reads_rt were provided; using --reads_rt"
    }
    readsProvided = readsRtProvided
    log.warn "--reads_rt is deprecated; use --reads with --run_mode realtime"
}

if (!readsProvided) {
    exit 1, "Please provide --reads (and optionally --run_mode batch|realtime)"
}
def effectiveReads = readsProvided

if (runMode == 'batch') {
    reads = Channel
        .fromPath(effectiveReads, checkIfExists: true)
        .ifEmpty { exit 1, """Cannot find any reads matching: ${effectiveReads}
NB: Path needs to be enclosed in quotes!""" }
} else {
    def realtimePattern = effectiveReads.toString()
    def realtimeDir = deriveGlobRoot(realtimePattern)
    def dirOk = (realtimeDir && new File(realtimeDir).exists() && new File(realtimeDir).isDirectory())
    if (!dirOk) {
        exit 1, "Realtime input directory does not exist: ${realtimeDir ?: '(unresolved from --reads)'}"
    }
    def initialReads = Channel.fromPath(realtimePattern)
    if (watchEnabled) {
        def watchedReads = Channel.watchPath(realtimePattern)
        reads = initialReads.concat(watchedReads)
    } else {
        reads = initialReads.ifEmpty { exit 1, """Cannot find any realtime reads matching: ${effectiveReads}
NB: Path needs to be enclosed in quotes!""" }
    }
}

// Rolling-state namespace. Use an explicit --state_id for stable restart/restore across different Nextflow run names.
// If not provided, default to the current run name (isolates runs, but you must pass --state_id to restore the same state).
def stateIdRaw = null
if (params.containsKey('state_id') && params.state_id != null) {
    stateIdRaw = params.state_id.toString().trim()
}
if (!stateIdRaw) {
    stateIdRaw = (custom_runName ?: workflow.runName)?.toString()
}
// Keep it filesystem-friendly.
def stateId = stateIdRaw.replaceAll(/[^A-Za-z0-9_.-]+/, "_")
def ongoingStateDir = params.outdir ? "${params.outdir}/temp/ongoing/state/${stateId}" : null
def currentStateDir = params.outdir ? "${params.outdir}/temp/current/state/${stateId}" : null
def currentResultsStateDir = params.outdir ? "${params.outdir}/current/state/${stateId}" : null
def ongoingResultsStateDir = params.outdir ? "${params.outdir}/ongoing/state/${stateId}" : null

// Skip POD5 files already recorded as processed in the rolling state.
// This is important for crash/restart scenarios where the input directory still contains old POD5 files.
def donePod5Path = ongoingStateDir ? "${ongoingStateDir}/_state/done_pod5.txt" : null
def donePod5RootDir = null
try {
    if (runMode == 'realtime' && effectiveReads) {
        // Realtime reads are typically a glob like `/path/*.pod5` or `/path/*pod5`.
        // Use its parent dir as a best-effort anchor for backward-compatible basename entries.
        donePod5RootDir = new File(effectiveReads.toString()).getParent()
    }
} catch (Exception e) {
    donePod5RootDir = null
}
def pod5Key = { p ->
    try {
        def path = (p instanceof java.nio.file.Path) ? (java.nio.file.Path)p : java.nio.file.Paths.get(p.toString())
        // Resolve symlinks for stable identity when we stage intake files as symlinks.
        def real = path.toRealPath()
        def attrs = java.nio.file.Files.readAttributes(real, java.nio.file.attribute.BasicFileAttributes)
        def size = attrs.size().toString()
        def mtime = ((long)(attrs.lastModifiedTime().toMillis() / 1000L)).toString()
        def inode = ''
        try {
            def ino = java.nio.file.Files.getAttribute(real, "unix:ino")
            inode = ino != null ? ino.toString() : ''
        } catch (Exception e) {
            inode = ''
        }
        if (!inode) inode = '0'
        return [real.toString(), size, mtime, inode]
    } catch (Exception e) {
        try {
            def f = new File(p.toString())
            def real = f.getCanonicalPath()
            def size = f.exists() ? f.length().toString() : ''
            def mtime = f.exists() ? ((long)(f.lastModified() / 1000L)).toString() : ''
            return [real, size, mtime, '0']
        } catch (Exception e2) {
            return [p.toString(), '', '', '0']
        }
    }
}
def donePod5Cache = [mtime:0L, keys:[] as Set, paths:[] as Set, basenames:[] as Set]
def loadDonePod5Cache = {
    if (!donePod5Path) return
    def f = file(donePod5Path)
    if (!f.exists() || f.size() == 0) return
    def mtime = f.lastModified()
    if (mtime == donePod5Cache.mtime) return
    def keys = [] as Set
    def paths = [] as Set
    def basenames = [] as Set
    f.eachLine { raw ->
        def line = raw?.trim()
        if (!line) return
        if (line.contains('\t')) {
            def parts = line.split('\t', -1)
            if (parts.size() >= 4 && (parts[1] ==~ /[0-9]+/) && (parts[2] ==~ /[0-9]+/) && (parts[3] ==~ /[0-9]+/)) {
                keys.add("${parts[0]?.trim()}\t${parts[1]?.trim()}\t${parts[2]?.trim()}\t${parts[3]?.trim()}")
                paths.add(parts[0]?.trim())
                return
            }
            if (parts.size() >= 3 && (parts[1] ==~ /[0-9]+/) && (parts[2] ==~ /[0-9]+/)) {
                keys.add("${parts[0]?.trim()}\t${parts[1]?.trim()}\t${parts[2]?.trim()}")
                paths.add(parts[0]?.trim())
                return
            }
            def storedKey = parts[0]?.trim()
            if (storedKey) paths.add(storedKey)
            return
        }
        basenames.add(line)
    }
    donePod5Cache = [mtime:mtime, keys:keys, paths:paths, basenames:basenames]
}
def isDonePod5 = { p ->
    if (!donePod5Path) return false
    loadDonePod5Cache()
    def name = p instanceof java.nio.file.Path ? p.getFileName().toString() : new File(p.toString()).getName()
    def keyParts = pod5Key(p)
    def keyPath = keyParts[0]
    def keySize = keyParts[1]
    def keyMtime = keyParts[2]
    def keyInode = keyParts[3]
    def keyComposite4 = (keyPath && keySize && keyMtime && keyInode) ? "${keyPath}\t${keySize}\t${keyMtime}\t${keyInode}" : null
    def keyComposite3 = (keyPath && keySize && keyMtime) ? "${keyPath}\t${keySize}\t${keyMtime}" : null
    def parent = null
    try {
        parent = p instanceof java.nio.file.Path ? ((java.nio.file.Path)p).getParent()?.toString() : new File(p.toString()).getParent()
    } catch (Exception e) {
        parent = null
    }
    if (keyComposite4 && donePod5Cache.keys.contains(keyComposite4)) return true
    if (keyComposite3 && donePod5Cache.keys.contains(keyComposite3)) return true
    if (keyPath && donePod5Cache.paths.contains(keyPath)) return true
    if (donePod5RootDir && parent && parent == donePod5RootDir) {
        return donePod5Cache.basenames.contains(name)
    }
    return false
}
reads = reads.filter { p -> !isDonePod5(p) }.distinct()

// --- PREAMBLE §3: Parameter defaults and validation ---
summary['Reads'] = effectiveReads
summary['Run Mode'] = runMode
summary['Run Name'] = custom_runName ?: workflow.runName
summary['State ID'] = stateId

def timingCfg  = validateTimingLockParams()
def staleLockTtlMinutesStr      = timingCfg.staleLockTtlMinutesStr
def roundLockScopeCanonical     = timingCfg.roundLockScopeCanonical

def forkCfg    = validateForkParams()
def maxForksFastVal             = forkCfg.maxForksFastVal
def maxForksReportingVal        = forkCfg.maxForksReportingVal
def maxForksConsensusVal        = forkCfg.maxForksConsensusVal
def maxForksCoreCpuVal          = forkCfg.maxForksCoreCpuVal

def htmlCfg    = validateHtmlReportParams()
def htmlReportEnabled           = htmlCfg.htmlReportEnabled
def htmlReportAutoRefresh       = htmlCfg.htmlReportAutoRefresh
def htmlReportRefreshSecondsStr = htmlCfg.htmlReportRefreshSecondsStr
def htmlReportUrlPrefix         = htmlCfg.htmlReportUrlPrefix
def htmlReportSamplePlotMaxStr  = htmlCfg.htmlReportSamplePlotMaxStr

// Avoid "Access to undefined parameter" warnings for optional params.
if ( !params.containsKey('hostnames') || params.hostnames == null ) {
    params.hostnames = [:]
}
if ( !params.containsKey('restart_force') || params.restart_force == null ) {
    params.restart_force = false
}

def otuRecoveryCfg = validateOtuRecoveryPruneParams()
def otuDbOnlyPolicyCanonical                 = otuRecoveryCfg.otuDbOnlyPolicyCanonical
def otuPrunedRecoveryEnabled                 = otuRecoveryCfg.otuPrunedRecoveryEnabled
def otuPrunedRecoveryIdentity                = otuRecoveryCfg.otuPrunedRecoveryIdentity
def otuPrunedRecoveryTargetPolicyCanonical   = otuRecoveryCfg.otuPrunedRecoveryTargetPolicyCanonical
def otuPrunedRecoveryFailurePolicyCanonical  = otuRecoveryCfg.otuPrunedRecoveryFailurePolicyCanonical
def pruneUnassignedClusters                  = otuRecoveryCfg.pruneUnassignedClusters
def pruneUnassignedDropReads                 = otuRecoveryCfg.pruneUnassignedDropReads
def pruneUnassignedGraceRoundsStr            = otuRecoveryCfg.pruneUnassignedGraceRoundsStr
def pruneUnassignedKeepTopStr                = otuRecoveryCfg.pruneUnassignedKeepTopStr
def otuPruneFrozenPolicyCanonical            = otuRecoveryCfg.otuPruneFrozenPolicyCanonical
def otuPruneSamplesFileValue                 = otuRecoveryCfg.otuPruneSamplesFileValue
def otuLockForcePruneMaxFastaMbStr           = otuRecoveryCfg.otuLockForcePruneMaxFastaMbStr
def otuForcePruneOverride                    = otuRecoveryCfg.otuForcePruneOverride
def otuConsolidatedKeysMixedPolicyCanonical  = otuRecoveryCfg.otuConsolidatedKeysMixedPolicyCanonical

def consensusCfg = validateConsensusAssignParams()
def consensusKeepOriginalReads           = consensusCfg.consensusKeepOriginalReads
def consensusZeroEmitPolicyCanonical     = consensusCfg.consensusZeroEmitPolicyCanonical
def consensusIdMismatchPolicyCanonical   = consensusCfg.consensusIdMismatchPolicyCanonical
def consensusCacheBelowMinPolicyCanonical = consensusCfg.consensusCacheBelowMinPolicyCanonical
def assignProtLevelCanonical             = consensusCfg.assignProtLevelCanonical
def pruneCumulativePoolAll               = consensusCfg.pruneCumulativePoolAll

def otuClusterCfg = validateOtuClusterLockParams()
def otuLockRatioStr                  = otuClusterCfg.otuLockRatioStr
def otuLockMinConsReadsStr           = otuClusterCfg.otuLockMinConsReadsStr
def otuLockMinStableRoundsStr        = otuClusterCfg.otuLockMinStableRoundsStr
def otuLockRevalidateEveryRoundsStr  = otuClusterCfg.otuLockRevalidateEveryRoundsStr
def otuSizeStreakModeCanonical       = otuClusterCfg.otuSizeStreakModeCanonical
def otuSizeStreakMinRoundsStr        = otuClusterCfg.otuSizeStreakMinRoundsStr

def otuBlastCfg = validateOtuBlastParams()
def otuBlastMinMembersStr                    = otuBlastCfg.otuBlastMinMembersStr
def otuBlastFilterModeCanonical              = otuBlastCfg.otuBlastFilterModeCanonical
def otuBlastForceUseFiltered                 = otuBlastCfg.otuBlastForceUseFiltered
def otuBlastFilterSkipRoundsCanonical        = otuBlastCfg.otuBlastFilterSkipRoundsCanonical
def otuBlastUnassignedGraceRoundsStr         = otuBlastCfg.otuBlastUnassignedGraceRoundsStr
def otuBlastEnforceMissingMaxFracStr         = otuBlastCfg.otuBlastEnforceMissingMaxFracStr
def otuBlastEnforceNoClustersPolicyCanonical = otuBlastCfg.otuBlastEnforceNoClustersPolicyCanonical
def otuBlastUnassignedModeCanonical          = otuBlastCfg.otuBlastUnassignedModeCanonical
def otuUnassignedStreakModeCanonical         = otuBlastCfg.otuUnassignedStreakModeCanonical

if ( !params.containsKey('make_round_tar') || params.make_round_tar == null ) {
    params.make_round_tar = false
}
def _runId        = params.run_id?.toString()?.trim() ?: ""
def podBaseDir    = _runId ? "${workflow.launchDir}/results/pod5/${_runId}"         : "${workflow.launchDir}/results/pod5"
def sampleInfoDir = _runId ? "${workflow.launchDir}/results/sample_info/${_runId}" : "${workflow.launchDir}/results/sample_info"
if ( !params.containsKey('replicate_mode') || params.replicate_mode == null ) {
    params.replicate_mode = 'collapse'
}
def _replicateModeCanonical = params.replicate_mode.toString().trim().toLowerCase()
if (!(_replicateModeCanonical in ['collapse', 'track'])) {
    exit 1, "Invalid --replicate_mode '${params.replicate_mode}'. Allowed values: collapse, track"
}
params.replicate_mode = _replicateModeCanonical
// Optional "species/genus of interest" inputs (used for filtering which taxa appear in plots/tables).
// Keep defaults defined to avoid Nextflow "Access to undefined parameter" warnings.
if ( !params.containsKey('metazoa_spc_basics') ) {
    params.metazoa_spc_basics = null
}
if ( !params.containsKey('viridiplantae_spc_basics') ) {
    params.viridiplantae_spc_basics = null
}
if ( !params.containsKey('local_metazoa_gns') ) {
    params.local_metazoa_gns = null
}
if ( !params.containsKey('local_viridiplantae_gns') ) {
    params.local_viridiplantae_gns = null
}

// Dorado runtime device selection: "metal" (default on macOS), "cpu", or "cuda:0" etc.
if ( !params.containsKey('dorado_device') || params.dorado_device == null ) {
    params.dorado_device = 'metal'
}

// Fail fast if Dorado or its model directories are missing/misconfigured.
// (We can't reliably sanity-check model load here without executing Dorado, so we do structural checks.)
def _doradoBinRaw = params.dorado_bin?.toString() ?: "bin/dorado/bin/dorado"
def doradoBin = _doradoBinRaw.startsWith('/') ? _doradoBinRaw : "${baseDir}/${_doradoBinRaw}"
def shellQuote = { Object value ->
    def s = value == null ? '' : value.toString()
    return "'${s.replace("'", "'\"'\"'")}'"
}
def resolveModelPath = { String p ->
    if (!p) return null
    p.startsWith('/') ? p : "${baseDir}/${p}"
}
def doradoFastModel = resolveModelPath(params.fast_model?.toString())
def doradoHacModel  = resolveModelPath(params.hac_model?.toString())
def doradoSupModel  = resolveModelPath(params.sup_model?.toString())
def resolveConfigPath = { String p ->
    if (!p) return null
    p.startsWith('/') ? p : "${baseDir}/${p}"
}
def validateDoradoModel = { String paramName, String resolvedPath ->
    if (!resolvedPath) return
    def p = file(resolvedPath)
    if (!p.exists()) {
        exit 1, "Missing Dorado model path for ${paramName}: ${p} (set --${paramName} to a valid model dir)."
    }
    if (!p.isDirectory()) {
        exit 1, "Dorado model path for ${paramName} is not a directory: ${p}"
    }
    def cfg = file("${p}/config.toml")
    if (!cfg.exists()) {
        exit 1, "Dorado model dir for ${paramName} does not contain config.toml: ${p}"
    }
}
if (!file(doradoBin).exists()) {
    exit 1, "Missing Dorado binary: ${doradoBin}"
}
validateDoradoModel('fast_model', doradoFastModel)
validateDoradoModel('hac_model', doradoHacModel)
validateDoradoModel('sup_model', doradoSupModel)

def doradoBasecallerHelp = ''
def doradoHelpExit = -1
try {
    def proc = new ProcessBuilder(doradoBin, 'basecaller', '--help').redirectErrorStream(true).start()
    doradoBasecallerHelp = proc.inputStream.getText('UTF-8')
    doradoHelpExit = proc.waitFor()
} catch (Exception e) {
    log.warn "Unable to inspect Dorado basecaller help for ${doradoBin}: ${e.message}"
}
if (doradoHelpExit != 0 || !doradoBasecallerHelp?.trim()) {
    log.warn "Falling back to legacy Dorado tuning flags because '${doradoBin} basecaller --help' did not return usable output"
}
def doradoHelpLc = doradoBasecallerHelp?.toLowerCase() ?: ''
def helpHasOption = { String longOpt, String shortOpt = null ->
    if (doradoHelpExit != 0 || !doradoBasecallerHelp) {
        return false
    }
    def escapedLong = java.util.regex.Pattern.quote(longOpt)
    def escapedShort = shortOpt ? java.util.regex.Pattern.quote(shortOpt) : null
    def pattern = escapedShort
        ? ~/(?m)^\s*(?:${escapedShort},\s*)?${escapedLong}\b/
        : ~/(?m)^\s*${escapedLong}\b/
    return (doradoBasecallerHelp =~ pattern).find()
}
def doradoSupportsEmitSam = helpHasOption('--emit-sam')
def doradoSupportsBatchsize = helpHasOption('--batchsize', '-b')
def doradoSupportsChunksize = helpHasOption('--chunksize', '-c')
def doradoSupportsOverlap = helpHasOption('--overlap', '-o')
def buildDoradoBasecallerArgs = { String stageName, String prefix ->
    def args = []
    if (doradoSupportsEmitSam) {
        args << '--emit-sam'
    }

    def overlap = params."${prefix}_overlap"
    if (overlap != null && overlap.toString() != 'null') {
        if (doradoSupportsOverlap) {
            args.addAll(['-o', overlap.toString()])
        } else if (doradoHelpExit == 0) {
            log.warn "Ignoring ${prefix}_overlap=${overlap} for Dorado ${stageName}; ${doradoBin} basecaller does not advertise an overlap flag"
        }
    }

    def chunksize = params."${prefix}_chunksize"
    if (chunksize != null && chunksize.toString() != 'null') {
        if (doradoSupportsChunksize) {
            args.addAll(['-c', chunksize.toString()])
        }
    }

    def batchsize = params."${prefix}_batchsize"
    if (batchsize != null && batchsize.toString() != 'null') {
        if (doradoSupportsBatchsize) {
            args.addAll(['-b', batchsize.toString()])
        } else if (doradoHelpExit == 0) {
            log.warn "Ignoring ${prefix}_batchsize=${batchsize} for Dorado ${stageName}; ${doradoBin} basecaller does not advertise a batch-size flag"
        }
    }

    args.collect(shellQuote).join(' ')
}
def doradoFastBasecallerArgs = buildDoradoBasecallerArgs('FAST', 'fast')
def doradoHacBasecallerArgs  = buildDoradoBasecallerArgs('HAC', 'hac')
def doradoSupBasecallerArgs  = buildDoradoBasecallerArgs('SUP', 'sup')

// Fail fast on missing BLAST DB inputs.
def validateDbPrefix = { String paramName, String rawPath ->
    if (!rawPath || rawPath == 'null') {
        exit 1, "Missing required parameter --${paramName}"
    }
    def resolved = resolveConfigPath(rawPath)
    def prefixFile = new File(resolved)
    def parent = prefixFile.getParentFile() ?: new File('.')
    def base = prefixFile.getName()
    def matches = parent.exists() ? (parent.listFiles()?.any { f -> f.name == base || f.name.startsWith("${base}.") } ?: false) : false
    if (!matches) {
        exit 1, "BLAST DB prefix for --${paramName} was not found: ${resolved}"
    }
}
def validateRequiredDir = { String paramName, String rawPath ->
    if (!rawPath || rawPath == 'null') {
        exit 1, "Missing required parameter --${paramName}"
    }
    def resolved = resolveConfigPath(rawPath)
    def d = new File(resolved)
    if (!d.exists() || !d.isDirectory()) {
        exit 1, "Required directory for --${paramName} was not found: ${resolved}"
    }
}
def validateOptionalFile = { String paramName, String rawPath ->
    if (!rawPath || rawPath == 'null') return
    def resolved = resolveConfigPath(rawPath)
    def f = new File(resolved)
    if (!f.exists() || !f.isFile()) {
        exit 1, "Configured file for --${paramName} was not found: ${resolved}"
    }
}
def validateOptionalFileOrDisable = { String paramName, String rawPath ->
    if (!rawPath || rawPath == 'null') return
    def resolved = resolveConfigPath(rawPath)
    def f = new File(resolved)
    if (!f.exists() || !f.isFile()) {
        log.warn "Configured file for --${paramName} was not found: ${resolved} — disabling this input"
        params."${paramName}" = 'null'
    }
}
validateDbPrefix('blast_filter_db', params.blast_filter_db?.toString())
validateRequiredDir('blast_taxdb', params.blast_taxdb?.toString())
validateOptionalFile('nonncbi_id2lineage_target', params.nonncbi_id2lineage_target?.toString())

// --- PREAMBLE §4: Startup operations (restart/restore handler) ---
// Apply rolling-state restart semantics at script evaluation time (i.e. always runs, even with `-resume`).
// This avoids `-resume` skipping restore/reset logic when processes are resumed from cache.
def effectiveRestartMode = params.restart_mode?.toString()?.trim()?.toLowerCase()
if (!effectiveRestartMode || effectiveRestartMode == 'null') {
    effectiveRestartMode = 'off'
}
	if (effectiveRestartMode == 'off' && (params.restart ?: 0) != 0) {
	    effectiveRestartMode = 'restore'
	}
	// Safety: require an explicit rolling-state namespace when using restore/reset.
	// Otherwise a new Nextflow run name will default to a new stateId and "restore" can appear ignored.
	if (effectiveRestartMode in ['restore', 'reset']) {
	    def rawState = (params.containsKey('state_id') && params.state_id != null) ? params.state_id.toString().trim() : ''
	    if (!rawState) {
	        exit 1, "restart_mode=${effectiveRestartMode} requires --state_id (the rolling-state namespace to restore/reset)."
	    }
	}
	if (effectiveRestartMode in ['restore', 'reset']) {
	    def env = new HashMap(System.getenv())
	    env.MODE = effectiveRestartMode
	    env.OUTDIR = params.outdir?.toString() ?: ''
	    env.LOCK_WAIT = (params.lock_wait_seconds ?: 300).toString()
    env.RUN_NAME = (custom_runName ?: workflow.runName)?.toString() ?: ''
    env.STATE_ID = (stateId ?: '') as String
    env.FORCE = ((params.restart_force ?: false) ? '1' : '0')

    // Avoid Groovy's ProcessGroovyMethods (`.execute()`, `.waitForProcessOutput()`), which are not available
    // in some Nextflow DSL1 runtimes. Use plain Java ProcessBuilder instead.
    def pb = new ProcessBuilder('/bin/bash', "${baseDir}/bin/restart_handler.sh")
    pb.environment().putAll(env)
    def proc = pb.start()

    def out = new StringBuffer()
    def err = new StringBuffer()
    def tOut = Thread.start {
        proc.inputStream.withReader { r ->
            r.eachLine { line -> out.append(line).append('\n') }
        }
    }
    def tErr = Thread.start {
        proc.errorStream.withReader { r ->
            r.eachLine { line -> err.append(line).append('\n') }
        }
    }

    def exitCode = proc.waitFor()
    tOut.join()
    tErr.join()

    if (exitCode != 0) {
        log.error "Restart mode application failed (restart_mode=${effectiveRestartMode}).\nSTDOUT:\n${out}\nSTDERR:\n${err}"
        throw new RuntimeException("Restart mode failed")
    } else if (err) {
        log.warn err.toString().trim()
    }
}

// --- PREAMBLE §5: Final computed values and channel bootstrap ---
// Used to invalidate Nextflow caching on explicit restore/reset runs. Interpolated into process scripts below.
def restartTokenForCache = (effectiveRestartMode in ['restore','reset']) ? "${effectiveRestartMode}:${custom_runName ?: workflow.runName}" : ''

// formatOtuIdentity() → bottom of this file (hoisted method)

// Check the hostnames against configured profiles
// checkHostname()

Channel.from(summary.collect{ [it.key, it.value] })
    .map { k,v -> "<dt>$k</dt><dd><samp>${v ?: '<span style=\"color:#999999;\">N/A</a>'}</samp></dd>" }
    .reduce { a, b -> return [a, b].join("\n            ") }
    .map { x -> """
    id: 'nf-core-rtnanopipeline-summary'
    description: " - this information is collected when the pipeline is started."
    section_name: 'nf-core/rtnanopipeline Workflow Summary'
    section_href: 'https://github.com/nf-core/rtnanopipeline'
    plot_type: 'html'
    data: |
        <dl class=\"dl-horizontal\">
            $x
        </dl>
    """.stripIndent() }
    .set { ch_workflow_summary }

	
	
// Get fast sequences, identify their kingdom and select the pairs target, kingdom to be kept for further analysis
// ============================================================
// STAGE B — FAST PRE-FILTER (fast_on_target_detection)
// ============================================================
process fast_on_target_detection {
	// Reserve only 1 CPU for scheduling so this task does not starve downstream processes.
	// Actual tool threading is controlled via params.align_threads inside the script.
	cpus 1
	maxForks maxForksFastVal
		
	    input:
	    val(read_path) from reads
	
    output:
    tuple env(barcode), env(round_barcode), file("*.pod5"), file("*reads_target.list") into hq_reads_get
	tuple env(barcode), env(round_barcode), val(read_path) into close_round_ch, get_summary_ch
	tuple  env(barcode), env(round_barcode), file("*fast.fasta"), file("*qced_reads_kingdom.txt"), file("*fast.sam"), file("*reads_target.list") into on_target_report
	
    script:
    read_file = file(read_path)
		"""
		    set -euo pipefail
		shopt -s nullglob
		export LC_ALL=C
			RESTART_TOKEN="${restartTokenForCache}"
			DORADO_LOCK="${ongoingStateDir}/_state/.dorado.lock"
			DORADO_LOCK_WAIT=${params.lock_wait_seconds}
			mkdir -p "${ongoingStateDir}/_state"
			# set -C (noclobber, O_CREAT|O_EXCL): first process writes timestamp, others silently no-op.
			( set -C; date -u '+%Y-%m-%dT%H:%M:%SZ' > "${ongoingStateDir}/_state/run_started_utc.txt" ) 2>/dev/null || true
		BLAST_HEADER='read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species'
		THREADS=${params.align_threads}
		if [ -z "\$THREADS" ] || [ "\$THREADS" = "null" ]; then
			THREADS=${task.cpus}
		fi

	barcode=\$(basename "${workflow.launchDir}")
	round_barcode=\$(basename "${read_file}")
    round_barcode=\${round_barcode%".pod5"}
	
	if [ ! -d ${ongoingStateDir}/ ];
	then
		mkdir -p ${ongoingStateDir}/
	fi
	if [ ! -d ${params.outdir}/ongoing/ ];
	then
		mkdir -p ${params.outdir}/ongoing/
	fi

	# Restart logic is applied once per pipeline invocation, not once per POD5.
	# Restart logic is applied at pipeline startup in Groovy (see top of main.nf)
	# to avoid being skipped when this process is resumed from cache.

		# Ensure rolling-state root exists
		mkdir -p ${ongoingStateDir}/_state
		ROUND_LOCK_SCOPE="${roundLockScopeCanonical}"

	# ---- Round lock: enforce one POD5 in-flight at a time ----
	# This pipeline relies on rolling state files under `${ongoingStateDir}/_state`.
	# If Nextflow overlaps rounds (eg. when 2+ POD5 files exist at launch), later rounds can
	# read stale/partial state and produce fewer assignments (notably consensus).
		# The lock is acquired here and released according to --round_lock_scope:
		# - full_round: at the end of `backup_update_and_clean`
		# - dorado_only: immediately after FAST Dorado basecalling completes
				ROUND_LOCKDIR="${ongoingStateDir}/_state/.round_inflight.lockdir"
				# Clear any stale handoff markers for this same round barcode (e.g. previous crash mid-round).
				rm -f "${ongoingStateDir}/_state/.round_lock_handoff.\$round_barcode"* 2>/dev/null || true
				ROUND_LOCK_HANDOFF_FILE="${ongoingStateDir}/_state/.round_lock_handoff.\$round_barcode"
				ROUND_LOCK_WAIT_MIN=${params.round_lock_wait_minutes}
				STALE_LOCK_TTL_MIN=${params.stale_lock_ttl_minutes}
				LOCK_META="\$ROUND_LOCKDIR/meta.env"
				THIS_HOST="\$(hostname 2>/dev/null || uname -n 2>/dev/null || echo unknown)"
				waited=0
					ROUND_LOCK_EARLY_RELEASED=0
					remove_round_lock_if_stale() {
						rm -rf "\$ROUND_LOCKDIR" 2>/dev/null || true
						rm -f "${ongoingStateDir}/_state/round_inflight.txt" 2>/dev/null || true
						rm -f "${ongoingStateDir}/_state/.round_lock_handoff."* 2>/dev/null || true
					}
					release_round_lock_now() {
						rm -f "\$ROUND_LOCKDIR/meta.env" 2>/dev/null || true
						rmdir "\$ROUND_LOCKDIR" 2>/dev/null || true
						rm -f "${ongoingStateDir}/_state/round_inflight.txt" 2>/dev/null || true
						ROUND_LOCK_EARLY_RELEASED=1
					}
					release_round_lock() {
						if [ "\$ROUND_LOCK_EARLY_RELEASED" -eq 1 ]; then
							return 0
						fi
						# If this round never reached the "handoff" point, release the lock so the next POD5 can proceed.
						if [ ! -f "\$ROUND_LOCK_HANDOFF_FILE" ]; then
							rmdir "\$ROUND_LOCKDIR" 2>/dev/null || true
					rm -f "${ongoingStateDir}/_state/round_inflight.txt" 2>/dev/null || true
				fi
			}
				trap release_round_lock EXIT
				lock_timeout=0
				source "${baseDir}/bin/lib/stale_lock_utils.sh"
				while ! mkdir "\$ROUND_LOCKDIR" 2>/dev/null; do
				reclaimed_round_lock=0
				set +e
				stale_lock_maybe_reclaim \
					"\$ROUND_LOCKDIR" \
					"\$LOCK_META" \
					"\$THIS_HOST" \
					"\$(( ${staleLockTtlMinutesStr} * 60 ))" \
					"round lock" \
					remove_round_lock_if_stale \
					1
				reclaim_status=\$?
				set -e
				if [ "\$reclaim_status" -eq 2 ]; then
					echo "ERROR: stale_lock_maybe_reclaim rejected round lock parameters" 1>&2
					exit 1
				fi
				if [ "\$reclaim_status" -eq 10 ]; then
					reclaimed_round_lock=1
				fi
				if [ "\$reclaimed_round_lock" -eq 1 ]; then
					continue
				fi

				sleep 5
				waited=\$((waited + 5))
				# Periodic status so it doesn't look "stalled" in Nextflow.
				if [ \$((waited % 60)) -eq 0 ]; then
				echo "INFO: Waiting for previous round to finish (lock: \$ROUND_LOCKDIR)" 1>&2
			fi
				if [ "\$ROUND_LOCK_WAIT_MIN" -gt 0 ] && [ "\$waited" -ge \$((ROUND_LOCK_WAIT_MIN * 60)) ]; then
					echo "ERROR: Timed out waiting for previous round to finish (lock: \$ROUND_LOCKDIR)" 1>&2
					lock_timeout=1
					break
				fi
				done
				if [ "\$lock_timeout" -eq 1 ]; then
					echo "ERROR: Round lock wait timed out for \${round_barcode}" 1>&2
					exit 1
				fi
	READ_FILE_ABS=\$(bash ${baseDir}/bin/round_barcode_source_guard.sh \
		--state-dir "${ongoingStateDir}/_state" \
		--round-barcode "\$round_barcode" \
		--read-file "${read_file}")
	if [ -z "\$READ_FILE_ABS" ]; then
		echo "ERROR: round_barcode_source_guard.sh returned empty canonical path for round '\$round_barcode'" 1>&2
		exit 1
	fi
	if [ ! -d ${ongoingStateDir}/\$round_barcode/ ];
	then
		mkdir -p ${ongoingStateDir}/\$round_barcode/
	fi
	{
		printf 'round_barcode=%s\n' "\$round_barcode"
		printf 'started_utc=%s\n' "\$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
		printf 'read_file=%s\n' "\$READ_FILE_ABS"
	} > ${ongoingStateDir}/_state/round_inflight.txt || true

	# Assign a deterministic round index under the existing round lock.
	ROUND_INDEX=\$(bash ${baseDir}/bin/round_index_assign.sh \
		--state-dir "${ongoingStateDir}/_state" \
		--round-barcode "\$round_barcode" \
		--lock-wait ${params.lock_wait_seconds} \
		--no-lock)
	if [ -z "\$ROUND_INDEX" ] || [[ "\$ROUND_INDEX" == *[!0-9]* ]] || [ "\$ROUND_INDEX" -lt 1 ]; then
		echo "ERROR: invalid ROUND_INDEX '\$ROUND_INDEX' for round '\$round_barcode'" 1>&2
		exit 1
	fi
	{
		printf 'round_barcode\tround_index\n'
		printf '%s\t%s\n' "\$round_barcode" "\$ROUND_INDEX"
	} > "${ongoingStateDir}/\$round_barcode/round_index.tsv.tmp" \
		&& mv "${ongoingStateDir}/\$round_barcode/round_index.tsv.tmp" "${ongoingStateDir}/\$round_barcode/round_index.tsv"
	echo "INFO: round index assigned round_barcode=\$round_barcode round_index=\$ROUND_INDEX" 1>&2

		# Dorado retry helper is sourced via `process.beforeScript` (see nextflow.config).

			# Stage POD5 only after acquiring the round lock.
			# Use a symlink when safe; copy inside containers to avoid broken links.
			SYMLINK_OK=${usingDockerProfile || usingCondaProfile ? 0 : 1}
			POD5_LOCAL="\${round_barcode}.pod5"
			rm -f "\$POD5_LOCAL" 2>/dev/null || true
			if [ "\$SYMLINK_OK" -eq 1 ]; then
				ln -s "${read_file}" "\$POD5_LOCAL" 2>/dev/null || cp -p "${read_file}" "\$POD5_LOCAL"
			else
				cp -p "${read_file}" "\$POD5_LOCAL"
			fi

	# Pre-create expected outputs so this round can soft-fail without breaking downstream channels.
	: > \${barcode}_fast.sam
	: > \$barcode\\_fast.fasta
	: > \$barcode\\_qced_reads_kingdom.txt
	: > \$barcode\\_reads_target.list
		
 	wait_seconds=${params.file_wait_minutes * 60}
    elapsed=0
    success=0
	while [ \$elapsed -lt \$wait_seconds ]; do
	        if [ -r ${read_file} ];
			then
				# Dorado can abort on empty/corrupt POD5s. Pre-check reads count when `pod5` is available.
				POD5_LOCAL="\${round_barcode}.pod5"
					pod5_reads=""
						if command -v pod5 >/dev/null 2>&1; then
								# If pod5 inspect fails for any reason, treat it as "unknown" and continue.
								pod5_reads=\$(pod5 inspect summary "\$POD5_LOCAL" 2>/dev/null | perl -ne 'if(/(\\d+)\\s+reads\\b/){print \$1; exit}' || true)
						fi
							if [ -n "\$pod5_reads" ] && [[ "\$pod5_reads" != *[!0-9]* ]] && [ "\$pod5_reads" -eq 0 ]; then
							echo "ERROR: POD5 contains 0 reads; failing this run" 1>&2
							exit 1
						fi
		                    if dorado_basecall_retry "FAST basecalling" "\${barcode}_fast.sam" \
						${baseDir}/bin/with_dorado_lock.sh "\$DORADO_LOCK" "\$DORADO_LOCK_WAIT" "fast_on_target_detection:\$round_barcode" -- \
						${doradoBin} basecaller -x ${params.dorado_device} \
						${doradoFastBasecallerArgs} \
						--min-qscore ${params.on_target_quality_score} \
						${doradoFastModel} ${read_file};
						then
							success=1
						else
							echo "ERROR: FAST basecalling failed for this POD5" 1>&2
							exit 1
						fi
		            break
			else
				if [ -f ${read_file} ];
				then
					echo "ERROR: ${read_file} exists but cannot be read" 1>&2
					exit 1
				fi
	        fi
	        sleep 5
	        elapsed=\$((elapsed + 5))
	    done
			    if [ \$success -ne 1 ];
				then
			        echo "ERROR: Timed out waiting ${params.file_wait_minutes} minute(s) for ${read_file} to become readable" 1>&2
					exit 1
			    fi
		if [ "\$ROUND_LOCK_SCOPE" = "dorado_only" ] && [ "\$ROUND_LOCK_EARLY_RELEASED" -ne 1 ]; then
			: > "\$ROUND_LOCK_HANDOFF_FILE" 2>/dev/null || true
			release_round_lock_now
			echo "INFO: round lock released after FAST basecalling (round_lock_scope=dorado_only)" 1>&2
		fi
		if [ -s \${barcode}_fast.sam ];
		then
		if samtools fasta \$barcode\\_fast.sam | seqkit seq -j "\$THREADS" -g -M ${params.max_read_length} -m ${params.min_read_length} > \$barcode\\_fast.fasta;
		then
			echo "\$barcode\\_fast.fasta created" 1>&2
		else
			echo "WARN: \$barcode\\_fast.fasta not created; continuing with empty placeholders" 1>&2
			: > \$barcode\\_fast.fasta
		fi
	else
		: > \$barcode\\_fast.fasta
	fi
	
	if [ ! -s \$barcode\\_fast.fasta ];
	then
		echo "Warning: No reads passed QC filtering, please check the quality of your run or modify the min/max read length and quality score thresholds" 1>&2
		: > \$barcode\\_qced_reads_kingdom.txt
	else
				if lastal ${baseDir}/${params.blast_filter_db} \$barcode\\_fast.fasta -f BlastTab -P "\$THREADS" | grep -v "^#" | awk '!seen[\$1]++' > \$barcode\\_qced_reads_kingdom.txt;
				then
					echo "\$barcode\\_qced_reads_kingdom.txt created" 1>&2
				else
				echo "WARN: \$barcode\\_qced_reads_kingdom.txt not created; continuing with empty placeholders" 1>&2
				: > \$barcode\\_qced_reads_kingdom.txt
			fi
		fi	
	
	
	_p_targets_ft="${params.targets}"
	_p_target_taxa="${params.target_taxa}"
	IFS='|' read -ra _F_TARGETS    <<< "\$_p_targets_ft"
	IFS='|' read -ra _F_TARGET_TAXA <<< "\$_p_target_taxa"
	: > \$barcode\\_reads_target.list
	for _fi in "\${!_F_TARGETS[@]}"; do
		_ft="\${_F_TARGETS[\$_fi]}"
		_ftx="\${_F_TARGET_TAXA[\$_fi]:-}"
		[ -z "\$_ft" ] && continue
		if [ -n "\$_ftx" ] && [ "\$_ftx" != "null" ]; then
			grep -F "\t\${_ft}|\${_ftx}" \$barcode\\_qced_reads_kingdom.txt | cut -f1 -d"|" | sed 's/\t/|/;' >> \$barcode\\_reads_target.list || true
		else
			grep -F "\t\${_ft}|" \$barcode\\_qced_reads_kingdom.txt | cut -f1 -d"|" | sed 's/\t/|/;' >> \$barcode\\_reads_target.list || true
		fi
	done
	if [ -s \$barcode\\_reads_target.list ]; then
		echo "\$barcode\\_reads_target.list created" 1>&2
	fi
		if [ ! -s \$barcode\\_reads_target.list ];
		then
			echo "WARN: No reads matched the target criteria for this POD5; producing empty placeholders and continuing to next round" 1>&2
			printf "No target reads for %s\n" "\$round_barcode" > ${ongoingStateDir}/\$round_barcode/ROUND_FAILED.txt
			: > \$barcode\\_reads_target.list
		fi

			# In full_round mode keep lock ownership until backup_update_and_clean.
			# In dorado_only mode this handoff/release already happened after FAST basecalling.
			if [ "\$ROUND_LOCK_SCOPE" = "full_round" ]; then
				: > "\$ROUND_LOCK_HANDOFF_FILE" 2>/dev/null || true
			fi
	
		"""
	}

// ============================================================
// STAGE B — REPORTING (_reporting_fast_on_target)
// ============================================================
process _reporting_fast_on_target {

	maxForks maxForksReportingVal

    input:
	tuple val(barcode), val(round_barcode), file(round_fast_fasta), file(round_blast_kingdom), file(round_fast_sam), file(round_reads_target_list) from on_target_report
	
    output:
    tuple val(barcode), val(round_barcode) into fast_control
	tuple val(barcode), val(round_barcode), file("${barcode}_on_target_rpt.txt") into target_rpt_summary
    file("${barcode}_read_info_rpt.txt")
	
	script:

	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"
	THREADS=${task.cpus}

	DORADO_SUMMARY_HEADER='input_filename\tbatch_id\tparent_read_id\tread_id\trun_id\tchannel\tmux\tminknow_events\tstart_time\tduration\tpasses_filtering\ttemplate_start\tnum_events_template\ttemplate_duration\tsequence_length_template\tmean_qscore_template\tpore_type\texperiment_id\tsample_id\tend_reason\n'

	# dorado summary can abort if the SAM is empty/invalid (e.g. no reads in this POD5).
	# In that case we still create a header-only TSV so downstream reporting doesn't fail.
	if [ -s ${round_fast_sam} ] && grep -q '^@' ${round_fast_sam}; then
		${doradoBin} summary ${round_fast_sam} > ${barcode}_round_fast.tsv || true
	fi
	if [ ! -s ${barcode}_round_fast.tsv ]; then
		printf "%b" "\$DORADO_SUMMARY_HEADER" > ${barcode}_round_fast.tsv
	fi
	
	# Generate reports; on failure, fall back to placeholder header-only outputs.
	if ! perl ${baseDir}/bin/reporting_getting_on_target.pl ${barcode}_round_fast.tsv ${round_fast_sam} ${round_blast_kingdom} ${round_reads_target_list} ${params.min_read_length} ${params.max_read_length} ${barcode}; then
		printf 'read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\n' > ${barcode}_read_info_rpt.txt
		printf 'read_id\tqc_filter\tbarcode\tkingdom\tkingdom_perc_identity\tkingdom_aln_length\ton_target_kingdom\n' > ${barcode}_on_target_rpt.txt
	fi
	
	# Accumulate kingdom assignments (allow empty).
	mkdir -p ${ongoingStateDir}/_state
	cat ${round_blast_kingdom} >> ${ongoingStateDir}/_state/${barcode}_blast_kingdom.txt 2>/dev/null || true
	
	
	if [ ! -d ${ongoingStateDir}/${round_barcode}/ ];
	then
		mkdir -p ${ongoingStateDir}/${round_barcode}/
	fi
	
	cp -f ${round_fast_fasta} ${ongoingStateDir}/${round_barcode}/${round_fast_fasta} 2>/dev/null || true
	cp -f ${round_blast_kingdom} ${ongoingStateDir}/${round_barcode}/${round_blast_kingdom} 2>/dev/null || true
	cp -f ${barcode}_round_fast.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_round_fast.tsv 2>/dev/null || true
	cp -f ${barcode}_read_info_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt 2>/dev/null || true
	cp -f ${barcode}_on_target_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_on_target_rpt.txt 2>/dev/null || true
	
	
	"""	
}

// ============================================================
// STAGE C — HAC BASECALLING (hac_basecalling · serialized via .dorado.lock)
// ============================================================
process hac_basecalling {
	cpus 1
	    maxForks 1
	    label 'dorado'
	
    input:
    tuple val(barcode), val(round_barcode), file(read_file), file(target_reads_list) from hq_reads_get
	
    output:
    tuple val(barcode), val(round_barcode), file("${barcode}_hac_filtered.fastq") into barcode_annotate
	tuple val(barcode), val(round_barcode), file(read_file) into hac_blast
	tuple val(barcode), val(round_barcode), file("${barcode}_hac.sam"), file("${barcode}_hac.fastq") into hq_reads_report
	
	script:
    
		"""
		    set -euo pipefail
		shopt -s nullglob
		export LC_ALL=C
			RESTART_TOKEN="${restartTokenForCache}"
			DORADO_LOCK="${ongoingStateDir}/_state/.dorado.lock"
			DORADO_LOCK_WAIT=${params.lock_wait_seconds}
			round_barcode="${round_barcode}"
			mkdir -p "${ongoingStateDir}/_state"

				# Dorado retry helper is sourced via `process.beforeScript` (see nextflow.config).

			# Always create expected outputs, even if there are no target reads.
			: > ${barcode}_hac.sam
		: > ${barcode}_hac.fastq
		: > ${barcode}_hac_filtered.fastq
		SKIP_HAC=0

		cut -f1 -d"|" ${target_reads_list} > ${barcode}_read_names_hq.list || : > ${barcode}_read_names_hq.list
		if [ ! -s ${barcode}_read_names_hq.list ]; then
			echo "WARN: No target read IDs for HAC basecalling; skipping HAC step for this POD5" 1>&2
			SKIP_HAC=1
		fi

		if [ "\$SKIP_HAC" -eq 1 ]; then
			# Keep placeholder outputs and fall through so Nextflow can still capture val outputs.
			: > ${barcode}_hac.sam
			: > ${barcode}_hac.fastq
			: > ${barcode}_hac_filtered.fastq
		else
			
		wait_seconds=${params.file_wait_minutes * 60}
		elapsed=0
		success=0
		while [ \$elapsed -lt \$wait_seconds ]
		do
			if [ -r ${read_file} ];
			then
				# Dorado can abort on empty/corrupt POD5s. Pre-check reads count when `pod5` is available.
				pod5_reads=""
				if command -v pod5 >/dev/null 2>&1; then
					# If pod5 inspect fails for any reason, treat it as "unknown" and continue.
					pod5_reads=\$(pod5 inspect summary "${read_file}" 2>/dev/null | perl -ne 'if(/(\\d+)\\s+reads\\b/){print \$1; exit}' || true)
				fi
						if [ -n "\$pod5_reads" ] && [[ "\$pod5_reads" != *[!0-9]* ]] && [ "\$pod5_reads" -eq 0 ]; then
						echo "ERROR: POD5 contains 0 reads; failing this run" 1>&2
						exit 1
					fi
						if dorado_basecall_retry "HAC basecalling" "${barcode}_hac.sam" \
							${baseDir}/bin/with_dorado_lock.sh "\$DORADO_LOCK" "\$DORADO_LOCK_WAIT" "hac_basecalling:\$round_barcode" -- \
							${doradoBin} basecaller -x ${params.dorado_device} \
							${doradoHacBasecallerArgs} \
							--min-qscore ${params.min_quality_score} -l ${barcode}_read_names_hq.list \
							${doradoHacModel} ${read_file};
					then
						success=1
				else
					# Hard fail: if HAC basecalling fails, continuing would silently reduce downstream assignments.
					exit 1
				fi
			break
		fi
		sleep 5
		elapsed=\$(( elapsed + 5 ))
		done
			if [ \$success -ne 1 ];
			then
				echo "ERROR: Timed out waiting ${params.file_wait_minutes} minute(s) for ${read_file} to become readable" 1>&2
				exit 1
			fi

		samtools fastq -@ ${task.cpus} ${barcode}_hac.sam > ${barcode}_hac.fastq || : > ${barcode}_hac.fastq
			if [ ! -s ${barcode}_hac.fastq ]; then
				echo "ERROR: No HAC reads recovered after successful HAC basecalling" 1>&2
				exit 1
			fi
	perl ${baseDir}/bin/fastq_add_annotations2ids.pl ${target_reads_list}  ${barcode}_hac.fastq >  ${barcode}_hac_annotated.fastq || : > ${barcode}_hac_annotated.fastq
	_p_targets="${params.targets}"
	IFS='|' read -ra _TARGETS  <<< "\$_p_targets"
	_p_min_read_lengths="${params.min_read_lengths}"
	IFS='|' read -ra _MIN_LENS <<< "\$_p_min_read_lengths"
	_p_max_read_lengths="${params.max_read_lengths}"
	IFS='|' read -ra _MAX_LENS <<< "\$_p_max_read_lengths"
	: > ${barcode}_hac_filtered.fastq
	for _i in "\${!_TARGETS[@]}"; do
		grep -A3 "|\${_TARGETS[\$_i]}\$" ${barcode}_hac_annotated.fastq | grep -v "^--\$" \
			| seqkit seq -j ${task.cpus} -g -M "\${_MAX_LENS[\$_i]}" -m "\${_MIN_LENS[\$_i]}" \
			>> ${barcode}_hac_filtered.fastq || true
	done
		
	if [ -f ${barcode}_read_names_hq.list ];
	then
		rm ${barcode}_read_names_hq.list
		echo "Removing ${barcode}_read_names_hq.list" 1>&2
	fi
		if [ -f ${barcode}_hac_annotated.fastq ];
		then
			rm  ${barcode}_hac_annotated.fastq
			echo "Removing ${barcode}_hac_annotated.fastq" 1>&2
		fi

		fi  # SKIP_HAC

	"""
}

hq_reads_report_with_fast_control = ChannelUtils.strictRoundJoin(hq_reads_report, fast_control)

// ============================================================
// STAGE C — REPORTING (_reporting_hac_basecalling)
// ============================================================
process _reporting_hac_basecalling {

	maxForks maxForksReportingVal

    input:
	tuple val(barcode), val(round_barcode), file(round_hac_sam), file(round_hac_fastq) from hq_reads_report_with_fast_control
	
    output:
	tuple val(barcode), val(round_barcode) into hac_read_control
	
    script:

	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"

	DORADO_SUMMARY_HEADER='input_filename\tbatch_id\tparent_read_id\tread_id\trun_id\tchannel\tmux\tminknow_events\tstart_time\tduration\tpasses_filtering\ttemplate_start\tnum_events_template\ttemplate_duration\tsequence_length_template\tmean_qscore_template\tpore_type\texperiment_id\tsample_id\tend_reason\n'

	# dorado summary may abort if SAM is empty/invalid; create a header-only TSV in that case.
	if [ -s ${round_hac_sam} ] && grep -q '^@' ${round_hac_sam}; then
		${doradoBin} summary ${round_hac_sam} > ${barcode}_round_hac.tsv || true
	fi
	if [ ! -s ${barcode}_round_hac.tsv ]; then
		printf "%b" "\$DORADO_SUMMARY_HEADER" > ${barcode}_round_hac.tsv
	fi

	# Append to rolling HAC summary without duplicating headers.
	mkdir -p ${ongoingStateDir}/_state
	if [ ! -f ${ongoingStateDir}/_state/${barcode}_hac.tsv ]; then
		cp ${barcode}_round_hac.tsv ${ongoingStateDir}/_state/${barcode}_hac.tsv
	else
		if [ -s ${barcode}_round_hac.tsv ]; then
			tail -n +2 ${barcode}_round_hac.tsv >> ${ongoingStateDir}/_state/${barcode}_hac.tsv || true
		fi
	fi
	cp -f ${barcode}_round_hac.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_hac.tsv 2>/dev/null || true

	# Update read_info report with HAC columns; on failure, keep the existing on-target report.
	if ! perl ${baseDir}/bin/reporting_getting_hq.pl ${barcode}_round_hac.tsv ${round_hac_sam} ${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt ${params.min_read_length} ${params.max_read_length} ${params.min_quality_score} ${barcode}; then
		cp -f ${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt ${barcode}_read_info_rpt.txt 2>/dev/null || printf 'read_id\tqc_filter\tbarcode\tkingdom\tkingdom_perc_identity\tkingdom_aln_length\ton_target_kingdom\thac_length\thac_mean_qscore\n' > ${barcode}_read_info_rpt.txt
	fi
	cp -f ${barcode}_read_info_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt 2>/dev/null || true
	
	"""
}

// ============================================================
// STAGE E — DEMULTIPLEXING + HQ FILTER (demultiplexing_hq_reads)
// ============================================================
process demultiplexing_hq_reads {
	cpus { params.align_threads }
    maxForks 1

    input:
    tuple val(barcode), val(round_barcode), file(hac_reads_fastq) from barcode_annotate
	
	
    output:
	tuple val(barcode), val(round_barcode), file("${barcode}_hac_sup_annotated_clean.fasta") into otu_analysis
	tuple val(barcode), val(round_barcode), file("${barcode}_hac_sup_annotated_clean.fastq") into annotate_reads_report	
    script:
	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"

	# Ensure expected outputs exist even if no reads are present or demux is disabled.
	: > ${barcode}_hac_sup_annotated_clean.fastq

	# Synchronize reads of rolling SUP fastq state with the writer in `blast_OTU_pretax`.
		STATE_DIR="${ongoingStateDir}/_state"
		SUPFASTQ_LOCK="\${STATE_DIR}/.blastreport_sup.lock"
		LOCK_WAIT=${params.lock_wait_seconds}
		source "${baseDir}/bin/lib/lock_utils.sh"
		init_lock_helpers
		_BASE_DIR="${baseDir}"
		source "\$_BASE_DIR/bin/lib/sup_fastq_restore.sh"

		# Dorado retry helper is sourced via `process.beforeScript` (see nextflow.config).

		mkdir -p "\$STATE_DIR"
			
	    ${demuxCfg.toShell()}
    DO_DEMUX=${demuxEnabledInt}

		restore_rolling_sup_fastq() {
			local local_fastq="${barcode}_blastreport_sup_annotated_pre.fastq"
			ROLLING_SUP_FASTQ=""
			if restore_sup_fastq "\$SUPFASTQ_LOCK" "\$STATE_DIR" "${barcode}" "\$local_fastq" "\$_BASE_DIR"; then
				ROLLING_SUP_FASTQ="\$local_fastq"
			fi
		}

		load_demux_target_arrays() {
			_p_targets="${params.targets}"
			IFS='|' read -ra _TARGETS <<< "\$_p_targets"
			_p_min_read_lengths="${params.min_read_lengths}"
			IFS='|' read -ra _MIN_LENS <<< "\$_p_min_read_lengths"
		}

	    # -- §2: Full-demux mode (barcode + primer cutadapt on rolling SUP + HAC reads) --
    if [ "\$DO_DEMUX" -eq 1 ] && [ "\$DEMUX_MODE" = "full" ] && [ -f "\$INDEXES_PATH" ] && [ -f "\$PRIMERS_PATH" ];
	    then
			# Copy rolling SUP fastq under lock into the task directory for stable demux input
			restore_rolling_sup_fastq

			if [ -n "\$ROLLING_SUP_FASTQ" ];
			then
				load_demux_target_arrays
				for _i in "\${!_TARGETS[@]}"; do
					_t="\${_TARGETS[\$_i]}"
					_ml="\${_MIN_LENS[\$_i]}"
					if grep -A3 "|\${_t}\$" "\$ROLLING_SUP_FASTQ" | grep -v "^--\$" \
						| cutadapt -g file:"\$INDEXES_PATH" -j ${task.cpus} --action=trim --rc -e 0.1 -m "\${_ml}" \
						  --rename '{id}|sup|barcode={cut_prefix}|adapter={adapter_name}' \
						  -o ${barcode}_sup_annotated_\${_t}.fastq - > output_cutadapt_stringent_sup_\${_t}.out;
					then
						if grep adapter ${barcode}_sup_annotated_\${_t}.fastq | grep -F -v no_adapter | sed 's/^@//;' \
							> ${barcode}_sup_annotated_with_adapter_\${_t}.list;
						then
							if samtools faidx ${barcode}_sup_annotated_\${_t}.fastq \
								-r ${barcode}_sup_annotated_with_adapter_\${_t}.list -f \
								> ${barcode}_sup_annotated_with_adapter_\${_t}.fastq;
							then
								echo "${barcode}_sup_annotated_with_adapter_\${_t}.fastq is successfully generated" 1>&2
							fi
						fi
						grep -F -A3 no_adapter ${barcode}_sup_annotated_\${_t}.fastq | grep -v "^--\$" \
							| cutadapt -g file:"\$PRIMERS_PATH" -j ${task.cpus} --action=trim --rc \
							  --discard-untrimmed -e 0.3 -m "\${_ml}" --rename "{id}|\${_t}" \
							  -o ${barcode}_sup_annotated_no_adapter_\${_t}.fastq - \
							  > output_cutadapt_stringent_\${_t}B.out || true
					fi
				done
	
				sup_with=( ${barcode}_sup_annotated_with_adapter_*.fastq )
				if (( \${#sup_with[@]} )); then
					cat "\${sup_with[@]}" > ${barcode}_sup_annotated_with_adapter.fastq
					cat ${barcode}_sup_annotated_with_adapter.fastq > ${barcode}_hac_sup_annotated_clean.fastq
				fi
				sup_noad=( ${barcode}_sup_annotated_no_adapter_*.fastq )
				if (( \${#sup_noad[@]} )); then
					cat "\${sup_noad[@]}" > ${barcode}_sup_annotated_no_adapter.fastq
					cat ${barcode}_sup_annotated_no_adapter.fastq >> ${barcode}_hac_sup_annotated_clean.fastq
				fi
			fi
		
		
			load_demux_target_arrays
			for _i in "\${!_TARGETS[@]}"; do
				_t="\${_TARGETS[\$_i]}"
				_ml="\${_MIN_LENS[\$_i]}"
				if grep -A3 "|\${_t}\$" ${hac_reads_fastq} | grep -v "^--\$" \
					| cutadapt -g file:"\$INDEXES_PATH" -j ${task.cpus} --action=trim --rc -e 0.1 -m "\${_ml}" \
					  --rename '{id}|hac|barcode={cut_prefix}|adapter={adapter_name}' \
					  -o ${barcode}_hac_annotated_\${_t}.fastq - > output_cutadapt_stringent_\${_t}.out;
				then
					if grep -F adapter ${barcode}_hac_annotated_\${_t}.fastq | grep -F -v no_adapter | sed 's/^@//;' \
						> ${barcode}_hac_annotated_with_adapter_\${_t}.list;
				then
						if samtools faidx ${barcode}_hac_annotated_\${_t}.fastq \
							-r ${barcode}_hac_annotated_with_adapter_\${_t}.list -f \
							> ${barcode}_hac_annotated_with_adapter_\${_t}.fastq;
						then
							echo "${barcode}_hac_annotated_with_adapter_\${_t}.fastq is successfully generated" 1>&2
						fi
					fi
					grep -F -A3 no_adapter ${barcode}_hac_annotated_\${_t}.fastq | grep -v "^--\$" \
						| cutadapt -g file:"\$PRIMERS_PATH" -j ${task.cpus} --action=trim --rc \
						  --discard-untrimmed -e 0.3 -m "\${_ml}" --rename "{id}|\${_t}" \
						  -o ${barcode}_hac_annotated_no_adapter_\${_t}.fastq - \
						  > output_cutadapt_stringent_\${_t}B.out || true
				fi
			done

		
		
			hac_with=( ${barcode}_hac_annotated_with_adapter_*.fastq )
			if (( \${#hac_with[@]} )); then
				cat "\${hac_with[@]}" > ${barcode}_hac_annotated_with_adapter.fastq
				if [ -f ${barcode}_hac_sup_annotated_clean.fastq ];
				then
					cat ${barcode}_hac_annotated_with_adapter.fastq >> ${barcode}_hac_sup_annotated_clean.fastq
				else
					cat ${barcode}_hac_annotated_with_adapter.fastq > ${barcode}_hac_sup_annotated_clean.fastq
				fi
			fi
			hac_noad=( ${barcode}_hac_annotated_no_adapter_*.fastq )
			if (( \${#hac_noad[@]} )); then
				cat "\${hac_noad[@]}" > ${barcode}_hac_annotated_no_adapter.fastq
				cat ${barcode}_hac_annotated_no_adapter.fastq >> ${barcode}_hac_sup_annotated_clean.fastq
			fi
		
	# -- §3: Primers-only mode (primer-only cutadapt on rolling SUP + HAC reads) --
elif [ "\$DO_DEMUX" -eq 1 ] && [ "\$DEMUX_MODE" = "primers_only" ] && [ -f "\$PRIMERS_PATH" ];
	then
			restore_rolling_sup_fastq

			if [ -n "\$ROLLING_SUP_FASTQ" ];
			then
				load_demux_target_arrays
				for _i in "\${!_TARGETS[@]}"; do
					_t="\${_TARGETS[\$_i]}"
					_ml="\${_MIN_LENS[\$_i]}"
					if grep -A3 "|\${_t}\$" "\$ROLLING_SUP_FASTQ" | grep -v "^--\$" \\
						| cutadapt -g file:"\$PRIMERS_PATH" -j ${task.cpus} --action=trim --rc \\
						  --discard-untrimmed -e 0.3 -m "\${_ml}" \\
						  --rename '{id}|sup|barcode={adapter_name}|adapter={adapter_name}' \\
						  -o ${barcode}_sup_annotated_with_adapter_\${_t}.fastq - \\
						  > output_cutadapt_stringent_sup_\${_t}.out;
					then
						if [ -s ${barcode}_sup_annotated_with_adapter_\${_t}.fastq ]; then
							awk 'NR % 4 == 1 { sub(/^@/, "", \$0); print }' \\
								${barcode}_sup_annotated_with_adapter_\${_t}.fastq \\
								> ${barcode}_sup_annotated_with_adapter_\${_t}.list
							if [ -s ${barcode}_sup_annotated_with_adapter_\${_t}.list ] \\
								&& samtools faidx ${barcode}_sup_annotated_with_adapter_\${_t}.fastq \\
								   -r ${barcode}_sup_annotated_with_adapter_\${_t}.list -f \\
								   > ${barcode}_sup_annotated_with_adapter_\${_t}.filtered.fastq;
							then
								mv ${barcode}_sup_annotated_with_adapter_\${_t}.filtered.fastq \\
								   ${barcode}_sup_annotated_with_adapter_\${_t}.fastq
							fi
						fi
					fi
				done

				sup_with=( ${barcode}_sup_annotated_with_adapter_*.fastq )
				if (( \${#sup_with[@]} )); then
					cat "\${sup_with[@]}" > ${barcode}_sup_annotated_with_adapter.fastq
					cat ${barcode}_sup_annotated_with_adapter.fastq > ${barcode}_hac_sup_annotated_clean.fastq
				fi
			fi

			load_demux_target_arrays
			for _i in "\${!_TARGETS[@]}"; do
				_t="\${_TARGETS[\$_i]}"
				_ml="\${_MIN_LENS[\$_i]}"
				if grep -A3 "|\${_t}\$" ${hac_reads_fastq} | grep -v "^--\$" \\
					| cutadapt -g file:"\$PRIMERS_PATH" -j ${task.cpus} --action=trim --rc \\
					  --discard-untrimmed -e 0.3 -m "\${_ml}" \\
					  --rename '{id}|hac|barcode={adapter_name}|adapter={adapter_name}' \\
					  -o ${barcode}_hac_annotated_with_adapter_\${_t}.fastq - \\
					  > output_cutadapt_stringent_\${_t}.out;
				then
					if [ -s ${barcode}_hac_annotated_with_adapter_\${_t}.fastq ]; then
						awk 'NR % 4 == 1 { sub(/^@/, "", \$0); print }' \\
							${barcode}_hac_annotated_with_adapter_\${_t}.fastq \\
							> ${barcode}_hac_annotated_with_adapter_\${_t}.list
						if [ -s ${barcode}_hac_annotated_with_adapter_\${_t}.list ] \\
							&& samtools faidx ${barcode}_hac_annotated_with_adapter_\${_t}.fastq \\
							   -r ${barcode}_hac_annotated_with_adapter_\${_t}.list -f \\
							   > ${barcode}_hac_annotated_with_adapter_\${_t}.filtered.fastq;
						then
							mv ${barcode}_hac_annotated_with_adapter_\${_t}.filtered.fastq \\
							   ${barcode}_hac_annotated_with_adapter_\${_t}.fastq
						fi
					fi
				fi
			done

			hac_with=( ${barcode}_hac_annotated_with_adapter_*.fastq )
			if (( \${#hac_with[@]} )); then
				cat "\${hac_with[@]}" > ${barcode}_hac_annotated_with_adapter.fastq
				if [ -f ${barcode}_hac_sup_annotated_clean.fastq ];
				then
					cat ${barcode}_hac_annotated_with_adapter.fastq >> ${barcode}_hac_sup_annotated_clean.fastq
				else
					cat ${barcode}_hac_annotated_with_adapter.fastq > ${barcode}_hac_sup_annotated_clean.fastq
				fi
			fi
	# -- §4: No-demux / demux-off mode (FASTQ tag rewrite + accumulate) --
	else
		: > ${barcode}_hac_sup_annotated_clean.fastq

		PYTHON_BIN=\$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
		if [ -z "\$PYTHON_BIN" ]; then
			echo "Error: python3 or python is required but was not found in PATH" 1>&2
			exit 1
		fi

		rewrite_fastq() {
			local input="\$1"
			local output="\$2"
			local tag="\$3"
		if [ ! -s "\$input" ]; then
			: > "\$output"
			return
		fi
			"\$PYTHON_BIN" - <<'PY' "\$input" "\$output" "\$tag"
import sys, os
src, dst, tag = sys.argv[1:]
if not os.path.exists(src) or os.path.getsize(src) == 0:
    open(dst, 'w').close()
else:
    with open(src, 'r', encoding='utf-8', errors='replace') as fin, open(dst, 'w', encoding='utf-8') as fout:
        while True:
            header = fin.readline()
            if not header:
                break
            seq = fin.readline()
            plus = fin.readline()
            qual = fin.readline()
            if not qual:
                break
            if '|{}|'.format(tag) not in header:
                base = header.rstrip('\\r\\n')
                if '|barcode=' in base:
                    base = f"{base}|{tag}"
                else:
                    base = f"{base}|{tag}|barcode=no_adapter_1|adapter=no_adapter_1"
                header = base + '\\n'
            fout.write(header)
            fout.write(seq)
            fout.write(plus)
            fout.write(qual)
PY
		}

			mkdir -p "\$STATE_DIR"
			restore_rolling_sup_fastq

				if [ -n "\$ROLLING_SUP_FASTQ" ]; then
					rewrite_fastq "\$ROLLING_SUP_FASTQ" "${barcode}_sup_annotated_demuxoff.fastq" "sup"
					if [ -s ${barcode}_sup_annotated_demuxoff.fastq ]; then
						cat ${barcode}_sup_annotated_demuxoff.fastq >> ${barcode}_hac_sup_annotated_clean.fastq
					fi
					rm -f ${barcode}_sup_annotated_demuxoff.fastq
				fi

		rewrite_fastq "${hac_reads_fastq}" "${barcode}_hac_annotated_demuxoff.fastq" "hac"
		if [ -s ${barcode}_hac_annotated_demuxoff.fastq ]; then
			cat ${barcode}_hac_annotated_demuxoff.fastq >> ${barcode}_hac_sup_annotated_clean.fastq
		fi
		rm -f ${barcode}_hac_annotated_demuxoff.fastq
	fi
	
	# -- §5: FASTA conversion and temp-file cleanup (common tail) --
	seqtk seq -a ${barcode}_hac_sup_annotated_clean.fastq > ${barcode}_hac_sup_annotated_clean.fasta
	
	if find ./ -type f -name "output_cutadapt_stringent_*" ! -type l -print -quit | grep -q .;
	then
		rm output_cutadapt_stringent_*
		echo "Removing output_cutadapt_stringent_*" 1>&2
	fi
	if find ./ -type f -name "${barcode}_sup_annotated_with_adapter*fast[aq]" ! -type l -print -quit | grep -q .;
	then
		rm ${barcode}_sup_annotated_with_adapter*fast[aq]
		echo "Removing ${barcode}_sup_annotated_with_adapter*fast[aq]" 1>&2
	fi
	_p_targets="${params.targets}"
	IFS='|' read -ra _TARGETS <<< "\$_p_targets"
	for _t in "\${_TARGETS[@]}"; do
		if [ -f ${barcode}_sup_annotated_\${_t}.fastq ]; then
			rm ${barcode}_sup_annotated_\${_t}.fastq
			echo "Removing ${barcode}_sup_annotated_\${_t}.fastq" 1>&2
		fi
	done
	
	"""
}

// ============================================================
// STAGE E — REPORTING (_reporting_hq_demultiplexing)
// ============================================================
process _reporting_hq_demultiplexing {

	maxForks maxForksReportingVal

    input:
	tuple val(barcode), val(round_barcode), file(hac_sup_annotated_clean_fastq) from annotate_reads_report
	
    output:
	tuple val(barcode), val(round_barcode), file("${barcode}_demult_rpt.txt") into demult_control
    tuple val(barcode), val(round_barcode), file("${barcode}_demult_rpt.txt") into demult_rpt_summary
    
	script:

	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"
		

	if ! perl ${baseDir}/bin/reporting_demultiplexing.pl ${hac_sup_annotated_clean_fastq} ${params.outdir}/ongoing ${round_barcode} ${barcode}; then
		printf 'read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n' > ${barcode}_demult_rpt.txt
	fi
		mkdir -p ${ongoingStateDir}/${round_barcode}/
		cp -f ${barcode}_demult_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_demult_rpt.txt 2>/dev/null || true
	
	
	"""	
}

def cdHitIdentity = formatOtuIdentity(params.otu_id)

// ============================================================
// STAGE F — OTU DEFINITION · core stateful step (OTU_definition)
// ============================================================
process OTU_definition {
	// Must be schedulable even when the next POD5 has already entered `fast_on_target_detection`
	// and is waiting on the round lock.
	cpus { params.cluster_threads }
	    maxForks maxForksCoreCpuVal
	    label 'cluster'
    input:
	tuple val(barcode), val(round_barcode), file(fasta_hq_qced) from otu_analysis
	
	output:
	tuple val(barcode), val(round_barcode), file(fasta_hq_qced), file("${barcode}_qced_reads_nr.fasta.clstr") into fastq_qced_blast
	tuple val(barcode), val(round_barcode), file(fasta_hq_qced) into fastq_qced_consensus
	tuple val(barcode), val(round_barcode), file("${barcode}_qced_reads_nr.fasta.clstr") into report_otu
	
    script:
	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	trap 'echo "ERROR: OTU_definition failed at line \$LINENO: \$BASH_COMMAND" >&2' ERR
	RESTART_TOKEN="${restartTokenForCache}"
	# Align cd-hit threads with CPUs reserved for this task (see `cpus { params.cluster_threads }`).
	THREADS=${task.cpus}
	if [ -z "\$THREADS" ]; then THREADS=1; fi
	OTU_PRUNED_RECOVERY_ENABLED="${otuPrunedRecoveryEnabled ? 1 : 0}"
	OTU_PRUNED_RECOVERY_ID="${otuPrunedRecoveryIdentity}"
	OTU_PRUNED_RECOVERY_TARGET_POLICY="${otuPrunedRecoveryTargetPolicyCanonical}"
	OTU_PRUNED_RECOVERY_FAILURE_POLICY="${otuPrunedRecoveryFailurePolicyCanonical}"

	# Read rolling state under the same lock used by `blast_OTU_pretax` when updating it.
	STATE_DIR="${ongoingStateDir}/_state"
		QCED_LOCK="\${STATE_DIR}/.qced_reads.lock"
		LOCK_WAIT=${params.lock_wait_seconds}
		PRUNED_BARRIER="\${STATE_DIR}/${barcode}_pruned_barrier.list"
		PROTECTED_READ_IDS_EVER_STATE="\${STATE_DIR}/${barcode}_protected_read_ids_ever.list"
	FASTA_HQ_QCED="${fasta_hq_qced}"
	if [ -s "\$PRUNED_BARRIER" ]; then
		perl ${baseDir}/bin/reads_apply_prune_ids.pl \
			"\$FASTA_HQ_QCED" "\$PRUNED_BARRIER" \
			"${fasta_hq_qced}.barrier_filtered" \
			"${fasta_hq_qced}.barrier_stats"
		FASTA_HQ_QCED="${fasta_hq_qced}.barrier_filtered"
	fi
		source "${baseDir}/bin/lib/lock_utils.sh"
		init_lock_helpers
		mkdir -p "\$STATE_DIR"
	# Frozen-rep incremental clustering state files
	FROZEN_ENABLED=${params.otu_frozen_enabled ? 1 : 0}
	FROZEN_META="\${STATE_DIR}/otu_frozen_meta.tsv"
	FROZEN_REPS="\${STATE_DIR}/otu_frozen_reps.fasta"
	FROZEN_HIST="\${STATE_DIR}/otu_frozen_history.tsv"
	FROZEN_MEMBERS="\${STATE_DIR}/otu_frozen_members.tsv"
	FROZEN_MEMBERS_SEEN="\${STATE_DIR}/otu_frozen_members_seen.tsv"
	ACTIVE_POOL="\${STATE_DIR}/otu_active_pool.fasta"
	if [ -s "\$ACTIVE_POOL" ] && [ -s "\$PRUNED_BARRIER" ]; then
		perl ${baseDir}/bin/reads_apply_prune_ids.pl \
			"\$ACTIVE_POOL" "\$PRUNED_BARRIER" \
			"\${ACTIVE_POOL}.barrier_filtered" \
			"\${STATE_DIR}/${barcode}_active_pool_barrier_filter_stats.tsv"
		mv "\${ACTIVE_POOL}.barrier_filtered" "\$ACTIVE_POOL"
	fi
		SEEN_HASHES="\${STATE_DIR}/otu_seen_hashes.tsv"
		ROUND_ID="${round_barcode}"
		OTU_ID_MODE="${params.otu_strict_ids ? 'strict' : 'legacy'}"
		FROZEN_DB_ONLY_POLICY="${otuDbOnlyPolicyCanonical}"
		[ -f "\$FROZEN_META" ] || : > "\$FROZEN_META"
	[ -f "\$FROZEN_REPS" ] || : > "\$FROZEN_REPS"
		[ -f "\$FROZEN_HIST" ] || : > "\$FROZEN_HIST"
		[ -f "\$FROZEN_MEMBERS" ] || : > "\$FROZEN_MEMBERS"
		[ -f "\$FROZEN_MEMBERS_SEEN" ] || : > "\$FROZEN_MEMBERS_SEEN"
		[ -f "\$SEEN_HASHES" ] || : > "\$SEEN_HASHES"
		PREFLIGHT_REPORT="${barcode}_otu_state_preflight.tsv"
		if ! ${baseDir}/bin/otu_state_preflight.pl "\$STATE_DIR" "\$OTU_ID_MODE" "\$PREFLIGHT_REPORT"; then
			echo "ERROR: OTU state preflight failed in mode=\$OTU_ID_MODE (see \$PREFLIGHT_REPORT)" 1>&2
			exit 1
		fi
		if [ -s "\$PREFLIGHT_REPORT" ]; then
			echo "INFO: OTU state preflight summary:" 1>&2
			sed 's/^/INFO: otu_preflight\t/' "\$PREFLIGHT_REPORT" 1>&2 || true
			cp -f "\$PREFLIGHT_REPORT" "\${STATE_DIR}/otu_state_preflight_last.tsv" 2>/dev/null || true
		fi
		# -- §2: Pruned-read recovery from archive --
	PRUNED_ARCHIVE="\${STATE_DIR}/${barcode}_pruned_archive.fasta"
		ARCHIVE_RECOVERY_MEMBERS_RAW="${barcode}_archive_recovered_frozen_members_raw.tsv"
		ARCHIVE_RECOVERY_MEMBERS="${barcode}_archive_recovered_frozen_members.tsv"
		ARCHIVE_RECOVERY_UNASSIGNED="${barcode}_archive_recovery_unassigned.list"
		ARCHIVE_RECOVERY_IDS="${barcode}_archive_recovered_ids.list"
		ARCHIVE_RECOVERY_FASTA="${barcode}_archive_recovered.fasta"
		ARCHIVE_RECOVERY_STATS="${barcode}_pruned_archive_recovery_stats.tsv"
		ARCHIVE_RECOVERY_STATE_STATS="\${STATE_DIR}/${barcode}_pruned_archive_recovery_last.tsv"
		ARCHIVE_RECOVERY_STATE_IDS="\${STATE_DIR}/${barcode}_pruned_archive_recovered_last.list"
		ARCHIVE_RECOVERY_ROUND_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_pruned_archive_recovery_stats.tsv"
		ARCHIVE_RECOVERY_ROUND_IDS="${ongoingStateDir}/${round_barcode}/${barcode}_pruned_archive_recovered_ids.list"
		: > "\$ARCHIVE_RECOVERY_MEMBERS_RAW"
		: > "\$ARCHIVE_RECOVERY_MEMBERS"
		: > "\$ARCHIVE_RECOVERY_UNASSIGNED"
		: > "\$ARCHIVE_RECOVERY_IDS"
		: > "\$ARCHIVE_RECOVERY_FASTA"
		{
			printf 'enabled\t%s\n' "\$OTU_PRUNED_RECOVERY_ENABLED"
			printf 'matched_frozen_before_target_filter\t0\n'
			printf 'target_filtered_out\t0\n'
			printf 'recovered_ids\t0\n'
			printf 'status\tnot_run\n'
		} > "\$ARCHIVE_RECOVERY_STATS"
		mkdir -p "${ongoingStateDir}/${round_barcode}"
		if [ "\$OTU_PRUNED_RECOVERY_ENABLED" = "1" ] && [ "\$FROZEN_ENABLED" -eq 1 ] && [ -s "\$PRUNED_ARCHIVE" ]; then
			if [ -f "\${FROZEN_REPS}.gz" ] && [ ! -s "\$FROZEN_REPS" ]; then
				gzip -dc "\${FROZEN_REPS}.gz" > "\$FROZEN_REPS" && rm -f "\${FROZEN_REPS}.gz"
			fi
			if [ ! -s "\$FROZEN_REPS" ]; then
				if [ "\$OTU_PRUNED_RECOVERY_FAILURE_POLICY" = "fail" ]; then
					echo "ERROR: pruned-read recovery enabled but frozen reps are unavailable" 1>&2
					exit 1
				fi
				echo "WARN: pruned-read recovery skipped; frozen reps unavailable" 1>&2
				awk -F'\t' 'BEGIN{OFS=FS} \$1=="status"{\$2="skipped_missing_frozen_reps"} {print}' "\$ARCHIVE_RECOVERY_STATS" > "\${ARCHIVE_RECOVERY_STATS}.tmp" && mv "\${ARCHIVE_RECOVERY_STATS}.tmp" "\$ARCHIVE_RECOVERY_STATS"
			elif ! command -v cd-hit-est-2d >/dev/null 2>&1; then
				if [ "\$OTU_PRUNED_RECOVERY_FAILURE_POLICY" = "fail" ]; then
					echo "ERROR: pruned-read recovery enabled but cd-hit-est-2d is unavailable" 1>&2
					exit 1
				fi
				echo "WARN: pruned-read recovery skipped; cd-hit-est-2d not found" 1>&2
				awk -F'\t' 'BEGIN{OFS=FS} \$1=="status"{\$2="skipped_missing_cdhit"} {print}' "\$ARCHIVE_RECOVERY_STATS" > "\${ARCHIVE_RECOVERY_STATS}.tmp" && mv "\${ARCHIVE_RECOVERY_STATS}.tmp" "\$ARCHIVE_RECOVERY_STATS"
			else
				ARCHIVE_CLSTR=""
				if cd-hit-est-2d -i "\$PRUNED_ARCHIVE" -i2 "\$FROZEN_REPS" -c "\$OTU_PRUNED_RECOVERY_ID" -d 0 -T "\$THREADS" -o ${barcode}_archive_vs_frozen; then
					if [ -s ${barcode}_archive_vs_frozen.clstr ]; then
						ARCHIVE_CLSTR="${barcode}_archive_vs_frozen.clstr"
					elif [ -s ${barcode}_archive_vs_frozen.fasta.clstr ]; then
						ARCHIVE_CLSTR="${barcode}_archive_vs_frozen.fasta.clstr"
					fi
				else
					if [ "\$OTU_PRUNED_RECOVERY_FAILURE_POLICY" = "fail" ]; then
						echo "ERROR: pruned-read recovery cd-hit-est-2d failed" 1>&2
						exit 1
					fi
					echo "WARN: pruned-read recovery cd-hit-est-2d failed; skipping recovery" 1>&2
					awk -F'\t' 'BEGIN{OFS=FS} \$1=="status"{\$2="skipped_cdhit_failed"} {print}' "\$ARCHIVE_RECOVERY_STATS" > "\${ARCHIVE_RECOVERY_STATS}.tmp" && mv "\${ARCHIVE_RECOVERY_STATS}.tmp" "\$ARCHIVE_RECOVERY_STATS"
				fi
				if [ -n "\$ARCHIVE_CLSTR" ]; then
					if ! ${baseDir}/bin/otu_frozen_members_from_clstr.pl "\$ARCHIVE_CLSTR" "\$ARCHIVE_RECOVERY_MEMBERS_RAW" "\$ARCHIVE_RECOVERY_UNASSIGNED" "\$OTU_ID_MODE" "\$FROZEN_DB_ONLY_POLICY"; then
						if [ "\$OTU_PRUNED_RECOVERY_FAILURE_POLICY" = "fail" ]; then
							echo "ERROR: failed to parse pruned-read recovery assignments" 1>&2
							exit 1
						fi
						echo "WARN: failed to parse pruned-read recovery assignments; skipping recovery" 1>&2
						: > "\$ARCHIVE_RECOVERY_MEMBERS_RAW"
						: > "\$ARCHIVE_RECOVERY_UNASSIGNED"
						awk -F'\t' 'BEGIN{OFS=FS} \$1=="status"{\$2="skipped_parse_failed"} {print}' "\$ARCHIVE_RECOVERY_STATS" > "\${ARCHIVE_RECOVERY_STATS}.tmp" && mv "\${ARCHIVE_RECOVERY_STATS}.tmp" "\$ARCHIVE_RECOVERY_STATS"
					fi
					_raw_recovered=0
					_filtered_recovered=0
					if [ -s "\$ARCHIVE_RECOVERY_MEMBERS_RAW" ]; then
						_raw_recovered=\$(wc -l < "\$ARCHIVE_RECOVERY_MEMBERS_RAW" | tr -d ' ')
						if [ "\$OTU_PRUNED_RECOVERY_TARGET_POLICY" = "same_target_only" ]; then
							if [ ! -s "\$FROZEN_META" ]; then
								echo "WARN: pruned-read recovery same_target_only filter: FROZEN_META is empty; all recovered reads will be discarded" 1>&2
							fi
							awk -F'\t' 'NR==FNR {
								split(\$2, a, "|");
								t=length(a[3]) ? a[3] : a[2];
								if (\$1 != "" && t != "") target[\$1]=t;
								next
							}
							{
								split(\$2, b, "|");
								qt=length(b[2]) ? b[2] : "";
								if ((\$1 in target) && qt != "" && qt == target[\$1]) print \$0
							}' "\$FROZEN_META" "\$ARCHIVE_RECOVERY_MEMBERS_RAW" > "\$ARCHIVE_RECOVERY_MEMBERS"
						else
							cp "\$ARCHIVE_RECOVERY_MEMBERS_RAW" "\$ARCHIVE_RECOVERY_MEMBERS"
						fi
						_filtered_recovered=\$(wc -l < "\$ARCHIVE_RECOVERY_MEMBERS" | tr -d ' ')
					fi
					_target_filtered_out=\$(( _raw_recovered - _filtered_recovered ))
					_recovered_ids=0
					if [ -s "\$ARCHIVE_RECOVERY_MEMBERS" ]; then
						awk -F'\t' '{id=\$2; sub(/[|].*/,"",id); if(id!="") print id}' "\$ARCHIVE_RECOVERY_MEMBERS" | LC_ALL=C sort -u > "\$ARCHIVE_RECOVERY_IDS"
						awk 'NR==FNR { ids[\$1]=1; next }
						     /^>/ { uuid=substr(\$0,2); sub(/[|].*/,"",uuid); keep=(uuid in ids); if(keep)print; next }
						     keep { print }' \
							"\$ARCHIVE_RECOVERY_IDS" "\$PRUNED_ARCHIVE" > "\$ARCHIVE_RECOVERY_FASTA"
						[ -s "\$ARCHIVE_RECOVERY_IDS" ] && _recovered_ids=\$(wc -l < "\$ARCHIVE_RECOVERY_IDS" | tr -d ' ')
						if [ -s "\$ARCHIVE_RECOVERY_FASTA" ]; then
							if acquire_lock "\$QCED_LOCK"; then
								_recovery_rc=0
								if ! perl ${baseDir}/bin/otu_members_append_unique.pl "\$FROZEN_MEMBERS" "\$ARCHIVE_RECOVERY_MEMBERS" "\$FROZEN_MEMBERS_SEEN"; then
									echo "ERROR: failed to append recovered pruned reads to frozen members" 1>&2
									_recovery_rc=1
								fi
								if [ "\$_recovery_rc" -eq 0 ] && [ -s "\$ARCHIVE_RECOVERY_FASTA" ]; then
									cat "\$ARCHIVE_RECOVERY_FASTA" >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" || _recovery_rc=1
									if [ "\$_recovery_rc" -eq 0 ]; then
										rm -f "\${STATE_DIR}/qced_reads_hq_accumulated.fasta.fai"
										command -v samtools >/dev/null 2>&1 && samtools faidx "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" 2>/dev/null || true
										seqkit faidx "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" 2>/dev/null || true
									fi
								fi
								if [ "\$_recovery_rc" -eq 0 ] && [ -s "\$PRUNED_BARRIER" ]; then
									LC_ALL=C comm -23 \
										<(LC_ALL=C sort -u "\$PRUNED_BARRIER") \
										<(LC_ALL=C sort -u "\$ARCHIVE_RECOVERY_IDS") > "\${PRUNED_BARRIER}.new" || _recovery_rc=1
									if [ "\$_recovery_rc" -eq 0 ]; then
										mv "\${PRUNED_BARRIER}.new" "\$PRUNED_BARRIER" || _recovery_rc=1
									fi
								fi
								if [ "\$_recovery_rc" -eq 0 ]; then
									awk 'NR==FNR { drop[\$1]=1; next }
									     /^>/ { uuid=substr(\$0,2); sub(/[|].*/,"",uuid); keep=!(uuid in drop); if(keep)print; next }
									     keep { print }' \
										"\$ARCHIVE_RECOVERY_IDS" "\$PRUNED_ARCHIVE" > "\${PRUNED_ARCHIVE}.trimmed" || _recovery_rc=1
									if [ "\$_recovery_rc" -eq 0 ]; then
										mv "\${PRUNED_ARCHIVE}.trimmed" "\$PRUNED_ARCHIVE" || _recovery_rc=1
									fi
								fi
								release_lock "\$QCED_LOCK"
								if [ "\$_recovery_rc" -ne 0 ]; then
									echo "ERROR: failed to commit pruned-read recovery state updates" 1>&2
									exit 1
								fi
							else
								exit 1
							fi
							echo "INFO: archive_recovery recovered=\$_recovered_ids target_filtered_out=\$_target_filtered_out" 1>&2
						fi
					fi
					_recovery_status="no_matches"
					[ "\$_recovered_ids" -gt 0 ] && _recovery_status="recovered"
					{
						printf 'enabled\t%s\n' "\$OTU_PRUNED_RECOVERY_ENABLED"
						printf 'matched_frozen_before_target_filter\t%s\n' "\$_raw_recovered"
						printf 'target_filtered_out\t%s\n' "\$_target_filtered_out"
						printf 'recovered_ids\t%s\n' "\$_recovered_ids"
						printf 'status\t%s\n' "\$_recovery_status"
					} > "\$ARCHIVE_RECOVERY_STATS"
				fi
			fi
		fi
		cp "\$ARCHIVE_RECOVERY_STATS" "\$ARCHIVE_RECOVERY_STATE_STATS" 2>/dev/null || true
		cp "\$ARCHIVE_RECOVERY_STATS" "\$ARCHIVE_RECOVERY_ROUND_STATS" 2>/dev/null || true
		cp "\$ARCHIVE_RECOVERY_IDS" "\$ARCHIVE_RECOVERY_STATE_IDS" 2>/dev/null || true
		cp "\$ARCHIVE_RECOVERY_IDS" "\$ARCHIVE_RECOVERY_ROUND_IDS" 2>/dev/null || true

	# -- §3: Rolling pool snapshot (model-priority merge: SUP > HAC) --
	ROLLING_QCED=""
	if acquire_lock "\$QCED_LOCK"; then
		if [ -f "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" ]; then
			cp "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" ${barcode}_rolling_qced_reads_hq_accumulated.fasta || true
			ROLLING_QCED="${barcode}_rolling_qced_reads_hq_accumulated.fasta"
		fi
		release_lock "\$QCED_LOCK"
	else
		exit 1
	fi

	if [ -n "\$ROLLING_QCED" ];
	then
		# Build rolling HQ FASTA with SUP prioritized over HAC_FIXED for duplicate read IDs.
		: > ${barcode}_qced_reads_hq_accumulated.fasta
		# Sticky protected reads must stay eligible for OTU clustering regardless of model.
		if [ -s "\$PROTECTED_READ_IDS_EVER_STATE" ]; then
			awk 'NR==FNR{ids[\$1]=1; next} /^>/{uuid=substr(\$0,2); sub(/[|].*/,"",uuid); keep=(uuid in ids); if(keep)print; next} keep{print}' "\$PROTECTED_READ_IDS_EVER_STATE" "\$ROLLING_QCED" > ${barcode}_rolling_protected.fasta || : > ${barcode}_rolling_protected.fasta
			cat ${barcode}_rolling_protected.fasta >> ${barcode}_qced_reads_hq_accumulated.fasta
		else
			: > ${barcode}_rolling_protected.fasta
		fi
		# Extract SUP reads
		seqkit grep -r -p "\\|sup\\|" "\$ROLLING_QCED" > ${barcode}_rolling_sup.fasta || : > ${barcode}_rolling_sup.fasta
		# Extract HAC_FIXED reads
		seqkit grep -r -p "\\|hac_fixed\\|" "\$ROLLING_QCED" > ${barcode}_rolling_hac_fixed.fasta || : > ${barcode}_rolling_hac_fixed.fasta
		# Write SUP first and track their IDs
		awk '/^>/{id=substr(\$0,2); sub(/ .*/,\"\",id); print id > \"sup_ids.list\"} {print}' ${barcode}_rolling_sup.fasta > ${barcode}_qced_reads_hq_accumulated.fasta
		# Append HAC_FIXED only if ID not present in SUP
		if [ -s sup_ids.list ]; then
			awk 'BEGIN{FS=\"\\n\"} NR==FNR {sup[\$0]=1; next}
				/^>/{id=substr(\$0,2); sub(/ .*/,\"\",id); keep=!(id in sup)}
				keep {print}' sup_ids.list ${barcode}_rolling_hac_fixed.fasta >> ${barcode}_qced_reads_hq_accumulated.fasta
		else
			cat ${barcode}_rolling_hac_fixed.fasta >> ${barcode}_qced_reads_hq_accumulated.fasta
		fi
		rm -f ${barcode}_rolling_protected.fasta ${barcode}_rolling_sup.fasta ${barcode}_rolling_hac_fixed.fasta sup_ids.list
		cat "\$FASTA_HQ_QCED" >> ${barcode}_qced_reads_hq_accumulated.fasta
	else
		cp "\$FASTA_HQ_QCED" ${barcode}_qced_reads_hq_accumulated.fasta
	fi
	# Deduplicate by read_id, keeping best model (sup > hac > fast).
	# -- §4: Hash dedup, tracking, and frozen member update --
	DEDUP_TMP="${barcode}_qced_reads_hq_accumulated.dedup.tmp"
	awk 'BEGIN{FS="|"}
		/^>/{
			header=\$0;
			id=substr(header,2);
			model="";
			if (index(id,"|")>0) { split(id,a,"|"); id=a[1]; model=a[3]; } else { model=""; }
			if (model=="hac2sup" || model=="hac_fixed") model="hac";
			rank=(model=="sup"?3:(model=="hac"?2:1));
			if (!(id in best) || rank>best[id]) {
				best[id]=rank; hdr[id]=header; seq[id]=""; keep=1;
			} else {
				keep=0;
			}
			cur=id;
			next
		}
			{
				if (keep) { seq[cur]=seq[cur] \$0 ORS; }
			}
				END{
					for (id in hdr) {
						print hdr[id];
						printf "%s", seq[id];
					}
				}' ${barcode}_qced_reads_hq_accumulated.fasta \
				| seqkit sort -n > "\$DEDUP_TMP" \
				&& mv "\$DEDUP_TMP" ${barcode}_qced_reads_hq_accumulated.fasta
		HASH_MAP="${barcode}_otu_hash_map.tsv"
		HASH_COUNTS="${barcode}_otu_hash_counts.tsv"
		${baseDir}/bin/otu_hash_map_from_fasta.pl ${barcode}_qced_reads_hq_accumulated.fasta "\$HASH_MAP" "\$HASH_COUNTS"
		mkdir -p "\$STATE_DIR" ${ongoingStateDir}/${round_barcode}
		cp "\$HASH_MAP" "\${STATE_DIR}/${barcode}_otu_hash_map.tsv.tmp" 2>/dev/null && mv "\${STATE_DIR}/${barcode}_otu_hash_map.tsv.tmp" "\${STATE_DIR}/${barcode}_otu_hash_map.tsv" || true
		cp "\$HASH_MAP" "${ongoingStateDir}/${round_barcode}/${barcode}_otu_hash_map.tsv" 2>/dev/null || true
		FROZEN_BY_HASH="${barcode}_frozen_members_by_hash.tsv"
		${baseDir}/bin/otu_add_frozen_members_by_hash.pl "\$FROZEN_META" "\$HASH_MAP" "\$FROZEN_BY_HASH" || : > "\$FROZEN_BY_HASH"
		if [ -s "\$FROZEN_BY_HASH" ]; then
			perl ${baseDir}/bin/otu_members_append_unique.pl "\$FROZEN_MEMBERS" "\$FROZEN_BY_HASH" "\$FROZEN_MEMBERS_SEEN"
		fi
			NEW_FASTA="${barcode}_otu_new_unique.fasta"
			NEW_HASHES="${barcode}_otu_new_hashes.tsv"
			NEW_HASHES_CAND="${barcode}_otu_new_hashes_candidate.tsv"
			NEW_HASHES_TO_COMMIT="${barcode}_otu_new_hashes_to_commit.tsv"
			NEW_HASHES_DROPPED="${barcode}_otu_new_hashes_dropped.tsv"
			NEW_BASE_HASH="${barcode}_otu_new_base_hash.tsv"
			POOL_DECISIONS="${barcode}_active_pool_merge_decisions.tsv"
			${baseDir}/bin/otu_split_new_by_hash.pl ${barcode}_qced_reads_hq_accumulated.fasta "\$SEEN_HASHES" "\$NEW_FASTA" "\$NEW_HASHES"
			: > "\$NEW_HASHES_CAND"
			: > "\$NEW_HASHES_TO_COMMIT"
			: > "\$NEW_HASHES_DROPPED"
			if [ -s "\$NEW_HASHES" ]; then
				if [ -s "\$SEEN_HASHES" ]; then
					awk 'NR==FNR{seen[\$1]=1; next} NF && !(\$1 in seen){print \$1}' "\$SEEN_HASHES" "\$NEW_HASHES" > "\$NEW_HASHES_CAND"
				else
					cp "\$NEW_HASHES" "\$NEW_HASHES_CAND"
				fi
			fi
			# Do not commit NEW_HASHES to SEEN_HASHES yet.
			# Commit only after successful clustering/state update for this round.


		# -- §5: Active pool merge decisions --
		# Always create the expected cluster output so downstream processes don't fail.
		: > ${barcode}_qced_reads_nr.fasta.clstr
		if [ ! -s ${barcode}_qced_reads_hq_accumulated.fasta ]; then
			echo "WARN: No HQ reads available for OTU definition; creating empty OTU clusters and continuing" 1>&2
			mkdir -p ${ongoingStateDir}/${round_barcode}/
			cp -f ${barcode}_qced_reads_nr.fasta.clstr ${ongoingStateDir}/${round_barcode}/qced_reads_nr.fasta.clstr 2>/dev/null || true
			cp -f ${barcode}_qced_reads_nr.fasta.clstr "\${STATE_DIR}/qced_reads_nr.fasta.clstr" 2>/dev/null || true
		else
		
		SKIP_CLUSTER=0
		SKIP_CLUSTER_REASON=""
		POOL_CHANGED=0
		if [ ! -s "\$NEW_FASTA" ]; then
			echo "WARN: No new unique reads for OTU clustering; reusing previous clusters" 1>&2
			if [ -f "\${STATE_DIR}/qced_reads_nr.fasta.clstr" ]; then
				cp "\${STATE_DIR}/qced_reads_nr.fasta.clstr" ${barcode}_qced_reads_nr.fasta.clstr
			fi
			SKIP_CLUSTER=1
			SKIP_CLUSTER_REASON="no_new_unique_reads"
		fi

		NEW_COUNT=0
		if [ -s "\$NEW_FASTA" ]; then
			NEW_COUNT=\$(grep -c '^>' "\$NEW_FASTA" 2>/dev/null || echo 0)
		fi
		if [ "\$NEW_COUNT" -lt ${params.otu_incremental_min_new} ]; then
			SKIP_CLUSTER=1
			if [ -z "\$SKIP_CLUSTER_REASON" ]; then
				SKIP_CLUSTER_REASON="below_min_new_reads"
			fi
		fi

		# Assign new reads to frozen (if applicable) and stage unassigned in active pool,
		# even when clustering is skipped. This avoids reprocessing small batches
		# without counting "no-new" rounds toward freeze history.
		NEW_HASHES_COMMIT_OK=0
		if [ -s "\$NEW_FASTA" ]; then
			: > ${barcode}_frozen_members_new.tsv
			: > ${barcode}_new_unassigned.list
			: > ${barcode}_new_unassigned.fasta

			FROZEN_2D_OK=0
			FROZEN_CLSTR=""
			# Decompress frozen reps if stored compressed.
			if [ -f "\${FROZEN_REPS}.gz" ] && [ ! -s "\$FROZEN_REPS" ]; then
				gzip -dc "\${FROZEN_REPS}.gz" > "\$FROZEN_REPS" && rm -f "\${FROZEN_REPS}.gz"
			fi
			if [ "\$FROZEN_ENABLED" -eq 1 ] && [ -s "\$FROZEN_REPS" ] && [ -s "\$NEW_FASTA" ]; then
				if command -v cd-hit-est-2d >/dev/null 2>&1; then
					if cd-hit-est-2d -i "\$NEW_FASTA" -i2 "\$FROZEN_REPS" -c ${cdHitIdentity} -d 0 -T "\$THREADS" -o ${barcode}_new_vs_frozen; then
						FROZEN_2D_OK=1
					else
						echo "WARN: cd-hit-est-2d failed; treating all new reads as unassigned" 1>&2
					fi
				else
					echo "WARN: cd-hit-est-2d not found; treating all new reads as unassigned" 1>&2
				fi

				if [ -s ${barcode}_new_vs_frozen.clstr ]; then
					FROZEN_CLSTR="${barcode}_new_vs_frozen.clstr"
				elif [ -s ${barcode}_new_vs_frozen.fasta.clstr ]; then
					FROZEN_CLSTR="${barcode}_new_vs_frozen.fasta.clstr"
				fi
			fi

			NEW_UNASSIGNED_READY=0
			if [ "\$FROZEN_2D_OK" -eq 1 ] && [ -n "\$FROZEN_CLSTR" ]; then
				${baseDir}/bin/otu_frozen_members_from_clstr.pl "\$FROZEN_CLSTR" ${barcode}_frozen_members_new.tsv ${barcode}_new_unassigned.list "\$OTU_ID_MODE" "\$FROZEN_DB_ONLY_POLICY"
			else
				: > ${barcode}_new_unassigned.list
				cp "\$NEW_FASTA" ${barcode}_new_unassigned.fasta
				NEW_UNASSIGNED_READY=1
			fi

			if [ -s ${barcode}_frozen_members_new.tsv ]; then
				perl ${baseDir}/bin/otu_members_append_unique.pl "\$FROZEN_MEMBERS" ${barcode}_frozen_members_new.tsv "\$FROZEN_MEMBERS_SEEN"
			fi
			if [ "\$NEW_UNASSIGNED_READY" -ne 1 ]; then
				if [ -s ${barcode}_new_unassigned.list ]; then
					seqkit faidx -j "\$THREADS" -l ${barcode}_new_unassigned.list -r "\$NEW_FASTA" > ${barcode}_new_unassigned.fasta || : > ${barcode}_new_unassigned.fasta
				else
					: > ${barcode}_new_unassigned.fasta
				fi
			fi

				if [ -s ${barcode}_new_unassigned.fasta ] && [ -s "\$NEW_HASHES_CAND" ]; then
					POOL_STATS="${barcode}_active_pool_merge_stats.tsv"
					if ! perl ${baseDir}/bin/otu_pool_merge_prefer_new.pl "\$ACTIVE_POOL" ${barcode}_new_unassigned.fasta "\$ACTIVE_POOL" "\$POOL_STATS" "\$POOL_DECISIONS" "${params.otu_pool_decision_include_hash}"; then
						echo "ERROR: otu_pool_merge_prefer_new.pl failed" 1>&2
						exit 1
					fi
					if [ -s "\$POOL_STATS" ]; then
						echo "INFO: active pool merge stats:" 1>&2
						sed 's/^/INFO: otu_pool_merge\t/' "\$POOL_STATS" 1>&2 || true
						cp -f "\$POOL_STATS" "\${STATE_DIR}/otu_pool_merge_stats_last.tsv" 2>/dev/null || true
					fi
					if [ ! -s "\$POOL_DECISIONS" ]; then
						echo "ERROR: active pool merge decisions file is missing or empty (\$POOL_DECISIONS)" 1>&2
						exit 1
					fi
					cp -f "\$POOL_DECISIONS" "\${STATE_DIR}/otu_pool_merge_decisions_last.tsv" 2>/dev/null || true
					kept_new_rows=\$(awk -F '\t' 'NF>=9 && \$9=="kept_new"{c++} END{print c+0}' "\$POOL_DECISIONS")
					if [ "\$kept_new_rows" -gt 0 ]; then
						POOL_CHANGED=1
					else
						POOL_CHANGED=0
					fi
					# Map new FASTA base IDs to sequence hashes for commit/drop auditing.
					: > "\$NEW_BASE_HASH"
					if [ -s "\$NEW_FASTA" ]; then
						perl ${baseDir}/bin/otu_base_hash_from_fasta.pl "\$NEW_FASTA" "\$NEW_BASE_HASH"
					fi
					: > "\$NEW_HASHES_TO_COMMIT"
					: > "\$NEW_HASHES_DROPPED"
					if [ -s "\$NEW_BASE_HASH" ] && [ -s "\$POOL_DECISIONS" ]; then
						# decisions.tsv has empty columns; force tab field splitting so decision stays in column 9.
						awk -F '\t' 'NR==FNR{h[\$1]=\$2; next} \$9=="kept_new"{ if(\$1 in h) print h[\$1]; }' "\$NEW_BASE_HASH" "\$POOL_DECISIONS" > "\${NEW_HASHES_TO_COMMIT}.raw"
						awk -F '\t' 'NR==FNR{h[\$1]=\$2; next} \$9=="dropped_new"{ if(\$1 in h) print h[\$1]; }' "\$NEW_BASE_HASH" "\$POOL_DECISIONS" > "\${NEW_HASHES_DROPPED}.raw"
						awk 'NR==FNR{cand[\$1]=1; next} (\$1 in cand){print \$1}' "\$NEW_HASHES_CAND" "\${NEW_HASHES_TO_COMMIT}.raw" | LC_ALL=C sort -u > "\$NEW_HASHES_TO_COMMIT"
						awk 'NR==FNR{cand[\$1]=1; next} (\$1 in cand){print \$1}' "\$NEW_HASHES_CAND" "\${NEW_HASHES_DROPPED}.raw" | LC_ALL=C sort -u > "\$NEW_HASHES_DROPPED"
						rm -f "\${NEW_HASHES_TO_COMMIT}.raw" "\${NEW_HASHES_DROPPED}.raw"
					fi
					if [ "${params.otu_commit_dropped_hashes}" = "true" ] && [ -s "\$NEW_HASHES_DROPPED" ]; then
						cat "\$NEW_HASHES_DROPPED" >> "\$NEW_HASHES_TO_COMMIT"
						LC_ALL=C sort -u -o "\$NEW_HASHES_TO_COMMIT" "\$NEW_HASHES_TO_COMMIT"
					fi
					cand_rows=\$(wc -l < "\$NEW_HASHES_CAND" | tr -d ' ')
					commit_rows=\$(wc -l < "\$NEW_HASHES_TO_COMMIT" | tr -d ' ')
					dropped_rows=\$(wc -l < "\$NEW_HASHES_DROPPED" | tr -d ' ')
					echo "INFO: new hash decisions candidate=\$cand_rows commit=\$commit_rows dropped=\$dropped_rows commit_dropped=${params.otu_commit_dropped_hashes}" 1>&2
					if [ "\$cand_rows" -gt 0 ] && [ \$((commit_rows + dropped_rows)) -eq 0 ]; then
						echo "ERROR: No commit/drop decisions could be resolved for candidate new hashes" 1>&2
						exit 1
					fi
					cp -f "\$NEW_HASHES_DROPPED" "\${STATE_DIR}/otu_new_hashes_dropped_last.tsv" 2>/dev/null || true
				elif [ -s ${barcode}_new_unassigned.fasta ] && [ ! -s "\$NEW_HASHES_CAND" ]; then
					echo "INFO: All candidate new hashes are already marked seen; skipping active pool append" 1>&2
					[ -f "\$ACTIVE_POOL" ] || : > "\$ACTIVE_POOL"
					POOL_CHANGED=0
				elif [ ! -s ${barcode}_new_unassigned.fasta ] && [ -s "\$NEW_HASHES_CAND" ]; then
					# New hashes were fully assigned to frozen clusters; mark them seen.
					cp "\$NEW_HASHES_CAND" "\$NEW_HASHES_TO_COMMIT"
					: > "\$NEW_HASHES_DROPPED"
					cp -f "\$NEW_HASHES_DROPPED" "\${STATE_DIR}/otu_new_hashes_dropped_last.tsv" 2>/dev/null || true
					POOL_CHANGED=0
				else
					[ -f "\$ACTIVE_POOL" ] || : > "\$ACTIVE_POOL"
					POOL_CHANGED=0
				fi

			NEW_HASHES_COMMIT_OK=1
		fi
		echo "INFO: active pool changed=\$POOL_CHANGED" 1>&2
		if [ "\$SKIP_CLUSTER" -eq 0 ] && [ "\$POOL_CHANGED" -eq 0 ]; then
			SKIP_CLUSTER=1
			SKIP_CLUSTER_REASON="pool_unchanged"
			echo "INFO: skipping cd-hit-est due to unchanged active pool" 1>&2
		fi
		if [ "\$SKIP_CLUSTER" -eq 1 ]; then
			if [ -f "\${STATE_DIR}/qced_reads_nr.fasta.clstr" ]; then
				echo "INFO: reusing previous cluster file due to skip reason=\$SKIP_CLUSTER_REASON" 1>&2
				cp "\${STATE_DIR}/qced_reads_nr.fasta.clstr" ${barcode}_qced_reads_nr.fasta.clstr
			elif [ "\$SKIP_CLUSTER_REASON" = "pool_unchanged" ]; then
				echo "ERROR: active pool unchanged but previous cluster file is missing at \${STATE_DIR}/qced_reads_nr.fasta.clstr" 1>&2
				exit 1
			else
				echo "WARN: skipping clustering with no previous cluster file (reason=\$SKIP_CLUSTER_REASON)" 1>&2
			fi
		fi

			if [ "\$SKIP_CLUSTER" -eq 0 ]; then
			# -- §6: cd-hit-est clustering and freeze promotion --
			ACTIVE_POOL_LOCAL="${barcode}_active_pool.fasta"
			if [ -f "\$ACTIVE_POOL" ]; then
				cp "\$ACTIVE_POOL" "\$ACTIVE_POOL_LOCAL"
			else
				: > "\$ACTIVE_POOL_LOCAL"
			fi

			if [ -s "\$ACTIVE_POOL_LOCAL" ]; then
				if cd-hit-est -i "\$ACTIVE_POOL_LOCAL" -c ${cdHitIdentity} -d 0 -o ${barcode}_active_nr.fasta -T "\$THREADS"; then
					echo "HQ reads cd-hit is successful" 1>&2
				fi
							${baseDir}/bin/otu_parse_clstr.pl ${barcode}_active_nr.fasta.clstr ${barcode}_active_members.tsv ${barcode}_active_counts.tsv
							ACTIVE_COUNTS_INST="${barcode}_active_counts_instances.tsv"
							ACTIVE_COUNTS_STATS="${barcode}_active_counts_stats.tsv"
							# Build hash map/counts from the same FASTA used for active clustering to keep strict ID matching coherent.
							ACTIVE_HASH_MAP="${barcode}_active_hash_map.tsv"
							ACTIVE_HASH_COUNTS="${barcode}_active_hash_counts.tsv"
							${baseDir}/bin/otu_hash_map_from_fasta.pl "\$ACTIVE_POOL_LOCAL" "\$ACTIVE_HASH_MAP" "\$ACTIVE_HASH_COUNTS"
							if ! ${baseDir}/bin/otu_counts_by_hash.pl ${barcode}_active_members.tsv "\$ACTIVE_HASH_MAP" "\$ACTIVE_HASH_COUNTS" "\$ACTIVE_COUNTS_INST" "\$OTU_ID_MODE" "\$ACTIVE_COUNTS_STATS"; then
								echo "ERROR: otu_counts_by_hash.pl failed" 1>&2
								exit 1
							fi
						if [ -s "\$ACTIVE_COUNTS_STATS" ]; then
							echo "INFO: otu_counts_by_hash stats:" 1>&2
							sed 's/^/INFO: otu_counts_by_hash\t/' "\$ACTIVE_COUNTS_STATS" 1>&2 || true
							cp -f "\$ACTIVE_COUNTS_STATS" "\${STATE_DIR}/otu_counts_stats_last.tsv" 2>/dev/null || true
						fi
						active_members_rows=\$(wc -l < ${barcode}_active_members.tsv | tr -d ' ')
						active_counts_inst_rows=\$(wc -l < "\$ACTIVE_COUNTS_INST" | tr -d ' ')
						echo "INFO: OTU freeze precheck active_members=\$active_members_rows active_counts_instances=\$active_counts_inst_rows" 1>&2
					if [ "\$active_members_rows" -gt 0 ] && [ "\$active_counts_inst_rows" -eq 0 ]; then
						echo "ERROR: otu_counts_by_hash produced 0 rows for non-empty active_members; aborting to protect OTU frozen-state integrity" 1>&2
						exit 1
					fi
						${baseDir}/bin/otu_update_frozen.pl "\$ACTIVE_COUNTS_INST" ${barcode}_active_nr.fasta "\$FROZEN_META" "\$FROZEN_REPS" "\$FROZEN_HIST" "\$FROZEN_MEMBERS" ${barcode}_promoted_clusters.tsv "\$ROUND_ID" ${params.otu_frozen_min_rounds} ${params.otu_frozen_min_reads} ${params.otu_frozen_growth_window} ${params.otu_frozen_drop_ratio} ${params.otu_frozen_min_frac}
						gzip -f "\$FROZEN_REPS" 2>/dev/null || true
						# otu_update_frozen.pl writes representative rows directly to FROZEN_MEMBERS,
						# bypassing the seen-index. Invalidate it so the snapshot append step
						# below rebuilds from the current file and avoids duplicate entries.
						rm -f "\$FROZEN_MEMBERS_SEEN"
						if [ -s ${barcode}_promoted_clusters.tsv ]; then
							# Snapshot full OTU membership at freeze time (rep + members)
								if [ "\$OTU_ID_MODE" = "strict" ]; then
									if ! ${baseDir}/bin/otu_snapshot_frozen_members.pl ${barcode}_promoted_clusters.tsv ${barcode}_active_members.tsv "\$FROZEN_META" "\$ACTIVE_HASH_MAP" ${barcode}_frozen_members_snapshot.tsv "\$OTU_ID_MODE" "${params.otu_hashmap_mixed_policy}" "${params.otu_allow_unsafe_recovery}"; then
										echo "ERROR: otu_snapshot_frozen_members.pl failed in strict mode" 1>&2
										exit 1
									fi
								else
									if ! ${baseDir}/bin/otu_snapshot_frozen_members.pl ${barcode}_promoted_clusters.tsv ${barcode}_active_members.tsv "\$FROZEN_META" "\$ACTIVE_HASH_MAP" ${barcode}_frozen_members_snapshot.tsv "\$OTU_ID_MODE" "${params.otu_hashmap_mixed_policy}" "${params.otu_allow_unsafe_recovery}"; then
										echo "WARN: otu_snapshot_frozen_members.pl failed in legacy mode; continuing with empty snapshot" 1>&2
										: > ${barcode}_frozen_members_snapshot.tsv
									fi
						fi
					if [ -s ${barcode}_frozen_members_snapshot.tsv ]; then
						perl ${baseDir}/bin/otu_members_append_unique.pl "\$FROZEN_MEMBERS" ${barcode}_frozen_members_snapshot.tsv "\$FROZEN_MEMBERS_SEEN"
					fi
					awk 'NR==FNR{p[\$1]=1; next} {if(p[\$1]) print \$2}' ${barcode}_promoted_clusters.tsv ${barcode}_active_members.tsv > promoted_read_ids.list
					if [ -s promoted_read_ids.list ]; then
						seqkit grep -v -f promoted_read_ids.list "\$ACTIVE_POOL_LOCAL" > ${barcode}_active_pool_pruned.fasta || : > ${barcode}_active_pool_pruned.fasta
						cp ${barcode}_active_pool_pruned.fasta "\$ACTIVE_POOL"
					else
						cp "\$ACTIVE_POOL_LOCAL" "\$ACTIVE_POOL"
					fi
				else
					cp "\$ACTIVE_POOL_LOCAL" "\$ACTIVE_POOL"
				fi
			else
				: > ${barcode}_active_members.tsv
			fi

				# Build NR-only hash map: active cluster reps (active_nr.fasta) + frozen rep hashes (frozen_meta.tsv).
				# Only the representative UUID per cluster is present, so stable_key resolution is invariant across re-clustering.
				NR_HASH_MAP_FILE="${barcode}_otu_nr_hash_map.tsv"
				: > "\$NR_HASH_MAP_FILE"
				if [ -s "${barcode}_active_nr.fasta" ]; then
					${baseDir}/bin/otu_hash_map_from_fasta.pl \
						"${barcode}_active_nr.fasta" \
						"${barcode}_active_nr_hash_map_tmp.tsv" \
						"${barcode}_active_nr_hash_counts_tmp.tsv" \
					&& cat "${barcode}_active_nr_hash_map_tmp.tsv" >> "\$NR_HASH_MAP_FILE" || true
				fi
				if [ -s "\$FROZEN_META" ]; then
					awk -F'\t' 'NF>=3 && length(\$2)>0 && length(\$3)>0{i=index(\$2,"|"); uuid=(i>1)?substr(\$2,1,i-1):\$2; if(length(uuid)>0) print uuid"\t"\$3}' "\$FROZEN_META" >> "\$NR_HASH_MAP_FILE"
				fi
				cp "\$NR_HASH_MAP_FILE" "\${STATE_DIR}/${barcode}_otu_nr_hash_map.tsv.tmp" 2>/dev/null \
					&& mv "\${STATE_DIR}/${barcode}_otu_nr_hash_map.tsv.tmp" "\${STATE_DIR}/${barcode}_otu_nr_hash_map.tsv" || true

				[ -f "\$FROZEN_MEMBERS" ] || : > "\$FROZEN_MEMBERS"
				[ -f ${barcode}_active_members.tsv ] || : > ${barcode}_active_members.tsv
				${baseDir}/bin/otu_merge_clstr.pl "\$FROZEN_MEMBERS" ${barcode}_active_members.tsv ${barcode}_qced_reads_nr.fasta.clstr
				if [ "\$NEW_HASHES_COMMIT_OK" -eq 1 ] && [ -s "\$NEW_HASHES_TO_COMMIT" ]; then
					cat "\$NEW_HASHES_TO_COMMIT" >> "\$SEEN_HASHES"
					awk 'NF' "\$SEEN_HASHES" | LC_ALL=C sort -u > "\${SEEN_HASHES}.tmp" && mv "\${SEEN_HASHES}.tmp" "\$SEEN_HASHES"
				fi
			fi

	
			
	# -- §7: State persistence and cleanup --
	if [ -f "\${STATE_DIR}/accumulated_qced_reads.fasta" ];
	then
		rm "\${STATE_DIR}/accumulated_qced_reads.fasta"
	fi
	
	if [ -f "\${STATE_DIR}/qced_reads_nr.fasta" ];
	then
		rm "\${STATE_DIR}/qced_reads_nr.fasta"
	fi
	
	if [ -f "\${STATE_DIR}/qced_reads_nr.fasta.clstr" ];
	then
		rm "\${STATE_DIR}/qced_reads_nr.fasta.clstr"
	fi

	if [ -f ${ongoingStateDir}/${round_barcode}/accumulated_qced_reads.fasta ];
	then
		rm ${ongoingStateDir}/${round_barcode}/accumulated_qced_reads.fasta
	fi	
	if [ -f ${ongoingStateDir}/${round_barcode}/qced_reads_nr.fasta ];
	then
		rm ${ongoingStateDir}/${round_barcode}/qced_reads_nr.fasta
	fi
	
	if [ -f ${ongoingStateDir}/${round_barcode}/qced_reads_nr.fasta.clstr ];
	then
		rm ${ongoingStateDir}/${round_barcode}/qced_reads_nr.fasta.clstr
	fi
	cp ${barcode}_qced_reads_nr.fasta.clstr ${ongoingStateDir}/${round_barcode}/qced_reads_nr.fasta.clstr
	cp ${barcode}_qced_reads_nr.fasta.clstr "\${STATE_DIR}/qced_reads_nr.fasta.clstr"
	

	if [ -f ${barcode}_qced_reads_hq_accumulated.fasta ];
	then
		rm ${barcode}_qced_reads_hq_accumulated.fasta
	fi
	set -- ${barcode}_qced_reads_nr[0-9]*
	[ -f "\${1:-}" ] && rm ${barcode}_qced_reads_nr[0-9]* || true

		set -- ${barcode}_qced_reads_*normal*
	[ -f "\${1:-}" ] && rm ${barcode}_qced_reads_*normal* || true
		fi  # HQ reads non-empty
		
		"""
}

otu_def_reporting_inputs = ChannelUtils.strictRoundJoin(report_otu, demult_control)

// ============================================================
// STAGE F — REPORTING (_reporting_OTU_definition)
// ============================================================
process _reporting_OTU_definition {
  maxForks maxForksReportingVal
//  publishDir "${params.outdir}/ongoing/", mode: 'copy', overwrite: true
  input:
    tuple val(barcode), val(round_barcode), file(otu_clstr), file(demult) from otu_def_reporting_inputs
  output:
    tuple val(barcode), val(round_barcode), file("${barcode}_otu_def_rpt.txt"), file("${barcode}_otu_members_round.tsv"), file("${barcode}_otu_sizes_round.tsv") into otu_def_rpt_summary, otu_def_rpt_summary_for_blast
    script:

	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"
	OTU_SIZE_STREAK_MODE="${otuSizeStreakModeCanonical}"
	OTU_SIZE_STREAK_MIN_ROUNDS="${otuSizeStreakMinRoundsStr}"
	LOCK_WAIT=${params.lock_wait_seconds}
	source "${baseDir}/bin/lib/lock_utils.sh"
	init_lock_helpers
		
	_p_targets="${params.targets}"
	IFS='|' read -ra _TARGETS <<< "\$_p_targets"
	if ! perl ${baseDir}/bin/reporting_otu_definition.pl ${otu_clstr} ${demult} ${round_barcode} ${barcode} "\${_TARGETS[@]}"; then
		printf 'read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tOTU_id\tOTU_role\n' > ${barcode}_otu_def_rpt.txt
		printf 'otu_id\tread_id\n' > ${barcode}_otu_members_round.tsv
		printf 'otu_id\tsize\n' > ${barcode}_otu_sizes_round.tsv
	fi
	[ -f ${barcode}_otu_members_round.tsv ] || printf 'otu_id\tread_id\n' > ${barcode}_otu_members_round.tsv
	[ -f ${barcode}_otu_sizes_round.tsv ] || printf 'otu_id\tsize\n' > ${barcode}_otu_sizes_round.tsv
	mkdir -p ${ongoingStateDir}/${round_barcode}
	OTU_MEMBERS_CANONICAL="${barcode}_otu_members.tsv"
	OTU_SIZES_CANONICAL="${barcode}_otu_sizes.tsv"
	OTU_MEMBERS_CANONICAL_STATS="${barcode}_otu_members_stats.tsv"
	: > "\$OTU_MEMBERS_CANONICAL"
	: > "\$OTU_SIZES_CANONICAL"
	: > "\$OTU_MEMBERS_CANONICAL_STATS"
	if [ -s ${barcode}_otu_def_rpt.txt ]; then
		if ! perl "${baseDir}/bin/otu_export_members_from_blastreport.pl" \
			${barcode}_otu_def_rpt.txt \
			"\$OTU_MEMBERS_CANONICAL" \
			"\$OTU_SIZES_CANONICAL" \
			"\$OTU_MEMBERS_CANONICAL_STATS"; then
			echo "WARN: failed to export canonical OTU membership from ${barcode}_otu_def_rpt.txt" 1>&2
			: > "\$OTU_MEMBERS_CANONICAL"
			: > "\$OTU_SIZES_CANONICAL"
			: > "\$OTU_MEMBERS_CANONICAL_STATS"
		fi
	fi
	if [ -s "\$OTU_MEMBERS_CANONICAL_STATS" ]; then
		sed 's/^/INFO: otu_members_canonical\t/' "\$OTU_MEMBERS_CANONICAL_STATS" 1>&2 || true
	fi
	if cp ${barcode}_otu_def_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_otu_def_rpt.txt
	then
		if [ ! -f ${ongoingStateDir}/_state/${barcode}_otu_def_rpt.txt ];
		then
			mkdir -p ${ongoingStateDir}/_state
			cp ${barcode}_otu_def_rpt.txt ${ongoingStateDir}/_state/${barcode}_otu_def_rpt.txt
		else
			# Append without failing on header-only files.
			tail -n +2 ${barcode}_otu_def_rpt.txt >> ${ongoingStateDir}/_state/${barcode}_otu_def_rpt.txt 2>/dev/null || true
			echo "Updated cumulative ${barcode}_otu_def_rpt.txt in _state" 1>&2
		fi
	fi
	cp "\$OTU_MEMBERS_CANONICAL" ${ongoingStateDir}/${round_barcode}/otu_members.tsv 2>/dev/null || true
	cp "\$OTU_SIZES_CANONICAL" ${ongoingStateDir}/${round_barcode}/otu_sizes.tsv 2>/dev/null || true
	cp ${barcode}_otu_members_round.tsv ${ongoingStateDir}/${round_barcode}/otu_members_round.tsv 2>/dev/null || true
	cp ${barcode}_otu_sizes_round.tsv ${ongoingStateDir}/${round_barcode}/otu_sizes_round.tsv 2>/dev/null || true
	cp "\$OTU_MEMBERS_CANONICAL_STATS" ${ongoingStateDir}/${round_barcode}/otu_members_stats.tsv 2>/dev/null || true
	mkdir -p ${ongoingStateDir}/_state
	_canon_members_state="${ongoingStateDir}/_state/${barcode}_otu_members.tsv"
	_canon_sizes_state="${ongoingStateDir}/_state/${barcode}_otu_sizes.tsv"
	_canon_stats_state="${ongoingStateDir}/_state/${barcode}_otu_members_stats_last.tsv"
	OTU_HASH_MAP_STATE="${ongoingStateDir}/_state/${barcode}_otu_nr_hash_map.tsv"
	# Canonical snapshots are always updated, independent of size-streak mode/lock.
	cp "\$OTU_MEMBERS_CANONICAL" "\${_canon_members_state}.tmp" 2>/dev/null && mv "\${_canon_members_state}.tmp" "\$_canon_members_state" || true
	cp "\$OTU_SIZES_CANONICAL" "\${_canon_sizes_state}.tmp" 2>/dev/null && mv "\${_canon_sizes_state}.tmp" "\$_canon_sizes_state" || true
	cp "\$OTU_MEMBERS_CANONICAL_STATS" "\${_canon_stats_state}.tmp" 2>/dev/null && mv "\${_canon_stats_state}.tmp" "\$_canon_stats_state" || true
		# Phase B: size-streak tracking (observe/enforce compute-only; no pruning yet).
			OTU_SIZE_STREAK_STATE="${ongoingStateDir}/_state/${barcode}_otu_size_streak.tsv"
			OTU_SIZE_STREAK_STATS_LAST="${ongoingStateDir}/_state/${barcode}_otu_size_streak_stats_last.tsv"
			OTU_SIZE_STREAK_IDS_LAST="${ongoingStateDir}/_state/${barcode}_otu_size_streak_prune_ids_last.txt"
		OTU_SIZE_STREAK_IDS="${barcode}_otu_size_streak_prune_ids.txt"
		OTU_SIZE_STREAK_STATS="${barcode}_otu_size_streak_stats.tsv"
		OTU_SIZE_STREAK_STATE_NEXT="${barcode}_otu_size_streak.tsv"
		OTU_SIZE_STREAK_LOCK="${ongoingStateDir}/_state/.otu_size_streak.lock"
		OTU_BLAST_FILTER_MODE="${otuBlastFilterModeCanonical}"
		: > "\$OTU_SIZE_STREAK_IDS"
		: > "\$OTU_SIZE_STREAK_STATS"
		: > "\$OTU_SIZE_STREAK_STATE_NEXT"
if [ "\$OTU_SIZE_STREAK_MODE" != "off" ] || [ "\$OTU_BLAST_FILTER_MODE" != "off" ]; then
		if acquire_lock "\$OTU_SIZE_STREAK_LOCK"; then
			[ -f "\$OTU_SIZE_STREAK_STATE" ] || : > "\$OTU_SIZE_STREAK_STATE"
			[ -f "\$OTU_HASH_MAP_STATE" ] || : > "\$OTU_HASH_MAP_STATE"
			if perl "${baseDir}/bin/otu_size_streak_update.pl" \
				"\$OTU_SIZES_CANONICAL" \
				"\$OTU_MEMBERS_CANONICAL" \
				"\$OTU_HASH_MAP_STATE" \
				"\$OTU_SIZE_STREAK_STATE" \
				"\$OTU_SIZE_STREAK_MIN_ROUNDS" \
				"\$OTU_SIZE_STREAK_IDS" \
				"\$OTU_SIZE_STREAK_STATE_NEXT" \
				"\$OTU_SIZE_STREAK_STATS" \
				"" \
				"" \
				"${otuBlastMinMembersStr}"; then
				cp "\$OTU_SIZE_STREAK_STATE_NEXT" "\${OTU_SIZE_STREAK_STATE}.tmp" 2>/dev/null && mv "\${OTU_SIZE_STREAK_STATE}.tmp" "\$OTU_SIZE_STREAK_STATE" || true
				cp "\$OTU_SIZE_STREAK_STATS" "\${OTU_SIZE_STREAK_STATS_LAST}.tmp" 2>/dev/null && mv "\${OTU_SIZE_STREAK_STATS_LAST}.tmp" "\$OTU_SIZE_STREAK_STATS_LAST" || true
				cp "\$OTU_SIZE_STREAK_IDS" "\${OTU_SIZE_STREAK_IDS_LAST}.tmp" 2>/dev/null && mv "\${OTU_SIZE_STREAK_IDS_LAST}.tmp" "\$OTU_SIZE_STREAK_IDS_LAST" || true
			else
				echo "WARN: size-streak update failed in mode=\$OTU_SIZE_STREAK_MODE" 1>&2
				: > "\$OTU_SIZE_STREAK_IDS"
				: > "\$OTU_SIZE_STREAK_STATS"
				: > "\$OTU_SIZE_STREAK_STATE_NEXT"
				if [ "\$OTU_SIZE_STREAK_MODE" = "enforce" ]; then exit 1; fi
			fi
			release_lock "\$OTU_SIZE_STREAK_LOCK"
		else
			echo "WARN: size-streak lock unavailable; skipping _state size-streak updates this round" 1>&2
			if [ "\$OTU_SIZE_STREAK_MODE" = "enforce" ]; then exit 1; fi
		fi
fi
		if [ -s "\$OTU_SIZE_STREAK_STATS" ]; then
			sgv() { awk -F'\t' -v k="\$1" '\$1==k{print \$2; exit}' "\$OTU_SIZE_STREAK_STATS"; }
			_otus_total=\$(sgv otus_total)
			_otus_size_streak=\$(sgv otus_size_streak)
			_otus_prune=\$(sgv otus_prune_candidate)
			_reads_prune=\$(sgv reads_prune_candidate)
			_missing_hash=\$(sgv otus_size_streak_missing_hash_rows)
		echo "INFO: otu_size_streak mode=\$OTU_SIZE_STREAK_MODE min_rounds=\$OTU_SIZE_STREAK_MIN_ROUNDS otus_total=\${_otus_total:-0} otus_size_streak=\${_otus_size_streak:-0} otus_prune_candidate=\${_otus_prune:-0} reads_prune_candidate=\${_reads_prune:-0} phase=observe_only" 1>&2
			if [ -n "\${_missing_hash:-}" ] && [ "\${_missing_hash}" -gt 0 ] 2>/dev/null; then
				echo "WARN: otu_size_streak_missing_hash_rows=\${_missing_hash} (size-streak skipped for missing hash)" 1>&2
			fi
		fi
		cp "\$OTU_SIZE_STREAK_IDS" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak_prune_ids.txt 2>/dev/null || true
		cp "\$OTU_SIZE_STREAK_STATS" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak_stats.tsv 2>/dev/null || true
		cp "\$OTU_SIZE_STREAK_STATE_NEXT" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak.tsv 2>/dev/null || true
	
	
	
		"""
}

blast_pretax_inputs = ChannelUtils.strictRoundJoinAll([
    fastq_qced_blast,
    hac_blast,
    otu_def_rpt_summary_for_blast,
], 'blast_pretax_inputs')

// ============================================================
// STAGE G — TAXONOMIC ASSIGNMENT / BLAST (blast_OTU_pretax)
// ============================================================
process blast_OTU_pretax {
	cpus { params.blast_threads }
	    maxForks maxForksCoreCpuVal
		label 'blast'
		time '20h'

//    publishDir "${params.outdir}/ongoing/", mode: 'copy', overwrite: true
    input: 
      tuple val(barcode), val(round_barcode), file(fasta_hq_qced), file(qced_reads_nr), file(read_file), file(otu_def_rpt), file(otu_members_round), file(otu_sizes_round) from blast_pretax_inputs
    output:
      tuple val(barcode), val(round_barcode), file('blast_report_annotated.txt') into blast_agg_ch
      tuple val(barcode), val(round_barcode), file("${barcode}_blastreport_sup.sam"), file("${barcode}_round_sup.tsv"), file("${barcode}_blastreport_sup_pre.fastq"), file("${barcode}_preblastreport_join.txt"), file("blast_report_annotated_preferred.txt"), file("blast_report_annotated_noadapter.txt"), file("${barcode}_blast_filter_stats.tsv") into report_blast
      tuple val(barcode), val(round_barcode), file("blast_report_annotated.txt"), file("${barcode}_assigned_read_ids.list") into blast2consensus

	script:
	
//	read_file = file(reads)
	
	if(!usingDockerProfile){
	    db_dir = "$baseDir/"
	    taxdb_dir = "$baseDir/"
	}
	else {
	    db_dir = "/tmp/"
	    taxdb_dir = "/tmp/"
	}
		taxdb_dir = taxdb_dir + params.blast_taxdb
	
	"""
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"
	THREADS=${task.cpus}
	OTU_BLAST_MIN_MEMBERS="${otuBlastMinMembersStr}"
	OTU_BLAST_FILTER_MODE="${otuBlastFilterModeCanonical}"
	OTU_BLAST_FILTER_SKIP_ROUNDS="${otuBlastFilterSkipRoundsCanonical}"
	OTU_BLAST_ENFORCE_MISSING_MAX_FRAC="${otuBlastEnforceMissingMaxFracStr}"
	OTU_BLAST_ENFORCE_NO_CLUSTERS_POLICY="${otuBlastEnforceNoClustersPolicyCanonical}"
	OTU_SIZE_STREAK_MODE="${otuSizeStreakModeCanonical}"
	round_barcode="${round_barcode}"
	export BLASTDB=${taxdb_dir}
		STATE_DIR="${ongoingStateDir}/_state"
		OTU_HASH_MAP_STATE="${ongoingStateDir}/_state/${barcode}_otu_nr_hash_map.tsv"
		ASSIGNED_READ_IDS_EVER_STATE="\${STATE_DIR}/${barcode}_assigned_read_ids_ever.list"
		ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE="\${STATE_DIR}/${barcode}_assigned_otu_member_ids_prev_round.list"
		CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE="\${STATE_DIR}/${barcode}_consensus_assigned_member_ids_prev_round.list"
		PROTECTED_READ_IDS_EVER_STATE="\${STATE_DIR}/${barcode}_protected_read_ids_ever.list"
		DORADO_LOCK="\${STATE_DIR}/.dorado.lock"
		DORADO_LOCK_WAIT=${params.lock_wait_seconds}
		BLASTREPORT_LOCK="\${STATE_DIR}/.blastreport.lock"
		SUPFASTQ_LOCK="\${STATE_DIR}/.blastreport_sup.lock"
		QCED_LOCK="\${STATE_DIR}/.qced_reads.lock"
		mkdir -p "\${STATE_DIR}"
		LOCK_WAIT=${params.lock_wait_seconds}
		PIPELINE_BASEDIR="${baseDir}"
		source "${baseDir}/bin/lib/lock_utils.sh"
		source "${baseDir}/bin/lib/blast_process_common.sh"
		init_lock_helpers
		refresh_protected_read_ids_ever "\$PROTECTED_READ_IDS_EVER_STATE"
			append_process_timing() {
				local phase="\$1"
				local start_ts="\$2"
			local end_ts="\$3"
			local seconds=0
			if [ -n "\$start_ts" ] && [ -n "\$end_ts" ] && [[ "\$start_ts" != *[!0-9]* ]] && [[ "\$end_ts" != *[!0-9]* ]] && [ "\$end_ts" -ge "\$start_ts" ]; then
				seconds=\$(( end_ts - start_ts ))
			fi
			printf '%s\t%s\t%s\n' "\${round_barcode}" "\$phase" "\$seconds" >> blast_process_timings.tsv
			}
			printf 'round_barcode\tphase\tseconds\n' > blast_process_timings.tsv
			append_otu_refine_breakdown() {
				local phase="\$1"
				local start_ms="\$2"
				local end_ms="\$3"
				local elapsed_ms=0
				local seconds=0
				if [ -n "\$start_ms" ] && [ -n "\$end_ms" ] && [[ "\$start_ms" != *[!0-9]* ]] && [[ "\$end_ms" != *[!0-9]* ]] && [ "\$end_ms" -ge "\$start_ms" ]; then
					elapsed_ms=\$(( end_ms - start_ms ))
					seconds=\$(( elapsed_ms / 1000 ))
				fi
				{
					printf '%s\t%s\t%s\n' "\${round_barcode}" "\$phase" "\$seconds"
				} >> "\$OTU_REFINE_PROCESS_BREAKDOWN_FILE" 2>/dev/null || true
				{
					printf '%s\t%s\t%s\t%s\n' "\${round_barcode}" "\$phase" "\$seconds" "\$elapsed_ms"
				} >> "\$OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE" 2>/dev/null || true
			}
			append_sup_path_timing() {
				local phase="\$1"
				local start_ms="\$2"
				local end_ms="\$3"
				local elapsed_ms=0
				local seconds=0
				if [ -n "\$start_ms" ] && [ -n "\$end_ms" ] && [[ "\$start_ms" != *[!0-9]* ]] && [[ "\$end_ms" != *[!0-9]* ]] && [ "\$end_ms" -ge "\$start_ms" ]; then
					elapsed_ms=\$(( end_ms - start_ms ))
					seconds=\$(( elapsed_ms / 1000 ))
				fi
				{
					printf '%s\t%s\t%s\t%s\n' "\${round_barcode}" "\$phase" "\$seconds" "\$elapsed_ms"
				} >> "\$SUP_PATH_TIMINGS_MS_FILE" 2>/dev/null || true
			}

			source "${baseDir}/bin/lib/db_sig_utils.sh"
			# -- §2: OTU size pre-filter and protection re-injection --
			_t_blast_prefilter_start=\$(date +%s)

			ROUND_INDEX_FILE="\${STATE_DIR}/round_index.tsv"
			ROUND_INDEX=\$(awk -F'\t' -v rb="\$round_barcode" '\$1==rb{print \$2; exit}' "\$ROUND_INDEX_FILE" 2>/dev/null || true)
			if [ -z "\$ROUND_INDEX" ] || [[ "\$ROUND_INDEX" == *[!0-9]* ]] || [ "\$ROUND_INDEX" -lt 1 ]; then
				echo "ERROR: missing/invalid round index for round_barcode=\$round_barcode (file: \$ROUND_INDEX_FILE)" 1>&2
				exit 1
			fi
			OTU_BLAST_EFFECTIVE_MODE_TSV="${barcode}_blast_filter_effective_mode.tsv"
			${baseDir}/bin/otu_blast_effective_mode.sh \
				"\$OTU_BLAST_FILTER_MODE" \
				"\$OTU_BLAST_FILTER_SKIP_ROUNDS" \
				"\$ROUND_INDEX" \
				> "\$OTU_BLAST_EFFECTIVE_MODE_TSV"
			effective_mode_value() {
				local key="\$1"
				awk -F'\t' -v k="\$key" '\$1==k{print \$2; exit}' "\$OTU_BLAST_EFFECTIVE_MODE_TSV"
			}
			OTU_BLAST_EFFECTIVE_MODE=\$(effective_mode_value effective_mode)
			OTU_BLAST_EFFECTIVE_REASON=\$(effective_mode_value reason)
				OTU_BLAST_FORCE_USE_FILTERED="${otuBlastForceUseFiltered ? '1' : '0'}"
				if [ "\$OTU_BLAST_FORCE_USE_FILTERED" = "1" ] && [ "\$OTU_BLAST_EFFECTIVE_MODE" != "off" ]; then
					echo "INFO: otu_blast_filter force_use_filtered=1 bypassing threshold and no-cluster decision knobs" 1>&2
					if [ "\$OTU_BLAST_EFFECTIVE_MODE" != "enforce" ]; then
						echo "WARN: forcing otu blast filter to enforce (was effective_mode=\$OTU_BLAST_EFFECTIVE_MODE reason=\$OTU_BLAST_EFFECTIVE_REASON)" 1>&2
					fi
				OTU_BLAST_EFFECTIVE_MODE="enforce"
				OTU_BLAST_EFFECTIVE_REASON="force_use_filtered"
			fi
			if [ "\$OTU_BLAST_EFFECTIVE_MODE" != "off" ] && [ "\$OTU_BLAST_EFFECTIVE_MODE" != "observe" ] && [ "\$OTU_BLAST_EFFECTIVE_MODE" != "enforce" ]; then
				echo "ERROR: invalid otu blast effective mode '\$OTU_BLAST_EFFECTIVE_MODE'" 1>&2
				exit 1
			fi
			echo "INFO: otu_blast_filter configured_mode=\$OTU_BLAST_FILTER_MODE effective_mode=\$OTU_BLAST_EFFECTIVE_MODE reason=\$OTU_BLAST_EFFECTIVE_REASON skip_rounds=\$OTU_BLAST_FILTER_SKIP_ROUNDS round_index=\$ROUND_INDEX" 1>&2

				BLAST_INPUT_FASTA="${fasta_hq_qced}"
				BLAST_FILTERED_FASTA="${barcode}_blast_filter_input.fasta"
				BLAST_FILTER_STATS="${barcode}_blast_filter_stats.tsv"
				BLAST_FILTER_KEPT_OTUS="${barcode}_blast_filter_kept_otus.tsv"
				BLAST_FILTER_DROPPED_IDS="${barcode}_blast_filter_dropped_read_ids.list"
				BLAST_FILTER_MISSING_POLICY="keep"
				BLAST_FILTER_DECISION="${barcode}_blast_filter_decision.tsv"
			ROUND_HASH_MAP="${barcode}_blast_round_hash_map.tsv"
			ROUND_HASH_COUNTS="${barcode}_blast_round_hash_counts.tsv"
			OTU_MEMBERS_BLASTDIAG="${barcode}_otu_members_blastdiag.tsv"
			OTU_SIZES_BLASTDIAG="${barcode}_otu_sizes_blastdiag.tsv"
			OTU_MEMBERS_BLASTDIAG_STATS="${barcode}_otu_members_blastdiag_stats.tsv"
			{
				printf 'effective_mode\t%s\n' "\$OTU_BLAST_EFFECTIVE_MODE"
				printf 'configured_mode\t%s\n' "\$OTU_BLAST_FILTER_MODE"
			} > "\$BLAST_FILTER_STATS"
				: > "\$BLAST_FILTER_KEPT_OTUS"
				: > "\$BLAST_FILTER_DROPPED_IDS"
				: > "\$BLAST_FILTER_DECISION"
			: > "\$ROUND_HASH_MAP"
			: > "\$ROUND_HASH_COUNTS"
			: > "\$OTU_MEMBERS_BLASTDIAG"
			: > "\$OTU_SIZES_BLASTDIAG"
			: > "\$OTU_MEMBERS_BLASTDIAG_STATS"
			# Rollout guard: compute filter stats in observe/enforce, but only switch query FASTA in enforce.
			if [ "\$OTU_BLAST_EFFECTIVE_MODE" = "observe" ] || [ "\$OTU_BLAST_EFFECTIVE_MODE" = "enforce" ]; then
				if [ "\$OTU_BLAST_EFFECTIVE_MODE" = "enforce" ]; then
					BLAST_FILTER_MISSING_POLICY="drop"
				fi
				${baseDir}/bin/otu_hash_map_from_fasta.pl \
					"${fasta_hq_qced}" \
					"\$ROUND_HASH_MAP" \
					"\$ROUND_HASH_COUNTS"
				perl "${baseDir}/bin/otu_filter_reads_by_otu_size.pl" \
					"${qced_reads_nr}" \
					"\$ROUND_HASH_MAP" \
					"${fasta_hq_qced}" \
					"\$OTU_BLAST_MIN_MEMBERS" \
					"\$BLAST_FILTERED_FASTA" \
					"\$BLAST_FILTER_STATS" \
					"\$BLAST_FILTER_KEPT_OTUS" \
					"\$BLAST_FILTER_MISSING_POLICY"
				${baseDir}/bin/otu_blast_filter_decide.sh \
					"\$BLAST_FILTER_STATS" \
					"\$OTU_BLAST_EFFECTIVE_MODE" \
					"\$OTU_BLAST_ENFORCE_MISSING_MAX_FRAC" \
					"\$OTU_BLAST_ENFORCE_NO_CLUSTERS_POLICY" \
					"\$OTU_BLAST_FORCE_USE_FILTERED" \
					> "\$BLAST_FILTER_DECISION"

				decision_value() {
					local key="\$1"
					awk -F'\t' -v k="\$key" '\$1==k{print \$2; exit}' "\$BLAST_FILTER_DECISION"
				}
				decision=\$(decision_value decision)
				reason=\$(decision_value reason)
				missing_frac=\$(decision_value missing_frac)
				reads_total=\$(decision_value total_reads)
				reads_missing=\$(decision_value reads_missing_from_clstr)
				clstr_has_clusters=\$(decision_value clstr_has_clusters)
				clstr_records=\$(decision_value clstr_records)
				total_otus=\$(decision_value total_otus)
				ambiguous_hash_cluster=\$(decision_value ambiguous_hash_cluster)
					echo "INFO: otu_blast_filter mode=\$OTU_BLAST_FILTER_MODE effective_mode=\$OTU_BLAST_EFFECTIVE_MODE min_members=\$OTU_BLAST_MIN_MEMBERS decision=\$decision reason=\$reason missing_frac=\$missing_frac reads_total=\$reads_total reads_missing_from_clstr=\$reads_missing clstr_has_clusters=\$clstr_has_clusters clstr_records=\$clstr_records total_otus=\$total_otus ambiguous_hash_cluster=\$ambiguous_hash_cluster no_clusters_policy=\$OTU_BLAST_ENFORCE_NO_CLUSTERS_POLICY round_index=\$ROUND_INDEX skip_rounds=\$OTU_BLAST_FILTER_SKIP_ROUNDS" 1>&2
					if [ "\$decision" = "use_filtered" ]; then
						BLAST_INPUT_FASTA="\$BLAST_FILTERED_FASTA"
						count_marker_reads() {
							local fasta_path="\$1"
							local marker_name="\$2"
							if [ ! -s "\$fasta_path" ]; then
								printf '0\n'
								return
							fi
							awk -F'|' -v marker="\$marker_name" '/^>/{hdr=substr(\$0,2); n=split(hdr,a,"|"); if(n>=2 && a[2]==marker) c++} END{print c+0}' "\$fasta_path"
						}
						extract_marker_reads() {
							local fasta_path="\$1"
							local marker_name="\$2"
							awk -F'|' -v marker="\$marker_name" '/^>/{hdr=substr(\$0,2); n=split(hdr,a,"|"); keep=(n>=2 && a[2]==marker)} keep{print}' "\$fasta_path"
						}
						MARKER_RESCUE_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_blast_marker_rescue.tsv"
						: > "\$MARKER_RESCUE_STATS"
						if [ "\$OTU_BLAST_MIN_MEMBERS" -gt 0 ]; then
							_p_targets="${params.targets}"
							IFS='|' read -ra _TARGETS <<< "\$_p_targets"
						for target_marker in "\${_TARGETS[@]}"; do
								if [ "\$target_marker" = "null" ] || [ -z "\$target_marker" ]; then
									continue
								fi
								orig_marker_count=\$(count_marker_reads "${fasta_hq_qced}" "\$target_marker")
								filtered_marker_count=\$(count_marker_reads "\$BLAST_INPUT_FASTA" "\$target_marker")
								rescued_marker_count=0
								if [ "\$orig_marker_count" -gt 0 ] && [ "\$filtered_marker_count" -lt "\$OTU_BLAST_MIN_MEMBERS" ]; then
									MARKER_RESCUE_FASTA="${ongoingStateDir}/${round_barcode}/${barcode}_blast_marker_rescue_\${target_marker}.fasta"
									extract_marker_reads "${fasta_hq_qced}" "\$target_marker" > "\$MARKER_RESCUE_FASTA" || : > "\$MARKER_RESCUE_FASTA"
									if [ -s "\$MARKER_RESCUE_FASTA" ]; then
										MARKER_RESCUE_MERGED="${ongoingStateDir}/${round_barcode}/${barcode}_blast_input_marker_rescue_\${target_marker}.fasta"
										awk '/^>/{id=\$0; if(!(id in seen)){seen[id]=1; print; skip=0} else skip=1; next} !skip{print}' "\$BLAST_INPUT_FASTA" "\$MARKER_RESCUE_FASTA" > "\$MARKER_RESCUE_MERGED"
										BLAST_INPUT_FASTA="\$MARKER_RESCUE_MERGED"
										rescued_marker_count=\$(( orig_marker_count - filtered_marker_count ))
										echo "INFO: rescuing sparse marker=\$target_marker into BLAST input because filtered_count=\$filtered_marker_count < min_members=\$OTU_BLAST_MIN_MEMBERS (original_count=\$orig_marker_count)" 1>&2
									fi
								fi
								printf 'marker\t%s\noriginal_count\t%s\nfiltered_count\t%s\nrescued_count\t%s\n' "\$target_marker" "\$orig_marker_count" "\$filtered_marker_count" "\$rescued_marker_count" >> "\$MARKER_RESCUE_STATS"
							done
						fi
						awk '/^>/{id=substr(\$0,2); sub(/ .*/, "", id); split(id,a,"|"); if (a[1]!="") print a[1]}' "${fasta_hq_qced}" | LC_ALL=C sort -u > "${barcode}_blast_filter_in_ids.list"
						awk '/^>/{id=substr(\$0,2); sub(/ .*/, "", id); split(id,a,"|"); if (a[1]!="") print a[1]}' "\$BLAST_INPUT_FASTA" | LC_ALL=C sort -u > "${barcode}_blast_filter_kept_ids.list"
						comm -23 "${barcode}_blast_filter_in_ids.list" "${barcode}_blast_filter_kept_ids.list" > "\$BLAST_FILTER_DROPPED_IDS" || : > "\$BLAST_FILTER_DROPPED_IDS"
						rm -f "${barcode}_blast_filter_in_ids.list" "${barcode}_blast_filter_kept_ids.list"
						# Preserve sticky protected reads: re-inject their current members into BLAST input
						ASSIGNED_OTU_KEYS_EVER="\${STATE_DIR}/${barcode}_assigned_otu_keys_ever.list"
						ASSIGNED_OTU_PRESERVE_PREBLAST="${ongoingStateDir}/${round_barcode}/${barcode}_assigned_otu_preserve_preblast.tsv"
						_assigned_keys_count=0
						[ -s "\$ASSIGNED_OTU_KEYS_EVER" ] && _assigned_keys_count=\$(wc -l < "\$ASSIGNED_OTU_KEYS_EVER" | tr -d ' ')
						_protected_ever_count=0
						[ -s "\$PROTECTED_READ_IDS_EVER_STATE" ] && _protected_ever_count=\$(wc -l < "\$PROTECTED_READ_IDS_EVER_STATE" | tr -d ' ')
						_protected_added=0
						if [ -s "\$PROTECTED_READ_IDS_EVER_STATE" ]; then
							_PROTECTED_IDS="${ongoingStateDir}/${round_barcode}/${barcode}_blast_protected_ids.list"
							cp "\$PROTECTED_READ_IDS_EVER_STATE" "\$_PROTECTED_IDS" 2>/dev/null || : > "\$_PROTECTED_IDS"
							if [ -s "\$_PROTECTED_IDS" ]; then
								_PROTECTED_FASTA="${ongoingStateDir}/${round_barcode}/${barcode}_blast_protected.fasta"
								# FASTA headers are UUID|TARGET|... but _PROTECTED_IDS has bare UUIDs;
								# awk matches on UUID prefix (before first |) to extract protected reads.
								awk 'NR==FNR{ids[\$1]=1; next} /^>/{uuid=substr(\$0,2); sub(/[|].*/,"",uuid); p=(uuid in ids); if(p)print; next} p{print}' "\$_PROTECTED_IDS" "${fasta_hq_qced}" > "\$_PROTECTED_FASTA" || : > "\$_PROTECTED_FASTA"
								if [ -s "\$_PROTECTED_FASTA" ]; then
									_MERGED="${ongoingStateDir}/${round_barcode}/${barcode}_blast_input_merged.fasta"
									awk '/^>/{id=\$0; if(!(id in seen)){seen[id]=1; print; skip=0} else skip=1; next} !skip{print}' "\$BLAST_INPUT_FASTA" "\$_PROTECTED_FASTA" > "\$_MERGED"
									BLAST_INPUT_FASTA="\$_MERGED"
									_protected_added=\$(wc -l < "\$_PROTECTED_IDS" | tr -d ' ')
								fi
							fi
						fi
						{
							printf 'assigned_otu_keys_ever_count_preblast\t%s\n' "\$_assigned_keys_count"
							printf 'protected_read_ids_ever_count_preblast\t%s\n' "\$_protected_ever_count"
							printf 'protected_reads_added_to_blast_input\t%s\n' "\$_protected_added"
						} > "\$ASSIGNED_OTU_PRESERVE_PREBLAST"
					fi
				fi
			_t_blast_prefilter_end=\$(date +%s)
			append_process_timing "prefilter" "\$_t_blast_prefilter_start" "\$_t_blast_prefilter_end"
			
		_WORD_SIZE=50
		_QCOV=50
		_p_targets="${params.targets}"
		IFS='|' read -ra _TARGETS   <<< "\$_p_targets"
		_p_blast_db_specs="${params.blast_db_specs}"
		IFS='|' read -ra _BLAST_DBS <<< "\$_p_blast_db_specs"
		_p_blast_id_family="${params.blast_id_family}"
		IFS='|' read -ra _ID_FAMILY <<< "\$_p_blast_id_family"
		_p_blast_id_genus="${params.blast_id_genus}"
		IFS='|' read -ra _ID_GENUS  <<< "\$_p_blast_id_genus"
		_p_blast_id_spec="${params.blast_id_spec}"
		IFS='|' read -ra _ID_SPEC   <<< "\$_p_blast_id_spec"
		_p_nonncbi_memtax="${params.nonncbi_memtax}"
		IFS='|' read -ra _MEMTAX    <<< "\$_p_nonncbi_memtax"
		# -- §3: Per-target BLAST --
		_t_per_target_blast_start=\$(date +%s)
		"${baseDir}/bin/blast_otu_pretax.sh" \
			"${barcode}" \
			"\$BLAST_INPUT_FASTA" \
			"\$STATE_DIR" \
			"\$THREADS" \
			"${baseDir}" \
			"${db_dir}" \
			"${taxdb_dir}" \
			"${ongoingStateDir}/${round_barcode}" \
			"\$_p_targets" \
			"\$_p_blast_db_specs" \
			"${params.blast_id_family}" \
			"${params.blast_id_genus}" \
			"${params.blast_id_spec}" \
			"${params.nonncbi_memtax}" \
			"${params.blast_evalue}" \
			"${params.blast_max_hsps}"
		_t_per_target_blast_end=\$(date +%s)
		append_process_timing "per_target_blast" "\$_t_per_target_blast_start" "\$_t_per_target_blast_end"
	
		# -- §4: BLAST report merge and OTU refinement --
		_t_blastreport_merge_start=\$(date +%s)
		STATE_BLASTREPORT_SNAPSHOT="${barcode}_blastreport_state_snapshot.txt"
		STATE_BLASTREPORT_EXISTS=0
		if acquire_lock "\${BLASTREPORT_LOCK}"; then
			if [ -f "\${STATE_DIR}/blastreport.txt" ]; then
				cp "\${STATE_DIR}/blastreport.txt" "\$STATE_BLASTREPORT_SNAPSHOT"
				STATE_BLASTREPORT_EXISTS=1
			fi
			release_lock "\${BLASTREPORT_LOCK}"
		else
			exit 1
		fi
		if [ "\$STATE_BLASTREPORT_EXISTS" -eq 0 ]; then
			cp ${barcode}_blastreport_join.txt ${barcode}_blastreport.txt
		else
			# Keep one line per qseqid; load new entries (small), stream existing (large).
			awk 'NR==FNR {
					line=\$0; if(line=="") next;
					if(index(line,";")>0){ split(line,a,";"); } else { split(line,a,","); }
					q=a[1]; if(q=="") next; len=a[4]+0; pid=a[5]+0;
					if(!(q in best) || pid>bp[q] || (pid==bp[q] && len>bl[q])) {
						best[q]=line; bp[q]=pid; bl[q]=len;
					}
					next
				} {
					line=\$0; if(line=="") next;
					if(index(line,";")>0){ split(line,a,";"); } else { split(line,a,","); }
					q=a[1]; if(q=="") next; len=a[4]+0; pid=a[5]+0;
					if (q in best) {
						if (bp[q]>pid || (bp[q]==pid && bl[q]>=len)) { print best[q]; } else { print line; }
						delete best[q]; delete bp[q]; delete bl[q];
					} else { print line; }
				} END {
					for(q in best) print best[q];
				}' ${barcode}_blastreport_join.txt "\$STATE_BLASTREPORT_SNAPSHOT" \
			| LC_ALL=C sort > ${barcode}_blastreport.txt
		fi
		rm -f "\$STATE_BLASTREPORT_SNAPSHOT"
		_t_blastreport_merge_end=\$(date +%s)
		append_process_timing "blastreport_merge" "\$_t_blastreport_merge_start" "\$_t_blastreport_merge_end"
		_t_otu_refine_start=\$(date +%s)
		    OTU_REFINE_PHASE_TIMINGS_FILE="${barcode}_otu_refine_phase_timings.tsv"
		    OTU_REFINE_PHASE_TIMINGS_MS_FILE="${barcode}_otu_refine_phase_timings_ms.tsv"
		    OTU_REFINE_WORKLOAD_STATS_FILE="${barcode}_otu_refine_workload_stats.tsv"
		    OTU_REFINE_PROCESS_BREAKDOWN_FILE="${barcode}_otu_refine_process_breakdown.tsv"
		    OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE="${barcode}_otu_refine_process_breakdown_ms.tsv"
		    SUP_PATH_STATS_FILE="${barcode}_sup_path_stats.tsv"
		    SUP_PATH_TIMINGS_MS_FILE="${barcode}_sup_path_timings_ms.tsv"
		    DORADO_SUMMARY_HEADER='input_filename\tbatch_id\tparent_read_id\tread_id\trun_id\tchannel\tmux\tminknow_events\tstart_time\tduration\tpasses_filtering\ttemplate_start\tnum_events_template\ttemplate_duration\tsequence_length_template\tmean_qscore_template\tpore_type\texperiment_id\tsample_id\tend_reason\n'
		    if ! printf 'round_barcode\tphase\tseconds\n' > "\$OTU_REFINE_PHASE_TIMINGS_FILE" 2>/dev/null; then
		    	:
		    fi
		    if ! printf 'round_barcode\tphase\tseconds\tms\n' > "\$OTU_REFINE_PHASE_TIMINGS_MS_FILE" 2>/dev/null; then
		    	:
		    fi
		    if ! printf 'round_barcode\tphase\tseconds\n' > "\$OTU_REFINE_PROCESS_BREAKDOWN_FILE" 2>/dev/null; then
		    	:
		    fi
		    if ! printf 'round_barcode\tphase\tseconds\tms\n' > "\$OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE" 2>/dev/null; then
		    	:
		    fi
		    if ! printf 'round_barcode\tphase\tseconds\tms\n' > "\$SUP_PATH_TIMINGS_MS_FILE" 2>/dev/null; then
		    	:
		    fi
		    if ! {
		        printf 'key\tvalue\n'
	        printf 'cluster_count\t0\n'
	        printf 'cluster_records\t0\n'
	        printf 'blastreport_rows\t0\n'
	        printf 'cluster_taxids_rows\t0\n'
	        printf 'worker_count\t0\n'
	        printf 'shard_count\t0\n'
	        printf 'shard_scheduler_mode\tequal_record_count\n'
	        printf 'target_records_per_shard\t0\n'
	        printf 'smallest_shard_records\t0\n'
	        printf 'median_shard_records\t0\n'
	        printf 'largest_shard_records\t0\n'
		        printf 'largest_shard_fraction\t0\n'
		        printf 'max_single_cluster_records\t0\n'
		        printf 'max_single_cluster_fraction\t0\n'
		        printf 'merged_pairs_rows\t0\n'
		        printf 'merged_pairs_bytes\t0\n'
		        printf 'annotated_rows\t0\n'
		        printf 'annotated_bytes\t0\n'
		        printf 'annotated_file_bytes\t0\n'
		    } > "\$OTU_REFINE_WORKLOAD_STATS_FILE" 2>/dev/null; then
		    	:
		    fi
		    if ! {
		    	printf 'key\tvalue\n'
		    	printf 'hac2sup_candidate_rows\t0\n'
		    	printf 'hac2sup_candidate_unique_read_ids\t0\n'
		    	printf 'sup_annotation_input_rows\t0\n'
		    	printf 'dorado_sup_sam_records\t0\n'
		    	printf 'dorado_sup_fastq_reads\t0\n'
		    	printf 'hac2sup_sup_fasta_reads\t0\n'
		    	printf 'sup_cache_hit_ids\t0\n'
		    	printf 'sup_cache_miss_ids\t0\n'
		    	printf 'sup_cache_restored_fastq_reads\t0\n'
		    	printf 'sup_cache_restored_summary_rows\t0\n'
		    	printf 'dorado_sup_reads_requested\t0\n'
		    	printf 'dorado_sup_sam_records_new\t0\n'
		    	printf 'dorado_sup_fastq_reads_new\t0\n'
		        printf 'dorado_sup_summary_rows_new\t0\n'
		        printf 'sup_pre_fastq_reads_merged\t0\n'
		        printf 'sup_summary_rows_merged\t0\n'
		        printf 'sup_cache_restore_missing_fastq_ids\t0\n'
		        printf 'sup_cache_restore_missing_summary_ids\t0\n'
		        printf 'shared_extract_union_ids\t0\n'
		        printf 'shared_extract_hac2sup_ids\t0\n'
		        printf 'shared_extract_hac_fixed_ids\t0\n'
		        printf 'shared_extract_fasta_reads\t0\n'
		    } > "\$SUP_PATH_STATS_FILE" 2>/dev/null; then
		    	:
		    fi
		    hac2sup_candidate_rows=0
		    hac2sup_candidate_unique_read_ids=0
		    sup_annotation_input_rows=0
		    dorado_sup_sam_records=0
		    dorado_sup_fastq_reads=0
		    hac2sup_sup_fasta_reads=0
		    sup_cache_hit_ids=0
		    sup_cache_miss_ids=0
		    sup_cache_restored_fastq_reads=0
		    sup_cache_restored_summary_rows=0
		    dorado_sup_reads_requested=0
		    dorado_sup_sam_records_new=0
		    dorado_sup_fastq_reads_new=0
		    dorado_sup_summary_rows_new=0
		    sup_pre_fastq_reads_merged=0
		    sup_summary_rows_merged=0
		    sup_cache_restore_missing_fastq_ids=0
		    sup_cache_restore_missing_summary_ids=0
	    : > blast_report_annotated.txt
	    : > blast_report_annotated_otu.txt
	    : > blast_report_annotated_otu_evidence.txt
	    : > blast_report_annotated_preferred.txt

			    # Only attempt refinement when both inputs are non-empty.
			    # Refinement is a core classification step, so unexpected failure is fatal.
				    _t_otu_refine_wrapper_start=\$(now_ms)
			    if [ -s "${barcode}_blastreport_round.txt" ] && [ -s "${qced_reads_nr}" ]; then
			        if ! OTU_REFINE_ROUND_ID="${round_barcode}" \
		             OTU_REFINE_PHASE_TIMINGS_FILE="\$OTU_REFINE_PHASE_TIMINGS_FILE" \
		             OTU_REFINE_PHASE_TIMINGS_MS_FILE="\$OTU_REFINE_PHASE_TIMINGS_MS_FILE" \
		             OTU_REFINE_WORKLOAD_STATS_FILE="\$OTU_REFINE_WORKLOAD_STATS_FILE" \
		             bash "${baseDir}/bin/otu_refine_blastreport_parallel.sh" \
		             "${barcode}_blastreport_round.txt" \
		             "${qced_reads_nr}" \
		             "${baseDir}/${params.nonncbi_id2lineage_target}" \
	             "\$THREADS" \
	          > blast_report_annotated_otu.txt; then
			            echo "ERROR: otu_refine_blastreport_parallel.sh failed for ${barcode}/${round_barcode}" 1>&2
			            exit 1
			        fi
			    fi
			    _t_otu_refine_wrapper_end=\$(now_ms)
			    append_otu_refine_breakdown "wrapper_invoke_total" "\$_t_otu_refine_wrapper_start" "\$_t_otu_refine_wrapper_end"
	    # Ensure OTU tokens always include marker when possible (OTUB_xxx-MARKER).
	    _t_otu_refine_marker_start=\$(now_ms)
	    if [ -s blast_report_annotated_otu.txt ]; then
	        awk 'BEGIN{FS=OFS="\t"}
            /^#/ {print; next}
            {
                hdr=\$1;
                n=split(hdr, a, "|");
                if (n < 2) { print; next }
                target=a[2];
                otu_idx=0;
                for (i=1; i<=n; i++) {
                    if (a[i] ~ /^OTUB_/) { otu_idx=i; break }
                }
                if (otu_idx > 0 && a[otu_idx] !~ /-/ && target != "" && target != "NA") {
                    a[otu_idx] = a[otu_idx] "-" target;
                    hdr = a[1];
                    for (i=2; i<=n; i++) hdr = hdr "|" a[i];
                    \$1 = hdr;
                }
                print
            }' blast_report_annotated_otu.txt > blast_report_annotated_otu.norm || :
	        mv blast_report_annotated_otu.norm blast_report_annotated_otu.txt
	    fi
	    _t_otu_refine_marker_end=\$(now_ms)
	    append_otu_refine_breakdown "post_marker_normalize" "\$_t_otu_refine_marker_start" "\$_t_otu_refine_marker_end"

		_t_otu_refine_evidence_copy_start=\$(now_ms)
		cp blast_report_annotated_otu.txt blast_report_annotated_otu_evidence.txt 2>/dev/null || true
		_t_otu_refine_evidence_copy_end=\$(now_ms)
		append_otu_refine_breakdown "evidence_copy" "\$_t_otu_refine_evidence_copy_start" "\$_t_otu_refine_evidence_copy_end"
				OBSERVED_NO_ADAPTER=0
			OBSERVED_NON_NO_ADAPTER=0
			SEPARATE_NO_ADAPTER=0
			NO_ADAPTER_POLICY_TSV="${barcode}_no_adapter_policy.tsv"
			: > "\$NO_ADAPTER_POLICY_TSV"
					_t_otu_refine_no_adapter_start=\$(now_ms)
					if [ -s blast_report_annotated_otu_evidence.txt ]; then
						if ! bash ${baseDir}/bin/detect_no_adapter_policy.sh blast_report_annotated_otu_evidence.txt > "\$NO_ADAPTER_POLICY_TSV"; then
							echo "ERROR: detect_no_adapter_policy.sh failed for ${barcode}/${round_barcode}" 1>&2
							exit 1
						fi
						no_adapter_policy_value() {
							local key="\$1"
							awk -F'\t' -v k="\$key" '\$1==k{print \$2; exit}' "\$NO_ADAPTER_POLICY_TSV"
						}
						validate_no_adapter_policy_value() {
							local key="\$1"
							local value="\$2"
							case "\$value" in
								0|1)
									return 0
									;;
							esac
							echo "ERROR: invalid no-adapter policy value for \${key}: '\${value}'" 1>&2
							exit 1
						}
						OBSERVED_NO_ADAPTER=\$(no_adapter_policy_value observed_no_adapter)
						OBSERVED_NON_NO_ADAPTER=\$(no_adapter_policy_value observed_non_no_adapter)
						SEPARATE_NO_ADAPTER=\$(no_adapter_policy_value separate_no_adapter)
						validate_no_adapter_policy_value observed_no_adapter "\$OBSERVED_NO_ADAPTER"
						validate_no_adapter_policy_value observed_non_no_adapter "\$OBSERVED_NON_NO_ADAPTER"
						validate_no_adapter_policy_value separate_no_adapter "\$SEPARATE_NO_ADAPTER"
					fi
					mkdir -p "${ongoingStateDir}/\${round_barcode}" 2>/dev/null || true
					cp "\$NO_ADAPTER_POLICY_TSV" "${ongoingStateDir}/\${round_barcode}/\$NO_ADAPTER_POLICY_TSV" 2>/dev/null || true
			_t_otu_refine_no_adapter_end=\$(now_ms)
			append_otu_refine_breakdown "no_adapter_policy" "\$_t_otu_refine_no_adapter_start" "\$_t_otu_refine_no_adapter_end"
			echo "INFO: no_adapter_policy observed_no_adapter=\$OBSERVED_NO_ADAPTER observed_non_no_adapter=\$OBSERVED_NON_NO_ADAPTER separate_no_adapter=\$SEPARATE_NO_ADAPTER" 1>&2

		_t_otu_refine_end=\$(date +%s)
		append_process_timing "otu_refine" "\$_t_otu_refine_start" "\$_t_otu_refine_end"
		_t_assignment_state_updates_start=\$(date +%s)
	: > ${barcode}_assigned_read_ids.list
	if [ -s "${barcode}_blastreport_round.txt" ]; then
		if ! perl ${baseDir}/bin/blast_assigned_read_ids.pl \
			"${barcode}_blastreport_round.txt" \
			${barcode}_assigned_read_ids.list; then
			echo "WARN: failed to compute assigned read IDs; disabling unassigned-cluster prune for this round" 1>&2
			: > ${barcode}_assigned_read_ids.list
		fi
	fi
	# Persist assigned read IDs (round snapshot + ever-assigned history).
	ROUND_DIR="${ongoingStateDir}/${round_barcode}"
	mkdir -p "\$ROUND_DIR" "\$STATE_DIR"
	ASSIGNED_READ_IDS_RAW_ROUND="\$ROUND_DIR/${barcode}_assigned_read_ids_raw_current.list"
	cp ${barcode}_assigned_read_ids.list "\$ASSIGNED_READ_IDS_RAW_ROUND" 2>/dev/null || true
	if ! persist_read_ids_ever_state \
		${barcode}_assigned_read_ids.list \
		"\$ASSIGNED_READ_IDS_EVER_STATE"; then
		echo "ERROR: failed to persist assigned read IDs" 1>&2
		exit 1
	fi
	refresh_protected_read_ids_ever "\$PROTECTED_READ_IDS_EVER_STATE"
	if ! filter_ids_present_in_fasta \
		"\$ASSIGNED_READ_IDS_EVER_STATE" \
		"${fasta_hq_qced}" \
		"\$ROUND_DIR/${barcode}_assigned_read_ids.list"; then
		echo "ERROR: failed to materialize effective assigned read IDs for current pool" 1>&2
		exit 1
	fi
		ASSIGNED_OTU_KEYS_EVER="\${STATE_DIR}/${barcode}_assigned_otu_keys_ever.list"
		BLAST_ASSIGNED_OTU_KEYS_ROUND="\$ROUND_DIR/${barcode}_blast_assigned_otu_keys_round.list"
			ASSIGNED_OTU_MEMBERS_CURRENT_RAW="\$ROUND_DIR/${barcode}_assigned_otu_member_ids_raw_current.list"
			ASSIGNED_OTU_MEMBERS_EVER="\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list"
			ASSIGNED_OTU_KEYS_PERSIST_STATS="\$ROUND_DIR/${barcode}_assigned_otu_keys_persist_stats.tsv"
				ASSIGNED_OTU_PRESERVE_STATS="\$ROUND_DIR/${barcode}_assigned_otu_preserve_stats.tsv"
				if ! perl ${baseDir}/bin/blast_assigned_otu_keys.pl \
					blast_report_annotated_otu_evidence.txt \
					"\$BLAST_ASSIGNED_OTU_KEYS_ROUND" \
					"\$OTU_HASH_MAP_STATE" \
					--min-level "${assignProtLevelCanonical}"; then
					echo "ERROR: failed to extract blast-assigned OTU keys" 1>&2
					exit 1
			fi
			if ! perl ${baseDir}/bin/persist_otu_keys_ever.pl \
				"\$BLAST_ASSIGNED_OTU_KEYS_ROUND" \
				"\$ASSIGNED_OTU_KEYS_EVER" \
				"\$ASSIGNED_OTU_KEYS_PERSIST_STATS"; then
				echo "ERROR: failed to persist blast-assigned OTU keys" 1>&2
				exit 1
			fi
			if ! perl ${baseDir}/bin/expand_otu_keys_to_member_ids.pl \
				"\$BLAST_ASSIGNED_OTU_KEYS_ROUND" \
				"\$ROUND_DIR/otu_members_round.tsv" \
				"\$ASSIGNED_OTU_MEMBERS_CURRENT_RAW" \
				"" \
				"\$OTU_HASH_MAP_STATE"; then
				echo "ERROR: failed to expand current blast-assigned OTU keys into member reads" 1>&2
				exit 1
			fi
			if ! materialize_round_grace_ids \
				"\$ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE" \
				"\$ASSIGNED_OTU_MEMBERS_CURRENT_RAW" \
				"\$ASSIGNED_OTU_MEMBERS_EVER"; then
				echo "ERROR: failed to materialize next-round grace assigned-OTU member reads for current pool" 1>&2
				exit 1
			fi
			_assigned_otu_keys_ever_count=0
			_assigned_otu_member_ids_ever_count=0
			[ -s "\$ASSIGNED_OTU_KEYS_EVER" ] && _assigned_otu_keys_ever_count=\$(wc -l < "\$ASSIGNED_OTU_KEYS_EVER" | tr -d ' ')
			[ -s "\$ASSIGNED_OTU_MEMBERS_EVER" ] && _assigned_otu_member_ids_ever_count=\$(wc -l < "\$ASSIGNED_OTU_MEMBERS_EVER" | tr -d ' ')
			{
				printf 'protected_otu_keys_round_count\t%s\n' "\$_assigned_otu_keys_ever_count"
				printf 'protected_otu_member_ids_round_count\t%s\n' "\$_assigned_otu_member_ids_ever_count"
			} > "\$ASSIGNED_OTU_PRESERVE_STATS"
		if [ -s "\$ASSIGNED_OTU_KEYS_PERSIST_STATS" ]; then
			cat "\$ASSIGNED_OTU_KEYS_PERSIST_STATS" >> "\$ASSIGNED_OTU_PRESERVE_STATS"
		fi
		ASSIGNED_OTU_PRESERVE_PREBLAST="\$ROUND_DIR/${barcode}_assigned_otu_preserve_preblast.tsv"
			if [ -s "\$ASSIGNED_OTU_PRESERVE_PREBLAST" ]; then
				cat "\$ASSIGNED_OTU_PRESERVE_PREBLAST" >> "\$ASSIGNED_OTU_PRESERVE_STATS"
			rm -f "\$ASSIGNED_OTU_PRESERVE_PREBLAST"
		fi
		_t_assignment_state_updates_end=\$(date +%s)
		append_process_timing "assignment_state_updates" "\$_t_assignment_state_updates_start" "\$_t_assignment_state_updates_end"
			if [ "\$SEPARATE_NO_ADAPTER" -eq 1 ]; then
		    if [ -s blast_report_annotated_otu_evidence.txt ]; then
		        if ! bash ${baseDir}/bin/filter_blast_rows_by_adapter_class.sh blast_report_annotated_otu_evidence.txt no_adapter > blast_report_annotated_otu_noadapter.txt; then
		        	echo "WARN: filter_blast_rows_by_adapter_class.sh failed for no_adapter split; continuing without split report" 1>&2
		        	: > blast_report_annotated_otu_noadapter.txt
		        fi
		    else
		        : > blast_report_annotated_otu_noadapter.txt
		    fi
	else
	    : > blast_report_annotated_otu_noadapter.txt
	fi
			# -- §5: SUP/HAC selection and re-basecalling path --
			_t_sup_selection_start=\$(date +%s)
	    _t_read_pident_build_start=\$(now_ms)
	    : > ${barcode}_read_pident.tsv
    if [ -s "${barcode}_blastreport_round.txt" ]; then
        awk -F'[;,]' 'NF>=5{
            q=\$1; pid=\$5+0;
            id=q; if(index(id,"|")>0){split(id,a,"|"); id=a[1];}
            if(id!="" && (!(id in best) || pid>best[id])) best[id]=pid;
        } END{ for(id in best) print id "\t" best[id]; }' ${barcode}_blastreport_round.txt > ${barcode}_read_pident.tsv
    fi
	    _t_read_pident_build_end=\$(now_ms)
	    append_sup_path_timing "read_pident_build" "\$_t_read_pident_build_start" "\$_t_read_pident_build_end"

	    _t_blocked_otu_build_start=\$(now_ms)
	    CONSOLIDATED_OTU="${barcode}_consolidated_otu.tsv"
    : > "\$CONSOLIDATED_OTU"
    if [ -s ${ongoingStateDir}/Consensus/consensus_otu_map.tsv ]; then
        awk 'BEGIN{FS=OFS="\t"} NR==1{next}
            {
                otu_key=\$2; sample=\$3; cons=\$7+0;
                if (otu_key=="" || sample=="") next;
                key=sample "\t" otu_key;
                seen[key]=1;
                if (cons==0) any_non[key]=1;
            }
            END{
                for (k in seen) if (!any_non[k]) print k;
            }' ${ongoingStateDir}/Consensus/consensus_otu_map.tsv > "\$CONSOLIDATED_OTU"
    fi
    FROZEN_READS="${barcode}_frozen_read_ids.list"
    : > "\$FROZEN_READS"
    if [ -s ${ongoingStateDir}/_state/otu_frozen_members.tsv ]; then
        cut -f2 ${ongoingStateDir}/_state/otu_frozen_members.tsv | LC_ALL=C sort -u > "\$FROZEN_READS"
    fi
    BLOCKED_OTU="${barcode}_sup_blocked_otu.tsv"
    : > "\$BLOCKED_OTU"
    if [ -s "\$FROZEN_READS" ] && [ -s "\$CONSOLIDATED_OTU" ] && [ -s blast_report_annotated_otu.txt ]; then
        perl ${baseDir}/bin/build_blocked_otu.pl "\$CONSOLIDATED_OTU" "\$FROZEN_READS" blast_report_annotated_otu.txt > "\$BLOCKED_OTU"
	    fi
	    _t_blocked_otu_build_end=\$(now_ms)
	    append_sup_path_timing "blocked_otu_build" "\$_t_blocked_otu_build_start" "\$_t_blocked_otu_build_end"

		    _t_select_reads2sup_start=\$(now_ms)
			    if [ -s "blast_report_annotated_otu.txt" ]; then
			"${baseDir}/bin/select_reads2sup.pl" blast_report_annotated_otu.txt 50 keep_no_adapter ${barcode}_read_pident.tsv "\$BLOCKED_OTU" > tmp || :
		    _t_select_reads2sup_end=\$(now_ms)
		    append_sup_path_timing "select_reads2sup" "\$_t_select_reads2sup_start" "\$_t_select_reads2sup_end"
		    _t_taxonomy_cleanup_emit_start=\$(now_ms)
		        if [ -s tmp ]; then
	            sed -E 's/[Kpcofgs]__//g' tmp > blast_report_annotated_otu.txt || mv tmp blast_report_annotated_otu.txt
	            sed -E 's/[Kpcofgs]__//g' blast_report_annotated_otu.txt \
	                | tr ';' '\t' \
	                | perl -pe 's/\blineage\b/kingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies/g' \
	                > blast_report_annotated.txt
	        else
	            : > blast_report_annotated_otu.txt
	            : > blast_report_annotated.txt
	        fi
		    else
		        : > tmp
		        : > blast_report_annotated_otu.txt
		        : > blast_report_annotated.txt
		    _t_select_reads2sup_end=\$(now_ms)
		    append_sup_path_timing "select_reads2sup" "\$_t_select_reads2sup_start" "\$_t_select_reads2sup_end"
		    _t_taxonomy_cleanup_emit_start=\$(now_ms)
		    fi

	if [ "\$SEPARATE_NO_ADAPTER" -eq 1 ]; then
	    if [ -s blast_report_annotated_otu_noadapter.txt ]; then
	        sed -E 's/[Kpcofgs]__//g' blast_report_annotated_otu_noadapter.txt > blast_report_annotated_otu_noadapter.tmp || mv blast_report_annotated_otu_noadapter.txt blast_report_annotated_otu_noadapter.tmp
	        mv blast_report_annotated_otu_noadapter.tmp blast_report_annotated_otu_noadapter.txt
            sed -E 's/[Kpcofgs]__//g' blast_report_annotated_otu_noadapter.txt \
                | tr ';' '\t' \
                | perl -pe 's/\blineage\b/kingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies/g' \
                > blast_report_annotated_noadapter.txt
	    else
	        : > blast_report_annotated_noadapter.txt
	    fi
	    _t_taxonomy_cleanup_emit_end=\$(now_ms)
	    append_sup_path_timing "taxonomy_cleanup_emit" "\$_t_taxonomy_cleanup_emit_start" "\$_t_taxonomy_cleanup_emit_end"
    else
        : > blast_report_annotated_noadapter.txt
        _t_taxonomy_cleanup_emit_end=\$(now_ms)
        append_sup_path_timing "taxonomy_cleanup_emit" "\$_t_taxonomy_cleanup_emit_start" "\$_t_taxonomy_cleanup_emit_end"
    fi

	    rm -f blast_report_annotated_otu_noadapter.tmp
	    hac2sup_candidate_rows=0
		    hac2sup_candidate_unique_read_ids=0
		    sup_annotation_input_rows=0
		    dorado_sup_sam_records=0
		    dorado_sup_fastq_reads=0
		    hac2sup_sup_fasta_reads=0
		    sup_cache_hit_ids=0
		    sup_cache_miss_ids=0
		    sup_cache_restored_fastq_reads=0
		    sup_cache_restored_summary_rows=0
		    dorado_sup_reads_requested=0
		    dorado_sup_sam_records_new=0
		    dorado_sup_fastq_reads_new=0
		    dorado_sup_summary_rows_new=0
		    sup_pre_fastq_reads_merged=0
		    sup_summary_rows_merged=0
		    sup_cache_restore_missing_fastq_ids=0
		    sup_cache_restore_missing_summary_ids=0
		    shared_extract_union_ids=0
		    shared_extract_hac2sup_ids=0
		    shared_extract_hac_fixed_ids=0
		    shared_extract_fasta_reads=0
		    barcode="${barcode}"
		    SUP_TASK_CPUS="${task.cpus}"
		    SUP_FASTA_HQ_QCED="${fasta_hq_qced}"
		    SUP_BASEDIR="${baseDir}"
		    SUP_DORADO_BIN="${doradoBin}"
		    SUP_CACHE_LOCK="\${STATE_DIR}/.sup_basecall_cache.lock"
		    SUP_CACHE_SCHEMA_VERSION="2"
		    SUP_CACHE_RESTART_TOKEN="${restartTokenForCache ?: workflow.runName}"
		    SUP_CACHE_DORADO_MODEL="${doradoSupModel}"
		    SUP_CACHE_DORADO_ARGS="${doradoSupBasecallerArgs}"
		    SUP_CACHE_MIN_QSCORE="${params.hq_quality_score}"
		    SUP_CACHE_SKIP_PERSIST=0
		    source "${baseDir}/bin/blast_sup_path.sh"
		    sup_candidate_extract
		    sup_cache_lookup
				
				_t_hac_fixed_extract_start=\$(now_ms)
					cut -f1 blast_report_annotated.txt | grep hac_fixed | cut -f1 -d"|" > hac_fixed_readids.list || true
					if [ -s hac_fixed_readids.list ]; then
						echo "hac_fixed is being extracted" 1>&2
					fi
		
			: > ${barcode}_qced_reads_hq_hac_fixed.fasta
			_t_hac_fixed_extract_end=\$(now_ms)
			append_sup_path_timing "hac_fixed_extract" "\$_t_hac_fixed_extract_start" "\$_t_hac_fixed_extract_end"
			sup_shared_candidate_extract
			_t_sup_selection_end=\$(date +%s)
		append_process_timing "sup_selection" "\$_t_sup_selection_start" "\$_t_sup_selection_end"
			
			_t_dorado_sup_start=\$(date +%s)
			: > ${barcode}_blastreport_sup.sam
			: > ${barcode}_blastreport_sup_new.fastq
			sup_summary_write_header ${barcode}_round_sup_new.tsv
			sup_summary_write_header ${barcode}_round_sup.tsv
				if [ -s ${barcode}_blastreport_hac.list ];
				then
				pod5_reads=""
				if command -v pod5 >/dev/null 2>&1; then
					pod5_reads=\$(pod5 inspect summary "${read_file}" 2>/dev/null | perl -ne 'if(/(\\d+)\\s+reads\\b/){print \$1; exit}' || true)
				fi
				if [ -n "\$pod5_reads" ] && [[ "\$pod5_reads" != *[!0-9]* ]] && [ "\$pod5_reads" -eq 0 ]; then
					echo "ERROR: POD5 contains 0 reads; failing this run" 1>&2
					exit 1
					else
						if [ -s ${barcode}_blastreport_hac_missing.list ]; then
							_t_dorado_sup_basecaller_start=\$(now_ms)
							if dorado_basecall_retry "SUP basecalling" "${barcode}_blastreport_sup.sam" \
							${baseDir}/bin/with_dorado_lock.sh "\$DORADO_LOCK" "\$DORADO_LOCK_WAIT" "blast_OTU_pretax:\$round_barcode:sup" -- \
							${doradoBin} basecaller -x ${params.dorado_device} \
							${doradoSupBasecallerArgs} \
							--min-qscore ${params.hq_quality_score} -l ${barcode}_blastreport_hac_missing.list \
							${doradoSupModel} ${read_file};
							then
								_t_dorado_sup_basecaller_end=\$(now_ms)
								append_sup_path_timing "dorado_sup_basecaller" "\$_t_dorado_sup_basecaller_start" "\$_t_dorado_sup_basecaller_end"
								echo "basecalling sup reads" 1>&2
							else
								exit 1
							fi
						
				#	recovers new sup reads 
							_t_sup_fastq_recover_start=\$(now_ms)
							samtools fastq -@ ${task.cpus} ${barcode}_blastreport_sup.sam > ${barcode}_blastreport_sup_new.fastq
							_t_sup_fastq_recover_end=\$(now_ms)
							append_sup_path_timing "sup_fastq_recover" "\$_t_sup_fastq_recover_start" "\$_t_sup_fastq_recover_end"
							dorado_sup_sam_records_new=\$(awk 'BEGIN{c=0} !/^@/{c++} END{print c+0}' ${barcode}_blastreport_sup.sam 2>/dev/null || echo 0)
							dorado_sup_fastq_reads_new=\$(awk 'END{print int(NR/4)}' ${barcode}_blastreport_sup_new.fastq 2>/dev/null || echo 0)
							dorado_sup_sam_records="\$dorado_sup_sam_records_new"
							sup_build_new_summary
						else
							echo "INFO: all HAC->SUP candidates satisfied from SUP cache; skipping Dorado" 1>&2
							append_sup_path_timing "dorado_sup_basecaller" "\$(now_ms)" "\$(now_ms)"
							append_sup_path_timing "sup_fastq_recover" "\$(now_ms)" "\$(now_ms)"
						fi
						sup_merge_outputs
						sup_cache_persist
						sup_post_dorado
				fi
		#	copies el fastq of sup classified to temp for being processed in the next round
			
				mkdir -p ${ongoingStateDir}/${round_barcode}
			cp ${barcode}_blastreport_sup_annotated_pre.fastq ${ongoingStateDir}/${round_barcode}/blastreport_sup_annotated_pre.fastq
			if acquire_lock "\${SUPFASTQ_LOCK}"; then
				SUP_DST="\${STATE_DIR}/blastreport_sup_annotated_pre.fastq"
				rm -f "\$SUP_DST" "\${SUP_DST}.gz"
				cp -f ${barcode}_blastreport_sup_annotated_pre.fastq.gz "\${SUP_DST}.gz" 2>/dev/null || true
				release_lock "\${SUPFASTQ_LOCK}"
			else
				exit 1
			fi
			else
				echo "No HAC->SUP reads detected; skipping SUP basecalling" 1>&2
				: > ${barcode}_blastreport_sup.sam
				: > ${barcode}_blastreport_sup_pre.fastq
				sup_summary_write_header ${barcode}_round_sup.tsv
				sup_emit_zero_timing "sup_fastq_merge"
				sup_emit_zero_timing "sup_summary_merge"
				sup_emit_zero_timing "sup_cache_persist"
			fi
		sup_write_stats
		_t_dorado_sup_end=\$(date +%s)
		append_process_timing "dorado_sup" "\$_t_dorado_sup_start" "\$_t_dorado_sup_end"
	
	#	gets the blast results only for sup
		grep -F "|sup|" ${barcode}_blastreport_join.txt > ${barcode}_preblastreport_sup.txt || :
	if [ -s ${barcode}_preblastreport_sup.txt ]; then
		echo "${barcode}_preblastreport_sup.txt obtained" 1>&2
	fi
	# All BLAST-hit reads (any model) for rolling pool retention
	BLAST_HIT_REPORT="${barcode}_blastreport_join.txt"
	_t_rolling_pool_update_start=\$(date +%s)
	: > ${barcode}_tmp_focus_sup.fasta
	: > ${barcode}_tmp_focus_hit.fasta
	BLAST_HIT_EXTRACT_FAILED=0
	if [ -s ${barcode}_preblastreport_sup.txt ]; then
		perl ${baseDir}/bin/focus_hq_tax_fasta.pl ${barcode}_preblastreport_sup.txt ${fasta_hq_qced} > ${barcode}_tmp_focus_sup.fasta || : > ${barcode}_tmp_focus_sup.fasta
	fi
	if [ -s "\$BLAST_HIT_REPORT" ]; then
		if ! perl ${baseDir}/bin/focus_hq_tax_fasta.pl "\$BLAST_HIT_REPORT" ${fasta_hq_qced} > ${barcode}_tmp_focus_hit.fasta; then
			BLAST_HIT_EXTRACT_FAILED=1
			: > ${barcode}_tmp_focus_hit.fasta
		fi
	fi
	
#	gets the set of fasta sequences for the next OTUs (first hac and then only sup)
			if acquire_lock "\${QCED_LOCK}"; then
					if [ -f "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" ];
					then
							if ! perl ${baseDir}/bin/filter_sup_non_no_adapter_fasta.pl "\$PROTECTED_READ_IDS_EVER_STATE" "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" > tmp; then
								echo "WARN: filter_sup_non_no_adapter_fasta.pl failed; retaining existing rolling pool prior to append" 1>&2
								: > tmp
							fi
				# Only rewrite the rolling FASTA when we have `|sup|` entries; otherwise keep existing contents.
				if [ -s tmp ]; then
					cat tmp > "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				fi

				# Always append HAC-derived sequences (hac_fixed + hac2sup) so consensus can extract them.
				: >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				cat ${barcode}_qced_reads_hq_hac_fixed.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				if [ -s ${barcode}_qced_reads_hq_hac2sup_sup.fasta ]; then
					cat ${barcode}_qced_reads_hq_hac2sup_sup.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				else
					cat ${barcode}_qced_reads_hq_hac2sup.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				fi

				if [ -s ${barcode}_tmp_focus_sup.fasta ]; then
					seqkit grep -r -p "\\|sup\\|" ${barcode}_tmp_focus_sup.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" || :
				fi
				# Ensure all BLAST-hit reads are retained in the rolling pool
				if [ -s ${barcode}_tmp_focus_hit.fasta ]; then
					cat ${barcode}_tmp_focus_hit.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				fi
			else
				if [ "\$BLAST_HIT_EXTRACT_FAILED" -ne 0 ] && [ -s "\$BLAST_HIT_REPORT" ]; then
					echo "ERROR: failed to initialize rolling pool from BLAST-hit FASTA extraction" 1>&2
					exit 1
				fi
				cat ${barcode}_tmp_focus_hit.fasta > "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				# Also include HAC-derived sequences on first creation.
				cat ${barcode}_qced_reads_hq_hac_fixed.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				if [ -s ${barcode}_qced_reads_hq_hac2sup_sup.fasta ]; then
					cat ${barcode}_qced_reads_hq_hac2sup_sup.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				else
					cat ${barcode}_qced_reads_hq_hac2sup.fasta >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
				fi
			fi
			# Preserve sticky protected reads in the rolling pool even when they are absent
			# from the current round's BLAST-hit/HAC-focused append set.
			protected_pool_added=0
			if [ -s "\$PROTECTED_READ_IDS_EVER_STATE" ] && [ -s ${barcode}_qced_reads_hq_accumulated.fasta ]; then
				PROTECTED_POOL_FASTA="\${STATE_DIR}/${barcode}_rolling_pool_protected.fasta"
				awk 'NR==FNR{ids[\$1]=1; next} /^>/{uuid=substr(\$0,2); sub(/[|].*/,"",uuid); keep=(uuid in ids); if(keep)print; next} keep{print}' \
					"\$PROTECTED_READ_IDS_EVER_STATE" \
					${barcode}_qced_reads_hq_accumulated.fasta > "\$PROTECTED_POOL_FASTA" || : > "\$PROTECTED_POOL_FASTA"
				if [ -s "\$PROTECTED_POOL_FASTA" ]; then
					cat "\$PROTECTED_POOL_FASTA" >> "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
					protected_pool_added=\$(grep -c '^>' "\$PROTECTED_POOL_FASTA" || echo 0)
				fi
				rm -f "\$PROTECTED_POOL_FASTA"
			fi
			# Rolling pool stats (pre-dedup)
			ROUND_DIR="${ongoingStateDir}/${round_barcode}"
			ROLLING_POOL_STATS="\$ROUND_DIR/${barcode}_rolling_pool_stats.tsv"
			: > "\$ROLLING_POOL_STATS"
			if [ -f "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" ]; then
				pool_before=\$(grep -c '^>' "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" || echo 0)
			else
				pool_before=0
			fi
			hit_appended=0
			if [ -s "\$BLAST_HIT_REPORT" ]; then
				hit_appended=\$(awk -F'[;,]' 'NF>0 && \$1!=""{print \$1}' "\$BLAST_HIT_REPORT" | LC_ALL=C sort -u | wc -l | tr -d ' ')
			fi
			printf 'hit_ids_appended\t%s\nprotected_ids_reappended\t%s\npool_before_dedup\t%s\n' "\$hit_appended" "\$protected_pool_added" "\$pool_before" >> "\$ROLLING_POOL_STATS"
			# Deduplicate rolling FASTA by read_id, keeping best model (sup > hac > fast).
			DEDUP_TMP="\${STATE_DIR}/qced_reads_hq_accumulated.dedup.tmp"
			awk 'BEGIN{FS="|"}
				/^>/{
					header=\$0;
					id=substr(header,2);
					model="";
					if (index(id,"|")>0) { split(id,a,"|"); id=a[1]; model=a[3]; } else { model=""; }
					if (model=="hac2sup" || model=="hac_fixed") model="hac";
					rank=(model=="sup"?3:(model=="hac"?2:1));
					if (!(id in best) || rank>best[id]) {
						best[id]=rank; hdr[id]=header; seq[id]=""; keep=1;
					} else {
						keep=0;
					}
					cur=id;
					next
				}
				{
					if (keep) { seq[cur]=seq[cur] \$0 ORS; }
				}
				END{
					for (id in hdr) {
						print hdr[id];
						printf "%s", seq[id];
					}
				}' "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" > "\$DEDUP_TMP" \
					&& seqkit sort -n "\$DEDUP_TMP" > "\${DEDUP_TMP}.sorted" \
					&& mv "\${DEDUP_TMP}.sorted" "\${STATE_DIR}/qced_reads_hq_accumulated.fasta"
			# O1: rebuild .fai index after dedup rewrite so consensus uses indexed random-access extraction.
			# Delete first: a failed rebuild must leave no stale index (wrong byte offsets).
			rm -f "\${STATE_DIR}/qced_reads_hq_accumulated.fasta.fai"
			command -v samtools >/dev/null 2>&1 && samtools faidx "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" 2>/dev/null || true
			seqkit faidx "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" 2>/dev/null || true
			pool_after=\$(grep -c '^>' "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" || echo 0)
			printf 'pool_after_dedup\t%s\n' "\$pool_after" >> "\$ROLLING_POOL_STATS"
			_t_rolling_pool_update_end=\$(date +%s)
			append_process_timing "rolling_pool_update" "\$_t_rolling_pool_update_start" "\$_t_rolling_pool_update_end"
			# -- §6: Unified prune application (C1, size-streak, blast-unassigned) --
			_t_prune_apply_start=\$(date +%s)
					# Build one canonical per-round prune list and apply it once to the rolling pool.
					# Sources: C1/frozen-consolidated, size-streak, consensus-dropped.
					PRUNE_CUMULATIVE_POOL_ALL="${pruneCumulativePoolAll ? '1' : '0'}"
					PRUNE_UNASSIGNED_DROP_READS="${(pruneUnassignedClusters && pruneUnassignedDropReads) ? 1 : 0}"
					OTU_PRUNE_POLICY="${otuPruneFrozenPolicyCanonical}"
					OTU_PRUNE_POLICY_EFFECTIVE="\$OTU_PRUNE_POLICY"
					FORCE_PRUNE_MAX_MB="${otuLockForcePruneMaxFastaMbStr}"
					FORCE_PRUNE_OVERRIDE="${otuForcePruneOverride ? '1' : '0'}"
					ROUND_PRUNE_IDS="\$ROUND_DIR/${barcode}_round_prune_ids.list"
					ROUND_PRUNE_STATS="\$ROUND_DIR/${barcode}_round_prune_stats.tsv"
					ROUND_PRUNE_APPLY_STATS="\$ROUND_DIR/${barcode}_round_prune_apply.tsv"
					C1_PRUNE_IDS="\$ROUND_DIR/${barcode}_c1_prune_ids.list"
					SIZE_STREAK_PRUNE_IDS="\$ROUND_DIR/${barcode}_size_streak_prune_ids.list"
					CONSENSUS_UNASSIGNED_PRUNE_IDS="\$ROUND_DIR/${barcode}_consensus_unassigned_prune_ids.list"
					ROUND_PRUNE_TMP="\${STATE_DIR}/qced_reads_hq_accumulated.round_pruned.tmp"
					mkdir -p "\$ROUND_DIR"
					: > "\$C1_PRUNE_IDS"
					: > "\$SIZE_STREAK_PRUNE_IDS"
					: > "\$CONSENSUS_UNASSIGNED_PRUNE_IDS"
					: > "\$ROUND_PRUNE_IDS"
					: > "\$ROUND_PRUNE_STATS"
					: > "\$ROUND_PRUNE_APPLY_STATS"

					if awk -v t="\$FORCE_PRUNE_MAX_MB" 'BEGIN{exit !(t>0)}'; then
						if [ -f "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" ]; then
								_fbytes=\$(wc -c < "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" | tr -d ' ')
								_fmb=\$(awk -v b="\$_fbytes" 'BEGIN{printf "%.3f", b/1048576.0}')
							if awk -v mb="\$_fmb" -v thr="\$FORCE_PRUNE_MAX_MB" 'BEGIN{exit !(mb>thr)}'; then
								if [ "\$OTU_PRUNE_POLICY" = "never" ] && [ "\$FORCE_PRUNE_OVERRIDE" != "1" ]; then
									echo "WARN: qced_reads_hq_accumulated.fasta size=\${_fmb}MB exceeds threshold=\${FORCE_PRUNE_MAX_MB}MB but archive policy is never and otu_force_prune_override=false; skipping forced archive (C1)" 1>&2
								elif [ -s "\${STATE_DIR}/otu_consolidated_keys.tsv" ]; then
									OTU_PRUNE_POLICY_EFFECTIVE="until_consolidated"
									echo "WARN: qced_reads_hq_accumulated.fasta size=\${_fmb}MB exceeds threshold=\${FORCE_PRUNE_MAX_MB}MB; forcing consolidated-key archive (C1) for this round" 1>&2
								else
									echo "WARN: qced_reads_hq_accumulated.fasta size=\${_fmb}MB exceeds threshold=\${FORCE_PRUNE_MAX_MB}MB but no consolidated keys available; skipping forced archive (C1)" 1>&2
								fi
							fi
						fi
					fi

					if [ "\$OTU_PRUNE_POLICY_EFFECTIVE" = "always" ]; then
						if [ -s "\${STATE_DIR}/otu_frozen_members.tsv" ]; then
							cut -f2 "\${STATE_DIR}/otu_frozen_members.tsv" \
								| awk '{split(\$0,a,"|"); if (a[1]!="") print a[1]}' \
								| LC_ALL=C sort -u > "\$C1_PRUNE_IDS"
						fi
					elif [ "\$OTU_PRUNE_POLICY_EFFECTIVE" = "until_consolidated" ]; then
						if [ -s "\${STATE_DIR}/otu_consolidated_keys.tsv" ]; then
							C1_SAMPLES_FILE="${otuPruneSamplesFileValue}"
							if [ -z "\$C1_SAMPLES_FILE" ]; then
								C1_SAMPLES_FILE="${sampleInfoDir}/samples.txt"
							fi
							if [ ! -f "\$C1_SAMPLES_FILE" ]; then
								C1_SAMPLES_FILE=""
							fi
							${baseDir}/bin/otu_c1_prune_ids.sh \
								"until_consolidated" \
								"\${STATE_DIR}/qced_reads_hq_accumulated.fasta" \
								"\${DEDUP_TMP}.active_ids" \
								"" \
								"\${STATE_DIR}/otu_consolidated_keys.tsv" \
								"\$C1_SAMPLES_FILE" \
								"${otuConsolidatedKeysMixedPolicyCanonical}" \
								"\$OBSERVED_NO_ADAPTER"
							awk '/^>/{id=substr(\$0,2); sub(/ .*/, "", id); split(id,a,"|"); if (a[1]!="") print a[1]}' "\${STATE_DIR}/qced_reads_hq_accumulated.fasta" | LC_ALL=C sort -u > "\${DEDUP_TMP}.all_ids"
							awk '{split(\$0,a,"|"); if (a[1]!="") print a[1]}' "\${DEDUP_TMP}.active_ids" | LC_ALL=C sort -u > "\${DEDUP_TMP}.active_base_ids"
							comm -23 "\${DEDUP_TMP}.all_ids" "\${DEDUP_TMP}.active_base_ids" > "\$C1_PRUNE_IDS" || : > "\$C1_PRUNE_IDS"
							rm -f "\${DEDUP_TMP}.active_ids" "\${DEDUP_TMP}.all_ids" "\${DEDUP_TMP}.active_base_ids"
						fi
					fi

					# Small-OTU pruning uses round-local OTU membership/size (independent of BLAST decision path).
					SMALL_OTU_PRUNE_ENABLED=0
					SMALL_OTU_PRUNE_REASON="unknown"
					OTU_BLAST_FILTER_MODE="${otuBlastFilterModeCanonical}"
					OTU_BLAST_FILTER_SKIP_ROUNDS="${otuBlastFilterSkipRoundsCanonical}"
					OTU_BLAST_FORCE_USE_FILTERED="${otuBlastForceUseFiltered ? '1' : '0'}"
					OTU_BLAST_MIN_MEMBERS="${otuBlastMinMembersStr}"
					ROUND_INDEX_FILE="\${STATE_DIR}/round_index.tsv"
					ROUND_INDEX=\$(awk -F'\t' -v rb="\$round_barcode" '\$1==rb{print \$2; exit}' "\$ROUND_INDEX_FILE" 2>/dev/null || true)
					if [ -z "\$ROUND_INDEX" ] || [[ "\$ROUND_INDEX" == *[!0-9]* ]] || [ "\$ROUND_INDEX" -lt 1 ]; then
						SMALL_OTU_PRUNE_REASON="missing_round_index"
						echo "WARN: missing/invalid round index for small-OTU prune (round_barcode=\$round_barcode, file=\$ROUND_INDEX_FILE); skipping size-based pruning this round" 1>&2
					elif [ -z "\$OTU_BLAST_MIN_MEMBERS" ] || [[ "\$OTU_BLAST_MIN_MEMBERS" == *[!0-9]* ]] || [ "\$OTU_BLAST_MIN_MEMBERS" -lt 2 ]; then
						SMALL_OTU_PRUNE_REASON="min_members_lt2"
					else
						OTU_BLAST_EFFECTIVE_MODE_TSV="${barcode}_blast_filter_effective_mode_prune.tsv"
						${baseDir}/bin/otu_blast_effective_mode.sh \
							"\$OTU_BLAST_FILTER_MODE" \
							"\$OTU_BLAST_FILTER_SKIP_ROUNDS" \
							"\$ROUND_INDEX" \
							> "\$OTU_BLAST_EFFECTIVE_MODE_TSV"
						effective_mode_value() {
							local key="\$1"
							awk -F'\t' -v k="\$key" '\$1==k{print \$2; exit}' "\$OTU_BLAST_EFFECTIVE_MODE_TSV"
						}
							OTU_BLAST_EFFECTIVE_MODE=\$(effective_mode_value effective_mode)
							OTU_BLAST_EFFECTIVE_REASON=\$(effective_mode_value reason)
							if [ "\$OTU_BLAST_FORCE_USE_FILTERED" = "1" ] && [ "\$OTU_BLAST_EFFECTIVE_MODE" != "off" ]; then
								echo "INFO: size_streak_prune force_use_filtered=1 bypassing threshold and no-cluster decision knobs" 1>&2
								OTU_BLAST_EFFECTIVE_MODE="enforce"
								OTU_BLAST_EFFECTIVE_REASON="force_use_filtered"
							fi
						if [ "\$OTU_BLAST_EFFECTIVE_MODE" = "enforce" ]; then
							SMALL_OTU_PRUNE_ENABLED=1
							SMALL_OTU_PRUNE_REASON="effective_mode_enforce"
						else
							SMALL_OTU_PRUNE_REASON="effective_mode_\${OTU_BLAST_EFFECTIVE_MODE}"
						fi
					fi
					if [ "\$SMALL_OTU_PRUNE_ENABLED" = "1" ]; then
						echo "INFO: size_streak_prune enabled=1 reason=\$SMALL_OTU_PRUNE_REASON round_index=\${ROUND_INDEX:-NA} min_members=\${OTU_BLAST_MIN_MEMBERS} skip_rounds=\${OTU_BLAST_FILTER_SKIP_ROUNDS} effective_mode=\${OTU_BLAST_EFFECTIVE_MODE:-NA}" 1>&2
					else
						echo "INFO: size_streak_prune enabled=0 reason=\$SMALL_OTU_PRUNE_REASON round_index=\${ROUND_INDEX:-NA} min_members=\${OTU_BLAST_MIN_MEMBERS} skip_rounds=\${OTU_BLAST_FILTER_SKIP_ROUNDS} effective_mode=\${OTU_BLAST_EFFECTIVE_MODE:-NA}" 1>&2
					fi
					APPLY_SIZE_STREAK=0
					if [ "\$OTU_SIZE_STREAK_MODE" = "enforce" ] || [ "\$SMALL_OTU_PRUNE_ENABLED" = "1" ]; then
						APPLY_SIZE_STREAK=1
					fi
						if [ "\$APPLY_SIZE_STREAK" = "1" ] && [ -s "\${STATE_DIR}/${barcode}_otu_size_streak_prune_ids_last.txt" ]; then
							awk '{split(\$0,a,\"|\"); if (a[1] != \"\") print a[1]}' "\${STATE_DIR}/${barcode}_otu_size_streak_prune_ids_last.txt" | LC_ALL=C sort -u > "\$SIZE_STREAK_PRUNE_IDS"
						fi
						# Protect ever-assigned reads plus current members of persisted protected OTU keys.
						PROTECTED_READ_IDS_EVER="\${STATE_DIR}/${barcode}_protected_read_ids_ever.list"
						PROTECTED_READ_IDS_ROUND="\${ROUND_DIR}/${barcode}_assigned_otu_member_ids_ever.list"
						PROTECTED_IDS="\${ROUND_DIR}/${barcode}_protected_prune_ids.list"
										# Compute blast-unassigned read IDs (reads whose OTU never received a BLAST assignment).
					BLAST_UNASSIGNED_IDS="${barcode}_blast_unassigned_reads_round.list"
					: > "\$BLAST_UNASSIGNED_IDS"
					BLAST_UNASSIGNED_STATUS="mode_off"
					if [ "${otuBlastUnassignedModeCanonical}" != "off" ]; then
						if [ "\${ROUND_INDEX:-0}" -le "${params.otu_blast_unassigned_grace_rounds}" ] 2>/dev/null; then
							BLAST_UNASSIGNED_STATUS="grace"
						else
							if [ -s blast_report_annotated_otu_evidence.txt ]; then
								if ! perl ${baseDir}/bin/blast_unassigned_read_ids.pl \
									blast_report_annotated_otu_evidence.txt \
									"\$BLAST_UNASSIGNED_IDS" \
									--min-level "${assignProtLevelCanonical}"; then
									echo "WARN: blast_unassigned_read_ids.pl failed; continuing without blast-unassigned prune" 1>&2
									: > "\$BLAST_UNASSIGNED_IDS"
								fi
							fi
							if [ "${otuBlastUnassignedModeCanonical}" = "observe" ]; then
								BLAST_UNASSIGNED_STATUS="observe"
								: > "\$BLAST_UNASSIGNED_IDS"
							else
								BLAST_UNASSIGNED_STATUS="applied"
							fi
						fi
					fi
					cp "\$BLAST_UNASSIGNED_IDS" "\$ROUND_DIR/${barcode}_blast_unassigned_reads_round.list" 2>/dev/null || true
				cp blast_report_annotated_otu_evidence.txt "\$ROUND_DIR/${barcode}_blast_report_annotated_otu_evidence.txt" 2>/dev/null || true
					# Intentional sticky semantics: _last.list retains the most recent consensus
					# unassigned determination across rounds. When consensus emits no new list,
					# the previous one is re-used, keeping those reads excluded until overridden.
					if [ "\$PRUNE_UNASSIGNED_DROP_READS" = "1" ] && [ -s "\${STATE_DIR}/${barcode}_pruned_unassigned_reads_last.list" ]; then
							awk '{split(\$0,a,"|"); if (a[1]!="") print a[1]}' "\${STATE_DIR}/${barcode}_pruned_unassigned_reads_last.list" | LC_ALL=C sort -u > "\$CONSENSUS_UNASSIGNED_PRUNE_IDS"
					fi

						if ! bash ${baseDir}/bin/prune_round_orchestrate.sh \
							--protected-read-ids-ever "\$PROTECTED_READ_IDS_EVER" \
							--protected-read-ids-round "\$PROTECTED_READ_IDS_ROUND" \
							--protected-ids "\$PROTECTED_IDS" \
							--candidate size_streak "\$SIZE_STREAK_PRUNE_IDS" \
							--candidate consensus_unassigned "\$CONSENSUS_UNASSIGNED_PRUNE_IDS" \
							--candidate blast_unassigned "\$BLAST_UNASSIGNED_IDS" \
						--c1-ids "\$C1_PRUNE_IDS" \
						--round-prune-ids "\$ROUND_PRUNE_IDS" \
						--round-prune-stats "\$ROUND_PRUNE_STATS" \
						--prune-cumulative-pool-all "\$PRUNE_CUMULATIVE_POOL_ALL" \
						--blast-unassigned-status "\$BLAST_UNASSIGNED_STATUS" \
						--merge-error-context "failed to merge round prune ID lists"; then
							exit 1
					fi

						cp "\$ROUND_PRUNE_IDS" "\${STATE_DIR}/${barcode}_round_prune_ids_last.list" 2>/dev/null || true
						cp "\$ROUND_PRUNE_STATS" "\${STATE_DIR}/${barcode}_round_prune_stats_last.tsv" 2>/dev/null || true
						_t_prune_apply_end=\$(date +%s)
						append_process_timing "prune_apply" "\$_t_prune_apply_start" "\$_t_prune_apply_end"
						release_lock "\${QCED_LOCK}"
			else
				exit 1
		fi
		rm -f ${barcode}_tmp_focus_sup.fasta ${barcode}_tmp_focus_hit.fasta
	
	
	
		# -- §7: Report dedup and state finalization --
		# Deduplicate canonical annotated reports by read_id keeping best model (sup > hac > fast).
			for f in blast_report_annotated.txt blast_report_annotated_otu.txt blast_report_annotated_noadapter.txt; do
				if [ -s "\$f" ]; then
					awk 'BEGIN{FS=OFS="\t"}
					{
						split(\$1,a,"|"); id=a[1]; model=a[3];
						if (model=="hac2sup" || model=="hac_fixed") model="hac";
						r=(model=="sup"?3:(model=="hac"?2:1));
						if (!(id in br) || r>br[id]) { br[id]=r; line[id]=\$0; }
					}
						END{ for(id in line) print line[id]; }' "\$f" \
							| LC_ALL=C sort -k1,1 > "\${f}.tmp" && mv "\${f}.tmp" "\$f"
					fi
				done

			if [ -f "\${STATE_DIR}/blastreport.txt" ]; then
				if ! "${baseDir}/bin/prefer_blast_rows_by_model.sh" \
					--input blast_report_annotated.txt \
					--output blast_report_annotated_preferred.txt \
					--policy sup_hac2sup_preferred; then
					echo "ERROR: failed to build preferred BLAST annotated report" 1>&2
					exit 1
				fi
			else
				cp blast_report_annotated.txt blast_report_annotated_preferred.txt 2>/dev/null || : > blast_report_annotated_preferred.txt
			fi

		# Diagnostic OTU membership export from evidence-tier BLAST OTU report.
		if [ -s blast_report_annotated_otu_evidence.txt ]; then
		if ! perl "${baseDir}/bin/otu_export_members_from_blastreport.pl" \
			blast_report_annotated_otu_evidence.txt \
			"\$OTU_MEMBERS_BLASTDIAG" \
			"\$OTU_SIZES_BLASTDIAG" \
			"\$OTU_MEMBERS_BLASTDIAG_STATS"; then
			echo "WARN: failed to export BLAST-diagnostic OTU membership from blast_report_annotated_otu_evidence.txt" 1>&2
			: > "\$OTU_MEMBERS_BLASTDIAG"
			: > "\$OTU_SIZES_BLASTDIAG"
			: > "\$OTU_MEMBERS_BLASTDIAG_STATS"
		fi
	else
		: > "\$OTU_MEMBERS_BLASTDIAG"
		: > "\$OTU_SIZES_BLASTDIAG"
		: > "\$OTU_MEMBERS_BLASTDIAG_STATS"
	fi
	if [ -s "\$OTU_MEMBERS_BLASTDIAG_STATS" ]; then
		sed 's/^/INFO: otu_members_blastdiag\t/' "\$OTU_MEMBERS_BLASTDIAG_STATS" 1>&2 || true
	fi
	
		mkdir -p ${ongoingStateDir}/${round_barcode}
			cp blast_process_timings.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_blast_process_timings.tsv 2>/dev/null || true
			cp blast_process_timings.tsv "\${STATE_DIR}/${barcode}_blast_process_timings_last.tsv" 2>/dev/null || true
			cp "\$OTU_REFINE_PHASE_TIMINGS_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_refine_phase_timings.tsv 2>/dev/null || true
			cp "\$OTU_REFINE_PHASE_TIMINGS_FILE" "\${STATE_DIR}/${barcode}_otu_refine_phase_timings_last.tsv" 2>/dev/null || true
			cp "\$OTU_REFINE_PHASE_TIMINGS_MS_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_refine_phase_timings_ms.tsv 2>/dev/null || true
			cp "\$OTU_REFINE_PHASE_TIMINGS_MS_FILE" "\${STATE_DIR}/${barcode}_otu_refine_phase_timings_ms_last.tsv" 2>/dev/null || true
			cp "\$OTU_REFINE_PROCESS_BREAKDOWN_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_refine_process_breakdown.tsv 2>/dev/null || true
			cp "\$OTU_REFINE_PROCESS_BREAKDOWN_FILE" "\${STATE_DIR}/${barcode}_otu_refine_process_breakdown_last.tsv" 2>/dev/null || true
			cp "\$OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_refine_process_breakdown_ms.tsv 2>/dev/null || true
			cp "\$OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE" "\${STATE_DIR}/${barcode}_otu_refine_process_breakdown_ms_last.tsv" 2>/dev/null || true
			cp "\$OTU_REFINE_WORKLOAD_STATS_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_otu_refine_workload_stats.tsv 2>/dev/null || true
			cp "\$OTU_REFINE_WORKLOAD_STATS_FILE" "\${STATE_DIR}/${barcode}_otu_refine_workload_stats_last.tsv" 2>/dev/null || true
			cp "\$SUP_PATH_STATS_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_sup_path_stats.tsv 2>/dev/null || true
			cp "\$SUP_PATH_TIMINGS_MS_FILE" ${ongoingStateDir}/${round_barcode}/${barcode}_sup_path_timings_ms.tsv 2>/dev/null || true
			cp "\$SUP_PATH_TIMINGS_MS_FILE" "\${STATE_DIR}/${barcode}_sup_path_timings_ms_last.tsv" 2>/dev/null || true
			cp ${barcode}_blastreport_hac.list ${ongoingStateDir}/${round_barcode}/${barcode}_blastreport_hac.list 2>/dev/null || true
			cp ${barcode}_blastreport_sup.list ${ongoingStateDir}/${round_barcode}/${barcode}_blastreport_sup.list 2>/dev/null || true
			cp ${barcode}_blastreport_round.txt ${ongoingStateDir}/${round_barcode}/blastreport.txt
		if acquire_lock "\${BLASTREPORT_LOCK}"; then
			STATE_BLASTREPORT_TMP="\${STATE_DIR}/blastreport.txt.tmp.\$\$"
			cp ${barcode}_blastreport.txt "\$STATE_BLASTREPORT_TMP"
			mv "\$STATE_BLASTREPORT_TMP" "\${STATE_DIR}/blastreport.txt"
			release_lock "\${BLASTREPORT_LOCK}"
		else
			exit 1
		fi
		cp "\$BLAST_FILTER_STATS" ${ongoingStateDir}/${round_barcode}/${barcode}_blast_filter_stats.tsv 2>/dev/null || true
		cp "\$BLAST_FILTER_DROPPED_IDS" ${ongoingStateDir}/${round_barcode}/${barcode}_blast_filter_dropped_read_ids.list 2>/dev/null || true
		cp "\$BLAST_FILTER_DROPPED_IDS" "\${STATE_DIR}/${barcode}_blast_filter_dropped_read_ids_last.list" 2>/dev/null || true
		cp "\$OTU_MEMBERS_BLASTDIAG" ${ongoingStateDir}/${round_barcode}/otu_members_blastdiag.tsv 2>/dev/null || true
	cp "\$OTU_SIZES_BLASTDIAG" ${ongoingStateDir}/${round_barcode}/otu_sizes_blastdiag.tsv 2>/dev/null || true
	cp "\$OTU_MEMBERS_BLASTDIAG_STATS" ${ongoingStateDir}/${round_barcode}/otu_members_blastdiag_stats.tsv 2>/dev/null || true
		cp "\$OTU_MEMBERS_BLASTDIAG" "\${STATE_DIR}/${barcode}_otu_members_blastdiag.tsv" 2>/dev/null || true
		cp "\$OTU_SIZES_BLASTDIAG" "\${STATE_DIR}/${barcode}_otu_sizes_blastdiag.tsv" 2>/dev/null || true
		cp "\$OTU_MEMBERS_BLASTDIAG_STATS" "\${STATE_DIR}/${barcode}_otu_members_blastdiag_stats_last.tsv" 2>/dev/null || true
		"""
  }

report_blast_inputs = ChannelUtils.strictRoundJoin(report_blast, hac_read_control)

// ============================================================
// STAGE G — REPORTING (_reporting_blast_pretax)
// ============================================================
process _reporting_blast_pretax {
  maxForks maxForksReportingVal
//  publishDir "${params.outdir}/ongoing/", mode: 'copy', overwrite: true
  input:
    tuple val(barcode), val(round_barcode), file(round_sup_sam), file(round_sup_tsv), file(blast_sup_fastq), file(blast_read), file(blast_report_otu), file(blast_report_noadapter), file(blast_filter_stats) from report_blast_inputs
  output:
    tuple val(barcode), val(round_barcode), file("${barcode}_blast_otu_pretax_rpt.txt"), file("${barcode}_read_info_rpt.txt"), file("${barcode}_blast_otu_noadapter_rpt.txt"), file(blast_filter_stats) into blst_rpt_summary
    file("${barcode}_blast_otu_pretax_rpt.txt")
    file("${barcode}_blast_otu_noadapter_rpt.txt")
	
    script:

		"""
		set -euo pipefail
		shopt -s nullglob
		export LC_ALL=C
		RESTART_TOKEN="${restartTokenForCache}"
		# Define report header unconditionally (safe under `set -u`).
		BLAST_HEADER='read_id	barcode_by_homology	basecalling_model	sample	hit_id	taxid	aln_length	perc_id	otu_id	otu_taxid	otu_kingdom	otu_phylum	otu_class	otu_order	otu_family	otu_genus	otu_species'
		DORADO_SUMMARY_HEADER='input_filename\tbatch_id\tparent_read_id\tread_id\trun_id\tchannel\tmux\tminknow_events\tstart_time\tduration\tpasses_filtering\ttemplate_start\tnum_events_template\ttemplate_duration\tsequence_length_template\tmean_qscore_template\tpore_type\texperiment_id\tsample_id\tend_reason\n'
	
		# Helper: persist per-round SUP summary to rolling state
		persist_sup_tsv() {
			local tsv="\$1"
			mkdir -p ${ongoingStateDir}/_state
			if [ ! -f ${ongoingStateDir}/_state/${barcode}_sup.tsv ]; then
				cp "\$tsv" ${ongoingStateDir}/_state/${barcode}_sup.tsv
			else
				tail -n +2 "\$tsv" >> ${ongoingStateDir}/_state/${barcode}_sup.tsv || true
			fi
			mkdir -p ${ongoingStateDir}/${round_barcode}
			cp -f "\$tsv" ${ongoingStateDir}/${round_barcode}/${barcode}_sup.tsv 2>/dev/null || true
		}

		# Helper: run noadapter blast reporting when a noadapter report exists
		run_noadapter_report() {
			if [ -s ${blast_report_noadapter} ]; then
				cp ${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt ${barcode}_noadapter_read_info_rpt.txt
				if perl ${baseDir}/bin/reporting_blast_otu.pl ${barcode}_round_sup.tsv ${blast_read} ${blast_report_noadapter} ${barcode}_noadapter_read_info_rpt.txt ${barcode}_noadapter; then
					mv ${barcode}_noadapter_blast_otu_pretax_rpt.txt ${barcode}_blast_otu_noadapter_rpt.txt
				else
					printf '%s\n' "\$BLAST_HEADER" > ${barcode}_blast_otu_noadapter_rpt.txt
				fi
				rm -f ${barcode}_noadapter_read_info_rpt.txt
			else
				printf '%s\n' "\$BLAST_HEADER" > ${barcode}_blast_otu_noadapter_rpt.txt
			fi
		}

			cp ${round_sup_tsv} ${barcode}_round_sup.tsv 2>/dev/null || true
			if [ ! -s ${barcode}_round_sup.tsv ]; then
				printf "%b" "\$DORADO_SUMMARY_HEADER" > ${barcode}_round_sup.tsv
			fi
			persist_sup_tsv ${barcode}_round_sup.tsv
			_rpt_state="${ongoingStateDir}/_state/${barcode}_read_info_rpt.txt"
			_rpt_round="${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt"
			[ -f "\$_rpt_round" ] || cp "\$_rpt_state" "\$_rpt_round" 2>/dev/null || : > "\$_rpt_round"
			if ! perl ${baseDir}/bin/reporting_blast_otu.pl ${barcode}_round_sup.tsv ${blast_read} ${blast_report_otu} "\$_rpt_round" ${barcode}; then
				printf '%s\n' "\$BLAST_HEADER" > ${barcode}_blast_otu_pretax_rpt.txt
				cp -f "\$_rpt_round" ${barcode}_read_info_rpt.txt 2>/dev/null || : > ${barcode}_read_info_rpt.txt
			else
				# Persist read info for downstream qscore/consensus tracking.
			# _state/ accumulation is handled solely by append_reports.pl; do NOT cp -f
			# here or it overwrites historical data, breaking cumulative read counts.
				cp -f ${barcode}_read_info_rpt.txt "\$_rpt_round" 2>/dev/null || true
			fi
			if [ ! -f ${barcode}_blast_otu_pretax_rpt.txt ]; then
				printf '%s\n' "\$BLAST_HEADER" > ${barcode}_blast_otu_pretax_rpt.txt
			fi
			run_noadapter_report
		
		# ---- Update rolling best per-read model/quality (sup > hac > fast) ----
		READ_INFO_SRC="${ongoingStateDir}/${round_barcode}/${barcode}_read_info_rpt.txt"
		BEST_INFO="${ongoingStateDir}/_state/read_info_best.tsv"
		ROLLING_QS="${ongoingStateDir}/_state/read_qscore_rolling.tsv"
		TMP_BEST="read_info_best_round.tsv"
		if [ -f "\$READ_INFO_SRC" ]; then
			cp "\$READ_INFO_SRC" read_info_rpt.txt
			awk 'BEGIN{FS=OFS="\t"}
				NR==1{
					for(i=1;i<=NF;i++) h[\$i]=i;
					rid=(h["read_id"]?h["read_id"]:1);
					sl=h["sup_length"]; sq=h["sup_mean_qscore"];
					hl=h["hac_length"]; hq=h["hac_mean_qscore"];
					fl=h["fast_length"]; fq=h["fast_mean_qscore"];
					next
				}
				{
					id=\$rid; model=""; len=""; q="";
					if (sq && \$sq!="NA" && \$sq!="") { model="sup"; q=\$sq; len=(sl? \$sl:""); }
					else if (hq && \$hq!="NA" && \$hq!="") { model="hac"; q=\$hq; len=(hl? \$hl:""); }
					else if (fq && \$fq!="NA" && \$fq!="") { model="fast"; q=\$fq; len=(fl? \$fl:""); }
					if (model!="") print id, model, len, q;
				}' read_info_rpt.txt > "\$TMP_BEST"
			mkdir -p ${ongoingStateDir}/_state
			[ -f "\$BEST_INFO" ] || : > "\$BEST_INFO"
			[ -f "\$ROLLING_QS" ] || : > "\$ROLLING_QS"
				cat "\$BEST_INFO" "\$ROLLING_QS" "\$TMP_BEST" \
					| awk 'BEGIN{FS=OFS=\"\t\"}
						{
							if (NF>=4) { id=\$1; m=\$2; len=\$3; q=\$4+0; }
							else if (NF==3) { id=\$1; m=\$2; len=\"\"; q=\$3+0; }
							else if (NF==2) { id=\$1; m=\"fast\"; len=\"\"; q=\$2+0; }
							else { next }
						r=(m==\"sup\"?3:(m==\"hac\"?2:1));
						if (!(id in rnk) || r>rnk[id] || (r==rnk[id] && q>qs[id])) {
							rnk[id]=r; qs[id]=q; mdl[id]=m; ln[id]=len;
						}
					}
						END{ for(id in rnk) print id, mdl[id], ln[id], qs[id]; }' \
					| LC_ALL=C sort -k1,1 \
					> "\${BEST_INFO}.tmp" && mv "\${BEST_INFO}.tmp" "\$BEST_INFO"
			awk 'BEGIN{FS=OFS="\t"} \$4 ~ /^[0-9]+([.][0-9]+)?/ {print \$1, \$2, \$4}' "\$BEST_INFO" > "\$ROLLING_QS"
			rm -f "\$TMP_BEST"
		fi
		
		if cp ${barcode}_blast_otu_pretax_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_blast_otu_pretax_rpt.txt;
		then
			mkdir -p ${ongoingStateDir}/_state
			if [ ! -f ${ongoingStateDir}/_state/${barcode}_blast_otu_pretax_rpt.txt ];
			then
				cp ${barcode}_blast_otu_pretax_rpt.txt ${ongoingStateDir}/_state/${barcode}_blast_otu_pretax_rpt.txt
			else
				if [ -s ${barcode}_blast_otu_pretax_rpt.txt ];
				then
					tail -n +2 ${barcode}_blast_otu_pretax_rpt.txt >> ${ongoingStateDir}/_state/${barcode}_blast_otu_pretax_rpt.txt
				fi
			fi
		fi

		if cp ${barcode}_blast_otu_noadapter_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_blast_otu_noadapter_rpt.txt;
		then
			mkdir -p ${ongoingStateDir}/_state
			if [ ! -f ${ongoingStateDir}/_state/${barcode}_blast_otu_noadapter_rpt.txt ];
			then
				cp ${barcode}_blast_otu_noadapter_rpt.txt ${ongoingStateDir}/_state/${barcode}_blast_otu_noadapter_rpt.txt
			else
				if [ -s ${barcode}_blast_otu_noadapter_rpt.txt ];
				then
					tail -n +2 ${barcode}_blast_otu_noadapter_rpt.txt >> ${ongoingStateDir}/_state/${barcode}_blast_otu_noadapter_rpt.txt
				fi
			fi
		fi
		
		
		
	"""	
}

consensus_inputs = ChannelUtils.strictRoundJoin(fastq_qced_consensus, blast2consensus)

// ============================================================
// STAGE H — CONSENSUS GENERATION (consensus)
// ============================================================
process consensus {
	// Must be schedulable even when the next POD5 has already entered `fast_on_target_detection`
	// and is waiting on the round lock.
	cpus { params.blast_threads }
	    maxForks maxForksConsensusVal
	    label 'blast'

    input: 
    tuple val(barcode), val(round_barcode), file(fasta_hq_qced), file(blast_report), file(assigned_read_ids) from consensus_inputs

	output:
	tuple val(barcode), val(round_barcode), file("${barcode}_preblastreport_join.txt"), file("consensus_blast_report_full.txt"), file("consensus_round_provenance.tsv") into report_consensus
	tuple val(barcode), val(round_barcode), file('consensus_blast_report_full.txt') into cons_agg_ch

	script:

    if(!usingDockerProfile){
	    db_dir = "$baseDir/"
	    taxdb_dir = "$baseDir/"
	}
	else {
	    db_dir = "/tmp/"
	    taxdb_dir = "/tmp/"
	}
		// Per-target DB paths are now computed in the bash loop from params.blast_db_specs
		
		taxdb_dir = taxdb_dir + params.blast_taxdb
		// taxonkit DB path removed; TaxonKit will use its default DB (or external env config) if invoked.

    

    """
	set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"
	THREADS=${task.cpus}
		export BLASTDB=${taxdb_dir}
		STATE_DIR="${ongoingStateDir}/_state"
		ASSIGNED_READ_IDS_EVER_STATE="\${STATE_DIR}/${barcode}_assigned_read_ids_ever.list"
		ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE="\${STATE_DIR}/${barcode}_assigned_otu_member_ids_prev_round.list"
		CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE="\${STATE_DIR}/${barcode}_consensus_assigned_member_ids_prev_round.list"
		PROTECTED_READ_IDS_EVER_STATE="\${STATE_DIR}/${barcode}_protected_read_ids_ever.list"
		mkdir -p "\${STATE_DIR}"
		persist_read_ids_ever_state() {
			local round_ids="\$1"
			local ever_ids="\$2"
			local stats_out="\${3:-}"
			if [ -n "\$stats_out" ]; then
				perl "${baseDir}/bin/persist_read_ids_ever.pl" "\$round_ids" "\$ever_ids" "\$stats_out"
			else
				perl "${baseDir}/bin/persist_read_ids_ever.pl" "\$round_ids" "\$ever_ids"
			fi
		}
		filter_ids_present_in_fasta() {
			local ids_in="\$1"
			local fasta_in="\$2"
			local out_ids="\$3"
			local stats_out="\${4:-}"
			if [ -n "\$stats_out" ]; then
				perl "${baseDir}/bin/filter_ids_present_in_fasta.pl" "\$ids_in" "\$fasta_in" "\$out_ids" "\$stats_out"
			else
				perl "${baseDir}/bin/filter_ids_present_in_fasta.pl" "\$ids_in" "\$fasta_in" "\$out_ids"
			fi
		}
		replace_read_ids_state() {
			local round_ids="\$1"
			local state_ids="\$2"
			if [ -s "\$round_ids" ]; then
				LC_ALL=C sort -u "\$round_ids" > "\${state_ids}.new"
				mv "\${state_ids}.new" "\$state_ids"
			else
				: > "\$state_ids"
			fi
		}
		materialize_round_grace_ids() {
			local prev_ids="\$1"
			local round_ids="\$2"
			local out_ids="\$3"
			: > "\$out_ids"
			if [ -s "\$prev_ids" ]; then
				cat "\$prev_ids" >> "\$out_ids"
			fi
			if [ -s "\$round_ids" ]; then
				cat "\$round_ids" >> "\$out_ids"
			fi
			if [ -s "\$out_ids" ]; then
				LC_ALL=C sort -u -o "\$out_ids" "\$out_ids"
			fi
		}
		refresh_protected_read_ids_ever() {
			local out_path="\$1"
			local tmp="\${out_path}.tmp.\$\$"
			: > "\$tmp"
			for src in \
				"\$ASSIGNED_READ_IDS_EVER_STATE" \
				"\$ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE" \
				"\$CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE"; do
				if [ -s "\$src" ]; then
					cat "\$src" >> "\$tmp"
				fi
			done
			if [ -s "\$tmp" ]; then
				LC_ALL=C sort -u "\$tmp" > "\${out_path}.new"
				mv "\${out_path}.new" "\$out_path"
			else
				: > "\$out_path"
			fi
			rm -f "\$tmp"
		}
		refresh_protected_read_ids_ever "\$PROTECTED_READ_IDS_EVER_STATE"
		source "${baseDir}/bin/lib/db_sig_utils.sh"
		source "${baseDir}/bin/lib/consensus_cache_state.sh"
		source "${baseDir}/bin/lib/consensus_prelaunch_gate.sh"
		timing_now_ms() {
			if command -v perl >/dev/null 2>&1; then
				perl -MTime::HiRes=time -e 'print int(time()*1000), "\n"'
			else
				date +%s000
			fi
		}
		append_process_timing() {
			local phase="\$1"
			local start_ts="\$2"
			local end_ts="\$3"
			local seconds=0
			local ms=0
			if [ -n "\$start_ts" ] && [ -n "\$end_ts" ] && [[ "\$start_ts" != *[!0-9]* ]] && [[ "\$end_ts" != *[!0-9]* ]] && [ "\$end_ts" -ge "\$start_ts" ]; then
				ms=\$(( end_ts - start_ts ))
				seconds=\$(( ms / 1000 ))
			fi
			printf '%s\t%s\t%s\n' "${round_barcode}" "\$phase" "\$seconds" >> consensus_process_timings.tsv
			printf '%s\t%s\t%s\n' "${round_barcode}" "\$phase" "\$ms" >> consensus_process_timings_ms.tsv
		}
		append_process_time_value() {
			local phase="\$1"
			local ms="\${2:-0}"
			local seconds=0
			if [ -z "\$ms" ] || [[ "\$ms" == *[!0-9]* ]]; then
				ms=0
			fi
			seconds=\$(( ms / 1000 ))
			printf '%s\t%s\t%s\n' "${round_barcode}" "\$phase" "\$seconds" >> consensus_process_timings.tsv
			printf '%s\t%s\t%s\n' "${round_barcode}" "\$phase" "\$ms" >> consensus_process_timings_ms.tsv
		}
		append_process_metric() {
			local metric="\$1"
			local value="\${2:-0}"
			if [ -z "\$value" ] || [[ "\$value" == *[!0-9]* ]]; then
				value=0
			fi
			printf '%s\t%s\t%s\n' "${round_barcode}" "\$metric" "\$value" >> consensus_process_metrics.tsv
		}
		printf 'round_barcode\tphase\tseconds\n' > consensus_process_timings.tsv
		printf 'round_barcode\tphase\tms\n' > consensus_process_timings_ms.tsv
		printf 'round_barcode\tmetric\tvalue\n' > consensus_process_metrics.tsv
		_t_consensus_prepare_start=\$(timing_now_ms)
		# -- §2: Prelaunch gate (reads/cache detection, sample mode, qscore prep) --
		_t_consensus_prelaunch_start=\$(timing_now_ms)

			# Pre-initialise required output files so Nextflow outputs are always satisfied,
			# even when the accumulated FASTA is empty (all reads pruned by C1).
			printf 'qseqid,sseqid,evalue,length,pident\n' > ${barcode}_preblastreport_join.txt
			printf 'long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n' > consensus_blast_report_full.txt
			printf 'round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n' > consensus_round_provenance.tsv
		export CONSENSUS_SUP_READS="${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta"
		CONSENSUS_MODE_TSV="consensus_sample_mode.tsv"
		CONSENSUS_CACHE_STATE_ROOT="${ongoingStateDir}/Consensus/.cache"
		CONSENSUS_CACHE_SYNC_SCRIPT="${baseDir}/bin/sync_dir_atomic.sh"
		_t_prelaunch_decision_gate_start=\$(timing_now_ms)
		# Decide whether consensus work is needed using only accumulated reads and cache presence.
		_CONS_HAS_READS=0
		if consensus_prelaunch_has_reads "\$CONSENSUS_SUP_READS"; then
			_CONS_HAS_READS=1
		fi
		_CONS_HAS_CACHE=0
		if consensus_prelaunch_has_cache "\$CONSENSUS_CACHE_STATE_ROOT"; then
			_CONS_HAS_CACHE=1
		fi
		_t_prelaunch_decision_gate_end=\$(timing_now_ms)
		append_process_timing "prelaunch_decision_gate" "\$_t_prelaunch_decision_gate_start" "\$_t_prelaunch_decision_gate_end"
		if [ "\$_CONS_HAS_READS" -eq 1 ] || [ "\$_CONS_HAS_CACHE" -eq 1 ]; then
			_t_prelaunch_sample_mode_detect_start=\$(timing_now_ms)
				if ! bash ${baseDir}/bin/detect_consensus_sample_mode.sh \
					blast_report_annotated.txt \
					samples.txt \
					"${sampleInfoDir}/samples.txt" \
					> "\$CONSENSUS_MODE_TSV"; then
				echo "ERROR: failed to determine consensus sample mode" 1>&2
				exit 1
			fi
			CONSENSUS_MODE=\$(awk -F'\t' '\$1=="mode"{print \$2; exit}' "\$CONSENSUS_MODE_TSV")
			CONSENSUS_MODE_SOURCE=\$(awk -F'\t' '\$1=="source"{print \$2; exit}' "\$CONSENSUS_MODE_TSV")
			echo "INFO: consensus sample mode mode=\${CONSENSUS_MODE:-unknown} source=\${CONSENSUS_MODE_SOURCE:-unknown}" 1>&2
			_t_prelaunch_sample_mode_detect_end=\$(timing_now_ms)
			append_process_timing "prelaunch_sample_mode_detect" "\$_t_prelaunch_sample_mode_detect_start" "\$_t_prelaunch_sample_mode_detect_end"
			_t_prelaunch_consensus_ids_restore_start=\$(timing_now_ms)
			# Restore previous consolidated IDs so cached-only rounds can re-evaluate emitted headers.
			if [ -f ${ongoingStateDir}/Consensus/consolidated_consensus_ids.txt ]; then
				mkdir -p Consensus
				cp ${ongoingStateDir}/Consensus/consolidated_consensus_ids.txt Consensus/consolidated_consensus_ids.txt 2>/dev/null || true
			fi
			_t_prelaunch_consensus_ids_restore_end=\$(timing_now_ms)
			append_process_timing "prelaunch_consensus_ids_restore" "\$_t_prelaunch_consensus_ids_restore_start" "\$_t_prelaunch_consensus_ids_restore_end"
			_t_prelaunch_qscore_prepare_start=\$(timing_now_ms)
			# Any future symlink optimization here depends on read_qscore.tsv remaining read-only
			# while preserving the local read_qscore.tsv path contract consumed by Consensus_simple.sh.
			ROLLING_QS="${ongoingStateDir}/_state/read_qscore_rolling.tsv"
			if [ -f "\$ROLLING_QS" ]; then
				cp "\$ROLLING_QS" read_qscore.tsv
			else
				: > read_qscore.tsv
			fi
			_t_prelaunch_qscore_prepare_end=\$(timing_now_ms)
			append_process_timing "prelaunch_qscore_prepare" "\$_t_prelaunch_qscore_prepare_start" "\$_t_prelaunch_qscore_prepare_end"
		else
			_t_prelaunch_sample_mode_detect_start=\$(timing_now_ms)
			_t_prelaunch_sample_mode_detect_end=\$(timing_now_ms)
			append_process_timing "prelaunch_sample_mode_detect" "\$_t_prelaunch_sample_mode_detect_start" "\$_t_prelaunch_sample_mode_detect_end"
			_t_prelaunch_consensus_ids_restore_start=\$(timing_now_ms)
			_t_prelaunch_consensus_ids_restore_end=\$(timing_now_ms)
			append_process_timing "prelaunch_consensus_ids_restore" "\$_t_prelaunch_consensus_ids_restore_start" "\$_t_prelaunch_consensus_ids_restore_end"
			_t_prelaunch_qscore_prepare_start=\$(timing_now_ms)
			_t_prelaunch_qscore_prepare_end=\$(timing_now_ms)
			append_process_timing "prelaunch_qscore_prepare" "\$_t_prelaunch_qscore_prepare_start" "\$_t_prelaunch_qscore_prepare_end"
		fi
		_t_consensus_prelaunch_end=\$(timing_now_ms)
		append_process_timing "consensus_prelaunch_setup" "\$_t_consensus_prelaunch_start" "\$_t_consensus_prelaunch_end"
		# -- §3: Consensus_simple.sh execution (reads or cache-only mode) --
		_t_consensus_script_exec_start=\$(timing_now_ms)
		if [ "\$_CONS_HAS_READS" -eq 1 ] || [ "\$_CONS_HAS_CACHE" -eq 1 ]; then
			# Consensus script: pass clustering identity so vsearch clustering is configurable.
			CONSENSUS_CACHE_STATE_ROOT="\$CONSENSUS_CACHE_STATE_ROOT" \
			CONSENSUS_CACHE_SYNC_SCRIPT="\$CONSENSUS_CACHE_SYNC_SCRIPT" \
			CONSENSUS_ZERO_EMIT_POLICY="${consensusZeroEmitPolicyCanonical}" \
			CONSENSUS_ID_MISMATCH_POLICY="${consensusIdMismatchPolicyCanonical}" \
			CONSENSUS_CACHE_BELOW_MIN_POLICY="${consensusCacheBelowMinPolicyCanonical}" \
			CONSENSUS_LOCK_ENABLED="${params.otu_consolidation_lock ? 1 : 0}" \
			CONSENSUS_LOCK_RATIO="${otuLockRatioStr}" \
			CONSENSUS_LOCK_MIN_CONS_READS="${otuLockMinConsReadsStr}" \
			CONSENSUS_LOCK_MIN_STABLE_ROUNDS="${otuLockMinStableRoundsStr}" \
			CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS="${otuLockRevalidateEveryRoundsStr}" \
			CONSENSUS_LOCK_KEYS_PREV="${ongoingStateDir}/_state/otu_consolidated_keys.tsv" \
			CONSENSUS_LOCK_RESET_KEYS="${params.otu_lock_reset_keys}" \
			CONSENSUS_PRUNE_FROZEN_POLICY="${otuPruneFrozenPolicyCanonical}" \
			CONSENSUS_PRUNE_UNASSIGNED_CLUSTERS="${pruneUnassignedClusters ? '1' : '0'}" \
			CONSENSUS_PRUNE_UNASSIGNED_DROP_READS="${(pruneUnassignedClusters && pruneUnassignedDropReads) ? '1' : '0'}" \
			CONSENSUS_PRUNE_UNASSIGNED_GRACE_ROUNDS="${pruneUnassignedGraceRoundsStr}" \
			CONSENSUS_PRUNE_UNASSIGNED_KEEP_TOP="${pruneUnassignedKeepTopStr}" \
			CONSENSUS_ASSIGNED_IDS="${assigned_read_ids}" \
			CONSENSUS_ROUND_INDEX_FILE="${ongoingStateDir}/_state/round_index.tsv" \
			CONSENSUS_ROUND_ID="${round_barcode}" \
			CONSENSUS_KEEP_ORIGINAL_READS="${consensusKeepOriginalReads ? 1 : 0}" \
			CONSENSUS_CPU_BUDGET="${params.consensus_cpu_budget}" \
			CONSENSUS_RSCRIPT_WORKERS="${params.consensus_workers}" \
			bash ${baseDir}/bin/Consensus_simple.sh ${baseDir}/bin/ ${params.consensus_id} ${params.consensus_min_reads} ${params.consensus_max_reads} ${params.consensus_min_qscore} ${params.consensus_consolidated_min_qscore} ${ongoingStateDir}/_state/otu_frozen_members.tsv "${params.consensus_reads_mode}" ${params.consensus_max_N}
		else
			echo "INFO: No sup_reads and no consensus cache; skipping Consensus_simple.sh this round" 1>&2
		fi
		_t_consensus_script_exec_end=\$(timing_now_ms)
		append_process_timing "consensus_script_exec" "\$_t_consensus_script_exec_start" "\$_t_consensus_script_exec_end"
		_t_consensus_postscript_start=\$(timing_now_ms)
		_cache_restore_seconds=0
		_cache_restore_max_sample_seconds=0
		_cache_restore_file_count=0
		if [ -f Consensus/cache_hydration_stats.tsv ]; then
			_cache_restore_seconds=\$(awk -F'\t' 'NR>1 && \$3=="1" && \$4=="1" && \$7 != "" && \$7 !~ /[^0-9]/ {sum+=\$7} END{print sum+0}' Consensus/cache_hydration_stats.tsv)
			_cache_restore_max_sample_seconds=\$(awk -F'\t' 'NR>1 && \$3=="1" && \$4=="1" && \$7 != "" && \$7 !~ /[^0-9]/ && \$7>max {max=\$7} END{print max+0}' Consensus/cache_hydration_stats.tsv)
			_cache_restore_file_count=\$(awk -F'\t' 'NR>1 && \$3=="1" && \$4=="1" && \$5 != "" && \$5 !~ /[^0-9]/ {sum+=\$5} END{print sum+0}' Consensus/cache_hydration_stats.tsv)
		fi
		append_process_time_value "cache_restore_worker_sum" "\$_cache_restore_seconds"
		# Diagnostic only: max single-sample hydrate duration, not round-level restore wall time.
		append_process_time_value "cache_restore_max_single_sample" "\$_cache_restore_max_sample_seconds"
		append_process_metric "cache_restore_file_count" "\$_cache_restore_file_count"
		# Explicit guard: report if consensus FASTA files were not produced
		cons_files=( Consensus/*/*_Merged_Consensus.fasta )
		CONSENSUS_PRODUCED=0
		if (( \${#cons_files[@]} == 0 )); then
			echo "WARN: No consensus FASTA files produced (Consensus/*/*_Merged_Consensus.fasta missing). Downstream consensus BLAST will be empty." 1>&2
			mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
			printf "No consensus FASTA files produced in this round (%s)\n" "${round_barcode}" > ${ongoingStateDir}/_state/${barcode}_consensus_missing.txt
			cp ${ongoingStateDir}/_state/${barcode}_consensus_missing.txt ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_missing.txt
		else
			CONSENSUS_PRODUCED=1
		fi
		_t_consensus_postscript_end=\$(timing_now_ms)
		append_process_timing "consensus_postscript_local_checks" "\$_t_consensus_postscript_start" "\$_t_consensus_postscript_end"
		_t_consensus_prepare_end=\$(timing_now_ms)
		append_process_timing "prepare_and_consensus_simple" "\$_t_consensus_prepare_start" "\$_t_consensus_prepare_end"
		# -- §4: Consensus BLAST annotation (per-target, parallel) --
		_t_consensus_annotate_start=\$(timing_now_ms)

		#Blast and consensus annotation:
		_p_targets="${params.targets}"
		IFS='|' read -ra _TARGETS   <<< "\$_p_targets"
		_p_blast_db_specs="${params.blast_db_specs}"
		IFS='|' read -ra _BLAST_DBS <<< "\$_p_blast_db_specs"
		_p_blast_id_family="${params.blast_id_family}"
		IFS='|' read -ra _ID_FAMILY <<< "\$_p_blast_id_family"
		_p_blast_id_genus="${params.blast_id_genus}"
		IFS='|' read -ra _ID_GENUS  <<< "\$_p_blast_id_genus"
		_p_blast_id_spec="${params.blast_id_spec}"
		IFS='|' read -ra _ID_SPEC   <<< "\$_p_blast_id_spec"
		# O2: split thread budget across concurrent per-target BLASTs
		_N_TARGETS="\${#_TARGETS[@]}"
		THREADS_PER_BLAST="\$THREADS"
		if [ "\$_N_TARGETS" -gt 1 ]; then
			THREADS_PER_BLAST=\$(( THREADS / _N_TARGETS ))
			[ "\$THREADS_PER_BLAST" -lt 1 ] && THREADS_PER_BLAST=1
		fi
			_blast_pids=()
			_blast_targets=()
			# C1: single source of truth for BLAST word_size/qcov
			_WORD_SIZE=50
			_QCOV=50

		for _i in "\${!_TARGETS[@]}"; do
			(
				_t="\${_TARGETS[\$_i]}"
				_db="${db_dir}\${_BLAST_DBS[\$_i]}"
				_id_fam="\${_ID_FAMILY[\$_i]}"
				_idx=\$(( _i + 1 ))
				_split_rc=0
				if (( \${#cons_files[@]} )); then
					# Split merged consensus FASTAs by header token rather than seqkit grep mode.
					# This preserves current header-driven target routing for IDs containing pipes.
					awk -v target="|\${_t}|" '
						BEGIN { RS=">"; ORS="" }
						NR > 1 {
							rec=\$0
							header=\$1
							sub(/[[:space:]].*/, "", header)
							if (index(header, target) > 0) {
								print ">" rec
							}
						}
					' "\${cons_files[@]}" > ${barcode}_\${_t}.fasta || _split_rc=\$?
				fi
				if [ "\$_split_rc" -ne 0 ]; then
					echo "ERROR: consensus target split failed target=\${_t} exit=\${_split_rc} sample=${barcode} round=${round_barcode}" 1>&2
					: > ${barcode}_preblastreport\${_idx}.txt
					exit 1
				elif [ -s ${barcode}_\${_t}.fasta ]; then
					_CACHE="\${STATE_DIR}/consensus_blast_cache_\${_t}.tsv"
					_CACHE_META="\${STATE_DIR}/consensus_blast_cache_\${_t}.meta"
					_KEY="db=\${_db}|sig=\$(db_sig \${_db})|taxdb=${taxdb_dir}|taxsig=\$(dir_sig ${taxdb_dir})|idfam=\${_id_fam}|evalue=${params.blast_evalue}|maxhsps=${params.blast_max_hsps}|word=\${_WORD_SIZE}|qcov=\${_QCOV}|target=\${_t}"
					if [ ! -f "\$_CACHE_META" ] || [ "\$(cat "\$_CACHE_META" 2>/dev/null)" != "\$_KEY" ]; then
						: > "\$_CACHE"
						printf '%s\n' "\$_KEY" > "\$_CACHE_META"
					fi
					[ -f "\$_CACHE" ] || : > "\$_CACHE"
					${baseDir}/bin/cache_blast_by_hash.pl ${barcode}_\${_t}.fasta "\$_CACHE" ${barcode}_preblast_cached\${_idx}.txt ${barcode}_preblast_new\${_idx}.fasta ${barcode}_preblast_hash_new\${_idx}.tsv
	
						if [ -s ${barcode}_preblast_new\${_idx}.fasta ]; then
							if ! blastn -query ${barcode}_preblast_new\${_idx}.fasta -db "\$_db" -num_threads "\$THREADS_PER_BLAST" -task megablast -dust no -outfmt "10 qseqid sseqid evalue length pident" -perc_identity "\$_id_fam" -evalue ${params.blast_evalue} -max_hsps ${params.blast_max_hsps} -max_target_seqs 1 -word_size "\$_WORD_SIZE" -qcov_hsp_perc "\$_QCOV" -mt_mode 2 > ${barcode}_preblast_new\${_idx}.txt; then
								echo "ERROR: consensus blastn failed target=\${_t} db=\${_db} sample=${barcode} round=${round_barcode}" 1>&2
								exit 1
							fi
						else
							: > ${barcode}_preblast_new\${_idx}.txt
						fi
	
					if [ -s ${barcode}_preblast_hash_new\${_idx}.tsv ] && [ -s ${barcode}_preblast_new\${_idx}.txt ]; then
						awk -F'\t' 'NR==FNR{h[\$1]=\$2; next} {split(\$0,a,","); if (a[1] in h) print h[a[1]] "\t" a[2] "\t" a[3] "\t" a[4] "\t" a[5];}' ${barcode}_preblast_hash_new\${_idx}.tsv ${barcode}_preblast_new\${_idx}.txt >> "\$_CACHE"
						awk -F'\t' '{line[\$1]=\$0} END{for (k in line) print line[k]}' "\$_CACHE" | LC_ALL=C sort > "\${_CACHE}.tmp" && mv "\${_CACHE}.tmp" "\$_CACHE"
					fi
	
					if [ -s ${barcode}_preblast_cached\${_idx}.txt ] || [ -s ${barcode}_preblast_new\${_idx}.txt ]; then
						cat ${barcode}_preblast_cached\${_idx}.txt ${barcode}_preblast_new\${_idx}.txt > ${barcode}_preblastreport\${_idx}.txt
					else
						: > ${barcode}_preblastreport\${_idx}.txt
					fi
				else
					# No consensus sequences matched this target — write empty report (non-fatal).
					: > ${barcode}_preblastreport\${_idx}.txt
				fi
				) &
				_blast_pids+=( "\$!" )
				_blast_targets+=( "\${_TARGETS[\$_i]}" )
			done
			worker_failures_blast=0
			for _j in "\${!_blast_pids[@]}"; do
				_pid="\${_blast_pids[\$_j]}"
				_t="\${_blast_targets[\$_j]:-unknown}"
				if ! wait "\$_pid"; then
					worker_failures_blast=\$((worker_failures_blast + 1))
					echo "ERROR: consensus BLAST worker failed target=\${_t} sample=${barcode} round=${round_barcode}" 1>&2
					# Kill remaining workers and abort immediately on first failure
					for _k in "\${_blast_pids[@]}"; do
						kill "\$_k" 2>/dev/null || true
					done
					echo "METRIC: worker_failures_blast=\${worker_failures_blast} sample=${barcode} round=${round_barcode}" 1>&2
					exit 1
				fi
			done
			echo "METRIC: worker_failures_blast=0 sample=${barcode} round=${round_barcode}" 1>&2

					# Robustly join preblast and blast reports; avoid `cat <glob> > out` hangs under `nullglob`.
					# Always produce a header-only placeholder (never a sentinel string) so downstream parsers don't ingest junk.
					if compgen -G "${barcode}_preblastreport[0-9]*.txt" > /dev/null; then
					cat ${barcode}_preblastreport[0-9]*.txt > ${barcode}_preblastreport_join.txt
				else
					printf 'qseqid,sseqid,evalue,length,pident\n' > ${barcode}_preblastreport_join.txt
				fi
					# Build a tabular consensus blast report with taxonomy columns.
					# Guard TaxonKit usage and skip non-numeric taxids (fallback to Unassigned).
					printf 'long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n' > consensus_blast_report_full.txt
						# -- §5: TaxonKit taxonomy depth assignment --
		TAXONKIT_CACHE_STATE="\${STATE_DIR}/${barcode}_consensus_taxonkit_lineage_cache.tsv"
						TAXONKIT_CACHE_META="\${STATE_DIR}/${barcode}_consensus_taxonkit_lineage_cache.meta"
						TAXONKIT_CACHE_LOCAL="consensus_taxonkit_lineage_cache.tsv"
						_TAXDB_SIG="\$(dir_sig ${taxdb_dir})"
						_CACHED_TAXDB_SIG="\$(cat "\$TAXONKIT_CACHE_META" 2>/dev/null || true)"
						if [ -s "\$TAXONKIT_CACHE_STATE" ] && [ "\$_CACHED_TAXDB_SIG" = "\$_TAXDB_SIG" ]; then
							cp "\$TAXONKIT_CACHE_STATE" "\$TAXONKIT_CACHE_LOCAL" 2>/dev/null || : > "\$TAXONKIT_CACHE_LOCAL"
						else
							[ -n "\$_CACHED_TAXDB_SIG" ] && [ "\$_CACHED_TAXDB_SIG" != "\$_TAXDB_SIG" ] && \
								echo "INFO: taxdb signature changed; invalidating taxonkit lineage cache sample=${barcode}" 1>&2 || true
							: > "\$TAXONKIT_CACHE_LOCAL"
						fi
						if [ -s ${barcode}_preblastreport_join.txt ]; then
						# tmp_idx columns: idx, qseqid, sseqid, taxid
						# Use preblastreport_join.txt (raw BLAST CSV) to extract numeric taxids
						# for TaxonKit lookup.
						awk 'BEGIN{FS=","; OFS="\t"; i=0}
							\$1 == "qseqid" { next }
							\$2 != "" {
								i++;
								q=\$1; s=\$2; t="";
								# s is the raw BLAST sseqid: either a bare numeric taxid
								# or a hit id containing |kraken:taxid|NNN. Extract a numeric taxid portably.
								# Avoid end-of-line anchors in regex here because dollar sign triggers Groovy interpolation
								# in Nextflow script blocks. This is equivalent to "only digits" given s != "".
								if (s !~ /[^0-9]/) {
									t=s;
								} else {
										# BSD/macOS awk does not support match(s, r, a) with a capture array; use portable parsing.
									tag="|kraken:taxid|";
									pos=index(s, tag);
									if (pos > 0) {
										rest=substr(s, pos+length(tag));
										if (match(rest, /^[0-9]+/)) {
											t=substr(rest, RSTART, RLENGTH);
										}
									}
								}
								print i, q, s, t;
							}' ${barcode}_preblastreport_join.txt > tmp_idx.tsv
						# Nothing to annotate -> keep header-only output.
						if [ ! -s tmp_idx.tsv ]; then
							: > tmp_tax.tsv
							: > tmp_tax_cols.tsv
						else

								# tmp_tax columns: idx, lineage (with rank prefixes); only for numeric taxids
								: > tmp_tax.tsv
								if command -v taxonkit >/dev/null 2>&1; then
									cut -f1,4 tmp_idx.tsv | awk 'BEGIN{FS=OFS="\t"} \$2 != "" && \$2 !~ /[^0-9]/ {print \$0}' > tmp_taxids_numeric.tsv
									if [ -s tmp_taxids_numeric.tsv ]; then
										cut -f2 tmp_taxids_numeric.tsv | LC_ALL=C sort -u > tmp_taxids_unique.txt
										: > tmp_tax_map.tsv
										if [ -s "\$TAXONKIT_CACHE_LOCAL" ]; then
											awk 'BEGIN{FS=OFS="\t"; first=ARGV[1]}
												FILENAME==first { want[\$1]=1; next }
												NF>=2 && (\$1 in want) && \$2 != "" { print \$1, \$2 }
											' tmp_taxids_unique.txt "\$TAXONKIT_CACHE_LOCAL" | LC_ALL=C sort -u > tmp_tax_map.tsv
										fi
										if [ -s tmp_tax_map.tsv ]; then
											cut -f1 tmp_tax_map.tsv | LC_ALL=C sort -u > tmp_taxids_cached.txt
										else
											: > tmp_taxids_cached.txt
										fi
										awk 'BEGIN{first=ARGV[1]}
											FILENAME==first { c[\$1]=1; next }
											!(\$1 in c) { print \$1 }
										' tmp_taxids_cached.txt tmp_taxids_unique.txt > tmp_taxids_missing.txt
										if [ -s tmp_taxids_missing.txt ]; then
											taxonkit lineage tmp_taxids_missing.txt 2>/dev/null \
												| taxonkit reformat -f "{K};{p};{c};{o};{f};{g};{s}" -P 2>/dev/null \
												| awk -F'\t' 'BEGIN{OFS="\t"} NF>=3 && \$1 != "" && \$1 !~ /[^0-9]/ && \$3 != "" { print \$1, \$3 }' \
												> tmp_tax_map_missing.tsv || : > tmp_tax_map_missing.tsv
											if [ -s tmp_tax_map_missing.tsv ]; then
												# B4: append-only + single compact instead of full cat+sort+mv rebuild
												cat tmp_tax_map_missing.tsv >> tmp_tax_map.tsv
												LC_ALL=C sort -u -o tmp_tax_map.tsv tmp_tax_map.tsv
												cat tmp_tax_map_missing.tsv >> "\$TAXONKIT_CACHE_LOCAL"
												LC_ALL=C sort -u -o "\$TAXONKIT_CACHE_LOCAL" "\$TAXONKIT_CACHE_LOCAL"
											fi
										fi
										awk 'BEGIN{FS=OFS="\t"; first=ARGV[1]}
											FILENAME==first { lin[\$1]=\$2; next }
											{
												idx=\$1;
												taxid=\$2;
												if (taxid in lin && lin[taxid] != "") {
													print idx, lin[taxid];
												}
											}
										' tmp_tax_map.tsv tmp_taxids_numeric.tsv > tmp_tax.tsv || : > tmp_tax.tsv
									fi
								fi

							fallback_lineage='K__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned'
							awk -v fb="\$fallback_lineage" 'BEGIN{FS=OFS="\t"; first=ARGV[1]}
								FILENAME==first { lin[\$1]=\$2; next }
								{
									idx=\$1; taxid=\$4;
									if (taxid == "" || taxid == "NA") { print fb; next }
									if (idx in lin && lin[idx] != "") { print lin[idx] } else { print fb }
								}' tmp_tax.tsv tmp_idx.tsv \
								| sed -E 's/[Kpcofgs]__//g' \
								| tr ';' '\t' \
								> tmp_tax_cols.tsv

							# Resolve assignment depth: pident + thresholds => level (species/genus/family/unassigned).
							# Per target, collect all hits then dedup by qseqid keeping deepest level.
							: > tmp_assign_levels.tsv
							for _i in "\${!_TARGETS[@]}"; do
								_id_fam="\${_ID_FAMILY[\$_i]}"
								_id_gen="\${_ID_GENUS[\$_i]}"
								_id_spec="\${_ID_SPEC[\$_i]}"
								_idx=\$(( _i + 1 ))
								if [ -s ${barcode}_preblastreport\${_idx}.txt ]; then
									bash ${baseDir}/bin/consensus_assign_depth.sh \
										--report   ${barcode}_preblastreport\${_idx}.txt \
										--family   "\$_id_fam" \
										--genus    "\$_id_gen" \
										--species  "\$_id_spec" \
										>> tmp_assign_levels.tsv
								fi
							done
							awk -F'\t' 'BEGIN{r["species"]=3;r["genus"]=2;r["family"]=1;r["unassigned"]=0}
								{ if (!(\$1 in best) || r[\$2]>r[best[\$1]]) best[\$1]=\$2 }
								END{ for (q in best) print q "\t" best[q] }
							' tmp_assign_levels.tsv | LC_ALL=C sort > tmp_assign_levels_uniq.tsv

							paste tmp_idx.tsv tmp_tax_cols.tsv > tmp_tax_join.tsv
							bash ${baseDir}/bin/consensus_threshold_mask.sh tmp_assign_levels_uniq.tsv tmp_tax_join.tsv >> consensus_blast_report_full.txt
						fi
						if [ -f "\$TAXONKIT_CACHE_LOCAL" ]; then
							cp "\$TAXONKIT_CACHE_LOCAL" "\$TAXONKIT_CACHE_STATE" 2>/dev/null || true
							printf '%s\n' "\$_TAXDB_SIG" > "\$TAXONKIT_CACHE_META" 2>/dev/null || true
						fi
							fi
						# consensus provenance failfast start
						if ! perl ${baseDir}/bin/emit_consensus_round_provenance.pl \
							--consensus-dir Consensus \
							--round-barcode "${round_barcode}" \
							--out consensus_round_provenance.tsv; then
							echo "ERROR: failed to emit consensus round provenance" 1>&2
							exit 1
						fi
						# consensus provenance failfast end
					# Generate recovery list now: OriginalReads are removed by the collect step when keep=1.
					# -- §6: Consensus state update (assigned OTU keys, protected reads) --
					RECOVERY_IDS="${round_barcode}_consensus_assigned_reads.list"
					: > "\$RECOVERY_IDS"
					if ! perl ${baseDir}/bin/consensus_recovered_reads.pl \
						--blast-report consensus_blast_report_full.txt \
						--consensus-dir Consensus \
						--out "\$RECOVERY_IDS" \
						--min-level "${assignProtLevelCanonical}"; then
						echo "WARN: failed to recover consensus-assigned reads fallback" 1>&2
						: > "\$RECOVERY_IDS"
					fi
					ROUND_DIR="${ongoingStateDir}/${round_barcode}"
					STATE_DIR="${ongoingStateDir}/_state"
					OTU_HASH_MAP_STATE="${ongoingStateDir}/_state/${barcode}_otu_nr_hash_map.tsv"
					ASSIGNED_OTU_MEMBERS_EVER="\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list"
					ASSIGNED_OTU_KEYS_EVER="\${STATE_DIR}/${barcode}_assigned_otu_keys_ever.list"
					CONSENSUS_ASSIGNED_OTU_KEYS_ROUND="\$ROUND_DIR/${barcode}_consensus_assigned_otu_keys_round.list"
					CONSENSUS_ASSIGNED_MEMBERS_CURRENT_RAW="\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_raw_current.list"
					CONSENSUS_ASSIGNED_MEMBERS_EVER_ROUND="\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list"
					PROTECTED_OTU_KEYS_ROUND="\$ROUND_DIR/${barcode}_protected_otu_keys_round.list"
					PROTECTED_READ_IDS_ROUND="\$ROUND_DIR/${barcode}_protected_read_ids_round.list"
					CONSENSUS_OTU_KEYS_PERSIST_STATS="\$ROUND_DIR/${barcode}_consensus_assigned_otu_keys_persist_stats.tsv"
					PROTECTED_OTU_ROUND_STATS="\$ROUND_DIR/${barcode}_protected_otu_round_stats.tsv"
						mkdir -p "\$ROUND_DIR" "\$STATE_DIR"
						if ! perl ${baseDir}/bin/consensus_assigned_otu_keys.pl \
							--blast-report consensus_blast_report_full.txt \
							--provenance consensus_round_provenance.tsv \
							--out "\$CONSENSUS_ASSIGNED_OTU_KEYS_ROUND" \
							--min-level "${assignProtLevelCanonical}"; then
							echo "ERROR: failed to extract consensus-assigned OTU keys" 1>&2
							exit 1
						fi
						if ! perl ${baseDir}/bin/expand_otu_keys_to_member_ids.pl \
							"\$CONSENSUS_ASSIGNED_OTU_KEYS_ROUND" \
							"\$ROUND_DIR/otu_members_round.tsv" \
							"\$CONSENSUS_ASSIGNED_MEMBERS_CURRENT_RAW" \
							"" \
							"\$OTU_HASH_MAP_STATE"; then
							echo "ERROR: failed to expand current consensus-assigned OTU keys into member reads" 1>&2
							exit 1
						fi
						if ! materialize_round_grace_ids \
							"\$CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE" \
							"\$CONSENSUS_ASSIGNED_MEMBERS_CURRENT_RAW" \
							"\$CONSENSUS_ASSIGNED_MEMBERS_EVER_ROUND"; then
							echo "ERROR: failed to materialize next-round grace consensus-assigned member reads for current pool" 1>&2
							exit 1
						fi
						refresh_protected_read_ids_ever "\$PROTECTED_READ_IDS_EVER_STATE"
						: > "\$PROTECTED_OTU_KEYS_ROUND"
					if [ -s "\$ASSIGNED_OTU_KEYS_EVER" ]; then
						cat "\$ASSIGNED_OTU_KEYS_EVER" >> "\$PROTECTED_OTU_KEYS_ROUND"
					fi
					if [ -s "\$CONSENSUS_ASSIGNED_OTU_KEYS_ROUND" ]; then
						cat "\$CONSENSUS_ASSIGNED_OTU_KEYS_ROUND" >> "\$PROTECTED_OTU_KEYS_ROUND"
					fi
					if [ -s "\$PROTECTED_OTU_KEYS_ROUND" ]; then
						LC_ALL=C sort -u -o "\$PROTECTED_OTU_KEYS_ROUND" "\$PROTECTED_OTU_KEYS_ROUND"
					fi
						: > "\$PROTECTED_READ_IDS_ROUND"
						if [ -s "\$ASSIGNED_OTU_MEMBERS_EVER" ]; then
							cat "\$ASSIGNED_OTU_MEMBERS_EVER" >> "\$PROTECTED_READ_IDS_ROUND"
						fi
						if [ -s "\$CONSENSUS_ASSIGNED_MEMBERS_EVER_ROUND" ]; then
							cat "\$CONSENSUS_ASSIGNED_MEMBERS_EVER_ROUND" >> "\$PROTECTED_READ_IDS_ROUND"
						fi
						if [ -s "\$PROTECTED_READ_IDS_ROUND" ]; then
							LC_ALL=C sort -u -o "\$PROTECTED_READ_IDS_ROUND" "\$PROTECTED_READ_IDS_ROUND"
						fi
						_protected_otu_keys_round_count=0
						_protected_read_ids_round_count=0
						[ -s "\$PROTECTED_OTU_KEYS_ROUND" ] && _protected_otu_keys_round_count=\$(wc -l < "\$PROTECTED_OTU_KEYS_ROUND" | tr -d ' ')
						[ -s "\$PROTECTED_READ_IDS_ROUND" ] && _protected_read_ids_round_count=\$(wc -l < "\$PROTECTED_READ_IDS_ROUND" | tr -d ' ')
						{
							printf 'protected_otu_keys_round_count\t%s\n' "\$_protected_otu_keys_round_count"
							printf 'protected_otu_member_ids_round_count\t%s\n' "\$_protected_read_ids_round_count"
						} > "\$PROTECTED_OTU_ROUND_STATS"
						if ! perl ${baseDir}/bin/persist_otu_keys_ever.pl \
							"\$CONSENSUS_ASSIGNED_OTU_KEYS_ROUND" \
							"\$ASSIGNED_OTU_KEYS_EVER" \
							"\$CONSENSUS_OTU_KEYS_PERSIST_STATS"; then
							echo "ERROR: failed to persist consensus-assigned OTU keys" 1>&2
							exit 1
						fi
					if ! bash ${baseDir}/bin/consensus_original_reads_collect.sh \
						--consensus-dir Consensus \
						--round-dir "${ongoingStateDir}/${round_barcode}" \
						--provenance consensus_round_provenance.tsv \
						--keep "${consensusKeepOriginalReads ? 1 : 0}"; then
						echo "WARN: failed to collect consensus OriginalReads lists" 1>&2
					fi
					_t_consensus_annotate_end=\$(timing_now_ms)
					append_process_timing "blast_taxonomy_and_recovery" "\$_t_consensus_annotate_start" "\$_t_consensus_annotate_end"
					_t_consensus_persist_start=\$(timing_now_ms)
					_cache_backfill_missing_count=\$(consensus_cache_backfill_missing_count "\$CONSENSUS_CACHE_STATE_ROOT" "Consensus/.cache")
					_cache_backfill_file_count=\$(consensus_cache_backfill_missing_file_count "\$CONSENSUS_CACHE_STATE_ROOT" "Consensus/.cache")
					append_process_metric "cache_backfill_missing_sample_dirs" "\$_cache_backfill_missing_count"
					append_process_metric "cache_backfill_file_count" "\$_cache_backfill_file_count"
					_t_cache_backfill_start=\$(timing_now_ms)
					if [ "\$_cache_backfill_missing_count" -gt 0 ]; then
						if ! restore_missing_consensus_cache_dirs "\$CONSENSUS_CACHE_STATE_ROOT" "Consensus/.cache" "\$CONSENSUS_CACHE_SYNC_SCRIPT"; then
							exit 1
						fi
					fi
					_t_cache_backfill_end=\$(timing_now_ms)
					append_process_timing "cache_backfill_before_persist" "\$_t_cache_backfill_start" "\$_t_cache_backfill_end"
					if [ "\$CONSENSUS_PRODUCED" -eq 1 ] && [ -d Consensus ];
			then
			# O5: incremental rsync directly to state — skips unchanged .cache files (same mtime+size)
			mkdir -p ${ongoingStateDir}/Consensus
			if ! rsync -a --delete Consensus/ ${ongoingStateDir}/Consensus/; then
				echo "ERROR: failed to persist Consensus directory" 1>&2
				exit 1
			fi
		elif [ -d Consensus/.cache ]; then
			# Consensus ran but produced no output — persist cache and status files only
			# O5: incremental rsync for cache-only path
			mkdir -p ${ongoingStateDir}/Consensus/.cache
			if ! rsync -a --delete Consensus/.cache/ ${ongoingStateDir}/Consensus/.cache/; then
				echo "ERROR: failed to persist consensus cache on zero-emission round" 1>&2
				exit 1
			fi
			# Persist status file updated by Consensus_simple.sh on zero-emission rounds.
			if [ -f Consensus/consolidated_ids_status.tsv ]; then
				mkdir -p ${ongoingStateDir}/Consensus
				cp Consensus/consolidated_ids_status.tsv ${ongoingStateDir}/Consensus/consolidated_ids_status.tsv || true
			fi
		fi
		_t_consensus_persist_end=\$(timing_now_ms)
		append_process_timing "cache_persist" "\$_t_consensus_persist_start" "\$_t_consensus_persist_end"
		# Persist per-round OTU lock summary (append to history and copy round snapshot).
		if [ -f Consensus/otu_lock_summary.tsv ]; then
			mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
			if [ ! -f ${ongoingStateDir}/_state/otu_lock_summary_history.tsv ]; then
				head -n 1 Consensus/otu_lock_summary.tsv > ${ongoingStateDir}/_state/otu_lock_summary_history.tsv
			fi
			if [ -s Consensus/otu_lock_summary.tsv ]; then
				tail -n +2 Consensus/otu_lock_summary.tsv >> ${ongoingStateDir}/_state/otu_lock_summary_history.tsv
			fi
			cp Consensus/otu_lock_summary.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_otu_lock_summary.tsv 2>/dev/null || true
		fi
		# Expose consolidated consensus IDs (if any) to state/round for reporting.
		if [ -f ${ongoingStateDir}/Consensus/consolidated_consensus_ids.txt ]; then
			cp ${ongoingStateDir}/Consensus/consolidated_consensus_ids.txt ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_ids.txt 2>/dev/null || true
			mkdir -p ${ongoingStateDir}/${round_barcode}
			cp ${ongoingStateDir}/Consensus/consolidated_consensus_ids.txt ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_consolidated_ids.txt 2>/dev/null || true
		fi
			if [ -f ${ongoingStateDir}/Consensus/consolidated_ids_status.tsv ]; then
				cp ${ongoingStateDir}/Consensus/consolidated_ids_status.tsv ${ongoingStateDir}/_state/${barcode}_consolidated_ids_status.tsv 2>/dev/null || true
				mkdir -p ${ongoingStateDir}/${round_barcode}
				cp ${ongoingStateDir}/Consensus/consolidated_ids_status.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consolidated_ids_status.tsv 2>/dev/null || true
			fi
			if [ -s consensus_round_provenance.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp consensus_round_provenance.tsv ${ongoingStateDir}/_state/${barcode}_consensus_round_provenance_last.tsv 2>/dev/null || true
				cp consensus_round_provenance.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_round_provenance.tsv 2>/dev/null || true
			fi
			if [ -s Consensus/eligible_pool_counts.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp Consensus/eligible_pool_counts.tsv ${ongoingStateDir}/_state/${barcode}_eligible_pool_counts_last.tsv 2>/dev/null || true
				cp Consensus/eligible_pool_counts.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_eligible_pool_counts.tsv 2>/dev/null || true
			fi
			if [ -f Consensus/consensus_phase_timings.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp Consensus/consensus_phase_timings.tsv ${ongoingStateDir}/_state/${barcode}_consensus_phase_timings_last.tsv 2>/dev/null || true
				cp Consensus/consensus_phase_timings.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_phase_timings.tsv 2>/dev/null || true
			fi
			if [ -f Consensus/consensus_sample_phase_timings.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp Consensus/consensus_sample_phase_timings.tsv ${ongoingStateDir}/_state/${barcode}_consensus_sample_phase_timings_last.tsv 2>/dev/null || true
				cp Consensus/consensus_sample_phase_timings.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_sample_phase_timings.tsv 2>/dev/null || true
			fi
				if [ -f Consensus/consensus_rscript_stats.tsv ]; then
					mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
					cp Consensus/consensus_rscript_stats.tsv ${ongoingStateDir}/_state/${barcode}_consensus_rscript_stats_last.tsv 2>/dev/null || true
					cp Consensus/consensus_rscript_stats.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_rscript_stats.tsv 2>/dev/null || true
				fi
				if [ -f Consensus/consensus_sample_totals.tsv ]; then
					mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
					cp Consensus/consensus_sample_totals.tsv ${ongoingStateDir}/_state/${barcode}_consensus_sample_totals_last.tsv 2>/dev/null || true
					cp Consensus/consensus_sample_totals.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_sample_totals.tsv 2>/dev/null || true
				fi
				if [ -f Consensus/cache_hydration_stats.tsv ]; then
					mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
					cp Consensus/cache_hydration_stats.tsv ${ongoingStateDir}/_state/${barcode}_cache_hydration_stats_last.tsv 2>/dev/null || true
				cp Consensus/cache_hydration_stats.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_cache_hydration_stats.tsv 2>/dev/null || true
			fi
			if [ -f consensus_process_timings.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp consensus_process_timings.tsv ${ongoingStateDir}/_state/${barcode}_consensus_process_timings_last.tsv 2>/dev/null || true
				cp consensus_process_timings.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_process_timings.tsv 2>/dev/null || true
			fi
			if [ -f consensus_process_timings_ms.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp consensus_process_timings_ms.tsv ${ongoingStateDir}/_state/${barcode}_consensus_process_timings_ms_last.tsv 2>/dev/null || true
				cp consensus_process_timings_ms.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_process_timings_ms.tsv 2>/dev/null || true
			fi
			if [ -f consensus_process_metrics.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp consensus_process_metrics.tsv ${ongoingStateDir}/_state/${barcode}_consensus_process_metrics_last.tsv 2>/dev/null || true
				cp consensus_process_metrics.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_process_metrics.tsv 2>/dev/null || true
			fi
			if [ -s Consensus/consensus_prune_unassigned_stats.tsv ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp Consensus/consensus_prune_unassigned_stats.tsv ${ongoingStateDir}/_state/${barcode}_consensus_prune_unassigned_stats_last.tsv 2>/dev/null || true
				cp Consensus/consensus_prune_unassigned_stats.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_consensus_prune_unassigned_stats.tsv 2>/dev/null || true
			fi
			if [ -f Consensus/pruned_unassigned_reads_round.list ]; then
				mkdir -p ${ongoingStateDir}/_state ${ongoingStateDir}/${round_barcode}
				cp Consensus/pruned_unassigned_reads_round.list ${ongoingStateDir}/${round_barcode}/${barcode}_pruned_unassigned_reads.list 2>/dev/null || true
				cp Consensus/pruned_unassigned_reads_round.list ${ongoingStateDir}/_state/${barcode}_pruned_unassigned_reads_last.list 2>/dev/null || true
				if ! bash ${baseDir}/bin/consensus_pruned_unassigned_merge.sh \
					--round-list Consensus/pruned_unassigned_reads_round.list \
					--state-dir ${ongoingStateDir}/_state \
					--barcode ${barcode}; then
					echo "WARN: failed to merge pruned unassigned reads into state list" 1>&2
				fi
			fi
			if [ -f Consensus/otu_consolidated_keys.tsv ]; then
			cp Consensus/otu_consolidated_keys.tsv ${ongoingStateDir}/_state/otu_consolidated_keys.tsv 2>/dev/null || true
			mkdir -p ${ongoingStateDir}/${round_barcode}
			cp Consensus/otu_consolidated_keys.tsv ${ongoingStateDir}/${round_barcode}/${barcode}_otu_consolidated_keys.tsv 2>/dev/null || true
		fi
		# -- Recompute unified size-streak prune IDs from round OTU members (includes new + carried reads) --
		ROUND_DIR="${ongoingStateDir}/${round_barcode}"
		ROUND_PRUNE_IDS="${ongoingStateDir}/${round_barcode}/${barcode}_round_prune_ids.list"
		ROUND_PRUNE_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_round_prune_stats.tsv"
		C1_PRUNE_IDS="${ongoingStateDir}/${round_barcode}/${barcode}_c1_prune_ids.list"
		CONSENSUS_UNASSIGNED_PRUNE_IDS="${ongoingStateDir}/${round_barcode}/${barcode}_consensus_unassigned_prune_ids.list"
			OTU_SIZE_STREAK_MODE="${otuSizeStreakModeCanonical}"
			OTU_BLAST_FILTER_MODE="${otuBlastFilterModeCanonical}"
			OTU_SIZE_STREAK_MIN_ROUNDS="${otuSizeStreakMinRoundsStr}"
			OTU_BLAST_MIN_MEMBERS="${otuBlastMinMembersStr}"
			OTU_SIZE_STREAK_STATE="${ongoingStateDir}/_state/${barcode}_otu_size_streak.tsv"
			OTU_SIZE_STREAK_STATS_LAST="${ongoingStateDir}/_state/${barcode}_otu_size_streak_stats_last.tsv"
			OTU_SIZE_STREAK_IDS_LAST="${ongoingStateDir}/_state/${barcode}_otu_size_streak_prune_ids_last.txt"
			OTU_HASH_MAP_STATE="${ongoingStateDir}/_state/${barcode}_otu_nr_hash_map.tsv"
			OTU_SIZE_STREAK_IDS_ROUND="${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak_prune_ids.txt"
			OTU_SIZE_STREAK_STATS_ROUND="${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak_stats.tsv"
			OTU_SIZE_STREAK_STATE_ROUND="${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak.tsv"
			OTU_SIZE_STREAK_STATE_NEXT="${ongoingStateDir}/${round_barcode}/${barcode}_otu_size_streak.next.tsv"
			SIZE_STREAK_PRUNE_IDS="${ongoingStateDir}/${round_barcode}/${barcode}_size_streak_prune_ids.list"
		if [ "\$OTU_SIZE_STREAK_MODE" != "off" ] || [ "\$OTU_BLAST_FILTER_MODE" != "off" ]; then
			[ -f "\$OTU_SIZE_STREAK_STATE" ] || : > "\$OTU_SIZE_STREAK_STATE"
			[ -f "\$OTU_HASH_MAP_STATE" ] || : > "\$OTU_HASH_MAP_STATE"
			if perl "${baseDir}/bin/otu_size_streak_update.pl" \
				"\$ROUND_DIR/otu_sizes_round.tsv" \
				"\$ROUND_DIR/otu_members_round.tsv" \
				"\$OTU_HASH_MAP_STATE" \
				"\$OTU_SIZE_STREAK_STATE" \
				"\$OTU_SIZE_STREAK_MIN_ROUNDS" \
				"\$OTU_SIZE_STREAK_IDS_ROUND" \
				"\$OTU_SIZE_STREAK_STATE_NEXT" \
				"\$OTU_SIZE_STREAK_STATS_ROUND" \
				"" \
				"" \
				"\$OTU_BLAST_MIN_MEMBERS"; then
				cp "\$OTU_SIZE_STREAK_STATE_NEXT" "\${OTU_SIZE_STREAK_STATE}.tmp" 2>/dev/null && mv "\${OTU_SIZE_STREAK_STATE}.tmp" "\$OTU_SIZE_STREAK_STATE" || true
				cp "\$OTU_SIZE_STREAK_STATS_ROUND" "\${OTU_SIZE_STREAK_STATS_LAST}.tmp" 2>/dev/null && mv "\${OTU_SIZE_STREAK_STATS_LAST}.tmp" "\$OTU_SIZE_STREAK_STATS_LAST" || true
				cp "\$OTU_SIZE_STREAK_IDS_ROUND" "\${OTU_SIZE_STREAK_IDS_LAST}.tmp" 2>/dev/null && mv "\${OTU_SIZE_STREAK_IDS_LAST}.tmp" "\$OTU_SIZE_STREAK_IDS_LAST" || true
				cp "\$OTU_SIZE_STREAK_STATE_NEXT" "\$OTU_SIZE_STREAK_STATE_ROUND" 2>/dev/null || true
			else
				echo "WARN: failed to recompute size-streak from round OTU members; keeping existing list" 1>&2
			fi
			if [ -s "\$OTU_SIZE_STREAK_IDS_ROUND" ]; then
				awk '{split(\$0,a,\"|\"); if (a[1] != \"\") print a[1]}' "\$OTU_SIZE_STREAK_IDS_ROUND" | LC_ALL=C sort -u > "\$SIZE_STREAK_PRUNE_IDS"
			else
				: > "\$SIZE_STREAK_PRUNE_IDS"
			fi
		fi
		# -- OTU-level unassigned-streak update --
		OTU_UNASSIGNED_STREAK_MODE="${otuUnassignedStreakModeCanonical}"
		OTU_UNASSIGNED_STREAK_IDS="\$ROUND_DIR/${barcode}_otu_unassigned_streak_prune_ids.txt"
		OTU_UNASSIGNED_STREAK_STATS="\$ROUND_DIR/${barcode}_otu_unassigned_streak_stats.tsv"
		OTU_UNASSIGNED_STREAK_STATE="${ongoingStateDir}/_state/${barcode}_otu_unassigned_streak.tsv"
		OTU_UNASSIGNED_STREAK_STATE_NEXT="${ongoingStateDir}/_state/${barcode}_otu_unassigned_streak_next.tsv"
		: > "\$OTU_UNASSIGNED_STREAK_IDS"
		[ -f "\$OTU_UNASSIGNED_STREAK_STATE" ] || : > "\$OTU_UNASSIGNED_STREAK_STATE"
		if [ "\$OTU_UNASSIGNED_STREAK_MODE" != "off" ]; then
			if perl "${baseDir}/bin/otu_unassigned_streak_update.pl" \
				"\$ROUND_DIR/otu_sizes_round.tsv" \
				"\$ROUND_DIR/otu_members_round.tsv" \
				"\$ROUND_DIR/${barcode}_blast_report_annotated_otu_evidence.txt" \
				"\$OTU_UNASSIGNED_STREAK_STATE" \
				"${params.otu_unassigned_streak_min_rounds}" \
				"${params.otu_unassigned_streak_min_size}" \
				"${params.otu_unassigned_streak_max_size}" \
				"\$OTU_UNASSIGNED_STREAK_IDS" \
				"\$OTU_UNASSIGNED_STREAK_STATS" \
				"\$OTU_UNASSIGNED_STREAK_STATE_NEXT" \
				"\$OTU_HASH_MAP_STATE"; then
				cp "\$OTU_UNASSIGNED_STREAK_STATE_NEXT" "\${OTU_UNASSIGNED_STREAK_STATE}.tmp" 2>/dev/null && mv "\${OTU_UNASSIGNED_STREAK_STATE}.tmp" "\$OTU_UNASSIGNED_STREAK_STATE" || true
			else
				echo "WARN: otu_unassigned_streak_update.pl failed; skipping unassigned-streak prune for this round" 1>&2
				: > "\$OTU_UNASSIGNED_STREAK_IDS"
			fi
			if [ "\$OTU_UNASSIGNED_STREAK_MODE" = "observe" ]; then
				: > "\$OTU_UNASSIGNED_STREAK_IDS"
			fi
		fi
		# -- Apply unified size-streak list after pooled update --
		OTU_BLAST_FILTER_MODE="${otuBlastFilterModeCanonical}"
		OTU_BLAST_FILTER_SKIP_ROUNDS="${otuBlastFilterSkipRoundsCanonical}"
		OTU_BLAST_FORCE_USE_FILTERED="${otuBlastForceUseFiltered ? '1' : '0'}"
		ROUND_INDEX_FILE="${ongoingStateDir}/_state/round_index.tsv"
		ROUND_INDEX=\$(awk -F'\t' -v rb="${round_barcode}" '\$1==rb{print \$2; exit}' "\$ROUND_INDEX_FILE" 2>/dev/null || true)
		: > "\$SIZE_STREAK_PRUNE_IDS"
		SMALL_OTU_REMERGE_ENABLED=0
		if [ -n "\$ROUND_INDEX" ] && [[ "\$ROUND_INDEX" != *[!0-9]* ]] && [ "\$ROUND_INDEX" -ge 1 ]; then
			OTU_BLAST_EFFECTIVE_MODE_TSV="${barcode}_blast_filter_effective_mode_prune.tsv"
			${baseDir}/bin/otu_blast_effective_mode.sh \
				"\$OTU_BLAST_FILTER_MODE" \
				"\$OTU_BLAST_FILTER_SKIP_ROUNDS" \
				"\$ROUND_INDEX" \
				> "\$OTU_BLAST_EFFECTIVE_MODE_TSV"
			effective_mode_value() {
				local key="\$1"
				awk -F'\t' -v k="\$key" '\$1==k{print \$2; exit}' "\$OTU_BLAST_EFFECTIVE_MODE_TSV"
			}
			OTU_BLAST_EFFECTIVE_MODE=\$(effective_mode_value effective_mode)
			if [ "\$OTU_BLAST_FORCE_USE_FILTERED" = "1" ] && [ "\$OTU_BLAST_EFFECTIVE_MODE" != "off" ]; then
				OTU_BLAST_EFFECTIVE_MODE="enforce"
			fi
			if [ "\$OTU_BLAST_EFFECTIVE_MODE" = "enforce" ]; then
				SMALL_OTU_REMERGE_ENABLED=1
			fi
		fi
		APPLY_SIZE_STREAK=0
		if [ "\$OTU_SIZE_STREAK_MODE" = "enforce" ] || [ "\$SMALL_OTU_REMERGE_ENABLED" = "1" ]; then
			APPLY_SIZE_STREAK=1
		fi
		if [ "\$APPLY_SIZE_STREAK" = "1" ] && [ -s "\$OTU_SIZE_STREAK_IDS_ROUND" ]; then
			awk '{split(\$0,a,\"|\"); if (a[1] != \"\") print a[1]}' "\$OTU_SIZE_STREAK_IDS_ROUND" | LC_ALL=C sort -u > "\$SIZE_STREAK_PRUNE_IDS"
		fi
		if [ "\$APPLY_SIZE_STREAK" != "1" ]; then
			: > "\$SIZE_STREAK_PRUNE_IDS"
		fi
		# Protect ever-assigned reads plus current members of persisted/provisional protected OTU keys.
			PROTECTED_READ_IDS_EVER="${ongoingStateDir}/_state/${barcode}_protected_read_ids_ever.list"
		PROTECTED_READ_IDS_ROUND="\$ROUND_DIR/${barcode}_protected_read_ids_round.list"
		PROTECTED_IDS="\$ROUND_DIR/${barcode}_protected_prune_ids.list"
		PROTECTED_STATS="\$ROUND_DIR/${barcode}_protected_ids_stats.tsv"
		BLAST_UNASSIGNED_STATUS=\$(awk -F'\t' '\$1=="blast_unassigned_status"{print \$2; exit}' "\$ROUND_PRUNE_STATS" 2>/dev/null || echo "unknown")
		BLAST_UNASSIGNED_IDS_POST="\$ROUND_DIR/${barcode}_blast_unassigned_reads_round.list"
		if [ ! -f "\$BLAST_UNASSIGNED_IDS_POST" ]; then : > "\$BLAST_UNASSIGNED_IDS_POST"; fi
		if ! bash ${baseDir}/bin/prune_round_orchestrate.sh \
			--protected-read-ids-ever "\$PROTECTED_READ_IDS_EVER" \
			--protected-read-ids-round "\$PROTECTED_READ_IDS_ROUND" \
			--protected-ids "\$PROTECTED_IDS" \
			--protected-stats "\$PROTECTED_STATS" \
			--candidate size_streak "\$SIZE_STREAK_PRUNE_IDS" \
			--candidate consensus_unassigned "\$CONSENSUS_UNASSIGNED_PRUNE_IDS" \
			--candidate blast_unassigned "\$BLAST_UNASSIGNED_IDS_POST" \
			--candidate otu_unassigned_streak "\$OTU_UNASSIGNED_STREAK_IDS" \
			--c1-ids "\$C1_PRUNE_IDS" \
			--round-prune-ids "\$ROUND_PRUNE_IDS" \
			--round-prune-stats "\$ROUND_PRUNE_STATS" \
			--prune-cumulative-pool-all "${pruneCumulativePoolAll ? '1' : '0'}" \
			--blast-unassigned-status "\$BLAST_UNASSIGNED_STATUS" \
			--merge-error-context "failed to re-merge round prune ID lists after eligible-count update"; then
			exit 1
		fi
		# -- Post-consensus prune apply (recovery generated before collect step above) --
		bash ${baseDir}/bin/consensus_prune_apply.sh \
			--prune-ids      "${ongoingStateDir}/${round_barcode}/${barcode}_round_prune_ids.list" \
			--recovery-ids   "\$RECOVERY_IDS" \
			--fasta          "${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" \
			--fasta-tmp      "${ongoingStateDir}/_state/qced_reads_hq_accumulated.round_pruned.tmp" \
			--apply-stats    "${ongoingStateDir}/${round_barcode}/${barcode}_round_prune_apply.tsv" \
			--apply-last     "${ongoingStateDir}/_state/${barcode}_round_prune_apply_last.tsv" \
			--final-last     "${ongoingStateDir}/_state/${barcode}_round_prune_ids_applied_last.list" \
			--prune-stats    "${ongoingStateDir}/${round_barcode}/${barcode}_round_prune_stats.tsv" \
			--c1-prune-ids   "${ongoingStateDir}/${round_barcode}/${barcode}_c1_prune_ids.list" \
			--pruned-barrier "${ongoingStateDir}/_state/${barcode}_pruned_barrier.list" \
			--pruned-archive "${ongoingStateDir}/_state/${barcode}_pruned_archive.fasta" \
			--round-cp       "${ongoingStateDir}/${round_barcode}/qced_reads_hq_accumulated.fasta" \
			--lock-dir       "${ongoingStateDir}/_state/.qced_reads.lock" \
			--lock-wait      "${params.lock_wait_seconds}" \
			--apply-script   "${baseDir}/bin/reads_apply_prune_ids.pl"
		BLAST_FILTER_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_blast_filter_stats.tsv"
		ASSIGNED_OTU_PRESERVE_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_assigned_otu_preserve_stats.tsv"
		ROLLING_POOL_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_rolling_pool_stats.tsv"
		ROUND_PRUNE_APPLY_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_round_prune_apply.tsv"
		BLAST_PRESSURE_STATS="${ongoingStateDir}/${round_barcode}/${barcode}_blast_pressure_stats.tsv"
		read_kv_stat() {
			local file="\$1"
			local key="\$2"
			awk -F'\t' -v k="\$key" '\$1==k{print \$2; exit}' "\$file" 2>/dev/null || true
		}
		normalize_int_stat() {
			local value="\$1"
			if [ -z "\$value" ]; then
				printf '%s\n' "0"
				return
			fi
			if [ "\${value#-}" != "\$value" ]; then
				value_body="\${value#-}"
				[ -n "\$value_body" ] || { printf '%s\n' "0"; return; }
			else
				value_body="\$value"
			fi
			case "\$value_body" in
				*[!0-9]*)
					printf '%s\n' "0"
					;;
				*)
					printf '%s\n' "\$value"
					;;
			esac
		}
		filtered_reads_kept=\$(read_kv_stat "\$BLAST_FILTER_STATS" kept_reads)
		[ -n "\$filtered_reads_kept" ] || filtered_reads_kept=\$(read_kv_stat "\$BLAST_FILTER_STATS" kept_reads_after_filter)
		filtered_reads_kept=\$(normalize_int_stat "\$filtered_reads_kept")
		filtered_reads_dropped=\$(read_kv_stat "\$BLAST_FILTER_STATS" dropped_reads)
		[ -n "\$filtered_reads_dropped" ] || filtered_reads_dropped=\$(read_kv_stat "\$BLAST_FILTER_STATS" dropped_reads_after_filter)
		filtered_reads_dropped=\$(normalize_int_stat "\$filtered_reads_dropped")
		protected_reads_added=\$(read_kv_stat "\$ASSIGNED_OTU_PRESERVE_STATS" protected_reads_added_to_blast_input)
		protected_reads_added=\$(normalize_int_stat "\$protected_reads_added")
		protected_read_ids_ever_preblast=\$(read_kv_stat "\$ASSIGNED_OTU_PRESERVE_STATS" protected_read_ids_ever_count_preblast)
		protected_read_ids_ever_preblast=\$(normalize_int_stat "\$protected_read_ids_ever_preblast")
		hit_ids_appended=\$(read_kv_stat "\$ROLLING_POOL_STATS" hit_ids_appended)
		hit_ids_appended=\$(normalize_int_stat "\$hit_ids_appended")
		pool_before_dedup=\$(read_kv_stat "\$ROLLING_POOL_STATS" pool_before_dedup)
		pool_before_dedup=\$(normalize_int_stat "\$pool_before_dedup")
		pool_after_dedup=\$(read_kv_stat "\$ROLLING_POOL_STATS" pool_after_dedup)
		pool_after_dedup=\$(normalize_int_stat "\$pool_after_dedup")
		pool_after_prune=0
		if [ -f "${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" ]; then
			pool_after_prune=\$(grep -c '^>' "${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" || echo 0)
		fi
		pool_after_prune=\$(normalize_int_stat "\$pool_after_prune")
		reads_pruned_round=\$(read_kv_stat "\$ROUND_PRUNE_APPLY_STATS" reads_pruned)
		reads_pruned_round=\$(normalize_int_stat "\$reads_pruned_round")
		prune_ids_total_round=\$(read_kv_stat "\$ROUND_PRUNE_APPLY_STATS" prune_ids_total)
		prune_ids_total_round=\$(normalize_int_stat "\$prune_ids_total_round")
		prune_ids_matched_round=\$(read_kv_stat "\$ROUND_PRUNE_APPLY_STATS" prune_ids_matched)
		prune_ids_matched_round=\$(normalize_int_stat "\$prune_ids_matched_round")
		prev_pool_after=0
		if [ -s "${ongoingStateDir}/_state/${barcode}_blast_pressure_stats_last.tsv" ]; then
			prev_pool_after=\$(read_kv_stat "${ongoingStateDir}/_state/${barcode}_blast_pressure_stats_last.tsv" pool_after_prune)
			[ -n "\$prev_pool_after" ] || prev_pool_after=\$(read_kv_stat "${ongoingStateDir}/_state/${barcode}_blast_pressure_stats_last.tsv" pool_after_dedup)
		fi
		prev_pool_after=\$(normalize_int_stat "\$prev_pool_after")
		net_pool_delta_vs_previous_round=\$(( pool_after_prune - prev_pool_after ))
			BLAST_PRESSURE_TMP="\${BLAST_PRESSURE_STATS}.tmp"
			blast_pressure_publish_ok=0
			rm -f "\$BLAST_PRESSURE_STATS" "\$BLAST_PRESSURE_TMP"
			if ! {
				printf 'filtered_reads_kept\t%s\n' "\$filtered_reads_kept"
				printf 'filtered_reads_dropped\t%s\n' "\$filtered_reads_dropped"
				printf 'protected_reads_added_to_blast_input\t%s\n' "\$protected_reads_added"
				printf 'protected_read_ids_ever_count_preblast\t%s\n' "\$protected_read_ids_ever_preblast"
				printf 'hit_ids_appended\t%s\n' "\$hit_ids_appended"
			printf 'pool_before_dedup\t%s\n' "\$pool_before_dedup"
			printf 'pool_after_dedup\t%s\n' "\$pool_after_dedup"
				printf 'pool_after_prune\t%s\n' "\$pool_after_prune"
				printf 'reads_pruned\t%s\n' "\$reads_pruned_round"
				printf 'prune_ids_total\t%s\n' "\$prune_ids_total_round"
				printf 'prune_ids_matched\t%s\n' "\$prune_ids_matched_round"
				printf 'net_pool_delta_vs_previous_round\t%s\n' "\$net_pool_delta_vs_previous_round"
			} > "\$BLAST_PRESSURE_TMP" 2>/dev/null; then
				rm -f "\$BLAST_PRESSURE_TMP"
				echo "WARN: failed to write blast pressure stats sidecar for ${barcode}/${round_barcode}" 1>&2
			else
				if mv "\$BLAST_PRESSURE_TMP" "\$BLAST_PRESSURE_STATS" 2>/dev/null; then
					blast_pressure_publish_ok=1
				else
					rm -f "\$BLAST_PRESSURE_TMP"
				fi
				if [ "\$blast_pressure_publish_ok" -eq 1 ] && awk -F'\t' '
					BEGIN {
						need["filtered_reads_kept"]=1;
						need["pool_after_prune"]=1;
						need["reads_pruned"]=1;
						need["net_pool_delta_vs_previous_round"]=1;
					}
					NF >= 2 && (\$1 in need) { seen[\$1]=1 }
					END {
						for (k in need) if (!(k in seen)) exit 1;
						exit 0;
					}
				' "\$BLAST_PRESSURE_STATS" 2>/dev/null; then
					cp "\$BLAST_PRESSURE_STATS" "${ongoingStateDir}/_state/${barcode}_blast_pressure_stats_last.tsv" 2>/dev/null || true
				elif [ "\$blast_pressure_publish_ok" -eq 1 ]; then
					echo "WARN: blast pressure stats sidecar incomplete; skipping _state refresh for ${barcode}/${round_barcode}" 1>&2
				else
					echo "WARN: failed to publish blast pressure stats sidecar for ${barcode}/${round_barcode}" 1>&2
				fi
			fi
			if ! filter_ids_present_in_fasta \
			"\$ASSIGNED_READ_IDS_EVER_STATE" \
			"${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" \
			"\$ROUND_DIR/${barcode}_assigned_read_ids.list"; then
			echo "ERROR: failed to finalize sticky assigned read snapshot from final pool" 1>&2
			exit 1
		fi
		if ! filter_ids_present_in_fasta \
			"\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list" \
			"${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" \
			"\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list.final"; then
			echo "ERROR: failed to finalize next-round grace assigned-OTU member snapshot from final pool" 1>&2
			exit 1
		fi
		mv "\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list.final" "\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list"
		if ! filter_ids_present_in_fasta \
			"\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list" \
			"${ongoingStateDir}/_state/qced_reads_hq_accumulated.fasta" \
			"\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list.final"; then
			echo "ERROR: failed to finalize next-round grace consensus-assigned member snapshot from final pool" 1>&2
			exit 1
		fi
		mv "\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list.final" "\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list"
		if ! replace_read_ids_state \
			"\$ROUND_DIR/${barcode}_assigned_otu_member_ids_raw_current.list" \
			"\$ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE"; then
			echo "ERROR: failed to persist next-round grace assigned-OTU member reads" 1>&2
			exit 1
		fi
		if ! replace_read_ids_state \
			"\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_raw_current.list" \
			"\$CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE"; then
			echo "ERROR: failed to persist next-round grace consensus-assigned member reads" 1>&2
			exit 1
		fi
		refresh_protected_read_ids_ever "\$PROTECTED_READ_IDS_EVER_STATE"
		: > "\$ROUND_DIR/${barcode}_protected_read_ids_round.list"
		if [ -s "\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list" ]; then
			cat "\$ROUND_DIR/${barcode}_assigned_otu_member_ids_ever.list" >> "\$ROUND_DIR/${barcode}_protected_read_ids_round.list"
		fi
		if [ -s "\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list" ]; then
			cat "\$ROUND_DIR/${barcode}_consensus_assigned_member_ids_ever.list" >> "\$ROUND_DIR/${barcode}_protected_read_ids_round.list"
		fi
		if [ -s "\$ROUND_DIR/${barcode}_protected_read_ids_round.list" ]; then
			LC_ALL=C sort -u -o "\$ROUND_DIR/${barcode}_protected_read_ids_round.list" "\$ROUND_DIR/${barcode}_protected_read_ids_round.list"
		fi
		_t_consensus_persist_end=\$(date +%s)
		append_process_timing "persist_and_prune" "\$_t_consensus_persist_start" "\$_t_consensus_persist_end"
		# B1: clean up taxonomy and BLAST temp files from work directory
		rm -f tmp_idx.tsv tmp_tax.tsv tmp_tax_cols.tsv tmp_taxids_numeric.tsv tmp_taxids_unique.txt \
			tmp_tax_map.tsv tmp_taxids_cached.txt tmp_taxids_missing.txt tmp_tax_map_missing.tsv \
			tmp_assign_levels.tsv tmp_assign_levels_uniq.tsv tmp_tax_join.tsv consensus_sample_mode.tsv
		rm -f ${barcode}_preblast_cached*.txt ${barcode}_preblast_new*.fasta \
			${barcode}_preblast_new*.txt ${barcode}_preblast_hash_new*.tsv \
			${barcode}_preblastreport[0-9]*.txt ${barcode}_COI.fasta ${barcode}_ITS2.fasta
		"""
}

// ============================================================
// STAGE H — REPORTING (_reporting_consensus_tax)
// ============================================================
process _reporting_consensus_tax {
  maxForks maxForksReportingVal
  input:
    tuple val(barcode), val(round_barcode), file(blast_read), file(blast_report_consensus), file(consensus_round_provenance) from report_consensus
  output:
	tuple val(barcode), val(round_barcode), file("${barcode}_blast_consensus_tax_rpt.txt"), file("consensus_round_provenance.tsv") into cons_rpt_summary
    file("${barcode}_blast_consensus_tax_rpt.txt")
	
    script:

		"""
		set -euo pipefail
		shopt -s nullglob
		export LC_ALL=C
		RESTART_TOKEN="${restartTokenForCache}"
		
		#	perl ${baseDir}/bin/reporting_blast_consensus.pl ${blast_read} ${blast_report_consensus} ${barcode} $baseDir/${params.metazoa_spc_basics} $baseDir/${params.viridiplantae_spc_basics} $baseDir/${params.local_metazoa_gns} $baseDir/${params.local_viridiplantae_gns}
			CONS_IDS_ROUND="${ongoingStateDir}/${round_barcode}/${barcode}_consensus_consolidated_ids.txt"
			CONS_IDS_STATE="${ongoingStateDir}/_state/${barcode}_consensus_consolidated_ids.txt"
			CONS_IDS_CANON="${ongoingStateDir}/Consensus/consolidated_consensus_ids.txt"
			CONS_IDS=""
			if [ -s "\$CONS_IDS_ROUND" ]; then
				CONS_IDS="\$CONS_IDS_ROUND"
			elif [ -s "\$CONS_IDS_STATE" ]; then
				CONS_IDS="\$CONS_IDS_STATE"
				mkdir -p ${ongoingStateDir}/${round_barcode}
				cp "\$CONS_IDS_STATE" "\$CONS_IDS_ROUND" 2>/dev/null || true
			elif [ -s "\$CONS_IDS_CANON" ]; then
				CONS_IDS="\$CONS_IDS_CANON"
				mkdir -p ${ongoingStateDir}/${round_barcode}
				cp "\$CONS_IDS_CANON" "\$CONS_IDS_ROUND" 2>/dev/null || true
				cp "\$CONS_IDS_CANON" "\$CONS_IDS_STATE" 2>/dev/null || true
			fi
			if [ -n "\$CONS_IDS" ]; then
				perl ${baseDir}/bin/reporting_blast_consensus.pl ${blast_read} ${blast_report_consensus} ${barcode} "\$CONS_IDS"
			else
				perl ${baseDir}/bin/reporting_blast_consensus.pl ${blast_read} ${blast_report_consensus} ${barcode}
			fi
			if [ -s "${consensus_round_provenance}" ]; then
				if [ "${consensus_round_provenance}" != "consensus_round_provenance.tsv" ]; then
					cp "${consensus_round_provenance}" consensus_round_provenance.tsv
				fi
			else
				printf 'round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n' > consensus_round_provenance.tsv
			fi
			# Ensure consolidated report is always refreshed per round to avoid stale carry-over.
			if [ ! -f ${barcode}_blast_consensus_tax_consolidated_rpt.txt ]; then
				printf 'consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\ttaxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n' > ${barcode}_blast_consensus_tax_consolidated_rpt.txt
			fi
	if cp ${barcode}_blast_consensus_tax_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_blast_consensus_tax_rpt.txt
	then
		echo "Copying ${barcode}_blast_consensus_tax_rpt.txt" 1>&2
	fi
	cp ${barcode}_blast_consensus_tax_consolidated_rpt.txt ${ongoingStateDir}/${round_barcode}/${barcode}_blast_consensus_tax_consolidated_rpt.txt 2>/dev/null || true
	cp ${barcode}_blast_consensus_tax_consolidated_rpt.txt ${ongoingStateDir}/_state/${barcode}_blast_consensus_tax_consolidated_rpt.txt 2>/dev/null || true
		"""	
}

reports_blast = ChannelUtils.strictRoundJoin(blast_agg_ch, cons_agg_ch, 'reports_blast')

getting_run_summary_inputs = ChannelUtils.strictRoundJoinAll([
	blst_rpt_summary,
	cons_rpt_summary,
	otu_def_rpt_summary,
	demult_rpt_summary,
	target_rpt_summary,
	reports_blast,
], 'getting_run_summary_inputs')
getting_run_summary_with_path = ChannelUtils.strictRoundJoin(getting_run_summary_inputs, get_summary_ch)


// ============================================================
// STAGE I — RUN SUMMARY (getting_run_summary)
// ============================================================
process getting_run_summary {
    maxForks maxForksReportingVal
    publishDir "${ongoingResultsStateDir}/", mode: 'copy', overwrite: true
	input:
	tuple val(barcode), val(round_barcode), file(blast_otu_pretax_rpt), file(read_info_rpt), file(blast_otu_noadapter_rpt), file(blast_filter_stats), file(blast_consensus_tax), file(consensus_round_provenance), file(otu_def_rpt), file(otu_members_round), file(otu_sizes_round), file(demult_rpt), file(on_target_rpt), file(summary), file(summary_otu), val(read_path) from getting_run_summary_with_path
    output:
		tuple val(barcode), val(round_barcode) into complete_round_ch	
		/*file("${barcode}_read_info_rpt.txt")
		file("${barcode}_on_target_rpt.txt")
		file("${barcode}_blast_otu_pretax_rpt.txt")
		file("${barcode}_blast_consensus_tax_rpt.txt")
			file("${barcode}_otu_tax_gns_COI_treemap.png")
			file("${barcode}_otu_tax_gns_ITS_treemap.png")
			file("${barcode}_consensus_tax_spc_COI_treemap.png")
			file("${barcode}_consensus_tax_spc_ITS_treemap.png")
		file("${barcode}_reads_per_barcode.png")
		file("${barcode}_reads_per_sample_log.png")*/

		    def demuxModeValue = params.demultiplex_mode?.toString()?.toLowerCase()
		    def generateDemuxReports = demuxEnabledInt
		    // Optional "species/genus of interest" files. Keep args stable (always pass 4),
		    // but avoid passing "$baseDir/" when the param is empty/null.
		    def metazoaBasicsParam = params.metazoa_spc_basics?.toString()
		    def viridiplantaeBasicsParam = params.viridiplantae_spc_basics?.toString()
		    def localMetazoaGnsParam = params.local_metazoa_gns?.toString()
		    def localViridiplantaeGnsParam = params.local_viridiplantae_gns?.toString()
		    def metazoaBasicsArg = (metazoaBasicsParam && metazoaBasicsParam != 'null') ? "${baseDir}/${metazoaBasicsParam}" : ''
		    def viridiplantaeBasicsArg = (viridiplantaeBasicsParam && viridiplantaeBasicsParam != 'null') ? "${baseDir}/${viridiplantaeBasicsParam}" : ''
		    def localMetazoaGnsArg = (localMetazoaGnsParam && localMetazoaGnsParam != 'null') ? "${baseDir}/${localMetazoaGnsParam}" : ''
		    def localViridiplantaeGnsArg = (localViridiplantaeGnsParam && localViridiplantaeGnsParam != 'null') ? "${baseDir}/${localViridiplantaeGnsParam}" : ''
		    // Build run config lines for reproducibility record
		    def runConfigLines = params
		        .sort { a, b -> a.key <=> b.key }
		        .collect { k, v ->
		            def fmtVal = (v == null) ? 'null'
		                       : (v instanceof Boolean || v instanceof Number) ? v.toString()
		                       : '"' + v.toString().replace('\\', '\\\\').replace('"', '\\"') + '"'
		            "  ${k.padRight(35)} = ${fmtVal}"
		        }
		        .join('\n')

	    script:
		"""
		set -euo pipefail
	shopt -s nullglob
	export LC_ALL=C
	RESTART_TOKEN="${restartTokenForCache}"
		DEMUX_MODE="${demuxModeValue ?: ''}"
		GENERATE_DEMUX_REPORTS=${generateDemuxReports}
	HTML_REPORT_ENABLED=${htmlReportEnabled ? 1 : 0}
	HTML_REPORT_AUTO_REFRESH=${htmlReportAutoRefresh ? 1 : 0}
	HTML_REPORT_REFRESH_SECONDS=${htmlReportRefreshSecondsStr}
	HTML_REPORT_URL_PREFIX="${htmlReportUrlPrefix}"
	HTML_REPORT_SAMPLE_PLOT_MAX=${htmlReportSamplePlotMaxStr}
	file_sig() {
		local f="\$1"
		if [ -f "\$f" ]; then
			stat -c '%s:%Y' "\$f" 2>/dev/null || stat -f '%z:%m' "\$f" 2>/dev/null || echo "missing"
		else
			echo "missing"
		fi
	}
	try_asset_copy() {
		local src="\$1"
		local dst="\$2"
		local rc=0
		if cp "\$src" "\$dst"; then
			return 0
		else
			rc=\$?
		fi
		if [ "\$rc" -eq 28 ]; then
			echo "WARN: plot asset copy failed with exit 28 (likely ENOSPC): \$src -> \$dst" 1>&2
		else
			echo "WARN: plot asset copy failed (rc=\$rc): \$src -> \$dst" 1>&2
		fi
		return 1
	}
	copy_plot_outputs_and_commit() {
		local plot_sig="\$1"
		local plot_key="\$2"
		shift 2
		local copy_ok=1
		local src=""
		local dst=""
		while [ "\$#" -ge 2 ]; do
			src="\$1"
			dst="\$2"
			shift 2
			if ! try_asset_copy "\$src" "\$dst"; then
				copy_ok=0
			fi
		done
		if [ "\$#" -ne 0 ]; then
			echo "WARN: copy_plot_outputs_and_commit received an unmatched copy argument" 1>&2
			copy_ok=0
		fi
		if [ "\$copy_ok" -eq 1 ]; then
			bash ${baseDir}/bin/plot_sig.sh commit "\$plot_sig" "\$plot_key"
			return 0
		fi
		echo "WARN: skipping plot signature commit because one or more plot outputs were not copied" 1>&2
		return 1
	}
	run_plot_with_sig() {
		RUN_PLOT_WITH_SIG_RSCRIPT_OK=0
		local plot_sig="\$1"
		local plot_key="\$2"
		local run_msg="\$3"
		local skip_msg="\$4"
		local success_msg="\$5"
		shift 5
		local current_section=""
		local token=""
		local seen_check=0
		local seen_rscript=0
		local seen_copy=0
		local check_outputs=()
		local rscript_items=()
		local copy_args=()
		local rscript_path=""
		local rscript_args=()
		local i=0
		while [ "\$#" -gt 0 ]; do
			token="\$1"
			shift
			case "\$token" in
				--check)
					if [ "\$seen_check" -eq 1 ]; then
						echo "ERROR: run_plot_with_sig duplicate --check marker" 1>&2
						exit 1
					fi
					if [ "\$seen_rscript" -eq 1 ] || [ "\$seen_copy" -eq 1 ]; then
						echo "ERROR: run_plot_with_sig marker out of order: --check" 1>&2
						exit 1
					fi
					seen_check=1
					current_section="check"
					;;
				--rscript)
					if [ "\$seen_check" -ne 1 ]; then
						echo "ERROR: run_plot_with_sig missing --check before --rscript" 1>&2
						exit 1
					fi
					if [ "\$seen_rscript" -eq 1 ]; then
						echo "ERROR: run_plot_with_sig duplicate --rscript marker" 1>&2
						exit 1
					fi
					if [ "\$seen_copy" -eq 1 ]; then
						echo "ERROR: run_plot_with_sig marker out of order: --rscript" 1>&2
						exit 1
					fi
					seen_rscript=1
					current_section="rscript"
					;;
				--copy)
					if [ "\$seen_check" -ne 1 ] || [ "\$seen_rscript" -ne 1 ]; then
						echo "ERROR: run_plot_with_sig missing earlier sections before --copy" 1>&2
						exit 1
					fi
					if [ "\$seen_copy" -eq 1 ]; then
						echo "ERROR: run_plot_with_sig duplicate --copy marker" 1>&2
						exit 1
					fi
					seen_copy=1
					current_section="copy"
					;;
				--*)
					echo "ERROR: run_plot_with_sig invalid marker '\$token'" 1>&2
					exit 1
					;;
				*)
					case "\$current_section" in
						check)
							check_outputs+=("\$token")
							;;
						rscript)
							rscript_items+=("\$token")
							;;
						copy)
							copy_args+=("\$token")
							;;
						*)
							echo "ERROR: run_plot_with_sig payload before --check marker" 1>&2
							exit 1
							;;
					esac
					;;
			esac
		done
		if [ "\$seen_check" -ne 1 ]; then
			echo "ERROR: run_plot_with_sig missing --check marker" 1>&2
			exit 1
		fi
		if [ "\$seen_rscript" -ne 1 ]; then
			echo "ERROR: run_plot_with_sig missing --rscript marker" 1>&2
			exit 1
		fi
		if [ "\$seen_copy" -ne 1 ]; then
			echo "ERROR: run_plot_with_sig missing --copy marker" 1>&2
			exit 1
		fi
		if [ "\${#check_outputs[@]}" -eq 0 ]; then
			echo "ERROR: run_plot_with_sig empty --check section" 1>&2
			exit 1
		fi
		if [ "\${#rscript_items[@]}" -eq 0 ]; then
			echo "ERROR: run_plot_with_sig missing R script path after --rscript" 1>&2
			exit 1
		fi
		rscript_path="\${rscript_items[0]}"
		if [ -z "\$rscript_path" ]; then
			echo "ERROR: run_plot_with_sig missing R script path after --rscript" 1>&2
			exit 1
		fi
		i=1
		while [ "\$i" -lt "\${#rscript_items[@]}" ]; do
			rscript_args+=("\${rscript_items[\$i]}")
			i=\$((i + 1))
		done
		if [ "\${#copy_args[@]}" -eq 0 ]; then
			echo "ERROR: run_plot_with_sig empty --copy section" 1>&2
			exit 1
		fi
		if [ \$((\${#copy_args[@]} % 2)) -ne 0 ]; then
			echo "ERROR: run_plot_with_sig odd number of --copy arguments" 1>&2
			exit 1
		fi
		if bash ${baseDir}/bin/plot_sig.sh check "\$plot_sig" "\$plot_key" "\${check_outputs[@]}"; then
			echo "\$run_msg" 1>&2
			if Rscript "\$rscript_path" "\${rscript_args[@]}"; then
				RUN_PLOT_WITH_SIG_RSCRIPT_OK=1
				if copy_plot_outputs_and_commit "\$plot_sig" "\$plot_key" "\${copy_args[@]}"; then
					if [ -n "\$success_msg" ]; then
						echo "\$success_msg" 1>&2
					fi
				fi
			fi
		else
			echo "\$skip_msg" 1>&2
		fi
		return 0
	}
	PLOT_SIG_DIR="${ongoingStateDir}/_state/plot_sigs"
	mkdir -p "\$PLOT_SIG_DIR"
		# -- §2: Rolling report + metadata ledger update (append_reports.pl) --
		READ_PATH="${read_path}"
		# Use a per-state ledger inside the state dir for plotting/reporting inputs.
		# Do NOT read from the persistent metadata ledger because it can contain rows
		# from previous independent runs and will skew the x-axis (e.g. appears to start
		# many hours before the current run).
		LEDGER_PATH="${ongoingStateDir}/_state/${barcode}_reads_time_rpt.txt"
		METADATA_LEDGER="${podBaseDir}/metadata/${barcode}_reads_time_rpt.txt"
		if [ ! -f "\$LEDGER_PATH" ]; then
			mkdir -p "\$(dirname \"\$LEDGER_PATH\")"
			printf "run_id\ttime\tdata\treads\n" > "\$LEDGER_PATH"
			# Best-effort restore for the same state only: if restart_mode=restore and the
			# metadata ledger exists, seed the state ledger from it.
			if [ "${params.restart_mode}" = "restore" ] && [ -f "\$METADATA_LEDGER" ]; then
				cp -f "\$METADATA_LEDGER" "\$LEDGER_PATH" || true
			fi
		fi
			
			# Append rolling summaries. Optional species/genus pre-classification is used (when provided)
			# to restrict which taxa are shown in tables/plots. `min_reads_sample` controls the display threshold.
				append_rc=0
				set +e
					perl ${baseDir}/bin/append_reports.pl ${round_barcode} ${ongoingStateDir}/_state/ "\$READ_PATH" ${barcode} "\$LEDGER_PATH" ${params.min_reads_sample} "${metazoaBasicsArg}" "${viridiplantaeBasicsArg}" "${localMetazoaGnsArg}" "${localViridiplantaeGnsArg}" "${ongoingStateDir}/_state/run_started_utc.txt"
				append_rc=\$?
				set -e
				# ENOSPC ("No space left on device") can manifest as exit 28; treat it as non-fatal so
				# snapshot/backup steps can still run and preserve already-computed results.
					if [ "\$append_rc" -ne 0 ]; then
						if [ "\$append_rc" -eq 28 ]; then
							echo "WARN: append_reports.pl failed with exit 28 (likely ENOSPC). Continuing." 1>&2
						else
							echo "ERROR: append_reports.pl failed with exit \$append_rc" 1>&2
							exit "\$append_rc"
						fi
					fi

				# Update the persistent metadata ledger for external consumption.
				mkdir -p "\$(dirname \"\$METADATA_LEDGER\")"
				cp -f "\$LEDGER_PATH" "\$METADATA_LEDGER" 2>/dev/null || true
		# -- §3: R plot generation (time-series, treemaps, circle trees) --
		_plot_key="Time_reads|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_reads_time_rpt.txt\")"
		_plot_sig="\$PLOT_SIG_DIR/Time_reads.sig"
		run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
			"INFO: running plot Time_reads.R" \
			"INFO: skipping plot Time_reads.R (signature unchanged)" \
			"Creating production Vs time plot" \
			--check \
			"${ongoingStateDir}/_state/${barcode}_reads_time.png" \
			"${ongoingStateDir}/_state/${barcode}_reads_time.pdf" \
			--rscript \
			"${baseDir}/bin/Time_reads.R" \
			"${ongoingStateDir}/_state/${barcode}_reads_time_rpt.txt" \
			--copy \
			"${barcode}_reads_time.png" "${ongoingStateDir}/_state/${barcode}_reads_time.png" \
			"${barcode}_reads_time.pdf" "${ongoingStateDir}/_state/${barcode}_reads_time.pdf"
	if [ -f ${ongoingStateDir}/_state/${barcode}_reads_cumulative_rpt.txt ];
	then
		_plot_key="Time_reads_cumulative|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_reads_cumulative_rpt.txt\")"
		_plot_sig="\$PLOT_SIG_DIR/Time_reads_cumulative.sig"
		run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
			"INFO: running plot Time_reads_cumulative.R" \
			"INFO: skipping plot Time_reads_cumulative.R (signature unchanged)" \
			"Creating cumulative reads plot" \
			--check \
			"${ongoingStateDir}/_state/${barcode}_reads_cumulative_log.png" \
			"${ongoingStateDir}/_state/${barcode}_reads_cumulative_log.pdf" \
			--rscript \
			"${baseDir}/bin/Time_reads_cumulative.R" \
			"${ongoingStateDir}/_state/${barcode}_reads_cumulative_rpt.txt" \
			--copy \
			"${barcode}_reads_cumulative_log.png" "${ongoingStateDir}/_state/${barcode}_reads_cumulative_log.png" \
			"${barcode}_reads_cumulative_log.pdf" "${ongoingStateDir}/_state/${barcode}_reads_cumulative_log.pdf"
	fi
		_plot_key="Treemap_abundance_species|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_metazoa_treemap_rpt.txt\")|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_viridiplantae_treemap_rpt.txt\")"
		_plot_sig="\$PLOT_SIG_DIR/Treemap_abundance_species.sig"
		run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
			"INFO: running plot Treemap_abundance_species.R" \
			"INFO: skipping plot Treemap_abundance_species.R (signature unchanged)" \
			"Creating otu_tax Vs time plot" \
			--check \
			"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_COI_treemap.png" \
			"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_ITS_treemap.png" \
			"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_COI_treemap.pdf" \
			"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_ITS_treemap.pdf" \
			--rscript \
			"${baseDir}/bin/Treemap_abundance_species.R" \
			"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_metazoa_treemap_rpt.txt" \
			"${ongoingStateDir}/_state/${barcode}_otu_tax_spc_viridiplantae_treemap_rpt.txt" \
			--copy \
			"${barcode}_otu_tax_spc_COI_treemap.png" "${ongoingStateDir}/_state/${barcode}_otu_tax_spc_COI_treemap.png" \
			"${barcode}_otu_tax_spc_ITS_treemap.png" "${ongoingStateDir}/_state/${barcode}_otu_tax_spc_ITS_treemap.png" \
			"${barcode}_otu_tax_spc_COI_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_otu_tax_spc_COI_treemap.pdf" \
			"${barcode}_otu_tax_spc_ITS_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_otu_tax_spc_ITS_treemap.pdf"
			_plot_key="Treemap_abundance_genus|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_metazoa_treemap_rpt.txt\")|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_viridiplantae_treemap_rpt.txt\")"
			_plot_sig="\$PLOT_SIG_DIR/Treemap_abundance_genus.sig"
			run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
				"INFO: running plot Treemap_abundance_genus.R" \
				"INFO: skipping plot Treemap_abundance_genus.R (signature unchanged)" \
				"Creating otu_tax Vs time plot" \
				--check \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_COI_treemap.png" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_ITS_treemap.png" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_COI_treemap.pdf" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_ITS_treemap.pdf" \
				--rscript \
				"${baseDir}/bin/Treemap_abundance_genus.R" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_metazoa_treemap_rpt.txt" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_gns_viridiplantae_treemap_rpt.txt" \
				--copy \
				"${barcode}_otu_tax_gns_COI_treemap.png" "${ongoingStateDir}/_state/${barcode}_otu_tax_gns_COI_treemap.png" \
				"${barcode}_otu_tax_gns_ITS_treemap.png" "${ongoingStateDir}/_state/${barcode}_otu_tax_gns_ITS_treemap.png" \
				"${barcode}_otu_tax_gns_COI_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_otu_tax_gns_COI_treemap.pdf" \
				"${barcode}_otu_tax_gns_ITS_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_otu_tax_gns_ITS_treemap.pdf"
			perl ${baseDir}/bin/assignments_circle_tree_prep.pl \
				--mode otu \
				--blast "${blast_otu_pretax_rpt}" \
				--otu-sizes-round "${otu_sizes_round}" \
				--marker COI \
				--top 200 \
				--out ${barcode}_otu_circle_tree_COI.tsv
			_plot_key="CircleTree_otu_COI|\$(file_sig \"${barcode}_otu_circle_tree_COI.tsv\")"
			_plot_sig="\$PLOT_SIG_DIR/CircleTree_otu_COI.sig"
			if bash ${baseDir}/bin/plot_sig.sh check "\$_plot_sig" "\$_plot_key" "${ongoingStateDir}/_state/${barcode}_otu_circle_tree_COI.png"; then
				echo "INFO: running plot TreeFan_cladogram.R (OTU COI)" 1>&2
				if Rscript ${baseDir}/bin/TreeFan_cladogram.R ${barcode}_otu_circle_tree_COI.tsv ${barcode}_otu_circle_tree_COI.png "OTU Fan Cladogram (COI)" "Weight: reads in OTUs";
				then
					copy_plot_outputs_and_commit "\$_plot_sig" "\$_plot_key" \
						${barcode}_otu_circle_tree_COI.png ${ongoingStateDir}/_state/${barcode}_otu_circle_tree_COI.png
				fi
			else
				echo "INFO: skipping plot TreeFan_cladogram.R (OTU COI, signature unchanged)" 1>&2
			fi
			perl ${baseDir}/bin/assignments_circle_tree_prep.pl \
				--mode otu \
				--blast "${blast_otu_pretax_rpt}" \
				--otu-sizes-round "${otu_sizes_round}" \
				--marker ITS2 \
				--top 200 \
				--out ${barcode}_otu_circle_tree_ITS.tsv
			_plot_key="CircleTree_otu_ITS|\$(file_sig \"${barcode}_otu_circle_tree_ITS.tsv\")"
			_plot_sig="\$PLOT_SIG_DIR/CircleTree_otu_ITS.sig"
			if bash ${baseDir}/bin/plot_sig.sh check "\$_plot_sig" "\$_plot_key" "${ongoingStateDir}/_state/${barcode}_otu_circle_tree_ITS.png"; then
				echo "INFO: running plot TreeFan_cladogram.R (OTU ITS2)" 1>&2
				if Rscript ${baseDir}/bin/TreeFan_cladogram.R ${barcode}_otu_circle_tree_ITS.tsv ${barcode}_otu_circle_tree_ITS.png "OTU Fan Cladogram (ITS2)" "Weight: reads in OTUs";
				then
					copy_plot_outputs_and_commit "\$_plot_sig" "\$_plot_key" \
						${barcode}_otu_circle_tree_ITS.png ${ongoingStateDir}/_state/${barcode}_otu_circle_tree_ITS.png
				fi
			else
				echo "INFO: skipping plot TreeFan_cladogram.R (OTU ITS2, signature unchanged)" 1>&2
			fi
			_plot_key="Time_taxonomy_otu|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_otu_tax_time_rpt.txt\")"
			_plot_sig="\$PLOT_SIG_DIR/Time_taxonomy_otu.sig"
			run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
				"INFO: running plot Time_taxonomy_otu.R" \
				"INFO: skipping plot Time_taxonomy_otu.R (signature unchanged)" \
				"Creating otu_tax Vs time plot" \
				--check \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_time.png" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_time.pdf" \
				--rscript \
				"${baseDir}/bin/Time_taxonomy_otu.R" \
				"${ongoingStateDir}/_state/${barcode}_otu_tax_time_rpt.txt" \
				--copy \
				"${barcode}_otu_tax_time.png" "${ongoingStateDir}/_state/${barcode}_otu_tax_time.png" \
				"${barcode}_otu_tax_time.pdf" "${ongoingStateDir}/_state/${barcode}_otu_tax_time.pdf"
			if [ "\$RUN_PLOT_WITH_SIG_RSCRIPT_OK" -eq 1 ]; then
				# Optional additional plot produced by Time_taxonomy_otu.R when basecalling-model rows exist.
				try_asset_copy ${barcode}_otu_tax_time_basecalling.png ${ongoingStateDir}/_state/${barcode}_otu_tax_time_basecalling.png || true
			fi
		_plot_key="Treemap_consensus_abundance_species|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_metazoa_treemap_rpt.txt\")|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_viridiplantae_treemap_rpt.txt\")"
		_plot_sig="\$PLOT_SIG_DIR/Treemap_consensus_abundance_species.sig"
		run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
			"INFO: running plot Treemap_consensus_abundance_species.R" \
			"INFO: skipping plot Treemap_consensus_abundance_species.R (signature unchanged)" \
			"Creating consensus_tax Vs time plot" \
			--check \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_COI_treemap.png" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_ITS_treemap.png" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_COI_treemap.pdf" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_ITS_treemap.pdf" \
			--rscript \
			"${baseDir}/bin/Treemap_consensus_abundance_species.R" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_metazoa_treemap_rpt.txt" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_viridiplantae_treemap_rpt.txt" \
			--copy \
			"${barcode}_consensus_tax_spc_COI_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_COI_treemap.png" \
			"${barcode}_consensus_tax_spc_ITS_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_ITS_treemap.png" \
			"${barcode}_consensus_tax_spc_COI_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_COI_treemap.pdf" \
			"${barcode}_consensus_tax_spc_ITS_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_tax_spc_ITS_treemap.pdf"
			perl ${baseDir}/bin/assignments_circle_tree_prep.pl \
				--mode consensus \
				--blast "${blast_consensus_tax}" \
				--marker COI \
				--top 200 \
				--out ${barcode}_consensus_circle_tree_COI.tsv
			_plot_key="CircleTree_consensus_COI|\$(file_sig \"${barcode}_consensus_circle_tree_COI.tsv\")"
			_plot_sig="\$PLOT_SIG_DIR/CircleTree_consensus_COI.sig"
			if bash ${baseDir}/bin/plot_sig.sh check "\$_plot_sig" "\$_plot_key" "${ongoingStateDir}/_state/${barcode}_consensus_circle_tree_COI.png"; then
				echo "INFO: running plot TreeFan_cladogram.R (Consensus COI)" 1>&2
				if Rscript ${baseDir}/bin/TreeFan_cladogram.R ${barcode}_consensus_circle_tree_COI.tsv ${barcode}_consensus_circle_tree_COI.png "Consensus Fan Cladogram (COI)" "Weight: consensus count";
				then
					copy_plot_outputs_and_commit "\$_plot_sig" "\$_plot_key" \
						${barcode}_consensus_circle_tree_COI.png ${ongoingStateDir}/_state/${barcode}_consensus_circle_tree_COI.png
				fi
			else
				echo "INFO: skipping plot TreeFan_cladogram.R (Consensus COI, signature unchanged)" 1>&2
			fi
			perl ${baseDir}/bin/assignments_circle_tree_prep.pl \
				--mode consensus \
				--blast "${blast_consensus_tax}" \
				--marker ITS2 \
				--top 200 \
				--out ${barcode}_consensus_circle_tree_ITS.tsv
			_plot_key="CircleTree_consensus_ITS|\$(file_sig \"${barcode}_consensus_circle_tree_ITS.tsv\")"
			_plot_sig="\$PLOT_SIG_DIR/CircleTree_consensus_ITS.sig"
			if bash ${baseDir}/bin/plot_sig.sh check "\$_plot_sig" "\$_plot_key" "${ongoingStateDir}/_state/${barcode}_consensus_circle_tree_ITS.png"; then
				echo "INFO: running plot TreeFan_cladogram.R (Consensus ITS2)" 1>&2
				if Rscript ${baseDir}/bin/TreeFan_cladogram.R ${barcode}_consensus_circle_tree_ITS.tsv ${barcode}_consensus_circle_tree_ITS.png "Consensus Fan Cladogram (ITS2)" "Weight: consensus count";
				then
					copy_plot_outputs_and_commit "\$_plot_sig" "\$_plot_key" \
						${barcode}_consensus_circle_tree_ITS.png ${ongoingStateDir}/_state/${barcode}_consensus_circle_tree_ITS.png
				fi
			else
				echo "INFO: skipping plot TreeFan_cladogram.R (Consensus ITS2, signature unchanged)" 1>&2
			fi
			if [ -f ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_metazoa_treemap_rpt.txt ] || [ -f ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_viridiplantae_treemap_rpt.txt ]; then
				_plot_key="Treemap_consensus_abundance_species_consolidated|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_metazoa_treemap_rpt.txt\")|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_viridiplantae_treemap_rpt.txt\")"
				_plot_sig="\$PLOT_SIG_DIR/Treemap_consensus_abundance_species_consolidated.sig"
			run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
				"INFO: running plot Treemap_consensus_abundance_species_consolidated.R" \
				"INFO: skipping plot Treemap_consensus_abundance_species_consolidated.R (signature unchanged)" \
				"Creating consolidated consensus species treemap plots" \
				--check \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_COI_treemap.png" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_ITS_treemap.png" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_COI_treemap.pdf" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_ITS_treemap.pdf" \
				--rscript \
				"${baseDir}/bin/Treemap_consensus_abundance_species_consolidated.R" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_metazoa_treemap_rpt.txt" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_viridiplantae_treemap_rpt.txt" \
				--copy \
				"${barcode}_consensus_consolidated_tax_spc_COI_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_COI_treemap.png" \
				"${barcode}_consensus_consolidated_tax_spc_ITS_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_ITS_treemap.png" \
				"${barcode}_consensus_consolidated_tax_spc_COI_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_COI_treemap.pdf" \
				"${barcode}_consensus_consolidated_tax_spc_ITS_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_spc_ITS_treemap.pdf"
		fi
		_plot_key="Treemap_consensus_abundance_genus|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_metazoa_treemap_rpt.txt\")|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_viridiplantae_treemap_rpt.txt\")"
		_plot_sig="\$PLOT_SIG_DIR/Treemap_consensus_abundance_genus.sig"
		run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
			"INFO: running plot Treemap_consensus_abundance_genus.R" \
			"INFO: skipping plot Treemap_consensus_abundance_genus.R (signature unchanged)" \
			"Creating consensus_tax Vs time plot" \
			--check \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_COI_treemap.png" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_ITS_treemap.png" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_COI_treemap.pdf" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_ITS_treemap.pdf" \
			--rscript \
			"${baseDir}/bin/Treemap_consensus_abundance_genus.R" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_metazoa_treemap_rpt.txt" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_viridiplantae_treemap_rpt.txt" \
			--copy \
			"${barcode}_consensus_tax_gns_COI_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_COI_treemap.png" \
			"${barcode}_consensus_tax_gns_ITS_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_ITS_treemap.png" \
			"${barcode}_consensus_tax_gns_COI_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_COI_treemap.pdf" \
			"${barcode}_consensus_tax_gns_ITS_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_tax_gns_ITS_treemap.pdf"
		if [ -f ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt ] || [ -f ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_viridiplantae_treemap_rpt.txt ]; then
			_plot_key="Treemap_consensus_abundance_genus_consolidated|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt\")|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_viridiplantae_treemap_rpt.txt\")"
			_plot_sig="\$PLOT_SIG_DIR/Treemap_consensus_abundance_genus_consolidated.sig"
			run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
				"INFO: running plot Treemap_consensus_abundance_genus_consolidated.R" \
				"INFO: skipping plot Treemap_consensus_abundance_genus_consolidated.R (signature unchanged)" \
				"Creating consolidated consensus genus treemap plots" \
				--check \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_COI_treemap.png" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_ITS_treemap.png" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_COI_treemap.pdf" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_ITS_treemap.pdf" \
				--rscript \
				"${baseDir}/bin/Treemap_consensus_abundance_genus_consolidated.R" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_viridiplantae_treemap_rpt.txt" \
				--copy \
				"${barcode}_consensus_consolidated_tax_gns_COI_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_COI_treemap.png" \
				"${barcode}_consensus_consolidated_tax_gns_ITS_treemap.png" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_ITS_treemap.png" \
				"${barcode}_consensus_consolidated_tax_gns_COI_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_COI_treemap.pdf" \
				"${barcode}_consensus_consolidated_tax_gns_ITS_treemap.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_gns_ITS_treemap.pdf"
		fi
		_plot_key="Time_taxonomy_consensus|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_tax_time_rpt.txt\")"
		_plot_sig="\$PLOT_SIG_DIR/Time_taxonomy_consensus.sig"
		run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
			"INFO: running plot Time_taxonomy_consensus.R" \
			"INFO: skipping plot Time_taxonomy_consensus.R (signature unchanged)" \
			"Creating otu_consensus Vs time plot" \
			--check \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_time.png" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_time.pdf" \
			--rscript \
			"${baseDir}/bin/Time_taxonomy_consensus.R" \
			"${ongoingStateDir}/_state/${barcode}_consensus_tax_time_rpt.txt" \
			--copy \
			"${barcode}_consensus_tax_time.png" "${ongoingStateDir}/_state/${barcode}_consensus_tax_time.png" \
			"${barcode}_consensus_tax_time.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_tax_time.pdf"
			if [ -f ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time_rpt.txt ] \
				&& awk 'NR>1{found=1; exit} END{exit !found}' ${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time_rpt.txt; then
			_plot_key="Time_taxonomy_consensus_consolidated|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time_rpt.txt\")"
			_plot_sig="\$PLOT_SIG_DIR/Time_taxonomy_consensus_consolidated.sig"
			run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
				"INFO: running plot Time_taxonomy_consensus_consolidated.R" \
				"INFO: skipping plot Time_taxonomy_consensus_consolidated.R (signature unchanged)" \
				"Creating consolidated consensus time plot" \
				--check \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time.png" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time.pdf" \
				--rscript \
				"${baseDir}/bin/Time_taxonomy_consensus_consolidated.R" \
				"${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time_rpt.txt" \
				--copy \
				"${barcode}_consensus_consolidated_tax_time.png" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time.png" \
				"${barcode}_consensus_consolidated_tax_time.pdf" "${ongoingStateDir}/_state/${barcode}_consensus_consolidated_tax_time.pdf"
		fi
		if [ -f ${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time_rpt.txt ] \
				&& awk 'NR>1{found=1; exit} END{exit !found}' ${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time_rpt.txt; then
			_plot_key="Time_taxonomy_otu_frozen|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time_rpt.txt\")"
			_plot_sig="\$PLOT_SIG_DIR/Time_taxonomy_otu_frozen.sig"
			run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
				"INFO: running plot Time_taxonomy_otu_frozen.R" \
				"INFO: skipping plot Time_taxonomy_otu_frozen.R (signature unchanged)" \
				"Creating frozen OTU time plot" \
				--check \
				"${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time.png" \
				"${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time.pdf" \
				--rscript \
				"${baseDir}/bin/Time_taxonomy_otu_frozen.R" \
				"${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time_rpt.txt" \
				--copy \
				"${barcode}_otu_frozen_tax_time.png" "${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time.png" \
				"${barcode}_otu_frozen_tax_time.pdf" "${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time.pdf"
		fi
			# -- §4: Demux-specific plots (Read_counts, Read_info_quality) --
			if [ "\$GENERATE_DEMUX_REPORTS" -eq 1 ]; then
				bash ${baseDir}/bin/demult_summary.sh ${barcode}_demult_rpt.txt ${barcode}
				_plot_key="Read_counts|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_summary_demult_rpt.txt\")"
				_plot_sig="\$PLOT_SIG_DIR/Read_counts.sig"
					run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
						"INFO: running plot Read_counts.R" \
						"INFO: skipping plot Read_counts.R (signature unchanged)" \
						"Creating demultiplex read count plots" \
						--check \
						"${ongoingStateDir}/_state/${barcode}_reads_per_barcode.png" \
						"${ongoingStateDir}/_state/${barcode}_reads_per_sample_log.png" \
						"${ongoingStateDir}/_state/${barcode}_reads_per_sample.png" \
						"${ongoingStateDir}/_state/${barcode}_reads_per_barcode.pdf" \
						"${ongoingStateDir}/_state/${barcode}_reads_per_sample_log.pdf" \
						"${ongoingStateDir}/_state/${barcode}_reads_per_sample.pdf" \
						--rscript \
						"${baseDir}/bin/Read_counts.R" \
						"${barcode}_summary_demult_rpt.txt" \
						--copy \
						"${barcode}_reads_per_barcode.png" "${ongoingStateDir}/_state/${barcode}_reads_per_barcode.png" \
						"${barcode}_reads_per_sample_log.png" "${ongoingStateDir}/_state/${barcode}_reads_per_sample_log.png" \
						"${barcode}_reads_per_sample.png" "${ongoingStateDir}/_state/${barcode}_reads_per_sample.png" \
					"${barcode}_reads_per_barcode.pdf" "${ongoingStateDir}/_state/${barcode}_reads_per_barcode.pdf" \
					"${barcode}_reads_per_sample_log.pdf" "${ongoingStateDir}/_state/${barcode}_reads_per_sample_log.pdf" \
					"${barcode}_reads_per_sample.pdf" "${ongoingStateDir}/_state/${barcode}_reads_per_sample.pdf"
				else
					echo "Skipping demultiplex-specific summaries (demultiplex_mode=\${DEMUX_MODE})" 1>&2
				fi
				if [ "\$GENERATE_DEMUX_REPORTS" -eq 1 ] && [ "\$HTML_REPORT_ENABLED" -eq 1 ] && [ "\$HTML_REPORT_SAMPLE_PLOT_MAX" -gt 0 ]; then
					SAMPLE_REPORT_ASSET_DIR="${params.outdir}/runs/${run_name}/report_assets/samples"
					mkdir -p "\$SAMPLE_REPORT_ASSET_DIR"
					if ! bash ${baseDir}/bin/report_sample_read_counts_plots.sh \
						--summary ${barcode}_summary_demult_rpt.txt \
						--out-dir "\$SAMPLE_REPORT_ASSET_DIR" \
						--max "\$HTML_REPORT_SAMPLE_PLOT_MAX" \
						--sig-dir "${params.outdir}/runs/${run_name}/report_assets/.private_signatures/read_counts"; then
						echo "WARN: sample Read_counts figure generation failed" 1>&2
					fi
				fi
				_plot_key="Read_info_quality|\$(file_sig \"${ongoingStateDir}/_state/${barcode}_read_info_rpt.txt\")"
				_plot_sig="\$PLOT_SIG_DIR/Read_info_quality.sig"
				run_plot_with_sig "\$_plot_sig" "\$_plot_key" \
					"INFO: running plot Read_info_quality.R" \
					"INFO: skipping plot Read_info_quality.R (signature unchanged)" \
					"Creating reads Vs time plot" \
					--check \
					"${ongoingStateDir}/_state/${barcode}_violin_quality_read_info.png" \
					"${ongoingStateDir}/_state/${barcode}_violin_length_read_info.png" \
					"${ongoingStateDir}/_state/${barcode}_density_read_info.png" \
					"${ongoingStateDir}/_state/${barcode}_violin_quality_read_info.pdf" \
					"${ongoingStateDir}/_state/${barcode}_violin_length_read_info.pdf" \
					"${ongoingStateDir}/_state/${barcode}_density_read_info.pdf" \
					--rscript \
					"${baseDir}/bin/Read_info_quality.R" \
					"${ongoingStateDir}/_state/${barcode}_read_info_rpt.txt" \
					--copy \
					"${barcode}_violin_quality_read_info.png" "${ongoingStateDir}/_state/${barcode}_violin_quality_read_info.png" \
					"${barcode}_violin_length_read_info.png" "${ongoingStateDir}/_state/${barcode}_violin_length_read_info.png" \
					"${barcode}_density_read_info.png" "${ongoingStateDir}/_state/${barcode}_density_read_info.png" \
				"${barcode}_violin_quality_read_info.pdf" "${ongoingStateDir}/_state/${barcode}_violin_quality_read_info.pdf" \
				"${barcode}_violin_length_read_info.pdf" "${ongoingStateDir}/_state/${barcode}_violin_length_read_info.pdf" \
				"${barcode}_density_read_info.pdf" "${ongoingStateDir}/_state/${barcode}_density_read_info.pdf"

		# -- §5: HTML report assembly (round JSON, history append, render) --
		# Incremental HTML report update (best-effort, never fails the round).
		# Serving is external by design; use bin/serve_report.sh if needed.
		ROUND_DIR="${ongoingStateDir}/${round_barcode}"
		STATE_DIR="${ongoingStateDir}/_state"
		ROUND_REPORT_JSON="\$ROUND_DIR/round_report.json"
		REPORT_HISTORY_JSONL="\$STATE_DIR/report_history.jsonl"
		REPORT_HISTORY_LOCK="\$STATE_DIR/.report_history.lock"
		REPORT_RENDER_LOCK="\$STATE_DIR/.report_render.lock"
		REPORT_HTML="${params.outdir}/report.html"
		REPORT_STATE_JSON="${params.outdir}/report_state.json"
		RUN_REPORT_JSON="\$ROUND_DIR/run_report.json"
		RUN_INDEX_JSONL="${params.outdir}/runs_index.jsonl"
		RUN_INDEX_LOCK="${params.outdir}/.runs_index.lock"
			RUN_REPORT_DIR="${params.outdir}/runs/${run_name}"
			RUN_REPORT_HTML="\$RUN_REPORT_DIR/report.html"
			RUN_REPORT_STATE="\$RUN_REPORT_DIR/report_state.json"
				RUN_REPORT_REL_PATH="runs/${run_name}/report.html"
				REPORT_FIG_LIST="${baseDir}/assets/report/figures.tsv"
			REPORT_SAMPLE_FIG_LIST="${baseDir}/assets/report/figures_sample.tsv"
			REPORT_ASSET_DIR="${params.outdir}/runs/${run_name}/report_assets"
			REPORT_SAMPLE_ASSET_DIR="\$REPORT_ASSET_DIR/samples"
			REPORT_ASSET_OUTDIR="${params.outdir}/runs/${run_name}"
			REPORT_FIG_EXISTS_DIR="${ongoingStateDir}/_state"
			REPORT_FIG_URL_PREFIX="runs/${run_name}/report_assets"
			REPORT_SAMPLE_FIG_URL_PREFIX="runs/${run_name}/report_assets/samples"
			ROUND_INDEX_FILE="\$STATE_DIR/round_index.tsv"
			ACTIVE_PRUNE_SIZE_STREAK_OUT="\$ROUND_DIR/active_prune_candidates_size_streak.list"
			ACTIVE_PRUNE_SIZE_CANDIDATES_OUT="\$ROUND_DIR/active_prune_candidates_size_candidates.list"
			ACTIVE_PRUNE_ALL_OUT="\$ROUND_DIR/active_prune_candidates_all.list"
			ACTIVE_PRUNE_COUNTS_OUT="\$ROUND_DIR/active_prune_candidates_counts.tsv"
			OTU_SIZE_STREAK_IDS_LAST="\$STATE_DIR/${barcode}_otu_size_streak_prune_ids_last.txt"
			mkdir -p "\$ROUND_DIR" "\$STATE_DIR" "${params.outdir}"

		set +e
			perl ${baseDir}/bin/active_prune_candidates.pl \
				--otu-members-round "${otu_members_round}" \
				--otu-sizes-round "${otu_sizes_round}" \
				--size-streak-ids "\$OTU_SIZE_STREAK_IDS_LAST" \
				--eligible-counts "\$ROUND_DIR/${barcode}_eligible_pool_counts.tsv" \
				--otu-blast-min-members ${otuBlastMinMembersStr} \
				--otu-blast-filter-mode "${otuBlastFilterModeCanonical}" \
				--otu-blast-filter-skip-rounds "${otuBlastFilterSkipRoundsCanonical}" \
				--force-use-filtered "${otuBlastForceUseFiltered ? 1 : 0}" \
				--round-index-file "\$ROUND_INDEX_FILE" \
				--round-barcode "${round_barcode}" \
				--effective-mode-helper "${baseDir}/bin/otu_blast_effective_mode.sh" \
				--out-size-streak "\$ACTIVE_PRUNE_SIZE_STREAK_OUT" \
				--out-size-candidates "\$ACTIVE_PRUNE_SIZE_CANDIDATES_OUT" \
				--out-all "\$ACTIVE_PRUNE_ALL_OUT" \
				--out-counts "\$ACTIVE_PRUNE_COUNTS_OUT"
			active_prune_rc=\$?
			if [ "\$active_prune_rc" -ne 0 ]; then
				echo "WARN: active_prune_candidates.pl failed (rc=\$active_prune_rc)" 1>&2
			fi
			perl ${baseDir}/bin/report_sync_figures.pl \
				--fig-list "\$REPORT_FIG_LIST" \
				--barcode "${barcode}" \
				--src-dir "${ongoingStateDir}/_state" \
				--out-dir "\$REPORT_ASSET_OUTDIR" \
				>/dev/null || true
				REPORT_SAMPLE_ROSTER_ARG=""
				if [ -s "${params.outdir}/sample_info/${run_name}/samples.txt" ]; then
					REPORT_SAMPLE_ROSTER_ARG="--sample-roster ${params.outdir}/sample_info/${run_name}/samples.txt"
				fi
				perl ${baseDir}/bin/report_round_json.pl \
				--run-id "${run_name}" \
				--state-id "${stateId}" \
				--barcode "${barcode}" \
				--round-barcode "${round_barcode}" \
				--targets "${params.targets}" \
				--schema-version "1.4" \
				--out "\$ROUND_REPORT_JSON" \
				--read-info "${read_info_rpt}" \
				--on-target "${on_target_rpt}" \
				--demult "${demult_rpt}" \
				--otu-def "${otu_def_rpt}" \
				--blast-otu "${blast_otu_pretax_rpt}" \
				--blast-otu-cumulative "${ongoingStateDir}/_state/${barcode}_blast_otu_pretax_rpt.txt" \
				--blast-noadapter "${blast_otu_noadapter_rpt}" \
				--otu-sizes-round "${otu_sizes_round}" \
				--blast-consensus "${blast_consensus_tax}" \
				--consensus-round-provenance "${consensus_round_provenance}" \
				--spec-basics-metazoa "${metazoaBasicsArg}" \
				--spec-basics-viridiplantae "${viridiplantaeBasicsArg}" \
				--summary "${summary}" \
				--summary-otu "${summary_otu}" \
			--otu-size-streak-stats "\$ROUND_DIR/${barcode}_otu_size_streak_stats.tsv" \
				--otu-size-streak "\$ROUND_DIR/${barcode}_otu_size_streak.tsv" \
			--otu-size-streak-mode "${otuSizeStreakModeCanonical}" \
			--otu-size-streak-min-rounds "${otuSizeStreakMinRoundsStr}" \
			--otu-lock-summary "\$ROUND_DIR/${barcode}_otu_lock_summary.tsv" \
			--active-prune-counts "\$ACTIVE_PRUNE_COUNTS_OUT" \
				--otu-blast-filter-stats "${blast_filter_stats}" \
				--blast-filter-dropped-ids "\$ROUND_DIR/${barcode}_blast_filter_dropped_read_ids.list" \
				--blast-filter-mode "${otuBlastFilterModeCanonical}" \
				--blast-id-family "${params.blast_id_family}" \
				--blast-id-genus "${params.blast_id_genus}" \
				--blast-id-spec "${params.blast_id_spec}" \
				--otu-blast-min-members "${otuBlastMinMembersStr}" \
				--otu-blast-filter-skip-rounds "${otuBlastFilterSkipRoundsCanonical}" \
			--otu-blast-unassigned-grace-rounds "${otuBlastUnassignedGraceRoundsStr}" \
			--round-index-file "\$ROUND_DIR/round_index.tsv" \
				--otu-members-blastdiag-stats "\$ROUND_DIR/otu_members_blastdiag_stats.tsv" \
					--consensus-consolidated-ids "\$ROUND_DIR/${barcode}_consensus_consolidated_ids.txt" \
				--blast-consensus-consolidated "${ongoingStateDir}/_state/${barcode}_blast_consensus_tax_consolidated_rpt.txt" \
					--debug-otu-out "\$ROUND_DIR/${barcode}_otu_assignment_debug.tsv" \
					--fig-list "\$REPORT_FIG_LIST" \
					--fig-dir "\$REPORT_FIG_EXISTS_DIR" \
					--fig-url-prefix "\$REPORT_FIG_URL_PREFIX" \
					--sample-fig-list "\$REPORT_SAMPLE_FIG_LIST" \
					\$REPORT_SAMPLE_ROSTER_ARG \
					--sample-fig-dir "\$REPORT_SAMPLE_ASSET_DIR" \
					--sample-fig-url-prefix "\$REPORT_SAMPLE_FIG_URL_PREFIX"
		round_json_rc=\$?
		if [ "\$round_json_rc" -ne 0 ]; then
			echo "WARN: report_round_json.pl failed (rc=\$round_json_rc)" 1>&2
		fi

		if [ "\$round_json_rc" -eq 0 ] && [ -s "\$ROUND_REPORT_JSON" ]; then
			python3 ${baseDir}/bin/extract_frozen_tax_time.py \
				--json "\$ROUND_REPORT_JSON" \
				--run-id "${barcode}" \
				--out "${ongoingStateDir}/_state/${barcode}_otu_frozen_tax_time_rpt.txt" || true
			LOCK_WAIT=${params.lock_wait_seconds} bash ${baseDir}/bin/report_history_append.sh "\$ROUND_REPORT_JSON" "\$REPORT_HISTORY_JSONL" "\$REPORT_HISTORY_LOCK"
			history_rc=\$?
			if [ "\$history_rc" -ne 0 ]; then
				echo "WARN: report_history_append.sh failed (rc=\$history_rc)" 1>&2
			fi
			if [ "\$history_rc" -eq 0 ]; then
				perl ${baseDir}/bin/report_run_json.pl \
					--history "\$REPORT_HISTORY_JSONL" \
					--out "\$RUN_REPORT_JSON" \
					--run-id "${run_name}" \
					--barcode "${barcode}" \
					--state-id "${stateId}" \
					--outdir "${params.outdir}" \
					--schema-version "1.4" \
					--report-rel-path "\$RUN_REPORT_REL_PATH" \
					--run-started-utc-file "${ongoingStateDir}/_state/run_started_utc.txt"
				run_json_rc=\$?
				if [ "\$run_json_rc" -ne 0 ]; then
					echo "WARN: report_run_json.pl failed (rc=\$run_json_rc)" 1>&2
				elif [ -s "\$RUN_REPORT_JSON" ]; then
					LOCK_WAIT=${params.lock_wait_seconds} bash ${baseDir}/bin/report_run_index_update.sh "\$RUN_REPORT_JSON" "\$RUN_INDEX_JSONL" "\$RUN_INDEX_LOCK"
					run_index_rc=\$?
					if [ "\$run_index_rc" -ne 0 ]; then
						echo "WARN: report_run_index_update.sh failed (rc=\$run_index_rc)" 1>&2
					fi
				fi
			fi
		else
			echo "WARN: skipping report history append (round report missing)" 1>&2
		fi

		if [ "\$HTML_REPORT_ENABLED" -eq 1 ]; then
			if command -v python3 >/dev/null 2>&1; then
				REPORT_RENDER_LOCK_DIR="\${REPORT_RENDER_LOCK}.lockdir"
				render_lock_acquired=0
				release_render_lock() {
					if [ "\${render_lock_acquired:-0}" -eq 1 ]; then
						rmdir "\$REPORT_RENDER_LOCK_DIR" 2>/dev/null || true
						render_lock_acquired=0
					fi
				}
				trap release_render_lock EXIT
					render_waited=0
					while true; do
						if mkdir "\$REPORT_RENDER_LOCK_DIR" 2>/dev/null; then
							render_lock_acquired=1
							break
						fi
						sleep 1
						render_waited=\$((render_waited + 1))
						if [ "\$render_waited" -ge ${params.lock_wait_seconds} ]; then
							echo "WARN: skipping HTML report render for round=${round_barcode}; lock timeout on \${REPORT_RENDER_LOCK}" 1>&2
							break
						fi
					done
					if [ "\$render_lock_acquired" -eq 1 ]; then
						mkdir -p "\$RUN_REPORT_DIR"
					# Write run config file (overwrite each round to keep in sync)
					RUN_CONF_FILE="${params.outdir}/${run_name}.conf"
					mkdir -p "\$(dirname "\$RUN_CONF_FILE")"
					cat > "\$RUN_CONF_FILE" << 'RTBCONF'
// Auto-generated run configuration: ${run_name}
// Command: ${workflow.commandLine}
params {
${runConfigLines}
}
RTBCONF
					# Write directory README.html files once (best-effort)
					_render_readme() {
						local dst="\$1" tmpl="\$2"
						[ -f "\$dst" ] && return
						mkdir -p "\$(dirname "\$dst")"
						sed "s|{{RUN_NAME}}|${run_name}|g; s|{{STATE_ID}}|${stateId}|g" "\$tmpl" > "\$dst"
					}
					_render_readme "${params.outdir}/pod5/${run_name}/README.html"         "${baseDir}/assets/readme/pod5.html"
					_render_readme "${currentResultsStateDir}/README.html"                 "${baseDir}/assets/readme/state.html"
					_render_readme "${params.outdir}/sample_info/${run_name}/README.html"   "${baseDir}/assets/readme/sample_info.html"
					# Write run config README.html (always overwrite to reflect latest conf)
					python3 ${baseDir}/bin/render_run_config_readme.py \
						--template "${baseDir}/assets/readme/run_config.html" \
						--conf "${params.outdir}/${run_name}.conf" \
						--out "${params.outdir}/${run_name}_config/README.html" \
						--run-name "${run_name}" \
						--state-id "${stateId}" \
						--cmd-line "${workflow.commandLine}" || true

						python3 ${baseDir}/bin/report_render.py \
							--history "\$REPORT_HISTORY_JSONL" \
							--run-index "\$RUN_INDEX_JSONL" \
							--template "${baseDir}/assets/report/template.html" \
						--css "${baseDir}/assets/report/report.css" \
						--js "${baseDir}/assets/report/report.js" \
						--schema-version "1.4" \
						--state-out "\$REPORT_STATE_JSON" \
						--auto-refresh-enabled "\$HTML_REPORT_AUTO_REFRESH" \
						--auto-refresh-seconds "\$HTML_REPORT_REFRESH_SECONDS" \
						--state-url "report_state.json" \
						--url-prefix "\$HTML_REPORT_URL_PREFIX" \
						--out "\$REPORT_HTML"
					render_rc=\$?
					if [ "\$render_rc" -ne 0 ]; then
						echo "WARN: report_render.py failed (rc=\$render_rc) round=${round_barcode}" 1>&2
					else
						python3 ${baseDir}/bin/report_render.py \
							--history "\$REPORT_HISTORY_JSONL" \
							--run-index "\$RUN_INDEX_JSONL" \
							--template "${baseDir}/assets/report/run_template.html" \
							--css "${baseDir}/assets/report/report.css" \
							--js "${baseDir}/assets/report/report.js" \
							--schema-version "1.4" \
							--state-out "\$RUN_REPORT_STATE" \
							--auto-refresh-enabled "\$HTML_REPORT_AUTO_REFRESH" \
							--auto-refresh-seconds "\$HTML_REPORT_REFRESH_SECONDS" \
							--state-url "report_state.json" \
							--url-prefix "\$HTML_REPORT_URL_PREFIX" \
							--run-id-filter "${run_name}" \
							--pod5-dir-url "../../pod5/${run_name}/README.html" \
							--state-dir-url "../../current/state/${stateId}/README.html" \
							--figures-dir-url "./figures/README.html" \
							--sample-info-url "../../sample_info/${run_name}/README.html" \
							--run-config-url "../../${run_name}_config/README.html" \
							--out "\$RUN_REPORT_HTML"
						run_render_rc=\$?
						if [ "\$run_render_rc" -ne 0 ]; then
							echo "WARN: report_render.py failed for run report (rc=\$run_render_rc) round=${round_barcode}" 1>&2
						fi
					fi
					release_render_lock
				fi
			else
				echo "WARN: python3 not found; skipping HTML report rendering" 1>&2
			fi
		fi
		set -e

		"""
}

complete_round_with_path = ChannelUtils.strictRoundJoin(complete_round_ch, close_round_ch)



// ============================================================
// STAGE J — STATE FINALIZATION · round boundary (backup_update_and_clean)
// ============================================================
process backup_update_and_clean {
	cache false
//  publishDir "${params.outdir}/temp/ongoing/", mode: 'copy', overwrite: true
//  publishDir "${params.outdir}/temp/ongoing/${round_barcode}/", mode: 'copy', overwrite: true
	input:
		tuple val(barcode), val(round_barcode), val(read_path) from complete_round_with_path
	output:
		file("done_pod5.txt")
	script:
		"""
			set -euo pipefail
			shopt -s nullglob
			export LC_ALL=C
			RESTART_TOKEN="${restartTokenForCache}"

		READ_PATH="${read_path ?: ''}"

	wait_minutes=${params.file_wait_minutes ?: 30}
		
			ROUND_TMP="${ongoingStateDir}/${round_barcode}"
			
			STATE_TMP="${ongoingStateDir}/_state"
			ONGOING_FINAL="${ongoingResultsStateDir}"
				ROUND_LOCKDIR="\${STATE_TMP}/.round_inflight.lockdir"
						DONE_LOCK="\${STATE_TMP}/.done_pod5.lock"
						ROUND_LOCK_SCOPE="${roundLockScopeCanonical}"
						LOCK_WAIT=${params.lock_wait_seconds}
				source "${baseDir}/bin/lib/lock_utils.sh"
				source "${baseDir}/bin/lib/backup_sync.sh"
				init_lock_helpers
			# Register the round lock (acquired by a prior process) so the EXIT trap
			# releases it if this process crashes before the explicit rmdir below.
			if [ "\$ROUND_LOCK_SCOPE" = "full_round" ] && [ -d "\$ROUND_LOCKDIR" ]; then
				acquired_locks+=( "\$STATE_TMP/.round_inflight" )
			fi
		
			CURRENT_TEMP_ROOT="${currentStateDir}"
			CURRENT_ROOT="${currentResultsStateDir}"

					# -- §2: Rolling state publish (copy _rpt.txt + PNGs to ongoing results) --
					# Copy rolling tables/plots to the "ongoing" results area.
					mkdir -p "\$ONGOING_FINAL"
					mkdir -p "\$STATE_TMP"
					rpts=( "\$STATE_TMP"/*_rpt.txt )
					rpts_plain=()
					for rpt in "\${rpts[@]}"; do
						base_name="\$(basename -- "\$rpt")"
						case "\$base_name" in
							"${barcode}_demult_rpt.txt"|\
							"${barcode}_otu_def_rpt.txt"|\
							"${barcode}_on_target_rpt.txt"|\
							"${barcode}_read_info_on_target_barcode_rpt.txt"|\
							"${barcode}_read_info_rpt.txt")
								;;
							*)
								rpts_plain+=( "\$rpt" )
								;;
						esac
					done
					pngs_all=( "\$STATE_TMP"/*.png )
					if (( \${#rpts_plain[@]} )); then sync_changed_files "\$ONGOING_FINAL" "\${rpts_plain[@]}"; fi
					if (( \${#pngs_all[@]} )); then sync_changed_files "\$ONGOING_FINAL" "\${pngs_all[@]}"; fi
					rpts_gzip_only=(
						"\$STATE_TMP/${barcode}_demult_rpt.txt"
						"\$STATE_TMP/${barcode}_otu_def_rpt.txt"
						"\$STATE_TMP/${barcode}_on_target_rpt.txt"
						"\$STATE_TMP/${barcode}_read_info_on_target_barcode_rpt.txt"
						"\$STATE_TMP/${barcode}_read_info_rpt.txt"
					)
					for rpt in "\${rpts_gzip_only[@]}"; do
						if [ -e "\$rpt" ]; then
							publish_gzip_atomic "\$rpt" "\$ONGOING_FINAL/\$(basename -- "\$rpt").gz"
						fi
					done
		
			mkdir -p "\$STATE_TMP"
			# -- §3: done_pod5 append (record processed POD5 under DONE_LOCK) --
			if acquire_lock "\$DONE_LOCK"; then
				# Record a stable POD5 identity for restart/skip logic.
				# Use the resolved absolute path (follows symlinks) to avoid collisions on basename alone.
				READ_KEY=""
				READ_SIZE=""
				READ_MTIME=""
				READ_INODE=""
				if [ -n "\$READ_PATH" ] && [ "\$READ_PATH" != "null" ] && [ -e "\$READ_PATH" ]; then
					read_meta="\$(perl -MCwd -e 'my \$p=shift; my \$abs=Cwd::abs_path(\$p)||\$p; my @st=stat(\$abs); my \$size=\$st[7]//0; my \$mtime=\$st[9]//0; my \$ino=\$st[1]//0; print \"\$abs\\t\$size\\t\$mtime\\t\$ino\";' "\$READ_PATH" 2>/dev/null || true)"
					if [ -n "\$read_meta" ]; then
						IFS="\$(printf '\t')" read -r READ_KEY READ_SIZE READ_MTIME READ_INODE <<< "\$read_meta"
					fi
				fi
				if [ -z "\$READ_KEY" ]; then
					READ_KEY="\$READ_PATH"
				fi
				READ_BASE="\$(basename -- "\$READ_PATH" 2>/dev/null || echo "\$READ_PATH")"
				ENTRY="\$READ_KEY\t\${READ_SIZE:-}\t\${READ_MTIME:-}\t\${READ_INODE:-0}\t\$READ_BASE"
				if [ -f \$STATE_TMP/done_pod5.txt ];
				then
						printf '%s\n' "\$ENTRY" >> \$STATE_TMP/done_pod5.txt
						echo "Appending to done_pod5.txt"  1>&2
				else
						printf '%s\n' "\$ENTRY" > \$STATE_TMP/done_pod5.txt
						echo "Creating done_pod5.txt"  1>&2
				fi
				release_lock "\$DONE_LOCK"
			else
			exit 1
		fi

		DELETE_INPUT_RAW="${params.delete_input_pod5}"
		DELETE_INPUT="\$(printf '%s' "\${DELETE_INPUT_RAW:-false}" | tr '[:upper:]' '[:lower:]')"
		DELETE_INPUT_FLAG=0
		if [ "\$DELETE_INPUT" = "true" ] || [ "\$DELETE_INPUT" = "1" ]; then
			DELETE_INPUT_FLAG=1
		fi

	# -- §4: POD5 disposal (delete or move to done_round_pod5) --
	if [ -n "\$READ_PATH" ] && [ "\$READ_PATH" != "null" ] && [ -e "\$READ_PATH" ];
	then
		if [ "\$DELETE_INPUT_FLAG" -eq 1 ]; then
			# Prune mode: delete the POD5 immediately to free disk space.
			# The pipeline has already fully processed this file; done_pod5.txt
			# tracks completion by path+inode, not by presence in done_round_pod5/.
			rm -f "\$READ_PATH" 2>/dev/null || true
			echo "INFO: deleted processed POD5 \$(basename -- "\$READ_PATH") (delete_input_pod5=true)" 1>&2
		else
			DONE_POD5_DIR="${podBaseDir}/done_round_pod5"
			mkdir -p "\$DONE_POD5_DIR"
			base="\$(basename -- "\$READ_PATH")"
			dest="\$DONE_POD5_DIR/\$base"
			# Move the processed POD5 into done_round_pod5 so the feeder can proceed.
			if ! mv -f "\$READ_PATH" "\$dest" 2>/dev/null; then
				cp -p "\$READ_PATH" "\$dest"
				rm -f "\$READ_PATH"
			fi
		fi
	fi
		
		# Do NOT delete files under the Nextflow work directory here.
		# Downstream tasks in subsequent rounds still rely on those staged inputs,
		# and cleaning them manually was causing missing FASTA/FASTQ files that
		# blocked consensus assignments. Use `nextflow clean` after the full run
		# if workspace cleanup is required.

			if [ -f \$STATE_TMP/done_pod5.txt ];
			then
				cp \$STATE_TMP/done_pod5.txt done_pod5.txt
				mkdir -p "\$CURRENT_TEMP_ROOT"
				cp \$STATE_TMP/done_pod5.txt "\$CURRENT_TEMP_ROOT"/
			fi
		

		# Derive pod5_dir only if 'READ_PATH' is meaningful and exists
		pod5_dir=""
		if [[ -n "\$READ_PATH" && "\$READ_PATH" != "null" ]]; then
			pod5_dir="\$(dirname -- "\$READ_PATH")"
		fi

			DELETE_ORI_RAW="${params.delete_from_ori_dir}"
			DELETE_ORI="\$(printf '%s' "\${DELETE_ORI_RAW:-false}" | tr '[:upper:]' '[:lower:]')"
			DELETE_ORI_FLAG=0
			if [ "\$DELETE_ORI" = "true" ] || [ "\$DELETE_ORI" = "1" ]; then
				DELETE_ORI_FLAG=1
			fi

			WATCH_RAW="${params.watch}"
			WATCH="\$(printf '%s' "\${WATCH_RAW:-true}" | tr '[:upper:]' '[:lower:]')"
			WATCH_FLAG=0
			if [ "\$WATCH" = "true" ] || [ "\$WATCH" = "1" ]; then
				WATCH_FLAG=1
			fi
			RUN_MODE="${runMode}"

		# -- §5: Staging helpers + realtime watch loop (stage next POD5) --
		DONE_POD5_PATH="\$STATE_TMP/done_pod5.txt"
		pod5_key() {
			perl -MCwd -e 'my \$p=shift; my \$abs=Cwd::abs_path(\$p)||\$p; my @st=stat(\$abs); my \$size=\$st[7]//0; my \$mtime=\$st[9]//0; my \$ino=\$st[1]//0; print "\$abs\\t\$size\\t\$mtime\\t\$ino";' "\$1" 2>/dev/null || true
		}
		done_contains() {
			local f="\$1"
			[ -f "\$DONE_POD5_PATH" ] || return 1
			local key
			key="\$(pod5_key "\$f")"
			if [ -n "\$key" ]; then
				if awk -v k="\$key" 'BEGIN{FS="\\t"} NF>=4 && (\$1"\\t"\$2"\\t"\$3"\\t"\$4)==k {found=1} END{exit !found}' "\$DONE_POD5_PATH"; then
					return 0
				fi
				if awk -v k="\$key" 'BEGIN{FS="\\t"} NF>=3 && (\$1"\\t"\$2"\\t"\$3)==k {found=1} END{exit !found}' "\$DONE_POD5_PATH"; then
					return 0
				fi
			fi
			local base
			base="\$(basename -- "\$f" 2>/dev/null || echo "\$f")"
			if grep -Fxq "\$base" "\$DONE_POD5_PATH" 2>/dev/null; then
				return 0
			fi
			return 1
		}

			# Proceed only for realtime mode when both origin and destination dirs exist.
			if [ "\$RUN_MODE" = "realtime" ] && [ "\$WATCH_FLAG" -eq 1 ] && [[ -d "${params.ori_dir}" && -n "\$pod5_dir" && -d "\$pod5_dir" ]]; then
			shopt -s nullglob
			for (( ; ; )); do
				# Use a nullglob+array check instead of compgen -G to avoid "option requires an argument"
				pods=( "\$pod5_dir"/*.pod5 )
				if (( \${#pods[@]} > 0 )); then
					# Clean up stale symlinks (broken or already-done) so intake doesn't stay non-empty.
					for p in "\${pods[@]}"; do
						if [ -L "\$p" ]; then
							if [ ! -e "\$p" ]; then
								rm -f "\$p"
								continue
							fi
							if done_contains "\$p"; then
								rm -f "\$p"
								continue
							fi
						fi
					done
					pods=( "\$pod5_dir"/*.pod5 )
					if (( \${#pods[@]} > 0 )); then
						break
					fi
				fi

			    oldest=""
			    while IFS= read -r cand; do
			    	[ -f "\$cand" ] || continue
			    	[ -r "\$cand" ] || continue
			    	if done_contains "\$cand"; then
			    		continue
			    	fi
			    	oldest="\$cand"
			    	break
			    done < <(ls -1tr "${params.ori_dir}"/*.pod5 2>/dev/null)
    			if [[ -n "\$oldest" && -f "\$oldest" && -r "\$oldest" ]]; then
					dst="\$pod5_dir/\$(basename -- "\$oldest")"
					# Always mv from ori_round_pod5 to reads_rt (never symlink).
					# Symlinking left the source in ori_round_pod5, which the feeder counted
					# as spool=1. With ready=1 (active symlink) the feeder's throttle
					# (ready>=1 AND spool>=1) prevented pre-staging the next round, causing
					# a full sleep_time (~5 min) delay at every round boundary.
					# Moving empties ori_round_pod5 so the feeder pre-stages the next round
					# while this one is still processing. Pod5 is preserved in done_round_pod5.
					if ! mv -f -- "\$oldest" "\$dst" 2> cp_new_pod5.err; then
						cp -p -- "\$oldest" "\$dst" 2> cp_new_pod5.err || { echo "WARNING: Failed to stage new pod5 file" >&2; cat cp_new_pod5.err >&2; }
						rm -f -- "\$oldest" 2>/dev/null || { echo "WARNING: Failed to remove \$oldest from ori_round_pod5" >&2; }
					fi
					break
				fi

				 sleep 10
			done
		fi



		# -- §6: State-tables snapshot + round-lock release --
		# ---- Snapshot rolling state tables while lock is still held ----
		# Done here (inside the round lock) to avoid a race with OTU_definition N+1,
		# which writes to these same \$STATE_TMP files without holding the round lock.
		# These are small TSV/FASTA files so the cost is negligible.

			mkdir -p "\$CURRENT_TEMP_ROOT/tables" "\$CURRENT_ROOT/tables"
			shopt -s nullglob
			state_tables=( "\$STATE_TMP"/read_qscore_rolling.tsv "\$STATE_TMP"/otu_frozen_*.tsv \
				"\$STATE_TMP"/otu_frozen_reps.fasta "\$STATE_TMP"/otu_frozen_reps.fasta.gz "\$STATE_TMP"/otu_active_pool.fasta \
				"\$STATE_TMP"/otu_seen_hashes.tsv "\$STATE_TMP"/*consensus_consolidated_ids.txt "\$STATE_TMP"/otu_consolidated_keys.tsv \
				"\$STATE_TMP"/*_seen_read_ids.tsv "\$STATE_TMP"/*_on_target_state.tsv )
			if (( \${#state_tables[@]} )); then
				sync_changed_files "\$CURRENT_TEMP_ROOT/tables" "\${state_tables[@]}" 2>/dev/null || true
				sync_changed_files "\$CURRENT_ROOT/tables" "\${state_tables[@]}" 2>/dev/null || true
			fi


		# ---- Release round lock (before heavy I/O) ----
		# POD5 staging complete; release now so round N+1 can start
		# while this process copies Consensus/plots/tables (outside lock).
		if [ "\$ROUND_LOCK_SCOPE" = "full_round" ]; then
			rm -f "\$ROUND_LOCKDIR/meta.env" 2>/dev/null || true
			rmdir "\$ROUND_LOCKDIR" 2>/dev/null || true
			rm -f "\$STATE_TMP/round_inflight.txt" 2>/dev/null || true
		fi
		rm -f "\$STATE_TMP/.round_lock_handoff.${round_barcode}"* 2>/dev/null || true


		# -- §7: Post-lock heavy copies + disk-pressure cleanup --
		mkdir -p "\$CURRENT_TEMP_ROOT/plots" "\$CURRENT_TEMP_ROOT/sequences"
		mkdir -p "\$CURRENT_ROOT/plots"       "\$CURRENT_ROOT/sequences"

			pngs=( "\$ROUND_TMP"/*.png )
			if (( \${#pngs[@]} )); then
				sync_changed_files "\$CURRENT_TEMP_ROOT/plots" "\${pngs[@]}"
			fi

		# ---- Copy tables (TXT/TSV/CSV and gzipped variants) ----
			tables=( "\$ROUND_TMP"/*.txt "\$ROUND_TMP"/*.tsv "\$ROUND_TMP"/*.csv \
				"\$ROUND_TMP"/*.txt.gz "\$ROUND_TMP"/*.tsv.gz "\$ROUND_TMP"/*.csv.gz \
				"\$ROUND_TMP"/*_rpt.txt "\$ROUND_TMP"/*_rpt.txt.gz )
			if (( \${#tables[@]} )); then
				sync_changed_files "\$CURRENT_TEMP_ROOT/tables" "\${tables[@]}"
			fi

		# ---- Copy sequences (FASTA/FASTQ and gzipped variants) ----
			seqs=( "\$ROUND_TMP"/*.fa "\$ROUND_TMP"/*.fasta "\$ROUND_TMP"/*.fq "\$ROUND_TMP"/*.fastq \
				"\$ROUND_TMP"/*.fa.gz "\$ROUND_TMP"/*.fasta.gz "\$ROUND_TMP"/*.fq.gz "\$ROUND_TMP"/*.fastq.gz )
			if (( \${#seqs[@]} )); then
				sync_changed_files "\$CURRENT_TEMP_ROOT/sequences" "\${seqs[@]}"
			fi
			# ---- Copy consensus cache/state for restore ----
			if [ -d ${ongoingStateDir}/Consensus ]; then
				mkdir -p "\$CURRENT_TEMP_ROOT/sequences/Consensus"
				mkdir -p "\$CURRENT_ROOT/sequences/Consensus"
				sync_changed_tree "${ongoingStateDir}/Consensus" "\$CURRENT_TEMP_ROOT/sequences/Consensus" 2>/dev/null || true
				sync_changed_tree "${ongoingStateDir}/Consensus" "\$CURRENT_ROOT/sequences/Consensus" 2>/dev/null || true
			fi

				if [ -d \$ONGOING_FINAL/single_exp ];
				then
					sync_changed_tree "\$ONGOING_FINAL/single_exp" "\$CURRENT_ROOT/sequences/single_exp"
					if [ -d ${ongoingStateDir}/Consensus ];
					then
						sync_changed_tree "${ongoingStateDir}/Consensus" "\$CURRENT_ROOT/sequences/single_exp/Consensus"
					fi
				fi
		
		
		# ---- Copy plots (PNGs) ----
			pngs=( "\$ONGOING_FINAL"/*.png )
			if (( \${#pngs[@]} )); then
				sync_changed_files "\$CURRENT_ROOT/plots" "\${pngs[@]}"
			fi

		# ---- Copy tables (TXT/TSV/CSV and gzipped variants) ----
			tables=( "\$ONGOING_FINAL"/*.txt "\$ONGOING_FINAL"/*.tsv "\$ONGOING_FINAL"/*.csv \
				"\$ONGOING_FINAL"/*.txt.gz "\$ONGOING_FINAL"/*.tsv.gz "\$ONGOING_FINAL"/*.csv.gz \
				"\$ONGOING_FINAL"/*_rpt.txt "\$ONGOING_FINAL"/*_rpt.txt.gz )
			if (( \${#tables[@]} )); then
				sync_changed_files "\$CURRENT_ROOT/tables" "\${tables[@]}"
			fi

		# ---- Copy sequences (FASTA/FASTQ and gzipped variants) ----
			seqs=( "\$ONGOING_FINAL"/*.fa "\$ONGOING_FINAL"/*.fasta "\$ONGOING_FINAL"/*.fq "\$ONGOING_FINAL"/*.fastq \
				"\$ONGOING_FINAL"/*.fa.gz "\$ONGOING_FINAL"/*.fasta.gz "\$ONGOING_FINAL"/*.fq.gz "\$ONGOING_FINAL"/*.fastq.gz )
			if (( \${#seqs[@]} )); then
				sync_changed_files "\$CURRENT_ROOT/sequences" "\${seqs[@]}"
			fi

		
			if [ -d \$ROUND_TMP/ ]
			then
						rm -rf \$ROUND_TMP/fastq_files
						if [ "${params.make_round_tar}" = "true" ] || [ "${params.make_round_tar}" = "1" ]; then
							tar -czvf "\$STATE_TMP/${round_barcode}.tar.gz"  "\$ROUND_TMP/"
						fi
					# Disk pressure: prune large per-round sequence files after snapshotting.
					if [ "${params.prune_round_sequences}" = "true" ] || [ "${params.prune_round_sequences}" = "1" ]; then
						rm -f "\$ROUND_TMP"/*.fastq "\$ROUND_TMP"/*.fq "\$ROUND_TMP"/*.fasta "\$ROUND_TMP"/*.fa "\$ROUND_TMP"/*.sam 2>/dev/null || true
					fi
			fi
			"""
		}



/*
 * STEP 3 - Output Description HTML
 */
/*
 * Completion e-mail notification
 */
workflow.onComplete {
    c_green = params.monochrome_logs ? '' : "\033[0;32m";
    c_purple = params.monochrome_logs ? '' : "\033[0;35m";
    c_red = params.monochrome_logs ? '' : "\033[0;31m";
    c_reset = params.monochrome_logs ? '' : "\033[0m";

    if (workflow.stats.ignoredCount > 0 && workflow.success) {
        log.info "-${c_purple}Warning, pipeline completed, but with errored process(es) ${c_reset}-"
        log.info "-${c_red}Number of ignored errored process(es) : ${workflow.stats.ignoredCount} ${c_reset}-"
        log.info "-${c_green}Number of successfully ran process(es) : ${workflow.stats.succeedCount} ${c_reset}-"
    }

    if (workflow.success) {
        log.info "-${c_purple}[nf-core/rtnanopipeline]${c_green} Pipeline completed successfully${c_reset}-"
    } else {
        checkHostname()
        log.info "-${c_purple}[nf-core/rtnanopipeline]${c_red} Pipeline completed with errors${c_reset}-"
    }

}


// ============================================================
// PREAMBLE VALIDATION METHODS — hoisted; called from §3 above
// ============================================================

def validateTimingLockParams() {
    if ( !params.containsKey('file_wait_minutes') || params.file_wait_minutes == null ) {
        params.file_wait_minutes = 30
    }
    // How long to wait for the per-POD5 "round lock" (0 = wait indefinitely).
    // This lock is used to prevent Nextflow from overlapping rounds when multiple POD5s
    // are available at once, which can otherwise cause later rounds to read stale/partial
    // rolling state files under `${params.outdir}/temp/ongoing/state/<stateId>/_state`.
    if ( !params.containsKey('round_lock_wait_minutes') || params.round_lock_wait_minutes == null ) {
        params.round_lock_wait_minutes = 360
    }
    // Stale lock reclaim TTL for the per-state "round lock" (minutes).
    // See the lock logic in `fast_on_target_detection`.
    if ( !params.containsKey('stale_lock_ttl_minutes') || params.stale_lock_ttl_minutes == null ) {
        params.stale_lock_ttl_minutes = 360
    }
    def staleLockTtlMinutesStr = params.stale_lock_ttl_minutes.toString().trim()
    if (!(staleLockTtlMinutesStr ==~ /^\d+$/)) {
        exit 1, "Invalid --stale_lock_ttl_minutes '${params.stale_lock_ttl_minutes}'. Provide an integer >= 0."
    }
    // Used for portable (no `flock`) directory-based locking.
    if ( !params.containsKey('lock_wait_seconds') || params.lock_wait_seconds == null ) {
        params.lock_wait_seconds = 300
    }
    if ( !params.containsKey('round_lock_scope') || params.round_lock_scope == null ) {
        params.round_lock_scope = 'full_round'
    }
    def roundLockScopeCanonical = params.round_lock_scope.toString().trim().toLowerCase()
    if (!(roundLockScopeCanonical in ['full_round', 'dorado_only'])) {
        exit 1, "Invalid --round_lock_scope '${params.round_lock_scope}'. Allowed values: full_round, dorado_only"
    }
    return [staleLockTtlMinutesStr: staleLockTtlMinutesStr, roundLockScopeCanonical: roundLockScopeCanonical]
}

def validateForkParams() {
    if ( !params.containsKey('maxforks_fast') || params.maxforks_fast == null ) {
        params.maxforks_fast = 1
    }
    def maxForksFastStr = params.maxforks_fast.toString().trim()
    if (!(maxForksFastStr ==~ /[0-9]+/) || maxForksFastStr.toInteger() < 1) {
        exit 1, "Invalid --maxforks_fast '${params.maxforks_fast}'. Provide an integer >= 1."
    }
    def maxForksFastVal = maxForksFastStr.toInteger()
    if ( !params.containsKey('maxforks_reporting') || params.maxforks_reporting == null ) {
        params.maxforks_reporting = 2
    }
    def maxForksReportingStr = params.maxforks_reporting.toString().trim()
    if (!(maxForksReportingStr ==~ /[0-9]+/) || maxForksReportingStr.toInteger() < 1) {
        exit 1, "Invalid --maxforks_reporting '${params.maxforks_reporting}'. Provide an integer >= 1."
    }
    def maxForksReportingVal = maxForksReportingStr.toInteger()
    if ( !params.containsKey('maxforks_consensus') || params.maxforks_consensus == null ) {
        params.maxforks_consensus = 1
    }
    def maxForksConsensusStr = params.maxforks_consensus.toString().trim()
    if (!(maxForksConsensusStr ==~ /[0-9]+/) || maxForksConsensusStr.toInteger() < 1) {
        exit 1, "Invalid --maxforks_consensus '${params.maxforks_consensus}'. Provide an integer >= 1."
    }
    def maxForksConsensusVal = maxForksConsensusStr.toInteger()
    if ( !params.containsKey('maxforks_core_cpu') || params.maxforks_core_cpu == null ) {
        params.maxforks_core_cpu = 1
    }
    def maxForksCoreCpuStr = params.maxforks_core_cpu.toString().trim()
    if (!(maxForksCoreCpuStr ==~ /[0-9]+/) || maxForksCoreCpuStr.toInteger() < 1) {
        exit 1, "Invalid --maxforks_core_cpu '${params.maxforks_core_cpu}'. Provide an integer >= 1."
    }
    def maxForksCoreCpuVal = maxForksCoreCpuStr.toInteger()
    return [maxForksFastVal: maxForksFastVal, maxForksReportingVal: maxForksReportingVal,
            maxForksConsensusVal: maxForksConsensusVal, maxForksCoreCpuVal: maxForksCoreCpuVal]
}

def validateHtmlReportParams() {
    if ( !params.containsKey('html_report_enabled') || params.html_report_enabled == null ) {
        params.html_report_enabled = true
    }
    def htmlReportEnabled = parseBoolStrict(params.html_report_enabled, true, 'html_report_enabled')
    if ( !params.containsKey('html_report_auto_refresh') || params.html_report_auto_refresh == null ) {
        params.html_report_auto_refresh = true
    }
    def htmlReportAutoRefresh = parseBoolStrict(params.html_report_auto_refresh, true, 'html_report_auto_refresh')
    if ( !params.containsKey('html_report_refresh_seconds') || params.html_report_refresh_seconds == null ) {
        params.html_report_refresh_seconds = 15
    }
    def htmlReportRefreshSecondsStr = params.html_report_refresh_seconds.toString().trim()
    if (!(htmlReportRefreshSecondsStr ==~ /[0-9]+/) || htmlReportRefreshSecondsStr.toInteger() < 1) {
        exit 1, "Invalid --html_report_refresh_seconds '${params.html_report_refresh_seconds}'. Provide an integer >= 1."
    }
    if ( !params.containsKey('html_report_url_prefix') || params.html_report_url_prefix == null ) {
        params.html_report_url_prefix = ""
    }
    def htmlReportUrlPrefix = params.html_report_url_prefix.toString().trim()
    if ( !params.containsKey('html_report_sample_plot_max') || params.html_report_sample_plot_max == null ) {
        params.html_report_sample_plot_max = 10
    }
    def htmlReportSamplePlotMaxStr = params.html_report_sample_plot_max.toString().trim()
    if (!(htmlReportSamplePlotMaxStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --html_report_sample_plot_max '${params.html_report_sample_plot_max}'. Provide an integer >= 0."
    }
    return [htmlReportEnabled: htmlReportEnabled, htmlReportAutoRefresh: htmlReportAutoRefresh,
            htmlReportRefreshSecondsStr: htmlReportRefreshSecondsStr, htmlReportUrlPrefix: htmlReportUrlPrefix,
            htmlReportSamplePlotMaxStr: htmlReportSamplePlotMaxStr]
}

def validateOtuRecoveryPruneParams() {
    // Frozen-rep incremental OTU clustering defaults.
    if ( !params.containsKey('otu_frozen_enabled') || params.otu_frozen_enabled == null ) {
        params.otu_frozen_enabled = true
    }
    if ( !params.containsKey('otu_frozen_min_rounds') || params.otu_frozen_min_rounds == null ) {
        params.otu_frozen_min_rounds = 3
    }
    if ( !params.containsKey('otu_frozen_min_reads') || params.otu_frozen_min_reads == null ) {
        params.otu_frozen_min_reads = 50
    }
    if ( !params.containsKey('otu_frozen_growth_window') || params.otu_frozen_growth_window == null ) {
        params.otu_frozen_growth_window = 3
    }
    if ( !params.containsKey('otu_frozen_drop_ratio') || params.otu_frozen_drop_ratio == null ) {
        params.otu_frozen_drop_ratio = 0.25
    }
    if ( !params.containsKey('otu_frozen_min_frac') || params.otu_frozen_min_frac == null ) {
        params.otu_frozen_min_frac = 0.0
    }
    if ( !params.containsKey('otu_incremental_min_new') || params.otu_incremental_min_new == null ) {
        params.otu_incremental_min_new = 1
    }
    if ( !params.containsKey('otu_hashmap_mixed_policy') || params.otu_hashmap_mixed_policy == null ) {
        params.otu_hashmap_mixed_policy = 'pipe_only'
    }
    if ( !params.containsKey('otu_allow_unsafe_recovery') || params.otu_allow_unsafe_recovery == null ) {
        params.otu_allow_unsafe_recovery = false
    }
    if ( !params.containsKey('otu_strict_ids') || params.otu_strict_ids == null ) {
        params.otu_strict_ids = true
    }
    if ( !params.containsKey('otu_commit_dropped_hashes') || params.otu_commit_dropped_hashes == null ) {
        params.otu_commit_dropped_hashes = false
    }
    if ( !params.containsKey('otu_pool_decision_include_hash') || params.otu_pool_decision_include_hash == null ) {
        params.otu_pool_decision_include_hash = false
    }
    if ( !params.containsKey('otu_frozen_db_only_policy') || params.otu_frozen_db_only_policy == null ) {
        params.otu_frozen_db_only_policy = 'auto'
    }
    def otuDbOnlyPolicyCanonical = params.otu_frozen_db_only_policy.toString().trim().toLowerCase()
    if (!(otuDbOnlyPolicyCanonical in ['auto', 'fail', 'warn_skip'])) {
        exit 1, "Invalid --otu_frozen_db_only_policy '${params.otu_frozen_db_only_policy}'. Allowed values: auto, fail, warn_skip"
    }
    if ( !params.containsKey('otu_pruned_recovery_enabled') || params.otu_pruned_recovery_enabled == null ) {
        params.otu_pruned_recovery_enabled = false
    }
    def otuPrunedRecoveryEnabled = parseBoolStrict(params.otu_pruned_recovery_enabled, false, 'otu_pruned_recovery_enabled')
    if ( !params.containsKey('otu_pruned_recovery_identity') || params.otu_pruned_recovery_identity == null ) {
        params.otu_pruned_recovery_identity = params.otu_id
    }
    def otuPrunedRecoveryIdentity = formatOtuIdentity(params.otu_pruned_recovery_identity)
    if ( !params.containsKey('otu_pruned_recovery_target_policy') || params.otu_pruned_recovery_target_policy == null ) {
        params.otu_pruned_recovery_target_policy = 'same_target_only'
    }
    def otuPrunedRecoveryTargetPolicyCanonical = params.otu_pruned_recovery_target_policy.toString().trim().toLowerCase()
    if (!(otuPrunedRecoveryTargetPolicyCanonical in ['same_target_only', 'any'])) {
        exit 1, "Invalid --otu_pruned_recovery_target_policy '${params.otu_pruned_recovery_target_policy}'. Allowed values: same_target_only, any"
    }
    if ( !params.containsKey('otu_pruned_recovery_failure_policy') || params.otu_pruned_recovery_failure_policy == null ) {
        params.otu_pruned_recovery_failure_policy = 'warn_skip'
    }
    def otuPrunedRecoveryFailurePolicyCanonical = params.otu_pruned_recovery_failure_policy.toString().trim().toLowerCase()
    if (!(otuPrunedRecoveryFailurePolicyCanonical in ['warn_skip', 'fail'])) {
        exit 1, "Invalid --otu_pruned_recovery_failure_policy '${params.otu_pruned_recovery_failure_policy}'. Allowed values: warn_skip, fail"
    }
    if ( !params.containsKey('prune_unassigned_clusters') || params.prune_unassigned_clusters == null ) {
        params.prune_unassigned_clusters = false
    }
    def pruneUnassignedClusters = parseBoolStrict(params.prune_unassigned_clusters, false, 'prune_unassigned_clusters')
    if ( !params.containsKey('prune_unassigned_drop_reads') || params.prune_unassigned_drop_reads == null ) {
        params.prune_unassigned_drop_reads = false
    }
    def pruneUnassignedDropReads = parseBoolStrict(params.prune_unassigned_drop_reads, false, 'prune_unassigned_drop_reads')
    if (!pruneUnassignedClusters && pruneUnassignedDropReads) {
        pruneUnassignedDropReads = false
        params.prune_unassigned_drop_reads = false
        log.warn "prune_unassigned_drop_reads disabled because prune_unassigned_clusters is false"
    }
    if ( !params.containsKey('prune_unassigned_grace_rounds') || params.prune_unassigned_grace_rounds == null ) {
        params.prune_unassigned_grace_rounds = 3
    }
    def pruneUnassignedGraceRoundsStr = params.prune_unassigned_grace_rounds.toString().trim()
    if (!(pruneUnassignedGraceRoundsStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --prune_unassigned_grace_rounds '${params.prune_unassigned_grace_rounds}'. Provide an integer >= 0."
    }
    if ( !params.containsKey('prune_unassigned_keep_top') || params.prune_unassigned_keep_top == null ) {
        params.prune_unassigned_keep_top = 5
    }
    def pruneUnassignedKeepTopStr = params.prune_unassigned_keep_top.toString().trim()
    if (!(pruneUnassignedKeepTopStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --prune_unassigned_keep_top '${params.prune_unassigned_keep_top}'. Provide an integer >= 0."
    }
    if ( !params.containsKey('otu_prune_frozen_policy') || params.otu_prune_frozen_policy == null ) {
        params.otu_prune_frozen_policy = 'until_consolidated'
    }
    def otuPruneFrozenPolicyCanonical = params.otu_prune_frozen_policy.toString().trim().toLowerCase()
    if (!(otuPruneFrozenPolicyCanonical in ['always', 'until_consolidated', 'never'])) {
        exit 1, "Invalid --otu_prune_frozen_policy '${params.otu_prune_frozen_policy}'. Allowed values: always, until_consolidated, never"
    }
    if ( !params.containsKey('otu_prune_samples_file') || params.otu_prune_samples_file == null ) {
        params.otu_prune_samples_file = ''
    }
    def otuPruneSamplesFileValue = params.otu_prune_samples_file.toString().trim()
    if ( !params.containsKey('otu_lock_force_prune_max_fasta_mb') || params.otu_lock_force_prune_max_fasta_mb == null ) {
        params.otu_lock_force_prune_max_fasta_mb = 0
    }
    def otuLockForcePruneMaxFastaMbStr = params.otu_lock_force_prune_max_fasta_mb.toString().trim()
    if (!(otuLockForcePruneMaxFastaMbStr ==~ /[0-9]+([.][0-9]+)?/)) {
        exit 1, "Invalid --otu_lock_force_prune_max_fasta_mb '${params.otu_lock_force_prune_max_fasta_mb}'. Provide a number >= 0."
    }
    if ( !params.containsKey('otu_force_prune_override') || params.otu_force_prune_override == null ) {
        params.otu_force_prune_override = false
    }
    def otuForcePruneOverride = parseBoolStrict(params.otu_force_prune_override, false, 'otu_force_prune_override')
    if ( !params.containsKey('otu_consolidated_keys_mixed_policy') || params.otu_consolidated_keys_mixed_policy == null ) {
        params.otu_consolidated_keys_mixed_policy = 'sample_scoped_only'
    }
    def otuConsolidatedKeysMixedPolicyCanonical = params.otu_consolidated_keys_mixed_policy.toString().trim().toLowerCase()
    if (!(otuConsolidatedKeysMixedPolicyCanonical in ['sample_scoped_only', 'warn_and_sample_scoped', 'fail'])) {
        exit 1, "Invalid --otu_consolidated_keys_mixed_policy '${params.otu_consolidated_keys_mixed_policy}'. Allowed values: sample_scoped_only, warn_and_sample_scoped, fail"
    }
    return [otuDbOnlyPolicyCanonical: otuDbOnlyPolicyCanonical,
            otuPrunedRecoveryEnabled: otuPrunedRecoveryEnabled,
            otuPrunedRecoveryIdentity: otuPrunedRecoveryIdentity,
            otuPrunedRecoveryTargetPolicyCanonical: otuPrunedRecoveryTargetPolicyCanonical,
            otuPrunedRecoveryFailurePolicyCanonical: otuPrunedRecoveryFailurePolicyCanonical,
            pruneUnassignedClusters: pruneUnassignedClusters,
            pruneUnassignedDropReads: pruneUnassignedDropReads,
            pruneUnassignedGraceRoundsStr: pruneUnassignedGraceRoundsStr,
            pruneUnassignedKeepTopStr: pruneUnassignedKeepTopStr,
            otuPruneFrozenPolicyCanonical: otuPruneFrozenPolicyCanonical,
            otuPruneSamplesFileValue: otuPruneSamplesFileValue,
            otuLockForcePruneMaxFastaMbStr: otuLockForcePruneMaxFastaMbStr,
            otuForcePruneOverride: otuForcePruneOverride,
            otuConsolidatedKeysMixedPolicyCanonical: otuConsolidatedKeysMixedPolicyCanonical]
}

def validateConsensusAssignParams() {
    // Consensus generation: minimum reads per OTU to attempt consensus.
    if ( !params.containsKey('consensus_min_reads') || params.consensus_min_reads == null ) {
        params.consensus_min_reads = 5
    }
    if ( !params.containsKey('consensus_max_reads') || params.consensus_max_reads == null ) {
        params.consensus_max_reads = 15
    }
    if ( !params.containsKey('consensus_min_qscore') || params.consensus_min_qscore == null ) {
        params.consensus_min_qscore = 15
    }
    if ( !params.containsKey('consensus_consolidated_min_qscore') || params.consensus_consolidated_min_qscore == null ) {
        params.consensus_consolidated_min_qscore = 20
    }
    if ( !params.containsKey('consensus_reads_mode') || params.consensus_reads_mode == null ) {
        params.consensus_reads_mode = 'representative'
    }
    if ( !params.containsKey('consensus_max_N') || params.consensus_max_N == null ) {
        params.consensus_max_N = 4
    }
    if ( !params.containsKey('consensus_keep_original_reads') || params.consensus_keep_original_reads == null ) {
        params.consensus_keep_original_reads = false
    }
    def consensusKeepOriginalReads = parseBoolStrict(params.consensus_keep_original_reads, false, 'consensus_keep_original_reads')
    if ( !params.containsKey('consensus_zero_emit_policy') || params.consensus_zero_emit_policy == null ) {
        params.consensus_zero_emit_policy = 'warn'
    }
    def consensusZeroEmitPolicyCanonical = params.consensus_zero_emit_policy.toString().trim().toLowerCase()
    if (!(consensusZeroEmitPolicyCanonical in ['warn', 'fail'])) {
        exit 1, "Invalid --consensus_zero_emit_policy '${params.consensus_zero_emit_policy}'. Allowed values: warn, fail"
    }
    if ( !params.containsKey('consensus_id_mismatch_policy') || params.consensus_id_mismatch_policy == null ) {
        // Default to warn: 0-resolved eligible reads is a valid operational state when all reads for
        // an OTU have been pruned in a previous round. resolve_ids_to_supreads uses multi-level
        // fallback matching, so a genuine ID-format bug would surface as many warnings across all OTUs,
        // not as a hard failure on a single OTU. Use --consensus_id_mismatch_policy fail to opt in.
        params.consensus_id_mismatch_policy = 'warn'
    }
    def consensusIdMismatchPolicyCanonical = params.consensus_id_mismatch_policy.toString().trim().toLowerCase()
    if (!(consensusIdMismatchPolicyCanonical in ['warn', 'fail'])) {
        exit 1, "Invalid --consensus_id_mismatch_policy '${params.consensus_id_mismatch_policy}'. Allowed values: warn, fail"
    }
    if ( !params.containsKey('consensus_cache_below_min_policy') || params.consensus_cache_below_min_policy == null ) {
        params.consensus_cache_below_min_policy = 'keep'
    }
    def consensusCacheBelowMinPolicyCanonical = params.consensus_cache_below_min_policy.toString().trim().toLowerCase()
    if (!(consensusCacheBelowMinPolicyCanonical in ['keep', 'drop'])) {
        exit 1, "Invalid --consensus_cache_below_min_policy '${params.consensus_cache_below_min_policy}'. Allowed values: keep, drop"
    }
    if ( !params.containsKey('otu_consolidation_lock') || params.otu_consolidation_lock == null ) {
        params.otu_consolidation_lock = true
    }
    def assignProtLevelCanonical = (params.containsKey('assign_protection_level') && params.assign_protection_level != null
        ? params.assign_protection_level.toString().trim().toLowerCase()
        : "genus")
    if (!(assignProtLevelCanonical in ['family', 'genus', 'species'])) {
        log.warn "assign_protection_level '${params.assign_protection_level}' unknown; defaulting to 'genus'"
        assignProtLevelCanonical = "genus"
    }
    if ( !params.containsKey('prune_round_sequences') || params.prune_round_sequences == null ) {
        params.prune_round_sequences = true
    }
    if ( !params.containsKey('prune_cumulative_pool_all') || params.prune_cumulative_pool_all == null ) {
        params.prune_cumulative_pool_all = true
    }
    def pruneCumulativePoolAll = parseBoolStrict(params.prune_cumulative_pool_all, true, 'prune_cumulative_pool_all')
    return [consensusKeepOriginalReads: consensusKeepOriginalReads,
            consensusZeroEmitPolicyCanonical: consensusZeroEmitPolicyCanonical,
            consensusIdMismatchPolicyCanonical: consensusIdMismatchPolicyCanonical,
            consensusCacheBelowMinPolicyCanonical: consensusCacheBelowMinPolicyCanonical,
            assignProtLevelCanonical: assignProtLevelCanonical,
            pruneCumulativePoolAll: pruneCumulativePoolAll]
}

def validateOtuClusterLockParams() {
    if ( !params.containsKey('otu_lock_small_cluster_ratio') || params.otu_lock_small_cluster_ratio == null ) {
        params.otu_lock_small_cluster_ratio = 0.1
    }
    def otuLockRatioStr = params.otu_lock_small_cluster_ratio.toString().trim()
    if (!(otuLockRatioStr ==~ /[0-9]+([.][0-9]+)?/)) {
        exit 1, "Invalid --otu_lock_small_cluster_ratio '${params.otu_lock_small_cluster_ratio}'. Provide a decimal ratio (e.g. 0.1)."
    }
    if ( !params.containsKey('otu_lock_min_consolidated_reads') || params.otu_lock_min_consolidated_reads == null ) {
        params.otu_lock_min_consolidated_reads = 10
    }
    def otuLockMinConsReadsStr = params.otu_lock_min_consolidated_reads.toString().trim()
    if (!(otuLockMinConsReadsStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --otu_lock_min_consolidated_reads '${params.otu_lock_min_consolidated_reads}'. Provide an integer >= 0."
    }
    if ( !params.containsKey('otu_lock_min_stable_rounds') || params.otu_lock_min_stable_rounds == null ) {
        params.otu_lock_min_stable_rounds = 1
    }
    def otuLockMinStableRoundsStr = params.otu_lock_min_stable_rounds.toString().trim()
    if (!(otuLockMinStableRoundsStr ==~ /[0-9]+/) || otuLockMinStableRoundsStr.toInteger() < 1) {
        exit 1, "Invalid --otu_lock_min_stable_rounds '${params.otu_lock_min_stable_rounds}'. Provide an integer >= 1."
    }
    if ( !params.containsKey('otu_lock_revalidate_every_rounds') || params.otu_lock_revalidate_every_rounds == null ) {
        params.otu_lock_revalidate_every_rounds = 0
    }
    def otuLockRevalidateEveryRoundsStr = params.otu_lock_revalidate_every_rounds.toString().trim()
    if (!(otuLockRevalidateEveryRoundsStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --otu_lock_revalidate_every_rounds '${params.otu_lock_revalidate_every_rounds}'. Provide an integer >= 0."
    }
    if ( !params.containsKey('otu_lock_reset_keys') || params.otu_lock_reset_keys == null ) {
        params.otu_lock_reset_keys = ''
    }
    if ( !params.containsKey('otu_size_streak_mode') || params.otu_size_streak_mode == null ) {
        params.otu_size_streak_mode = 'enforce'
    }
    def otuSizeStreakModeCanonical = params.otu_size_streak_mode.toString().trim().toLowerCase()
    if (!(otuSizeStreakModeCanonical in ['off', 'observe', 'enforce'])) {
        exit 1, "Invalid --otu_size_streak_mode '${params.otu_size_streak_mode}'. Allowed values: off, observe, enforce"
    }
    if ( !params.containsKey('otu_size_streak_min_rounds') || params.otu_size_streak_min_rounds == null ) {
        params.otu_size_streak_min_rounds = 3
    }
    def otuSizeStreakMinRoundsStr = params.otu_size_streak_min_rounds.toString().trim()
    if (!(otuSizeStreakMinRoundsStr ==~ /[0-9]+/) || otuSizeStreakMinRoundsStr.toInteger() < 1) {
        exit 1, "Invalid --otu_size_streak_min_rounds '${params.otu_size_streak_min_rounds}'. Provide an integer >= 1."
    }
    return [otuLockRatioStr: otuLockRatioStr,
            otuLockMinConsReadsStr: otuLockMinConsReadsStr,
            otuLockMinStableRoundsStr: otuLockMinStableRoundsStr,
            otuLockRevalidateEveryRoundsStr: otuLockRevalidateEveryRoundsStr,
            otuSizeStreakModeCanonical: otuSizeStreakModeCanonical,
            otuSizeStreakMinRoundsStr: otuSizeStreakMinRoundsStr]
}

def validateOtuBlastParams() {
    if ( !params.containsKey('otu_blast_min_members') || params.otu_blast_min_members == null ) {
        params.otu_blast_min_members = 3
    }
    def otuBlastMinMembersStr = params.otu_blast_min_members.toString().trim()
    if (!(otuBlastMinMembersStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --otu_blast_min_members '${params.otu_blast_min_members}'. Provide an integer >= 0."
    }
    if ( !params.containsKey('otu_blast_filter_mode') || params.otu_blast_filter_mode == null ) {
        params.otu_blast_filter_mode = 'enforce'
    }
    def otuBlastFilterModeCanonical = params.otu_blast_filter_mode.toString().trim().toLowerCase()
    if (!(otuBlastFilterModeCanonical in ['off', 'observe', 'enforce'])) {
        exit 1, "Invalid --otu_blast_filter_mode '${params.otu_blast_filter_mode}'. Allowed values: off, observe, enforce"
    }
    if ( !params.containsKey('otu_blast_force_use_filtered') || params.otu_blast_force_use_filtered == null ) {
        params.otu_blast_force_use_filtered = true
    }
    def otuBlastForceUseFiltered = parseBoolStrict(params.otu_blast_force_use_filtered, false, 'otu_blast_force_use_filtered')
    if (otuBlastForceUseFiltered && otuBlastFilterModeCanonical != 'enforce') {
        exit 1, "Invalid --otu_blast_force_use_filtered with --otu_blast_filter_mode='${otuBlastFilterModeCanonical}'. Set --otu_blast_filter_mode enforce."
    }
    if ( !params.containsKey('otu_blast_filter_skip_rounds') || params.otu_blast_filter_skip_rounds == null ) {
        params.otu_blast_filter_skip_rounds = '3'
    }
    def otuBlastFilterSkipRoundsRaw = params.otu_blast_filter_skip_rounds.toString().trim().toLowerCase()
    if (otuBlastFilterSkipRoundsRaw == '' || otuBlastFilterSkipRoundsRaw == '0') {
        otuBlastFilterSkipRoundsRaw = 'none'
    }
    def otuBlastFilterSkipRoundsCanonical = ''
    if (otuBlastFilterSkipRoundsRaw ==~ /[0-9]+/) {
        def skipRoundsVal = otuBlastFilterSkipRoundsRaw.toInteger()
        if (skipRoundsVal < 0) {
            exit 1, "Invalid --otu_blast_filter_skip_rounds '${params.otu_blast_filter_skip_rounds}'. Allowed values: none, all, or integer >= 0."
        }
        otuBlastFilterSkipRoundsCanonical = (skipRoundsVal == 0) ? 'none' : skipRoundsVal.toString()
    } else if (otuBlastFilterSkipRoundsRaw in ['none', 'all']) {
        otuBlastFilterSkipRoundsCanonical = otuBlastFilterSkipRoundsRaw
    } else {
        exit 1, "Invalid --otu_blast_filter_skip_rounds '${params.otu_blast_filter_skip_rounds}'. Allowed values: none, all, or integer >= 0."
    }
    if ( !params.containsKey('otu_blast_unassigned_grace_rounds') || params.otu_blast_unassigned_grace_rounds == null ) {
        params.otu_blast_unassigned_grace_rounds = 3
    }
    def otuBlastUnassignedGraceRoundsStr = params.otu_blast_unassigned_grace_rounds.toString().trim()
    if (!(otuBlastUnassignedGraceRoundsStr ==~ /[0-9]+/)) {
        exit 1, "Invalid --otu_blast_unassigned_grace_rounds '${params.otu_blast_unassigned_grace_rounds}'. Provide an integer >= 0."
    }
    if ( !params.containsKey('otu_blast_enforce_missing_max_frac') || params.otu_blast_enforce_missing_max_frac == null ) {
        params.otu_blast_enforce_missing_max_frac = 0.1
    }
    def otuBlastEnforceMissingMaxFracStr = params.otu_blast_enforce_missing_max_frac.toString().trim()
    if (!(otuBlastEnforceMissingMaxFracStr ==~ /[0-9]+([.][0-9]+)?/)) {
        exit 1, "Invalid --otu_blast_enforce_missing_max_frac '${params.otu_blast_enforce_missing_max_frac}'. Provide a decimal fraction in [0,1]."
    }
    if (otuBlastEnforceMissingMaxFracStr == '') {
        otuBlastEnforceMissingMaxFracStr = '0.1'
    }
    def otuBlastEnforceMissingMaxFracVal = otuBlastEnforceMissingMaxFracStr.toBigDecimal()
    if (otuBlastEnforceMissingMaxFracVal < 0 || otuBlastEnforceMissingMaxFracVal > 1) {
        exit 1, "Invalid --otu_blast_enforce_missing_max_frac '${params.otu_blast_enforce_missing_max_frac}'. Provide a decimal fraction in [0,1]."
    }
    if ( !params.containsKey('otu_blast_enforce_no_clusters_policy') || params.otu_blast_enforce_no_clusters_policy == null ) {
        params.otu_blast_enforce_no_clusters_policy = 'fallback_unfiltered'
    }
    def otuBlastEnforceNoClustersPolicyCanonical = params.otu_blast_enforce_no_clusters_policy.toString().trim().toLowerCase()
    if (!(otuBlastEnforceNoClustersPolicyCanonical in ['fail', 'fallback_unfiltered', 'allow_empty'])) {
        exit 1, "Invalid --otu_blast_enforce_no_clusters_policy '${params.otu_blast_enforce_no_clusters_policy}'. Allowed values: fail, fallback_unfiltered, allow_empty"
    }
    if ( !params.containsKey('otu_blast_unassigned_mode') || params.otu_blast_unassigned_mode == null ) {
        params.otu_blast_unassigned_mode = 'enforce'
    }
    def otuBlastUnassignedModeCanonical = params.otu_blast_unassigned_mode.toString().trim().toLowerCase()
    if (!(otuBlastUnassignedModeCanonical in ['off', 'observe', 'enforce'])) {
        exit 1, "Invalid --otu_blast_unassigned_mode '${params.otu_blast_unassigned_mode}'. Allowed values: off, observe, enforce"
    }
    if ( !params.containsKey('otu_blast_unassigned_max_otu_size') || params.otu_blast_unassigned_max_otu_size == null ) {
        params.otu_blast_unassigned_max_otu_size = 50
    }
    if ( !params.containsKey('otu_unassigned_streak_mode') || params.otu_unassigned_streak_mode == null ) {
        params.otu_unassigned_streak_mode = 'enforce'
    }
    def otuUnassignedStreakModeCanonical = params.otu_unassigned_streak_mode.toString().trim().toLowerCase()
    if (!(otuUnassignedStreakModeCanonical in ['off', 'observe', 'enforce'])) {
        exit 1, "Invalid --otu_unassigned_streak_mode '${params.otu_unassigned_streak_mode}'. Allowed values: off, observe, enforce"
    }
    if ( !params.containsKey('otu_unassigned_streak_min_rounds') || params.otu_unassigned_streak_min_rounds == null ) {
        params.otu_unassigned_streak_min_rounds = 3
    }
    if ( !params.containsKey('otu_unassigned_streak_min_size') || params.otu_unassigned_streak_min_size == null ) {
        params.otu_unassigned_streak_min_size = 2
    }
    if ( !params.containsKey('otu_unassigned_streak_max_size') || params.otu_unassigned_streak_max_size == null ) {
        params.otu_unassigned_streak_max_size = 50
    }
    return [otuBlastMinMembersStr: otuBlastMinMembersStr,
            otuBlastFilterModeCanonical: otuBlastFilterModeCanonical,
            otuBlastForceUseFiltered: otuBlastForceUseFiltered,
            otuBlastFilterSkipRoundsCanonical: otuBlastFilterSkipRoundsCanonical,
            otuBlastUnassignedGraceRoundsStr: otuBlastUnassignedGraceRoundsStr,
            otuBlastEnforceMissingMaxFracStr: otuBlastEnforceMissingMaxFracStr,
            otuBlastEnforceNoClustersPolicyCanonical: otuBlastEnforceNoClustersPolicyCanonical,
            otuBlastUnassignedModeCanonical: otuBlastUnassignedModeCanonical,
            otuUnassignedStreakModeCanonical: otuUnassignedStreakModeCanonical]
}

// ============================================================
// PREAMBLE HELPERS — hoisted top-level methods (callable before their position)
// ============================================================

// Boolean parsing helpers (moved from §1 closures; converted to hoisted methods).
def parseBool(v, boolean defaultVal) {
    if (v == null) return defaultVal
    def s = v.toString().trim().toLowerCase()
    if (!s) return defaultVal
    if (s in ['true', '1', 'yes', 'y', 'on']) return true
    if (s in ['false', '0', 'no', 'n', 'off']) return false
    defaultVal
}
def parseBoolStrict(v, boolean defaultVal, String paramName) {
    if (v == null) return defaultVal
    def s = v.toString().trim().toLowerCase()
    if (!s) return defaultVal
    if (s in ['true', '1', 'yes', 'y', 'on']) return true
    if (s in ['false', '0', 'no', 'n', 'off']) return false
    exit 1, "Invalid --${paramName} '${v}'. Allowed boolean values: true/false, 1/0, yes/no, on/off"
}

// FASTA header check and demux config builder (moved from §1; file() replaced with new File()).
boolean hasFastaHeader(def path) {
    if (!path) return false
    def f = new File(path.toString())
    if (!f.exists() || f.size() == 0) return false
    boolean headerFound = false
    f.withReader { reader ->
        String line
        while ((line = reader.readLine()) != null) {
            if (line.startsWith('>')) {
                headerFound = true
                break
            }
        }
    }
    headerFound
}

boolean isRegularFilePath(def path) {
    if (!path) return false
    new File(path.toString()).isFile()
}

Map getTrackArtifactPaths() {
    def runId = params.run_id?.toString()?.trim() ?: ""
    def resolvedSampleInfoDir = runId ? "${workflow.launchDir}/results/sample_info/${runId}" : "${workflow.launchDir}/results/sample_info"
    [
        sampleInfoDir: resolvedSampleInfoDir,
        trackIdx: "${resolvedSampleInfoDir}/track_demult.fasta",
        trackRoster: "${resolvedSampleInfoDir}/track_roster.tsv",
        trackActiveUnits: "${resolvedSampleInfoDir}/track_active_units.txt",
        trackIdentity: "${resolvedSampleInfoDir}/track_identity.tsv",
    ]
}

void requireTrackArtifactPath(def path, String artifactName, String context) {
    if (!isRegularFilePath(path)) {
        exit 1, "Track replicate mode requires ${artifactName} at ${path} ${context}"
    }
}

DemuxConfig getDemuxConfig() {
    // Always resolve to absolute so INDEXES_PATH/PRIMERS_PATH are correct when
    // used inside process scripts (which run from a work/ subdirectory).
    def absPath = { p ->
        if (!p) return null
        def s = p.toString()
        s.startsWith('/') ? s : new File(System.getProperty("user.dir"), s).canonicalPath
    }
    def trackArtifacts = getTrackArtifactPaths()
    def replicateMode = params.replicate_mode?.toString()?.trim()?.toLowerCase() ?: 'collapse'
    def mode = params.demultiplex_mode?.toString()?.toLowerCase()
    def idx = absPath(params.indexes?.toString())
    def pri = absPath(params.primer_indexes?.toString())
    def trackIdx = absPath(trackArtifacts.trackIdx)
    if (mode in ['on', 'true']) {
        if (replicateMode == 'track') {
            requireTrackArtifactPath(trackIdx, 'track_demult.fasta', "before full demultiplexing can use ${trackIdx}")
        }
        return new DemuxConfig(true, 'full', replicateMode == 'track' ? trackIdx : idx, pri)
    }
    if (mode in ['primers_only', 'primers-only', 'primer_only', 'primer-only'])
        return new DemuxConfig(true, 'primers_only', null, absPath(params.primer_indexes?.toString()))
    if (mode in ['off', 'false'])
        return new DemuxConfig(false, 'off', null, null)
    if (replicateMode == 'track' && hasFastaHeader(pri)) {
        requireTrackArtifactPath(trackIdx, 'track_demult.fasta', "before auto full demultiplexing can use ${trackIdx}")
    }
    def effectiveIdx = replicateMode == 'track' ? trackIdx : idx
    def enabled = hasFastaHeader(effectiveIdx) && hasFastaHeader(pri)
    return new DemuxConfig(enabled, enabled ? 'full' : 'off', effectiveIdx, pri)
}

// OTU identity normalizer — converts percent or decimal to plain decimal string (moved from §5).
def formatOtuIdentity(value) {
    def defaultVal = new BigDecimal('0.97')
    if (value == null) {
        return defaultVal.toPlainString()
    }
    def raw = value.toString().trim()
    if (!raw) {
        return defaultVal.toPlainString()
    }
    try {
        BigDecimal num = new BigDecimal(raw)
        if (num > BigDecimal.ONE) {
            if (num.scale() <= 0) {
                num = num.divide(new BigDecimal('100'))
            }
        }
        if (num > BigDecimal.ONE) {
            num = BigDecimal.ONE
        }
        BigDecimal minThreshold = new BigDecimal('0.8')
        if (num < minThreshold) {
            num = minThreshold
        }
        return num.stripTrailingZeros().toPlainString()
    } catch (Exception e) {
        return defaultVal.toPlainString()
    }
}

def nfcoreHeader() {
    // Log colors ANSI codes
    c_black = params.monochrome_logs ? '' : "\033[0;30m";
    c_blue = params.monochrome_logs ? '' : "\033[0;34m";
    c_cyan = params.monochrome_logs ? '' : "\033[0;36m";
    c_dim = params.monochrome_logs ? '' : "\033[2m";
    c_green = params.monochrome_logs ? '' : "\033[0;32m";
    c_purple = params.monochrome_logs ? '' : "\033[0;35m";
    c_reset = params.monochrome_logs ? '' : "\033[0m";
    c_white = params.monochrome_logs ? '' : "\033[0;37m";
    c_yellow = params.monochrome_logs ? '' : "\033[0;33m";

    return """    -${c_dim}--------------------------------------------------${c_reset}-
	${c_blue}      __   ____  __       __     __   ___   __         ${c_reset}
    ${c_blue}  |__))  ||  |__)) || /  \\\\ ((__  /     /__\\\\ |\\ || ${c_reset}
    ${c_blue}  |  \\\\  ||  |__)) || \\__//  __)) \\___  |  || | \\|| ${c_reset}
    ${c_green}${c_reset}
    ${c_purple}  github/rtbioscan v${workflow.manifest.version}${c_reset}
    -${c_dim}--------------------------------------------------${c_reset}-
    """.stripIndent()
}

def checkHostname() {
    def c_reset = params.monochrome_logs ? '' : "\033[0m"
    def c_white = params.monochrome_logs ? '' : "\033[0;37m"
    def c_red = params.monochrome_logs ? '' : "\033[1;91m"
    def c_yellow_bold = params.monochrome_logs ? '' : "\033[1;93m"
    if (params.hostnames) {
        def hostname = null
        try {
            hostname = java.net.InetAddress.getLocalHost().getHostName()
        } catch (Exception e) {
            hostname = System.getenv('HOSTNAME') ?: ''
        }
        params.hostnames.each { prof, hnames ->
            hnames.each { hname ->
                if (hostname.contains(hname) && !workflow.profile.contains(prof)) {
                    log.error "====================================================\n" +
                            "  ${c_red}WARNING!${c_reset} You are running with `-profile $workflow.profile`\n" +
                            "  but your machine hostname is ${c_white}'$hostname'${c_reset}\n" +
                            "  ${c_yellow_bold}It's highly recommended that you use `-profile $prof${c_reset}`\n" +
                            "============================================================"
                }
            }
        }
    }
}
