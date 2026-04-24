// FoundryLocalSkill.js
// Misty skill for wake word detection, recording, and Foundry Local inference orchestration.
// Latency SLO: p50 < 3s, p95 < 6s end-to-end.

// Configuration
const CONFIG = {
  WINDOWS_HOST: "http://192.168.1.100:5000", // Update with actual Windows companion IP:port
  WAKE_WORD: "Hey, Misty!",
  MAX_RECORDING_DURATION_MS: 10000, // 10s max recording
  RESPONSE_TIMEOUT_MS: 6000, // 6s max wait for Windows service
  SILENCE_THRESHOLD_MS: 800, // Stop recording after 800ms of silence
  MIN_RECORDING_MS: 500, // Minimum 500ms of audio before sending
};

// Fallback responses (pre-generated or cached WAV URIs)
const FALLBACK_RESPONSES = {
  SERVICE_UNREACHABLE: "I'm having trouble connecting to my thinking service. Please check the network.",
  TIMEOUT: "That took too long. I'm sorry, can you try again?",
  MODEL_LOAD_FAILURE: "My models aren't ready yet. Please wait and try again.",
  EMPTY_RESPONSE: "I didn't understand that. Could you say it again?",
};

// Global state
let isListening = false;
let recordingStartTime = 0;
let silenceStartTime = 0;

// ============================================================================
// MAIN SKILL LIFECYCLE
// ============================================================================

misty.Debug("FoundryLocalSkill initialized");

// On skill startup: register events and prepare for wake word detection
misty.RegisterEvent("KeyPhraseRecognized", "KeyPhraseRecognized", false);
misty.RegisterEvent("RecordingStatusChanged", "RecordingStatusChanged", false);

// Start listening for wake word immediately
startWakeWordDetection();

// ============================================================================
// WAKE WORD DETECTION
// ============================================================================

function startWakeWordDetection() {
  misty.Debug("Starting wake word detection");
  misty.StartKeyPhraseRecognition(true, CONFIG.WAKE_WORD, true);
  isListening = true;
}

misty.Events.KeyPhraseRecognized(function (data) {
  misty.Debug("Wake word detected: " + CONFIG.WAKE_WORD);
  
  if (!isListening) return;
  
  isListening = false;
  recordingStartTime = Date.now();
  silenceStartTime = Date.now();
  
  // Begin recording immediately after wake word
  misty.StartRecordingAudio("foundry_input.wav", true);
  misty.Debug("Recording started");
});

// ============================================================================
// RECORDING & SILENCE DETECTION
// ============================================================================

misty.Events.RecordingStatusChanged(function (data) {
  if (data.IsRecording) {
    misty.Debug("Recording in progress");
  } else {
    misty.Debug("Recording stopped");
    recordingStartTime = 0;
    handleRecordingComplete("foundry_input.wav");
  }
});

// Timeout-based recording stop (fallback if silence detection fails)
setTimeout(function () {
  if (recordingStartTime > 0) {
    let elapsed = Date.now() - recordingStartTime;
    if (elapsed > CONFIG.MAX_RECORDING_DURATION_MS) {
      misty.Debug("Max recording duration reached; stopping");
      misty.StopRecordingAudio();
    }
  }
}, 500); // Poll every 500ms

// ============================================================================
// ORCHESTRATION: RECORD -> TRANSCRIBE -> INFER -> SPEAK
// ============================================================================

function handleRecordingComplete(wavFileName) {
  let recordedDuration = Date.now() - recordingStartTime;
  
  if (recordedDuration < CONFIG.MIN_RECORDING_MS) {
    misty.Debug("Recording too short (" + recordedDuration + "ms); ignoring");
    rearmWakeWord();
    return;
  }
  
  misty.Debug("Recording complete (" + recordedDuration + "ms); sending to orchestration service");
  
  // Prepare multipart request with WAV file
  let skillRequest = {
    url: CONFIG.WINDOWS_HOST + "/api/orchestrate",
    method: "POST",
    contentType: "multipart/form-data",
    fileName: wavFileName,
    timeout: CONFIG.RESPONSE_TIMEOUT_MS,
  };
  
  misty.SendRequest(skillRequest, handleOrchestrationResponse);
}

function handleOrchestrationResponse(response) {
  if (!response.IsSucceeded) {
    misty.Debug("Orchestration request failed: " + response.ErrorMessage);
    playFallback(FALLBACK_RESPONSES.SERVICE_UNREACHABLE);
    rearmWakeWord();
    return;
  }
  
  let result = JSON.parse(response.Result);
  
  if (result.status === "error") {
    misty.Debug("Orchestration error: " + result.error);
    
    if (result.error === "model_load_failure") {
      playFallback(FALLBACK_RESPONSES.MODEL_LOAD_FAILURE);
    } else if (result.error === "timeout") {
      playFallback(FALLBACK_RESPONSES.TIMEOUT);
    } else {
      playFallback(FALLBACK_RESPONSES.EMPTY_RESPONSE);
    }
    
    rearmWakeWord();
    return;
  }
  
  if (!result.responseAudio) {
    misty.Debug("No audio in response");
    playFallback(FALLBACK_RESPONSES.EMPTY_RESPONSE);
    rearmWakeWord();
    return;
  }
  
  // Play response audio (Misty will handle the playback)
  misty.PlayAudio(result.responseAudio, 100, function (data) {
    if (data.IsSucceeded) {
      misty.Debug("Response audio playback completed");
    } else {
      misty.Debug("Response audio playback failed: " + data.ErrorMessage);
    }
    
    // Re-arm wake word detection after playback completes
    rearmWakeWord();
  });
}

// ============================================================================
// FALLBACK HANDLING
// ============================================================================

function playFallback(message) {
  misty.Debug("Playing fallback response: " + message);
  
  // For v1, use text-to-speech via orchestration service's TTS fallback
  let ttsRequest = {
    url: CONFIG.WINDOWS_HOST + "/api/fallback-tts",
    method: "POST",
    contentType: "application/json",
    body: JSON.stringify({ text: message }),
    timeout: 3000,
  };
  
  misty.SendRequest(ttsRequest, function (response) {
    if (response.IsSucceeded) {
      let ttsResult = JSON.parse(response.Result);
      if (ttsResult.audioUri) {
        misty.PlayAudio(ttsResult.audioUri, 100);
      }
    } else {
      misty.Debug("Fallback TTS failed");
    }
  });
}

// ============================================================================
// RE-ARM & LIFECYCLE
// ============================================================================

function rearmWakeWord() {
  misty.Debug("Re-arming wake word detection");
  startWakeWordDetection();
}

// On skill stop: clean up
misty.SkillStop(function () {
  misty.Debug("FoundryLocalSkill stopping");
  misty.StopKeyPhraseRecognition();
  misty.StopRecordingAudio();
});
