import SwiftUI
import Foundation
import Combine

/// The main coordinator for the application, responsible for managing all bridges and their components.
@MainActor
class BridgeManager: ObservableObject {
    
    @Published private(set) var bridges: [BridgeConfig] = []
    private let userDefaultsKey = "Shiri.Bridges"
    
    init() {
        // Load bridges on initialization
        loadBridges()
    }
    
    // We will add logic here to:
    // - Load and save bridge configurations
    // - Create, start, and stop bridges
    // - Coordinate the Docker, Network, and Audio managers
    
    func loadBridges() {
        if let data = UserDefaults.standard.data(forKey: userDefaultsKey) {
            do {
                let decodedBridges = try JSONDecoder().decode([BridgeConfig].self, from: data)
                self.bridges = decodedBridges
                print("Successfully loaded \(bridges.count) bridges from UserDefaults.")
            } catch {
                print("Failed to decode bridges from UserDefaults: \(error.localizedDescription)")
            }
        }
    }
    
    func saveBridges() {
        do {
            let data = try JSONEncoder().encode(bridges)
            UserDefaults.standard.set(data, forKey: userDefaultsKey)
            print("Successfully saved \(bridges.count) bridges.")
        } catch {
            print("Failed to encode bridges for saving: \(error.localizedDescription)")
        }
    }
    
    func createBridge(name: String, airplayName: String, targetSpeakerIDs: Set<String>) {
        let containerName = "sps-\(name.lowercased().replacingOccurrences(of: " ", with: "-"))"
        let newBridge = BridgeConfig(name: name, airplayName: airplayName, containerName: containerName, targetSpeakerIDs: targetSpeakerIDs)
        bridges.append(newBridge)
        saveBridges()
    }
    
    func deleteBridge(at offsets: IndexSet) {
        // TODO: Add logic to stop container and remove bridge
        bridges.remove(atOffsets: offsets)
        saveBridges()
    }
} 