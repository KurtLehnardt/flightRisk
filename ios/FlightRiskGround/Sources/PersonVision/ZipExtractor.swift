import Compression
import CryptoKit
import Foundation

/// Unpacks a zip file the user picked (a `.mlpackage` folder compressed by the Finder or a script).
///
/// iOS has no public unzip API, so this reads the format itself, and only what a zipped model
/// needs: stored or deflated entries, no encryption, no ZIP64, no symbolic links, no absolute
/// paths and no `..`. Sizes are taken from the central directory and capped before anything is
/// unpacked, each entry is unpacked into a buffer of its stated size only, and its CRC-32 is
/// checked. The Finder's `__MACOSX` folder and `._` files are skipped.
public enum ZipExtractor {
    public struct Limits: Sendable {
        public var maxTotalBytes: Int64
        public var maxEntries: Int

        public init(maxTotalBytes: Int64 = CustomModelValidator.maxBytes, maxEntries: Int = 10_000) {
            self.maxTotalBytes = maxTotalBytes
            self.maxEntries = maxEntries
        }
    }

    public enum ZipError: Error, Equatable, Sendable {
        case notAZip
        case unsupported(String)
        case unsafePath
        case tooLarge(Int64)
        case tooManyEntries
        case corrupt(String)

        public var message: String {
            switch self {
            case .notAZip: return "it is not a zip archive."
            case .unsupported(let what): return "it uses \(what), which FlightRisk does not unpack."
            case .unsafePath: return "it holds a path outside the archive or a link."
            case .tooLarge(let bytes): return "unpacked it would be \(CustomModelValidator.megabytes(bytes)); the limit is \(CustomModelValidator.megabytes(CustomModelValidator.maxBytes))."
            case .tooManyEntries: return "it holds too many files."
            case .corrupt(let why): return "it is damaged (\(why))."
            }
        }
    }

    public struct Entry: Equatable, Sendable {
        public let path: String
        public let isDirectory: Bool
        public let method: UInt16
        public let crc32: UInt32
        public let compressedSize: Int
        public let uncompressedSize: Int
        public let localHeaderOffset: Int
    }

    /// The entries of `data`, checked against `limits`, without unpacking anything.
    public static func entries(of data: Data, limits: Limits = Limits()) throws -> [Entry] {
        let bytes = [UInt8](data)
        let n = bytes.count
        func u16(_ o: Int) throws -> UInt16 {
            guard o >= 0, o + 2 <= n else { throw ZipError.corrupt("truncated") }
            return UInt16(bytes[o]) | UInt16(bytes[o + 1]) << 8
        }
        func u32(_ o: Int) throws -> UInt32 {
            guard o >= 0, o + 4 <= n else { throw ZipError.corrupt("truncated") }
            return UInt32(bytes[o]) | UInt32(bytes[o + 1]) << 8 | UInt32(bytes[o + 2]) << 16 | UInt32(bytes[o + 3]) << 24
        }
        guard n >= 22 else { throw ZipError.notAZip }
        // End of central directory: the last signature within the final 22 + 65535 bytes.
        var eocd = -1
        var p = n - 22
        let lowest = max(0, n - 22 - 65_535)
        while p >= lowest {
            if bytes[p] == 0x50, bytes[p + 1] == 0x4B, bytes[p + 2] == 0x05, bytes[p + 3] == 0x06 {
                eocd = p
                break
            }
            p -= 1
        }
        guard eocd >= 0 else { throw ZipError.notAZip }
        let disk = try u16(eocd + 4), cdDisk = try u16(eocd + 6)
        let count = Int(try u16(eocd + 10))
        let cdSize = Int(try u32(eocd + 12)), cdOffset = Int(try u32(eocd + 16))
        if disk != 0 || cdDisk != 0 { throw ZipError.unsupported("several disks") }
        if count == 0xFFFF || cdSize == 0xFFFF_FFFF || cdOffset == 0xFFFF_FFFF { throw ZipError.unsupported("ZIP64") }
        guard count <= limits.maxEntries else { throw ZipError.tooManyEntries }
        guard cdOffset + cdSize <= eocd else { throw ZipError.corrupt("central directory") }

        var result: [Entry] = []
        var total: Int64 = 0
        var o = cdOffset
        for _ in 0..<count {
            guard try u32(o) == 0x0201_4B50 else { throw ZipError.corrupt("central directory entry") }
            let madeBy = try u16(o + 4)
            let flags = try u16(o + 8)
            let method = try u16(o + 10)
            let crc = try u32(o + 16)
            let csize = try u32(o + 20), usize = try u32(o + 24)
            let nameLen = Int(try u16(o + 28)), extraLen = Int(try u16(o + 30)), commentLen = Int(try u16(o + 32))
            let external = try u32(o + 38)
            let local = try u32(o + 42)
            guard o + 46 + nameLen <= n else { throw ZipError.corrupt("name") }
            let nameBytes = Array(bytes[(o + 46)..<(o + 46 + nameLen)])
            o += 46 + nameLen + extraLen + commentLen
            if flags & 0x1 != 0 { throw ZipError.unsupported("encryption") }
            if csize == 0xFFFF_FFFF || usize == 0xFFFF_FFFF || local == 0xFFFF_FFFF { throw ZipError.unsupported("ZIP64") }
            guard let name = String(bytes: nameBytes, encoding: .utf8) else { throw ZipError.unsafePath }
            let isDirectory = name.hasSuffix("/")
            // A symbolic link made on a Unix system: file type 0o120000 in the high bits.
            if madeBy >> 8 == 3, (external >> 16) & 0o170000 == 0o120000 { throw ZipError.unsafePath }
            guard let clean = safePath(name) else { throw ZipError.unsafePath }
            if ignored(clean) { continue }
            if !isDirectory {
                guard method == 0 || method == 8 else { throw ZipError.unsupported("compression method \(method)") }
                total += Int64(usize)
                if total > limits.maxTotalBytes { throw ZipError.tooLarge(total) }
            }
            result.append(Entry(path: clean, isDirectory: isDirectory, method: method, crc32: crc,
                                compressedSize: Int(csize), uncompressedSize: Int(usize), localHeaderOffset: Int(local)))
        }
        return result
    }

