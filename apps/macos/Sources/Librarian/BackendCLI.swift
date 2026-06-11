import Foundation

struct CLIResult {
    let exitCode: Int32
    let output: String

    var succeeded: Bool { exitCode == 0 }
}

/// Runs the bundled `python -m librarian ...` CLI for local file tools that
/// have no HTTP equivalent (convert, transcript utilities, doctor, migrate).
enum BackendCLI {
    static var isAvailable: Bool {
        BackendController.bundledPythonURL != nil
    }

    static func run(_ arguments: [String]) async throws -> CLIResult {
        guard let python = BackendController.bundledPythonURL else {
            throw APIClientError(
                message: "This build has no bundled backend; command-line tools "
                    + "need the packaged app."
            )
        }
        let dataDir = BackendController.dataDirectory
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        let credentialOverlay = ProviderCredentials.environmentOverlay()
        let networkOverlay = SystemNetworkEnvironment.overlay(dataDirectory: dataDir)
        return try await Task.detached(priority: .userInitiated) {
            let process = Process()
            process.executableURL = python
            process.arguments = ["-m", "librarian"] + arguments
            var environment = ProcessInfo.processInfo.environment
            environment["LIBRARIAN_DATA_DIR"] = dataDir.path
            environment["PYTHONUNBUFFERED"] = "1"
            environment["NO_COLOR"] = "1"
            environment["TERM"] = "dumb"
            environment["COLUMNS"] = "200"
            for (name, value) in credentialOverlay {
                environment[name] = value
            }
            for (name, value) in networkOverlay {
                environment[name] = value
            }
            process.environment = environment
            process.currentDirectoryURL = dataDir

            // Merge stderr into stdout: one stream cannot deadlock on full
            // pipe buffers, and tool output stays in order.
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return CLIResult(
                exitCode: process.terminationStatus,
                output: String(data: data, encoding: .utf8) ?? ""
            )
        }.value
    }

    /// Extract the JSON object from merged CLI output (warnings may surround it).
    static func jsonObject(in output: String) -> Data? {
        guard let start = output.firstIndex(of: "{"),
              let end = output.lastIndex(of: "}") else { return nil }
        return String(output[start...end]).data(using: .utf8)
    }
}
