import GroundProtocol
import SwiftUI

/// What the plan grid shows, as plain data so the empty and stale states can be tested without
/// a view. The plan is read only: nothing here sends anything, and nothing here decides whether
/// the plan is right. Uncovered areas are reported as "Not searched" and nothing more.
struct PlanGridViewState: Equatable {
    static let emptyText = "No plan yet. Plan from the dashboard."
    static let uncoveredLegend = "Not searched"

    let drawing: PlanDrawing?
    /// True when the ground state is not live (not connected, or waiting for a fresh status).
    let isStale: Bool

    init(plan: FleetPlanPayload?, isStale: Bool) {
        self.drawing = plan.map { PlanGeometry.drawing($0) }
        self.isStale = isStale
    }

    /// True when there is no plan, or a plan with nothing to draw.
    var isEmpty: Bool { drawing?.isEmpty ?? true }

    /// "Search plan", or "Last plan" while the ground state is stale.
    var title: String { isStale ? "Last plan" : "Search plan" }

    /// The spacing note, for example "Gap between zones: 100 cm", or nil when the plan has none.
    var gapNote: String? {
        guard let gap = drawing?.gapCm else { return nil }
        return "Gap between zones: \(Self.cm(gap))"
    }

    /// Notes about data that could not be drawn, or nil when everything was drawn.
    var skippedNote: String? {
        guard let d = drawing else { return nil }
        var parts: [String] = []
        if d.skippedLanes > 0 {
            parts.append("\(d.skippedLanes) lane\(d.skippedLanes == 1 ? "" : "s") not drawn (unknown direction or distance)")
        }
        if d.skippedUncovered > 0 {
            parts.append("\(d.skippedUncovered) \"\(Self.uncoveredLegend)\" entr\(d.skippedUncovered == 1 ? "y" : "ies") not drawn")
        }
        if d.truncated { parts.append("very large plan, only part is drawn") }
        return parts.isEmpty ? nil : parts.joined(separator: "; ") + "."
    }

    var accessibilitySummary: String {
        guard let d = drawing, !d.isEmpty else { return Self.emptyText }
        let lost = d.zones.filter(\.lost).count
        var s = "\(title): \(d.zones.count) zone\(d.zones.count == 1 ? "" : "s")"
        if lost > 0 { s += ", \(lost) lost" }
        if d.hasUncovered { s += ", \(d.uncoveredLanes.count + d.uncoveredAreas.count) \(Self.uncoveredLegend.lowercased())" }
        return s
    }

    static func cm(_ v: Double) -> String {
        v == v.rounded() ? "\(Int(v)) cm" : String(format: "%.1f cm", v)
    }

    /// The dashboard's per-drone colours (`DRONE_COLORS` in `ui.js`), picked by the number at the
    /// end of the drone id.
    static func colorIndex(droneId: String, count: Int = 4) -> Int {
        let digits = droneId.reversed().prefix { $0.isNumber }.reversed()
        guard let n = Int(String(digits)), n >= 1 else { return 0 }
        return (n - 1) % count
    }
}

/// A read-only picture of the fleet plan: the grid, each drone's zone and lane path with
/// direction arrows and a start marker, lost zones greyed and dashed, and uncovered lanes and
/// areas in the warning colour. There are no taps and nothing is sent.
struct PlanGridView: View {
    let state: PlanGridViewState

