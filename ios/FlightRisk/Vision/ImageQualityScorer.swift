import CoreGraphics
import CoreImage
import Vision
import os

// MARK: - QualityReport

/// Quality report for a target reference photo.
///
/// Port of Android `ImageQualityScorer.kt` to Swift using CoreGraphics
/// and Vision framework. Quality checks include blur detection,
/// brightness/contrast analysis, resolution validation, and face
/// presence detection via `VNDetectFaceRectanglesRequest`.
struct QualityReport {
    /// Quality score 0.0-1.0.
    let overallScore: Float
    /// Letter grade A-F derived from ``overallScore``.
    let grade: String
    /// List of identified quality issues.
    let issues: [String]
    /// Actionable suggestions to improve photo quality.
    let suggestions: [String]

    /// Whether the photo quality is acceptable for use as a target.
    /// Grade C or above is acceptable.
    var isAcceptable: Bool { grade == "A" || grade == "B" || grade == "C" }
}

// MARK: - ImageQualityScorer

/// Scores the quality of a target reference photo for person ReID and
/// face matching.
///
/// This is a CoreGraphics-based port of the Android `ImageQualityScorer`.
/// Blur detection uses a simplified Laplacian-like edge variance metric
/// computed from pixel luminance differences.
///
/// ## Scoring breakdown (each 0.0-1.0, weighted average):
/// - **Blur** (0.20): variance of luminance gradients (Laplacian approximation)
/// - **Brightness** (0.15): penalizes too dark (<50 mean) or too bright (>200 mean)
/// - **Contrast** (0.15): standard deviation of pixel luminance
/// - **Resolution** (0.15): penalizes images below 200x200
/// - **Face** (0.35): bonus if Vision detects at least one face
enum ImageQualityScorer {

    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.flightrisk",
        category: "ImageQualityScorer"
    )

    /// Minimum acceptable resolution in either dimension.
    private static let minResolution = 200

    /// Blur variance threshold below which the image is considered blurry.
    private static let blurThreshold: Float = 100

    // MARK: - Public API

    /// Analyze a `CGImage` and return a ``QualityReport``.
    ///
    /// - Parameter image: The target photo to analyze.
    /// - Returns: Quality report with score, grade, issues, and suggestions.
    static func analyze(image: CGImage) -> QualityReport {
        var issues: [String] = []
        var suggestions: [String] = []

        let blurScore = analyzeBlur(image: image)
        let brightnessScore = analyzeBrightness(image: image)
        let contrastScore = analyzeContrast(image: image)
        let resolutionScore = analyzeResolution(image: image)
        let faceScore = analyzeFacePresence(image: image)

        if blurScore < 0.5 {
            issues.append("Too blurry")
            suggestions.append("Use a sharper photo with the subject in focus")
        }
        if brightnessScore < 0.5 {
            issues.append("Poor lighting")
            suggestions.append("Use a photo taken in good lighting conditions")
        }
        if contrastScore < 0.5 {
            issues.append("Low contrast")
            suggestions.append("Use a photo with clear distinction between subject and background")
        }
        if resolutionScore < 0.5 {
            issues.append("Low resolution")
            suggestions.append("Use a higher-resolution photo (at least 200x200 pixels)")
        }
        if faceScore < 0.5 {
            issues.append("Face not detected")
            suggestions.append("Use a photo where the face is clearly visible and facing the camera")
        }

        // Weighted average: face detection is most important for matching
        let overallScore =
            blurScore * 0.20 +
            brightnessScore * 0.15 +
            contrastScore * 0.15 +
            resolutionScore * 0.15 +
            faceScore * 0.35

        let grade: String
        switch overallScore {
        case 0.85...:
            grade = "A"
        case 0.70...:
            grade = "B"
        case 0.50...:
            grade = "C"
        case 0.30...:
            grade = "D"
        default:
            grade = "F"
        }

        return QualityReport(
            overallScore: overallScore,
            grade: grade,
            issues: issues,
            suggestions: suggestions
        )
    }

    // MARK: - Blur Detection (Laplacian Approximation)

    /// Estimate image sharpness using a simplified Laplacian filter.
    ///
    /// Computes the variance of luminance second-derivatives by comparing
    /// each interior pixel with its horizontal and vertical neighbors.
    /// Higher variance indicates more edges (sharper image).
    ///
    /// - Returns: Score 0.0-1.0 where 1.0 is sharp.
    private static func analyzeBlur(image: CGImage) -> Float {
        guard let luminances = extractLuminances(from: image, maxSize: 200) else {
            return 0
        }
        let w = luminances.width
        let h = luminances.height
        let data = luminances.data

        if w < 3 || h < 3 { return 0 }

        var sum: Double = 0
        var sumSq: Double = 0
        var count = 0

        for y in 1..<(h - 1) {
            for x in 1..<(w - 1) {
                let idx = y * w + x
                let center = Double(data[idx])
                let left = Double(data[idx - 1])
                let right = Double(data[idx + 1])
                let top = Double(data[(y - 1) * w + x])
                let bottom = Double(data[(y + 1) * w + x])

                // Laplacian: sum of neighbors minus 4*center
                let laplacian = left + right + top + bottom - 4.0 * center
                sum += laplacian
                sumSq += laplacian * laplacian
                count += 1
            }
        }

        guard count > 0 else { return 0 }

        let mean = sum / Double(count)
        let variance = Float(sumSq / Double(count) - mean * mean)

        // Map variance to 0-1 score with blurThreshold as reference
        return min(max(variance / (variance + blurThreshold), 0), 1)
    }

    // MARK: - Brightness Analysis

    /// Score image brightness. Penalizes very dark (<50 mean luminance)
    /// and very bright (>200 mean luminance) images.
    ///
    /// - Returns: Score 0.0-1.0 where 1.0 is ideal brightness.
    private static func analyzeBrightness(image: CGImage) -> Float {
        guard let luminances = extractLuminances(from: image, maxSize: 100) else {
            return 0
        }
        let data = luminances.data

        let meanLum = data.reduce(Float(0)) { $0 + $1 } / Float(data.count)

        switch meanLum {
        case ..<30:
            return 0.1
        case 30..<50:
            return 0.3 + (meanLum - 30) / 20 * 0.2
        case 200..<230:
            return 0.3 + (230 - meanLum) / 30 * 0.2
        case 230...:
            return 0.1
        default:
            // Ideal range 50-200: score based on distance from midpoint
            let ideal: Float = 125
            let dist = abs(meanLum - ideal) / 75
            return 1 - dist * 0.3
        }
    }

    // MARK: - Contrast Analysis

    /// Score image contrast using the standard deviation of luminance.
    ///
    /// - Returns: Score 0.0-1.0 where 1.0 is good contrast.
    private static func analyzeContrast(image: CGImage) -> Float {
        guard let luminances = extractLuminances(from: image, maxSize: 100) else {
            return 0
        }
        let data = luminances.data

        let mean = Double(data.reduce(Float(0)) { $0 + $1 }) / Double(data.count)
        let variance = data.reduce(0.0) { acc, val in
            let diff = Double(val) - mean
            return acc + diff * diff
        } / Double(data.count)
        let stdDev = Float(variance.squareRoot())

        switch stdDev {
        case ..<10:
            return 0.1
        case 10..<20:
            return 0.3
        case 20..<40:
            return 0.5 + (stdDev - 20) / 20 * 0.3
        default:
            return min(0.8 + (stdDev - 40) / 40 * 0.2, 1)
        }
    }

    // MARK: - Resolution Check

    /// Score image resolution. Full score for 200x200+, degraded below.
    ///
    /// - Returns: Score 0.0-1.0 where 1.0 meets minimum resolution.
    private static func analyzeResolution(image: CGImage) -> Float {
        let minDim = min(image.width, image.height)

        switch minDim {
        case minResolution...:
            return 1
        case 100..<minResolution:
            return 0.5 + Float(minDim - 100) / 100 * 0.5
        case 50..<100:
            return 0.2 + Float(minDim - 50) / 50 * 0.3
        default:
            return 0.1
        }
    }

    // MARK: - Face Presence Check

    /// Check for face presence using Vision framework's
    /// `VNDetectFaceRectanglesRequest`.
    ///
    /// - Returns: 1.0 if at least one face is detected, 0.0 otherwise.
    private static func analyzeFacePresence(image: CGImage) -> Float {
        let request = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: image, options: [:])

        do {
            try handler.perform([request])
            guard let results = request.results, !results.isEmpty else {
                return 0
            }
            return 1
        } catch {
            logger.error("Face detection failed: \(error.localizedDescription)")
            return 0
        }
    }

    // MARK: - Utilities

    /// Container for extracted luminance data from a scaled image.
    private struct LuminanceData {
        let data: [Float]
        let width: Int
        let height: Int
    }

    /// Extract luminance values (0-255) from a CGImage, scaling it
    /// down so the longest side is at most `maxSize`.
    ///
    /// Uses the ITU-R BT.601 formula: `Y = 0.299R + 0.587G + 0.114B`.
    ///
    /// - Parameters:
    ///   - image: Source image.
    ///   - maxSize: Maximum dimension for the scaled image.
    /// - Returns: Luminance data or `nil` if the context could not be created.
    private static func extractLuminances(from image: CGImage, maxSize: Int) -> LuminanceData? {
        let maxDim = max(image.width, image.height)
        let scale: Float
        let targetW: Int
        let targetH: Int

        if maxDim <= maxSize {
            scale = 1
            targetW = image.width
            targetH = image.height
        } else {
            scale = Float(maxSize) / Float(maxDim)
            targetW = max(Int(Float(image.width) * scale), 1)
            targetH = max(Int(Float(image.height) * scale), 1)
        }

        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let bytesPerPixel = 4
        let bytesPerRow = bytesPerPixel * targetW
        let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue

        guard let context = CGContext(
            data: nil,
            width: targetW,
            height: targetH,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: bitmapInfo
        ) else {
            logger.error("Failed to create CGContext for luminance extraction")
            return nil
        }

        context.interpolationQuality = .medium
        context.draw(image, in: CGRect(x: 0, y: 0, width: targetW, height: targetH))

        guard let data = context.data else { return nil }

        let pixelBuffer = data.bindMemory(to: UInt8.self, capacity: targetW * targetH * bytesPerPixel)
        var luminances = [Float](repeating: 0, count: targetW * targetH)

        for i in 0..<(targetW * targetH) {
            let offset = i * bytesPerPixel
            let r = Float(pixelBuffer[offset])
            let g = Float(pixelBuffer[offset + 1])
            let b = Float(pixelBuffer[offset + 2])
            luminances[i] = 0.299 * r + 0.587 * g + 0.114 * b
        }

        return LuminanceData(data: luminances, width: targetW, height: targetH)
    }
}
