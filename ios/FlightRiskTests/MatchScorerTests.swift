import XCTest
@testable import FlightRisk

final class MatchScorerTests: XCTestCase {

    // MARK: - Default init

    func testDefaultWeights() {
        let scorer = MatchScorer()
        XCTAssertEqual(scorer.reidWeight, 0.35)
        XCTAssertEqual(scorer.faceWeight, 0.40)
        XCTAssertEqual(scorer.reasoningWeight, 0.25)
        XCTAssertEqual(scorer.matchThreshold, 0.45)
    }

    func testCustomWeights() {
        let scorer = MatchScorer(reidWeight: 0.5, faceWeight: 0.3, reasoningWeight: 0.2, matchThreshold: 0.6)
        XCTAssertEqual(scorer.reidWeight, 0.5)
        XCTAssertEqual(scorer.faceWeight, 0.3)
        XCTAssertEqual(scorer.reasoningWeight, 0.2)
        XCTAssertEqual(scorer.matchThreshold, 0.6)
    }

    // MARK: - score() with all zeros

    func testScoreAllZerosReturnsZero() {
        let scorer = MatchScorer()
        let result = scorer.score(reidScore: 0, faceScore: 0)
        XCTAssertEqual(result.combinedScore, 0.0)
        XCTAssertFalse(result.isMatch)
        XCTAssertEqual(result.confidenceLevel, "none")
        XCTAssertEqual(result.signalsUsed, 0)
    }

    // MARK: - score() with single signal

    func testScoreReidOnly() {
        let scorer = MatchScorer()
        let result = scorer.score(reidScore: 0.8, faceScore: 0)
        // Only reid active: weight 0.35 -> normalizes to 1.0
        // combined = 0.8 * 1.0 = 0.8
        XCTAssertEqual(result.combinedScore, 0.8)
        XCTAssertTrue(result.isMatch) // 0.8 >= 0.45
        XCTAssertEqual(result.signalsUsed, 1)
    }

    func testScoreFaceOnly() {
        let scorer = MatchScorer()
        let result = scorer.score(reidScore: 0, faceScore: 0.6)
        // Only face active: weight 0.40 -> normalizes to 1.0
        // combined = 0.6 * 1.0 = 0.6
        XCTAssertEqual(result.combinedScore, 0.6)
        XCTAssertTrue(result.isMatch) // 0.6 >= 0.45
        XCTAssertEqual(result.signalsUsed, 1)
    }

    // MARK: - score() with two signals

    func testScoreReidAndFace() {
        let scorer = MatchScorer()
        let result = scorer.score(reidScore: 0.7, faceScore: 0.9)
        // reid weight 0.35, face weight 0.40; total = 0.75
        // norm: reid = 0.35/0.75 ~ 0.4667, face = 0.40/0.75 ~ 0.5333
        // combined = 0.7 * 0.4667 + 0.9 * 0.5333 = 0.3267 + 0.48 = 0.8067
        // Rounded to 3 decimal places like Python's round(): 0.80666... becomes 0.807
        XCTAssertEqual(result.combinedScore, 0.807)
        XCTAssertTrue(result.isMatch)
        XCTAssertEqual(result.signalsUsed, 2)
    }

    // MARK: - score() with all three signals via reasoning

    func testScoreAllThreeSignals() {
        let scorer = MatchScorer()
        let reasoning = ReasoningResult(isMatch: true, confidence: "high", reasoning: "looks right")
        // reasoningToScore: isMatch + high -> 0.90 * 1.0 = 0.90
        let result = scorer.score(reidScore: 0.7, faceScore: 0.8, reasoningResult: reasoning)
        // All three active: weights 0.35 + 0.40 + 0.25 = 1.0 (no redistribution)
        // combined = 0.7 * 0.35 + 0.8 * 0.40 + 0.90 * 0.25
        //          = 0.245 + 0.32 + 0.225 = 0.79
        // The arithmetic is in Double and rounds like Python, so this is exactly 0.79.
        XCTAssertEqual(result.combinedScore, 0.79)
        XCTAssertTrue(result.isMatch)
        XCTAssertEqual(result.signalsUsed, 3)
    }

    // MARK: - Determinism

