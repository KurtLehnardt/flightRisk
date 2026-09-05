# ProGuard rules for FlightRisk

# ONNX Runtime - keep JNI and model loading classes
-keep class ai.onnxruntime.** { *; }
-keepclassmembers class ai.onnxruntime.** { *; }
-dontwarn ai.onnxruntime.**

# Keep native methods
-keepclasseswithmembernames class * {
    native <methods>;
}

# CameraX
-keep class androidx.camera.** { *; }

# Coroutines
-keepnames class kotlinx.coroutines.internal.MainDispatcherFactory {}
-keepnames class kotlinx.coroutines.CoroutineExceptionHandler {}
-keepclassmembers class kotlinx.coroutines.** {
    volatile <fields>;
}
