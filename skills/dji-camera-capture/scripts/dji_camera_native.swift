import Foundation
import AVFoundation
import CoreImage

struct CLIError: Error {
    let message: String
}

struct DeviceRecord: Codable {
    let index: Int
    let name: String
    let unique_id: String
}

struct ListResponse: Codable {
    let ok: Bool
    let video_devices: [DeviceRecord]
    let audio_devices: [DeviceRecord]
}

struct PhotoResponse: Codable {
    let ok: Bool
    let photo_path: String
    let camera_name: String
}

struct RecordStartResponse: Codable {
    let ok: Bool
    let output_path: String
    let camera_name: String
    let with_audio: Bool
}

final class JSONPrinter {
    static func printObject<T: Encodable>(_ value: T) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(value)
        guard let text = String(data: data, encoding: .utf8) else {
            throw CLIError(message: "Failed to encode JSON output.")
        }
        FileHandle.standardOutput.write(text.data(using: .utf8)!)
        FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    }

    static func printErrorAndExit(_ message: String) -> Never {
        let payload: [String: Any] = ["ok": false, "error": message]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            FileHandle.standardError.write(text.data(using: .utf8)!)
            FileHandle.standardError.write("\n".data(using: .utf8)!)
        } else {
            FileHandle.standardError.write("Error: \(message)\n".data(using: .utf8)!)
        }
        exit(1)
    }
}

func normalizeName(_ value: String) -> String {
    return value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
}

func preferredVideoDeviceTypes() -> [AVCaptureDevice.DeviceType] {
    if #available(macOS 14.0, *) {
        return [.external, .builtInWideAngleCamera]
    }
    return [.externalUnknown, .builtInWideAngleCamera]
}

func videoDevices() -> [AVCaptureDevice] {
    return AVCaptureDevice.DiscoverySession(
        deviceTypes: preferredVideoDeviceTypes(),
        mediaType: .video,
        position: .unspecified
    ).devices
}

func audioDevices() -> [AVCaptureDevice] {
    return AVCaptureDevice.DiscoverySession(
        deviceTypes: [.microphone],
        mediaType: .audio,
        position: .unspecified
    ).devices
}

func pickDevice(devices: [AVCaptureDevice], name: String?, index: Int?, uniqueId: String?) throws -> AVCaptureDevice {
    if let uniqueId, !uniqueId.isEmpty {
        if let exact = devices.first(where: { $0.uniqueID == uniqueId }) {
            return exact
        }
        throw CLIError(message: "Device unique ID '\(uniqueId)' was not found.")
    }
    if let index {
        guard devices.indices.contains(index) else {
            throw CLIError(message: "Device index \(index) is out of range.")
        }
        return devices[index]
    }
    if let name, !name.isEmpty {
        let normalized = normalizeName(name)
        if let exact = devices.first(where: { normalizeName($0.localizedName) == normalized }) {
            return exact
        }
        if let partial = devices.first(where: { normalizeName($0.localizedName).contains(normalized) }) {
            return partial
        }
        let available = devices.map(\.localizedName).joined(separator: ", ")
        throw CLIError(message: "Device '\(name)' was not found. Available: \(available)")
    }
    guard let first = devices.first else {
        throw CLIError(message: "No device is available.")
    }
    return first
}

func sessionPreset(width: Int, height: Int) -> AVCaptureSession.Preset {
    if width >= 1920 || height >= 1080 {
        return .hd1920x1080
    }
    if width >= 1280 || height >= 720 {
        return .hd1280x720
    }
    if width >= 640 || height >= 480 {
        return .vga640x480
    }
    return .high
}

final class FrameCaptureHandler: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    private let outputURL: URL
    private let semaphore = DispatchSemaphore(value: 0)
    private var captureError: Error?
    private let context = CIContext()
    private var frameCount = 0
    private var completed = false
    private let targetFrame: Int

    init(outputURL: URL, targetFrame: Int = 10) {
        self.outputURL = outputURL
        self.targetFrame = max(1, targetFrame)
        super.init()
    }

    func wait(timeoutSec: TimeInterval) throws {
        let deadline = DispatchTime.now() + timeoutSec
        if semaphore.wait(timeout: deadline) == .timedOut {
            throw CLIError(message: "Timed out while waiting for photo capture.")
        }
        if let captureError {
            throw captureError
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard !completed else {
            return
        }
        frameCount += 1
        if frameCount < targetFrame {
            return
        }
        completed = true
        defer { semaphore.signal() }
        guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            captureError = CLIError(message: "Sample buffer did not contain an image buffer.")
            return
        }
        let ciImage = CIImage(cvPixelBuffer: imageBuffer)
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        do {
            guard let data = context.jpegRepresentation(of: ciImage, colorSpace: colorSpace) else {
                throw CLIError(message: "Failed to encode JPEG data from camera frame.")
            }
            try data.write(to: outputURL, options: .atomic)
        } catch {
            captureError = error
        }
    }
}

