import SwiftUI

enum ProviderPreset: String, CaseIterable, Identifiable {
    case anthropic
    case openai
    case compatible
    case ollama
    case none

    var id: String { rawValue }

    var label: String {
        switch self {
        case .anthropic: return "Anthropic"
        case .openai: return "OpenAI"
        case .compatible: return "OpenAI-compatible"
        case .ollama: return "Ollama"
        case .none: return "None"
        }
    }

    var defaultModel: String {
        switch self {
        case .anthropic: return "claude-sonnet-4-6"
        case .openai: return "gpt-4.1-mini"
        case .ollama: return "llama3.2"
        case .compatible, .none: return ""
        }
    }

    var keyAccount: String {
        self == .anthropic ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY"
    }

    var fixedBaseURL: String? {
        switch self {
        case .anthropic: return "https://api.anthropic.com/v1"
        case .ollama: return "http://127.0.0.1:11434/v1"
        case .openai, .compatible, .none: return nil
        }
    }

    var needsKey: Bool {
        switch self {
        case .anthropic, .openai, .compatible: return true
        case .ollama, .none: return false
        }
    }
}

/// The settings drawer: one pane. Cleaning on top, Advanced collapsed.
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    @State private var preset: ProviderPreset = .none
    @State private var apiKey = ""
    @State private var modelName = ""
    @State private var baseURL = ""
    @State private var statusLine: String?
    @State private var statusOK = false
    @State private var isApplying = false

    @AppStorage(AppModel.keepOriginalsKey) private var keepOriginals = false
    @AppStorage(AppModel.useEmbeddedKey) private var useEmbedded = true
    @AppStorage(AppModel.baseURLKey) private var externalURL = AppModel.defaultBaseURL
    @AppStorage(AppModel.apiKeyKey) private var externalKey = ""

    var body: some View {
        Form {
            Section("Cleaning") {
                Picker("Provider", selection: $preset) {
                    ForEach(ProviderPreset.allCases) { candidate in
                        Text(candidate.label).tag(candidate)
                    }
                }
                .onChange(of: preset) {
                    modelName = ""
                    statusLine = nil
                    if preset != .compatible {
                        baseURL = preset.fixedBaseURL ?? ""
                    }
                    apiKey = KeychainStore.get(preset.keyAccount) ?? ""
                }

                if preset == .none {
                    Text(Copy.providerNoneNote)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    TextField("Model", text: $modelName, prompt: Text(preset.defaultModel))
                        .autocorrectionDisabled()
                    if preset == .compatible {
                        TextField(
                            "Base URL",
                            text: $baseURL,
                            prompt: Text("https://api.example.com/v1")
                        )
                        .autocorrectionDisabled()
                    }
                    if preset.needsKey {
                        VStack(alignment: .leading, spacing: 3) {
                            SecureField("API key", text: $apiKey)
                                .onSubmit { Task { await applyAndValidate() } }
                            Text(Copy.keychainNote)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                HStack(spacing: 10) {
                    Button(isApplying ? "Applying…" : "Apply") {
                        Task { await applyAndValidate() }
                    }
                    .disabled(isApplying)
                    if let statusLine {
                        Label(
                            statusLine,
                            systemImage: statusOK
                                ? "checkmark.circle.fill" : "xmark.circle.fill"
                        )
                        .foregroundStyle(statusOK ? .green : .red)
                        .font(.callout)
                    }
                }
            }

            Section {
                DisclosureGroup("Advanced") {
                    Toggle("Also keep original files in the destination", isOn: $keepOriginals)
                    DisclosureGroup("Connect to a Librarian server instead") {
                        Toggle("Use the built-in engine", isOn: $useEmbedded)
                            .onChange(of: useEmbedded) {
                                Task { await model.applyBackendPreference() }
                            }
                        TextField(
                            "Server URL",
                            text: $externalURL,
                            prompt: Text(AppModel.defaultBaseURL)
                        )
                        .autocorrectionDisabled()
                        .disabled(useEmbedded)
                        SecureField("Server API key", text: $externalKey)
                            .disabled(useEmbedded)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .fixedSize(horizontal: false, vertical: true)
        .onAppear(perform: loadCurrent)
    }

    private func loadCurrent() {
        let values = EnvFile.read()
        let provider = values["LIBRARIAN_LLM_PROVIDER"] ?? "mock"
        let storedBase = values["LIBRARIAN_LLM_BASE_URL"] ?? ""
        if provider != "openai-compatible" {
            preset = .none
        } else if storedBase == ProviderPreset.anthropic.fixedBaseURL {
            preset = .anthropic
        } else if storedBase == ProviderPreset.ollama.fixedBaseURL {
            preset = .ollama
        } else if storedBase.isEmpty {
            preset = .openai
        } else {
            preset = .compatible
        }
        modelName = values["LIBRARIAN_LLM_MODEL"] ?? ""
        baseURL = storedBase
        apiKey = KeychainStore.get(preset.keyAccount) ?? ""
    }

    private func applyAndValidate() async {
        isApplying = true
        defer { isApplying = false }
        statusLine = nil

        let resolvedModel = modelName.isEmpty ? preset.defaultModel : modelName
        let trimmedKey = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedBase = preset == .compatible
            ? baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            : (preset.fixedBaseURL ?? "")

        if preset.needsKey && trimmedKey.isEmpty {
            statusOK = false
            statusLine = Copy.providerKeyFailed
            return
        }
        if preset == .compatible && resolvedBase.isEmpty {
            statusOK = false
            statusLine = "Enter the provider's base URL"
            return
        }

        // The key lives in the Keychain only; .env keeps the non-secrets.
        var updates: [String: String?] = [:]
        for account in ProviderCredentials.knownKeyAccounts {
            updates.updateValue(nil, forKey: account)
        }
        if preset == .none {
            updates["LIBRARIAN_LLM_PROVIDER"] = "mock"
            updates.updateValue(nil, forKey: "LIBRARIAN_LLM_MODEL")
            updates.updateValue(nil, forKey: "LIBRARIAN_LLM_BASE_URL")
            updates.updateValue(nil, forKey: "LIBRARIAN_LLM_API_KEY_ENV")
        } else {
            KeychainStore.set(
                trimmedKey.isEmpty ? "local" : trimmedKey,
                account: preset.keyAccount
            )
            updates["LIBRARIAN_LLM_PROVIDER"] = "openai-compatible"
            updates["LIBRARIAN_LLM_MODEL"] = resolvedModel
            if resolvedBase.isEmpty {
                updates.updateValue(nil, forKey: "LIBRARIAN_LLM_BASE_URL")
            } else {
                updates["LIBRARIAN_LLM_BASE_URL"] = resolvedBase
            }
            if preset.keyAccount == "OPENAI_API_KEY" {
                updates.updateValue(nil, forKey: "LIBRARIAN_LLM_API_KEY_ENV")
            } else {
                updates["LIBRARIAN_LLM_API_KEY_ENV"] = preset.keyAccount
            }
        }
        do {
            try EnvFile.update(updates)
        } catch {
            statusOK = false
            statusLine = error.localizedDescription
            return
        }

        if preset == .none {
            await model.restartBackend()
            statusOK = true
            statusLine = Copy.providerNoneNote
            return
        }

        // Validate the key directly against the provider before restarting.
        let valid = await Self.validateKey(
            base: resolvedBase.isEmpty ? "https://api.openai.com/v1" : resolvedBase,
            key: trimmedKey
        )
        await model.restartBackend()
        statusOK = valid
        statusLine = valid ? Copy.providerConnected(resolvedModel) : Copy.providerKeyFailed
    }

    /// GET {base}/models with the key; 2xx means the credentials work.
    private static func validateKey(base: String, key: String) async -> Bool {
        guard let url = URL(string: base.hasSuffix("/") ? base + "models" : base + "/models")
        else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 3
        if !key.isEmpty {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            request.setValue(key, forHTTPHeaderField: "x-api-key")
            // Anthropic's native API requires a version header; everyone
            // else ignores it.
            request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        }
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return false }
            return (200..<300).contains(http.statusCode)
        } catch {
            return false
        }
    }
}
