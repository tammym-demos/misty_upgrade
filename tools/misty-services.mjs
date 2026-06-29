#!/usr/bin/env node

import fs from "node:fs";
import dns from "node:dns/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");
const ORCH_DIR = path.join(REPO_ROOT, "src", "windows-orchestration");
const STATE_PATH = path.join(REPO_ROOT, ".misty-services.json");
const LOG_DIR = path.join(REPO_ROOT, "logs", "services");

const DEFAULTS = {
  controllerPort: 5001,
  orchestrationHealthUrl: "http://127.0.0.1:5000/api/health",
  controllerStatusUrl: "http://127.0.0.1:5001/api/status",
  mistyIp: "10.0.0.44",
  chatModelId: "Phi-3.5-mini-instruct-openvino-gpu:2",
  chatModelAlias: "phi-3.5-mini",
  modelTtlSeconds: 3600,
};

function usage() {
  console.log(`Usage: npx . <command> [options]

Commands:
  start     Start Foundry Local, orchestration_service.py, and misty_controller.py
  stop      Gracefully stop the controller, orchestration service, and owned Foundry service
  restart   Stop then start all services
  status    Show service status

Options:
  --skip-foundry          Do not start or stop Foundry Local
  --skip-model-load       Do not load the chat model into Foundry Local
  --keep-foundry          Leave Foundry Local running on stop
  --misty-ip <ip>         Override MISTY_IP for controller and cleanup calls
  --no-scan               Do not scan local networks when Misty is unreachable
  --orchestration-url <url>
                          Override ORCHESTRATION_URL for the controller
  --controller-port <n>   Override controller API port (default: 5001)
  --python <command>      Python executable to use (default: python)
  -h, --help              Show this help
`);
}

function parseArgs(argv) {
  const options = {
    command: argv[2] ?? "help",
    skipFoundry: false,
    skipModelLoad: false,
    keepFoundry: false,
    scan: true,
    controllerPort: DEFAULTS.controllerPort,
    python: "python",
  };
  if (options.command === "-h" || options.command === "--help") {
    options.command = "help";
  }

  for (let index = 3; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--skip-foundry") {
      options.skipFoundry = true;
    } else if (arg === "--skip-model-load") {
      options.skipModelLoad = true;
    } else if (arg === "--keep-foundry") {
      options.keepFoundry = true;
    } else if (arg === "--misty-ip") {
      options.mistyIp = argv[++index];
    } else if (arg === "--no-scan") {
      options.scan = false;
    } else if (arg === "--orchestration-url") {
      options.orchestrationUrl = argv[++index];
    } else if (arg === "--controller-port") {
      options.controllerPort = Number(argv[++index]);
    } else if (arg === "--python") {
      options.python = argv[++index];
    } else if (arg === "-h" || arg === "--help") {
      options.command = "help";
    } else {
      throw new Error(`Unknown option: ${arg}`);
    }
  }

  if (!Number.isInteger(options.controllerPort) || options.controllerPort < 1 || options.controllerPort > 65535) {
    throw new Error("--controller-port must be a valid TCP port");
  }

  return options;
}

function readDotEnv(filePath) {
  if (!fs.existsSync(filePath)) {
    return {};
  }

  const env = {};
  for (const rawLine of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }

    const separator = line.indexOf("=");
    if (separator === -1) {
      continue;
    }

    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    env[key] = value;
  }
  return env;
}

function serviceEnv(options, state = {}) {
  const env = {
    ...process.env,
    ...readDotEnv(path.join(ORCH_DIR, ".env")),
  };

  if (options.mistyIp) {
    env.MISTY_IP = options.mistyIp;
  } else if (!env.MISTY_IP && state.misty?.ipAddress) {
    env.MISTY_IP = state.misty.ipAddress;
  }
  if (options.orchestrationUrl) {
    env.ORCHESTRATION_URL = options.orchestrationUrl;
  }
  env.CONTROLLER_API_PORT = String(options.controllerPort);
  env.USE_LAPTOP_WAKE_WORD = env.USE_LAPTOP_WAKE_WORD || "true";
  return env;
}