func capturePhoto(
    cameraName: String?,
    videoIndex: Int?,
    videoUniqueId: String?,
    outputPath: String,
    width: Int,
    height: Int,
    fps: Int
) throws {
    let session = AVCaptureSession()
    let devices = videoDevices()
    let camera = try pickDevice(devices: devices, name: cameraName, index: videoIndex, uniqueId: videoUniqueId)
    let input = try AVCaptureDeviceInput(device: camera)
    let videoOutput = AVCaptureVideoDataOutput()
    videoOutput.alwaysDiscardsLateVideoFrames = true
    videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA)]

    session.beginConfiguration()
    let preset = sessionPreset(width: width, height: height)
    if session.canSetSessionPreset(preset) {
        session.sessionPreset = preset
    }
    guard session.canAddInput(input) else {
        throw CLIError(message: "Could not add video input for \(camera.localizedName).")
    }
    session.addInput(input)
    guard session.canAddOutput(videoOutput) else {
        throw CLIError(message: "Could not add video frame output.")
    }
    session.addOutput(videoOutput)
    session.commitConfiguration()

    let outputURL = URL(fileURLWithPath: outputPath)
    try FileManager.default.createDirectory(
        at: outputURL.deletingLastPathComponent(),
        withIntermediateDirectories: true,
        attributes: nil
    )
    let handler = FrameCaptureHandler(outputURL: outputURL, targetFrame: max(8, fps / 2))
    let queue = DispatchQueue(label: "dji_camera_native.frame_capture")
    videoOutput.setSampleBufferDelegate(handler, queue: queue)

    session.startRunning()
    try handler.wait(timeoutSec: 12.0)
    session.stopRunning()

    let response = PhotoResponse(ok: true, photo_path: outputURL.path, camera_name: camera.localizedName)
    try JSONPrinter.printObject(response)
}

final class MovieRecorder: NSObject, AVCaptureFileOutputRecordingDelegate {
    private let session = AVCaptureSession()
    private let movieOutput = AVCaptureMovieFileOutput()
    private let readyPath: String?
    private let outputURL: URL
    private var finished = false
    private var finishedError: String?
    private var didStart = false
    private let keepAudio: Bool
    private let selectedCameraName: String

    init(
        cameraName: String?,
        videoIndex: Int?,
        videoUniqueId: String?,
        audioName: String?,
        audioIndex: Int?,
        audioUniqueId: String?,
        outputPath: String,
        readyPath: String?,
        width: Int,
        height: Int,
        fps: Int,
        withAudio: Bool
    ) throws {
        self.readyPath = readyPath
        self.outputURL = URL(fileURLWithPath: outputPath)
        self.keepAudio = withAudio
        let video = try pickDevice(devices: videoDevices(), name: cameraName, index: videoIndex, uniqueId: videoUniqueId)
        self.selectedCameraName = video.localizedName
        super.init()

        let videoInput = try AVCaptureDeviceInput(device: video)

        session.beginConfiguration()
        let preset = sessionPreset(width: width, height: height)
        if session.canSetSessionPreset(preset) {
            session.sessionPreset = preset
        }
        guard session.canAddInput(videoInput) else {
            throw CLIError(message: "Could not add video input for \(video.localizedName).")
        }
        session.addInput(videoInput)
        if withAudio {
            let audioDevice = try pickDevice(devices: audioDevices(), name: audioName, index: audioIndex, uniqueId: audioUniqueId)
            let audioInput = try AVCaptureDeviceInput(device: audioDevice)
            if session.canAddInput(audioInput) {
                session.addInput(audioInput)
            }
        }

        guard session.canAddOutput(movieOutput) else {
            throw CLIError(message: "Could not add movie output.")
        }
        session.addOutput(movieOutput)
        session.commitConfiguration()
    }

    func start() throws {
        try FileManager.default.createDirectory(
            at: outputURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: nil
        )
        session.startRunning()
        Thread.sleep(forTimeInterval: 1.0)
        movieOutput.startRecording(to: outputURL, recordingDelegate: self)
        DispatchQueue.main.asyncAfter(deadline: .now() + 6.0) { [weak self] in
            guard let self else { return }
            if !self.didStart && !self.finished {
                self.finishedError = "Timed out while waiting for movie recording to start."
                self.finish()
            }
        }
    }

