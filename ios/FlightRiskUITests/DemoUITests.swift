import XCTest

/// The demo as an App Store reviewer meets it, from the first onboarding screen.
///
/// The app can send LAND ALL to real drones, so in the demo LAND ALL must stay visible and
/// tappable, never covered by the demo banner, at the default text size and at the largest
/// accessibility text size; and it must read as the demo's, never as a live LAND ALL.
///
/// The screenshots are kept in the test results (`.xcresult`), for review. Run with:
/// `xcodebuild test -project FlightRisk.xcodeproj -scheme FlightRisk -only-testing:FlightRiskUITests
/// -destination "platform=iOS Simulator,name=iPhone 17"`.
final class DemoUITests: XCTestCase {
    /// The largest text size a user can choose (Settings > Accessibility > Larger Text).
    private static let largestAccessibilitySize = "UICTContentSizeCategoryAccessibilityXXXL"

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    private func launch(size: String? = nil, privacyAcknowledged: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += [
            "-flightrisk_onboarding_complete", "NO",
            "-flightrisk_privacy_notice_acknowledged", privacyAcknowledged ? "YES" : "NO",
        ]
        if let size { app.launchArguments += ["-UIPreferredContentSizeCategoryName", size] }
        app.launch()
        return app
    }

    private func shot(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// Starts the demo from onboarding and checks LAND ALL on the Ground tab.
    private func checkLandAllInTheDemo(size: String?) throws {
        let label = size ?? "default"
        let app = launch(size: size)
        let tryDemo = app.buttons["onboarding.tryDemo"]
        XCTAssertTrue(tryDemo.waitForExistence(timeout: 30))
        XCTAssertTrue(tryDemo.isHittable, "Try the demo is at the top of the privacy notice")
        shot(app, "onboarding-\(label)")
        tryDemo.tap()

        let banner = app.staticTexts["demo.banner"]
        XCTAssertTrue(banner.waitForExistence(timeout: 10))
        let landAll = app.buttons["ground.landAll"]
        XCTAssertTrue(landAll.waitForExistence(timeout: 10))
        XCTAssertTrue(landAll.isHittable, "LAND ALL is covered")
        XCTAssertEqual(landAll.label, "LAND ALL (demo)")
        let exit = app.buttons["demo.exit"]
        XCTAssertTrue(exit.isHittable)
        XCTAssertFalse(landAll.frame.intersects(exit.frame), "the banner overlaps LAND ALL")
        XCTAssertLessThanOrEqual(banner.frame.maxY, landAll.frame.minY, "the banner is above LAND ALL, not over it")

        // The simulated drones take off at 5 s and fly their lanes.
        sleep(8)
        XCTAssertTrue(landAll.isHittable, "LAND ALL is covered once the drones fly")
        shot(app, "ground-demo-\(label)")

        landAll.tap()
        let result = app.staticTexts["ground.landAllResult"]
        XCTAssertTrue(result.waitForExistence(timeout: 5), "the tap reached LAND ALL")
        XCTAssertTrue(result.label.hasPrefix("DEMO: LAND ALL"), result.label)
        // The result is on screen, not scrolled out of the bar's text area, at every text size.
        XCTAssertTrue(result.isHittable, "the LAND ALL result is not visible")
        XCTAssertTrue(landAll.isHittable)
        sleep(2)
        shot(app, "ground-demo-after-land-all-\(label)")

        // The Target tab offers no photo picker and no camera while the demo runs.
        let target = app.tabBars.buttons["Target"]
        if target.exists { target.tap() } else { app.buttons["Target"].firstMatch.tap() }
        XCTAssertTrue(app.staticTexts["Target (demo)"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Select Photo"].exists)
        XCTAssertFalse(app.buttons["Take Photo"].exists)
        XCTAssertTrue(banner.exists)
        shot(app, "target-demo-\(label)")

        exit.tap()
        XCTAssertTrue(app.buttons["onboarding.tryDemo"].waitForExistence(timeout: 10), "back to onboarding")
        XCTAssertFalse(banner.exists)
    }

    func testLandAllIsHittableInTheDemoAtTheDefaultTextSize() throws {
        try checkLandAllInTheDemo(size: nil)
    }

    func testLandAllIsHittableInTheDemoAtTheLargestAccessibilityTextSize() throws {
        try checkLandAllInTheDemo(size: Self.largestAccessibilitySize)
    }

    /// "Try the demo" is on every onboarding step, not only the first.
    func testTryTheDemoIsOnALaterOnboardingStep() throws {
        let app = launch(privacyAcknowledged: true)
        XCTAssertTrue(app.staticTexts["Important"].waitForExistence(timeout: 30), "the emergency step")
        let tryDemo = app.buttons["onboarding.tryDemo"]
        XCTAssertTrue(tryDemo.exists)
        XCTAssertTrue(tryDemo.isHittable)
        app.buttons["Continue to FlightRisk"].tap()
        XCTAssertTrue(tryDemo.waitForExistence(timeout: 5))
        XCTAssertTrue(tryDemo.isHittable)
    }
}
