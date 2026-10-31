import CoreGraphics
import Foundation
import GroundProtocol
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

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
    /// True when this fresh frame says the ground is not streaming video. Never true on a stale
    /// tile: a tile with no recent frame cannot say why.
    let videoOffAtGround: Bool
    /// Why a stale tile shows no video; nil on a fresh tile.
    let noVideoReason: String?
    /// Boxes beyond `maxBoxesPerTile` that are not drawn.
    let hiddenBoxCount: Int

    var accessibilityText: String {
        switch mode {
        case .noVideo:
            return "\(droneLabel) video, no video, \(noVideoReason ?? "no recent frame")"
        case .live, .lastFrame:
            let n = boxes.count
            let total = n + hiddenBoxCount
            var detections = "\(total) detection\(total == 1 ? "" : "s")"
            let matches = boxes.filter(\.matched).count
            if matches > 0 { detections += ", \(matches) match\(matches == 1 ? "" : "es")" }
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
    /// Boxes drawn per tile; more are counted as "+N more".
    static let maxBoxesPerTile = 64
    /// Longest base64 text decoded (about 9 MB of JPEG). Longer is treated as corrupt.
    static let maxBase64Characters = 12_000_000
    /// Images with more source pixels than this are refused (the edge sends 320x180).
    static let maxSourcePixels = 16_000_000

    static let videoOffText = "Video is off at the ground. Turn it on from the dashboard."

    // MARK: Staleness

    /// True when the frame is older than `staleAfter` or has no arrival time. A frame stamped
    /// more than `staleAfter` in the future (the wall clock moved back) is stale too; a small
    /// negative age is jitter and counts as fresh.
    static func isStale(arrivedAt: Date?, now: Date) -> Bool {
        guard let arrivedAt else { return true }
        let age = now.timeIntervalSince(arrivedAt)
        return age > staleAfter || age < -staleAfter
    }

    /// The area a decoded image occupies inside a tile, fitted whole and centered like the
    /// dashboard's `getVideoRect` (`ui.js`). Boxes are normalized to the image, so they map onto
    /// this rectangle. With no usable image size it is the whole tile.
    static func imageRect(imageSize: CGSize?, in tile: CGSize) -> CGRect {
        let whole = CGRect(origin: .zero, size: tile)
        guard let imageSize, imageSize.width > 0, imageSize.height > 0, tile.width > 0, tile.height > 0 else {
            return whole
        }
        let scale = min(tile.width / imageSize.width, tile.height / imageSize.height)
        let w = imageSize.width * scale, h = imageSize.height * scale
        return CGRect(x: (tile.width - w) / 2, y: (tile.height - h) / 2, width: w, height: h)
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

    /// At most `maxBoxesPerTile` valid boxes, plus how many raw entries were left unexamined.
    static func cappedBoxes(from frameBoxes: [FrameBox]?) -> (boxes: [VideoTileBox], hidden: Int) {
        let raw = frameBoxes ?? []
        var result: [VideoTileBox] = []
        var index = 0
        while index < raw.count, result.count < maxBoxesPerTile {
            let b = raw[index]
            if let box = box(bboxNorm: b.bboxNorm, matched: b.matched ?? false, trackId: b.trackId) {
                result.append(box)
            }
            index += 1
        }
        return (result, raw.count - index)
    }

    static func boxes(from frameBoxes: [FrameBox]?) -> [VideoTileBox] {
        cappedBoxes(from: frameBoxes).boxes
    }

    /// The reason a tile with no recent frame shows no video.
    static func noVideoReason(droneLabel: String, stateStale: Bool) -> String {
        stateStale ? "no frames from the ground" : "no recent frame from \(droneLabel)"
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
            .sorted {
                let order = $0.localizedStandardCompare($1)
                return order == .orderedSame ? $0 < $1 : order == .orderedAscending
            }
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
                        videoOffAtGround: false,
                        noVideoReason: noVideoReason(droneLabel: label, stateStale: stateStale),
                        hiddenBoxCount: 0
                    )
                }
                let capped = cappedBoxes(from: frame.boxes)
                // The remembered stream flag is not trusted while the ground state is stale.
                let videoOff = frame.streamVideo == false || (!stateStale && streamVideoEnabled == false)
                return VideoTileModel(
                    id: id, droneLabel: label, mode: stateStale ? .lastFrame : .live, arrivedAt: at,
                    imageBase64: hasImage ? frame.image : nil,
                    boxes: capped.boxes, aspect: aspect,
                    videoOffAtGround: videoOff,
                    noVideoReason: nil,
                    hiddenBoxCount: capped.hidden
                )
            }
    }

    // MARK: Decode

    /// Decodes a base64 JPEG to an image no larger than `maxPixelSize` on its long edge, without
    /// ever holding the full-size bitmap. Returns nil for text that is not base64, not an image,
    /// too long, or too many pixels. Never traps.
    static func decodeImage(base64: String, maxPixelSize: Int, isCancelled: () -> Bool = { false }) -> CGImage? {
        guard !base64.isEmpty, !isCancelled(), base64.utf8.count <= maxBase64Characters,
              let data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters),
              !data.isEmpty
        else { return nil }
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithData(data as CFData, sourceOptions),
              CGImageSourceGetType(source) as String? == UTType.jpeg.identifier,
              CGImageSourceGetCount(source) > 0,
              let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let w = (props[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue,
              let h = (props[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue,
              w > 0, h > 0, w * h <= maxSourcePixels
        else { return nil }
        guard !isCancelled() else { return nil }
        // Never upscale: the cap is the smaller of the request and the source's long edge.
        let cap = min(max(maxPixelSize, 1), maxDecodePixels, max(w, h))
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: cap,
        ]
        let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        return isCancelled() ? nil : image
    }
}

/// Decodes the frames of one tile, one at a time. While a decode runs, newer requests replace
/// each other, so only the latest waiting frame is decoded and the rest are dropped. Only the
/// latest decoded image is kept.
@MainActor @Observable
final class VideoTileDecoder {
    typealias Decode = @Sendable (String, Int, () -> Bool) -> CGImage?

    private(set) var image: CGImage?
    private(set) var failed = false
    /// How many decodes have started; for tests.
    private(set) var decodesStarted = 0

    @ObservationIgnored private let decode: Decode
    @ObservationIgnored private var pending: (base64: String, pixels: Int)?
    @ObservationIgnored private var worker: Task<Void, Never>?
    @ObservationIgnored private var generation = 0

    init(decode: @escaping Decode = { VideoTileLogic.decodeImage(base64: $0, maxPixelSize: $1, isCancelled: $2) }) {
        self.decode = decode
    }

    /// Asks for this frame to be decoded; nil clears the image and abandons queued work.
    func submit(base64: String?, pixels: Int) {
        guard let base64 else {
            clear()
            return
        }
        pending = (base64, pixels)
        guard worker == nil else { return }
        let myGeneration = generation
        worker = Task { [weak self] in
            await self?.drain(generation: myGeneration)
        }
    }

    /// Drops queued work, cancels a running decode and clears the image.
    func clear() {
        generation += 1
        pending = nil
        worker?.cancel()
        worker = nil
        image = nil
        failed = false
    }

    private func drain(generation mine: Int) async {
        while mine == generation, let next = pending {
            pending = nil
            decodesStarted += 1
            let decode = self.decode
            let result = await Self.run(decode, next.base64, next.pixels)
            guard mine == generation, !Task.isCancelled else { return }
            image = result
            failed = result == nil
        }
        if mine == generation { worker = nil }
    }

    /// Runs off the main actor and inherits this task's cancellation.
    nonisolated private static func run(_ decode: Decode, _ base64: String, _ pixels: Int) async -> CGImage? {
        decode(base64, pixels) { Task.isCancelled }
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
        let showOffNote = viewModel.streamVideoEnabled == false && !viewModel.isStateStale
        if !tiles.isEmpty || showOffNote {
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
    /// Decodes this tile's frames one at a time and keeps only the latest image.
    @State private var decoder = VideoTileDecoder()

    private struct DecodeRequest: Equatable {
        let base64: String?
        let pixels: Int
    }

    var body: some View {
        Color.clear
            .aspectRatio(CGFloat(tile.aspect), contentMode: .fit)
            .overlay {
                GeometryReader { geo in
                    content(size: geo.size)
                        .onChange(of: request(width: geo.size.width), initial: true) { _, new in
                            decoder.submit(base64: new.base64, pixels: new.pixels)
                        }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .onDisappear { decoder.clear() }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(tile.accessibilityText)
            .accessibilityIdentifier("ground.video.tile.\(tile.id)")
    }

    private func request(width: CGFloat) -> DecodeRequest {
        DecodeRequest(
            base64: tile.imageBase64,
            pixels: VideoTileLogic.targetPixelSize(pointWidth: Double(width), scale: Double(displayScale))
        )
    }

    @ViewBuilder
    private func content(size: CGSize) -> some View {
        let image = tile.mode == .noVideo ? nil : decoder.image
        let imageSize = image.map { CGSize(width: $0.width, height: $0.height) }
        // The whole image, never cropped; boxes are normalized to the image like on the dashboard.
        let area = VideoTileLogic.imageRect(imageSize: imageSize, in: size)
        ZStack(alignment: .topLeading) {
            Rectangle().fill(Color.secondary.opacity(0.18))
            if let image {
                Image(decorative: image, scale: 1)
                    .resizable()
                    .frame(width: area.width, height: area.height)
                    .offset(x: area.minX, y: area.minY)
            }
            if tile.mode != .noVideo {
                boxLayer(area: area)
            }
            placeholderText(hasImage: image != nil)
            badge
        }
        .frame(width: size.width, height: size.height)
    }

    private func boxLayer(area: CGRect) -> some View {
        Canvas { context, _ in
            for box in tile.boxes {
                let rect = box.rect(in: area.size).offsetBy(dx: area.minX, dy: area.minY)
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
                let textSize = label.measure(in: area.size)
                let pad: CGFloat = 3
                let bg = CGRect(
                    x: min(max(rect.minX, area.minX), max(area.maxX - textSize.width - pad * 2, area.minX)),
                    y: max(rect.minY - textSize.height - pad * 2, area.minY),
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
    private func placeholderText(hasImage: Bool) -> some View {
        VStack(spacing: 4) {
            if tile.mode == .noVideo {
                Text("NO VIDEO").font(.headline)
                if let reason = tile.noVideoReason {
                    Text(reason.prefix(1).uppercased() + reason.dropFirst()).font(.caption)
                }
            } else if !hasImage {
                if tile.videoOffAtGround {
                    Text(VideoTileLogic.videoOffText).font(.caption)
                } else if decoder.failed {
                    Text("Video frame could not be read.").font(.caption)
                } else if tile.imageBase64 == nil {
                    Text("No image in this frame.").font(.caption)
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
            if tile.hiddenBoxCount > 0 { Text("+\(tile.hiddenBoxCount) more").font(.caption) }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(.black.opacity(0.65), in: Capsule())
        .padding(6)
    }
}
