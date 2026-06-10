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

    private static func generateAPIKey() -> String {
        let raw = UUID().uuidString + UUID().uuidString
        return raw.replacingOccurrences(of: "-", with: "").lowercased()
    }

    static let candidatePorts = [8765, 8766, 8767, 8768]

    static var bundledPythonURL: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let python = resources.appendingPathComponent("backend/python/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: python.path) else { return nil }
        return python
    }

    static var isEmbeddedAvailable: Bool {
        bundledPythonURL != nil
    }

    static var dataDirectory: URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("Librarian", isDirectory: true)
    }

    static var logFileURL: URL {
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
        process?.terminate()
        process = nil
        if case .embedded = mode {
            mode = .external
        } else if case .starting = mode {
            mode = .external
        }
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
        launched.environment = environment
        // Run from the data directory so an optional `.env` there configures
        // the backend (LLM provider, model, API keys, ...).
        launched.currentDirectoryURL = dataDir

        let logURL = Self.logFileURL
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            launched.standardOutput = handle
            launched.standardError = handle
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
