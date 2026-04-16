package com.momoagent.mlp

import org.json.JSONObject
import java.net.URI

data class HaiGuiTangSceneConfig(
    val sceneId: String = "haiguitang",
    val title: String = "海龟汤",
    val subtitle: String = "片头结束后进入互动模式",
    val introVideoUrl: String = "",
    val introVideoAutoPlay: Boolean = true,
    val introVideoSkipable: Boolean = true,
    val introVideoTimeoutSec: Double = 8.0,
    val defaultStatusText: String = "进入互动区后，可以先用点头和摇头调试动作效果。",
    val placeholderTitle: String = "片头占位",
    val placeholderBody: String = "当前还没有正式视频素材，后续把 mp4 放到固定路径或改配置 URL 就能替换。",
    val mediaFilePath: String = "",
    val mediaRoutePath: String = "",
) {
    fun resolvedIntroVideoUrl(baseUrl: String): String {
        val raw = introVideoUrl.trim()
        if (raw.isBlank()) {
            return ""
        }
        return try {
            URI(baseUrl).resolve(raw).toString()
        } catch (_: Exception) {
            raw
        }
    }

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
                introVideoAutoPlay = payload.optBoolean("intro_video_auto_play", true),
                introVideoSkipable = payload.optBoolean("intro_video_skipable", true),
                introVideoTimeoutSec = payload.optDouble("intro_video_timeout_sec", 8.0),
                defaultStatusText = payload.optString("default_status_text").ifBlank {
                    "进入互动区后，可以先用点头和摇头调试动作效果。"
                },
                placeholderTitle = payload.optString("placeholder_title").ifBlank { "片头占位" },
                placeholderBody = payload.optString("placeholder_body").ifBlank {
                    "当前还没有正式视频素材，后续把 mp4 放到固定路径或改配置 URL 就能替换。"
                },
                mediaFilePath = payload.optString("media_file_path").trim(),
                mediaRoutePath = payload.optString("media_route_path").trim(),
            )
        }
    }
}
