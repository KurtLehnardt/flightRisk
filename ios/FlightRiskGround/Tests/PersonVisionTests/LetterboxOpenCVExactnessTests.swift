import Foundation
import XCTest
@testable import PersonVision

/// The detector letterbox equals OpenCV 5.1 `INTER_LINEAR` exactly at integer frame-to-canvas
/// ratios, which include the app's 1920 x 1080 frames. At non-integer ratios it can differ by 1.
///
/// Fixture `Fixtures/opencv-letterbox.json` holds the SHA-256 of each 640 x 640 canvas. Generator
/// (Python with OpenCV 5.1 and numpy):
///
///     import cv2, numpy as np, hashlib, json
///     cases = []
///     for w, h in [(1920, 1080), (1280, 720), (3840, 2160), (1080, 1920)]:
///         x = np.arange(w)[None, :, None]; y = np.arange(h)[:, None, None]; c = np.arange(3)[None, None, :]
///         img = (((x * 31) ^ (y * 17) ^ (c * 101) ^ ((x * y) >> 3)) & 255).astype(np.uint8)
///         r = min(640 / h, 640 / w); rw = max(1, int(w * r)); rh = max(1, int(h * r))
///         out = cv2.resize(img, (rw, rh), interpolation=cv2.INTER_LINEAR)
///         canvas = np.full((640, 640, 3), 114, np.uint8); canvas[:rh, :rw] = out
///         cases.append(dict(width=w, height=h, canvas_sha256=hashlib.sha256(canvas.tobytes()).hexdigest()))
///     json.dump(dict(opencv=cv2.__version__, cases=cases), open("opencv-letterbox.json", "w"), indent=1)
final class LetterboxOpenCVExactnessTests: XCTestCase {
    struct Fixture: Decodable {
        struct Case: Decodable {
            let width: Int
            let height: Int
            let canvas_sha256: String
        }
        let opencv: String
        let cases: [Case]
    }

    func testLetterboxEqualsOpenCVAtIntegerRatios() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "opencv-letterbox", withExtension: "json", subdirectory: "Fixtures"))
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
        XCTAssertTrue(fixture.opencv.hasPrefix("5."))
        XCTAssertEqual(fixture.cases.count, 4)
        for c in fixture.cases {
            let image = ResampleExactnessTests.pattern(width: c.width, height: c.height)
            let canvas = Letterbox.make(image).canvas
            XCTAssertEqual(canvas.width, 640)
            XCTAssertEqual(canvas.height, 640)
            XCTAssertEqual(FileDigest.sha256(of: Data(canvas.pixels)), c.canvas_sha256, "\(c.width) x \(c.height)")
        }
    }
}
