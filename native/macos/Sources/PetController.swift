import AppKit
import ApplicationServices
import QuartzCore
import UserNotifications

/// Native AppKit companion controller. Implements the full BigFish feature set
/// on Apple's official APIs:
/// - Borderless transparent NSPanel, `fullScreenAuxiliary` + `canJoinAllSpaces`
///   + `.floating` level, re-asserted every 2 s so the pet stays above
///   full-screen apps.
/// - Faithful port of the Qt helper's status card, multi-task card, animation
///   model, procedural motion, drag/click interactions, right-click menu,
///   idle micro-animations and layout persistence.
/// - Apple official permission handling (UserNotifications + Accessibility).
final class PetController: NSObject, NSTextFieldDelegate {
    static let labels: [String: String] = [
        "IDLE": "休息中",
        "THINKING": "思考中",
        "WORKING": "干活中",
        "WAITING": "等你呢",
        "SUCCESS": "完成啦",
        "ERROR": "出问题了",
        "DISCONNECTED": "已断开",
    ]

    static let statusColors: [String: (bg: String, fg: String)] = [
        "SUCCESS": ("#D9F7E4", "#12B85A"),
        "ERROR": ("#FDE3E3", "#E5484D"),
        "WAITING": ("#FFF0CE", "#D88A00"),
        "THINKING": ("#E2ECFF", "#4C78E8"),
        "WORKING": ("#DDEBFF", "#3478F6"),
        "DISCONNECTED": ("#ECEEF1", "#7B818A"),
    ]

    static let persistentStates: Set<String> = ["THINKING", "WORKING", "WAITING", "ERROR"]

    static let microIntervals: [String: (Double, Double)] = [
        "quiet": (12, 24),
        "normal": (6.5, 12.5),
        "lively": (3.5, 8),
    ]

    let model: AnimationModel
    let manifest: [String: Any]
    let assetRoot: URL
    let layoutURL: URL
    let webuiURL: String
    let eventLogURL: URL?
    let snapshotURL: URL?

    private var frameData: [String: Data] = [:]
    private let imageCache = NSCache<NSString, NSImage>()
    /// Decoded frames are held only in this bounded cache: a 24 fps set is
    /// thousands of frames and each decoded one costs about 0.55 MB.
    // Keep a complete short interaction clip resident. The old 24-frame
    // cache evicted almost every frame of the 96-frame head/poke/tail clips,
    // forcing WebP decode on every paint and making them visibly stutter.
    private static let decodedFrameCache = 280
    private var maxFrameWidth: CGFloat = 238
    private var maxFrameHeight: CGFloat = 260

    private(set) var panel: NSPanel?
    private(set) var contentView: PetView?
    private var inputField: NSTextField?
    private var sendButton: NSButton?

    var scale: Double
    var bubbleScale: Double
    var reducedMotion: Bool
    var activityLevel: String
    var soundEnabled: Bool
    var bubbleMode: String
    var bubbleStates: [String]
    var selectedModel: String
    var reasoningEffort: String
    var movementMode: String
    var walkSpeed: Double
    /// `topmost` is the default: the pet stays on the desktop top layer.
    /// `desktop` lets it behave like an ordinary desktop-level window.
    var windowLevelMode: String

    // Durable status
    var displayState = "IDLE"
    var statusState = "IDLE"
    var statusMessage = "我在这儿等新任务哦"
    var statusDetail = "DSH · 等待下一次任务"
    var statusDeadlineMs: Int?
    var overlayState: String?
    var overlayMessage = ""
    var overlayDetail = ""
    var overlayDeadlineMs: Int?
    var task = ""
    var tasks: [[String: Any]] = []

    // Geometry (pet anchor in AppKit bottom-left coordinates)
    private var petX: CGFloat = 0
    private var petY: CGFloat = 0

    private(set) var dragging = false

    private var animTimer: Timer?
    private var keepFrontTimer: Timer?
    private var microTimer: Timer?
    private var shakeTimer: Timer?
    private var shakeOrigin: NSPoint?
    private var shakeCount = 0
    private var lastTickMs: Int
    private var dragPetOffsetX: CGFloat = 0
    private var dragPetOffsetY: CGFloat = 8
    private var dragChainID = 0
    let interaction = PetInteractionTracker()
    private let throwPhysics = PetThrowPhysics()
    private let activityDirector = PetActivityDirector()
    private var recentPokes: [Int] = []
    private var headHoldTimer: Timer?
    private var headHoldTriggered = false
    private var pendingDragRelease: DragRelease?
    private var dialogue: [String: [[String: String]]] = [:]
    private var localAwarenessEnabled = false
    private var windowLandingEnabled = false
    private var hoverPauseEnabled = false
    private var walking = false
    private var walkDirection: CGFloat = 1
    private var walkTargetX: CGFloat?
    private var nextWalkAt: TimeInterval = 0
    private var walkToken = 0
    private var dragReleaseActive = false
    private var fadeFromFrame: String?
    private var fadeStarted: CFTimeInterval = 0
    private var fadeDuration: Double = 0.15
    private var snapshotSaved = false
    private var quitting = false
    private var conversation: [(role: String, text: String)] = []
    private var conversationScroll: CGFloat = 0

    private func normalizedMovementMode(_ value: String) -> String {
        switch value {
        case "still": return "quiet"
        case "wander": return "lively"
        case "quiet", "lively", "follow": return value
        default: return "follow"
        }
    }

    private var conversationURL: URL {
        layoutURL.deletingLastPathComponent().appendingPathComponent("conversation.json")
    }

