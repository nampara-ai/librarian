import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var model: AppModel?

    func applicationWillTerminate(_ notification: Notification) {
        Self.model?.shutDown()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct LibrarianApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()

    /// The site's indigo, so app and site read as one product.
    static let tint = Color(red: 61 / 255, green: 64 / 255, blue: 184 / 255)

    init() {
        // Allows `swift run` launches (no app bundle) to show a real window.
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    var body: some Scene {
        WindowGroup("Librarian") {
            ContentView()
                .environmentObject(model)
                .tint(Self.tint)
                .toolbar {
                    // Adding files must not depend on the empty-state button or
                    // drag-and-drop: once the queue is non-empty those vanish,
                    // and keyboard/VoiceOver users need a persistent control.
                    ToolbarItem(placement: .automatic) {
                        Button {
                            model.presentChooseFilesPanel()
                        } label: {
                            Label(Copy.addFiles, systemImage: "plus")
                        }
                        .help("Add documents to clean")
                    }
                    ToolbarItem(placement: .automatic) {
                        OpenAuxiliaryWindowButton(
                            title: Copy.libraryTitle,
                            systemImage: "books.vertical",
                            windowID: "library"
                        )
                        .help("Browse and search everything you've processed")
                    }
                    ToolbarItem(placement: .primaryAction) {
                        SettingsLink {
                            Label("Settings", systemImage: "gearshape")
                        }
                        .help("Cleaning provider and options")
                    }
                }
                .task {
                    AppDelegate.model = model
                }
        }
        .defaultSize(width: 680, height: 520)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button(Copy.addFiles) {
                    model.presentChooseFilesPanel()
                }
                .keyboardShortcut("o")
            }
            CommandMenu("Tools") {
                OpenAuxiliaryWindowButton(
                    title: "\(Copy.libraryTitle)…",
                    windowID: "library"
                )
                .keyboardShortcut("l")
                OpenAuxiliaryWindowButton(
                    title: "File & Transcript Tools…",
                    windowID: "tools"
                )
            }
            CommandGroup(after: .help) {
                OpenAuxiliaryWindowButton(title: "Diagnostics…", windowID: "diagnostics")
            }
        }

        Window(Copy.libraryTitle, id: "library") {
            LibraryView()
                .environmentObject(model)
                .tint(Self.tint)
        }
        .defaultSize(width: 640, height: 460)

        Window("Tools", id: "tools") {
            ToolsView()
                .environmentObject(model)
                .tint(Self.tint)
        }
        .windowResizability(.contentSize)

        Window("Diagnostics", id: "diagnostics") {
            DiagnosticsView()
                .environmentObject(model)
                .tint(Self.tint)
        }
        .windowResizability(.contentSize)

        Settings {
            SettingsView()
                .environmentObject(model)
                .tint(Self.tint)
        }
    }
}

/// Menu items need environment access to open windows; a tiny View provides it.
private struct OpenAuxiliaryWindowButton: View {
    @Environment(\.openWindow) private var openWindow
    let title: String
    var systemImage: String?
    let windowID: String

    var body: some View {
        Button {
            openWindow(id: windowID)
        } label: {
            if let systemImage {
                Label(title, systemImage: systemImage)
            } else {
                Text(title)
            }
        }
    }
}
