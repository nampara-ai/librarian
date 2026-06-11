import Foundation
import Security

/// Minimal Keychain wrapper for provider API keys: generic passwords under
/// one service, keyed by the environment-variable name the backend reads.
enum KeychainStore {
    private static let service = "ai.nampara.librarian"

    static func get(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ value: String, account: String) {
        let encoded = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let update: [String: Any] = [kSecValueData as String: encoded]
        let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = encoded
            SecItemAdd(insert as CFDictionary, nil)
        }
    }

    static func delete(_ account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}

/// Bridges Keychain-held provider keys to the backend, which reads them from
/// environment variables. The key never touches the .env file on disk.
enum ProviderCredentials {
    static let knownKeyAccounts = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]

    /// Environment entries to add when spawning the backend or CLI.
    /// Also migrates any key found in a legacy .env into the Keychain.
    static func environmentOverlay() -> [String: String] {
        migrateLegacyEnvKeysIfNeeded()
        var overlay: [String: String] = [:]
        for account in knownKeyAccounts {
            if let value = KeychainStore.get(account), !value.isEmpty {
                overlay[account] = value
            }
        }
        return overlay
    }

    static func migrateLegacyEnvKeysIfNeeded() {
        let values = EnvFile.read()
        var removals: [String: String?] = [:]
        for account in knownKeyAccounts {
            guard let legacy = values[account], !legacy.isEmpty else { continue }
            if KeychainStore.get(account) == nil {
                KeychainStore.set(legacy, account: account)
            }
            removals.updateValue(nil, forKey: account)
        }
        if !removals.isEmpty {
            try? EnvFile.update(removals)
        }
    }
}
