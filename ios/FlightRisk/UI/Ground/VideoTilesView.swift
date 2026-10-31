import CoreGraphics
import Foundation
import GroundProtocol
import ImageIO
import SwiftUI

// MARK: - Pure helpers

/// A detection box in normalized frame coordinates, already clamped to 0...1.
struct VideoTileBox: Equatable {
    /// Left, top, right and bottom, each in 0...1, with left < right and top < bottom.
    let x1: Double
    let y1: Double
    let x2: Double
    let y2: Double
    let matched: Bool
    /// Present only when the ground sent a non-empty track id.
    let trackId: String?

    /// The box in a tile of the given size.
    func rect(in size: CGSize) -> CGRect {
        CGRect(
            x: x1 * Double(size.width),
            y: y1 * Double(size.height),
            width: (x2 - x1) * Double(size.width),
            height: (y2 - y1) * Double(size.height)
        )
    }
}

/// How a tile is shown.
enum VideoTileMode: Equatable {
    /// A frame newer than the staleness limit, ground state live.
    case live
    /// A frame newer than the staleness limit, but the ground state is stale (the link dropped).
    case lastFrame
    /// No frame newer than the staleness limit: placeholder, no boxes.
    case noVideo
}

/// Everything a tile needs, computed from the view model's frames. A value type so the logic is
/// testable without a view model.
struct VideoTileModel: Identifiable, Equatable {
    let id: String
    let droneLabel: String
    let mode: VideoTileMode
    let arrivedAt: Date?
    /// Base64 JPEG of a fresh frame; nil for a detections-only frame or a stale tile.
    let imageBase64: String?
    let boxes: [VideoTileBox]
    /// Width over height of the source frame.
    let aspect: Double
    /// True when the ground is known not to be streaming video.
    let videoOffAtGround: Bool

    var accessibilityText: String {
        switch mode {
        case .noVideo:
            return "\(droneLabel) video, no video"
        case .live, .lastFrame:
            let n = boxes.count
            let detections = "\(n) detection\(n == 1 ? "" : "s")"
            return mode == .lastFrame
                ? "\(droneLabel) video, last frame, \(detections)"
                : "\(droneLabel) video, \(detections)"
        }
    }
}

enum VideoTileLogic {
    /// A tile whose newest frame is older than this many seconds shows "NO VIDEO".
    static let staleAfter: TimeInterval = 3
    /// The default aspect ratio when the frame carries no usable size.
    static let defaultAspect: Double = 4.0 / 3.0
    /// Upper bound on the decoded edge in pixels, whatever the tile asks for.
    static let maxDecodePixels = 2048
    /// Longest base64 text decoded (about 9 MB of JPEG). Longer is treated as corrupt.
    static let maxBase64Characters = 12_000_000
    /// Images with more source pixels than this are refused.
    static let maxSourcePixels = 100_000_000

    static let videoOffText = "Video is off at the ground. Turn it on from the dashboard."

    // MARK: Staleness

    /// True when the frame is older than `staleAfter` or has no arrival time. A frame stamped
    /// in the future (the clock moved back) counts as fresh.
    static func isStale(arrivedAt: Date?, now: Date) -> Bool {
        guard let arrivedAt else { return true }
        return now.timeIntervalSince(arrivedAt) > staleAfter
    }

    // MARK: Layout

    /// One column for a single drone and on a phone held upright; two for two or more drones in
    /// landscape or at regular width.
    static func columns(count: Int, regularWidth: Bool, compactHeight: Bool) -> Int {
        guard count >= 2 else { return 1 }
        return (regularWidth || compactHeight) ? 2 : 1
    }

    /// The pixel size to decode for a tile this many points wide, bounded.
    static func targetPixelSize(pointWidth: Double, scale: Double) -> Int {
        guard pointWidth.isFinite, scale.isFinite, pointWidth > 0, scale > 0 else { return 600 }
        let raw = (pointWidth * scale / 50).rounded(.up) * 50
        return Int(min(max(raw, 100), Double(maxDecodePixels)))
    }

    /// Width over height from the frame's size, or the default when either side is unusable.
    static func aspect(width: Double?, height: Double?) -> Double {
        guard let width, let height, width.isFinite, height.isFinite, width > 0, height > 0 else {
            return defaultAspect
        }
        return min(max(width / height, 0.25), 4)
    }

    // MARK: Boxes

