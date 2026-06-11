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

struct UploadItem: Identifiable, Hashable {
    enum State: Hashable {
        case uploading
        case done
        case failed(String)
    }

    let id: UUID
    let filename: String
    var state: State
}
