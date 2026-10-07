import { createLiveClient } from '/live/client.js';

const MAX_PHOTO_BYTES = 2 * 1024 * 1024;
const ASSESSMENT_TIMEOUT_MS = 45_000;
const $ = id => document.getElementById(id);
const ui = Object.fromEntries(['statusDot', 'statusText', 'chooseFiles', 'photoAccess', 'fileInput', 'photo', 'emptyState', 'emptyTitle', 'emptyDescription', 'emptyAction', 'photoPosition', 'photoDate', 'evaluation', 'scoreValue', 'description', 'reason', 'reviewHint', 'previous', 'next', 'miraToggle', 'miraLabel', 'miraIcon', 'retry', 'loginDialog', 'loginForm', 'loginCode', 'loginError', 'loginSubmit', 'browseOnly'].map(id => [id, $(id)]));
const state = {
  authenticated: false, authChecked: false, model: null, miraAvailable: null, photo: null,
  wantedIndex: 0, photoTotal: null, photoEpoch: 0, flow: 0, phase: 'idle', liveState: 'off',
  voiceWanted: true, foreground: true, evaluation: null, error: null,
  photoError: null, wait: null, timer: null, cancelUpload: null,
  files: [], nativeInfo: null, loadingPhoto: false, turnInFlight: false,
};

function nativeAvailable() { return typeof window.MediaBankAndroid?.postMessage === 'function'; }
function nativeAction(action, extra = {}) {
  if (!nativeAvailable()) return false;
  window.MediaBankAndroid.postMessage(JSON.stringify({ action, ...extra }));
  return true;
}
function fault(code, message) { return Object.assign(new Error(message), { code }); }
function current(epoch, flow) { return epoch === state.photoEpoch && flow === state.flow && state.foreground; }
function clearTimer() { clearTimeout(state.timer); state.timer = null; }
function setError(message, retry = true) { state.error = { message, retry }; state.phase = 'idle'; clearTimer(); render(); }
function friendlyError(error) {
  const code = String(error?.code ?? '').toUpperCase();
  if (error?.status === 401 || code.includes('AUTH') || code.includes('LOGIN')) return 'Введите код доступа, чтобы подключить Миру.';
  if (code.includes('RESOURCE') || code.includes('BUDGET') || code.includes('CAPACITY') || code.includes('LIMIT')) return 'Мира сейчас занята. Попробуйте подключиться немного позже.';
  if (code.includes('MICROPHONE') || code.includes('AUDIO_WORKLET') || error?.name === 'NotAllowedError') return 'Разрешите доступ к микрофону и нажмите «Подключить Миру».';
  if (code.includes('PLAYBACK')) return 'Звук не запустился. Нажмите «Попробовать ещё раз».';
  if (code.includes('PHOTO_TOO_LARGE') || code.includes('PHOTO_SIZE')) return 'Фотография слишком большая для просмотра Мирой. Откройте другую.';
  if (code.includes('PHOTO_STALE') || code.includes('PHOTO_NOT_FOUND')) return 'Фотография больше не доступна Мире. Нажмите «Попробовать ещё раз».';
  if (code.includes('PHOTO_FORMAT')) return 'Не удалось прочитать фотографию. Откройте другую.';
  if (navigator.onLine === false) return 'Нет сети. Фотографии можно листать; Миру подключим после восстановления связи.';
  return 'Не удалось связаться с Мирой. Фотография на месте — можно попробовать ещё раз.';
}