    /// A box from `[x1, y1, x2, y2]` in 0...1. Coordinates outside 0...1 are clamped; swapped
    /// corners are put in order. Returns nil for the wrong count, a NaN or infinite value, or a
    /// box with no area left after clamping.
    static func box(bboxNorm: [Double]?, matched: Bool = false, trackId: String? = nil) -> VideoTileBox? {
        guard let b = bboxNorm, b.count == 4, b.allSatisfy({ $0.isFinite }) else { return nil }
        let c = b.map { min(max($0, 0), 1) }
        let left = min(c[0], c[2]), right = max(c[0], c[2])
        let top = min(c[1], c[3]), bottom = max(c[1], c[3])
        guard right > left, bottom > top else { return nil }
        let track = trackId?.trimmingCharacters(in: .whitespacesAndNewlines)
        return VideoTileBox(
            x1: left, y1: top, x2: right, y2: bottom,
            matched: matched,
            trackId: (track?.isEmpty == false) ? track : nil
        )
    }

    static func boxes(from frameBoxes: [FrameBox]?) -> [VideoTileBox] {
        (frameBoxes ?? []).compactMap {
            box(bboxNorm: $0.bboxNorm, matched: $0.matched ?? false, trackId: $0.trackId)
        }
    }

    // MARK: Tiles

    /// One tile per drone id, sorted so drone-2 comes before drone-10.
    static func makeTiles(
        frames: [String: FramePayload],
        arrivedAt: [String: Date],
        now: Date,
        streamVideoEnabled: Bool?,
        stateStale: Bool
    ) -> [VideoTileModel] {
        frames.keys
            .sorted { $0.localizedStandardCompare($1) == .orderedAscending }
            .compactMap { id -> VideoTileModel? in
                guard let frame = frames[id] else { return nil }
                let at = arrivedAt[id]
                let stale = isStale(arrivedAt: at, now: now)
                let label = id.isEmpty ? "unknown drone" : id
                let aspect = aspect(width: frame.frameWidth, height: frame.frameHeight)
                let hasImage = !(frame.image ?? "").isEmpty
                if stale {
                    return VideoTileModel(
                        id: id, droneLabel: label, mode: .noVideo, arrivedAt: at,
                        imageBase64: nil, boxes: [], aspect: aspect,
                        videoOffAtGround: streamVideoEnabled == false
                    )
                }
                return VideoTileModel(
                    id: id, droneLabel: label, mode: stateStale ? .lastFrame : .live, arrivedAt: at,
                    imageBase64: hasImage ? frame.image : nil,
                    boxes: boxes(from: frame.boxes), aspect: aspect,
                    videoOffAtGround: !hasImage || streamVideoEnabled == false
                )
            }
    }

    // MARK: Decode

