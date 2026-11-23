import CoreGraphics
import UIKit
import XCTest
@testable import FlightRisk

final class PhotoColourSpaceTests: XCTestCase {
    private let w = 8, h = 6

    /// A 16-bit half-float image filled with pure red in the named colour space. This is the
    /// format `UIGraphicsImageRenderer` produces for an extended-range photo; iOS has no 16-bit
    /// integer RGBA context with little-endian byte order.
    private func sixteenBitRed(_ name: CFString) throws -> CGImage {
        let space = try XCTUnwrap(CGColorSpace(name: name))
        let info = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.floatComponents.rawValue
            | CGBitmapInfo.byteOrder16Little.rawValue
        let ctx = try XCTUnwrap(CGContext(
            data: nil, width: w, height: h, bitsPerComponent: 16, bytesPerRow: 0, space: space, bitmapInfo: info))
        ctx.setFillColor(CGColor(colorSpace: space, components: [1, 0, 0, 1])!)
        ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        return try XCTUnwrap(ctx.makeImage())
    }

    private func images() throws -> [(String, CGImage)] {
        [("displayP3", try sixteenBitRed(CGColorSpace.displayP3)),
         ("extendedSRGB16", try sixteenBitRed(CGColorSpace.extendedSRGB))]
    }

    func testRGBConvertsWideAndSixteenBitImages() throws {
        for (name, image) in try images() {
            let out = try XCTUnwrap(ModelIO.rgb(image), name)
            XCTAssertEqual(out.width, w, name)
            XCTAssertEqual(out.height, h, name)
            XCTAssertEqual(out.pixels.count, w * h * 3, name)
            XCTAssertGreaterThan(Int(out.pixels[0]), 200, name)
            XCTAssertLessThan(Int(out.pixels[1]), 60, name)
            XCTAssertLessThan(Int(out.pixels[2]), 60, name)
        }
    }

    func testNormalizedCGImageIsEightBitSRGBAtPixelSize() throws {
        for (name, image) in try images() {
            let ui = UIImage(cgImage: image, scale: 1, orientation: .up)
            let out = try XCTUnwrap(ui.normalizedCGImage, name)
            XCTAssertEqual(out.width, w, name)
            XCTAssertEqual(out.height, h, name)
            XCTAssertEqual(out.bitsPerComponent, 8, name)
            let space = try XCTUnwrap(out.colorSpace, name)
            XCTAssertEqual(space.model, .rgb, name)
            XCTAssertFalse(CGColorSpaceUsesExtendedRange(space), name)
            let rgb = try XCTUnwrap(ModelIO.rgb(out), name)
            XCTAssertGreaterThan(Int(rgb.pixels[0]), 200, name)
        }
    }
}
