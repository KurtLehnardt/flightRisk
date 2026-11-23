import Foundation

/// The bundled detector's contract (`docs/mobile-models.md`, "Detector: YOLOX-S"), as
/// `scripts/mobile_models/mobile_preprocess.py` states it.
public enum YOLOXContract {
    /// The installed Core ML package's name, without extension.
    public static let modelName = "YOLOXs"
    public static let inputSize = 640
    /// The Core ML input: a 640 x 640 image whose colour layout is BGR, values 0..255.
    public static let inputName = "images"
    /// The Core ML output: `[1, 8400, 85]` float32.
    public static let outputName = "detections"
    public static let strides = [8, 16, 32]
    public static let classCount = 80
    /// `cx, cy, w, h, objectness`, then 80 class scores.
    public static let rowLength = 5 + classCount
    /// 80 * 80 + 40 * 40 + 20 * 20.
    public static let rowCount = strides.reduce(0) { $0 + (inputSize / $1) * (inputSize / $1) }
    /// COCO class index of "person".
    public static let personClass = 0
    /// Upstream's per-class NMS IoU.
    public static let nmsIoU = 0.45

    /// The grid cell and stride of output row `row`: stride 8 first, then 16, then 32, each
    /// row-major (y outer, x inner). The model has already decoded its boxes with these.
    public static func anchor(row: Int) -> (x: Int, y: Int, stride: Int)? {
        guard row >= 0 else { return nil }
        var start = 0
        for stride in strides {
            let n = inputSize / stride
            if row < start + n * n {
                let i = row - start
                return (i % n, i / n, stride)
            }
            start += n * n
        }
        return nil
    }
}

/// One detection, in frame pixels.
public struct DetectedBox: Equatable, Sendable {
    public var x1: Float
    public var y1: Float
    public var x2: Float
    public var y2: Float
    /// For YOLOX, objectness times the class score.
    public var score: Float
    public var classIndex: Int

    public init(x1: Float, y1: Float, x2: Float, y2: Float, score: Float, classIndex: Int) {
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.score = score
        self.classIndex = classIndex
    }

    public var width: Float { x2 - x1 }
    public var height: Float { y2 - y1 }

    /// Intersection over union, in double precision as `mobile_preprocess.box_iou`.
    public func iou(_ other: DetectedBox) -> Double {
        let ix1 = max(Double(x1), Double(other.x1))
        let iy1 = max(Double(y1), Double(other.y1))
        let ix2 = min(Double(x2), Double(other.x2))
        let iy2 = min(Double(y2), Double(other.y2))
        let inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        let a = max(0, Double(x2) - Double(x1)) * max(0, Double(y2) - Double(y1))
        let b = max(0, Double(other.x2) - Double(other.x1)) * max(0, Double(other.y2) - Double(other.y1))
        let union = a + b - inter
        return union > 0 ? inter / union : 0
    }
}

/// Non-maximum suppression as upstream YOLOX and `mobile_preprocess.yolox_postprocess` do it.
public enum NonMaxSuppression {
    /// Greedy NMS over boxes of one class: highest score first (ties keep their order), and a box
    /// is dropped when its IoU with a kept box is above `iou`.
    public static func singleClass(_ boxes: [DetectedBox], iou: Double) -> [DetectedBox] {
        var order = boxes.indices.sorted { a, b in
            boxes[a].score != boxes[b].score ? boxes[a].score > boxes[b].score : a < b
        }
        var keep: [DetectedBox] = []
        while !order.isEmpty {
            let i = order.removeFirst()
            keep.append(boxes[i])
            order.removeAll { boxes[i].iou(boxes[$0]) > iou }
        }
        return keep
    }

    /// NMS run separately for each class (classes in ascending order), then every kept box sorted
    /// by score, highest first.
    public static func perClass(_ boxes: [DetectedBox], iou: Double) -> [DetectedBox] {
        let classes = Set(boxes.map(\.classIndex)).sorted()
        var kept: [DetectedBox] = []
        for c in classes {
            kept += singleClass(boxes.filter { $0.classIndex == c }, iou: iou)
        }
        return kept.enumerated()
            .sorted { $0.element.score != $1.element.score ? $0.element.score > $1.element.score : $0.offset < $1.offset }
            .map(\.element)
    }
}

/// Turns the YOLOX output into detections in frame pixels.
public enum YOLOXDecoder {
    /// Detections from a `[rowCount, 85]` output (the `[1, 8400, 85]` Core ML output without its
    /// batch axis), as upstream's `yolox.utils.postprocess` makes them: each row takes its best
    /// class (the first on a tie), its score is objectness times that class score, rows under
    /// `scoreThreshold` are dropped, NMS runs per class at `nmsIoU`, and the boxes are converted
    /// to corners and divided by the letterbox ratio. `classes` keeps only those class indices
    /// (`[0]` for persons, `nil` for all). Sorted by score, highest first.
    public static func decode(
        _ output: UnsafeBufferPointer<Float>,
        rowCount: Int = YOLOXContract.rowCount,
        geometry: LetterboxGeometry,
        scoreThreshold: Float,
        nmsIoU: Double = YOLOXContract.nmsIoU,
        classes: Set<Int>? = [YOLOXContract.personClass]
    ) -> [DetectedBox] {
        let n = YOLOXContract.rowLength
        guard output.count >= rowCount * n else { return [] }
        var candidates: [DetectedBox] = []
        for row in 0..<rowCount {
            let base = row * n
            let objectness = output[base + 4]
            var best = 0
            var bestValue = output[base + 5]
            for c in 1..<YOLOXContract.classCount {
                let v = output[base + 5 + c]
                if v > bestValue {
                    bestValue = v
                    best = c
                }
            }
            let score = objectness * bestValue
            guard score >= scoreThreshold, score.isFinite else { continue }
            if let classes, !classes.contains(best) { continue }
            let cx = output[base], cy = output[base + 1], w = output[base + 2], h = output[base + 3]
            candidates.append(DetectedBox(
                x1: geometry.frameX(cx - w / 2),
                y1: geometry.frameY(cy - h / 2),
                x2: geometry.frameX(cx + w / 2),
                y2: geometry.frameY(cy + h / 2),
                score: score,
                classIndex: best
            ))
        }
        return NonMaxSuppression.perClass(candidates, iou: nmsIoU)
    }

    /// `decode` over an array.
    public static func decode(
        _ output: [Float],
        geometry: LetterboxGeometry,
        scoreThreshold: Float,
        nmsIoU: Double = YOLOXContract.nmsIoU,
        classes: Set<Int>? = [YOLOXContract.personClass]
    ) -> [DetectedBox] {
        output.withUnsafeBufferPointer {
            decode($0, rowCount: output.count / YOLOXContract.rowLength, geometry: geometry,
                   scoreThreshold: scoreThreshold, nmsIoU: nmsIoU, classes: classes)
        }
    }
}
