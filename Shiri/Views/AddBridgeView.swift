import SwiftUI
import AppKit

struct AppKitTextField: NSViewRepresentable {
    @Binding var text: String
    let placeholder: String
    
    func makeNSView(context: Context) -> NSTextField {
        let textField = NSTextField()
        textField.stringValue = text
        textField.placeholderString = placeholder
        textField.delegate = context.coordinator
        return textField
    }
    
    func updateNSView(_ nsView: NSTextField, context: Context) {
        nsView.stringValue = text
    }
    
    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }
    
    class Coordinator: NSObject, NSTextFieldDelegate {
        let parent: AppKitTextField
        
        init(_ parent: AppKitTextField) {
            self.parent = parent
        }
        
        func controlTextDidChange(_ obj: Notification) {
            if let textField = obj.object as? NSTextField {
                parent.text = textField.stringValue
            }
        }
    }
}

struct AddBridgeView: View {
    @EnvironmentObject var bridgeManager: BridgeManager
    @EnvironmentObject var networkManager: NetworkManager
    @Environment(\.dismiss) private var dismiss

    @State private var name: String = ""
    @State private var airplayName: String = ""
    @State private var selectedSpeakerIDs = Set<String>()
    // Removed @FocusState to simplify text field handling

    var body: some View {
        VStack(spacing: 20) {
            VStack(alignment: .leading, spacing: 16) {
                Text("Bridge Details")
                    .font(.headline)
                    .padding(.horizontal)
                
                VStack(alignment: .leading, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Bridge Name")
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                        AppKitTextField(text: $name, placeholder: "e.g., Living Room")
                            .frame(height: 22)
                    }
                    
                    VStack(alignment: .leading, spacing: 4) {
                        Text("AirPlay Name")
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                        AppKitTextField(text: $airplayName, placeholder: "e.g., Living Room Speakers")
                            .frame(height: 22)
                    }
                }
                .padding(.horizontal)
            }
            
            VStack(alignment: .leading, spacing: 16) {
                Text("Target Speakers")
                    .font(.headline)
                    .padding(.horizontal)
                
                if networkManager.discoveredSpeakers.isEmpty {
                    Text("No speakers found on the selected output network. Check your network settings.")
                        .foregroundColor(.secondary)
                        .padding(.horizontal)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 8) {
                            ForEach(networkManager.discoveredSpeakers) { speaker in
                                Button(action: {
                                    toggleSpeakerSelection(speaker)
                                }) {
                                    HStack {
                                        Image(systemName: selectedSpeakerIDs.contains(speaker.id) ? "checkmark.circle.fill" : "circle")
                                            .foregroundColor(selectedSpeakerIDs.contains(speaker.id) ? .blue : .secondary)
                                        Text(speaker.name)
                                            .foregroundColor(.primary)
                                        Spacer()
                                    }
                                    .padding(.horizontal)
                                    .padding(.vertical, 8)
                                    .background(selectedSpeakerIDs.contains(speaker.id) ? Color.blue.opacity(0.1) : Color.clear)
                                    .cornerRadius(8)
                                }
                                .buttonStyle(PlainButtonStyle())
                            }
                        }
                        .padding(.horizontal)
                    }
                }
            }
            
            Spacer()

            HStack {
                Button("Cancel") {
                    dismiss()
                }
                // .keyboardShortcut(.cancelAction) // Temporarily disabled for debugging

                Spacer()

                Button("Save") {
                    if !name.isEmpty && !airplayName.isEmpty {
                        bridgeManager.createBridge(name: name, airplayName: airplayName, targetSpeakerIDs: selectedSpeakerIDs)
                        dismiss()
                    }
                }
                // .keyboardShortcut(.defaultAction) // Temporarily disabled for debugging
                .disabled(name.isEmpty || airplayName.isEmpty)
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .frame(minWidth: 500, idealWidth: 550, minHeight: 450)
        .background(Color(NSColor.windowBackgroundColor))
        // Removed automatic focus to avoid potential text input conflicts
    }
    
    private func toggleSpeakerSelection(_ speaker: AirPlayDevice) {
        if selectedSpeakerIDs.contains(speaker.id) {
            selectedSpeakerIDs.remove(speaker.id)
        } else {
            selectedSpeakerIDs.insert(speaker.id)
        }
    }
}

struct AddBridgeView_Previews: PreviewProvider {
    static var previews: some View {
        AddBridgeView()
            .environmentObject(BridgeManager())
            .environmentObject(NetworkManager())
    }
} 