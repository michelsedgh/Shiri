import Foundation
import AVFoundation
import AVKit
import Combine

@MainActor
class AudioPipelineManager: ObservableObject {
    
    // Published dictionary to drive the UI, showing available speakers for each bridge.
    @Published private(set) var availableSpeakers: [String: [String]] = [:]
    // Published dictionary to show which speakers are active for each bridge.
    @Published private(set) var activeSpeakers: [String: [String]] = [:]
    
    private struct Pipeline {
        let engine: AVAudioEngine
        let playerNode: AVAudioPlayerNode
        let audioPipeReader: PipeReader
        let metadataPipeReader: PipeReader
        let availableDevices: [String]
    }
    
    private var pipelines: [String: Pipeline] = [:]

    /// Starts the audio pipeline for a given bridge.
    /// This involves setting up the audio engine, routing it to the correct speakers,
    /// and starting a background thread to read from the Docker container's pipe.
    func startPipeline(for bridge: BridgeConfig, bridgeManager: BridgeManager) async {
        print("Starting audio pipeline for \(bridge.name)")
        
        let engine = AVAudioEngine()
        let playerNode = AVAudioPlayerNode()
        engine.attach(playerNode)
        
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 44100, channels: 2, interleaved: true)
        engine.connect(playerNode, to: engine.mainMixerNode, format: format)
        
        // For now, use placeholder device discovery
        // TODO: Implement proper AirPlay device discovery using AVRoutePickerView or Core Audio APIs
        let availableDevices = ["Speaker 1", "Speaker 2", "Speaker 3"] // Placeholder
        self.availableSpeakers[bridge.containerName] = availableDevices
        
        // Set initial speakers from config
        let savedDeviceIDs = Array(bridge.targetSpeakerIDs)
        self.activeSpeakers[bridge.containerName] = savedDeviceIDs
        
        do {
            try engine.start()
            playerNode.play()
        } catch {
            print("Error starting audio engine: \(error.localizedDescription)")
            return
        }
        
        let audioReader = makePipeReader(for: bridge, type: "audio") { data in
            if let buffer = self.createPCMBuffer(from: data, format: format!) {
                playerNode.scheduleBuffer(buffer)
            }
        }
        
        let metadataReader = makePipeReader(for: bridge, type: "metadata") { data in
            if let vol = self.parseVolume(from: data) { engine.mainMixerNode.outputVolume = vol }
        }
        
        audioReader.start()
        metadataReader.start()
        
        pipelines[bridge.containerName] = Pipeline(engine: engine, playerNode: playerNode, audioPipeReader: audioReader, metadataPipeReader: metadataReader, availableDevices: availableDevices)
    }
    
    /// Stops the audio pipeline for a given bridge.
    func stopPipeline(for bridge: BridgeConfig) {
        if let pipeline = pipelines.removeValue(forKey: bridge.containerName) {
            pipeline.engine.stop()
            pipeline.audioPipeReader.stop()
            pipeline.metadataPipeReader.stop()
        }
        availableSpeakers.removeValue(forKey: bridge.containerName)
        activeSpeakers.removeValue(forKey: bridge.containerName)
    }
    
    // MARK: - UI Interaction
    
    func getAvailableSpeakers(for bridge: BridgeConfig) -> [String]? {
        return pipelines[bridge.containerName]?.availableDevices
    }
    
    func isActive(speaker: String, for bridge: BridgeConfig) -> Bool {
        return activeSpeakers[bridge.containerName]?.contains(speaker) ?? false
    }
    
    func toggleSpeaker(_ speaker: String, for bridge: BridgeConfig) async {
        guard pipelines[bridge.containerName] != nil else { return }
        
        var currentSpeakers = activeSpeakers[bridge.containerName] ?? []
        
        if let index = currentSpeakers.firstIndex(of: speaker) {
            currentSpeakers.remove(at: index)
        } else {
            currentSpeakers.append(speaker)
        }
        
        // TODO: Implement actual speaker routing
        self.activeSpeakers[bridge.containerName] = currentSpeakers
        print("Toggled speaker \(speaker) for bridge \(bridge.name)")
    }
    
    // MARK: - Helpers

    private func makePipeReader(for bridge: BridgeConfig, type: String, onData: @escaping (Data) -> Void) -> PipeReader {
        let path = "/tmp/\(bridge.containerName)/\(type)"
        return PipeReader(pipePath: path, onData: onData)
    }
    
    private func createPCMBuffer(from data: Data, format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let frameCapacity = AVAudioFrameCount(data.count) / format.streamDescription.pointee.mBytesPerFrame
        guard frameCapacity > 0, let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCapacity) else { return nil }
        
        pcmBuffer.frameLength = pcmBuffer.frameCapacity
        data.withUnsafeBytes { bufferPointer in
            if let dest = pcmBuffer.int16ChannelData?[0] {
                memcpy(dest, bufferPointer.baseAddress!, data.count)
            }
        }
        return pcmBuffer
    }
    
    private func parseVolume(from data: Data) -> Float? {
        let metadata = String(data: data, encoding: .utf8) ?? ""
        if let range = metadata.range(of: "<item><type>prsv</type><code>pvol</code><data encoding=\"base64\">") {
            let from = range.upperBound
            if let to = metadata[from...].range(of: "</data>")?.lowerBound {
                let base64 = String(metadata[from..<to])
                if let data = Data(base64Encoded: base64),
                   let volumeString = String(data: data, encoding: .utf8),
                   let volumeDB = Float(volumeString) {
                    if volumeDB <= -144.0 { return 0.0 }
                    return pow(10, volumeDB / 20)
                }
            }
        }
        return nil
    }
}


/// A helper class to read from a named pipe on a background thread.
private class PipeReader {
    private let pipePath: String
    private let onData: (Data) -> Void
    private var isRunning = false
    private let queue: DispatchQueue
    
    init(pipePath: String, onData: @escaping (Data) -> Void) {
        self.pipePath = pipePath
        self.onData = onData
        self.queue = DispatchQueue(label: "com.shiri.pipereader.\(UUID().uuidString)")
    }
    
    func start() {
        isRunning = true
        queue.async { [weak self] in
            guard let self = self else { return }
            
            var attempts = 0
            while !FileManager.default.fileExists(atPath: self.pipePath) && self.isRunning && attempts < 50 {
                usleep(100_000) // 0.1 seconds, wait up to 5s
                attempts += 1
            }
            
            guard let fileHandle = FileHandle(forReadingAtPath: self.pipePath) else {
                print("Failed to open pipe at \(self.pipePath) after waiting.")
                return
            }
            
            while self.isRunning {
                if let data = try? fileHandle.read(upToCount: 32768), !data.isEmpty {
                    DispatchQueue.main.async {
                        self.onData(data)
                    }
                } else {
                    usleep(10_000) // 10ms sleep
                }
            }
            try? fileHandle.close()
        }
    }
    
    func stop() {
        isRunning = false
    }
} 