import Foundation
import Network

/// Represents a discovered AirPlay 2 device on the network.
struct AirPlayDevice: Identifiable, Hashable {
    /// The stable, unique identifier for the device provided by the system.
    let id: String
    
    /// The user-friendly name of the speaker (e.g., "Living Room HomePod").
    let name: String
    
    /// The network interface the device was discovered on.
    let interface: NWInterface?
    
    // Conformance to Hashable
    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }
    
    // Conformance to Equatable
    static func == (lhs: AirPlayDevice, rhs: AirPlayDevice) -> Bool {
        return lhs.id == rhs.id
    }
} 