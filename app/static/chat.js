(function() {
  const chatForm = document.getElementById('chatForm');
  const chatInput = document.getElementById('chatInput');
  const chatMessages = document.getElementById('chatMessages');
  const sessionIdInput = document.getElementById('sessionId');
  const chatStatus = document.getElementById('chatStatus');
  const sendBtn = document.getElementById('sendBtn');
  const stopBtn = document.getElementById('stopBtn');
  const quickChips = document.querySelectorAll('.quick-chip');

  let isStreaming = false;

  function escapeHtml(text) {
    if (!text) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function renderMarkdown(text) {
    if (!text) return '';
    if (typeof marked === 'undefined') {
      return escapeHtml(text).replace(/\n/g, '<br>');
    }
    try {
      const raw = marked.parse(text, {
        breaks: true,
        gfm: true,
        headerIds: false,
        mangle: false,
      });
      if (typeof DOMPurify !== 'undefined') {
        return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
      }
      return raw;
    } catch (e) {
      console.warn('Markdown render failed', e);
      return escapeHtml(text).replace(/\n/g, '<br>');
    }
  }

  function autoResize(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
  }

  chatInput.addEventListener('input', function() {
    autoResize(chatInput);
  });

  chatInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      chatForm.dispatchEvent(new Event('submit'));
    }
  });

  quickChips.forEach(function(chip) {
    chip.addEventListener('click', function() {
      chatInput.value = chip.dataset.question;
      autoResize(chatInput);
      chatForm.dispatchEvent(new Event('submit'));
    });
  });

  function appendUserMessage(text) {
    const div = document.createElement('div');
    div.className = 'chat-message user-message';
    div.innerHTML = '<div class="message-bubble"><div class="message-content">' + escapeHtml(text) + '</div></div>';
    chatMessages.appendChild(div);
    scrollToBottom();
  }

  function appendAiContainer() {
    const div = document.createElement('div');
    div.className = 'chat-message ai-message';
    div.innerHTML = '<div class="message-avatar">AI</div><div class="message-bubble"><div class="message-status">正在思考...</div><div class="message-content"><span class="typing-cursor"></span></div><div class="message-tools"></div></div>';
    chatMessages.appendChild(div);
    scrollToBottom();
    return {
      root: div,
      status: div.querySelector('.message-status'),
      content: div.querySelector('.message-content'),
      tools: div.querySelector('.message-tools'),
    };
  }

  function isNearBottom() {
    const threshold = 80;
    return chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < threshold;
  }

  function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function scrollToBottomIfNear() {
    if (isNearBottom()) {
      scrollToBottom();
    }
  }

  function setStatus(text) {
    chatStatus.textContent = text || '';
  }

  function createToolElement(toolName, toolInput) {
    const item = document.createElement('div');
    item.className = 'tool-item pending';
    const header = document.createElement('div');
    header.className = 'tool-header';
    const icon = document.createElement('span');
    icon.className = 'tool-status-icon spinner';
    header.appendChild(icon);
    const name = document.createElement('span');
    name.className = 'tool-name';
    name.textContent = toolName;
    header.appendChild(name);
    item.appendChild(header);

    const inputBlock = document.createElement('pre');
    inputBlock.className = 'tool-input';
    inputBlock.textContent = typeof toolInput === 'string' ? toolInput : JSON.stringify(toolInput, null, 2);
    item.appendChild(inputBlock);

    const outputBlock = document.createElement('pre');
    outputBlock.className = 'tool-output';
    outputBlock.style.display = 'none';
    item.appendChild(outputBlock);

    const toggle = document.createElement('button');
    toggle.className = 'tool-toggle';
    toggle.textContent = '查看详情';
    toggle.addEventListener('click', function() {
      const show = outputBlock.style.display === 'none';
      outputBlock.style.display = show ? 'block' : 'none';
      toggle.textContent = show ? '收起详情' : '查看详情';
    });
    item.appendChild(toggle);

    return { item, outputBlock, icon };
  }

