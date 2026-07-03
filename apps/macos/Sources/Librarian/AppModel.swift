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

    /// Client-side upload ceiling, mirroring the backend's 100 MiB limit so an
    /// oversize file is rejected instantly instead of being read into RAM and
    /// streamed only to be refused by the server.
    static let maxUploadBytes = 100 * 1024 * 1024

    let backend = BackendController()

    @Published var queue: [QueueItem] = []
    @Published var serverOnline = false
    /// True once the first health probe has completed, so disconnect
    /// indicators don't flash during launch.
    @Published var hasRefreshedOnce = false

    private var documents: [Document] = []
    private var runs: [Run] = []
    private var exportsInFlight: Set<UUID> = []
    private var okfSyncTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var backendObservation: AnyCancellable?

    /// Bounded upload scheduling: a large drop must not launch one upload (each
    /// with its own health-poll wait) per file. Uploads beyond
    /// `maxConcurrentUploads` wait in `pendingUploads` until a slot frees up.
    static let maxConcurrentUploads = 4
    private var pendingUploads: [UUID] = []
    private var activeUploadCount = 0

    /// Set when a folder drop was truncated at the expansion limit, so the UI
    /// can tell the user that some files were skipped rather than silently
    /// dropping them.
    @Published var skippedFilesNotice: String?

    /// Bundle-mode sync status: set when the OKF bundle write failed or left
    /// documents out, cleared on the next fully successful sync. Rows are
    /// marked Saved before the (debounced) bundle write, so without this a
    /// persistently failing sync would be invisible.
    @Published var okfSyncNotice: String?

    init() {
        backendObservation = backend.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
    }

    // MARK: - Settings

    var useEmbeddedBackend: Bool {
        let stored = UserDefaults.standard.object(forKey: Self.useEmbeddedKey) as? Bool ?? true
        if !stored && BackendController.isEmbeddedAvailable {
            // Self-heal: external mode without a usable server address is
            // unreachable by construction (a stale preference can survive
            // reinstalls); fall back to the built-in engine.
            let raw = UserDefaults.standard.string(forKey: Self.baseURLKey) ?? ""
            let usable = URL(string: raw)?.scheme?.hasPrefix("http") == true
            if !usable {
                UserDefaults.standard.set(true, forKey: Self.useEmbeddedKey)
                return true
            }
        }
        return stored
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

    /// A base URL that resolves to nowhere, used while the embedded engine is
    /// still starting so the client fails fast instead of sending files and the
    /// per-launch API key to whatever stranger happens to answer on a guessed
    /// candidate port (e.g. 8765).
    private static let unresolvedEmbeddedURL = URL(string: "http://127.0.0.1:1")!

    var client: APIClient {
        if useEmbeddedBackend && BackendController.isEmbeddedAvailable {
            // Embedded mode must never send real traffic to a *guessed* port:
            // the engine might launch on a different candidate port, and a
            // stranger already listening on the guess would receive our files
            // and per-launch key. Only target the port the controller actually
            // confirmed healthy (embeddedBaseURL). Until then, return a client
            // pointed at a dead address that fails fast — and withhold the
            // per-launch key entirely; callers wait for the engine via
            // waitForEngine() and retry once it is confirmed.
            if let embeddedURL = backend.embeddedBaseURL {
                return APIClient(baseURL: embeddedURL, apiKey: backend.embeddedAPIKey ?? "")
            }
            return APIClient(baseURL: Self.unresolvedEmbeddedURL, apiKey: "")
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
            // Graceful async stop: the sync variant busy-waits up to 3 s and
            // would beach-ball the UI when toggled from Settings.
            await backend.stopGracefully()
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
        defer { hasRefreshedOnce = true }
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
        let (files, skipped) = expandDroppedURLs(urls)
        for url in files {
            let item = QueueItem(
                id: UUID(),
                sourceURL: url,
                stage: .queued,
                documentID: nil,
                runID: nil,
                startedAt: Date()
            )
            queue.append(item)
            enqueueUpload(item.id)
        }
        if skipped > 0 {
            // Surface the truncation instead of silently dropping files.
            skippedFilesNotice =
                "Added the first \(files.count) files; \(skipped) more were skipped. "
                + "Drop them in a smaller batch to process the rest."
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

    /// Expand dropped URLs (recursing into folders) into a flat file list,
    /// capped at `limit`. Returns the accepted files plus a count of how many
    /// additional files were skipped because the cap was hit, so the caller can
    /// tell the user instead of silently truncating.
    private func expandDroppedURLs(_ urls: [URL], limit: Int = 200) -> (files: [URL], skipped: Int) {
        var files: [URL] = []
        var skipped = 0
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
                    let isFile = (try? child.resourceValues(forKeys: [.isRegularFileKey]))?
                        .isRegularFile
                    guard isFile == true else { continue }
                    if files.count < limit {
                        files.append(child)
                    } else {
                        // Keep counting so the "N skipped" message is accurate.
                        skipped += 1
                    }
                }
            } else if files.count < limit {
                files.append(url)
            } else {
                skipped += 1
            }
        }
        return (files, skipped)
    }

    // MARK: - Pipeline

    /// Schedule an upload through the bounded runner: start immediately if a
    /// slot is free, otherwise queue it. This prevents a large drop from
    /// launching hundreds of simultaneous uploads and health polls.
    private func enqueueUpload(_ itemID: UUID) {
        pendingUploads.append(itemID)
        startNextUploadsIfPossible()
    }

    /// Fill any free upload slots from the pending queue.
    private func startNextUploadsIfPossible() {
        while activeUploadCount < Self.maxConcurrentUploads, !pendingUploads.isEmpty {
            let itemID = pendingUploads.removeFirst()
            activeUploadCount += 1
            Task { [weak self] in
                guard let self else { return }
                await self.upload(itemID: itemID)
                self.activeUploadCount -= 1
                // A slot freed up; pull in the next waiting upload.
                self.startNextUploadsIfPossible()
            }
        }
    }

    private func upload(itemID: UUID) async {
        guard let index = queue.firstIndex(where: { $0.id == itemID }) else { return }
        // A queued item can be stopped while waiting for an upload slot; the
        // slot runner must not resurrect it into .uploading.
        guard isStillActive(itemID) else { return }
        let sourceURL = queue[index].sourceURL
        setStage(itemID, .uploading(progress: nil))
        // Reject oversize files locally before reading a byte, so we neither
        // buffer a huge file in RAM nor waste an upload the server will refuse.
        if let size = try? FileManager.default.attributesOfItem(atPath: sourceURL.path)[.size]
            as? Int, size > Self.maxUploadBytes {
            setStage(
                itemID,
                .failed(reason: "File is too large", retryable: false)
            )
            return
        }
        // The engine restarts briefly when settings change; wait for it
        // instead of failing files dropped during the gap.
        await waitForEngine(seconds: 15)
        let client = self.client
        do {
            // Stream from disk rather than buffering the whole file (and again
            // as a multipart Data) in memory.
            let document = try await client.uploadDocument(
                filename: sourceURL.lastPathComponent,
                fileURL: sourceURL
            )
            // The user may have stopped or removed the row while the upload
            // was in flight; a late completion must not revive it.
            guard isStillActive(itemID) else { return }
            update(itemID) { item in
                item.documentID = document.id
                item.startedAt = Date()
            }
            let run = try await client.createRun(documentId: document.id)
            guard isStillActive(itemID) else {
                // Stopped in the createRun window: the row never learned this
                // run's id, so Stop couldn't cancel it — cancel it here.
                Task { _ = try? await client.cancelRun(id: run.id) }
                return
            }
            update(itemID) { item in
                item.runID = run.id
                item.stage = .converting(progress: nil)
            }
        } catch {
            guard isStillActive(itemID) else { return }
            setStage(
                itemID,
                .failed(
                    reason: Copy.userFacingReason(for: error.localizedDescription),
                    retryable: true
                )
            )
        }
    }

    /// Whether the row still exists and has not been stopped/finished, so
    /// late async completions don't overwrite a user action.
    private func isStillActive(_ itemID: UUID) -> Bool {
        guard let item = queue.first(where: { $0.id == itemID }) else { return false }
        return !item.stage.isTerminal
    }

    /// Wait for the engine to come (back) up, e.g. across the restart that
    /// follows a settings change. While the embedded engine is still booting
    /// the wait is open-ended — boot has its own bounded deadline in
    /// BackendController — so files dropped during a slow first launch
    /// (cold start, migrations) don't fail before the engine ever answers.
    private func waitForEngine(seconds: Double) async {
        while case .starting = backend.mode {
            try? await Task.sleep(for: .milliseconds(400))
        }
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if case .starting = backend.mode {
                // A restart began mid-wait; defer to its own deadline again.
                try? await Task.sleep(for: .milliseconds(400))
                continue
            }
            if let healthy = try? await client.health(), healthy {
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
            // Prefer the exact run we started for this item (item.runID); a
            // document can accumulate several runs across retries, and matching
            // only by document could pick a stale one. Fall back to the latest
            // run for the document when we don't yet know the run id.
            let run = item.runID.flatMap { id in runs.first { $0.id == id } }
                ?? runs.first { $0.documentId == documentID }
            let document = documents.first { $0.id == documentID }

            if let run, run.status == "failed" {
                setStage(
                    item.id,
                    .failed(reason: Copy.userFacingReason(for: run.error), retryable: true)
                )
                continue
            }
            if let run, run.status == "canceled" {
                // Canceled from this app or externally (CLI/API): stop the row
                // instead of letting it spin until the stage timeout.
                setStage(item.id, .failed(reason: Copy.reasonStopped, retryable: true))
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
        // In OKF-bundle mode the destination folder *is* the bundle: the
        // document is processed, so mark it saved and (debounced) rebuild the
        // whole bundle from the engine. reconcileQueue skips terminal items, so
        // this fires once per document.
        if exportFormat.isBundle {
            setStage(itemID, .done(outputURL: outputFolderURL))
            scheduleOkfBundleSync()
            return
        }
        exportsInFlight.insert(itemID)
        let format = exportFormat
        let client = self.client
        Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.exportsInFlight.remove(itemID) }
            do {
                let export = try await client.exportRaw(documentId: documentID, format: format)
                await self.finishExport(itemID: itemID, export: export, format: format)
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

    private func finishExport(itemID: UUID, export: RawExport, format: ExportFormat) async {
        guard let item = queue.first(where: { $0.id == itemID }),
              !item.stage.isDone else { return }
        do {
            let folder = outputFolderURL
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let sourceStem = item.sourceURL.deletingPathExtension().lastPathComponent
            let stem = Self.sanitizedExportStem(export.suggestedStem)
                ?? (sourceStem.isEmpty ? "document" : sourceStem)
            let destination = collisionFreeURL(
                in: folder,
                stem: stem,
                fileExtension: format.fileExtension
            )
            try export.data.write(to: destination)
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

    /// Rebuild the OKF bundle into the destination folder after a short quiet
    /// period, coalescing bursts of completions into a single write so the
    /// bundle's indexes and cross-links stay consistent.
    private func scheduleOkfBundleSync() {
        okfSyncTask?.cancel()
        let client = self.client
        let folder = outputFolderURL
        okfSyncTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(1.5))
            if Task.isCancelled { return }
            guard let self else { return }
            do {
                let bundle = try await client.exportOkfBundle()
                try await Self.writeOkfBundle(bundle, into: folder)
                // Rows are marked Saved before this write, so surface partial
                // bundles instead of leaving them invisible.
                self.okfSyncNotice = bundle.skipped.isEmpty
                    ? nil
                    : Copy.okfSkipped(bundle.skipped.count)
            } catch {
                // A newer completion cancelled this sync mid-write; its
                // replacement is about to run, so this is not a failure.
                if Task.isCancelled { return }
                // The documents are processed and will appear on the next
                // successful sync (another completion or a manual refresh);
                // leave the rows saved but say the bundle is stale.
                self.okfSyncNotice = Copy.okfSyncFailed
                return
            }
        }
    }

    // MARK: - Library

    /// Export a document's cleaned output into the destination folder using
    /// the current format, returning the written file. Used by the Library
    /// window's "Save a Copy" action.
    func exportDocumentToFolder(documentID: String, fallbackStem: String) async throws -> URL {
        let format = exportFormat.isBundle ? ExportFormat.markdown : exportFormat
        let export = try await client.exportRaw(documentId: documentID, format: format)
        let folder = outputFolderURL
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let stem = Self.sanitizedExportStem(export.suggestedStem)
            ?? (fallbackStem.isEmpty ? "document" : fallbackStem)
        let destination = collisionFreeURL(
            in: folder,
            stem: stem,
            fileExtension: format.fileExtension
        )
        try export.data.write(to: destination)
        return destination
    }

    /// Delete a document (and its cleaned output) from the engine's corpus.
    func deleteDocument(id: String) async throws {
        try await client.deleteDocument(id: id)
    }

    /// Write the bundle's path -> content map under `folder`, creating
    /// subdirectories. Never deletes unmanaged files; guards against path
    /// escapes even though the engine emits safe relative paths.
    nonisolated static func writeOkfBundle(_ bundle: OkfBundle, into folder: URL) async throws {
        try await Task.detached(priority: .utility) {
            let manager = FileManager.default
            try manager.createDirectory(at: folder, withIntermediateDirectories: true)
            for (relativePath, content) in bundle.files {
                let components = relativePath.split(separator: "/").map(String.init)
                if components.isEmpty || components.contains("..") {
                    continue
                }
                var destination = folder
                for component in components {
                    destination.appendPathComponent(component)
                }
                try manager.createDirectory(
                    at: destination.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try Data(content.utf8).write(to: destination)
            }
        }.value
    }

    /// The engine sanitizes its suggested stem, but the filesystem is ours:
    /// strip anything path-hostile again before trusting it, and reject empty
    /// results so the caller falls back to the source filename.
    static func sanitizedExportStem(_ raw: String?) -> String? {
        guard let raw else { return nil }
        var hostile = CharacterSet(charactersIn: "/\\:")
        hostile.formUnion(.controlCharacters)
        hostile.formUnion(.newlines)
        let collapsed = raw
            .components(separatedBy: hostile)
            .joined(separator: " ")
            .split(separator: " ")
            .joined(separator: " ")
        let trimmed = String(collapsed.prefix(100))
            .trimmingCharacters(in: CharacterSet(charactersIn: ". "))
        return trimmed.isEmpty ? nil : trimmed
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

    /// Stop an in-flight item: cancel its backend run (so the engine stops
    /// spending provider tokens on it) and mark the row Stopped/retryable.
    func stop(_ itemID: UUID) {
        guard let item = queue.first(where: { $0.id == itemID }),
              !item.stage.isTerminal else { return }
        setStage(itemID, .failed(reason: Copy.reasonStopped, retryable: true))
        guard let runID = item.runID else { return }
        let client = self.client
        Task {
            // Best effort: the reconcile loop also handles externally-visible
            // "canceled" status, so a failed cancel call just leaves the run
            // to finish or fail on its own.
            _ = try? await client.cancelRun(id: runID)
        }
    }

    func remove(_ itemID: UUID) {
        guard let item = queue.first(where: { $0.id == itemID }) else { return }
        if !item.stage.isTerminal, let runID = item.runID {
            // Removing an active row must not orphan a backend run that keeps
            // spending provider tokens; cancel it on the way out.
            let client = self.client
            Task { _ = try? await client.cancelRun(id: runID) }
        }
        queue.removeAll { $0.id == itemID }
        exportsInFlight.remove(itemID)
    }

    /// The backend's per-run event log for a failed item, newest last. Used by
    /// the row's Details popover so failures are more than one lossy line.
    func failureEvents(runID: String) async -> [RunEvent] {
        (try? await client.runEvents(runId: runID)) ?? []
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
