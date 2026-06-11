import Foundation

/// GUI-launched processes don't inherit shell proxy variables, and the
/// bundled Python trusts only its own certificate list — unlike the app's
/// URLSession, which follows macOS system settings for both. This bridges
/// the system's proxy configuration and trust store into the engine's
/// environment so Python connects wherever the app can.
enum SystemNetworkEnvironment {
    /// Extra environment for the engine and CLI processes.
    nonisolated static func overlay(dataDirectory: URL) -> [String: String] {
        var overlay: [String: String] = [:]
        for (name, value) in proxyVariables() {
            overlay[name] = value
        }
        if let bundle = exportSystemTrustStore(into: dataDirectory) {
            // httpx (the engine's HTTP client) honors SSL_CERT_FILE, so
            // corporate or filtering proxies with their own roots work.
            overlay["SSL_CERT_FILE"] = bundle.path
        }
        return overlay
    }

    // MARK: - Proxies

    /// Read macOS proxy settings via `scutil --proxy`.
    nonisolated private static func proxyVariables() -> [String: String] {
        guard let output = runTool("/usr/sbin/scutil", arguments: ["--proxy"]) else {
            return [:]
        }
        var settings: [String: String] = [:]
        for line in output.components(separatedBy: "\n") {
            let parts = line.split(separator: ":", maxSplits: 1)
            guard parts.count == 2 else { continue }
            settings[parts[0].trimmingCharacters(in: .whitespaces)] =
                parts[1].trimmingCharacters(in: .whitespaces)
        }
        var variables: [String: String] = [:]
        if settings["HTTPEnable"] == "1",
           let host = settings["HTTPProxy"], let port = settings["HTTPPort"] {
            variables["HTTP_PROXY"] = "http://\(host):\(port)"
            variables["http_proxy"] = "http://\(host):\(port)"
        }
        if settings["HTTPSEnable"] == "1",
           let host = settings["HTTPSProxy"], let port = settings["HTTPSPort"] {
            variables["HTTPS_PROXY"] = "http://\(host):\(port)"
            variables["https_proxy"] = "http://\(host):\(port)"
        }
        if !variables.isEmpty {
            // Never proxy the app's own loopback engine traffic.
            variables["NO_PROXY"] = "127.0.0.1,localhost"
            variables["no_proxy"] = "127.0.0.1,localhost"
        }
        return variables
    }

    // MARK: - Trust store

    /// Export the system trust store (system roots plus admin-added
    /// certificates) to a PEM bundle the engine can use.
    nonisolated private static func exportSystemTrustStore(into dataDirectory: URL) -> URL? {
        let roots = runTool(
            "/usr/bin/security",
            arguments: [
                "find-certificate", "-a", "-p",
                "/System/Library/Keychains/SystemRootCertificates.keychain",
            ]
        ) ?? ""
        let admin = runTool(
            "/usr/bin/security",
            arguments: ["find-certificate", "-a", "-p", "/Library/Keychains/System.keychain"]
        ) ?? ""
        let combined = roots + "\n" + admin
        guard combined.contains("BEGIN CERTIFICATE") else { return nil }
        let destination = dataDirectory.appendingPathComponent("system-roots.pem")
        do {
            try FileManager.default.createDirectory(
                at: dataDirectory, withIntermediateDirectories: true
            )
            try combined.write(to: destination, atomically: true, encoding: .utf8)
            return destination
        } catch {
            return nil
        }
    }

    nonisolated private static func runTool(_ path: String, arguments: [String]) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
