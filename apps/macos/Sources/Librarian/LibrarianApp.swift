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

    init() {
        // Allows `swift run` launches (no app bundle) to show a real window.
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    var body: some Scene {
        WindowGroup("Librarian") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 900, minHeight: 560)
                .task {
                    AppDelegate.model = model
                }
        }

        Settings {
            SettingsView()
                .environmentObject(model)
        }
    }
}
