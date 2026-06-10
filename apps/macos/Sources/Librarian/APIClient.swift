import Foundation

struct APIClientError: LocalizedError {
    let message: String

    var errorDescription: String? { message }
}

/// Thin async client for the Librarian FastAPI service.
struct APIClient {
    let baseURL: URL
    let apiKey: String

    private static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 600
        return URLSession(configuration: configuration)
    }()

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    func health() async throws -> Bool {
        let (data, _) = try await send("GET", "/health")
        struct Health: Codable { let status: String }
        return try Self.decoder.decode(Health.self, from: data).status == "healthy"
    }

    func ready() async throws -> Readiness {
        let (data, _) = try await send("GET", "/ready")
        return try Self.decoder.decode(Readiness.self, from: data)
    }

    func version() async throws -> String {
        let (data, _) = try await send("GET", "/version")
        struct Version: Codable { let version: String }
        return try Self.decoder.decode(Version.self, from: data).version
    }

    func listDocuments(limit: Int = 500) async throws -> DocumentsPage {
        let (data, _) = try await send(
            "GET", "/documents", query: [URLQueryItem(name: "limit", value: String(limit))]
        )
        return try Self.decoder.decode(DocumentsPage.self, from: data)
    }

    func listRuns(limit: Int = 100) async throws -> RunsPage {
        let (data, _) = try await send(
            "GET", "/runs", query: [URLQueryItem(name: "limit", value: String(limit))]
        )
        return try Self.decoder.decode(RunsPage.self, from: data)
    }

    func uploadDocument(filename: String, contents: Data) async throws -> Document {
        let boundary = "librarian-\(UUID().uuidString)"
        let safeName = filename
            .replacingOccurrences(of: "\"", with: "_")
            .replacingOccurrences(of: "\r", with: "_")
            .replacingOccurrences(of: "\n", with: "_")
        var body = Data()
        body.appendString("--\(boundary)\r\n")
        body.appendString("Content-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\n")
        body.appendString("Content-Type: application/octet-stream\r\n\r\n")
        body.append(contents)
        body.appendString("\r\n--\(boundary)--\r\n")
        let (data, _) = try await send(
            "POST", "/documents",
            body: body,
            contentType: "multipart/form-data; boundary=\(boundary)"
        )
        return try Self.decoder.decode(Document.self, from: data)
    }

    func createRun(documentId: String) async throws -> Run {
        let body = try JSONEncoder().encode(["document_id": documentId])
        let (data, _) = try await send("POST", "/runs", body: body, contentType: "application/json")
        return try Self.decoder.decode(Run.self, from: data)
    }

    func runEvents(runId: String, limit: Int = 500) async throws -> [RunEvent] {
        let (data, _) = try await send(
            "GET", "/runs/\(runId)/events/records",
            query: [URLQueryItem(name: "limit", value: String(limit))]
        )
        return try Self.decoder.decode(RunEventsPage.self, from: data).events
    }

    func export(documentId: String) async throws -> ExportedDocument {
        let (data, _) = try await send(
            "GET", "/documents/\(documentId)/export",
            query: [URLQueryItem(name: "format", value: "json")]
        )
        return try Self.decoder.decode(ExportedDocument.self, from: data)
    }

    func search(query: String, limit: Int = 25) async throws -> [SearchResult] {
        struct SearchBody: Codable {
            let query: String
            let limit: Int
        }
        let body = try JSONEncoder().encode(SearchBody(query: query, limit: limit))
        let (data, _) = try await send(
            "POST", "/search/results", body: body, contentType: "application/json"
        )
        return try Self.decoder.decode(SearchResultsPage.self, from: data).results
    }

    func deleteDocument(id: String) async throws {
        _ = try await send("DELETE", "/documents/\(id)")
    }

    private func send(
        _ method: String,
        _ path: String,
        query: [URLQueryItem] = [],
        body: Data? = nil,
        contentType: String? = nil
    ) async throws -> (Data, HTTPURLResponse) {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw APIClientError(message: "Invalid server URL")
        }
        let basePath = components.path.hasSuffix("/")
            ? String(components.path.dropLast())
            : components.path
        components.path = basePath + path
        if !query.isEmpty {
            components.queryItems = query
        }
        guard let url = components.url else {
            throw APIClientError(message: "Invalid server URL")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        if !apiKey.isEmpty {
            request.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        }
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await Self.session.data(for: request)
        } catch {
            throw APIClientError(message: "Cannot reach server: \(error.localizedDescription)")
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIClientError(message: "Unexpected response from server")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIClientError(message: Self.errorMessage(status: http.statusCode, data: data))
        }
        return (data, http)
    }

    private static func errorMessage(status: Int, data: Data) -> String {
        struct ErrorBody: Codable { let detail: String? }
        if let body = try? JSONDecoder().decode(ErrorBody.self, from: data), let detail = body.detail {
            return detail
        }
        return "Server returned HTTP \(status)"
    }
}

private extension Data {
    mutating func appendString(_ string: String) {
        append(Data(string.utf8))
    }
}
