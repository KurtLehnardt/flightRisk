package com.flightrisk.app.byo

/**
 * The words of the "Bring your own detector" setting, kept apart from the
 * screen so a unit test can check them.
 */
object ByoDetectorText {
    const val TITLE = "Bring your own detector"

    const val OFF_SUMMARY =
        "Off. FlightRisk finds people with its built-in YOLOX-S detector (Apache-2.0)."

    const val ON_SUMMARY = "On. Searches use the detector you imported. Turning this off deletes it and goes back to YOLOX-S."

    const val GATE_TITLE = "Use your own YOLO model?"

    /** The licence explanation shown before anything can be imported. */
    const val GATE_BODY =
        "You can import a person detector exported to ONNX, for example a YOLOv8 or YOLO11 model " +
            "from Ultralytics. FlightRisk does not include, supply, host or license any YOLO model.\n\n" +
            "Ultralytics licenses its YOLO models under AGPL-3.0, an open-source licence with conditions " +
            "on sharing source code, or under the Ultralytics Enterprise License, which it offers for " +
            "commercial products and services that do not meet the AGPL-3.0 conditions. Which licence " +
            "applies to you depends on who you are and how you use the model; this is not legal advice.\n\n" +
            "The model must be an .onnx file of at most 100 MB, with a 640 x 640 input and the 80 COCO " +
            "classes (person is class 0). It is copied into this app's private storage on this phone, is " +
            "not backed up and is never uploaded. Turning this setting off deletes it."

    const val LICENSE_URL = "https://www.ultralytics.com/license"
    const val LICENSE_LINK_LABEL = "Ultralytics licensing (ultralytics.com/license)"

    /** The statement the user must tick before choosing a file. */
    const val ATTESTATION = "I have the right to use this model. FlightRisk does not supply or license YOLO."

    const val CHOOSE_FILE = "Choose .onnx file"
    const val CANCEL = "Cancel"
    const val CHECKING = "Checking the model..."
    const val APPLIES_NEXT_SEARCH = "A change applies from the next search."

    fun imported(sizeBytes: Long, layout: String, sha256: String): String =
        "Imported (%.1f MB, %s output).\nSHA-256: %s".format(
            sizeBytes / 1_000_000.0,
            layout,
            ByoDetectorChecks.groupedHex(sha256),
        )

    fun failed(reason: String): String = "Not imported: $reason"
}