    private static let droneColors: [Color] = [
        Color(red: 0x0E / 255.0, green: 0xA5 / 255.0, blue: 0xE9 / 255.0),
        Color(red: 0xF5 / 255.0, green: 0x9E / 255.0, blue: 0x0B / 255.0),
        Color(red: 0x8B / 255.0, green: 0x5C / 255.0, blue: 0xF6 / 255.0),
        Color(red: 0x10 / 255.0, green: 0xB9 / 255.0, blue: 0x81 / 255.0),
    ]
    private static let padding = 18.0
    private static let arrowBudget = 600

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(state.title).font(.headline)
                if state.isStale {
                    Text("not live").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let d = state.drawing, !state.isEmpty {
                Canvas { context, size in
                    Self.draw(d, in: &context, size: size)
                }
                .aspectRatio(Self.aspect(d.bounds), contentMode: .fit)
                .frame(maxWidth: .infinity, maxHeight: 420)
                .opacity(state.isStale ? 0.7 : 1)
                legend(d)
            } else {
                Text(PlanGridViewState.emptyText)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("ground.plan.empty")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(state.accessibilitySummary)
        .accessibilityIdentifier("ground.plan")
    }

    /// Width over height of the bounds, kept within a range so a long thin plan stays readable.
    private static func aspect(_ b: PlanBox) -> CGFloat {
        let w = max(b.width, PlanGeometry.minExtentCm), h = max(b.height, PlanGeometry.minExtentCm)
        return CGFloat(min(max(w / h, 0.4), 2.5))
    }

    @ViewBuilder
    private func legend(_ d: PlanDrawing) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            FlowLabels(zones: d.zones)
            if d.hasLostZone {
                Label("LOST: the drone is lost; its zone is greyed", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if d.hasUncovered {
                Label(PlanGridViewState.uncoveredLegend, systemImage: "square.dashed")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(FlightRiskTheme.alertOrange)
                    .accessibilityIdentifier("ground.plan.notSearched")
            }
            if let gap = state.gapNote { Text(gap).font(.caption).foregroundStyle(.secondary) }
            if let skipped = state.skippedNote { Text(skipped).font(.caption).foregroundStyle(.secondary) }
        }
    }

    private struct FlowLabels: View {
        let zones: [ZoneDrawing]
        var body: some View {
            HStack(spacing: 10) {
                ForEach(Array(zones.prefix(8).enumerated()), id: \.offset) { _, z in
                    HStack(spacing: 4) {
                        Circle().fill(PlanGridView.color(z)).frame(width: 8, height: 8)
                        Text(z.droneId.isEmpty ? "zone" : z.droneId).font(.caption)
                    }
                }
                if zones.count > 8 { Text("+\(zones.count - 8)").font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    static func color(_ z: ZoneDrawing) -> Color {
        if z.lost { return .gray }
        return droneColors[PlanGridViewState.colorIndex(droneId: z.droneId, count: droneColors.count)]
    }

    // MARK: drawing

    private static func draw(_ d: PlanDrawing, in context: inout GraphicsContext, size: CGSize) {
        let t = PlanGeometry.transform(bounds: d.bounds, canvasWidth: size.width, canvasHeight: size.height,
                                       padding: padding)
        guard t.scale > 0 else { return }
        func pt(_ p: PlanPoint) -> CGPoint {
            let m = t.map(p)
            return CGPoint(x: m.x, y: m.y)
        }
        func rectPath(_ b: PlanBox) -> Path {
            let a = pt(PlanPoint(x: b.minX, y: b.maxY)), c = pt(PlanPoint(x: b.maxX, y: b.minY))
            return Path(CGRect(x: a.x, y: a.y, width: c.x - a.x, height: c.y - a.y))
        }
        let warning = FlightRiskTheme.alertOrange

        if let grid = d.grid {
            context.stroke(rectPath(grid), with: .color(.secondary), lineWidth: 1.5)
        }
        for area in d.uncoveredAreas {
            let p = rectPath(area.rect)
            context.fill(p, with: .color(warning.opacity(0.22)))
            context.stroke(p, with: .color(warning), style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
        }
        var arrows = 0
        for z in d.zones {
            let color = Self.color(z)
            if let r = z.rect {
                let p = rectPath(r)
                if z.lost {
                    context.fill(p, with: .color(.gray.opacity(0.15)))
                    context.stroke(p, with: .color(.gray), style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                } else {
                    context.fill(p, with: .color(color.opacity(0.08)))
                    context.stroke(p, with: .color(color), lineWidth: 1)
                }
            }
            if z.points.count > 1 {
                var path = Path()
                path.move(to: pt(z.points[0]))
                for p in z.points.dropFirst() { path.addLine(to: pt(p)) }
                context.stroke(path, with: .color(color),
                               style: StrokeStyle(lineWidth: 2.5, lineCap: .round, lineJoin: .round,
                                                  dash: z.lost ? [5, 4] : []))
                for s in z.segments where !s.isZeroLength && arrows < arrowBudget {
                    let a = pt(s.from), b = pt(s.to)
                    guard hypot(b.x - a.x, b.y - a.y) >= 16 else { continue }
                    drawArrow(&context, from: a, to: b, color: color)
                    arrows += 1
                }
            }
            if let start = z.start {
                let c = pt(start)
                let ring = Path(ellipseIn: CGRect(x: c.x - 6, y: c.y - 6, width: 12, height: 12))
                context.fill(ring, with: .color(Color(.systemBackground)))
                context.stroke(ring, with: .color(color), lineWidth: 2.5)
            }
            if let r = z.rect {
                let corner = pt(PlanPoint(x: r.minX, y: r.maxY))
                let label = z.lost ? "\(z.droneId) LOST" : z.droneId
                context.draw(Text(label).font(.caption2.weight(.semibold)).foregroundColor(color),
                             at: CGPoint(x: corner.x + 3, y: corner.y + 3), anchor: .topLeading)
            }
        }
        for lane in d.uncoveredLanes {
            var p = Path()
            p.move(to: pt(lane.from))
            p.addLine(to: pt(lane.to))
            context.stroke(p, with: .color(warning),
                           style: StrokeStyle(lineWidth: 4, lineCap: .round, dash: [2, 5]))
        }
    }

    private static func drawArrow(_ context: inout GraphicsContext, from a: CGPoint, to b: CGPoint, color: Color) {
        let mid = CGPoint(x: (a.x + b.x) / 2, y: (a.y + b.y) / 2)
        let angle = atan2(b.y - a.y, b.x - a.x)
        let size = 5.0
        var p = Path()
        p.move(to: CGPoint(x: mid.x + cos(angle) * size, y: mid.y + sin(angle) * size))
        p.addLine(to: CGPoint(x: mid.x + cos(angle + 2.5) * size, y: mid.y + sin(angle + 2.5) * size))
        p.addLine(to: CGPoint(x: mid.x + cos(angle - 2.5) * size, y: mid.y + sin(angle - 2.5) * size))
        p.closeSubpath()
        context.fill(p, with: .color(color))
    }
}