    /// Decodes a base64 JPEG to an image no larger than `maxPixelSize` on its long edge, without
    /// ever holding the full-size bitmap. Returns nil for text that is not base64, not an image,
    /// too long, or too many pixels. Never traps.
    static func decodeImage(base64: String, maxPixelSize: Int) -> CGImage? {
        guard !base64.isEmpty, base64.utf8.count <= maxBase64Characters,
              let data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters),
              !data.isEmpty
        else { return nil }
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithData(data as CFData, sourceOptions),
              CGImageSourceGetCount(source) > 0,
              let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let w = (props[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue,
              let h = (props[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue,
              w > 0, h > 0, w * h <= maxSourcePixels
        else { return nil }
        // Never upscale: the cap is the smaller of the request and the source's long edge.
        let cap = min(max(maxPixelSize, 1), maxDecodePixels, max(w, h))
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: cap,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }
}

extension GroundViewModel {
    /// The video tiles for the current frames and clock, in stable order.
    var videoTiles: [VideoTileModel] {
        VideoTileLogic.makeTiles(
            frames: latestFrames,
            arrivedAt: latestFrameAt,
            now: now,
            streamVideoEnabled: streamVideoEnabled,
            stateStale: isStateStale
        )
    }
}

// MARK: - Views

/// The Ground tab's video section: one tile per drone with detection boxes. Read only: the
/// phone never asks the ground to start or stop video.
struct VideoTilesView: View {
    let viewModel: GroundViewModel
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @Environment(\.verticalSizeClass) private var verticalSizeClass

    var body: some View {
        let tiles = viewModel.videoTiles
        if !tiles.isEmpty || viewModel.streamVideoEnabled == false {
            VStack(alignment: .leading, spacing: 8) {
                Text("Video").font(.headline)
                if tiles.isEmpty {
                    Text(VideoTileLogic.videoOffText)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("ground.video.off")
                } else {
                    let count = VideoTileLogic.columns(
                        count: tiles.count,
                        regularWidth: horizontalSizeClass == .regular,
                        compactHeight: verticalSizeClass == .compact
                    )
                    LazyVGrid(
                        columns: Array(repeating: GridItem(.flexible(), spacing: 12, alignment: .top), count: count),
                        spacing: 12
                    ) {
                        ForEach(tiles) { VideoTileView(tile: $0) }
                    }
                }
            }
            .accessibilityIdentifier("ground.video")
        }
    }
}

struct VideoTileView: View {
    let tile: VideoTileModel
    @Environment(\.displayScale) private var displayScale
    /// Only the latest decoded image of this drone is kept; the next frame replaces it.
    @State private var image: CGImage?
    @State private var decodeFailed = false

    private struct DecodeKey: Equatable {
        let arrivedAt: Date?
        let hasImage: Bool
        let pixels: Int
    }

    var body: some View {
        Color.clear
            .aspectRatio(CGFloat(tile.aspect), contentMode: .fit)
            .overlay {
                GeometryReader { geo in
                    content(size: geo.size)
                        .task(id: DecodeKey(
                            arrivedAt: tile.arrivedAt,
                            hasImage: tile.imageBase64 != nil,
                            pixels: VideoTileLogic.targetPixelSize(pointWidth: Double(geo.size.width), scale: Double(displayScale))
                        )) {
                            await decode(pixels: VideoTileLogic.targetPixelSize(pointWidth: Double(geo.size.width), scale: Double(displayScale)))
                        }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(tile.accessibilityText)
            .accessibilityIdentifier("ground.video.tile.\(tile.id)")
    }

    private func decode(pixels: Int) async {
        guard let text = tile.imageBase64 else {
            image = nil
            decodeFailed = false
            return
        }
        let decoded = await Task.detached(priority: .userInitiated) {
            VideoTileLogic.decodeImage(base64: text, maxPixelSize: pixels)
        }.value
        if Task.isCancelled { return }
        image = decoded
        decodeFailed = decoded == nil
    }

    @ViewBuilder
    private func content(size: CGSize) -> some View {
        ZStack(alignment: .topLeading) {
            Rectangle().fill(Color.secondary.opacity(0.18))
            if tile.mode != .noVideo, let image {
                Image(decorative: image, scale: 1)
                    .resizable()
                    .scaledToFill()
                    .frame(width: size.width, height: size.height)
                    .clipped()
            }
            if tile.mode != .noVideo {
                boxLayer
            }
            placeholderText
            badge
        }
        .frame(width: size.width, height: size.height)
    }

    private var boxLayer: some View {
        Canvas { context, size in
            for box in tile.boxes {
                let rect = box.rect(in: size)
                let color: Color = box.matched ? FlightRiskTheme.matchGreen : FlightRiskTheme.detectionBlue
                context.stroke(Path(rect), with: .color(color), lineWidth: box.matched ? 4 : 2)
                var parts: [String] = []
                if box.matched { parts.append("MATCH") }
                if let track = box.trackId { parts.append("#\(track)") }
                guard !parts.isEmpty else { continue }
                let label = context.resolve(
                    Text(parts.joined(separator: " "))
                        .font(.system(size: 12, weight: .bold))
                        .foregroundColor(color)
                )
                let textSize = label.measure(in: size)
                let pad: CGFloat = 3
                let bg = CGRect(
                    x: min(max(rect.minX, 0), max(size.width - textSize.width - pad * 2, 0)),
                    y: max(rect.minY - textSize.height - pad * 2, 0),
                    width: textSize.width + pad * 2,
                    height: textSize.height + pad * 2
                )
                context.fill(Path(bg), with: .color(.black.opacity(0.7)))
                context.draw(label, at: CGPoint(x: bg.midX, y: bg.midY))
            }
        }
        .allowsHitTesting(false)
    }

    @ViewBuilder
    private var placeholderText: some View {
        VStack(spacing: 4) {
            if tile.mode == .noVideo {
                Text("NO VIDEO").font(.headline)
                if tile.videoOffAtGround { Text(VideoTileLogic.videoOffText).font(.caption) }
            } else if image == nil {
                if decodeFailed {
                    Text("Video frame could not be read.").font(.caption)
                } else if tile.videoOffAtGround {
                    Text(VideoTileLogic.videoOffText).font(.caption)
                }
            }
        }
        .multilineTextAlignment(.center)
        .foregroundStyle(.secondary)
        .padding(8)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var badge: some View {
        HStack(spacing: 6) {
            Text(tile.droneLabel).font(.caption.weight(.semibold))
            if tile.mode == .lastFrame { Text("Last frame").font(.caption) }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(.black.opacity(0.65), in: Capsule())
        .padding(6)
    }
}
