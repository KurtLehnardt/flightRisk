import GroundProtocol
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
            .disabled((!viewModel.isConfigured && !viewModel.isEnabled) || viewModel.isDemoActive)
            if viewModel.isDemoActive {
                Text(GroundUIText.demoBlocksLink)
                    .font(.footnote)
                    .foregroundStyle(FlightRiskTheme.alertOrange)
            }

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
            .disabled(isTesting || !viewModel.isConfigured || viewModel.isDemoActive)
            if let result = testResult {
                Text(result).font(.footnote).foregroundStyle(.secondary)
            }
        } header: {
            Text("Ground Station")
        } footer: {
            Text("Enter ws://HOST:PORT (port 5555 if omitted) and the ground's FLIGHTRISK_API_KEY. A ws:// or http:// address must be on your local network; use wss:// or https:// for any other address.")
        }
        .onAppear {
            if addressInput.isEmpty { addressInput = viewModel.savedAddress ?? "" }
        }
    }
}

// MARK: - Demo mode

/// Texts of the demo mode, shared by Settings, onboarding and the banner.
enum DemoUIText {
    /// The banner shown on every tab while the demo runs.
    static let banner = "DEMO \u{2013} no real drones"
    static let tryDemo = "Try the demo (no drones, no photo)"
    static let tryDemoHint = "Shows the app with simulated drones. No camera, no photo, and nothing is sent to any network."
    static let confirmTitle = "Disconnect the ground station?"
    static let confirmMessage =
        "The demo cannot run beside a live ground station. The link closes now and stays off until you switch it on again in Settings. "
        + "While the demo runs, LAND ALL on this phone lands only the simulated drones: use the ground laptop for real drones."
    static let confirmButton = "Disconnect and start the demo"
    static let footer =
        "The demo plays a scenario on this phone: two simulated drones search their lanes, one stops at an obstacle, "
        + "a match alert gets an AI verdict, and the drones land. It needs no camera, photo or ground station, and nothing is sent to any network. "
        + "Every command is answered by the simulated ground, and LAND ALL reads \"LAND ALL (demo)\". Turning the demo off clears everything it showed. "
        + "The demo does not start while the ground station reports drones in the air or a LAND ALL is unanswered or failed."
}

/// Asks before a demo starts beside a live link, then starts it, and says why when the demo is
/// refused. Used by Settings and onboarding. `start` runs only after the user confirmed.
struct DemoStartConfirmation: ViewModifier {
    @Binding var isPresented: Bool
    /// Why the demo cannot start; the alert shows while it is set.
    @Binding var refusal: String?
    let start: () -> Void

    func body(content: Content) -> some View {
        content
            .confirmationDialog(DemoUIText.confirmTitle, isPresented: $isPresented, titleVisibility: .visible) {
                Button(DemoUIText.confirmButton, role: .destructive, action: start)
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(DemoUIText.confirmMessage)
            }
            .alert(DemoModePolicy.refusedTitle, isPresented: Binding(
                get: { refusal != nil },
                set: { if !$0 { refusal = nil } }
            )) {
                Button("OK", role: .cancel) { refusal = nil }
            } message: {
                Text(refusal ?? "")
            }
    }

    /// Starts the demo at once when no live link is open or switched on, asks first when one
    /// is, and gives the reason when the demo is refused (live drones in the air, a LAND ALL
    /// unanswered or failed).
    static func request(_ viewModel: GroundViewModel, start: () -> Void, confirm: () -> Void, refuse: (String) -> Void) {
        switch viewModel.demoStartStep {
        case .startNow: start()
        case .confirmDisconnectFirst: confirm()
        case let .refused(reason): refuse(reason)
        }
    }

    /// Starts the demo after a request or a confirmation. Something may have changed while the
    /// dialog showed (a drone took off): then nothing starts and the reason is returned.
    @discardableResult
    static func start(_ viewModel: GroundViewModel) -> String? {
        if viewModel.startDemo() { return nil }
        if case let .refused(reason) = viewModel.demoStartStep { return reason }
        return nil
    }
}

/// The demo switch in Settings.
struct DemoSettingsSection: View {
    let viewModel: GroundViewModel
    @State private var confirmingStart = false
    @State private var refusal: String?

    var body: some View {
        Section {
            Toggle("Demo mode", isOn: Binding(
                get: { viewModel.isDemoActive },
                set: { on in
                    if on {
                        DemoStartConfirmation.request(viewModel, start: start, confirm: { confirmingStart = true }, refuse: { refusal = $0 })
                    } else {
                        viewModel.stopDemo()
                    }
                }
            ))
            .accessibilityHint("Shows a simulated ground station with simulated drones. Nothing is sent to any network.")
            .accessibilityIdentifier("settings.demoMode")
            if viewModel.isDemoActive {
                Button("Play the demo again") { viewModel.restartDemo() }
            }
        } header: {
            Text("Demo")
        } footer: {
            Text(DemoUIText.footer)
        }
        .modifier(DemoStartConfirmation(isPresented: $confirmingStart, refusal: $refusal, start: start))
    }

    private func start() {
        refusal = DemoStartConfirmation.start(viewModel)
    }
}
