import SwiftUI

/// Slide-in banner displayed at the top of the screen when a match is
/// detected.
///
/// Mirrors the Android `MatchAlertBanner` composable:
/// - Confirmed match: dark red background, "POSSIBLE MATCH -- VERIFY IN PERSON"
/// - Possible match: orange background, "POSSIBLE MATCH -- VERIFY"
///
/// Animated with a spring slide-in from the top.
struct MatchAlertBannerView: View {

    /// One of `AlertManager.confirmedMatch`, `AlertManager.possibleMatch`,
    /// `AlertManager.weakSignal`.
    let alertLevel: String

    var body: some View {
        Text(bannerText)
            .font(.headline)
            .fontWeight(.heavy)
            .foregroundStyle(FlightRiskTheme.hudWhite)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
            .background(backgroundColor)
            .accessibilityAddTraits(.isHeader)
            .accessibilityLabel(bannerText)
    }

    // MARK: - Computed Properties

    private var isConfirmed: Bool {
        alertLevel == AlertManager.confirmedMatch
    }

    private var backgroundColor: Color {
        isConfirmed ? FlightRiskTheme.alertRedDark : FlightRiskTheme.alertOrange
    }

    private var bannerText: String {
        isConfirmed
            ? "POSSIBLE MATCH -- VERIFY IN PERSON"
            : "POSSIBLE MATCH -- VERIFY"
    }
}
