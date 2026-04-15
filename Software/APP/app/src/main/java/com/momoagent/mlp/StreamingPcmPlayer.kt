package com.momoagent.mlp

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Base64
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.max

class StreamingPcmPlayer {
    private val executor = Executors.newSingleThreadExecutor()
    private val generation = AtomicInteger(0)
    private var audioTrack: AudioTrack? = null
    private var currentSampleRate = 0

    fun playChunk(pcm16Base64: String, sampleRate: Int) {
        val chunk = try {
            Base64.decode(pcm16Base64, Base64.DEFAULT)
        } catch (_: IllegalArgumentException) {
            return
        }
        if (chunk.isEmpty() || sampleRate <= 0) {
            return
        }
        val currentGeneration = generation.get()
        executor.execute {
            if (currentGeneration != generation.get()) {
                return@execute
            }
            val track = ensureTrack(sampleRate, chunk.size)
            if (currentGeneration != generation.get()) {
                return@execute
            }
            track.write(chunk, 0, chunk.size)
        }
    }

    fun reset() {
        generation.incrementAndGet()
        executor.execute {
            releaseTrackLocked()
        }
    }

    fun close() {
        reset()
        executor.shutdown()
    }

    private fun ensureTrack(sampleRate: Int, chunkBytes: Int): AudioTrack {
        val existing = audioTrack
        if (
            existing != null &&
            existing.state == AudioTrack.STATE_INITIALIZED &&
            currentSampleRate == sampleRate
        ) {
            if (existing.playState != AudioTrack.PLAYSTATE_PLAYING) {
                existing.play()
            }
            return existing
        }

        releaseTrackLocked()
        val minBuffer = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = max(max(8192, minBuffer), chunkBytes * 4)
        val newTrack = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
            )
            .setBufferSizeInBytes(bufferSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        newTrack.play()
        audioTrack = newTrack
        currentSampleRate = sampleRate
        return newTrack
    }

    private fun releaseTrackLocked() {
        audioTrack?.let { track ->
            try {
                if (track.playState == AudioTrack.PLAYSTATE_PLAYING) {
                    track.pause()
                }
                track.flush()
            } catch (_: Exception) {
            }
            try {
                track.release()
            } catch (_: Exception) {
            }
        }
        audioTrack = null
        currentSampleRate = 0
    }
}
