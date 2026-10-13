plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization") version "2.0.21"
}

/**
 * The repository root. `rootProject` is android/, which is a subdirectory, so
 * every path to a shared artefact has to go up one level — the contract and the
 * frontend are both shared with the Python half of the project.
 *
 * Read directly by the tests rather than copied onto the classpath. Copying
 * means wiring a Copy task into AGP's internal ProcessJavaResTask, and the
 * Android build always sits inside this repository anyway, so the extra
 * machinery bought nothing.
 */
val repoRoot = rootProject.layout.projectDirectory.dir("..").asFile

android {
    namespace = "dev.mediagrab"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.mediagrab"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.0.0-dev"

        // arm64-v8a only. The AAR carries ~14MB of Python per ABI and the
        // plan's budget assumes one (§1.1 Decision #7).
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }

    sourceSets {
        getByName("main").java.srcDirs("src/main/kotlin")
        getByName("test").java.srcDirs("src/test/kotlin")
    }

    testOptions {
        unitTests {
            // The Ktor server is exercised over real loopback HTTP in src/test.
            // Returning 127.0.0.1 from Android's network stack is a stub that
            // throws unless this is set, and the alternative -- an instrumented
            // test -- would mean an emulator for every assertion.
            isReturnDefaultValues = true
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")

    implementation("io.ktor:ktor-server-core-jvm:3.0.3")
    implementation("io.ktor:ktor-server-cio-jvm:3.0.3")
    implementation("io.ktor:ktor-server-content-negotiation-jvm:3.0.3")
    implementation("io.ktor:ktor-serialization-kotlinx-json-jvm:3.0.3")
    implementation("io.ktor:ktor-server-sse-jvm:3.0.3")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    testImplementation("io.ktor:ktor-server-test-host-jvm:3.0.3")
    testImplementation("io.ktor:ktor-client-content-negotiation-jvm:3.0.3")
    testImplementation("io.ktor:ktor-client-cio-jvm:3.0.3")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

/**
 * The contract test serves the real frontend from the repository's static/
 * tree rather than fixtures, so the asset routes are exercised against what
 * will actually ship inside the APK. Both paths point at the repository so the
 * Kotlin assertions run against the same artefacts the Python tests do.
 */
tasks.withType<Test>().configureEach {
    systemProperty("mediagrab.webRoot", File(repoRoot, "static").absolutePath)
    systemProperty("mediagrab.contract", File(repoRoot, "openapi-contract.json").absolutePath)
    testLogging {
        events("passed", "failed", "skipped")
        showStandardStreams = false
    }
}