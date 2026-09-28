import XCTest
@testable import BigFishCore

final class InteractionPhysicsTests: XCTestCase {
    func testHitRegionsMatchCharacterZones() {
        XCTAssertEqual(PetInteractionTracker.classifyRegion(x: 0.5, y: 0.3), .face)
        XCTAssertEqual(PetInteractionTracker.classifyRegion(x: 0.5, y: 0.08), .head)
        XCTAssertEqual(PetInteractionTracker.classifyRegion(x: 0.9, y: 0.65), .tail)
        XCTAssertEqual(PetInteractionTracker.classifyRegion(x: 0.5, y: 0.72), .body)
    }

    func testDragVelocityUsesOnlyRecentMotion() {
        let tracker = PetInteractionTracker()
        tracker.press(x: 0.5, y: 0.5, sampleX: 0, sampleY: 0, now: 1)
        _ = tracker.move(x: 5, y: 2, now: 1.1)
        let release = tracker.release(x: 0.5, y: 0.5, sampleX: 20, sampleY: 10, now: 1.2)
        XCTAssertTrue(release!.wasDrag)
        XCTAssertEqual(release!.velocityX, 100, accuracy: 0.01)
        XCTAssertEqual(release!.velocityY, 50, accuracy: 0.01)
    }

    func testPhysicsSettlesOnFloor() {
        let physics = PetThrowPhysics()
        physics.launch(x: 100, y: 10, velocityX: 0, velocityY: 100)
        let bounds = PetPhysicsBounds(left: 0, top: 0, right: 500, bottom: 360, platforms: [])
        var settled = false
        for _ in 0..<240 {
            let step = physics.step(1.0 / 60.0, width: 80, height: 100, bounds: bounds)
            settled = settled || step.settled
            if settled { break }
        }
        XCTAssertTrue(settled)
        XCTAssertFalse(physics.active)
    }

    func testPlatformCatchesDescendingPet() {
        let physics = PetThrowPhysics()
        physics.launch(x: 120, y: 10, velocityX: 0, velocityY: 80)
        let platform = PetPhysicsPlatform(left: 100, right: 300, top: 180, identifier: "front-window")
        let bounds = PetPhysicsBounds(left: 0, top: 0, right: 500, bottom: 400, platforms: [platform])
        var landed = false
        for _ in 0..<180 {
            let step = physics.step(1.0 / 60.0, width: 80, height: 80, bounds: bounds)
            if step.surfaceID == "front-window" { landed = true; break }
        }
        XCTAssertTrue(landed)
    }
}
