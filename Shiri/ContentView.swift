import SwiftUI
import Network
import AVKit

struct ContentView: View {
    @EnvironmentObject private var bridgeManager: BridgeManager
    @EnvironmentObject private var dockerManager: DockerManager
    @EnvironmentObject private var networkManager: NetworkManager
    @EnvironmentObject private var audioPipelineManager: AudioPipelineManager
    @Environment(\.openWindow) private var openWindow
    
    // Use interface names as strings to avoid picker issues
    @State private var inputInterfaceName: String = ""
    @State private var outputInterfaceName: String = ""

    var body: some View {
        NavigationSplitView {
            VStack(alignment: .leading) {
                
                Form {
                    Section(header: Text("Global Network Settings")) {
                        Picker("Input (Receives AirPlay)", selection: $inputInterfaceName) {
                            Text("Auto").tag("")
                            ForEach(networkManager.availableInterfaces, id: \.name) { iface in
                                Text(iface.name).tag(iface.name)
                            }
                        }
                        Picker("Output (Sends to Speakers)", selection: $outputInterfaceName) {
                            Text("Auto").tag("")
                            ForEach(networkManager.availableInterfaces, id: \.name) { iface in
                                Text(iface.name).tag(iface.name)
                            }
                        }
                    }
                }.padding()

                
                Divider()

                HStack {
                    DockerStatusView(status: dockerManager.status)
                    Spacer()
                    Button("Refresh Docker") {
                        Task {
                            await dockerManager.checkDockerStatus()
                        }
                    }
                    .font(.caption)
                }
                .padding(.horizontal)
                
                List {
                    ForEach(bridgeManager.bridges) { bridge in
                        BridgeRowView(bridge: bridge)
                    }
                    .onDelete(perform: deleteBridge)
                }
                .listStyle(SidebarListStyle())
            }
            .navigationTitle("AirPlay Bridges")
            .frame(minWidth: 280)
        } detail: {
            // The default view when nothing is selected
            VStack {
                Text("Select a bridge to see details, or add a new one.")
                    .foregroundColor(.secondary)
                
                Button(action: {
                    // Open the dedicated "add-bridge" window instead of a sheet
                    openWindow(id: "add-bridge")
                }) {
                    Label("Add Bridge", systemImage: "plus")
                        .font(.title2)
                        .padding()
                        .background(Color.blue.opacity(0.1))
                        .cornerRadius(8)
                }
                .buttonStyle(PlainButtonStyle())
                .padding(.top)
            }
        }
        .onAppear {
            // Set default interfaces when the view appears
            if networkManager.availableInterfaces.contains(where: { $0.name == "en0" }) {
                inputInterfaceName = "en0"
            } else {
                inputInterfaceName = networkManager.availableInterfaces.first?.name ?? ""
            }
            
            if let preferredInterface = networkManager.availableInterfaces.first(where: { $0.name == "en6" }) {
                outputInterfaceName = preferredInterface.name
            } else {
                outputInterfaceName = networkManager.availableInterfaces.first?.name ?? ""
            }
            
            print("Set interface selections - Input: \(inputInterfaceName), Output: \(outputInterfaceName)")
            print("Available interfaces: \(networkManager.availableInterfaces.map { $0.name })")
        }
        .onChange(of: outputInterfaceName) { newInterfaceName in
            // When the output interface changes, restart speaker discovery
            let selectedInterface = newInterfaceName.isEmpty ? nil : networkManager.availableInterfaces.first { $0.name == newInterfaceName }
            networkManager.startSpeakerDiscovery(on: selectedInterface)
        }
        .frame(minWidth: 900, minHeight: 600)
    }

    private func deleteBridge(at offsets: IndexSet) {
        bridgeManager.deleteBridge(at: offsets)
    }
}

// MARK: - Supporting Views

struct DockerStatusView: View {
    let status: DockerManager.Status
    
