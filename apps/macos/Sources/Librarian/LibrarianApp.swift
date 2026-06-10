import AppKit
import SwiftUI

@main
struct LibrarianApp: App {
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
        }

        Settings {
            SettingsView()
                .environmentObject(model)
        }
    }
}
