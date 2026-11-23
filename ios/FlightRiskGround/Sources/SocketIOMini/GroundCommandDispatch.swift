import Foundation
import GroundProtocol

/// Where the phone's commands go: the demo ground on the phone, or the live ground station.
///
/// The Ground tab sends every command through `GroundCommandDispatch.send`, with the destination
/// that `choose` returns. While a demo session exists, the destination is the demo, even if a
/// live client also exists: a command in demo mode is answered on the phone and never reaches a
/// client, a socket or any network.
public enum GroundCommandDestination: Sendable {
    /// The demo ground. `elapsed` is the demo clock, in seconds.
    case demo(DemoGroundSession, elapsed: TimeInterval)
    /// The live ground station.
    case live(GroundClient)
    /// No link at all.
    case none

    /// The demo when there is one, else the live client, else none.
    public static func choose(demo: DemoGroundSession?, elapsed: TimeInterval, live: GroundClient?) -> GroundCommandDestination {
        if let demo { return .demo(demo, elapsed: elapsed) }
        if let live { return .live(live) }
        return .none
    }

    public var isDemo: Bool {
        if case .demo = self { return true }
        return false
    }
}

/// What happened to one command.
public enum GroundDispatchOutcome: Equatable, Sendable {
    /// The demo ground took it; its answer comes from the demo, on the phone.
    case answeredByDemo
    /// Handed to the live client's WebSocket (not proof of delivery).
    case sent
    /// The live client did not send it; nothing is queued.
    case notSent(GroundNotSentReason)
    /// There is no link.
    case noDestination
    /// The command cannot be sent (an empty drone id).
    case notSendable
    /// The live client refused it (not on the allow-list, or not encodable).
    case refusedByClient(String)
}

public enum GroundCommandDispatch {
    /// Sends one command to `destination`. Only a `GroundCommand` can be sent, only through its
    /// `wireMessage`, and an unsendable command goes nowhere, demo included.
    public static func send(_ command: GroundCommand, to destination: GroundCommandDestination) async -> GroundDispatchOutcome {
        guard let wire = command.wireMessage else { return .notSendable }
        switch destination {
        case let .demo(session, elapsed):
            session.receive(command, at: elapsed)
            return .answeredByDemo
        case let .live(client):
            do {
                switch try await client.emit(wire.eventName, payload: wire.payloadJSON) {
                case .sent: return .sent
                case let .notSent(reason): return .notSent(reason)
                }
            } catch {
                return .refusedByClient(String(describing: error))
            }
        case .none:
            return .noDestination
        }
    }
}
