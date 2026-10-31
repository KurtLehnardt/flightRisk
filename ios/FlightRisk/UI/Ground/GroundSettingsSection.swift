import SwiftUI

/// The ground station connection settings, shown inside Settings.
///
/// The dashboard API key is typed into a secure field, saved in the Keychain, and the field is
/// cleared at once: the key is never shown again. Only whether a key is saved is displayed.
struct GroundSettingsSection: View {
    let viewModel: GroundViewModel

    @State private var addressInput: String = ""
    @State private var addressMessage: String?
    @State private var keyInput: String = ""
    @State private var keyMessage: String?
    @State private var testResult: String?
    @State private var isTesting = false

    private var keyStateText: String {
        switch viewModel.keyState {
        case .saved: return "A key is saved in the Keychain."
        case .none: return "No key saved."
        case .unavailable: return "Keychain locked: unlock the phone."
        }
    }

    var body: some View {
        Section {
            Text(GroundUIText.privacyNote)
                .font(.footnote)
                .foregroundStyle(.secondary)

            TextField("ws://192.168.1.20:5555", text: $addressInput)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .accessibilityLabel("Ground station address")
            Button("Save address") {
                addressMessage = viewModel.saveAddress(addressInput)
                if addressMessage == nil { addressInput = viewModel.savedAddress ?? "" }
            }
            if let message = addressMessage {
                Text(message).font(.footnote).foregroundStyle(FlightRiskTheme.alertOrange)
            }

            SecureField("Dashboard API key", text: $keyInput)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .textContentType(.password)
            Button("Save key") {
                keyMessage = viewModel.saveKey(keyInput)
                // Never echo the key back, whether or not it was saved.
                keyInput = ""
            }
            .disabled(keyInput.isEmpty)
            Text(keyStateText)
                .font(.footnote)
                .foregroundStyle(.secondary)
            if let message = keyMessage {
                Text(message).font(.footnote).foregroundStyle(FlightRiskTheme.alertOrange)
            }
            if viewModel.hasSavedKey {
                Button("Remove saved key", role: .destructive) {
                    viewModel.removeKey()
                    keyMessage = nil
                }
            }

            Toggle("Connect to the ground station", isOn: Binding(
                get: { viewModel.isEnabled },
                set: { viewModel.setEnabled($0) }
            ))
            .disabled(!viewModel.isConfigured && !viewModel.isEnabled)

            Text(viewModel.linkStatus.text)
                .font(.footnote)
                .foregroundStyle(.secondary)

            Button {
                isTesting = true
                testResult = nil
                Task {
                    testResult = await viewModel.testConnection()
                    isTesting = false
                }
            } label: {
                HStack {
                    Text("Test connection")
                    if isTesting { ProgressView().controlSize(.small) }
                }
            }
            .disabled(isTesting || !viewModel.isConfigured)
            if let result = testResult {
                Text(result).font(.footnote).foregroundStyle(.secondary)
            }
        } header: {
            Text("Ground Station")
        } footer: {
            Text("Enter ws://HOST:PORT (port 5555 if omitted) and the ground's FLIGHTRISK_API_KEY.")
        }
        .onAppear {
            if addressInput.isEmpty { addressInput = viewModel.savedAddress ?? "" }
        }
    }
}
