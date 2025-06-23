import Foundation

/// Represents the complete, persisted configuration for a single AirPlay bridge.
struct BridgeConfig: Codable, Identifiable {
    /// A unique, stable identifier for the bridge.
    let id: UUID
    
    /// The user-facing name for the bridge (e.g., "Living Room").
    var name: String
    
    /// The name that is broadcast on the network via AirPlay (e.g., "Living Room Speakers").
    var airplayName: String
    
    /// The name of the Docker container that will be created for this bridge.
    var containerName: String
    
    /// A set of unique identifiers for the target AirPlay 2 speakers.
    var targetSpeakerIDs: Set<String>
    
    /// A flag indicating if this bridge should start automatically when the app launches.
    var autoStart: Bool
    
    /// The volume range for the bridge, in decibels.
    var volumeRangeDb: Int = 60
    
    /// A manual latency offset to apply, in seconds, to compensate for system delays.
    var latencyOffset: TimeInterval = 0.0
    
    /// The timeout before an inactive AirPlay session is terminated.
    var sessionTimeout: Int = 120
    
    init(id: UUID = UUID(), name: String, airplayName: String, containerName: String, targetSpeakerIDs: Set<String> = [], autoStart: Bool = false) {
        self.id = id
        self.name = name
        self.airplayName = airplayName
        self.containerName = containerName
        self.targetSpeakerIDs = targetSpeakerIDs
        self.autoStart = autoStart
    }
} 