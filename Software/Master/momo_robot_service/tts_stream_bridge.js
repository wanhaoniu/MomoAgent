#!/usr/bin/env node
"use strict";

const fs = require("fs");

function writeEvent(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

function readRequest() {
  const raw = String(fs.readFileSync(0, "utf8") || "").trim();
  if (!raw) {
    throw new Error("Remote TTS bridge received empty stdin payload");
  }
  const payload = JSON.parse(raw);
  if (!payload || payload.op !== "speak_text_stream") {
    throw new Error(`Unsupported remote TTS bridge op: ${String(payload && payload.op)}`);
  }
  return payload;
}

function deriveWsUrl(baseUrl) {
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws";
  url.search = "";
  url.hash = "";
  return url.toString();
}

function finishPayload(state, finishReason) {
  const spokenText =
    String(state.llmText || "").trim() ||
    state.ttsSegments.map((item) => String(item || "")).filter(Boolean).join("") ||
    String(state.inputText || "").trim();
  return {
    type: "done",
    session_id: state.sessionId,
    spoken_text: spokenText,
    sample_rate: state.sampleRate,
    audio_chunks: state.audioChunks,
    audio_bytes: state.audioBytes,
    finish_reason: finishReason,
    elapsed_sec: Number(((Date.now() - state.startedAt) / 1000).toFixed(3)),
  };
}

async function runStream(request) {
  const baseUrl = String(request.base_url || request.baseUrl || "").trim();
  const model = String(request.model || "").trim();
  const systemPrompt = String(request.system_prompt || request.systemPrompt || "").trim();
  const inputText = String(request.input || "").trim();
  const timeoutSec = Math.max(3, Number(request.timeout_sec || request.timeoutSec || 30));

  if (!baseUrl) {
    throw new Error("Remote TTS base_url is empty");
  }
  if (!model) {
    throw new Error("Remote TTS model is empty");
  }
  if (!inputText) {
    throw new Error("Remote TTS input is empty");
  }

  await new Promise((resolve, reject) => {
    const ws = new WebSocket(deriveWsUrl(baseUrl));
    const state = {
      startedAt: Date.now(),
      sessionId: "",
      inputText,
      llmText: "",
      ttsSegments: [],
      sampleRate: 0,
      audioChunks: 0,
      audioBytes: 0,
      gotAudio: false,
      finished: false,
    };

    const timeoutHandle = setTimeout(() => {
      if (state.finished) {
        return;
      }
      if (state.gotAudio) {
        state.finished = true;
        writeEvent(finishPayload(state, "timeout_after_audio"));
        try {
          ws.close();
        } catch (_err) {}
        resolve();
        return;
      }
      reject(new Error(`Remote TTS bridge timed out after ${timeoutSec.toFixed(1)}s`));
    }, Math.round(timeoutSec * 1000));

    function finishOk(reason) {
      if (state.finished) {
        return;
      }
      state.finished = true;
      clearTimeout(timeoutHandle);
      writeEvent(finishPayload(state, reason));
      try {
        ws.close();
      } catch (_err) {}
      resolve();
    }

    function finishError(message) {
      if (state.finished) {
        return;
      }
      state.finished = true;
      clearTimeout(timeoutHandle);
      reject(new Error(String(message || "Remote TTS bridge failed")));
    }

    ws.addEventListener("open", () => {
      ws.send(
        JSON.stringify({
          model,
          system_prompt: systemPrompt,
          input: inputText,
        })
      );
    });

    ws.addEventListener("message", (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_err) {
        return;
      }
      if (!payload || typeof payload !== "object") {
        return;
      }

      const eventType = String(payload.type || "").trim();
      if (eventType === "session_ready") {
        state.sessionId = String(payload.session_id || "").trim();
        state.sampleRate = Number(payload.sample_rate || 0) || state.sampleRate;
        writeEvent({
          type: "tts_session_ready",
          session_id: state.sessionId,
          model: String(payload.model || "").trim(),
          sample_rate: state.sampleRate,
          interrupt_path: String(payload.interrupt_path || "").trim(),
        });
        return;
      }

      if (eventType === "llm_delta") {
        const content = String(payload.content || "");
        if (content) {
          state.llmText += content;
          writeEvent({
            type: "tts_llm_delta",
            content,
          });
        }
        return;
      }

      if (eventType === "tts_segment") {
        const text = String(payload.text || "");
        if (text) {
          state.ttsSegments.push(text);
        }
        writeEvent({
          type: "tts_segment",
          text,
        });
        return;
      }

      if (eventType === "tts_segment_done") {
        writeEvent({
          type: "tts_segment_done",
          elapsed_seconds: Number(payload.elapsed_seconds || 0),
          total_samples: Number(payload.total_samples || 0),
        });
        return;
      }

      if (eventType === "audio_chunk") {
        const pcm16Base64 = String(payload.pcm16_base64 || "");
        const chunkBytes = Buffer.byteLength(pcm16Base64, "base64");
        state.gotAudio = true;
        state.audioChunks += 1;
        state.audioBytes += Math.max(0, chunkBytes);
        state.sampleRate = Number(payload.sample_rate || 0) || state.sampleRate;
        writeEvent({
          type: "audio_chunk",
          pcm16_base64: pcm16Base64,
          sample_rate: state.sampleRate,
        });
        return;
      }

      if (eventType === "done") {
        finishOk("remote_done");
        return;
      }

      if (eventType === "interrupted") {
        writeEvent({
          type: "interrupted",
          reason: String(payload.reason || "").trim(),
        });
        finishOk("remote_interrupted");
        return;
      }

      if (eventType === "error") {
        const message = String(payload.message || "Remote TTS service returned error").trim();
        if (state.gotAudio) {
          writeEvent({
            type: "tts_warning",
            message,
          });
          return;
        }
        finishError(message);
      }
    });

    ws.addEventListener("close", (event) => {
      if (state.finished) {
        return;
      }
      if (state.gotAudio) {
        finishOk("socket_closed_after_audio");
        return;
      }
      finishError(
        `Remote TTS websocket closed before audio was received (code=${Number(event.code || 0)})`
      );
    });

    ws.addEventListener("error", () => {
      if (state.finished) {
        return;
      }
      if (state.gotAudio) {
        return;
      }
      finishError("Remote TTS websocket error");
    });
  });
}

async function main() {
  try {
    const request = readRequest();
    await runStream(request);
    process.exit(0);
  } catch (error) {
    writeEvent({
      type: "error",
      message: error instanceof Error ? error.message : String(error),
    });
    process.exit(1);
  }
}

void main();
