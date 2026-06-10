import SwiftUI

struct DocumentDetailView: View {
    @EnvironmentObject private var model: AppModel
    let document: Document

    @State private var exported: ExportedDocument?
    @State private var loadError: String?
    @State private var confirmDelete = false

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
            content
        }
        .task(id: reloadKey) {
            await loadExport()
        }
        .toolbar {
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

                Button {
                    if let exported {
                        model.save(exported)
                    }
                } label: {
                    Label("Save As…", systemImage: "square.and.arrow.down")
                }
                .help("Save cleaned output as Markdown")
                .disabled(exported == nil)

                Button(role: .destructive) {
                    confirmDelete = true
                } label: {
                    Label("Delete", systemImage: "trash")
                }
                .help("Delete this document")
            }
        }
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
        if let exported {
            ScrollView {
                Text(exported.text)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
            }
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
        } else if let loadError {
            placeholder(
                systemImage: "doc.text",
                title: "No cleaned output yet",
                message: loadError
            )
        } else {
            placeholder(
                systemImage: "doc.text",
                title: "No cleaned output yet",
                message: "Press Process to clean and classify this document."
            )
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

    private func loadExport() async {
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
