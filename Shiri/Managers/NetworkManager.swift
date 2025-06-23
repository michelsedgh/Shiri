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
                print("Network path updated. Available interfaces count: \(path.availableInterfaces.count)")
                
                // Log all interfaces first
                for interface in path.availableInterfaces {
                    print("Found interface: \(interface.name) (type: \(interface.type))")
                }
                
                // Filter out duplicates by interface name to prevent ForEach ID conflicts
                var uniqueInterfaces: [NWInterface] = []
                var seenNames: Set<String> = []
                
                for interface in path.availableInterfaces {
                    if !seenNames.contains(interface.name) {
                        uniqueInterfaces.append(interface)
                        seenNames.insert(interface.name)
                        print("Added unique interface: \(interface.name)")
                    } else {
                        print("Skipped duplicate interface: \(interface.name)")
                    }
                }
                
                self.availableInterfaces = uniqueInterfaces
                print("Final interface list: \(self.availableInterfaces.map { $0.name })")
                monitor.cancel() // We only need the list once.
            }
        }
        let queue = DispatchQueue(label: "network-monitor")
        monitor.start(queue: queue)
        print("Started network monitor")
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