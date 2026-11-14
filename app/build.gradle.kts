plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.stellai.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.stellai.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 43
        versionName = "0.29.0"

        // ONNX Runtime (wake word) ships native code; phones only need ARM.
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
    }

    // Release signing: GitHub Actions decodes the SIGNING_KEY_BASE64 secret to a file and passes its path and
    // passwords here (see .github/workflows/build.yml). Without them, only the debug APK can be built.
    val keystore = System.getenv("SIGNING_KEYSTORE_FILE")
    signingConfigs {
        if (!keystore.isNullOrBlank()) {
            create("release") {
                storeFile = file(keystore)
                storePassword = System.getenv("SIGNING_STORE_PASSWORD")
                keyAlias = System.getenv("SIGNING_KEY_ALIAS")
                keyPassword = System.getenv("SIGNING_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (!keystore.isNullOrBlank()) signingConfig = signingConfigs.getByName("release")
        }
    }

    // Unit tests (app/src/test) run on GitHub before every build; android.* calls in them return defaults
    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")
    // Line icons used throughout the app instead of emoji (unused ones are stripped from release builds)
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.20.0")
    implementation("androidx.exifinterface:exifinterface:1.3.7")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // QR scanner from Google Play services: no camera permission needed, the scanner UI is Google's
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")

    // Unit tests, run by GitHub Actions before every build (a failure stops the release)
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
