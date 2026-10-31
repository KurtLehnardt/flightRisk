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
/// once. The key matches the one read by ``ContentView``. Acknowledging the
/// privacy notice is stored separately (see ``PrivacyNotice``), so a person who
/// accepted the notice but quit before finishing setup is not asked again.
struct OnboardingView: View {
    @AppStorage("flightrisk_onboarding_complete") private var onboardingComplete = false
    @State private var currentStep = PrivacyNotice.isAcknowledged() ? 1 : 0
    @State private var selectedImage: CGImage?
    @State private var qualityReport: QualityReport?

    var onPhotoSelected: ((CGImage, QualityReport?) -> Void)?

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
                        description: "Person detection and matching run on your device. Camera frames are not uploaded or stored."
                    )
                    PrivacyBulletView(
                        title: "Ground Station (Optional)",
                        description: PrivacyNotice.groundLinkText
                    )
                    PrivacyBulletView(
                        title: "Cloud Reasoning (Off by Default)",
                        description: PrivacyNotice.cloudText
                    )
                    PrivacyBulletView(
                        title: "Location Data",
                        description: "GPS coordinates are attached to match alerts so you can navigate to the location. Location data stays on your device."
                    )
                    PrivacyBulletView(
                        title: "No Analytics or Accounts",
                        description: "FlightRisk does not collect analytics or telemetry. There is no account or sign-in."
                    )
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Privacy Policy")
                            .font(.subheadline)
                            .fontWeight(.bold)
                        Link("Read the full privacy policy", destination: PrivacyNotice.policyURL)
                            .font(.subheadline)
                    }
                }
                .padding(20)
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 12))

                Spacer().frame(height: 8)

                Button {
                    PrivacyNotice.acknowledge()
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
            if let image = selectedImage {
                onPhotoSelected?(image, qualityReport)
            }
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

// MARK: - PrivacyNotice

/// The first-launch privacy notice: its text and the acknowledgement keys.
///
/// Two keys exist. `flightrisk_onboarding_complete` is the original key,
/// written when the whole onboarding flow finishes. `flightrisk_privacy_notice_acknowledged`
/// is the key named in `docs/first-launch-notice.md`, written when the person
/// taps "I Understand".
///
/// Decision for people who finished onboarding under the old notice: they are
/// not asked again. The notice changed in two ways, and neither widens what an
/// existing install does. The ground station link is off until the person
/// types an address in Settings, and that screen states where data goes. The
/// cloud wording became stricter and more complete, and cloud reasoning now
/// needs the opt-in on top of a key, so it can only send less than before.
/// Their old acknowledgement is copied to the new key by
/// ``migrateLegacyAcknowledgement(defaults:)`` so it is never lost. A person
/// who has neither key is shown the notice.
enum PrivacyNotice {
    static let acknowledgedKey = "flightrisk_privacy_notice_acknowledged"
    static let legacyOnboardingCompleteKey = "flightrisk_onboarding_complete"

    /// Stand-in location of the policy until a hosted page exists. It is the
    /// public repository copy of `docs/privacy-policy.md`.
    static let policyURL = URL(string: "https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md")!

    /// The ground station bullet.
    static let groundLinkText =
        "If you add a ground station address in Settings, the app connects over your local network to that address only, "
        + "to show drone status and send land commands. A ws:// or http:// link is not encrypted, so use a network you trust. "
        + "The station's key is kept in the iOS Keychain."

    /// The cloud bullet. It matches the consent text in Settings
    /// (`SettingsView.cloudConsentMessage`).
    static let cloudText =
        "Cloud reasoning is off. If you turn it on in Settings, save a Claude API key and select Cloud Claude, "
        + "the cropped image of each detected person, the reference photo and the text description you entered are sent to Anthropic. "
        + "The images can show a face. You can turn it off at any time."

    /// True when either key says the person already accepted a notice.
    static func isAcknowledged(defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: acknowledgedKey) || defaults.bool(forKey: legacyOnboardingCompleteKey)
    }

    /// Record the tap on "I Understand". The legacy key is written only when
    /// onboarding finishes, so the camera screens still follow the photo step.
    static func acknowledge(defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: acknowledgedKey)
    }

    /// Copy an old acknowledgement to the new key. Never clears anything.
    static func migrateLegacyAcknowledgement(defaults: UserDefaults = .standard) {
        if defaults.bool(forKey: legacyOnboardingCompleteKey), !defaults.bool(forKey: acknowledgedKey) {
            defaults.set(true, forKey: acknowledgedKey)
        }
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