    func requestStop() {
        if movieOutput.isRecording {
            movieOutput.stopRecording()
            return
        }
        finish()
    }

    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection]) {
        didStart = true
        let response = RecordStartResponse(ok: true, output_path: fileURL.path, camera_name: selectedCameraName, with_audio: keepAudio)
        if let readyPath {
            if let data = try? JSONEncoder().encode(response) {
                FileManager.default.createFile(atPath: readyPath, contents: data)
            }
        }
    }

    func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL, from connections: [AVCaptureConnection], error: Error?) {
        if let error {
            finishedError = error.localizedDescription
        }
        finish()
    }

    private func finish() {
        guard !finished else { return }
        finished = true
        if session.isRunning {
            session.stopRunning()
        }
        if let finishedError {
            JSONPrinter.printErrorAndExit(finishedError)
        }
        exit(0)
    }
}

func installSignalHandler(_ signalCode: Int32, handler: @escaping () -> Void) {
    signal(signalCode, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: signalCode, queue: .main)
    source.setEventHandler(handler: handler)
    source.resume()
    signalSources.append(source)
}

var signalSources: [DispatchSourceSignal] = []

func listCommand() throws {
    let videos = videoDevices().enumerated().map { DeviceRecord(index: $0.offset, name: $0.element.localizedName, unique_id: $0.element.uniqueID) }
    let audios = audioDevices().enumerated().map { DeviceRecord(index: $0.offset, name: $0.element.localizedName, unique_id: $0.element.uniqueID) }
    try JSONPrinter.printObject(ListResponse(ok: true, video_devices: videos, audio_devices: audios))
}

func parseArgs(_ args: [String]) -> [String: String] {
    var result: [String: String] = [:]
    var index = 0
    while index < args.count {
        let item = args[index]
        if item.hasPrefix("--") {
            let key = String(item.dropFirst(2))
            if index + 1 < args.count, !args[index + 1].hasPrefix("--") {
                result[key] = args[index + 1]
                index += 2
                continue
            }
            result[key] = "true"
        }
        index += 1
    }
    return result
}

do {
    let args = Array(CommandLine.arguments.dropFirst())
    guard let command = args.first else {
        throw CLIError(message: "Expected a command: list, photo, or record.")
    }
    let options = parseArgs(Array(args.dropFirst()))
    switch command {
    case "list":
        try listCommand()
    case "photo":
        guard let outputPath = options["output"] else {
            throw CLIError(message: "--output is required for photo.")
        }
        let cameraName = options["camera-name"]
        let videoIndex = options["video-index"].flatMap(Int.init)
        let videoUniqueId = options["video-unique-id"]
        let width = options["width"].flatMap(Int.init) ?? 1280
        let height = options["height"].flatMap(Int.init) ?? 720
        let fps = options["fps"].flatMap(Int.init) ?? 30
        try capturePhoto(
            cameraName: cameraName,
            videoIndex: videoIndex,
            videoUniqueId: videoUniqueId,
            outputPath: outputPath,
            width: width,
            height: height,
            fps: fps
        )
    case "record":
        guard let outputPath = options["output"] else {
            throw CLIError(message: "--output is required for record.")
        }
        let recorder = try MovieRecorder(
            cameraName: options["camera-name"],
            videoIndex: options["video-index"].flatMap(Int.init),
            videoUniqueId: options["video-unique-id"],
            audioName: options["audio-name"],
            audioIndex: options["audio-index"].flatMap(Int.init),
            audioUniqueId: options["audio-unique-id"],
            outputPath: outputPath,
            readyPath: options["ready-path"],
            width: options["width"].flatMap(Int.init) ?? 1280,
            height: options["height"].flatMap(Int.init) ?? 720,
            fps: options["fps"].flatMap(Int.init) ?? 30,
            withAudio: options["with-audio"] == "true"
        )
        installSignalHandler(SIGINT) {
            recorder.requestStop()
        }
        installSignalHandler(SIGTERM) {
            recorder.requestStop()
        }
        try recorder.start()
        RunLoop.main.run()
    default:
        throw CLIError(message: "Unsupported command: \(command)")
    }
} catch let error as CLIError {
    JSONPrinter.printErrorAndExit(error.message)
} catch {
    JSONPrinter.printErrorAndExit(error.localizedDescription)
}
