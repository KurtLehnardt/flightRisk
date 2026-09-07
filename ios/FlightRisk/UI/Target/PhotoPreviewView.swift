import SwiftUI

/// Reusable photo preview with quality badge overlay.
///
/// Displays the selected target photo with rounded corners, a grade
/// badge in the top-right corner, a score percentage, and issue chips
/// in a flowing layout.
struct PhotoPreviewView: View {
    let image: CGImage
    let qualityReport: QualityReport?

    /// Size of the photo preview. Defaults to 240pt.
    var previewSize: CGFloat = 240

    var body: some View {
        VStack(spacing: 12) {
            // Photo with grade badge overlay
            photoWithBadge

            // Issue chips
            if let report = qualityReport, !report.issues.isEmpty {
                issueChips(report.issues)
            }

            // Score percentage
            if let report = qualityReport {
                scoreLabel(report)
            }
        }
    }

    // MARK: - Photo + Badge

    @ViewBuilder
    private var photoWithBadge: some View {
        let borderColor: Color = {
            guard let grade = qualityReport?.grade else { return .secondary }
            return FlightRiskTheme.gradeColor(grade)
        }()

        ZStack(alignment: .topTrailing) {
            Image(decorative: image, scale: 1)
                .resizable()
                .aspectRatio(contentMode: .fill)
                .frame(width: previewSize, height: previewSize)
                .clipped()
                .clipShape(RoundedRectangle(cornerRadius: 16))
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .strokeBorder(borderColor, lineWidth: 2)
                )
                .accessibilityLabel("Selected target photo")

            if let report = qualityReport {
                GradeBadgeView(grade: report.grade)
                    .padding(8)
            }
        }
        .frame(width: previewSize, height: previewSize)
    }

    // MARK: - Issue Chips

    @ViewBuilder
    private func issueChips(_ issues: [String]) -> some View {
        FlowLayout(spacing: 8) {
            ForEach(issues, id: \.self) { issue in
                Text(issue)
                    .font(.caption)
                    .fontWeight(.medium)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.red.opacity(0.12))
                    .foregroundStyle(Color.red)
                    .clipShape(Capsule())
                    .accessibilityLabel("Quality issue: \(issue)")
            }
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Score Label

    @ViewBuilder
    private func scoreLabel(_ report: QualityReport) -> some View {
        let pct = Int(report.overallScore * 100)
        Text("Quality: \(pct)%")
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .accessibilityLabel(
                "Photo quality score: \(pct) percent, grade \(report.grade)"
            )
    }
}

// MARK: - GradeBadgeView

/// Colored badge showing the quality grade letter (A-F).
struct GradeBadgeView: View {
    let grade: String

    /// Badge size in points. Defaults to 36.
    var size: CGFloat = 36

    var body: some View {
        Text(grade)
            .font(.system(.body, design: .rounded, weight: .heavy))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(FlightRiskTheme.gradeColor(grade))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .accessibilityLabel("Quality grade: \(grade)")
    }
}

// MARK: - FlowLayout

/// Simple horizontal wrapping layout for chip-style views.
///
/// On iOS 16+ this could use `Layout`, but a wrapped `HStack` approach
/// keeps backward compatibility simple and avoids `ExperimentalLayoutApi`
/// equivalent gymnastics.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var currentX: CGFloat = 0
        var currentY: CGFloat = 0
        var rowHeight: CGFloat = 0
        var totalHeight: CGFloat = 0
        var totalWidth: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if currentX + size.width > maxWidth, currentX > 0 {
                currentX = 0
                currentY += rowHeight + spacing
                rowHeight = 0
            }
            currentX += size.width + spacing
            totalWidth = max(totalWidth, currentX - spacing)
            rowHeight = max(rowHeight, size.height)
            totalHeight = currentY + rowHeight
        }

        return CGSize(width: totalWidth, height: totalHeight)
    }

    func placeSubviews(
        in bounds: CGRect,
        proposal: ProposedViewSize,
        subviews: Subviews,
        cache: inout ()
    ) {
        var currentX: CGFloat = bounds.minX
        var currentY: CGFloat = bounds.minY
        var rowHeight: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if currentX + size.width > bounds.maxX, currentX > bounds.minX {
                currentX = bounds.minX
                currentY += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(
                at: CGPoint(x: currentX, y: currentY),
                proposal: ProposedViewSize(size)
            )
            currentX += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
