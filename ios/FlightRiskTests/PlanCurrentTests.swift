import Foundation
import SocketIOMini
import XCTest
@testable import FlightRisk

/// A plan kept from before a reconnect must not read as the live plan (U9 review round).
@MainActor
final class PlanCurrentTests: XCTestCase {
    private var harness: GroundHarness!
    private var vm: GroundViewModel { harness.viewModel }

    private let plan = #"{"grid":{"x_cm":0,"y_cm":0,"w_cm":400,"h_cm":400},"zones":[{"drone_id":"drone-1","x_cm":0,"y_cm":0,"w_cm":150,"h_cm":400,"lanes":[{"direction":"forward","distance_cm":400}]}],"uncovered":[]}"#

    override func setUp() async throws {
        try await super.setUp()
        harness = GroundHarness()
    }

    override func tearDown() async throws {
        await harness.tearDown()
        harness = nil
        try await super.tearDown()
    }

    private var label: String { PlanGridViewState(plan: vm.fleetPlan, isStale: vm.isStateStale, planIsCurrent: vm.planIsCurrent).title }

    func testPlanIsCurrentOnlyAfterAPlanArrivesThisSession() async {
        let t1 = await harness.connect()
        XCTAssertFalse(vm.planIsCurrent, "a fresh connection has no confirmed plan")
        await harness.push(t1, "fleet_plan", plan) { self.vm.fleetPlan != nil }
        XCTAssertTrue(vm.planIsCurrent)
        await harness.push(t1, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        XCTAssertEqual(label, "Search plan")

        // Reconnect; the ground has no plan now and sends none.
        t1.serverDrop()
        await harness.waitUntil("reconnecting") { !self.vm.linkStatus.isConnected }
        XCTAssertFalse(vm.planIsCurrent)
        let before = harness.recorder.transports.count
        vm.retryConnection()
        let t2 = await harness.handshake(after: before)
        await harness.push(t2, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        XCTAssertNotNil(vm.fleetPlan, "the old plan stays visible")
        XCTAssertFalse(vm.planIsCurrent)
        XCTAssertEqual(label, "Last plan (not confirmed this session)")

        // A new fleet_plan clears it.
        await harness.push(t2, "fleet_plan", plan) { self.vm.planIsCurrent }
        XCTAssertEqual(label, "Search plan")
    }

    func testDroppedEventsUnconfirmThePlan() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_plan", plan) { self.vm.planIsCurrent }
        vm.handle(SocketIOMini.GroundEvent.eventsDropped(count: 3))
        XCTAssertFalse(vm.planIsCurrent)
        await harness.push(t, "fleet_plan", plan) { self.vm.planIsCurrent }
    }
}
