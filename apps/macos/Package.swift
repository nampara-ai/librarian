// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Librarian",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "Librarian",
            path: "Sources/Librarian"
        )
    ]
)
