import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedDocumentID: String?
    @State private var showActivity = true
    @State private var isDropTargeted = false

    var body: some View {
        NavigationSplitView {
            SidebarView(selection: $selectedDocumentID)
                .navigationSplitViewColumnWidth(min: 240, ideal: 300)
        } detail: {
            if let document = selectedDocument {
                DocumentDetailView(document: document)
            } else {
                EmptyLibraryView()
            }
        }
        .inspector(isPresented: $showActivity) {
            ActivityView()
                .inspectorColumnWidth(min: 260, ideal: 320)
        }
        .toolbar {
            ToolbarItem(placement: .navigation) {
                ServerStatusView()
            }
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showActivity.toggle()
                } label: {
                    Label("Activity", systemImage: "waveform.path.ecg")
                }
                .help("Show processing activity")
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            model.handleDrop(of: urls)
            showActivity = true
            return true
        } isTargeted: { targeted in
            withAnimation(.easeInOut(duration: 0.15)) {
                isDropTargeted = targeted
            }
        }
        .overlay {
            if isDropTargeted {
                DropOverlayView()
            }
        }
        .overlay(alignment: .bottom) {
            if let message = model.lastError {
                ErrorBannerView(message: message) {
                    model.lastError = nil
                }
                .padding(.bottom, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: model.lastError)
        .task {
            model.startPolling()
        }
    }

    private var selectedDocument: Document? {
        guard let id = selectedDocumentID else { return nil }
        return model.documents.first { $0.id == id }
    }
}

struct EmptyLibraryView: View {
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "tray.and.arrow.down")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.secondary)
            Text("Drop files anywhere")
                .font(.title2.weight(.semibold))
            Text(
                "PDFs, DOCX, Markdown, text, transcripts, and scanned images are "
                    + "converted, cleaned, and classified automatically."
            )
            .multilineTextAlignment(.center)
            .foregroundStyle(.secondary)
            .frame(maxWidth: 380)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct DropOverlayView: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
            RoundedRectangle(cornerRadius: 16)
                .strokeBorder(style: StrokeStyle(lineWidth: 2, dash: [8, 6]))
                .foregroundStyle(.tint)
            VStack(spacing: 12) {
                Image(systemName: "arrow.down.doc.fill")
                    .font(.system(size: 44))
                Text("Drop to import")
                    .font(.title3.weight(.semibold))
            }
            .foregroundStyle(.tint)
        }
        .padding(18)
        .allowsHitTesting(false)
    }
}

struct ErrorBannerView: View {
    let message: String
    let dismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.yellow)
            Text(message)
                .lineLimit(2)
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
        }
        .font(.callout)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.regularMaterial, in: Capsule())
        .shadow(radius: 4, y: 2)
    }
}
