#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");
const { execFileSync } = require("child_process");
const { pathToFileURL } = require("url");

function resolveOpenClawDistDir() {
  const envDir = String(process.env.OPENCLAW_DIST_DIR || "").trim();
  if (envDir && fs.existsSync(envDir) && fs.statSync(envDir).isDirectory()) {
    return envDir;
  }

  const candidateDirs = [];
  const home = os.homedir();
  if (home) {
    candidateDirs.push(path.join(home, ".npm-global", "lib", "node_modules", "openclaw", "dist"));
  }
  try {
    const npmRoot = execFileSync("npm", ["root", "-g"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (npmRoot) {
      candidateDirs.push(path.join(npmRoot, "openclaw", "dist"));
    }
  } catch (_err) {}

  for (const dir of candidateDirs) {
    if (dir && fs.existsSync(dir) && fs.statSync(dir).isDirectory()) {
      return dir;
    }
  }

  throw new Error("Unable to locate OpenClaw dist directory");
}

async function loadGatewayModules() {
  const distDir = resolveOpenClawDistDir();
  const callModuleUrl = pathToFileURL(path.join(distDir, "call-DS_a955m.js")).href;
  const scopesModuleUrl = pathToFileURL(path.join(distDir, "method-scopes-D4ep-GlN.js")).href;
  const callModule = await import(callModuleUrl);
  const scopesModule = await import(scopesModuleUrl);
  const GatewayClient = scopesModule.f;
  if (typeof callModule.buildGatewayConnectionDetails !== "function") {
    throw new Error("OpenClaw buildGatewayConnectionDetails export is unavailable");
  }
  if (typeof GatewayClient !== "function") {
    throw new Error("OpenClaw GatewayClient export is unavailable");
  }
  return {
    buildGatewayConnectionDetails: callModule.buildGatewayConnectionDetails,
    GatewayClient,
  };
}

function extractTextFromMessage(message) {
  if (!message || typeof message !== "object") {
    return "";
  }
  if (typeof message.text === "string" && message.text.trim()) {
    return message.text.trim();
  }
  const content = message.content;
  if (typeof content === "string" && content.trim()) {
    return content.trim();
  }
  if (!Array.isArray(content)) {
    return "";
  }
  const parts = [];
  for (const entry of content) {
    if (typeof entry === "string" && entry.trim()) {
      parts.push(entry.trim());
      continue;
    }
    if (!entry || typeof entry !== "object") {
      continue;
    }
    if (typeof entry.text === "string" && entry.text.trim()) {
      parts.push(entry.text.trim());
      continue;
    }
    if (typeof entry.content === "string" && entry.content.trim()) {
      parts.push(entry.content.trim());
    }
  }
  return parts.join("\n").trim();
}

function extractLatestAssistantText(messages) {
  if (!Array.isArray(messages)) {
    return "";
  }
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const entry = messages[idx];
    if (!entry || typeof entry !== "object") {
      continue;
    }
    if (String(entry.role || "").trim().toLowerCase() !== "assistant") {
      continue;
    }
    const text = extractTextFromMessage(entry);
    if (text) {
      return text;
    }
  }
  return "";
}

function writeEnvelope(envelope) {
  process.stdout.write(`${JSON.stringify(envelope)}\n`);
}

function toErrorMessage(error) {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return String(error || "Unknown error");
}

let gatewayClient = null;
let gatewayClientReadyPromise = null;
let currentTurn = null;

function clearCurrentTurn(turn) {
  if (currentTurn === turn) {
    currentTurn = null;
  }
}

function finishCurrentTurn(turn, outcome) {
  if (!turn || turn.done) {
    return;
  }
  turn.done = true;
  if (turn.timeout) {
    clearTimeout(turn.timeout);
    turn.timeout = null;
  }
  clearCurrentTurn(turn);
  turn.resolve(outcome);
}

function handleGatewayEvent(evt) {
  const turn = currentTurn;
  if (!turn || !evt || evt.event !== "chat") {
    return;
  }

  const payload = evt.payload;
  if (!payload || typeof payload !== "object") {
    return;
  }

  const eventSessionKey = String(payload.sessionKey || "").trim();
  if (!eventSessionKey || eventSessionKey !== turn.sessionKey) {
    return;
  }

  const eventRunId = String(payload.runId || "").trim();
  if (!turn.runId && eventRunId) {
    turn.runId = eventRunId;
  }
  if (eventRunId && turn.runId && eventRunId !== turn.runId) {
    return;
  }

  const now = Date.now();
  const state = String(payload.state || "").trim();
  const reply = extractTextFromMessage(payload.message) || turn.reply;
  if (reply) {
    turn.reply = reply;
  }

  if (state === "delta") {
    if (!turn.firstDeltaAt) {
      turn.firstDeltaAt = now;
    }
    writeEnvelope({
      id: turn.requestId,
      type: "delta",
      run_id: turn.runId,
      session_key: turn.sessionKey,
      delta: turn.reply,
      reply: turn.reply,
      elapsed_ms: now - turn.startedAt,
    });
    return;
  }

  if (state === "final") {
    finishCurrentTurn(turn, {
      state,
      reply: turn.reply,
      elapsed_ms: now - turn.startedAt,
      error: "",
    });
    return;
  }

  if (state === "error" || state === "aborted") {
    finishCurrentTurn(turn, {
      state,
      reply: turn.reply,
      elapsed_ms: now - turn.startedAt,
      error: String(payload.errorMessage || "").trim() || `Gateway chat ended with ${state}`,
    });
  }
}

async function stopGatewayClient() {
  const client = gatewayClient;
  gatewayClient = null;
  if (!client || typeof client.stopAndWait !== "function") {
    return;
  }
  try {
    await client.stopAndWait({ timeoutMs: 2000 });
  } catch (_err) {}
}

async function ensureGatewayClient() {
  if (gatewayClient && gatewayClient.connected) {
    return gatewayClient;
  }
  if (gatewayClientReadyPromise) {
    return gatewayClientReadyPromise;
  }

  gatewayClientReadyPromise = (async () => {
    const { buildGatewayConnectionDetails, GatewayClient } = await loadGatewayModules();
    const connectionDetails = await buildGatewayConnectionDetails({});

    return await new Promise((resolve, reject) => {
      let settled = false;

      const settleReject = async (error) => {
        if (settled) {
          return;
        }
        settled = true;
        gatewayClient = null;
        const turn = currentTurn;
        if (turn) {
          finishCurrentTurn(turn, {
            state: "error",
            reply: turn.reply,
            elapsed_ms: Date.now() - turn.startedAt,
            error: toErrorMessage(error),
          });
        }
        reject(error instanceof Error ? error : new Error(toErrorMessage(error)));
      };

      const client = new GatewayClient({
        url: String(connectionDetails.url || "").trim(),
        clientName: "gateway-client",
        clientVersion: "momoagent-quick-control-api-stream",
        mode: "backend",
        caps: ["tool-events"],
        onHelloOk: () => {
          if (settled) {
            return;
          }
          settled = true;
          gatewayClient = client;
          resolve(client);
        },
        onConnectError: settleReject,
        onClose: (info) => {
          if (gatewayClient === client) {
            gatewayClient = null;
          }
          const turn = currentTurn;
          if (turn) {
            finishCurrentTurn(turn, {
              state: "error",
              reply: turn.reply,
              elapsed_ms: Date.now() - turn.startedAt,
              error: `Gateway client closed (${info && info.code ? info.code : 1006})`,
            });
          }
        },
        onEvent: handleGatewayEvent,
      });

      gatewayClient = client;
      client.start();
    });
  })().finally(() => {
    gatewayClientReadyPromise = null;
  });

  return gatewayClientReadyPromise;
}

async function handleChatStreamTurn(request) {
  const requestId = String(request.id || crypto.randomUUID()).trim() || crypto.randomUUID();
  const message = String(request.message || "").trim();
  if (!message) {
    throw new Error("OpenClaw chat stream message is empty");
  }

  const sessionKey = String(request.session_key || request.sessionKey || "").trim();
  if (!sessionKey) {
    throw new Error("OpenClaw chat stream session key is empty");
  }

  const thinking = String(request.thinking || "").trim();
  const timeoutSec = Math.max(5, Number(request.timeout_sec || request.timeoutSec || 90));
  const timeoutMs = Math.round(timeoutSec * 1000);
  const client = await ensureGatewayClient();
  const startedAt = Date.now();

  const turn = {
    requestId,
    sessionKey,
    runId: requestId,
    startedAt,
    firstDeltaAt: 0,
    reply: "",
    done: false,
    timeout: null,
  };

  const outcomePromise = new Promise((resolve) => {
    turn.resolve = resolve;
  });

  turn.timeout = setTimeout(async () => {
    try {
      if (turn.runId) {
        await client.request(
          "chat.abort",
          {
            sessionKey: turn.sessionKey,
            runId: turn.runId,
          },
          { timeoutMs: 5000 }
        );
      }
    } catch (_err) {}

    finishCurrentTurn(turn, {
      state: "error",
      reply: turn.reply,
      elapsed_ms: Date.now() - turn.startedAt,
      error: `Gateway chat stream timed out (${timeoutSec.toFixed(1)}s)`,
    });
  }, timeoutMs);
  turn.timeout.unref?.();

  currentTurn = turn;

  let acceptMs = 0;
  try {
    const acceptedStartedAt = Date.now();
    const accepted = await client.request(
      "agent",
      {
        sessionKey,
        message,
        thinking: thinking || undefined,
        deliver: false,
        idempotencyKey: requestId,
      },
      { timeoutMs: Math.min(timeoutMs, 15000) }
    );
    acceptMs = Date.now() - acceptedStartedAt;
    turn.runId = String((accepted && accepted.runId) || requestId).trim() || requestId;
    writeEnvelope({
      id: requestId,
      type: "accepted",
      run_id: turn.runId,
      session_key: sessionKey,
      status: String((accepted && accepted.status) || "").trim(),
    });
  } catch (error) {
    clearCurrentTurn(turn);
    if (turn.timeout) {
      clearTimeout(turn.timeout);
      turn.timeout = null;
    }
    throw error;
  }

  const outcome = await outcomePromise;
  const firstDeltaMs = turn.firstDeltaAt ? Math.max(0, turn.firstDeltaAt - startedAt) : 0;
  const finalMs = Math.max(0, Number(outcome.elapsed_ms || 0));

  if (outcome.state !== "final") {
    writeEnvelope({
      id: requestId,
      type: "error",
      stage: "chat",
      run_id: turn.runId,
      session_key: sessionKey,
      reply: turn.reply,
      error: String(outcome.error || "").trim() || `Gateway chat ended with ${outcome.state}`,
      timing: {
        accept_ms: acceptMs,
        first_delta_ms: firstDeltaMs,
        final_ms: finalMs,
        wait_ms: Math.max(0, finalMs - acceptMs),
        total_ms: Math.max(0, Date.now() - startedAt),
      },
    });
    return;
  }

  let history = null;
  let historyMs = 0;
  let historyError = "";
  try {
    const historyStartedAt = Date.now();
    history = await client.request(
      "chat.history",
      {
        sessionKey,
        limit: 20,
      },
      { timeoutMs: Math.min(timeoutMs, 5000) }
    );
    historyMs = Date.now() - historyStartedAt;
  } catch (error) {
    historyError = toErrorMessage(error);
  }

  const reply = extractLatestAssistantText(history && history.messages) || turn.reply;
  if (!reply) {
    writeEnvelope({
      id: requestId,
      type: "error",
      stage: historyError ? "history" : "chat",
      run_id: turn.runId,
      session_key: sessionKey,
      reply: "",
      error: historyError || "OpenClaw chat stream could not resolve a final assistant reply",
      timing: {
        accept_ms: acceptMs,
        first_delta_ms: firstDeltaMs,
        final_ms: finalMs,
        history_ms: historyMs,
        wait_ms: Math.max(0, finalMs - acceptMs),
        total_ms: Math.max(0, Date.now() - startedAt),
      },
    });
    return;
  }

  writeEnvelope({
    id: requestId,
    type: "done",
    ok: true,
    run_id: turn.runId,
    session_id: String((history && history.sessionId) || "").trim(),
    session_key: String((history && history.sessionKey) || sessionKey).trim(),
    reply,
    history_error: historyError,
    timing: {
      accept_ms: acceptMs,
      first_delta_ms: firstDeltaMs,
      final_ms: finalMs,
      history_ms: historyMs,
      wait_ms: Math.max(0, finalMs - acceptMs),
      total_ms: Math.max(0, Date.now() - startedAt),
    },
  });
}

async function handleRequest(request) {
  if (!request || typeof request !== "object") {
    throw new Error("OpenClaw chat stream bridge expects a JSON object");
  }
  const op = String(request.op || "").trim();
  if (op !== "chat_stream_turn") {
    throw new Error(`Unsupported bridge op: ${op || "<empty>"}`);
  }
  await handleChatStreamTurn(request);
}

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

let queue = Promise.resolve();

rl.on("line", (line) => {
  const raw = String(line || "").trim();
  if (!raw) {
    return;
  }

  queue = queue
    .then(async () => {
      let request;
      try {
        request = JSON.parse(raw);
      } catch (_err) {
        writeEnvelope({
          id: "",
          type: "error",
          stage: "request",
          error: "OpenClaw chat stream bridge received invalid JSON",
        });
        return;
      }

      try {
        await handleRequest(request);
      } catch (error) {
        writeEnvelope({
          id: String((request && request.id) || "").trim(),
          type: "error",
          stage: "request",
          error: toErrorMessage(error),
        });
      }
    })
    .catch((_err) => {});
});

rl.on("close", () => {
  queue
    .catch((_err) => {})
    .then(async () => {
      await stopGatewayClient();
      process.exit(0);
    });
});
