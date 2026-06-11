import SwiftUI

struct SidebarView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var selection: String?
    @AppStorage(AppModel.searchScopeKey) private var scope = "cleaned"

    var body: some View {
        List(selection: $selection) {
            if !model.searchText.isEmpty {
                Picker("Search in", selection: $scope) {
                    Text("Cleaned output").tag("cleaned")
                    Text("Original text").tag("raw")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .listRowSeparator(.hidden)
                .onChange(of: scope) {
                    Task { await model.runSearch() }
                }
            }
            if model.searchText.isEmpty {
                Section("Library") {
                    if model.documents.isEmpty {
                        Text("No documents yet")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(model.documents) { document in
                        DocumentRowView(document: document)
                            .tag(document.id)
                    }
                }
            } else {
                Section("Results") {
                    if model.isSearching && model.searchResults.isEmpty {
                        ProgressView()
                            .controlSize(.small)
                    } else if model.searchResults.isEmpty {
                        Text("No matches")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(model.searchResults) { result in
                        SearchResultRowView(result: result)
                            .tag(result.documentId)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .searchable(text: $model.searchText, placement: .sidebar, prompt: "Search library")
        .task(id: model.searchText) {
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            await model.runSearch()
        }
        .navigationTitle("Librarian")
    }
}

struct DocumentRowView: View {
    let document: Document

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(document.filename)
                    .lineLimit(1)
                Text("\(document.status) · \(byteString)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    private var statusColor: Color {
        switch document.status {
        case "ready":
            return .green
        case "failed":
            return .red
        case "processing":
            return .orange
        default:
            return .secondary
        }
    }

    private var byteString: String {
        ByteCountFormatter.string(
            fromByteCount: Int64(document.byteSize),
            countStyle: .file
        )
    }
}

struct SearchResultRowView: View {
    let result: SearchResult

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(result.filename)
                .lineLimit(1)
            Text(result.snippet.strippingSearchMarkup())
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(3)
            if let label = result.classificationLabel, let code = result.classificationCode {
                Text("\(code) · \(label)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 2)
    }
}

extension String {
    func strippingSearchMarkup() -> String {
        replacingOccurrences(of: "<mark>", with: "")
            .replacingOccurrences(of: "</mark>", with: "")
            .replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&amp;", with: "&")
    }
}
