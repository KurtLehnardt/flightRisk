import SwiftUI

/// Overlay drawing scaled bounding boxes over the camera/drone video preview.
///
/// Mirrors the Android `DetectionOverlay` composable. Blue rectangles
/// indicate detected persons; green rectangles with a thicker stroke
/// indicate matched persons. Track ID labels are drawn above each box
/// with a dark background for readability.
///
/// Coordinates are scaled from the source frame dimensions to the
/// view's layout size so boxes align correctly regardless of preview
/// scaling.
struct DetectionOverlayView: View {

    /// Bounding boxes to draw. Each entry has pixel coordinates
    /// `[x1, y1, x2, y2]`, a match flag, and a stable track ID.
    let boxes: [(bbox: [Int], isMatch: Bool, trackId: Int)]

    /// Width of the source camera frame in pixels.
    let frameWidth: Int

    /// Height of the source camera frame in pixels.
    let frameHeight: Int

    var body: some View {
        Canvas { context, size in
            guard frameWidth > 0, frameHeight > 0 else { return }

            let scaleX = size.width / CGFloat(frameWidth)
            let scaleY = size.height / CGFloat(frameHeight)

            for box in boxes {
                guard box.bbox.count >= 4 else { continue }

                let x1 = CGFloat(box.bbox[0]) * scaleX
                let y1 = CGFloat(box.bbox[1]) * scaleY
                let x2 = CGFloat(box.bbox[2]) * scaleX
                let y2 = CGFloat(box.bbox[3]) * scaleY
                let boxRect = CGRect(x: x1, y: y1, width: x2 - x1, height: y2 - y1)

                let color: Color = box.isMatch
                    ? FlightRiskTheme.matchGreen
                    : FlightRiskTheme.detectionBlue
                let strokeWidth: CGFloat = box.isMatch ? 4 : 2

                // Bounding box rectangle
                context.stroke(
                    Path(boxRect),
                    with: .color(color),
                    lineWidth: strokeWidth
                )

                // Track ID label above the box
                let labelText = "#\(box.trackId)"
                let resolvedLabel = context.resolve(
                    Text(labelText)
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(color)
                )
                let textSize = resolvedLabel.measure(in: size)
                let padding: CGFloat = 4

                let bgRect = CGRect(
                    x: x1,
                    y: y1 - textSize.height - padding * 2,
                    width: textSize.width + padding * 2,
                    height: textSize.height + padding * 2
                )

                // Dark background for readability
                context.fill(Path(bgRect), with: .color(.black.opacity(0.7)))

                // Label text
                context.draw(
                    resolvedLabel,
                    at: CGPoint(x: bgRect.midX, y: bgRect.midY)
                )
            }
        }
        .allowsHitTesting(false)
        .accessibilityLabel(accessibilityDescription)
    }

    private var accessibilityDescription: String {
        let detected = boxes.filter { !$0.isMatch }.count
        let matched = boxes.filter { $0.isMatch }.count
        var desc = "Detection overlay: \(detected) person\(detected != 1 ? "s" : "") detected"
        if matched > 0 {
            desc += ", \(matched) match\(matched != 1 ? "es" : "")"
        }
        return desc
    }
}
