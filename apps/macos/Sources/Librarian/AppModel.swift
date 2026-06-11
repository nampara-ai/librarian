import AppKit
import Combine
import Foundation

@MainActor
final class AppModel: ObservableObject {
    static let baseURLKey = "librarian.baseURL"
    static let apiKeyKey = "librarian.apiKey"
    static let useEmbeddedKey = "librarian.useEmbeddedBackend"
    static let outputFolderKey = "librarian.outputFolderPath"
    static let autoProcessKey = "librarian.autoProcessUploads"
    static let searchScopeKey = "librarian.searchScope"
    static let defaultBaseURL = "http://127.0.0.1:8080"

    /// Keep the uploads feed bounded so long sessions do not accumulate rows.
    static let maxUploadRows = 50

    let backend = BackendController()

    @Published var documents: [Document] = []
    @Published var runs: [Run] = []
    @Published var uploads: [UploadItem] = []
    @Published var serverOnline = false
    @Published var lastError: String?
    @Published var searchText = ""
    @Published var searchResults: [SearchResult] = []
    @Published var isSearching = false

    private var pollTask: Task<Void, Never>?
    private var backendObservation: AnyCancellable?

    init() {
        backendObservation = backend.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
    }

    // MARK: - Settings

    var useEmbeddedBackend: Bool {
        UserDefaults.standard.object(forKey: Self.useEmbeddedKey) as? Bool ?? true
    }

    /// Process uploads automatically after ingest (the CLI `--process` flag);
    /// off matches plain `librarian ingest`.
    var autoProcessUploads: Bool {
        UserDefaults.standard.object(forKey: Self.autoProcessKey) as? Bool ?? true
    }

    /// Destination folder for exported outputs. Defaults to ~/Downloads.
    var outputFolderURL: URL {
        if let path = UserDefaults.standard.string(forKey: Self.outputFolderKey),
           !path.isEmpty {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        return FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
    }

    var searchScope: String {
        UserDefaults.standard.string(forKey: Self.searchScopeKey) ?? "cleaned"
    }

    var client: APIClient {
        if useEmbeddedBackend, let embeddedURL = backend.embeddedBaseURL {
            return APIClient(baseURL: embeddedURL, apiKey: backend.embeddedAPIKey ?? "")
        }
        let raw = UserDefaults.standard.string(forKey: Self.baseURLKey) ?? Self.defaultBaseURL
        let url = URL(string: raw) ?? URL(string: Self.defaultBaseURL)!
        let key = UserDefaults.standard.string(forKey: Self.apiKeyKey) ?? ""
        return APIClient(baseURL: url, apiKey: key)
    }

    var hasActiveWork: Bool {
        runs.contains(where: \.isActive) || uploads.contains { $0.state == .uploading }
    }

    // MARK: - Lifecycle

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            if let self, self.useEmbeddedBackend {
                await self.backend.startEmbeddedIfNeeded()
            }
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                let interval: Duration = self.hasActiveWork ? .seconds(1) : .seconds(4)
                try? await Task.sleep(for: interval)
            }
        }
    }

    func applyBackendPreference() async {
        if useEmbeddedBackend {
            await backend.startEmbeddedIfNeeded()
        } else {
            backend.stop()
        }
        await refresh()
    }

    /// Restart the embedded backend so .env configuration changes apply.
    func restartBackend() async {
        guard useEmbeddedBackend, BackendController.isEmbeddedAvailable else { return }
        await backend.restart()
        await refresh()
    }

    func shutDown() {
        pollTask?.cancel()
        pollTask = nil
        backend.stop()
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

    // MARK: - Ingest (drag and drop, Import panel)

    func handleDrop(of urls: [URL]) {
        for url in expandDroppedURLs(urls) {
            let item = UploadItem(id: UUID(), filename: url.lastPathComponent, state: .uploading)
            uploads.insert(item, at: 0)
            if uploads.count > Self.maxUploadRows {
                uploads.removeLast(uploads.count - Self.maxUploadRows)
            }
            Task { await self.upload(url: url, itemID: item.id) }
        }
    }

    /// `librarian import` equivalent: pick files or folders and ingest them.
    func presentImportPanel() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.message = "Choose files or folders to add to your library"
        panel.prompt = "Import"
        panel.begin { [weak self] response in
            guard response == .OK else { return }
            let urls = panel.urls
            Task { @MainActor [weak self] in
                self?.handleDrop(of: urls)
            }
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
            if autoProcessUploads {
                _ = try await client.createRun(documentId: document.id)
            }
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

    // MARK: - Document and run actions

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

    func cancelRun(id: String) async {
        do {
            _ = try await client.cancelRun(id: id)
            await refresh()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func retryRun(id: String) async {
        do {
            _ = try await client.retryRun(id: id)
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
            searchResults = try await client.search(query: query, scope: searchScope)
        } catch {
            searchResults = []
        }
    }

    // MARK: - Export

    /// Export to the configured output folder without a save panel; returns
    /// the written file URL.
    @discardableResult
    func exportToOutputFolder(
        document: Document,
        format: ExportFormat,
        citationQuote: String? = nil
    ) async -> URL? {
        do {
            let data = try await client.exportRaw(
                documentId: document.id,
                format: format,
                citationQuote: citationQuote
            )
            let folder = outputFolderURL
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let base = (document.filename as NSString).deletingPathExtension
            let stem = base.isEmpty ? document.id : base
            var destination = folder.appendingPathComponent("\(stem).\(format.fileExtension)")
            var counter = 2
            while FileManager.default.fileExists(atPath: destination.path) {
                destination = folder.appendingPathComponent(
                    "\(stem)-\(counter).\(format.fileExtension)"
                )
                counter += 1
            }
            try data.write(to: destination)
            return destination
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    /// Export every ready document to the output folder.
    func exportAll(format: ExportFormat) async -> Int {
        var exported = 0
        for document in documents where document.status == "ready" {
            if await exportToOutputFolder(document: document, format: format) != nil {
                exported += 1
            }
        }
        return exported
    }

    /// Export via a save panel (user picks the destination).
    func saveAs(document: Document, format: ExportFormat, citationQuote: String? = nil) {
        let base = (document.filename as NSString).deletingPathExtension
        let panel = NSSavePanel()
        panel.nameFieldStringValue =
            (base.isEmpty ? "export" : base) + "." + format.fileExtension
        panel.canCreateDirectories = true
        panel.directoryURL = outputFolderURL
        let client = self.client
        let documentId = document.id
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            Task {
                do {
                    let data = try await client.exportRaw(
                        documentId: documentId,
                        format: format,
                        citationQuote: citationQuote
                    )
                    try data.write(to: url)
                } catch {
                    await MainActor.run {
                        AppDelegate.model?.lastError = error.localizedDescription
                    }
                }
            }
        }
    }

    func chooseOutputFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputFolderURL
        panel.message = "Choose where exported outputs are saved"
        panel.prompt = "Choose"
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            UserDefaults.standard.set(url.path, forKey: Self.outputFolderKey)
            Task { @MainActor in
                AppDelegate.model?.objectWillChange.send()
            }
        }
    }

    func revealInFinder(_ url: URL) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func copyToPasteboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
