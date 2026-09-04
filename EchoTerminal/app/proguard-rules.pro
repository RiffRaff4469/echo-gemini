# Minification is off for release builds (see app/build.gradle.kts): this APK is
# sideloaded onto exactly one device and debugged through logcat, so readable
# stack traces are worth more than the few hundred KB shrinking would save.
#
# These rules exist so that turning minification on later does not immediately
# break the WebSocket layer.

# OkHttp ships references to optional platform pieces that are absent here.
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# Kotlin coroutines' debug agent is excluded from packaging.
-dontwarn kotlinx.coroutines.debug.**

# The WebView push surface may be handed HTML that calls into a JS bridge in a
# later version. Keep any @JavascriptInterface members if one is ever added.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
