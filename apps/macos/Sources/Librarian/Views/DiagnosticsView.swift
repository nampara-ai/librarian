import SwiftUI

/// Doctor checks, migrations, and backend information — the GUI face of
/// `librarian doctor`, `init`, `migrate`, and `version`.
struct DiagnosticsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    @State private var checks: [DoctorCheck] = []
    @State private var version: String?
    @State private var readiness: Readiness?
    @State private var isLoading = false
    @State private var actionOutput: String?
    @State private var isMigrating = false
    @State private var isReclaiming = false
    @State private var configText: String?

    /// What the readiness rows show while unknown: a spinner-stand-in when the
    /// engine is reachable, an honest "offline" when it is not — never an
    /// eternal "…".
    private var pendingDetail: String {
        model.serverOnline ? "…" : "offline"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Diagnostics")
                    .font(.title2.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(isLoading)
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }

            GroupBox("Backend") {
                VStack(alignment: .leading, spacing: 8) {
                    ChecklistRowView(
                        ok: model.serverOnline,
                        title: "Server",
                        detail: version.map { "v\($0)" } ?? pendingDetail
                    )
                    ChecklistRowView(
                        ok: readiness?.database == "ok",
                        title: "Database",
                        detail: readiness?.database ?? pendingDetail
                    )
                    ChecklistRowView(
                        ok: readiness?.storage == "ok",
                        title: "Storage",
                        detail: readiness?.storage ?? pendingDetail
                    )
                    ChecklistRowView(
                        ok: (readiness?.appliedMigrations ?? 0) > 0,
                        title: "Migrations",
                        detail: readiness.map { "\($0.appliedMigrations) applied" } ?? pendingDetail
                    )
                }
                .padding(6)
            }

            GroupBox("Capabilities") {
                if isLoading && checks.isEmpty {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text("Running doctor checks…")
                            .foregroundStyle(.secondary)
                    }
                    .padding(6)
                } else if checks.isEmpty {
                    Text(
                        BackendCLI.isAvailable
                            ? "Doctor checks unavailable."
                            : "Doctor checks need the packaged app with a bundled backend."
                    )
                    .foregroundStyle(.secondary)
                    .padding(6)
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(checks) { check in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Image(
                                    systemName: check.status == "ok"
                                        ? "checkmark.circle.fill"
                                        : "exclamationmark.circle.fill"
                                )
                                .foregroundStyle(check.status == "ok" ? .green : .orange)
                                Text(check.name)
                                    .frame(width: 130, alignment: .leading)
                                Text(check.capability)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Text(check.detail)
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                    .frame(maxWidth: 220, alignment: .trailing)
                            }
                            .font(.callout)
                        }
                    }
                    .padding(6)
                }
            }

            GroupBox("Maintenance") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 10) {
                        Button(isMigrating ? "Running…" : "Run Migrations") {
                            Task { await migrate() }
                        }
                        .disabled(isMigrating || !BackendCLI.isAvailable)
                        Button(isReclaiming ? "Reclaiming…" : "Reclaim Disk Space") {
                            Task { await reclaimDiskSpace() }
                        }
                        .disabled(isReclaiming || !BackendCLI.isAvailable)
                        .help(
                            "Removes cached work older than 30 days and compacts "
                                + "the database. Your documents are not touched."
                        )
                        Button("Restart Backend") {
                            Task {
                                await model.restartBackend()
                                await load()
                            }
                        }
                        .disabled(!BackendController.isEmbeddedAvailable ||
                            !model.useEmbeddedBackend)
                        Button("Data Folder") {
                            model.backend.revealDataFolder()
                        }
                        Button("Backend Log") {
                            model.revealInFinder(BackendController.logFileURL)
                        }
                    }
                    if let actionOutput {
                        Text(actionOutput)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(4)
                    }
                }
                .padding(6)
            }

            if BackendCLI.isAvailable {
                DisclosureGroup("Effective configuration") {
                    ScrollView {
                        Text(configText ?? "Loading…")
                            .font(.caption.monospaced())
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(6)
                    }
                    .frame(height: 180)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
                    .task {
                        if configText == nil {
                            await loadConfig()
                        }
                    }
                }
            }
        }
        .padding(20)
        .frame(width: 600)
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        version = try? await model.client.version()
        readiness = try? await model.client.ready()
        guard BackendCLI.isAvailable else { return }
        if let result = try? await BackendCLI.run(["doctor", "--json"]),
           let data = BackendCLI.jsonObject(in: result.output),
           let report = try? JSONDecoder().decode(DoctorReport.self, from: data) {
            checks = report.checks
        }
    }

    private func migrate() async {
        isMigrating = true
        defer { isMigrating = false }
        do {
            let result = try await BackendCLI.run(["migrate"])
            let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            actionOutput = text.isEmpty
                ? (result.succeeded ? "Migrations are up to date." : "migrate failed")
                : text
            readiness = try? await model.client.ready()
        } catch {
            actionOutput = error.localizedDescription
        }
    }

    /// Prune caches older than 30 days and compact the database. The engine's
    /// caches only speed up re-processing of identical content; removing old
    /// entries never loses documents.
    private func reclaimDiskSpace() async {
        isReclaiming = true
        defer { isReclaiming = false }
        do {
            let result = try await BackendCLI.run([
                "admin", "db-maintain", "--prune-cache-days", "30", "--vacuum",
            ])
            let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            actionOutput = text.isEmpty
                ? (result.succeeded ? "Done." : "Reclaim failed — see backend.log.")
                : text
        } catch {
            actionOutput = error.localizedDescription
        }
    }

    /// The engine's full effective configuration with secrets redacted,
    /// via `admin config --json` — the support answer to "what is it
    /// actually running with?".
    private func loadConfig() async {
        guard let result = try? await BackendCLI.run(["admin", "config", "--json"]),
              result.succeeded else {
            configText = "Configuration unavailable."
            return
        }
        let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        configText = text.isEmpty ? "Configuration unavailable." : text
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
