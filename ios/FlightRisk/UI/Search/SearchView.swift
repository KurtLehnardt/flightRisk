import AVFoundation
import MapKit
import SwiftUI

/// Main search screen with camera preview, detection overlay, HUD,
/// and match alerts.
///
/// Mirrors the Android `SearchScreen` composable. Layer order (back to
/// front):
///
///  1. Camera preview (full screen background)
///  2. Detection overlay (bounding boxes)
///  3. HUD overlay (FPS, Persons, Match score pills)
///  4. Confidence progress bar (below HUD when building confidence)
///  5. Reasoning status banner (cloud active / fallback notice)
///  6. Match alert banner (slide-in from top)
///  7. Match alert card (centered, highest z-order)
///  8. Start/Stop Search button (bottom action area)
///  9. Disclaimer footer
/// 10. "Models not loaded" banner (when fps < 0.5 after 2s delay)
struct SearchView: View {

    /// The view model driving all search screen state.
    @Bindable var viewModel: SearchViewModel

    /// The camera capture session for the live preview layer.
    var cameraSession: AVCaptureSession

    /// Whether the "models not loaded" banner should be shown.
    /// Delayed by 2 seconds after search starts to avoid flash.
    @State private var showModelsNotLoadedBanner = false

    var body: some View {
        ZStack {
            // ----- Layer 1: Camera preview (full screen) -----
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

            // ----- Layers 3-4: Top overlays -----
            VStack(spacing: 0) {
                topOverlays
                Spacer()
            }

            // ----- Layer 10: Models not loaded banner -----
            if viewModel.isSearching && showModelsNotLoadedBanner && viewModel.fps < 0.5 {
                VStack {
                    modelsNotLoadedBanner
                        .padding(.top, 100)
                    Spacer()
                }
            }

            // ----- Reasoning status: "Cloud active" indicator and fallback notice -----
            VStack {
                Spacer().frame(height: 60)
                ReasoningStatusBanner()
                Spacer()
            }
            .allowsHitTesting(false)

            // ----- Layers 8-9: Bottom section -----
            VStack {
                Spacer()
                bottomSection
            }

            // ----- Layers 6-7: Match alert (HIGHEST Z) -----
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
    }

    // MARK: - Video Preview (Layer 1)

    @ViewBuilder
    private var videoPreview: some View {
        if viewModel.isSearching {
            CameraPreviewView(session: cameraSession)
        } else {
            cameraPreviewPlaceholder
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

    // MARK: - Top Overlays (Layers 3-4)

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

        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
    }

    // MARK: - Models Not Loaded Banner (Layer 10)

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

    // MARK: - Bottom Section (Layers 8-9)

    private var bottomSection: some View {
        VStack(spacing: 12) {
            // Layer 8: Start/Stop Search button
            startStopSearchButton

            // Layer 9: Disclaimer footer
            disclaimerFooter
        }
        .padding(.bottom, 16)
    }

    // MARK: - Start/Stop Search Button (Layer 8)

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
            .background(
                RoundedRectangle(cornerRadius: 28)
                    .fill(viewModel.isSearching
                          ? FlightRiskTheme.alertRed
                          : Color.accentColor)
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(viewModel.isSearching ? "Stop search" : "Start search")
    }

    // MARK: - Disclaimer Footer (Layer 9)

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

    // MARK: - Match Alert Overlay (Layers 6-7)

    @ViewBuilder
    private var matchAlertOverlay: some View {
        if let alert = viewModel.activeAlert {
            VStack(spacing: 8) {
                // Layer 6: Match alert banner
                MatchAlertBannerView(alertLevel: alert.alertLevel)

                // Layer 7: Match alert card
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
