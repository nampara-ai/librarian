import SwiftUI

struct ActivityView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        List {
            if !model.uploads.isEmpty {
                Section {
                    ForEach(model.uploads) { item in
                        UploadRowView(item: item)
                    }
                } header: {
                    HStack {
                        Text("Uploads")
                        Spacer()
                        Button("Clear") {
                            model.clearFinishedUploads()
                        }
                        .font(.caption)
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                    }
                }
            }
            Section("Runs") {
                if model.runs.isEmpty {
                    Text("No processing runs yet")
                        .foregroundStyle(.secondary)
                }
                ForEach(model.runs) { run in
                    RunRowView(run: run)
                }
            }
        }
    }
}

struct UploadRowView: View {
    let item: UploadItem

    var body: some View {
        HStack(spacing: 8) {
            switch item.state {
            case .uploading:
                ProgressView()
                    .controlSize(.small)
            case .done:
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            case .failed:
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.red)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(item.filename)
                    .lineLimit(1)
                if case .failed(let message) = item.state {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

struct RunRowView: View {
    @EnvironmentObject private var model: AppModel
    let run: Run

    @State private var expanded = false
    @State private var events: [RunEvent] = []

    private var filename: String {
        model.documents.first { $0.id == run.documentId }?.filename ?? run.documentId
    }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            if events.isEmpty {
                Text("No events yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(events) { event in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(event.stage)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tint)
                    Text(event.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.leading, 4)
            }
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    statusIcon
                    Text(filename)
                        .lineLimit(1)
                }
                if run.isActive {
                    ProgressView(value: min(max(run.fractionComplete, 0), 1))
                        .progressViewStyle(.linear)
                }
                HStack {
                    Text(run.isActive ? run.stage : run.status)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if run.isActive {
                        Button("Cancel") {
                            Task { await model.cancelRun(id: run.id) }
                        }
                        .font(.caption)
                        .buttonStyle(.plain)
                        .foregroundStyle(.red)
                    } else if run.status == "failed" {
                        Button("Retry") {
                            Task { await model.retryRun(id: run.id) }
                        }
                        .font(.caption)
                        .buttonStyle(.plain)
                        .foregroundStyle(.tint)
                    }
                }
                if let error = run.error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
            }
        }
        .task(id: "\(expanded)-\(run.status)-\(run.completedChunks)") {
            guard expanded else { return }
            if let loaded = try? await model.client.runEvents(runId: run.id) {
                events = loaded
            }
        }
    }

    @ViewBuilder
    private var statusIcon: some View {
        switch run.status {
        case "succeeded":
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case "failed":
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        case "canceled":
            Image(systemName: "slash.circle.fill")
                .foregroundStyle(.gray)
        default:
            ProgressView()
                .controlSize(.small)
        }
    }
}
