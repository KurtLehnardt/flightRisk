import SwiftUI

/// First-launch onboarding flow.
///
/// Five steps with animated transitions:
/// 0. Privacy notice (camera access, on-device processing, etc.)
/// 1. Emergency callout (911)
/// 2. "My child is missing" CTA
/// 3. Photo selection (inline ``TargetPickerView``)
/// 4. Quality check with retry
///
/// Persists completion via `@AppStorage` so the flow is only shown
/// once. The key matches the one read by ``ContentView``.
struct OnboardingView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false
    @State private var currentStep = 0
    @State private var selectedImage: CGImage?
    @State private var qualityReport: QualityReport?

    var body: some View {
        ZStack {
            // Animated step transitions
            Group {
                switch currentStep {
                case 0:
                    privacyNoticeStep
                        .transition(.asymmetric(
                            insertion: .move(edge: .trailing).combined(with: .opacity),
                            removal: .move(edge: .leading).combined(with: .opacity)
                        ))
                case 1:
                    emergencyCalloutStep
                        .transition(.asymmetric(
                            insertion: .move(edge: .trailing).combined(with: .opacity),
                            removal: .move(edge: .leading).combined(with: .opacity)
                        ))
                case 2:
                    missingChildStep
                        .transition(.asymmetric(
                            insertion: .move(edge: .trailing).combined(with: .opacity),
                            removal: .move(edge: .leading).combined(with: .opacity)
                        ))
                case 3:
                    photoSelectionStep
                        .transition(.asymmetric(
                            insertion: .move(edge: .trailing).combined(with: .opacity),
                            removal: .move(edge: .leading).combined(with: .opacity)
                        ))
                case 4:
                    qualityCheckStep
                        .transition(.asymmetric(
                            insertion: .move(edge: .trailing).combined(with: .opacity),
                            removal: .move(edge: .leading).combined(with: .opacity)
                        ))
                default:
                    EmptyView()
                }
            }
            .animation(.easeInOut(duration: 0.35), value: currentStep)
        }
    }

    // MARK: - Step 0: Privacy Notice

    @ViewBuilder
    private var privacyNoticeStep: some View {
        ScrollView {
            VStack(spacing: 24) {
                Spacer().frame(height: 16)

                Text("Privacy Notice")
                    .font(.title)
                    .fontWeight(.bold)
                    .accessibilityAddTraits(.isHeader)

                VStack(alignment: .leading, spacing: 16) {
                    PrivacyBulletView(
                        title: "Camera Access",
                        description: "FlightRisk uses your camera to scan for people who match the target photo you provide."
                    )
                    PrivacyBulletView(
                        title: "On-Device Processing",
                        description: "All person detection and matching runs entirely on your device. Camera frames are never uploaded or stored."
                    )
                    PrivacyBulletView(
                        title: "Optional Cloud Features",
                        description: "If you enable Cloud Claude, match snapshots may be sent to Anthropic's API for reasoning verification. This is optional and can be disabled in Settings."
                    )
                    PrivacyBulletView(
                        title: "Location Data",
                        description: "GPS coordinates are attached to match alerts so you can navigate to the location. Location data stays on your device."
                    )
                    PrivacyBulletView(
                        title: "No Data Collection",
                        description: "FlightRisk does not collect analytics, telemetry, or personal data. There is no account or sign-in."
                    )
                }
                .padding(20)
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 12))

                Spacer().frame(height: 8)

                Button {
                    withAnimation { currentStep = 1 }
                } label: {
                    Text("I Understand")
                        .font(.headline)
                        .fontWeight(.bold)
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("I understand the privacy notice")
            }
            .padding(24)
        }
    }

    // MARK: - Step 1: Emergency Callout

    @ViewBuilder
    private var emergencyCalloutStep: some View {
        VStack(spacing: 0) {
            Spacer()

            Text("Important")
                .font(.title)
                .fontWeight(.bold)
                .foregroundStyle(FlightRiskTheme.alertRed)
                .accessibilityAddTraits(.isHeader)

            Spacer().frame(height: 24)

            Text("If your child is in immediate danger, call 911 first.")
                .font(.title3)
                .fontWeight(.bold)
                .multilineTextAlignment(.center)

            Spacer().frame(height: 32)

            // Large red 911 circle button
            Link(destination: URL(string: "tel://911")!) {
                Text("911")
                    .font(.system(size: 36, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                    .frame(width: 120, height: 120)
                    .background(FlightRiskTheme.alertRed)
                    .clipShape(Circle())
            }
            .accessibilityLabel("Call 911 emergency services")

            Spacer().frame(height: 48)

            Button {
                withAnimation { currentStep = 2 }
            } label: {
                Text("Continue to FlightRisk")
                    .font(.headline)
                    .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.bordered)

            Spacer()
        }
        .padding(24)
    }

    // MARK: - Step 2: Missing Child CTA

    @ViewBuilder
    private var missingChildStep: some View {
        VStack(spacing: 0) {
            Spacer()

            Text("FlightRisk")
                .font(.largeTitle)
                .fontWeight(.bold)
                .accessibilityAddTraits(.isHeader)

            Spacer().frame(height: 8)

            Text("AI-powered lost child finder")
                .font(.body)
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                withAnimation { currentStep = 3 }
            } label: {
                Text("My child is missing")
                    .font(.title3)
                    .fontWeight(.bold)
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity, minHeight: 72)
                    .background(Color.accentColor)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
            }
            .accessibilityLabel("My child is missing, begin setup")

            Spacer().frame(height: 0)
                .frame(maxHeight: .infinity)
                .layoutPriority(-1)
        }
        .padding(24)
    }

    // MARK: - Step 3: Photo Selection

    @ViewBuilder
    private var photoSelectionStep: some View {
        TargetPickerView { image, report in
            selectedImage = image
            qualityReport = report
            withAnimation { currentStep = 4 }
        }
    }

    // MARK: - Step 4: Quality Check

    @ViewBuilder
    private var qualityCheckStep: some View {
        VStack(spacing: 24) {
            Spacer()

            if qualityReport?.isAcceptable == true {
                acceptedQualityContent
            } else {
                rejectedQualityContent
            }

            Spacer()
        }
        .padding(24)
    }

    // MARK: Quality Check - Accepted

    @ViewBuilder
    private var acceptedQualityContent: some View {
        Text("Photo accepted")
            .font(.title2)
            .fontWeight(.bold)
            .accessibilityAddTraits(.isHeader)

        if let report = qualityReport {
            HStack(spacing: 8) {
                GradeBadgeView(grade: report.grade)
                Text("\(Int(report.overallScore * 100))%")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
        }

        if let image = selectedImage {
            PhotoPreviewView(
                image: image,
                qualityReport: qualityReport,
                previewSize: 200
            )
        }

        Button {
            onboardingComplete = true
        } label: {
            Text("Start Searching")
                .font(.headline)
                .fontWeight(.bold)
                .frame(maxWidth: .infinity, minHeight: 56)
        }
        .buttonStyle(.borderedProminent)
        .accessibilityLabel("Start searching for your child")
    }

    // MARK: Quality Check - Rejected

    @ViewBuilder
    private var rejectedQualityContent: some View {
        Text("Photo quality is too low")
            .font(.title2)
            .fontWeight(.bold)
            .foregroundStyle(FlightRiskTheme.alertRed)
            .accessibilityAddTraits(.isHeader)

        if let report = qualityReport {
            // Issue list
            VStack(alignment: .leading, spacing: 4) {
                ForEach(report.issues, id: \.self) { issue in
                    Label(issue, systemImage: "exclamationmark.triangle")
                        .font(.subheadline)
                        .foregroundStyle(FlightRiskTheme.alertRed)
                }
            }

            // Suggestions
            if !report.suggestions.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(report.suggestions, id: \.self) { suggestion in
                        Text("- \(suggestion)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }

        if let image = selectedImage {
            PhotoPreviewView(
                image: image,
                qualityReport: qualityReport,
                previewSize: 180
            )
        }

        Button {
            withAnimation { currentStep = 3 }
        } label: {
            Text("Try Another Photo")
                .font(.headline)
                .fontWeight(.bold)
                .frame(maxWidth: .infinity, minHeight: 56)
        }
        .buttonStyle(.borderedProminent)
        .accessibilityLabel("Go back and try another photo")
    }
}

// MARK: - PrivacyBulletView

/// A single privacy notice bullet with title and description.
private struct PrivacyBulletView: View {
    let title: String
    let description: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.subheadline)
                .fontWeight(.bold)
            Text(description)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }
}
