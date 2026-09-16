import SwiftUI

/// The one screen: destination strip, queue, footer. The whole window is a
/// drop target; everything else lives in the settings drawer or the menu bar.
struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isDropTargeted = false
    @State private var selection: UUID?

    var body: some View {
        VStack(spacing: 0) {
            DestinationStripView()
            Divider()
            if model.queue.isEmpty {
                EmptyQueueView()
            } else {
                queueList
            }
            Divider()
            FooterView()
        }
        .frame(minWidth: 540, minHeight: 400)
        .dropDestination(for: URL.self) { urls, _ in
            model.handleDrop(of: urls)
            return true
        } isTargeted: { targeted in
            if reduceMotion {
                isDropTargeted = targeted
            } else {
                withAnimation(.easeInOut(duration: 0.15)) {
                    isDropTargeted = targeted
                }
            }
        }
        .overlay {
            if isDropTargeted {
                DropOverlayView()
            }
        }
        .alert(
            "Some files were skipped",
            isPresented: Binding(
                get: { model.skippedFilesNotice != nil },
                set: { if !$0 { model.skippedFilesNotice = nil } }
            )
        ) {
            Button("OK", role: .cancel) { model.skippedFilesNotice = nil }
        } message: {
            Text(model.skippedFilesNotice ?? "")
        }
        .task {
            model.startPolling()
        }
    }

    private var queueList: some View {
        List(model.queue, selection: $selection) { item in
            QueueRowView(item: item)
                .listRowSeparator(.visible)
                .tag(item.id)
        }
        .listStyle(.inset)
        .scrollContentBackground(.hidden)
        .onDeleteCommand {
            if let selection {
                model.remove(selection)
            }
        }
    }
}

// MARK: - Destination strip

struct DestinationStripView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 10) {
            Text(Copy.destinationLabel)
                .foregroundStyle(.secondary)
            Button {
                model.chooseOutputFolder()
            } label: {
                Label(
                    model.outputFolderURL.lastPathComponent,
                    systemImage: "folder"
                )
                .lineLimit(1)
                .truncationMode(.middle)
            }
            .help(model.outputFolderURL.path)

            Spacer()

            Text(Copy.formatLabel)
                .foregroundStyle(.secondary)
            Picker(Copy.formatLabel, selection: formatBinding) {
                ForEach(ExportFormat.allCases) { format in
                    Text(format.label).tag(format)
                }
            }
            .labelsHidden()
            .fixedSize()
            .help(
                model.exportFormat.isBundle
                    ? "Builds one navigable Open Knowledge Format bundle in the destination "
                        + "folder instead of separate files."
                    : "Choose how cleaned documents are written to the destination folder."
            )
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private var formatBinding: Binding<ExportFormat> {
        Binding(
            get: { model.exportFormat },
            set: { model.exportFormat = $0 }
        )
    }
}

// MARK: - Queue rows

