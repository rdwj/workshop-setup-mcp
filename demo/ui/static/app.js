"use strict";

// Conversation history sent to the agent each turn. Only user and
// assistant content are tracked; tool decisions / results are
// agent-internal details and don't round-trip through the client.
let messages = [];
let streaming = false;

// -- Settings panel state ---------------------------------------------------
let agentInfo = null;
let userTemperature = null;  // null = use server default
let userMaxTokens = null;
let userTopP = null;
let userTopK = null;
let userFreqPenalty = null;
let userPresencePenalty = null;
let userRepPenalty = null;
let userReasoningEffort = null;
let userApiBase = null;  // null = use default (direct vLLM)
let userResponsesApi = false;

async function loadAgentInfo() {
  try {
    const resp = await fetch("/v1/agent-info");
    if (!resp.ok) return;
    agentInfo = await resp.json();
    populateSettings();
  } catch (e) {
    console.warn("Could not load agent info:", e);
  }
}

function populateSettings() {
  if (!agentInfo) return;

  // Model name in header subtitle
  const modelNameEl = document.getElementById("model-name");
  if (modelNameEl && agentInfo.model) {
    // Show just the model's short name, e.g. "gpt-oss-20b" from "openai/RedHatAI/gpt-oss-20b"
    const parts = agentInfo.model.name.split("/");
    modelNameEl.textContent = parts[parts.length - 1];
  }

  // Model info section
  const modelInfoEl = document.getElementById("model-info");
  if (modelInfoEl && agentInfo.model) {
    modelInfoEl.innerHTML =
      '<div class="info-row"><span class="info-label">Name</span><span class="info-value">' +
      agentInfo.model.name.split("/").pop() + '</span></div>' +
      '<div class="info-row"><span class="info-label">Default Temperature</span><span class="info-value">' +
      agentInfo.model.temperature + '</span></div>' +
      '<div class="info-row"><span class="info-label">Default Max Tokens</span><span class="info-value">' +
      agentInfo.model.max_tokens + '</span></div>';
  }

  // Set parameter controls to defaults
  const tempSlider = document.getElementById("param-temperature");
  const tempValue = document.getElementById("temp-value");
  const maxTokensInput = document.getElementById("param-max-tokens");
  if (tempSlider && agentInfo.model) {
    tempSlider.value = agentInfo.model.temperature;
    tempValue.textContent = agentInfo.model.temperature;
  }
  if (maxTokensInput && agentInfo.model) {
    maxTokensInput.value = agentInfo.model.max_tokens;
  }

  const topPSlider = document.getElementById("param-top-p");
  const topPValue = document.getElementById("top-p-value");
  if (topPSlider) { topPValue.textContent = ""; }

  const freqSlider = document.getElementById("param-freq-penalty");
  const freqValue = document.getElementById("freq-penalty-value");
  if (freqSlider) { freqSlider.value = 0; freqValue.textContent = "0"; }

  const presSlider = document.getElementById("param-presence-penalty");
  const presValue = document.getElementById("presence-penalty-value");
  if (presSlider) { presSlider.value = 0; presValue.textContent = "0"; }

  const repSlider = document.getElementById("param-rep-penalty");
  const repValue = document.getElementById("rep-penalty-value");
  if (repSlider) { repSlider.value = 1; repValue.textContent = "1"; }

  // Backend selector
  const backendSelect = document.getElementById("param-backend");
  const responsesGroup = document.getElementById("responses-api-group");
  const responsesCheckbox = document.getElementById("param-responses-api");
  if (backendSelect && agentInfo.backends) {
    // Show/hide Responses API option based on LlamaStack availability
    if (agentInfo.backends.llamastack && agentInfo.backends.llamastack.responses_api) {
      responsesCheckbox.disabled = false;
      responsesGroup.querySelector(".param-hint").textContent = "";
    }
  }

  // System prompt
  const promptEl = document.getElementById("system-prompt");
  if (promptEl) {
    promptEl.textContent = agentInfo.system_prompt || "(none)";
  }

  // Tools list
  const toolsListEl = document.getElementById("tools-list");
  const toolsCountEl = document.getElementById("tools-count");
  if (toolsListEl && agentInfo.tools) {
    toolsCountEl.textContent = "(" + agentInfo.tools.length + ")";
    toolsListEl.innerHTML = "";
    for (const tool of agentInfo.tools) {
      const details = document.createElement("details");
      details.className = "tool-info";
      const summary = document.createElement("summary");
      summary.textContent = tool.name;
      details.appendChild(summary);
      const desc = document.createElement("p");
      desc.className = "tool-description";
      desc.textContent = tool.description;
      details.appendChild(desc);
      if (tool.parameters && tool.parameters.properties) {
        const params = document.createElement("div");
        params.className = "tool-params";
        const keys = Object.keys(tool.parameters.properties);
        params.textContent = "Parameters: " + keys.join(", ");
        details.appendChild(params);
      }
      toolsListEl.appendChild(details);
    }
  }
}

