import CoreGraphics
import CoreML
import Foundation
import ImageIO
import PersonVision

/// What FlightRisk records about a person detector the user imported. The model itself is kept
/// only in the app's own storage; it is never bundled, uploaded, shared or logged.
struct CustomDetectorInfo: Codable, Equatable, Sendable {
    /// SHA-256 of the file the user picked (the `.zip` or the `.mlmodel`), shown in Settings.
    let sha256: String
    /// Size of that file in bytes.
    let bytes: Int64
    let inputName: String
    let outputName: String
    let layout: CustomHeadLayout
    /// The best person score the model gave on FlightRisk's test picture.
    let testPictureScore: Float
    /// When the user confirmed the licence statement and imported the model.
    let importedAt: Date
}

/// The optional person detector the user brings (Settings > Your own person detector).
///
/// Off by default. Importing a model takes a gate screen and the user's statement that they
/// may use it (YOLO models from Ultralytics are AGPL-3.0 or need an Ultralytics Enterprise
/// licence). The import checks the file (100 MB at most), unpacks a zipped `.mlpackage` with
/// `ZipExtractor`, compiles it with Core ML on the phone, checks its interface
/// (`CustomModelValidator`) and that it finds a person on the bundled synthetic test picture, and
/// only then stores the compiled model in Application Support, excluded from backups. Switching it
/// off, or removing it, goes back to the bundled YOLOX-S.
final class CustomDetectorStore: @unchecked Sendable {
    static let shared = CustomDetectorStore()

    /// Posted on the main queue after the model or the switch changed.
    static let didChange = Notification.Name("FlightRiskCustomDetectorDidChange")

    static let enabledKey = "flightrisk_custom_detector_enabled"
    static let checkPictureName = "person-check-scene"

    let directory: URL
    private let defaults: UserDefaults
    private let checkPicture: () -> RGBImage?
    private let lock = NSLock()
    private var _generation = 0

    /// - Parameters:
    ///   - directory: Where the compiled model and its record are kept (default: Application
    ///     Support/CustomDetector).
    ///   - defaults: Where the on/off switch is kept.
    ///   - checkPicture: The synthetic picture a model must find a person in.
    init(
        directory: URL? = nil,
        defaults: UserDefaults = .standard,
        checkPicture: @escaping () -> RGBImage? = { CustomDetectorStore.bundledCheckPicture() }
    ) {
        let base = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("CustomDetector", isDirectory: true)
        self.directory = base
        self.defaults = defaults
        self.checkPicture = checkPicture
    }

    var modelURL: URL { directory.appendingPathComponent("model.mlmodelc", isDirectory: true) }
    var infoURL: URL { directory.appendingPathComponent("info.json") }

    /// Changes whenever the model or the switch changes; the detector reloads when it does.
    var generation: Int {
        lock.lock()
        defer { lock.unlock() }
        return _generation
    }

    private func changed() {
        lock.lock()
        _generation += 1
        lock.unlock()
        DispatchQueue.main.async { NotificationCenter.default.post(name: Self.didChange, object: self) }
    }

    /// The imported model's record, or `nil` when there is none.
    var info: CustomDetectorInfo? {
        guard FileManager.default.fileExists(atPath: modelURL.path),
              let data = try? Data(contentsOf: infoURL) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(CustomDetectorInfo.self, from: data)
    }

    /// Whether the user's model is switched on. Off by default; never on without a model.
    var isEnabled: Bool {
        get { defaults.bool(forKey: Self.enabledKey) && info != nil }
        set {
            defaults.set(newValue && info != nil, forKey: Self.enabledKey)
            changed()
        }
    }

    /// The backend for the user's model while it is switched on, or `nil` (the bundled detector is
    /// used then, including when the stored model no longer loads).
    func makeBackend() -> CustomYOLOBackend? {
        guard isEnabled, let info else { return nil }
        let config = MLModelConfiguration()
        config.computeUnits = .all
        guard let model = try? MLModel(contentsOf: modelURL, configuration: config) else { return nil }
        return CustomYOLOBackend(model: model, inputName: info.inputName, outputName: info.outputName, layout: info.layout)
    }

    /// Deletes the imported model and switches it off.
    func remove() {
        try? FileManager.default.removeItem(at: directory)
        defaults.set(false, forKey: Self.enabledKey)
        changed()
    }

    // MARK: - Import

    /// Imports the model at `source` (a `.zip` holding one `.mlpackage`, or a `.mlmodel`), checks
    /// it, stores it and switches it on. Nothing is kept when any check fails.
    ///
    /// - Parameter attested: The user confirmed they may use the model; the import refuses otherwise.
    @discardableResult
    func importModel(from source: URL, attested: Bool) async throws -> CustomDetectorInfo {
        guard attested else { throw CustomModelError.notAttested }
        let kind = try CustomModelValidator.fileKind(forExtension: source.pathExtension)
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }

