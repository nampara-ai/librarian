import SwiftUI

enum ProviderPreset: String, CaseIterable, Identifiable {
    case anthropic
    case openai
    case deepseek
    case ollama
    case lmstudio
    case custom

    var id: String { rawValue }

    var label: String {
        switch self {
        case .anthropic: return "Anthropic"
        case .openai: return "OpenAI"
        case .deepseek: return "DeepSeek"
        case .ollama: return "Ollama"
        case .lmstudio: return "LM Studio"
        case .custom: return "Custom"
        }
    }

    /// Local servers take an address instead of an API key.
    var isLocal: Bool {
        self == .ollama || self == .lmstudio
    }

    var needsKey: Bool {
        switch self {
        case .anthropic, .openai, .deepseek: return true
        case .ollama, .lmstudio, .custom: return false
        }
    }

    /// Base URL written to the engine config (nil = client default, OpenAI).
    var configuredBaseURL: String? {
        switch self {
        case .anthropic: return "https://api.anthropic.com/v1"
        case .deepseek: return "https://api.deepseek.com/v1"
        case .ollama: return "http://127.0.0.1:11434/v1"
        case .lmstudio: return "http://127.0.0.1:1234/v1"
        case .openai, .custom: return nil
        }
    }

    /// Base URL used for the live model lookup.
    var lookupBaseURL: String {
        configuredBaseURL ?? "https://api.openai.com/v1"
    }

    var keyAccount: String {
        switch self {
        case .anthropic: return "ANTHROPIC_API_KEY"
        case .deepseek: return "DEEPSEEK_API_KEY"
        default: return "OPENAI_API_KEY"
        }
    }

    var preferredModel: String {
        switch self {
        case .anthropic: return "claude-sonnet-4-6"
        case .openai: return "gpt-4.1-mini"
        case .deepseek: return "deepseek-chat"
        case .ollama, .lmstudio, .custom: return ""
        }
    }
}

