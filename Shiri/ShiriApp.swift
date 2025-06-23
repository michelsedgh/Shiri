import SwiftUI

@main
struct ShiriApp: App {
    // Keep a single instance of the audio pipeline manager
    private var audioPipelineManager = AudioPipelineManager()

    // Lazily initialize managers that depend on the audio pipeline
    @StateObject private var bridgeManager = BridgeManager()
    @StateObject private var networkManager = NetworkManager()
    @StateObject private var dockerManager: DockerManager

    init() {
        // Use a single audio manager instance for both Docker and the environment
        let audioManager = AudioPipelineManager()
        _dockerManager = StateObject(wrappedValue: DockerManager(audioPipelineManager: audioManager))
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(bridgeManager)
                .environmentObject(dockerManager)
                .environmentObject(networkManager)
                .environmentObject(audioPipelineManager) // Pass the single instance
        }

        // Define a separate window for the AddBridgeView to work around the text input bug
        Window("Add New Bridge", id: "add-bridge") {
            AddBridgeView()
                .environmentObject(bridgeManager)
                .environmentObject(networkManager)
                // Also pass the audio pipeline manager if AddBridgeView needs it
                .environmentObject(audioPipelineManager)
        }
    }
}