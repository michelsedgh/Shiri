import Foundation
import NIO
import NIOHTTP1

/// A service class to handle low-level communication with the Docker daemon API.
/// This class connects to the Docker unix socket and sends raw HTTP requests.
class DockerAPI {
    
    private let possibleSocketPaths = [
        "/var/run/docker.sock",  // Traditional Docker daemon
        "\(NSHomeDirectory())/.docker/run/docker.sock"  // Docker Desktop
    ]
    
    private let group: EventLoopGroup
    private var workingSocketPath: String?

    init() {
        self.group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
        findWorkingSocketPath()
    }
    
    private func findWorkingSocketPath() {
        for path in possibleSocketPaths {
            if FileManager.default.fileExists(atPath: path) {
                workingSocketPath = path
                print("Found Docker socket at: \(path)")
                break
            }
        }
        if workingSocketPath == nil {
            print("Warning: No Docker socket found at any expected path")
        }
    }

    deinit {
        try? group.syncShutdownGracefully()
    }

    // MARK: - Public API

    func createContainer(name: String, config: DockerCreateContainerRequest) -> EventLoopFuture<DockerCreateContainerResponse> {
        let path = "/containers/create?name=\(name)"
        let promise = group.next().makePromise(of: DockerCreateContainerResponse.self)
        
        guard let socketPath = workingSocketPath else {
            promise.fail(DockerAPIError.noSocketFound)
            return promise.futureResult
        }
        
        do {
            let requestBody = try JSONEncoder().encode(config)
            let bootstrap = makeBootstrap(for: promise)

            bootstrap.connect(unixDomainSocketPath: socketPath).whenSuccess { channel in
                self.sendRequest(on: channel, method: .POST, path: path, bodyData: requestBody, promise: promise)
            }
            
            bootstrap.connect(unixDomainSocketPath: socketPath).whenFailure { error in
                promise.fail(error)
            }
        } catch {
            promise.fail(error)
        }
        
        return promise.futureResult
    }

    func startContainer(id: String) -> EventLoopFuture<Void> {
        let path = "/containers/\(id)/start"
        let promise = group.next().makePromise(of: Void.self)
        
        guard let socketPath = workingSocketPath else {
            promise.fail(DockerAPIError.noSocketFound)
            return promise.futureResult
        }
        
        let bootstrap = makeBootstrap(for: promise)

        bootstrap.connect(unixDomainSocketPath: socketPath).whenSuccess { channel in
            self.sendRequest(on: channel, method: .POST, path: path, bodyData: nil, promise: promise)
        }
        
        bootstrap.connect(unixDomainSocketPath: socketPath).whenFailure { error in
            promise.fail(error)
        }
        
        return promise.futureResult
    }

    func stopContainer(id: String) -> EventLoopFuture<Void> {
        let path = "/containers/\(id)/stop"
        return sendRequestWithEmptyResponse(method: .POST, path: path)
    }

    func removeContainer(id: String) -> EventLoopFuture<Void> {
        let path = "/containers/\(id)"
        return sendRequestWithEmptyResponse(method: .DELETE, path: path)
    }
    
    func ping() -> EventLoopFuture<String> {
        let path = "/_ping"
        let promise = group.next().makePromise(of: String.self)
        
        guard let socketPath = workingSocketPath else {
            promise.fail(DockerAPIError.noSocketFound)
            return promise.futureResult
        }
        
        let bootstrap = makeBootstrap(for: promise)
        
        bootstrap.connect(unixDomainSocketPath: socketPath).whenSuccess { channel in
            self.sendRequest(on: channel, method: .GET, path: path, bodyData: nil, promise: promise)
        }
        
        bootstrap.connect(unixDomainSocketPath: socketPath).whenFailure { error in
            promise.fail(error)
        }
        
        return promise.futureResult
    }

    // MARK: - Generic Request Logic

    private func sendRequestWithDecodableResponse<Body: Encodable, Response: Decodable>(method: HTTPMethod, path: String, body: Body) -> EventLoopFuture<Response> {
        let promise = group.next().makePromise(of: Response.self)
        
        guard let socketPath = workingSocketPath else {
            promise.fail(DockerAPIError.noSocketFound)
            return promise.futureResult
        }
        
        do {
            let requestBody: Data? = (body is EmptyBody) ? nil : try JSONEncoder().encode(body)
            
            let bootstrap = makeBootstrap(for: promise)

            bootstrap.connect(unixDomainSocketPath: socketPath).whenSuccess { channel in
                self.sendRequest(on: channel, method: method, path: path, bodyData: requestBody, promise: promise)
            }
            
            bootstrap.connect(unixDomainSocketPath: socketPath).whenFailure { error in
                promise.fail(error)
            }
        } catch {
            promise.fail(error)
        }
        
        return promise.futureResult
    }
    
