import SwiftUI

struct ServerStatusView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showPanel = false

    var body: some View {
        Button {
            showPanel.toggle()
        } label: {
            HStack(spacing: 6) {
                Circle()
                    .fill(model.serverOnline ? Color.green : Color.red)
                    .frame(width: 8, height: 8)
                Text(model.serverOnline ? "Connected" : "Offline")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(.quaternary.opacity(0.5), in: Capsule())
        }
        .buttonStyle(.plain)
        .help("Backend status")
        .popover(isPresented: $showPanel, arrowEdge: .bottom) {
            ServerPanelView()
                .environmentObject(model)
        }
    }
}

struct ServerPanelView: View {
    @EnvironmentObject private var model: AppModel
    @State private var readiness: Readiness?
    @State private var version: String?

    private var baseURL: String {
        UserDefaults.standard.string(forKey: AppModel.baseURLKey) ?? AppModel.defaultBaseURL
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Backend")
                .font(.headline)

            if model.serverOnline {
                ChecklistRowView(
                    ok: true,
                    title: "Server",
                    detail: version.map { "v\($0)" } ?? baseURL
                )
                ChecklistRowView(
                    ok: readiness?.database == "ok",
                    title: "Database",
                    detail: readiness?.database ?? "checking…"
                )
                ChecklistRowView(
                    ok: readiness?.storage == "ok",
                    title: "Storage",
                    detail: readiness?.storage ?? "checking…"
                )
                ChecklistRowView(
                    ok: (readiness?.appliedMigrations ?? 0) > 0,
                    title: "Migrations",
                    detail: readiness.map { "\($0.appliedMigrations) applied" } ?? "checking…"
                )
            } else {
                Label("Not reachable at \(baseURL)", systemImage: "bolt.slash")
                    .font(.callout)
                VStack(alignment: .leading, spacing: 6) {
                    Text("Start the backend in a terminal:")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack {
                        Text("librarian api")
                            .font(.callout.monospaced())
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
                        Button {
                            model.copyToPasteboard("librarian api")
                        } label: {
                            Image(systemName: "doc.on.doc")
                        }
                        .buttonStyle(.plain)
                        .help("Copy command")
                    }
                }
            }

            Divider()
            Text("Change the server URL and API key in Settings (⌘,).")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(16)
        .frame(width: 300)
        .task {
            readiness = try? await model.client.ready()
            version = try? await model.client.version()
        }
    }
}

struct ChecklistRowView: View {
    let ok: Bool
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(ok ? .green : .orange)
            Text(title)
            Spacer()
            Text(detail)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .font(.callout)
    }
}