    init(model: AnimationModel,
         manifest: [String: Any],
         assetRoot: URL,
         layoutURL: URL,
         webuiURL: String,
         eventLogURL: URL?,
         snapshotURL: URL?) {
        self.model = model
        self.manifest = manifest
        self.assetRoot = assetRoot
        self.layoutURL = layoutURL
        self.webuiURL = webuiURL
        self.eventLogURL = eventLogURL
        self.snapshotURL = snapshotURL

        let env = ProcessInfo.processInfo.environment
        let layout = PetLayout.load(from: layoutURL)
        if let raw = env["DSH_DAFEIYU_SCALE"], let value = Double(raw) {
            self.scale = Self.clampedScale(value)
        } else {
            self.scale = Self.clampedScale(layout.scale)
        }
        if let raw = env["DSH_DAFEIYU_BUBBLE_SCALE"], let value = Double(raw) {
            self.bubbleScale = Self.clampedBubbleScale(value)
        } else {
            self.bubbleScale = Self.clampedBubbleScale(layout.bubbleScale)
        }
        // The original companion is animated by default. Only an explicit
        // accessibility environment flag disables idle loops and micro clips.
        self.reducedMotion = env["DSH_DAFEIYU_REDUCED_MOTION"] == "1"
        self.activityLevel = env["DSH_DAFEIYU_ACTIVITY_LEVEL"] ?? "normal"
        self.soundEnabled = env["DSH_DAFEIYU_SOUND_ENABLED"] != "0"
        let configuredBubbleMode = env["DSH_DAFEIYU_BUBBLE_MODE"]
        self.bubbleMode = ["always", "hidden", "custom"].contains(configuredBubbleMode ?? "")
            ? configuredBubbleMode!
            : layout.bubbleMode
        self.bubbleStates = env["DSH_DAFEIYU_BUBBLE_STATES"]
            .map { $0.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) } }
            ?? layout.bubbleStates
        self.selectedModel = env["DSH_DAFEIYU_MODEL"] ?? ""
        self.reasoningEffort = env["DSH_DAFEIYU_REASONING"] ?? ""
        self.localAwarenessEnabled = false
        self.windowLandingEnabled = false
        self.hoverPauseEnabled = env["DSH_DAFEIYU_HOVER_PAUSE"] == "1"
        self.movementMode = layout.movementMode
        self.walkSpeed = layout.walkSpeed
        self.windowLevelMode = ["topmost", "desktop"].contains(layout.windowLevel) ? layout.windowLevel : "topmost"
        self.windowLandingEnabled = env["DSH_DAFEIYU_WINDOW_LANDING"].map { $0 == "1" } ?? layout.windowLanding
        self.localAwarenessEnabled = env["DSH_DAFEIYU_LOCAL_AWARENESS"].map { $0 == "1" } ?? layout.localAwareness
        self.lastTickMs = Self.nowMs()
        super.init()

        if let mfw = manifest["maxFrameWidth"] as? Int { maxFrameWidth = CGFloat(mfw) }
        if let mfh = manifest["maxFrameHeight"] as? Int { maxFrameHeight = CGFloat(mfh) }
        loadFrames()
        validateAnimationAssets()
        loadDialogue()
        loadConversation()
        if !isHeadless() {
            buildWindow()
            restorePosition()
            startTimers()
            NotificationCenter.default.addObserver(
                self,
                selector: #selector(screenParametersChanged),
                name: NSApplication.didChangeScreenParametersNotification,
                object: nil
            )
        }
    }

    private func isHeadless() -> Bool {
        ProcessInfo.processInfo.arguments.contains("--headless")
    }

    // MARK: - Frames & geometry

    private func loadFrames() {
        for clip in model.clips.values {
            for frame in clip.frames where frameData[frame] == nil {
                frameData[frame] = try? Data(contentsOf: assetRoot.appendingPathComponent(frame))
            }
        }
        imageCache.totalCostLimit = Self.decodedFrameCache * decodedFrameBytes
    }

    /// Validate the shipped manifest at launch. A malformed/empty frame list
    /// must never silently become a one-frame animation: report it and keep
    /// the fallback clip usable instead.
    private func validateAnimationAssets() {
        for (name, clip) in model.clips {
            let missing = clip.frames.filter { frameData[$0] == nil }.count
            if clip.frames.count < 2 || missing > 0 {
                let message = "DSH animation clip \(name): \(clip.frames.count) frame(s), missing \(missing)"
                FileHandle.standardError.write(Data((message + "\n").utf8))
            }
        }
    }

    private func loadDialogue() {
        guard let root = Bundle.main.resourceURL?.appendingPathComponent("assets/dialogue") else { return }
        guard let files = try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return }
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            var sections: [[String: String]] = []
            for (key, raw) in value {
                if let values = raw as? [String] {
                    sections.append(contentsOf: values.map { ["kind": key, "text": $0] })
                } else if let groups = raw as? [String: [String]] {
                    for (group, values) in groups {
                        sections.append(contentsOf: values.map { ["kind": group, "text": $0] })
                    }
                }
            }
            dialogue[file.deletingPathExtension().lastPathComponent] = sections
        }
    }

    private func loadConversation() {
        guard let data = try? Data(contentsOf: conversationURL),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
        conversation = Array(raw.compactMap { item in
            guard let role = item["role"] as? String,
                  let text = item["text"] as? String,
                  !text.isEmpty else { return nil }
            return (role: role, text: text)
        }.suffix(100))
    }

    private func saveConversation() {
        let raw = conversation.map { ["role": $0.role, "text": $0.text] }
        guard let data = try? JSONSerialization.data(withJSONObject: raw, options: [.prettyPrinted]) else { return }
        try? FileManager.default.createDirectory(at: conversationURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: conversationURL, options: .atomic)
    }

    private func appendConversation(role: String, text: String) {
        guard !text.isEmpty else { return }
        conversation.append((role: role, text: text))
        if conversation.count > 100 { conversation.removeFirst(conversation.count - 100) }
        conversationScroll = 0
        saveConversation()
        contentView?.needsDisplay = true
    }

    private func dialogueLine(kind: String, fallback: String) -> String {
        let candidates = dialogue.values.flatMap { $0 }.filter { entry in
            entry["kind"] == kind || kind == "tap" && entry["kind"] == "standard"
        }
        return candidates.randomElement()?["text"] ?? fallback
    }

    private func physicsBounds() -> PetPhysicsBounds {
        let frame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var platforms: [PetPhysicsPlatform] = []
        if windowLandingEnabled,
           let window = frontmostWindowFrame(),
           window.width > 120,
           window.height > 80 {
            platforms.append(PetPhysicsPlatform(
                left: Double(window.minX),
                right: Double(window.maxX),
                top: Double(window.maxY),
                identifier: "front-window"
            ))
        }
        return PetPhysicsBounds(
            left: Double(frame.minX),
            top: Double(frame.minY),
            right: Double(frame.maxX),
            bottom: Double(frame.maxY),
            platforms: platforms
        )
    }

    private func frontmostWindowFrame() -> NSRect? {
        guard localAwarenessEnabled,
              let app = NSWorkspace.shared.frontmostApplication,
              app.processIdentifier != ProcessInfo.processInfo.processIdentifier else { return nil }
        let element = AXUIElementCreateApplication(app.processIdentifier)
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXFocusedWindowAttribute as CFString, &value) == .success,
              let window = value as? AXUIElement else { return nil }
        var positionValue: CFTypeRef?
        var sizeValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(window, kAXPositionAttribute as CFString, &positionValue) == .success,
              AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &sizeValue) == .success else { return nil }
        var position = CGPoint.zero
        var size = CGSize.zero
        guard let positionValue = positionValue as? AXValue,
              let sizeValue = sizeValue as? AXValue,
              AXValueGetValue(positionValue, .cgPoint, &position),
              AXValueGetValue(sizeValue, .cgSize, &size) else { return nil }
        let screen = NSScreen.screens.first { $0.frame.contains(NSPoint(x: position.x, y: screenFrameHeight() - position.y)) } ?? NSScreen.main
        let bottomY = (screen?.frame.maxY ?? screenFrameHeight()) - position.y - size.height
        return NSRect(x: position.x, y: bottomY, width: size.width, height: size.height)
    }

    private func screenFrameHeight() -> CGFloat {
        NSScreen.main?.frame.maxY ?? 900
    }

    private var decodedFrameBytes: Int { Int(maxFrameWidth * maxFrameHeight * 4) }

    private func frameImage(for frame: String) -> NSImage? {
        let key = frame as NSString
        if let cached = imageCache.object(forKey: key) { return cached }
        guard let data = frameData[frame],
              let bitmap = NSBitmapImageRep(data: data) else { return nil }
        // WebP keeps a one-byte black matte in many otherwise transparent
        // pixels (RGBA = 0,0,0,1).  AppKit's buffered translucent windows can
        // blend those pixels as a visible silhouette when the panel moves.
        // Remove the matte at decode time so every caller, including cached
        // frames and animation transitions, gets genuinely transparent art.
        if !bitmap.isPlanar, bitmap.samplesPerPixel >= 4,
           let bytes = bitmap.bitmapData {
            let alphaIndex = bitmap.bitmapFormat.contains(.alphaFirst) ? 0 : bitmap.samplesPerPixel - 1
            for y in 0..<bitmap.pixelsHigh {
                let row = bytes.advanced(by: y * bitmap.bytesPerRow)
                for x in 0..<bitmap.pixelsWide {
                    let pixel = row.advanced(by: x * bitmap.samplesPerPixel)
                    let alpha = pixel[alphaIndex]
                    if alpha <= 32 {
                        pixel[alphaIndex] = 0
                        for channel in 0..<bitmap.samplesPerPixel where channel != alphaIndex {
                            pixel[channel] = 0
                        }
                    }
                }
            }
        }
        // NSImage(data:) is lazy on macOS. Wrapping an already-created bitmap
        // rep makes the first display deterministic and avoids decode work on
        // the animation timer thread.
        let image = NSImage(size: bitmap.size)
        image.addRepresentation(bitmap)
        imageCache.setObject(image, forKey: key, cost: decodedFrameBytes)
        return image
    }

    private var petWidth: CGFloat { maxFrameWidth * scale }
    private var petHeight: CGFloat { maxFrameHeight * scale }

    private var cardHeightPoints: CGFloat {
        if !conversation.isEmpty {
            return min(220, max(84, CGFloat(conversation.count) * 20 + 44)) * bubbleScale
        }
        if tasks.count >= 2 {
            let rows = min(tasks.count, 3)
            return (58 + CGFloat(rows) * 26) * bubbleScale
        }
        return 84 * bubbleScale
    }

    private var cardWidthPoints: CGFloat { 360 * bubbleScale }

    private func windowSize() -> NSSize {
        NSSize(width: max(petWidth + 36, cardWidthPoints + 24), height: petHeight + cardHeightPoints + 122)
    }

    func petRect() -> NSRect {
        let viewSize = contentView?.bounds.size ?? windowSize()
        let offsetX = (viewSize.width - petWidth) / 2
        return NSRect(x: offsetX, y: viewSize.height - petHeight - 8, width: petWidth, height: petHeight)
    }

    func bubbleRect() -> NSRect {
        let viewSize = contentView?.bounds.size ?? windowSize()
        let cardWidth = cardWidthPoints
        let cardHeight = cardHeightPoints
        let petCenterX = petRect().midX
        let margin: CGFloat = 14
        let minX = margin
        let maxX = max(minX, viewSize.width - cardWidth - margin)
        let cardX = min(max(petCenterX - cardWidth / 2, minX), maxX)
        return NSRect(x: cardX, y: 7, width: cardWidth, height: cardHeight)
    }

    private func screenContaining(_ point: NSPoint) -> NSScreen? {
        NSScreen.screens.first { $0.frame.contains(point) }
    }

    func moveToPet(_ x: CGFloat, _ y: CGFloat) {
        guard let panel = panel else { return }
        let size = windowSize()
        let geometry = screenContaining(NSPoint(x: x, y: y))?.visibleFrame ?? NSScreen.main?.visibleFrame
        let minX = geometry?.minX ?? 0
        let maxX = max(minX, (geometry?.maxX ?? minX + size.width) - size.width + 1)
        let centerOffsetX = (size.width - petWidth) / 2
        let windowX = min(max(x - centerOffsetX, minX), maxX)
        self.petX = windowX + centerOffsetX

        let minY = geometry?.minY ?? 0
        let maxY = max(minY, (geometry?.maxY ?? minY + size.height) - size.height + 1)
        let windowY = min(max(y - 8, minY), maxY)
        self.petY = windowY + 8

        panel.setFrameOrigin(NSPoint(x: windowX, y: windowY))
        contentView?.needsDisplay = true
    }

    private func restorePosition() {
        let layout = PetLayout.load(from: layoutURL)
        let screenHeight = NSScreen.main?.frame.height ?? 982
        if let px = layout.petX, let py = layout.petY {
            let ax = CGFloat(px)
            var ay = CGFloat(py)
            if layout.coordinateSpace == nil && ay < screenHeight * 0.5 {
                // Legacy Qt layout stores top-left coordinates; convert to
                // AppKit bottom-left before first use.
                ay = screenHeight - (ay + petHeight)
            }
            moveToPet(ax, ay)
        } else {
            let geometry = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1512, height: 982)
            moveToPet(geometry.maxX - petWidth - 24, geometry.minY + 24)
        }
        saveLayout()
    }

    func saveLayout() {
        guard let panel = panel else { return }
        let origin = panel.frame.origin
        var layout = PetLayout()
        layout.x = Int(origin.x.rounded())
        layout.y = Int(origin.y.rounded())
        layout.petX = Int(petX.rounded())
        layout.petY = Int(petY.rounded())
        layout.scale = scale
        layout.bubbleScale = bubbleScale
        layout.reducedMotion = reducedMotion
        layout.bubbleMode = bubbleMode
        layout.bubbleStates = bubbleStates
        layout.movementMode = movementMode
        layout.walkSpeed = walkSpeed
        layout.windowLanding = windowLandingEnabled
        layout.localAwareness = localAwarenessEnabled
        layout.windowLevel = windowLevelMode
        layout.save(to: layoutURL)
    }

    func resizeAndReposition() {
        guard let panel = panel, let contentView = contentView else { return }
        let size = windowSize()
        let origin = panel.frame.origin
        panel.setContentSize(size)
        panel.setFrameOrigin(origin)
        contentView.frame = NSRect(origin: .zero, size: size)
        let inputY = 7 + cardHeightPoints + 18
        inputField?.frame = NSRect(x: 14, y: inputY, width: size.width - 92, height: 34)
        sendButton?.frame = NSRect(x: size.width - 72, y: inputY, width: 58, height: 34)
        moveToPet(petX, petY)
        contentView.needsDisplay = true
    }

    // MARK: - Window

    private func buildWindow() {
        let size = windowSize()
        let panel = NSPanel(
            contentRect: NSRect(x: 100, y: 100, width: size.width, height: size.height),
            styleMask: .borderless,
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        applyWindowLevel(to: panel)
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = false
        panel.isMovableByWindowBackground = false
        panel.title = "DSH 大肥鱼"

        let view = PetView(frame: NSRect(x: 0, y: 0, width: size.width, height: size.height))
        view.controller = self
        panel.contentView = view
        self.panel = panel
        self.contentView = view

        let inputY = 7 + cardHeightPoints + 18
        let field = NSTextField(frame: NSRect(x: 14, y: inputY, width: size.width - 92, height: 34))
        field.placeholderString = "输入消息给 Codex…"
        field.font = NSFont.systemFont(ofSize: 15)
        field.bezelStyle = .roundedBezel
        field.backgroundColor = NSColor(calibratedWhite: 1, alpha: 0.97)
        field.delegate = self
        field.target = self
        field.action = #selector(submitInput(_:))
        view.addSubview(field)
        let button = NSButton(frame: NSRect(x: size.width - 72, y: inputY, width: 58, height: 34))
        button.title = "发送"
        button.bezelStyle = .rounded
        button.target = self
        button.action = #selector(submitInput(_:))
        view.addSubview(button)
        inputField = field
        sendButton = button
    }

    func show() {
        guard !isHeadless() else { return }
        panel?.orderFrontRegardless()
        keepFront()
    }

    @objc private func screenParametersChanged() {
        moveToPet(petX, petY)
    }

    // MARK: - Timers

    private var animationInterval: TimeInterval {
        if reducedMotion { return 0.04 }
        // Keep source 24 fps loops close to their native pace. Only the
        // 96-frame touch reactions need 60 Hz; repainting idle at 60 Hz made
        // the Python/AppKit helpers spend a core decoding and skip frames.
        return model.activeClip.frameMs <= 30 ? 1.0 / 60.0 : 1.0 / 30.0
    }

    private func startAnimTimer() {
        animTimer?.invalidate()
        animTimer = Timer.scheduledTimer(withTimeInterval: animationInterval, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    private func startTimers() {
        startAnimTimer()
        keepFrontTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.keepFront()
        }
        scheduleMicro()
        activityDirector.level = activityLevel
        activityDirector.movementMode = normalizedMovementMode(movementMode)
        activityDirector.schedule(initial: true)
        scheduleWalk(initial: true)
    }

    private func keepFront() {
        guard let panel = panel, !quitting else { return }
        // Re-assert every 2 s: space/full-screen transitions can reset the
        // collection behavior. Do not call orderFrontRegardless in desktop
        // mode: doing so steals focus and makes the pet cover normal windows.
        // The same applies to topmost mode: the watchdog must not repeatedly
        // activate the panel while the user is typing in another app.
        applyWindowLevel(to: panel)
    }

    private func applyWindowLevel(to panel: NSPanel) {
        if windowLevelMode == "topmost" {
            panel.level = .floating
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        } else {
            panel.level = .normal
            panel.collectionBehavior = [.canJoinAllSpaces, .stationary]
        }
    }

    private func scheduleMicro() {
        microTimer?.invalidate()
        guard !reducedMotion else { return }
        let effectiveLevel = normalizedMovementMode(movementMode) == "quiet" ? "quiet" : normalizedMovementMode(movementMode) == "lively" ? "lively" : activityLevel
        let range = Self.microIntervals[effectiveLevel] ?? Self.microIntervals["normal"]!
        let delay = Double.random(in: range.0...range.1)
        microTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            if !self.dragging {
                let index = Int.random(in: 0..<max(1, self.model.idleMicroClips.count))
                _ = self.model.playIdleMicro(index: index)
                self.contentView?.needsDisplay = true
            }
            self.scheduleMicro()
        }
    }

    private func tick() {
        let now = Self.nowMs()
        let elapsed = max(0, now - lastTickMs)
        lastTickMs = now
        let hadPulse = model.pulseState != nil
        let previousFrame = model.frame
        let previousClip = model.activeClipName
        // Reduced-motion suppresses decorative micro-actions, but it must not
        // freeze authored action/walk loops. Keeping the model clock running
        // prevents a mode switch from leaving the pet on one frame.
        let modelElapsed = elapsed
        model.advance(elapsedMs: modelElapsed, nowMs: now)
        if dragReleaseActive,
           let active = model.overlayClipName,
           !["falling", "landing", "dragging_dizzy"].contains(active) {
            dragReleaseActive = false
        }
        updateWalk(now: Date.timeIntervalSinceReferenceDate, elapsed: Double(elapsed) / 1000.0)
        if throwPhysics.active && !dragging {
            let bounds = physicsBounds()
            let step = throwPhysics.step(
                Double(elapsed) / 1000.0,
                width: Double(petWidth),
                height: Double(petHeight),
                bounds: bounds
            )
            moveToPet(CGFloat(step.x + petWidth / 2), CGFloat(step.y + petHeight))
            if step.firstImpact {
                if step.hardLanding {
                    _ = model.playOverlay("dragging_dizzy")
                    showOverlay("哎呀，摔得有点重…", dialogueLine(kind: "drag_fast", fallback: "让我缓一缓。"), "ERROR", 2200)
                } else {
                    // A normal release ends with the authored standing
                    // recovery. The old code used dragging_release here and
                    // then cleared it in the same tick, so the visible result
                    // was either a flash or only the dazed fallback.
                    _ = model.playOverlay("landing")
                    showOverlay("稳稳落地", dialogueLine(kind: "drag_gentle", fallback: "我站稳啦。"), "SUCCESS", 1400)
                }
            }
            if step.settled {
                throwPhysics.cancel()
                dragReleaseActive = false
                // Keep the landing/dizzy clip visible for its authored two
                // seconds even when the physics solver settles in the same
                // timer tick as impact. A new grab increments dragChainID and
                // invalidates this delayed cleanup.
                let token = dragChainID
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                    guard let self, token == self.dragChainID, !self.dragging else { return }
                    self.model.clearOverlay()
                    self.contentView?.needsDisplay = true
                }
                saveLayout()
            }
        } else if !dragging && model.baseState == "IDLE" && !reducedMotion {
            let available = Set(model.clips.keys)
            if let clip = activityDirector.chooseIdleClip(available: available, now: Date.timeIntervalSinceReferenceDate) {
                _ = model.playOverlay(clip)
            }
        }
        syncFrameTransition(previousFrame: previousFrame, previousClip: previousClip)
        if hadPulse && model.pulseState == nil {
            displayState = model.baseState
        }
        if let deadline = overlayDeadlineMs, now >= deadline {
            clearOverlay()
        }
        if animTimer?.timeInterval != animationInterval {
            startAnimTimer()
        }
        contentView?.needsDisplay = true
    }

    private func scheduleWalk(initial: Bool = false) {
        guard normalizedMovementMode(movementMode) == "lively", !reducedMotion else { return }
        let range = 9.0...11.0
        nextWalkAt = Date.timeIntervalSinceReferenceDate + Double.random(in: range)
    }

    private func updateWalk(now: TimeInterval, elapsed: TimeInterval) {
        if let active = model.overlayClipName,
           ["dragging", "dragging_release", "dragging_dizzy", "falling", "landing", "dizzy"].contains(active) {
            walkToken &+= 1
            walkTargetX = nil
            walking = false
            return
        }
        guard normalizedMovementMode(movementMode) == "lively", !dragging, !throwPhysics.active,
              !dragReleaseActive,
              model.baseState == "IDLE" else { return }
        if walkTargetX == nil {
            guard now >= nextWalkAt else { return }
            let frame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            let half = max(40, petWidth * 0.5)
            let left = frame.minX + half + 12
            let right = frame.maxX - half - 12
            guard right > left else { scheduleWalk(); return }
            let current = petX
            var target = CGFloat.random(in: left...right)
            if abs(target - current) < petWidth * 0.8 { target = current < frame.midX ? right : left }
            walkTargetX = min(max(target, left), right)
            walkDirection = (walkTargetX ?? current) >= current ? 1 : -1
            beginWalk(direction: walkDirection > 0 ? "right" : "left")
        }
        guard let target = walkTargetX else { return }
        // The authored enter pose is two seconds long. Keep the anchor still
        // until it hands off to the directional body loop; translating during
        // the enter clip looked like a slide rather than a step.
        if let program = model.program(for: walkDirection > 0 ? "walk_right" : "walk_left"),
           model.activeClipName == program.enter {
            return
        }
        let step = CGFloat(max(20, walkSpeed)) * CGFloat(elapsed) * walkDirection
        let next = petX + step
        if (walkDirection > 0 && next >= target) || (walkDirection < 0 && next <= target) {
            moveToPet(target, petY)
            walkTargetX = nil
            finishWalk(direction: walkDirection > 0 ? "right" : "left")
        } else {
            moveToPet(next, petY)
        }
    }

    private func beginWalk(direction: String) {
        walkToken &+= 1
        let token = walkToken
        let name = "walk_\(direction)"
        guard let program = model.program(for: name) else {
            _ = model.playOverlay(name)
            return
        }
        if let enter = program.enter { _ = model.playOverlay(enter) }
        let enterDelay: Double = program.enter == nil ? 0 : 2.0
        DispatchQueue.main.asyncAfter(deadline: .now() + enterDelay) { [weak self] in
            guard let self, self.walkToken == token, self.walkTargetX != nil, !self.dragging else { return }
            _ = self.model.playOverlay(program.body)
            self.contentView?.needsDisplay = true
        }
    }

    private func finishWalk(direction: String) {
        walkToken &+= 1
        let name = "walk_\(direction)"
        guard let program = model.program(for: name), let exit = program.exit else {
            model.clearOverlay()
            scheduleWalk()
            return
        }
        _ = model.playOverlay(exit)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self else { return }
            self.model.clearOverlay()
            self.scheduleWalk()
            self.contentView?.needsDisplay = true
        }
    }
    private func syncFrameTransition(previousFrame: String, previousClip: String) {
        let currentFrame = model.frame
        guard currentFrame != previousFrame else { return }
        // Avoid crossfading transparent WebP frames: on some macOS builds the
        // old frame is composited as a black silhouette beside the new pet.
        fadeFromFrame = nil
    }

    // MARK: - Interaction

    func beginDrag() {
        guard !dragging else { return }
        dragging = true
        dragReleaseActive = false
        dragChainID &+= 1
        walkToken &+= 1
        walkTargetX = nil
        walking = false
        // Remember where the pet sits inside the window when the drag starts.
        // During the drag the window moves directly; without re-anchoring the
        // pet it would slide inside the window at the same rate and stay
        // frozen on screen while the bubble moves — a visible desync.
        let rect = petRect()
        dragPetOffsetX = rect.minX
        dragPetOffsetY = rect.minY
        animTimer?.invalidate()
        microTimer?.invalidate()
        _ = model.playOverlay("dragging")
        contentView?.needsDisplay = true
    }

    func updateDrag() {
        guard let panel = panel else { return }
        let viewSize = contentView?.bounds.size ?? windowSize()
        // Re-anchor the pet to the window's current origin using the offsets
        // captured at drag start, so the character and the bubble move as one.
        petX = panel.frame.origin.x + dragPetOffsetX
        petY = panel.frame.origin.y + (viewSize.height - dragPetOffsetY - petHeight)
        contentView?.needsDisplay = true
    }

    func endDrag() {
        guard dragging else { return }
        let now = Self.nowMs()
        model.advance(elapsedMs: 0, nowMs: now)
        let release = pendingDragRelease
        pendingDragRelease = nil
        model.clearOverlay()
        dragging = false
        walkToken &+= 1
        walkTargetX = nil
        walking = false
        lastTickMs = now
        startAnimTimer()
        if let release, release.wasDrag {
            let speed = hypot(release.velocityX, release.velocityY)
            // A small release is a normal drop, not a throw. The previous
            // 180 px/s cutoff sent almost every mouse-up through the physics
            // solver; because the old coordinate conversion started it far
            // above the floor, it accumulated a hard impact and showed only
            // dizziness. Reserve physics/dizzy handling for an intentional
            // fast flick and let gentle drags use falling -> landing.
            if speed > 700 {
                dragReleaseActive = true
                throwPhysics.launch(
                    // Physics coordinates describe the character bounds, not
                    // the larger bubble window. Starting at the panel origin
                    // made every throw fall hundreds of pixels and exceed the
                    // hard-impact threshold, which is why ordinary drags
                    // almost always ended in dizziness.
                    x: Double(petRect().minX + (panel?.frame.origin.x ?? 0)),
                    y: Double((panel?.frame.origin.y ?? 0) + 8),
                    velocityX: release.velocityX,
                    velocityY: -release.velocityY
                )
                _ = model.playOverlay("falling")
                showOverlay("抓稳啦，开始下落", dialogueLine(kind: speed > 850 ? "drag_fast" : "drag_gentle", fallback: "我会自己落地。"), "THINKING", 1800)
            }
        }
        if !reducedMotion && !throwPhysics.active {
            scheduleMicro()
            runDragReleaseChain()
        }
        saveLayout()
        contentView?.needsDisplay = true
    }

    func interactionPress(at point: NSPoint) {
        let rect = petRect()
        let x = Double((point.x - rect.minX) / max(1.0, rect.width))
        let y = Double((point.y - rect.minY) / max(1.0, rect.height))
        let origin = panel?.frame.origin ?? .zero
        interaction.press(x: x, y: y, sampleX: Double(origin.x + point.x), sampleY: Double(origin.y + point.y))
        headHoldTriggered = false
        headHoldTimer?.invalidate()
        if interaction.region == .face || interaction.region == .head {
            headHoldTimer = Timer.scheduledTimer(withTimeInterval: 0.55, repeats: false) { [weak self] _ in
                guard let self, self.interaction.active, !self.interaction.dragging else { return }
                self.headHoldTriggered = true
                _ = self.model.playOverlay("head_pat")
                self.showOverlay("慢慢摸就很舒服。", self.dialogueLine(kind: "head_hold", fallback: "再摸一会儿也可以。"), "SUCCESS", 2400)
                self.contentView?.needsDisplay = true
            }
        }
    }

    func interactionMove(at point: NSPoint) {
        let rect = petRect()
        let x = Double((point.x - rect.minX) / max(1.0, rect.width))
        let y = Double((point.y - rect.minY) / max(1.0, rect.height))
        let origin = panel?.frame.origin ?? .zero
        _ = interaction.move(x: Double(origin.x + point.x), y: Double(origin.y + point.y))
    }

    func interactionRelease(at point: NSPoint, clickCount: Int) {
        headHoldTimer?.invalidate()
        let rect = petRect()
        let x = Double((point.x - rect.minX) / max(1.0, rect.width))
        let y = Double((point.y - rect.minY) / max(1.0, rect.height))
        let origin = panel?.frame.origin ?? .zero
        let release = interaction.release(
            x: x,
            y: y,
            sampleX: Double(origin.x + point.x),
            sampleY: Double(origin.y + point.y),
            now: Date.timeIntervalSinceReferenceDate
        )
        if release?.wasDrag == true {
            pendingDragRelease = release
            endDrag()
            return
        }
        if clickCount >= 2 {
            _ = model.playOverlay("eating")
            showOverlay("投喂时间到！", dialogueLine(kind: "food", fallback: "好吃，谢谢投喂。"), "SUCCESS", 2200)
            contentView?.needsDisplay = true
            return
        }
        guard let release else { return }
        switch release.region {
        case .face:
            _ = model.playOverlay("head_pat")
            showOverlay("戳到脸啦。", dialogueLine(kind: "face", fallback: "轻一点碰就好啦。"), "SUCCESS", 1700)
        case .head:
            _ = model.playOverlay("head_pat")
            showOverlay("摸摸头，今天也辛苦啦。", dialogueLine(kind: "head", fallback: "头顶是可以摸的。"), "SUCCESS", 1700)
        case .tail:
            _ = model.playOverlay("tail")
            showOverlay("尾巴不是进度条啦！", dialogueLine(kind: "tail", fallback: "尾巴被碰到啦。"), "WAITING", 1700)
        case .body:
            recentPokes.append(Self.nowMs())
            recentPokes = recentPokes.filter { Self.nowMs() - $0 < 2600 }
            _ = model.playOverlay("poke")
            if recentPokes.count >= 3 {
                showOverlay("再戳我就要生气了。", dialogueLine(kind: "sharp", fallback: "请不要连续戳我。"), "ERROR", 1900)
            } else {
                showOverlay("戳我干嘛，任务还在跑呢", dialogueLine(kind: "tap", fallback: "收到互动，心情加一点。"), "WAITING", 1500)
            }
        }
        contentView?.needsDisplay = true
    }

    func handleClick(at point: NSPoint, clickCount: Int) {
        if clickCount >= 2 {
            _ = model.playOverlay("head_pat")
            showOverlay("好啦好啦，知道你喜欢我~", statusDetail, statusState, 1800)
            contentView?.needsDisplay = true
            return
        }
        let rect = petRect()
        let relativeX = max(0, point.x - rect.minX)
        let relativeY = max(0, point.y - rect.minY)
        if relativeY < rect.height * 0.45 {
            _ = model.playOverlay("head_pat")
            showOverlay("摸摸也不能让我少干活哦~", statusDetail, statusState, 1800)
        } else if relativeX > rect.width * 0.72 {
            _ = model.playOverlay("tail")
            showOverlay("尾巴不是进度条啦！", statusDetail, statusState, 1500)
        } else {
            _ = model.playOverlay("poke")
            showOverlay("戳我干嘛，任务还在跑呢", statusDetail, statusState, 1500)
        }
        contentView?.needsDisplay = true
    }

    func showMenu(with event: NSEvent) {
        guard let contentView = contentView else { return }
        let menu = NSMenu(title: "DSH 大肥鱼")

        let quick = NSMenu(title: "快速控制")
        for (label, selector) in [("投喂", #selector(feedFromMenu(_:))), ("说话", #selector(sayFromMenu(_:))), ("开心", #selector(happyFromMenu(_:))), ("休息", #selector(restFromMenu(_:)))] {
            let item = NSMenuItem(title: label, action: selector, keyEquivalent: "")
            item.target = self
            quick.addItem(item)
        }
        let quickItem = NSMenuItem(title: "快速控制", action: nil, keyEquivalent: "")
        quickItem.submenu = quick
        menu.addItem(quickItem)

        let movementMenu = NSMenu(title: "移动模式")
        for (label, value) in [("跟随鼠标", "follow"), ("安静陪伴", "quiet"), ("活泼陪伴", "lively")] {
            let item = NSMenuItem(title: label, action: #selector(changeMovementMode(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = value
            item.state = normalizedMovementMode(movementMode) == value ? .on : .off
            movementMenu.addItem(item)
        }
        let movementItem = NSMenuItem(title: "移动模式", action: nil, keyEquivalent: "")
        movementItem.submenu = movementMenu
        menu.addItem(movementItem)

        menu.addItem(.separator())
        let settings = NSMenu(title: "更多设置")
        let sizeMenu = NSMenu(title: "大小")
        for (label, value) in [("迷你", 0.6), ("小", 0.8), ("标准", 1.0), ("大", 1.25)] {
            let item = NSMenuItem(title: label, action: #selector(changeSize(_:)), keyEquivalent: "")
            item.target = self
            item.tag = Int(value * 100)
            item.state = abs(scale - value) < 0.05 ? .on : .off
            sizeMenu.addItem(item)
        }
        let sizeItem = NSMenuItem(title: "大小", action: nil, keyEquivalent: "")
        sizeItem.submenu = sizeMenu
        settings.addItem(sizeItem)

        let bubbleMenu = NSMenu(title: "气泡大小")
        for (label, value) in [("小", 0.8), ("标准", 1.0), ("大", 1.2)] {
            let item = NSMenuItem(title: label, action: #selector(changeBubbleScale(_:)), keyEquivalent: "")
            item.target = self
            item.tag = Int(value * 100)
            item.state = abs(bubbleScale - value) < 0.05 ? .on : .off
            bubbleMenu.addItem(item)
        }
        let bubbleItem = NSMenuItem(title: "气泡大小", action: nil, keyEquivalent: "")
        bubbleItem.submenu = bubbleMenu
        settings.addItem(bubbleItem)

        let modelMenu = NSMenu(title: "模型")
        for (label, value) in [("Codex 默认", ""), ("gpt-5-mini", "gpt-5-mini"), ("gpt-5.6-terra", "gpt-5.6-terra"), ("gpt-5.5", "gpt-5.5"), ("gpt-5.6-sol", "gpt-5.6-sol"), ("gpt-5.6-luna", "gpt-5.6-luna")] {
            let item = NSMenuItem(title: label, action: #selector(changeModel(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = value
            item.state = selectedModel == value ? .on : .off
            modelMenu.addItem(item)
        }
        let modelItem = NSMenuItem(title: "模型", action: nil, keyEquivalent: "")
        modelItem.submenu = modelMenu
        settings.addItem(modelItem)

        let effortMenu = NSMenu(title: "推理强度")
        for (label, value) in [("跟随模型", ""), ("最小", "minimal"), ("低", "low"), ("中", "medium"), ("高", "high"), ("极高", "xhigh"), ("最大", "max"), ("超高", "ultra")] {
            let item = NSMenuItem(title: label, action: #selector(changeReasoningEffort(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = value
            item.state = reasoningEffort == value ? .on : .off
            effortMenu.addItem(item)
        }
        let effortItem = NSMenuItem(title: "推理强度", action: nil, keyEquivalent: "")
        effortItem.submenu = effortMenu
        settings.addItem(effortItem)

        let reduced = NSMenuItem(title: "减少动态", action: #selector(toggleReducedMotion(_:)), keyEquivalent: "")
        reduced.target = self
        reduced.state = reducedMotion ? .on : .off
        settings.addItem(reduced)

        let openWeb = NSMenuItem(title: "打开 WebUI", action: #selector(openWebUI(_:)), keyEquivalent: "")
        openWeb.target = self
        settings.addItem(openWeb)

        let accessibility = NSMenuItem(title: "辅助功能权限…", action: #selector(accessibilityPermission(_:)), keyEquivalent: "")
        accessibility.target = self
        settings.addItem(accessibility)

        let local = NSMenuItem(title: "本地环境感知", action: #selector(toggleLocalAwareness(_:)), keyEquivalent: "")
        local.target = self
        local.state = localAwarenessEnabled ? .on : .off
        settings.addItem(local)
        let landing = NSMenuItem(title: "窗口顶部落脚", action: #selector(toggleWindowLanding(_:)), keyEquivalent: "")
        landing.target = self
        landing.state = windowLandingEnabled ? .on : .off
        settings.addItem(landing)

        let levelMenu = NSMenu(title: "窗口层级")
        for (label, value) in [("桌面顶部（始终置顶）", "topmost"), ("普通窗口层级", "desktop")] {
            let item = NSMenuItem(title: label, action: #selector(changeWindowLevel(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = value
            item.state = normalizedMovementMode(movementMode) == value ? .on : .off
            levelMenu.addItem(item)
        }
        let levelItem = NSMenuItem(title: "窗口层级", action: nil, keyEquivalent: "")
        levelItem.submenu = levelMenu
        settings.addItem(levelItem)
        let hover = NSMenuItem(title: "悬停暂停", action: #selector(toggleHoverPause(_:)), keyEquivalent: "")
        hover.target = self
        hover.state = hoverPauseEnabled ? .on : .off
        settings.addItem(hover)

        let settingsItem = NSMenuItem(title: "更多设置", action: nil, keyEquivalent: "")
        settingsItem.submenu = settings
        menu.addItem(settingsItem)

        menu.addItem(.separator())

        let hide = NSMenuItem(title: "本次隐藏", action: #selector(hidePet(_:)), keyEquivalent: "")
        hide.target = self
        menu.addItem(hide)

        let quit = NSMenuItem(title: "本次关闭", action: #selector(quitFromMenu(_:)), keyEquivalent: "")
        quit.target = self
        menu.addItem(quit)

        NSMenu.popUpContextMenu(menu, with: event, for: contentView)
    }

    @objc private func feedFromMenu(_ sender: Any?) { _ = model.playOverlay("eat_token"); showOverlay("投喂成功", dialogueLine(kind: "food", fallback: "好吃，谢谢投喂。"), "SUCCESS", 2200); contentView?.needsDisplay = true }
    @objc private func sayFromMenu(_ sender: Any?) { showOverlay("我在听", dialogueLine(kind: "standard", fallback: "今天也一起加油。"), "WAITING", 2200); contentView?.needsDisplay = true }
    @objc private func happyFromMenu(_ sender: Any?) { _ = model.playOverlay("success"); showOverlay("开心一下", dialogueLine(kind: "community", fallback: "收到一份好心情。"), "SUCCESS", 1800); contentView?.needsDisplay = true }
    @objc private func restFromMenu(_ sender: Any?) { model.applyState("IDLE"); showOverlay("休息中", "我会安静陪着你。", "IDLE", 1800); contentView?.needsDisplay = true }
    @objc private func changeMovementMode(_ sender: NSMenuItem) { movementMode = normalizedMovementMode((sender.representedObject as? String) ?? "follow"); walkTargetX = nil; model.clearOverlay(); activityDirector.movementMode = movementMode; activityDirector.level = movementMode == "quiet" ? "quiet" : movementMode == "lively" ? "lively" : activityLevel; activityDirector.schedule(initial: false); scheduleWalk(); saveLayout(); reportSettings(["movementMode": movementMode]); showOverlay("移动模式已切换", sender.title, "SUCCESS", 1600); contentView?.needsDisplay = true }
    @objc private func toggleLocalAwareness(_ sender: NSMenuItem) { localAwarenessEnabled.toggle(); reportSettings(["localAwareness": localAwarenessEnabled]); showOverlay("本地感知", localAwarenessEnabled ? "已开启，仅读取应用元数据" : "已关闭", "SUCCESS", 1800); contentView?.needsDisplay = true }
    @objc private func toggleWindowLanding(_ sender: NSMenuItem) { windowLandingEnabled.toggle(); saveLayout(); reportSettings(["windowLanding": windowLandingEnabled]); showOverlay("窗口顶部落脚", windowLandingEnabled ? "已开启" : "已关闭", "SUCCESS", 1800); contentView?.needsDisplay = true }
    @objc private func changeWindowLevel(_ sender: NSMenuItem) {
        let value = sender.representedObject as? String ?? "topmost"
        guard ["topmost", "desktop"].contains(value) else { return }
        windowLevelMode = value
        if let panel {
            applyWindowLevel(to: panel)
            // A normal-level panel can be left behind the desktop after a
            // level transition. Reorder it explicitly so selecting “普通”
            // never makes the pet look as if it disappeared.
            panel.orderFront(nil)
        }
        saveLayout()
        reportSettings(["windowLevel": windowLevelMode])
        showOverlay("窗口层级已切换", value == "topmost" ? "桌面顶部（始终置顶）" : "普通窗口层级", "SUCCESS", 1800)
        contentView?.needsDisplay = true
    }
    @objc private func toggleHoverPause(_ sender: NSMenuItem) { hoverPauseEnabled.toggle(); reportSettings(["hoverPause": hoverPauseEnabled]); showOverlay("悬停暂停", hoverPauseEnabled ? "已开启" : "已关闭", "SUCCESS", 1800); contentView?.needsDisplay = true }

    @objc private func changeSize(_ sender: NSMenuItem) {
        scale = Self.clampedScale(Double(sender.tag) / 100.0)
        resizeAndReposition()
        saveLayout()
        reportSettings(["scale": scale])
    }

    @objc private func changeBubbleScale(_ sender: NSMenuItem) {
        bubbleScale = Self.clampedBubbleScale(Double(sender.tag) / 100.0)
        resizeAndReposition()
        saveLayout()
        reportSettings(["bubbleScale": bubbleScale])
    }

    @objc private func changeModel(_ sender: NSMenuItem) {
        selectedModel = sender.representedObject as? String ?? ""
        reportSettings(["model": selectedModel])
        showOverlay("模型已切换", selectedModel.isEmpty ? "Codex 默认" : selectedModel, "SUCCESS", 1800)
        contentView?.needsDisplay = true
    }

    @objc private func changeReasoningEffort(_ sender: NSMenuItem) {
        reasoningEffort = sender.representedObject as? String ?? ""
        reportSettings(["reasoningEffort": reasoningEffort])
        showOverlay("推理强度已切换", reasoningEffort.isEmpty ? "跟随模型" : reasoningEffort, "SUCCESS", 1800)
        contentView?.needsDisplay = true
    }

    private func setReducedMotion(_ enabled: Bool) {
        reducedMotion = enabled
        restartAnimTimer()
        if enabled {
            microTimer?.invalidate()
            cancelDragReleaseChain()
        } else {
            scheduleMicro()
        }
    }

    @objc private func toggleReducedMotion(_ sender: NSMenuItem) {
        setReducedMotion(sender.state == .off)
        saveLayout()
        reportSettings(["reducedMotion": reducedMotion])
        contentView?.needsDisplay = true
    }

    @objc private func openWebUI(_ sender: Any?) {
        if let url = URL(string: webuiURL) {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func accessibilityPermission(_ sender: Any?) {
        Permissions.requestAccessibility()
    }

    @objc private func hidePet(_ sender: Any?) {
        panel?.orderOut(nil)
    }

    @objc private func quitFromMenu(_ sender: Any?) {
        quit(reason: "user")
    }

    // MARK: - Protocol

    func apply(_ message: [String: Any]) {
        logEvent(message)
        guard let kind = message["kind"] as? String else { return }
        switch kind {
        case "shutdown":
            quit(reason: "host")
        case "state":
            handleState(message)
        case "pulse":
            handlePulse(message)
        case "task":
            handleTask(message)
        case "reply":
            inputField?.isEnabled = true
            sendButton?.isEnabled = true
            appendConversation(role: "Codex", text: Self.stringValue(message["message"]) ?? "")
            showStatus(Self.stringValue(message["message"]) ?? "Codex 回复完成", Self.stringValue(message["detail"]) ?? "", "SUCCESS", 10000)
            contentView?.needsDisplay = true
        case "reply_error":
            inputField?.isEnabled = true
            sendButton?.isEnabled = true
            showStatus(Self.stringValue(message["message"]) ?? "Codex 没有回复", Self.stringValue(message["detail"]) ?? "", "ERROR", 10000)
            contentView?.needsDisplay = true
        case "tasks":
            tasks = (message["tasks"] as? [[String: Any]]) ?? []
            resizeAndReposition()
            contentView?.needsDisplay = true
        case "config":
            applyConfig(message)
        default:
            break
        }
        maybeSaveSnapshot()
    }

    private func handleState(_ message: [String: Any]) {
        let state = Self.stringValue(message["state"]) ?? "IDLE"
        let activity = Self.stringValue(message["activity"])
        displayState = state
        model.applyState(state, activity: activity)
        clearOverlay()
        showStatus(
            Self.stringValue(message["message"]) ?? Self.labels[state] ?? state,
            Self.stringValue(message["detail"]) ?? "",
            state,
            Self.persistentStates.contains(state) ? nil : 4200
        )
        contentView?.needsDisplay = true
    }

    private func handlePulse(_ message: [String: Any]) {
        let state = Self.stringValue(message["state"]) ?? "IDLE"
        let ttl = max(250, Self.intValue(message["ttlMs"]) ?? 1800)
        let resumeState = Self.stringValue(message["resumeState"]) ?? model.baseState
        let resumeActivity = Self.stringValue(message["resumeActivity"])
        model.applyPulse(
            state: state,
            ttlMs: ttl,
            nowMs: Self.nowMs(),
            resumeState: resumeState,
            resumeActivity: resumeActivity
        )
        showStatus(
            Self.stringValue(message["resumeMessage"]) ?? Self.labels[resumeState] ?? resumeState,
            Self.stringValue(message["resumeDetail"]) ?? "",
            resumeState,
            Self.persistentStates.contains(resumeState) ? nil : ttl + 2200
        )
        showOverlay(
            Self.stringValue(message["message"]) ?? Self.labels[state] ?? state,
            Self.stringValue(message["detail"]) ?? "",
            state,
            ttl
        )
        if state == "SUCCESS" || state == "ERROR" {
            notifyAlert(state)
        }
        contentView?.needsDisplay = true
    }

    private func handleTask(_ message: [String: Any]) {
        task = Self.stringValue(message["task"]) ?? ""
        showStatus(
            Self.stringValue(message["message"]) ?? task,
            Self.stringValue(message["detail"]) ?? "",
            model.baseState,
            Self.persistentStates.contains(model.baseState) ? nil : 6000
        )
        contentView?.needsDisplay = true
    }

    @objc private func submitInput(_ sender: Any?) {
        guard let text = inputField?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return }
        inputField?.stringValue = ""
        inputField?.isEnabled = false
        sendButton?.isEnabled = false
        appendConversation(role: "你", text: text)
        ProtocolIO.shared.write([
            "protocolVersion": 1,
            "kind": "user_input",
            "text": text,
            "timestamp": Self.nowMs(),
        ])
        showStatus("已发送给 Codex", "等待 Codex 回复", "THINKING", nil)
        contentView?.needsDisplay = true
    }

    private func applyConfig(_ message: [String: Any]) {
        var changed = false
        if let value = Self.doubleValue(message["scale"]), value != scale {
            scale = Self.clampedScale(value)
            changed = true
        }
        if let value = Self.doubleValue(message["bubbleScale"]), value != bubbleScale {
            bubbleScale = Self.clampedBubbleScale(value)
            changed = true
        }
        if let value = message["reducedMotion"] as? Bool, value != reducedMotion {
            setReducedMotion(value)
            changed = true
        }
        if let value = message["soundEnabled"] as? Bool {
            soundEnabled = value
        }
        if let value = message["activityLevel"] as? String, ["quiet", "normal", "lively"].contains(value) {
            activityLevel = value
            if !reducedMotion {
                scheduleMicro()
            }
        }
        if let value = message["bubbleMode"] as? String, ["always", "hidden", "custom"].contains(value) {
            bubbleMode = value
            changed = true
        }
        if let value = message["bubbleStates"] as? [String] {
            bubbleStates = value
            changed = true
        }
        if let value = Self.doubleValue(message["walkSpeed"]) { walkSpeed = min(180, max(20, value)); changed = true }
        if let value = message["movementMode"] as? String, ["follow", "quiet", "lively", "still", "wander"].contains(value) { movementMode = normalizedMovementMode(value); walkToken &+= 1; walkTargetX = nil; model.clearOverlay(); activityDirector.movementMode = movementMode; changed = true }
        if changed {
            resizeAndReposition()
            saveLayout()
        }
    }

    func quit(reason: String, reportClosed: Bool = true) {
        guard !quitting else { return }
        quitting = true
        saveLayout()
        animTimer?.invalidate()
        keepFrontTimer?.invalidate()
        microTimer?.invalidate()
        shakeTimer?.invalidate()
        if reportClosed {
            ProtocolIO.shared.write([
                "protocolVersion": 1,
                "kind": "closed",
                "reason": reason,
                "timestamp": Self.nowMs(),
            ])
        }
        panel?.close()
        NSApp.terminate(nil)
    }

    // MARK: - Status helpers

    private func showStatus(_ message: String, _ detail: String, _ state: String, _ ttlMs: Int?) {
        statusMessage = message
        statusDetail = detail
        statusState = state
        statusDeadlineMs = ttlMs.map { Self.nowMs() + $0 }
    }

    private func showOverlay(_ message: String, _ detail: String, _ state: String, _ ttlMs: Int) {
        overlayMessage = message
        overlayDetail = detail.isEmpty ? statusDetail : detail
        overlayState = state
        overlayDeadlineMs = Self.nowMs() + ttlMs
    }

    private func clearOverlay() {
        overlayMessage = ""
        overlayDetail = ""
        overlayState = nil
        overlayDeadlineMs = nil
    }

    private func runDragReleaseChain() {
        dragChainID &+= 1
        playDragReleaseStage(0, token: dragChainID)
    }

    private func playDragReleaseStage(_ index: Int, token: Int) {
        guard token == dragChainID, !dragging else { return }
        guard !reducedMotion, index < AnimationModel.dragReleaseStages.count else {
            clearDragReleaseOverlay()
            return
        }

        let stage = AnimationModel.dragReleaseStages[index]
        let previousFrame = model.frame
        let previousClip = model.activeClipName
        guard model.playOverlay(stage.clipName) else {
            clearDragReleaseOverlay()
            return
        }
        syncFrameTransition(previousFrame: previousFrame, previousClip: previousClip)
        contentView?.needsDisplay = true

        DispatchQueue.main.asyncAfter(deadline: .now() + Double(stage.holdMs) / 1000.0) { [weak self] in
            self?.playDragReleaseStage(index + 1, token: token)
        }
    }

    private func clearDragReleaseOverlay() {
        guard !dragging else { return }
        let previousFrame = model.frame
        let previousClip = model.activeClipName
        model.clearOverlay()
        syncFrameTransition(previousFrame: previousFrame, previousClip: previousClip)
        contentView?.needsDisplay = true
    }

    private func cancelDragReleaseChain() {
        dragChainID &+= 1
        let releaseClips = Set(AnimationModel.dragReleaseStages.map { $0.clipName } + ["dragging_dizzy"])
        if !dragging, releaseClips.contains(model.activeClipName) {
            clearDragReleaseOverlay()
        }
    }

    func currentCard() -> (title: String, detail: String, state: String)? {
        let now = Self.nowMs()
        if !overlayMessage.isEmpty && (overlayDeadlineMs == nil || now < overlayDeadlineMs!) {
            return (overlayMessage, overlayDetail, overlayState ?? statusState)
        }
        if !statusMessage.isEmpty && (statusDeadlineMs == nil || now < statusDeadlineMs!) {
            return (statusMessage, statusDetail, statusState)
        }
        return nil
    }

    private func bubbleVisible() -> Bool {
        if bubbleMode == "hidden" { return false }
        if bubbleMode == "always" { return true }
        if tasks.count >= 2 {
            return tasks.contains { task in
                guard let state = task["state"] as? String else { return false }
                return bubbleStates.contains(state)
            }
        }
        return bubbleStates.contains(overlayState ?? statusState)
    }

    private func notifyAlert(_ state: String) {
        if soundEnabled {
            let filename = state == "SUCCESS" ? "success" : "error"
            if let url = Bundle.main.resourceURL?
                .appendingPathComponent("assets/sounds/\(filename).wav"),
               let sound = NSSound(contentsOf: url, byReference: true) {
                sound.play()
            }
        }
        shakeWindow()
        Permissions.requestNotificationAuthorizationIfNeeded()
        guard Bundle.main.bundleIdentifier != nil else { return }
        let content = UNMutableNotificationContent()
        content.title = state == "SUCCESS" ? "任务完成" : "任务出错"
        content.body = state == "SUCCESS" ? "DSH 任务已完成" : "DSH 任务遇到问题"
        content.sound = soundEnabled ? .default : nil
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error = error {
                FileHandle.standardError.write(Data("Notification error: \(error)\n".utf8))
            }
        }
    }

    private func shakeWindow() {
        guard let panel = panel else { return }
        shakeTimer?.invalidate()
        shakeOrigin = panel.frame.origin
        shakeCount = 0
        shakeTimer = Timer.scheduledTimer(withTimeInterval: 0.03, repeats: true) { [weak self] _ in
            self?.shakeTick()
        }
    }

    private func shakeTick() {
        guard let panel = panel, let origin = shakeOrigin else {
            shakeTimer?.invalidate()
            shakeTimer = nil
            return
        }
        let offsets: [(CGFloat, CGFloat)] = [(6, 0), (-6, 0), (4, 0), (-4, 0), (2, 0), (-2, 0), (0, 0)]
        if shakeCount < offsets.count {
            let offset = offsets[shakeCount]
            panel.setFrameOrigin(NSPoint(x: origin.x + offset.0, y: origin.y + offset.1))
            shakeCount += 1
        } else {
            shakeTimer?.invalidate()
            shakeTimer = nil
            panel.setFrameOrigin(origin)
        }
    }

    private func restartAnimTimer() {
        lastTickMs = Self.nowMs()
        startAnimTimer()
    }

    // MARK: - Drawing

    func drawPet(in view: NSView) {
        guard let image = frameImage(for: model.frame) else { return }
        let phase = CACurrentMediaTime()
        var motion = model.activeClip.motion
        if reducedMotion {
            motion = nil
        }
        var scaleExtra: CGFloat = 1
        var angle: CGFloat = 0
        var offsetX: CGFloat = 0
        var offsetY: CGFloat = 0
        let clipName = model.activeClipName

        switch motion {
        case "breathe":
            scaleExtra = 1 + 0.02 * CGFloat(sin(phase * 2.5))
            angle = CGFloat(sin(phase * 2.5)) * 1.5
        case "think":
            offsetY = CGFloat(sin(phase * 2.8)) * 3
            angle = CGFloat(sin(phase * 1.3)) * 0.8
        case "work":
            offsetX = CGFloat(sin(phase * 5.4)) * 3
            angle = CGFloat(sin(phase * 3.1)) * 1.0
        case "wait":
            offsetY = CGFloat(sin(phase * 1.8)) * 1
            angle = CGFloat(sin(phase * 1.2)) * 0.8
        case "bounce":
            offsetY = -abs(CGFloat(sin(phase * 5.2))) * 8
            scaleExtra = 1 + 0.02 * CGFloat(sin(phase * 5.2))
        case "shake", "dizzy":
            offsetX = CGFloat(sin(phase * 11.0)) * 4
            angle = CGFloat(sin(phase * 11.0)) * 1.5
        case "float":
            offsetY = CGFloat(sin(phase * 3.0)) * 4
            angle = CGFloat(sin(phase * 1.6)) * 1.0
        default:
            break
        }
        if clipName == "working_search" || clipName == "working_command" {
            offsetY = -abs(CGFloat(sin(phase * 4.5))) * 5
            angle = CGFloat(sin(phase * 9.0)) * 2.5
        }
        offsetX *= scale
        offsetY *= scale

        var fadeAlpha: CGFloat = 1
        var fadeImage: NSImage?
        if let fromFrame = fadeFromFrame, let fromImage = frameImage(for: fromFrame) {
            let elapsed = CACurrentMediaTime() - fadeStarted
            if elapsed < fadeDuration {
                fadeAlpha = min(1, pow(CGFloat(elapsed / fadeDuration), 0.7))
                fadeImage = fromImage
            } else {
                fadeFromFrame = nil
            }
        }

        let pet = petRect()
        let baseWidth = pet.width
        let baseHeight = pet.height
        let drawWidth = baseWidth * scaleExtra
        let drawHeight = baseHeight * scaleExtra
        let x = pet.minX + (baseWidth - drawWidth) / 2 + offsetX
        var y = pet.minY + (baseHeight - drawHeight) / 2 + offsetY
        // The pet is anchored to the bottom of the shared window. Do not
        // move it based on the card height; that made long replies shift the
        // character and exposed a stale black silhouette beside it.
        let centerX = x + drawWidth / 2
        let centerY = y + drawHeight / 2

        func draw(_ img: NSImage, alpha: CGFloat) {
            NSGraphicsContext.saveGraphicsState()
            guard let ctx = NSGraphicsContext.current?.cgContext else {
                NSGraphicsContext.restoreGraphicsState()
                return
            }
            ctx.saveGState()
            ctx.translateBy(x: centerX, y: centerY)
            if angle != 0 {
                ctx.rotate(by: angle * .pi / 180)
            }
            // The content view is flipped (top-left origin). NSImage drawing
            // does not compensate for a flipped context, which would render
            // the pet vertically mirrored. Mirror about the image's own
            // center so it draws right-side up while staying in place.
            ctx.scaleBy(x: 1, y: -1)
            img.draw(
                in: NSRect(x: -drawWidth / 2, y: -drawHeight / 2, width: drawWidth, height: drawHeight),
                from: NSRect(origin: .zero, size: img.size),
                operation: .sourceOver,
                fraction: alpha
            )
            ctx.restoreGState()
            NSGraphicsContext.restoreGraphicsState()
        }

        if fadeAlpha < 1, let fadeImage = fadeImage {
            draw(fadeImage, alpha: 1)
        }
        draw(image, alpha: fadeAlpha)
    }

    func drawCard(in view: NSView) {
        guard bubbleVisible() else { return }
        let rect = bubbleRect()
        if !conversation.isEmpty {
            drawConversationCard(rect: rect)
        } else if tasks.count >= 2 {
            drawTaskCard(rect: rect)
        } else if let card = currentCard() {
            drawStatusCard(rect: rect, card: card)
        }
    }

    private func drawConversationCard(rect: NSRect) {
        let s = bubbleScale
        let corner: CGFloat = 16 * s
        let cardPath = NSBezierPath(roundedRect: rect, xRadius: corner, yRadius: corner)
        NSColor(calibratedRed: 0.988, green: 0.988, blue: 0.992, alpha: 0.97).setFill()
        cardPath.fill()
        NSColor(calibratedWhite: 0.85, alpha: 0.8).setStroke()
        cardPath.lineWidth = 1
        cardPath.stroke()
        let textX = rect.minX + 16 * s
        let textWidth = max(40, rect.width - 32 * s)
        drawText("最近对话 · \(conversation.count) 条", in: NSRect(x: textX, y: rect.minY + 10 * s, width: textWidth, height: 22 * s), font: NSFont.systemFont(ofSize: 12 * s, weight: .semibold), color: Self.hex("#25282D"))
        var y = rect.minY + 36 * s - conversationScroll
        let bottom = rect.maxY - 8 * s
        for item in conversation {
            let prefix = item.role == "你" ? "你：" : "Codex："
            let line = prefix + item.text
            let font = NSFont.systemFont(ofSize: 11 * s, weight: item.role == "你" ? .regular : .semibold)
            let bounds = (line as NSString).boundingRect(with: NSSize(width: textWidth, height: 1000), options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [.font: font])
            let height = max(18 * s, ceil(bounds.height) + 4 * s)
            if y + height >= rect.minY + 34 * s && y <= bottom {
                drawText(line, in: NSRect(x: textX, y: y, width: textWidth, height: height), font: font, color: item.role == "你" ? Self.hex("#747981") : Self.hex("#1677FF"))
            }
            y += height + 6 * s
        }
        let contentHeight = max(1, y - (rect.minY + 36 * s) + conversationScroll)
        let viewportHeight = max(1, rect.height - 42 * s)
        if contentHeight > viewportHeight {
            let track = NSRect(x: rect.maxX - 10 * s, y: rect.minY + 34 * s, width: 4 * s, height: viewportHeight)
            NSColor(calibratedWhite: 0.68, alpha: 0.35).setFill()
            NSBezierPath(roundedRect: track, xRadius: 2 * s, yRadius: 2 * s).fill()
            let thumbHeight = max(20 * s, viewportHeight * viewportHeight / contentHeight)
            let maxOffset = max(1, contentHeight - viewportHeight)
            let thumbY = track.minY + (viewportHeight - thumbHeight) * (conversationScroll / maxOffset)
            NSColor(calibratedRed: 0.12, green: 0.48, blue: 0.96, alpha: 0.9).setFill()
            NSBezierPath(roundedRect: NSRect(x: track.minX, y: thumbY, width: track.width, height: thumbHeight), xRadius: 2 * s, yRadius: 2 * s).fill()
        }
    }

    func scrollConversation(by delta: CGFloat) {
        guard !conversation.isEmpty else { return }
        let maxScroll = max(0, CGFloat(conversation.count * 28) * bubbleScale - (cardHeightPoints - 42 * bubbleScale))
        conversationScroll = min(max(0, conversationScroll + delta), maxScroll)
        contentView?.needsDisplay = true
    }

    private func drawStatusCard(rect: NSRect, card: (title: String, detail: String, state: String)) {
        let s = bubbleScale
        let corner: CGFloat = 16 * s
        let shadow1 = NSRect(x: rect.minX + 1, y: rect.minY + 6, width: rect.width - 2, height: rect.height)
        let shadow2 = NSRect(x: rect.minX, y: rect.minY + 3, width: rect.width, height: rect.height)
        NSColor(calibratedWhite: 0.05, alpha: 0.05).setFill()
        NSBezierPath(roundedRect: shadow1, xRadius: corner, yRadius: corner).fill()
        NSColor(calibratedWhite: 0.08, alpha: 0.08).setFill()
        NSBezierPath(roundedRect: shadow2, xRadius: corner, yRadius: corner).fill()

        let cardPath = NSBezierPath(roundedRect: rect, xRadius: corner, yRadius: corner)
        NSColor(calibratedRed: 0.988, green: 0.988, blue: 0.992, alpha: 0.97).setFill()
        cardPath.fill()
        NSColor(calibratedWhite: 0.85, alpha: 0.8).setStroke()
        cardPath.lineWidth = 1
        cardPath.stroke()

        let iconCenter = NSPoint(x: rect.maxX - 34 * s, y: rect.midY)
        drawStatusIcon(center: iconCenter, state: card.state, scale: s)

        let textX = rect.minX + 16 * s
        let textWidth = max(40, rect.width - 102 * s)
        let titleFont = NSFont.systemFont(ofSize: max(8.0, 11.0 * s), weight: .semibold)
        let detailFont = NSFont.systemFont(ofSize: max(7.0, 9.0 * s))
        drawText(
            card.title,
            in: NSRect(x: textX, y: rect.minY + 15 * s, width: textWidth, height: max(12, 27 * s)),
            font: titleFont,
            color: Self.hex("#25282D")
        )
        drawText(
            card.detail,
            in: NSRect(x: textX, y: rect.minY + 43 * s, width: textWidth, height: max(12, 24 * s)),
            font: detailFont,
            color: Self.hex("#747981")
        )
    }

    private func drawStatusIcon(center: NSPoint, state: String, scale s: CGFloat) {
        let (bgHex, fgHex) = Self.statusColors[state] ?? ("#ECEEF1", "#747A84")
        let radius: CGFloat = 23 * s
        Self.hex(bgHex).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)).fill()

        let foreground = Self.hex(fgHex)
        let lineWidth: CGFloat = 3 * s
        switch state {
        case "SUCCESS":
            strokeLine(from: NSPoint(x: center.x - 10 * s, y: center.y),
                       to: NSPoint(x: center.x - 3 * s, y: center.y + 8 * s),
                       width: lineWidth, color: foreground)
            strokeLine(from: NSPoint(x: center.x - 3 * s, y: center.y + 8 * s),
                       to: NSPoint(x: center.x + 12 * s, y: center.y - 10 * s),
                       width: lineWidth, color: foreground)
        case "ERROR":
            strokeLine(from: NSPoint(x: center.x - 8 * s, y: center.y - 8 * s),
                       to: NSPoint(x: center.x + 8 * s, y: center.y + 8 * s),
                       width: lineWidth, color: foreground)
            strokeLine(from: NSPoint(x: center.x + 8 * s, y: center.y - 8 * s),
                       to: NSPoint(x: center.x - 8 * s, y: center.y + 8 * s),
                       width: lineWidth, color: foreground)
        case "WAITING":
            strokeLine(from: NSPoint(x: center.x, y: center.y - 10 * s),
                       to: NSPoint(x: center.x, y: center.y + 3 * s),
                       width: lineWidth, color: foreground)
            foreground.setFill()
            NSBezierPath(ovalIn: NSRect(x: center.x - 2 * s, y: center.y + 9 * s, width: 4 * s, height: 4 * s)).fill()
        case "THINKING", "WORKING":
            foreground.setFill()
            for offset in [-9.0, 0.0, 9.0] {
                NSBezierPath(ovalIn: NSRect(x: center.x + CGFloat(offset) * s - 3 * s,
                                            y: center.y - 3 * s,
                                            width: 6 * s,
                                            height: 6 * s)).fill()
            }
        default:
            foreground.setFill()
            NSBezierPath(ovalIn: NSRect(x: center.x - 5 * s, y: center.y - 5 * s, width: 10 * s, height: 10 * s)).fill()
        }
    }

    private func drawTaskCard(rect: NSRect) {
        let s = bubbleScale
        let corner: CGFloat = 16 * s
        NSColor(calibratedWhite: 0.05, alpha: 0.05).setFill()
        NSBezierPath(roundedRect: NSRect(x: rect.minX + 1, y: rect.minY + 6, width: rect.width - 2, height: rect.height),
                     xRadius: corner, yRadius: corner).fill()
        let cardPath = NSBezierPath(roundedRect: rect, xRadius: corner, yRadius: corner)
        NSColor(calibratedRed: 0.988, green: 0.988, blue: 0.992, alpha: 0.97).setFill()
        cardPath.fill()
        NSColor(calibratedWhite: 0.85, alpha: 0.8).setStroke()
        cardPath.lineWidth = 1
        cardPath.stroke()

        let textX = rect.minX + 16 * s
        let textWidth = max(40, rect.width - 32 * s)
        let titleFont = NSFont.systemFont(ofSize: max(8.0, 11.0 * s), weight: .semibold)
        let detailFont = NSFont.systemFont(ofSize: max(7.0, 9.0 * s))
        drawText(
            "\(tasks.count) 个任务进行中",
            in: NSRect(x: textX, y: rect.minY + 10 * s, width: textWidth, height: max(12, 22 * s)),
            font: titleFont,
            color: Self.hex("#25282D")
        )

        for (index, task) in tasks.prefix(3).enumerated() {
            let rowY = rect.minY + (36 + CGFloat(index) * 24) * s
            let state = Self.stringValue(task["state"]) ?? "IDLE"
            let stateLabel = Self.labels[state] ?? state
            let label = Self.stringValue(task["project"])
                ?? Self.stringValue(task["task"])
                ?? Self.stringValue(task["message"])
                ?? stateLabel
            let line = "\(stateLabel) · \(label)"
            let (_, fgHex) = Self.statusColors[state] ?? ("#ECEEF1", "#747A84")
            Self.hex(fgHex).setFill()
            NSBezierPath(ovalIn: NSRect(x: textX, y: rowY + 4 * s, width: 8 * s, height: 8 * s)).fill()
            drawText(
                line,
                in: NSRect(x: textX + 14 * s, y: rowY, width: textWidth - 14 * s, height: max(12, 20 * s)),
                font: detailFont,
                color: Self.hex("#747981")
            )
        }
        if tasks.count > 3 {
            drawText(
                "还有 \(tasks.count - 3) 个任务…",
                in: NSRect(x: textX + 14 * s, y: rect.minY + (36 + 3 * 24) * s,
                           width: textWidth, height: max(12, 20 * s)),
                font: detailFont,
                color: Self.hex("#9AA0A6")
            )
        }
    }

    private func drawText(_ text: String, in rect: NSRect, font: NSFont, color: NSColor) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byWordWrapping
        paragraph.alignment = .left
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: color,
            .paragraphStyle: paragraph,
        ]
        (text as NSString).draw(in: rect, withAttributes: attributes)
    }

    private func strokeLine(from: NSPoint, to: NSPoint, width: CGFloat, color: NSColor) {
        color.setStroke()
        let path = NSBezierPath()
        path.lineWidth = width
        path.lineCapStyle = .round
        path.move(to: from)
        path.line(to: to)
        path.stroke()
    }

    // MARK: - Misc

    private func maybeSaveSnapshot() {
        guard let snapshotURL = snapshotURL, !snapshotSaved, !isHeadless() else { return }
        snapshotSaved = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) { [weak self] in
            guard let self = self, let view = self.contentView else { return }
            if let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: rep)
                if let data = rep.representation(using: .png, properties: [:]) {
                    try? data.write(to: snapshotURL)
                }
            }
        }
    }

    private func logEvent(_ message: [String: Any]) {
        guard let eventLogURL = eventLogURL,
              let data = try? JSONSerialization.data(withJSONObject: message),
              let line = String(data: data, encoding: .utf8) else { return }
        let payload = Data((line + "\n").utf8)
        if let handle = try? FileHandle(forWritingTo: eventLogURL) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: payload)
        } else {
            try? payload.write(to: eventLogURL)
        }
    }

    static func clampedScale(_ value: Double) -> Double {
        min(1.4, max(0.55, value))
    }

    static func clampedBubbleScale(_ value: Double) -> Double {
        min(1.2, max(0.8, value))
    }

    private static func stringValue(_ value: Any?) -> String? {
        if let string = value as? String { return string }
        if let number = value as? NSNumber { return number.stringValue }
        return nil
    }

    private static func intValue(_ value: Any?) -> Int? {
        if let int = value as? Int { return int }
        if let double = value as? Double { return Int(double) }
        if let string = value as? String { return Int(string) }
        return nil
    }

    private static func doubleValue(_ value: Any?) -> Double? {
        if let double = value as? Double { return double }
        if let int = value as? Int { return Double(int) }
        if let string = value as? String { return Double(string) }
        return nil
    }

    private static func nowMs() -> Int {
        Int(Date().timeIntervalSince1970 * 1000)
    }

    private func reportSettings(_ values: [String: Any]) {
        ProtocolIO.shared.write([
            "protocolVersion": 1,
            "kind": "settings",
            "timestamp": Self.nowMs(),
        ].merging(values) { _, new in new })
    }

    private static func hex(_ hex: String) -> NSColor {
        let value = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        if value.count == 6 {
            let red = Double(Int(value.prefix(2), radix: 16) ?? 0) / 255
            let green = Double(Int(value.dropFirst(2).prefix(2), radix: 16) ?? 0) / 255
            let blue = Double(Int(value.dropFirst(4).prefix(2), radix: 16) ?? 0) / 255
            return NSColor(calibratedRed: red, green: green, blue: blue, alpha: 1)
        }
        return .gray
    }
}