function setupSettings() {
  const btn = document.getElementById("settings-btn");
  const panel = document.getElementById("settings-panel");
  const overlay = document.getElementById("settings-overlay");
  const closeBtn = document.getElementById("settings-close");

  function toggle() {
    const open = panel.classList.toggle("open");
    overlay.classList.toggle("open", open);
  }

  if (btn) btn.addEventListener("click", toggle);
  if (closeBtn) closeBtn.addEventListener("click", toggle);
  if (overlay) overlay.addEventListener("click", toggle);

  // Temperature slider
  const tempSlider = document.getElementById("param-temperature");
  const tempValue = document.getElementById("temp-value");
  if (tempSlider) {
    tempSlider.addEventListener("input", function () {
      tempValue.textContent = this.value;
      userTemperature = parseFloat(this.value);
    });
  }

  // Max tokens input
  const maxTokensInput = document.getElementById("param-max-tokens");
  if (maxTokensInput) {
    maxTokensInput.addEventListener("change", function () {
      userMaxTokens = parseInt(this.value, 10) || null;
    });
  }

  // Top P slider
  const topPSlider = document.getElementById("param-top-p");
  const topPValue = document.getElementById("top-p-value");
  if (topPSlider) {
    topPSlider.addEventListener("input", function () {
      topPValue.textContent = this.value;
      userTopP = parseFloat(this.value) || null;
    });
  }

  // Top K
  const topKInput = document.getElementById("param-top-k");
  if (topKInput) {
    topKInput.addEventListener("change", function () {
      const v = parseInt(this.value, 10);
      userTopK = (v > 0) ? v : null;
    });
  }

  // Frequency penalty
  const freqSlider = document.getElementById("param-freq-penalty");
  const freqValue = document.getElementById("freq-penalty-value");
  if (freqSlider) {
    freqSlider.addEventListener("input", function () {
      freqValue.textContent = this.value;
      userFreqPenalty = parseFloat(this.value) || null;
    });
  }

  // Presence penalty
  const presSlider = document.getElementById("param-presence-penalty");
  const presValue = document.getElementById("presence-penalty-value");
  if (presSlider) {
    presSlider.addEventListener("input", function () {
      presValue.textContent = this.value;
      userPresencePenalty = parseFloat(this.value) || null;
    });
  }

  // Repetition penalty
  const repSlider = document.getElementById("param-rep-penalty");
  const repValue = document.getElementById("rep-penalty-value");
  if (repSlider) {
    repSlider.addEventListener("input", function () {
      repValue.textContent = this.value;
      const v = parseFloat(this.value);
      userRepPenalty = (v !== 1.0) ? v : null;
    });
  }

  // Reasoning effort
  const reasoningSelect = document.getElementById("param-reasoning");
  if (reasoningSelect) {
    reasoningSelect.addEventListener("change", function () {
      userReasoningEffort = this.value || null;
    });
  }

  // Backend selector
  const backendSelect = document.getElementById("param-backend");
  const responsesGroup = document.getElementById("responses-api-group");
  if (backendSelect) {
    backendSelect.addEventListener("change", function () {
      const val = this.value;
      if (val === "llamastack" && agentInfo && agentInfo.backends && agentInfo.backends.llamastack) {
        userApiBase = agentInfo.backends.llamastack.api_base;
        responsesGroup.style.display = "block";
      } else {
        userApiBase = null;
        responsesGroup.style.display = "none";
        // Uncheck Responses API when switching away from LlamaStack
        const cb = document.getElementById("param-responses-api");
        if (cb) { cb.checked = false; userResponsesApi = false; }
      }
    });
  }

  // Responses API checkbox
  const responsesCheckbox = document.getElementById("param-responses-api");
  if (responsesCheckbox) {
    responsesCheckbox.addEventListener("change", function () {
      userResponsesApi = this.checked;
    });
  }
}

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const chatEl = document.getElementById("chat");
const attachBtn = document.getElementById("attach-btn");
const fileInputEl = document.getElementById("file-input");
const attachmentsEl = document.getElementById("attachments");
const dropOverlayEl = document.getElementById("drop-overlay");
const inputForm = document.getElementById("input-form");

// -- File-upload state ------------------------------------------------------
// Each entry: {id, file, file_id, status: 'uploading'|'ready'|'failed',
//              progress: 0..1, error?, xhr?}.  `id` is a client-only key
//              used to keep DOM nodes in sync; `file_id` is what the
//              gateway returns and is what we send on the chat request.
const attachments = [];
let nextAttachmentId = 1;
let maxFileBytes = 25 * 1024 * 1024; // mirrors gateway default; replaced from /api/config
let allowedMimePatterns = null;       // null = allow-all (server is authoritative)
let dragDepth = 0;                    // dragenter/leave fire on each child; depth-count
                                      // them so the overlay only hides on the final leave

async function loadUiConfig() {
  try {
    const resp = await fetch("/api/config");
    if (!resp.ok) return;
    const cfg = await resp.json();
    if (typeof cfg.maxFileBytes === "number" && cfg.maxFileBytes > 0) {
      maxFileBytes = cfg.maxFileBytes;
    }
    if (Array.isArray(cfg.allowedMime) && cfg.allowedMime.length > 0) {
      allowedMimePatterns = cfg.allowedMime.map(s => String(s).toLowerCase());
    }
  } catch (e) {
    console.warn("Could not load UI config:", e);
  }
}

async function init() {
  setupSettings();
  await loadUiConfig();
  loadAgentInfo();
  setupFileUploads();
}

function appendMessage(role, content) {
  const el = document.createElement("div");
  el.classList.add("message", role);
  el.innerHTML = renderContent(content);
  messagesEl.appendChild(el);
  scrollToBottom();
  return el;
}

function appendError(text) {
  const el = document.createElement("div");
  el.classList.add("message", "error");
  el.textContent = text;
  messagesEl.appendChild(el);
  scrollToBottom();
}

function scrollToBottom() {
  chatEl.scrollTop = chatEl.scrollHeight;
}

