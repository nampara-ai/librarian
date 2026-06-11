import SwiftUI

/// File tools backed by the bundled CLI: convert, convert a folder,
/// normalize a transcript, and find a quote in a transcript.
struct ToolsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    enum Tool: String, CaseIterable, Identifiable {
        case convert = "Convert File"
        case convertDir = "Convert Folder"
        case normalize = "Normalize Transcript"
        case find = "Find in Transcript"

        var id: String { rawValue }
    }

    @State private var tool: Tool = .convert
    @State private var inputURL: URL?
    @State private var format = "md"
    @State private var transcriptFormat = "md"
    @State private var outputMode = "workspace"
    @State private var overwrite = false
    @State private var recursive = false
    @State private var searchPhrase = ""
    @State private var isRunning = false
    @State private var output = ""
    @State private var lastWrittenURL: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Tools")
                    .font(.title2.weight(.semibold))
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }

            Picker("Tool", selection: $tool) {
                ForEach(Tool.allCases) { candidate in
                    Text(candidate.rawValue).tag(candidate)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .onChange(of: tool) {
                inputURL = nil
                output = ""
                lastWrittenURL = nil
            }

            GroupBox {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text(inputURL?.path ?? placeholder)
                            .font(.callout)
                            .foregroundStyle(inputURL == nil ? .secondary : .primary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Spacer()
                        Button(tool == .convertDir ? "Choose Folder…" : "Choose File…") {
                            chooseInput()
                        }
                    }
                    optionsRow
                    if tool == .find {
                        TextField(
                            "Quoted source phrase",
                            text: $searchPhrase,
                            prompt: Text("e.g. follow-up care")
                        )
                        .textFieldStyle(.roundedBorder)
                    }
                }
                .padding(6)
            }

            HStack {
                Button {
                    Task { await run() }
                } label: {
                    if isRunning {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Run", systemImage: "play.fill")
                    }
                }
                .disabled(isRunning || !canRun)
                if let lastWrittenURL {
                    Button("Reveal Output in Finder") {
                        model.revealInFinder(lastWrittenURL)
                    }
                }
                Spacer()
            }

            ScrollView {
                Text(output.isEmpty ? "Output appears here." : output)
                    .font(.callout.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .foregroundStyle(output.isEmpty ? .secondary : .primary)
                    .padding(8)
            }
            .frame(minHeight: 160, maxHeight: 240)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
        }
        .padding(20)
        .frame(width: 620)
    }

    private var placeholder: String {
        switch tool {
        case .convert: return "Choose a document (PDF, DOCX, Markdown, text, image…)"
        case .convertDir: return "Choose a folder of documents"
        case .normalize, .find: return "Choose a transcript (.srt, .vtt, .txt, .md)"
        }
    }

    @ViewBuilder
    private var optionsRow: some View {
        switch tool {
        case .convert:
            HStack(spacing: 16) {
                Picker("Format", selection: $format) {
                    Text("Markdown").tag("md")
                    Text("Plain text").tag("txt")
                }
                .fixedSize()
                Toggle("Overwrite existing output", isOn: $overwrite)
            }
        case .convertDir:
            HStack(spacing: 16) {
                Picker("Format", selection: $format) {
                    Text("Markdown").tag("md")
                    Text("Plain text").tag("txt")
                }
                .fixedSize()
                Picker("Placement", selection: $outputMode) {
                    Text("Output folder").tag("workspace")
                    Text("Subfolder next to source").tag("subdirectory")
                }
                .fixedSize()
                Toggle("Recursive", isOn: $recursive)
                Toggle("Overwrite", isOn: $overwrite)
            }
        case .normalize:
            Picker("Format", selection: $transcriptFormat) {
                Text("Markdown").tag("md")
                Text("Plain text").tag("txt")
                Text("SRT").tag("srt")
                Text("VTT").tag("vtt")
                Text("CSV").tag("csv")
            }
            .fixedSize()
        case .find:
            EmptyView()
        }
    }

    private var canRun: Bool {
        guard BackendCLI.isAvailable, inputURL != nil else { return false }
        if tool == .find {
            return !searchPhrase.trimmingCharacters(in: .whitespaces).isEmpty
        }
        return true
    }

    private func chooseInput() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = tool != .convertDir
        panel.canChooseDirectories = tool == .convertDir
        panel.allowsMultipleSelection = false
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            Task { @MainActor in
                inputURL = url
            }
        }
    }

    private func run() async {
        guard let inputURL else { return }
        isRunning = true
        defer { isRunning = false }
        output = ""
        lastWrittenURL = nil
        do {
            let result: CLIResult
            switch tool {
            case .convert:
                let destination = uniqueOutputURL(
                    stem: inputURL.deletingPathExtension().lastPathComponent,
                    fileExtension: format
                )
                var arguments = [
                    "convert", inputURL.path,
                    "--format", format,
                    "--output", destination.path,
                ]
                if overwrite { arguments.append("--overwrite") }
                result = try await BackendCLI.run(arguments)
                if result.succeeded { lastWrittenURL = destination }
            case .convertDir:
                var arguments = [
                    "convert-dir", inputURL.path,
                    "--format", format,
                    "--output-mode", outputMode == "workspace" ? "new-directory" : outputMode,
                ]
                if outputMode == "workspace" {
                    let destination = model.outputFolderURL
                        .appendingPathComponent(inputURL.lastPathComponent + "-converted")
                    arguments += ["--output-dir", destination.path]
                    lastWrittenURL = destination
                }
                if recursive { arguments.append("--recursive") }
                if overwrite { arguments.append("--overwrite") }
                result = try await BackendCLI.run(arguments)
                if !result.succeeded { lastWrittenURL = nil }
            case .normalize:
                let destination = uniqueOutputURL(
                    stem: inputURL.deletingPathExtension().lastPathComponent + "-normalized",
                    fileExtension: transcriptFormat
                )
                result = try await BackendCLI.run([
                    "transcript-normalize", inputURL.path,
                    "--format", transcriptFormat,
                    "--output", destination.path,
                ])
                if result.succeeded { lastWrittenURL = destination }
            case .find:
                result = try await BackendCLI.run([
                    "transcript-find", inputURL.path, searchPhrase,
                ])
            }
            let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            output = text.isEmpty
                ? (result.succeeded ? "Done." : "Failed with exit code \(result.exitCode).")
                : text
        } catch {
            output = error.localizedDescription
        }
    }

    private func uniqueOutputURL(stem: String, fileExtension: String) -> URL {
        let folder = model.outputFolderURL
        var candidate = folder.appendingPathComponent("\(stem).\(fileExtension)")
        var counter = 2
        while FileManager.default.fileExists(atPath: candidate.path) && !overwrite {
            candidate = folder.appendingPathComponent("\(stem)-\(counter).\(fileExtension)")
            counter += 1
        }
        return candidate
    }
}
