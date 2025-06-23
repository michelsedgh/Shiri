import Foundation
import Combine
import NIO

@MainActor
class DockerManager: ObservableObject {
    
    enum Status {
        case unknown
        case running
        case notRunning
    }
    
    enum ContainerState {
        case running
        case stopped
        case error(String)
        case unknown
    }
    
    @Published private(set) var status: Status = .unknown
    @Published private(set) var containerStates: [String: ContainerState] = [:]
    
    private let dockerAPI = DockerAPI()
    private let audioPipelineManager: AudioPipelineManager
    
    init(audioPipelineManager: AudioPipelineManager) {
        self.audioPipelineManager = audioPipelineManager
        
        Task {
            await checkDockerStatus()
        }
    }
    
    func checkDockerStatus() async {
        print("Checking Docker status...")
        do {
            let responseString = try await dockerAPI.ping().get()
            print("Docker ping response: '\(responseString)'")
            print("Response length: \(responseString.count)")
            print("Response bytes: \(Array(responseString.utf8))")
            
            // Docker ping returns "OK" but sometimes with extra whitespace
            let trimmedResponse = responseString.trimmingCharacters(in: .whitespacesAndNewlines)
            print("Trimmed response: '\(trimmedResponse)'")
            
            let isRunning = (trimmedResponse == "OK" || !trimmedResponse.isEmpty)
            print("Setting status to: \(isRunning ? "running" : "notRunning")")
            
            self.status = isRunning ? .running : .notRunning
        } catch {
            print("Docker ping failed: \(error)")
            print("Error details: \(error.localizedDescription)")
            self.status = .notRunning
        }
        print("Docker status updated to: \(self.status)")
    }
    
    func start(bridge: BridgeConfig, bridgeManager: BridgeManager) async {
        print("Attempting to start bridge: \(bridge.name)")
        
        // Use /tmp since we're no longer sandboxed
        let tempDir = "/tmp/\(bridge.containerName)"
        
        do {
            try FileManager.default.createDirectory(atPath: tempDir, withIntermediateDirectories: true, attributes: nil)
            print("Created temp directory: \(tempDir)")
        } catch {
            print("Failed to create temp directory \(tempDir): \(error.localizedDescription)")
            self.containerStates[bridge.containerName] = .error("Failed to create directory: \(error.localizedDescription)")
            return
        }
        
        let createConfig = makeCreateConfig(for: bridge, tempDir: tempDir)
        
        do {
            print("Creating container with config: \(createConfig)")
            let createResponse = try await dockerAPI.createContainer(name: bridge.containerName, config: createConfig).get()
            print("Container created with ID: \(createResponse.Id)")
            
            print("Starting container: \(createResponse.Id)")
            try await dockerAPI.startContainer(id: createResponse.Id).get()
            
            print("Successfully started container for bridge: \(bridge.name)")
            self.containerStates[bridge.containerName] = .running
            await audioPipelineManager.startPipeline(for: bridge, bridgeManager: bridgeManager)
            
        } catch {
            print("Failed to start bridge: \(error)")
            print("Error details: \(error.localizedDescription)")
            self.containerStates[bridge.containerName] = .error(error.localizedDescription)
        }
    }
    
    func stop(bridge: BridgeConfig) async {
        print("Attempting to stop bridge: \(bridge.name)")
        
        do {
            try await dockerAPI.stopContainer(id: bridge.containerName).get()
            print("Successfully stopped container \(bridge.containerName). Now removing.")
            
            try await dockerAPI.removeContainer(id: bridge.containerName).get()
            print("Successfully removed container.")
            
            self.containerStates[bridge.containerName] = .stopped
            audioPipelineManager.stopPipeline(for: bridge)
            
        } catch {
            print("Failed to stop or remove container, marking as stopped. Error: \(error.localizedDescription)")
            // If stopping fails, it might already be stopped. We assume it's stopped.
            self.containerStates[bridge.containerName] = .stopped
            audioPipelineManager.stopPipeline(for: bridge)
        }
    }
    
    private func makeCreateConfig(for bridge: BridgeConfig, tempDir: String) -> DockerCreateContainerRequest {
        let envVars = [
            "AIRPLAY_NAME=\(bridge.airplayName)",
            "AIRPLAY_BACKEND=pipe", // Deprecated name, but good for compatibility
            "SPS_OUTPUT_BACKEND=pipe",
            "SPS_PIPE_NAME=/tmp/shairport/audio",
            "SPS_METADATA_ENABLED=yes",
            "SPS_METADATA_PIPE_NAME=/tmp/shairport/metadata",
            "SPS_ALSA_IGNORE_VOLUME=yes",
            "SPS_VOLUME_RANGE_DB=\(bridge.volumeRangeDb)",
            "SPS_LATENCY_OFFSET=\(bridge.latencyOffset)",
            "SPS_SESSION_TIMEOUT=\(bridge.sessionTimeout)",
            "SPS_INTERPOLATION=soxr",
            "AIRPLAY_PORT=7000" // For AirPlay 2
        ]
        
        let hostConfig = DockerCreateContainerRequest.HostConfig(
            NetworkMode: "host",
            Binds: ["\(tempDir):/tmp/shairport"]
        )
        
        return DockerCreateContainerRequest(
            Image: "mikebrady/shairport-sync:latest",
            Env: envVars,
            HostConfig: hostConfig
        )
    }
} 