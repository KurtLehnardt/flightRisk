import CoreGraphics
import os

/// HSV color histogram matcher for clothing appearance.
///
/// Splits a person crop into upper body (top 40%) and lower body (bottom 40%),
/// converts to HSV color space, computes normalized histograms, and compares
/// target vs detection histograms using correlation.
///
/// Registered as MatchScorer signal `clothingColor`.
final class ClothingColorMatcher {

    private let logger = Logger(subsystem: "com.flightrisk", category: "clothingColor")

    /// Number of bins per HSV channel.
    private static let hBins = 30
    private static let sBins = 32
    private static let vBins = 32

    /// Upper body = top 40% of person crop, lower body = bottom 40%.
    private static let upperBodyFraction: CGFloat = 0.40
    private static let lowerBodyFraction: CGFloat = 0.40

    /// Stored target histograms (upper + lower body).
    private var targetUpperHist: [Float]?
    private var targetLowerHist: [Float]?

    /// Whether a target has been set.
    var hasTarget: Bool { targetUpperHist != nil }

    // MARK: - Target Management

    /// Set the reference person crop and compute target color histograms.
    ///
    /// - Parameter photo: CGImage of the target person (full body crop).
    /// - Returns: `true` if histograms were computed successfully.
    func setTarget(photo: CGImage) -> Bool {
        guard let (upper, lower) = computeHistograms(image: photo) else {
            logger.warning("Failed to compute target color histograms")
            return false
        }
        targetUpperHist = upper
        targetLowerHist = lower
        logger.debug("Target color histograms set (upper: \(upper.count), lower: \(lower.count) bins)")
        return true
    }

    /// Clear the current target histograms.
    func clearTarget() {
        targetUpperHist = nil
        targetLowerHist = nil
    }

    // MARK: - Comparison

    /// Compare a detected person crop's clothing colors against the target.
    ///
    /// - Parameter crop: Image of a detected person.
    /// - Returns: Correlation score 0-1, or 0.0 if no target set or extraction fails.
    func compare(crop: CGImage) -> Float {
        guard let targetUpper = targetUpperHist, let targetLower = targetLowerHist else {
            return 0.0
        }

        guard let (detUpper, detLower) = computeHistograms(image: crop) else {
            return 0.0
        }

        // Average the correlation of upper and lower body histograms
        let upperCorr = histogramCorrelation(targetUpper, detUpper)
        let lowerCorr = histogramCorrelation(targetLower, detLower)
        let combined = (upperCorr + lowerCorr) / 2.0

        // Clamp to [0, 1]
        return max(0.0, min(1.0, combined))
    }

    // MARK: - Histogram Computation

    /// Compute HSV histograms for upper and lower body regions of a person crop.
    ///
    /// - Parameter image: Person crop CGImage.
    /// - Returns: Tuple of (upper body histogram, lower body histogram), or nil on failure.
    private func computeHistograms(image: CGImage) -> (upper: [Float], lower: [Float])? {
        let width = image.width
        let height = image.height

        guard width > 0, height > 0 else { return nil }

        // Extract raw pixel data
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else {
            return nil
        }

        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let data = context.data else { return nil }
        let pixels = data.bindMemory(to: UInt8.self, capacity: width * height * 4)

        // Define regions
        let upperEnd = Int(CGFloat(height) * Self.upperBodyFraction)
        let lowerStart = height - Int(CGFloat(height) * Self.lowerBodyFraction)

        // Compute histograms for each region
        let upperHist = computeRegionHistogram(
            pixels: pixels, width: width,
            yStart: 0, yEnd: upperEnd
        )
        let lowerHist = computeRegionHistogram(
            pixels: pixels, width: width,
            yStart: lowerStart, yEnd: height
        )

        return (upperHist, lowerHist)
    }

    /// Compute a normalized HSV hue-saturation histogram for a pixel region.
    ///
    /// Uses a 2D histogram of H (hue) and S (saturation) bins, which captures
    /// clothing color while being somewhat robust to lighting variation.
    private func computeRegionHistogram(
        pixels: UnsafeMutablePointer<UInt8>,
        width: Int,
        yStart: Int,
        yEnd: Int
    ) -> [Float] {
        let totalBins = Self.hBins * Self.sBins
        var histogram = [Float](repeating: 0.0, count: totalBins)
        var pixelCount: Float = 0

        for y in yStart..<yEnd {
            for x in 0..<width {
                let offset = (y * width + x) * 4
                let r = Float(pixels[offset]) / 255.0
                let g = Float(pixels[offset + 1]) / 255.0
                let b = Float(pixels[offset + 2]) / 255.0

                let (h, s, _) = rgbToHSV(r: r, g: g, b: b)

                // Quantize to histogram bins
                let hBin = min(Int(h / 360.0 * Float(Self.hBins)), Self.hBins - 1)
                let sBin = min(Int(s * Float(Self.sBins)), Self.sBins - 1)

                histogram[hBin * Self.sBins + sBin] += 1.0
                pixelCount += 1.0
            }
        }

        // Normalize
        if pixelCount > 0 {
            for i in 0..<totalBins {
                histogram[i] /= pixelCount
            }
        }

        return histogram
    }

    // MARK: - HSV Conversion

    /// Convert RGB (0-1 range) to HSV.
    ///
    /// - Returns: (hue 0-360, saturation 0-1, value 0-1)
    private func rgbToHSV(r: Float, g: Float, b: Float) -> (h: Float, s: Float, v: Float) {
        let maxC = max(r, g, b)
        let minC = min(r, g, b)
        let delta = maxC - minC

        // Value
        let v = maxC

        // Saturation
        let s: Float = maxC > 0 ? delta / maxC : 0

        // Hue
        var h: Float = 0
        if delta > 0 {
            if maxC == r {
                h = 60.0 * fmodf((g - b) / delta, 6.0)
            } else if maxC == g {
                h = 60.0 * ((b - r) / delta + 2.0)
            } else {
                h = 60.0 * ((r - g) / delta + 4.0)
            }
            if h < 0 { h += 360.0 }
        }

        return (h, s, v)
    }

    // MARK: - Histogram Correlation

    /// Compute Pearson correlation between two normalized histograms.
    ///
    /// Maps to OpenCV's `HISTCMP_CORREL` method.
    /// Result is in [-1, 1]; we remap to [0, 1] for scoring.
    private func histogramCorrelation(_ a: [Float], _ b: [Float]) -> Float {
        guard a.count == b.count, !a.isEmpty else { return 0.0 }

        let n = Float(a.count)
        let meanA = a.reduce(0, +) / n
        let meanB = b.reduce(0, +) / n

        var numerator: Float = 0
        var denomA: Float = 0
        var denomB: Float = 0

        for i in 0..<a.count {
            let da = a[i] - meanA
            let db = b[i] - meanB
            numerator += da * db
            denomA += da * da
            denomB += db * db
        }

        let denom = sqrtf(denomA * denomB)
        guard denom > 0 else { return 0.0 }

        let correlation = numerator / denom

        // Negative correlations map to 0 (matches Python/OpenCV HISTCMP_CORREL usage).
        return max(0.0, correlation)
    }
}