    func testScoreIsIdenticalAcrossDictionaryInsertionOrders() {
        let reasoning = ReasoningResult(isMatch: true, confidence: "high", reasoning: "looks right")
        let extras: [(String, Double)] = [("thermal", 0.6), ("gait", 0.5), ("audio", 0.4), ("pose", 0.7)]
        var baseline: Double?
        var extrasBaseline: Double?
        for i in 0..<200 {
            var scorer = MatchScorer()
            var order = extras
            // Rotate and alternate direction so insertion order differs per run.
            order = Array(order[(i % order.count)...] + order[..<(i % order.count)])
            if i % 2 == 1 { order.reverse() }
            for (name, weight) in order { scorer.registerSignal(name: name, weight: weight / 4) }
            var extraScores: [String: Double] = [:]
            for (name, value) in order { extraScores[name] = value }

            let plain = scorer.score(reidScore: 0.7, faceScore: 0.8, reasoningResult: reasoning).combinedScore
            let withExtras = scorer.score(
                reidScore: 0.7, faceScore: 0.8, reasoningResult: reasoning, extraSignalScores: extraScores
            ).combinedScore
            if let b = baseline, let e = extrasBaseline {
                XCTAssertEqual(plain, b, "run \(i) differed")
                XCTAssertEqual(withExtras, e, "run \(i) with extras differed")
            } else {
                baseline = plain
                extrasBaseline = withExtras
            }
        }
    }

    // MARK: - Weight redistribution

    func testWeightRedistributionExcludesMissingSignals() {
        let scorer = MatchScorer()
        // If face=0 (missing), reid (0.35) and reasoning (0.25) share the weight
        let reasoning = ReasoningResult(isMatch: true, confidence: "medium", reasoning: "maybe")
        // reasoningToScore: isMatch + medium -> 0.65 * 1.0 = 0.65
        let result = scorer.score(reidScore: 0.5, faceScore: 0, reasoningResult: reasoning)
        // Active: reid 0.35, reasoning 0.25; total = 0.60
        // norm: reid = 0.35/0.60 ~ 0.5833, reasoning = 0.25/0.60 ~ 0.4167
        // combined = 0.5 * 0.5833 + 0.65 * 0.4167 = 0.2917 + 0.2708 = 0.5625
        XCTAssertEqual(result.combinedScore, 0.562, accuracy: 0.001)
        XCTAssertTrue(result.isMatch)
        XCTAssertEqual(result.signalsUsed, 2)
    }

    // MARK: - Reasoning "no match" included in weighted average

    func testNoMatchReasoningIncludedInAverage() {
        let scorer = MatchScorer()
        // "no match" with high confidence -> 0.10 (dampened, not zero)
        let reasoning = ReasoningResult(isMatch: false, confidence: "high", reasoning: "different person")
        let result = scorer.score(reidScore: 0.8, faceScore: 0.7, reasoningResult: reasoning)
        // reasoningToScore: !isMatch + high -> 0.10 * 1.0 = 0.10
        // 0.10 > 0, so reasoning IS included
        // All three active: weights sum to 1.0
        // combined = 0.8 * 0.35 + 0.7 * 0.40 + 0.10 * 0.25
        //          = 0.28 + 0.28 + 0.025 = 0.585
        XCTAssertEqual(result.combinedScore, 0.585, accuracy: 0.001)
        XCTAssertTrue(result.isMatch) // 0.585 >= 0.45
        XCTAssertEqual(result.signalsUsed, 3)
    }

    func testNoMatchReasoningDragsDownScore() {
        let scorer = MatchScorer()
        // Without reasoning
        let withoutReasoning = scorer.score(reidScore: 0.6, faceScore: 0.5)
        // With "no match" reasoning (dampened score)
        let reasoning = ReasoningResult(isMatch: false, confidence: "high", reasoning: "no")
        let withReasoning = scorer.score(reidScore: 0.6, faceScore: 0.5, reasoningResult: reasoning)
        // The dampened reasoning (0.10) should lower the combined score
        XCTAssertLessThan(withReasoning.combinedScore, withoutReasoning.combinedScore)
    }

    // MARK: - positiveSignalCount and confidence level

