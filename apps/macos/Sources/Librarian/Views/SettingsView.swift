import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage(AppModel.baseURLKey) private var baseURL = AppModel.defaultBaseURL
    @AppStorage(AppModel.apiKeyKey) private var apiKey = ""
    @State private var testResult: String?
    @State private var testOK = false

    var body: some View {
        Form {
            Section("Server") {
                TextField("Server URL", text: $baseURL, prompt: Text(AppModel.defaultBaseURL))
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                SecureField("API key (optional)", text: $apiKey)
                    .textFieldStyle(.roundedBorder)
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
                    "The app talks to a running Librarian backend. Start one locally with "
                        + "“librarian api”."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 440)
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
