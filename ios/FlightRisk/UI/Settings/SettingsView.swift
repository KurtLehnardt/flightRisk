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
    @State private var llmBackend: String = "cloud_claude"
    @State private var advancedExpanded: Bool = false
    @State private var showApiKeySaved: Bool = false
    @State private var gemmaState: GemmaDownloadState = .idle
    @State private var showDeleteConfirmation: Bool = false

    // MARK: - Derived State

    private var activePreset: SensitivityPreset? {
        SensitivityPreset(rawValue: activePresetRaw)
    }

    // MARK: - Body

    var body: some View {
        Form {
            sensitivitySection
            llmSection
            droneSection
            advancedSection
            aboutSection
        }
        .navigationTitle("Settings")
        .onAppear {
            apiKeyInput = KeychainHelper.loadApiKey() ?? ""
        }
        .alert("Delete AI Model?", isPresented: $showDeleteConfirmation) {
            Button("Delete", role: .destructive) {
                Task { try? await modelManager?.deleteModel() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This will free ~1.5 GB. You'll need WiFi to re-download.")
        }
        .task(id: llmBackend) {
            guard llmBackend == "local_gemma", let manager = modelManager else { return }
            gemmaState = await manager.state
            // Poll state periodically during download
            while !Task.isCancelled && llmBackend == "local_gemma" {
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

    // MARK: - Section 2: LLM Backend

    private var llmSection: some View {
        Section {
            Picker("Backend", selection: $llmBackend) {
                Text("Cloud Claude").tag("cloud_claude")
                Text("Local Gemma").tag("local_gemma")
                Text("None").tag("none")
            }
            .onChange(of: llmBackend) { _, newValue in
                onLlmBackendChanged(newValue)
            }

            if llmBackend == "cloud_claude" {
                SecureField("sk-ant-...", text: $apiKeyInput)
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
                .disabled(apiKeyInput.isEmpty)

                HStack(spacing: 8) {
                    Circle()
                        .fill(llmStatusColor)
                        .frame(width: 8, height: 8)
                    Text(llmStatusText)
                        .font(.footnote)
                        .foregroundStyle(llmStatusColor)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("LLM status: \(llmStatusText)")
            } else if llmBackend == "local_gemma" {
                Text("Runs on-device. No API key required.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Local Gemma model runs on-device. No API key required.")

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
            } else {
                Text("LLM reasoning disabled")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("AI Reasoning")
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
        do {
            try KeychainHelper.saveApiKey(apiKeyInput)
            onApiKeyChanged(apiKeyInput)
            showApiKeySaved = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                showApiKeySaved = false
            }
        } catch {
            // Silently fail; the status dot will show unavailable
        }
    }

    private func clearPreset() {
        activePresetRaw = ""
    }

    private var appVersion: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        return "\(version) (\(build))"
    }

    // LLM status helpers
    private var llmStatusColor: Color {
        if llmAvailable {
            return FlightRiskTheme.matchGreen
        } else {
            return FlightRiskTheme.alertRed
        }
    }

    private var llmStatusText: String {
        if llmAvailable {
            return "Available"
        } else if apiKeyInput.isEmpty {
            return "No API key"
        } else {
            return "No internet"
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
