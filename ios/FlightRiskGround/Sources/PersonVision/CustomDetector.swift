import Foundation

/// The output layouts a person detector the user imports may have. FlightRisk never bundles,
/// hosts or downloads such a model; see `CustomModelValidator`.
public enum CustomHeadLayout: Equatable, Sendable, Codable {
    /// A YOLOv8 or YOLO11 head without NMS, `[1, 4 + classes, anchors]`: per anchor `cx, cy, w, h`
    /// (input pixels) and one sigmoid score per class, no objectness.
    case channelsFirst(classCount: Int)
    /// The same head transposed, `[1, anchors, 4 + classes]`.
    case anchorsFirst(classCount: Int)
    /// An NMS-free (end-to-end) head, `[1, detections, 6]`: `x1, y1, x2, y2` (input pixels),
    /// score, class index.
    case nmsFree(detectionCount: Int)

    /// The anchor count of a v8 or v11 head at a 640 x 640 input (strides 8, 16 and 32).
    public static let anchorCount = YOLOXContract.rowCount
    /// The most classes a head may have.
    public static let maxClassCount = 1000
    /// The most rows an NMS-free head may have.
    public static let maxDetectionCount = 1000

    /// The layout of an output of `shape`, or `nil` when it is none of the supported ones. A
    /// leading batch axis of 1 is allowed.
    public static func classify(shape: [Int]) -> CustomHeadLayout? {
        var dims = shape
        if dims.count == 3, dims[0] == 1 { dims.removeFirst() }
        guard dims.count == 2 else { return nil }
        let (a, b) = (dims[0], dims[1])
        if b == anchorCount, (5...(4 + maxClassCount)).contains(a) {
            return .channelsFirst(classCount: a - 4)
        }
        if a == anchorCount, (5...(4 + maxClassCount)).contains(b) {
            return .anchorsFirst(classCount: b - 4)
        }
        if b == 6, (1...maxDetectionCount).contains(a) {
            return .nmsFree(detectionCount: a)
        }
        return nil
    }

    /// The number of floats an output of this layout holds.
    public var valueCount: Int {
        switch self {
        case .channelsFirst(let n), .anchorsFirst(let n): return (4 + n) * Self.anchorCount
        case .nmsFree(let k): return 6 * k
        }
    }

    /// A short name for the Settings screen.
    public var displayName: String {
        switch self {
        case .channelsFirst(let n), .anchorsFirst(let n): return "YOLOv8/YOLO11 head, \(n) classes"
        case .nmsFree: return "NMS-free head"
        }
    }
}

/// Person detections from a user's own YOLO model. Class 0 must be "person" (COCO order).
public enum CustomHeadDecoder {
    /// Person detections (class 0) in frame pixels, highest score first. For the v8 and v11 heads
    /// each anchor takes its best class (the first on a tie), anchors under `scoreThreshold` or
    /// whose best class is not 0 are dropped, and NMS runs at `nmsIoU`. The NMS-free head's rows
    /// are kept as they are when their class is 0 and their score reaches `scoreThreshold`.
    public static func decodePersons(
        _ output: [Float],
        layout: CustomHeadLayout,
        geometry: LetterboxGeometry,
        scoreThreshold: Float,
        nmsIoU: Double = YOLOXContract.nmsIoU
    ) -> [DetectedBox] {
        guard output.count >= layout.valueCount else { return [] }
        switch layout {
        case .channelsFirst(let n), .anchorsFirst(let n):
            let anchors = CustomHeadLayout.anchorCount
            let channels = 4 + n
            let firstChannels: Bool
            if case .channelsFirst = layout { firstChannels = true } else { firstChannels = false }
            func value(_ anchor: Int, _ channel: Int) -> Float {
                firstChannels ? output[channel * anchors + anchor] : output[anchor * channels + channel]
            }
            var boxes: [DetectedBox] = []
            for i in 0..<anchors {
                var best = 0
                var bestValue = value(i, 4)
                for c in 1..<n {
                    let v = value(i, 4 + c)
                    if v > bestValue {
                        bestValue = v
                        best = c
                    }
                }
                guard best == 0, bestValue >= scoreThreshold, bestValue.isFinite else { continue }
                let cx = value(i, 0), cy = value(i, 1), w = value(i, 2), h = value(i, 3)
                boxes.append(DetectedBox(
                    x1: geometry.frameX(cx - w / 2), y1: geometry.frameY(cy - h / 2),
                    x2: geometry.frameX(cx + w / 2), y2: geometry.frameY(cy + h / 2),
                    score: bestValue, classIndex: 0
                ))
            }
            return NonMaxSuppression.singleClass(boxes, iou: nmsIoU)
        case .nmsFree(let k):
            var boxes: [DetectedBox] = []
            for i in 0..<k {
                let r = i * 6
                let score = output[r + 4]
                let cls = output[r + 5]
                guard cls.isFinite, Int(cls.rounded()) == 0, score >= scoreThreshold, score.isFinite else { continue }
                boxes.append(DetectedBox(
                    x1: geometry.frameX(output[r]), y1: geometry.frameY(output[r + 1]),
                    x2: geometry.frameX(output[r + 2]), y2: geometry.frameY(output[r + 3]),
                    score: score, classIndex: 0
                ))
            }
            return boxes.enumerated()
                .sorted { $0.element.score != $1.element.score ? $0.element.score > $1.element.score : $0.offset < $1.offset }
                .map(\.element)
        }
    }
}

