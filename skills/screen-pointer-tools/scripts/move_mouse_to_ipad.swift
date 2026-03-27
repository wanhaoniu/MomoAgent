#!/usr/bin/env swift

import AppKit
import ApplicationServices
import Foundation

enum Command: String {
    case point
    case displays
    case push
    case sweep
}

enum Edge: String {
    case left
    case right
    case up
    case down
}

struct ParsedArguments {
    let command: Command
    let options: [String: String]
    let flags: Set<String>
}

struct DisplayInfo {
    let id: CGDirectDisplayID
    let bounds: CGRect
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    fputs(message + "\n", stderr)
    exit(code)
}

func usage() {
    let text = """
    Usage:
      move_mouse_to_ipad.swift point [--json]
      move_mouse_to_ipad.swift displays
      move_mouse_to_ipad.swift push --edge left|right|up|down [--lane 0.5] [--display cursor|main|ID] [--approach-step 32] [--overshoot 96] [--push-count 10] [--delay-ms 16] [--settle-ms 120] [--relative] [--dry-run]
      move_mouse_to_ipad.swift sweep --edge left|right|up|down [--display cursor|main|ID] [--min-lane 0.2] [--max-lane 0.8] [--samples 32] [--cycles 0] [--overshoot 140] [--delay-ms 24] [--approach-step 32] [--relative] [--dry-run]

    Examples:
      swift scripts/move_mouse_to_ipad.swift displays
      swift scripts/move_mouse_to_ipad.swift point --json
      swift scripts/move_mouse_to_ipad.swift push --edge right
      swift scripts/move_mouse_to_ipad.swift push --edge left --lane 0.35 --dry-run
      swift scripts/move_mouse_to_ipad.swift push --edge left --relative
      swift scripts/move_mouse_to_ipad.swift sweep --edge left --cycles 0
    """
    print(text)
}

func parseArguments() -> ParsedArguments {
    let args = Array(CommandLine.arguments.dropFirst())
    guard let rawCommand = args.first, let command = Command(rawValue: rawCommand) else {
        usage()
        exit(64)
    }

    var options: [String: String] = [:]
    var flags = Set<String>()
    var index = 1

    while index < args.count {
        let argument = args[index]
        if argument.hasPrefix("--") {
            if index + 1 < args.count && !args[index + 1].hasPrefix("--") {
                options[argument] = args[index + 1]
                index += 2
            } else {
                flags.insert(argument)
                index += 1
            }
        } else {
            fail("Unexpected argument: \(argument)", code: 64)
        }
    }

    return ParsedArguments(command: command, options: options, flags: flags)
}

func format(_ number: Double) -> String {
    String(format: "%.2f", number)
}

func currentPoint() -> CGPoint {
    NSEvent.mouseLocation
}

func mainDisplayHeight() -> Double {
    CGDisplayBounds(CGMainDisplayID()).height
}

func quartzPoint(fromAppKit point: CGPoint) -> CGPoint {
    CGPoint(x: point.x, y: mainDisplayHeight() - point.y)
}

func appKitRect(fromQuartz rect: CGRect) -> CGRect {
    CGRect(
        x: rect.origin.x,
        y: mainDisplayHeight() - rect.origin.y - rect.height,
        width: rect.width,
        height: rect.height
    )
}

func parseDoubleOption(_ rawValue: String?, name: String, default defaultValue: Double) -> Double {
    guard let rawValue else {
        return defaultValue
    }
    guard let value = Double(rawValue) else {
        fail("Invalid \(name): \(rawValue)", code: 64)
    }
    return value
}

func parseIntOption(_ rawValue: String?, name: String, default defaultValue: Int) -> Int {
    guard let rawValue else {
        return defaultValue
    }
    guard let value = Int(rawValue), value > 0 else {
        fail("Invalid \(name): \(rawValue)", code: 64)
    }
    return value
}

func parseNonNegativeIntOption(_ rawValue: String?, name: String, default defaultValue: Int) -> Int {
    guard let rawValue else {
        return defaultValue
    }
    guard let value = Int(rawValue), value >= 0 else {
        fail("Invalid \(name): \(rawValue)", code: 64)
    }
    return value
}

func activeDisplays() -> [DisplayInfo] {
    var count: UInt32 = 0
    guard CGGetActiveDisplayList(0, nil, &count) == .success else {
        fail("Unable to query active displays.")
    }

    var ids = Array(repeating: CGDirectDisplayID(), count: Int(count))
    guard CGGetActiveDisplayList(count, &ids, &count) == .success else {
        fail("Unable to load active display list.")
    }

    return ids.map { id in
        DisplayInfo(id: id, bounds: appKitRect(fromQuartz: CGDisplayBounds(id)))
    }
}

