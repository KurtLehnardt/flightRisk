import Network
import SwiftUI

/// Main settings screen for FlightRisk iOS.
///
/// Uses native `Form` with `Section` groups for an idiomatic iOS feel.
/// Mirrors the layout of the Android `SettingsScreen.kt`:
///   1. Sensitivity presets (pill buttons)
///   2. LLM backend picker + API key
///   3. Drone status (read-only)
///   4. Advanced threshold sliders (collapsible)
///   5. About
struct SettingsView: View {
    let config: FlightRiskConfig
    let onPresetSelected: (SensitivityPreset) -> Void
    let onThresholdChanged: (String, Float) -> Void
    let onLlmBackendChanged: (String) -> Void
    let onApiKeyChanged: (String) -> Void
    let llmAvailable: Bool
    let droneState: TelloState?
    let frameSourceMode: FrameSourceMode
    let modelManager: (any GemmaModelManaging)?

    @AppStorage("flightrisk_active_preset") private var activePresetRaw: String = SensitivityPreset.balanced.rawValue
    @State private var apiKeyInput: String = ""
    @State private var reasoningProvider: ReasoningProvider = .off
    @State private var cloudOptIn: Bool = false
    @State private var hasSavedClaudeKey: Bool = false
    @State private var showCloudConsent: Bool = false
    @State private var consentSelectsCloud: Bool = false
    private let reasoningSettings = ReasoningSettings()
    private let reasoningKeyStore = KeychainReasoningKeyStore()
    @State private var advancedExpanded: Bool = false
    @State private var showApiKeySaved: Bool = false
    @State private var hasInternet: Bool = true
    @State private var gemmaState: GemmaDownloadState = .idle
    @State private var showDeleteConfirmation: Bool = false
    @State private var algorithmsExpanded: Bool = false

    // MARK: - Derived State

    private var activePreset: SensitivityPreset? {
        SensitivityPreset(rawValue: activePresetRaw)
    }

    // MARK: - Body

