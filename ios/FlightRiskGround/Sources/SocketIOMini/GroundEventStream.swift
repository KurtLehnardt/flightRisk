import Foundation

/// A single-consumer async sequence with a bound on the number of buffered
/// elements and on their total payload bytes.
///
/// `GroundClient` uses two of them:
/// - `events` drops *incoming* elements when full and then delivers one
///   `.eventsDropped(count:)` marker at the gap, so a stalled consumer learns
///   that it missed something and must re-request the state.
/// - `frames` drops the *oldest* buffered elements when full, so a stalled
///   consumer resumes with the newest video frames.
///
/// Iterate each stream from one task only.
public struct GroundEventStream<Element: Sendable>: AsyncSequence, Sendable {
    let buffer: GroundEventBuffer<Element>

    public func makeAsyncIterator() -> Iterator {
        Iterator(buffer: buffer)
    }

    /// How many elements the bound has dropped so far.
    public var droppedCount: Int { buffer.droppedTotal }

    public struct Iterator: AsyncIteratorProtocol {
        let buffer: GroundEventBuffer<Element>

        public mutating func next() async -> Element? {
            await buffer.next()
        }
    }
}

final class GroundEventBuffer<Element: Sendable>: @unchecked Sendable {
    enum Overflow {
        /// Drop the incoming element and later deliver `marker(count)` at the gap.
        case dropIncoming(marker: @Sendable (Int) -> Element)
        /// Drop the oldest buffered elements to make room.
        case dropOldest
    }

    let maxCount: Int
    let maxBytes: Int
    private let overflow: Overflow
    private let lock = NSLock()
    private var items: [(element: Element, bytes: Int)] = []
    private var head = 0
    private var bufferedBytes = 0
    private var waiter: CheckedContinuation<Element?, Never>?
    private var finished = false
    private var pendingDrops = 0
    private var dropped = 0

    init(maxCount: Int, maxBytes: Int, overflow: Overflow) {
        self.maxCount = max(2, maxCount)
        self.maxBytes = max(0, maxBytes)
        self.overflow = overflow
    }

    var stream: GroundEventStream<Element> { GroundEventStream(buffer: self) }

    var droppedTotal: Int { lock.withLock { dropped } }

    var bufferedCount: Int { lock.withLock { items.count - head } }

    /// Adds an element of `bytes` payload bytes. Returns false when the bound dropped it.
    @discardableResult
    func push(_ element: Element, bytes: Int) -> Bool {
        lock.lock()
        if finished {
            lock.unlock()
            return false
        }
        if let waiter {
            // A waiting consumer means the buffer is empty and no marker is pending.
            self.waiter = nil
            lock.unlock()
            waiter.resume(returning: element)
            return true
        }
        let count = items.count - head
        switch overflow {
        case .dropOldest:
            if bytes > maxBytes {
                dropped += 1
                lock.unlock()
                return false
            }
            var remaining = count
            while remaining > 0, remaining >= maxCount || bufferedBytes + bytes > maxBytes {
                bufferedBytes -= items[head].bytes
                head += 1
                remaining -= 1
                dropped += 1
            }
        case let .dropIncoming(marker):
            let needed = pendingDrops > 0 ? 2 : 1
            if count + needed > maxCount || bufferedBytes + bytes > maxBytes {
                pendingDrops += 1
                dropped += 1
                lock.unlock()
                return false
            }
            if pendingDrops > 0 {
                items.append((marker(pendingDrops), 0))
                pendingDrops = 0
            }
        }
        items.append((element, bytes))
        bufferedBytes += bytes
        compact()
        lock.unlock()
        return true
    }

    func next() async -> Element? {
        await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Element?, Never>) in
                lock.lock()
                if head < items.count {
                    let first = items[head]
                    head += 1
                    bufferedBytes -= first.bytes
                    compact()
                    lock.unlock()
                    continuation.resume(returning: first.element)
                    return
                }
                if pendingDrops > 0, case let .dropIncoming(marker) = overflow {
                    let count = pendingDrops
                    pendingDrops = 0
                    lock.unlock()
                    continuation.resume(returning: marker(count))
                    return
                }
                if finished || Task.isCancelled {
                    lock.unlock()
                    continuation.resume(returning: nil)
                    return
                }
                waiter = continuation
                lock.unlock()
            }
        } onCancel: {
            lock.lock()
            let pending = waiter
            waiter = nil
            lock.unlock()
            pending?.resume(returning: nil)
        }
    }

    /// Ends the sequence once the buffered elements are consumed.
    func finish() {
        lock.lock()
        finished = true
        let pending = waiter
        waiter = nil
        lock.unlock()
        pending?.resume(returning: nil)
    }

    private func compact() {
        if head > 64, head * 2 > items.count {
            items.removeFirst(head)
            head = 0
        }
    }
}
