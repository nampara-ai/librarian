import Foundation

/// Single source of truth for user-facing strings (redesign spec §7).
/// The main window must never say: library, import, server, workspace, run,
/// document ID, backend, or API (except "API key" in the settings drawer).
enum Copy {
    static let emptyTitle = "Drop documents here"
    static let emptyBody =
        "PDFs, Word files, transcripts, scans, and text are converted, cleaned, "
        + "and saved to your folder — ready to use."
    static let emptyButton = "Choose Files…"
    static let destinationLabel = "Save to:"
    static let formatLabel = "Format:"

    static let stageWaiting = "Waiting"
    static let stageSending = "Sending…"
    static let stageConverting = "Converting"
    static let stageCleaning = "Cleaning"
    static let stageClassifying = "Classifying"
    static let stageSaved = "Saved"

    static let showInFinder = "Show in Finder"
    static let openFile = "Open"
    static let removeFromList = "Remove from list"
    static let retry = "Retry"
    static let clearFinished = "Clear Finished"

    static func footerActive(_ current: Int, of total: Int) -> String {
        "Cleaning \(current) of \(total)"
    }

    static func footerIdle(_ saved: Int) -> String {
        "All done — \(saved) file\(saved == 1 ? "" : "s") saved"
    }

    static let engineStarting = "Starting engine…"
    static let engineFailed = "Engine didn't start"
    static let engineFailedDetails = "Details"

    static let providerNoneNote = "Files are converted and organized without AI cleaning."
    static let keychainNote = "Stored in this Mac's keychain."
    static let setupBanner = "Add an API key or a local model to start AI cleaning."
    static let setupLink = "Set up AI cleaning…"
    static let connect = "Connect"
    static let connecting = "Connecting…"
    static let connected = "Connected — choose a model"
    static let applying = "Applying…"
    static let serverAddressLabel = "Server address"
    static let modelLabel = "Model"
    static func cleaningWith(_ model: String) -> String {
        "Cleaning with \(model)"
    }
    static func localNote(_ name: String) -> String {
        "Talks to the \(name) running on this Mac — no key needed."
    }
    static func providerConnected(_ model: String) -> String {
        "Connected — using \(model)"
    }
    static let providerKeyFailed = "Key didn't work — check it and try again."

    static let reasonTimeout = "Took too long — try again"
    static let reasonInterrupted = "Interrupted"

    /// Translate backend failure text into plain words. All failure copy
    /// lives here so wording stays in one place.
    static func userFacingReason(for backendError: String?) -> String {
        guard let raw = backendError?.lowercased(), !raw.isEmpty else {
            return "Couldn't process this file"
        }
        if raw.contains("tesseract") || raw.contains("ocr") {
            return "Scanned image — OCR isn't available in this build"
        }
        if raw.contains("unsupported file extension") || raw.contains("unsupported type") {
            return "This file type isn't supported"
        }
        if raw.contains("exceeds") && raw.contains("bytes") {
            return "File is too large"
        }
        if raw.contains("appears to be binary") {
            return "This file looks like raw binary data"
        }
        if raw.contains("timeout") || raw.contains("timed out") {
            return Copy.reasonTimeout
        }
        if raw.contains("api key") || raw.contains("authentication")
            || raw.contains("unauthorized") {
            return "The AI provider rejected the key — check it in settings"
        }
        if raw.contains("certificate") || raw.contains("ssl") || raw.contains("tls") {
            return "Secure connection to the AI provider failed — a VPN, proxy, "
                + "or security tool may be interfering"
        }
        if raw.contains("connection") || raw.contains("connect") {
            return "Couldn't reach the AI provider — check your connection"
        }
        return "Couldn't process this file"
    }
}
