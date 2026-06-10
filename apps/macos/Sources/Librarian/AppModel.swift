import AppKit
import Foundation

@MainActor
final class AppModel: ObservableObject {
    static let baseURLKey = "librarian.baseURL"
    static let apiKeyKey = "librarian.apiKey"
    static let defaultBaseURL = "http://127.0.0.1:8080"

    @Published var documents: [Document] = []
    @Published var runs: [Run] = []
    @Published var uploads: [UploadItem] = []
    @Published var serverOnline = false
    @Published var lastError: String?
    @Published var searchText = ""
    @Published var searchResults: [SearchResult] = []
    @Published var isSearching = false

    private var pollTask: Task<Void, Never>?

    var client: APIClient {
        let raw = UserDefaults.standard.string(forKey: Self.baseURLKey) ?? Self.defaultBaseURL
        let url = URL(string: raw) ?? URL(string: Self.defaultBaseURL)!
        let key = UserDefaults.standard.string(forKey: Self.apiKeyKey) ?? ""
        return APIClient(baseURL: url, apiKey: key)
    }

    var hasActiveWork: Bool {
        runs.contains(where: \.isActive) || uploads.contains { $0.state == .uploading }
    }

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                let interval: Duration = self.hasActiveWork ? .seconds(1) : .seconds(4)
                try? await Task.sleep(for: interval)
            }
        }
    }

    func refresh() async {
        let client = self.client
        do {
            serverOnline = try await client.health()
        } catch {
            serverOnline = false
            return
        }
        do {
            async let documentsPage = client.listDocuments()
            async let runsPage = client.listRuns()
            let (loadedDocuments, loadedRuns) = try await (documentsPage, runsPage)
            documents = loadedDocuments.documents
            runs = loadedRuns.runs
        } catch {
            lastError = error.localizedDescription
        }
    }

    // MARK: - Drag and drop ingest

    func handleDrop(of urls: [URL]) {
        for url in expandDroppedURLs(urls) {
            let item = UploadItem(id: UUID(), filename: url.lastPathComponent, state: .uploading)
            uploads.insert(item, at: 0)
            Task { await self.upload(url: url, itemID: item.id) }
        }
    }

    private func expandDroppedURLs(_ urls: [URL], limit: Int = 200) -> [URL] {
        var files: [URL] = []
        for url in urls {
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
                continue
            }
            if isDirectory.boolValue {
                let enumerator = FileManager.default.enumerator(
                    at: url,
                    includingPropertiesForKeys: [.isRegularFileKey],
                    options: [.skipsHiddenFiles, .skipsPackageDescendants]
                )
                while let child = enumerator?.nextObject() as? URL {
                    guard files.count < limit else { break }
                    let isFile = (try? child.resourceValues(forKeys: [.isRegularFileKey]))?
                        .isRegularFile
                    if isFile == true {
                        files.append(child)
                    }
                }
            } else {
                files.append(url)
            }
            if files.count >= limit { break }
        }
        return files
    }

    private func upload(url: URL, itemID: UUID) async {
        let client = self.client
        do {
            let contents = try await Task.detached(priority: .userInitiated) {
                try Data(contentsOf: url)
            }.value
            let document = try await client.uploadDocument(
                filename: url.lastPathComponent,
                contents: contents
            )
            _ = try await client.createRun(documentId: document.id)
            setUploadState(itemID, to: .done)
            await refresh()
        } catch {
            setUploadState(itemID, to: .failed(error.localizedDescription))
        }
    }

    private func setUploadState(_ id: UUID, to state: UploadItem.State) {
        guard let index = uploads.firstIndex(where: { $0.id == id }) else { return }
        uploads[index].state = state
    }

    func clearFinishedUploads() {
        uploads.removeAll { $0.state != .uploading }
    }

    // MARK: - Document actions

    func process(documentId: String) async {
        do {
            _ = try await client.createRun(documentId: documentId)
            await refresh()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func delete(documentId: String) async {
        do {
            try await client.deleteDocument(id: documentId)
            await refresh()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func runSearch() async {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            searchResults = []
            return
        }
        isSearching = true
        defer { isSearching = false }
        do {
            searchResults = try await client.search(query: query)
        } catch {
            searchResults = []
        }
    }

    // MARK: - Output helpers

    func save(_ exported: ExportedDocument) {
        let panel = NSSavePanel()
        let base = (exported.filename as NSString).deletingPathExtension
        panel.nameFieldStringValue = (base.isEmpty ? "export" : base) + ".md"
        panel.canCreateDirectories = true
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            try? exported.text.write(to: url, atomically: true, encoding: .utf8)
        }
    }

    func copyToPasteboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
