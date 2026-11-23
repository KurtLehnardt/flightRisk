import Foundation

/// Where a frame goes on the square detector canvas, and how to map detector pixels back to it.
///
/// YOLOX (the bundled detector, `docs/mobile-models.md`, "Detector: YOLOX-S") resizes the frame by
/// `r = min(640 / height, 640 / width)`, truncates each resized side toward zero, and places the
/// result at the **top-left** corner of a canvas filled with 114. A user's own YOLO model (an
/// Ultralytics export) expects the resized frame **centred**, each side rounded, as Ultralytics'
/// `LetterBox` does.
public struct LetterboxGeometry: Equatable, Sendable {
    public enum Placement: Sendable {
        /// YOLOX: sides truncated, image at the top-left corner.
        case topLeft
        /// Ultralytics: sides rounded, image centred (`round(pad / 2 - 0.1)` on the top and left).
        case centred
    }

    public let frameWidth: Int
    public let frameHeight: Int
    /// The canvas side in pixels (640).
    public let size: Int
    /// `min(size / height, size / width)`.
    public let ratio: Double
    public let resizedWidth: Int
    public let resizedHeight: Int
    public let padLeft: Int
    public let padTop: Int

    public init(frameWidth: Int, frameHeight: Int, size: Int = YOLOXContract.inputSize, placement: Placement = .topLeft) {
        precondition(frameWidth > 0 && frameHeight > 0, "the frame has no pixels")
        self.frameWidth = frameWidth
        self.frameHeight = frameHeight
        self.size = size
        let r = min(Double(size) / Double(frameHeight), Double(size) / Double(frameWidth))
        self.ratio = r
        switch placement {
        case .topLeft:
            resizedWidth = max(1, Int(Double(frameWidth) * r))
            resizedHeight = max(1, Int(Double(frameHeight) * r))
            padLeft = 0
            padTop = 0
        case .centred:
            let w = min(size, max(1, Int((Double(frameWidth) * r).rounded(.toNearestOrEven))))
            let h = min(size, max(1, Int((Double(frameHeight) * r).rounded(.toNearestOrEven))))
            resizedWidth = w
            resizedHeight = h
            padLeft = max(0, Int((Double(size - w) / 2 - 0.1).rounded(.toNearestOrEven)))
            padTop = max(0, Int((Double(size - h) / 2 - 0.1).rounded(.toNearestOrEven)))
        }
    }

    /// A detector x coordinate (canvas pixels) in frame pixels. YOLOX's contract divides by the
    /// ratio in float32, as `mobile_preprocess.yolox_postprocess` does.
    public func frameX(_ canvasX: Float) -> Float {
        (canvasX - Float(padLeft)) / Float(ratio)
    }

    /// A detector y coordinate (canvas pixels) in frame pixels.
    public func frameY(_ canvasY: Float) -> Float {
        (canvasY - Float(padTop)) / Float(ratio)
    }
}

/// The square detector canvas: the frame resized bilinearly and placed on a field of 114.
public enum Letterbox {
    /// The padding value in all three channels.
    public static let padValue: UInt8 = 114

    /// The `size` x `size` RGB canvas of `image` and its geometry.
    ///
    /// The resize is bilinear with OpenCV's `INTER_LINEAR` sampling positions (pixel centres,
    /// `src = (dst + 0.5) * scale - 0.5`, clamped at the edges, no antialiasing), which is what
    /// upstream YOLOX's `preproc` uses. OpenCV rounds in fixed point; this rounds in floating
    /// point, so a value can differ from OpenCV's by 1 (the package tests check that bound
    /// against the golden fixtures).
    public static func make(
        _ image: RGBImage,
        size: Int = YOLOXContract.inputSize,
        placement: LetterboxGeometry.Placement = .topLeft
    ) -> (canvas: RGBImage, geometry: LetterboxGeometry) {
        let g = LetterboxGeometry(frameWidth: image.width, frameHeight: image.height, size: size, placement: placement)
        var canvas = [UInt8](repeating: padValue, count: size * size * 3)
        let xs = samples(source: image.width, destination: g.resizedWidth)
        let ys = samples(source: image.height, destination: g.resizedHeight)
        let srcW = image.width
        image.pixels.withUnsafeBufferPointer { src in
            canvas.withUnsafeMutableBufferPointer { dst in
                for dy in 0..<g.resizedHeight {
                    let sy = ys[dy]
                    let row0 = sy.index * srcW * 3
                    let row1 = sy.next * srcW * 3
                    let wy1 = sy.weight
                    let wy0 = 1 - wy1
                    let out = ((dy + g.padTop) * size + g.padLeft) * 3
                    for dx in 0..<g.resizedWidth {
                        let sx = xs[dx]
                        let c0 = sx.index * 3
                        let c1 = sx.next * 3
                        let wx1 = sx.weight
                        let wx0 = 1 - wx1
                        for c in 0..<3 {
                            let top = wx0 * Float(src[row0 + c0 + c]) + wx1 * Float(src[row0 + c1 + c])
                            let bottom = wx0 * Float(src[row1 + c0 + c]) + wx1 * Float(src[row1 + c1 + c])
                            let v = (wy0 * top + wy1 * bottom).rounded(.toNearestOrAwayFromZero)
                            dst[out + dx * 3 + c] = UInt8(min(255, max(0, v)))
                        }
                    }
                }
            }
        }
        return (RGBImage(width: size, height: size, pixels: canvas), g)
    }

    struct Sample {
        let index: Int
        let next: Int
        let weight: Float
    }

    /// OpenCV's linear sampling positions for one axis.
    static func samples(source: Int, destination: Int) -> [Sample] {
        let scale = Double(source) / Double(destination)
        return (0..<destination).map { d in
            var f = Float((Double(d) + 0.5) * scale - 0.5)
            var s = Int(f.rounded(.down))
            f -= Float(s)
            if s < 0 {
                s = 0
                f = 0
            }
            if s >= source - 1 {
                s = source - 1
                f = 0
            }
            return Sample(index: s, next: min(s + 1, source - 1), weight: f)
        }
    }
}
