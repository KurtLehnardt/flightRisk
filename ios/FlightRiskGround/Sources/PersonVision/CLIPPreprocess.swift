import Foundation

/// The appearance embedder's contract (`docs/mobile-models.md`, "Appearance embedder: open_clip
/// ViT-B-16 `laion2b_s34b_b88k`").
public enum CLIPContract {
    /// The installed Core ML package's name, without extension.
    public static let modelName = "CLIPViTB16LAION"
    /// The ground's model id (`ReidEmbedding.MODEL_ID`, `flightrisk/vision/reid_spec.py`).
    public static let modelID = "open_clip:ViT-B-16:laion2b_s34b_b88k"
    /// The Core ML input: `[1, 3, 224, 224]` float32, RGB.
    public static let inputName = "input"
    /// The Core ML output: `[1, 512]` float32, L2-normalised.
    public static let outputName = "embedding"
    public static let inputSize = 224
    public static let embeddingCount = 512
    public static let mean: [Float] = [0.48145466, 0.4578275, 0.40821073]
    public static let std: [Float] = [0.26862954, 0.26130258, 0.27577711]
}

/// The embedder's preprocessing, exactly as open_clip's evaluation transform does it on the
/// ground (torchvision on a Pillow image): shortest side to 224 with Pillow's bicubic filter,
/// the centre 224 x 224, then `(v / 255 - mean) / std` per channel.
///
/// The bicubic resize is a port of Pillow's own (`Resample.c`, 8 bits per channel, fixed point),
/// so the tensor is the ground's tensor bit for bit; the package tests check its SHA-256 against
/// the golden fixture.
public enum CLIPPreprocess {
    /// `(height, width)` after the shortest side becomes `size` and the long side
    /// `int(size * long / short)`, as torchvision's `Resize(size)`.
    public static func resizedSize(width: Int, height: Int, size: Int = CLIPContract.inputSize) -> (width: Int, height: Int) {
        precondition(width > 0 && height > 0, "the image has no pixels")
        if width <= height {
            return (size, Int(Double(size * height) / Double(width)))
        }
        return (Int(Double(size * width) / Double(height)), size)
    }

    /// `(top, left)` of the centre crop: `round((side - size) / 2)` with halves to the even
    /// neighbour, as torchvision's `center_crop` (Python's `round`).
    public static func cropOffsets(width: Int, height: Int, size: Int = CLIPContract.inputSize) -> (top: Int, left: Int) {
        (Int((Double(height - size) / 2).rounded(.toNearestOrEven)),
         Int((Double(width - size) / 2).rounded(.toNearestOrEven)))
    }

    /// The `size` x `size` RGB image the embedder sees. Only the centre window of the resize is
    /// computed (each output pixel depends only on its own weights, so the window is exactly the
    /// centre of the full resize).
    public static func resizeAndCrop(_ image: RGBImage, size: Int = CLIPContract.inputSize) -> RGBImage {
        let target = resizedSize(width: image.width, height: image.height, size: size)
        let off = cropOffsets(width: target.width, height: target.height, size: size)
        let columns = off.left..<(off.left + size)
        let rows = off.top..<(off.top + size)
        return PillowBicubic.resize(image, width: target.width, height: target.height, columns: columns, rows: rows)
    }

    /// The `[3, size, size]` float tensor (channel-major, RGB) of a `size` x `size` crop:
    /// `(v / 255 - mean) / std`, in float32 as torchvision's `ToTensor` and `Normalize`.
    public static func tensor(_ crop: RGBImage) -> [Float] {
        let plane = crop.width * crop.height
        var out = [Float](repeating: 0, count: plane * 3)
        let mean = CLIPContract.mean, std = CLIPContract.std
        crop.pixels.withUnsafeBufferPointer { px in
            out.withUnsafeMutableBufferPointer { dst in
                for i in 0..<plane {
                    for c in 0..<3 {
                        let v = Float(px[i * 3 + c]) / Float(255)
                        dst[c * plane + i] = (v - mean[c]) / std[c]
                    }
                }
            }
        }
        return out
    }

    /// `tensor(resizeAndCrop(image))`: the embedder input for a person crop.
    public static func input(_ image: RGBImage) -> [Float] {
        tensor(resizeAndCrop(image))
    }
}

/// Pillow's bicubic resampling (`Image.resize(..., Image.BICUBIC)`), 8 bits per channel.
///
/// A port of `src/libImaging/Resample.c` as Pillow 13 (the version
/// `scripts/mobile_models/requirements.lock` pins) has it: separable, each pass with the bicubic
/// kernel (`a = -0.5`) widened by the scale when shrinking (antialiasing), coefficients in fixed
/// point with 22 fractional bits, each pass rounded and clipped to 0..255. The rows are resized
/// first (horizontal pass), unless the height shrinks by more than twice as many pixels as the
/// width, when the columns go first; the order changes the rounding between the passes. Android's
/// `ImageResize.bicubicPillow` uses the same rule.
public enum PillowBicubic {
    static let precisionBits = 32 - 8 - 2
    static let support = 2.0

