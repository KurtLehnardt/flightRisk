/// Which person boxes the app keeps after NMS, by size and shape relative to the frame. It is
/// applied to the integer crop box (the detection after `CropRule`), as Android's
/// `DetectionSizeFilter` is.
///
/// The app used to keep only boxes at least 12% of the frame height and 0.8% of its area, which
/// dropped every person who is small in the frame: the normal case from a drone's altitude. The
/// default is now loose enough for aerial search, with the same values as Android's `AERIAL`; each
/// limit can still be changed.
public struct PersonBoxFilter: Equatable, Sendable {
    /// Smallest box height, as a fraction of the frame height.
    public var minHeightRatio: Float
    /// Smallest box area, as a fraction of the frame area.
    public var minAreaRatio: Float
    /// Smallest width / height.
    public var minAspectRatio: Float
    /// Largest width / height.
    public var maxAspectRatio: Float
    /// Smallest width and height in pixels, whatever the frame size: a crop thinner than this
    /// cannot be resized into a meaningful embedder input.
    public var minSidePixels: Int

    public init(minHeightRatio: Float, minAreaRatio: Float, minAspectRatio: Float, maxAspectRatio: Float, minSidePixels: Int = 2) {
        self.minHeightRatio = minHeightRatio
        self.minAreaRatio = minAreaRatio
        self.minAspectRatio = minAspectRatio
        self.maxAspectRatio = maxAspectRatio
        self.minSidePixels = minSidePixels
    }

    /// The default for aerial search, Android's `AERIAL`: a box at least 1% of the frame height
    /// and 0.005% of its area, and between 0.1 and 4 times as wide as it is tall, so that a person
    /// lying down, seen from the side or from above, is kept; and at least 2 pixels on each side.
    /// This is the filter for detection only. Matching has its own, separate limit: only crops at
    /// least 40 pixels tall are scored (`FlightRiskConfig.vision.matchMinCropHeightPx`), so a
    /// smaller box is still detected and published but not scored.
    public static let aerial = PersonBoxFilter(
        minHeightRatio: 0.01, minAreaRatio: 0.00005, minAspectRatio: 0.1, maxAspectRatio: 4.0
    )

    /// The filter the app used before aerial search (12% of the height, 0.8% of the area, 0.15 to
    /// 1.5 wide per tall; Android's `LEGACY`). Kept for comparison and tests; it drops small people.
    public static let groundLevel = PersonBoxFilter(
        minHeightRatio: 0.12, minAreaRatio: 0.008, minAspectRatio: 0.15, maxAspectRatio: 1.5
    )

    /// Whether an integer crop box of `width` x `height` pixels passes, in a `frameWidth` x
    /// `frameHeight` frame.
    public func accepts(width: Int, height: Int, frameWidth: Int, frameHeight: Int) -> Bool {
        guard width > 0, height > 0, frameWidth > 0, frameHeight > 0,
              width >= minSidePixels, height >= minSidePixels else { return false }
        let w = Float(width), h = Float(height)
        let aspect = w / h
        return h >= Float(frameHeight) * minHeightRatio
            && w * h >= Float(frameWidth) * Float(frameHeight) * minAreaRatio
            && aspect >= minAspectRatio
            && aspect <= maxAspectRatio
    }

    /// `accepts` for the crop box `CropRule` makes of `box`, or `nil` when that box is empty or
    /// filtered out.
    public func cropBox(_ box: DetectedBox, frameWidth: Int, frameHeight: Int) -> (x1: Int, y1: Int, x2: Int, y2: Int)? {
        guard let p = CropRule.pixels(box, frameWidth: frameWidth, frameHeight: frameHeight),
              accepts(width: p.x2 - p.x1, height: p.y2 - p.y1, frameWidth: frameWidth, frameHeight: frameHeight)
        else { return nil }
        return p
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
