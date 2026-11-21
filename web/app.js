'use strict';
const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="session-token"]').content;
const isPreview = location.protocol === 'file:';
const state = {settings: {}, models: [], conversations: [], active: null, attachments: [], busy: false, uploading: 0, assistantId: null, abort: null, search: '', connected: false, mode:'chat', busyKind:'chat', polling:false};
const control = {running:false, runId:null, abort:null, step:0, maxSteps:20, mode:'confirm', screen:null, pending:null, stopping:false};
const isImageModel = (name) => /image|imagine|dall.e|flux|sdxl|stable.diffusion|seedream|ideogram|recraft|cogview|kolors|nano.banana/i.test(name) && !/video/i.test(name);
const icon = (name) => '<svg aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
const esc = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const fileUrl = (id, preview) => '/api/attachments/' + id + (preview ? '/preview' : '/file') + '?token=' + encodeURIComponent(token);
const sizeLabel = (bytes) => bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
const timeLabel = (value) => new Date(value).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', hour12:false});

async function api(path, options = {}) {
  if (isPreview) throw new Error('当前是界面预览。请双击项目文件夹里的「启动 GrokDesk.vbs」打开聊天软件。');
  const response = await fetch('/api' + path, {...options, headers: {'Content-Type':'application/json', 'X-GrokDesk-Token':token, ...options.headers}});
  let body;
  try { body = await response.json(); } catch { throw new Error('软件连接中断，请重新打开。'); }
  if (!response.ok) throw new Error(body.error || '操作失败，请重试。');
  return body;
}
function toast(text, error = false) {
  const element = document.createElement('div');
  element.className = 'toast' + (error ? ' error' : '');
  element.textContent = text;
  $('#toastStack').append(element);
  setTimeout(() => element.remove(), error ? 6500 : 3200);
}
function safely(task) { return (...args) => Promise.resolve(task(...args)).catch((e) => toast(e.message, true)); }
function scrollBottom(force = false) {
  const area = $('#chatArea');
  if (force || area.scrollHeight - area.scrollTop - area.clientHeight < 180) area.scrollTop = area.scrollHeight;
}
function resizeInput() {
  const field = $('#messageInput');
  field.style.height = 'auto';
  field.style.height = Math.min(field.scrollHeight, 180) + 'px';
  $('#sendButton').disabled = control.running || (!state.busy && ((!field.value.trim() && !state.attachments.length) || state.uploading > 0));
}
function saveDraft() {
  try { localStorage.setItem('grok-draft-' + (state.active?.id || 'new'), $('#messageInput').value); } catch {}
}
function restoreDraft() {
  try { $('#messageInput').value = localStorage.getItem('grok-draft-' + (state.active?.id || 'new')) || ''; } catch { $('#messageInput').value = ''; }
  resizeInput();
}
function applyTheme() {
  document.documentElement.dataset.theme = state.settings.theme || 'light';
  document.documentElement.style.setProperty('--chat-font', (state.settings.font_size || 15) + 'px');
  $('#themeToggle').innerHTML = icon(state.settings.theme === 'dark' ? 'sun' : 'moon');
}
function connectionStatus() {
  const hasKey = state.settings.has_api_key;
  $('#connectionLabel').textContent = hasKey ? '连接与偏好' : '配置模型连接';
  $('#connectionPill').classList.toggle('connected', state.connected);
  $('#connectionPill span').textContent = isPreview ? '界面预览' : (state.connected ? '已连接' : (hasKey ? '待验证连接' : '未配置连接'));
}
function renderModels() {
  const image = state.mode === 'image';
  const model = image ? state.settings.image_model || '' : state.active?.model || state.settings.model || '';
  const candidates = state.models.filter((name) => isImageModel(name) === image);
  const options = [...new Set([model, image ? state.settings.image_model : state.settings.model, ...candidates].filter(Boolean))];
  $('#modelSelect').innerHTML = '<option value="">' + (image ? '选择生图模型' : 'Grok') + '</option>' + options.map((name) => '<option value="' + esc(name) + '">' + esc(name) + '</option>').join('');
  $('#modelSelect').value = model;
  $('#modelOptions').innerHTML = state.models.map((name) => '<option value="' + esc(name) + '"></option>').join('');
  const imageModels = state.models.filter(isImageModel);
  $('#imageModelOptions').innerHTML = imageModels.map((name) => '<option value="' + esc(name) + '"></option>').join('');
  $('#imageModelHint').textContent = state.models.length && !imageModels.length ? '当前密钥返回的列表没有生图模型。请向服务商开通权限，或换用支持生图的密钥。' : '生图和聊天使用同一把密钥。列表按名称筛选，实际生图权限由服务商决定。';
}
function setMode(mode) {
  if (state.busy || state.uploading) return;
  if (mode === 'image' && state.attachments.length) return toast('生图根据文字描述生成，请先移除输入框里的附件。');
  state.mode = mode;
  const image = mode === 'image';
  $('.main').classList.toggle('image-mode', image);
  $('#imageMode').setAttribute('aria-pressed', String(image));
  $('#imageOptions').hidden = !image;
  $('#imageSize').value = state.settings.image_size || 'auto';
  $('#imageCount').value = String(state.settings.image_count || 1);
  $('#attachFile').disabled = image; $('#attachImage').disabled = image;
  $('#messageInput').placeholder = image ? '描述你想生成的图片…' : '向 Grok 发送消息';
  $('#welcome h1').textContent = image ? '想把什么画出来？' : '今天有什么想聊的？';
  renderModels(); resizeInput(); $('#messageInput').focus();
}
async function refreshHistory() {
  state.conversations = await api('/conversations?q=' + encodeURIComponent(state.search));
  renderHistory();
}
function renderHistory() {
  $('#historyCount').textContent = state.conversations.length;
  if (!state.conversations.length) {
    $('#historyList').innerHTML = '<div class="history-empty">' + (state.search ? '没有找到相关对话' : '开始聊天后，对话会显示在这里。') + '</div>';
    return;
  }
  let group = '';
  $('#historyList').innerHTML = state.conversations.map((item) => {
    const date = new Date(item.updated_at).toDateString();
    const label = item.pinned ? '置顶' : (date === new Date().toDateString() ? '今天' : '更早');
    const heading = label === group ? '' : '<div class="history-date">' + label + '</div>';
    group = label;
    return heading + '<div class="history-item' + (item.id === state.active?.id ? ' active' : '') + '" data-conversation="' + item.id + '" tabindex="0" role="button" aria-label="打开对话：' + esc(item.title) + '">' + icon(item.pinned ? 'pin' : 'chat') + '<div class="history-item-text"><div class="history-item-title">' + esc(item.title) + '</div><div class="history-item-preview">' + esc(item.preview || '开始你的第一条消息') + '</div></div><button class="history-more" data-history-menu="' + item.id + '" aria-label="对话选项">' + icon('more') + '</button></div>';
  }).join('');
}
function renderMarkdown(text) {
  const html = DOMPurify.sanitize(marked.parse(text, {breaks:true, gfm:true}), {FORBID_TAGS:['style','form','input','button','iframe','video','audio'], FORBID_ATTR:['style']});
  const holder = document.createElement('div');
  holder.innerHTML = html;
  holder.querySelectorAll('a').forEach((a) => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
  holder.querySelectorAll('pre').forEach((pre) => {
    const code = pre.querySelector('code');
    const label = document.createElement('span');
    label.className = 'code-label';
    label.textContent = code?.className.replace('language-', '') || '代码';
    const button = document.createElement('button');
    button.className = 'copy-code'; button.textContent = '复制代码';
    pre.prepend(label); pre.append(button);
  });
  return holder.innerHTML;
}
function renderAttachment(attachment) {
  if (attachment.image_name) return '<div class="image-card"><img src="' + fileUrl(attachment.id, true) + '" data-preview="' + attachment.id + '" alt="' + esc(attachment.name) + '"><a href="' + fileUrl(attachment.id) + '" download>' + esc(attachment.name) + ' · 下载原图 ↗</a></div>';
  return '<a class="file-card" href="' + fileUrl(attachment.id) + '" download>' + icon('file') + '<span><strong>' + esc(attachment.name) + '</strong><small>' + sizeLabel(attachment.size) + ' · 下载文件</small></span>' + icon('download') + '</a>';
}
function messageContent(message) {
  let body = message.role === 'user' ? '<div class="user-text">' + esc(message.content) + '</div>' : (message.content ? renderMarkdown(message.content) : (message.status === 'streaming' ? '<div class="typing-indicator"><span></span><span></span><span></span></div>' : ''));
  if (message.attachments?.length) body += '<div class="message-attachments">' + message.attachments.map(renderAttachment).join('') + '</div>';
  if (message.kind === 'image') {
    const labels = {auto:'默认比例', square:'方图', landscape:'横图', portrait:'竖图'};
    body = (message.status === 'streaming' && !message.content ? '<div class="image-waiting"><div class="typing-indicator"><span></span><span></span><span></span></div><span>正在生成图片…</span></div>' : body);
    if (message.role === 'assistant') body += '<div class="image-details">' + esc(message.model) + ' · ' + (labels[message.generation?.size] || '默认比例') + (message.generation?.images?.length ? ' · ' + message.generation.images.map((item) => item.width + ' × ' + item.height).join(' / ') : '') + '</div>';
  }
  if (message.error) body += '<div class="message-error">' + esc(message.error) + '</div>';
  if (message.status === 'stopped') body += '<div class="message-stopped">已停止生成 · 已有内容已保存</div>';
  return body;
}
function renderMessages() {
  const items = state.active?.messages || [];
  $('#welcome').hidden = items.length > 0;
  $('.main').classList.toggle('empty-state', items.length === 0);
  $('#messages').innerHTML = items.map((message, index) => {
    const assistant = message.role === 'assistant';
    const usage = message.usage?.total_tokens;
    return '<article class="message ' + message.role + (message.kind === 'image' ? ' image-generation' : '') + '" id="msg-' + message.id + '"><div class="message-header"><span class="message-avatar">' + (assistant ? icon('spark') : '你') + '</span><span>' + (assistant ? 'Grok' : '你') + '</span><time title="' + esc(message.model || '') + '">' + timeLabel(message.created_at) + '</time></div><div class="message-body message-content">' + messageContent(message) + '</div><div class="message-actions"><button class="icon-button" data-copy="' + message.id + '" title="复制消息" aria-label="复制消息">' + icon('copy') + '</button>' + (assistant && index === items.length - 1 && !state.busy && message.status !== 'streaming' ? '<button class="icon-button" data-regenerate title="重新生成" aria-label="重新生成">' + icon('refresh') + '</button>' : '') + (usage ? '<span class="usage">' + usage.toLocaleString() + ' tokens</span>' : '') + '</div></article>';
  }).join('');
}
function updateAssistant(message) {
  const node = $('#msg-' + message.id + ' .message-body');
  if (node) { node.innerHTML = messageContent(message); scrollBottom(); }
}
function renderPending() {
  $('#attachmentTray').innerHTML = state.attachments.map((item) => '<div class="pending-attachment">' + (item.image_name ? '<img alt="' + esc(item.name) + '" src="' + fileUrl(item.id, true) + '">' : icon('file')) + '<span><strong>' + esc(item.name) + '</strong><small>' + sizeLabel(item.size) + (item.text_length ? ' · ' + item.text_length.toLocaleString() + ' 字' : '') + '</small></span><button class="icon-button" data-remove-attachment="' + item.id + '" aria-label="移除附件">' + icon('close') + '</button></div>').join('');
  resizeInput();
}
async function clearPending() {
  const pending = [...state.attachments];
  state.attachments = []; renderPending();
  await Promise.allSettled(pending.map((item) => api('/attachments/' + item.id, {method:'DELETE'})));
}
async function openConversation(id) {
  if (state.busy) return toast('请先等待回复完成，或点击停止生成。');
  if (state.uploading) return toast('文件还在读取，请稍等。');
  saveDraft();
  await clearPending();
  state.active = await api('/conversations/' + id);
  setMode(state.active.messages.at(-1)?.kind === 'image' ? 'image' : 'chat');
  restoreDraft(); renderHistory(); renderMessages(); renderModels(); scrollBottom(true);
  closeMobileSidebar();
  try { localStorage.setItem('grok-active', id); } catch {}
  safely(resumeImageGeneration)();
}
async function newChat() {
  if (state.busy) return toast('请先等待回复完成，或点击停止生成。');
  if (state.uploading) return toast('文件还在读取，请稍等。');
  saveDraft(); await clearPending(); state.active = null;
  try { localStorage.removeItem('grok-active'); } catch {}
  restoreDraft(); renderHistory(); renderMessages(); renderModels();
  $('#messageInput').focus(); closeMobileSidebar();
}
async function uploadFiles(files) {
  if (state.mode === 'image') return toast('当前为文字生图。分析图片或文件请先点击「生图」切回聊天。');
  if (state.busy) return toast('回复生成后再添加下一条消息的附件。');
  for (const file of files) {
    if (state.attachments.length >= 8) { toast('每条消息最多添加 8 个附件。', true); break; }
    if (file.size > 20 * 1048576) { toast(file.name + ' 超过 20 MB，请压缩或分段。', true); continue; }
    state.uploading++; resizeInput();
    $('#composerStatus').textContent = '正在读取 ' + file.name + '…';
    try {
      const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('文件读取失败')); reader.readAsDataURL(file); });
      const attachment = await api('/attachments', {method:'POST', body:JSON.stringify({name:file.name, data})});
      state.attachments.push(attachment); renderPending();
    } catch (error) { toast(file.name + '：' + error.message, true); }
    finally { state.uploading--; resizeInput(); }
  }
  $('#composerStatus').textContent = 'Enter 发送 · Shift + Enter 换行';
  $('#messageInput').focus();
}
function setBusy(busy) {
  state.busy = busy;
  $('#sendButton').classList.toggle('stopping', busy);
  $('#sendButton').setAttribute('aria-label', busy ? '停止生成' : '发送消息');
  $('#sendButton').title = busy ? '停止生成' : '发送消息';
  $('#messageInput').disabled = busy;
  $('#attachFile').disabled = busy || state.mode === 'image'; $('#attachImage').disabled = busy || state.mode === 'image';
  $('#imageMode').disabled = busy; $('#imageSize').disabled = busy; $('#imageCount').disabled = busy;
  $('#modelSelect').disabled = busy;
  $('#composerStatus').textContent = busy ? (state.busyKind === 'image' ? '正在生成图片，请稍等…' : '正在回复…') : 'Enter 发送 · Shift + Enter 换行';
  resizeInput();
}
async function stopGeneration() {
  if (!state.assistantId) return toast('正在建立连接，稍后可停止。');
  $('#composerStatus').textContent = '正在停止，已有内容会保留…';
  await api('/chat/' + state.assistantId + '/stop', {method:'POST', body:'{}'});
  state.abort?.abort();
}
async function resumeImageGeneration() {
  const last = state.active?.messages.at(-1);
  if (state.polling || !last || last.kind !== 'image' || last.status !== 'streaming') return;
  state.polling = true; state.busyKind = 'image'; state.assistantId = last.id; setBusy(true);
  try {
    while (state.active?.id === last.conversation_id) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      state.active = await api('/conversations/' + last.conversation_id);
      renderMessages(); scrollBottom();
      const current = state.active.messages.find((message) => message.id === last.id);
      if (!current || current.status !== 'streaming') break;
    }
  } finally {
    state.polling = false; state.assistantId = null; setBusy(false); renderMessages(); await refreshHistory();
  }
}
async function sendMessage(regenerate = false) {
  if (control.running) return toast('电脑控制正在运行，请先停止任务。');
  if (state.busy) return stopGeneration();
  if (state.uploading) return toast('文件还在读取，请稍等。');
  if (!state.settings.has_api_key) { openSettings(); return toast('先填入密钥，获取模型后就能聊天。'); }
  const kind = regenerate ? state.active?.messages.at(-1)?.kind || 'chat' : state.mode;
  if (kind !== state.mode) setMode(kind);
  const model = regenerate ? state.active?.messages.at(-1)?.model : $('#modelSelect').value;
  if (!model) { openSettings(); if (kind === 'image') $('#settingsImageModel').focus(); return toast(kind === 'image' ? '请先在设置里选择有权限的生图模型。' : '先获取并选择一个模型。'); }
  const text = $('#messageInput').value.trim();
  if (!regenerate && !text && !state.attachments.length) return;
  if (kind === 'image' && !regenerate && (!text || state.attachments.length)) return toast('请输入图片描述，并移除待发送的附件。');
  let conversationId = state.active?.id;
  let accepted = false, completed = false, renderQueued = false;
  state.abort = new AbortController(); state.assistantId = null; state.busyKind = kind; setBusy(true);
  try {
    if (!state.active) state.active = await api('/conversations', {method:'POST', body:JSON.stringify({model:kind === 'image' ? state.settings.model || '' : model})});
    conversationId = state.active.id;
    const body = {conversation_id:conversationId, text, model, attachments:state.attachments.map((item) => item.id), regenerate, image_size:$('#imageSize').value, image_count:Number($('#imageCount').value)};
    const response = await fetch(kind === 'image' ? '/api/images/generations' : '/api/chat', {method:'POST', headers:{'Content-Type':'application/json','X-GrokDesk-Token':token}, body:JSON.stringify(body), signal:state.abort.signal});
    if (!response.ok) { const result = await response.json(); throw new Error(result.error || '发送失败'); }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    function event(packet) {
      const lines = packet.split('\n');
      const name = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
      const raw = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
      if (!raw) return;
      const data = JSON.parse(raw);
      if (name === 'start') {
        accepted = true; state.assistantId = data.assistant_id; state.active = data.conversation;
        if (!regenerate) { $('#messageInput').value = ''; state.attachments = []; renderPending(); saveDraft(); try { localStorage.removeItem('grok-draft-new'); } catch {} }
        renderMessages(); scrollBottom(true); refreshHistory().catch(() => {});
        try { localStorage.setItem('grok-active', conversationId); } catch {}
      }
      if (name === 'delta') {
        const message = state.active.messages.find((item) => item.id === state.assistantId);
        if (message) {
          message.content += data.text;
          if (!renderQueued) { renderQueued = true; requestAnimationFrame(() => { renderQueued = false; updateAssistant(message); }); }
        }
      }
      if (name === 'progress') { state.active = data.conversation; renderMessages(); scrollBottom(true); }
      if (name === 'done') {
        completed = true;
        const message = state.active.messages.find((item) => item.id === state.assistantId);
        if (message) { Object.assign(message, {status:data.status, error:data.error, usage:data.usage}); updateAssistant(message); }
        if (data.error) toast(data.error, true);
        if (data.status === 'complete') { state.connected = true; connectionStatus(); }
      }
    }
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value, {stream:!done}).replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) { const packet = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2); event(packet); }
      if (done) break;
    }
    if (!completed) throw new Error('回复连接中断，已接收的内容会保留。');
  } catch (error) {
    if (error.name !== 'AbortError') toast(error.message, true);
  } finally {
    state.abort = null; state.assistantId = null; setBusy(false);
    if (accepted) {
      try {
        for (let attempt = 0; attempt < 30; attempt++) {
          state.active = await api('/conversations/' + conversationId);
          if (!state.active.messages.some((message) => message.status === 'streaming')) break;
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
      } catch {}
      renderMessages(); await refreshHistory(); scrollBottom();
      safely(resumeImageGeneration)();
    }
    $('#messageInput').focus();
  }
}
function controlActionLabel(action = {}) {
  const point = '(' + action.x + ', ' + action.y + ')';
  if (action.type === 'click') return (action.button === 'right' ? (action.count === 2 ? '右键双击 ' : '右键点击 ') : (action.count === 2 ? '双击 ' : '点击 ')) + point;
  if (action.type === 'type') return '输入文字：\n' + String(action.text || '');
  if (action.type === 'key') return '按键：' + (Array.isArray(action.keys) ? action.keys.join(' + ') : String(action.key || ''));
  if (action.type === 'scroll') return '滚动：' + ({up:'向上',down:'向下',left:'向左',right:'向右'}[action.direction] || action.direction || '') + ' ' + (action.amount || 1) + ' 格';
  if (action.type === 'wait') return '等待 ' + (action.seconds || 1) + ' 秒';
  if (action.type === 'done') return '完成任务';
  return '未知动作';
}
function controlLog(text) {
  const entry = document.createElement('li');
  entry.textContent = text; $('#controlLog').append(entry);
  while ($('#controlLog').children.length > 60) $('#controlLog').firstChild.remove();
  entry.scrollIntoView({block:'nearest'});
}
function updateControlUI() {
  $('#controlBanner').hidden = !control.running;
  $('#controlBannerText').textContent = (control.mode === 'auto' ? '完全权限' : '需要确认') + ' · ' + control.step + ' / ' + control.maxSteps + ' 步' + (control.stopping ? ' · 正在停止' : '');
  $('#controlStepLabel').textContent = control.step + ' / ' + control.maxSteps + ' 步';
  for (const id of ['controlTask','controlModel','controlMode','controlMaxSteps','controlStart']) $('#' + id).disabled = control.running;
  $('#controlStop').disabled = !control.running || control.stopping;
  $('#controlBannerStop').disabled = !control.running || control.stopping;
  $('#computerMode').setAttribute('aria-pressed', String(control.running));
  resizeInput();
}
function showControlDialog() {
  if (!control.running) {
    $('#controlModel').value = $('#modelSelect').value || state.settings.model || '';
    if (!$('#controlTask').value.trim()) $('#controlTask').value = $('#messageInput').value.trim();
  }
  if (!$('#controlDialog').open) $('#controlDialog').showModal();
}
function showControlScreen(data) {
  const url = typeof data.url === 'string' ? data.url : '';
  if (!/^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(url)) return;
  control.screen = url;
  $('#controlScreenImage').src = url; $('#controlScreen').hidden = false;
  $('#controlScreenLabel').textContent = '第 ' + (data.step || control.step || 1) + ' 步 · ' + data.width + ' × ' + data.height;
}
function showControlApproval(data) {
  if (!control.running || control.stopping) return;
  control.pending = data.action_id;
  $('#controlActionText').textContent = controlActionLabel(data.action);
  $('#controlActionReason').textContent = data.action?.reason || '允许后执行这一个动作；拒绝会停止任务。';
  $('#controlApprovalScreen').hidden = !control.screen;
  if (control.screen) $('#controlApprovalImage').src = control.screen;
  $('#controlApprove').disabled = false; $('#controlReject').disabled = false;
  if (!$('#controlApprovalDialog').open) $('#controlApprovalDialog').showModal();
}
async function decideControlAction(approved) {
  const actionId = control.pending;
  if (!actionId || !control.runId || !control.running) return;
  $('#controlApprove').disabled = true; $('#controlReject').disabled = true;
  try {
    await api('/control/' + control.runId + '/approve', {method:'POST', body:JSON.stringify({action_id:actionId, approved})});
    if (control.pending === actionId) {
      control.pending = null; $('#controlApprovalDialog').close();
      $('#controlStatus').textContent = approved ? '已允许，正在执行动作…' : '已拒绝，正在停止任务…';
      if (!approved) { control.stopping = true; updateControlUI(); }
    }
  } catch (error) {
    $('#controlApprove').disabled = false; $('#controlReject').disabled = false;
    throw error;
  }
}
async function stopControl() {
  if (!control.running || control.stopping) return;
  control.stopping = true; control.pending = null;
  $('#controlApprovalDialog').close();
  $('#controlStatus').textContent = '正在停止电脑控制…'; updateControlUI();
  try {
    if (control.runId) await api('/control/' + control.runId + '/stop', {method:'POST', body:'{}'});
    else control.abort?.abort();
  } catch (error) {
    control.stopping = false; updateControlUI(); throw error;
  }
}
function handleControlEvent(name, data) {
  if (name === 'start') {
    control.runId = data.run_id; control.mode = data.mode || control.mode; control.maxSteps = data.max_steps || control.maxSteps;
    $('#controlStatus').textContent = '正在查看屏幕，请切换到目标窗口。';
    updateControlUI();
  } else if (name === 'screen') {
    control.step = Number(data.step) || control.step; showControlScreen(data); updateControlUI();
    $('#controlStatus').textContent = '正在分析屏幕，准备下一步…';
  } else if (name === 'action') {
    control.step = Number(data.step) || control.step; updateControlUI();
    controlLog('第 ' + control.step + ' 步：' + controlActionLabel(data.action));
    $('#controlStatus').textContent = data.requires_approval ? '等待你确认动作。' : '正在执行：' + controlActionLabel(data.action);
    if (data.requires_approval) showControlApproval(data);
  } else if (name === 'executed') {
    $('#controlStatus').textContent = '动作已执行，正在查看结果…';
  } else if (name === 'done') {
    control.step = Number(data.steps) || control.step;
    const labels = {complete:'任务完成',stopped:'任务已停止',error:'任务未完成',limit:'已达到步骤上限'};
    const result = (labels[data.status] || '任务已结束') + (data.summary ? '：' + data.summary : '') + (data.error ? '\n' + data.error : '');
    $('#controlStatus').textContent = result; controlLog(result); updateControlUI();
    toast(result, data.status === 'error');
  }
}
async function startControl() {
  if (control.running) return;
  if (isPreview) return toast('请从 GrokDesk 启动程序打开，预览页面不能控制电脑。', true);
  if (state.busy || state.uploading) return toast('请等待当前回复或文件上传完成。');
  if (!state.settings.has_api_key) { $('#controlDialog').close(); openSettings(); return toast('请先设置模型连接。'); }
  const task = $('#controlTask').value.trim(), model = $('#controlModel').value.trim();
  const mode = $('#controlMode').value, maxSteps = Number($('#controlMaxSteps').value);
  if (!task) return $('#controlTask').focus();
  if (!model) return toast('请选择支持看图的聊天模型。', true);
  if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 50) return toast('最多步骤需为 1–50 的整数。', true);
  if (!['confirm','auto'].includes(mode)) return toast('请选择权限模式。', true);
  Object.assign(control, {running:true, runId:null, abort:new AbortController(), step:0, maxSteps, mode, screen:null, pending:null, stopping:false});
  $('#controlLog').replaceChildren(); $('#controlScreen').hidden = true; $('#controlScreenImage').removeAttribute('src');
  $('#controlStatus').textContent = '正在开始，请切换到目标窗口…'; updateControlUI(); $('#controlDialog').close();
  let completed = false;
  try {
    const response = await fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json','X-GrokDesk-Token':token}, body:JSON.stringify({task,model,mode,max_steps:maxSteps}), signal:control.abort.signal});
    if (!response.ok) { const result = await response.json(); throw new Error(result.error || '电脑控制启动失败。'); }
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    const packet = (raw) => {
      const lines = raw.split('\n'), name = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
      const payload = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
      if (!payload) return;
      const data = JSON.parse(payload); handleControlEvent(name, data); if (name === 'done') completed = true;
    };
    while (true) {
      const {value,done} = await reader.read(); buffer += decoder.decode(value,{stream:!done}).replace(/\r\n/g,'\n');
      let boundary; while ((boundary = buffer.indexOf('\n\n')) !== -1) { packet(buffer.slice(0,boundary)); buffer = buffer.slice(boundary + 2); }
      if (done) { if (buffer.trim()) packet(buffer); break; }
    }
    if (!completed) throw new Error(control.stopping ? '电脑控制已停止。' : '电脑控制连接中断，任务已停止。');
  } catch (error) {
    if (control.runId && !completed) { try { await api('/control/' + control.runId + '/stop', {method:'POST',body:'{}'}); } catch {} }
    const text = error.name === 'AbortError' ? '电脑控制已停止。' : error.message;
    $('#controlStatus').textContent = text; controlLog(text); if (error.name !== 'AbortError' && !control.stopping) toast(text,true);
  } finally {
    Object.assign(control,{running:false,runId:null,abort:null,pending:null,stopping:false});
    $('#controlApprovalDialog').close(); updateControlUI();
  }
}
function bindControlEvents() {
  $('#computerMode').onclick = showControlDialog;
  $('#controlShowProgress').onclick = showControlDialog;
  $('#controlStart').onclick = safely(startControl);
  $('#controlStop').onclick = safely(stopControl); $('#controlBannerStop').onclick = safely(stopControl);
  $('#controlApprove').onclick = safely(() => decideControlAction(true));
  $('#controlReject').onclick = safely(() => decideControlAction(false));
  $('#controlApprovalDialog').oncancel = (event) => { event.preventDefault(); safely(() => decideControlAction(false))(); };
  $('#controlMode').onchange = () => { $('#controlModeHint').textContent = $('#controlMode').value === 'auto' ? '在本次任务中自动执行支持的电脑动作，不再逐步弹窗；仍可随时停止。' : '每个动作先展示给你，允许后才执行。'; };
  $('#controlMaxSteps').oninput = () => { if (!control.running) { control.maxSteps = Number($('#controlMaxSteps').value) || 20; updateControlUI(); } };
  window.addEventListener('beforeunload', () => { if (control.running && control.runId) fetch('/api/control/' + control.runId + '/stop',{method:'POST',headers:{'Content-Type':'application/json','X-GrokDesk-Token':token},body:'{}',keepalive:true}).catch(() => {}); });
}
function openSettings() {
  const settings = state.settings;
  $('#baseUrl').value = settings.base_url || 'https://ergouzi.life';
  $('#apiKey').value = ''; $('#apiKey').type = 'password';
  $('#settingsModel').value = state.active?.model || settings.model || '';
  $('#settingsImageModel').value = settings.image_model || '';
  $('#systemPrompt').value = settings.system_prompt || '';
  $('#temperature').value = settings.temperature ?? 0.7;
  $('#maxTokens').value = settings.max_tokens || 4096;
  $('#contextTurns').value = settings.context_turns || 20;
  $('#fontSize').value = String(settings.font_size || 15);
  $('#keyHint').textContent = settings.has_api_key ? '已保存密钥。留空即可继续使用，填写新密钥会替换。' : '密钥会由 Windows 加密后保存在本机。';
  $('#connectionResult').hidden = true;
  $('#connectionJson').value = ''; $('#importArea').hidden = true;
  $('#settingsDialog').showModal();
}
async function saveSettings(close = true) {
  const data = {base_url:$('#baseUrl').value.trim(), api_key:$('#apiKey').value.trim(), model:$('#settingsModel').value.trim(), image_model:$('#settingsImageModel').value.trim(), system_prompt:$('#systemPrompt').value, temperature:Number($('#temperature').value), max_tokens:Number($('#maxTokens').value), context_turns:Number($('#contextTurns').value), font_size:Number($('#fontSize').value)};
  state.settings = await api('/settings', {method:'POST', body:JSON.stringify(data)});
  $('#apiKey').value = ''; $('#keyHint').textContent = '密钥已加密保存。留空即可继续使用。';
  if (state.active && data.model && state.active.model !== data.model) { state.active.model = data.model; await api('/conversations/' + state.active.id, {method:'PATCH', body:JSON.stringify({model:data.model})}); }
  applyTheme(); renderModels(); connectionStatus();
  if (close) { $('#settingsDialog').close(); toast('设置已保存。'); }
}
async function fetchModels() {
  const button = $('#fetchModels'); button.disabled = true; button.textContent = '连接中…';
  const result = $('#connectionResult'); result.hidden = false; result.classList.remove('error'); result.textContent = '正在连接服务并获取可用模型…';
  try {
    await saveSettings(false);
    const body = await api('/models'); state.models = body.models; state.connected = true;
    if (!state.models.length) throw new Error('已连接，但服务没有返回可用模型。请向服务商确认模型权限。');
    if (!$('#settingsModel').value) $('#settingsModel').value = state.models.find((name) => !isImageModel(name) && name.toLowerCase().includes('grok')) || state.models.find((name) => !isImageModel(name)) || '';
    if (!$('#settingsImageModel').value) $('#settingsImageModel').value = state.models.find(isImageModel) || '';
    renderModels(); connectionStatus();
    result.textContent = '连接成功 · 找到 ' + state.models.length + ' 个模型。\n确认默认模型后，点击「保存设置」开始聊天。';
    try { localStorage.setItem('grok-models', JSON.stringify(state.models)); } catch {}
  } catch (error) { state.connected = false; connectionStatus(); result.classList.add('error'); result.textContent = error.message; }
  finally { button.disabled = false; button.innerHTML = icon('refresh') + '获取模型'; }
}
function importConnection() {
  try {
    const raw = $('#connectionJson').value.trim().replace(/^\x60\x60\x60(?:json)?\s*|\s*\x60\x60\x60$/g, '');
    const data = JSON.parse(raw);
    if (!data.key || !data.url || typeof data.key !== 'string' || typeof data.url !== 'string') throw new Error();
    $('#baseUrl').value = data.url; $('#apiKey').value = data.key;
    $('#connectionJson').value = ''; $('#importArea').hidden = true;
    toast('已填入连接信息，点击「获取模型」继续。');
  } catch { toast('没有读到 key 和 url，请粘贴完整的连接 JSON。', true); }
}
function question(title, description, value) {
  return new Promise((resolve) => {
    const dialog = $('#smallDialog');
    $('#smallTitle').textContent = title; $('#smallDescription').textContent = description;
    $('#smallInput').hidden = value === undefined; $('#smallInput').value = value || '';
    const finish = (result) => { dialog.close(); $('#smallConfirm').onclick = null; $('#smallCancel').onclick = null; dialog.oncancel = null; resolve(result); };
    $('#smallConfirm').onclick = () => finish(value === undefined ? true : $('#smallInput').value);
    $('#smallCancel').onclick = () => finish(null); dialog.oncancel = () => finish(null);
    dialog.showModal(); if (value !== undefined) { $('#smallInput').focus(); $('#smallInput').select(); }
  });
}
async function exitApp() {
  if (isPreview) return toast('当前是界面预览，请从桌面图标打开软件。');
  if (!await question('退出 Grok Desk？', '已有聊天会保留，正在生成的回复会停止。下次双击桌面图标即可打开。')) return;
  saveDraft();
  await api('/exit', {method:'POST', body:'{}'});
  state.abort?.abort();
  document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
  const notice = $('#startupNotice');
  notice.querySelector('span').textContent = 'Grok Desk 已退出。再次使用请双击桌面上的 Grok Desk 图标。';
  notice.querySelector('a').hidden = true;
  notice.hidden = false;
  $('.app-shell').inert = true;
  toast('软件已退出，聊天记录已保留。');
}
function download(content, name, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
}
async function exportConversation(format = 'md', id = state.active?.id) {
  if (!id) return toast('聊上几句后，就可以导出这段对话。');
  const item = await api('/conversations/' + id), name = item.title.replace(/[\\/:*?"<>|]/g, '_');
  if (format === 'json') {
    const backup = {...item, format:'grok-desk-export-v1', exported_at:new Date().toISOString()};
    for (const message of backup.messages) for (const attachment of message.attachments || []) {
      const response = await fetch(fileUrl(attachment.id));
      const blob = await response.blob();
      attachment.data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(blob); });
    }
    download(JSON.stringify(backup, null, 2), name + '.json', 'application/json');
  } else {
    const text = '# ' + item.title + '\n\n' + item.messages.map((message) => '## ' + (message.role === 'user' ? '你' : 'Grok · ' + message.model) + '\n\n' + message.content + (message.attachments?.length ? '\n\n附件：' + message.attachments.map((a) => a.name).join('、') : '') + (message.error ? '\n\n> ' + message.error : '')).join('\n\n---\n\n');
    download(text, name + '.md', 'text/markdown;charset=utf-8');
  }
  toast('对话已导出' + (format === 'json' ? '，包含原始附件。' : '。'));
}
function showMenu(button, id) {
  const item = state.conversations.find((entry) => entry.id === id) || (state.active?.id === id ? state.active : null);
  if (!item) return toast('先开始一段对话。');
  const menu = $('#contextMenu'), rect = button.getBoundingClientRect();
  menu.innerHTML = '<button data-menu="rename">' + icon('edit') + '重命名对话</button><button data-menu="pin">' + icon('pin') + (item.pinned ? '取消置顶' : '置顶对话') + '</button><hr><button data-menu="md">' + icon('download') + '导出 Markdown</button><button data-menu="json">' + icon('file') + '导出 JSON 与附件</button><hr><button data-menu="delete" class="danger">' + icon('trash') + '删除对话</button>';
  menu.hidden = false;
  menu.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(rect.bottom + 5, window.innerHeight - menu.offsetHeight - 8)) + 'px';
  menu.onclick = safely(async (event) => {
    const action = event.target.closest('[data-menu]')?.dataset.menu; if (!action) return; menu.hidden = true;
    if (action === 'md' || action === 'json') return exportConversation(action, id);
    if (action === 'rename') {
      const title = await question('给对话换个名字', '方便下次找到它。', item.title); if (!title?.trim()) return;
      await api('/conversations/' + id, {method:'PATCH', body:JSON.stringify({title:title.trim()})});
      if (state.active?.id === id) state.active.title = title.trim();
    }
    if (action === 'pin') await api('/conversations/' + id, {method:'PATCH', body:JSON.stringify({pinned:!item.pinned})});
    if (action === 'delete') {
      if (state.busy && state.active?.id === id) return toast('请先停止这段对话的生成。');
      if (!await question('删除这段对话？', '「' + item.title + '」的消息和附件会从本机删除。')) return;
      await api('/conversations/' + id, {method:'DELETE'});
      if (state.active?.id === id) await newChat();
      try { localStorage.removeItem('grok-draft-' + id); } catch {}
      toast('对话已删除。');
    }
    await refreshHistory();
  });
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); } catch {
    const input = document.createElement('textarea'); input.value = text; input.style.position = 'fixed'; input.style.opacity = '0'; document.body.append(input); input.select(); document.execCommand('copy'); input.remove();
  }
  toast('已复制。');
}
function closeMobileSidebar() {
  $('#sidebar').classList.remove('open');
  $('#sidebarBackdrop').hidden = true;
}
function setSidebar(open) {
  if (window.matchMedia('(max-width: 760px)').matches) {
    $('#sidebar').classList.toggle('open', open);
    $('#sidebarBackdrop').hidden = !open;
  } else {
    $('.app-shell').classList.toggle('sidebar-collapsed', !open);
    try { localStorage.setItem('grok-sidebar-collapsed', String(!open)); } catch {}
  }
}
function bindEvents() {
  bindControlEvents();
  $('#newChat').onclick = safely(newChat);
  $('#openSettings').onclick = openSettings;
  $('#saveSettings').onclick = safely(() => saveSettings(true));
  $('#exitApp').onclick = safely(exitApp);
  $('#fetchModels').onclick = fetchModels;
  $('#toggleImport').onclick = () => { $('#importArea').hidden = !$('#importArea').hidden; if (!$('#importArea').hidden) $('#connectionJson').focus(); };
  $('#importConnection').onclick = importConnection;
  $('#toggleKey').onclick = () => { $('#apiKey').type = $('#apiKey').type === 'password' ? 'text' : 'password'; };
  $('#sendButton').onclick = safely(() => sendMessage());
  $('#imageMode').onclick = () => setMode(state.mode === 'image' ? 'chat' : 'image');
  $('#imageModelSettings').onclick = () => { openSettings(); $('#settingsImageModel').focus(); };
  for (const [selector, key] of [['#imageSize','image_size'], ['#imageCount','image_count']]) $(selector).onchange = safely(async (event) => {
    const value = key === 'image_count' ? Number(event.target.value) : event.target.value;
    state.settings[key] = value;
    if (!isPreview) state.settings = await api('/settings', {method:'POST', body:JSON.stringify({[key]:value})});
  });
  $('#attachFile').onclick = () => $('#fileInput').click(); $('#attachImage').onclick = () => $('#imageInput').click();
  for (const selector of ['#fileInput','#imageInput']) $(selector).onchange = safely(async (event) => { await uploadFiles([...event.target.files]); event.target.value = ''; });
  $('#messageInput').oninput = () => { resizeInput(); saveDraft(); };
  $('#messageInput').onkeydown = (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); safely(() => sendMessage())(); } };
  $('#messageInput').onpaste = safely(async (event) => { const files = [...(event.clipboardData?.files || [])]; if (files.length) { event.preventDefault(); await uploadFiles(files); } });
  $('#attachmentTray').onclick = safely(async (event) => { const id = event.target.closest('[data-remove-attachment]')?.dataset.removeAttachment; if (!id) return; await api('/attachments/' + id, {method:'DELETE'}); state.attachments = state.attachments.filter((item) => item.id !== id); renderPending(); });
  $('#historyList').onclick = safely(async (event) => {
    const button = event.target.closest('[data-history-menu]'); if (button) return showMenu(button, button.dataset.historyMenu);
    const id = event.target.closest('[data-conversation]')?.dataset.conversation; if (id) await openConversation(id);
  });
  $('#historyList').onkeydown = (event) => { if (event.key === 'Enter' && event.target.dataset.conversation) safely(() => openConversation(event.target.dataset.conversation))(); };
  let searchTimer;
  $('#historySearch').oninput = (event) => { state.search = event.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(safely(refreshHistory), 180); };
  $('#modelSelect').onchange = safely(async (event) => {
    const model = event.target.value;
    if (state.mode === 'image') { state.settings = await api('/settings', {method:'POST', body:JSON.stringify({image_model:model})}); return; }
    state.settings = await api('/settings', {method:'POST', body:JSON.stringify({model})});
    if (state.active) { state.active.model = model; await api('/conversations/' + state.active.id, {method:'PATCH', body:JSON.stringify({model})}); }
  });
  $('#themeToggle').onclick = safely(async () => { state.settings.theme = state.settings.theme === 'dark' ? 'light' : 'dark'; applyTheme(); if (!isPreview) await api('/settings', {method:'POST', body:JSON.stringify({theme:state.settings.theme})}); });
  $('#exportChat').onclick = safely(() => exportConversation());
  $('#conversationMenu').onclick = (event) => showMenu(event.currentTarget, state.active?.id);
  $('#sidebarToggle').onclick = () => setSidebar(true);
  $('#sidebarCollapse').onclick = () => setSidebar(false);
  $('#sidebarBackdrop').onclick = closeMobileSidebar;
  window.matchMedia('(max-width: 760px)').addEventListener('change', closeMobileSidebar);
  $('#messages').onclick = safely(async (event) => {
    const copy = event.target.closest('[data-copy]'); if (copy) return copyText(state.active.messages.find((message) => message.id === copy.dataset.copy)?.content || '');
    if (event.target.closest('[data-regenerate]')) return sendMessage(true);
    const code = event.target.closest('.copy-code'); if (code) return copyText(code.parentElement.querySelector('code')?.textContent || '');
    const preview = event.target.closest('[data-preview]'); if (preview) { $('#previewImage').src = fileUrl(preview.dataset.preview, true); $('#imageDialog').showModal(); }
  });
  document.querySelectorAll('.suggestion').forEach((button) => { button.onclick = () => { if (button.dataset.action === 'generate') return setMode('image'); if (button.dataset.action) { setMode('chat'); $(button.dataset.action === 'image' ? '#imageInput' : '#fileInput').click(); } else { $('#messageInput').value = button.dataset.prompt; resizeInput(); saveDraft(); $('#messageInput').focus(); } }; });
  document.querySelectorAll('[data-close]').forEach((button) => { button.onclick = () => $('#' + button.dataset.close).close(); });
  document.addEventListener('click', (event) => { if (!event.target.closest('#contextMenu,[data-history-menu],#conversationMenu')) $('#contextMenu').hidden = true; });
  document.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); if (!document.querySelector('dialog[open]')) safely(newChat)(); } if (event.key === 'Escape') { $('#contextMenu').hidden = true; closeMobileSidebar(); } });
  let dragDepth = 0;
  document.addEventListener('dragenter', (event) => { if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); dragDepth++; $('#dropOverlay').hidden = false; } });
  document.addEventListener('dragover', (event) => event.preventDefault());
  document.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('#dropOverlay').hidden = true; });
  document.addEventListener('drop', safely(async (event) => { event.preventDefault(); dragDepth = 0; $('#dropOverlay').hidden = true; if (event.dataTransfer?.files.length) await uploadFiles([...event.dataTransfer.files]); }));
  window.addEventListener('beforeunload', () => { saveDraft(); if (state.busy && state.assistantId && state.busyKind !== 'image') fetch('/api/chat/' + state.assistantId + '/stop', {method:'POST', headers:{'Content-Type':'application/json','X-GrokDesk-Token':token}, body:'{}', keepalive:true}).catch(() => {}); });
}
async function initialize() {
  try { $('.app-shell').classList.toggle('sidebar-collapsed', localStorage.getItem('grok-sidebar-collapsed') === 'true'); } catch {}
  bindEvents();
  if (isPreview) {
    $('#startupNotice').hidden = false;
    $('#dataLocation').textContent = '启动软件后，聊天和附件会保存在这台电脑。';
    applyTheme(); connectionStatus(); renderModels(); renderHistory(); renderMessages(); resizeInput();
    return;
  }
  const data = await api('/bootstrap'); state.settings = data.settings;
  $('#dataLocation').textContent = data.data_dir;
  try { state.models = JSON.parse(localStorage.getItem('grok-models') || '[]'); } catch {}
  if (!Array.isArray(state.models)) state.models = [];
  applyTheme(); connectionStatus(); renderModels(); await refreshHistory();
  let last;
  try { last = localStorage.getItem('grok-active'); } catch {}
  if (last && state.conversations.some((item) => item.id === last)) await openConversation(last);
  else { restoreDraft(); renderMessages(); }
}
initialize().catch((error) => toast(error.message, true));