    func testPositiveSignalCountExcludesDampenedScores() {
        let scorer = MatchScorer()
        // "no match" high confidence -> 0.10, which is < 0.3 so not a positive signal
        let reasoning = ReasoningResult(isMatch: false, confidence: "high", reasoning: "no")
        let result = scorer.score(reidScore: 0.8, faceScore: 0, reasoningResult: reasoning)
        // Only reid (0.8) is >= 0.3, reasoning (0.10) is not
        // positiveSignalCount = 1
        // combined: reid only normalizes to 1.0 for active weights
        // but reasoning IS included in active weights since 0.10 > 0
        // Active: reid 0.35, reasoning 0.25; total = 0.60
        // norm: reid = 0.35/0.60, reasoning = 0.25/0.60
        // combined = 0.8 * (0.35/0.60) + 0.10 * (0.25/0.60)
        //          = 0.8 * 0.5833 + 0.10 * 0.4167
        //          = 0.4667 + 0.0417 = 0.5083
        // positiveSignalCount = 1 (only reid >= 0.3)
        // So combined >= 0.40 -> "medium" but NOT "high" (needs >= 2 positive signals)
        XCTAssertEqual(result.confidenceLevel, "medium")
    }

    func testHighConfidenceRequiresTwoPositiveSignals() {
        let scorer = MatchScorer()
        // Two strong signals -> high confidence
        let result = scorer.score(reidScore: 0.9, faceScore: 0.85)
        // combined high enough and 2 positive signals
        XCTAssertEqual(result.confidenceLevel, "high")
        XCTAssertGreaterThanOrEqual(result.combinedScore, 0.65)
    }

    func testLowConfidenceForWeakSignals() {
        let scorer = MatchScorer()
        let result = scorer.score(reidScore: 0.15, faceScore: 0.1)
        // Both > 0 so both active, but combined will be very low
        // combined = 0.15 * (0.35/0.75) + 0.1 * (0.40/0.75)
        //         = 0.15 * 0.4667 + 0.1 * 0.5333
        //         = 0.07 + 0.0533 = 0.1233
        XCTAssertEqual(result.confidenceLevel, "low")
    }

    // MARK: - isMatch threshold

    func testIsMatchAtThreshold() {
        let scorer = MatchScorer(matchThreshold: 0.5)
        // Single signal: normalized to 1.0, so combined = reidScore
        let result = scorer.score(reidScore: 0.5, faceScore: 0)
        XCTAssertTrue(result.isMatch) // 0.5 >= 0.5
    }

    func testIsMatchBelowThreshold() {
        let scorer = MatchScorer(matchThreshold: 0.5)
        let result = scorer.score(reidScore: 0.49, faceScore: 0)
        XCTAssertFalse(result.isMatch) // 0.49 < 0.5
    }

    // MARK: - alertLevel()

    func testAlertLevelConfirmedMatch() {
        let scorer = MatchScorer()
        let result = ScoredResult(combinedScore: 0.75, isMatch: true, confidenceLevel: "high", signalsUsed: 2)
        XCTAssertEqual(scorer.alertLevel(result), "confirmed_match")
    }

    func testAlertLevelPossibleMatch() {
        let scorer = MatchScorer()
        let result = ScoredResult(combinedScore: 0.50, isMatch: true, confidenceLevel: "medium", signalsUsed: 1)
        XCTAssertEqual(scorer.alertLevel(result), "possible_match")
    }

    func testAlertLevelWeakSignal() {
        let scorer = MatchScorer()
        // matchThreshold * 0.5 = 0.225
        let result = ScoredResult(combinedScore: 0.25, isMatch: false, confidenceLevel: "low", signalsUsed: 1)
        XCTAssertEqual(scorer.alertLevel(result), "weak_signal")
    }

    func testAlertLevelNoMatch() {
        let scorer = MatchScorer()
        // below matchThreshold * 0.5 = 0.225
        let result = ScoredResult(combinedScore: 0.1, isMatch: false, confidenceLevel: "low", signalsUsed: 1)
        XCTAssertEqual(scorer.alertLevel(result), "no_match")
    }