func displayContaining(point: CGPoint, displays: [DisplayInfo]) -> DisplayInfo? {
    displays.first(where: { $0.bounds.contains(point) })
}

func resolveDisplay(selection: String?, current: CGPoint, displays: [DisplayInfo]) -> DisplayInfo {
    let requested = selection ?? "cursor"

    switch requested {
    case "cursor":
        if let display = displayContaining(point: current, displays: displays) {
            return display
        }
    case "main":
        let mainID = CGMainDisplayID()
        if let display = displays.first(where: { $0.id == mainID }) {
            return display
        }
    default:
        if let id = UInt32(requested), let display = displays.first(where: { $0.id == id }) {
            return display
        }
    }

    fail("Unable to resolve display '\(requested)'. Try `displays` first.", code: 64)
}

func requireAccessibility() {
    if !AXIsProcessTrusted() {
        fail(
            """
            Accessibility permission is required.

            If you run this from Terminal, enable Accessibility for Terminal.
            If you run this from Codex, enable Accessibility for Codex.

            System Settings > Privacy & Security > Accessibility
            """,
            code: 2
        )
    }
}

func sleepMilliseconds(_ milliseconds: Int) {
    usleep(useconds_t(milliseconds * 1_000))
}

func postMouseMove(to point: CGPoint) {
    let quartzPoint = quartzPoint(fromAppKit: point)
    guard let event = CGEvent(
        mouseEventSource: nil,
        mouseType: .mouseMoved,
        mouseCursorPosition: quartzPoint,
        mouseButton: .left
    ) else {
        fail("Failed to create mouse move event.")
    }
    event.post(tap: .cghidEventTap)
}

func postRelativeMouseMove(from start: CGPoint, to end: CGPoint) {
    let quartzStart = quartzPoint(fromAppKit: start)
    let quartzEnd = quartzPoint(fromAppKit: end)
    guard let event = CGEvent(
        mouseEventSource: nil,
        mouseType: .mouseMoved,
        mouseCursorPosition: quartzEnd,
        mouseButton: .left
    ) else {
        fail("Failed to create relative mouse move event.")
    }

    let dx = Int64(lround(quartzEnd.x - quartzStart.x))
    let dy = Int64(lround(quartzEnd.y - quartzStart.y))
    event.setIntegerValueField(.mouseEventDeltaX, value: dx)
    event.setIntegerValueField(.mouseEventDeltaY, value: dy)
    event.post(tap: .cghidEventTap)
}

func pointsBetween(from start: CGPoint, to end: CGPoint, stepSize: Double) -> [CGPoint] {
    let dx = end.x - start.x
    let dy = end.y - start.y
    let distance = hypot(dx, dy)

    if distance <= 0.001 {
        return [end]
    }

    let safeStep = max(stepSize, 1)
    let steps = max(Int(ceil(distance / safeStep)), 1)
    return (1...steps).map { index in
        let fraction = Double(index) / Double(steps)
        return CGPoint(x: start.x + dx * fraction, y: start.y + dy * fraction)
    }
}

func clampedLane(_ lane: Double) -> Double {
    if lane < 0 || lane > 1 {
        fail("--lane must be between 0 and 1.", code: 64)
    }
    return lane
}

func edgePoint(in bounds: CGRect, edge: Edge, lane: Double, overshoot: Double) -> CGPoint {
    let laneValue = clampedLane(lane)
    let xAlong = bounds.minX + bounds.width * laneValue
    let yAlong = bounds.minY + bounds.height * laneValue
    let inset = 2.0

    switch edge {
    case .left:
        return CGPoint(x: bounds.minX + inset - overshoot, y: yAlong)
    case .right:
        return CGPoint(x: bounds.maxX - inset + overshoot, y: yAlong)
    case .up:
        return CGPoint(x: xAlong, y: bounds.maxY - inset + overshoot)
    case .down:
        return CGPoint(x: xAlong, y: bounds.minY + inset - overshoot)
    }
}

func printPoint(json: Bool) {
    let point = currentPoint()
    if json {
        print("{\"x\": \(format(point.x)), \"y\": \(format(point.y))}")
    } else {
        print("\(format(point.x)) \(format(point.y))")
    }
}