/// What the validator needs to know about a compiled model, read from its Core ML description.
public struct CustomModelInterface: Equatable, Sendable {
    public enum InputKind: Equatable, Sendable {
        case image(width: Int, height: Int)
        case multiArray(shape: [Int])
        case other
    }

    public struct Input: Equatable, Sendable {
        public let name: String
        public let kind: InputKind
        public init(name: String, kind: InputKind) {
            self.name = name
            self.kind = kind
        }
    }

    public struct Output: Equatable, Sendable {
        public let name: String
        /// The multi-array shape, or `nil` when the output is not a multi-array.
        public let shape: [Int]?
        public init(name: String, shape: [Int]?) {
            self.name = name
            self.shape = shape
        }
    }

    public let inputs: [Input]
    public let outputs: [Output]
    /// The model's creator-defined metadata (an Ultralytics export records `names` there).
    public let metadata: [String: String]

    public init(inputs: [Input], outputs: [Output], metadata: [String: String] = [:]) {
        self.inputs = inputs
        self.outputs = outputs
        self.metadata = metadata
    }
}

/// Why a model the user picked cannot be used. Every message is for the person using the app and
/// never contains the file's name, path or contents.
public enum CustomModelError: Error, Equatable, Sendable {
    case notAttested
    case unsupportedFile
    case empty
    case tooLarge(bytes: Int64)
    case archive(String)
    case noPackageInArchive
    case severalPackagesInArchive
    case compileFailed
    case loadFailed
    case inputCount(Int)
    case inputNotImage
    case inputSize(width: Int, height: Int)
    case outputNotRecognised
    case personNotClassZero(String)
    case noPersonFound
    case storageFailed

    public var message: String {
        switch self {
        case .notAttested:
            return "Confirm that you may use this model before importing it."
        case .unsupportedFile:
            return "Choose a Core ML model: a .mlmodel file, or a .mlpackage folder compressed into a .zip file."
        case .empty:
            return "The file is empty."
        case .tooLarge(let bytes):
            return "The model is \(CustomModelValidator.megabytes(bytes)); the limit is \(CustomModelValidator.megabytes(CustomModelValidator.maxBytes))."
        case .archive(let why):
            return "The .zip file cannot be used: \(why)"
        case .noPackageInArchive:
            return "The .zip file holds no .mlpackage folder at its top level."
        case .severalPackagesInArchive:
            return "The .zip file holds more than one .mlpackage folder; it must hold exactly one."
        case .compileFailed:
            return "Core ML could not compile the model on this phone."
        case .loadFailed:
            return "Core ML could not load the compiled model."
        case .inputCount(let n):
            return "The model has \(n) inputs; it must have exactly one, a 640 x 640 image."
        case .inputNotImage:
            return "The model's input is not an image. Export it with an image input (the default for a Core ML export)."
        case .inputSize(let w, let h):
            return "The model's input is \(w) x \(h); it must be 640 x 640."
        case .outputNotRecognised:
            return "The model's output is not a YOLOv8 or YOLO11 detection head ([1, 84, 8400] for the 80 COCO classes) "
                + "or an NMS-free head ([1, N, 6]). Export it without NMS."
        case .personNotClassZero(let name):
            return "Class 0 of this model is \"\(name)\", not \"person\". Only models trained with person as class 0 (as COCO) can be used."
        case .noPersonFound:
            return "The model found no person in FlightRisk's test picture, so it cannot be used for search."
        case .storageFailed:
            return "The model could not be saved in the app's storage."
        }
    }
}

