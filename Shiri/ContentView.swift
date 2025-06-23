import SwiftUI
import Network
import AVKit

struct ContentView: View {
    @EnvironmentObject private var bridgeManager: BridgeManager
    @EnvironmentObject private var dockerManager: DockerManager
    @EnvironmentObject private var networkManager: NetworkManager
    @EnvironmentObject private var audioPipelineManager: AudioPipelineManager
    @Environment(\.openWindow) private var openWindow
    
    // Default interfaces. In a real app, these would be persisted.
    @State private var inputInterface: NWInterface?
    @State private var outputInterface: NWInterface?

    var body: some View {
        NavigationSplitView {
            VStack(alignment: .leading) {
                
                Form {
                    Section(header: Text("Global Network Settings")) {
                        Picker("Input (Receives AirPlay)", selection: $inputInterface) {
                            ForEach(networkManager.availableInterfaces, id: \.self) { iface in
                                Text(iface.name).tag(iface as NWInterface?)
                            }
                        }
                        Picker("Output (Sends to Speakers)", selection: $outputInterface) {
                            ForEach(networkManager.availableInterfaces, id: \.self) { iface in
                                Text(iface.name).tag(iface as NWInterface?)
                            }
                        }
                    }
                }.padding()

                
                Divider()

                HStack {
                    DockerStatusView(status: dockerManager.status)
                    Spacer()
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
            inputInterface = networkManager.availableInterfaces.first(where: { $0.name == "en0" })
            outputInterface = networkManager.availableInterfaces.first(where: { $0.name == "en6" }) ?? networkManager.availableInterfaces.first
        }
        .onChange(of: outputInterface) { newInterface in
            // When the output interface changes, restart speaker discovery
            networkManager.startSpeakerDiscovery(on: newInterface)
        }
        .frame(minWidth: 900, minHeight: 600)
    }

    private func deleteBridge(at offsets: IndexSet) {
        bridgeManager.deleteBridge(at: offsets)
    }
}

struct DockerStatusView: View {
    let status: DockerManager.Status

    var body: some View {
        HStack {
            Image(systemName: statusIcon)
                .foregroundColor(statusColor)
            Text(statusText)
                .foregroundColor(.secondary)
        }
        .padding(.vertical, 8)
    }

    private var statusIcon: String {
        switch status {
        case .unknown:
            return "questionmark.circle"
        case .running:
            return "checkmark.circle.fill"
        case .notRunning:
            return "xmark.circle.fill"
        }
    }

    private var statusColor: Color {
        switch status {
        case .unknown:
            return .yellow
        case .running:
            return .green
        case .notRunning:
            return .red
        }
    }

    private var statusText: String {
        switch status {
        case .unknown:
            return "Checking Docker status..."
        case .running:
            return "Docker is running"
        case .notRunning:
            return "Docker is not running"
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
                Image(systemName: isRunning ? "stop.fill" : "play.fill")
            }
            .buttonStyle(BorderlessButtonStyle())
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
        isRunning ? .blue : .gray
    }
    
    private func toggleBridgeState() async {
        if isRunning {
            await dockerManager.stop(bridge: bridge)
        } else {
            let pipeDir = "/tmp/\(bridge.containerName)"
            try? FileManager.default.createDirectory(atPath: pipeDir, withIntermediateDirectories: true, attributes: nil)
            await dockerManager.start(bridge: bridge, bridgeManager: bridgeManager)
        }
    }
}

struct BridgeStatusView: View {
    let status: DockerManager.ContainerState
    
    var body: some View {
        Text(statusText)
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(statusColor.opacity(0.2))
            .foregroundColor(statusColor)
            .cornerRadius(8)
    }
    
    private var statusText: String {
        switch status {
        case .running: return "Running"
        case .stopped: return "Stopped"
        case .error(_): return "Error"
        case .unknown: return "Unknown"
        }
    }
    
    private var statusColor: Color {
        switch status {
        case .running: return .green
        case .stopped: return .gray
        case .error: return .red
        case .unknown: return .yellow
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