    var body: some View {
        HStack {
            Image(systemName: "externaldrive.connected.to.line.below")
                .foregroundColor(statusColor)
            Text("Docker: \(statusText)")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }
    
    private var statusColor: Color {
        switch status {
        case .running: return .green
        case .notRunning: return .red
        case .unknown: return .orange
        }
    }
    
    private var statusText: String {
        switch status {
        case .running: return "Running"
        case .notRunning: return "Not Running"
        case .unknown: return "Unknown"
        }
    }
}

struct BridgeStatusView: View {
    let status: DockerManager.ContainerState
    
    var body: some View {
        HStack {
            Image(systemName: statusIcon)
                .foregroundColor(statusColor)
                .font(.caption)
            Text(statusText)
                .font(.caption2)
                .foregroundColor(.secondary)
        }
    }
    
    private var statusColor: Color {
        switch status {
        case .running: return .green
        case .stopped: return .gray
        case .error: return .red
        case .unknown: return .orange
        }
    }
    
    private var statusIcon: String {
        switch status {
        case .running: return "play.circle.fill"
        case .stopped: return "stop.circle"
        case .error: return "exclamationmark.triangle.fill"
        case .unknown: return "questionmark.circle"
        }
    }
    
    private var statusText: String {
        switch status {
        case .running: return "Running"
        case .stopped: return "Stopped"
        case .error(let message): return "Error: \(message)"
        case .unknown: return "Unknown"
        }
    }
}

struct BridgeRowView: View {
    let bridge: BridgeConfig
    @EnvironmentObject var dockerManager: DockerManager
    @EnvironmentObject var audioPipelineManager: AudioPipelineManager
    @EnvironmentObject var bridgeManager: BridgeManager

    var body: some View {
        HStack {
            Image(systemName: "speaker.wave.2.circle.fill")
                .foregroundColor(statusColor)
                .font(.largeTitle)
            
            VStack(alignment: .leading) {
                Text(bridge.name)
                    .font(.headline)
                Text(bridge.airplayName)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }
            
            Spacer()
            
            BridgeStatusView(status: dockerManager.containerStates[bridge.containerName] ?? .unknown)

            Menu {
                if let availableSpeakers = audioPipelineManager.getAvailableSpeakers(for: bridge) {
                    ForEach(availableSpeakers, id: \.self) { speaker in
                        Button(action: {
                            Task {
                                await audioPipelineManager.toggleSpeaker(speaker, for: bridge)
                            }
                        }) {
                            Text(speaker)
                            if audioPipelineManager.isActive(speaker: speaker, for: bridge) {
                                Image(systemName: "checkmark")
                            }
                        }
                    }
                } else {
                    Text("No speakers available")
                }
            } label: {
                 Image(systemName: "airplayaudio")
            }
            .menuStyle(BorderlessButtonMenuStyle())
            .frame(width: 30)
            .disabled(!isRunning)
            
            Button(action: {
                Task {
                    await toggleBridgeState()
                }
            }) {
                Image(systemName: isRunning ? "stop.circle" : "play.circle")
                    .foregroundColor(isRunning ? .red : .green)
                    .font(.title2)
            }
            .buttonStyle(PlainButtonStyle())
        }
        .padding(.vertical, 4)
    }
    
    private var isRunning: Bool {
        if case .running = dockerManager.containerStates[bridge.containerName] {
            return true
        }
        return false
    }
    
    private var statusColor: Color {
        switch dockerManager.containerStates[bridge.containerName] {
        case .running: return .green
        case .stopped: return .gray
        case .error: return .red
        case .unknown, .none: return .orange
        }
    }
    
    private func toggleBridgeState() async {
        if isRunning {
            await dockerManager.stop(bridge: bridge)
        } else {
            await dockerManager.start(bridge: bridge, bridgeManager: bridgeManager)
        }
    }
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
            .environmentObject(BridgeManager())
            .environmentObject(DockerManager(audioPipelineManager: AudioPipelineManager()))
            .environmentObject(NetworkManager())
            .environmentObject(AudioPipelineManager())
    }
} 