        let size = Int64((try? source.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        try CustomModelValidator.checkSize(size)
        let digest: String
        do {
            digest = try FileDigest.sha256(of: source)
        } catch {
            throw CustomModelError.unsupportedFile
        }

        let fm = FileManager.default
        let work = fm.temporaryDirectory.appendingPathComponent("custom-detector-\(UUID().uuidString)", isDirectory: true)
        try? fm.createDirectory(at: work, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: work) }

        let modelSource: URL
        switch kind {
        case .zip:
            let unpacked = work.appendingPathComponent("unpacked", isDirectory: true)
            try? fm.createDirectory(at: unpacked, withIntermediateDirectories: true)
            do {
                let data = try Data(contentsOf: source, options: .mappedIfSafe)
                try ZipExtractor.extract(data, to: unpacked)
            } catch let e as ZipExtractor.ZipError {
                throw CustomModelError.archive(e.message)
            } catch {
                throw CustomModelError.archive("it cannot be read.")
            }
            modelSource = try Self.singlePackage(in: unpacked)
        case .mlmodel:
            modelSource = work.appendingPathComponent("model.mlmodel")
            do {
                try fm.copyItem(at: source, to: modelSource)
            } catch {
                throw CustomModelError.unsupportedFile
            }
        }

        let compiled: URL
        do {
            compiled = try await MLModel.compileModel(at: modelSource)
        } catch {
            throw CustomModelError.compileFailed
        }
        defer { try? fm.removeItem(at: compiled) }
        try CustomModelValidator.checkSize(Self.treeSize(compiled))

        let model: MLModel
        do {
            let config = MLModelConfiguration()
            config.computeUnits = .all
            model = try MLModel(contentsOf: compiled, configuration: config)
        } catch {
            throw CustomModelError.loadFailed
        }
        let checked = try CustomModelValidator.check(Self.interface(of: model))
        guard let picture = checkPicture() else { throw CustomModelError.noPersonFound }
        let backend = CustomYOLOBackend(model: model, inputName: checked.input, outputName: checked.output, layout: checked.layout)
        let persons: [DetectedBox]
        do {
            persons = try backend.persons(in: picture, scoreThreshold: CustomModelValidator.personCheckThreshold)
        } catch {
            throw CustomModelError.loadFailed
        }
        try CustomModelValidator.checkPersonFound(persons)

        let info = CustomDetectorInfo(
            sha256: digest, bytes: size, inputName: checked.input, outputName: checked.output, layout: checked.layout,
            testPictureScore: persons.map(\.score).max() ?? 0,
            // Whole seconds, as the record stores it (ISO 8601).
            importedAt: Date(timeIntervalSince1970: Date().timeIntervalSince1970.rounded(.down))
        )
        try store(compiled: compiled, info: info)
        defaults.set(true, forKey: Self.enabledKey)
        changed()
        return info
    }

    /// Moves the compiled model into place, replacing any earlier one, and writes its record.
    private func store(compiled: URL, info: CustomDetectorInfo) throws {
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            var dir = directory
            try dir.setResourceValues(values)
            let staged = directory.appendingPathComponent("staging-\(UUID().uuidString).mlmodelc", isDirectory: true)
            try fm.copyItem(at: compiled, to: staged)
            if fm.fileExists(atPath: modelURL.path) {
                _ = try fm.replaceItemAt(modelURL, withItemAt: staged)
            } else {
                try fm.moveItem(at: staged, to: modelURL)
            }
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            try encoder.encode(info).write(to: infoURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            throw CustomModelError.storageFailed
        }
    }

    /// The one `.mlpackage` folder at the top of an unpacked archive.
    static func singlePackage(in folder: URL) throws -> URL {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        let packages = names.filter { $0.lowercased().hasSuffix(".mlpackage") }
        guard !packages.isEmpty else { throw CustomModelError.noPackageInArchive }
        guard packages.count == 1 else { throw CustomModelError.severalPackagesInArchive }
        let url = folder.appendingPathComponent(packages[0], isDirectory: true)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue,
              FileManager.default.fileExists(atPath: url.appendingPathComponent("Manifest.json").path)
        else { throw CustomModelError.noPackageInArchive }
        return url
    }

    /// The model's inputs, outputs and creator metadata, for `CustomModelValidator`.
    static func interface(of model: MLModel) -> CustomModelInterface {
        let d = model.modelDescription
        let inputs = d.inputDescriptionsByName.keys.sorted().map { name -> CustomModelInterface.Input in
            let f = d.inputDescriptionsByName[name]!
            switch f.type {
            case .image:
                let c = f.imageConstraint
                return .init(name: name, kind: .image(width: c?.pixelsWide ?? 0, height: c?.pixelsHigh ?? 0))
            case .multiArray:
                return .init(name: name, kind: .multiArray(shape: f.multiArrayConstraint?.shape.map(\.intValue) ?? []))
            default:
                return .init(name: name, kind: .other)
            }
        }
        let outputs = d.outputDescriptionsByName.keys.sorted().map { name -> CustomModelInterface.Output in
            let f = d.outputDescriptionsByName[name]!
            return .init(name: name, shape: f.type == .multiArray ? f.multiArrayConstraint?.shape.map(\.intValue) : nil)
        }
        let metadata = d.metadata[MLModelMetadataKey.creatorDefinedKey] as? [String: String] ?? [:]
        return CustomModelInterface(inputs: inputs, outputs: outputs, metadata: metadata)
    }

    static func treeSize(_ url: URL) -> Int64 {
        guard let e = FileManager.default.enumerator(at: url, includingPropertiesForKeys: [.fileSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let file as URL in e {
            total += Int64((try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }
        return total
    }

    /// The synthetic test picture bundled with the app: drawn figures on drawn grass, no
    /// photograph (made by `ios/scripts/make_person_check_scene.py`).
    static func bundledCheckPicture(in bundle: Bundle = .main) -> RGBImage? {
        guard let url = bundle.url(forResource: checkPictureName, withExtension: "png"),
              let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        else { return nil }
        return ModelIO.rgb(image)
    }
}