function readState() {
  if (!fs.existsSync(STATE_PATH)) {
    return {};
  }

  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
  } catch {
    return {};
  }
}

function writeState(state) {
  fs.writeFileSync(STATE_PATH, `${JSON.stringify(state, null, 2)}\n`);
}

function removeStateIfEmpty(state) {
  const hasProcess = state.orchestration?.pid || state.controller?.pid || state.foundry?.managed;
  if (!hasProcess && fs.existsSync(STATE_PATH)) {
    fs.rmSync(STATE_PATH);
  } else {
    writeState(state);
  }
}

function isPidRunning(pid) {
  if (!pid) {
    return false;
  }

  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd ?? REPO_ROOT,
    env: options.env ?? process.env,
    encoding: "utf8",
    timeout: options.timeout ?? 30000,
    windowsHide: true,
  });

  return {
    ok: result.status === 0,
    status: result.status,
    stdout: result.stdout?.trim() ?? "",
    stderr: result.stderr?.trim() ?? "",
    error: result.error,
  };
}

function ensureCommand(command, args) {
  const result = run(command, args);
  if (!result.ok) {
    const details = result.error?.message || result.stderr || result.stdout || `exit code ${result.status}`;
    throw new Error(`Required command failed: ${command} ${args.join(" ")}\n${details}`);
  }
}

function foundryIsRunning() {
  const result = run("foundry", ["service", "status"], { timeout: 15000 });
  const output = `${result.stdout}\n${result.stderr}`.toLowerCase();
  if (!result.ok || output.includes("not running")) {
    return false;
  }
  return output.includes("running") || output.includes("listening") || output.includes("http");
}

function startFoundry(state, options) {
  if (options.skipFoundry) {
    console.log("Foundry Local: skipped");
    return;
  }

  const wasRunning = foundryIsRunning();
  if (!wasRunning) {
    console.log("Foundry Local: starting service");
    const result = run("foundry", ["service", "start"], { timeout: 60000 });
    if (!result.ok && !foundryIsRunning()) {
      throw new Error(`Unable to start Foundry Local:\n${result.stderr || result.stdout}`);
    }
  } else {
    console.log("Foundry Local: already running");
  }

  state.foundry = {
    managed: !wasRunning,
    startedAt: new Date().toISOString(),
  };
}

function stopFoundry(state, options) {
  if (options.skipFoundry || options.keepFoundry) {
    console.log("Foundry Local: left running");
    return;
  }

  if (!state.foundry?.managed) {
    console.log("Foundry Local: not owned by this CLI run; leaving it running");
    return;
  }

  console.log("Foundry Local: stopping service");
  const result = run("foundry", ["service", "stop"], { timeout: 60000 });
  if (!result.ok) {
    console.warn(`Foundry Local: stop command reported a problem:\n${result.stderr || result.stdout}`);
  }
  delete state.foundry;
}

function chatModelIsLoaded() {
  const result = run("foundry", ["service", "ps"], { timeout: 30000 });
  return result.ok && (
    result.stdout.includes(DEFAULTS.chatModelId)
    || result.stdout.includes(DEFAULTS.chatModelAlias)
  );
}

function loadChatModel(state, options) {
  if (options.skipFoundry || options.skipModelLoad) {
    console.log("Foundry chat model: skipped");
    return;
  }

  if (chatModelIsLoaded()) {
    console.log(`Foundry chat model: already loaded (${DEFAULTS.chatModelAlias})`);
    state.chatModel = {
      alias: DEFAULTS.chatModelAlias,
      modelId: DEFAULTS.chatModelId,
      loadedAt: state.chatModel?.loadedAt || new Date().toISOString(),
    };
    return;
  }

  console.log(`Foundry chat model: loading ${DEFAULTS.chatModelAlias}`);
  const result = run("foundry", [
    "model",
    "load",
    DEFAULTS.chatModelId,
    "--ttl",
    String(DEFAULTS.modelTtlSeconds),
  ], { timeout: 180000 });

  if (!result.ok && !chatModelIsLoaded()) {
    throw new Error(`Unable to load Foundry chat model:\n${result.stderr || result.stdout}`);
  }

  state.chatModel = {
    alias: DEFAULTS.chatModelAlias,
    modelId: DEFAULTS.chatModelId,
    ttlSeconds: DEFAULTS.modelTtlSeconds,
    loadedAt: new Date().toISOString(),
  };
}