    static func kernel(_ value: Double) -> Double {
        let a = -0.5
        let x = abs(value)
        if x < 1 {
            return ((a + 2) * x - (a + 3)) * x * x + 1
        }
        if x < 2 {
            return (((x - 5) * x + 8) * x - 4) * a
        }
        return 0
    }

    struct Coefficients {
        let ksize: Int
        /// Per output position: the first input index and how many inputs it reads.
        let bounds: [(start: Int, count: Int)]
        /// `outSize * ksize` fixed-point weights.
        let weights: [Int]
    }

    static func coefficients(inSize: Int, outSize: Int) -> Coefficients {
        let scale = Double(inSize) / Double(outSize)
        let filterScale = max(scale, 1)
        let support = Self.support * filterScale
        let ksize = Int(support.rounded(.up)) * 2 + 1
        var bounds: [(start: Int, count: Int)] = []
        bounds.reserveCapacity(outSize)
        var weights = [Int](repeating: 0, count: outSize * ksize)
        let one = Double(1 << precisionBits)
        for xx in 0..<outSize {
            let center = (Double(xx) + 0.5) * scale
            let ss = 1 / filterScale
            // C's (int) cast truncates toward zero.
            var xmin = Int(center - support + 0.5)
            if xmin < 0 { xmin = 0 }
            var xmax = Int(center + support + 0.5)
            if xmax > inSize { xmax = inSize }
            xmax -= xmin
            var k = [Double](repeating: 0, count: ksize)
            var ww = 0.0
            for x in 0..<max(0, xmax) {
                let w = kernel((Double(x + xmin) - center + 0.5) * ss)
                k[x] = w
                ww += w
            }
            if ww != 0 {
                for x in 0..<max(0, xmax) { k[x] /= ww }
            }
            for x in 0..<ksize {
                let v = k[x]
                weights[xx * ksize + x] = v < 0 ? Int(-0.5 + v * one) : Int(0.5 + v * one)
            }
            bounds.append((xmin, max(0, xmax)))
        }
        return Coefficients(ksize: ksize, bounds: bounds, weights: weights)
    }

    @inline(__always)
    static func clip8(_ value: Int) -> UInt8 {
        let v = value >> precisionBits
        return UInt8(clamping: min(255, max(0, v)))
    }

    /// `image` resized to `width` x `height`.
    public static func resize(_ image: RGBImage, width: Int, height: Int) -> RGBImage {
        resize(image, width: width, height: height, columns: 0..<width, rows: 0..<height)
    }

    /// Whether Pillow 13 runs the vertical pass first for this resize.
    public static func verticalFirst(fromWidth: Int, fromHeight: Int, toWidth: Int, toHeight: Int) -> Bool {
        (fromHeight - toHeight) > 0 && (fromHeight - toHeight) > (fromWidth - toWidth) * 2
    }

    /// Columns `columns` and rows `rows` of `image` resized to `width` x `height`, without
    /// computing the rest: exactly that window of the full resize.
    public static func resize(_ image: RGBImage, width: Int, height: Int, columns: Range<Int>, rows: Range<Int>) -> RGBImage {
        precondition(width > 0 && height > 0, "the output has no pixels")
        precondition(columns.lowerBound >= 0 && columns.upperBound <= width && !columns.isEmpty, "columns outside the output")
        precondition(rows.lowerBound >= 0 && rows.upperBound <= height && !rows.isEmpty, "rows outside the output")
        let needH = width != image.width
        let needV = height != image.height
        if !needH && !needV {
            return image.cropped(x1: columns.lowerBound, y1: rows.lowerBound, x2: columns.upperBound, y2: rows.upperBound)!
        }
        let hc = coefficients(inSize: image.width, outSize: width)
        let vc = coefficients(inSize: image.height, outSize: height)
        if verticalFirst(fromWidth: image.width, fromHeight: image.height, toWidth: width, toHeight: height) {
            // Vertical pass on the source columns the horizontal pass will read, then horizontal.
            let src = needH ? sourceRange(hc, columns) : columns
            let strip = image.cropped(x1: src.lowerBound, y1: 0, x2: src.upperBound, y2: image.height)!
            let tmp = needV
                ? vertical(strip, rows: rows, co: vc, sourceOffset: 0)
                : strip.cropped(x1: 0, y1: rows.lowerBound, x2: strip.width, y2: rows.upperBound)!
            return needH ? horizontal(tmp, rows: 0..<tmp.height, columns: columns, co: hc, sourceOffset: src.lowerBound) : tmp
        }
        // Horizontal pass on the source rows the vertical pass will read, then vertical.
        let src = needV ? sourceRange(vc, rows) : rows
        let tmp = needH
            ? horizontal(image, rows: src, columns: columns, co: hc, sourceOffset: 0)
            : image.cropped(x1: columns.lowerBound, y1: src.lowerBound, x2: columns.upperBound, y2: src.upperBound)!
        return needV ? vertical(tmp, rows: rows, co: vc, sourceOffset: src.lowerBound) : tmp
    }