const TOOL_OUTPUT_PREVIEW_LIMIT = 4000;

  function updateToolResult(toolItem, toolOutput) {
    toolItem.item.classList.remove('pending');
    toolItem.item.classList.add('done');
    toolItem.icon.className = 'tool-status-icon done';
    toolItem.icon.innerHTML = '✓';
    const output = typeof toolOutput === 'string' ? toolOutput : JSON.stringify(toolOutput, null, 2);
    if (output.length > TOOL_OUTPUT_PREVIEW_LIMIT) {
      toolItem.fullOutput = output;
      toolItem.outputBlock.textContent = output.slice(0, TOOL_OUTPUT_PREVIEW_LIMIT) + '\n...（内容较长，已折叠）';
      const expandBtn = document.createElement('button');
      expandBtn.className = 'tool-toggle';
      expandBtn.textContent = '显示完整内容';
      expandBtn.addEventListener('click', function() {
        if (toolItem.outputBlock.dataset.expanded === 'true') {
          toolItem.outputBlock.textContent = output.slice(0, TOOL_OUTPUT_PREVIEW_LIMIT) + '\n...（内容较长，已折叠）';
          toolItem.outputBlock.dataset.expanded = 'false';
          expandBtn.textContent = '显示完整内容';
        } else {
          toolItem.outputBlock.textContent = toolItem.fullOutput;
          toolItem.outputBlock.dataset.expanded = 'true';
          expandBtn.textContent = '收起完整内容';
        }
      });
      toolItem.item.appendChild(expandBtn);
    } else {
      toolItem.outputBlock.textContent = output;
    }
    toolItem.outputBlock.style.display = 'block';
  }

  function updateToolError(toolItem, message) {
    toolItem.item.classList.remove('pending');
    toolItem.item.classList.add('error');
    toolItem.icon.className = 'tool-status-icon error';
    toolItem.icon.innerHTML = '✗';
    toolItem.outputBlock.textContent = message || '执行失败';
    toolItem.outputBlock.style.display = 'block';
  }

  function parseSseEvent(raw) {
    const lines = raw.split('\n');
    let event = '';
    let dataParts = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataParts.push(line.slice(5));
      }
    }
    return { event, data: dataParts.join('\n').trim() };
  }

  let activeController = null;

  function stopStreaming() {
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
  }

  function setInputLoading(loading) {
    if (loading) {
      sendBtn.style.display = 'none';
      stopBtn.style.display = 'inline-flex';
    } else {
      sendBtn.style.display = 'inline-flex';
      stopBtn.style.display = 'none';
    }
  }

  async function sendMessage(question) {
    if (isStreaming) return;
    isStreaming = true;
    sendBtn.disabled = true;
    stopBtn.disabled = false;
    setInputLoading(true);
    setStatus('AI 正在处理...');

    appendUserMessage(question);
    chatInput.value = '';
    autoResize(chatInput);

    const emptyState = document.getElementById('chatEmptyState');
    if (emptyState) {
      emptyState.style.display = 'none';
    }

    const aiContainer = appendAiContainer();
    let fullAnswer = '';
    const pendingTools = [];
    activeController = new AbortController();

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        signal: activeController.signal,
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': readCookie('csrf_token') || '',
        },
        body: JSON.stringify({
          question: question,
          session_id: sessionIdInput.value ? parseInt(sessionIdInput.value, 10) : null,
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(function() { return { error: '请求失败' }; });
        throw new Error(err.error || '请求失败');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();
        for (let i = 0; i < parts.length; i++) {
          const { event, data } = parseSseEvent(parts[i]);
          if (event === 'token') {
            fullAnswer += data;
            aiContainer.status.textContent = '正在生成回答...';
            aiContainer.content.innerHTML = renderMarkdown(fullAnswer) + '<span class="typing-cursor"></span>';
            scrollToBottomIfNear();
          } else if (event === 'thinking') {
            aiContainer.status.textContent = data || '正在思考...';
          } else if (event === 'tool_call') {
            let payload;
            try { payload = JSON.parse(data); } catch (e) { payload = {}; }
            aiContainer.status.textContent = '正在查询：' + (payload.tool_name || '工具') + '...';
            const toolEl = createToolElement(payload.tool_name, payload.tool_input);
            aiContainer.tools.appendChild(toolEl.item);
            pendingTools.push({ id: payload.tool_call_id || 'tc_' + pendingTools.length, el: toolEl });
            aiContainer.tools.style.display = 'block';
            scrollToBottomIfNear();
          } else if (event === 'tool_result') {
            let payload;
            try { payload = JSON.parse(data); } catch (e) { payload = {}; }
            const callId = payload.tool_call_id;
            let matched = null;
            if (callId) {
              matched = pendingTools.find(function(t) { return t.id === callId && t.el.item.classList.contains('pending'); });
            }
            if (!matched) {
              matched = pendingTools.find(function(t) { return t.el.item.classList.contains('pending'); });
            }
            if (matched) {
              updateToolResult(matched.el, payload.tool_output);
            }
            aiContainer.status.textContent = '查询完成，正在生成回答...';
            scrollToBottomIfNear();
          } else if (event === 'session') {
            try {
              const sessionInfo = JSON.parse(data);
              if (sessionInfo.session_id) {
                sessionIdInput.value = sessionInfo.session_id;
                if (window.history.replaceState) {
                  window.history.replaceState({}, '', '/chat?session_id=' + sessionInfo.session_id);
                }
              }
            } catch (e) {
              console.warn('Failed to parse session event', e);
            }
          } else if (event === 'error') {
            let payload;
            try { payload = JSON.parse(data); } catch (e) { payload = { message: data }; }
            throw new Error(payload.message || 'AI 处理失败');
          } else if (event === 'done') {
            isStreaming = false;
          }
        }
      }

      aiContainer.content.innerHTML = renderMarkdown(fullAnswer) || '<span class="message-placeholder">AI 没有返回任何内容</span>';
      aiContainer.status.style.display = 'none';
      setStatus('');

    } catch (err) {
      if (err.name === 'AbortError') {
        aiContainer.status.textContent = '';
        aiContainer.content.innerHTML = '<span class="message-error">已停止生成</span>';
      } else {
        console.error(err);
        aiContainer.status.textContent = '';
        aiContainer.content.innerHTML = '<span class="message-error">出错了：' + escapeHtml(err.message) + '</span>';
      }
      pendingTools.forEach(function(t) {
        if (t.el.item.classList.contains('pending')) {
          updateToolError(t.el, '已中断');
        }
      });
      setStatus('');
    } finally {
      activeController = null;
      isStreaming = false;
      sendBtn.disabled = false;
      stopBtn.disabled = false;
      setInputLoading(false);
      chatInput.focus();
    }
  }

  chatForm.addEventListener('submit', function(e) {
    e.preventDefault();
    const question = chatInput.value.trim();
    if (!question) return;
    sendMessage(question);
  });

  stopBtn.addEventListener('click', function(e) {
    e.preventDefault();
    stopStreaming();
    stopBtn.disabled = true;
  });

  function readCookie(name) {
    const prefix = name + '=';
    const parts = document.cookie ? document.cookie.split(';') : [];
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i].trim();
      if (part.indexOf(prefix) === 0) {
        return decodeURIComponent(part.slice(prefix.length));
      }
    }
    return '';
  }

  // Scroll to bottom on load.
  scrollToBottom();

  // Render markdown for pre-existing assistant messages loaded from history.
  document.querySelectorAll('.ai-message .message-content').forEach(function(el) {
    el.innerHTML = renderMarkdown(el.textContent);
  });
})();