func printDisplays() {
    let displays = activeDisplays()
    let cursor = currentPoint()
    let mainID = CGMainDisplayID()

    for display in displays {
        var tags: [String] = []
        if display.id == mainID {
            tags.append("main")
        } else {
            tags.append("secondary")
        }
        if display.bounds.contains(cursor) {
            tags.append("cursor")
        }

        print(
            "\(tags.joined(separator: ","))\tid=\(display.id)\tx=\(format(display.bounds.origin.x))\ty=\(format(display.bounds.origin.y))\tw=\(format(display.bounds.size.width))\th=\(format(display.bounds.size.height))"
        )
    }
}

func emitPath(_ points: [CGPoint], delayMs: Int, relative: Bool) {
    var previous = currentPoint()

    for point in points {
        if relative {
            postRelativeMouseMove(from: previous, to: point)
        } else {
            postMouseMove(to: point)
        }
        previous = point
        sleepMilliseconds(delayMs)
    }
}

func pushTowardEdge(arguments: ParsedArguments) {
    guard let rawEdge = arguments.options["--edge"], let edge = Edge(rawValue: rawEdge) else {
        fail("Missing or invalid --edge. Use left, right, up, or down.", code: 64)
    }

    let lane = parseDoubleOption(arguments.options["--lane"], name: "--lane", default: 0.5)
    let approachStep = parseDoubleOption(arguments.options["--approach-step"], name: "--approach-step", default: 32)
    let overshoot = parseDoubleOption(arguments.options["--overshoot"], name: "--overshoot", default: 96)
    let pushCount = parseIntOption(arguments.options["--push-count"], name: "--push-count", default: 10)
    let delayMs = parseIntOption(arguments.options["--delay-ms"], name: "--delay-ms", default: 16)
    let settleMs = parseIntOption(arguments.options["--settle-ms"], name: "--settle-ms", default: 120)
    let dryRun = arguments.flags.contains("--dry-run")
    let relative = arguments.flags.contains("--relative")

    let displays = activeDisplays()
    let start = currentPoint()
    let display = resolveDisplay(selection: arguments.options["--display"], current: start, displays: displays)

    let approachPoint = edgePoint(in: display.bounds, edge: edge, lane: lane, overshoot: 0)
    let path = pointsBetween(from: start, to: approachPoint, stepSize: approachStep)

    print(
        """
        pushing edge=\(edge.rawValue) display=\(display.id) lane=\(format(lane)) relative=\(relative) dryRun=\(dryRun)
        start=(\(format(start.x)), \(format(start.y))) approach=(\(format(approachPoint.x)), \(format(approachPoint.y)))
        """
    )

    if dryRun {
        for point in path {
            print("approach \(format(point.x)) \(format(point.y))")
        }
        for index in 1...pushCount {
            let amount = overshoot * Double(index) / Double(pushCount)
            let point = edgePoint(in: display.bounds, edge: edge, lane: lane, overshoot: amount)
            print("push \(format(point.x)) \(format(point.y))")
        }
        return
    }

    requireAccessibility()

    emitPath(path, delayMs: delayMs, relative: relative)

    sleepMilliseconds(settleMs)

    // Repeated overshoot events are what encourage Universal Control to hand the pointer to the iPad.
    let overshootPath = (1...pushCount).map { index in
        let amount = overshoot * Double(index) / Double(pushCount)
        return edgePoint(in: display.bounds, edge: edge, lane: lane, overshoot: amount)
    }
    emitPath(overshootPath, delayMs: delayMs, relative: relative)
}

func sweepLanes(minLane: Double, maxLane: Double, samples: Int) -> [Double] {
    let clampedMin = clampedLane(minLane)
    let clampedMax = clampedLane(maxLane)
    if clampedMin >= clampedMax {
        fail("--min-lane must be smaller than --max-lane.", code: 64)
    }

    let safeSamples = max(samples, 2)
    let forward = (0..<safeSamples).map { index in
        let fraction = Double(index) / Double(safeSamples - 1)
        return clampedMin + (clampedMax - clampedMin) * fraction
    }
    let backward = Array(forward.dropFirst().dropLast().reversed())
    return forward + backward
}

