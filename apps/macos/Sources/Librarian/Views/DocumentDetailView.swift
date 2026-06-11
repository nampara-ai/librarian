import SwiftUI

struct DocumentDetailView: View {
    @EnvironmentObject private var model: AppModel
    let document: Document

    enum Pane: String, CaseIterable, Identifiable {
        case cleaned = "Cleaned"
        case original = "Original"

        var id: String { rawValue }
    }

    /// Cap what we render so multi-megabyte outputs do not balloon the UI;
    /// exports always contain the full text.
    private static let displayCharacterLimit = 400_000

    @State private var pane: Pane = .cleaned
    @State private var exported: ExportedDocument?
    @State private var originalPage: ContentPage?
    @State private var loadError: String?
    @State private var confirmDelete = false
    @State private var askCitation = false
    @State private var citationQuote = ""
    @State private var citationFormat: ExportFormat = .markdown
    @State private var exportNotice: String?

    private var latestRun: Run? {
        model.runs.first { $0.documentId == document.id }
    }

    private var reloadKey: String {
        "\(document.id)-\(latestRun?.status ?? "none")"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            Picker("View", selection: $pane) {
                ForEach(Pane.allCases) { candidate in
                    Text(candidate.rawValue).tag(candidate)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 220)
            .padding(.horizontal)
            .padding(.vertical, 8)
            content
        }
        .task(id: reloadKey) {
            await loadCleaned()
        }
        .task(id: "\(document.id)-\(pane.rawValue)") {
            if pane == .original && originalPage?.documentId != document.id {
                originalPage = try? await model.client.content(documentId: document.id)
            }
        }
        .toolbar { toolbarContent }
        .confirmationDialog(
            "Delete “\(document.filename)”?",
            isPresented: $confirmDelete
        ) {
            Button("Delete", role: .destructive) {
                Task { await model.delete(documentId: document.id) }
            }
        } message: {
            Text("This removes the document, its runs, and its cleaned output.")
        }
        .alert("Export with Citation", isPresented: $askCitation) {
            TextField("Quoted source phrase", text: $citationQuote)
            Button("Export") {
                let quote = citationQuote
                let format = citationFormat
                Task {
                    if let url = await model.exportToOutputFolder(
                        document: document, format: format, citationQuote: quote
                    ) {
                        exportNotice = "Saved \(url.lastPathComponent)"
                    }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Adds quote-grounded transcript citation evidence when the source is a timestamped transcript.")
        }
        .overlay(alignment: .bottom) {
            if let exportNotice {
                Label(exportNotice, systemImage: "checkmark.circle.fill")
                    .font(.callout)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(.regularMaterial, in: Capsule())
                    .padding(.bottom, 10)
                    .task {
                        try? await Task.sleep(for: .seconds(3))
                        self.exportNotice = nil
                    }
            }
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Button {
                Task { await model.process(documentId: document.id) }
            } label: {
                Label("Process", systemImage: "arrow.clockwise")
            }
            .help("Run cleaning and classification again")
            .disabled(latestRun?.isActive == true)

            Button {
                if let exported {
                    model.copyToPasteboard(exported.text)
                }
            } label: {
                Label("Copy", systemImage: "doc.on.doc")
            }
            .help("Copy cleaned output")
            .disabled(exported == nil)

            Menu {
                Section("To output folder") {
                    ForEach(ExportFormat.allCases) { format in
                        Button(format.label) {
                            Task {
                                if let url = await model.exportToOutputFolder(
                                    document: document, format: format
                                ) {
                                    exportNotice = "Saved \(url.lastPathComponent)"
                                }
                            }
                        }
                    }
                }
                Section {
                    ForEach(ExportFormat.allCases) { format in
                        Button("Save As… (\(format.label))") {
                            model.saveAs(document: document, format: format)
                        }
                    }
                }
                Section {
                    Button("With Transcript Citation…") {
                        citationFormat = .markdown
                        citationQuote = ""
                        askCitation = true
                    }
                    Button("Show Output Folder") {
                        model.revealInFinder(model.outputFolderURL)
                    }
                }
            } label: {
                Label("Export", systemImage: "square.and.arrow.down")
            }
            .help("Export cleaned output")
            .disabled(document.status != "ready")

            Button(role: .destructive) {
                confirmDelete = true
            } label: {
                Label("Delete", systemImage: "trash")
            }
            .help("Delete this document")
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(document.filename)
                .font(.title2.weight(.semibold))
                .lineLimit(2)
            HStack(spacing: 8) {
                StatusBadgeView(status: document.status)
                if let classification = exported?.classification {
                    Text(classification)
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(.tint.opacity(0.12), in: Capsule())
                        .foregroundStyle(.tint)
                }
                Text(document.id)
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
                Text(
                    ByteCountFormatter.string(
                        fromByteCount: Int64(document.byteSize), countStyle: .file
                    )
                )
                .font(.caption)
                .foregroundStyle(.tertiary)
            }
            if let run = latestRun, run.isActive {
                VStack(alignment: .leading, spacing: 4) {
                    ProgressView(value: min(max(run.fractionComplete, 0), 1))
                        .progressViewStyle(.linear)
                    Text("\(run.stage) · \(run.completedChunks)/\(max(run.totalChunks, 1)) chunks")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 4)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var content: some View {
        switch pane {
        case .cleaned:
            cleanedContent
        case .original:
            originalContent
        }
    }

    @ViewBuilder
    private var cleanedContent: some View {
        if let exported {
            textScroller(exported.text)
        } else if let run = latestRun, run.isActive {
            placeholder(
                systemImage: "gearshape.2",
                title: "Processing…",
                message: "Cleaned output will appear here when the run finishes."
            )
        } else if let run = latestRun, run.status == "failed" {
            placeholder(
                systemImage: "exclamationmark.triangle",
                title: "Processing failed",
                message: run.error ?? "Check the activity panel for run events."
            )
        } else {
            placeholder(
                systemImage: "doc.text",
                title: "No cleaned output yet",
                message: loadError ?? "Press Process to clean and classify this document."
            )
        }
    }

    @ViewBuilder
    private var originalContent: some View {
        if let originalPage {
            VStack(alignment: .leading, spacing: 0) {
                if originalPage.truncated {
                    Text(
                        "Showing the first \(originalPage.text.count.formatted()) of "
                            + "\(originalPage.totalChars.formatted()) characters."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal)
                    .padding(.top, 6)
                }
                textScroller(originalPage.text)
            }
        } else {
            placeholder(
                systemImage: "doc.plaintext",
                title: "Loading original…",
                message: "The extracted source text appears here."
            )
        }
    }

    private func textScroller(_ text: String) -> some View {
        let capped = text.count > Self.displayCharacterLimit
            ? String(text.prefix(Self.displayCharacterLimit))
            : text
        return ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                Text(capped)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if capped.count < text.count {
                    Text("Display truncated — export to get the full text.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding()
        }
    }

    private func placeholder(systemImage: String, title: String, message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: systemImage)
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.headline)
            Text(message)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func loadCleaned() async {
        do {
            exported = try await model.client.export(documentId: document.id)
            loadError = nil
        } catch {
            exported = nil
            loadError = error.localizedDescription
        }
    }
}

struct StatusBadgeView: View {
    let status: String

    var body: some View {
        Text(status)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }

    private var color: Color {
        switch status {
        case "ready":
            return .green
        case "failed":
            return .red
        case "processing":
            return .orange
        default:
            return .gray
        }
    }
}
