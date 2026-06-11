import Foundation

/// Reads and updates the backend configuration file at
/// `<data dir>/.env`. Updates preserve unrelated lines and comments so a
/// hand-edited file survives the settings UI.
enum EnvFile {
    static var url: URL {
        BackendController.dataDirectory.appendingPathComponent(".env")
    }

    static func read() -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        var values: [String: String] = [:]
        for rawLine in text.components(separatedBy: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let equals = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<equals]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: equals)...])
                .trimmingCharacters(in: .whitespaces)
            value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if !key.isEmpty {
                values[key] = value
            }
        }
        return values
    }

    /// Apply updates; a nil value removes the key. Other lines are preserved.
    static func update(_ updates: [String: String?]) throws {
        try FileManager.default.createDirectory(
            at: BackendController.dataDirectory,
            withIntermediateDirectories: true
        )
        let existing = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        var lines = existing.isEmpty ? [String]() : existing.components(separatedBy: "\n")
        var remaining = updates

        var result: [String] = []
        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.hasPrefix("#"), let equals = trimmed.firstIndex(of: "=") else {
                result.append(line)
                continue
            }
            let key = String(trimmed[..<equals]).trimmingCharacters(in: .whitespaces)
            if let pending = remaining.removeValue(forKey: key) {
                if let newValue = pending {
                    result.append("\(key)=\(newValue)")
                }
                // nil: drop the line entirely
            } else {
                result.append(line)
            }
        }
        for (key, value) in remaining.sorted(by: { $0.key < $1.key }) {
            if let value {
                result.append("\(key)=\(value)")
            }
        }
        lines = result

        var text = lines.joined(separator: "\n")
        if !text.hasSuffix("\n") {
            text += "\n"
        }
        try text.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600],
            ofItemAtPath: url.path
        )
    }
}