    /// Unpacks `data` into `directory` (which must exist) and returns the unpacked paths.
    @discardableResult
    public static func extract(_ data: Data, to directory: URL, limits: Limits = Limits()) throws -> [String] {
        let list = try entries(of: data, limits: limits)
        let fm = FileManager.default
        let root = directory.standardizedFileURL
        var written: [String] = []
        for entry in list {
            let url = root.appendingPathComponent(entry.path, isDirectory: entry.isDirectory)
            guard url.standardizedFileURL.path.hasPrefix(root.path + "/") else { throw ZipError.unsafePath }
            if entry.isDirectory {
                try fm.createDirectory(at: url, withIntermediateDirectories: true)
                continue
            }
            let content = try contents(of: entry, in: data)
            try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            guard fm.createFile(atPath: url.path, contents: content) else { throw ZipError.corrupt("write") }
            written.append(entry.path)
        }
        return written
    }

    static func contents(of entry: Entry, in data: Data) throws -> Data {
        let o = entry.localHeaderOffset
        guard o + 30 <= data.count else { throw ZipError.corrupt("local header") }
        let header = [UInt8](data[data.startIndex + o ..< data.startIndex + o + 30])
        guard header[0] == 0x50, header[1] == 0x4B, header[2] == 0x03, header[3] == 0x04 else {
            throw ZipError.corrupt("local header")
        }
        let nameLen = Int(header[26]) | Int(header[27]) << 8
        let extraLen = Int(header[28]) | Int(header[29]) << 8
        let start = o + 30 + nameLen + extraLen
        guard start + entry.compressedSize <= data.count else { throw ZipError.corrupt("data") }
        let compressed = data[data.startIndex + start ..< data.startIndex + start + entry.compressedSize]
        let out: Data
        if entry.method == 0 {
            guard entry.compressedSize == entry.uncompressedSize else { throw ZipError.corrupt("size") }
            out = Data(compressed)
        } else {
            out = try inflate(Data(compressed), expected: entry.uncompressedSize)
        }
        guard CRC32.checksum(out) == entry.crc32 else { throw ZipError.corrupt("checksum") }
        return out
    }

    /// Raw DEFLATE (RFC 1951) into a buffer of exactly `expected` bytes.
    static func inflate(_ input: Data, expected: Int) throws -> Data {
        if expected == 0 { return Data() }
        var out = Data(count: expected)
        let written = out.withUnsafeMutableBytes { dst -> Int in
            input.withUnsafeBytes { src -> Int in
                guard let d = dst.bindMemory(to: UInt8.self).baseAddress,
                      let s = src.bindMemory(to: UInt8.self).baseAddress else { return 0 }
                return compression_decode_buffer(d, expected, s, input.count, nil, COMPRESSION_ZLIB)
            }
        }
        guard written == expected else { throw ZipError.corrupt("deflate") }
        return out
    }

    /// The entry name without a trailing slash, or `nil` when it is absolute, empty, has `..`, a
    /// backslash or a control character.
    static func safePath(_ name: String) -> String? {
        var trimmed = name
        while trimmed.hasSuffix("/") { trimmed.removeLast() }
        guard !trimmed.isEmpty, !trimmed.hasPrefix("/"), !trimmed.contains("\\") else { return nil }
        guard !trimmed.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7F }) else { return nil }
        let parts = trimmed.split(separator: "/", omittingEmptySubsequences: false)
        guard !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else { return nil }
        return trimmed
    }

    /// The Finder's resource-fork copies, which are not part of the model.
    static func ignored(_ path: String) -> Bool {
        path == "__MACOSX" || path.hasPrefix("__MACOSX/") || path.split(separator: "/").contains { $0.hasPrefix("._") || $0 == ".DS_Store" }
    }
}

/// CRC-32 (IEEE 802.3), as zip uses it.
public enum CRC32 {
    static let table: [UInt32] = (0..<256).map { i -> UInt32 in
        var c = UInt32(i)
        for _ in 0..<8 { c = c & 1 != 0 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
        return c
    }

    public static func checksum(_ data: Data) -> UInt32 {
        var crc: UInt32 = 0xFFFF_FFFF
        data.withUnsafeBytes { raw in
            for b in raw.bindMemory(to: UInt8.self) {
                crc = table[Int((crc ^ UInt32(b)) & 0xFF)] ^ (crc >> 8)
            }
        }
        return crc ^ 0xFFFF_FFFF
    }
}

/// SHA-256 of a file, read in pieces.
public enum FileDigest {
    public static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    public static func sha256(of data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
