import java.net.URI
import java.security.MessageDigest
import java.util.Properties

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

/**
 * The frontend, copied into the APK under `assets/web/`, and the licence texts
 * under `assets/licenses/`.
 *
 * Copied rather than pointing an asset srcDir at static/ directly: a bare
 * srcDir lands the files at the top of the asset tree, where they mix with
 * whatever the AARs contribute and the server's catch-all route would serve
 * those too. The `web/` prefix keeps what is reachable over HTTP to exactly
 * the frontend.
 */
val webAssetsRoot = layout.buildDirectory.dir("generated/webassets")
val syncWebAssets by tasks.registering(Sync::class) {
    into(webAssetsRoot)
    from(File(repoRoot, "static")) {
        exclude("**/README.md")
        into("web")
    }
    // GPL-3.0 §4: the licence must accompany the distributed work, and the APK
    // is the work as distributed (plan §1.8 Decision #32). Outside web/, so
    // the server does not expose them; dev/check-apk.py fails without them.
    from(files(File(repoRoot, "LICENSE"), File(repoRoot, "NOTICE"))) {
        into("licenses")
    }
}

/**
 * Release signing, from the environment and never from the repository.
 *
 * The key is the app's identity: Android installs an update only if it is
 * signed with the same key as the installed copy, so anyone holding it can
 * ship updates that phones accept as genuine, and losing it means no
 * existing install can ever be updated again. Hence: a keystore kept outside
 * the repository, located and unlocked by these variables -- locally from
 * the shell, in CI from repository secrets.
 *
 * Without them the release build still runs and produces an unsigned APK,
 * which Android will not install; dev/check-apk.py refuses it.
 */
val releaseKeystore: String? = System.getenv("MEDIAGRAB_KEYSTORE")

/**
 * yt-dlp, pinned by android/ytdlp.lock and fetched at build time.
 *
 * Generated as res/raw/ytdlp, the resource name the library installs from;
 * an application resource overrides a library's of the same name, so this
 * replaces the AAR's own -- which is whatever yt-dlp the library's release
 * happened to bundle (2025.11.12 at 0.18.1, by then 403ing on YouTube).
 *
 * The download is cached under .toolchain/ with the rest of the toolchain and
 * verified against the pinned SHA-256 on every build, so a tampered or
 * truncated file fails the build instead of shipping.
 */
val ytdlpLock = Properties().apply {
    rootProject.file("ytdlp.lock").inputStream().use { load(it) }
}
val ytdlpVersion: String = ytdlpLock.getProperty("version")
val ytdlpSha256: String = ytdlpLock.getProperty("sha256")
val ytdlpResRoot = layout.buildDirectory.dir("generated/ytdlp-res")
val fetchYtdlp by tasks.registering {
    val cache = File(repoRoot, ".toolchain/downloads/yt-dlp-$ytdlpVersion")
    val target = ytdlpResRoot.map { it.file("raw/ytdlp") }
    inputs.property("version", ytdlpVersion)
    inputs.property("sha256", ytdlpSha256)
    outputs.file(target)
    doLast {
        fun sha256(f: File) = MessageDigest.getInstance("SHA-256")
            .digest(f.readBytes()).joinToString("") { "%02x".format(it) }
        if (!cache.isFile || sha256(cache) != ytdlpSha256) {
            cache.parentFile.mkdirs()
            val url = "https://github.com/yt-dlp/yt-dlp/releases/download/$ytdlpVersion/yt-dlp"
            logger.lifecycle("MediaGrab: fetching yt-dlp $ytdlpVersion")
            val partial = File(cache.path + ".part")
            URI(url).toURL().openStream().use { input ->
                partial.outputStream().use { input.copyTo(it) }
            }
            val actual = sha256(partial)
            if (actual != ytdlpSha256) {
                partial.delete()
                throw GradleException(
                    "yt-dlp $ytdlpVersion checksum mismatch: expected $ytdlpSha256, got $actual. " +
                        "Refusing to bundle it; see android/ytdlp.lock."
                )
            }
            partial.renameTo(cache)
        }
        cache.copyTo(target.get().asFile, overwrite = true)
    }
}

android {
    namespace = "dev.mediagrab"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.mediagrab"
        minSdk = 26
        targetSdk = 35
        // Stamped by CI from the release tag (build-android.yml); local builds
        // keep the dev values. versionCode must only ever increase -- Android
        // refuses an update with a lower one -- so CI derives it from the
        // semver: major*10000 + minor*100 + patch.
        versionCode = (findProperty("mediagrab.versionCode") as String?)?.toInt() ?: 1
        versionName = (findProperty("mediagrab.versionName") as String?) ?: "0.0.0-dev"

        // arm64-v8a only. The AAR carries ~14MB of Python per ABI and the
        // plan's budget assumes one (§1.1 Decision #7).
        ndk { abiFilters += listOf("arm64-v8a") }

        // Lets the app tell when the on-device copy predates this build's.
        buildConfigField("String", "YTDLP_VERSION", "\"$ytdlpVersion\"")
    }

    buildFeatures { buildConfig = true }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("MEDIAGRAB_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("MEDIAGRAB_KEY_ALIAS") ?: "mediagrab"
                keyPassword = System.getenv("MEDIAGRAB_KEY_PASSWORD")
                    ?: System.getenv("MEDIAGRAB_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        getByName("release") {
            signingConfig = signingConfigs.findByName("release")
            // No R8: Ktor and kotlinx.serialization lean on reflection, and
            // shrinking them needs keep rules this project has not written or
            // tested. The size is in the native libraries, which R8 cannot
            // touch, so the saving would be small for real risk.
            isMinifyEnabled = false
        }
        getByName("debug") {
            // Debug also carries x86_64 so it runs natively on the emulator.
            // The x86_64 system images can translate arm64 apps, but not this
            // one: the library spawns python with LD_LIBRARY_PATH pointing at
            // its own arm64 libandroid.so, and the translation runner (an
            // x86_64 binary) inherits that and fails to link. Release builds
            // stay arm64-only, so the size budget is unaffected.
            ndk { abiFilters += listOf("x86_64") }
        }
    }

    packaging {
        // youtubedl-android unpacks libpython.zip.so and friends from the
        // app's native library directory, so they must exist there as files.
        // Since AGP 3.6 they stay compressed inside the APK by default and the
        // directory is empty: init fails with ENOENT on libpython.zip.so, found
        // on the first emulator run. Costs install size, not download size.
        jniLibs { useLegacyPackaging = true }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }

    sourceSets {
        getByName("main").java.srcDirs("src/main/kotlin")
        getByName("main").assets.srcDir(webAssetsRoot.get().asFile)
        getByName("main").res.srcDir(ytdlpResRoot.get().asFile)
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
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")

    // The media engine. Resolved from the composite build in settings.gradle.kts,
    // not from a repository -- JitPack cannot build these. See §1.8 Decision #29.
    implementation("io.github.junkfood02.youtubedl-android:library:0.18.1")
    implementation("io.github.junkfood02.youtubedl-android:ffmpeg:0.18.1")

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
tasks.named("preBuild") { dependsOn(syncWebAssets, fetchYtdlp) }
