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

struct RunQualityReport: Codable {
    let runId: String
    let documentId: String
    let extraction: ExtractionQuality?
    let processing: ProcessingQuality?
}

struct ExtractionQuality: Codable {
    let qualitySummary: ExtractionQualitySummary?
    let figuresExtracted: Int?
    let figureReferencesRestored: Int?
    let figureCaptionTitlesRestored: Int?
    let pagesReusedFromCache: Int?
    let assetPagesReusedFromCache: Int?
    let pages: [PageQuality]?
}

struct ExtractionQualitySummary: Codable {
    let pages: Int
    let pagesRequiringLayoutRepair: Int
    let pagesWithWarnings: Int
    let figureOcrLinesRemoved: Int
    let sideFurnitureLinesRemoved: Int?
    let nativeBodyLinesRestored: Int?
}

struct PageQuality: Codable, Identifiable {
    let pageNumber: Int
    let kind: String
    let action: String
    let nativeTextCoverage: Double
    let imageReferences: Int
    let figureReferencesRestored: Int?
    let figureOcrLinesRemoved: Int
    let warnings: [String]

    var id: Int { pageNumber }
}

struct ProcessingQuality: Codable {
    let pages: Int?
    let imageReferences: Int
    let tocEntriesWithoutMatchingHeadingOrBody: Int?
    let tocUnmatchedEntries: [String]?
    let chunks: Int
    let cachedCleanedChunks: Int
    let verifiedSourceChunks: Int?
    let sourcePreservedChunks: Int
    let refinement: RefinementQuality?
    let warnings: [String]
    let fatal: [String]
}

struct RefinementQuality: Codable {
    let attempted: Int
    let applied: Int
    let rejected: Int
    let deferred: Int
    let actions: [RefinementActionQuality]
}

struct RefinementActionQuality: Codable {
    let kind: String
    let label: String
    let status: String
    let reason: String?
    let pageNumber: Int?
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
    let assets: [ExportAsset]
}

struct ExportAsset: Codable {
    let filename: String
    let mediaType: String
    let dataBase64: String
    let sha256: String
}

struct DocumentAssetsResponse: Codable {
    let assets: [ExportAsset]
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
    case okfBundle = "okf"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .markdown: return "Markdown"
        case .text: return "Plain Text"
        case .json: return "JSON"
        case .okfBundle: return "Markdown (OKF bundle)"
        }
    }

    /// The per-document file extension. Unused for `.okfBundle`, which writes a
    /// whole directory tree rather than one file per document.
    var fileExtension: String {
        switch self {
        case .okfBundle: return "md"
        default: return rawValue
        }
    }

    /// Whether this format produces a single bundle directory instead of one
    /// output file per document.
    var isBundle: Bool { self == .okfBundle }
}

/// The Open Knowledge Format bundle returned by `GET /export/okf`: a map of
/// bundle-relative path to file content, plus the declared OKF version.
struct OkfBundle: Codable {
    let okfVersion: String
    let files: [String: String]
    let skipped: [String]
}

/// One row in the main-window queue (redesign spec §5). The whole window
/// renders from this; it is a projection over uploads, documents, and runs.
struct QueueItem: Identifiable {
    enum Stage {
        case queued
        case uploading(progress: Double?)
        case converting(progress: Double?)
        case cleaning(progress: Double)
        case refining
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

    /// A client deadline is only safe before a durable backend run exists.
    /// Processing time scales with document size, provider latency, retries,
    /// and cleaning style, while the backend reports its own terminal state.
    func exceededPreRunTimeout(at now: Date, timeout: TimeInterval) -> Bool {
        runID == nil && now.timeIntervalSince(startedAt) > timeout
    }
}
