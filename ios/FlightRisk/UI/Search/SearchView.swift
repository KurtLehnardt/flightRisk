import AVFoundation
import MapKit
import SwiftUI

/// Main search screen with camera preview, detection overlay, HUD,
/// match alerts, flight controls, and drone connection UI.
///
/// Mirrors the Android `SearchScreen` composable. Layer order (back to
/// front):
///
///  1. Camera or drone video preview (full screen background)
///  2. Detection overlay (bounding boxes)
///  3. HUD overlay (FPS, Persons, Match score pills)
///  4. Confidence progress bar (below HUD when building confidence)
///  5. Tello status badge (below HUD)
///  6. Battery warning pill (next to Tello badge)
///  7. Drone alert banner (red card, top area, with OK dismiss)
///  8. Drone connection card (centered when not streaming)
///  9. Match alert banner (slide-in from top)
/// 10. Match alert card (centered, highest z-order)
/// 11. Flight controls overlay (bottom, collapsible)
/// 12. Start/Stop Search button (bottom action area)
/// 13. Disclaimer footer
/// 14. "Models not loaded" banner (when fps < 0.5 after 2s delay)
struct SearchView: View {

    /// The view model driving all search screen state.
    @Bindable var viewModel: SearchViewModel

    /// The camera capture session for the live preview layer.
    var cameraSession: AVCaptureSession

    /// Whether the "models not loaded" banner should be shown.
    /// Delayed by 2 seconds after search starts to avoid flash.
    @State private var showModelsNotLoadedBanner = false

    /// Whether the flight controls overlay is expanded.
    @State private var controlsExpanded = true

    var body: some View {
        ZStack {
            // ----- Layer 1: Camera / drone preview (full screen) -----
            videoPreview
                .ignoresSafeArea()

            // ----- Layer 2: Detection overlay -----
            if !viewModel.boxes.isEmpty {
                DetectionOverlayView(
                    boxes: viewModel.boxes,
                    frameWidth: viewModel.frameWidth,
                    frameHeight: viewModel.frameHeight
                )
                .ignoresSafeArea()
            }

            // ----- Layers 3-7: Top overlays -----
            VStack(spacing: 0) {
                topOverlays
                Spacer()
            }

            // ----- Layer 14: Models not loaded banner -----
            if viewModel.isSearching && showModelsNotLoadedBanner && viewModel.fps < 0.5 {
                VStack {
                    modelsNotLoadedBanner
                        .padding(.top, 100)
                    Spacer()
                }
            }

            // ----- Layer 7: Drone alert banner -----
            if let droneAlert = viewModel.droneAlert {
                VStack {
                    Spacer().frame(height: 120)
                    droneAlertBanner(message: droneAlert)
                    Spacer()
                }
                .transition(.move(edge: .top).combined(with: .opacity))
                .animation(.spring(response: 0.4, dampingFraction: 0.7), value: viewModel.droneAlert)
            }

            // ----- Layers 8, 11-13: Bottom section -----
            VStack {
                Spacer()
                bottomSection
            }

            // ----- Layers 9-10: Match alert (HIGHEST Z) -----
            if viewModel.activeAlert != nil {
                matchAlertOverlay
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .animation(.spring(response: 0.4, dampingFraction: 0.7), value: viewModel.activeAlert?.id)
            }
        }
        .task(id: viewModel.isSearching) {
            // Delay the "models not loaded" banner by 2 seconds
            showModelsNotLoadedBanner = false
            if viewModel.isSearching {
                try? await Task.sleep(for: .seconds(2))
                showModelsNotLoadedBanner = true
            }
        }
        .onChange(of: viewModel.activeAlert?.id) { _, newValue in
            // Auto-collapse flight controls when match alert appears
            if newValue != nil {
                controlsExpanded = false
            }
        }
    }

    // MARK: - Video Preview (Layer 1)

    @ViewBuilder
    private var videoPreview: some View {
        switch viewModel.frameSourceMode {
        case .drone:
            droneVideoPreview

        case .camera:
            if viewModel.isSearching {
                CameraPreviewView(session: cameraSession)
            } else {
                cameraPreviewPlaceholder
            }
        }
    }

