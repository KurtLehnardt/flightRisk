import CoreGraphics
import CoreML
import CoreText
import CoreVideo
import Foundation
import os
import PersonVision

/// Something that finds people in a frame and returns their boxes in frame pixels.
protocol PersonDetectorBackend: AnyObject {
    func persons(in image: RGBImage, scoreThreshold: Float) throws -> [DetectedBox]
}

/// The bundled detector: YOLOX-S (Apache-2.0), converted and pinned by
/// `scripts/mobile_models/make_models.py`, with the contract in `docs/mobile-models.md`.
///
/// The frame is letterboxed at the top-left of a 640 x 640 canvas filled with 114, handed to
/// Core ML as a pixel buffer (the model's input is declared BGR, so Core ML orders the
/// channels), and the `[1, 8400, 85]` output is decoded as upstream's postprocess does.
final class YOLOXBackend: PersonDetectorBackend {
    let model: MLModel
    private var buffer: CVPixelBuffer?

    init(url: URL, computeUnits: MLComputeUnits) throws {
        let config = MLModelConfiguration()
        config.computeUnits = computeUnits
        model = try MLModel(contentsOf: url, configuration: config)
    }

    /// The raw `[8400 * 85]` output for a 640 x 640 RGB canvas that is already letterboxed.
    func rawOutput(canvas: RGBImage) throws -> [Float] {
        let pb = try ModelIO.pixelBuffer(canvas, reuse: &buffer)
        let input = try MLDictionaryFeatureProvider(dictionary: [YOLOXContract.inputName: MLFeatureValue(pixelBuffer: pb)])
        let out = try model.prediction(from: input)
        guard let array = out.featureValue(for: YOLOXContract.outputName)?.multiArrayValue else {
            throw ModelIO.Failure.output(YOLOXContract.outputName)
        }
        let values = ModelIO.floats(array)
        guard values.count == YOLOXContract.rowCount * YOLOXContract.rowLength else {
            throw ModelIO.Failure.output(YOLOXContract.outputName)
        }
        return values
    }

    func persons(in image: RGBImage, scoreThreshold: Float) throws -> [DetectedBox] {
        let (canvas, geometry) = Letterbox.make(image)
        return YOLOXDecoder.decode(try rawOutput(canvas: canvas), geometry: geometry, scoreThreshold: scoreThreshold)
    }
}

/// A person detector the user imported (Settings > Your own person detector). FlightRisk never
/// bundles, hosts or downloads one. An Ultralytics export expects the frame centred on the
/// canvas; its output is one of the heads `CustomHeadLayout` knows.
final class CustomYOLOBackend: PersonDetectorBackend {
    let model: MLModel
    let inputName: String
    let outputName: String
    let layout: CustomHeadLayout
    private var buffer: CVPixelBuffer?

    init(model: MLModel, inputName: String, outputName: String, layout: CustomHeadLayout) {
        self.model = model
        self.inputName = inputName
        self.outputName = outputName
        self.layout = layout
    }

    func persons(in image: RGBImage, scoreThreshold: Float) throws -> [DetectedBox] {
        let (canvas, geometry) = Letterbox.make(image, placement: .centred)
        let pb = try ModelIO.pixelBuffer(canvas, reuse: &buffer)
        let input = try MLDictionaryFeatureProvider(dictionary: [inputName: MLFeatureValue(pixelBuffer: pb)])
        let out = try model.prediction(from: input)
        guard let array = out.featureValue(for: outputName)?.multiArrayValue else {
            throw ModelIO.Failure.output("custom")
        }
        return CustomHeadDecoder.decodePersons(ModelIO.floats(array), layout: layout, geometry: geometry,
                                               scoreThreshold: scoreThreshold)
    }
}

/// Finds people in camera frames and cuts their crops for the appearance embedder.
///
/// It runs the bundled YOLOX-S, or the user's own model while one is imported and switched on
/// (`CustomDetectorStore`); switching it off goes back to YOLOX-S. Boxes pass through the
/// configurable `PersonBoxFilter` (loose by default, for people who are small in the frame), and
/// each crop is cut from the full frame by the contract's crop rule.
final class PersonDetector {

