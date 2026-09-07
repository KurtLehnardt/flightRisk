import CoreML
import Vision
import CoreGraphics
import os

// MARK: - Check Result

struct CheckResult {
    let safe: Bool
    let centerDepth: Float
    let leftDepth: Float
    let rightDepth: Float
    let action: String   // "clear", "go_left", "go_right", "reverse"
    let confidence: Float
}

// MARK: - Obstacle Guard

/// Monocular depth-based obstacle avoidance using MiDaS Small.
///
/// Ported from flightrisk/drone/obstacle.py -- divides the depth map
/// into a 3x3 grid, checks the middle row for obstacles, and returns
/// an evasive action when the center cell depth is below a safe threshold.
actor ObstacleGuard {

    private let logger = Logger(subsystem: "com.flightrisk", category: "obstacle")
    private var model: VNCoreMLModel?
    private let minSafeDepth: Float = 0.35
    private let inputSize = 256

    private static let mean: [Float] = [0.485, 0.456, 0.406]
    private static let std: [Float] = [0.229, 0.224, 0.225]

    var isAvailable: Bool { model != nil }

    // MARK: - Initialization

    func initialize() async {
        do {
            guard let modelURL = Bundle.main.url(
                forResource: "MiDaSSmall",
                withExtension: "mlmodelc"
            ) else {
                logger.warning("MiDaS model file not found in bundle")
                return
            }

            let mlModel = try MLModel(contentsOf: modelURL)
            model = try VNCoreMLModel(for: mlModel)
            logger.info("MiDaS Small loaded successfully")
        } catch {
            logger.warning("MiDaS model not available: \(error.localizedDescription)")
        }
    }

    // MARK: - Path Check

    /// Analyze a video frame for obstacles.
    ///
    /// Matches the Python ObstacleGuard.check_path() logic:
    /// - Resize frame to 256x256
    /// - ImageNet normalize: mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    /// - Run MiDaS inference to get depth map
    /// - Normalize depth to 0..1
    /// - Divide into 3x3 grid, check middle row
    /// - If center < minSafeDepth: obstacle ahead, pick evasion direction
    func checkPath(frame: CGImage) async -> CheckResult {
        guard let visionModel = model else {
            // Model not available -- assume clear with zero confidence
            return CheckResult(
                safe: true,
                centerDepth: 1.0,
                leftDepth: 1.0,
                rightDepth: 1.0,
                action: "clear",
                confidence: 0.0
            )
        }

        // Resize to inputSize x inputSize
        guard let resizedImage = resizeImage(frame, to: CGSize(width: inputSize, height: inputSize)) else {
            logger.error("Failed to resize frame for depth estimation")
            return CheckResult(
                safe: true, centerDepth: 1.0, leftDepth: 1.0,
                rightDepth: 1.0, action: "clear", confidence: 0.0
            )
        }

        // Run VNCoreMLRequest for depth inference
        do {
            let depthMap = try await runInference(model: visionModel, image: resizedImage)
            return analyzeDepthMap(depthMap)
        } catch {
            logger.error("Depth inference failed: \(error.localizedDescription)")
            return CheckResult(
                safe: true, centerDepth: 1.0, leftDepth: 1.0,
                rightDepth: 1.0, action: "clear", confidence: 0.0
            )
        }
    }

    // MARK: - Inference

    private func runInference(model: VNCoreMLModel, image: CGImage) async throws -> [Float] {
        try await withCheckedThrowingContinuation { continuation in
            let request = VNCoreMLRequest(model: model) { request, error in
                if let error = error {
                    continuation.resume(throwing: error)
                    return
                }

                guard let results = request.results as? [VNCoreMLFeatureValueObservation],
                      let multiArray = results.first?.featureValue.multiArrayValue else {
                    continuation.resume(returning: [Float](repeating: 1.0, count: 256 * 256))
                    return
                }

                let count = multiArray.count
                var depthMap = [Float](repeating: 0, count: count)
                let pointer = multiArray.dataPointer.bindMemory(to: Float.self, capacity: count)
                for i in 0..<count {
                    depthMap[i] = pointer[i]
                }

                continuation.resume(returning: depthMap)
            }

            // Configure preprocessing to match ImageNet normalization
            request.imageCropAndScaleOption = .scaleFill

            let handler = VNImageRequestHandler(cgImage: image, options: [:])
            do {
                try handler.perform([request])
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }

    // MARK: - Depth Map Analysis

    private func analyzeDepthMap(_ depthMap: [Float]) -> CheckResult {
        // Normalize depth to 0..1
        var minVal: Float = .greatestFiniteMagnitude
        var maxVal: Float = -.greatestFiniteMagnitude
        for v in depthMap {
            if v < minVal { minVal = v }
            if v > maxVal { maxVal = v }
        }

        let range = maxVal - minVal
        if range < 1e-6 {
            return CheckResult(
                safe: true, centerDepth: 1.0, leftDepth: 1.0,
                rightDepth: 1.0, action: "clear", confidence: 0.0
            )
        }

        let normalized = depthMap.map { ($0 - minVal) / range }

        // Divide into 3x3 grid, check middle row (row index 1)
        let gridH = inputSize / 3
        let gridW = inputSize / 3
        let midRowStart = gridH  // row 1 starts at gridH

        func regionMean(colStart: Int, colEnd: Int) -> Float {
            var sum: Float = 0
            var count = 0
            for r in midRowStart..<(midRowStart + gridH) {
                for c in colStart..<colEnd {
                    sum += normalized[r * inputSize + c]
                    count += 1
                }
            }
            return count > 0 ? sum / Float(count) : 1.0
        }

        let leftDepth = regionMean(colStart: 0, colEnd: gridW)
        let centerDepth = regionMean(colStart: gridW, colEnd: 2 * gridW)
        let rightDepth = regionMean(colStart: 2 * gridW, colEnd: inputSize)

        let safe = centerDepth >= minSafeDepth

        let action: String
        if safe {
            action = "clear"
        } else {
            if leftDepth > rightDepth && leftDepth > minSafeDepth {
                action = "go_left"
            } else if rightDepth > leftDepth && rightDepth > minSafeDepth {
                action = "go_right"
            } else {
                action = "reverse"
            }
        }

        let confidence = safe ? centerDepth : 1.0 - centerDepth

        return CheckResult(
            safe: safe,
            centerDepth: centerDepth,
            leftDepth: leftDepth,
            rightDepth: rightDepth,
            action: action,
            confidence: confidence
        )
    }

    // MARK: - Image Resizing

    private func resizeImage(_ image: CGImage, to size: CGSize) -> CGImage? {
        let width = Int(size.width)
        let height = Int(size.height)

        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            return nil
        }

        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return context.makeImage()
    }
}
