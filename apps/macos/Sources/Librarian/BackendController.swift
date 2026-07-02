import AppKit
import Foundation

/// Launches and supervises the Librarian backend that ships inside the app
/// bundle (Contents/Resources/backend). Falls back to an external server when
/// no bundled backend is present or the user disables embedded mode.
@MainActor
final class BackendController: ObservableObject {
    enum Mode: Equatable {
        case external
        case starting
        case embedded(port: Int)
        case failed(String)
    }

    @Published private(set) var mode: Mode = .external

    /// Random credential generated for each embedded launch and required by
    /// the spawned API, so other local processes cannot use it.
    private(set) var embeddedAPIKey: String?

    private var process: Process?
    private var logHandle: FileHandle?

    /// Timestamps of recent automatic relaunches after an unexpected exit,
    /// used to detect a crash loop and stop hammering a backend that cannot
    /// stay up instead of restarting it forever.
    private var recentRelaunches: [Date] = []
    private static let relaunchWindow: TimeInterval = 60
    private static let maxRelaunchesInWindow = 3

    private static func generateAPIKey() -> String {
        let raw = UUID().uuidString + UUID().uuidString
        return raw.replacingOccurrences(of: "-", with: "").lowercased()
    }

    static let candidatePorts = [8765, 8766, 8767, 8768]

    nonisolated static var bundledPythonURL: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let python = resources.appendingPathComponent("backend/python/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: python.path) else { return nil }
        return python
    }

    nonisolated static var isEmbeddedAvailable: Bool {
        bundledPythonURL != nil
    }

