import CryptoKit
import Foundation
import Network
@testable import SocketIOMini

/// A raw TCP server on 127.0.0.1 with an OS-assigned port, for tests that need
/// a real socket: a hand-written WebSocket server that can misbehave, and HTTP
/// servers that redirect. It speaks just enough HTTP and WebSocket by hand.
final class LoopbackServer: @unchecked Sendable {
    typealias Handler = @Sendable (_ requestHead: String, _ connection: LoopbackConnection) async -> Void

    private let listener: NWListener
    private let queue = DispatchQueue(label: "loopback-server")
    private let handler: Handler
    private let lock = NSLock()
    private var heads: [String] = []
    private var connections: [LoopbackConnection] = []

    init(host: NWEndpoint.Host = "127.0.0.1", handler: @escaping Handler) throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: host, port: .any)
        listener = try NWListener(using: parameters)
        self.handler = handler
    }

    /// The request heads received so far.
    var requestHeads: [String] { lock.withLock { heads } }

    /// Starts listening and returns the port.
    func start() async throws -> UInt16 {
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            let wrapped = LoopbackConnection(connection: connection, queue: self.queue)
            self.lock.withLock { self.connections.append(wrapped) }
            connection.start(queue: self.queue)
            Task {
                guard let head = try? await wrapped.readHead() else {
                    wrapped.close()
                    return
                }
                self.lock.withLock { self.heads.append(head) }
                await self.handler(head, wrapped)
            }
        }
        final class Once: @unchecked Sendable { var done = false }
        let once = Once()
        let listener = self.listener
        return try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { state in
                guard !once.done else { return }
                switch state {
                case .ready:
                    once.done = true
                    continuation.resume(returning: listener.port?.rawValue ?? 0)
                case let .failed(error):
                    once.done = true
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
    }

    func stop() {
        listener.cancel()
        for connection in lock.withLock({ connections }) {
            connection.close()
        }
    }

    // MARK: WebSocket helpers

    /// The `101 Switching Protocols` answer to a WebSocket upgrade request.
    static func upgradeResponse(for head: String) -> Data {
        let key = head.split(separator: "\r\n")
            .first { $0.lowercased().hasPrefix("sec-websocket-key:") }
            .map { $0.split(separator: ":", maxSplits: 1)[1].trimmingCharacters(in: .whitespaces) } ?? ""
        let digest = Insecure.SHA1.hash(data: Data((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").utf8))
        let accept = Data(digest).base64EncodedString()
        return Data((
            "HTTP/1.1 101 Switching Protocols\r\n"
                + "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                + "Sec-WebSocket-Accept: \(accept)\r\n\r\n"
        ).utf8)
    }

    /// The header of an unmasked server-to-client text frame of `length` bytes.
    static func textFrameHeader(length: Int) -> Data {
        var header = Data([0x81])
        if length < 126 {
            header.append(UInt8(length))
        } else if length <= 0xFFFF {
            header.append(126)
            header.append(UInt8(length >> 8))
            header.append(UInt8(length & 0xFF))
        } else {
            header.append(127)
            for shift in stride(from: 56, through: 0, by: -8) {
                header.append(UInt8((UInt64(length) >> UInt64(shift)) & 0xFF))
            }
        }
        return header
    }

    static func textFrame(_ text: String) -> Data {
        let payload = Data(text.utf8)
        return textFrameHeader(length: payload.count) + payload
    }

    /// A plain HTTP response that closes the connection.
    static func httpResponse(status: String, headers: [String] = [], body: String = "") -> Data {
        var text = "HTTP/1.1 \(status)\r\n"
        for header in headers {
            text += header + "\r\n"
        }
        text += "Content-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n" + body
        return Data(text.utf8)
    }
}

final class LoopbackConnection: @unchecked Sendable {
    private let connection: NWConnection
    private let queue: DispatchQueue
    private let lock = NSLock()
    private var sent = 0

    init(connection: NWConnection, queue: DispatchQueue) {
        self.connection = connection
        self.queue = queue
    }

    /// Bytes the kernel has accepted from this side so far.
    var bytesSent: Int { lock.withLock { sent } }

    func readHead() async throws -> String {
        var data = Data()
        while !data.contains(Data("\r\n\r\n".utf8)) {
            let chunk: Data = try await withCheckedThrowingContinuation { continuation in
                connection.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { content, _, isComplete, error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else if let content, !content.isEmpty {
                        continuation.resume(returning: content)
                    } else if isComplete {
                        continuation.resume(throwing: NWError.posix(.ECONNRESET))
                    } else {
                        continuation.resume(returning: Data())
                    }
                }
            }
            data.append(chunk)
            if data.count > 64 * 1024 { throw NWError.posix(.EMSGSIZE) }
        }
        return String(decoding: data, as: UTF8.self)
    }

    func send(_ data: Data) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: data, completion: .contentProcessed { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            })
        }
        lock.withLock { sent += data.count }
    }

    /// Streams `total` bytes of `byte` in chunks; stops at the first send error.
    /// Returns true when every byte was accepted.
    func stream(total: Int, byte: UInt8 = UInt8(ascii: "a"), chunk: Int = 256 * 1024) async -> Bool {
        let block = Data(repeating: byte, count: chunk)
        var remaining = total
        while remaining > 0 {
            let size = min(chunk, remaining)
            do {
                try await send(size == chunk ? block : block.prefix(size))
            } catch {
                return false
            }
            remaining -= size
        }
        return true
    }

    func close() {
        connection.cancel()
    }
}

/// The process's physical memory footprint in bytes (what Xcode's memory gauge shows).
func physicalFootprint() -> Int {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    let result = withUnsafeMutablePointer(to: &info) { pointer in
        pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
        }
    }
    return result == KERN_SUCCESS ? Int(info.phys_footprint) : 0
}
