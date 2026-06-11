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

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: leadingSymbol)
                .font(.title3)
                .foregroundStyle(leadingColor)
                .frame(width: 24)
                .transition(reduceMotion ? .opacity : .scale.combined(with: .opacity))
                .id(leadingSymbol)

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
            Button(Copy.showInFinder) {
                model.revealInFinder(outputURL)
            }
            .buttonStyle(.link)
            .transition(reduceMotion ? .opacity : .move(edge: .trailing).combined(with: .opacity))
        case .failed(_, let retryable):
            if retryable {
                Button(Copy.retry) {
                    model.retry(item.id)
                }
                .buttonStyle(.bordered)
            }
        default:
            EmptyView()
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
