import Foundation

/// Interaction and throw primitives ported from ds-local-pet.  They are kept
/// independent from AppKit so hit testing and physics remain deterministic.
enum PetInteractionRegion: String, Equatable {
    case face, head, body, tail
}

struct DragSample {
    let time: TimeInterval
    let x: Double
    let y: Double
}

struct DragRelease {
    let region: PetInteractionRegion
    let heldSeconds: TimeInterval
    let velocityX: Double
    let velocityY: Double
    let wasDrag: Bool
}

final class PetInteractionTracker {
    private(set) var region: PetInteractionRegion = .body
    private(set) var pressedAt: TimeInterval = 0
    private(set) var dragging = false
    private var pressX = 0.0
    private var pressY = 0.0
    private var samples: [DragSample] = []

    var active: Bool { pressedAt > 0 }
    var heldSeconds: TimeInterval {
        active ? max(0.0, Date.timeIntervalSinceReferenceDate - pressedAt) : 0
    }

    static func classifyRegion(x: Double, y: Double) -> PetInteractionRegion {
        guard x >= 0, x <= 1, y >= 0, y <= 1 else { return .body }
        if x >= 0.27 && x <= 0.73 && y >= 0.20 && y <= 0.43 { return .face }
        if x >= 0.14 && x <= 0.86 && y >= 0.02 && y <= 0.40 { return .head }
        if (x >= 0.72 || x <= 0.24) && y >= 0.45 && y <= 0.88 { return .tail }
        return .body
    }

    func press(x: Double, y: Double, sampleX: Double? = nil, sampleY: Double? = nil, now: TimeInterval = Date.timeIntervalSinceReferenceDate) {
        region = Self.classifyRegion(x: x, y: y)
        pressedAt = now
        pressX = sampleX ?? x
        pressY = sampleY ?? y
        dragging = false
        samples = [DragSample(time: now, x: pressX, y: pressY)]
    }

    @discardableResult
    func move(x: Double, y: Double, now: TimeInterval = Date.timeIntervalSinceReferenceDate) -> Bool {
        guard active else { return false }
        samples.append(DragSample(time: now, x: x, y: y))
        samples = Array(samples.suffix(18))
        if !dragging && abs(x - pressX) + abs(y - pressY) > 6 { dragging = true }
        return dragging
    }

    func release(x: Double, y: Double, sampleX: Double? = nil, sampleY: Double? = nil, now: TimeInterval = Date.timeIntervalSinceReferenceDate) -> DragRelease? {
        guard active else { return nil }
        _ = move(x: sampleX ?? x, y: sampleY ?? y, now: now)
        let result = DragRelease(
            region: region,
            heldSeconds: max(0.0, now - pressedAt),
            velocityX: velocity(now: now).0,
            velocityY: velocity(now: now).1,
            wasDrag: dragging
        )
        cancel()
        return result
    }

    func cancel() {
        pressedAt = 0
        dragging = false
        samples.removeAll(keepingCapacity: true)
    }

    private func velocity(now: TimeInterval) -> (Double, Double) {
        guard samples.count >= 2 else { return (0, 0) }
        let recent = samples.filter { $0.time >= now - 0.16 }
        guard let first = recent.first, let last = recent.last else { return (0, 0) }
        let dt = last.time - first.time
        guard dt >= 0.018 else { return (0, 0) }
        return ((last.x - first.x) / dt, (last.y - first.y) / dt)
    }
}

struct PetPhysicsPlatform {
    let left: Double
    let right: Double
    let top: Double
    let identifier: String
}

struct PetPhysicsBounds {
    let left: Double
    let top: Double
    let right: Double
    let bottom: Double
    let platforms: [PetPhysicsPlatform]
}

struct PetPhysicsStep {
    let x: Double
    let y: Double
    let velocityX: Double
    let velocityY: Double
    let firstImpact: Bool
    let settled: Bool
    let hardLanding: Bool
    let impactSpeed: Double
    let surfaceID: String?
}

final class PetThrowPhysics {
    var active = false
    private(set) var x = 0.0
    private(set) var y = 0.0
    private(set) var velocityX = 0.0
    private(set) var velocityY = 0.0
    private let gravity = 2350.0
    private let maxLaunchSpeed = 1850.0
    private let wallRestitution = 0.34
    private let ceilingRestitution = 0.24
    private let floorRestitution = 0.18
    private let hardImpactSpeed = 980.0
    private let bounceImpactSpeed = 360.0
    private var bounceCount = 0
    private var impacted = false
    private var largestImpact = 0.0
    private var hard = false
    private var surfaceID: String?

    func launch(x: Double, y: Double, velocityX: Double, velocityY: Double) {
        let speed = hypot(velocityX, velocityY)
        let factor = speed > maxLaunchSpeed ? maxLaunchSpeed / speed : 1
        self.x = x
        self.y = y
        self.velocityX = velocityX * factor
        self.velocityY = velocityY * factor
        bounceCount = 0
        impacted = false
        largestImpact = 0
        hard = false
        surfaceID = nil
        active = true
    }