    private func sendRequestWithEmptyResponse(method: HTTPMethod, path: String) -> EventLoopFuture<Void> {
        let promise = group.next().makePromise(of: Void.self)
        
        guard let socketPath = workingSocketPath else {
            promise.fail(DockerAPIError.noSocketFound)
            return promise.futureResult
        }
        
        let bootstrap = makeBootstrap(for: promise)

        bootstrap.connect(unixDomainSocketPath: socketPath).whenSuccess { channel in
            self.sendRequest(on: channel, method: method, path: path, bodyData: nil, promise: promise)
        }
        
        return promise.futureResult
    }
    
    // MARK: - Helpers
    
    private func makeBootstrap<R>(for promise: EventLoopPromise<R>) -> ClientBootstrap {
        return ClientBootstrap(group: group)
            .channelInitializer { channel in
                channel.pipeline.addHTTPClientHandlers().flatMap {
                    channel.pipeline.addHandler(HTTPResponseHandler<R>(promise: promise))
                }
            }
    }
    
    private func sendRequest<R>(on channel: Channel, method: HTTPMethod, path: String, bodyData: Data?, promise: EventLoopPromise<R>) {
        var request = HTTPRequestHead(version: .http1_1, method: method, uri: path)
        request.headers.add(name: "Host", value: "localhost")
        if bodyData != nil {
            request.headers.add(name: "Content-Type", value: "application/json")
        }
        
        channel.write(HTTPClientRequestPart.head(request), promise: nil)
        if let body = bodyData {
            var buffer = channel.allocator.buffer(capacity: body.count)
            buffer.writeBytes(body)
            channel.write(HTTPClientRequestPart.body(.byteBuffer(buffer)), promise: nil)
        }
        channel.writeAndFlush(HTTPClientRequestPart.end(nil)).whenFailure { error in
            promise.fail(error)
        }
    }
}

// MARK: - Private Helpers & Handlers

private struct EmptyBody: Encodable, Sendable {}

private class HTTPResponseHandler<T>: ChannelInboundHandler {
    typealias InboundIn = HTTPClientResponsePart
    private let promise: EventLoopPromise<T>
    private var responseBodyData = Data()
    private var isCompleted = false

    init(promise: EventLoopPromise<T>) {
        self.promise = promise
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        guard !isCompleted else { return }
        
        let responsePart = self.unwrapInboundIn(data)
        
        switch responsePart {
        case .head(let httpResponseHead):
            guard httpResponseHead.status.code >= 200 && httpResponseHead.status.code < 300 else {
                isCompleted = true
                promise.fail(DockerAPIError.badResponse(httpResponseHead.status))
                context.close(promise: nil)
                return
            }
            if T.self == Void.self {
                // For empty responses, we can succeed as soon as we get a good header.
                isCompleted = true
                promise.succeed(() as! T)
                context.close(promise: nil)
                return
            }
        case .body(let byteBuffer):
            let data = Data(byteBuffer.readableBytesView)
            responseBodyData.append(data)
        case .end:
            guard !isCompleted else {
                context.close(promise: nil)
                return
            }
            isCompleted = true
            
            // Handle String responses (for ping) - try this first
            if T.self == String.self {
                let result = String(data: responseBodyData, encoding: .utf8) ?? ""
                promise.succeed(result as! T)
                context.close(promise: nil)
                return
            }
            
            // Handle empty responses
            if responseBodyData.isEmpty {
                if T.self == Void.self {
                    // This shouldn't happen as Void responses are handled in .head
                    promise.succeed(() as! T)
                } else {
                    // For ping, if we get no body but good status, return "OK"
                    if T.self == String.self {
                        promise.succeed("OK" as! T)
                    } else {
                        promise.fail(DockerAPIError.missingBody)
                    }
                }
                context.close(promise: nil)
                return
            }

            do {
                if let decodableType = T.self as? Decodable.Type {
                    let decoded = try JSONDecoder().decode(decodableType, from: responseBodyData) as! T
                    promise.succeed(decoded)
                } else {
                    // For non-decodable types like String (fallback)
                    if T.self == String.self {
                        let string = String(data: responseBodyData, encoding: .utf8) ?? ""
                        promise.succeed(string as! T)
                    } else {
                        promise.fail(DockerAPIError.missingBody)
                    }
                }
            } catch {
                // If JSON decoding fails but we're expecting a String, try raw string
                if T.self == String.self {
                    let string = String(data: responseBodyData, encoding: .utf8) ?? ""
                    promise.succeed(string as! T)
                } else {
                    promise.fail(error)
                }
            }
            context.close(promise: nil)
        }
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        guard !isCompleted else {
            context.close(promise: nil)
            return
        }
        isCompleted = true
        promise.fail(error)
        context.close(promise: nil)
    }
}

enum DockerAPIError: Error, LocalizedError {
    case badResponse(HTTPResponseStatus)
    case missingBody
    case noSocketFound
    case timeout
    
    var errorDescription: String? {
        switch self {
        case .badResponse(let status):
            return "Docker API returned bad response: \(status)"
        case .missingBody:
            return "Docker API response missing body"
        case .noSocketFound:
            return "No Docker socket found. Make sure Docker is running."
        case .timeout:
            return "Docker API request timed out"
        }
    }
} 