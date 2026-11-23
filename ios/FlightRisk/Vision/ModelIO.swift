import CoreGraphics
import CoreML
import CoreVideo
import Foundation
import PersonVision

/// Conversions between Core Graphics, Core Video, Core ML and `PersonVision`'s plain types.
enum ModelIO {
    enum Failure: Error {
        case pixelBuffer
        case context
        case output(String)
    }

    /// The 8-bit RGB pixels of `image`.
    ///
    /// An 8-bit RGB image in a standard-range colour space is drawn 1:1 in its own space, so no
    /// colour conversion changes its values (camera frames, the golden PNGs). Anything else (16-bit
    /// or float pixels, extended-range spaces such as the extended sRGB an iPhone photo of a
    /// Display P3 scene decodes to, grey or CMYK) is converted to 8-bit sRGB: Core Graphics cannot
    /// make an 8-bit context in an extended-range space, so drawing in the image's own space would
    /// fail. `nil` only when no context can be made at all.
    static func rgb(_ image: CGImage) -> RGBImage? {
        let w = image.width, h = image.height
        guard w > 0, h > 0 else { return nil }
        var spaces: [CGColorSpace] = []
        if let own = image.colorSpace, own.model == .rgb, image.bitsPerComponent == 8, !CGColorSpaceUsesExtendedRange(own) {
            spaces.append(own)
        }
        if let srgb = CGColorSpace(name: CGColorSpace.sRGB) {
            spaces.append(srgb)
        }
        var rgba = [UInt8](repeating: 0, count: w * h * 4)
        var drawn = false
        for space in spaces where !drawn {
            drawn = rgba.withUnsafeMutableBytes { buf -> Bool in
                guard let ctx = CGContext(
                    data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                    space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
                ) else { return false }
                ctx.interpolationQuality = .none
                ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
                return true
            }
        }
        guard drawn else { return nil }
        var rgb = [UInt8](repeating: 0, count: w * h * 3)
        rgba.withUnsafeBufferPointer { src in
            rgb.withUnsafeMutableBufferPointer { dst in
                for i in 0..<(w * h) {
                    dst[i * 3] = src[i * 4]
                    dst[i * 3 + 1] = src[i * 4 + 1]
                    dst[i * 3 + 2] = src[i * 4 + 2]
                }
            }
        }
        return RGBImage(width: w, height: h, pixels: rgb)
    }

    /// A 32BGRA pixel buffer holding `image` (reused when `reuse` already has its size). Core ML
    /// reads a BGR or an RGB image input from it in the order the model declares.
    static func pixelBuffer(_ image: RGBImage, reuse: inout CVPixelBuffer?) throws -> CVPixelBuffer {
        if let existing = reuse,
           CVPixelBufferGetWidth(existing) != image.width || CVPixelBufferGetHeight(existing) != image.height {
            reuse = nil
        }
        if reuse == nil {
            var created: CVPixelBuffer?
            let attrs: [CFString: Any] = [kCVPixelBufferIOSurfacePropertiesKey: [String: Any]()]
            let status = CVPixelBufferCreate(kCFAllocatorDefault, image.width, image.height,
                                             kCVPixelFormatType_32BGRA, attrs as CFDictionary, &created)
            guard status == kCVReturnSuccess, let created else { throw Failure.pixelBuffer }
            reuse = created
        }
        guard let buffer = reuse else { throw Failure.pixelBuffer }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { throw Failure.pixelBuffer }
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        let dst = base.assumingMemoryBound(to: UInt8.self)
        image.pixels.withUnsafeBufferPointer { src in
            for y in 0..<image.height {
                let row = dst + y * rowBytes
                let s = y * image.width * 3
                for x in 0..<image.width {
                    row[x * 4] = src[s + x * 3 + 2]
                    row[x * 4 + 1] = src[s + x * 3 + 1]
                    row[x * 4 + 2] = src[s + x * 3]
                    row[x * 4 + 3] = 255
                }
            }
        }
        return buffer
    }

