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

    /// The `size` x `size` RGB image the embedder sees.
    public static func resizeAndCrop(_ image: RGBImage, size: Int = CLIPContract.inputSize) -> RGBImage {
        let target = resizedSize(width: image.width, height: image.height, size: size)
        let resized = (target.width == image.width && target.height == image.height)
            ? image
            : PillowBicubic.resize(image, width: target.width, height: target.height)
        let off = cropOffsets(width: resized.width, height: resized.height, size: size)
        // The offsets are inside the image: the short side is `size` and the long side is longer.
        return resized.cropped(x1: off.left, y1: off.top, x2: off.left + size, y2: off.top + size)!
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
/// A port of `src/libImaging/Resample.c`: separable, horizontal pass then vertical pass, each
/// with the bicubic kernel (`a = -0.5`) widened by the scale when shrinking (antialiasing),
/// coefficients in fixed point with 22 fractional bits, each pass rounded and clipped to 0..255.
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
        precondition(width > 0 && height > 0, "the output has no pixels")
        var current = image
        if width != image.width {
            current = horizontal(current, to: width)
        }
        if height != image.height {
            current = vertical(current, to: height)
        }
        return current
    }

    static func horizontal(_ image: RGBImage, to outWidth: Int) -> RGBImage {
        let co = coefficients(inSize: image.width, outSize: outWidth)
        var out = [UInt8](repeating: 0, count: outWidth * image.height * 3)
        let half = 1 << (precisionBits - 1)
        image.pixels.withUnsafeBufferPointer { src in
            out.withUnsafeMutableBufferPointer { dst in
                co.weights.withUnsafeBufferPointer { kk in
                    for y in 0..<image.height {
                        let rowIn = y * image.width * 3
                        let rowOut = y * outWidth * 3
                        for xx in 0..<outWidth {
                            let (start, count) = co.bounds[xx]
                            let k = xx * co.ksize
                            var s0 = half, s1 = half, s2 = half
                            for x in 0..<count {
                                let p = rowIn + (start + x) * 3
                                let w = kk[k + x]
                                s0 += Int(src[p]) * w
                                s1 += Int(src[p + 1]) * w
                                s2 += Int(src[p + 2]) * w
                            }
                            dst[rowOut + xx * 3] = clip8(s0)
                            dst[rowOut + xx * 3 + 1] = clip8(s1)
                            dst[rowOut + xx * 3 + 2] = clip8(s2)
                        }
                    }
                }
            }
        }
        return RGBImage(width: outWidth, height: image.height, pixels: out)
    }

    static func vertical(_ image: RGBImage, to outHeight: Int) -> RGBImage {
        let co = coefficients(inSize: image.height, outSize: outHeight)
        let stride = image.width * 3
        var out = [UInt8](repeating: 0, count: image.width * outHeight * 3)
        let half = 1 << (precisionBits - 1)
        image.pixels.withUnsafeBufferPointer { src in
            out.withUnsafeMutableBufferPointer { dst in
                co.weights.withUnsafeBufferPointer { kk in
                    for yy in 0..<outHeight {
                        let (start, count) = co.bounds[yy]
                        let k = yy * co.ksize
                        let rowOut = yy * stride
                        for i in 0..<stride {
                            var s = half
                            for y in 0..<count {
                                s += Int(src[(start + y) * stride + i]) * kk[k + y]
                            }
                            dst[rowOut + i] = clip8(s)
                        }
                    }
                }
            }
        }
        return RGBImage(width: image.width, height: outHeight, pixels: out)
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