struct QueueRowView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let item: QueueItem
    @State private var showFailureDetails = false
    @State private var failureEvents: [RunEvent] = []
    @State private var isLoadingFailureEvents = false
    @State private var showQuality = false
    @State private var qualityReport: RunQualityReport?
    @State private var isLoadingQuality = false

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: leadingSymbol)
                .font(.title3)
                .foregroundStyle(leadingColor)
                .frame(width: 24)
                .transition(reduceMotion ? .opacity : .scale.combined(with: .opacity))
                .id(leadingSymbol)
                .accessibilityLabel(stageAccessibilityLabel)

            VStack(alignment: .leading, spacing: 3) {
                Text(item.filename)
                    .lineLimit(1)
                    .truncationMode(.middle)
                detail
            }

            Spacer()

            trailingAction
        }
        .frame(minHeight: 44)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: item.stage.isDone)
        .contextMenu {
            if case .done(let outputURL) = item.stage {
                Button(Copy.showInFinder) { model.revealInFinder(outputURL) }
                Button(Copy.openFile) { model.openFile(outputURL) }
            }
            if !item.stage.isTerminal {
                Button(Copy.stop) { model.stop(item.id) }
            }
            Button(Copy.removeFromList) { model.remove(item.id) }
        }
        .onTapGesture(count: 2) {
            if case .done(let outputURL) = item.stage {
                model.openFile(outputURL)
            }
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch item.stage {
        case .queued:
            Text(Copy.stageWaiting)
                .font(.caption)
                .foregroundStyle(.secondary)
        case .uploading:
            stageBar(Copy.stageSending, progress: nil)
        case .converting(let progress):
            stageBar(Copy.stageConverting, progress: progress)
        case .cleaning(let progress):
            stageBar(Copy.stageCleaning, progress: progress)
        case .classifying(let progress):
            stageBar(Copy.stageClassifying, progress: progress)
        case .done:
            Text(Copy.stageSaved)
                .font(.caption)
                .foregroundStyle(.secondary)
        case .failed(let reason, _):
            Text(reason)
                .font(.caption)
                .foregroundStyle(.orange)
                .lineLimit(1)
        }
    }

    private func stageBar(_ label: String, progress: Double?) -> some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            if let progress {
                ProgressView(value: min(max(progress, 0), 1))
                    .progressViewStyle(.linear)
                    .frame(maxWidth: 180)
            } else {
                ProgressView()
                    .progressViewStyle(.linear)
                    .frame(maxWidth: 180)
            }
        }
    }

    @ViewBuilder
    private var trailingAction: some View {
        switch item.stage {
        case .done(let outputURL):
            HStack(spacing: 8) {
                qualityAction
                Button(Copy.showInFinder) {
                    model.revealInFinder(outputURL)
                }
                .buttonStyle(.link)
            }
            .transition(reduceMotion ? .opacity : .move(edge: .trailing).combined(with: .opacity))
        case .failed(_, let retryable):
            HStack(spacing: 8) {
                qualityAction
                if item.runID != nil {
                    Button(Copy.failureDetails) {
                        // Open immediately with a loading state; a slow or
                        // unreachable engine must not make the button feel dead.
                        showFailureDetails = true
                        isLoadingFailureEvents = true
                        let runID = item.runID
                        Task { @MainActor in
                            if let runID {
                                failureEvents = await model.failureEvents(runID: runID)
                            }
                            isLoadingFailureEvents = false
                        }
                    }
                    .buttonStyle(.link)
                    .popover(isPresented: $showFailureDetails) {
                        FailureDetailsView(
                            item: item,
                            events: failureEvents,
                            isLoading: isLoadingFailureEvents
                        )
                    }
                }
                if retryable {
                    Button(Copy.retry) {
                        model.retry(item.id)
                    }
                    .buttonStyle(.bordered)
                }
            }
        case .queued, .uploading:
            EmptyView()
        default:
            // Converting/cleaning/classifying: the engine is spending real
            // provider tokens, so stopping must not require the context menu.
            Button(Copy.stop) {
                model.stop(item.id)
            }
            .buttonStyle(.borderless)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var qualityAction: some View {
        if let runID = item.runID {
            Button(Copy.qualityReport) {
                showQuality = true
                isLoadingQuality = true
                Task { @MainActor in
                    qualityReport = await model.qualityReport(runID: runID)
                    isLoadingQuality = false
                }
            }
            .buttonStyle(.link)
            .popover(isPresented: $showQuality) {
                QualityDetailsView(
                    filename: item.filename,
                    report: qualityReport,
                    isLoading: isLoadingQuality
                )
            }
        }
    }

    private var stageAccessibilityLabel: String {
        switch item.stage {
        case .queued: return Copy.stageWaiting
        case .uploading: return Copy.stageSending
        case .converting: return Copy.stageConverting
        case .cleaning: return Copy.stageCleaning
        case .classifying: return Copy.stageClassifying
        case .done: return Copy.stageSaved
        case .failed(let reason, _): return reason
        }
    }

    private var leadingSymbol: String {
        switch item.stage {
        case .done:
            return "checkmark.circle.fill"
        case .failed:
            return "exclamationmark.triangle.fill"
        default:
            return fileTypeSymbol
        }
    }

    private var leadingColor: Color {
        switch item.stage {
        case .done: return .green
        case .failed: return .orange
        default: return .secondary
        }
    }

    private var fileTypeSymbol: String {
        switch item.sourceURL.pathExtension.lowercased() {
        case "pdf", "docx", "doc", "pptx", "epub":
            return "doc.richtext"
        case "png", "jpg", "jpeg", "tiff", "heic":
            return "photo"
        case "srt", "vtt":
            return "waveform"
        default:
            return "doc.text"
        }
    }
}

// MARK: - Quality details

struct QualityDetailsView: View {
    let filename: String
    let report: RunQualityReport?
    let isLoading: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(filename)
                .font(.headline)
                .lineLimit(1)
            Text("Quality report")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Divider()
            if isLoading {
                ProgressView("Loading…").controlSize(.small)
            } else if let report {
                if let summary = report.extraction?.qualitySummary {
                    Text("\(summary.pages) pages · \(summary.pagesRequiringLayoutRepair) layout repairs · \(summary.pagesWithWarnings) pages flagged")
                    Text("\(summary.figureOcrLinesRemoved) figure OCR fragments separated · \(report.extraction?.figuresExtracted ?? 0) figures preserved")
                    if let restored = report.extraction?.figureReferencesRestored, restored > 0 {
                        Text("\(restored) figures restored after the PDF renderer omitted them")
                    }
                    if let captions = report.extraction?.figureCaptionTitlesRestored, captions > 0 {
                        Text("\(captions) figure captions repaired from PDF text")
                    }
                    if let removed = summary.sideFurnitureLinesRemoved, removed > 0 {
                        Text("\(removed) repeated margin fragments removed")
                    }
                    if let restored = summary.nativeBodyLinesRestored, restored > 0 {
                        Text("\(restored) missing prose lines restored from PDF text")
                    }
                    if let reused = report.extraction?.pagesReusedFromCache, reused > 0 {
                        Text("\(reused) pages reused from cache")
                    }
                }
                if let processing = report.processing {
                    Text("\(processing.chunks) chunks · \(processing.cachedCleanedChunks) reused · \(processing.sourcePreservedChunks) kept verbatim for fidelity")
                    if let verified = processing.verifiedSourceChunks, verified > 0 {
                        Text("\(verified) verified source chunks skipped model cleaning")
                    }
                    Text("\(processing.imageReferences) image references in final output")
                    if let unmatched = processing.tocEntriesWithoutMatchingHeadingOrBody,
                       unmatched > 0 {
                        Text("\(unmatched) contents or figure-list entries need review")
                    }
                    ForEach(processing.warnings, id: \.self) { warning in
                        Label(warning.replacingOccurrences(of: "-", with: " "), systemImage: "exclamationmark.triangle")
                    }
                }
                let flagged = (report.extraction?.pages ?? []).filter { !$0.warnings.isEmpty }
                if !flagged.isEmpty {
                    Divider()
                    Text("Pages to review")
                        .font(.subheadline.weight(.semibold))
                    ScrollView {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(Array(flagged.prefix(20))) { page in
                                Text("Page \(page.pageNumber): \(page.warnings.joined(separator: ", "))")
                                    .font(.caption)
                                    .textSelection(.enabled)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(maxHeight: 180)
                }
            } else {
                Text("No quality report is available for this run.")
                    .foregroundStyle(.secondary)
            }
        }
        .font(.caption)
        .padding(14)
        .frame(width: 460, alignment: .leading)
    }
}

// MARK: - Failure details

/// The engine's per-run event log for a failed row, so a failure is more than
/// one summarized line. Read-only; the last few events usually name the stage
/// and error that sank the file.
struct FailureDetailsView: View {
    let item: QueueItem
    let events: [RunEvent]
    var isLoading: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(item.filename)
                .font(.headline)
                .lineLimit(1)
                .truncationMode(.middle)
            if case .failed(let reason, _) = item.stage {
                Text(reason)
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            Divider()
            if isLoading && events.isEmpty {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            } else if events.isEmpty {
                Text(Copy.failureDetailsEmpty)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(events) { event in
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(event.stage)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                    .frame(width: 70, alignment: .leading)
                                Text(event.message)
                                    .font(.caption)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 220)
            }
        }
        .padding(14)
        .frame(width: 420)
    }
}

