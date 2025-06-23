import Foundation

/// Codable structs for interacting with the Docker Engine API.

// MARK: - Create Container Request

struct DockerCreateContainerRequest: Codable {
    let Image: String
    let Env: [String]
    let HostConfig: HostConfig
    let Cmd: [String]?  // Command to run in container

    struct HostConfig: Codable {
        let NetworkMode: String
        let Binds: [String]
        var Privileged: Bool = true // This must be mutable to be included in encoding if it has a default value.
    }
    
    // Initialize with optional Cmd
    init(Image: String, Env: [String], HostConfig: HostConfig, Cmd: [String]? = nil) {
        self.Image = Image
        self.Env = Env
        self.HostConfig = HostConfig
        self.Cmd = Cmd
    }
}

extension DockerCreateContainerRequest: CustomStringConvertible {
    var description: String {
        let cmdString = Cmd?.joined(separator: " ") ?? "default"
        return "DockerCreateContainerRequest(Image: \(Image), Env: \(Env), HostConfig: NetworkMode=\(self.HostConfig.NetworkMode), Binds=\(self.HostConfig.Binds), Cmd: [\(cmdString)])"
    }
}

// MARK: - Create Container Response

struct DockerCreateContainerResponse: Codable {
    let Id: String
    let Warnings: [String]?
}

// MARK: - Container Inspect Response

struct DockerInspectContainerResponse: Codable {
    let State: ContainerState
    
    struct ContainerState: Codable {
        let Status: String // e.g., "running", "created", "exited"
        let Running: Bool
        let Paused: Bool
        let Restarting: Bool
        let OOMKilled: Bool
        let Dead: Bool
        let Pid: Int
        let ExitCode: Int
        let Error: String
        let StartedAt: String
        let FinishedAt: String
    }
} 