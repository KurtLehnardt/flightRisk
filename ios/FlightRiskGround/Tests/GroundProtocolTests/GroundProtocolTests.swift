import XCTest
@testable import GroundProtocol

final class GroundProtocolTests: XCTestCase {
    func testPlaceholder() {
        let placeholder = GroundProtocolPlaceholder()
        XCTAssertNotNil(placeholder)
    }
}