    /// The input indices the outputs `outputs` read.
    static func sourceRange(_ co: Coefficients, _ outputs: Range<Int>) -> Range<Int> {
        var lo = Int.max, hi = 0
        for o in outputs {
            let (start, count) = co.bounds[o]
            lo = min(lo, start)
            hi = max(hi, start + count)
        }
        return lo..<max(hi, lo + 1)
    }

    /// The horizontal pass for output `columns` of `src`'s rows `rows`. `sourceOffset` is the
    /// input column `src`'s first column stands for.
    static func horizontal(_ src: RGBImage, rows: Range<Int>, columns: Range<Int>, co: Coefficients, sourceOffset: Int) -> RGBImage {
        let outWidth = columns.count
        var out = [UInt8](repeating: 0, count: outWidth * rows.count * 3)
        let half = 1 << (precisionBits - 1)
        src.pixels.withUnsafeBufferPointer { s in
            out.withUnsafeMutableBufferPointer { dst in
                co.weights.withUnsafeBufferPointer { kk in
                    for (r, y) in rows.enumerated() {
                        let rowIn = y * src.width * 3
                        let rowOut = r * outWidth * 3
                        for (c, xx) in columns.enumerated() {
                            let (start, count) = co.bounds[xx]
                            let k = xx * co.ksize
                            var s0 = half, s1 = half, s2 = half
                            for x in 0..<count {
                                let p = rowIn + (start - sourceOffset + x) * 3
                                let w = kk[k + x]
                                s0 += Int(s[p]) * w
                                s1 += Int(s[p + 1]) * w
                                s2 += Int(s[p + 2]) * w
                            }
                            dst[rowOut + c * 3] = clip8(s0)
                            dst[rowOut + c * 3 + 1] = clip8(s1)
                            dst[rowOut + c * 3 + 2] = clip8(s2)
                        }
                    }
                }
            }
        }
        return RGBImage(width: outWidth, height: rows.count, pixels: out)
    }

    /// The vertical pass for output `rows`, over every column of `src`. `sourceOffset` is the
    /// input row `src`'s first row stands for.
    static func vertical(_ src: RGBImage, rows: Range<Int>, co: Coefficients, sourceOffset: Int) -> RGBImage {
        let stride = src.width * 3
        var out = [UInt8](repeating: 0, count: stride * rows.count)
        let half = 1 << (precisionBits - 1)
        src.pixels.withUnsafeBufferPointer { s in
            out.withUnsafeMutableBufferPointer { dst in
                co.weights.withUnsafeBufferPointer { kk in
                    for (r, yy) in rows.enumerated() {
                        let (start, count) = co.bounds[yy]
                        let k = yy * co.ksize
                        let rowOut = r * stride
                        for i in 0..<stride {
                            var acc = half
                            for y in 0..<count {
                                acc += Int(s[(start - sourceOffset + y) * stride + i]) * kk[k + y]
                            }
                            dst[rowOut + i] = clip8(acc)
                        }
                    }
                }
            }
        }
        return RGBImage(width: src.width, height: rows.count, pixels: out)
    }
}

/// Vector helpers for embeddings.
public enum Embedding {
    /// `v` divided by its L2 norm; a zero vector stays zero.
    public static func l2Normalized(_ v: [Float]) -> [Float] {
        var sum: Double = 0
        for x in v { sum += Double(x) * Double(x) }
        let norm = sum.squareRoot()
        guard norm > 0, norm.isFinite else { return v.map { _ in 0 } }
        return v.map { Float(Double($0) / norm) }
    }

    /// The dot product, which is the cosine similarity of two L2-normalised vectors; 0 when the
    /// lengths differ.
    public static func dot(_ a: [Float], _ b: [Float]) -> Float {
        guard a.count == b.count else { return 0 }
        var s: Double = 0
        for i in a.indices { s += Double(a[i]) * Double(b[i]) }
        return Float(s)
    }

    /// The cosine similarity of two vectors of any length.
    public static func cosine(_ a: [Float], _ b: [Float]) -> Float {
        dot(l2Normalized(a), l2Normalized(b))
    }
}
