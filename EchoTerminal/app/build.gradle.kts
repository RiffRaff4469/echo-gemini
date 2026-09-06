// Imported explicitly: inside the android/defaultConfig blocks the name `java`
// resolves to Gradle's own `java` extension, so a fully-qualified
// `java.util.Properties()` fails to compile with "Unresolved reference: util".
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "com.echogemini.terminal"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.echogemini.terminal"

        // LineageOS 18.1 == Android 11 == API 30. The device cannot run less,
        // and targeting higher than 33 buys nothing on a device that will never
        // see Play Store distribution.
        minSdk = 30
        targetSdk = 33

        versionCode = 1
        versionName = "0.1.0"

        // MT8163 with a 32-bit userspace. armeabi-v7a is the ONLY ABI that will
        // ever run here; anything else is dead weight in a 1 GB device.
        ndk { abiFilters += "armeabi-v7a" }

        // The server URL and shared secret are build-time config, not source.
        // Override per machine in EchoTerminal/local.properties (gitignored):
        //     echo.serverUrl=ws://your-pc.tail1234.ts.net:8765/ws
        //     echo.sharedSecret=<the same value as ECHO_SHARED_SECRET in .env>
        val localProps = Properties().apply {
            val f = rootProject.file("local.properties")
            if (f.exists()) f.inputStream().use { stream -> this.load(stream) }
        }
        buildConfigField(
            "String",
            "SERVER_URL",
            "\"${localProps.getProperty("echo.serverUrl", "ws://10.0.2.2:8765/ws")}\""
        )
        buildConfigField(
            "String",
            "SHARED_SECRET",
            "\"${localProps.getProperty("echo.sharedSecret", "")}\""
        )
    }

    buildTypes {
        release {
            // Left off deliberately: this APK is sideloaded onto one device and
            // read back through logcat when something misbehaves. Obfuscated
            // stack traces would cost more than the few hundred KB saved.
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            applicationIdSuffix = ".debug"
        }
    }

    // No Google Play Services, no Firebase, no GApps anywhere in this tree.
    // The device has none of it and 1 GB of RAM to spare none for it.

    buildFeatures {
        buildConfig = true
        // No view binding: the whole UI is built in code, so there are no
        // layout XML resources to bind against.
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions { unitTests.isIncludeAndroidResources = true }

    packaging {
        resources {
            excludes += setOf(
                "/META-INF/{AL2.0,LGPL2.1}",
                "/META-INF/*.kotlin_module",
                "DebugProbesKt.bin"
            )
        }
    }

    lint {
        // android.hardware.Camera is deprecated but is the right API for a
        // HAL1 device -- see CameraSource.kt for why. Do not "fix" this.
        disable += "UnsafeOptInUsageError"
        abortOnError = false
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.14.1")
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)
}
