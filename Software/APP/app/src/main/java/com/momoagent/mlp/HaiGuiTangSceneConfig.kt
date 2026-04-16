package com.momoagent.mlp

import org.json.JSONObject
import java.net.URI

data class HaiGuiTangSceneConfig(
    val sceneId: String = "haiguitang",
    val title: String = "海龟汤",
    val subtitle: String = "片头结束后进入互动模式",
    val introVideoUrl: String = "",
    val defaultVideoUrl: String = "",
    val nodVideoUrl: String = "",
    val shakeVideoUrl: String = "",
    val outroVideoUrl: String = "",
    val introVideoAutoPlay: Boolean = true,
    val introVideoSkipable: Boolean = true,
    val introVideoTimeoutSec: Double = 8.0,
    val defaultStatusText: String = "片头结束后会进入全屏角色表情，等待 agent 通过 POST 切换表情，也可以手动点头和摇头调试动作。",
    val placeholderTitle: String = "片头占位",
    val placeholderBody: String = "当前还没有找到 begin.mp4 或 default.mp4，后续把视频放到 runtime/media 里就会自动识别。",
    val mediaFilePath: String = "",
    val mediaRoutePath: String = "",
    val mediaDirectoryPath: String = "",
) {
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

    fun resolvedIntroVideoUrl(baseUrl: String): String = resolveUrl(introVideoUrl, baseUrl)

    fun resolvedDefaultVideoUrl(baseUrl: String): String = resolveUrl(defaultVideoUrl, baseUrl)

    fun resolvedNodVideoUrl(baseUrl: String): String = resolveUrl(nodVideoUrl, baseUrl)

    fun resolvedShakeVideoUrl(baseUrl: String): String = resolveUrl(shakeVideoUrl, baseUrl)

    fun resolvedOutroVideoUrl(baseUrl: String): String = resolveUrl(outroVideoUrl, baseUrl)

    companion object {
        fun fromApiEnvelope(root: JSONObject?): HaiGuiTangSceneConfig {
            val payload = root?.optJSONObject("data") ?: return HaiGuiTangSceneConfig()
            return fromJson(payload)
        }

        fun fromJson(payload: JSONObject?): HaiGuiTangSceneConfig {
            if (payload == null) {
                return HaiGuiTangSceneConfig()
            }
            return HaiGuiTangSceneConfig(
                sceneId = payload.optString("scene_id").ifBlank { "haiguitang" },
                title = payload.optString("title").ifBlank { "海龟汤" },
                subtitle = payload.optString("subtitle").ifBlank { "片头结束后进入互动模式" },
                introVideoUrl = payload.optString("intro_video_url").trim(),
                defaultVideoUrl = payload.optString("default_video_url").trim(),
                nodVideoUrl = payload.optString("nod_video_url").trim(),
                shakeVideoUrl = payload.optString("shake_video_url").trim(),
                outroVideoUrl = payload.optString("outro_video_url").trim(),
                introVideoAutoPlay = payload.optBoolean("intro_video_auto_play", true),
                introVideoSkipable = payload.optBoolean("intro_video_skipable", true),
                introVideoTimeoutSec = payload.optDouble("intro_video_timeout_sec", 8.0),
                defaultStatusText = payload.optString("default_status_text").ifBlank {
                    "片头结束后会进入全屏角色表情，等待 agent 通过 POST 切换表情，也可以手动点头和摇头调试动作。"
                },
                placeholderTitle = payload.optString("placeholder_title").ifBlank { "片头占位" },
                placeholderBody = payload.optString("placeholder_body").ifBlank {
                    "当前还没有找到 begin.mp4 或 default.mp4，后续把视频放到 runtime/media 里就会自动识别。"
                },
                mediaFilePath = payload.optString("media_file_path").trim(),
                mediaRoutePath = payload.optString("media_route_path").trim(),
                mediaDirectoryPath = payload.optString("media_directory_path").trim(),
            )
        }
    }
}
