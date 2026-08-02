const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  token: localStorage.getItem('taskflow_token'),
  user: null,
  tasks: [],
  projects: [],
  kanbanColumns: [],
  archivedProjects: [],
  checklistItems: [],
  checklistTaskId: null,
  discussionMessages: [],
  discussionTaskId: null,
  noteFolders: [],
  notes: [],
  noteLinks: [],
  noteSearchResults: null,
  noteFilter: 'all',
  activeNoteId: null,
  noteMode: 'view',
  noteSaveTimer: null,
  noteSaveInFlight: false,
  noteSavePending: false,
  expandedChecklistTasks: new Set(),
  filter: 'today',
  sort: 'priority',
  view: 'list',
  calendarWeekStart: null,
  savedFilterIndex: -1,
  taskFilters: { query: '', project: 'all', status: 'all', priority: 'all', tag: '', date: 'all', dateFrom: '', dateTo: '' },
};

let focusTicker;
function focusState() {
  try {
    const value = JSON.parse(localStorage.getItem('taskflow_focus') || '{}');
    return {
      taskId: typeof value.taskId === 'string' ? value.taskId : '',
      seconds: Number.isFinite(value.seconds) && value.seconds >= 0 ? value.seconds : 0,
      startedAt: Number.isFinite(value.startedAt) && value.startedAt > 0 ? value.startedAt : null,
    };
  } catch (_) {
    localStorage.removeItem('taskflow_focus');
    return { taskId: '', seconds: 0, startedAt: null };
  }
}
function saveFocus(value) { localStorage.setItem('taskflow_focus', JSON.stringify(value)); }
function renderFocusTimer() {
  const focus = focusState();
  const seconds = focus.seconds + (focus.startedAt ? Math.floor((Date.now() - focus.startedAt) / 1000) : 0);
  const formatted = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  $('#focusTime').textContent = formatted;
  $('#focusStart').hidden = Boolean(focus.startedAt);
  $('#focusPause').hidden = !focus.startedAt;
  $('#focusReset').disabled = !focus.seconds && !focus.startedAt;
  const badge = $('#focusBadge');
  badge.hidden = !focus.startedAt;
  badge.textContent = formatted;
  $('#focusTimer').classList.toggle('running', Boolean(focus.startedAt));
}
function openFocusTimer() {
  const focus = focusState();
  const active = state.tasks.filter(task => task.status !== 'done');
  $('#focusTask').innerHTML = active.map(task => `<option value="${task.id}">${escapeHtml(task.title)}</option>`).join('') || '<option value="">Нет активных задач</option>';
  $('#focusTask').value = active.some(task => task.id === focus.taskId) ? focus.taskId : active[0]?.id || '';
  $('#focusTask').disabled = active.length === 0;
  $('#focusStart').disabled = active.length === 0;
  $('#focusDone').disabled = active.length === 0;
  $('#focusReset').disabled = !focus.seconds && !focus.startedAt;
  $('#focusEmpty').hidden = active.length > 0;
  renderFocusTimer();
  clearInterval(focusTicker);
  focusTicker = setInterval(renderFocusTimer, 1000);
  $('#focusDialog').showModal();
}
$('#focusTimer').addEventListener('click', openFocusTimer);
$('#focusDialog').addEventListener('close', () => clearInterval(focusTicker));
$('#focusStart').addEventListener('click', () => {
  const focus = focusState();
  const taskId = $('#focusTask').value;
  if (!taskId) return;
  saveFocus({ taskId, seconds: focus.taskId === taskId ? focus.seconds : 0, startedAt: Date.now() });
  const task = state.tasks.find(item => item.id === taskId);
  if (task?.status !== 'in_progress') patchTask(task, { status: 'in_progress' });
  renderFocusTimer();
});
$('#focusPause').addEventListener('click', () => {
  const focus = focusState();
  saveFocus({ ...focus, seconds: focus.seconds + Math.floor((Date.now() - focus.startedAt) / 1000), startedAt: null });
  renderFocusTimer();
});
$('#focusReset').addEventListener('click', () => { saveFocus({ taskId: $('#focusTask').value, seconds: 0, startedAt: null }); renderFocusTimer(); });
$('#focusDone').addEventListener('click', () => {
  const task = state.tasks.find(item => item.id === $('#focusTask').value);
  if (task) patchTask(task, { status: 'done' });
  saveFocus({ taskId: '', seconds: 0, startedAt: null });
  renderFocusTimer();
  $('#focusDialog').close();
});
$('#mobileFocus').addEventListener('click', () => {
  $('#mobileMoreDialog').close();
  openFocusTimer();
});
const today = () => new Date().toLocaleDateString('sv-SE');
const localDate = value => {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day, 12);
};
const dateValue = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const addDays = (value, days) => { const date = localDate(value); date.setDate(date.getDate() + days); return dateValue(date); };
const startOfWeek = value => { const date = localDate(value); date.setDate(date.getDate() - ((date.getDay() + 6) % 7)); return dateValue(date); };
const isTaskOverdue = task => Boolean(task.due_at && new Date(task.due_at).getTime() < Date.now() && task.status !== 'done');
const formatPlannedDate = value => new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' }).format(new Date(`${value}T12:00:00`));
const formatDueAt = value => new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
const toDateTimeLocal = value => {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const COLUMN_STATUS_LABELS = { inbox: 'Входящие', todo: 'Запланировано', in_progress: 'В работе', done: 'Выполнено' };
const COLUMN_STATUS_HINTS = { inbox: 'Новые идеи и задачи', todo: 'Готово к началу', in_progress: 'Текущий фокус', done: 'Готово' };

const ICON_NAMES = { check: 'check', checklist: 'checklist', chevron: 'chevron_right', edit: 'edit', move: 'drive_file_move', trash: 'delete', more: 'more_horiz', clock: 'history', message: 'chat' };
function icon(name) {
  return `<span class="ui-icon material-symbols-rounded" aria-hidden="true">${ICON_NAMES[name]}</span>`;
}

function applyTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === 'dark' ? '#171816' : '#f5f3ee';
  $$('[data-theme-toggle]').forEach(button => {
    const dark = theme === 'dark';
    button.querySelector('span').textContent = dark ? 'light_mode' : 'dark_mode';
    button.title = dark ? 'Включить светлую тему' : 'Включить тёмную тему';
    button.setAttribute('aria-label', button.title);
  });
  if (persist) localStorage.setItem('taskflow_theme', theme);
}

$$('[data-theme-toggle]').forEach(button => button.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
}));
applyTheme(document.documentElement.dataset.theme || 'light', false);
if (!localStorage.getItem('taskflow_theme')) {
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', event => applyTheme(event.matches ? 'dark' : 'light', false));
}
$('#skipLink').addEventListener('click', event => {
  const target = document.querySelector(event.currentTarget.hash);
  if (target) requestAnimationFrame(() => target.focus());
});
const api = async (path, options = {}) => {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...options.headers },
  });
  const data = await response.json();
  if (response.status === 401 && state.token) logout();
  if (!response.ok) {
    const error = new Error(data.error?.message || 'Не удалось выполнить запрос');
    error.code = data.error?.code;
    throw error;
  }
  return data;
};
const reminderTimers = new Map();
const shownReminderKey = task => `taskflow_reminder_${task.id}_${task.due_at}`;

function scheduleReminders() {
  reminderTimers.forEach(timer => clearTimeout(timer));
  reminderTimers.clear();
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  state.tasks.filter(task => task.status !== 'done' && task.due_at && task.reminder_offsets?.length).forEach(task => {
    task.reminder_offsets.forEach(offset => {
      const at = new Date(task.due_at).getTime() - offset * 60_000;
      const delay = at - Date.now();
      const key = `${shownReminderKey(task)}_${offset}`;
      if (delay < 0 || delay > 2_147_000_000 || localStorage.getItem(key)) return;
      reminderTimers.set(key, setTimeout(() => {
        new Notification(offset ? `Через ${offset === 1440 ? 'день' : offset === 60 ? 'час' : '15 минут'}: ${task.title}` : `Срок задачи: ${task.title}`, { body: 'Откройте TaskFlow, чтобы посмотреть задачу.' });
        localStorage.setItem(key, '1');
      }, delay));
    });
  });
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  $('#srStatus').textContent = message;
  clearTimeout(toast.hideTimer);
  toast.hideTimer = setTimeout(() => element.classList.remove('show'), message.length > 55 ? 4200 : 2600);
}

let confirmResolver = null;
function finishConfirmation(accepted) {
  if (!confirmResolver) return;
  const resolve = confirmResolver;
  confirmResolver = null;
  $('#confirmDialog').close();
  resolve(accepted);
}

function confirmAction({ title, message, confirmLabel = 'Продолжить', danger = false }) {
  if (confirmResolver) finishConfirmation(false);
  $('#confirmTitle').textContent = title;
  $('#confirmMessage').textContent = message;
  const accept = $('#confirmAccept');
  accept.textContent = confirmLabel;
  accept.classList.toggle('danger', danger);
  accept.classList.toggle('primary', !danger);
  $('#confirmDialog').showModal();
  return new Promise(resolve => { confirmResolver = resolve; });
}

$('#confirmCancel').addEventListener('click', () => finishConfirmation(false));
$('#confirmAccept').addEventListener('click', () => finishConfirmation(true));
$('#confirmDialog').addEventListener('cancel', event => {
  event.preventDefault();
  finishConfirmation(false);
});

function setAuthenticated(yes) {
  $('#landingView').hidden = true;
  $('#authView').hidden = yes;
  $('#appView').hidden = !yes;
  $('#skipLink').href = yes ? '#appMain' : '#authForm';
}

function showLanding() {
  $('#landingView').hidden = false;
  $('#authView').hidden = true;
  $('#appView').hidden = true;
  $('#skipLink').href = '#landingMain';
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function showAuth(register = false) {
  $('#landingView').hidden = true;
  $('#authView').hidden = false;
  $('#appView').hidden = true;
  $('#authForm').hidden = false;
  $('#passwordResetForm').hidden = true;
  $('#skipLink').href = '#authForm';
  setRegisterMode(register);
  requestAnimationFrame(() => $('#authForm').elements[register ? 'display_name' : 'email'].focus());
}

function showPasswordReset(token = '') {
  $('#landingView').hidden = true;
  $('#authView').hidden = false;
  $('#appView').hidden = true;
  $('#authForm').hidden = true;
  $('#passwordResetForm').hidden = false;
  $('#passwordResetEmailField').hidden = Boolean(token);
  $('#passwordResetNewPasswordField').hidden = !token;
  $('#passwordResetNewPasswordField input').required = Boolean(token);
  $('#passwordResetTitle').textContent = token ? 'Установить новый пароль' : 'Сбросить пароль';
  $('#passwordResetDescription').textContent = token ? 'Введите новый пароль для вашего аккаунта.' : 'Укажите подтверждённый email. Мы отправим ссылку для установки нового пароля.';
  $('#passwordResetSubmit').textContent = token ? 'Сохранить новый пароль' : 'Отправить ссылку';
  $('#passwordResetForm').dataset.token = token;
  $('#passwordResetError').textContent = '';
  $('#passwordResetSuccess').textContent = '';
  requestAnimationFrame(() => $('#passwordResetForm').elements[token ? 'new_password' : 'email'].focus());
}

async function bootstrap() {
  if (!state.token) return showLanding();
  try {
    const [{ user }, { tasks }, { projects: allProjects }, { columns: kanbanColumns }, { checklist_items: checklistItems }, { folders: noteFolders }, { notes }, { note_links: noteLinks }, health] = await Promise.all([api('/me'), api('/tasks'), api('/projects?include_archived=true'), api('/kanban/columns'), api('/checklist'), api('/note-folders'), api('/notes'), api('/note-links'), fetch('/api/health').then(response => response.json())]);
    const projects = allProjects.filter(project => !project.archived_at);
    const archivedProjects = allProjects.filter(project => project.archived_at);
    Object.assign(state, { user, tasks, projects, archivedProjects, kanbanColumns, checklistItems, noteFolders, notes, noteLinks, version: health.version });
    setAuthenticated(true);
    render();
  } catch {
    logout();
  }
}

let registerMode = false;
function setRegisterMode(enabled) {
  registerMode = enabled;
  $('#nameField').hidden = !registerMode;
  $('#authTitle').textContent = registerMode ? 'Создать пространство' : 'Войти в TaskFlow';
  $('#authEyebrow').textContent = registerMode ? 'НАЧНЁМ С ЧИСТОГО ЛИСТА' : 'С ВОЗВРАЩЕНИЕМ';
  $('#authSubmit').textContent = registerMode ? 'Создать аккаунт' : 'Войти';
  $('#authToggle').textContent = registerMode ? 'Уже есть аккаунт? Войти' : 'Нет аккаунта? Создать';
  $('#nameField input').required = registerMode;
  $('#authError').textContent = '';
  $('#authSuccess').textContent = '';
  $('#resendVerification').hidden = true;
}
$('#authToggle').addEventListener('click', () => setRegisterMode(!registerMode));
$$('[data-open-auth]').forEach(button => button.addEventListener('click', () => showAuth(button.dataset.openAuth === 'register')));
$$('[data-back-landing]').forEach(button => button.addEventListener('click', showLanding));

$('#authForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  $('#authError').textContent = '';
  $('#authSuccess').textContent = '';
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    const data = await api(`/auth/${registerMode ? 'register' : 'login'}`, { method: 'POST', body: JSON.stringify(values) });
    if (data.verification_required) {
      setRegisterMode(false);
      event.currentTarget.elements.email.value = data.email;
      event.currentTarget.elements.password.value = '';
      $('#authSuccess').textContent = `Письмо отправлено на ${data.email}. Перейдите по ссылке, чтобы активировать аккаунт.`;
      $('#resendVerification').hidden = false;
      return;
    }
    state.token = data.token;
    localStorage.setItem('taskflow_token', state.token);
    await bootstrap();
  } catch (error) {
    $('#authError').textContent = error.message;
    if (error.code === 'email_not_verified') $('#resendVerification').hidden = false;
  } finally {
    submit.disabled = false;
  }
});