/// The checks a person detector the user imports must pass before FlightRisk uses it.
public enum CustomModelValidator {
    /// The largest model file, and the largest unpacked model, in bytes (100 MB).
    public static let maxBytes: Int64 = 100_000_000
    /// The score a person detection on the test picture must reach.
    public static let personCheckThreshold: Float = 0.25

    /// "36.1 MB".
    public static func megabytes(_ bytes: Int64) -> String {
        String(format: "%.1f MB", Double(bytes) / 1_000_000)
    }

    /// Throws when a file or an unpacked model of `bytes` is empty or over the limit.
    public static func checkSize(_ bytes: Int64) throws {
        if bytes <= 0 { throw CustomModelError.empty }
        if bytes > maxBytes { throw CustomModelError.tooLarge(bytes: bytes) }
    }

    /// The kind of file picked, from its extension: a zipped `.mlpackage` or a `.mlmodel`.
    public enum FileKind: Equatable, Sendable {
        case zip
        case mlmodel
    }

    public static func fileKind(forExtension ext: String) throws -> FileKind {
        switch ext.lowercased() {
        case "zip": return .zip
        case "mlmodel": return .mlmodel
        default: throw CustomModelError.unsupportedFile
        }
    }

    /// The checked interface: one 640 x 640 image input, exactly one output with a supported head,
    /// and, when the model names its classes, "person" as class 0.
    public static func check(_ interface: CustomModelInterface) throws -> (input: String, output: String, layout: CustomHeadLayout) {
        guard interface.inputs.count == 1 else { throw CustomModelError.inputCount(interface.inputs.count) }
        let input = interface.inputs[0]
        guard case .image(let w, let h) = input.kind else { throw CustomModelError.inputNotImage }
        guard w == YOLOXContract.inputSize, h == YOLOXContract.inputSize else {
            throw CustomModelError.inputSize(width: w, height: h)
        }
        let heads = interface.outputs.compactMap { out -> (String, CustomHeadLayout)? in
            guard let shape = out.shape, let layout = CustomHeadLayout.classify(shape: shape) else { return nil }
            return (out.name, layout)
        }
        guard heads.count == 1 else { throw CustomModelError.outputNotRecognised }
        if let names = classNames(interface.metadata), let first = names[0],
           first.trimmingCharacters(in: .whitespaces).lowercased() != "person" {
            throw CustomModelError.personNotClassZero(first)
        }
        return (input.name, heads[0].0, heads[0].1)
    }

    /// The class names a model records in its metadata under `names`, as an Ultralytics export
    /// writes them (`{0: 'person', 1: 'bicycle', ...}`) or as JSON (`{"0": "person"}`); `nil`
    /// when there are none.
    public static func classNames(_ metadata: [String: String]) -> [Int: String]? {
        guard let text = metadata["names"] else { return nil }
        let pattern = #"["']?(\d+)["']?\s*:\s*(?:'([^']*)'|"([^"]*)")"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return nil }
        var names: [Int: String] = [:]
        let ns = text as NSString
        for m in regex.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            guard let index = Int(ns.substring(with: m.range(at: 1))) else { continue }
            let r = m.range(at: 2).location != NSNotFound ? m.range(at: 2) : m.range(at: 3)
            names[index] = ns.substring(with: r)
        }
        return names.isEmpty ? nil : names
    }

    /// Throws unless the model found a person, at `personCheckThreshold` or more, on the test picture.
    public static func checkPersonFound(_ persons: [DetectedBox]) throws {
        guard persons.contains(where: { $0.classIndex == 0 && $0.score >= personCheckThreshold }) else {
            throw CustomModelError.noPersonFound
        }
    }
}
