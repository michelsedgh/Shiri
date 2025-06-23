// swift-tools-version:5.7
import PackageDescription

let package = Package(
    name: "Shiri",
    platforms: [
        .macOS(.v15) // Target the latest macOS version for compatibility with developer beta.
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio.git", from: "2.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "Shiri",
            dependencies: [
                .product(name: "NIO", package: "swift-nio"),
                .product(name: "NIOHTTP1", package: "swift-nio")
            ],
            path: "Shiri", // Specifies that our source files are in the "Shiri" subdirectory
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Shiri/Info.plist"
                ])
            ]
        )
    ]
) 