function renderContent(text) {
  // Phase 0: Render LaTeX blocks directly to HTML via KaTeX (if loaded).
  // We do this before HTML escaping so the TeX source isn't mangled.
  // The rendered HTML is stored as a placeholder and restored after
  // all markdown processing.
  var latexBlocks = [];
  if (typeof katex !== "undefined") {
    // Display math: \[...\]
    text = text.replace(/\\\[([\s\S]*?)\\\]/g, function (_, tex) {
      var idx = latexBlocks.length;
      var html;
      try {
        html = katex.renderToString(tex.trim(), { displayMode: true, throwOnError: false });
        html = '<div class="katex-display-block">' + html + '</div>';
      } catch (e) {
        html = "\\[" + tex + "\\]";
      }
      latexBlocks.push(html);
      return "\x00LATEX" + idx + "\x00";
    });
    // Inline math: \(...\)
    text = text.replace(/\\\(([\s\S]*?)\\\)/g, function (_, tex) {
      var idx = latexBlocks.length;
      var html;
      try {
        html = katex.renderToString(tex.trim(), { displayMode: false, throwOnError: false });
      } catch (e) {
        html = "\\(" + tex + "\\)";
      }
      latexBlocks.push(html);
      return "\x00LATEX" + idx + "\x00";
    });
  }

  // Escape HTML first
  var safe = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Phase 1: Extract code blocks so they aren't parsed for markdown.
  // Replace each code block with a placeholder token.
  var codeBlocks = [];
  safe = safe.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
    var idx = codeBlocks.length;
    codeBlocks.push("<pre><code>" + code.trimEnd() + "</code></pre>");
    return "\x00CODEBLOCK" + idx + "\x00";
  });

  // Phase 2: Extract inline code spans.
  var inlineCode = [];
  safe = safe.replace(/`([^`]+)`/g, function (_, code) {
    var idx = inlineCode.length;
    inlineCode.push("<code>" + code + "</code>");
    return "\x00INLINE" + idx + "\x00";
  });

  // Phase 3: Process block-level markdown on each line.
  var paragraphs = safe.split(/\n\n+/);
  var rendered = [];

  for (var p = 0; p < paragraphs.length; p++) {
    var para = paragraphs[p];

    if (/^\x00CODEBLOCK\d+\x00$/.test(para.trim())) {
      rendered.push(para.trim());
      continue;
    }

    var lines = para.split("\n");
    var out = [];
    var listType = null;
    var listItems = [];

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      var ulMatch = /^(\-|\*) (.+)$/.exec(line);
      var olMatch = /^(\d+)\. (.+)$/.exec(line);

      if (ulMatch) {
        if (listType && listType !== "ul") {
          out.push("<" + listType + ">" + listItems.join("") + "</" + listType + ">");
          listItems = [];
        }
        listType = "ul";
        listItems.push("<li>" + processInline(ulMatch[2]) + "</li>");
        continue;
      }

      if (olMatch) {
        if (listType && listType !== "ol") {
          out.push("<" + listType + ">" + listItems.join("") + "</" + listType + ">");
          listItems = [];
        }
        listType = "ol";
        listItems.push("<li>" + processInline(olMatch[2]) + "</li>");
        continue;
      }

      if (listType) {
        out.push("<" + listType + ">" + listItems.join("") + "</" + listType + ">");
        listType = null;
        listItems = [];
      }

      var headerMatch = /^(#{1,3}) (.+)$/.exec(line);
      if (headerMatch) {
        var level = headerMatch[1].length;
        out.push("<h" + level + ">" + processInline(headerMatch[2]) + "</h" + level + ">");
        continue;
      }

      out.push(processInline(line));
    }

    if (listType) {
      out.push("<" + listType + ">" + listItems.join("") + "</" + listType + ">");
    }

    var joined = "";
    for (var j = 0; j < out.length; j++) {
      if (joined && !isBlockElement(out[j]) && !isBlockElement(out[j - 1 >= 0 ? j - 1 : 0])) {
        joined += "<br>";
      } else if (joined) {
        // no separator needed between block elements
      }
      joined += out[j];
    }

    rendered.push(joined);
  }

  var result;
  if (rendered.length === 1) {
    result = rendered[0];
  } else {
    var parts = [];
    for (var k = 0; k < rendered.length; k++) {
      if (isBlockElement(rendered[k])) {
        parts.push(rendered[k]);
      } else if (rendered[k].trim()) {
        parts.push("<p>" + rendered[k] + "</p>");
      }
    }
    result = parts.join("");
  }

  result = result.replace(/\x00CODEBLOCK(\d+)\x00/g, function (_, idx) {
    return codeBlocks[parseInt(idx, 10)];
  });
  result = result.replace(/\x00INLINE(\d+)\x00/g, function (_, idx) {
    return inlineCode[parseInt(idx, 10)];
  });
  result = result.replace(/\x00LATEX(\d+)\x00/g, function (_, idx) {
    return latexBlocks[parseInt(idx, 10)];
  });

  return result;
}

function processInline(text) {
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
  return text;
}

function isBlockElement(html) {
  if (!html) return false;
  return /^<(h[1-3]|ul|ol|pre|p|blockquote)[\s>]/.test(html)
      || /^\x00CODEBLOCK\d+\x00$/.test(html);
}

function setStreaming(value) {
  streaming = value;
  inputEl.disabled = value;
  if (attachBtn) attachBtn.disabled = value;
  // sendBtn state is owned by updateSendButton (it considers both
  // streaming and in-flight uploads). When the upload state machine
  // hasn't been wired yet (early init), fall back to direct disable
  // so the button reflects streaming immediately.
  if (typeof updateSendButton === "function") {
    updateSendButton();
  } else {
    sendBtn.disabled = value;
  }
}

// -- Per-message stream renderer --------------------------------------------
// Encapsulates the four-phase rendering (thinking / tool calls / response /
// done) for a single assistant turn. Constructed when the user submits;
// fed delta events as they arrive; finalized on stream completion.

function createStreamRenderer(assistantEl) {
  let thinkingPanel = null;     // <details> element, lazy-created
  let thinkingContent = null;   // <div> inside thinkingPanel
  let toolCallsContainer = null;
  let responseEl = null;
  let responseText = "";
  let responseIndicator = null;
  let streamMetrics = null;     // server-sent metrics object
  let rawChunks = [];
  let traceId = null;           // populated from usage chunk or response header

  // Per-tool state: index -> { pillEl, nameEl, statusEl, argsEl, resultEl, args, name, callId }
  const toolCalls = new Map();

  function ensureThinkingPanel() {
    if (thinkingPanel) return;
    thinkingPanel = document.createElement("details");
    thinkingPanel.className = "thinking-panel";
    // Collapsed by default per design.
    const summary = document.createElement("summary");
    summary.textContent = "Thinking\u2026";
    thinkingPanel.appendChild(summary);
    thinkingContent = document.createElement("div");
    thinkingContent.className = "thinking-content";
    thinkingPanel.appendChild(thinkingContent);
    assistantEl.appendChild(thinkingPanel);
  }

  function ensureToolCallsContainer() {
    if (toolCallsContainer) return;
    toolCallsContainer = document.createElement("div");
    toolCallsContainer.className = "tool-calls";
    assistantEl.appendChild(toolCallsContainer);
  }

  function ensureResponseEl() {
    if (responseEl) return;
    responseEl = document.createElement("div");
    responseEl.className = "response-content";
    responseIndicator = document.createElement("span");
    responseIndicator.className = "streaming-indicator";
    assistantEl.appendChild(responseEl);
    assistantEl.appendChild(responseIndicator);
  }

  function startToolCall(index, callId, name) {
    ensureToolCallsContainer();
    const pill = document.createElement("details");
    pill.className = "tool-call running";

    const header = document.createElement("summary");
    header.className = "tool-header";
    const icon = document.createElement("span");
    icon.className = "tool-icon";
    icon.textContent = "\u2699";
    const nameEl = document.createElement("span");
    nameEl.className = "tool-name";
    nameEl.textContent = name;
    const statusEl = document.createElement("span");
    statusEl.className = "tool-status";
    statusEl.textContent = "running";
    header.appendChild(icon);
    header.appendChild(nameEl);
    header.appendChild(statusEl);

    const argsEl = document.createElement("pre");
    argsEl.className = "tool-args";
    argsEl.textContent = "";

    const resultEl = document.createElement("div");
    resultEl.className = "tool-result";
    resultEl.style.display = "none";

    pill.appendChild(header);
    pill.appendChild(argsEl);
    pill.appendChild(resultEl);
    toolCallsContainer.appendChild(pill);

    toolCalls.set(index, {
      pillEl: pill,
      nameEl,
      statusEl,
      argsEl,
      resultEl,
      args: "",
      name,
      callId,
    });
    scrollToBottom();
  }

  function appendToolArgs(index, argsDelta) {
    const tc = toolCalls.get(index);
    if (!tc) return;
    tc.args += argsDelta;
    tc.argsEl.textContent = tc.args;
    scrollToBottom();
  }

  function completeToolCall(callId, content, isError) {
    // Find the matching tool call by call_id.
    let match = null;
    for (const tc of toolCalls.values()) {
      if (tc.callId === callId) { match = tc; break; }
    }
    if (!match) return;
    match.pillEl.classList.remove("running");
    match.pillEl.classList.add(isError ? "error" : "done");
    match.statusEl.textContent = isError ? "error" : "done";
    match.resultEl.style.display = "block";
    match.resultEl.textContent = content;
    scrollToBottom();
  }

  function appendThinking(text) {
    ensureThinkingPanel();
    thinkingContent.textContent += text;
    scrollToBottom();
  }

  function appendContent(text) {
    ensureResponseEl();
    responseText += text;
    responseEl.innerHTML = renderContent(responseText);
    // Re-attach indicator (re-render replaced it)
    if (responseIndicator && !responseEl.contains(responseIndicator)) {
      // indicator lives as sibling, not inside responseEl, so this is OK
    }
    scrollToBottom();
  }

  function setMetrics(metrics, usage) {
    streamMetrics = { ...metrics, usage };
  }

  function finalize(clientTtft) {
    // Remove the streaming cursor.
    if (responseIndicator && responseIndicator.parentNode) {
      responseIndicator.parentNode.removeChild(responseIndicator);
    }
    // Mark thinking panel as no longer pulsing.
    if (thinkingPanel) {
      thinkingPanel.classList.add("done");
    }
    // If there was no response content (e.g. tool-only turn), make
    // that visible rather than leaving a blank message.
    if (!responseText.trim() && !toolCalls.size && !thinkingPanel) {
      assistantEl.textContent = "(no response)";
    }

    // Raw API response button -- always shown
    const rawBtn = document.createElement("button");
    rawBtn.className = "raw-response-btn";
    rawBtn.textContent = "View Raw Response";
    rawBtn.title = "View full API response chunks";
    rawBtn.addEventListener("click", function () {
      showRawResponse(rawChunks);
    });
    assistantEl.appendChild(rawBtn);

    // Render metrics bar if we have data.
    const m = streamMetrics;
    if (!m) return;

    const bar = document.createElement("div");
    bar.className = "stream-metrics";

    const items = [];
    const ttft = m.time_to_first_content ?? clientTtft;
    if (ttft != null) items.push(["TTFT", ttft.toFixed(1) + "s"]);
    if (m.time_to_first_reasoning != null) items.push(["Thinking", m.time_to_first_reasoning.toFixed(1) + "s"]);
    if (m.total_time != null) items.push(["Total", m.total_time.toFixed(1) + "s"]);
    if (m.usage && m.usage.total_tokens != null) items.push(["Tokens", m.usage.total_tokens.toLocaleString()]);
    if (m.model_calls != null) items.push(["Model calls", m.model_calls]);
    if (m.tool_calls != null) items.push(["Tool calls", m.tool_calls]);
    if (m.inter_token_latencies && m.inter_token_latencies.length > 0) {
      const sum = m.inter_token_latencies.reduce(function (a, b) { return a + b; }, 0);
      const avg = sum / m.inter_token_latencies.length;
      items.push(["Avg ITL", (avg * 1000).toFixed(0) + "ms"]);
    }

    for (var i = 0; i < items.length; i++) {
      var span = document.createElement("span");
      span.className = "metric";
      span.innerHTML = '<span class="metric-label">' + items[i][0] + '</span>'
        + '<span class="metric-value">' + items[i][1] + '</span>';
      bar.appendChild(span);
    }

    assistantEl.appendChild(bar);

    // Feedback controls (only when we have a trace_id to attach to)
    if (traceId) {
      assistantEl.appendChild(createFeedbackControls(traceId));
    }
  }

  function pushRawChunk(chunk) {
    rawChunks.push(chunk);
  }

  function setTraceId(id) {
    if (id) traceId = id;
  }

  return {
    setTraceId,
    handleDelta(delta) {
      // Reasoning ("thinking") phase
      if (delta.reasoning_content) {
        appendThinking(delta.reasoning_content);
      }
      // Tool call deltas (decisions made by the model)
      if (delta.tool_calls && Array.isArray(delta.tool_calls)) {
        for (const tc of delta.tool_calls) {
          const idx = tc.index ?? 0;
          // First delta for a new tool call brings id+name.
          // Check by call_id, not just index — a second model
          // iteration reuses index 0 with a different call_id.
          const existing = toolCalls.get(idx);
          if (tc.id && (!existing || existing.callId !== tc.id)) {
            const name = (tc.function && tc.function.name) || "tool";
            startToolCall(idx, tc.id, name);
            // Some chunks include initial args along with id+name.
            const initialArgs = tc.function && tc.function.arguments;
            if (initialArgs) appendToolArgs(idx, initialArgs);
          } else if (tc.function && tc.function.arguments) {
            appendToolArgs(idx, tc.function.arguments);
          }
        }
      }
      // Tool execution result (role:"tool" message in the stream)
      if (delta.role === "tool" && delta.tool_call_id) {
        completeToolCall(delta.tool_call_id, delta.content || "", false);
      }
      // Assistant content (the user-visible response)
      if (delta.content && delta.role !== "tool") {
        appendContent(delta.content);
      }
      // delta.role === "assistant" with no other fields is a role
      // announcement we can safely ignore.
    },
    finalize,
    setMetrics,
    getResponseText: () => responseText,
    pushRawChunk,
    getRawChunks: () => rawChunks,
  };
}

// -- Feedback controls -----------------------------------------------------
// Hover-revealed thumbs-up/-down on completed assistant messages. Thumbs-up
// records a positive rating immediately; thumbs-down opens a modal so the
// user can pick a category and (optionally) leave a free-text comment.
//
// State per message is local: once a rating is submitted, both icons stay
// visible but the chosen one is filled and further clicks are ignored.

const FEEDBACK_CATEGORIES = [
  "Inaccurate",
  "Not helpful",
  "Harmful",
  "Too long",
  "Other",
];

function createFeedbackControls(traceId) {
  const wrap = document.createElement("div");
  wrap.className = "feedback-controls";
  wrap.dataset.state = "idle"; // idle | submitted

  const upBtn = document.createElement("button");
  upBtn.type = "button";
  upBtn.className = "feedback-btn feedback-up";
  upBtn.title = "Good response";
  upBtn.setAttribute("aria-label", "Thumbs up");
  upBtn.innerHTML = THUMB_UP_SVG;

  const downBtn = document.createElement("button");
  downBtn.type = "button";
  downBtn.className = "feedback-btn feedback-down";
  downBtn.title = "Bad response";
  downBtn.setAttribute("aria-label", "Thumbs down");
  downBtn.innerHTML = THUMB_DOWN_SVG;

  // Inline note rendered next to the thumbs after a thumbs-down submission.
  // Click it to re-open the modal and edit the existing record.
  const noteEl = document.createElement("button");
  noteEl.type = "button";
  noteEl.className = "feedback-note";
  noteEl.hidden = true;
  noteEl.title = "Edit feedback";

  // Per-message state. feedbackId is null until the first POST returns,
  // then every subsequent edit PATCHes that record.
  const state = {
    feedbackId: null,
    rating: null,        // 1 | -1 | null
    category: null,
    comment: null,
  };

  function persistChange(rating, category, comment) {
    const previous = { ...state };
    optimisticApply(wrap, noteEl, state, rating, category, comment);
    sendFeedbackUpdate(traceId, state, rating, category, comment).catch(function (err) {
      // Roll back on failure.
      Object.assign(state, previous);
      optimisticApply(wrap, noteEl, state, previous.rating, previous.category, previous.comment);
      appendError("Could not save feedback: " + err.message);
    });
  }

  upBtn.addEventListener("click", function () {
    if (state.rating === 1) return; // already up; nothing to do
    persistChange(1, null, null);
  });

  downBtn.addEventListener("click", function () {
    showFeedbackModal({
      category: state.category,
      comment: state.comment,
    }, function (category, comment) {
      persistChange(-1, category, comment);
    });
  });

  noteEl.addEventListener("click", function () {
    showFeedbackModal({
      category: state.category,
      comment: state.comment,
    }, function (category, comment) {
      persistChange(-1, category, comment);
    });
  });

  wrap.appendChild(upBtn);
  wrap.appendChild(downBtn);
  wrap.appendChild(noteEl);
  return wrap;
}

// Update the local state object and sync the DOM in one place.
function optimisticApply(wrap, noteEl, state, rating, category, comment) {
  state.rating = rating;
  state.category = category;
  state.comment = comment;
  if (rating === 1) {
    wrap.dataset.state = "submitted";
    wrap.dataset.choice = "up";
    noteEl.hidden = true;
    noteEl.textContent = "";
  } else if (rating === -1) {
    wrap.dataset.state = "submitted";
    wrap.dataset.choice = "down";
    noteEl.textContent = formatNote(category, comment);
    noteEl.hidden = false;
  } else {
    wrap.dataset.state = "idle";
    delete wrap.dataset.choice;
    noteEl.hidden = true;
    noteEl.textContent = "";
  }
}

function formatNote(category, comment) {
  const cat = category ? "[" + category + "]" : "";
  const txt = comment ? " " + comment : "";
  return (cat + txt).trim() || "(no details)";
}

// First call POSTs and stores the new feedback_id; later calls PATCH.
async function sendFeedbackUpdate(traceId, state, rating, category, comment) {
  if (state.feedbackId) {
    const updated = await patchFeedback(state.feedbackId, rating, category, comment);
    return updated;
  }
  const created = await submitFeedback(traceId, rating, category, comment);
  state.feedbackId = created.feedback_id;
  return created;
}

// Hand-aligned 16×16 paths, in the same outline style as the rest of
// the UI's icons. Filled state is applied via CSS (`fill: currentColor`)
// so the same path serves both states.
const THUMB_UP_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
  + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M7 10v12"></path>'
  + '<path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H7V10l4.06-9a1.93 1.93 0 0 1 1.78 1.04 5.93 5.93 0 0 1 .98 4.5z"></path>'
  + '</svg>';

const THUMB_DOWN_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
  + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M17 14V2"></path>'
  + '<path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H17v12l-4.06 9a1.93 1.93 0 0 1-1.78-1.04 5.93 5.93 0 0 1-.98-4.5z"></path>'
  + '</svg>';

function markSubmitted(wrap, which) {
  wrap.dataset.state = "submitted";
  wrap.dataset.choice = which;
}

function restorePrevious(wrap, previous) {
  if (previous) {
    wrap.dataset.state = "submitted";
    wrap.dataset.choice = previous;
  } else {
    wrap.dataset.state = "idle";
    delete wrap.dataset.choice;
  }
}

// Encode the optional UI category as a bracketed prefix on `comment` so
// the backend's existing schema captures it without a first-class column.
function encodeComment(category, comment) {
  if (category && comment) return "[" + category + "] " + comment;
  if (category) return "[" + category + "]";
  return comment || null;
}

async function submitFeedback(traceId, rating, category, comment) {
  const body = { trace_id: traceId, rating: rating };
  const c = encodeComment(category, comment);
  if (c !== null) body.comment = c;

  const resp = await fetch("/v1/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(function () { return ""; });
    throw new Error("HTTP " + resp.status + (text ? " — " + text : ""));
  }
  return resp.json();
}

// PATCH an existing record. The backend treats `null` fields as
// "leave unchanged"; we always send rating + comment because the user
// may flip either or both in a single edit.
async function patchFeedback(feedbackId, rating, category, comment) {
  const body = {
    rating: rating,
    // Send "" rather than null when there's no comment so the field
    // gets explicitly cleared on the backend (e.g. flipping from down
    // back to up — though our UI currently doesn't expose that flow).
    comment: encodeComment(category, comment) || "",
  };
  const resp = await fetch("/v1/feedback/" + encodeURIComponent(feedbackId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(function () { return ""; });
    throw new Error("HTTP " + resp.status + (text ? " — " + text : ""));
  }
  return resp.json();
}

function showFeedbackModal(prefill, onSubmit) {
  // Backwards-compat: allow `showFeedbackModal(onSubmit)` calls.
  if (typeof prefill === "function") {
    onSubmit = prefill;
    prefill = {};
  }
  prefill = prefill || {};

  let modal = document.getElementById("feedback-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "feedback-modal";
    modal.className = "feedback-modal";
    const optionsHtml = FEEDBACK_CATEGORIES.map(function (c) {
      return '<label class="feedback-option">'
        + '<input type="radio" name="feedback-category" value="' + c + '"> ' + c
        + '</label>';
    }).join("");
    modal.innerHTML = '<div class="feedback-modal-content">'
      + '<div class="feedback-modal-header">'
      +   '<h3>What was wrong?</h3>'
      +   '<button class="feedback-modal-close" aria-label="Close">&times;</button>'
      + '</div>'
      + '<div class="feedback-modal-body">'
      +   '<div class="feedback-options">' + optionsHtml + '</div>'
      +   '<textarea class="feedback-comment" placeholder="Optional details..." rows="3"></textarea>'
      + '</div>'
      + '<div class="feedback-modal-footer">'
      +   '<button class="feedback-cancel">Cancel</button>'
      +   '<button class="feedback-submit">Submit</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(modal);
    modal.addEventListener("click", function (e) {
      if (e.target === modal) closeFeedbackModal();
    });
    modal.querySelector(".feedback-modal-close")
      .addEventListener("click", closeFeedbackModal);
    modal.querySelector(".feedback-cancel")
      .addEventListener("click", closeFeedbackModal);
  }

  // Pre-populate with the caller's last submission (or defaults).
  const radios = modal.querySelectorAll('input[name="feedback-category"]');
  const wantCategory = prefill.category || FEEDBACK_CATEGORIES[0];
  let matched = false;
  radios.forEach(function (r) {
    if (r.value === wantCategory) {
      r.checked = true;
      matched = true;
    } else {
      r.checked = false;
    }
  });
  if (!matched && radios.length) radios[0].checked = true;
  modal.querySelector(".feedback-comment").value = prefill.comment || "";

  // Re-bind submit (closure over the new callback).
  const submitBtn = modal.querySelector(".feedback-submit");
  const newSubmit = submitBtn.cloneNode(true);
  submitBtn.parentNode.replaceChild(newSubmit, submitBtn);
  newSubmit.addEventListener("click", function () {
    const checked = modal.querySelector('input[name="feedback-category"]:checked');
    const category = checked ? checked.value : null;
    const comment = modal.querySelector(".feedback-comment").value.trim() || null;
    closeFeedbackModal();
    onSubmit(category, comment);
  });

  modal.classList.add("open");
  modal.querySelector(".feedback-comment").focus();
}

function closeFeedbackModal() {
  const modal = document.getElementById("feedback-modal");
  if (modal) modal.classList.remove("open");
}

function showRawResponse(chunks) {
  let modal = document.getElementById("raw-response-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "raw-response-modal";
    modal.className = "raw-modal";
    modal.innerHTML = '<div class="raw-modal-content">' +
      '<div class="raw-modal-header"><h3>Raw API Response</h3><button class="raw-modal-close">&times;</button></div>' +
      '<pre class="raw-modal-body"></pre></div>';
    document.body.appendChild(modal);
    modal.querySelector(".raw-modal-close").addEventListener("click", function () {
      modal.classList.remove("open");
    });
    modal.addEventListener("click", function (e) {
      if (e.target === modal) modal.classList.remove("open");
    });
  }
  const body = modal.querySelector(".raw-modal-body");
  body.textContent = JSON.stringify(chunks, null, 2);
  modal.classList.add("open");
}

// -- File uploads -----------------------------------------------------------

function setupFileUploads() {
  attachBtn.addEventListener("click", () => {
    if (streaming) return;
    fileInputEl.click();
  });

  fileInputEl.addEventListener("change", () => {
    handleFiles(fileInputEl.files);
    // Reset the input so selecting the same file again still fires `change`.
    fileInputEl.value = "";
  });

  // Paste support — clipboard items can include images and arbitrary
  // files (Finder copy-on-Mac copies the file itself, not just the
  // path).  Use clipboardData.files when available; otherwise walk
  // items[] looking for kind === "file".
  inputEl.addEventListener("paste", (e) => {
    if (streaming) return;
    const dt = e.clipboardData;
    if (!dt) return;
    const files = pasteFiles(dt);
    if (files.length > 0) {
      e.preventDefault();
      handleFiles(files);
    }
  });

  // Drag-and-drop — listen on the form so the overlay covers the
  // input row + chips.  The overlay itself has pointer-events:none so
  // it doesn't swallow the drop event.
  inputForm.addEventListener("dragenter", (e) => {
    if (streaming) return;
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    dragDepth++;
    dropOverlayEl.hidden = false;
  });
  inputForm.addEventListener("dragover", (e) => {
    if (streaming) return;
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  inputForm.addEventListener("dragleave", () => {
    if (dragDepth > 0) dragDepth--;
    if (dragDepth === 0) dropOverlayEl.hidden = true;
  });
  inputForm.addEventListener("drop", (e) => {
    dragDepth = 0;
    dropOverlayEl.hidden = true;
    if (streaming) return;
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    handleFiles(e.dataTransfer.files);
  });

  // Block the browser's default "open this file" behavior anywhere
  // outside the drop zone, so a missed drop doesn't navigate away
  // from the chat.
  window.addEventListener("dragover", (e) => {
    if (hasFiles(e.dataTransfer)) e.preventDefault();
  });
  window.addEventListener("drop", (e) => {
    if (hasFiles(e.dataTransfer) && e.target !== dropOverlayEl
        && !inputForm.contains(e.target)) {
      e.preventDefault();
    }
  });
}

function hasFiles(dt) {
  if (!dt) return false;
  // types is a DOMStringList — check via for-of so we don't trip on
  // browser quirks where indexOf is missing.
  for (const t of dt.types || []) {
    if (t === "Files") return true;
  }
  return false;
}

function pasteFiles(dt) {
  const out = [];
  if (dt.files && dt.files.length > 0) {
    for (const f of dt.files) out.push(f);
    if (out.length > 0) return out;
  }
  if (dt.items) {
    for (const item of dt.items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) out.push(f);
      }
    }
  }
  return out;
}

function handleFiles(fileList) {
  for (const file of fileList) {
    addAttachment(file);
  }
}

function addAttachment(file) {
  const att = {
    id: "att_" + (nextAttachmentId++),
    file: file,
    file_id: null,
    status: "uploading",
    progress: 0,
    error: null,
    xhr: null,
  };

  // Pre-flight client-side validation. Cheap checks so the user
  // doesn't watch a giant file upload only to be 413'd.
  if (maxFileBytes > 0 && file.size > maxFileBytes) {
    att.status = "failed";
    att.error = "File exceeds max size (" + formatBytes(maxFileBytes) + ")";
  } else if (allowedMimePatterns && !mimeAllowed(file.type, allowedMimePatterns)) {
    att.status = "failed";
    att.error = "File type not allowed";
  }

  attachments.push(att);
  renderAttachments();

  if (att.status === "uploading") {
    uploadAttachment(att);
  }
  updateSendButton();
}

function mimeAllowed(ct, patterns) {
  const norm = (ct || "").toLowerCase().split(";")[0].trim();
  if (!norm) return false;
  for (const p of patterns) {
    if (p === norm) return true;
    if (p.endsWith("/*") && norm.startsWith(p.slice(0, -1))) return true;
  }
  return false;
}

function uploadAttachment(att) {
  const fd = new FormData();
  fd.append("file", att.file, att.file.name);

  const xhr = new XMLHttpRequest();
  att.xhr = xhr;
  xhr.open("POST", "/v1/files");

  xhr.upload.addEventListener("progress", (e) => {
    if (!e.lengthComputable) return;
    att.progress = e.loaded / e.total;
    renderAttachments();
  });

  xhr.addEventListener("load", () => {
    att.xhr = null;
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const body = JSON.parse(xhr.responseText);
        if (body.file_id) {
          att.file_id = body.file_id;
          att.status = "ready";
          att.progress = 1;
        } else {
          att.status = "failed";
          att.error = "Upload returned no file_id";
        }
      } catch (e) {
        att.status = "failed";
        att.error = "Could not parse upload response";
      }
    } else {
      att.status = "failed";
      att.error = describeUploadError(xhr);
    }
    renderAttachments();
    updateSendButton();
  });

  xhr.addEventListener("error", () => {
    att.xhr = null;
    att.status = "failed";
    att.error = "Network error during upload";
    renderAttachments();
    updateSendButton();
  });

  xhr.addEventListener("abort", () => {
    att.xhr = null;
    // Removed by user; nothing to do.
  });

  xhr.send(fd);
}

function describeUploadError(xhr) {
  if (xhr.status === 413) return "File too large";
  if (xhr.status === 415) return "File type not allowed";
  if (xhr.status === 422) return "File rejected (virus scan)";
  if (xhr.status === 0)   return "Upload aborted";
  // Try to extract a JSON error message; fall back to status text.
  try {
    const body = JSON.parse(xhr.responseText);
    if (body && typeof body.error === "string") return body.error;
  } catch {}
  return "Upload failed (HTTP " + xhr.status + ")";
}

function removeAttachment(id) {
  const i = attachments.findIndex(a => a.id === id);
  if (i < 0) return;
  const att = attachments[i];
  if (att.xhr) {
    try { att.xhr.abort(); } catch {}
  }
  attachments.splice(i, 1);
  renderAttachments();
  updateSendButton();
}

function renderAttachments() {
  if (attachments.length === 0) {
    attachmentsEl.hidden = true;
    attachmentsEl.replaceChildren();
    return;
  }
  attachmentsEl.hidden = false;
  attachmentsEl.replaceChildren(...attachments.map(renderChip));
}

function renderChip(att) {
  const el = document.createElement("div");
  el.className = "attachment";
  if (att.status === "uploading") el.classList.add("uploading");
  if (att.status === "failed") el.classList.add("failed");
  el.dataset.id = att.id;

  const icon = document.createElement("span");
  icon.className = "attachment-icon";
  icon.innerHTML = fileIconSvg(att.file.type);
  el.appendChild(icon);

  const name = document.createElement("span");
  name.className = "attachment-name";
  name.textContent = att.file.name;
  name.title = att.file.name;
  el.appendChild(name);

  const size = document.createElement("span");
  size.className = "attachment-size";
  size.textContent = formatBytes(att.file.size);
  el.appendChild(size);

  if (att.status === "failed" && att.error) {
    const err = document.createElement("span");
    err.className = "attachment-error-text";
    err.textContent = "— " + att.error;
    err.title = att.error;
    el.appendChild(err);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "attachment-remove";
  remove.setAttribute("aria-label", "Remove attachment");
  remove.textContent = "×";
  remove.addEventListener("click", () => removeAttachment(att.id));
  el.appendChild(remove);

  if (att.status === "uploading" || att.status === "failed") {
    const bar = document.createElement("div");
    bar.className = "attachment-progress";
    bar.style.width = Math.max(2, Math.round(att.progress * 100)) + "%";
    el.appendChild(bar);
  }

  return el;
}

function fileIconSvg(mime) {
  const s = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">`;
  if (mime && mime.startsWith("image/")) {
    return s + `<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/></svg>`;
  }
  if (mime === "application/pdf") {
    return s + `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`;
  }
  return s + `<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="13" y2="17"/></svg>`;
}

function formatBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
  return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

function updateSendButton() {
  // Disable send while any attachment is still uploading. Failed
  // attachments don't block — the user can either remove them or
  // send anyway (they're effectively no-ops since they have no
  // file_id).
  const anyUploading = attachments.some(a => a.status === "uploading");
  sendBtn.disabled = streaming || anyUploading;
}

function readyFileIds() {
  return attachments.filter(a => a.status === "ready" && a.file_id).map(a => a.file_id);
}

function clearAttachmentsAfterSend() {
  attachments.length = 0;
  renderAttachments();
  updateSendButton();
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || streaming) return;
  // Refuse to send while uploads are in flight.  setStreaming will
  // re-disable the send button anyway, but we want a hard refusal
  // here to avoid racing the upload completion.
  if (attachments.some(a => a.status === "uploading")) return;

  inputEl.value = "";
  autoResize();
  appendMessage("user", text);
  messages.push({ role: "user", content: text });

  // Build the assistant message container and a renderer that owns it.
  const assistantEl = document.createElement("div");
  assistantEl.classList.add("message", "assistant");
  messagesEl.appendChild(assistantEl);
  scrollToBottom();

  const renderer = createStreamRenderer(assistantEl);
  const requestStart = performance.now();
  let clientTtft = null;

  setStreaming(true);

  // Snapshot the file_ids that are ready *now*; don't include
  // anything that finishes uploading after we've started the
  // request. Clear the chip list immediately so the user gets visual
  // feedback that the attachment was consumed.
  const fileIds = readyFileIds();
  clearAttachmentsAfterSend();

  try {
    const reqBody = { messages: messages, stream: true };
    if (fileIds.length > 0) reqBody.file_ids = fileIds;
    if (userTemperature !== null) reqBody.temperature = userTemperature;
    if (userMaxTokens !== null) reqBody.max_tokens = userMaxTokens;
    if (userTopP !== null) reqBody.top_p = userTopP;
    if (userTopK !== null) reqBody.top_k = userTopK;
    if (userFreqPenalty !== null) reqBody.frequency_penalty = userFreqPenalty;
    if (userPresencePenalty !== null) reqBody.presence_penalty = userPresencePenalty;
    if (userRepPenalty !== null) reqBody.repetition_penalty = userRepPenalty;
    if (userReasoningEffort !== null) reqBody.reasoning_effort = userReasoningEffort;
    if (userApiBase !== null) reqBody.api_base = userApiBase;
    if (userResponsesApi) reqBody.use_responses_api = true;

    const resp = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reqBody),
    });

    if (!resp.ok) {
      throw new Error("API returned " + resp.status + ": " + resp.statusText);
    }

    // Capture trace_id from the response header so feedback controls
    // can attach to a real trace. (The same value is also echoed on
    // the final usage chunk for transports that hide headers.)
    const headerTraceId = resp.headers.get("X-Trace-Id");
    if (headerTraceId) renderer.setTraceId(headerTraceId);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // SSE messages are separated by blank lines (\n\n). A single
      // ``data:`` line may also be split across read() boundaries, so
      // we keep any incomplete trailing line in the buffer.
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.startsWith("data:")) continue;

        const payload = trimmed.slice(5).trim();
        if (payload === "[DONE]") continue;

        let parsed;
        try {
          parsed = JSON.parse(payload);
        } catch {
          continue; // skip malformed
        }

        renderer.pushRawChunk(parsed);

        // Surface backend errors that arrive mid-stream.
        if (parsed.error) {
          appendError("Stream error: " + (parsed.error.message || "unknown"));
          continue;
        }

        // Detect metrics chunk (empty choices array + stream_metrics).
        if (parsed.stream_metrics) {
          renderer.setMetrics(parsed.stream_metrics, parsed.usage);
          if (parsed.trace_id) renderer.setTraceId(parsed.trace_id);
          continue;
        }

        const delta = parsed.choices?.[0]?.delta;
        if (delta) {
          // Record client-side TTFT on first content delta.
          if (delta.content && clientTtft === null) {
            clientTtft = (performance.now() - requestStart) / 1000;
          }
          renderer.handleDelta(delta);
        }
      }
    }
  } catch (err) {
    if (!renderer.getResponseText()) {
      assistantEl.remove();
      appendError("Error: " + err.message);
      setStreaming(false);
      return;
    }
  }

  renderer.finalize(clientTtft);
  const finalText = renderer.getResponseText();
  if (finalText) {
    messages.push({ role: "assistant", content: finalText });
  }
  setStreaming(false);
  inputEl.focus();
}

function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + "px";
}

document.getElementById("input-form").addEventListener("submit", function (e) {
  e.preventDefault();
  sendMessage();
});

inputEl.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

inputEl.addEventListener("input", autoResize);

init();