    func cancel() {
        active = false
        velocityX = 0
        velocityY = 0
    }

    func step(_ elapsed: TimeInterval, width: Double, height: Double, bounds: PetPhysicsBounds) -> PetPhysicsStep {
        guard active else { return result(firstImpact: false, settled: false) }
        var remaining = min(0.10, max(0.0, elapsed))
        let minX = bounds.left
        let maxX = max(minX, bounds.right - width + 1)
        let minY = bounds.top
        let maxY = max(minY, bounds.bottom - height + 1)
        var firstImpact = false
        var settled = false
        while remaining > 0.0000001 && active {
            let dt = min(remaining, 1.0 / 120.0)
            remaining -= dt
            velocityY += gravity * dt
            let previousY = y
            x += velocityX * dt
            y += velocityY * dt
            if x < minX { x = minX; velocityX = abs(velocityX) * wallRestitution }
            if x > maxX { x = maxX; velocityX = -abs(velocityX) * wallRestitution }
            if y < minY { y = minY; velocityY = abs(velocityY) * ceilingRestitution }
            var landingY = maxY
            var landedSurface: PetPhysicsPlatform?
            for platform in bounds.platforms {
                let previousBottom = previousY + height
                let currentBottom = y + height
                let overlap = max(0.0, min(x + width, platform.right) - max(x, platform.left))
                if previousBottom <= platform.top + 0.5 && currentBottom >= platform.top && overlap >= min(36.0, width * 0.22) {
                    if landedSurface == nil || platform.top < landedSurface!.top { landedSurface = platform }
                }
            }
            if let platform = landedSurface { landingY = platform.top - height; surfaceID = platform.identifier }
            if landedSurface != nil || y >= maxY {
                y = landingY
                let impact = max(0.0, velocityY)
                largestImpact = max(largestImpact, impact)
                hard = hard || impact >= hardImpactSpeed
                if !impacted { firstImpact = true; impacted = true }
                if impact >= bounceImpactSpeed && bounceCount < 2 {
                    velocityY = -impact * floorRestitution
                    velocityX *= 0.72
                    bounceCount += 1
                } else {
                    velocityY = 0
                    velocityX *= pow(0.045, dt)
                    if abs(velocityX) <= 42 {
                        velocityX = 0
                        active = false
                        settled = true
                    }
                }
            }
        }
        return result(firstImpact: firstImpact, settled: settled)
    }

    private func result(firstImpact: Bool, settled: Bool) -> PetPhysicsStep {
        PetPhysicsStep(x: x, y: y, velocityX: velocityX, velocityY: velocityY, firstImpact: firstImpact, settled: settled, hardLanding: hard, impactSpeed: largestImpact, surfaceID: surfaceID)
    }
}

final class PetActivityDirector {
    private var nextAt: TimeInterval = 0
    private var lastClip = ""
    private var recent: [String] = []
    var level = "normal"
    var movementMode = "follow"

    func schedule(now: TimeInterval = Date.timeIntervalSinceReferenceDate, initial: Bool = false) {
        var range: ClosedRange<Double>
        switch level {
        case "quiet": range = initial ? 18...30 : 28...48
        case "lively": range = initial ? 6...12 : 8...18
        default: range = initial ? 10...18 : 14...30
        }
        if movementMode == "quiet" { range = initial ? 18...30 : 28...48 }
        if movementMode == "lively" { range = 9...11 }
        nextAt = now + Double.random(in: range)
    }

    func chooseIdleClip(available: Set<String>, now: TimeInterval = Date.timeIntervalSinceReferenceDate) -> String? {
        if nextAt == 0 { schedule(now: now, initial: true); return nil }
        guard now >= nextAt else { return nil }
        // Keep idle scheduling aligned with the authored ds-local-pet action
        // set. `eat_token` is a long token-overlay and the old scheduler made
        // it dominate idle time; the other actions below contain the actual
        // multi-frame life motion (blink, glance, happy, talk, sweep, sleep).
        let preferred: [String]
        switch movementMode {
        case "quiet": preferred = ["sleep", "blink", "glance"]
        case "lively": preferred = ["happy", "talk", "sweep", "eating", "glance", "blink"]
        case "follow": preferred = ["blink", "glance", "sleep"]
        default: preferred = ["blink", "glance", "happy", "talk", "sweep", "sleep", "eating"]
        }
        let choices = preferred.filter { available.contains($0) && $0 != lastClip }
        let clip = choices.randomElement() ?? available.first
        if let clip { lastClip = clip; recent.append(clip); recent = Array(recent.suffix(3)) }
        schedule(now: now)
        return clip
    }
}
