import Foundation

/// Codable structs for interacting with the Docker Engine API.

// MARK: - Create Container Request

struct DockerCreateContainerRequest: Codable {
    let Image: String
    let Env: [String]
    let HostConfig: HostConfig

    struct HostConfig: Codable {
        let NetworkMode: String
        let Binds: [String]
        var Privileged: Bool = true // This must be mutable to be included in encoding if it has a default value.
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