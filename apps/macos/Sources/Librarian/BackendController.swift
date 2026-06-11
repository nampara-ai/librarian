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
        for port in Self.candidatePorts {
            // A previous (orphaned) instance holds its own per-launch key and
            // cannot be adopted; treat a responding port as occupied.
            if await Self.isLibrarianHealthy(port: port) {
                continue
            }
            do {
                try launch(port: port, apiKey: apiKey)
            } catch {
                continue
            }
            for _ in 0..<60 {
                if process?.isRunning != true { break }
                if await Self.isLibrarianHealthy(port: port) {
                    mode = .embedded(port: port)
                    return
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
            stop()
        }
        mode = .failed(
            "The embedded backend did not start. See backend.log in the data folder."
        )
    }

    func stop() {
        process?.terminationHandler = nil
        process?.terminate()
        process = nil
        try? logHandle?.close()
        logHandle = nil
        if case .embedded = mode {
            mode = .external
        } else if case .starting = mode {
            mode = .external
        }
    }

    /// Stop the embedded backend and start a fresh instance, picking up any
    /// configuration changes from the data directory's .env file.
    func restart() async {
        stop()
        // Give uvicorn a moment to release its port before relaunching.
        try? await Task.sleep(for: .milliseconds(750))
        await startEmbeddedIfNeeded()
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
        launched.environment = environment
        // Run from the data directory so an optional `.env` there configures
        // the backend (LLM provider, model, API keys, ...).
        launched.currentDirectoryURL = dataDir

        let logURL = Self.logFileURL
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
                if case .embedded = self.mode {
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
