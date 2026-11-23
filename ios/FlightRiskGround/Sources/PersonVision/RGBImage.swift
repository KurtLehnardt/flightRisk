/// An 8-bit RGB image: rows top to bottom, three bytes per pixel (red, green, blue), no padding.
///
/// The person detector and the appearance embedder work on this type, so their preprocessing is
/// plain Swift that the package tests can check against the golden fixtures without Core ML,
/// Core Graphics or a device.
public struct RGBImage: Equatable, Sendable {
    public let width: Int
    public let height: Int
    /// `width * height * 3` bytes.
    public var pixels: [UInt8]

    /// - Precondition: `pixels.count == width * height * 3` and both sides are positive.
    public init(width: Int, height: Int, pixels: [UInt8]) {
        precondition(width > 0 && height > 0, "an image needs pixels")
        precondition(pixels.count == width * height * 3, "expected width * height * 3 bytes")
        self.width = width
        self.height = height
        self.pixels = pixels
    }

    /// An image of one colour.
    public init(width: Int, height: Int, red: UInt8, green: UInt8, blue: UInt8) {
        precondition(width > 0 && height > 0, "an image needs pixels")
        var pixels = [UInt8](repeating: 0, count: width * height * 3)
        var i = 0
        while i < pixels.count {
            pixels[i] = red
            pixels[i + 1] = green
            pixels[i + 2] = blue
            i += 3
        }
        self.init(width: width, height: height, pixels: pixels)
    }

    /// The value of channel `c` (0 red, 1 green, 2 blue) at column `x`, row `y`.
    public subscript(x x: Int, y y: Int, c c: Int) -> UInt8 {
        pixels[(y * width + x) * 3 + c]
    }

    /// Columns `x1..<x2` of rows `y1..<y2`, copied, or `nil` when that is empty or outside the image.
    public func cropped(x1: Int, y1: Int, x2: Int, y2: Int) -> RGBImage? {
        guard x1 >= 0, y1 >= 0, x2 <= width, y2 <= height, x2 > x1, y2 > y1 else { return nil }
        let w = x2 - x1
        var out = [UInt8](repeating: 0, count: w * (y2 - y1) * 3)
        pixels.withUnsafeBufferPointer { src in
            out.withUnsafeMutableBufferPointer { dst in
                for row in 0..<(y2 - y1) {
                    let from = ((y1 + row) * width + x1) * 3
                    let to = row * w * 3
                    for k in 0..<(w * 3) {
                        dst[to + k] = src[from + k]
                    }
                }
            }
        }
        return RGBImage(width: w, height: y2 - y1, pixels: out)
    }
}
