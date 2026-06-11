import AppKit
import Combine
import Foundation

@MainActor
final class AppModel: ObservableObject {
    static let baseURLKey = "librarian.baseURL"
    static let apiKeyKey = "librarian.apiKey"
    static let useEmbeddedKey = "librarian.useEmbeddedBackend"
    static let outputFolderKey = "librarian.outputFolderPath"
    static let exportFormatKey = "librarian.exportFormat"
    static let keepOriginalsKey = "librarian.keepOriginals"
    static let defaultBaseURL = "http://127.0.0.1:8080"

    /// Items stuck in a non-terminal stage longer than this are failed.
    static let stageTimeout: TimeInterval = 15 * 60

    let backend = BackendController()

    @Published var queue: [QueueItem] = []
    @Published var serverOnline = false

    private var documents: [Document] = []
    private var runs: [Run] = []
    private var exportsInFlight: Set<UUID> = []
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

    /// Where cleaned files land. Default: ~/Documents/Librarian, created
    /// lazily when the first file is saved.
    var outputFolderURL: URL {
        if let path = UserDefaults.standard.string(forKey: Self.outputFolderKey),
           !path.isEmpty {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        let documentsDir = FileManager.default.urls(
            for: .documentDirectory, in: .userDomainMask
        ).first ?? FileManager.default.homeDirectoryForCurrentUser
        return documentsDir.appendingPathComponent("Librarian", isDirectory: true)
    }

    var exportFormat: ExportFormat {
        get {
            ExportFormat(
                rawValue: UserDefaults.standard.string(forKey: Self.exportFormatKey) ?? "md"
            ) ?? .markdown
        }
        set {
            UserDefaults.standard.set(newValue.rawValue, forKey: Self.exportFormatKey)
            objectWillChange.send()
        }
    }

    var keepOriginals: Bool {
        UserDefaults.standard.bool(forKey: Self.keepOriginalsKey)
    }

    /// Whether an AI provider has been connected; without one the engine
    /// converts and organizes documents without AI cleaning.
    var aiConfigured: Bool {
        EnvFile.read()["LIBRARIAN_LLM_PROVIDER"] == "openai-compatible"
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
        queue.contains { !$0.stage.isTerminal }
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

    /// Restart the embedded backend so configuration changes apply.
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
            // Still apply timeouts so nothing spins forever while offline.
            reconcileQueue()
            return
        }
        if hasActiveWork {
            do {
                async let documentsPage = client.listDocuments()
                async let runsPage = client.listRuns()
                let (loadedDocuments, loadedRuns) = try await (documentsPage, runsPage)
                documents = loadedDocuments.documents
                runs = loadedRuns.runs
            } catch {
                // Transient fetch errors leave the queue as-is; the timeout
                // below fails items that never make progress.
            }
            reconcileQueue()
        }
    }

    // MARK: - Adding files

    func handleDrop(of urls: [URL]) {
        for url in expandDroppedURLs(urls) {
            let item = QueueItem(
                id: UUID(),
                sourceURL: url,
                stage: .queued,
                documentID: nil,
                runID: nil,
                startedAt: Date()
            )
            queue.append(item)
            Task { await self.upload(itemID: item.id) }
        }
    }

