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
        // Ingesting a scanned/image PDF runs OCR synchronously and can take
        // minutes; a short request timeout would fire before the engine
        // answers and be misreported as an engine restart. The resource
        // timeout still bounds truly stuck calls.
        configuration.timeoutIntervalForRequest = 300
        configuration.timeoutIntervalForResource = 1800
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

    // 500 is the backend's page ceiling; a lower default could lose track of
    // queue items during large drops with retries.
    func listRuns(limit: Int = 500) async throws -> RunsPage {
        let (data, _) = try await send(
            "GET", "/runs", query: [URLQueryItem(name: "limit", value: String(limit))]
        )
        return try Self.decoder.decode(RunsPage.self, from: data)
    }

    /// Upload a file by streaming it from disk. The multipart envelope is
    /// assembled into a temporary file (header + the source file's bytes +
    /// footer) and handed to `URLSession.upload(fromFile:)`, so the file is
    /// never fully resident in memory — and never copied a second time as a
    /// multipart `Data` blob.
    func uploadDocument(filename: String, fileURL: URL) async throws -> Document {
        let boundary = "librarian-\(UUID().uuidString)"
        let safeName = filename
            .replacingOccurrences(of: "\"", with: "_")
            .replacingOccurrences(of: "\r", with: "_")
            .replacingOccurrences(of: "\n", with: "_")

        var header = Data()
        header.appendString("--\(boundary)\r\n")
        header.appendString(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\n"
        )
        header.appendString("Content-Type: application/octet-stream\r\n\r\n")
        let footer = Data("\r\n--\(boundary)--\r\n".utf8)

        let bodyFileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("librarian-upload-\(UUID().uuidString)")
        // Best-effort cleanup of the temporary envelope regardless of outcome.
        defer { try? FileManager.default.removeItem(at: bodyFileURL) }
        try Self.assembleMultipartFile(
            at: bodyFileURL,
            header: header,
            source: fileURL,
            footer: footer
        )

        let (data, _) = try await upload(
            "POST", "/documents",
            fromFile: bodyFileURL,
            contentType: "multipart/form-data; boundary=\(boundary)"
        )
        return try Self.decoder.decode(Document.self, from: data)
    }

    /// Stream the multipart envelope to disk: write the header, copy the source
    /// file in chunks, then the footer — bounding memory to one chunk.
    private static func assembleMultipartFile(
        at destination: URL,
        header: Data,
        source: URL,
        footer: Data
    ) throws {
        FileManager.default.createFile(atPath: destination.path, contents: nil)
        let out = try FileHandle(forWritingTo: destination)
        defer { try? out.close() }
        let input = try FileHandle(forReadingFrom: source)
        defer { try? input.close() }
        try out.write(contentsOf: header)
        while true {
            let chunk = try input.read(upToCount: 1024 * 1024) ?? Data()
            if chunk.isEmpty { break }
            try out.write(contentsOf: chunk)
        }
        try out.write(contentsOf: footer)
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

    /// Raw export body in the requested format (Markdown/text bodies, or the
    /// JSON document for `.json`), optionally with transcript citation evidence.
    /// The engine suggests an output filename stem ("{dewey code} {title}")
    /// via a percent-encoded response header.
    func exportRaw(
        documentId: String,
        format: ExportFormat,
        citationQuote: String? = nil
    ) async throws -> RawExport {
        var query = [URLQueryItem(name: "format", value: format.rawValue)]
        if let citationQuote, !citationQuote.isEmpty {
            query.append(URLQueryItem(name: "citation_quote", value: citationQuote))
        }
        let (data, response) = try await send(
            "GET", "/documents/\(documentId)/export", query: query
        )
        let stem = response.value(forHTTPHeaderField: "X-Librarian-Export-Stem")?
            .removingPercentEncoding
        return RawExport(data: data, suggestedStem: stem)
    }

    /// The whole processed corpus rendered as an Open Knowledge Format bundle
    /// (a map of bundle-relative path to markdown content).
    func exportOkfBundle() async throws -> OkfBundle {
        let (data, _) = try await send("GET", "/export/okf")
        return try Self.decoder.decode(OkfBundle.self, from: data)
    }

    func content(documentId: String, offset: Int = 0) async throws -> ContentPage {
        let (data, _) = try await send(
            "GET", "/documents/\(documentId)/content",
            query: [URLQueryItem(name: "offset", value: String(offset))]
        )
        return try Self.decoder.decode(ContentPage.self, from: data)
    }

    func cancelRun(id: String) async throws -> Run {
        let (data, _) = try await send("POST", "/runs/\(id)/cancel")
        return try Self.decoder.decode(Run.self, from: data)
    }

    func retryRun(id: String) async throws -> Run {
        let (data, _) = try await send("POST", "/runs/\(id)/retry")
        return try Self.decoder.decode(Run.self, from: data)
    }

    func search(
        query: String,
        scope: String = "cleaned",
        limit: Int = 25
    ) async throws -> [SearchResult] {
        struct SearchBody: Codable {
            let query: String
            let limit: Int
            let scope: String
        }
        let body = try JSONEncoder().encode(SearchBody(query: query, limit: limit, scope: scope))
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
        var request = try makeRequest(method, path, query: query, contentType: contentType)
        request.httpBody = body
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await Self.session.data(for: request)
        } catch {
            throw APIClientError(message: "Cannot reach server: \(error.localizedDescription)")
        }
        return try Self.validate(data: data, response: response)
    }

    /// Like `send`, but streams the request body from a file on disk instead of
    /// holding it in memory, for large multipart uploads.
    private func upload(
        _ method: String,
        _ path: String,
        fromFile fileURL: URL,
        contentType: String?
    ) async throws -> (Data, HTTPURLResponse) {
        let request = try makeRequest(method, path, contentType: contentType)
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await Self.session.upload(for: request, fromFile: fileURL)
        } catch {
            throw APIClientError(message: "Cannot reach server: \(error.localizedDescription)")
        }
        return try Self.validate(data: data, response: response)
    }

    private func makeRequest(
        _ method: String,
        _ path: String,
        query: [URLQueryItem] = [],
        contentType: String? = nil
    ) throws -> URLRequest {
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
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        if !apiKey.isEmpty {
            request.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        }
        return request
    }

    private static func validate(
        data: Data,
        response: URLResponse
    ) throws -> (Data, HTTPURLResponse) {
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