    /// The values of `array` in row-major order of its shape, whatever its strides (Core ML may pad
    /// the last axis) and its float type. Strided arrays are read with a raw-pointer copy, element
    /// by element at `sum(index * stride)`.
    static func floats(_ array: MLMultiArray) -> [Float] {
        let shape = array.shape.map(\.intValue)
        let strides = array.strides.map(\.intValue)
        let count = shape.reduce(1, *)
        guard count > 0, !shape.isEmpty else { return [] }
        var contiguous = true
        var expected = 1
        for i in stride(from: shape.count - 1, through: 0, by: -1) {
            if shape[i] > 1 && strides[i] != expected { contiguous = false }
            expected *= shape[i]
        }
        func read<T>(_ type: T.Type, _ convert: (T) -> Float) -> [Float] {
            array.withUnsafeBytes { raw in
                let base = raw.bindMemory(to: T.self)
                if contiguous {
                    return (0..<count).map { convert(base[$0]) }
                }
                var out = [Float](repeating: 0, count: count)
                var index = [Int](repeating: 0, count: shape.count)
                var offset = 0
                for i in 0..<count {
                    out[i] = convert(base[offset])
                    var d = shape.count - 1
                    while d >= 0 {
                        index[d] += 1
                        offset += strides[d]
                        if index[d] < shape[d] { break }
                        offset -= strides[d] * shape[d]
                        index[d] = 0
                        d -= 1
                    }
                }
                return out
            }
        }
        switch array.dataType {
        case .float32:
            return read(Float.self) { $0 }
        case .double:
            return read(Double.self) { Float($0) }
        case .int32:
            return read(Int32.self) { Float($0) }
        #if arch(arm64)
        case .float16:
            return read(Float16.self) { Float($0) }
        #endif
        default:
            return (0..<count).map { array[$0].floatValue }
        }
    }

    /// A `[1, C, H, W]` float32 array of a channel-major tensor.
    static func multiArray(_ values: [Float], channels: Int, height: Int, width: Int) throws -> MLMultiArray {
        let array = try MLMultiArray(shape: [1, NSNumber(value: channels), NSNumber(value: height), NSNumber(value: width)],
                                     dataType: .float32)
        array.withUnsafeMutableBufferPointer(ofType: Float.self) { dst, strides in
            // A freshly made array is contiguous; check anyway and fall back to strided writes.
            let contiguous = strides == [channels * height * width, height * width, width, 1]
            if contiguous {
                for i in 0..<values.count { dst[i] = values[i] }
            } else {
                for c in 0..<channels {
                    for y in 0..<height {
                        for x in 0..<width {
                            dst[c * strides[1] + y * strides[2] + x * strides[3]] = values[(c * height + y) * width + x]
                        }
                    }
                }
            }
        }
        return array
    }
}

/// The models `make_models.py install` put in `ios/FlightRisk/Models`, which the build phase
/// "Core ML models (optional)" compiles into the app with their manifest and notices.
enum BundledModels {
    static let manifestName = "mobile-models"
    static let noticesName = "model-notices"

    /// The compiled model `name.mlmodelc` in `bundle`, or `nil` when this build has none.
    static func url(_ name: String, in bundle: Bundle = .main) -> URL? {
        bundle.url(forResource: name, withExtension: "mlmodelc")
    }

    /// The compute units a bundled model must be loaded with: its own
    /// `flightrisk.coreml_compute_units` metadata (in the compiled model's `metadata.json`), or else
    /// what `mobile-models.json` says for `installedName` (`YOLOXs.mlpackage`); `.cpuAndGPU` for the
    /// fp16 detector, `.all` otherwise and when neither says.
    static func computeUnits(for installedName: String, in bundle: Bundle = .main) -> MLComputeUnits {
        let name = (installedName as NSString).deletingPathExtension
        if let units = metadataComputeUnits(url(name, in: bundle)) {
            return units == "cpuAndGPU" ? .cpuAndGPU : .all
        }
        guard let url = bundle.url(forResource: manifestName, withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let models = json["models"] as? [[String: Any]],
              let entry = models.first(where: { $0["file"] as? String == installedName }),
              let units = entry["coreml_compute_units"] as? String
        else { return .all }
        return units == "cpuAndGPU" ? .cpuAndGPU : .all
    }

    /// The `flightrisk.coreml_compute_units` value in a compiled model's `metadata.json`.
    static func metadataComputeUnits(_ compiled: URL?) -> String? {
        guard let compiled,
              let data = try? Data(contentsOf: compiled.appendingPathComponent("metadata.json")),
              let json = try? JSONSerialization.jsonObject(with: data)
        else { return nil }
        let entry = (json as? [[String: Any]])?.first ?? (json as? [String: Any])
        let user = entry?["userDefinedMetadata"] as? [String: Any]
        return user?["flightrisk.coreml_compute_units"] as? String
    }

    /// The licence notices for the bundled models (`model-notices.md`), or `nil` when this build
    /// has no models.
    static func notices(in bundle: Bundle = .main) -> String? {
        guard let url = bundle.url(forResource: noticesName, withExtension: "md") else { return nil }
        return try? String(contentsOf: url, encoding: .utf8)
    }
}