async function request(url, options = {}) {
  // Shared start() does not supply its own signal. Its server readiness bound is
  // 35 s; retain the product's independent 45 s assessment/Stop bound above it.
  const timeout = new URL(url, location.href).pathname === '/api/live' ? 40_000 : 30_000;
  const response = await fetch(url, {
    cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(timeout), ...options,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = fault(result.error?.code ?? `HTTP_${response.status}`, result.error?.message ?? 'Ошибка подключения');
    error.status = response.status;
    if (response.status === 401) { state.authenticated = false; render(); }
    throw error;
  }
  return result;
}

const live = createLiveClient({
  transport: 'wss', request,
  suppressCaptureDuringPlayback: 'adaptive',
  manualActivityDetection: true,
  speechEndSilenceMs: 2000,
  onState(value, detail = {}) {
    state.liveState = value;
    if (value === 'off') {
      state.turnInFlight = false;
      if (!['photo_changed', 'user_stop', 'background', 'offline', 'retry', 'assessment_timeout'].includes(detail.reason) && state.phase !== 'idle') {
        setError(friendlyError(detail));
      }
    }
    if (value === 'microphone_unavailable') setError(friendlyError({ code: 'MICROPHONE_UNAVAILABLE' }));
    if (value === 'budget_wait') state.wait = { stage: 'resource', elapsed_ms: 0 };
    if (value === 'budget_ready') state.wait = null;
    render();
  },
  onEvent(event, generation) {
    if (generation !== live.generation || !state.foreground) return;
    if (event.type === 'input_transcript' || event.type === 'interim_input_transcript') state.turnInFlight = true;
    if (event.type === 'turn_complete') state.turnInFlight = false;
    if (event.type === 'photo_evaluation') {
      if (!state.photo?.serverId || event.photo_id !== state.photo.serverId) return;
      const score = Number(event.score);
      if (!Number.isFinite(score) || score < 1 || score > 10 || typeof event.description !== 'string') {
        setError('Мира ответила без корректной оценки. Можно попросить ещё раз.');
        return;
      }
      state.evaluation = { ...event, score, description: event.description.slice(0, 1000), reason: String(event.reason ?? '').slice(0, 500) };
      state.error = null; state.phase = 'idle'; clearTimer(); render();
    } else if (event.type === 'photo_next') {
      if (event.photo_id === state.photo?.serverId) navigate(1);
    } else if (event.type === 'error') {
      if (event.photo_id && event.photo_id !== state.photo?.serverId) return;
      setError(friendlyError(event));
    }
  },
  onNotice(notice, error) {
    if (notice === 'voice_stop_confirmation_requested') {
      ui.reviewHint.textContent = 'Мира уточнит, остановить ли разговор. Кнопка «Стоп» выключает её сразу.';
      return;
    }
    if (['start_error', 'microphone_error', 'playback_error', 'transport_error', 'provider_failure', 'resource_denial'].includes(notice)) {
      setError(friendlyError(error));
    }
    if (error?.status === 401) showLogin();
  },
  onTiming(event) { if (event === 'speech_start') state.turnInFlight = true; },
  onWait(wait) { state.wait = wait; render(); },
});

function availability() {
  if (state.photoError) return { text: state.photoError.title, tone: 'error' };
  if (navigator.onLine === false) return { text: 'Без сети · фотографии доступны', tone: 'idle' };
  if (state.error) return { text: 'Мира · нужна ваша помощь', tone: 'error' };
  if (state.phase === 'checking') return { text: 'Мира · проверяю подключение', tone: 'busy' };
  if (!state.authenticated) return { text: 'Мира · нужен код доступа', tone: 'idle' };
  if (state.miraAvailable === false) return { text: 'Мира · сервер пока не готов', tone: 'error' };
  if (state.phase === 'uploading') return { text: 'Мира · получает фотографию', tone: 'busy' };
  if (state.liveState === 'budget_wait' || state.wait?.stage === 'resource') return { text: 'Мира · ждёт свободный ресурс', tone: 'busy' };
  if (state.liveState === 'reconnecting') return { text: 'Мира · восстанавливает связь', tone: 'busy' };
  if (state.phase === 'starting' || state.liveState === 'starting') return { text: 'Мира · подключается', tone: 'busy' };
  if (state.liveState === 'answering') return { text: 'Мира · рассказывает', tone: 'ready' };
  if (state.phase === 'evaluating') {
    const seconds = Math.floor((state.wait?.elapsed_ms ?? 0) / 1000);
    return { text: `Мира · смотрит фотографию${seconds ? ` · ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : ''}`, tone: 'busy' };
  }
  if (live.sessionId) return { text: 'Мира · слушает вас', tone: 'ready' };
  return { text: 'Мира · на паузе', tone: 'idle' };
}

function render() {
  const status = availability();
  ui.statusText.textContent = status.text; ui.statusDot.dataset.tone = status.tone;
  ui.chooseFiles.hidden = nativeAvailable();
  ui.photoAccess.hidden = !nativeAvailable();
  ui.photoAccess.textContent = state.nativeInfo?.photoAccess === 'selected' ? 'Выбрать фото' : 'Доступ к фото';
  const photo = state.photo;
  const navigable = !state.loadingPhoto && !state.photoError?.blocksNavigation && Boolean(photo || state.photoError);
  ui.previous.disabled = !navigable || state.wantedIndex <= 0;
  ui.next.disabled = !navigable || (state.photoTotal !== null && state.wantedIndex + 1 >= state.photoTotal);
  ui.photoPosition.textContent = state.photoTotal ? `${Math.min(state.wantedIndex + 1, state.photoTotal)} / ${state.photoTotal}${photo?.access === 'selected' ? ' · выбранные' : ''}` : 'От новых к старым';
  ui.photoDate.textContent = photo ? formatDate(photo.takenAt) : '';
  ui.evaluation.hidden = !state.evaluation;
  if (state.evaluation) {
    ui.scoreValue.textContent = new Intl.NumberFormat('ru', { maximumFractionDigits: 1 }).format(state.evaluation.score);
    ui.description.textContent = state.evaluation.description;
    ui.reason.textContent = state.evaluation.reason;
    ui.photo.alt = state.evaluation.description;
  }
  const active = Boolean(live.sessionId || live.starting || state.phase !== 'idle');
  ui.miraToggle.dataset.active = String(active);
  ui.miraLabel.textContent = active ? 'Стоп' : state.error ? 'Подключить Миру' : 'Позвать Миру';
  ui.miraIcon.textContent = active ? '■' : '●';
  ui.miraToggle.disabled = !photo && state.authenticated;
  ui.retry.hidden = !state.error?.retry;
  ui.reviewHint.hidden = Boolean(state.evaluation && !state.error && state.phase === 'idle');
  ui.reviewHint.textContent = state.error?.message ?? (
    state.loadingPhoto ? 'Открываю фотографию…' : !photo ? 'Оценка появится здесь.' : !state.authenticated ? 'Подключите Миру, чтобы услышать описание и оценку.' :
    state.phase === 'checking' ? 'Проверяю сохранённый доступ к серверу.' : state.phase === 'uploading' ? 'Передаю уменьшенную копию для просмотра.' : state.phase === 'starting' ? 'Подключаю голос. Разрешите доступ к микрофону.' :
    state.phase === 'evaluating' ? 'Мира оценит открыточность и объяснит свой выбор.' : 'Можно поговорить о фотографии или перейти к следующей.'
  );
}

function formatDate(value) {
  if (!value) return '';
  const number = Number(value);
  const date = Number.isFinite(number) && number > 0 ? new Date(number < 1e12 ? number * 1000 : number) : new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString('ru', { day: 'numeric', month: 'short', year: 'numeric' }) : '';
}

function invalidateFlow(reason, stop = false) {
  state.flow += 1; clearTimer();
  state.cancelUpload?.(); state.cancelUpload = null; state.phase = 'idle'; state.wait = null;
  if (stop && (live.sessionId || live.starting)) live.stop({ reason, keepalive: reason === 'background' });
}

function stopMira(reason = 'user_stop') {
  state.voiceWanted = false;
  invalidateFlow(reason, true); state.turnInFlight = false; state.error = null; render();
}

function dataUrlBlob(dataUrl) {
  if (typeof dataUrl !== 'string' || !dataUrl.startsWith('data:image/jpeg;base64,') || dataUrl.length > MAX_PHOTO_BYTES * 4 / 3 + 128) throw fault('PHOTO_FORMAT', 'Некорректная фотография');
  const encoded = dataUrl.slice(dataUrl.indexOf(',') + 1);
  const raw = atob(encoded);
  if (raw.length > MAX_PHOTO_BYTES) throw fault('PHOTO_TOO_LARGE', 'Фотография слишком большая');
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return new Blob([bytes], { type: 'image/jpeg' });
}

function uploadPhoto(photo, epoch, flow) {
  const blob = photo.blob ?? dataUrlBlob(photo.dataUrl);
  const url = new URL('/api/photos', location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    let settled = false;
    const timer = setTimeout(() => finish(fault('PHOTO_UPLOAD_TIMEOUT', 'Передача фотографии заняла слишком долго')), 20_000);
    const cancel = () => finish(fault('CANCELLED', 'Отменено'));
    state.cancelUpload = cancel;
    function finish(error, result) {
      if (settled) return; settled = true; clearTimeout(timer);
      if (state.cancelUpload === cancel) state.cancelUpload = null;
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      socket.close(1000);
      if (error) reject(error); else resolve(result);
    }
    socket.onopen = () => {
      if (!current(epoch, flow)) return cancel();
      socket.send(blob);
    };
    socket.onmessage = event => {
      if (!current(epoch, flow)) return cancel();
      let result;
      try { result = JSON.parse(event.data); } catch { return finish(fault('PHOTO_RESPONSE_INVALID', 'Некорректный ответ сервера')); }
      if (result.error) return finish(fault(result.error.code, result.error.message));
      if (typeof result.photo_id !== 'string' || !result.photo_id || result.photo_id.length > 200) return finish(fault('PHOTO_RESPONSE_INVALID', 'Нет подтверждения фотографии'));
      finish(null, result);
    };
    socket.onerror = () => finish(fault('PHOTO_CONNECTION', 'Не удалось передать фотографию'));
    socket.onclose = () => finish(fault('PHOTO_CONNECTION', 'Передача фотографии прервалась'));
  });
}

async function assessPhoto({ retry = false } = {}) {
  if (!state.photo || !state.foreground || !state.voiceWanted) return;
  if (!state.authenticated) { showLogin(); return; }
  if (navigator.onLine === false) { setError(friendlyError({})); return; }
  if (state.miraAvailable === false) {
    if (retry) {
      try { applyMe(await request('/api/me', { signal: AbortSignal.timeout(10_000) })); }
      catch (error) { setError(friendlyError(error)); return; }
    }
    if (state.miraAvailable === false) { setError('Сервер Миры пока не готов к разговору. Фотографии можно просматривать.'); return; }
  }
  if (retry) { invalidateFlow('retry', true); state.photo.serverId = null; }
  else invalidateFlow('photo_changed');
  const photo = state.photo, epoch = state.photoEpoch, flow = state.flow;
  state.error = null; state.phase = 'uploading'; render();
  state.timer = setTimeout(() => {
    if (!current(epoch, flow)) return;
    invalidateFlow('assessment_timeout', true);
    setError('Мира пока не ответила. Можно попробовать ещё раз или перейти к другой фотографии.');
  }, ASSESSMENT_TIMEOUT_MS);
  try {
    if (!photo.serverId) {
      const receipt = await uploadPhoto(photo, epoch, flow);
      if (!current(epoch, flow)) return;
      photo.serverId = receipt.photo_id;
    }
    if (!live.sessionId) {
      state.phase = 'starting'; render();
      const started = await live.start({
        url: '/api/live', body: { photo_id: photo.serverId }, microphone: true, captureDuringStart: false,
        authorize: async () => {
          if (!current(epoch, flow) || !state.authenticated) throw fault('CANCELLED', 'Отменено');
        },
      });
      if (!current(epoch, flow)) return;
      if (!started || !live.sessionId) {
        if (!state.error) setError('Мира не подключилась. Нажмите «Попробовать ещё раз».');
        return;
      }
      if (state.liveState === 'microphone_unavailable') {
        invalidateFlow('user_stop', true);
        setError(friendlyError({ code: 'MICROPHONE_UNAVAILABLE' }));
        return;
      }
    }
    if (!current(epoch, flow)) return;
    state.phase = 'evaluating'; state.turnInFlight = true; render();
    await live.input({ photo_id: photo.serverId, request_id: crypto.randomUUID() });
  } catch (error) {
    if (!current(epoch, flow) || error?.code === 'CANCELLED') return;
    setError(friendlyError(error));
    if (error?.status === 401) showLogin();
  }
}

function acceptPhoto(photo) {
  if (!photo || Number(photo.index) !== state.wantedIndex || typeof photo.dataUrl !== 'string') return;
  const stop = live.starting || state.phase !== 'idle' || state.turnInFlight || live.playingCount > 0 || state.liveState === 'answering';
  invalidateFlow('photo_changed', stop);
  state.photoEpoch += 1;
  state.photo = { ...photo, index: Number(photo.index), total: Math.max(1, Number(photo.total)), serverId: null };
  state.photoTotal = state.photo.total;
  state.loadingPhoto = false; state.evaluation = null; state.error = null; state.photoError = null;
  ui.photo.src = photo.dataUrl; ui.photo.alt = 'Текущая фотография'; ui.photo.hidden = false; ui.emptyState.hidden = true;
  render();
  if (state.authenticated && state.voiceWanted && state.foreground) void assessPhoto();
}

function photoFailure(error) {
  if (Number.isInteger(error?.index) && error.index !== state.wantedIndex) return;
  invalidateFlow('photo_changed', true);
  state.photoEpoch += 1; state.loadingPhoto = false; state.photo = null; state.evaluation = null;
  ui.photo.hidden = true; ui.photo.removeAttribute('src');
  const code = String(error?.code ?? '').toLowerCase();
  const permission = code.includes('permission') || code.includes('access');
  const empty = code.includes('empty') || code.includes('no_photo');
  const end = code === 'end_of_photos';
  if (empty) state.photoTotal = 0;
  if (end) state.photoTotal = state.wantedIndex;
  state.photoError = {
    title: permission ? 'Нужен доступ к фотографиям' : empty ? 'Фотографий пока нет' : end ? 'Фотографии закончились' : 'Не удалось открыть фотографию',
    message: permission ? 'Разрешите доступ ко всем фотографиям или выберите нужные.' : empty ? 'Добавьте снимки на телефон или выберите фотографии для приложения.' : end ? 'Можно вернуться к предыдущим снимкам или выбрать другие.' : 'Этот снимок не читается. Можно перейти к следующему.',
    blocksNavigation: permission || empty,
  };
  if (!state.photo) {
    ui.emptyState.hidden = false; ui.emptyTitle.textContent = state.photoError.title;
    ui.emptyDescription.textContent = state.photoError.message;
    ui.emptyAction.textContent = permission ? 'Разрешить доступ' : 'Открыть фотографии';
  }
  render();
}

function requestPhoto(index) {
  if (!Number.isInteger(index) || index < 0) return;
  state.wantedIndex = index; state.loadingPhoto = true; render();
  if (!nativeAction('photo', { index })) void openBrowserPhoto(index);
}

function navigate(direction) {
  if (state.loadingPhoto || state.photoError?.blocksNavigation || (!state.photo && !state.photoError)) return;
  const index = state.wantedIndex + direction;
  if (index < 0 || (state.photoTotal !== null && index >= state.photoTotal)) return;
  // End old speech immediately; the new image itself arrives asynchronously.
  if (live.starting || state.phase !== 'idle' || state.turnInFlight || live.playingCount > 0 || state.liveState === 'answering') invalidateFlow('photo_changed', true);
  requestPhoto(index);
}

async function openBrowserPhoto(index) {
  const file = state.files[index];
  if (!file) { state.loadingPhoto = false; render(); return; }
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
    if (index !== state.wantedIndex) return;
    const canvas = document.createElement('canvas');
    let edge = 2048, blob;
    for (const quality of [.85, .73, .6]) {
      const scale = Math.min(1, edge / Math.max(bitmap.width, bitmap.height));
      canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const context = canvas.getContext('2d', { alpha: false });
      context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (blob && blob.size <= MAX_PHOTO_BYTES) break;
      edge = Math.round(edge * .75);
    }
    if (!blob || blob.size > MAX_PHOTO_BYTES) throw fault('PHOTO_TOO_LARGE', 'Фотография слишком большая');
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(blob);
    });
    acceptPhoto({ id: `browser-${index}-${file.lastModified}`, index, total: state.files.length, name: file.name, takenAt: file.lastModified, dataUrl, blob, width: canvas.width, height: canvas.height });
    canvas.width = canvas.height = 0;
  } catch (error) { if (index === state.wantedIndex) photoFailure(error); }
  finally { bitmap?.close(); }
}

function showLogin() {
  if (!ui.loginDialog.open && state.foreground) ui.loginDialog.showModal();
}

function chooseNativePhotos() {
  invalidateFlow('photo_changed', true);
  state.photoEpoch += 1; state.wantedIndex = 0; state.photoTotal = null;
  state.photo = null; state.evaluation = null; state.photoError = null; state.error = null; state.loadingPhoto = true;
  ui.photo.hidden = true; ui.photo.removeAttribute('src'); ui.emptyState.hidden = false;
  ui.emptyTitle.textContent = 'Открываю фотографии';
  ui.emptyDescription.textContent = 'Выберите снимки или разрешите доступ к фотографиям.';
  ui.emptyAction.textContent = 'Выбрать фотографии';
  render(); nativeAction('permissions');
}

async function startByUser() {
  invalidateFlow('retry', true);
  state.voiceWanted = true; state.error = null; state.phase = 'checking'; render();
  const epoch = state.photoEpoch, flow = state.flow;
  try {
    // A network failure is not a lost login: check the existing HttpOnly cookie
    // before asking for another one-use invitation.
    const result = await request('/api/me', { signal: AbortSignal.timeout(10_000) });
    if (!current(epoch, flow)) return;
    applyMe(result); state.phase = 'idle'; render();
    if (!state.authenticated) { showLogin(); return; }
    if (state.photo) await assessPhoto({ retry: true });
  } catch (error) {
    if (!current(epoch, flow)) return;
    setError(friendlyError(error));
  }
}

async function login(code) {
  ui.loginSubmit.disabled = true; ui.loginError.textContent = '';
  try {
    await request('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ code }), signal: AbortSignal.timeout(15_000) });
    state.authenticated = true; state.authChecked = true; state.voiceWanted = true; state.error = null;
    ui.loginCode.value = ''; ui.loginDialog.close(); render();
    await checkAuth();
    return true;
  } catch (error) {
    ui.loginError.textContent = navigator.onLine === false ? 'Нет связи с сервером. Можно пока смотреть фотографии.' : [400, 401, 403].includes(error.status) ? 'Код не подошёл или уже истёк. Проверьте его и попробуйте ещё раз.' : 'Сервер не ответил. Попробуйте ещё раз.';
    showLogin(); return false;
  } finally { ui.loginSubmit.disabled = false; }
}

function applyMe(result) {
  state.authenticated = result.authenticated === true;
  state.authChecked = true; state.model = result.model ?? null;
  state.miraAvailable = typeof result.mira_available === 'boolean' ? result.mira_available : null;
}

async function checkAuth() {
  try {
    const result = await request('/api/me', { signal: AbortSignal.timeout(10_000) });
    applyMe(result);
    if (!state.authenticated) showLogin();
    else if (state.photo && state.voiceWanted && state.foreground) void assessPhoto();
  } catch (error) {
    state.authChecked = true;
    if (error.status === 401) showLogin();
    else setError(friendlyError(error));
  }
  render();
}

function background() {
  if (!state.foreground) return;
  state.foreground = false; stopMira('background');
}
function foreground() {
  if (state.foreground) return;
  state.foreground = true; state.error = null;
  nativeAction('app_info'); render();
  // Do not replay a partly captured voice turn on return. The Start button resumes.
}

window.MyMediaBank = {
  onPhoto: acceptPhoto,
  onNativeError(error) {
    if (String(error?.code ?? '').toLowerCase().includes('microphone')) setError(friendlyError({ code: 'MICROPHONE_UNAVAILABLE' }));
    else photoFailure(error);
  },
  onAppInfo(info) { state.nativeInfo = info; render(); },
  onPermissions(info) {
    state.nativeInfo = { ...state.nativeInfo, ...info };
    if (info?.photoAccess !== 'denied') state.photoError = null;
    render();
  },
  onBackground: background,
  onForeground: foreground,
};

ui.previous.addEventListener('click', () => navigate(-1));
ui.next.addEventListener('click', () => navigate(1));
ui.miraToggle.addEventListener('click', () => {
  if (live.sessionId || live.starting || state.phase !== 'idle') { stopMira(); return; }
  void startByUser();
});
ui.retry.addEventListener('click', () => void startByUser());
ui.chooseFiles.addEventListener('click', () => ui.fileInput.click());
ui.photoAccess.addEventListener('click', chooseNativePhotos);
ui.emptyAction.addEventListener('click', () => {
  if (nativeAvailable()) chooseNativePhotos(); else ui.fileInput.click();
});
ui.fileInput.addEventListener('change', () => {
  state.files = Array.from(ui.fileInput.files ?? []).filter(file => file.type.startsWith('image/')).sort((a, b) => b.lastModified - a.lastModified);
  if (state.files.length) requestPhoto(0);
  ui.fileInput.value = '';
});
ui.loginForm.addEventListener('submit', event => { event.preventDefault(); const code = ui.loginCode.value.trim(); if (code) void login(code); });
ui.browseOnly.addEventListener('click', () => { state.voiceWanted = false; ui.loginDialog.close(); render(); });
ui.loginDialog.addEventListener('cancel', () => { state.voiceWanted = false; });
document.addEventListener('visibilitychange', () => { if (document.hidden) background(); else foreground(); });
window.addEventListener('pagehide', background);
window.addEventListener('offline', () => { stopMira('offline'); setError(friendlyError({})); });
window.addEventListener('online', () => { state.error = null; render(); });
document.addEventListener('keydown', event => {
  if (ui.loginDialog.open || ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) return;
  if (event.key === 'ArrowRight') { event.preventDefault(); navigate(1); }
  if (event.key === 'ArrowLeft') { event.preventDefault(); navigate(-1); }
});

render();
if (nativeAvailable()) { nativeAction('app_info'); requestPhoto(0); }
const invitation = new URLSearchParams(location.hash.slice(1)).get('invite');
if (invitation) {
  history.replaceState(null, '', location.pathname + location.search);
  if (invitation.length <= 128) void login(invitation); else void checkAuth();
} else void checkAuth();
