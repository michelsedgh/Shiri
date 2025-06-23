import Foundation
import NIO
import NIOHTTP1

/// A service class to handle low-level communication with the Docker daemon API.
/// This class connects to the Docker unix socket and sends raw HTTP requests.
class DockerAPI {
    
    private let socketPath = "/var/run/docker.sock"
    private let group: EventLoopGroup

    init() {
        self.group = MultiThreadedEventLoopGroup(numberOfThreads: 1)
    }

    deinit {
        try? group.syncShutdownGracefully()
    }

    // MARK: - Public API

    func createContainer(name: String, config: DockerCreateContainerRequest) -> EventLoopFuture<DockerCreateContainerResponse> {
        let path = "/containers/create?name=\(name)"
        return sendRequestWithDecodableResponse(method: .POST, path: path, body: config)
    }

    func startContainer(id: String) -> EventLoopFuture<Void> {
        let path = "/containers/\(id)/start"
        return sendRequestWithEmptyResponse(method: .POST, path: path)
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
        return sendRequestWithDecodableResponse(method: .GET, path: path, body: EmptyBody())
    }

    // MARK: - Generic Request Logic

    private func sendRequestWithDecodableResponse<Body: Encodable, Response: Decodable>(method: HTTPMethod, path: String, body: Body) -> EventLoopFuture<Response> {
        let promise = group.next().makePromise(of: Response.self)
        
        do {
            let requestBody: Data? = (body is EmptyBody) ? nil : try JSONEncoder().encode(body)
            
            let bootstrap = makeBootstrap(for: promise)

            bootstrap.connect(unixDomainSocketPath: socketPath).whenSuccess { channel in
                self.sendRequest(on: channel, method: method, path: path, bodyData: requestBody, promise: promise)
            }
        } catch {
            promise.fail(error)
        }
        
        return promise.futureResult
    }
    
    private func sendRequestWithEmptyResponse(method: HTTPMethod, path: String) -> EventLoopFuture<Void> {
        let promise = group.next().makePromise(of: Void.self)
        
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

private struct EmptyBody: Encodable {}

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
            
            // Handle String responses (for ping)
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
                    promise.fail(DockerAPIError.missingBody)
                }
                context.close(promise: nil)
                return
            }

            do {
                if let decodableType = T.self as? Decodable.Type {
                    let decoded = try JSONDecoder().decode(decodableType, from: responseBodyData) as! T
                    promise.succeed(decoded)
                } else {
                    // For non-decodable types like String
                    if T.self == String.self {
                        let string = String(data: responseBodyData, encoding: .utf8) ?? ""
                        promise.succeed(string as! T)
                    } else {
                        promise.fail(DockerAPIError.missingBody)
                    }
                }
            } catch {
                promise.fail(error)
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

    var errorDescription: String? {
        switch self {
        case .badResponse(let status):
            return "Received bad response from Docker: \(status)"
        case .missingBody:
            return "Expected a response body, but received none."
        }
    }
} 