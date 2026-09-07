import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import os

/// Stores target embeddings and photo for session persistence.
///
/// Keeps the ReID and face embeddings in memory for fast access,
/// and serialises them to files in the app's documents directory
/// for crash recovery. Thread-safe via Swift actor isolation.
actor TargetStore {

    private let logger = Logger(subsystem: "com.flightrisk", category: "targetStore")

    private let documentsDir: URL

    /// ReID (CLIP) embedding for the target person.
    private(set) var reidEmbedding: [Float]?
    /// Face (ArcFace) embedding for the target person.
    private(set) var faceEmbedding: [Float]?
    /// Reference photo of the target person.
    private(set) var targetImage: CGImage?

    private static let reidFile = "target_reid_embedding.bin"
    private static let faceFile = "target_face_embedding.bin"
    private static let imageFile = "target_photo.png"

    init() {
        // swiftlint:disable:next force_unwrapping
        documentsDir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
    }

    /// Whether any target data is currently stored.
    var hasTarget: Bool {
        reidEmbedding != nil || faceEmbedding != nil
    }

    // MARK: - Setters

    /// Store a ReID embedding and optionally persist to disk.
    func setReidEmbedding(_ embedding: [Float], persist: Bool = true) {
        reidEmbedding = embedding
        if persist {
            saveEmbedding(embedding, filename: Self.reidFile)
        }
    }

    /// Store a face embedding and optionally persist to disk.
    func setFaceEmbedding(_ embedding: [Float], persist: Bool = true) {
        faceEmbedding = embedding
        if persist {
            saveEmbedding(embedding, filename: Self.faceFile)
        }
    }

    /// Store the target reference photo and optionally persist to disk.
    func setTargetImage(_ image: CGImage, persist: Bool = true) {
        targetImage = image
        if persist {
            saveImage(image, filename: Self.imageFile)
        }
    }

    // MARK: - Restore

    /// Restore persisted target data from disk.
    ///
    /// Call on app startup to recover the target after a crash or
    /// process termination.
    ///
    /// - Returns: `true` if any data was restored.
    @discardableResult
    func restore() -> Bool {
        var restored = false

        if let reid = loadEmbedding(filename: Self.reidFile) {
            reidEmbedding = reid
            restored = true
        }
        if let face = loadEmbedding(filename: Self.faceFile) {
            faceEmbedding = face
            restored = true
        }
        if let image = loadImage(filename: Self.imageFile) {
            targetImage = image
            restored = true
        }

        if restored {
            logger.debug("Restored target data from disk")
        }
        return restored
    }

    // MARK: - Clear

    /// Clear all stored data (both in memory and on disk).
    func clear() {
        reidEmbedding = nil
        faceEmbedding = nil
        targetImage = nil

        deleteFile(Self.reidFile)
        deleteFile(Self.faceFile)
        deleteFile(Self.imageFile)
    }

    // MARK: - Private Helpers

    /// Save an embedding as binary: 4-byte Int32 count prefix, then N Float32 values.
    private func saveEmbedding(_ embedding: [Float], filename: String) {
        let url = documentsDir.appendingPathComponent(filename)
        do {
            var data = Data()
            var count = Int32(embedding.count)
            data.append(Data(bytes: &count, count: MemoryLayout<Int32>.size))
            for var value in embedding {
                data.append(Data(bytes: &value, count: MemoryLayout<Float>.size))
            }
            try data.write(to: url)
        } catch {
            logger.warning("Failed to persist embedding to \(filename): \(error.localizedDescription)")
        }
    }

    /// Load an embedding from binary file.
    private func loadEmbedding(filename: String) -> [Float]? {
        let url = documentsDir.appendingPathComponent(filename)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do {
            let data = try Data(contentsOf: url)
            let countSize = MemoryLayout<Int32>.size
            guard data.count >= countSize else { return nil }

            let count = data.withUnsafeBytes { $0.load(as: Int32.self) }
            let floatSize = MemoryLayout<Float>.size
            let expectedSize = countSize + Int(count) * floatSize
            guard data.count >= expectedSize else { return nil }

            var result = [Float](repeating: 0, count: Int(count))
            for i in 0..<Int(count) {
                let offset = countSize + i * floatSize
                result[i] = data.withUnsafeBytes {
                    $0.load(fromByteOffset: offset, as: Float.self)
                }
            }
            return result
        } catch {
            logger.warning("Failed to load embedding from \(filename): \(error.localizedDescription)")
            return nil
        }
    }

    /// Save a CGImage as PNG.
    private func saveImage(_ image: CGImage, filename: String) {
        let url = documentsDir.appendingPathComponent(filename)
        guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            logger.warning("Failed to create image destination for \(filename)")
            return
        }
        CGImageDestinationAddImage(dest, image, nil)
        if !CGImageDestinationFinalize(dest) {
            logger.warning("Failed to write target image to \(filename)")
        }
    }

    /// Load a CGImage from a PNG file.
    private func loadImage(filename: String) -> CGImage? {
        let url = documentsDir.appendingPathComponent(filename)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
            logger.warning("Failed to create image source from \(filename)")
            return nil
        }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }

    /// Delete a file from the documents directory.
    private func deleteFile(_ filename: String) {
        let url = documentsDir.appendingPathComponent(filename)
        do {
            try FileManager.default.removeItem(at: url)
        } catch {
            logger.warning("Failed to delete \(filename): \(error.localizedDescription)")
        }
    }
}
