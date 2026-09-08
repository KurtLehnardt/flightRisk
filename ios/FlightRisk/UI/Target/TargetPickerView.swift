import SwiftUI
import PhotosUI

/// Target photo selection view.
///
/// Allows the user to pick a photo from the gallery or take one with
/// the camera. The selected photo is analyzed for quality using
/// ``ImageQualityScorer`` and a quality report with grade badge and
/// issue chips is displayed via ``PhotoPreviewView``.
///
/// The "Use This Photo" button is enabled only when the quality grade
/// is C or above (``QualityReport/isAcceptable``).
///
/// Uses `PhotosPicker` (iOS 16+) for gallery selection and wraps
/// `UIImagePickerController` for camera capture.
struct TargetPickerView: View {
    /// Selected `PhotosPickerItem` from the gallery picker.
    @State private var selectedItem: PhotosPickerItem?
    /// The decoded `CGImage` of the selected photo.
    @State private var selectedImage: CGImage?
    /// Quality analysis result for the selected photo.
    @State private var qualityReport: QualityReport?
    /// Whether quality analysis is in progress.
    @State private var isAnalyzing = false
    /// Whether the camera sheet is presented.
    @State private var showCamera = false

    /// Callback when a photo is confirmed for use.
    /// Receives the `CGImage` and its ``QualityReport``.
    let onPhotoSelected: (CGImage, QualityReport) -> Void

    var body: some View {
        ScrollView {
            VStack(spacing: 24) {
                header
                selectionButtons
                photoContent
            }
            .padding(24)
        }
    }

    // MARK: - Header

    @ViewBuilder
    private var header: some View {
        VStack(spacing: 8) {
            Text("Select Target Photo")
                .font(.title2)
                .fontWeight(.bold)
                .accessibilityAddTraits(.isHeader)

            Text("Choose a clear, recent photo of the child you are searching for.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - Selection Buttons

    @ViewBuilder
    private var selectionButtons: some View {
        HStack(spacing: 12) {
            PhotosPicker(
                selection: $selectedItem,
                matching: .images,
                photoLibrary: .shared()
            ) {
                Label("Select Photo", systemImage: "photo.on.rectangle")
                    .frame(minHeight: 44)
            }
            .buttonStyle(.borderedProminent)
            .onChange(of: selectedItem) { _, newItem in
                Task { await loadImage(from: newItem) }
            }

            Button {
                showCamera = true
            } label: {
                Label("Take Photo", systemImage: "camera")
                    .frame(minHeight: 44)
            }
            .buttonStyle(.bordered)
            .sheet(isPresented: $showCamera) {
                CameraCaptureView { cgImage in
                    processImage(cgImage)
                }
            }
        }
    }

    // MARK: - Photo Content Area

    @ViewBuilder
    private var photoContent: some View {
        if isAnalyzing {
            ProgressView("Analyzing photo quality...")
                .padding(.top, 32)
        } else if let image = selectedImage {
            PhotoPreviewView(
                image: image,
                qualityReport: qualityReport
            )

            Spacer().frame(height: 0) // layout separator

            usePhotoButton
        } else {
            emptyState
        }
    }

    // MARK: - Empty State

    @ViewBuilder
    private var emptyState: some View {
        RoundedRectangle(cornerRadius: 16)
            .fill(Color(.secondarySystemBackground))
            .frame(width: 240, height: 240)
            .overlay(
                VStack(spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.plus")
                        .font(.system(size: 48))
                        .foregroundStyle(.secondary)
                    Text("No photo selected")
                        .font(.body)
                        .foregroundStyle(.secondary)
                }
            )
            .accessibilityLabel("No photo selected")
    }

    // MARK: - Use This Photo Button

    @ViewBuilder
    private var usePhotoButton: some View {
        let acceptable = qualityReport?.isAcceptable == true

        Button {
            guard let image = selectedImage,
                  let report = qualityReport else { return }
            onPhotoSelected(image, report)
        } label: {
            Text("Use This Photo")
                .font(.headline)
                .fontWeight(.bold)
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity, minHeight: 56)
                .background(
                    acceptable
                        ? FlightRiskTheme.matchGreen
                        : Color.gray
                )
                .clipShape(RoundedRectangle(cornerRadius: 12))
        }
        .disabled(!acceptable)
        .accessibilityLabel(
            acceptable
                ? "Use this photo"
                : "Photo quality too low, select a better photo"
        )

        if !acceptable {
            Text("Photo quality is too low. Please select a better photo.")
                .font(.caption)
                .foregroundStyle(FlightRiskTheme.alertRed)
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - Image Loading

    /// Load and decode a `CGImage` from a `PhotosPickerItem`.
    private func loadImage(from item: PhotosPickerItem?) async {
        guard let item else { return }
        isAnalyzing = true
        defer { isAnalyzing = false }

        guard let data = try? await item.loadTransferable(type: Data.self),
              let uiImage = UIImage(data: data),
              let cgImage = normalizedCGImage(from: uiImage) else {
            return
        }

        processImage(cgImage)
    }

    /// Re-draw the image into a bitmap with `.up` orientation so that
    /// Vision's face detector sees correctly-oriented pixels.
    private func normalizedCGImage(from uiImage: UIImage) -> CGImage? {
        if uiImage.imageOrientation == .up {
            return uiImage.cgImage
        }
        let renderer = UIGraphicsImageRenderer(size: uiImage.size)
        let normalized = renderer.image { _ in
            uiImage.draw(in: CGRect(origin: .zero, size: uiImage.size))
        }
        return normalized.cgImage
    }

    /// Analyze a `CGImage` and update state.
    private func processImage(_ cgImage: CGImage) {
        isAnalyzing = true
        let report = ImageQualityScorer.analyze(image: cgImage)
        selectedImage = cgImage
        qualityReport = report
        isAnalyzing = false
    }
}

// MARK: - CameraCaptureView

/// UIKit wrapper for `UIImagePickerController` camera capture.
///
/// SwiftUI does not provide a native camera picker, so we wrap the
/// UIKit controller. The captured image is returned as a `CGImage`.
struct CameraCaptureView: UIViewControllerRepresentable {
    @Environment(\.dismiss) private var dismiss

    /// Callback with the captured `CGImage`.
    let onCapture: (CGImage) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(
        _ uiViewController: UIImagePickerController,
        context: Context
    ) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(onCapture: onCapture, dismiss: dismiss)
    }

    final class Coordinator: NSObject,
        UIImagePickerControllerDelegate,
        UINavigationControllerDelegate
    {
        let onCapture: (CGImage) -> Void
        let dismiss: DismissAction

        init(onCapture: @escaping (CGImage) -> Void, dismiss: DismissAction) {
            self.onCapture = onCapture
            self.dismiss = dismiss
        }

        func imagePickerController(
            _ picker: UIImagePickerController,
            didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
        ) {
            if let uiImage = info[.originalImage] as? UIImage {
                let cgImage: CGImage?
                if uiImage.imageOrientation == .up {
                    cgImage = uiImage.cgImage
                } else {
                    let renderer = UIGraphicsImageRenderer(size: uiImage.size)
                    let normalized = renderer.image { _ in
                        uiImage.draw(in: CGRect(origin: .zero, size: uiImage.size))
                    }
                    cgImage = normalized.cgImage
                }
                if let cg = cgImage {
                    onCapture(cg)
                }
            }
            dismiss()
        }

        func imagePickerControllerDidCancel(
            _ picker: UIImagePickerController
        ) {
            dismiss()
        }
    }
}