// MARK: - Empty state

struct EmptyQueueView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "arrow.down.doc")
                .font(.system(size: 52, weight: .light))
                .foregroundStyle(.secondary)
            Text(Copy.emptyTitle)
                .font(.title2.weight(.semibold))
            Text(Copy.emptyBody)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 400)
            Button(Copy.emptyButton) {
                model.presentChooseFilesPanel()
            }
            if !model.aiConfigured {
                SettingsLink {
                    Label(Copy.setupLink, systemImage: "sparkles")
                }
                .buttonStyle(.link)
                .padding(.top, 6)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Footer

struct FooterView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        HStack(spacing: 10) {
            aggregateStatus
            enginePill
            if let notice = model.okfSyncNotice {
                Label(notice, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(notice)
            }
            Spacer()
            if model.queue.contains(where: { $0.stage.isDone }) {
                Button(Copy.clearFinished) {
                    model.clearFinished()
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.bar)
    }

    @ViewBuilder
    private var aggregateStatus: some View {
        let total = model.queue.count
        let active = model.queue.filter { !$0.stage.isTerminal }.count
        let saved = model.queue.filter { $0.stage.isDone }.count
        if active > 0 {
            HStack(spacing: 8) {
                Text(Copy.footerActive(total - active + 1, of: total))
                    .font(.callout)
                ProgressView(
                    value: Double(total - active),
                    total: Double(max(total, 1))
                )
                .progressViewStyle(.linear)
                .frame(width: 140)
            }
        } else if saved > 0 {
            Text(Copy.footerIdle(saved))
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var enginePill: some View {
        switch model.backend.mode {
        case .starting:
            Label(Copy.engineStarting, systemImage: "hourglass")
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.yellow.opacity(0.2), in: Capsule())
        case .failed:
            HStack(spacing: 6) {
                Text(Copy.engineFailed)
                Button(Copy.engineFailedDetails) {
                    model.revealInFinder(BackendController.logFileURL)
                }
                .buttonStyle(.link)
            }
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(.red.opacity(0.15), in: Capsule())
        case .embedded, .external:
            // Healthy is silent; a dead target is not. A stale external
            // address or stopped engine must never be a silent black hole.
            if !model.serverOnline && model.hasRefreshedOnce {
                HStack(spacing: 6) {
                    Text(Copy.engineNotConnected)
                    SettingsLink {
                        Text(Copy.engineOpenSettings)
                    }
                    .buttonStyle(.link)
                }
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.red.opacity(0.15), in: Capsule())
            }
        }
    }
}

// MARK: - Drop overlay

struct DropOverlayView: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
            RoundedRectangle(cornerRadius: 16)
                .strokeBorder(style: StrokeStyle(lineWidth: 2, dash: [8, 6]))
                .foregroundStyle(.tint)
            VStack(spacing: 12) {
                Image(systemName: "arrow.down.doc.fill")
                    .font(.system(size: 44))
                Text(Copy.emptyTitle)
                    .font(.title3.weight(.semibold))
            }
            .foregroundStyle(.tint)
        }
        .padding(18)
        .allowsHitTesting(false)
    }
}
