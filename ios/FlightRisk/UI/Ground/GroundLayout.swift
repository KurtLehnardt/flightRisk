import CoreGraphics

/// The height budget of the Ground tab. The pinned top bar must never push the drone list (and
/// every per-drone Land button in it) off the screen, so the scrolling text area of the bar is
/// limited and the list always keeps a guaranteed minimum. It mirrors the Android
/// `GroundLayout`. All values are points.
enum GroundLayout {
    /// The share of the height the bar's text area may take.
    static let feedbackShare: CGFloat = 0.35

    /// The part of the bar that never scrolls: the pill row, the LAND ALL button, the Retry and
    /// Dismiss buttons and the padding.
    static let fixedBarHeight: CGFloat = 160

    /// The least height the drone list keeps, enough for one card with its Land button.
    static let minListHeight: CGFloat = 180

    /// The least height the text area keeps, so a short message is still readable.
    static let minFeedbackHeight: CGFloat = 48

    /// The largest height the bar's scrolling text area may have on a screen `available` points
    /// high: 35% of the height, and never so much that the list drops below `minListHeight`. On
    /// a screen too small for both, the text area keeps `minFeedbackHeight`.
    static func feedbackMaxHeight(available: CGFloat) -> CGFloat {
        guard available.isFinite, available > 0 else { return minFeedbackHeight }
        let share = available * feedbackShare
        let leaveForList = available - fixedBarHeight - minListHeight
        return max(minFeedbackHeight, min(share, leaveForList))
    }

    /// The height the list keeps when the text area is full.
    static func listHeight(available: CGFloat) -> CGFloat {
        available - fixedBarHeight - feedbackMaxHeight(available: available)
    }
}
