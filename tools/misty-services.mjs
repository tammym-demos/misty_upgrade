#!/usr/bin/env node

import fs from "node:fs";
import dns from "node:dns/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";
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
  chatModelId: "Phi-3.5-mini-instruct-generic-cpu:2",
  chatModelAlias: "phi-3.5-mini",
  modelTtlSeconds: 3600,
  wakeWordModelPath: path.join(REPO_ROOT, "models", "hey_misty.onnx"),
};

const WAKE_WORD_PYTHON_MODULES = ["numpy", "sounddevice", "openwakeword"];
const ORCHESTRATION_PYTHON_MODULES = ["faster_whisper", "kokoro_onnx", "soundfile"];
const OPENWAKEWORD_RESOURCE_MODELS = ["melspectrogram.onnx", "embedding_model.onnx"];
const KOKORO_MODEL_FILES = ["kokoro-v1.0.int8.onnx", "voices-v1.0.bin"];

function usage() {
  console.log(`Usage: npx . <command> [options]

Commands:
  start     Start Foundry Local, orchestration_service.py, and misty_controller.py
  stop      Gracefully stop the controller, orchestration service, and Foundry Local
  restart   Stop then start all services
  status    Show service status
  conference <subcommand> [opts]
            Run Conference Mode (scripted stage dialog).
            Subcommands: dry-run, prepare, verify, run

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
  --yes                   Assume yes for install prompts
  --no-install            Do not prompt to install missing prerequisites
  -h, --help              Show this help

Conference Mode (npx . conference <subcommand>):
  dry-run [--script <path>]       Preview the ordered cue plan (no hardware)
  prepare [--script <path>]       Generate/import/reuse cue audio + manifest
  verify                          Confirm every cue has playable audio
  run [--auto|--no-auto]          Live interactive stage runner (requires Misty)
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
    pythonExplicit: false,
    yes: false,
    install: true,
  };
  if (options.command === "-h" || options.command === "--help") {
    options.command = "help";
  }

  // Conference passes all remaining args through to conference_mode.py.
  if (options.command === "conference") {
    return options;
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
      options.pythonExplicit = true;
    } else if (arg === "--yes" || arg === "-y") {
      options.yes = true;
    } else if (arg === "--no-install") {
      options.install = false;
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

function runInherited(command, args, options = {}) {
  return spawnSync(command, args, {
    cwd: options.cwd ?? REPO_ROOT,
    env: options.env ?? process.env,
    stdio: "inherit",
    timeout: options.timeout ?? 600000,
    windowsHide: false,
  });
}

function commandWorks(command, args) {
  return run(command, args).ok;
}

async function confirmInstall(question, options) {
  if (!options.install) {
    return false;
  }
  if (options.yes) {
    return true;
  }
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    return false;
  }

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    const answer = (await rl.question(`${question} [Y/n] `)).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally {
    rl.close();
  }
}

function pythonRequirements() {
  const requirementsPath = path.join(ORCH_DIR, "requirements.txt");
  const packages = [];

  for (const rawLine of fs.readFileSync(requirementsPath, "utf8").split(/\r?\n/)) {
    const line = rawLine.split("#", 1)[0].trim();
    if (!line || line.startsWith("-")) {
      continue;
    }
    const packageName = line.split(/[<>=~!;\[]/, 1)[0].trim();
    if (packageName) {
      packages.push(packageName);
    }
  }

  return packages;
}

function missingPythonRequirements(python) {
  const packages = pythonRequirements();
  const script = `
import importlib.metadata as metadata
import sys

missing = []
for package in sys.argv[1:]:
    try:
        metadata.version(package)
    except metadata.PackageNotFoundError:
        missing.append(package)

print("\\n".join(missing))
sys.exit(1 if missing else 0)
`;

  const result = run(python, ["-c", script, ...packages]);
  if (result.error) {
    throw new Error(`Unable to inspect Python packages with ${python}:\n${result.error.message}`);
  }

  return result.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function pythonPlatform(python) {
  const script = `
import platform
import sys
import sysconfig

print(sys.version.split()[0])
print(platform.machine())
print(sysconfig.get_platform())
`;
  const result = run(python, ["-c", script]);
  if (!result.ok) {
    const details = result.error?.message || result.stderr || result.stdout || `exit code ${result.status}`;
    throw new Error(`Unable to inspect Python runtime with ${python}:\n${details}`);
  }
  const [version, machine, platformTag] = result.stdout.split(/\r?\n/).map((line) => line.trim());
  return { version, machine, platformTag };
}

function windowsX64PythonPath() {
  return path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python312-x64", "python.exe");
}

function needsFasterWhisper(packages = pythonRequirements()) {
  return packages.includes("faster-whisper");
}

function assertPythonRuntimeCompatible(options, packages = pythonRequirements()) {
  if (!needsFasterWhisper(packages)) {
    return;
  }

  const runtime = pythonPlatform(options.python);
  if (process.platform === "win32" && runtime.platformTag === "win-arm64") {
    const x64Python = windowsX64PythonPath();
    const pythonSuggestion = fs.existsSync(x64Python)
      ? `  npx . start --python "${x64Python}"\n`
      : "  npx . start --python C:\\Path\\To\\Python312-x64\\python.exe\n";
    throw new Error(
      "The active Python runtime is Windows ARM64, but faster-whisper requires ctranslate2, " +
      "which does not publish a Windows ARM64 pip wheel.\n" +
      `Detected: ${options.python} ${runtime.version} (${runtime.machine}, ${runtime.platformTag})\n` +
      "Install a Windows x64 Python runtime and retry with that executable, for example:\n" +
      pythonSuggestion +
      "Do not use the PyPI package named 'foundry-local'; Foundry Local is installed with winget.",
    );
  }
}

function selectCompatiblePython(options) {
  if (options.pythonExplicit || process.platform !== "win32" || !needsFasterWhisper()) {
    return;
  }

  const runtime = pythonPlatform(options.python);
  if (runtime.platformTag !== "win-arm64") {
    return;
  }

  const x64Python = windowsX64PythonPath();
  if (!fs.existsSync(x64Python)) {
    return;
  }

  const x64Runtime = pythonPlatform(x64Python);
  if (x64Runtime.platformTag !== "win-amd64") {
    return;
  }

  options.python = x64Python;
  console.log(`Python: using Windows x64 runtime at ${x64Python}`);
}

async function ensurePython(options) {
  const result = run(options.python, ["--version"]);
  if (result.ok) {
    return;
  }

  const details = result.error?.message || result.stderr || result.stdout || `exit code ${result.status}`;
  throw new Error(
    `Python is required but '${options.python}' is not available.\n` +
    "Install Python, or pass the executable with --python <command>.\n" +
    `Details: ${details}`,
  );
}

async function ensurePythonDependencies(options) {
  assertPythonRuntimeCompatible(options);
  const missing = missingPythonRequirements(options.python);
  if (missing.length === 0) {
    return;
  }

  const installCommand = `${options.python} -m pip install -r ${path.join("src", "windows-orchestration", "requirements.txt")}`;
  if (!await confirmInstall(
    `Python dependencies are missing (${missing.join(", ")}). Run '${installCommand}' now?`,
    options,
  )) {
    throw new Error(
      `Python dependencies are missing: ${missing.join(", ")}\n` +
      `Install them with: ${installCommand}`,
    );
  }

  const install = runInherited(options.python, [
    "-m",
    "pip",
    "install",
    "-r",
    path.join(ORCH_DIR, "requirements.txt"),
  ]);
  if (install.status !== 0) {
    throw new Error(`Python dependency installation failed with exit code ${install.status}`);
  }

  const stillMissing = missingPythonRequirements(options.python);
  if (stillMissing.length > 0) {
    throw new Error(`Python dependencies are still missing after install: ${stillMissing.join(", ")}`);
  }
}

async function ensureFoundryCli(options) {
  if (options.skipFoundry || commandWorks("foundry", ["--version"])) {
    return;
  }

  const wingetAvailable = process.platform === "win32" && commandWorks("winget", ["--version"]);
  if (!wingetAvailable) {
    throw new Error(
      "Foundry Local CLI is required but 'foundry' is not on PATH.\n" +
      "Install Microsoft Foundry Local, then open a new terminal and retry.",
    );
  }

  if (!await confirmInstall(
    "Foundry Local CLI is missing. Install Microsoft.FoundryLocal with winget now?",
    options,
  )) {
    throw new Error(
      "Foundry Local CLI is required but 'foundry' is not on PATH.\n" +
      "Install it with: winget install --id Microsoft.FoundryLocal --source winget",
    );
  }

  const install = runInherited("winget", [
    "install",
    "--id",
    "Microsoft.FoundryLocal",
    "--source",
    "winget",
    "--accept-package-agreements",
    "--accept-source-agreements",
  ], { timeout: 900000 });
  if (install.status !== 0) {
    throw new Error(`Foundry Local installation failed with exit code ${install.status}`);
  }
  if (!commandWorks("foundry", ["--version"])) {
    throw new Error(
      "Foundry Local was installed, but 'foundry' is still not available on PATH.\n" +
      "Open a new terminal and retry, or ensure the WindowsApps directory is on PATH.",
    );
  }
}

async function ensureStartPrerequisites(options) {
  await ensurePython(options);
  selectCompatiblePython(options);
  await ensurePythonDependencies(options);
  await ensureFoundryCli(options);
}

function redactSensitive(value) {
  return String(value ?? "")
    .replace(/(token|key|secret|password|pwd)=([^&\s]+)/gi, "$1=<redacted>")
    .replace(/(https?:\/\/[^:\s]+:)[^@\s]+@/gi, "$1<redacted>@");
}

function envFlagEnabled(value, defaultValue) {
  if (value === undefined || value === null || value === "") {
    return defaultValue;
  }

  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

function parseImportFailures(stdout) {
  return parseJsonMarker(stdout, "MISTY_IMPORT_CHECK=");
}

function parseJsonMarker(stdout, marker) {
  const line = stdout.split(/\r?\n/).find((entry) => entry.startsWith(marker));
  if (!line) {
    return null;
  }

  try {
    return JSON.parse(line.slice(marker.length));
  } catch {
    return null;
  }
}

function checkPythonImports(options, env, modules, label) {
  const script = `
import importlib
import json
import sys

failures = {}
for module in ${JSON.stringify(modules)}:
    try:
        importlib.import_module(module)
    except Exception as exc:
        failures[module] = f"{type(exc).__name__}: {exc}"

print("MISTY_IMPORT_CHECK=" + json.dumps(failures, sort_keys=True))
sys.exit(1 if failures else 0)
`;
  const result = run(options.python, ["-c", script], { cwd: ORCH_DIR, env, timeout: 30000 });
  if (result.ok) {
    return;
  }

  const failures = parseImportFailures(result.stdout);
  const details = failures
    ? Object.entries(failures).map(([module, error]) => `  - ${module}: ${error}`).join("\n")
    : redactSensitive(result.stderr || result.stdout || result.error?.message || `exit code ${result.status}`);
  throw new Error(
    `${label} prerequisites are not ready:\n${details}\n\n` +
    "Install runtime dependencies, then retry startup:\n" +
    `  cd ${ORCH_DIR}\n` +
    `  ${options.python} -m pip install -r requirements.txt\n\n` +
    "On Windows ARM64, faster-whisper/PyAV wheels may be unavailable for ARM64 Python. " +
    "Use x64 Python for live services, for example:\n" +
    "  npx . start --python C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312-x64\\python.exe",
  );
}

function warnIfLikelyArm64Python(options, env) {
  const script = `
import json
import platform
import sys

print("MISTY_PYTHON_RUNTIME=" + json.dumps({
    "executable": sys.executable,
    "machine": platform.machine(),
    "platform": sys.platform,
}, sort_keys=True))
`;
  const result = run(options.python, ["-c", script], { cwd: ORCH_DIR, env, timeout: 30000 });
  const payload = parseJsonMarker(result.stdout, "MISTY_PYTHON_RUNTIME=");
  const executable = String(payload?.executable || options.python);
  if (process.platform === "win32" && /arm64/i.test(executable)) {
    console.warn(
      "Preflight warning: Python path appears to be Windows ARM64. " +
      "Live STT uses faster-whisper, which may require x64 Python on this companion device. " +
      "If startup or STT fails, retry with --python pointing at Python312-x64\\python.exe.",
    );
  }
}

function checkWakeWordModelPath(env) {
  const configuredPath = env.OWW_CUSTOM_MODEL_PATH?.trim();
  if (!configuredPath) {
    if (fs.existsSync(DEFAULTS.wakeWordModelPath)) {
      env.OWW_CUSTOM_MODEL_PATH = DEFAULTS.wakeWordModelPath;
      return;
    }

    throw new Error(
      "Laptop wake-word prerequisites are not ready:\n" +
      `  - Bundled wake-word model is missing: ${DEFAULTS.wakeWordModelPath}\n` +
      "  - OWW_CUSTOM_MODEL_PATH is not configured.\n\n" +
      "The supported wake path requires a trained custom OpenWakeWord model for \"Hey Misty\"; " +
      "Misty's built-in keyphrase is unsupported.\n" +
      "Restore models\\hey_misty.onnx or set OWW_CUSTOM_MODEL_PATH in " +
      "src\\windows-orchestration\\.env or the environment, then retry startup.",
    );
  }

  const modelPath = path.isAbsolute(configuredPath)
    ? configuredPath
    : path.resolve(ORCH_DIR, configuredPath);
  if (!fs.existsSync(modelPath)) {
    throw new Error(
      "Laptop wake-word prerequisites are not ready:\n" +
      `  - OWW_CUSTOM_MODEL_PATH does not exist: ${modelPath}\n\n` +
      "Set OWW_CUSTOM_MODEL_PATH to a trained custom \"Hey Misty\" OpenWakeWord model artifact, " +
      "or unset it to use the bundled models\\hey_misty.onnx artifact.",
    );
  }
}

function checkOpenWakeWordResources(options, env) {
  const script = `
import json
import sys
from pathlib import Path

import openwakeword

resource_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
missing = [
    model
    for model in ${JSON.stringify(OPENWAKEWORD_RESOURCE_MODELS)}
    if not (resource_dir / model).exists()
]

print("MISTY_OWW_RESOURCE_CHECK=" + json.dumps({
    "resource_dir": str(resource_dir),
    "missing": missing,
}, sort_keys=True))
sys.exit(1 if missing else 0)
`;
  const result = run(options.python, ["-c", script], { cwd: ORCH_DIR, env, timeout: 30000 });
  if (result.ok) {
    return;
  }

  const payload = parseJsonMarker(result.stdout, "MISTY_OWW_RESOURCE_CHECK=");
  const missing = payload?.missing?.length ? payload.missing.join(", ") : "unknown";
  const resourceDir = payload?.resource_dir || "unknown";
  throw new Error(
    "Laptop wake-word prerequisites are not ready:\n" +
    `  - OpenWakeWord resource models are missing: ${missing}\n` +
    `  - Resource directory: ${resourceDir}\n\n` +
    "Download the bundled OpenWakeWord resource models, then retry startup:\n" +
    `  ${options.python} -c "from openwakeword.utils import download_models; download_models()"`,
  );
}

function preflightPythonRuntime(options, env) {
  warnIfLikelyArm64Python(options, env);
  checkPythonImports(options, env, ORCHESTRATION_PYTHON_MODULES, "Orchestration STT/TTS");
  checkKokoroAssets();
  if (envFlagEnabled(env.USE_LAPTOP_WAKE_WORD, true)) {
    checkPythonImports(options, env, WAKE_WORD_PYTHON_MODULES, "Laptop wake-word");
    checkOpenWakeWordResources(options, env);
    checkWakeWordModelPath(env);
  }
}

function checkKokoroAssets() {
  const missing = KOKORO_MODEL_FILES.filter((fileName) => !fs.existsSync(path.join(ORCH_DIR, fileName)));
  if (!missing.length) {
    return;
  }

  throw new Error(
    "Kokoro TTS prerequisites are not ready:\n" +
    missing.map((fileName) => `  - Missing ${path.join(ORCH_DIR, fileName)}`).join("\n") +
    "\n\nDownload the Kokoro model files into src\\windows-orchestration, then retry startup:\n" +
    "  cd src\\windows-orchestration\n" +
    "  curl.exe -L --fail --output kokoro-v1.0.int8.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx\n" +
    "  curl.exe -L --fail --output voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin\n\n" +
    "Without these files the service falls back to pyttsx3/SAPI5, which can be slow enough to trigger controller timeouts.",
  );
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

  if (!foundryIsRunning()) {
    console.log("Foundry Local: not running");
    delete state.foundry;
    return;
  }

  console.log("Foundry Local: stopping service");
  const result = run("foundry", ["service", "stop"], { timeout: 60000 });
  if (!result.ok) {
    console.warn(`Foundry Local: stop command reported a problem:\n${result.stderr || result.stdout}`);
  }
  delete state.foundry;
}

function configuredChatModel(env = {}) {
  const modelId = (env.CHAT_MODEL_ID || DEFAULTS.chatModelId).trim();
  const alias = (env.CHAT_MODEL_ALIAS || modelId.split("-instruct-")[0] || modelId).trim();
  const safeModelValue = /^[A-Za-z0-9._:+-]{1,200}$/;
  if (!safeModelValue.test(modelId) || !safeModelValue.test(alias)) {
    throw new Error("CHAT_MODEL_ID and CHAT_MODEL_ALIAS contain unsupported characters");
  }
  return { modelId, alias };
}

function chatModelIsLoaded(model) {
  const result = run("foundry", ["service", "ps"], { timeout: 30000 });
  return result.ok && (
    result.stdout.includes(model.modelId)
    || result.stdout.includes(model.alias)
  );
}

function chatModelIsCached(model) {
  const result = run("foundry", ["cache", "list"], { timeout: 30000 });
  return result.ok && (
    result.stdout.includes(model.modelId)
    || result.stdout.includes(model.alias)
  );
}

function ensureChatModelCached(model) {
  if (chatModelIsCached(model)) {
    return;
  }

  console.log("Foundry chat model: downloading configured model");
  const result = runInherited("foundry", [
    "model",
    "download",
    model.modelId,
  ], { timeout: 1800000 });

  if (result.status !== 0 && !chatModelIsCached(model)) {
    throw new Error(`Unable to download Foundry chat model; foundry exited with code ${result.status}`);
  }
}

function loadChatModel(state, options, env) {
  if (options.skipFoundry || options.skipModelLoad) {
    console.log("Foundry chat model: skipped");
    return;
  }

  const model = configuredChatModel(env);
  if (chatModelIsLoaded(model)) {
    console.log("Foundry chat model: configured model is already loaded");
    state.chatModel = {
      alias: model.alias,
      modelId: model.modelId,
      loadedAt: state.chatModel?.loadedAt || new Date().toISOString(),
    };
    return;
  }

  ensureChatModelCached(model);

  console.log("Foundry chat model: loading configured model");
  const result = run("foundry", [
    "model",
    "load",
    model.modelId,
    "--ttl",
    String(DEFAULTS.modelTtlSeconds),
  ], { timeout: 180000 });

  if (!result.ok && !chatModelIsLoaded(model)) {
    throw new Error(`Unable to load Foundry chat model:\n${result.stderr || result.stdout}`);
  }

  state.chatModel = {
    alias: model.alias,
    modelId: model.modelId,
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
      console.log("Misty robot: reachable");
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
      "Misty robot is not reachable; not starting the controller.\n" +
      `Check power, Wi-Fi, pass the correct address with --misty-ip <ip>, or allow network scanning.\n` +
      `Details: ${redactSensitive(error.message)}`,
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
  await ensureStartPrerequisites(options);

  const state = readState();
  const env = serviceEnv(options, state);
  preflightPythonRuntime(options, env);
  state.version = 1;
  state.startedAt = state.startedAt || new Date().toISOString();

  startFoundry(state, options);
  loadChatModel(state, options, env);

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
    ["POST", "/api/halt", undefined],
    ["POST", "/api/images/display", JSON.stringify({ FileName: "face_idle.gif", Alpha: 1 })],
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

async function conference(options) {
  // Conference Mode delegates to conference_mode.py with the remaining argv.
  // Usage: npx . conference <subcommand> [opts]
  // e.g.:  npx . conference dry-run --script ../../talks/20260710-2.md
  const subArgs = process.argv.slice(3); // everything after "conference"
  if (subArgs.length === 0 || subArgs[0] === "--help" || subArgs[0] === "-h") {
    console.log(`Usage: npx . conference <subcommand> [options]

Subcommands:
  dry-run [--script <path>]       Preview the ordered cue plan (no hardware)
  prepare [--script <path>]       Generate/import/reuse cue audio + manifest
  verify                          Confirm every cue has playable audio
  run [--auto|--no-auto]          Live interactive stage runner (requires Misty)

Options are passed directly to conference_mode.py.
`);
    return;
  }

  // For "run", stop the main controller service if it's running — it holds the
  // laptop mic wake word listener and will conflict with conference mode.
  const subcommand = subArgs[0];
  let restartRegularMode = false;
  if (subcommand === "run") {
    const state = readState();
    if (state.controller?.pid && isPidRunning(state.controller.pid)) {
      restartRegularMode = true;
      console.log(`Stopping main controller (PID ${state.controller.pid}) to free laptop mic...`);
      await gracefulControllerShutdown(state, options);
      await waitForPidExit(state.controller.pid, 10000);
      await stopPid("Misty controller", state.controller.pid, "SIGTERM");
      delete state.controller;
      writeState(state);
    }
  }

  selectCompatiblePython(options);
  const python = options.python;
  const script = path.join(ORCH_DIR, "conference_mode.py");

  if (!fs.existsSync(script)) {
    throw new Error(`Conference Mode module not found: ${script}`);
  }

  console.log(`Running: ${python} conference_mode.py ${subArgs.join(" ")}`);
  try {
    const result = runInherited(python, [script, ...subArgs], { cwd: ORCH_DIR });
    if (result.status !== 0) {
      process.exitCode = result.status ?? 1;
    }
  } finally {
    if (restartRegularMode) {
      console.log("Restoring regular mode after conference...");
      await start(options);
    }
  }
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
    case "conference":
      await conference(options);
      break;
    case "help":
      usage();
      break;
    default:
      throw new Error(`Unknown command: ${options.command}`);
  }
}

main().catch((error) => {
  console.error(redactSensitive(error.message));
  process.exitCode = 1;
});