    func testAlertLevelHighScoreLowSignalsNotConfirmed() {
        let scorer = MatchScorer()
        // High score but only 1 signal -> not confirmed
        let result = ScoredResult(combinedScore: 0.80, isMatch: true, confidenceLevel: "high", signalsUsed: 1)
        // score >= 0.65 but signals < 2 -> fails confirmed
        // score >= matchThreshold (0.45) and conf == "high" -> possible_match
        XCTAssertEqual(scorer.alertLevel(result), "possible_match")
    }

    // MARK: - reasoningToScore

    func testReasoningToScoreNil() {
        XCTAssertEqual(MatchScorer.reasoningToScore(result: nil), 0.0)
    }

    func testReasoningToScoreMatchHigh() {
        let r = ReasoningResult(isMatch: true, confidence: "high", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.90)
    }

    func testReasoningToScoreMatchMedium() {
        let r = ReasoningResult(isMatch: true, confidence: "medium", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.65)
    }

    func testReasoningToScoreMatchLow() {
        let r = ReasoningResult(isMatch: true, confidence: "low", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.40)
    }

    func testReasoningToScoreMatchUnknown() {
        let r = ReasoningResult(isMatch: true, confidence: "unknown", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.50)
    }

    func testReasoningToScoreNoMatchHigh() {
        let r = ReasoningResult(isMatch: false, confidence: "high", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.10)
    }

    func testReasoningToScoreNoMatchMedium() {
        let r = ReasoningResult(isMatch: false, confidence: "medium", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.20)
    }

    func testReasoningToScoreNoMatchLow() {
        let r = ReasoningResult(isMatch: false, confidence: "low", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.30)
    }

    func testReasoningToScoreNoMatchUnknown() {
        let r = ReasoningResult(isMatch: false, confidence: "whatever", reasoning: "")
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.30)
    }

    func testReasoningToScoreWithDiscount() {
        let r = ReasoningResult(isMatch: true, confidence: "high", reasoning: "", confidenceDiscount: 0.5)
        // 0.90 * 0.5 = 0.45
        XCTAssertEqual(MatchScorer.reasoningToScore(result: r), 0.45)
    }

    // MARK: - Extra signals via registerSignal

    func testRegisterAndScoreExtraSignal() {
        var scorer = MatchScorer()
        scorer.registerSignal(name: "thermal", weight: 0.20)
        let result = scorer.score(reidScore: 0.7, faceScore: 0.8, extraSignalScores: ["thermal": 0.6])
        // Active: reid 0.35, face 0.40, thermal 0.20; total = 0.95
        // combined = 0.7*(0.35/0.95) + 0.8*(0.40/0.95) + 0.6*(0.20/0.95)
        XCTAssertGreaterThan(result.combinedScore, 0)
        XCTAssertEqual(result.signalsUsed, 3)
    }
}

// MARK: - Shared vectors computed by the Python scorer

/// `tests/fixtures/mobile_contract/match_score_vectors.json` holds inputs and the results of the Python
/// `MatchScorer.score`. The Swift and Kotlin scorers must reproduce them exactly (the same file is read by
/// `tests/test_match_score_vectors.py` and by the Android tests).
final class MatchScoreVectorTests: XCTestCase {
    private struct Vector {
        let name: String
        let scorer: [String: Double]
        let extraWeights: [String: Double]
        let signals: [String: Double]
        let combined: Double
        let isMatch: Bool
        let signalsUsed: Int
        let confidence: String
    }

    private static func number(_ any: Any?) -> Double {
        if let s = any as? String {
            switch s {
            case "nan": return .nan
            case "inf": return .infinity
            case "-inf": return -.infinity
            default: return Double(s) ?? .nan
            }
        }
        return (any as? NSNumber)?.doubleValue ?? .nan
    }

