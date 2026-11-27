import Observation
import PersonVision
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// The text of the bring-your-own detector screens, kept together so the tests can check it.
enum CustomDetectorText {
    static let ultralyticsLicensingURL = URL(string: "https://www.ultralytics.com/license")!
    static let agplURL = URL(string: "https://www.gnu.org/licenses/agpl-3.0.html")!

    static let sectionFooter =
        "Off by default. The built-in detector is YOLOX-S (Apache-2.0)."

    static let intro =
        "FlightRisk finds people with its built-in detector, YOLOX-S (Apache-2.0). If you have your own YOLO person "
        + "detector, you can use it instead. FlightRisk does not provide, host or download any YOLO model, and the "
        + "model you import stays on this phone: it is not uploaded, shared or included in backups."

    static let licence =
        "YOLO models from Ultralytics (YOLOv8, YOLO11 and later) are licensed under the GNU AGPL-3.0, or under a "
        + "paid Ultralytics Enterprise licence. Using one in this app may need an Enterprise licence. You are "
        + "responsible for having the right to use the model you import."

    static let requirements =
        "A Core ML model: a .mlmodel file, or a zipped .mlpackage, 100 MB at most. Input 640 x 640. "
        + "Output a YOLOv8 or YOLO11 head without NMS ([1, 84, 8400]) or an NMS-free head ([1, N, 6]). "
        + "Class 0 must be \"person\". FlightRisk checks it on a test picture before use."

    static let attestation =
        "I confirm that I have the right to use this model in FlightRisk, under the AGPL-3.0, an Ultralytics "
        + "Enterprise licence or the licence of the model I import."
}

/// State of the bring-your-own detector for the Settings screens.
@MainActor @Observable
final class CustomDetectorSettingsModel {
    private let store: CustomDetectorStore

    var info: CustomDetectorInfo?
    var isEnabled = false
    var isImporting = false
    var errorMessage: String?

    init(store: CustomDetectorStore = .shared) {
        self.store = store
    }

    func refresh() {
        info = store.info
        isEnabled = store.isEnabled
    }

    func setEnabled(_ on: Bool) {
        store.isEnabled = on
        refresh()
    }

    func remove() {
        store.remove()
        refresh()
    }

    /// Imports the picked file; on failure `errorMessage` says why, in words for the user.
    func importModel(from url: URL, attested: Bool) async -> Bool {
        isImporting = true
        errorMessage = nil
        defer {
            // The picker copied the file into the app's temporary folder; it is not kept.
            try? FileManager.default.removeItem(at: url)
            isImporting = false
            refresh()
        }
        do {
            try await store.importModel(from: url, attested: attested)
            return true
        } catch let error as CustomModelError {
            errorMessage = error.message
        } catch {
            errorMessage = CustomModelError.loadFailed.message
        }
        return false
    }
}

/// The Settings section for the optional bring-your-own detector.
struct CustomDetectorSection: View {
    /// The model's init is cheap; it reads the disk in `refresh()`, which `.task` calls. The
    /// section therefore always renders, including its off switch.
    @State private var model = CustomDetectorSettingsModel()

    var body: some View {
        CustomDetectorSectionContent(model: model)
            .task { model.refresh() }
    }
}

private struct CustomDetectorSectionContent: View {
    let model: CustomDetectorSettingsModel
    @State private var showingImport = false
    @State private var confirmingRemove = false