/// Paste a key (or point at a local server), press Connect, pick a model from
/// the provider's live list. Selection applies immediately. Flat layout — no
/// Form, no nesting — so there is nothing to scroll and nothing to glitch.
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    private enum Phase: Equatable {
        case idle
        case connecting
        case connected
        case applying
        case active(String)
        case failed(String)
    }

    @State private var preset: ProviderPreset = .anthropic
    @State private var apiKey = ""
    @State private var customAddress = ""
    @State private var availableModels: [String] = []
    @State private var selectedModel = ""
    @State private var manualModel = ""
    @State private var phase: Phase = .idle
    /// Only user-driven or connect-driven selection applies; restoring the
    /// saved model on open must not restart the engine.
    @State private var applyOnSelect = false

    // Engine options surfaced from the backend configuration. Guarded by
    // `engineOptionsLoaded` so restoring saved values on open doesn't
    // restart the engine.
    @State private var coherenceMode = "balanced"
    @State private var figureVisionEnabled = false
    @State private var figureVisionModel = ""
    @State private var engineOptionsLoaded = false

    @AppStorage(AppModel.keepOriginalsKey) private var keepOriginals = false
    @AppStorage(AppModel.useEmbeddedKey) private var useEmbedded = true
    @AppStorage(AppModel.baseURLKey) private var externalURL = AppModel.defaultBaseURL
    @AppStorage(AppModel.apiKeyKey) private var externalKey = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if case .idle = phase, !model.aiConfigured {
                Label(Copy.setupBanner, systemImage: "sparkles")
                    .font(.callout)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.tint.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
            }

            Picker("Provider", selection: $preset) {
                ForEach(ProviderPreset.allCases) { candidate in
                    Text(candidate.label).tag(candidate)
                }
            }
            .onChange(of: preset) {
                resetForPresetChange()
            }

            if preset.needsKey || preset == .custom {
                SecureField("API key", text: $apiKey, prompt: Text(keyPrompt))
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await connect() } }
                    .onChange(of: apiKey) {
                        // A stale "key didn't work" banner over a freshly
                        // edited key reads as a live failure; clear it.
                        if case .failed = phase { phase = .idle }
                    }
                Text(Copy.keychainNote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if preset.isLocal || preset == .custom {
                TextField(
                    Copy.serverAddressLabel,
                    text: $customAddress,
                    prompt: Text(preset.configuredBaseURL ?? "https://api.example.com/v1")
                )
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                if preset.isLocal {
                    Text(Copy.localNote(preset.label))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 10) {
                Button {
                    Task { await connect() }
                } label: {
                    if phase == .connecting {
                        ProgressView().controlSize(.small)
                    } else {
                        Text(Copy.connect)
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(phase == .connecting || phase == .applying || !canConnect)
                statusLabel
            }

            if showModelPicker {
                if availableModels.isEmpty {
                    TextField(
                        Copy.modelLabel,
                        text: $manualModel,
                        prompt: Text("model name")
                    )
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .onSubmit {
                        Task { await apply(modelName: manualModel) }
                    }
                } else {
                    Picker(Copy.modelLabel, selection: $selectedModel) {
                        ForEach(availableModels, id: \.self) { name in
                            Text(name).tag(name)
                        }
                    }
                    .onChange(of: selectedModel) {
                        guard applyOnSelect else { return }
                        Task { await apply(modelName: selectedModel) }
                    }
                }
            }

            Divider()

            DisclosureGroup("Advanced") {
                VStack(alignment: .leading, spacing: 10) {
                    Picker("Cleaning style", selection: $coherenceMode) {
                        Text("Fastest").tag("fast")
                        Text("Balanced").tag("balanced")
                        Text("Most careful").tag("max-coherence")
                    }
                    .onChange(of: coherenceMode) {
                        guard engineOptionsLoaded else { return }
                        Task { await applyEngineOptions() }
                    }
                    .help(
                        "How much surrounding context each part of a document "
                            + "gets while cleaning. Most careful reads strictly in "
                            + "order; Fastest cleans parts in parallel."
                    )
                    Toggle("Describe charts and figures with AI", isOn: $figureVisionEnabled)
                        .onChange(of: figureVisionEnabled) {
                            guard engineOptionsLoaded else { return }
                            Task { await applyEngineOptions() }
                        }
                        .help(
                            "Adds a written description under each chart or figure "
                                + "found in PDFs, using your AI provider. Costs extra "
                                + "tokens per figure."
                        )
                    if figureVisionEnabled {
                        TextField(
                            "Vision model (blank = cleaning model)",
                            text: $figureVisionModel,
                            prompt: Text("optional")
                        )
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                        .onSubmit {
                            Task { await applyEngineOptions() }
                        }
                    }
                    Divider()
                    Toggle("Also keep original files in the destination", isOn: $keepOriginals)
                    Toggle("Use the built-in engine", isOn: $useEmbedded)
                        .onChange(of: useEmbedded) {
                            // External mode needs an address; never allow the
                            // unreachable "off + empty URL" state.
                            if !useEmbedded && externalURL.trimmingCharacters(
                                in: .whitespacesAndNewlines
                            ).isEmpty {
                                externalURL = AppModel.defaultBaseURL
                            }
                            Task { await model.applyBackendPreference() }
                        }
                    TextField(
                        "Remote Librarian server URL",
                        text: $externalURL,
                        prompt: Text(AppModel.defaultBaseURL)
                    )
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .disabled(useEmbedded)
                    SecureField("Remote server API key", text: $externalKey)
                        .textFieldStyle(.roundedBorder)
                        .disabled(useEmbedded)
                }
                .padding(.top, 8)
            }
        }
        .padding(20)
        .frame(width: 480)
        .onAppear(perform: loadCurrent)
    }

    // MARK: - Pieces

    @ViewBuilder
    private var statusLabel: some View {
        switch phase {
        case .idle:
            EmptyView()
        case .connecting:
            Text(Copy.connecting)
                .font(.callout)
                .foregroundStyle(.secondary)
        case .connected:
            Label(Copy.connected, systemImage: "checkmark.circle.fill")
                .font(.callout)
                .foregroundStyle(.green)
        case .applying:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(Copy.applying)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        case .active(let name):
            Label(Copy.cleaningWith(name), systemImage: "checkmark.circle.fill")
                .font(.callout)
                .foregroundStyle(.green)
        case .failed(let reason):
            Label(reason, systemImage: "xmark.circle.fill")
                .font(.callout)
                .foregroundStyle(.red)
        }
    }

    private var keyPrompt: String {
        preset == .custom ? "API key (if the server needs one)" : "Paste your API key"
    }

    private var canConnect: Bool {
        if preset.needsKey {
            return !apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        if preset == .custom {
            return !customAddress.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return true
    }

    private var showModelPicker: Bool {
        switch phase {
        case .connected, .applying, .active: return true
        default: return false
        }
    }

    private var lookupBase: String {
        if preset == .custom || preset.isLocal {
            let typed = customAddress.trimmingCharacters(in: .whitespacesAndNewlines)
            if !typed.isEmpty { return typed }
        }
        return preset.lookupBaseURL
    }

    // MARK: - Flow

    private func resetForPresetChange() {
        phase = .idle
        applyOnSelect = false
        availableModels = []
        selectedModel = ""
        manualModel = ""
        customAddress = preset.isLocal ? (preset.configuredBaseURL ?? "") : ""
        // Never pre-fill the Custom preset from the shared OPENAI_API_KEY: its
        // address is user-typed, so a prefilled real key would be sent to an
        // arbitrary (possibly hostile or mistyped) URL. Require an explicit paste.
        apiKey = preset == .custom ? "" : (KeychainStore.get(preset.keyAccount) ?? "")
    }

    private func loadCurrent() {
        let values = EnvFile.read()
        coherenceMode = values["LIBRARIAN_COHERENCE_MODE"] ?? "balanced"
        figureVisionEnabled = values["LIBRARIAN_FIGURE_VISION_ENABLED"] == "true"
        figureVisionModel = values["LIBRARIAN_FIGURE_VISION_MODEL"] ?? ""
        engineOptionsLoaded = true
        guard values["LIBRARIAN_LLM_PROVIDER"] == "openai-compatible" else {
            resetForPresetChange()
            return
        }
        let base = values["LIBRARIAN_LLM_BASE_URL"] ?? ""
        if base == ProviderPreset.anthropic.configuredBaseURL {
            preset = .anthropic
        } else if base == ProviderPreset.deepseek.configuredBaseURL {
            preset = .deepseek
        } else if base.contains("11434") {
            preset = .ollama
        } else if base.contains("1234") {
            preset = .lmstudio
        } else if base.isEmpty {
            preset = .openai
        } else {
            preset = .custom
        }
        // See resetForPresetChange: the Custom preset must not inherit the
        // shared OpenAI key, since it would be sent to the user-typed address.
        apiKey = preset == .custom ? "" : (KeychainStore.get(preset.keyAccount) ?? "")
        customAddress = (preset.isLocal || preset == .custom) ? base : ""
        let current = values["LIBRARIAN_LLM_MODEL"] ?? ""
        if !current.isEmpty {
            availableModels = [current]
            selectedModel = current
            manualModel = current
            phase = .active(current)
        }
    }

    /// Persist the Advanced engine options and restart the engine so they
    /// apply. Defaults are removed from the file rather than written, so a
    /// hand-edited .env only carries deliberate overrides.
    private func applyEngineOptions() async {
        var updates: [String: String?] = [:]
        updates.updateValue(
            coherenceMode == "balanced" ? nil : coherenceMode,
            forKey: "LIBRARIAN_COHERENCE_MODE"
        )
        updates.updateValue(
            figureVisionEnabled ? "true" : nil,
            forKey: "LIBRARIAN_FIGURE_VISION_ENABLED"
        )
        let visionModel = figureVisionModel.trimmingCharacters(in: .whitespacesAndNewlines)
        updates.updateValue(
            (figureVisionEnabled && !visionModel.isEmpty) ? visionModel : nil,
            forKey: "LIBRARIAN_FIGURE_VISION_MODEL"
        )
        do {
            try EnvFile.update(updates)
        } catch {
            phase = .failed(error.localizedDescription)
            return
        }
        await model.restartBackend()
    }

    private func connect() async {
        guard canConnect else { return }
        phase = .connecting
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let fetched = await ModelDirectory.fetch(base: lookupBase, key: key)
        guard let fetched else {
            phase = .failed(Copy.providerKeyFailed)
            return
        }
        availableModels = ModelDirectory.curate(fetched, for: preset)
        if availableModels.isEmpty {
            // Reachable, but no readable list: manual model entry.
            phase = .connected
            return
        }
        let preferred = preset.preferredModel
        let initial = availableModels.contains(preferred)
            ? preferred
            : availableModels[0]
        phase = .connected
        applyOnSelect = true
        if selectedModel == initial {
            await apply(modelName: initial)
        } else {
            // Triggers onChange, which applies.
            selectedModel = initial
        }
    }

    private func apply(modelName: String) async {
        let chosen = modelName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !chosen.isEmpty else { return }
        phase = .applying
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        // Only store a key the user actually entered. Local servers (Ollama,
        // LM Studio, keyless custom) write nothing to the Keychain; the
        // engine gets a transient placeholder in its environment instead.
        if !key.isEmpty {
            guard KeychainStore.set(key, account: preset.keyAccount) else {
                phase = .failed(Copy.keychainSaveFailed)
                return
            }
        }

        var updates: [String: String?] = [:]
        for account in ProviderCredentials.knownKeyAccounts {
            updates.updateValue(nil, forKey: account)
        }
        updates["LIBRARIAN_LLM_PROVIDER"] = "openai-compatible"
        updates["LIBRARIAN_LLM_MODEL"] = chosen
        let base = (preset == .custom || preset.isLocal) ? lookupBase : preset.configuredBaseURL
        if let base, !base.isEmpty {
            updates["LIBRARIAN_LLM_BASE_URL"] = base
        } else {
            updates.updateValue(nil, forKey: "LIBRARIAN_LLM_BASE_URL")
        }
        if preset.keyAccount == "OPENAI_API_KEY" {
            updates.updateValue(nil, forKey: "LIBRARIAN_LLM_API_KEY_ENV")
        } else {
            updates["LIBRARIAN_LLM_API_KEY_ENV"] = preset.keyAccount
        }
        do {
            try EnvFile.update(updates)
        } catch {
            phase = .failed(error.localizedDescription)
            return
        }
        await model.restartBackend()
        phase = .active(chosen)
    }
}

/// Live model discovery against any OpenAI-compatible /models endpoint
/// (OpenAI, Anthropic, DeepSeek, Ollama, LM Studio all speak it).
enum ModelDirectory {
    /// nil = unreachable or rejected key; [] = reachable but no readable list.
    static func fetch(base: String, key: String) async -> [String]? {
        let endpoint = base.hasSuffix("/") ? base + "models" : base + "/models"
        guard let url = URL(string: endpoint) else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        if !key.isEmpty {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
            request.setValue(key, forHTTPHeaderField: "x-api-key")
            // Anthropic's native API requires a version header; others ignore it.
            request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        }
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse,
                  (200..<300).contains(http.statusCode) else {
                return nil
            }
            struct ModelList: Codable {
                struct Entry: Codable {
                    let id: String
                }
                let data: [Entry]
            }
            guard let list = try? JSONDecoder().decode(ModelList.self, from: data) else {
                return []
            }
            return list.data.map(\.id)
        } catch {
            return nil
        }
    }

    /// Hide non-chat models so the dropdown only offers things that clean text.
    static func curate(_ ids: [String], for preset: ProviderPreset) -> [String] {
        var ids = ids
        if preset == .openai {
            let excluded = [
                "embedding", "whisper", "tts", "dall-e", "audio", "moderation",
                "davinci", "babbage", "realtime", "transcribe", "image",
            ]
            ids = ids.filter { id in !excluded.contains { id.contains($0) } }
        }
        return ids.sorted()
    }
}