    private func loadVectors() throws -> [Vector] {
        var url = URL(fileURLWithPath: "\(#filePath)").deletingLastPathComponent()
        while url.path != "/" {
            let candidate = url.appendingPathComponent("tests/fixtures/mobile_contract/match_score_vectors.json")
            if FileManager.default.fileExists(atPath: candidate.path) {
                let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: candidate)) as? [String: Any])
                let cases = try XCTUnwrap(root["cases"] as? [String: [String: Any]])
                return try cases.keys.sorted().map { name in
                    let payload = try XCTUnwrap(cases[name]?["payload"] as? [String: Any])
                    func map(_ key: String) -> [String: Double] {
                        (payload[key] as? [String: Any] ?? [:]).mapValues { Self.number($0) }
                    }
                    let expected = try XCTUnwrap(payload["expected"] as? [String: Any])
                    return Vector(
                        name: name, scorer: map("scorer"), extraWeights: map("extra_signal_weights"),
                        signals: map("signals"), combined: Self.number(expected["combined_score"]),
                        isMatch: expected["is_match"] as? Bool ?? false,
                        signalsUsed: (expected["signals_used"] as? NSNumber)?.intValue ?? -1,
                        confidence: expected["confidence_level"] as? String ?? ""
                    )
                }
            }
            url.deleteLastPathComponent()
        }
        XCTFail("the repository fixtures are not reachable from this file")
        return []
    }

    private func sameDouble(_ a: Double, _ b: Double) -> Bool {
        if a.isNaN { return b.isNaN }
        return a == b && a.sign == b.sign
    }

    func testEveryPythonVectorIsReproducedExactly() throws {
        let vectors = try loadVectors()
        XCTAssertGreaterThanOrEqual(vectors.count, 80)
        var compared = 0
        for v in vectors {
            var scorer = MatchScorer(
                reidWeight: try XCTUnwrap(v.scorer["reid_weight"]),
                faceWeight: try XCTUnwrap(v.scorer["face_weight"]),
                reasoningWeight: try XCTUnwrap(v.scorer["reasoning_weight"]),
                matchThreshold: try XCTUnwrap(v.scorer["match_threshold"])
            )
            for (name, weight) in v.extraWeights { scorer.registerSignal(name: name, weight: weight) }
            var extras = v.signals
            let reid = extras.removeValue(forKey: "reid") ?? 0
            let face = extras.removeValue(forKey: "face") ?? 0
            let result = scorer.score(reidScore: reid, faceScore: face, extraSignalScores: extras)
            XCTAssertTrue(sameDouble(result.combinedScore, v.combined), "\(v.name): got \(result.combinedScore), Python \(v.combined)")
            XCTAssertEqual(result.isMatch, v.isMatch, v.name)
            XCTAssertEqual(result.signalsUsed, v.signalsUsed, v.name)
            // The phone counts only signals of at least 0.3 when it decides the confidence level, while
            // Python counts every signal with a positive score (a known difference, see
            // docs/mobile-followups.md), so the level is compared only where the two counts agree.
            let positive = v.signals.values.filter { $0 > 0 }
            if positive.allSatisfy({ $0 >= 0.3 }) {
                XCTAssertEqual(result.confidenceLevel, v.confidence, v.name)
                compared += 1
            }
        }
        XCTAssertGreaterThanOrEqual(compared, 20, "the confidence level is compared for a good share of the vectors")
    }

    func testRoundingMatchesPythonOnTiesAndBoundaries() {
        // Python: round(0.0625, 3) == 0.062 and round(0.1875, 3) == 0.188 (exact ties go to even);
        // round(0.0005, 3) == 0.001 because the double 0.0005 is slightly above the tie;
        // truncation would give 0.062, 0.187 and 0.0.
        XCTAssertEqual(MatchScorer.roundToThreePlaces(0.0625), 0.062)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(0.1875), 0.188)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(0.0005), 0.001)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(0.9995), 1.0)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(0.4496), 0.45)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(0.4494), 0.449)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(-0.0001).sign, .minus, "round(-0.0001, 3) is -0.0")
        XCTAssertEqual(MatchScorer.roundToThreePlaces(-0.0001), 0)
        XCTAssertTrue(MatchScorer.roundToThreePlaces(.nan).isNaN)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(.infinity), .infinity)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(-.infinity), -.infinity)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(1e300), 1e300)
        XCTAssertEqual(MatchScorer.roundToThreePlaces(5e-324), 0)
    }

    func testConfiguredFloatSettingsBecomeTheDecimalPythonHolds() {
        XCTAssertEqual(MatchScorer.decimal(0.35), 0.35)
        XCTAssertEqual(MatchScorer.decimal(0.40), 0.4)
        XCTAssertEqual(MatchScorer.decimal(0.25), 0.25)
        XCTAssertEqual(MatchScorer.decimal(0.45), 0.45)
    }
}
