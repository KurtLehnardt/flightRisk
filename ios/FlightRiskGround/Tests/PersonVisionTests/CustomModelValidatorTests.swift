import XCTest
@testable import PersonVision

/// The checks for a detector the user imports, on synthetic interfaces and tensors.
final class CustomModelValidatorTests: XCTestCase {
    let image640 = CustomModelInterface.Input(name: "image", kind: .image(width: 640, height: 640))

    // MARK: - Output layouts

    func testHeadLayoutsAreRecognisedByShape() {
        XCTAssertEqual(CustomHeadLayout.classify(shape: [1, 84, 8400]), .channelsFirst(classCount: 80))
        XCTAssertEqual(CustomHeadLayout.classify(shape: [84, 8400]), .channelsFirst(classCount: 80))
        XCTAssertEqual(CustomHeadLayout.classify(shape: [1, 8400, 84]), .anchorsFirst(classCount: 80))
        XCTAssertEqual(CustomHeadLayout.classify(shape: [1, 5, 8400]), .channelsFirst(classCount: 1))
        XCTAssertEqual(CustomHeadLayout.classify(shape: [1, 300, 6]), .nmsFree(detectionCount: 300))
        XCTAssertNil(CustomHeadLayout.classify(shape: [2, 84, 8400]), "a batch of 2")
        XCTAssertNil(CustomHeadLayout.classify(shape: [1, 84, 2100]), "anchors of a 320 input")
        XCTAssertNil(CustomHeadLayout.classify(shape: [1, 4, 8400]), "no class")
        XCTAssertNil(CustomHeadLayout.classify(shape: [1, 2000, 6]))
        XCTAssertNil(CustomHeadLayout.classify(shape: [1, 3, 640, 640]))
        XCTAssertNil(CustomHeadLayout.classify(shape: []))
    }

