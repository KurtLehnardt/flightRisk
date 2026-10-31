import GroundProtocol
import SwiftUI

/// What the plan grid shows, as plain data so the empty and stale states can be tested without
/// a view. The plan is read only: nothing here sends anything, and nothing here decides whether
/// the plan is right. Uncovered areas are reported as "Not searched" and nothing more.
struct PlanGridViewState: Equatable {
    static let emptyText = "No plan yet. Plan from the dashboard."
    /// Lanes of a lost drone that nobody took.
    static let uncoveredLegend = "Not searched"
    /// Coverage gaps of a map plan, from the nominal camera model. A neutral report, not a verdict.
    static let areaLegend = "May not be covered (camera model)"
    static let partialNote = "Only part of the plan is drawn."

    let drawing: PlanDrawing?
    /// True when the ground state is not live (not connected, or waiting for a fresh status).
    let isStale: Bool
    /// False until a `fleet_plan` arrives in the current session: a plan kept from before a
    /// reconnect may no longer be the ground's plan.
    let planIsCurrent: Bool

    init(plan: FleetPlanPayload?, isStale: Bool, planIsCurrent: Bool = true) {
        self.drawing = plan.map { Self.cachedDrawing($0) }
        self.isStale = isStale
        self.planIsCurrent = planIsCurrent
    }

    // The Ground view builds this state on every body evaluation; the geometry is recomputed only
    // when the plan value changes.
    private static let cacheLock = NSLock()
    nonisolated(unsafe) private static var cache: (plan: FleetPlanPayload, drawing: PlanDrawing)?

    private static func cachedDrawing(_ plan: FleetPlanPayload) -> PlanDrawing {
        cacheLock.lock()
        if let c = cache, c.plan == plan {
            cacheLock.unlock()
            return c.drawing
        }
        cacheLock.unlock()
        let d = PlanGeometry.drawing(plan)
        cacheLock.lock()
        cache = (plan, d)
        cacheLock.unlock()
        return d
    }

    /// True when there is no plan, or a plan with nothing to draw.
    var isEmpty: Bool { drawing?.isEmpty ?? true }

    /// True when the plan must not read as the live plan.
    var isNotLive: Bool { isStale || !planIsCurrent }

    /// "Search plan"; "Last plan" while the ground state is stale; "Last plan (not confirmed this
    /// session)" until the ground has sent a plan in this session.
    var title: String {
        if !planIsCurrent { return "Last plan (not confirmed this session)" }
        return isStale ? "Last plan" : "Search plan"
    }

    /// The spacing note, for example "Gap between zones: 100 cm", or nil when the plan has none.
    var gapNote: String? {
        guard let gap = drawing?.gapCm else { return nil }
        return "Gap between zones: \(Self.cm(gap))"
    }

    /// Notes about data that could not be drawn as given, or nil when everything was drawn.
    var skippedNote: String? {
        guard let d = drawing else { return nil }
        var parts: [String] = []
        if d.skippedLanes > 0 {
            parts.append("\(d.skippedLanes) lane\(d.skippedLanes == 1 ? "" : "s") not drawn (unknown direction or distance)")
        }
        if d.skippedUncovered > 0 {
            parts.append("\(d.skippedUncovered) uncovered entr\(d.skippedUncovered == 1 ? "y" : "ies") not drawn")
        }
        for id in d.takeoffFallbackDroneIds {
            parts.append("takeoff point out of range for \(id); drawn from the zone corner")
        }
        var text = parts.isEmpty ? "" : parts.joined(separator: "; ") + "."
        if d.truncated { text += (text.isEmpty ? "" : " ") + Self.partialNote }
        return text.isEmpty ? nil : text
    }

    /// Up to three distinct reasons the ground gave, for lane entries and for area entries.
    var laneReasons: [String] { Self.distinct(drawing?.uncoveredLanes.map(\.reason) ?? []) }
    var areaReasons: [String] { Self.distinct(drawing?.uncoveredAreas.map(\.reason) ?? []) }

    private static func distinct(_ reasons: [String?]) -> [String] {
        var out: [String] = []
        for r in reasons.compactMap({ $0 }) where !r.isEmpty && !out.contains(r) { out.append(r) }
        return Array(out.prefix(3))
    }

    var accessibilitySummary: String {
        guard let d = drawing, !d.isEmpty else { return Self.emptyText }
        let lost = d.zones.filter(\.lost).count
        var s = "\(title): \(d.zones.count) zone\(d.zones.count == 1 ? "" : "s")"
        if lost > 0 { s += ", \(lost) lost" }
        if !d.uncoveredLanes.isEmpty { s += ", \(d.uncoveredLanes.count) \(Self.uncoveredLegend.lowercased())" }
        if !d.uncoveredAreas.isEmpty { s += ", \(d.uncoveredAreas.count) \(Self.areaLegend.lowercased())" }
        if let gap = gapNote { s += ". " + gap }
        if let note = skippedNote { s += ". " + note }
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

    /// Dashboard drone colours; the light-mode variants are darker so labels reach 4.5:1 on white.
    private static let droneColors: [Color] = [
        dynamic(light: (0x03, 0x69, 0xA1), dark: (0x0E, 0xA5, 0xE9)),
        dynamic(light: (0xB4, 0x53, 0x09), dark: (0xF5, 0x9E, 0x0B)),
        dynamic(light: (0x6D, 0x28, 0xD9), dark: (0x8B, 0x5C, 0xF6)),
        dynamic(light: (0x04, 0x78, 0x57), dark: (0x10, 0xB9, 0x81)),
    ]
    /// The warning colour as text and strokes: darker than the theme's orange on light backgrounds.
    private static let warning = dynamic(light: (0xC2, 0x41, 0x0C), dark: (0xFB, 0x92, 0x3C))
    private static let lostGrey = dynamic(light: (0x52, 0x52, 0x5B), dark: (0xA1, 0xA1, 0xAA))

    private static func dynamic(light: (Int, Int, Int), dark: (Int, Int, Int)) -> Color {
        Color(UIColor { trait in
            let c = trait.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: CGFloat(c.0) / 255, green: CGFloat(c.1) / 255, blue: CGFloat(c.2) / 255, alpha: 1)
        })
    }
    private static let padding = 18.0
    private static let arrowBudget = 600

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(state.title).font(.headline)
                if state.isNotLive {
                    Text("not live").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let d = state.drawing, !state.isEmpty {
                Canvas { context, size in
                    Self.draw(d, in: &context, size: size)
                }
                .aspectRatio(Self.aspect(d.bounds), contentMode: .fit)
                .frame(maxWidth: .infinity, maxHeight: 420)
                .opacity(state.isNotLive ? 0.7 : 1)
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
            if !d.uncoveredLanes.isEmpty {
                Label(PlanGridViewState.uncoveredLegend, systemImage: "square.dashed")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Self.warning)
                    .accessibilityIdentifier("ground.plan.notSearched")
                ForEach(state.laneReasons, id: \.self) { Text($0).font(.caption2).foregroundStyle(.secondary) }
            }
            if !d.uncoveredAreas.isEmpty {
                Label(PlanGridViewState.areaLegend, systemImage: "square.dashed")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Self.warning)
                    .accessibilityIdentifier("ground.plan.mayNotBeCovered")
                ForEach(state.areaReasons, id: \.self) { Text($0).font(.caption2).foregroundStyle(.secondary) }
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
        if z.lost { return lostGrey }
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
                    context.fill(p, with: .color(lostGrey.opacity(0.15)))
                    context.stroke(p, with: .color(lostGrey), style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
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