    /// Placeholder when the camera is not active, showing the latest
    /// frame or a dark background.
    private var cameraPreviewPlaceholder: some View {
        Group {
            if let frame = viewModel.cameraFrame {
                Image(decorative: frame, scale: 1.0)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .accessibilityLabel("Camera preview")
            } else {
                Color(white: 0.07)
                    .overlay {
                        Text("Camera Preview")
                            .foregroundStyle(.gray)
                    }
                    .accessibilityLabel("Camera preview inactive")
            }
        }
    }

    /// Drone video preview with state-dependent content.
    @ViewBuilder
    private var droneVideoPreview: some View {
        let connectionState = viewModel.droneState?.connectionState ?? .disconnected

        switch connectionState {
        case .streaming:
            if let frame = viewModel.cameraFrame ?? viewModel.latestDroneFrame {
                Image(decorative: frame, scale: 1.0)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .accessibilityLabel("Drone video streaming")
            } else {
                Color(white: 0.07)
                    .overlay {
                        Text("Waiting for video feed...")
                            .foregroundStyle(.gray)
                    }
            }

        case .connecting:
            Color(white: 0.07)
                .overlay {
                    VStack(spacing: 16) {
                        ProgressView()
                        Text("Connecting to drone...")
                            .foregroundStyle(.gray)
                    }
                }

        case .connected:
            Color(white: 0.07)
                .overlay {
                    Text("Waiting for video feed...")
                        .foregroundStyle(.gray)
                }

        default:
            cameraPreviewPlaceholder
        }
    }

    // MARK: - Top Overlays (Layers 3-6)

    private var topOverlays: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Layer 3: HUD overlay
            if viewModel.isSearching {
                HUDOverlayView(
                    fps: viewModel.fps,
                    personsDetected: viewModel.personsDetected,
                    matchScore: viewModel.highestMatchScore
                )

                // Layer 4: Confidence progress
                if viewModel.confidenceFrames > 0
                    && viewModel.confidenceFrames < viewModel.confidenceNeeded {
                    ConfidenceProgressView(
                        framesMatched: viewModel.confidenceFrames,
                        framesNeeded: viewModel.confidenceNeeded
                    )
                }
            }

            // Layer 5: Tello status badge
            if let droneState = viewModel.droneState,
               droneState.connectionState != .disconnected {
                HStack(spacing: 8) {
                    TelloStatusBadgeView(droneState: droneState)

                    // Layer 6: Battery warning pill
                    batteryWarningPill(for: droneState)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
    }

    /// Battery warning pill, shown when battery is critically low.
    @ViewBuilder
    private func batteryWarningPill(for droneState: TelloState) -> some View {
        let battery = droneState.telemetry.battery ?? 100

        if battery <= 10 {
            BatteryWarningPillView(
                text: "CRITICAL: \(battery)%",
                color: FlightRiskTheme.alertRed
            )
        } else if battery <= 20 {
            BatteryWarningPillView(
                text: "LOW: \(battery)%",
                color: FlightRiskTheme.alertOrange
            )
        }
    }

    // MARK: - Models Not Loaded Banner (Layer 14)

    private var modelsNotLoadedBanner: some View {
        Text("Camera active but AI models not loaded")
            .font(.caption)
            .fontWeight(.medium)
            .foregroundStyle(Color(white: 0.8))
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(Color.black.opacity(0.8))
            )
            .accessibilityLabel("Camera active but AI models not loaded")
    }

    // MARK: - Drone Alert Banner (Layer 7)

    private func droneAlertBanner(message: String) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Drone Alert")
                    .font(.subheadline)
                    .fontWeight(.bold)
                    .foregroundStyle(FlightRiskTheme.hudWhite)

