/// Which person boxes the app keeps after NMS, by size and shape relative to the frame.
///
/// The app used to keep only boxes at least 12% of the frame height and 0.8% of its area, which
/// dropped every person who is small in the frame: the normal case from a drone's altitude. The
/// defaults are now loose enough for aerial search; each limit can still be changed.
public struct PersonBoxFilter: Equatable, Sendable {
    /// Smallest box height, as a fraction of the frame height.
    public var minHeightRatio: Float
    /// Smallest box area, as a fraction of the frame area.
    public var minAreaRatio: Float
    /// Smallest width / height.
    public var minAspectRatio: Float
    /// Largest width / height.
    public var maxAspectRatio: Float

    public init(minHeightRatio: Float, minAreaRatio: Float, minAspectRatio: Float, maxAspectRatio: Float) {
        self.minHeightRatio = minHeightRatio
        self.minAreaRatio = minAreaRatio
        self.minAspectRatio = minAspectRatio
        self.maxAspectRatio = maxAspectRatio
    }

    /// The default for aerial search: a box at least 1% of the frame height (11 pixels of a
    /// 1080-pixel frame) and 0.01% of its area, and between 0.15 and 3 times as wide as it is
    /// tall, so that a person lying down, seen from the side or from above, is kept.
    public static let aerial = PersonBoxFilter(
        minHeightRatio: 0.01, minAreaRatio: 0.0001, minAspectRatio: 0.15, maxAspectRatio: 3.0
    )

    /// The filter the app used before aerial search (12% of the height, 0.8% of the area, 0.15 to
    /// 1.5 wide per tall). Kept for comparison and tests; it drops small people.
    public static let groundLevel = PersonBoxFilter(
        minHeightRatio: 0.12, minAreaRatio: 0.008, minAspectRatio: 0.15, maxAspectRatio: 1.5
    )

    /// Whether a box of `width` x `height` frame pixels passes, in a `frameWidth` x `frameHeight` frame.
    public func accepts(width: Float, height: Float, frameWidth: Int, frameHeight: Int) -> Bool {
        guard width > 0, height > 0, frameWidth > 0, frameHeight > 0 else { return false }
        let aspect = width / height
        return height >= Float(frameHeight) * minHeightRatio
            && width * height >= Float(frameWidth) * Float(frameHeight) * minAreaRatio
            && aspect >= minAspectRatio
            && aspect <= maxAspectRatio
    }
}

/// How a detection becomes the embedder's crop (`docs/mobile-models.md`, "From a detection to the
/// embedder's crop"; `mobile_preprocess.crop_pixels`): the box in frame pixels is clipped to the
/// frame, each corner is truncated toward zero, and the frame is cut at those pixels with no
/// margin, padding or squaring.
public enum CropRule {
    /// The integer `(x1, y1, x2, y2)` to cut, or `nil` when the clipped box is empty.
    public static func pixels(
        x1: Float, y1: Float, x2: Float, y2: Float, frameWidth: Int, frameHeight: Int
    ) -> (x1: Int, y1: Int, x2: Int, y2: Int)? {
        func clip(_ v: Float, _ limit: Int) -> Int {
            let d = Double(v)
            guard d.isFinite else { return d > 0 ? limit : 0 }
            return Int(min(max(d, 0), Double(limit)))
        }
        let a = clip(x1, frameWidth), b = clip(y1, frameHeight)
        let c = clip(x2, frameWidth), d = clip(y2, frameHeight)
        guard c > a, d > b else { return nil }
        return (a, b, c, d)
    }

    /// `pixels` for a detection.
    public static func pixels(_ box: DetectedBox, frameWidth: Int, frameHeight: Int) -> (x1: Int, y1: Int, x2: Int, y2: Int)? {
        pixels(x1: box.x1, y1: box.y1, x2: box.x2, y2: box.y2, frameWidth: frameWidth, frameHeight: frameHeight)
    }
}
