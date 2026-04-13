#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");
const { execFileSync } = require("child_process");

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

function resolveGatewayModulePaths() {
  const explicitModule = String(process.env.OPENCLAW_GATEWAY_CALL_MODULE || "").trim();
  if (explicitModule && fs.existsSync(explicitModule)) {
    return [explicitModule];
  }

  const distDir = resolveOpenClawDistDir();
  const names = fs.readdirSync(distDir).filter((name) => {
    if (!/\.js$/.test(name)) {
      return false;
    }
    if (/^call-status-/.test(name)) {
      return false;
    }
    return /^call-/.test(name) || /^gateway-rpc\.runtime-/.test(name) || /^auth-profiles-/.test(name);
  });

  // Prefer the re-export file with stable named exports when it exists.
  names.sort((left, right) => {
    const score = (name) => {
      if (/^call-DS_/.test(name)) return 0;
      if (/^call-/.test(name)) return 1;
      if (/^gateway-rpc\.runtime-/.test(name)) return 2;
      if (/^auth-profiles-/.test(name)) return 3;
      return 4;
    };
    return score(left) - score(right) || left.localeCompare(right);
  });

  return names.map((name) => path.join(distDir, name));
}

function resolveGatewayCaller() {
  const candidates = resolveGatewayModulePaths();
  const seenErrors = [];

  for (const modulePath of candidates) {
    try {
      const mod = require(modulePath);

      if (typeof mod.callGateway === "function") {
        return { callGateway: mod.callGateway, modulePath, exportName: "callGateway" };
      }

      for (const [exportName, value] of Object.entries(mod)) {
        if (typeof value === "function" && value.name === "callGateway") {
          return { callGateway: value, modulePath, exportName };
        }
      }

      if (typeof mod.En === "function") {
        return { callGateway: mod.En, modulePath, exportName: "En" };
      }

      seenErrors.push(
        `${path.basename(modulePath)} exports: ${Object.keys(mod).join(",") || "<none>"}`
      );
    } catch (error) {
      seenErrors.push(
        `${path.basename(modulePath)} load failed: ${
          error instanceof Error ? error.message : String(error)
        }`
      );
    }
  }

  throw new Error(
    "OpenClaw bridge could not resolve callGateway export"
      + (seenErrors.length > 0 ? `; candidates=${seenErrors.join(" | ")}` : "")
  );
}

function resolveAuthProfilesModulePath() {
  const envPath = String(process.env.OPENCLAW_AUTH_PROFILES_MODULE || "").trim();
  if (envPath && fs.existsSync(envPath)) {
    return envPath;
  }

  const distDir = resolveOpenClawDistDir();
  const found = fs
    .readdirSync(distDir)
    .filter((name) => /^auth-profiles-.*\.js$/.test(name))
    .sort()
    .at(0);
  if (found) {
    return path.join(distDir, found);
  }

  throw new Error("Unable to locate OpenClaw auth-profiles module");
}

const gatewayCaller = resolveGatewayCaller();
const callGateway = gatewayCaller.callGateway;

if (typeof callGateway !== "function") {
  throw new Error("OpenClaw bridge could not resolve callGateway export");
}

function deriveSessionKey(agentId, sessionKey) {
  const explicit = String(sessionKey || "").trim();
  if (explicit) {
    return explicit;
  }
  const normalizedAgentId = String(agentId || "main").trim() || "main";
  return `agent:${normalizedAgentId}:main`;
}

function extractTextFromContentPart(part) {
  if (typeof part === "string") {
    return part.trim();
  }
  if (!part || typeof part !== "object") {
    return "";
  }
  if (typeof part.text === "string" && part.text.trim()) {
    return part.text.trim();
  }
  if (typeof part.content === "string" && part.content.trim()) {
    return part.content.trim();
  }
  return "";
}

function extractLatestAssistantText(messages) {
  if (!Array.isArray(messages)) {
    return "";
  }
  for (let idx = messages.length - 1; idx >= 0; idx -= 1) {
    const entry = messages[idx];
    if (!entry || typeof entry !== "object" || entry.role !== "assistant") {
      continue;
    }
    const content = entry.content;
    if (Array.isArray(content)) {
      const parts = content.map(extractTextFromContentPart).filter(Boolean);
      if (parts.length > 0) {
        return parts.join("\n").trim();
      }
    }
    if (typeof content === "string" && content.trim()) {
      return content.trim();
    }
    if (typeof entry.text === "string" && entry.text.trim()) {
      return entry.text.trim();
    }
  }
  return "";
}

async function runAgentTurn(request) {
  const message = String(request.message || "").trim();
  if (!message) {
    throw new Error("OpenClaw bridge message is empty");
  }

  const timeoutSec = Math.max(5, Number(request.timeout_sec || request.timeoutSec || 90));
  const timeoutMs = Math.round(timeoutSec * 1000);
  const sessionKey = deriveSessionKey(request.agent_id, request.session_key);
  const idempotencyKey = String(
    request.idempotency_key || request.idempotencyKey || `bridge-${crypto.randomUUID()}`
  ).trim();
  const thinking = String(request.thinking || "minimal").trim() || "minimal";

  const agentParams = {
    message,
    thinking,
    deliver: false,
    sessionKey,
    idempotencyKey,
  };

  const acceptStarted = Date.now();
  const accepted = await callGateway({
    method: "agent",
    params: agentParams,
    timeoutMs: Math.min(timeoutMs, 10000),
  });
  const acceptMs = Date.now() - acceptStarted;

  const runId =
    (accepted && typeof accepted.runId === "string" && accepted.runId.trim()) || idempotencyKey;

  const waitStarted = Date.now();
  const waited = await callGateway({
    method: "agent.wait",
    params: {
      runId,
      timeoutMs,
    },
    timeoutMs: timeoutMs + 2000,
  });
  const waitMs = Date.now() - waitStarted;

  const historyStarted = Date.now();
  const history = await callGateway({
    method: "chat.history",
    params: {
      sessionKey,
      limit: 20,
    },
    timeoutMs: Math.min(timeoutMs, 5000),
  });
  const historyMs = Date.now() - historyStarted;

  const reply = extractLatestAssistantText(history && history.messages);
  if (!reply) {
    throw new Error("OpenClaw bridge could not resolve latest assistant reply");
  }

  return {
    reply,
    run_id: String(runId).trim(),
    session_id: String((history && history.sessionId) || "").trim(),
    session_key: String((history && history.sessionKey) || sessionKey).trim(),
    wait_status: String((waited && waited.status) || "").trim(),
    timing: {
      accept_ms: acceptMs,
      wait_ms: waitMs,
      history_ms: historyMs,
      total_ms: acceptMs + waitMs + historyMs,
    },
  };
}

function writeEnvelope(envelope) {
  process.stdout.write(`${JSON.stringify(envelope)}\n`);
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
          ok: false,
          error: "OpenClaw bridge received invalid JSON",
        });
        return;
      }

      const requestId = String((request && request.id) || "").trim();
      try {
        if (!request || request.op !== "agent_turn") {
          throw new Error(`Unsupported bridge op: ${String(request && request.op)}`);
        }
        const payload = await runAgentTurn(request);
        writeEnvelope({
          id: requestId,
          ok: true,
          payload,
        });
      } catch (err) {
        writeEnvelope({
          id: requestId,
          ok: false,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    })
    .catch((_err) => {});
});

rl.on("close", () => {
  process.exit(0);
});