func runSweep(edge: Edge, display: DisplayInfo, lanes: [Double], overshoot: Double, delayMs: Int, dryRun: Bool, relative: Bool) {
    if dryRun {
        for lane in lanes {
            let point = edgePoint(in: display.bounds, edge: edge, lane: lane, overshoot: overshoot)
            print("sweep lane=\(format(lane)) point=(\(format(point.x)), \(format(point.y)))")
        }
        return
    }

    let points = lanes.map { lane in
        edgePoint(in: display.bounds, edge: edge, lane: lane, overshoot: overshoot)
    }
    emitPath(points, delayMs: delayMs, relative: relative)
}

func approachPath(start: CGPoint, display: DisplayInfo, edge: Edge, minLane: Double, maxLane: Double, approachStep: Double) -> [CGPoint] {
    let centerLane = (minLane + maxLane) / 2
    let approachPoint = edgePoint(in: display.bounds, edge: edge, lane: centerLane, overshoot: 0)
    return pointsBetween(from: start, to: approachPoint, stepSize: approachStep)
}

func sweepApproachPoint(display: DisplayInfo, edge: Edge, minLane: Double, maxLane: Double) -> CGPoint {
    let centerLane = (minLane + maxLane) / 2
    return edgePoint(in: display.bounds, edge: edge, lane: centerLane, overshoot: 0)
}

func sweepPoints(edge: Edge, display: DisplayInfo, lanes: [Double], overshoot: Double) -> [CGPoint] {
    lanes.map { lane in
        let point = edgePoint(in: display.bounds, edge: edge, lane: lane, overshoot: overshoot)
        return point
    }
}

func sweepTowardEdge(arguments: ParsedArguments) {
    guard let rawEdge = arguments.options["--edge"], let edge = Edge(rawValue: rawEdge) else {
        fail("Missing or invalid --edge. Use left, right, up, or down.", code: 64)
    }

    let minLane = parseDoubleOption(arguments.options["--min-lane"], name: "--min-lane", default: 0.2)
    let maxLane = parseDoubleOption(arguments.options["--max-lane"], name: "--max-lane", default: 0.8)
    let approachStep = parseDoubleOption(arguments.options["--approach-step"], name: "--approach-step", default: 32)
    let overshoot = parseDoubleOption(arguments.options["--overshoot"], name: "--overshoot", default: 140)
    let samples = parseIntOption(arguments.options["--samples"], name: "--samples", default: 32)
    let cycles = parseNonNegativeIntOption(arguments.options["--cycles"], name: "--cycles", default: 0)
    let delayMs = parseIntOption(arguments.options["--delay-ms"], name: "--delay-ms", default: 24)
    let dryRun = arguments.flags.contains("--dry-run")
    let relative = arguments.flags.contains("--relative")

    let displays = activeDisplays()
    let start = currentPoint()
    let display = resolveDisplay(selection: arguments.options["--display"], current: start, displays: displays)
    let approachPoint = sweepApproachPoint(display: display, edge: edge, minLane: minLane, maxLane: maxLane)
    let path = approachPath(start: start, display: display, edge: edge, minLane: minLane, maxLane: maxLane, approachStep: approachStep)
    let lanes = sweepLanes(minLane: minLane, maxLane: maxLane, samples: samples)

    print(
        """
        sweeping edge=\(edge.rawValue) display=\(display.id) minLane=\(format(minLane)) maxLane=\(format(maxLane)) cycles=\(cycles == 0 ? "infinite" : String(cycles)) relative=\(relative) dryRun=\(dryRun)
        start=(\(format(start.x)), \(format(start.y))) approach=(\(format(approachPoint.x)), \(format(approachPoint.y)))
        """
    )

    if dryRun {
        for point in path {
            print("approach \(format(point.x)) \(format(point.y))")
        }
        runSweep(edge: edge, display: display, lanes: lanes, overshoot: overshoot, delayMs: delayMs, dryRun: true, relative: relative)
        return
    }

    requireAccessibility()

    emitPath(path, delayMs: delayMs, relative: relative)

    if cycles == 0 {
        while true {
            runSweep(edge: edge, display: display, lanes: lanes, overshoot: overshoot, delayMs: delayMs, dryRun: false, relative: relative)
        }
    } else {
        for _ in 0..<cycles {
            runSweep(edge: edge, display: display, lanes: lanes, overshoot: overshoot, delayMs: delayMs, dryRun: false, relative: relative)
        }
    }
}

let arguments = parseArguments()

switch arguments.command {
case .point:
    printPoint(json: arguments.flags.contains("--json"))
case .displays:
    printDisplays()
case .push:
    pushTowardEdge(arguments: arguments)
case .sweep:
    sweepTowardEdge(arguments: arguments)
}
