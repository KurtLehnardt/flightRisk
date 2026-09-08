import CoreGraphics
import CoreImage
import Vision
import os

/// Extracts structured text descriptions from images for text-only LLM consumption.
///
/// Since the on-device Gemma model has no vision capability, images must be
/// converted to text descriptions before LLM reasoning. This extractor uses
/// iOS Vision framework APIs to produce structured text from a `CGImage`.
///
/// Port of the Android `ImageDescriptionExtractor.kt` concept. Combines
/// multiple Vision framework requests (classification, face detection,
/// text recognition) with manual pixel sampling for clothing colors.
enum ImageDescriptionExtractor {

    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.flightrisk",
        category: "ImageDescriptionExtractor"
    )

    // MARK: - Public API

    /// Extract a structured text description from a `CGImage`.
    ///
    /// Uses multiple Vision framework requests:
    /// - `VNClassifyImageRequest` for scene/object classification
    /// - `VNDetectFaceLandmarksRequest` for face presence and position
    /// - `VNRecognizeTextRequest` for visible text (logos, numbers on clothing)
    /// - Manual pixel sampling for dominant clothing colors
    ///
    /// - Parameter image: The source image to describe.
    /// - Returns: Structured text description suitable for LLM comparison.
    static func describe(_ image: CGImage) async -> String {
        var parts: [String] = ["Person description from camera image:"]

        // 1. Scene/object classification
        let classifications = classifyImage(image)
        if !classifications.isEmpty {
            parts.append("- Scene labels: \(classifications.joined(separator: ", "))")
        }

        // 2. Face detection
        let faceInfo = detectFaces(image)
        parts.append("- Face visible: \(faceInfo)")

        // 3. Text recognition (logos, numbers on clothing)
        let visibleText = recognizeText(image)
        if !visibleText.isEmpty {
            parts.append("- Visible text on clothing: \(visibleText.joined(separator: ", "))")
        }

        // 4. Dominant colors
        let colors = extractDominantColors(image)
        if !colors.isEmpty {
            parts.append("- Dominant clothing colors: \(colors.joined(separator: ", "))")
        }

        // 5. Build estimation from aspect ratio
        let buildEstimate = estimateBuild(image)
        parts.append("- Estimated build: \(buildEstimate)")

        return parts.joined(separator: "\n")
    }

    // MARK: - Vision Requests

    /// Classify the image using `VNClassifyImageRequest`.
    ///
    /// - Returns: Top 5 classification labels with confidence > 0.3.
    private static func classifyImage(_ image: CGImage) -> [String] {
        let request = VNClassifyImageRequest()
        let handler = VNImageRequestHandler(cgImage: image, options: [:])

        do {
            try handler.perform([request])
            guard let results = request.results else { return [] }
            return results
                .filter { $0.confidence > 0.3 }
                .prefix(5)
                .map { $0.identifier.replacingOccurrences(of: "_", with: " ") }
        } catch {
            logger.debug("Classification failed: \(error.localizedDescription)")
            return []
        }
    }

    /// Detect faces and describe their size/proximity using
    /// `VNDetectFaceLandmarksRequest`, with `CIDetector` fallback.
    ///
    /// - Returns: Human-readable face presence description.
    private static func detectFaces(_ image: CGImage) -> String {
        let request = VNDetectFaceLandmarksRequest()
        let handler = VNImageRequestHandler(cgImage: image, options: [:])

        do {
            try handler.perform([request])
            guard let results = request.results, !results.isEmpty else {
                return "no"
            }
            let face = results[0]
            let bbox = face.boundingBox
            let relativeSize = bbox.width * bbox.height
            let sizeDesc: String
            if relativeSize > 0.15 {
                sizeDesc = "large (close-up)"
            } else if relativeSize > 0.05 {
                sizeDesc = "medium"
            } else {
                sizeDesc = "small (distant)"
            }
            return "yes, \(results.count) face(s), \(sizeDesc)"
        } catch {
            // Fallback to CIDetector (same pattern as ImageQualityScorer)
            let ciImage = CIImage(cgImage: image)
            guard let detector = CIDetector(
                ofType: CIDetectorTypeFace,
                context: nil,
                options: [CIDetectorAccuracy: CIDetectorAccuracyHigh]
            ) else {
                return "detection unavailable"
            }
            let faces = detector.features(in: ciImage)
            return faces.isEmpty ? "no" : "yes, \(faces.count) face(s)"
        }
    }

    /// Recognize visible text (logos, jersey numbers, etc.) using
    /// `VNRecognizeTextRequest`.
    ///
    /// - Returns: Array of recognized text strings.
    private static func recognizeText(_ image: CGImage) -> [String] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .fast
        let handler = VNImageRequestHandler(cgImage: image, options: [:])

        do {
            try handler.perform([request])
            guard let results = request.results else { return [] }
            return results.compactMap { observation in
                observation.topCandidates(1).first?.string
            }
        } catch {
            logger.debug("Text recognition failed: \(error.localizedDescription)")
            return []
        }
    }

    // MARK: - Color Analysis

    /// Sample dominant colors from the image by dividing into upper and
    /// lower thirds (roughly torso and legs).
    ///
    /// Draws the image into an RGBA bitmap context and samples ~500 pixels.
    /// Each sampled pixel is mapped to the nearest named clothing color.
    ///
    /// - Returns: Array of color descriptions like `"blue (upper body)"`.
    private static func extractDominantColors(_ image: CGImage) -> [String] {
        let width = image.width
        let height = image.height
        guard width > 0, height > 0 else { return [] }

        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let bytesPerPixel = 4
        let bytesPerRow = bytesPerPixel * width
        let bitmapInfo =
            CGImageAlphaInfo.premultipliedLast.rawValue |
            CGBitmapInfo.byteOrder32Big.rawValue

        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: bitmapInfo
        ) else { return [] }

        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let pixelData = context.data else { return [] }

        let pixels = pixelData.bindMemory(
            to: UInt8.self,
            capacity: width * height * bytesPerPixel
        )

        // Sample upper third (torso area) and lower third (legs area)
        var upperColors: [String: Int] = [:]
        var lowerColors: [String: Int] = [:]

        let sampleStep = max(width * height / 500, 1) // ~500 pixels total

        for i in stride(from: 0, to: width * height, by: sampleStep) {
            let offset = i * bytesPerPixel
            let r = pixels[offset]
            let g = pixels[offset + 1]
            let b = pixels[offset + 2]
            let colorName = nearestColorName(r: r, g: g, b: b)

            let y = i / width
            if y < height / 3 {
                upperColors[colorName, default: 0] += 1
            } else if y > height * 2 / 3 {
                lowerColors[colorName, default: 0] += 1
            }
        }

        var result: [String] = []
        if let topUpper = upperColors.max(by: { $0.value < $1.value }) {
            result.append("\(topUpper.key) (upper body)")
        }
        if let topLower = lowerColors.max(by: { $0.value < $1.value }) {
            result.append("\(topLower.key) (lower body)")
        }

        return result
    }

    /// Map RGB values to the nearest named color from a palette of common
    /// clothing colors using squared Euclidean distance.
    private static func nearestColorName(r: UInt8, g: UInt8, b: UInt8) -> String {
        let palette: [(name: String, r: UInt8, g: UInt8, b: UInt8)] = [
            ("white", 255, 255, 255),
            ("black", 0, 0, 0),
            ("gray", 128, 128, 128),
            ("red", 255, 0, 0),
            ("dark red", 139, 0, 0),
            ("blue", 0, 0, 255),
            ("navy", 0, 0, 128),
            ("light blue", 135, 206, 235),
            ("green", 0, 128, 0),
            ("dark green", 0, 100, 0),
            ("yellow", 255, 255, 0),
            ("orange", 255, 165, 0),
            ("pink", 255, 192, 203),
            ("purple", 128, 0, 128),
            ("brown", 139, 69, 19),
            ("tan", 210, 180, 140),
        ]

        var closest = "unknown"
        var minDist = Int.max

        for color in palette {
            let dr = Int(r) - Int(color.r)
            let dg = Int(g) - Int(color.g)
            let db = Int(b) - Int(color.b)
            let dist = dr * dr + dg * dg + db * db
            if dist < minDist {
                minDist = dist
                closest = color.name
            }
        }

        return closest
    }

    // MARK: - Build Estimation

    /// Estimate person's build from image aspect ratio.
    ///
    /// Uses the height-to-width ratio of the cropped person bounding box
    /// as a rough proxy for body proportions.
    ///
    /// - Returns: Build description string.
    private static func estimateBuild(_ image: CGImage) -> String {
        let ratio = Float(image.height) / Float(max(image.width, 1))
        if ratio > 3.0 {
            return "tall/slim"
        } else if ratio > 2.0 {
            return "average"
        } else if ratio > 1.0 {
            return "stocky"
        } else {
            return "cannot determine"
        }
    }
}
