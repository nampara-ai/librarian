import Foundation

struct Document: Codable, Identifiable, Hashable {
    let id: String
    let filename: String
    let status: String
    let byteSize: Int
}

struct DocumentsPage: Codable {
    let documents: [Document]
    let total: Int
}

struct Run: Codable, Identifiable, Hashable {
    let id: String
    let documentId: String
    let status: String
    let stage: String
    let totalChunks: Int
    let completedChunks: Int
    let failedChunks: Int
    let error: String?

    var isActive: Bool {
        status == "queued" || status == "running"
    }

    var fractionComplete: Double {
        guard totalChunks > 0 else { return status == "succeeded" ? 1 : 0 }
        return Double(completedChunks) / Double(totalChunks)
    }
}

struct RunsPage: Codable {
    let runs: [Run]
    let total: Int
}

struct RunEvent: Codable, Identifiable, Hashable {
    let sequence: Int
    let stage: String
    let message: String
    let createdAt: String

    var id: Int { sequence }
}

struct RunEventsPage: Codable {
    let events: [RunEvent]
}

struct ExportedDocument: Codable {
    let documentId: String
    let filename: String
    let classification: String?
    let text: String
}

/// A raw export body plus the engine's suggested output filename stem.
struct RawExport {
    let data: Data
    let suggestedStem: String?
}

struct SearchResult: Codable, Identifiable, Hashable {
    let documentId: String
    let runId: String?
    let source: String
    let filename: String
    let documentStatus: String
    let snippet: String
    let score: Double
    let classificationCode: String?
    let classificationLabel: String?

    var id: String { "\(documentId)-\(source)" }
}

struct SearchResultsPage: Codable {
    let results: [SearchResult]
    let total: Int
}

struct Readiness: Codable {
    let status: String
    let database: String
    let storage: String
    let appliedMigrations: Int
}

struct ContentPage: Codable {
    let documentId: String
    let text: String
    let totalChars: Int
    let offset: Int
    let limit: Int
    let truncated: Bool
}

struct DoctorCheck: Codable, Identifiable, Hashable {
    let name: String
    let capability: String
    let status: String
    let detail: String

    var id: String { name + capability }
}

struct DoctorReport: Codable {
    let checks: [DoctorCheck]
}

enum ExportFormat: String, CaseIterable, Identifiable {
    case markdown = "md"
    case text = "txt"
    case json = "json"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .markdown: return "Markdown"
        case .text: return "Plain Text"
        case .json: return "JSON"
        }
    }

    var fileExtension: String { rawValue }
}

/// One row in the main-window queue (redesign spec §5). The whole window
/// renders from this; it is a projection over uploads, documents, and runs.
struct QueueItem: Identifiable {
    enum Stage {
        case queued
        case uploading(progress: Double?)
        case converting(progress: Double?)
        case cleaning(progress: Double)
        case classifying(progress: Double?)
        case done(outputURL: URL)
        case failed(reason: String, retryable: Bool)

        var isTerminal: Bool {
            switch self {
            case .done, .failed: return true
            default: return false
            }
        }

        var isDone: Bool {
            if case .done = self { return true }
            return false
        }

        var isFailed: Bool {
            if case .failed = self { return true }
            return false
        }
    }

    let id: UUID
    let sourceURL: URL
    var stage: Stage
    var documentID: String?
    var runID: String?
    var startedAt: Date

    var filename: String { sourceURL.lastPathComponent }
}