$('#resendVerification').addEventListener('click', async event => {
  const email = $('#authForm').elements.email.value.trim();
  if (!email) return;
  event.currentTarget.disabled = true;
  $('#authError').textContent = '';
  try {
    const data = await api('/auth/resend-verification', { method: 'POST', body: JSON.stringify({ email }) });
    $('#authSuccess').textContent = data.message;
  } catch (error) {
    $('#authError').textContent = error.message;
  } finally {
    event.currentTarget.disabled = false;
  }
});

$('#forgotPassword').addEventListener('click', () => showPasswordReset());
$('#passwordResetBack').addEventListener('click', () => showAuth());
$('#passwordResetForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  const token = form.dataset.token;
  submit.disabled = true;
  $('#passwordResetError').textContent = '';
  try {
    const data = token
      ? await api('/auth/reset-password', { method: 'POST', body: JSON.stringify({ token, new_password: form.elements.new_password.value }) })
      : await api('/auth/request-password-reset', { method: 'POST', body: JSON.stringify({ email: form.elements.email.value.trim() }) });
    if (token) {
      history.replaceState({}, '', location.pathname);
      showAuth();
      $('#authSuccess').textContent = 'Пароль изменён. Теперь можно войти с новым паролем.';
    } else $('#passwordResetSuccess').textContent = data.message;
  } catch (error) {
    $('#passwordResetError').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

async function logout() {
  if (state.view === 'notes') await flushNoteSave();
  state.token = null;
  localStorage.removeItem('taskflow_token');
  showLanding();
}
function openAccountDialog() {
  $('#accountName').value = state.user.display_name;
  $('#accountEmail').value = state.user.email;
  $('#accountCurrentPassword').value = '';
  $('#accountNewPassword').value = '';
  $('#accountError').textContent = '';
  $('#accountSuccess').textContent = '';
  $('#accountDialog').showModal();
}
$('#logout').addEventListener('click', openAccountDialog);
$('#mobileLogout').addEventListener('click', logout);
$$('[data-account-close]').forEach(button => button.addEventListener('click', () => $('#accountDialog').close()));
$('#accountForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  const data = { display_name: form.elements.display_name.value.trim() };
  const email = form.elements.email.value.trim();
  if (email && email !== state.user.email) data.email = email;
  if (form.elements.new_password.value) data.new_password = form.elements.new_password.value;
  if (data.email || data.new_password) data.current_password = form.elements.current_password.value;
  submit.disabled = true;
  $('#accountError').textContent = '';
  try {
    const result = await api('/account', { method: 'PATCH', body: JSON.stringify(data) });
    state.user = result.user;
    render();
    form.elements.current_password.value = '';
    form.elements.new_password.value = '';
    $('#accountSuccess').textContent = result.email_change_pending ? `Подтвердите новый email: ${result.email_change_pending}` : 'Настройки сохранены.';
  } catch (error) {
    $('#accountError').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

function filteredTasks() {
  const matchesTaskFilters = task => {
    const filters = state.taskFilters;
    const query = filters.query.trim().toLocaleLowerCase('ru-RU');
    if (query && !`${task.title} ${task.description || ''}`.toLocaleLowerCase('ru-RU').includes(query)) return false;
    if (filters.project === 'none' && task.project_id) return false;
    if (filters.project !== 'all' && filters.project !== 'none' && task.project_id !== filters.project) return false;
    if (filters.status !== 'all' && task.status !== filters.status) return false;
    if (filters.priority !== 'all' && task.priority !== filters.priority) return false;
    if (filters.tag && !(task.tags || []).some(tag => tag.toLocaleLowerCase('ru-RU') === filters.tag.trim().toLocaleLowerCase('ru-RU'))) return false;
    if (filters.date === 'today' && task.scheduled_date !== today()) return false;
    if (filters.date === 'overdue' && !isTaskOverdue(task)) return false;
    if (filters.date === 'unscheduled' && task.scheduled_date) return false;
    if (filters.date === 'range') {
      if (!task.scheduled_date) return false;
      if (filters.dateFrom && task.scheduled_date < filters.dateFrom) return false;
      if (filters.dateTo && task.scheduled_date > filters.dateTo) return false;
    }
    return true;
  };
  if (state.view === 'board') {
    const tasks = state.filter.startsWith('project:') ? state.tasks.filter(task => task.project_id === state.filter.split(':')[1]) : [...state.tasks];
    return tasks.filter(matchesTaskFilters);
  }
  let tasks = state.tasks.filter(task => {
    if (state.filter === 'today') return task.scheduled_date && task.scheduled_date <= today() && task.status !== 'done';
    if (state.filter === 'inbox') return task.status === 'inbox';
    if (state.filter === 'done') return task.status === 'done';
    if (state.filter.startsWith('project:')) return task.project_id === state.filter.split(':')[1];
    return task.status !== 'done';
  });
  tasks = tasks.filter(matchesTaskFilters);
  const weight = { urgent: 0, high: 1, normal: 2, low: 3 };
  return tasks.sort((a, b) => state.sort === 'priority' ? weight[a.priority] - weight[b.priority] : b.created_at.localeCompare(a.created_at));
}

function activeTaskFilterCount() {
  const filters = state.taskFilters;
  return Number(Boolean(filters.query.trim() || filters.tag.trim())) + ['project', 'status', 'priority'].filter(key => filters[key] !== 'all').length + Number(filters.date !== 'all');
}

function savedFilters() {
  try {
    const value = JSON.parse(localStorage.getItem('taskflow_saved_filters') || '[]');
    return Array.isArray(value) ? value.filter(item => item && typeof item.name === 'string' && item.filters && typeof item.filters === 'object') : [];
  } catch (_) {
    localStorage.removeItem('taskflow_saved_filters');
    return [];
  }
}

function renderTaskFilters(resultCount) {
  const filters = state.taskFilters;
  const projectFilter = $('#filterProject');
  projectFilter.innerHTML = '<option value="all">Все проекты</option><option value="none">Без проекта</option>' + state.projects.map(project => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join('');
  projectFilter.value = filters.project;
  $('#filterStatus').value = filters.status;
  $('#filterPriority').value = filters.priority;
  $('#filterTag').value = filters.tag;
  $('#filterDate').value = filters.date;
  $('#filterDateFrom').value = filters.dateFrom;
  $('#filterDateTo').value = filters.dateTo;
  $('#filterDates').hidden = filters.date !== 'range';
  const count = activeTaskFilterCount();
  $('#filterCount').hidden = count === 0;
  $('#filterCount').textContent = count;
  $('#filterToggle').classList.toggle('active', count > 0);
  $('#clearFilters').disabled = count === 0;
  $('#filterResult').textContent = `${resultCount} ${resultCount % 10 === 1 && resultCount % 100 !== 11 ? 'задача' : [2, 3, 4].includes(resultCount % 10) && ![12, 13, 14].includes(resultCount % 100) ? 'задачи' : 'задач'}`;
  const saved = savedFilters();
  if (state.savedFilterIndex >= saved.length) state.savedFilterIndex = -1;
  $('#savedFilter').innerHTML = '<option value="">Выберите фильтр</option>' + saved.map((item, index) => `<option value="${index}">${escapeHtml(item.name)}</option>`).join('');
  $('#savedFilter').value = state.savedFilterIndex >= 0 ? String(state.savedFilterIndex) : '';
  $('#deleteSavedFilter').disabled = state.savedFilterIndex < 0;
}

function render() {
  const tasks = filteredTasks();
  const notesView = state.view === 'notes';
  const calendarView = state.view === 'calendar';
  const project = state.filter.startsWith('project:') ? state.projects.find(item => item.id === state.filter.split(':')[1]) : null;
  const titles = { today: ['ВАШ ДЕНЬ', 'Сегодня', 'План на сегодня'], inbox: ['БЫСТРЫЙ СБОР', 'Входящие', 'Неразобранные задачи'], all: ['ОБЩАЯ КАРТИНА', 'Все задачи', 'Активные задачи'], done: ['АРХИВ', 'Выполненные', 'Завершённые задачи'] };
  const title = notesView ? ['ВАША БАЗА ЗНАНИЙ', 'Заметки', ''] : calendarView ? ['ПЛАН НЕДЕЛИ', 'Неделя', ''] : state.view === 'board'
    ? (project ? ['КАНБАН ПРОЕКТА', project.name, `Доска проекта «${project.name}»`] : ['РАБОЧИЙ ПРОЦЕСС', 'Доска', 'Все задачи по статусам'])
    : (project ? ['ПРОЕКТ', project.name, `Задачи проекта «${project.name}»`] : titles[state.filter]);
  $('#viewEyebrow').textContent = title[0];
  $('#viewTitle').textContent = title[1];
  $('#listTitle').textContent = title[2];
  $('#dateLabel').textContent = new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date());
  $('#userName').textContent = state.user.display_name;
  $('#profileMeta').textContent = `v${state.version || 'dev'} · Настройки`;
  $('#avatar').textContent = state.user.display_name.slice(0, 1).toUpperCase();
  $('#todayCount').textContent = state.tasks.filter(task => task.scheduled_date && task.scheduled_date <= today() && task.status !== 'done').length;
  $('#inboxCount').textContent = state.tasks.filter(task => task.status === 'inbox').length;
  $$('.nav-item, .mobile-nav-item, .mobile-menu-item').forEach(button => button.classList.toggle('active', state.view !== 'list' ? button.dataset.view === state.view : button.dataset.filter === state.filter));
  $$('.nav-item, .mobile-nav-item, .mobile-menu-item').forEach(button => button.setAttribute('aria-current', button.classList.contains('active') ? 'page' : 'false'));
  $$('[data-task-view]').forEach(button => button.classList.toggle('active', button.dataset.taskView === state.view));
  $('.progress-card').hidden = state.view !== 'list';
  $('.task-tools').hidden = notesView || calendarView;
  $('.task-section').hidden = state.view !== 'list';
  $('#boardSection').hidden = state.view !== 'board';
  $('#calendarSection').hidden = !calendarView;
  $('#notesSection').hidden = !notesView;
  $('.view-switcher').hidden = notesView;
  $('#addTask').hidden = notesView;
  $('#mobileProjectList').hidden = notesView;
  renderProjects();
  renderColumnOptions();
  renderTaskFilters(tasks.length);
  if (notesView) renderNotes();
  else if (calendarView) renderCalendar();
  else if (state.view === 'board') renderBoard(tasks);
  else renderTasks(tasks);
  const relevant = state.tasks.filter(task => task.scheduled_date === today());
  const done = relevant.filter(task => task.status === 'done').length;
  const percent = relevant.length ? Math.round(done / relevant.length * 100) : 0;
  $('#progressText').textContent = `${done} из ${relevant.length} задач`;
  $('#progressPercent').textContent = `${percent}%`;
  $('#progressBar').style.width = `${percent}%`;
  $('#dailyProgress').setAttribute('aria-valuenow', String(percent));
  $('#dailyProgress').setAttribute('aria-valuetext', `${done} из ${relevant.length} задач выполнено`);
  const load = relevant.filter(task => task.status !== 'done').reduce((sum, task) => sum + (task.estimated_minutes || 0), 0);
  const capacity = Number(localStorage.getItem('taskflow_daily_capacity') || 480);
  $('#dailyLoad').textContent = load ? `${load} из ${capacity} мин запланировано${load > capacity ? ' · перегрузка' : ''}` : 'Нагрузка не оценена';
  renderFocusTimer();
  scheduleReminders();
}

function renderColumnOptions() {
  const options = state.kanbanColumns.map(column => `<option value="${column.id}">${escapeHtml(column.name)}</option>`).join('');
  $('#taskColumnSelect').innerHTML = options;
}

function renderColumnsDialog() {
  const semanticOptions = column => Object.entries(COLUMN_STATUS_LABELS).map(([value, label]) => `<option value="${value}" ${column.semantic_status === value ? 'selected' : ''}>${label}</option>`).join('');
  $('#columnsList').innerHTML = state.kanbanColumns.map((column, index) => `<article class="column-settings-row" draggable="true" data-column-id="${column.id}">
    <i style="background:${escapeAttribute(column.color)}" aria-hidden="true"></i>
    <input class="column-name" maxlength="80" value="${escapeAttribute(column.name)}" aria-label="Название колонки">
    <select class="column-semantic" aria-label="Системный смысл">${semanticOptions(column)}</select>
    <span class="color-input-control"><input class="column-color-picker" type="color" value="${escapeAttribute(column.color)}" aria-label="Выбрать цвет"><input class="column-color" maxlength="7" value="${escapeAttribute(column.color)}" aria-label="HEX-цвет"></span>
    <div class="column-row-actions"><button class="icon-button column-up" type="button" aria-label="Переместить левее" ${index === 0 ? 'disabled' : ''}><span class="material-symbols-rounded">arrow_back</span></button><button class="icon-button column-down" type="button" aria-label="Переместить правее" ${index === state.kanbanColumns.length - 1 ? 'disabled' : ''}><span class="material-symbols-rounded">arrow_forward</span></button><button class="secondary column-save" type="button">Сохранить</button><button class="danger column-delete" type="button" ${state.kanbanColumns.length === 1 ? 'disabled' : ''}>Удалить</button></div>
  </article>`).join('');
  $$('.column-settings-row', $('#columnsList')).forEach(row => {
     const column = state.kanbanColumns.find(item => item.id === row.dataset.columnId);
     const picker = $('.column-color-picker', row);
     const hex = $('.column-color', row);
     picker.addEventListener('input', () => { hex.value = picker.value; $('i', row).style.background = picker.value; });
     hex.addEventListener('input', () => { if (/^#[0-9a-f]{6}$/i.test(hex.value)) { picker.value = hex.value; $('i', row).style.background = hex.value; } });
    $('.column-save', row).addEventListener('click', async event => {
      const name = $('.column-name', row).value.trim();
       const color = hex.value.trim();
      if (!name || !/^#[0-9a-f]{6}$/i.test(color)) {
        $('#columnsError').textContent = 'Укажите название и HEX-цвет в формате #6d5dfc.';
        return;
      }
      event.currentTarget.disabled = true;
      try {
        await api(`/kanban/columns/${column.id}`, { method: 'PATCH', body: JSON.stringify({ name, color, semantic_status: $('.column-semantic', row).value, expected_version: column.version }) });
        await bootstrap();
        renderColumnsDialog();
        toast('Колонка сохранена');
      } catch (error) { $('#columnsError').textContent = error.message; event.currentTarget.disabled = false; }
    });
    const reorder = async offset => {
      const index = state.kanbanColumns.findIndex(item => item.id === column.id);
      const reordered = [...state.kanbanColumns];
      [reordered[index], reordered[index + offset]] = [reordered[index + offset], reordered[index]];
      try {
        const { columns } = await api('/kanban/columns/reorder', { method: 'POST', body: JSON.stringify({ column_ids: reordered.map(item => item.id) }) });
        state.kanbanColumns = columns;
        render();
        renderColumnsDialog();
      } catch (error) { $('#columnsError').textContent = error.message; }
    };
     $('.column-up', row).addEventListener('click', () => reorder(-1));
     $('.column-down', row).addEventListener('click', () => reorder(1));
     row.addEventListener('dragstart', event => {
       row.classList.add('is-dragging');
       event.dataTransfer.effectAllowed = 'move';
       event.dataTransfer.setData('text/plain', column.id);
     });
     row.addEventListener('dragend', () => row.classList.remove('is-dragging'));
     row.addEventListener('dragover', event => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; row.classList.add('drag-over'); });
     row.addEventListener('dragleave', event => { if (!row.contains(event.relatedTarget)) row.classList.remove('drag-over'); });
     row.addEventListener('drop', async event => {
       event.preventDefault();
       row.classList.remove('drag-over');
       const draggedId = event.dataTransfer.getData('text/plain');
       if (!draggedId || draggedId === column.id) return;
       const from = state.kanbanColumns.findIndex(item => item.id === draggedId);
       const to = state.kanbanColumns.findIndex(item => item.id === column.id);
       if (from < 0 || to < 0) return;
       const reordered = [...state.kanbanColumns];
       const [dragged] = reordered.splice(from, 1);
       reordered.splice(to, 0, dragged);
       try {
         const { columns } = await api('/kanban/columns/reorder', { method: 'POST', body: JSON.stringify({ column_ids: reordered.map(item => item.id) }) });
         state.kanbanColumns = columns;
         render();
         renderColumnsDialog();
       } catch (error) { $('#columnsError').textContent = error.message; }
     });
    $('.column-delete', row).addEventListener('click', async () => {
      const destination = state.kanbanColumns.find(item => item.id !== column.id && item.semantic_status === column.semantic_status) || state.kanbanColumns.find(item => item.id !== column.id);
      if (!destination || !await confirmAction({ title: 'Удалить колонку?', message: `Задачи из «${column.name}» будут перенесены в «${destination.name}».`, confirmLabel: 'Удалить', danger: true })) return;
      try {
        await api(`/kanban/columns/${column.id}`, { method: 'DELETE', body: JSON.stringify({ move_to_column_id: destination.id, expected_version: column.version }) });
        await bootstrap();
        renderColumnsDialog();
        toast('Колонка удалена, задачи перенесены');
      } catch (error) { $('#columnsError').textContent = error.message; }
    });
  });
}

$('#manageColumns').addEventListener('click', () => {
  $('#columnsError').textContent = '';
  renderColumnsDialog();
  $('#columnsDialog').showModal();
});
$$('[data-columns-close]').forEach(button => button.addEventListener('click', () => $('#columnsDialog').close()));
$('#columnAddForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  const values = Object.fromEntries(new FormData(form));
  submit.disabled = true;
  $('#columnsError').textContent = '';
  try {
    const { column } = await api('/kanban/columns', { method: 'POST', body: JSON.stringify(values) });
    state.kanbanColumns.push(column);
    form.reset();
    render();
    renderColumnsDialog();
    toast('Колонка добавлена');
  } catch (error) { $('#columnsError').textContent = error.message; }
  finally { submit.disabled = false; }
});
const addColumnColorPicker = $('.column-color-picker', $('#columnAddForm'));
const addColumnColorHex = $('#columnAddForm [name="color"]');
const syncAddColumnColor = event => { if (/^#[0-9a-f]{6}$/i.test(event.currentTarget.value)) addColumnColorPicker.value = event.currentTarget.value; };
addColumnColorPicker.addEventListener('input', event => { addColumnColorHex.value = event.currentTarget.value; });
addColumnColorHex.addEventListener('input', syncAddColumnColor);
addColumnColorHex.addEventListener('change', syncAddColumnColor);
$('#columnAddForm').addEventListener('input', event => { if (event.target === addColumnColorHex) syncAddColumnColor(event); });

function renderProjects() {
  $('#projectList').innerHTML = state.projects.map(project => `<div class="project-row"><button class="project-item" data-project="${project.id}"><i style="background:${escapeHtml(project.color)}"></i><span>${escapeHtml(project.name)}</span></button><button class="project-edit" data-edit-project="${project.id}" aria-label="Настроить проект"><span class="material-symbols-rounded">more_horiz</span></button></div>`).join('');
  $('#mobileProjectList').innerHTML = state.projects.map(project => `<button class="mobile-project-item ${state.filter === `project:${project.id}` ? 'active' : ''}" data-mobile-project="${project.id}"><i style="background:${escapeHtml(project.color)}"></i>${escapeHtml(project.name)}</button>`).join('') + '<button class="mobile-project-item mobile-archive" type="button" data-open-archive><span class="material-symbols-rounded">inventory_2</span> Архив</button>';
  const projectOptions = '<option value="">Без проекта</option>' + state.projects.map(project => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join('');
  $('#projectSelect').innerHTML = projectOptions;
  $('#moveProjectSelect').innerHTML = projectOptions;
  $$('.project-item').forEach(button => button.addEventListener('click', () => { state.filter = `project:${button.dataset.project}`; render(); }));
  $$('[data-mobile-project]').forEach(button => button.addEventListener('click', () => { state.filter = `project:${button.dataset.mobileProject}`; render(); }));
  $$('.project-edit').forEach(button => button.addEventListener('click', () => openProject(state.projects.find(project => project.id === button.dataset.editProject))));
  $$('[data-open-archive]').forEach(button => button.addEventListener('click', openArchive));
}

function findProject(projectId) {
  return [...state.projects, ...state.archivedProjects].find(project => project.id === projectId);
}

function checklistForTask(taskId) {
  return state.checklistItems.filter(item => item.task_id === taskId).sort((a, b) => a.position - b.position || a.created_at.localeCompare(b.created_at));
}

function inlineChecklistMarkup(task, context) {
  const items = checklistForTask(task.id);
  if (!items.length) return '';
  const done = items.filter(item => item.is_done).length;
  const expanded = state.expandedChecklistTasks.has(task.id);
  const detailsId = `${context}-subtasks-${task.id}`;
  return `<div class="inline-checklist ${expanded ? 'expanded' : ''}" data-inline-task="${task.id}">
    <button class="inline-checklist-toggle" type="button" aria-expanded="${expanded}" aria-controls="${detailsId}">
      ${icon('chevron')}<span>Подзадачи</span><strong>${done}/${items.length}</strong><i class="inline-checklist-progress"><span style="width:${Math.round(done / items.length * 100)}%"></span></i>
    </button>
    <div class="inline-checklist-items" id="${detailsId}" ${expanded ? '' : 'hidden'}>
      ${items.map(item => `<button class="inline-checklist-item ${item.is_done ? 'done' : ''}" type="button" data-inline-checklist="${item.id}" aria-pressed="${item.is_done}"><i>${item.is_done ? icon('check') : ''}</i><span>${escapeHtml(item.title)}</span></button>`).join('')}
      <button class="inline-checklist-manage" type="button">${icon('edit')} Управлять чек-листом</button>
    </div>
  </div>`;
}

function bindInlineChecklist(container, task) {
  const checklist = $('.inline-checklist', container);
  if (!checklist) return;
  const toggle = $('.inline-checklist-toggle', checklist);
  const items = $('.inline-checklist-items', checklist);
  toggle.addEventListener('click', () => {
    const expanded = !state.expandedChecklistTasks.has(task.id);
    if (expanded) state.expandedChecklistTasks.add(task.id);
    else state.expandedChecklistTasks.delete(task.id);
    checklist.classList.toggle('expanded', expanded);
    toggle.setAttribute('aria-expanded', String(expanded));
    items.hidden = !expanded;
  });
  $$('[data-inline-checklist]', checklist).forEach(button => button.addEventListener('click', () => {
    const item = state.checklistItems.find(candidate => candidate.id === button.dataset.inlineChecklist);
    if (item) patchChecklistItem(item, { is_done: !item.is_done });
  }));
  $('.inline-checklist-manage', checklist).addEventListener('click', () => openChecklist(task));
}

function renderTasks(tasks) {
  $('#taskList').innerHTML = tasks.map(task => {
    const project = findProject(task.project_id);
    const priority = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' }[task.priority];
    const status = { inbox: 'Входящие', todo: 'Запланировано', in_progress: 'В работе', done: 'Выполнено' }[task.status];
    const estimate = task.estimated_minutes ? `<span>${icon('clock')} ${task.estimated_minutes} мин</span>` : '';
    const planned = task.scheduled_date ? `<span>План: ${formatPlannedDate(task.scheduled_date)}</span>` : '';
    const recurrence = task.recurrence ? `<span>Повтор: ${{ daily: 'ежедневно', weekly: 'еженедельно', monthly: 'ежемесячно' }[task.recurrence]}</span>` : '';
    const due = task.due_at ? `<span class="${isTaskOverdue(task) ? 'overdue' : 'task-due'}">Срок: ${formatDueAt(task.due_at)}</span>` : '';
    const tags = (task.tags || []).map(tag => `<span>#${escapeHtml(tag)}</span>`).join('');
    return `<article class="task ${task.status === 'done' ? 'done' : ''}" data-id="${task.id}">
      <button class="check" aria-label="${task.status === 'done' ? 'Вернуть задачу' : 'Выполнить задачу'}">${icon('check')}</button>
      <div class="task-main"><div class="task-title">${escapeHtml(task.title)}</div><div class="task-meta">
        <span class="priority-${task.priority}"><i class="dot" style="background:${task.priority === 'urgent' || task.priority === 'high' ? '#df695f' : '#aaa'}"></i>${priority}</span>
        <span>${status}</span>${project ? `<span>${escapeHtml(project.name)}</span>` : ''}${tags}${planned}${recurrence}${due}${estimate}</div></div>
      <div class="task-actions"><button class="icon-button history-task" aria-label="Открыть историю" title="История">${icon('clock')}</button><button class="icon-button discussion-task" aria-label="Открыть обсуждение" title="Обсуждение">${icon('message')}</button><button class="icon-button checklist-task" aria-label="Открыть подзадачи" title="Подзадачи">${icon('checklist')}</button><button class="icon-button move-task" aria-label="Перенести задачу" title="Перенести">${icon('move')}</button><button class="icon-button edit-task" aria-label="Изменить">${icon('edit')}</button><button class="icon-button delete-task" aria-label="Удалить">${icon('trash')}</button></div>
      ${inlineChecklistMarkup(task, 'list')}
    </article>`;
  }).join('');
  const filteredEmpty = activeTaskFilterCount() > 0;
  $('#emptyState h3').textContent = filteredEmpty ? 'Ничего не найдено' : 'Здесь пока тихо';
  $('#emptyState p').textContent = filteredEmpty ? 'Измените условия поиска или сбросьте фильтры.' : 'Добавьте первую задачу — небольшой шаг уже считается.';
  $('#emptyState').hidden = tasks.length > 0;
  $$('.task').forEach(element => {
    const task = state.tasks.find(item => item.id === element.dataset.id);
    bindInlineChecklist(element, task);
    $('.check', element).addEventListener('click', () => patchTask(task, { status: task.status === 'done' ? 'todo' : 'done' }));
    $('.checklist-task', element).addEventListener('click', () => openChecklist(task));
    $('.discussion-task', element).addEventListener('click', () => openDiscussion(task));
    $('.history-task', element).addEventListener('click', () => openHistory(task));
    $('.move-task', element).addEventListener('click', () => openMoveTask(task));
    $('.edit-task', element).addEventListener('click', () => openTask(task));
    $('.delete-task', element).addEventListener('click', () => deleteTask(task));
  });
}

function renderBoard(tasks) {
  const priorityNames = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' };
  let mouseDragTask = null;
  $('#kanbanBoard').innerHTML = state.kanbanColumns.map(column => {
    const columnTasks = tasks.filter(task => task.column_id === column.id).sort((a, b) => a.kanban_position - b.kanban_position || a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
    const cards = columnTasks.map(task => {
      const project = findProject(task.project_id);
      const date = task.scheduled_date ? formatPlannedDate(task.scheduled_date) : '';
      const due = task.due_at ? formatDueAt(task.due_at) : '';
      const overdue = isTaskOverdue(task);
      return `<article class="kanban-card" draggable="true" data-id="${task.id}" data-priority="${task.priority}">
        <div class="kanban-card-head"><span class="kanban-priority priority-${task.priority}"><i class="dot"></i>${priorityNames[task.priority]}</span><div class="board-actions"><button class="icon-button board-menu-toggle" type="button" aria-label="Действия задачи «${escapeAttribute(task.title)}»" aria-haspopup="menu" aria-expanded="false">${icon('more')}</button><div class="board-card-menu" role="menu" aria-label="Действия задачи" hidden><button class="board-edit" type="button" role="menuitem">${icon('edit')}<span>Изменить</span></button><button class="board-discussion" type="button" role="menuitem">${icon('message')}<span>Обсуждение</span></button><button class="board-subtasks" type="button" role="menuitem">${icon('checklist')}<span>Подзадачи</span></button><button class="board-move" type="button" role="menuitem">${icon('move')}<span>Переместить</span></button><button class="board-delete" type="button" role="menuitem">${icon('trash')}<span>Удалить</span></button></div></div></div>
        <h3>${escapeHtml(task.title)}</h3>
        ${task.description ? `<p>${escapeHtml(task.description)}</p>` : ''}
        <div class="kanban-card-meta">${project ? `<span><i class="project-dot" style="background:${escapeHtml(project.color)}"></i>${escapeHtml(project.name)}</span>` : '<span>Без проекта</span>'}${(task.tags || []).map(tag => `<span>#${escapeHtml(tag)}</span>`).join('')}${date ? `<span>План: ${date}</span>` : ''}${task.recurrence ? `<span>Повтор: ${{ daily: 'день', weekly: 'неделя', monthly: 'месяц' }[task.recurrence]}</span>` : ''}${due ? `<span class="${overdue ? 'overdue' : 'task-due'}">Срок: ${due}</span>` : ''}${task.estimated_minutes ? `<span>${icon('clock')} ${task.estimated_minutes} мин</span>` : ''}</div>
        ${inlineChecklistMarkup(task, 'board')}
      </article>`;
    }).join('');
    return `<section class="kanban-column" data-column-id="${column.id}" style="--column-color:${escapeAttribute(column.color)}">
      <header><div><h2>${escapeHtml(column.name)}<span>${columnTasks.length}</span></h2><p>${COLUMN_STATUS_HINTS[column.semantic_status]}</p></div><button class="kanban-add" type="button" data-add-column="${column.id}" aria-label="Добавить задачу в ${escapeAttribute(column.name)}"><span class="material-symbols-rounded">add</span></button></header>
      <div class="kanban-dropzone" data-column-id="${column.id}">${cards || '<div class="kanban-empty">Перетащите задачу сюда</div>'}</div>
    </section>`;
  }).join('');

  $$('.kanban-card').forEach(card => {
    const task = state.tasks.find(item => item.id === card.dataset.id);
    card.addEventListener('dragstart', event => {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', task.id);
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      $$('.kanban-dropzone').forEach(zone => zone.classList.remove('drag-over'));
    });
    card.addEventListener('mousedown', event => {
      if (event.button !== 0 || event.target.closest('button, select, label')) return;
      mouseDragTask = task;
      const finishMouseDrag = upEvent => {
        card.classList.remove('dragging');
        const droppedTask = mouseDragTask;
        mouseDragTask = null;
         const target = document.elementFromPoint(upEvent.clientX, upEvent.clientY);
         const zone = target?.closest('.kanban-dropzone');
         const before = target?.closest('.kanban-card');
         if (droppedTask?.id === task.id && zone) moveTask(task, zone.dataset.columnId, before?.dataset.id === task.id ? null : before?.dataset.id || null);
      };
      document.addEventListener('mouseup', finishMouseDrag, { once: true });
    });
    bindInlineChecklist(card, task);
    const menu = $('.board-card-menu', card);
    const menuToggle = $('.board-menu-toggle', card);
    const closeMenu = () => {
      menu.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false');
      card.classList.remove('menu-open');
    };
    menuToggle.addEventListener('click', event => {
      event.stopPropagation();
      $$('.kanban-card.menu-open').filter(other => other !== card).forEach(other => {
        other.classList.remove('menu-open');
        $('.board-card-menu', other).hidden = true;
        $('.board-menu-toggle', other).setAttribute('aria-expanded', 'false');
      });
      menu.hidden = !menu.hidden;
      menuToggle.setAttribute('aria-expanded', String(!menu.hidden));
      card.classList.toggle('menu-open', !menu.hidden);
      if (!menu.hidden) requestAnimationFrame(() => $('button', menu).focus());
    });
    $('.board-move', card).addEventListener('click', () => { closeMenu(); openMoveTask(task); });
    $('.board-edit', card).addEventListener('click', () => { closeMenu(); openTask(task); });
    $('.board-discussion', card).addEventListener('click', () => { closeMenu(); openDiscussion(task); });
    $('.board-subtasks', card).addEventListener('click', () => { closeMenu(); openChecklist(task); });
    $('.board-delete', card).addEventListener('click', () => { closeMenu(); deleteTask(task); });
    card.addEventListener('focusout', event => { if (!card.contains(event.relatedTarget)) closeMenu(); });
  });
  $$('.kanban-dropzone').forEach(zone => {
    zone.addEventListener('dragover', event => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      zone.classList.add('drag-over');
    });
    zone.addEventListener('dragleave', event => { if (!zone.contains(event.relatedTarget)) zone.classList.remove('drag-over'); });
    zone.addEventListener('drop', event => {
      event.preventDefault();
      zone.classList.remove('drag-over');
      const task = state.tasks.find(item => item.id === event.dataTransfer.getData('text/plain'));
      const before = event.target.closest('.kanban-card');
      mouseDragTask = null;
      if (task) moveTask(task, zone.dataset.columnId, before?.dataset.id === task.id ? null : before?.dataset.id || null);
    });
  });
  $$('[data-add-column]').forEach(button => button.addEventListener('click', () => openTask(null, button.dataset.addColumn)));
}

document.addEventListener('click', event => {
  if (event.target.closest('.board-actions')) return;
  $$('.kanban-card.menu-open').forEach(card => {
    card.classList.remove('menu-open');
    $('.board-card-menu', card).hidden = true;
    $('.board-menu-toggle', card).setAttribute('aria-expanded', 'false');
  });
});

async function moveTask(task, columnId, beforeTaskId = null) {
  const column = state.kanbanColumns.find(item => item.id === columnId);
  try {
    const { task: updated, affected_tasks: affected } = await api(`/tasks/${task.id}/move`, { method: 'POST', body: JSON.stringify({ column_id: columnId, before_task_id: beforeTaskId, expected_version: task.version }) });
    const changed = new Map([updated, ...affected].map(item => [item.id, item]));
    state.tasks = state.tasks.map(item => changed.get(item.id) || item);
    render();
    toast(`Задача перемещена в «${column.name}»`);
  } catch (error) { toast(error.message); }
}

function calendarCard(task) {
  const project = findProject(task.project_id);
  return `<article class="calendar-card priority-${task.priority}" draggable="true" data-id="${task.id}"><strong>${escapeHtml(task.title)}</strong><small>${project ? escapeHtml(project.name) : 'Без проекта'}${task.estimated_minutes ? ` · ${task.estimated_minutes} мин` : ''}</small><button type="button" aria-label="Перенести задачу">${icon('move')}</button></article>`;
}

function bindCalendarDropzones() {
  $$('.calendar-card').forEach(card => {
    const task = state.tasks.find(item => item.id === card.dataset.id);
    card.addEventListener('dragstart', event => { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', task.id); card.classList.add('dragging'); });
    card.addEventListener('dragend', () => { card.classList.remove('dragging'); $$('.calendar-dropzone').forEach(zone => zone.classList.remove('drag-over')); });
    $('button', card).addEventListener('click', () => openMoveTask(task));
  });
  $$('.calendar-dropzone').forEach(zone => {
    zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', event => { if (!zone.contains(event.relatedTarget)) zone.classList.remove('drag-over'); });
    zone.addEventListener('drop', event => {
      event.preventDefault(); zone.classList.remove('drag-over');
      const task = state.tasks.find(item => item.id === event.dataTransfer.getData('text/plain'));
      if (task && (task.scheduled_date || '') !== zone.dataset.date) patchTask(task, { scheduled_date: zone.dataset.date || null }, zone.dataset.date ? `Запланировано на ${formatPlannedDate(zone.dataset.date)}` : 'Дата планирования удалена');
    });
  });
}

function renderCalendar() {
  state.calendarWeekStart ||= startOfWeek(today());
  const dates = Array.from({ length: 7 }, (_, index) => addDays(state.calendarWeekStart, index));
  const formatter = new Intl.DateTimeFormat('ru-RU', { weekday: 'short', day: 'numeric', month: 'short' });
  $('#weekLabel').textContent = `${formatPlannedDate(dates[0])} — ${formatPlannedDate(dates[6])}`;
  const capacity = Number(localStorage.getItem('taskflow_daily_capacity') || 480);
  $('#dailyCapacity').value = capacity;
  $('#weekCalendar').innerHTML = dates.map(date => {
    const tasks = state.tasks.filter(task => task.scheduled_date === date && task.status !== 'done');
    const load = tasks.reduce((sum, task) => sum + (task.estimated_minutes || 0), 0);
    const loadLabel = load ? `${load}/${capacity} мин` : 'Без оценки';
    return `<section class="calendar-day ${date === today() ? 'today' : ''} ${load > capacity ? 'overloaded' : ''}"><header><strong>${formatter.format(localDate(date))}</strong><span>${tasks.length} · ${loadLabel}</span></header><div class="calendar-dropzone" data-date="${date}">${tasks.map(calendarCard).join('') || '<small>Свободный день</small>'}</div></section>`;
  }).join('');
  const unscheduled = state.tasks.filter(task => !task.scheduled_date && task.status !== 'done');
  $('#unscheduledTasks').innerHTML = unscheduled.map(calendarCard).join('') || '<small>Все задачи распределены</small>';
  bindCalendarDropzones();
}

$('#dailyCapacity').addEventListener('change', event => {
  const value = Math.max(30, Math.min(1440, Number(event.currentTarget.value) || 480));
  localStorage.setItem('taskflow_daily_capacity', String(value));
  render();
});

$('#previousWeek').addEventListener('click', () => { state.calendarWeekStart = addDays(state.calendarWeekStart || startOfWeek(today()), -7); render(); });
$('#nextWeek').addEventListener('click', () => { state.calendarWeekStart = addDays(state.calendarWeekStart || startOfWeek(today()), 7); render(); });
$('#currentWeek').addEventListener('click', () => { state.calendarWeekStart = startOfWeek(today()); render(); });

function escapeHtml(value = '') {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
}

function escapeAttribute(value = '') {
  return String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

function renderChecklistDialog() {
  const task = state.tasks.find(item => item.id === state.checklistTaskId);
  if (!task) return;
  const items = checklistForTask(task.id);
  const done = items.filter(item => item.is_done).length;
  const percent = items.length ? Math.round(done / items.length * 100) : 0;
  $('#checklistTitle').textContent = task.title;
  $('#checklistProgressText').textContent = `${done} из ${items.length} выполнено`;
  $('#checklistProgressBar').style.width = `${percent}%`;
  $('#checklistProgress').setAttribute('aria-valuenow', String(percent));
  $('#checklistProgress').setAttribute('aria-valuetext', `${done} из ${items.length} подзадач выполнено`);
  $('#checklistEmpty').hidden = items.length > 0;
  $('#checklistItems').innerHTML = items.map(item => `<div class="checklist-item ${item.is_done ? 'done' : ''}" data-id="${item.id}">
    <button class="checklist-toggle" type="button" aria-label="${item.is_done ? 'Вернуть подзадачу' : 'Выполнить подзадачу'}" aria-pressed="${item.is_done}">${item.is_done ? '<span class="material-symbols-rounded">check</span>' : ''}</button>
    <input class="checklist-title-input" value="${escapeAttribute(item.title)}" maxlength="240" aria-label="Название подзадачи">
    <button class="icon-button checklist-delete" type="button" aria-label="Удалить подзадачу"><span class="material-symbols-rounded">delete</span></button>
  </div>`).join('');
  $$('.checklist-item', $('#checklistItems')).forEach(element => {
    const item = state.checklistItems.find(candidate => candidate.id === element.dataset.id);
    $('.checklist-toggle', element).addEventListener('click', () => patchChecklistItem(item, { is_done: !item.is_done }));
    $('.checklist-title-input', element).addEventListener('change', event => {
      const title = event.currentTarget.value.trim();
      if (!title) {
        event.currentTarget.value = item.title;
        return;
      }
      if (title !== item.title) patchChecklistItem(item, { title });
    });
    $('.checklist-title-input', element).addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        event.currentTarget.blur();
      }
    });
    $('.checklist-delete', element).addEventListener('click', () => deleteChecklistItem(item));
  });
}

function openChecklist(task) {
  state.checklistTaskId = task.id;
  $('#checklistError').textContent = '';
  $('#checklistAddForm').reset();
  renderChecklistDialog();
  $('#checklistDialog').showModal();
  requestAnimationFrame(() => $('#checklistNewTitle').focus());
}

$$('[data-checklist-close]').forEach(button => button.addEventListener('click', () => $('#checklistDialog').close()));

$('#checklistAddForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  const title = form.elements.title.value.trim();
  if (!title || !state.checklistTaskId) return;
  submit.disabled = true;
  $('#checklistError').textContent = '';
  try {
    const { checklist_item: item } = await api(`/tasks/${state.checklistTaskId}/checklist`, { method: 'POST', body: JSON.stringify({ title }) });
    state.checklistItems.push(item);
    form.reset();
    renderChecklistDialog();
    render();
    $('#checklistNewTitle').focus();
  } catch (error) {
    $('#checklistError').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

async function patchChecklistItem(old, changes) {
  $('#checklistError').textContent = '';
  try {
    const { checklist_item: item } = await api(`/checklist/${old.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: old.version }) });
    state.checklistItems = state.checklistItems.map(candidate => candidate.id === old.id ? item : candidate);
    renderChecklistDialog();
    render();
  } catch (error) {
    $('#checklistError').textContent = error.message;
    renderChecklistDialog();
  }
}

async function deleteChecklistItem(item) {
  if (!await confirmAction({ title: 'Удалить подзадачу?', message: `«${item.title}» исчезнет из чек-листа.`, confirmLabel: 'Удалить', danger: true })) return;
  $('#checklistError').textContent = '';
  try {
    await api(`/checklist/${item.id}`, { method: 'DELETE' });
    state.checklistItems = state.checklistItems.filter(candidate => candidate.id !== item.id);
    renderChecklistDialog();
    render();
  } catch (error) {
    $('#checklistError').textContent = error.message;
  }
}

function formatMessageTime(value) {
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}

function renderDiscussion() {
  const messages = state.discussionMessages;
  $('#discussionEmpty').hidden = messages.length > 0;
  $('#discussionMessages').innerHTML = messages.map(message => `<article class="discussion-message ${message.kind === 'system' ? 'system' : ''}" data-id="${message.id}">
    <header><strong>${escapeHtml(message.author_name || state.user?.display_name || 'TaskFlow')}</strong><time datetime="${escapeAttribute(message.created_at)}">${formatMessageTime(message.created_at)}</time>${message.edited_at ? '<span>изменено</span>' : ''}</header>
    <p>${escapeHtml(message.body)}</p>
    ${message.kind === 'comment' ? `<div class="discussion-actions"><button class="link-action discussion-edit" type="button">Изменить</button><button class="link-action danger-text discussion-delete" type="button">Удалить</button></div>` : ''}
  </article>`).join('');
  $$('.discussion-message', $('#discussionMessages')).forEach(element => {
    const message = messages.find(item => item.id === element.dataset.id);
    $('.discussion-edit', element)?.addEventListener('click', () => beginMessageEdit(element, message));
    $('.discussion-delete', element)?.addEventListener('click', () => deleteMessage(message));
  });
  $('#discussionMessages').scrollTop = $('#discussionMessages').scrollHeight;
}

function beginMessageEdit(element, message) {
  element.innerHTML = `<form class="discussion-edit-form"><textarea name="body" maxlength="5000" required>${escapeHtml(message.body)}</textarea><div class="dialog-actions"><button class="secondary" type="button">Отмена</button><button class="primary" type="submit">Сохранить</button></div></form>`;
  const form = $('form', element);
  $('button[type="button"]', form).addEventListener('click', renderDiscussion);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const body = form.elements.body.value.trim();
    if (!body) return;
    try {
      const { message: updated } = await api(`/messages/${message.id}`, { method: 'PATCH', body: JSON.stringify({ body, expected_version: message.version }) });
      state.discussionMessages = state.discussionMessages.map(item => item.id === message.id ? updated : item);
      renderDiscussion();
    } catch (error) {
      $('#discussionError').textContent = error.message;
      renderDiscussion();
    }
  });
  requestAnimationFrame(() => form.elements.body.focus());
}

async function openDiscussion(task) {
  state.discussionTaskId = task.id;
  state.discussionMessages = [];
  $('#discussionTitle').textContent = task.title;
  $('#discussionError').textContent = '';
  $('#discussionForm').reset();
  $('#discussionDialog').showModal();
  try {
    const { messages } = await api(`/messages?task_id=${encodeURIComponent(task.id)}`);
    if (state.discussionTaskId !== task.id) return;
    state.discussionMessages = messages;
    renderDiscussion();
    requestAnimationFrame(() => $('#discussionBody').focus());
  } catch (error) {
    $('#discussionError').textContent = error.message;
  }
}

function historyValue(field, value) {
  if (value === null || value === '' || (Array.isArray(value) && !value.length)) return 'не указано';
  if (field === 'status') return COLUMN_STATUS_LABELS[value] || value;
  if (field === 'priority') return ({ urgent: 'Срочный', high: 'Высокий', normal: 'Обычный', low: 'Низкий' })[value] || value;
  if (field === 'recurrence') return ({ daily: 'Каждый день', weekly: 'Каждую неделю', monthly: 'Каждый месяц' })[value] || value;
  if (field === 'scheduled_date') return formatPlannedDate(value);
  if (field === 'due_at') return formatDueAt(value);
  if (field === 'project_id') return state.projects.find(project => project.id === value)?.name || 'Без проекта';
  if (field === 'column_id') return state.kanbanColumns.find(column => column.id === value)?.name || 'Другая колонка';
  if (field === 'tags') return value.join(', ');
  if (field === 'reminder_offsets') return value.length ? `${value.length} шт.` : 'выключены';
  if (field === 'description') return value ? 'обновлено' : 'очищено';
  if (field === 'estimated_minutes') return `${value} мин`;
  return String(value);
}

function renderHistoryChanges(entry) {
  const labels = { title: 'Название', description: 'Описание', scheduled_date: 'Дата плана', due_at: 'Срок', priority: 'Приоритет', recurrence: 'Повторение', tags: 'Теги', reminder_offsets: 'Напоминания', column_id: 'Колонка', project_id: 'Проект', estimated_minutes: 'Оценка', status: 'Статус' };
  const rows = Object.entries(entry.changes || {}).filter(([field]) => labels[field]);
  if (!rows.length) return '';
  return `<ul>${rows.map(([field, value]) => `<li><span>${labels[field]}</span><strong>${escapeHtml(historyValue(field, value))}</strong></li>`).join('')}</ul>`;
}

async function openHistory(task) {
  $('#historyTitle').textContent = task.title;
  $('#historyEntries').innerHTML = '<p class="history-status">Загружаем историю…</p>';
  $('#historyDialog').showModal();
  try {
    const { history } = await api(`/tasks/${task.id}/history`);
    const labels = { created: 'Задача создана', updated: 'Задача изменена', moved: 'Задача перемещена', deleted: 'Задача удалена' };
    $('#historyEntries').innerHTML = history.length ? history.map(entry => `<article class="history-entry"><i aria-hidden="true"></i><div><header><strong>${labels[entry.event_type] || escapeHtml(entry.event_type)}</strong><time datetime="${escapeAttribute(entry.created_at)}">${formatDueAt(entry.created_at)}</time></header>${renderHistoryChanges(entry)}</div></article>`).join('') : '<p class="history-status">Изменений пока нет.</p>';
  } catch (error) {
    $('#historyEntries').innerHTML = `<p class="history-status error">${escapeHtml(error.message)}</p>`;
  }
}
$$('[data-history-close]').forEach(button => button.addEventListener('click', () => $('#historyDialog').close()));

$$('[data-discussion-close]').forEach(button => button.addEventListener('click', () => {
  state.discussionTaskId = null;
  $('#discussionDialog').close();
}));

$('#discussionForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  const body = form.elements.body.value.trim();
  if (!body || !state.discussionTaskId) return;
  submit.disabled = true;
  $('#discussionError').textContent = '';
  try {
    const { message } = await api(`/tasks/${state.discussionTaskId}/messages`, { method: 'POST', body: JSON.stringify({ body }) });
    state.discussionMessages.push(message);
    form.reset();
    renderDiscussion();
    $('#discussionBody').focus();
  } catch (error) {
    $('#discussionError').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

async function deleteMessage(message) {
  if (!await confirmAction({ title: 'Удалить сообщение?', message: 'Сообщение исчезнет из обсуждения задачи.', confirmLabel: 'Удалить', danger: true })) return;
  try {
    await api(`/messages/${message.id}`, { method: 'DELETE' });
    state.discussionMessages = state.discussionMessages.filter(item => item.id !== message.id);
    renderDiscussion();
  } catch (error) {
    $('#discussionError').textContent = error.message;
  }
}

function markdownInline(value) {
  const inlineCode = [];
  value = value.replace(/`([^`]+)`/g, (_, code) => {
    inlineCode.push(`<code>${escapeHtml(code)}</code>`);
    return `@@CODE${inlineCode.length - 1}@@`;
  });
  const wikiLinks = [];
  value = value.replace(/\[\[([^\]\n]{1,240})\]\]/g, (_, rawTitle) => {
    const title = rawTitle.trim();
    if (!title) return _;
    const matches = state.notes.filter(note => note.title.localeCompare(title, undefined, { sensitivity: 'accent' }) === 0);
    const token = `@@WIKI${wikiLinks.length}@@`;
    if (matches.length === 1) wikiLinks.push(`<button class="wiki-link" type="button" data-note-id="${matches[0].id}">${escapeHtml(title)}</button>`);
    else wikiLinks.push(`<span class="wiki-link ${matches.length ? 'ambiguous' : 'missing'}" title="${matches.length ? 'Найдено несколько заметок' : 'Заметка не найдена'}">${escapeHtml(title)}</span>`);
    return token;
  });
  let output = escapeHtml(value);
  output = output.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  output = output.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  output = output.replace(/(^|\s)\*([^*]+)\*/g, '$1<em>$2</em>');
  output = output.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  output = output.replace(/@@WIKI(\d+)@@/g, (_, index) => wikiLinks[Number(index)]);
  output = output.replace(/@@CODE(\d+)@@/g, (_, index) => inlineCode[Number(index)]);
  return output;
}

function renderMarkdown(markdown = '') {
  const output = [];
  let listOpen = false;
  let codeOpen = false;
  const closeList = () => { if (listOpen) { output.push('</ul>'); listOpen = false; } };
  for (const line of markdown.split(/\r?\n/)) {
    if (line.trim().startsWith('```')) {
      closeList();
      output.push(codeOpen ? '</code></pre>' : '<pre><code>');
      codeOpen = !codeOpen;
      continue;
    }
    if (codeOpen) { output.push(`${escapeHtml(line)}\n`); continue; }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const list = line.match(/^\s*[-*]\s+(.+)$/);
    if (heading) { closeList(); const level = heading[1].length; output.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`); }
    else if (list) { if (!listOpen) { output.push('<ul>'); listOpen = true; } output.push(`<li>${markdownInline(list[1])}</li>`); }
    else if (line.startsWith('> ')) { closeList(); output.push(`<blockquote>${markdownInline(line.slice(2))}</blockquote>`); }
    else if (!line.trim()) { closeList(); }
    else { closeList(); output.push(`<p>${markdownInline(line)}</p>`); }
  }
  closeList();
  if (codeOpen) output.push('</code></pre>');
  return output.join('');
}

function filteredNotes() {
  const source = state.noteSearchResults || state.notes;
  if (state.noteFilter === 'favorite') return source.filter(note => note.is_favorite);
  if (state.noteFilter === 'none') return source.filter(note => !note.folder_id);
  if (state.noteFilter.startsWith('folder:')) return source.filter(note => note.folder_id === state.noteFilter.slice(7));
  return [...source];
}

function noteFilterTitle() {
  if (state.noteFilter === 'favorite') return 'Избранное';
  if (state.noteFilter === 'none') return 'Без папки';
  if (state.noteFilter.startsWith('folder:')) return state.noteFolders.find(folder => folder.id === state.noteFilter.slice(7))?.name || 'Папка';
  return 'Все заметки';
}

function renderNoteEditor(note) {
  $('#noteEditor').hidden = !note;
  $('#notesSection').classList.toggle('note-open', Boolean(note));
  if (!note) return;
  $('#noteTitle').value = note.title;
  $('#noteContent').value = note.content;
  $('#noteFolderSelect').innerHTML = '<option value="">Без папки</option>' + state.noteFolders.map(folder => `<option value="${folder.id}">${escapeHtml(folder.name)}</option>`).join('');
  $('#noteFolderSelect').value = note.folder_id || '';
  $('#notePreview').innerHTML = renderMarkdown(note.content);
  $('#noteEditor').classList.toggle('is-editing', state.noteMode === 'edit');
  $('#viewNote').classList.toggle('active', state.noteMode === 'view');
  $('#editNote').classList.toggle('active', state.noteMode === 'edit');
  $('#noteTitle').readOnly = state.noteMode !== 'edit';
  $('#noteFolderSelect').disabled = state.noteMode !== 'edit';
  $('#noteRelationTarget').disabled = state.noteMode !== 'edit';
  $('#noteRelationForm button').disabled = state.noteMode !== 'edit';
  renderNoteRelations(note);
  $('#favoriteNote').classList.toggle('active', note.is_favorite);
  $('#favoriteNote').innerHTML = `<span class="material-symbols-rounded" aria-hidden="true">${note.is_favorite ? 'star' : 'star_border'}</span>`;
  $('#favoriteNote').setAttribute('aria-label', note.is_favorite ? 'Убрать из избранного' : 'Добавить в избранное');
  $('#noteSaveStatus').textContent = 'Сохранено';
  $('#noteError').textContent = '';
}

function relationTarget(link) {
  if (link.task_id) return { type: 'Задача', item: state.tasks.find(item => item.id === link.task_id) };
  return { type: 'Проект', item: [...state.projects, ...state.archivedProjects].find(item => item.id === link.project_id) };
}

function renderNoteRelations(note) {
  const links = state.noteLinks.filter(link => link.note_id === note.id);
  $('#noteRelations').innerHTML = links.map(link => {
    const target = relationTarget(link);
    return `<span class="note-relation-chip"><small>${target.type}</small>${escapeHtml(target.item?.title || target.item?.name || 'Недоступно')}<button type="button" data-link-id="${link.id}" aria-label="Удалить связь"><span class="material-symbols-rounded">close</span></button></span>`;
  }).join('') || '<small class="note-relations-empty">Связей пока нет</small>';
  const linkedTasks = new Set(links.map(link => link.task_id));
  const linkedProjects = new Set(links.map(link => link.project_id));
  const taskOptions = state.tasks.filter(task => !linkedTasks.has(task.id)).map(task => `<option value="task:${task.id}">${escapeHtml(task.title)}</option>`).join('');
  const projectOptions = [...state.projects, ...state.archivedProjects].filter(project => !linkedProjects.has(project.id)).map(project => `<option value="project:${project.id}">${escapeHtml(project.name)}${project.archived_at ? ' (архив)' : ''}</option>`).join('');
  $('#noteRelationTarget').innerHTML = `<option value="">Выберите задачу или проект</option><optgroup label="Задачи">${taskOptions}</optgroup><optgroup label="Проекты">${projectOptions}</optgroup>`;
  $$('.note-relation-chip button').forEach(button => button.addEventListener('click', () => deleteNoteRelation(button.dataset.linkId)));
}

function renderNotes() {
  const notes = filteredNotes().sort((a, b) => Number(b.is_favorite) - Number(a.is_favorite) || b.updated_at.localeCompare(a.updated_at));
  $('#allNotesCount').textContent = state.notes.length;
  $('#notesListTitle').textContent = noteFilterTitle();
  $$('.notes-filter').forEach(button => button.classList.toggle('active', button.dataset.noteFilter === state.noteFilter));
  $('#noteFolderList').innerHTML = state.noteFolders.map(folder => `<div class="note-folder-row ${state.noteFilter === `folder:${folder.id}` ? 'active' : ''}" data-folder-id="${folder.id}"><span class="material-symbols-rounded">folder</span><input value="${escapeAttribute(folder.name)}" maxlength="100" aria-label="Название папки ${escapeAttribute(folder.name)}"><button class="folder-delete" type="button" aria-label="Удалить папку ${escapeAttribute(folder.name)}"><span class="material-symbols-rounded">delete</span></button></div>`).join('');
  $$('.note-folder-row').forEach(row => {
    const folder = state.noteFolders.find(item => item.id === row.dataset.folderId);
    const input = $('input', row);
    input.addEventListener('focus', () => {
      if (state.noteFilter === `folder:${folder.id}`) return;
      state.noteFilter = `folder:${folder.id}`;
      renderNotes();
      requestAnimationFrame(() => { const current = $(`.note-folder-row[data-folder-id="${folder.id}"] input`); current.focus(); current.select(); });
    });
    input.addEventListener('change', () => renameNoteFolder(folder, input.value));
    input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur(); } });
    $('.folder-delete', row).addEventListener('click', () => deleteNoteFolder(folder));
  });
  $('#notesList').innerHTML = notes.map(note => `<button class="note-list-item ${note.id === state.activeNoteId ? 'active' : ''}" type="button" data-note-id="${note.id}"><header><strong>${escapeHtml(note.title)}</strong>${note.is_favorite ? '<span class="material-symbols-rounded">star</span>' : ''}</header><p>${escapeHtml(note.content.replace(/[#>*_`\[\]-]/g, ' ').trim() || 'Пустая заметка')}</p><small>${formatMessageTime(note.updated_at)}</small></button>`).join('');
  $('#notesEmpty').hidden = notes.length > 0;
  $('#notesEmpty small').textContent = state.noteSearchResults && !notes.length ? 'По вашему запросу ничего не найдено.' : 'Создайте первую и сохраните важный контекст.';
   $$('.note-list-item').forEach(button => button.addEventListener('click', async () => {
     await flushNoteSave();
     state.activeNoteId = button.dataset.noteId;
     state.noteMode = 'view';
    renderNotes();
    renderNoteEditor(state.notes.find(note => note.id === state.activeNoteId));
    requestAnimationFrame(() => $('#noteTitle').focus());
  }));
  if (state.activeNoteId && !state.notes.some(note => note.id === state.activeNoteId)) {
    state.activeNoteId = null;
    renderNoteEditor(null);
  }
}

let noteSearchTimer;
$('#noteSearch').addEventListener('input', event => {
  clearTimeout(noteSearchTimer);
  const query = event.currentTarget.value.trim();
  noteSearchTimer = setTimeout(async () => {
    try {
      state.noteSearchResults = query ? (await api(`/notes?q=${encodeURIComponent(query)}`)).notes : null;
      renderNotes();
    } catch (error) { $('#noteFolderError').textContent = error.message; }
  }, 300);
});

$('#notePreview').addEventListener('click', async event => {
  const link = event.target.closest('.wiki-link[data-note-id]');
  if (!link) return;
  await flushNoteSave();
  state.activeNoteId = link.dataset.noteId;
  renderNotes();
  renderNoteEditor(state.notes.find(note => note.id === state.activeNoteId));
});

$('#noteRelationForm').addEventListener('submit', async event => {
  event.preventDefault();
  await flushNoteSave();
  const [type, id] = $('#noteRelationTarget').value.split(':');
  if (!id || !state.activeNoteId) return;
  try {
    const body = { note_id: state.activeNoteId, [`${type}_id`]: id };
    const { note_link: link } = await api('/note-links', { method: 'POST', body: JSON.stringify(body) });
    state.noteLinks.push(link);
    renderNoteRelations(state.notes.find(note => note.id === state.activeNoteId));
  } catch (error) { $('#noteRelationError').textContent = error.message; }
});

async function deleteNoteRelation(linkId) {
  try {
    await api(`/note-links/${linkId}`, { method: 'DELETE' });
    state.noteLinks = state.noteLinks.filter(link => link.id !== linkId);
    renderNoteRelations(state.notes.find(note => note.id === state.activeNoteId));
  } catch (error) { $('#noteRelationError').textContent = error.message; }
}

async function renameNoteFolder(folder, rawName) {
  const name = rawName.trim();
  if (!name) return renderNotes();
  try {
    const { folder: updated } = await api(`/note-folders/${folder.id}`, { method: 'PATCH', body: JSON.stringify({ name, expected_version: folder.version }) });
    state.noteFolders = state.noteFolders.map(item => item.id === folder.id ? updated : item);
    renderNotes();
  } catch (error) { $('#noteFolderError').textContent = error.message; renderNotes(); }
}

async function deleteNoteFolder(folder) {
  await flushNoteSave();
  if (!await confirmAction({ title: 'Удалить папку?', message: `Заметки из «${folder.name}» останутся и перейдут в раздел «Без папки».`, confirmLabel: 'Удалить', danger: true })) return;
  try {
    await api(`/note-folders/${folder.id}`, { method: 'DELETE' });
    state.noteFolders = state.noteFolders.filter(item => item.id !== folder.id);
    state.notes = state.notes.map(note => note.folder_id === folder.id ? { ...note, folder_id: null, version: note.version + 1 } : note);
    if (state.noteFilter === `folder:${folder.id}`) state.noteFilter = 'all';
    renderNotes();
    const active = state.notes.find(note => note.id === state.activeNoteId);
    if (active) renderNoteEditor(active);
  } catch (error) { $('#noteFolderError').textContent = error.message; }
}

$('#noteFolderForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = event.submitter;
  const name = form.elements.name.value.trim();
  if (!name) return;
  submit.disabled = true;
  try {
    const { folder } = await api('/note-folders', { method: 'POST', body: JSON.stringify({ name }) });
    state.noteFolders.push(folder);
    state.noteFilter = `folder:${folder.id}`;
    form.reset();
    renderNotes();
  } catch (error) { $('#noteFolderError').textContent = error.message; }
  finally { submit.disabled = false; }
});

$$('.notes-filter').forEach(button => button.addEventListener('click', async () => {
  await flushNoteSave();
  state.noteFilter = button.dataset.noteFilter;
  state.activeNoteId = null;
  renderNotes();
  renderNoteEditor(null);
}));

$('#newNote').addEventListener('click', async () => {
  await flushNoteSave();
  const folderId = state.noteFilter.startsWith('folder:') ? state.noteFilter.slice(7) : null;
  try {
    const { note } = await api('/notes', { method: 'POST', body: JSON.stringify({ title: 'Новая заметка', content: '', folder_id: folderId }) });
    state.notes.unshift(note);
    state.activeNoteId = note.id;
    state.noteMode = 'edit';
    renderNotes();
    renderNoteEditor(note);
    requestAnimationFrame(() => { $('#noteTitle').focus(); $('#noteTitle').select(); });
  } catch (error) { $('#noteFolderError').textContent = error.message; }
});

$('#viewNote').addEventListener('click', async () => {
  await flushNoteSave();
  state.noteMode = 'view';
  renderNoteEditor(state.notes.find(note => note.id === state.activeNoteId));
});
$('#editNote').addEventListener('click', () => {
  state.noteMode = 'edit';
  renderNoteEditor(state.notes.find(note => note.id === state.activeNoteId));
  requestAnimationFrame(() => $('#noteContent').focus());
});

function scheduleNoteSave() {
  $('#notePreview').innerHTML = renderMarkdown($('#noteContent').value);
  $('#noteSaveStatus').textContent = 'Есть несохранённые изменения';
  clearTimeout(state.noteSaveTimer);
  state.noteSaveTimer = setTimeout(flushNoteSave, 700);
}

async function flushNoteSave() {
  clearTimeout(state.noteSaveTimer);
  state.noteSaveTimer = null;
  if (!state.activeNoteId) return;
  if (state.noteSaveInFlight) {
    state.noteSavePending = true;
    return state.noteSavePromise;
  }
  const note = state.notes.find(item => item.id === state.activeNoteId);
  if (!note) return;
  const title = $('#noteTitle').value.trim() || 'Без названия';
  if (!$('#noteTitle').value.trim()) $('#noteTitle').value = title;
  const changes = { title, content: $('#noteContent').value, folder_id: $('#noteFolderSelect').value || null };
  if (changes.title === note.title && changes.content === note.content && changes.folder_id === note.folder_id) { $('#noteSaveStatus').textContent = 'Сохранено'; return; }
  state.noteSaveInFlight = true;
  $('#noteSaveStatus').textContent = 'Сохраняем…';
  state.noteSavePromise = api(`/notes/${note.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: note.version }) })
    .then(({ note: updated }) => {
      state.notes = state.notes.map(item => item.id === note.id ? updated : item);
      $('#noteSaveStatus').textContent = 'Сохранено';
      renderNotes();
    })
    .catch(error => { $('#noteSaveStatus').textContent = 'Не сохранено'; $('#noteError').textContent = error.message; })
    .finally(() => {
      state.noteSaveInFlight = false;
      state.noteSavePromise = null;
      if (state.noteSavePending) { state.noteSavePending = false; scheduleNoteSave(); }
    });
  return state.noteSavePromise;
}

$('#noteTitle').addEventListener('input', scheduleNoteSave);
$('#noteContent').addEventListener('input', scheduleNoteSave);
$('#noteFolderSelect').addEventListener('change', scheduleNoteSave);

$('.markdown-toolbar').addEventListener('click', event => {
  const button = event.target.closest('[data-markdown-action]');
  if (!button) return;
  const textarea = $('#noteContent');
  const selected = textarea.value.slice(textarea.selectionStart, textarea.selectionEnd) || 'текст';
  const action = button.dataset.markdownAction;
  const formats = {
    heading: `# ${selected}`,
    bold: `**${selected}**`,
    italic: `*${selected}*`,
    'bulleted-list': `- ${selected}`,
    'numbered-list': `1. ${selected}`,
    link: `[${selected}](https://)`,
    code: `\`${selected}\``,
  };
  textarea.setRangeText(formats[action], textarea.selectionStart, textarea.selectionEnd, 'select');
  textarea.focus();
  scheduleNoteSave();
});

$('#favoriteNote').addEventListener('click', async () => {
  await flushNoteSave();
  const note = state.notes.find(item => item.id === state.activeNoteId);
  if (!note) return;
  try {
    const { note: updated } = await api(`/notes/${note.id}`, { method: 'PATCH', body: JSON.stringify({ is_favorite: !note.is_favorite, expected_version: note.version }) });
    state.notes = state.notes.map(item => item.id === note.id ? updated : item);
    renderNotes();
    renderNoteEditor(updated);
  } catch (error) { $('#noteError').textContent = error.message; }
});

$('#deleteNote').addEventListener('click', async () => {
  await flushNoteSave();
  const note = state.notes.find(item => item.id === state.activeNoteId);
  if (!note || !await confirmAction({ title: 'Удалить заметку?', message: `«${note.title}» будет удалена.`, confirmLabel: 'Удалить', danger: true })) return;
  try {
    await api(`/notes/${note.id}`, { method: 'DELETE' });
    state.notes = state.notes.filter(item => item.id !== note.id);
    state.noteLinks = state.noteLinks.filter(link => link.note_id !== note.id);
    state.activeNoteId = null;
    renderNotes();
    renderNoteEditor(null);
  } catch (error) { $('#noteError').textContent = error.message; }
});

$('#closeNoteEditor').addEventListener('click', async () => {
  await flushNoteSave();
  state.activeNoteId = null;
  renderNotes();
  renderNoteEditor(null);
});

$$('.nav-item, .mobile-nav-item, .mobile-menu-item').forEach(button => button.addEventListener('click', async () => {
  if (!button.dataset.view && !button.dataset.filter) return;
  if (state.view === 'notes') await flushNoteSave();
  state.view = button.dataset.view || 'list';
  if (button.dataset.view === 'board') state.filter = 'all';
  else if (button.dataset.filter) state.filter = button.dataset.filter;
  if ($('#mobileMoreDialog').open) $('#mobileMoreDialog').close();
  render();
}));
$('#mobileMore').addEventListener('click', () => $('#mobileMoreDialog').showModal());
$$('[data-mobile-more-close]').forEach(button => button.addEventListener('click', () => $('#mobileMoreDialog').close()));
$$('[data-task-view]').forEach(button => button.addEventListener('click', () => {
  state.view = button.dataset.taskView;
  if (state.view === 'board' && !state.filter.startsWith('project:')) state.filter = 'all';
  render();
}));
$('#filterToggle').addEventListener('click', event => {
  const panel = $('#filterPanel');
  panel.hidden = !panel.hidden;
  event.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
});
$('#taskSearch').addEventListener('input', event => {
  state.savedFilterIndex = -1;
  state.taskFilters.query = event.currentTarget.value;
  render();
});
$$('[data-task-filter]').forEach(control => control.addEventListener('change', event => {
  state.savedFilterIndex = -1;
  state.taskFilters[event.currentTarget.dataset.taskFilter] = event.currentTarget.value;
  render();
}));
$('#clearFilters').addEventListener('click', () => {
  state.savedFilterIndex = -1;
  Object.assign(state.taskFilters, { query: '', project: 'all', status: 'all', priority: 'all', tag: '', date: 'all', dateFrom: '', dateTo: '' });
  $('#taskSearch').value = '';
  render();
  $('#taskSearch').focus();
});
$('#saveFilter').addEventListener('click', () => {
  const input = $('#savedFilterName');
  const name = input.value.trim();
  if (!name) { input.focus(); return; }
  const saved = savedFilters().filter(item => item.name !== name);
  saved.push({ name, filters: { ...state.taskFilters } });
  localStorage.setItem('taskflow_saved_filters', JSON.stringify(saved));
  state.savedFilterIndex = saved.length - 1;
  input.value = '';
  render();
  toast(`Фильтр «${name}» сохранён`);
});
$('#savedFilter').addEventListener('change', event => {
  state.savedFilterIndex = event.currentTarget.value === '' ? -1 : Number(event.currentTarget.value);
  const item = savedFilters()[state.savedFilterIndex];
  if (!item) { render(); return; }
  Object.assign(state.taskFilters, item.filters);
  $('#taskSearch').value = state.taskFilters.query;
  render();
});
$('#deleteSavedFilter').addEventListener('click', async () => {
  const saved = savedFilters();
  const item = saved[state.savedFilterIndex];
  if (!item || !await confirmAction({ title: 'Удалить сохранённый фильтр?', message: `«${item.name}» исчезнет из списка. Задачи не изменятся.`, confirmLabel: 'Удалить', danger: true })) return;
  saved.splice(state.savedFilterIndex, 1);
  localStorage.setItem('taskflow_saved_filters', JSON.stringify(saved));
  state.savedFilterIndex = -1;
  render();
  toast('Сохранённый фильтр удалён');
});
$$('.segmented button').forEach(button => button.addEventListener('click', () => {
  state.sort = button.dataset.sort;
  $$('.segmented button').forEach(item => item.classList.toggle('active', item === button));
  render();
}));

function syncReminderControls(clearWhenDisabled = false) {
  const hasDueAt = Boolean($('#taskForm').elements.due_at.value);
  $$('input[name="reminder_offsets"]', $('#taskForm')).forEach(input => {
    input.disabled = !hasDueAt;
    if (!hasDueAt && clearWhenDisabled) input.checked = false;
  });
  const hint = $('#reminderHint');
  if (!hasDueAt) hint.textContent = 'Сначала укажите срок выполнения.';
  else if (!('Notification' in window)) hint.textContent = 'Этот браузер не поддерживает системные уведомления.';
  else if (Notification.permission === 'denied') hint.textContent = 'Уведомления запрещены в настройках браузера.';
  else if (Notification.permission === 'granted') hint.textContent = 'Напоминания появятся, пока TaskFlow открыт.';
  else hint.textContent = 'После сохранения браузер может запросить разрешение.';
}

function openTask(task = null, initialColumnId = null) {
  const form = $('#taskForm');
  form.reset();
  $('#taskError').textContent = '';
  $('#dialogTitle').textContent = task ? 'Изменить задачу' : 'Новая задача';
  const plannedToday = state.filter === 'today';
  const defaultStatus = plannedToday ? 'todo' : 'inbox';
  const defaultColumn = state.kanbanColumns.find(column => column.semantic_status === defaultStatus) || state.kanbanColumns[0];
  const values = task ? { ...task, due_at: toDateTimeLocal(task.due_at) } : { scheduled_date: plannedToday ? today() : '', due_at: '', project_id: state.filter.startsWith('project:') ? state.filter.split(':')[1] : '', priority: 'normal', recurrence: '', column_id: initialColumnId || defaultColumn?.id || '' };
  ['id', 'title', 'description', 'scheduled_date', 'due_at', 'priority', 'recurrence', 'column_id', 'project_id', 'estimated_minutes'].forEach(key => { if (form.elements[key]) form.elements[key].value = values[key] ?? ''; });
  form.elements.tags.value = (values.tags || []).join(', ');
  $$('input[name="reminder_offsets"]', form).forEach(input => { input.checked = (values.reminder_offsets || []).includes(Number(input.value)); });
  $('#taskAdvanced').open = Boolean(task && (values.recurrence || values.estimated_minutes || values.tags?.length || values.reminder_offsets?.length));
  syncReminderControls();
  $('#taskDialog').showModal();
  requestAnimationFrame(() => form.elements.title.focus());
}
$('#addTask').addEventListener('click', () => openTask());
$$('[data-close]').forEach(button => button.addEventListener('click', () => $('#taskDialog').close()));
$('#taskForm').elements.due_at.addEventListener('input', () => syncReminderControls(true));

document.addEventListener('keydown', event => {
  const target = event.target;
  const typing = target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
  const taskDialog = $('#taskDialog');
  const openBoardCard = $('.kanban-card.menu-open');
  if (event.key === 'Escape' && openBoardCard) {
    const toggle = $('.board-menu-toggle', openBoardCard);
    $('.board-card-menu', openBoardCard).hidden = true;
    openBoardCard.classList.remove('menu-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.focus();
    return;
  }
  if (event.key === 'Escape' && !$('#filterPanel').hidden && !document.querySelector('dialog[open]')) {
    $('#filterPanel').hidden = true;
    $('#filterToggle').setAttribute('aria-expanded', 'false');
    $('#filterToggle').focus();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && taskDialog.open) {
    event.preventDefault();
    const form = $('#taskForm');
    form.requestSubmit(form.querySelector('button[type="submit"]'));
    return;
  }
  if (event.key === '/' && state.view !== 'notes' && !event.ctrlKey && !event.metaKey && !event.altKey && !typing && state.token && !$('#appView').hidden && !document.querySelector('dialog[open]')) {
    event.preventDefault();
    $('#taskSearch').focus();
    return;
  }
  if (event.key.toLowerCase() !== 'n' || event.ctrlKey || event.metaKey || event.altKey || event.repeat || typing) return;
  if (!state.token || $('#appView').hidden || document.querySelector('dialog[open]') || state.view === 'notes') return;
  event.preventDefault();
  openTask();
});

$('#taskForm').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  const formData = new FormData(event.currentTarget);
  const raw = Object.fromEntries(formData);
  raw.reminder_offsets = formData.getAll('reminder_offsets').map(Number);
  raw.tags = (raw.tags || '').split(',').map(tag => tag.trim()).filter(Boolean);
  const id = raw.id;
  delete raw.id;
  Object.keys(raw).forEach(key => { if (raw[key] === '') raw[key] = null; });
  if (raw.estimated_minutes) raw.estimated_minutes = Number(raw.estimated_minutes);
  if (raw.due_at) raw.due_at = new Date(raw.due_at).toISOString();
  try {
    const requestReminderPermission = raw.reminder_offsets.length && raw.due_at && 'Notification' in window && Notification.permission === 'default';
    if (id) {
      const old = state.tasks.find(task => task.id === id);
      const { task } = await api(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify({ ...raw, expected_version: old.version }) });
      state.tasks = state.tasks.map(item => item.id === id ? task : item);
    } else {
      const { task } = await api('/tasks', { method: 'POST', body: JSON.stringify(raw) });
      state.tasks.push(task);
    }
    $('#taskDialog').close();
    let reminderMessage = '';
    if (requestReminderPermission) {
      try {
        const permission = await Notification.requestPermission();
        if (permission === 'granted') scheduleReminders();
        else reminderMessage = ' Уведомления не включены — срок сохранён без напоминания.';
      } catch (_) { reminderMessage = ' Уведомления недоступны в этом браузере.'; }
    }
    render();
    toast(`Задача сохранена.${reminderMessage}`);
  } catch (error) {
    $('#taskError').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

function dateWithOffset(days) {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return date.toLocaleDateString('sv-SE');
}

function updateQuickDateSelection() {
  const value = $('#moveScheduledDate').value;
  const quickValue = value === today() ? 'today' : value === dateWithOffset(1) ? 'tomorrow' : value ? '' : 'none';
  $$('[data-move-date]').forEach(button => button.classList.toggle('active', button.dataset.moveDate === quickValue));
}

function openMoveTask(task) {
  const form = $('#moveTaskForm');
  form.reset();
  form.elements.id.value = task.id;
  form.elements.project_id.value = task.project_id || '';
  form.elements.scheduled_date.value = task.scheduled_date || '';
  $('#moveTaskName').textContent = task.title;
  $('#moveTaskError').textContent = '';
  updateQuickDateSelection();
  $('#moveTaskDialog').showModal();
  requestAnimationFrame(() => form.elements.project_id.focus());
}

$$('[data-move-close]').forEach(button => button.addEventListener('click', () => $('#moveTaskDialog').close()));
$$('[data-move-date]').forEach(button => button.addEventListener('click', () => {
  $('#moveScheduledDate').value = button.dataset.moveDate === 'today' ? today() : button.dataset.moveDate === 'tomorrow' ? dateWithOffset(1) : '';
  updateQuickDateSelection();
}));
$('#moveScheduledDate').addEventListener('input', updateQuickDateSelection);

$('#moveTaskForm').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  const task = state.tasks.find(item => item.id === event.currentTarget.elements.id.value);
  if (!task) return $('#moveTaskDialog').close();
  const changes = {
    project_id: event.currentTarget.elements.project_id.value || null,
    scheduled_date: event.currentTarget.elements.scheduled_date.value || null,
  };
  if (changes.project_id === task.project_id && changes.scheduled_date === task.scheduled_date) {
    $('#moveTaskDialog').close();
    return;
  }
  submit.disabled = true;
  $('#moveTaskError').textContent = '';
  try {
    const { task: updated } = await api(`/tasks/${task.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: task.version }) });
    state.tasks = state.tasks.map(item => item.id === task.id ? updated : item);
    $('#moveTaskDialog').close();
    render();
    toast('Задача перенесена');
  } catch (error) {
    $('#moveTaskError').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

async function patchTask(old, changes, successMessage = '') {
  try {
    const { task, next_task: nextTask } = await api(`/tasks/${old.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: old.version }) });
    state.tasks = [...state.tasks.map(item => item.id === old.id ? task : item), ...(nextTask ? [nextTask] : [])];
    render();
    if (successMessage) toast(nextTask ? `${successMessage}. Следующая задача создана` : successMessage);
  } catch (error) {
    render();
    toast(error.message);
  }
}

async function deleteTask(task) {
  if (!await confirmAction({ title: 'Удалить задачу?', message: `«${task.title}» и её подзадачи будут удалены.`, confirmLabel: 'Удалить', danger: true })) return;
  try {
    await api(`/tasks/${task.id}`, { method: 'DELETE' });
    state.tasks = state.tasks.filter(item => item.id !== task.id);
    state.checklistItems = state.checklistItems.filter(item => item.task_id !== task.id);
    state.expandedChecklistTasks.delete(task.id);
    render();
    toast('Задача удалена');
  } catch (error) { toast(error.message); }
}

const PROJECT_COLOR_NAMES = {
  '#6d5dfc': 'Фиолетовый', '#3b82f6': 'Синий', '#06a5a5': 'Бирюзовый', '#22a447': 'Зелёный',
  '#84a920': 'Лаймовый', '#e8a126': 'Янтарный', '#e2653f': 'Оранжевый', '#df4f68': 'Красный',
  '#d14fa2': 'Розовый', '#7b6f68': 'Коричневый', '#64748b': 'Серый', '#252724': 'Графитовый',
};

function setProjectColor(value, updateText = true) {
  const normalized = /^#[0-9a-f]{6}$/i.test(value) ? value.toLowerCase() : '#6d5dfc';
  $('#projectColorValue').value = normalized;
  $('#projectColorPreview').style.background = normalized;
  $('#projectColorName').textContent = PROJECT_COLOR_NAMES[normalized] || 'Свой цвет';
  if (updateText) $('#projectColorHex').value = normalized;
  $$('[data-project-color]').forEach(button => {
    const selected = button.dataset.projectColor === normalized;
    button.setAttribute('aria-checked', String(selected));
    button.classList.toggle('selected', selected);
  });
}

$$('[data-project-color]').forEach(button => {
  button.style.background = button.dataset.projectColor;
  button.setAttribute('role', 'radio');
  button.addEventListener('click', () => setProjectColor(button.dataset.projectColor));
});
$('#projectColorHex').addEventListener('input', event => {
  const value = event.currentTarget.value.trim();
  const valid = /^#[0-9a-f]{6}$/i.test(value);
  $('#projectError').textContent = valid || !value ? '' : 'Введите HEX-цвет в формате #6d5dfc.';
  if (valid) setProjectColor(value, false);
});

function openProject(project = null) {
  const form = $('#projectForm');
  form.reset();
  form.elements.id.value = project?.id || '';
  form.elements.name.value = project?.name || '';
  setProjectColor(project?.color || '#6d5dfc');
  $('#projectDialogTitle').textContent = project ? 'Настроить проект' : 'Новый проект';
  $('#projectError').textContent = '';
  $('#deleteProject').hidden = !project;
  $('#archiveProject').hidden = !project;
  $('#projectDialog').showModal();
}

$('#newProject').addEventListener('click', () => openProject());
$$('[data-project-close]').forEach(button => button.addEventListener('click', () => $('#projectDialog').close()));

$('#projectForm').addEventListener('submit', async event => {
  event.preventDefault();
  const colorText = $('#projectColorHex').value.trim();
  if (!/^#[0-9a-f]{6}$/i.test(colorText)) {
    $('#projectError').textContent = 'Введите HEX-цвет в формате #6d5dfc.';
    $('#projectColorHex').focus();
    return;
  }
  setProjectColor(colorText);
  const submit = event.submitter;
  submit.disabled = true;
  const values = Object.fromEntries(new FormData(event.currentTarget));
  const id = values.id;
  delete values.id;
  try {
    if (id) {
      const current = state.projects.find(project => project.id === id);
      const { project } = await api(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify({ ...values, expected_version: current.version }) });
      state.projects = state.projects.map(item => item.id === id ? project : item);
    } else {
      const { project } = await api('/projects', { method: 'POST', body: JSON.stringify(values) });
      state.projects.push(project);
    }
    $('#projectDialog').close();
    render();
    toast('Проект сохранён');
  } catch (error) { $('#projectError').textContent = error.message; }
  finally { submit.disabled = false; }
});

$('#deleteProject').addEventListener('click', async () => {
  const id = $('#projectForm').elements.id.value;
  const project = state.projects.find(item => item.id === id);
  if (!project || !await confirmAction({ title: 'Удалить проект?', message: `«${project.name}» будет удалён, а его задачи останутся без проекта.`, confirmLabel: 'Удалить', danger: true })) return;
  try {
    await api(`/projects/${id}`, { method: 'DELETE' });
    state.projects = state.projects.filter(item => item.id !== id);
    state.tasks = state.tasks.map(task => task.project_id === id ? { ...task, project_id: null, version: task.version + 1 } : task);
    if (state.filter === `project:${id}`) state.filter = 'all';
    if (state.taskFilters.project === id) state.taskFilters.project = 'all';
    $('#projectDialog').close();
    render();
    toast('Проект удалён, задачи сохранены');
  } catch (error) { $('#projectError').textContent = error.message; }
});

$('#archiveProject').addEventListener('click', async () => {
  const id = $('#projectForm').elements.id.value;
  const project = state.projects.find(item => item.id === id);
  if (!project) return;
  $('#archiveProject').disabled = true;
  try {
    const { project: archived } = await api(`/projects/${id}/archive`, { method: 'POST', body: JSON.stringify({ expected_version: project.version }) });
    state.projects = state.projects.filter(item => item.id !== id);
    state.archivedProjects.push(archived);
    if (state.filter === `project:${id}`) state.filter = 'all';
    if (state.taskFilters.project === id) state.taskFilters.project = 'all';
    $('#projectDialog').close();
    render();
    toast(`Проект «${project.name}» перемещён в архив`);
  } catch (error) {
    $('#projectError').textContent = error.message;
  } finally {
    $('#archiveProject').disabled = false;
  }
});

function renderArchive() {
  $('#archiveEmpty').hidden = state.archivedProjects.length > 0;
  $('#archiveList').innerHTML = state.archivedProjects.map(project => `<article class="archive-item" data-id="${project.id}"><i style="background:${escapeAttribute(project.color)}"></i><div><strong>${escapeHtml(project.name)}</strong><small>${state.tasks.filter(task => task.project_id === project.id).length} задач</small></div><button class="secondary restore-project" type="button">Восстановить</button></article>`).join('');
  $$('.restore-project', $('#archiveList')).forEach(button => button.addEventListener('click', async () => {
    const project = state.archivedProjects.find(item => item.id === button.closest('.archive-item').dataset.id);
    button.disabled = true;
    $('#archiveError').textContent = '';
    try {
      const { project: restored } = await api(`/projects/${project.id}/archive`, { method: 'DELETE', body: JSON.stringify({ expected_version: project.version }) });
      state.archivedProjects = state.archivedProjects.filter(item => item.id !== project.id);
      state.projects.push(restored);
      renderArchive();
      render();
      toast(`Проект «${project.name}» восстановлен`);
    } catch (error) {
      $('#archiveError').textContent = error.message;
      button.disabled = false;
    }
  }));
}

function openArchive() {
  $('#archiveError').textContent = '';
  renderArchive();
  $('#archiveDialog').showModal();
}

$('#projectArchive').addEventListener('click', openArchive);
$$('[data-archive-close]').forEach(button => button.addEventListener('click', () => $('#archiveDialog').close()));

function openDataDialog() {
  $('#dataError').textContent = '';
  $('#dataSuccess').textContent = '';
  $('#importDataFile').value = '';
  $('#dataDialog').showModal();
}

$('#dataTools').addEventListener('click', openDataDialog);
$('#mobileDataTools').addEventListener('click', () => {
  $('#mobileMoreDialog').close();
  openDataDialog();
});
$$('[data-data-close]').forEach(button => button.addEventListener('click', () => $('#dataDialog').close()));

$('#exportData').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  $('#dataError').textContent = '';
  $('#dataSuccess').textContent = '';
  try {
    const exported = await api('/data/export');
    const blob = new Blob([JSON.stringify(exported, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `taskflow-export-${today()}.json`;
    link.click();
    URL.revokeObjectURL(url);
    $('#dataSuccess').textContent = 'Экспорт подготовлен и скачан.';
  } catch (error) {
    $('#dataError').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$('#importDataFile').addEventListener('change', async event => {
  const input = event.currentTarget;
  const file = input.files?.[0];
  if (!file) return;
  $('#dataError').textContent = '';
  $('#dataSuccess').textContent = '';
  if (file.size > 1_000_000) {
    $('#dataError').textContent = 'Файл больше 1 МБ.';
    input.value = '';
    return;
  }
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch {
    $('#dataError').textContent = 'Файл содержит некорректный JSON.';
    input.value = '';
    return;
  }
  const isYougile = !payload?.format && typeof payload?.title === 'string' && Array.isArray(payload?.boards) && payload?.tasks && typeof payload.tasks === 'object';
  const sourceName = isYougile ? 'YouGile' : 'TaskFlow';
  if (!await confirmAction({ title: `Импортировать данные ${sourceName}?`, message: `Из файла «${file.name}» будут добавлены новые проекты, колонки и задачи. Существующие записи не изменятся.`, confirmLabel: 'Импортировать' })) {
    input.value = '';
    return;
  }
  input.disabled = true;
  try {
    const endpoint = isYougile ? '/data/import/yougile' : '/data/import';
    const { imported, skipped } = await api(endpoint, { method: 'POST', body: JSON.stringify(payload) });
    $('#dataDialog').close();
    await bootstrap();
    const skippedTotal = skipped ? Object.values(skipped).reduce((total, count) => total + count, 0) : 0;
    toast(`Импортировано: ${imported.projects} проектов, ${imported.tasks} задач, ${imported.task_messages || 0} сообщений, ${imported.notes || 0} заметок${skippedTotal ? `; не перенесено: ${skippedTotal}` : ''}`);
  } catch (error) {
    $('#dataError').textContent = error.message;
  } finally {
    input.disabled = false;
    input.value = '';
  }
});

async function startApplication() {
  const verificationToken = location.hash.startsWith('#verify=') ? location.hash.slice('#verify='.length) : '';
  const resetToken = location.hash.startsWith('#reset-password=') ? location.hash.slice('#reset-password='.length) : '';
  const emailChangeToken = location.hash.startsWith('#confirm-email-change=') ? location.hash.slice('#confirm-email-change='.length) : '';
  if (resetToken) return showPasswordReset(resetToken);
  if (emailChangeToken) {
    setAuthenticated(false);
    try {
      await api('/auth/confirm-email-change', { method: 'POST', body: JSON.stringify({ token: emailChangeToken }) });
      history.replaceState({}, '', location.pathname);
      showAuth();
      $('#authSuccess').textContent = 'Новый email подтверждён. Войдите с текущим паролем.';
    } catch (error) {
      history.replaceState({}, '', location.pathname);
      showAuth();
      $('#authError').textContent = error.message;
    }
    return;
  }
  if (!verificationToken) return bootstrap();
  setAuthenticated(false);
  $('#authError').textContent = '';
  $('#authSuccess').textContent = 'Подтверждаем email…';
  try {
    const data = await api('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token: verificationToken }) });
    state.token = data.token;
    localStorage.setItem('taskflow_token', state.token);
    history.replaceState({}, '', location.pathname);
    await bootstrap();
    toast('Email подтверждён. Добро пожаловать!');
  } catch (error) {
    history.replaceState({}, '', location.pathname);
    $('#authSuccess').textContent = '';
    $('#authError').textContent = error.message;
    $('#resendVerification').hidden = false;
  }
}

startApplication();