    var body: some View {
        Section {
            if let info = model.info {
                Toggle("Use my detector", isOn: Binding(get: { model.isEnabled }, set: { model.setEnabled($0) }))
                    .accessibilityHint("Off uses the built-in detector")
                LabeledContent("Model", value: info.layout.displayName)
                LabeledContent("Size", value: CustomModelValidator.megabytes(info.bytes))
                VStack(alignment: .leading, spacing: 2) {
                    Text("SHA-256").font(.footnote)
                    Text(info.sha256)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                .accessibilityElement(children: .combine)
                Button("Replace model") { showingImport = true }
                Button("Remove model", role: .destructive) { confirmingRemove = true }
            } else {
                Text("Built-in detector (YOLOX-S).")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Button("Import detector…") { showingImport = true }
                    .accessibilityIdentifier("settings.customDetector.import")
            }
        } header: {
            Text("Detector")
        } footer: {
            Text(CustomDetectorText.sectionFooter)
        }
        .sheet(isPresented: $showingImport, onDismiss: { model.refresh() }) {
            CustomDetectorImportView(model: model)
        }
        .alert("Remove detector?", isPresented: $confirmingRemove) {
            Button("Remove", role: .destructive) { model.remove() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("FlightRisk goes back to the built-in detector.")
        }
    }
}

/// The gate screen: what the licence means, what the model must be, the user's statement, then
/// the file picker.
struct CustomDetectorImportView: View {
    let model: CustomDetectorSettingsModel
    @Environment(\.dismiss) private var dismiss
    @State private var attested = false
    @State private var picking = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(CustomDetectorText.intro)
                }
                Section {
                    Text(CustomDetectorText.licence)
                    Link("Ultralytics licensing (AGPL-3.0 or Enterprise)", destination: CustomDetectorText.ultralyticsLicensingURL)
                    Link("Read the GNU AGPL-3.0", destination: CustomDetectorText.agplURL)
                } header: {
                    Text("Licence")
                }
                Section {
                    Text(CustomDetectorText.requirements)
                        .font(.footnote)
                } header: {
                    Text("Requirements")
                }
                Section {
                    Toggle(isOn: $attested) {
                        Text(CustomDetectorText.attestation)
                            .font(.footnote)
                    }
                    .accessibilityIdentifier("customDetector.attest")
                    Button("Choose file…") { picking = true }
                        .disabled(!attested || model.isImporting)
                        .accessibilityIdentifier("customDetector.choose")
                    if model.isImporting {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("Checking…").font(.footnote)
                        }
                        .accessibilityElement(children: .combine)
                    }
                    if let error = model.errorMessage {
                        Text(error)
                            .font(.footnote)
                            .foregroundStyle(FlightRiskTheme.alertRed)
                    }
                }
            }
            .navigationTitle("Own detector")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(model.isImporting)
                }
            }
            .sheet(isPresented: $picking) {
                ModelFilePicker { url in
                    picking = false
                    guard let url else { return }
                    Task {
                        if await model.importModel(from: url, attested: attested) { dismiss() }
                    }
                }
                .ignoresSafeArea()
            }
            .interactiveDismissDisabled(model.isImporting)
        }
    }
}

/// `UIDocumentPickerViewController` for a zipped `.mlpackage` or a `.mlmodel`. The file is
/// copied into the app's temporary folder (`asCopy`), so no security scope is kept.
struct ModelFilePicker: UIViewControllerRepresentable {
    let onPick: (URL?) -> Void

    static var contentTypes: [UTType] {
        var types: [UTType] = [.zip]
        if let mlmodel = UTType("com.apple.coreml.model") ?? UTType(filenameExtension: "mlmodel") {
            types.append(mlmodel)
        }
        return types
    }

    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: Self.contentTypes, asCopy: true)
        picker.allowsMultipleSelection = false
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ controller: UIDocumentPickerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onPick: onPick) }

    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let onPick: (URL?) -> Void
        init(onPick: @escaping (URL?) -> Void) { self.onPick = onPick }

        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            onPick(urls.first)
        }

        func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
            onPick(nil)
        }
    }
}

/// The licences of the on-device models (`model-notices.md`, which `make_models.py install`
/// copies next to the models), shown in full.
struct ModelNoticesView: View {
    @Environment(\.dismiss) private var dismiss
    let notices: String?

    init(notices: String? = BundledModels.notices()) {
        self.notices = notices
    }

    static let missingText = "This build of FlightRisk includes no on-device models, so it has no model notices."

    var body: some View {
        NavigationStack {
            ScrollView {
                Text(notices ?? Self.missingText)
                    .font(.footnote.monospaced())
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
            }
            .navigationTitle("Model licences")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