                Text(message)
                    .font(.caption)
                    .foregroundStyle(FlightRiskTheme.hudWhite)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button("OK") {
                viewModel.droneAlert = nil
            }
            .fontWeight(.bold)
            .foregroundStyle(FlightRiskTheme.hudWhite)
            .frame(minHeight: 44)
            .accessibilityLabel("Dismiss drone alert")
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(red: 0.72, green: 0.11, blue: 0.11, opacity: 0.9))
        )
        .shadow(color: .black.opacity(0.3), radius: 8, y: 2)
        .padding(.horizontal, 16)
    }

    // MARK: - Bottom Section (Layers 8, 11-13)

    @ViewBuilder
    private var bottomSection: some View {
        if viewModel.droneState?.connectionState == .streaming {
            // Layer 11: Flight controls overlay
            FlightControlsOverlayView(
                isFlying: viewModel.droneState?.telemetry.isFlying ?? false,
                isSearching: viewModel.isSearching,
                isExpanded: $controlsExpanded,
                selectedPattern: viewModel.selectedPattern,
                onPatternSelected: { viewModel.selectedPattern = $0 },
                onLand: { /* DroneManager.land() */ },
                onMove: { direction, distance in /* DroneManager.move() */ },
                onRotate: { degrees in /* DroneManager.rotate() */ },
                onStartSearch: { viewModel.startSearch() },
                onStopSearch: { viewModel.stopSearch() },
                onEmergencyStop: { /* DroneManager.emergencyStop() */ }
            )
        } else {
            VStack(spacing: 12) {
                // Layer 8: Drone connection card
                DroneConnectionCardView(
                    droneState: viewModel.droneState,
                    droneConnectionMessage: viewModel.droneConnectionMessage,
                    onConnect: { /* DroneManager.connect() */ },
                    onDisconnect: { /* DroneManager.disconnect() */ }
                )

                // Layer 12: Start/Stop Search button
                startStopSearchButton

                // Layer 13: Disclaimer footer
                disclaimerFooter
            }
            .padding(.bottom, 16)
        }
    }

    // MARK: - Start/Stop Search Button (Layer 12)

    private var startStopSearchButton: some View {
        Button(action: { viewModel.isSearching ? viewModel.stopSearch() : viewModel.startSearch() }) {
            HStack(spacing: 8) {
                Image(systemName: viewModel.isSearching ? "xmark" : "play.fill")
                    .font(.title3)

                Text(viewModel.isSearching ? "Stop Search" : "Start Search")
                    .font(.headline)
                    .fontWeight(.bold)
            }
            .foregroundStyle(FlightRiskTheme.hudWhite)
            .frame(minWidth: 200, minHeight: 56)
        }
        .background(
            RoundedRectangle(cornerRadius: 28)
                .fill(viewModel.isSearching
                      ? FlightRiskTheme.alertRed
                      : Color.accentColor)
        )
        .accessibilityLabel(viewModel.isSearching ? "Stop search" : "Start search")
    }

    // MARK: - Disclaimer Footer (Layer 13)

    private var disclaimerFooter: some View {
        Text("FlightRisk is an assistive search tool. Always verify matches visually before approaching anyone.")
            .font(.caption2)
            .foregroundStyle(Color(white: 0.67))
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color.black.opacity(0.6))
            )
            .padding(.horizontal, 16)
    }

    // MARK: - Match Alert Overlay (Layers 9-10)

    @ViewBuilder
    private var matchAlertOverlay: some View {
        if let alert = viewModel.activeAlert {
            VStack(spacing: 8) {
                // Layer 9: Match alert banner
                MatchAlertBannerView(alertLevel: alert.alertLevel)

                // Layer 10: Match alert card
                MatchAlertCardView(
                    matchEntry: alert,
                    onDismiss: {
                        viewModel.dismissAlert()
                    },
                    onNotMyChild: {
                        viewModel.dismissAndSuppressTrack(trackKey: alert.trackId)
                    },
                    onNavigate: { latitude, longitude in
                        let coordinate = CLLocationCoordinate2D(
                            latitude: latitude,
                            longitude: longitude
                        )
                        let placemark = MKPlacemark(coordinate: coordinate)
                        let mapItem = MKMapItem(placemark: placemark)
                        mapItem.name = "Possible Match Location"
                        mapItem.openInMaps(launchOptions: [
                            MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeWalking,
                        ])
                    }
                )
            }
        }
    }
}

// MARK: - Battery Warning Pill

/// Small pill showing a battery warning or critical message.
private struct BatteryWarningPillView: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2)
            .fontWeight(.bold)
            .foregroundStyle(FlightRiskTheme.hudWhite)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(color)
            )
            .accessibilityLabel(text)
    }
}