    private let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.flightrisk",
        category: "PersonDetector"
    )

    private let confidence: Float
    private let boxFilter: PersonBoxFilter
    private let bundled: YOLOXBackend?
    private let customStore: CustomDetectorStore?
    private let bundledReason: String?

    /// Guards the fields below. Never held while a model loads or runs.
    private let lock = NSLock()
    /// Serialises loads of the user's model, so two threads never load it at once.
    private let loadLock = NSLock()
    private var customGeneration = -1
    private var custom: CustomYOLOBackend?
    private var consecutiveCustomFailures = 0
    private var customDisabled = false
    private var _customFailureNotice: String?

    /// How many frames in a row the user's model may fail before the bundled one takes over.
    static let customFailureLimit = 3

    /// Posted on the main queue when the user's model is dropped for the session.
    static let customDetectorFailed = Notification.Name("FlightRiskCustomDetectorFailed")

    /// The banner text after the user's model failed and was dropped for the session.
    static let customFailedNotice = "Your imported detector failed; using the built-in detector."

    // MARK: - Init

    /// - Parameters:
    ///   - modelName: The compiled bundled model, without `.mlmodelc`.
    ///   - confidence: The smallest person score kept.
    ///   - boxFilter: Which person boxes are kept, by size and shape.
    ///   - bundle: Where the compiled model is.
    ///   - customStore: The user's own detector, or `nil` to use only the bundled one.
    init(
        modelName: String = YOLOXContract.modelName,
        confidence: Float = FlightRiskConfig.shared.vision.detectorConfidence,
        boxFilter: PersonBoxFilter = FlightRiskConfig.shared.vision.personBoxFilter,
        bundle: Bundle = .main,
        customStore: CustomDetectorStore? = nil
    ) {
        self.confidence = confidence
        self.boxFilter = boxFilter
        self.customStore = customStore

        if let url = BundledModels.url(modelName, in: bundle) {
            do {
                bundled = try YOLOXBackend(url: url, computeUnits: BundledModels.computeUnits(for: "\(modelName).mlpackage", in: bundle))
                bundledReason = nil
            } catch {
                logger.warning("The bundled person detection model could not be loaded")
                bundled = nil
                bundledReason = Self.loadFailedReason()
            }
        } else {
            logger.warning("No person detection model in this build; detection is off")
            bundled = nil
            bundledReason = Self.missingModelReason()
        }
    }

    /// Why person detection is off, in words for the person using the app, or `nil` when a
    /// detector is ready (the bundled one, or the user's own while it is on). The Search tab
    /// shows it as a banner, so a missing or broken model is never silent.
    ///
    /// This reads settings and files only. It never loads a model, so it is safe on the main thread.
    var unavailableReason: String? {
        if bundled != nil { return nil }
        lock.lock()
        let disabled = customDisabled
        lock.unlock()
        if !disabled, let store = customStore, store.isUsable { return nil }
        return bundledReason ?? Self.missingModelReason()
    }

    /// Set after the user's model failed repeatedly and was dropped for this session.
    var customFailureNotice: String? {
        lock.lock()
        defer { lock.unlock() }
        return _customFailureNotice
    }

    /// True when `detect(frame:)` can find people with what is loaded now.
    var isAvailable: Bool { activeBackend() != nil }

    /// True while the user's own detector is the one in use. Never loads a model.
    var isUsingCustomModel: Bool {
        lock.lock()
        defer { lock.unlock() }
        return custom != nil
    }

    /// The banner text when the model is not in the app bundle.
    static func missingModelReason() -> String {
        "Person detection model not installed. This build of FlightRisk does not include the person detection model. "
            + "The camera can run, but no one will be found or matched. "
            + "Install a build that includes the model."
    }

    /// The banner text when the model is in the bundle but could not be loaded.
    static func loadFailedReason() -> String {
        "Person detection model not loaded. The person detection model could not be loaded on this device. "
            + "The camera can run, but no one will be found or matched. "
            + "Restart the app; if this stays, reinstall it."
    }

    // MARK: - Backend choice

    /// Loads the user's own model in the background when its settings changed, so the first
    /// frame after an import does not wait for it.
    func preload() {
        Task.detached(priority: .utility) { [self] in ensureLoaded() }
    }

    /// Loads the user's own model when its settings changed since the last load. This is slow, so
    /// it runs only on the pipeline's queue or a detached task, never on the main thread. The load
    /// lock keeps two callers from loading at once; the second sees the finished result.
    private func ensureLoaded() {
        guard let store = customStore else { return }
        loadLock.lock()
        defer { loadLock.unlock() }
        let generation = store.generation
        lock.lock()
        let stale = generation != customGeneration
        lock.unlock()
        guard stale else { return }
        let backend = store.makeBackend()
        lock.lock()
        custom = backend
        customGeneration = generation
        consecutiveCustomFailures = 0
        customDisabled = false
        _customFailureNotice = nil
        lock.unlock()
    }

    /// The loaded backend: the user's model, or the bundled one. Never loads.
    private func activeBackend() -> PersonDetectorBackend? {
        lock.lock()
        defer { lock.unlock() }
        return custom ?? bundled
    }

    /// Counts a failed frame of the user's model; after three in a row, drops it for the session.
    private func customFailed(_ backend: PersonDetectorBackend, error: Error) {
        lock.lock()
        guard let current = custom, current === backend else { lock.unlock(); return }
        consecutiveCustomFailures += 1
        let drop = consecutiveCustomFailures >= Self.customFailureLimit
        if drop {
            custom = nil
            customDisabled = true
            _customFailureNotice = Self.customFailedNotice
        }
        lock.unlock()
        logger.error("Custom detector failed: \(String(describing: type(of: error)))")
        if drop {
            DispatchQueue.main.async { NotificationCenter.default.post(name: Self.customDetectorFailed, object: self) }
        }
    }

    private func customSucceeded(_ backend: PersonDetectorBackend) {
        lock.lock()
        if let current = custom, current === backend { consecutiveCustomFailures = 0 }
        lock.unlock()
    }

    // MARK: - Detection

    /// People in `image`, in frame pixels, after the score threshold, NMS and the box filter.
    func persons(in image: RGBImage) -> [DetectedBox] {
        ensureLoaded()
        guard let backend = activeBackend() else { return [] }
        do {
            let found = try backend.persons(in: image, scoreThreshold: confidence).filter {
                boxFilter.cropBox($0, frameWidth: image.width, frameHeight: image.height) != nil
            }
            customSucceeded(backend)
            return found
        } catch {
            if backend is CustomYOLOBackend {
                customFailed(backend, error: error)
            } else {
                logger.error("Person detection failed on a frame: \(String(describing: type(of: error)))")
            }
            return []
        }
    }

    /// Detect people in a camera frame and cut each one's crop from the full frame.
    ///
    /// - Parameter frame: The camera frame.
    /// - Returns: One `Detection` per person, highest score first.
    func detect(frame: CGImage) -> [Detection] {
        ensureLoaded()
        guard isAvailable, let image = ModelIO.rgb(frame) else { return [] }
        var detections: [Detection] = []
        for box in persons(in: image) {
            guard let p = CropRule.pixels(box, frameWidth: frame.width, frameHeight: frame.height),
                  let crop = frame.cropping(to: CGRect(x: p.x1, y: p.y1, width: p.x2 - p.x1, height: p.y2 - p.y1))
            else { continue }
            detections.append(Detection(bbox: [p.x1, p.y1, p.x2, p.y2], confidence: box.score, crop: crop))
        }
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

}

// MARK: - PipelineDetectionCallback

extension PersonDetector: PipelineDetectionCallback {}