    func testDecodingAChannelsFirstHead() {
        // Anchor 7: a person at (320, 240), 40 x 120 in a 640 x 640 frame; anchor 9: a car.
        var out = [Float](repeating: 0, count: 84 * 8400)
        func set(_ anchor: Int, _ channel: Int, _ v: Float) { out[channel * 8400 + anchor] = v }
        set(7, 0, 320); set(7, 1, 240); set(7, 2, 40); set(7, 3, 120); set(7, 4, 0.83)
        set(8, 0, 322); set(8, 1, 241); set(8, 2, 40); set(8, 3, 120); set(8, 4, 0.6)  // duplicate, suppressed
        set(9, 0, 100); set(9, 1, 100); set(9, 2, 60); set(9, 3, 30); set(9, 4 + 2, 0.9)
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640, placement: .centred)
        let d = CustomHeadDecoder.decodePersons(out, layout: .channelsFirst(classCount: 80), geometry: g, scoreThreshold: 0.25)
        XCTAssertEqual(d.count, 1)
        XCTAssertEqual(d[0].score, 0.83)
        XCTAssertEqual([d[0].x1, d[0].y1, d[0].x2, d[0].y2], [300, 180, 340, 300])
    }

    func testDecodingAnAnchorsFirstHeadMapsBackThroughTheCentredPadding() {
        var out = [Float](repeating: 0, count: 8400 * 6)
        // One class; anchor 3 at canvas (320, 320), 20 x 60. The frame is 1280 x 720: r = 0.5, pad top 140.
        out[3 * 5 + 0] = 320; out[3 * 5 + 1] = 320; out[3 * 5 + 2] = 20; out[3 * 5 + 3] = 60; out[3 * 5 + 4] = 0.5
        let g = LetterboxGeometry(frameWidth: 1280, frameHeight: 720, placement: .centred)
        XCTAssertEqual(g.padTop, 140)
        let d = CustomHeadDecoder.decodePersons(out, layout: .anchorsFirst(classCount: 1), geometry: g, scoreThreshold: 0.25)
        XCTAssertEqual(d.count, 1)
        XCTAssertEqual([d[0].x1, d[0].y1, d[0].x2, d[0].y2], [620, 300, 660, 420])
    }

    func testDecodingAnNMSFreeHeadKeepsPersonRows() {
        var out = [Float](repeating: 0, count: 300 * 6)
        out.replaceSubrange(0..<6, with: [10, 20, 30, 80, 0.4, 0])
        out.replaceSubrange(6..<12, with: [100, 20, 130, 80, 0.9, 1])
        out.replaceSubrange(12..<18, with: [200, 20, 230, 80, 0.7, 0])
        out.replaceSubrange(18..<24, with: [300, 20, 330, 80, 0.1, 0])
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640, placement: .centred)
        let d = CustomHeadDecoder.decodePersons(out, layout: .nmsFree(detectionCount: 300), geometry: g, scoreThreshold: 0.25)
        XCTAssertEqual(d.map(\.score), [0.7, 0.4])
        XCTAssertEqual(d[0].x1, 200)
    }

    func testAShortOutputDecodesToNothing() {
        let g = LetterboxGeometry(frameWidth: 640, frameHeight: 640)
        XCTAssertTrue(CustomHeadDecoder.decodePersons([1, 2, 3], layout: .nmsFree(detectionCount: 300), geometry: g, scoreThreshold: 0).isEmpty)
    }

    // MARK: - Interface

    func testAnUltralyticsStyleInterfacePasses() throws {
        let i = CustomModelInterface(
            inputs: [image640],
            outputs: [.init(name: "var_1224", shape: [1, 84, 8400])],
            metadata: ["names": "{0: 'person', 1: 'bicycle', 2: 'car'}"]
        )
        let checked = try CustomModelValidator.check(i)
        XCTAssertEqual(checked.input, "image")
        XCTAssertEqual(checked.output, "var_1224")
        XCTAssertEqual(checked.layout, .channelsFirst(classCount: 80))
    }

    func testTheInputMustBeOne640Image() {
        XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [], outputs: []))) {
            XCTAssertEqual($0 as? CustomModelError, .inputCount(0))
        }
        XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [image640, image640], outputs: []))) {
            XCTAssertEqual($0 as? CustomModelError, .inputCount(2))
        }
        let array = CustomModelInterface.Input(name: "x", kind: .multiArray(shape: [1, 3, 640, 640]))
        XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [array], outputs: []))) {
            XCTAssertEqual($0 as? CustomModelError, .inputNotImage)
        }
        let small = CustomModelInterface.Input(name: "x", kind: .image(width: 320, height: 320))
        XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [small], outputs: [.init(name: "o", shape: [1, 84, 8400])]))) {
            XCTAssertEqual($0 as? CustomModelError, .inputSize(width: 320, height: 320))
        }
    }

    func testExactlyOneOutputMustBeASupportedHead() {
        for outputs: [CustomModelInterface.Output] in [
            [],
            [.init(name: "a", shape: [1, 85, 8400 * 2])],
            [.init(name: "a", shape: nil)],
            [.init(name: "a", shape: [1, 84, 8400]), .init(name: "b", shape: [1, 300, 6])],
        ] {
            XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [image640], outputs: outputs))) {
                XCTAssertEqual($0 as? CustomModelError, .outputNotRecognised)
            }
        }
        // An extra output that is not a head (a pipeline's confidence array, say) is ignored.
        XCTAssertNoThrow(try CustomModelValidator.check(.init(
            inputs: [image640], outputs: [.init(name: "a", shape: [1, 300, 6]), .init(name: "b", shape: [7])]
        )))
    }

    func testAYOLOXShapedOutputIsRefusedUnlessTheModelNames81Classes() throws {
        for shape in [[1, 8400, 85], [1, 85, 8400]] {
            XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [image640], outputs: [.init(name: "o", shape: shape)]))) {
                XCTAssertEqual($0 as? CustomModelError, .yoloxShapedOutput)
            }
        }
        let eighty = (0..<80).map { "\($0): 'c\($0)'" }.joined(separator: ", ")
        XCTAssertThrowsError(try CustomModelValidator.check(.init(
            inputs: [image640], outputs: [.init(name: "o", shape: [1, 8400, 85])], metadata: ["names": "{0: 'person', \(eighty)}"]
        ))) { XCTAssertEqual($0 as? CustomModelError, .yoloxShapedOutput) }
        let eightyOne = (1..<81).map { "\($0): 'c\($0)'" }.joined(separator: ", ")
        let ok = try CustomModelValidator.check(.init(
            inputs: [image640], outputs: [.init(name: "o", shape: [1, 8400, 85])], metadata: ["names": "{0: 'person', \(eightyOne)}"]
        ))
        XCTAssertEqual(ok.layout, .anchorsFirst(classCount: 81))
    }

    func testClassZeroMustBePersonWhenTheModelNamesItsClasses() {
        let out = [CustomModelInterface.Output(name: "o", shape: [1, 6, 8400])]
        XCTAssertThrowsError(try CustomModelValidator.check(.init(inputs: [image640], outputs: out, metadata: ["names": "{0: 'helmet', 1: 'person'}"]))) {
            XCTAssertEqual($0 as? CustomModelError, .personNotClassZero("helmet"))
        }
        XCTAssertNoThrow(try CustomModelValidator.check(.init(inputs: [image640], outputs: out, metadata: ["names": #"{"0": "Person", "1": "dog"}"#])))
        XCTAssertNoThrow(try CustomModelValidator.check(.init(inputs: [image640], outputs: out)))
    }

    func testClassNamesAreParsedFromBothFormats() {
        XCTAssertEqual(CustomModelValidator.classNames(["names": "{0: 'person', 1: 'traffic light', 12: 'stop sign'}"]),
                       [0: "person", 1: "traffic light", 12: "stop sign"])
        XCTAssertEqual(CustomModelValidator.classNames(["names": #"{"0": "person"}"#]), [0: "person"])
        XCTAssertNil(CustomModelValidator.classNames(["names": "nothing here"]))
        XCTAssertNil(CustomModelValidator.classNames([:]))
    }

    // MARK: - Test picture, size and file kind

    func testAPersonMustBeFoundOnTheTestPicture() {
        XCTAssertThrowsError(try CustomModelValidator.checkPersonFound([])) {
            XCTAssertEqual($0 as? CustomModelError, .noPersonFound)
        }
        let weak = DetectedBox(x1: 0, y1: 0, x2: 1, y2: 2, score: 0.2, classIndex: 0)
        XCTAssertThrowsError(try CustomModelValidator.checkPersonFound([weak]))
        XCTAssertNoThrow(try CustomModelValidator.checkPersonFound([DetectedBox(x1: 0, y1: 0, x2: 1, y2: 2, score: 0.25, classIndex: 0)]))
    }

    func testTheSizeCapIs100MB() {
        XCTAssertNoThrow(try CustomModelValidator.checkSize(100_000_000))
        XCTAssertThrowsError(try CustomModelValidator.checkSize(100_000_001)) {
            XCTAssertEqual($0 as? CustomModelError, .tooLarge(bytes: 100_000_001))
        }
        XCTAssertThrowsError(try CustomModelValidator.checkSize(0)) {
            XCTAssertEqual($0 as? CustomModelError, .empty)
        }
        XCTAssertEqual(CustomModelError.tooLarge(bytes: 123_400_000).message,
                       "The model is 123.4 MB; the limit is 100.0 MB.")
    }

    func testOnlyZipAndMLModelFilesAreAccepted() throws {
        XCTAssertEqual(try CustomModelValidator.fileKind(forExtension: "ZIP"), .zip)
        XCTAssertEqual(try CustomModelValidator.fileKind(forExtension: "mlmodel"), .mlmodel)
        for ext in ["pt", "onnx", "mlpackage", "mlmodelc", ""] {
            XCTAssertThrowsError(try CustomModelValidator.fileKind(forExtension: ext))
        }
    }

    func testMessagesNeverNameTheFile() {
        let all: [CustomModelError] = [
            .notAttested, .unsupportedFile, .empty, .tooLarge(bytes: 1), .archive("x"), .noPackageInArchive, .severalPackagesInArchive,
            .compileFailed, .loadFailed, .inputCount(2), .inputNotImage, .inputSize(width: 1, height: 1),
            .outputNotRecognised, .yoloxShapedOutput, .personNotClassZero("cat"), .noPersonFound, .storageFailed,
        ]
        for e in all {
            XCTAssertFalse(e.message.isEmpty)
            XCTAssertFalse(e.message.contains("/"), "no path in \(e)")
        }
    }
}
