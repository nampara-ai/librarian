import SwiftUI

struct SettingsView: View {
    var body: some View {
        TabView {
            GeneralSettingsView()
                .tabItem { Label("General", systemImage: "gearshape") }
            ProviderSettingsView()
                .tabItem { Label("AI Provider", systemImage: "sparkles") }
            ServerSettingsView()
                .tabItem { Label("Server", systemImage: "network") }
        }
        .frame(width: 540)
    }
}

// MARK: - General

struct GeneralSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage(AppModel.autoProcessKey) private var autoProcess = true
    @AppStorage(AppModel.outputFolderKey) private var outputFolderPath = ""

    var body: some View {
        Form {
            Section("Outputs") {
                LabeledContent("Destination folder") {
                    HStack(spacing: 8) {
                        Text(
                            outputFolderPath.isEmpty
                                ? model.outputFolderURL.path
                                : outputFolderPath
                        )
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        Button("Choose…") {
                            model.chooseOutputFolder()
                        }
                    }
                }
            }
            Section {
                Toggle("Process documents automatically after import", isOn: $autoProcess)
            } footer: {
                Text(
                    "When off, imported documents are only ingested (like "
                        + "“librarian ingest”) and wait until you press Process."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(.bottom, 8)
    }
}

// MARK: - AI Provider

enum ProviderPreset: String, CaseIterable, Identifiable {
    case builtin
    case openai
    case anthropic
    case custom

    var id: String { rawValue }

    var label: String {
        switch self {
        case .builtin: return "Built-in (no API key)"
        case .openai: return "OpenAI"
        case .anthropic: return "Anthropic"
        case .custom: return "Custom (OpenAI-compatible)"
        }
    }

    var defaultModel: String {
        switch self {
        case .builtin: return ""
        case .openai: return "gpt-4.1-mini"
        case .anthropic: return "claude-sonnet-4-6"
        case .custom: return ""
        }
    }

    var keyEnvName: String {
        switch self {
        case .anthropic: return "ANTHROPIC_API_KEY"
        default: return "OPENAI_API_KEY"
        }
    }

    var fixedBaseURL: String? {
        switch self {
        case .anthropic: return "https://api.anthropic.com/v1"
        case .openai, .builtin: return nil
        case .custom: return nil
        }
    }
}

struct ProviderSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var preset: ProviderPreset = .builtin
    @State private var apiKey = ""
    @State private var modelName = ""
    @State private var baseURL = ""
    @State private var statusMessage: String?
    @State private var statusIsError = false
    @State private var isApplying = false

    private var embeddedConfigurable: Bool {
        BackendController.isEmbeddedAvailable && model.useEmbeddedBackend
    }

    var body: some View {
        Form {
            Section {
                Picker("Provider", selection: $preset) {
                    ForEach(ProviderPreset.allCases) { candidate in
                        Text(candidate.label).tag(candidate)
                    }
                }
                .onChange(of: preset) {
                    modelName = preset.defaultModel
                    if preset != .custom {
                        baseURL = preset.fixedBaseURL ?? ""
                    }
                }
            } footer: {
                Text(
                    preset == .builtin
                        ? "The built-in cleaner works offline with no account. Connect a "
                            + "provider for higher-quality cleaning and classification."
                        : "The key is stored in the backend configuration file inside "
                            + "your data folder and never leaves this Mac except to call "
                            + "the provider."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            if preset != .builtin {
                Section {
                    SecureField("API key", text: $apiKey)
                        .textFieldStyle(.roundedBorder)
                    TextField("Model", text: $modelName, prompt: Text(preset.defaultModel))
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                    if preset == .custom {
                        TextField(
                            "Base URL",
                            text: $baseURL,
                            prompt: Text("https://api.example.com/v1")
                        )
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                    }
                }
            }

            Section {
                HStack {
                    Button(isApplying ? "Applying…" : "Apply & Restart Backend") {
                        Task { await apply() }
                    }
                    .disabled(isApplying || !embeddedConfigurable)
                    if let statusMessage {
                        Label(
                            statusMessage,
                            systemImage: statusIsError
                                ? "xmark.circle.fill" : "checkmark.circle.fill"
                        )
                        .foregroundStyle(statusIsError ? .red : .green)
                        .font(.callout)
                    }
                }
            } footer: {
                Text(
                    embeddedConfigurable
                        ? "Applying restarts the built-in backend so the provider takes "
                            + "effect immediately."
                        : "Provider settings configure the built-in backend. For an "
                            + "external server, set LIBRARIAN_LLM_* on that server instead."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(.bottom, 8)
        .onAppear(perform: loadCurrent)
    }

    private func loadCurrent() {
        let values = EnvFile.read()
        let provider = values["LIBRARIAN_LLM_PROVIDER"] ?? "mock"
        let storedBase = values["LIBRARIAN_LLM_BASE_URL"] ?? ""
        if provider != "openai-compatible" {
            preset = .builtin
        } else if storedBase == ProviderPreset.anthropic.fixedBaseURL {
            preset = .anthropic
        } else if storedBase.isEmpty {
            preset = .openai
        } else {
            preset = .custom
        }
        modelName = values["LIBRARIAN_LLM_MODEL"] ?? preset.defaultModel
        baseURL = storedBase
        apiKey = values[preset.keyEnvName] ?? ""
    }

    private func apply() async {
        isApplying = true
        defer { isApplying = false }
        statusMessage = nil
        do {
            // Note: assigning `nil` via subscript would drop the entry, so use
            // updateValue to record explicit deletions for EnvFile.
            var updates: [String: String?] = [:]
            switch preset {
            case .builtin:
                updates["LIBRARIAN_LLM_PROVIDER"] = "mock"
                updates.updateValue(nil, forKey: "LIBRARIAN_LLM_MODEL")
                updates.updateValue(nil, forKey: "LIBRARIAN_LLM_BASE_URL")
                updates.updateValue(nil, forKey: "LIBRARIAN_LLM_API_KEY_ENV")
            case .openai, .anthropic, .custom:
                let trimmedKey = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !trimmedKey.isEmpty else {
                    statusIsError = true
                    statusMessage = "Enter an API key"
                    return
                }
                let resolvedModel = modelName.isEmpty ? preset.defaultModel : modelName
                guard !resolvedModel.isEmpty else {
                    statusIsError = true
                    statusMessage = "Enter a model name"
                    return
                }
                updates["LIBRARIAN_LLM_PROVIDER"] = "openai-compatible"
                updates["LIBRARIAN_LLM_MODEL"] = resolvedModel
                updates[preset.keyEnvName] = trimmedKey
                if preset == .anthropic {
                    updates["LIBRARIAN_LLM_BASE_URL"] = preset.fixedBaseURL
                    updates["LIBRARIAN_LLM_API_KEY_ENV"] = preset.keyEnvName
                } else if preset == .custom {
                    let trimmedBase = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !trimmedBase.isEmpty else {
                        statusIsError = true
                        statusMessage = "Enter the provider's base URL"
                        return
                    }
                    updates["LIBRARIAN_LLM_BASE_URL"] = trimmedBase
                    updates.updateValue(nil, forKey: "LIBRARIAN_LLM_API_KEY_ENV")
                } else {
                    updates.updateValue(nil, forKey: "LIBRARIAN_LLM_BASE_URL")
                    updates.updateValue(nil, forKey: "LIBRARIAN_LLM_API_KEY_ENV")
                }
            }
            try EnvFile.update(updates)
            await model.restartBackend()
            statusIsError = false
            statusMessage = model.serverOnline
                ? "Applied — backend restarted"
                : "Saved — backend restarting…"
        } catch {
            statusIsError = true
            statusMessage = error.localizedDescription
        }
    }
}

// MARK: - Server

struct ServerSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage(AppModel.baseURLKey) private var baseURL = AppModel.defaultBaseURL
    @AppStorage(AppModel.apiKeyKey) private var apiKey = ""
    @AppStorage(AppModel.useEmbeddedKey) private var useEmbedded = true
    @State private var testResult: String?
    @State private var testOK = false

    private var embeddedAvailable: Bool {
        BackendController.isEmbeddedAvailable
    }

    var body: some View {
        Form {
            if embeddedAvailable {
                Section {
                    Toggle("Run the built-in backend automatically", isOn: $useEmbedded)
                        .onChange(of: useEmbedded) {
                            Task { await model.applyBackendPreference() }
                        }
                    LabeledContent("Data folder") {
                        Button("Reveal in Finder") {
                            model.backend.revealDataFolder()
                        }
                    }
                } header: {
                    Text("Built-in backend")
                } footer: {
                    Text(
                        "Documents, the database, and converted outputs live in "
                            + "~/Library/Application Support/Librarian."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
            Section("External server") {
                TextField("Server URL", text: $baseURL, prompt: Text(AppModel.defaultBaseURL))
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .disabled(embeddedAvailable && useEmbedded)
                SecureField("API key (optional)", text: $apiKey)
                    .textFieldStyle(.roundedBorder)
                    .disabled(embeddedAvailable && useEmbedded)
            }
            Section {
                HStack {
                    Button("Test Connection") {
                        Task { await testConnection() }
                    }
                    if let testResult {
                        Label(
                            testResult,
                            systemImage: testOK ? "checkmark.circle.fill" : "xmark.circle.fill"
                        )
                        .foregroundStyle(testOK ? .green : .red)
                        .font(.callout)
                    }
                }
            } footer: {
                Text(
                    embeddedAvailable
                        ? "Turn off the built-in backend to point the app at a remote "
                            + "Librarian server instead."
                        : "This build has no bundled backend. Start one with "
                            + "“librarian api” or point the URL at a remote server."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(.bottom, 8)
    }

    private func testConnection() async {
        do {
            let version = try await model.client.version()
            testOK = true
            testResult = "Connected to Librarian v\(version)"
        } catch {
            testOK = false
            testResult = error.localizedDescription
        }
        await model.refresh()
    }
}
