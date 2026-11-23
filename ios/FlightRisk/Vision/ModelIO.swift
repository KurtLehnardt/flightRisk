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

    /// The 8-bit RGB pixels of `image`, drawn 1:1 in the image's own RGB colour space so that no
    /// colour conversion changes them (sRGB when the image has no RGB colour space).
    static func rgb(_ image: CGImage) -> RGBImage? {
        let w = image.width, h = image.height
        guard w > 0, h > 0 else { return nil }
        let space: CGColorSpace
        if let own = image.colorSpace, own.model == .rgb {
            space = own
        } else if let srgb = CGColorSpace(name: CGColorSpace.sRGB) {
            space = srgb
        } else {
            return nil
        }
        var rgba = [UInt8](repeating: 0, count: w * h * 4)
        let drawn = rgba.withUnsafeMutableBytes { buf -> Bool in
            guard let ctx = CGContext(
                data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
            ) else { return false }
            ctx.interpolationQuality = .none
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
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

    /// The values of `array` in row-major order of its shape, whatever its strides and its
    /// float type.
    static func floats(_ array: MLMultiArray) -> [Float] {
        let shape = array.shape.map(\.intValue)
        let strides = array.strides.map(\.intValue)
        let count = shape.reduce(1, *)
        guard count > 0 else { return [] }
        var contiguous = true
        var expected = 1
        for i in stride(from: shape.count - 1, through: 0, by: -1) {
            if shape[i] > 1 && strides[i] != expected { contiguous = false }
            expected *= shape[i]
        }
        if contiguous {
            switch array.dataType {
            case .float32:
                return array.withUnsafeBufferPointer(ofType: Float.self) { Array($0.prefix(count)) }
            case .double:
                return array.withUnsafeBufferPointer(ofType: Double.self) { $0.prefix(count).map { Float($0) } }
            #if arch(arm64)
            case .float16:
                return array.withUnsafeBufferPointer(ofType: Float16.self) { $0.prefix(count).map { Float($0) } }
            #endif
            default:
                break
            }
        }
        // Any other layout or type: walk the logical indices.
        var out = [Float](repeating: 0, count: count)
        var index = [Int](repeating: 0, count: shape.count)
        for i in 0..<count {
            out[i] = array[index.map { NSNumber(value: $0) }].floatValue
            var d = shape.count - 1
            while d >= 0 {
                index[d] += 1
                if index[d] < shape[d] { break }
                index[d] = 0
                d -= 1
            }
        }
        return out
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

    /// The compute units `mobile-models.json` asks for, for the installed model built from
    /// `installedName` (`YOLOXs.mlpackage`): `.cpuAndGPU` for the fp16 detector, `.all` otherwise
    /// and when there is no manifest.
    static func computeUnits(for installedName: String, in bundle: Bundle = .main) -> MLComputeUnits {
        guard let url = bundle.url(forResource: manifestName, withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let models = json["models"] as? [[String: Any]],
              let entry = models.first(where: { $0["file"] as? String == installedName }),
              let units = entry["coreml_compute_units"] as? String
        else { return .all }
        return units == "cpuAndGPU" ? .cpuAndGPU : .all
    }

    /// The licence notices for the bundled models (`model-notices.md`), or `nil` when this build
    /// has no models.
    static func notices(in bundle: Bundle = .main) -> String? {
        guard let url = bundle.url(forResource: noticesName, withExtension: "md") else { return nil }
        return try? String(contentsOf: url, encoding: .utf8)
    }
}