    func presentChooseFilesPanel() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.prompt = "Add"
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
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory)
            else { continue }
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

    // MARK: - Pipeline

    private func upload(itemID: UUID) async {
        guard let index = queue.firstIndex(where: { $0.id == itemID }) else { return }
        let sourceURL = queue[index].sourceURL
        setStage(itemID, .uploading(progress: nil))
        // The engine restarts briefly when settings change; wait for it
        // instead of failing files dropped during the gap.
        await waitForEngine(seconds: 15)
        let client = self.client
        do {
            let contents = try await Task.detached(priority: .userInitiated) {
                try Data(contentsOf: sourceURL)
            }.value
            let document = try await client.uploadDocument(
                filename: sourceURL.lastPathComponent,
                contents: contents
            )
            update(itemID) { item in
                item.documentID = document.id
                item.startedAt = Date()
            }
            let run = try await client.createRun(documentId: document.id)
            update(itemID) { item in
                item.runID = run.id
                item.stage = .converting(progress: nil)
            }
        } catch {
            setStage(
                itemID,
                .failed(
                    reason: Copy.userFacingReason(for: error.localizedDescription),
                    retryable: true
                )
            )
        }
    }

    /// Wait briefly for the engine to come (back) up, e.g. across the
    /// restart that follows a settings change.
    private func waitForEngine(seconds: Double) async {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if case .starting = backend.mode {
                // Still booting; keep waiting.
            } else if let healthy = try? await client.health(), healthy {
                serverOnline = true
                return
            }
            try? await Task.sleep(for: .milliseconds(400))
        }
    }

    /// Fold backend documents and runs into queue stages, fire exports for
    /// finished documents, and time out items that stopped making progress.
    private func reconcileQueue() {
        let now = Date()
        for item in queue where !item.stage.isTerminal {
            if now.timeIntervalSince(item.startedAt) > Self.stageTimeout {
                setStage(item.id, .failed(reason: Copy.reasonTimeout, retryable: true))
                continue
            }
            guard let documentID = item.documentID else { continue }
            let run = runs.first { $0.documentId == documentID }
            let document = documents.first { $0.id == documentID }

            if let run, run.status == "failed" {
                setStage(
                    item.id,
                    .failed(reason: Copy.userFacingReason(for: run.error), retryable: true)
                )
                continue
            }
            if document?.status == "failed" {
                setStage(
                    item.id,
                    .failed(reason: Copy.userFacingReason(for: run?.error), retryable: true)
                )
                continue
            }
            if document?.status == "ready" {
                startExport(itemID: item.id, documentID: documentID)
                continue
            }
            if let run, run.isActive {
                setStage(item.id, stage(for: run))
            }
        }
    }

    private func stage(for run: Run) -> QueueItem.Stage {
        switch run.stage {
        case "clean", "validate", "assemble":
            return .cleaning(progress: min(max(run.fractionComplete, 0), 1))
        case "classify", "index", "complete":
            return .classifying(progress: nil)
        default:
            return .converting(progress: nil)
        }
    }

    private func startExport(itemID: UUID, documentID: String) {
        guard !exportsInFlight.contains(itemID) else { return }
        exportsInFlight.insert(itemID)
        let format = exportFormat
        let client = self.client
        Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.exportsInFlight.remove(itemID) }
            do {
                let data = try await client.exportRaw(documentId: documentID, format: format)
                await self.finishExport(itemID: itemID, data: data, format: format)
            } catch {
                self.setStage(
                    itemID,
                    .failed(
                        reason: Copy.userFacingReason(for: error.localizedDescription),
                        retryable: true
                    )
                )
            }
        }
    }

    private func finishExport(itemID: UUID, data: Data, format: ExportFormat) async {
        guard let item = queue.first(where: { $0.id == itemID }),
              !item.stage.isDone else { return }
        do {
            let folder = outputFolderURL
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let stem = item.sourceURL.deletingPathExtension().lastPathComponent
            let destination = collisionFreeURL(
                in: folder,
                stem: stem.isEmpty ? "document" : stem,
                fileExtension: format.fileExtension
            )
            try data.write(to: destination)
            if keepOriginals {
                let originalCopy = collisionFreeURL(
                    in: folder,
                    stem: item.sourceURL.deletingPathExtension().lastPathComponent,
                    fileExtension: item.sourceURL.pathExtension
                )
                try? FileManager.default.copyItem(at: item.sourceURL, to: originalCopy)
            }
            setStage(itemID, .done(outputURL: destination))
        } catch {
            setStage(
                itemID,
                .failed(
                    reason: Copy.userFacingReason(for: error.localizedDescription),
                    retryable: true
                )
            )
        }
    }

    /// Never overwrite, never ask: append " (2)", " (3)", …
    private func collisionFreeURL(in folder: URL, stem: String, fileExtension: String) -> URL {
        let suffix = fileExtension.isEmpty ? "" : ".\(fileExtension)"
        var candidate = folder.appendingPathComponent(stem + suffix)
        var counter = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = folder.appendingPathComponent("\(stem) (\(counter))\(suffix)")
            counter += 1
        }
        return candidate
    }

    // MARK: - Queue actions

    func retry(_ itemID: UUID) {
        update(itemID) { item in
            item.stage = .queued
            item.startedAt = Date()
            item.runID = nil
        }
        Task { [weak self] in
            guard let self, let item = self.queue.first(where: { $0.id == itemID }) else {
                return
            }
            await self.waitForEngine(seconds: 15)
            if let documentID = item.documentID {
                do {
                    let run = try await self.client.createRun(documentId: documentID)
                    self.update(itemID) { queued in
                        queued.runID = run.id
                        queued.stage = .converting(progress: nil)
                    }
                } catch {
                    // Backend may have lost the document; start over from the file.
                    self.update(itemID) { queued in queued.documentID = nil }
                    await self.upload(itemID: itemID)
                }
            } else {
                await self.upload(itemID: itemID)
            }
        }
    }

    func remove(_ itemID: UUID) {
        queue.removeAll { $0.id == itemID }
        exportsInFlight.remove(itemID)
    }

    func clearFinished() {
        queue.removeAll { $0.stage.isDone }
    }

    func revealInFinder(_ url: URL) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func openFile(_ url: URL) {
        NSWorkspace.shared.open(url)
    }

    func chooseOutputFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputFolderURL
        panel.prompt = "Choose"
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            UserDefaults.standard.set(url.path, forKey: Self.outputFolderKey)
            Task { @MainActor in
                AppDelegate.model?.objectWillChange.send()
            }
        }
    }

    // MARK: - Helpers

    private func update(_ itemID: UUID, _ mutate: (inout QueueItem) -> Void) {
        guard let index = queue.firstIndex(where: { $0.id == itemID }) else { return }
        mutate(&queue[index])
    }

    private func setStage(_ itemID: UUID, _ stage: QueueItem.Stage) {
        update(itemID) { $0.stage = stage }
    }
}
