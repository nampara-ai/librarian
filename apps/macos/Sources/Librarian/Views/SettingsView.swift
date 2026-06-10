import SwiftUI

struct SettingsView: View {
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
                            + "~/Library/Application Support/Librarian. Add a .env file "
                            + "there to configure an LLM provider."
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
        .frame(width: 460)
        .fixedSize(horizontal: false, vertical: true)
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