function httpRequest(url, options = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request(url, {
      method: options.method ?? "GET",
      timeout: options.timeoutMs ?? 3000,
      headers: options.headers ?? {},
    }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        resolve({ statusCode: response.statusCode ?? 0, body });
      });
    });

    request.on("timeout", () => {
      request.destroy(new Error(`Timed out requesting ${url}`));
    });
    request.on("error", reject);
    if (options.body) {
      request.write(options.body);
    }
    request.end();
  });
}

async function waitForHttp(url, acceptedStatusCodes, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "";

  while (Date.now() < deadline) {
    try {
      const response = await httpRequest(url, { timeoutMs: 3000 });
      if (acceptedStatusCodes.includes(response.statusCode)) {
        return response;
      }
      lastError = `HTTP ${response.statusCode}`;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  throw new Error(`${label} did not become ready within ${Math.round(timeoutMs / 1000)}s (${lastError})`);
}

function parseJsonObject(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function isMistyDevice(payload) {
  const result = payload?.result;
  return payload?.status === "Success"
    && typeof result?.robotId === "string"
    && typeof result?.robotVersion === "string"
    && typeof result?.serialNumber === "string";
}

function getPrivateIpv4Prefixes() {
  const prefixes = new Set();
  for (const adapter of Object.values(os.networkInterfaces())) {
    for (const address of adapter ?? []) {
      if (address.family !== "IPv4" || address.internal) {
        continue;
      }

      const octets = address.address.split(".").map(Number);
      const isPrivate = octets[0] === 10
        || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
        || (octets[0] === 192 && octets[1] === 168);
      if (isPrivate) {
        prefixes.add(octets.slice(0, 3).join("."));
      }
    }
  }
  return [...prefixes];
}

async function reverseName(ipAddress) {
  try {
    const names = await dns.reverse(ipAddress);
    return names[0] || null;
  } catch {
    return null;
  }
}

function mistyIdentity(ipAddress, payload, broadcastName) {
  const result = payload.result;
  const macSuffix = (result.macAddress || "").split(":").slice(-3).join("").toUpperCase();
  const fallbackName = result.serialNumber ? `misty-${result.serialNumber}` : `misty-${macSuffix || ipAddress}`;
  return {
    ipAddress: result.ipAddress || ipAddress,
    broadcastName: broadcastName || fallbackName,
    serialNumber: result.serialNumber || null,
    macAddress: result.macAddress || null,
    robotId: result.robotId || null,
    robotVersion: result.robotVersion || null,
    networkProfileName: result.currentProfileName || null,
    discoveredAt: new Date().toISOString(),
  };
}

async function probeMisty(ipAddress, timeoutMs = 1500) {
  const response = await httpRequest(`http://${ipAddress}/api/device`, { timeoutMs });
  if (response.statusCode < 200 || response.statusCode >= 300) {
    return null;
  }

  const payload = parseJsonObject(response.body);
  if (!isMistyDevice(payload)) {
    return null;
  }

  return mistyIdentity(ipAddress, payload, await reverseName(ipAddress));
}

async function mapWithConcurrency(items, concurrency, mapper) {
  const results = [];
  let cursor = 0;

  async function worker() {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await mapper(items[index]);
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  return results;
}

async function scanForMisty() {
  const prefixes = getPrivateIpv4Prefixes();
  if (prefixes.length === 0) {
    return null;
  }

  console.log(`Misty robot: scanning ${prefixes.map((prefix) => `${prefix}.0/24`).join(", ")}`);
  const addresses = prefixes.flatMap((prefix) => (
    Array.from({ length: 254 }, (_, index) => `${prefix}.${index + 1}`)
  ));

  const matches = (await mapWithConcurrency(addresses, 96, async (ipAddress) => {
    try {
      return await probeMisty(ipAddress);
    } catch {
      return null;
    }
  })).filter(Boolean);

  return matches[0] || null;
}

async function pingMisty(env, state, options) {
  const mistyIp = env.MISTY_IP || state.misty?.ipAddress || DEFAULTS.mistyIp;
  const deviceUrl = `http://${mistyIp}/api/device`;

  try {
    const discovered = await probeMisty(mistyIp, 5000);
    if (discovered) {
      env.MISTY_IP = discovered.ipAddress;
      state.misty = discovered;
      console.log(`Misty robot: reachable at ${discovered.ipAddress} (${discovered.broadcastName})`);
      return;
    }

    const response = await httpRequest(deviceUrl, { timeoutMs: 5000 });
    throw new Error(`HTTP ${response.statusCode}`);
  } catch (error) {
    if (options.scan) {
      const discovered = await scanForMisty();
      if (discovered) {
        env.MISTY_IP = discovered.ipAddress;
        state.misty = discovered;
        console.log(`Misty robot: discovered at ${discovered.ipAddress} (${discovered.broadcastName})`);
        return;
      }
    }

    throw new Error(
      `Misty robot is not reachable at ${mistyIp}; not starting the controller.\n` +
      `Check power, Wi-Fi, pass the correct address with --misty-ip <ip>, or allow network scanning.\n` +
      `Details: ${error.message}`,
    );
  }
}

function spawnService(name, command, args, cwd, env) {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const stdoutPath = path.join(LOG_DIR, `${name}.log`);
  const stderrPath = path.join(LOG_DIR, `${name}.err.log`);
  const stdout = fs.openSync(stdoutPath, "a");
  const stderr = fs.openSync(stderrPath, "a");

  const child = spawn(command, args, {
    cwd,
    env,
    detached: true,
    stdio: ["ignore", stdout, stderr],
    windowsHide: false,
  });

  child.unref();
  return {
    pid: child.pid,
    command: [command, ...args].join(" "),
    cwd,
    stdoutPath,
    stderrPath,
    startedAt: new Date().toISOString(),
  };
}

async function start(options) {
  ensureCommand(options.python, ["--version"]);
  if (!options.skipFoundry) {
    ensureCommand("foundry", ["--version"]);
  }

  const state = readState();
  const env = serviceEnv(options, state);
  state.version = 1;
  state.startedAt = state.startedAt || new Date().toISOString();

  startFoundry(state, options);
  loadChatModel(state, options);

  const orchestrationAlreadyRunning = await httpRequest(DEFAULTS.orchestrationHealthUrl, { timeoutMs: 1500 })
    .then((response) => [200, 503].includes(response.statusCode))
    .catch(() => false);
  if (orchestrationAlreadyRunning) {
    console.log("Orchestration service: already running");
  } else {
    console.log("Orchestration service: starting");
    state.orchestration = spawnService(
      "orchestration",
      options.python,
      ["orchestration_service.py"],
      ORCH_DIR,
      env,
    );
    writeState(state);
    await waitForHttp(DEFAULTS.orchestrationHealthUrl, [200, 503], 180000, "Orchestration service");
  }

  const controllerStatusUrl = `http://127.0.0.1:${options.controllerPort}/api/status`;
  const controllerAlreadyRunning = await httpRequest(controllerStatusUrl, { timeoutMs: 1500 })
    .then((response) => response.statusCode === 200)
    .catch(() => false);
  if (controllerAlreadyRunning) {
    console.log("Misty controller: already running");
  } else {
    await pingMisty(env, state, options);
    console.log("Misty controller: starting");
    state.controller = spawnService(
      "misty-controller",
      options.python,
      ["misty_controller.py"],
      ORCH_DIR,
      env,
    );
    state.controller.port = options.controllerPort;
    writeState(state);
    await waitForHttp(controllerStatusUrl, [200], 90000, "Misty controller");
  }

  writeState(state);
  console.log("Misty services are running");
}

async function gracefulControllerShutdown(state, options) {
  const port = state.controller?.port ?? options.controllerPort;
  const shutdownUrl = `http://127.0.0.1:${port}/api/shutdown`;
  try {
    const response = await httpRequest(shutdownUrl, { method: "POST", timeoutMs: 3000 });
    if (response.statusCode === 200) {
      console.log("Misty controller: graceful shutdown requested");
    } else {
      console.warn(`Misty controller: shutdown endpoint returned HTTP ${response.statusCode}`);
    }
  } catch (error) {
    console.warn(`Misty controller: shutdown endpoint unavailable (${error.message})`);
  }
}

async function waitForPidExit(pid, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isPidRunning(pid)) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return !isPidRunning(pid);
}

async function stopPid(label, pid, gracefulSignal = "SIGTERM") {
  if (!pid || !isPidRunning(pid)) {
    console.log(`${label}: not running`);
    return;
  }

  try {
    process.kill(pid, gracefulSignal);
  } catch (error) {
    console.warn(`${label}: failed to send ${gracefulSignal} to PID ${pid}: ${error.message}`);
  }

  if (await waitForPidExit(pid, 10000)) {
    console.log(`${label}: stopped`);
    return;
  }

  process.kill(pid, "SIGKILL");
  console.log(`${label}: force-stopped`);
}

async function cleanupMisty(env) {
  const mistyIp = env.MISTY_IP;
  if (!mistyIp) {
    return;
  }

  const baseUrl = `http://${mistyIp}`;
  const calls = [
    ["POST", "/api/audio/keyphrase/stop", undefined],
    ["POST", "/api/audio/record/stop", undefined],
    ["POST", "/api/skills/cancel", undefined],
    ["POST", "/api/led", JSON.stringify({ red: 0, green: 0, blue: 0 })],
  ];

  for (const [method, endpoint, body] of calls) {
    try {
      await httpRequest(`${baseUrl}${endpoint}`, {
        method,
        body,
        timeoutMs: 2500,
        headers: body ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) } : {},
      });
    } catch (error) {
      console.warn(`Misty cleanup ${endpoint}: ${error.message}`);
    }
  }
}

async function stop(options) {
  const state = readState();
  const env = serviceEnv(options, state);

  await gracefulControllerShutdown(state, options);
  await waitForPidExit(state.controller?.pid, 10000);
  await cleanupMisty(env);
  await stopPid("Misty controller", state.controller?.pid, "SIGTERM");
  delete state.controller;

  await stopPid("Orchestration service", state.orchestration?.pid, "SIGTERM");
  delete state.orchestration;

  stopFoundry(state, options);
  removeStateIfEmpty(state);
  console.log("Misty services are stopped");
}

async function status(options) {
  const state = readState();
  const orchestration = await httpRequest(DEFAULTS.orchestrationHealthUrl, { timeoutMs: 1500 })
    .then((response) => [200, 503].includes(response.statusCode) ? `running (HTTP ${response.statusCode})` : `unexpected HTTP ${response.statusCode}`)
    .catch(() => "not reachable");
  const controller = await httpRequest(`http://127.0.0.1:${options.controllerPort}/api/status`, { timeoutMs: 1500 })
    .then((response) => response.statusCode === 200 ? "running" : `unexpected HTTP ${response.statusCode}`)
    .catch(() => "not reachable");
  const foundry = options.skipFoundry ? "skipped" : foundryIsRunning() ? "running" : "not running";

  console.log(`Foundry Local: ${foundry}`);
  console.log(`Orchestration service: ${orchestration}${state.orchestration?.pid ? ` (PID ${state.orchestration.pid})` : ""}`);
  console.log(`Misty controller: ${controller}${state.controller?.pid ? ` (PID ${state.controller.pid})` : ""}`);
}

async function main() {
  const options = parseArgs(process.argv);
  switch (options.command) {
    case "start":
      await start(options);
      break;
    case "stop":
      await stop(options);
      break;
    case "restart":
      await stop(options);
      await start(options);
      break;
    case "status":
      await status(options);
      break;
    case "help":
      usage();
      break;
    default:
      throw new Error(`Unknown command: ${options.command}`);
  }
}

main().catch(() => {
  console.error("Command failed. Re-run with safe diagnostics if needed.");
  process.exitCode = 1;
});
