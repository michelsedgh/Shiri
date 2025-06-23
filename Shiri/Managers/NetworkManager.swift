import Foundation
import Network
import Combine

@MainActor
class NetworkManager: ObservableObject {
    @Published private(set) var availableInterfaces: [NWInterface] = []
    @Published private(set) var discoveredSpeakers: [AirPlayDevice] = []
    
    private var browser: NWBrowser?
    
    init() {
        discoverInterfaces()
    }
    
    private func discoverInterfaces() {
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { path in
            Task { @MainActor in
                self.availableInterfaces = path.availableInterfaces
                monitor.cancel() // We only need the list once.
            }
        }
        monitor.start(queue: .main)
    }
    
    func startSpeakerDiscovery(on interface: NWInterface?) {
        browser?.cancel() // Cancel any previous browsing
        
        let parameters = NWParameters()
        parameters.includePeerToPeer = true // Important for AirPlay
        if let requiredInterface = interface {
            parameters.requiredInterface = requiredInterface
        }
        
        browser = NWBrowser(for: .bonjour(type: "_airplay._tcp", domain: nil), using: parameters)
        
        browser?.browseResultsChangedHandler = { results, changes in
            Task { @MainActor in
                self.discoveredSpeakers = results.compactMap { result in
                    if case .service(let name, _, _, _) = result.endpoint {
                        return AirPlayDevice(id: name, name: name, interface: result.interfaces.first)
                    }
                    return nil
                }
                print("Discovered speakers: \(self.discoveredSpeakers.count)")
            }
        }
        
        browser?.stateUpdateHandler = { newState in
             Task { @MainActor in
                switch newState {
                case .ready:
                    print("AirPlay Speaker Browser is ready.")
                case .failed(let error):
                    print("AirPlay Speaker Browser failed with error: \(error.localizedDescription)")
                    self.browser?.cancel()
                default:
                    break
                }
            }
        }
        
        browser?.start(queue: .main) // Dispatch to the main queue
    }
    
    func stopSpeakerDiscovery() {
        browser?.cancel()
        browser = nil
    }
} 