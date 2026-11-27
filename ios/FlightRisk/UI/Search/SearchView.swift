import MapKit
import SwiftUI

/// Main search screen with the drone video, detection overlay, HUD,
/// and match alerts. A search runs only while a drone is connected; there is no phone camera
/// mode.
///
/// Layer order (back to front):
///
///  1. Drone video (full screen background)
///  2. Detection overlay (bounding boxes)
///  3. HUD overlay (FPS, Persons, Match score pills)
///  4. Confidence progress bar (below HUD when building confidence)
///  5. Reasoning status banner (cloud active / fallback notice)
///  6. Match alert banner (slide-in from top)
///  7. Match alert card (centered, highest z-order)
///  8. Start/Stop Search button (bottom action area)
///  9. Disclaimer footer
/// 10. "Models not loaded" banner (when fps < 0.5 after 2s delay)
/// 11. "Person detection unavailable" banner (always, while the detection
///     model is missing or failed to load)
struct SearchView: View {

    /// The view model driving all search screen state.
    @Bindable var viewModel: SearchViewModel

    /// Whether the "models not loaded" banner should be shown.
    /// Delayed by 2 seconds after search starts to avoid flash.
    @State private var showModelsNotLoadedBanner = false

    var body: some View {
        ZStack {
            // ----- Layer 1: Drone video (full screen) -----
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
            // Not shown when layer 11 already explains that detection is off.
            if viewModel.detectionUnavailableReason == nil
                && viewModel.isSearching && showModelsNotLoadedBanner && viewModel.fps < 0.5 {
                VStack {
                    modelsNotLoadedBanner
                        .padding(.top, 100)
                    Spacer()
                }
            }

            // ----- Layer 11: Person detection unavailable -----
            // Shown whether or not a search is running, so the missing model is
            // never silent.
            // ----- Reasoning status: "Cloud active" indicator and fallback notice -----
            // Both banners share one stack, so they never overlap, however large the
            // Dynamic Type size is.
            VStack(spacing: 8) {
                if let reason = viewModel.detectionUnavailableReason {
                    DetectionUnavailableBanner(reason: reason)
                        .padding(.horizontal, 16)
                        .allowsHitTesting(false)
                }
                if viewModel.isUsingCustomDetector {
                    Text("Own detector")
                        .font(.caption2)
                        .fontWeight(.semibold)
                        .foregroundStyle(FlightRiskTheme.hudWhite)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.black.opacity(0.7), in: Capsule())
                        .accessibilityLabel("Using your own detector")
                        .allowsHitTesting(false)
                }
                ReasoningStatusBanner()
                    .allowsHitTesting(false)
                Spacer()
            }
            .padding(.top, 60)

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
        .onChange(of: viewModel.hasDrone) { _, connected in
            if !connected { viewModel.droneDidDisconnect() }
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

    /// The latest annotated drone frame, or a dark background that says what is missing.
    private var videoPreview: some View {
        Group {
            if let frame = viewModel.videoFrame {
                Image(decorative: frame, scale: 1.0)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .accessibilityLabel("Drone video")
            } else {
                Color(white: 0.07)
                    .overlay {
                        Label(viewModel.hasDrone ? "Waiting for video" : "No drone connected",
                              systemImage: "dot.radiowaves.left.and.right")
                            .foregroundStyle(.gray)
                    }
                    .accessibilityLabel(viewModel.hasDrone ? "Waiting for drone video" : "No drone connected")
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
        Text("AI models not loaded.")
            .font(.caption)
            .fontWeight(.medium)
            .foregroundStyle(Color(white: 0.8))
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(
                Capsule()
                    .fill(Color.black.opacity(0.8))
            )
            .accessibilityLabel("AI models not loaded")
    }

    // MARK: - Bottom Section (Layers 8-9)

    private var bottomSection: some View {
        VStack(spacing: 12) {
            if let message = viewModel.statusMessage {
                Text(message)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(FlightRiskTheme.hudWhite)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(Color.black.opacity(0.8), in: Capsule())
                    .accessibilityIdentifier("search.status")
            }

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
                Image(systemName: viewModel.isSearching ? "stop.fill" : "play.fill")
                    .font(.title3.weight(.semibold))

                Text(viewModel.isSearching ? "Stop" : "Start search")
                    .font(.headline)
                    .fontWeight(.bold)
            }
            .foregroundStyle(FlightRiskTheme.hudWhite)
            .frame(minWidth: 200, minHeight: 56)
            .background(
                Capsule()
                    .fill(viewModel.isSearching
                          ? FlightRiskTheme.alertRed
                          : Color.accentColor)
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(viewModel.isSearching ? "Stop search" : "Start search")
        .accessibilityIdentifier("search.startStop")
    }

    // MARK: - Disclaimer Footer (Layer 9)

    private var disclaimerFooter: some View {
        Text("Assistive tool. Check every match yourself.")
            .font(.caption2)
            .foregroundStyle(Color(white: 0.67))
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 8)
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

// MARK: - Detection Unavailable Banner (Layer 11)

/// A persistent warning that person detection is off, with the reason.
/// It cannot be dismissed: it goes away only when the model loads.
struct DetectionUnavailableBanner: View {
    let reason: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.headline)
                .foregroundStyle(.yellow)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text("Person detection unavailable")
                    .font(.subheadline)
                    .fontWeight(.bold)
                Text(reason)
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(FlightRiskTheme.hudWhite)
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color.black.opacity(0.85))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.yellow.opacity(0.8), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Warning: person detection unavailable. \(reason)")
    }
}
