package com.momoagent.mlp

import org.json.JSONObject
import java.net.URI

data class HaiGuiTangSceneState(
    val version: Int = 0,
    val clip: String = "default",
    val subtitleText: String = "",
    val videoUrl: String = "",
    val loopPlayback: Boolean = true,
    val defaultVideoUrl: String = "",
) {
    fun normalizedClip(): String {
        return when (clip.trim().lowercase()) {
            "intro", "default", "nod", "shake", "outro" -> clip.trim().lowercase()
            else -> "default"
        }
    }

    private fun resolveUrl(rawUrl: String, baseUrl: String): String {
        val raw = rawUrl.trim()
        if (raw.isBlank()) {
            return ""
        }
        return try {
            URI(baseUrl).resolve(raw).toString()
        } catch (_: Exception) {
            raw
        }
    }

    fun resolvedVideoUrl(baseUrl: String): String = resolveUrl(videoUrl, baseUrl)

    fun resolvedDefaultVideoUrl(baseUrl: String): String = resolveUrl(defaultVideoUrl, baseUrl)

    companion object {
        fun fromApiEnvelope(root: JSONObject?): HaiGuiTangSceneState {
            val payload = root?.optJSONObject("data") ?: return HaiGuiTangSceneState()
            return fromJson(payload)
        }

        fun fromJson(payload: JSONObject?): HaiGuiTangSceneState {
            if (payload == null) {
                return HaiGuiTangSceneState()
            }
            val clip = payload.optString("clip").ifBlank { "default" }
            val defaultLoop = clip.trim().lowercase() == "default"
            return HaiGuiTangSceneState(
                version = payload.optInt("version", 0),
                clip = clip,
                subtitleText = payload.optString("subtitle_text").trim(),
                videoUrl = payload.optString("video_url").trim(),
                loopPlayback = payload.optBoolean("loop_playback", defaultLoop),
                defaultVideoUrl = payload.optString("default_video_url").trim(),
            )
        }
    }
}
