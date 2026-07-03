import SwiftUI

/// One row in the Library window, flattened from either a browsed document or
/// a search match so the list renders both identically.
struct LibraryRow: Identifiable, Hashable {
    let id: String
    let documentID: String
    let filename: String
    let snippet: String?
    let classificationLabel: String?
}

/// Browse and search everything the engine has processed. Full-text search
/// runs on the engine (with word-stem matching), so "dividend" finds
/// "dividends". Rows offer Save a Copy (re-export into the destination
/// folder) and Delete.
struct LibraryView: View {
    @EnvironmentObject private var model: AppModel

    @State private var query = ""
    @State private var rows: [LibraryRow] = []
    @State private var isLoading = false
    @State private var notice: String?
    @State private var pendingDelete: LibraryRow?
    @State private var searchTask: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField(Copy.librarySearchPrompt, text: $query)
                    .textFieldStyle(.plain)
                    .onSubmit { scheduleSearch(immediate: true) }
                if isLoading {
                    ProgressView().controlSize(.small)
                } else if !query.isEmpty {
                    Button {
                        // onChange(of: query) below owns the reload; calling
                        // scheduleSearch here too would race it.
                        query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear search")
                }
            }
            .padding(10)
            .background(.bar)
            .onChange(of: query) {
                // Emptying the field (clear button, select-all-delete) shows
                // the full library immediately; typing debounces.
                scheduleSearch(immediate: query.isEmpty)
            }

            Divider()

            if rows.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: query.isEmpty ? "books.vertical" : "magnifyingglass")
                        .font(.system(size: 36, weight: .light))
                        .foregroundStyle(.secondary)
                    Text(query.isEmpty ? Copy.libraryEmpty : Copy.libraryNoMatches)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(rows) { row in
                    LibraryRowView(
                        row: row,
                        onSave: { save(row) },
                        onDelete: { pendingDelete = row }
                    )
                    .listRowSeparator(.visible)
                }
                .listStyle(.inset)
                .scrollContentBackground(.hidden)
            }

            if let notice {
                Divider()
                Text(notice)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .padding(8)
            }
        }
        .frame(minWidth: 560, minHeight: 380)
        .task { await reload() }
        .confirmationDialog(
            Copy.libraryDeleteConfirmTitle,
            isPresented: Binding(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } }
            ),
            presenting: pendingDelete
        ) { row in
            Button(role: .destructive) {
                delete(row)
            } label: {
                Text("Delete \"\(row.filename)\"")
            }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: { row in
            Text(Copy.libraryDeleteConfirmBody(row.filename))
        }
    }

    // MARK: - Data

    /// Debounce keystrokes; run immediately on submit/clear.
    private func scheduleSearch(immediate: Bool) {
        searchTask?.cancel()
        searchTask = Task { @MainActor in
            if !immediate {
                try? await Task.sleep(for: .milliseconds(300))
                if Task.isCancelled { return }
            }
            await reload()
        }
    }

    private func reload() async {
        isLoading = true
        defer { isLoading = false }
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            if trimmed.isEmpty {
                let page = try await model.client.listDocuments()
                rows = page.documents
                    .filter { $0.status == "ready" }
                    .map { document in
                        LibraryRow(
                            id: "doc-\(document.id)",
                            documentID: document.id,
                            filename: document.filename,
                            snippet: nil,
                            classificationLabel: nil
                        )
                    }
            } else {
                let results = try await model.client.search(query: trimmed)
                rows = results.map { result in
                    LibraryRow(
                        id: "hit-\(result.id)",
                        documentID: result.documentId,
                        filename: result.filename,
                        snippet: Self.plainSnippet(result.snippet),
                        classificationLabel: result.classificationLabel
                    )
                }
            }
            notice = nil
        } catch {
            // A newer keystroke cancelled this reload; keep showing the
            // current rows rather than blanking the list with an error the
            // replacement reload is about to overwrite.
            if Task.isCancelled { return }
            rows = []
            notice = Copy.userFacingReason(for: error.localizedDescription)
        }
    }

    /// The engine returns snippets HTML-escaped with `<mark>` highlight tags
    /// (for web clients); render them as plain text here. `&amp;` must be
    /// unescaped LAST: document text "&lt;" arrives as "&amp;lt;", and
    /// unescaping the ampersand first would double-unescape it to "<".
    static func plainSnippet(_ raw: String) -> String {
        raw
            .replacingOccurrences(of: "<mark>", with: "")
            .replacingOccurrences(of: "</mark>", with: "")
            .replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&#x27;", with: "'")
            .replacingOccurrences(of: "&amp;", with: "&")
            .replacingOccurrences(of: "\n", with: " ")
    }

    // MARK: - Actions

    private func save(_ row: LibraryRow) {
        let stem = (row.filename as NSString).deletingPathExtension
        Task { @MainActor in
            do {
                let written = try await model.exportDocumentToFolder(
                    documentID: row.documentID,
                    fallbackStem: stem
                )
                notice = "Saved \(written.lastPathComponent)"
                model.revealInFinder(written)
            } catch {
                notice = Copy.userFacingReason(for: error.localizedDescription)
            }
        }
    }

    private func delete(_ row: LibraryRow) {
        pendingDelete = nil
        Task { @MainActor in
            do {
                try await model.deleteDocument(id: row.documentID)
                rows.removeAll { $0.documentID == row.documentID }
                notice = "Deleted \(row.filename)"
            } catch {
                notice = Copy.userFacingReason(for: error.localizedDescription)
            }
        }
    }
}

struct LibraryRowView: View {
    let row: LibraryRow
    let onSave: () -> Void
    let onDelete: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "doc.text")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 24)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(row.filename)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let label = row.classificationLabel {
                        Text(label)
                            .font(.caption)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(.tint.opacity(0.12), in: Capsule())
                    }
                }
                if let snippet = row.snippet {
                    Text(snippet)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            Spacer()

            Button(Copy.librarySaveCopy, action: onSave)
                .buttonStyle(.link)
        }
        .frame(minHeight: 40)
        .contextMenu {
            Button(Copy.librarySaveCopy, action: onSave)
            Divider()
            Button(Copy.libraryDelete, role: .destructive, action: onDelete)
        }
    }
}
