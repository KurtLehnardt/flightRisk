#if canImport(CoreGraphics)
import CoreGraphics

/// The demo's stand-in for a reference photo: a flat drawing of a figure (a round head and a
/// body) in one colour on a plain background. It is generated in code, is not a photograph and
/// has no face, so the demo needs no picture of a real person and the repository holds none.
public enum DemoPlaceholder {
    public static let defaultSize = 240

    /// The background and the figure, as RGBA bytes. The image contains exactly these two values.
    public static let backgroundRGBA: [UInt8] = [214, 226, 240, 255]
    public static let figureRGBA: [UInt8] = [51, 84, 140, 255]

    /// A square drawing `size` pixels wide, or nil for a size under 16. Antialiasing is off, so
    /// every pixel is one of the two flat colours.
    public static func makeImage(size: Int = defaultSize) -> CGImage? {
        guard size >= 16, size <= 4096 else { return nil }
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(
                  data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: size * 4,
                  space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              )
        else { return nil }
        context.setShouldAntialias(false)
        context.setAllowsAntialiasing(false)
        let s = Double(size)
        context.setFillColor(color(backgroundRGBA, in: space))
        context.fill(CGRect(x: 0, y: 0, width: s, height: s))
        context.setFillColor(color(figureRGBA, in: space))
        // Core Graphics puts the origin at the bottom left: the head is above the body.
        context.fillEllipse(in: CGRect(x: s * 0.37, y: s * 0.60, width: s * 0.26, height: s * 0.26))
        let body = CGPath(
            roundedRect: CGRect(x: s * 0.27, y: s * 0.10, width: s * 0.46, height: s * 0.44),
            cornerWidth: s * 0.12, cornerHeight: s * 0.12, transform: nil
        )
        context.addPath(body)
        context.fillPath()
        return context.makeImage()
    }

    private static func color(_ rgba: [UInt8], in space: CGColorSpace) -> CGColor {
        let c = rgba.map { CGFloat($0) / 255 }
        return CGColor(colorSpace: space, components: c) ?? CGColor(gray: 0.5, alpha: 1)
    }
}
#endif