    var body: some View {
        Form {
            sensitivitySection
            llmSection
            matchingAlgorithmsSection
            droneSection
            advancedSection
            aboutSection
        }
        .navigationTitle("Settings")
        .onAppear {
            loadReasoningSettings()
            checkNetworkConnectivity()
        }
        .alert("Delete AI Model?", isPresented: $showDeleteConfirmation) {
            Button("Delete", role: .destructive) {
                Task { try? await modelManager?.deleteModel() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This will free ~1.5 GB. You'll need WiFi to re-download.")
        }
        // Cloud consent dialog for the reasoning section, attached to the
        // Form so it is presented once from either the toggle or the picker.
        .alert("Send images to Anthropic?", isPresented: $showCloudConsent) {
            Button("Allow Cloud Reasoning") {
                confirmCloudOptIn()
            }
            Button("Cancel", role: .cancel) {
                consentSelectsCloud = false
            }
        } message: {
            Text(Self.cloudConsentMessage)
        }
        .onChange(of: gemmaState) { _, _ in
            // Let the selector pick up Gemma as soon as it becomes ready (or
            // drop it when it is unloaded or deleted).
            onLlmBackendChanged(reasoningProvider.rawValue)
        }
        .onChange(of: reasoningStatus.selected) { _, newValue in
            // With no explicit choice stored, follow the resolved default.
            if reasoningSettings.storedProvider == nil { reasoningProvider = newValue }
        }
        .task(id: reasoningProvider) {
            guard reasoningProvider == .localGemma, let manager = modelManager else { return }
            gemmaState = await manager.state
            // Poll state periodically during download
            while !Task.isCancelled && reasoningProvider == .localGemma {
                try? await Task.sleep(for: .seconds(0.5))
                gemmaState = await manager.state
            }
        }
    }

    // MARK: - Section 1: Sensitivity Presets

    private var sensitivitySection: some View {
        Section {
            VStack(alignment: .leading, spacing: 4) {
                Text("Controls how aggressively the system alerts on potential matches.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            .listRowSeparator(.hidden)

            ForEach(SensitivityPreset.allCases) { preset in
                SensitivityPillButton(
                    preset: preset,
                    isActive: activePreset == preset,
                    onTap: {
                        activePresetRaw = preset.rawValue
                        onPresetSelected(preset)
                    }
                )
                .listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
            }
        } header: {
            Text("Detection Sensitivity")
        }
    }

    // MARK: - Section 2: AI Reasoning

    /// Reasoning provider picker (Local Gemma, Cloud Claude, Off), the
    /// explicit cloud opt-in with its consent dialog, the Claude key field
    /// (shown only after opt-in) and a "Cloud active" indicator.
    private var llmSection: some View {
        Section {
            Picker("Provider", selection: providerBinding) {
                ForEach(ReasoningProvider.allCases) { provider in
                    Text(pickerLabel(for: provider)).tag(provider)
                }
            }

            if reasoningStatus.isCloudActive {
                CloudActiveIndicator()
            } else {
                HStack(spacing: 8) {
                    Circle()
                        .fill(reasoningStatus.active == .off ? Color.secondary : FlightRiskTheme.matchGreen)
                        .frame(width: 8, height: 8)
                    Text("Active: \(reasoningStatus.active.displayName)")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Active reasoning: \(reasoningStatus.active.displayName)")
            }

            if let notice = reasoningStatus.notice {
                Text(notice)
                    .font(.footnote)
                    .foregroundStyle(FlightRiskTheme.alertOrange)
            }

            Toggle("Allow cloud reasoning", isOn: cloudOptInBinding)
                .accessibilityHint("Asks for confirmation before any image can be sent to Anthropic")

            if let reason = reasoningStatus.unavailable[.cloudClaude] {
                Text("Cloud Claude unavailable: \(reason)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if cloudOptIn {
                claudeKeyRows
            }

            // Removing a saved key is always possible, opted in or not.
            if hasSavedClaudeKey {
                Button("Remove Saved Key", role: .destructive) {
                    removeApiKey()
                }
            }

            if reasoningProvider == .localGemma {
                Text("Runs on-device. No image leaves the phone.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Local Gemma model runs on-device. No image leaves the phone.")

                // State-driven UI
                switch gemmaState {
                case .idle:
                    Button("Download Model (~1.5 GB)") {
                        Task { try? await modelManager?.download() }
                    }
                    .accessibilityHint("Downloads the Gemma AI model for on-device reasoning")
                    Text("Requires WiFi. Model stored locally on device.")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)

                case .downloading(let progress):
                    ProgressView(value: progress) {
                        Text("Downloading... \(Int(progress * 100))%")
                            .font(.footnote)
                    }
                    .accessibilityLabel("Downloading model, \(Int(progress * 100)) percent complete")
                    Button("Cancel", role: .destructive) {
                        Task { await modelManager?.cancelDownload() }
                    }
                    .font(.footnote)
                    .accessibilityLabel("Cancel model download")

                case .downloaded:
                    HStack(spacing: 8) {
                        Circle().fill(FlightRiskTheme.matchGreen).frame(width: 8, height: 8)
                        Text("Downloaded").font(.footnote).foregroundStyle(FlightRiskTheme.matchGreen)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Model status: Downloaded")
                    Button("Delete Model", role: .destructive) {
                        showDeleteConfirmation = true
                    }
                    .font(.footnote)
                    .accessibilityLabel("Delete downloaded AI model")

                case .loading:
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Loading model...").font(.footnote).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Loading AI model")

                case .ready:
                    HStack(spacing: 8) {
                        Circle().fill(FlightRiskTheme.matchGreen).frame(width: 8, height: 8)
                        Text("Ready").font(.footnote).foregroundStyle(FlightRiskTheme.matchGreen)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("AI model status: Ready")
                    Button("Unload Model") {
                        Task { await modelManager?.unloadModel() }
                    }
                    .font(.footnote)
                    .accessibilityLabel("Unload AI model from memory")
                    Button("Delete Model", role: .destructive) {
                        showDeleteConfirmation = true
                    }
                    .font(.footnote)
                    .accessibilityLabel("Delete AI model from device")

                case .error(let message):
                    HStack(spacing: 8) {
                        Circle().fill(FlightRiskTheme.alertRed).frame(width: 8, height: 8)
                        Text(message).font(.footnote).foregroundStyle(FlightRiskTheme.alertRed)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Model error: \(message)")
                    Button("Retry") {
                        Task { try? await modelManager?.download() }
                    }
                    .font(.footnote)
                    .accessibilityLabel("Retry model download")
                }

                Text("Local analysis uses text descriptions. Match confidence is automatically adjusted.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            } else if reasoningProvider == .off {
                Text("Reasoning is off. Matches are scored without a language model.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("AI Reasoning")
        } footer: {
            Text("Cloud reasoning is off by default. Local Gemma and Off keep every image on this phone.")
        }
    }

    /// The consent text shown before cloud reasoning can be turned on.
    static let cloudConsentMessage =
        "With cloud reasoning on, the cropped image of each detected person, the reference photo you chose "
        + "and the text description you entered are sent to Anthropic (Claude) for analysis. The images can "
        + "show a face, and Anthropic's privacy policy applies to them. Nothing is sent until you also save a "
        + "Claude API key and select Cloud Claude. You can turn this off at any time here; turning it off "
        + "stops sending at once and cancels any request still in progress."

    /// The Claude API key rows, shown only after the user opted in.
    @ViewBuilder
    private var claudeKeyRows: some View {
        SecureField(hasSavedClaudeKey ? "Replace saved key" : "sk-ant-...", text: $apiKeyInput)
            .textContentType(.password)
            .autocorrectionDisabled()
            .textInputAutocapitalization(.never)

        Button {
            saveApiKey()
        } label: {
            HStack {
                Text("Save API Key")
                Spacer()
                if showApiKeySaved {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(FlightRiskTheme.matchGreen)
                }
            }
        }
        .disabled(ReasoningSettings.normalizedKey(apiKeyInput) == nil)

        HStack(spacing: 8) {
            Circle()
                .fill(llmStatusColor)
                .frame(width: 8, height: 8)
            Text(llmStatusText)
                .font(.footnote)
                .foregroundStyle(llmStatusColor)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Cloud Claude status: \(llmStatusText)")
    }

    private var reasoningStatus: ReasoningStatus {
        ReasoningActivity.shared.status
    }

    private func pickerLabel(for provider: ReasoningProvider) -> String {
        if reasoningStatus.unavailable[provider] != nil, provider != .off {
            return "\(provider.displayName) (unavailable)"
        }
        return provider.displayName
    }

    /// Picker binding. The decision (select now, or ask for consent first)
    /// is made by ``ReasoningSettings/requestProvider(_:)``.
    private var providerBinding: Binding<ReasoningProvider> {
        Binding(
            get: { reasoningProvider },
            set: { newValue in
                switch reasoningSettings.requestProvider(newValue) {
                case .applied:
                    reasoningProvider = newValue
                    onLlmBackendChanged(newValue.rawValue)
                case .askForConsent(let selectCloud):
                    askForCloudConsent(selectCloud: selectCloud)
                }
            }
        )
    }

    /// Toggle binding. Turning it on asks for consent (the toggle shows on
    /// while the dialog is open and returns to off on Cancel); turning it off
    /// applies at once, see ``ReasoningSettings/requestCloudOptIn(_:)``.
    private var cloudOptInBinding: Binding<Bool> {
        Binding(
            get: { cloudOptIn || (showCloudConsent && !consentSelectsCloud) },
            set: { enabled in
                switch reasoningSettings.requestCloudOptIn(enabled) {
                case .applied:
                    if !enabled { didDisableCloudReasoning() }
                case .askForConsent(let selectCloud):
                    askForCloudConsent(selectCloud: selectCloud)
                }
            }
        )
    }

    private func askForCloudConsent(selectCloud: Bool) {
        guard !showCloudConsent else { return }
        consentSelectsCloud = selectCloud
        showCloudConsent = true
    }

    private func loadReasoningSettings() {
        cloudOptIn = reasoningSettings.cloudOptIn
        reasoningProvider = reasoningSettings.storedProvider ?? reasoningStatus.selected
        refreshSavedKeyState()
        apiKeyInput = ""
    }

    private func refreshSavedKeyState() {
        hasSavedClaudeKey = reasoningKeyStore.lookupClaudeApiKey() != .missing
    }

    private func confirmCloudOptIn() {
        reasoningSettings.confirmCloudConsent(selectCloud: consentSelectsCloud)
        cloudOptIn = true
        if consentSelectsCloud {
            reasoningProvider = .cloudClaude
        }
        consentSelectsCloud = false
        onLlmBackendChanged(reasoningProvider.rawValue)
    }

    /// The opt-in was already turned off and saved by ``ReasoningSettings``,
    /// which also told the cloud backend to cancel requests in flight.
    private func didDisableCloudReasoning() {
        cloudOptIn = false
        apiKeyInput = ""
        if reasoningProvider.isCloud {
            let localReady = reasoningStatus.unavailable[.localGemma] == nil
            reasoningProvider = reasoningSettings.selectedProvider(localModelAvailable: localReady)
        }
        onLlmBackendChanged(reasoningProvider.rawValue)
    }

    // MARK: - Section: Matching Algorithms

    private var matchingAlgorithmsSection: some View {
        Section {
            DisclosureGroup(isExpanded: $algorithmsExpanded) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Configure which matching signals are active and their relative weights.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .listRowSeparator(.hidden)

                ForEach(FlightRiskConfig.SignalConfig.categories, id: \.name) { category in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(category.name)
                            .font(.subheadline.bold())
                            .foregroundStyle(.primary)
                            .padding(.top, 8)

                        ForEach(category.signals, id: \.self) { signalName in
                            SignalConfigRow(
                                signalName: signalName,
                                displayName: FlightRiskConfig.SignalConfig.displayNames[signalName] ?? signalName,
                                isEnabled: config.signalConfig.isEnabled(signalName),
                                weight: config.signalConfig.weight(signalName),
                                status: signalStatus(for: signalName),
                                onEnabledChanged: { enabled in
                                    config.signalConfig.setEnabled(signalName, enabled: enabled)
                                    config.saveSignalConfig()
                                },
                                onWeightChanged: { weight in
                                    config.signalConfig.setWeight(signalName, weight: weight)
                                    config.saveSignalConfig()
                                }
                            )
                        }
                    }
                }
            } label: {
                Text("Matching Algorithms")
                    .font(.headline)
            }
        }
    }

    /// Determine the status of a signal for display.
    private func signalStatus(for name: String) -> SignalStatus {
        let enabled = config.signalConfig.isEnabled(name)

        switch name {
        case "osnetReid":
            let modelExists = Bundle.main.url(forResource: "OSNetReID", withExtension: "mlmodelc") != nil
            if !modelExists { return .notInstalled }
            return enabled ? .active : .inactive
        case "insightFace":
            let modelExists = Bundle.main.url(forResource: "InsightFaceR18", withExtension: "mlmodelc") != nil
            if !modelExists { return .notInstalled }
            return enabled ? .active : .inactive
        case "face":
            let detectorExists = Bundle.main.url(forResource: "SCRFDFaceDetector", withExtension: "mlmodelc") != nil
            let arcfaceExists = Bundle.main.url(forResource: "ArcFaceMobile", withExtension: "mlmodelc") != nil
            if !detectorExists || !arcfaceExists { return .notInstalled }
            return enabled ? .active : .inactive
        case "reid":
            let modelExists = Bundle.main.url(forResource: "CLIPVisual", withExtension: "mlmodelc") != nil
            if !modelExists { return .notInstalled }
            return enabled ? .active : .inactive
        default:
            // clothingColor, heightRatio, reasoning are always available
            return enabled ? .active : .inactive
        }
    }

    // MARK: - Section 3: Drone Status

    private var droneSection: some View {
        Section {
            HStack {
                Label {
                    Text(frameSourceMode == .drone ? "Tello Drone" : "Device Camera")
                } icon: {
                    Image(systemName: frameSourceMode == .drone ? "airplane" : "camera.fill")
                }
                .foregroundStyle(.primary)
            }
            .accessibilityLabel("Video source: \(frameSourceMode == .drone ? "Tello Drone" : "Device Camera")")

            HStack(spacing: 8) {
                Circle()
                    .fill(connectionColor)
                    .frame(width: 8, height: 8)
                Text(connectionText)
                    .foregroundStyle(connectionColor)
            }
            .accessibilityLabel("Drone connection: \(connectionText)")

            if let battery = droneState?.telemetry.battery,
               droneState?.connectionState == .connected || droneState?.connectionState == .streaming {
                HStack(spacing: 8) {
                    Image(systemName: batteryIconName(for: battery))
                        .foregroundStyle(batteryColor(for: battery))
                    Text("\(battery)%")
                        .foregroundStyle(batteryColor(for: battery))
                }
                .accessibilityLabel("Drone battery: \(battery) percent")
            }
        } header: {
            Text("Drone Status")
        }
    }

    // MARK: - Section 4: Advanced (Collapsible)

    private var advancedSection: some View {
        Section {
            DisclosureGroup(isExpanded: $advancedExpanded) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Manual threshold overrides. Changing these clears the active preset.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .listRowSeparator(.hidden)

                ThresholdSliderRow(
                    label: "ReID Threshold",
                    value: config.vision.reidThreshold,
                    onChanged: { newValue in
                        clearPreset()
                        onThresholdChanged("reid", newValue)
                    }
                )

                ThresholdSliderRow(
                    label: "Face Threshold",
                    value: config.vision.faceMatchThreshold,
                    onChanged: { newValue in
                        clearPreset()
                        onThresholdChanged("face", newValue)
                    }
                )

                ThresholdSliderRow(
                    label: "Scorer Threshold",
                    value: config.vision.scorerMatchThreshold,
                    onChanged: { newValue in
                        clearPreset()
                        onThresholdChanged("scorer", newValue)
                    }
                )

                // Debug info
                VStack(alignment: .leading, spacing: 4) {
                    Text("Debug Info")
                        .font(.caption.bold())
                    Text(String(
                        format: "ReID: %.2f | Face: %.2f | Scorer: %.2f",
                        config.vision.reidThreshold,
                        config.vision.faceMatchThreshold,
                        config.vision.scorerMatchThreshold
                    ))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .monospaced()

                    Text("Version: \(appVersion)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 8)
            } label: {
                Text("Advanced Settings")
                    .font(.headline)
            }
        }
    }

    // MARK: - Section 5: About

    private var aboutSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 4) {
                Text("FlightRisk - AI-Powered Search Assistant")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Text("Version \(appVersion)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        } header: {
            Text("About")
        }
    }

    // MARK: - Helpers

    private func saveApiKey() {
        // Only reachable after opt-in. The key is trimmed, goes straight to
        // the Keychain and is not kept in view state.
        guard cloudOptIn, let key = ReasoningSettings.normalizedKey(apiKeyInput) else { return }
        do {
            try reasoningKeyStore.saveClaudeApiKey(key)
            apiKeyInput = ""
            refreshSavedKeyState()
            onApiKeyChanged("")
            showApiKeySaved = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                showApiKeySaved = false
            }
        } catch {
            // Saving failed; the status row keeps showing that no key is saved.
        }
    }

    private func removeApiKey() {
        // Deletes the key and cancels any cloud request in flight at once.
        reasoningSettings.removeClaudeKey(from: reasoningKeyStore)
        apiKeyInput = ""
        refreshSavedKeyState()
        onApiKeyChanged("")
    }

    private func clearPreset() {
        activePresetRaw = ""
    }

    private var appVersion: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(version) (\(build))"
    }

    private func checkNetworkConnectivity() {
        let monitor = NWPathMonitor()
        let queue = DispatchQueue(label: "com.flightrisk.settings.network")
        monitor.pathUpdateHandler = { path in
            let satisfied = path.status == .satisfied
            DispatchQueue.main.async {
                hasInternet = satisfied
            }
            monitor.cancel()
        }
        monitor.start(queue: queue)
    }

    // Cloud Claude status helpers
    private var llmStatusColor: Color {
        if !hasInternet {
            return FlightRiskTheme.alertOrange
        } else if !hasSavedClaudeKey {
            return FlightRiskTheme.alertRed
        } else if reasoningStatus.isCloudActive {
            return FlightRiskTheme.matchGreen
        } else {
            return .secondary
        }
    }

    private var llmStatusText: String {
        if !hasInternet {
            return "No internet"
        } else if !hasSavedClaudeKey {
            return "No API key saved"
        } else if reasoningStatus.isCloudActive {
            return "Key saved, in use"
        } else {
            return "Key saved, not in use"
        }
    }

    // Connection status helpers
    private var connectionState: TelloConnectionState {
        droneState?.connectionState ?? .disconnected
    }

    private var connectionColor: Color {
        switch connectionState {
        case .connected, .streaming: return FlightRiskTheme.matchGreen
        case .connecting: return FlightRiskTheme.alertOrange
        case .error: return FlightRiskTheme.alertRed
        case .disconnected: return .secondary
        }
    }

    private var connectionText: String {
        switch connectionState {
        case .disconnected: return "Disconnected"
        case .connecting: return "Connecting..."
        case .connected: return "Connected"
        case .streaming: return "Streaming"
        case .error: return "Error"
        }
    }

    private func batteryColor(for level: Int) -> Color {
        switch level {
        case 51...: return FlightRiskTheme.matchGreen
        case 21...50: return FlightRiskTheme.alertOrange
        default: return FlightRiskTheme.alertRed
        }
    }

    private func batteryIconName(for level: Int) -> String {
        switch level {
        case 76...: return "battery.100"
        case 51...75: return "battery.75"
        case 26...50: return "battery.50"
        case 1...25: return "battery.25"
        default: return "battery.0"
        }
    }
}

// MARK: - Cloud Active Indicator

/// A visible marker shown whenever cloud reasoning is the active backend.
/// Other views can show it by checking `ReasoningActivity.shared.isCloudActive`.
struct CloudActiveIndicator: View {
    var body: some View {
        Label("Cloud active: images are sent to Anthropic", systemImage: "icloud.and.arrow.up")
            .font(.footnote.weight(.semibold))
            .foregroundStyle(FlightRiskTheme.alertOrange)
            .accessibilityLabel("Cloud reasoning active. Person crops and the reference photo are sent to Anthropic.")
    }
}

/// Small banner for the Search screen: the "Cloud active" indicator while
/// cloud reasoning is active, and the fallback notice when the selected
/// provider could not be used.
struct ReasoningStatusBanner: View {
    var body: some View {
        let status = ReasoningActivity.shared.status
        if status.isCloudActive || status.notice != nil {
            VStack(alignment: .leading, spacing: 4) {
                if status.isCloudActive {
                    CloudActiveIndicator()
                }
                if let notice = status.notice {
                    Text(notice)
                        .font(.caption)
                        .foregroundStyle(.white)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 10))
            .padding(.horizontal, 16)
        }
    }
}

// MARK: - Sensitivity Pill Button

/// A single sensitivity preset rendered as a large tappable pill.
private struct SensitivityPillButton: View {
    let preset: SensitivityPreset
    let isActive: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            VStack(alignment: .leading, spacing: 4) {
                Text(preset.rawValue)
                    .font(.subheadline.bold())
                    .foregroundStyle(isActive ? FlightRiskTheme.matchGreen : .primary)

                Text(preset.description)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(isActive ? FlightRiskTheme.matchGreen.opacity(0.1) : Color.clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(
                        isActive ? FlightRiskTheme.matchGreen : Color.secondary.opacity(0.3),
                        lineWidth: isActive ? 2 : 1
                    )
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(preset.rawValue) preset")
        .accessibilityHint(preset.description)
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }
}

// MARK: - Threshold Slider Row

/// A labeled slider for a single threshold value (0.10 - 0.90, step 0.05).
private struct ThresholdSliderRow: View {
    let label: String
    let value: Float
    let onChanged: (Float) -> Void

    @State private var sliderValue: Float = 0.5

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label)
                    .font(.subheadline)
                Spacer()
                Text(String(format: "%.2f", sliderValue))
                    .font(.subheadline.monospacedDigit().bold())
                    .accessibilityLabel("\(label): \(String(format: "%.2f", sliderValue))")
            }

            Slider(
                value: $sliderValue,
                in: 0.1...0.9,
                step: 0.05
            ) {
                Text(label)
            } onEditingChanged: { editing in
                if !editing {
                    onChanged(sliderValue)
                }
            }
            .tint(FlightRiskTheme.detectionBlue)
        }
        .onAppear {
            sliderValue = value
        }
        .onChange(of: value) { _, newValue in
            sliderValue = newValue
        }
        .padding(.vertical, 4)
    }
}

// MARK: - Signal Status

/// Status indicator for a matching signal.
enum SignalStatus {
    /// Model available and signal enabled.
    case active
    /// Model available but signal disabled.
    case inactive
    /// Required model not bundled.
    case notInstalled

    var label: String {
        switch self {
        case .active: return "Active"
        case .inactive: return "Available"
        case .notInstalled: return "Not Installed"
        }
    }

    var color: Color {
        switch self {
        case .active: return FlightRiskTheme.matchGreen
        case .inactive: return .secondary
        case .notInstalled: return FlightRiskTheme.alertOrange
        }
    }

    var iconName: String {
        switch self {
        case .active: return "checkmark.circle.fill"
        case .inactive: return "circle"
        case .notInstalled: return "exclamationmark.triangle.fill"
        }
    }
}

// MARK: - Signal Config Row

/// A row for configuring a single matching signal: toggle, weight slider, status.
private struct SignalConfigRow: View {
    let signalName: String
    let displayName: String
    let isEnabled: Bool
    let weight: Float
    let status: SignalStatus
    let onEnabledChanged: (Bool) -> Void
    let onWeightChanged: (Float) -> Void

    @State private var localEnabled: Bool = true
    @State private var localWeight: Float = 0.5

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Toggle(isOn: $localEnabled) {
                    HStack(spacing: 8) {
                        Text(displayName)
                            .font(.subheadline)

                        Image(systemName: status.iconName)
                            .font(.caption)
                            .foregroundStyle(status.color)

                        Text(status.label)
                            .font(.caption2)
                            .foregroundStyle(status.color)
                    }
                }
                .toggleStyle(.switch)
                .tint(FlightRiskTheme.matchGreen)
                .disabled(status == .notInstalled)
                .onChange(of: localEnabled) { _, newValue in
                    onEnabledChanged(newValue)
                }
            }

            if localEnabled && status != .notInstalled {
                HStack {
                    Text("Weight")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Slider(
                        value: $localWeight,
                        in: 0.0...1.0,
                        step: 0.05
                    ) {
                        Text("Weight")
                    } onEditingChanged: { editing in
                        if !editing {
                            onWeightChanged(localWeight)
                        }
                    }
                    .tint(FlightRiskTheme.detectionBlue)

                    Text(String(format: "%.2f", localWeight))
                        .font(.caption.monospacedDigit().bold())
                        .frame(width: 36, alignment: .trailing)
                }
            }
        }
        .padding(.vertical, 4)
        .onAppear {
            localEnabled = isEnabled
            localWeight = weight
        }
        .onChange(of: isEnabled) { _, newValue in
            localEnabled = newValue
        }
        .onChange(of: weight) { _, newValue in
            localWeight = newValue
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(displayName): \(status.label), weight \(String(format: "%.2f", localWeight))")
    }
}

// MARK: - Preview

#Preview {
    NavigationStack {
        SettingsView(
            config: .shared,
            onPresetSelected: { _ in },
            onThresholdChanged: { _, _ in },
            onLlmBackendChanged: { _ in },
            onApiKeyChanged: { _ in },
            llmAvailable: true,
            droneState: TelloState(
                connectionState: .connected,
                telemetry: TelloTelemetry(battery: 72)
            ),
            frameSourceMode: .camera,
            modelManager: nil
        )
    }
}
