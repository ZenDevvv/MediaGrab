package dev.mediagrab.contract

import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json

/** Serialises a DTO exactly as the server would, for shape assertions. */
object Probe {
    private val json = Json { explicitNulls = true; encodeDefaults = true }

    fun info(value: InfoResponse): String =
        json.encodeToString(InfoResponse.serializer(), value)
}
