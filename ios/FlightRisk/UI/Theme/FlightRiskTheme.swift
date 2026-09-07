import SwiftUI

enum FlightRiskTheme {
    // Alert Colors
    static let alertRed = Color(red: 0xB9 / 255.0, green: 0x1C / 255.0, blue: 0x1C / 255.0)
    static let alertRedDark = Color(red: 0x99 / 255.0, green: 0x1B / 255.0, blue: 0x1B / 255.0)
    static let alertOrange = Color(red: 0xEA / 255.0, green: 0x58 / 255.0, blue: 0x0C / 255.0)

    // Detection Colors
    static let detectionBlue = Color(red: 0x25 / 255.0, green: 0x63 / 255.0, blue: 0xEB / 255.0)
    static let matchGreen = Color(red: 0x16 / 255.0, green: 0xA3 / 255.0, blue: 0x4A / 255.0)

    // HUD Colors
    static let hudWhite = Color.white
    static let hudBlack = Color.black
    static let hudBackground = Color.black.opacity(0.6)

    // Surface Colors
    static let surfaceLight = Color(red: 0xF5 / 255.0, green: 0xF5 / 255.0, blue: 0xF5 / 255.0)
    static let surfaceDark = Color(red: 0x1C / 255.0, green: 0x1C / 255.0, blue: 0x1E / 255.0)

    // Quality Grade Colors
    static func gradeColor(_ grade: String) -> Color {
        switch grade {
        case "A", "B": return matchGreen
        case "C": return alertOrange
        default: return alertRed
        }
    }
}