    nonisolated static var dataDirectory: URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("Librarian", isDirectory: true)
    }

    nonisolated static var logFileURL: URL {
        dataDirectory.appendingPathComponent("backend.log")
    }

    /// backend.log is opened for append on every launch, so without rotation it
    /// grows forever. Cap it at a few MB.
    private static let maxLogBytes: UInt64 = 5 * 1024 * 1024

    /// If the log has exceeded the cap, move it aside to a single `.1` backup
    /// (overwriting any earlier backup) so the active file starts empty.
    nonisolated private static func rotateLogIfNeeded(at logURL: URL) {
        let manager = FileManager.default
        guard let attributes = try? manager.attributesOfItem(atPath: logURL.path),
              let size = attributes[.size] as? UInt64, size > maxLogBytes else {
            return
        }
        let rotated = logURL.appendingPathExtension("1")
        try? manager.removeItem(at: rotated)
        try? manager.moveItem(at: logURL, to: rotated)
    }

    /// Directory of the bundled OCR command-line tools (tesseract, pdftoppm,
    /// …), if this build shipped them.
    nonisolated static var bundledOCRBinURL: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let bin = resources.appendingPathComponent("ocr/bin")
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: bin.path, isDirectory: &isDirectory),
              isDirectory.boolValue else { return nil }
        return bin
    }

    /// Bundled Tesseract language data directory, if present.
    nonisolated static var bundledTessdataURL: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let tessdata = resources.appendingPathComponent("ocr/share/tessdata")
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: tessdata.path, isDirectory: &isDirectory),
              isDirectory.boolValue else { return nil }
        return tessdata
    }

    var embeddedBaseURL: URL? {
        guard case .embedded(let port) = mode else { return nil }
        return URL(string: "http://127.0.0.1:\(port)")
    }

    func startEmbeddedIfNeeded() async {
        guard Self.isEmbeddedAvailable else {
            mode = .external
            return
        }
        switch mode {
        case .embedded, .starting:
            return
        case .external, .failed:
            break
        }
        mode = .starting
        // Fresh credential for every launch: the embedded API requires this
        // key, so other local processes cannot read or modify the corpus.
        let apiKey = Self.generateAPIKey()
        embeddedAPIKey = apiKey
        var occupiedPorts = 0
        for port in Self.candidatePorts {
            // A previous (orphaned) instance holds its own per-launch key and
            // cannot be adopted; treat a responding port as occupied.
            if await Self.isLibrarianHealthy(port: port) {
                occupiedPorts += 1
                continue
            }
            do {
                try launch(port: port, apiKey: apiKey)
            } catch {
                continue
            }
            // A cold first launch (relocatable Python start + migrations) can
            // legitimately take tens of seconds; killing a healthy boot at 15 s
            // read as "Engine didn't start" on slower Macs. Wait up to 60 s
            // while the process is alive, and bail out as soon as it exits so
            // the next candidate port is tried without burning the full budget.
            for _ in 0..<240 {
                if process?.isRunning != true { break }
                if await Self.isLibrarianHealthy(port: port) {
                    mode = .embedded(port: port)
                    return
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
            let ranFullBudget = process?.isRunning == true
            await stopGracefully(preserveMode: true)
            if ranFullBudget {
                // The engine ran for the whole budget without answering; a
                // different port will not help, so surface the failure now.
                break
            }
        }
        if occupiedPorts == Self.candidatePorts.count {
            mode = .failed(
                "Other programs are using the engine's ports "
                    + "(\(Self.candidatePorts.map(String.init).joined(separator: ", "))). "
                    + "Quit them or restart your Mac, then try again."
            )
            return
        }
        mode = .failed(
            "The embedded backend did not start. See backend.log in the data folder."
        )
    }

    /// Synchronous stop for app termination, where blocking briefly is the
    /// only way to guarantee no orphaned engine survives the app. UI paths
    /// (settings toggles, restarts) use `stopGracefully()` instead.
    func stop() {
        if let running = process {
            running.terminationHandler = nil
            running.terminate()
            // Bounded wait for a graceful exit; escalate so no orphaned
            // engine survives the app.
            let deadline = Date().addingTimeInterval(3)
            while running.isRunning && Date() < deadline {
                usleep(100_000)
            }
            if running.isRunning {
                kill(running.processIdentifier, SIGKILL)
            }
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
        if case .embedded = mode {
            mode = .external
        } else if case .starting = mode {
            mode = .external
        }
    }

    /// Async stop that never blocks the main actor: terminate, await exit for
    /// up to 3 s, then SIGKILL. `preserveMode` keeps the current mode (used
    /// mid-boot, where the caller sets the final mode itself).
    func stopGracefully(preserveMode: Bool = false) async {
        if let running = process {
            running.terminationHandler = nil
            running.terminate()
            var waited = 0
            while running.isRunning && waited < 30 {
                try? await Task.sleep(for: .milliseconds(100))
                waited += 1
            }
            if running.isRunning {
                kill(running.processIdentifier, SIGKILL)
            }
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
        if !preserveMode {
            if case .embedded = mode {
                mode = .external
            } else if case .starting = mode {
                mode = .external
            }
        }
    }

    /// Stop the embedded backend and start a fresh instance, picking up any
    /// configuration changes from the data directory's .env file.
    func restart() async {
        // stopGracefully waits for the old instance to exit (and uvicorn to
        // release its port) before we relaunch.
        await stopGracefully()
        await startEmbeddedIfNeeded()
    }

    /// Whether the termination handler may auto-relaunch after an unexpected
    /// exit. Records the attempt and refuses once more than
    /// `maxRelaunchesInWindow` have occurred inside `relaunchWindow`, so a
    /// backend that crashes on boot surrenders to `.failed` instead of looping.
    private func shouldAttemptRelaunch() -> Bool {
        let now = Date()
        recentRelaunches.removeAll { now.timeIntervalSince($0) > Self.relaunchWindow }
        guard recentRelaunches.count < Self.maxRelaunchesInWindow else { return false }
        recentRelaunches.append(now)
        return true
    }

    func revealDataFolder() {
        let directory = Self.dataDirectory
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        NSWorkspace.shared.activateFileViewerSelecting([directory])
    }

    private func launch(port: Int, apiKey: String) throws {
        guard let python = Self.bundledPythonURL else {
            throw APIClientError(message: "No bundled backend in this build")
        }
        let dataDir = Self.dataDirectory
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)

        let launched = Process()
        launched.executableURL = python
        launched.arguments = [
            "-m", "librarian", "api",
            "--host", "127.0.0.1",
            "--port", String(port),
        ]
        var environment = ProcessInfo.processInfo.environment
        environment["LIBRARIAN_DATA_DIR"] = dataDir.path
        // Environment variables take precedence over a user .env in the data
        // directory, so the per-launch key always applies.
        environment["LIBRARIAN_API_KEY"] = apiKey
        // Explicitly blank the multi-key variants so a user-writable .env
        // cannot mint additional API credentials for the embedded engine.
        environment["LIBRARIAN_API_KEYS"] = ""
        environment["LIBRARIAN_API_KEY_SHA256"] = ""
        environment["LIBRARIAN_API_KEY_HASHES"] = ""
        // Lets the backend detect an orphaned launch (app gone) and exit.
        environment["LIBRARIAN_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        environment["PYTHONUNBUFFERED"] = "1"
        // Provider API keys live in the Keychain, not on disk; hand them to
        // the backend through its environment.
        for (name, value) in ProviderCredentials.environmentOverlay() {
            environment[name] = value
        }
        // Bridge macOS proxy settings and the system trust store so the
        // engine can reach providers wherever the app can.
        for (name, value) in SystemNetworkEnvironment.overlay(dataDirectory: dataDir) {
            environment[name] = value
        }
        // OCR for scanned/image PDFs needs the `tesseract` and `pdftoppm`
        // binaries. GUI apps launched from Finder inherit only a bare system
        // PATH, so we put the bundled OCR tools first, then common Homebrew
        // locations as a fallback for users who installed their own.
        var pathEntries: [String] = []
        if let ocrBin = Self.bundledOCRBinURL {
            pathEntries.append(ocrBin.path)
        }
        if let tessdata = Self.bundledTessdataURL {
            environment["TESSDATA_PREFIX"] = tessdata.path
            // Point the bundled liteparse engine's in-process Tesseract at the
            // same traineddata so high-fidelity OCR works fully offline instead
            // of trying to fetch language data on first use.
            environment["LIBRARIAN_LITEPARSE_TESSDATA_PATH"] = tessdata.path
        }
        let existingPath = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        pathEntries.append(existingPath)
        pathEntries.append(contentsOf: ["/opt/homebrew/bin", "/usr/local/bin"])
        environment["PATH"] = pathEntries.joined(separator: ":")
        // One unreadable page should never sink a large mixed PDF: skip the
        // page (recorded in the page manifest) and keep going; ingest only
        // fails when no text can be extracted at all.
        environment["LIBRARIAN_OCR_FAIL_ON_PAGE_ERROR"] = "false"
        launched.environment = environment
        // Run from the data directory so an optional `.env` there configures
        // the backend (LLM provider, model, API keys, ...).
        launched.currentDirectoryURL = dataDir

        let logURL = Self.logFileURL
        // Keep backend.log from growing without bound: if it has passed the
        // cap, rotate it to backend.log.1 (replacing any previous rotation)
        // and start fresh. Every launch appends, so this bounds it to roughly
        // twice the cap on disk.
        Self.rotateLogIfNeeded(at: logURL)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        try? logHandle?.close()
        logHandle = nil
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            launched.standardOutput = handle
            launched.standardError = handle
            logHandle = handle
        }
        launched.terminationHandler = { [weak self] finished in
            Task { @MainActor [weak self] in
                guard let self, self.process === finished else { return }
                self.process = nil
                try? self.logHandle?.close()
                self.logHandle = nil
                // Only an *unexpected* exit while we believed the engine was
                // healthy triggers recovery; a deliberate stop() clears the
                // termination handler and never reaches here.
                guard case .embedded = self.mode else { return }
                if self.shouldAttemptRelaunch() {
                    // One automatic relaunch attempt: reset to a restartable
                    // state and boot again. The crash-loop guard prevents this
                    // from spinning forever.
                    self.mode = .external
                    await self.startEmbeddedIfNeeded()
                } else {
                    self.mode = .failed(
                        "The backend stopped unexpectedly. See backend.log in the data folder."
                    )
                }
            }
        }

        try launched.run()
        process = launched
    }

    nonisolated private static func isLibrarianHealthy(port: Int) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                return false
            }
            return String(data: data, encoding: .utf8)?.contains("healthy") == true
        } catch {
            return false
        }
    }
}
