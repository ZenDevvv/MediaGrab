import java.io.File

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        maven("https://jitpack.io")
    }
}

rootProject.name = "MediaGrab"
include(":app")

/**
 * youtubedl-android, consumed as a composite build.
 *
 * Not a normal dependency. JitPack cannot build this library at all: it
 * requests a JDK 17 toolchain and JitPack's image has none, so every version
 * 404s (MULTI_PLATFORM_IMPLEMENTATION_PLAN.md §1.8 Decision #29). Against a
 * local JDK 17 the same tag builds in about two and a half minutes.
 *
 * So the source is fetched here, pinned to a tag, and included as a build.
 * The checkout lives under .toolchain/ alongside the rest of the toolchain so
 * it is gitignored and cleaned up with everything else.
 *
 * Pinned by tag rather than branch: the build must not start producing
 * different binaries from the same commit of this repository.
 */
val youtubedlVersion = "0.18.1"
val youtubedlUrl = "https://github.com/yausername/youtubedl-android.git"
val checkout = File(rootDir, "../.toolchain/youtubedl-android")

if (!File(checkout, ".git").isDirectory) {
    logger.lifecycle("MediaGrab: fetching youtubedl-android $youtubedlVersion")
    checkout.parentFile.mkdirs()
    val result = exec {
        workingDir = checkout.parentFile
        commandLine(
            "git", "clone", "--depth", "1",
            // Pinned. A floating ref would make the APK non-reproducible and
            // would let an upstream change break the build silently.
            "--branch", youtubedlVersion,
            youtubedlUrl, checkout.name,
        )
    }
    if (result.exitValue != 0) {
        throw GradleException(
            "Could not clone $youtubedlUrl at $youtubedlVersion. " +
                "MediaGrab needs it as a composite build because JitPack " +
                "cannot build it; see the plan §1.8 Decision #29."
        )
    }
}

includeBuild(checkout) {
    // The published group is still the project's old name; these are the
    // coordinates substitution has to intercept.
    dependencySubstitution {
        substitute(module("io.github.junkfood02.youtubedl-android:library"))
            .using(project(":library"))
        substitute(module("io.github.junkfood02.youtubedl-android:ffmpeg"))
            .using(project(":ffmpeg"))
        substitute(module("io.github.junkfood02.youtubedl-android:common"))
            .using(project(":common"))
    }
}