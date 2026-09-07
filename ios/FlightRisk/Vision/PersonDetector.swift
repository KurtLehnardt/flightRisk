import CoreGraphics
import CoreImage
import CoreML
import os
import Vision

/// YOLO-based person detector using CoreML / Vision.
///
/// Port of `PersonDetector.kt` (Android/ONNX) to iOS. Loads a YOLO11n
/// CoreML model from the app bundle, runs inference on camera frames,
/// and returns bounding boxes for detected persons (COCO class 0).
final class PersonDetector {

    // MARK: - Constants

    private static let inputSize = 640
    private static let personClassId = 0
    private static let nmsIouThreshold: Float = 0.45
    private static let paddingGray: UInt8 = 114
    private static let minBboxAreaRatio: Float = 0.008
    private static let minBboxHeightRatio: Float = 0.12
    private static let minAspectRatio: Float = 0.15
    private static let maxAspectRatio: Float = 1.5

    private let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.flightrisk",
        category: "PersonDetector"
    )

    private let confidence: Float
    private let iouThreshold: Float

    /// The CoreML model wrapper for Vision requests. `nil` when the
    /// `.mlmodelc` bundle is missing — `detect()` returns an empty array
    /// in that case.
    private let vnModel: VNCoreMLModel?

    private var debugFrameCount = 0

    // MARK: - Init

    /// - Parameters:
    ///   - modelName: Name of the `.mlmodelc` bundle (without extension).
    ///   - confidence: Minimum person-class confidence to keep a detection.
    ///   - iouThreshold: IoU threshold for greedy NMS.
    init(
        modelName: String = "YOLOPersonDetector",
        confidence: Float = FlightRiskConfig.shared.vision.detectorConfidence,
        iouThreshold: Float = Self.nmsIouThreshold
    ) {
        self.confidence = confidence
        self.iouThreshold = iouThreshold

        if let modelURL = Bundle.main.url(forResource: modelName, withExtension: "mlmodelc") {
            do {
                let mlModel = try MLModel(contentsOf: modelURL)
                self.vnModel = try VNCoreMLModel(for: mlModel)
            } catch {
                logger.warning("Failed to load CoreML model '\(modelName)': \(error.localizedDescription)")
                self.vnModel = nil
            }
        } else {
            logger.warning("CoreML model '\(modelName).mlmodelc' not found in bundle — detector disabled")
            self.vnModel = nil
        }
    }

    // MARK: - Detection

    /// Detect persons in a camera frame.
    ///
    /// - Parameter frame: RGB `CGImage` from the camera.
    /// - Returns: Array of `Detection` for each detected person.
    func detect(frame: CGImage) -> [Detection] {
        guard let model = vnModel else { return [] }

        // Letterbox resize to 640x640
        let srcW = frame.width
        let srcH = frame.height

        let scale = min(
            Float(Self.inputSize) / Float(srcW),
            Float(Self.inputSize) / Float(srcH)
        )
        let newW = Int(Float(srcW) * scale)
        let newH = Int(Float(srcH) * scale)
        let padX = (Self.inputSize - newW) / 2
        let padY = (Self.inputSize - newH) / 2

        guard let letterboxed = createLetterboxedImage(
            frame: frame, newW: newW, newH: newH, padX: padX, padY: padY
        ) else {
            logger.error("Failed to create letterboxed image")
            return []
        }

        // Run CoreML inference via Vision
        let request = VNCoreMLRequest(model: model)
        request.imageCropAndScaleOption = .scaleFill

        let handler = VNImageRequestHandler(cgImage: letterboxed, options: [:])
        do {
            try handler.perform([request])
        } catch {
            logger.error("Vision inference failed: \(error.localizedDescription)")
            return []
        }

        // Parse raw YOLO output from the MLMultiArray
        guard let results = request.results else { return [] }
        let rawDetections = parseYoloOutput(
            results: results, frame: frame,
            scale: scale, padX: padX, padY: padY
        )

        // Apply NMS
        let boxes = rawDetections.map {
            [Float($0.x1), Float($0.y1), Float($0.x2), Float($0.y2)]
        }
        let scores = rawDetections.map { $0.conf }
        let nmsIndices = applyNms(boxes: boxes, scores: scores)

        // Body-part filter and crop
        let frameArea = Float(frame.width) * Float(frame.height)
        let minArea = frameArea * Self.minBboxAreaRatio
        let minHeight = Float(frame.height) * Self.minBboxHeightRatio

        var detections: [Detection] = []
        var bodyPartFiltered = 0

        for idx in nmsIndices {
            let raw = rawDetections[idx]
            let bboxW = Float(raw.x2 - raw.x1)
            let bboxH = Float(raw.y2 - raw.y1)
            let bboxArea = bboxW * bboxH
            let aspectRatio = bboxH > 0 ? bboxW / bboxH : 0

            if bboxArea < minArea || bboxH < minHeight ||
                aspectRatio < Self.minAspectRatio || aspectRatio > Self.maxAspectRatio {
                bodyPartFiltered += 1
                continue
            }

            let cropRect = CGRect(
                x: max(0, raw.x1),
                y: max(0, raw.y1),
                width: min(raw.x2 - raw.x1, frame.width - raw.x1),
                height: min(raw.y2 - raw.y1, frame.height - raw.y1)
            )
            guard let crop = frame.cropping(to: cropRect) else { continue }

            detections.append(Detection(
                bbox: [raw.x1, raw.y1, raw.x2, raw.y2],
                confidence: raw.conf,
                crop: crop
            ))
        }

        if bodyPartFiltered > 0 {
            logger.debug("Filtered \(bodyPartFiltered) body-part detections (too small or wrong aspect ratio)")
        }
        logger.debug("Detected \(detections.count) persons (\(rawDetections.count) pre-NMS)")
        return detections
    }

    // MARK: - Annotation

    /// Draw bounding boxes on a frame.
    ///
    /// - Parameters:
    ///   - frame: The original frame.
    ///   - detections: Output from `detect(frame:)`.
    ///   - matchIdx: Index of the matched person (drawn in green; others in blue).
    /// - Returns: Annotated frame copy.
    func annotate(frame: CGImage, detections: [Detection], matchIdx: Int? = nil) -> CGImage? {
        let width = frame.width
        let height = frame.height

        guard let colorSpace = frame.colorSpace ?? CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: nil,
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bytesPerRow: 0,
                  space: colorSpace,
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              ) else {
            return nil
        }

        // Draw original frame
        ctx.draw(frame, in: CGRect(x: 0, y: 0, width: width, height: height))

        // Core Graphics origin is bottom-left; bbox coords are top-left origin
        for (i, det) in detections.enumerated() {
            let x1 = det.bbox[0]
            let y1 = det.bbox[1]
            let x2 = det.bbox[2]
            let y2 = det.bbox[3]

            let isMatch = matchIdx != nil && i == matchIdx

            // Flip y for CG coordinate system (origin bottom-left)
            let rect = CGRect(
                x: CGFloat(x1),
                y: CGFloat(height - y2),
                width: CGFloat(x2 - x1),
                height: CGFloat(y2 - y1)
            )

            if isMatch {
                ctx.setStrokeColor(CGColor(red: 0, green: 1, blue: 0, alpha: 1))
                ctx.setLineWidth(4)
            } else {
                ctx.setStrokeColor(CGColor(red: 0, green: 0.706, blue: 1, alpha: 1))
                ctx.setLineWidth(2)
            }
            ctx.stroke(rect)

            // Label
            let label = isMatch
                ? "MATCH \(Int(det.confidence * 100))%"
                : "\(Int(det.confidence * 100))%"

            // Draw label background
            let fontSize: CGFloat = 14
            let labelWidth = CGFloat(label.count) * fontSize * 0.6
            let labelHeight = fontSize + 4
            let labelRect = CGRect(
                x: CGFloat(x1),
                y: CGFloat(height - y1) + 2,
                width: labelWidth,
                height: labelHeight
            )
            ctx.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 0.6))
            ctx.fill(labelRect)

            // Draw label text — use Core Text for proper text rendering
            let attributes: [NSAttributedString.Key: Any] = [
                .font: CTFontCreateWithName("Helvetica-Bold" as CFString, fontSize, nil),
                .foregroundColor: isMatch
                    ? CGColor(red: 0, green: 1, blue: 0, alpha: 1)
                    : CGColor(red: 0, green: 0.706, blue: 1, alpha: 1)
            ]
            let attrString = NSAttributedString(string: label, attributes: attributes)
            let line = CTLineCreateWithAttributedString(attrString)

            ctx.textPosition = CGPoint(
                x: CGFloat(x1) + 2,
                y: CGFloat(height - y1) + 4
            )
            CTLineDraw(line, ctx)
        }

        return ctx.makeImage()
    }

    // MARK: - IoU (static helper)

    /// Compute IoU between two boxes `[x1, y1, x2, y2]`.
    static func computeIou(box1: [Int], box2: [Int]) -> Float {
        let x1 = max(box1[0], box2[0])
        let y1 = max(box1[1], box2[1])
        let x2 = min(box1[2], box2[2])
        let y2 = min(box1[3], box2[3])
        let intersection = Float(max(0, x2 - x1)) * Float(max(0, y2 - y1))
        let area1 = Float(box1[2] - box1[0]) * Float(box1[3] - box1[1])
        let area2 = Float(box2[2] - box2[0]) * Float(box2[3] - box2[1])
        let union = area1 + area2 - intersection
        return union > 0 ? intersection / union : 0
    }

    // MARK: - Private Helpers

    /// Create a 640x640 letterboxed image with gray (114,114,114) padding.
    private func createLetterboxedImage(
        frame: CGImage, newW: Int, newH: Int, padX: Int, padY: Int
    ) -> CGImage? {
        let size = Self.inputSize
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: nil,
                  width: size,
                  height: size,
                  bitsPerComponent: 8,
                  bytesPerRow: size * 4,
                  space: colorSpace,
                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
              ) else {
            return nil
        }

        // Fill with gray padding (114/255)
        let gray = CGFloat(Self.paddingGray) / 255.0
        ctx.setFillColor(CGColor(red: gray, green: gray, blue: gray, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: size, height: size))

        // Draw resized frame centered (CG origin is bottom-left)
        let drawRect = CGRect(
            x: padX,
            y: size - padY - newH,
            width: newW,
            height: newH
        )
        ctx.draw(frame, in: drawRect)

        return ctx.makeImage()
    }

    /// Raw intermediate detection before NMS/filtering.
    private struct RawDetection {
        let x1: Int
        let y1: Int
        let x2: Int
        let y2: Int
        let conf: Float
    }

    /// Parse YOLO output from Vision results.
    ///
    /// YOLO11n ONNX output shape is `[1, 84, N]` but CoreML may produce
    /// `[1, N, 84]` or a different layout. We read the MLMultiArray shape
    /// at runtime and adapt.
    private func parseYoloOutput(
        results: [VNObservation],
        frame: CGImage,
        scale: Float,
        padX: Int,
        padY: Int
    ) -> [RawDetection] {
        // Try to get raw MLMultiArray output first
        if let coreMLObs = results.first as? VNCoreMLFeatureValueObservation,
           let multiArray = coreMLObs.featureValue.multiArrayValue {
            return parseMultiArray(
                multiArray, frame: frame,
                scale: scale, padX: padX, padY: padY
            )
        }

        // Fallback: if Vision produced VNRecognizedObjectObservation
        // (happens when CoreML model has NMS baked in)
        if let objectResults = results as? [VNRecognizedObjectObservation] {
            return parseRecognizedObjects(objectResults, frame: frame)
        }

        // Try to iterate all observations for any MLMultiArray
        for obs in results {
            if let featureObs = obs as? VNCoreMLFeatureValueObservation,
               let multiArray = featureObs.featureValue.multiArrayValue {
                return parseMultiArray(
                    multiArray, frame: frame,
                    scale: scale, padX: padX, padY: padY
                )
            }
        }

        logger.warning("No parseable YOLO output found in Vision results")
        return []
    }

    /// Parse raw MLMultiArray from YOLO11n. Handles both [1, 84, N] and
    /// [1, N, 84] layouts by inspecting the shape at runtime.
    private func parseMultiArray(
        _ multiArray: MLMultiArray,
        frame: CGImage,
        scale: Float,
        padX: Int,
        padY: Int
    ) -> [RawDetection] {
        let shape = multiArray.shape.map { $0.intValue }
        guard shape.count >= 2 else {
            logger.warning("Unexpected MLMultiArray shape: \(shape)")
            return []
        }

        // Determine layout
        // YOLO11n has 84 values per detection: 4 bbox + 80 class scores
        let dim1 = shape.count > 1 ? shape[1] : 0
        let dim2 = shape.count > 2 ? shape[2] : 0

        let transposed: Bool
        let numDetections: Int

        if dim1 == 84 {
            // Original ONNX layout: [1, 84, N] — columns are detections
            transposed = false
            numDetections = dim2
        } else if dim2 == 84 {
            // Transposed layout: [1, N, 84] — rows are detections
            transposed = true
            numDetections = dim1
        } else {
            logger.warning("Cannot determine YOLO output layout: shape \(shape)")
            return []
        }

        // Diagnostic logging
        debugFrameCount += 1
        if debugFrameCount % 30 == 0 {
            var maxPersonScore: Float = 0
            var maxAnyClass: Float = 0
            for i in 0..<numDetections {
                let personScore = value(multiArray, detection: i, channel: 4 + Self.personClassId, transposed: transposed, dim1: dim1)
                maxPersonScore = max(maxPersonScore, personScore)
                for c in 4..<84 {
                    let score = value(multiArray, detection: i, channel: c, transposed: transposed, dim1: dim1)
                    maxAnyClass = max(maxAnyClass, score)
                }
            }
            logger.debug("Output shape: \(shape), maxPersonScore=\(maxPersonScore), maxAnyClass=\(maxAnyClass), threshold=\(self.confidence)")
        }

        var rawDetections: [RawDetection] = []

        for i in 0..<numDetections {
            let personConf = value(multiArray, detection: i, channel: 4 + Self.personClassId, transposed: transposed, dim1: dim1)
            if personConf < confidence { continue }

            // Bbox: center_x, center_y, width, height (in letterboxed coords)
            let cx = value(multiArray, detection: i, channel: 0, transposed: transposed, dim1: dim1)
            let cy = value(multiArray, detection: i, channel: 1, transposed: transposed, dim1: dim1)
            let w = value(multiArray, detection: i, channel: 2, transposed: transposed, dim1: dim1)
            let h = value(multiArray, detection: i, channel: 3, transposed: transposed, dim1: dim1)

            // Convert to corner coords and undo letterbox transform
            let x1 = max(0, Int((cx - w / 2 - Float(padX)) / scale))
            let y1 = max(0, Int((cy - h / 2 - Float(padY)) / scale))
            let x2 = min(frame.width, Int((cx + w / 2 - Float(padX)) / scale))
            let y2 = min(frame.height, Int((cy + h / 2 - Float(padY)) / scale))

            if x2 > x1 && y2 > y1 {
                rawDetections.append(RawDetection(x1: x1, y1: y1, x2: x2, y2: y2, conf: personConf))
            }
        }

        return rawDetections
    }

    /// Read a single float from the MLMultiArray, handling both layouts.
    private func value(
        _ array: MLMultiArray,
        detection i: Int,
        channel c: Int,
        transposed: Bool,
        dim1: Int
    ) -> Float {
        let index: [NSNumber]
        if transposed {
            // [1, N, 84] — row i, column c
            index = [0, NSNumber(value: i), NSNumber(value: c)]
        } else {
            // [1, 84, N] — row c, column i
            index = [0, NSNumber(value: c), NSNumber(value: i)]
        }
        return array[index].floatValue
    }

    /// Fallback parser for when the CoreML model produces
    /// `VNRecognizedObjectObservation` (NMS baked in).
    private func parseRecognizedObjects(
        _ observations: [VNRecognizedObjectObservation],
        frame: CGImage
    ) -> [RawDetection] {
        var detections: [RawDetection] = []
        let w = Float(frame.width)
        let h = Float(frame.height)

        for obs in observations {
            // Check if top label is "person"
            guard let topLabel = obs.labels.first,
                  topLabel.identifier == "person" || topLabel.identifier == "0",
                  topLabel.confidence >= confidence else {
                continue
            }

            // Vision normalized coords: origin bottom-left, 0-1 range
            let bbox = obs.boundingBox
            let x1 = Int(Float(bbox.minX) * w)
            let y1 = Int((1 - Float(bbox.maxY)) * h)
            let x2 = Int(Float(bbox.maxX) * w)
            let y2 = Int((1 - Float(bbox.minY)) * h)

            if x2 > x1 && y2 > y1 {
                detections.append(RawDetection(
                    x1: max(0, x1),
                    y1: max(0, y1),
                    x2: min(frame.width, x2),
                    y2: min(frame.height, y2),
                    conf: topLabel.confidence
                ))
            }
        }

        return detections
    }

    /// Greedy NMS: suppress overlapping boxes by IoU.
    private func applyNms(boxes: [[Float]], scores: [Float]) -> [Int] {
        guard !boxes.isEmpty else { return [] }

        let indices = scores.indices.sorted { scores[$0] > scores[$1] }
        var keep: [Int] = []
        var suppressed = [Bool](repeating: false, count: boxes.count)

        for i in indices {
            if suppressed[i] { continue }
            keep.append(i)
            for j in indices {
                if suppressed[j] || i == j { continue }
                if computeIouFloat(box1: boxes[i], box2: boxes[j]) > iouThreshold {
                    suppressed[j] = true
                }
            }
        }

        return keep
    }

    /// IoU for float-array boxes (internal NMS use).
    private func computeIouFloat(box1: [Float], box2: [Float]) -> Float {
        let x1 = max(box1[0], box2[0])
        let y1 = max(box1[1], box2[1])
        let x2 = min(box1[2], box2[2])
        let y2 = min(box1[3], box2[3])
        let intersection = max(0, x2 - x1) * max(0, y2 - y1)
        let area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        let area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        let union = area1 + area2 - intersection
        return union > 0 ? intersection / union : 0
    }
}
