import Foundation
import Security

/// Errors that can occur during Keychain operations.
enum KeychainError: Error {
    case duplicateItem
    case itemNotFound
    case unexpectedStatus(OSStatus)
    case invalidData
}

/// Secure storage for API keys and other sensitive data via the iOS Keychain.
///
/// Uses `kSecClassGenericPassword` items scoped to the FlightRisk service.
/// API keys MUST be stored here, never in UserDefaults.
enum KeychainHelper {
    private static let service = "com.flightrisk.app"

    // MARK: - Generic Operations

    /// Save data to the Keychain under the given key.
    /// Overwrites any existing item with the same key.
    static func save(key: String, data: Data) throws {
        // Delete any existing item first to avoid duplicates
        delete(key: key)

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]

        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            if status == errSecDuplicateItem {
                throw KeychainError.duplicateItem
            }
            throw KeychainError.unexpectedStatus(status)
        }
    }

    /// Load data from the Keychain for the given key.
    /// Returns `nil` if the item does not exist.
    static func load(key: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess else { return nil }
        return result as? Data
    }

    /// Delete an item from the Keychain for the given key.
    /// No-op if the item does not exist.
    static func delete(key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]

        SecItemDelete(query as CFDictionary)
    }

    // MARK: - API Key Convenience

    /// Save an API key string for a specific provider.
    static func saveApiKey(_ apiKey: String, provider: String = "cloud_claude") throws {
        guard let data = apiKey.data(using: .utf8) else {
            throw KeychainError.invalidData
        }
        try save(key: "flightrisk_api_key_\(provider)", data: data)
    }

    /// Load the stored API key for a specific provider.
    static func loadApiKey(provider: String = "cloud_claude") -> String? {
        guard let data = load(key: "flightrisk_api_key_\(provider)") else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// Delete the stored API key for a specific provider.
    static func deleteApiKey(provider: String = "cloud_claude") {
        delete(key: "flightrisk_api_key_\(provider)")
    }
}
