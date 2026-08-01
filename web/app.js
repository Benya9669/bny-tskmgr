const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  token: localStorage.getItem('taskflow_token'),
  user: null,
  tasks: [],
  projects: [],
  archivedProjects: [],
  checklistItems: [],
  checklistTaskId: null,
  expandedChecklistTasks: new Set(),
  filter: 'today',
  sort: 'priority',
  view: 'list',
  taskFilters: { query: '', project: 'all', status: 'all', priority: 'all', date: 'all', dateFrom: '', dateTo: '' },
};
const today = () => new Date().toLocaleDateString('sv-SE');
const isTaskOverdue = task => Boolean(task.due_at && new Date(task.due_at).getTime() < Date.now() && task.status !== 'done');
const formatPlannedDate = value => new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' }).format(new Date(`${value}T12:00:00`));
const formatDueAt = value => new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
const toDateTimeLocal = value => {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const BOARD_COLUMNS = [
  { status: 'inbox', title: 'Входящие', hint: 'Новые идеи и задачи' },
  { status: 'todo', title: 'Запланировано', hint: 'Готово к началу' },
  { status: 'in_progress', title: 'В работе', hint: 'Текущий фокус' },
  { status: 'done', title: 'Выполнено', hint: 'Готово' },
];

const ICON_PATHS = {
  check: '<path d="m5 12 4 4L19 7"/>',
  checklist: '<path d="m4 6 1.5 1.5L8 4.8M11 6h9M4 12l1.5 1.5L8 10.8M11 12h9M4 18l1.5 1.5L8 16.8M11 18h9"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  edit: '<path d="M4 20h4l11-11-4-4L4 16v4ZM13.5 6.5l4 4"/>',
  move: '<path d="M5 19 19 5M10 5h9v9"/>',
  trash: '<path d="M4 7h16M9 11v6M15 11v6M6 7l1 14h10l1-14M9 7V4h6v3"/>',
  more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
function icon(name) {
  return `<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON_PATHS[name]}</svg>`;
}

function applyTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === 'dark' ? '#171816' : '#f5f3ee';
  $$('[data-theme-toggle]').forEach(button => {
    const dark = theme === 'dark';
    button.querySelector('span').textContent = dark ? '☀' : '☾';
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

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  $('#srStatus').textContent = message;
  setTimeout(() => element.classList.remove('show'), 2200);
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
  $('#skipLink').href = '#authForm';
  setRegisterMode(register);
  requestAnimationFrame(() => $('#authForm').elements[register ? 'display_name' : 'email'].focus());
}

async function bootstrap() {
  if (!state.token) return showLanding();
  try {
    const [{ user }, { tasks }, { projects: allProjects }, { checklist_items: checklistItems }, health] = await Promise.all([api('/me'), api('/tasks'), api('/projects?include_archived=true'), api('/checklist'), fetch('/api/health').then(response => response.json())]);
    const projects = allProjects.filter(project => !project.archived_at);
    const archivedProjects = allProjects.filter(project => project.archived_at);
    Object.assign(state, { user, tasks, projects, archivedProjects, checklistItems, version: health.version });
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

function logout() {
  state.token = null;
  localStorage.removeItem('taskflow_token');
  showLanding();
}
$('#logout').addEventListener('click', logout);
$('#mobileLogout').addEventListener('click', logout);

function filteredTasks() {
  const matchesTaskFilters = task => {
    const filters = state.taskFilters;
    const query = filters.query.trim().toLocaleLowerCase('ru-RU');
    if (query && !`${task.title} ${task.description || ''}`.toLocaleLowerCase('ru-RU').includes(query)) return false;
    if (filters.project === 'none' && task.project_id) return false;
    if (filters.project !== 'all' && filters.project !== 'none' && task.project_id !== filters.project) return false;
    if (filters.status !== 'all' && task.status !== filters.status) return false;
    if (filters.priority !== 'all' && task.priority !== filters.priority) return false;
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
  return Number(Boolean(filters.query.trim())) + ['project', 'status', 'priority'].filter(key => filters[key] !== 'all').length + Number(filters.date !== 'all');
}

function renderTaskFilters(resultCount) {
  const filters = state.taskFilters;
  const projectFilter = $('#filterProject');
  projectFilter.innerHTML = '<option value="all">Все проекты</option><option value="none">Без проекта</option>' + state.projects.map(project => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join('');
  projectFilter.value = filters.project;
  $('#filterStatus').value = filters.status;
  $('#filterPriority').value = filters.priority;
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
}

function render() {
  const tasks = filteredTasks();
  const project = state.filter.startsWith('project:') ? state.projects.find(item => item.id === state.filter.split(':')[1]) : null;
  const titles = { today: ['ВАШ ДЕНЬ', 'Сегодня', 'План на сегодня'], inbox: ['БЫСТРЫЙ СБОР', 'Входящие', 'Неразобранные задачи'], all: ['ОБЩАЯ КАРТИНА', 'Все задачи', 'Активные задачи'], done: ['АРХИВ', 'Выполненные', 'Завершённые задачи'] };
  const title = state.view === 'board'
    ? (project ? ['КАНБАН ПРОЕКТА', project.name, `Доска проекта «${project.name}»`] : ['РАБОЧИЙ ПРОЦЕСС', 'Доска', 'Все задачи по статусам'])
    : (project ? ['ПРОЕКТ', project.name, `Задачи проекта «${project.name}»`] : titles[state.filter]);
  $('#viewEyebrow').textContent = title[0];
  $('#viewTitle').textContent = title[1];
  $('#listTitle').textContent = title[2];
  $('#dateLabel').textContent = new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date());
  $('#userName').textContent = state.user.display_name;
  $('#profileMeta').textContent = `v${state.version || 'dev'} · Выйти`;
  $('#avatar').textContent = state.user.display_name.slice(0, 1).toUpperCase();
  $('#todayCount').textContent = state.tasks.filter(task => task.scheduled_date && task.scheduled_date <= today() && task.status !== 'done').length;
  $('#inboxCount').textContent = state.tasks.filter(task => task.status === 'inbox').length;
  $$('.nav-item, .mobile-nav-item').forEach(button => button.classList.toggle('active', state.view === 'board' ? button.dataset.view === 'board' : button.dataset.filter === state.filter));
  $$('.nav-item, .mobile-nav-item').forEach(button => button.setAttribute('aria-current', button.classList.contains('active') ? 'page' : 'false'));
  $$('[data-task-view]').forEach(button => button.classList.toggle('active', button.dataset.taskView === state.view));
  $('.progress-card').hidden = state.view === 'board';
  $('.task-section').hidden = state.view === 'board';
  $('#boardSection').hidden = state.view !== 'board';
  renderProjects();
  renderTaskFilters(tasks.length);
  if (state.view === 'board') renderBoard(tasks);
  else renderTasks(tasks);
  const relevant = state.tasks.filter(task => task.scheduled_date === today());
  const done = relevant.filter(task => task.status === 'done').length;
  const percent = relevant.length ? Math.round(done / relevant.length * 100) : 0;
  $('#progressText').textContent = `${done} из ${relevant.length} задач`;
  $('#progressPercent').textContent = `${percent}%`;
  $('#progressBar').style.width = `${percent}%`;
  $('#dailyProgress').setAttribute('aria-valuenow', String(percent));
  $('#dailyProgress').setAttribute('aria-valuetext', `${done} из ${relevant.length} задач выполнено`);
}

function renderProjects() {
  $('#projectList').innerHTML = state.projects.map(project => `<div class="project-row"><button class="project-item" data-project="${project.id}"><i style="background:${escapeHtml(project.color)}"></i><span>${escapeHtml(project.name)}</span></button><button class="project-edit" data-edit-project="${project.id}" aria-label="Настроить проект">•••</button></div>`).join('');
  $('#mobileProjectList').innerHTML = state.projects.map(project => `<button class="mobile-project-item ${state.filter === `project:${project.id}` ? 'active' : ''}" data-mobile-project="${project.id}"><i style="background:${escapeHtml(project.color)}"></i>${escapeHtml(project.name)}</button>`).join('') + '<button class="mobile-project-item mobile-archive" type="button" data-open-archive>⌂ Архив</button>';
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
    const due = task.due_at ? `<span class="${isTaskOverdue(task) ? 'overdue' : 'task-due'}">Срок: ${formatDueAt(task.due_at)}</span>` : '';
    return `<article class="task ${task.status === 'done' ? 'done' : ''}" data-id="${task.id}">
      <button class="check" aria-label="${task.status === 'done' ? 'Вернуть задачу' : 'Выполнить задачу'}">${icon('check')}</button>
      <div class="task-main"><div class="task-title">${escapeHtml(task.title)}</div><div class="task-meta">
        <span class="priority-${task.priority}"><i class="dot" style="background:${task.priority === 'urgent' || task.priority === 'high' ? '#df695f' : '#aaa'}"></i>${priority}</span>
        <span>${status}</span>${project ? `<span>${escapeHtml(project.name)}</span>` : ''}${planned}${due}${estimate}</div></div>
      <div class="task-actions"><button class="icon-button checklist-task" aria-label="Открыть подзадачи" title="Подзадачи">${icon('checklist')}</button><button class="icon-button move-task" aria-label="Перенести задачу" title="Перенести">${icon('move')}</button><button class="icon-button edit-task" aria-label="Изменить">${icon('edit')}</button><button class="icon-button delete-task" aria-label="Удалить">${icon('trash')}</button></div>
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
    $('.move-task', element).addEventListener('click', () => openMoveTask(task));
    $('.edit-task', element).addEventListener('click', () => openTask(task));
    $('.delete-task', element).addEventListener('click', () => deleteTask(task));
  });
}

function renderBoard(tasks) {
  const priorityNames = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' };
  let mouseDragTask = null;
  $('#kanbanBoard').innerHTML = BOARD_COLUMNS.map(column => {
    const columnTasks = tasks.filter(task => task.status === column.status);
    const cards = columnTasks.map(task => {
      const project = findProject(task.project_id);
      const date = task.scheduled_date ? formatPlannedDate(task.scheduled_date) : '';
      const due = task.due_at ? formatDueAt(task.due_at) : '';
      const overdue = isTaskOverdue(task);
      return `<article class="kanban-card" draggable="true" data-id="${task.id}" data-priority="${task.priority}">
        <div class="kanban-card-head"><span class="kanban-priority priority-${task.priority}"><i class="dot"></i>${priorityNames[task.priority]}</span><div class="board-actions"><button class="icon-button board-menu-toggle" type="button" aria-label="Действия задачи «${escapeAttribute(task.title)}»" aria-haspopup="menu" aria-expanded="false">${icon('more')}</button><div class="board-card-menu" role="menu" aria-label="Действия задачи" hidden><button class="board-edit" type="button" role="menuitem">${icon('edit')}<span>Изменить</span></button><button class="board-subtasks" type="button" role="menuitem">${icon('checklist')}<span>Подзадачи</span></button><button class="board-move" type="button" role="menuitem">${icon('move')}<span>Переместить</span></button><button class="board-delete" type="button" role="menuitem">${icon('trash')}<span>Удалить</span></button></div></div></div>
        <h3>${escapeHtml(task.title)}</h3>
        ${task.description ? `<p>${escapeHtml(task.description)}</p>` : ''}
        <div class="kanban-card-meta">${project ? `<span><i class="project-dot" style="background:${escapeHtml(project.color)}"></i>${escapeHtml(project.name)}</span>` : '<span>Без проекта</span>'}${date ? `<span>План: ${date}</span>` : ''}${due ? `<span class="${overdue ? 'overdue' : 'task-due'}">Срок: ${due}</span>` : ''}${task.estimated_minutes ? `<span>${icon('clock')} ${task.estimated_minutes} мин</span>` : ''}</div>
        ${inlineChecklistMarkup(task, 'board')}
      </article>`;
    }).join('');
    return `<section class="kanban-column" data-status="${column.status}">
      <header><div><h2>${column.title}<span>${columnTasks.length}</span></h2><p>${column.hint}</p></div><button class="kanban-add" type="button" data-add-status="${column.status}" aria-label="Добавить задачу в ${column.title}">＋</button></header>
      <div class="kanban-dropzone" data-status="${column.status}">${cards || '<div class="kanban-empty">Перетащите задачу сюда</div>'}</div>
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
        const zone = document.elementFromPoint(upEvent.clientX, upEvent.clientY)?.closest('.kanban-dropzone');
        if (droppedTask?.id === task.id && zone) moveTask(task, zone.dataset.status);
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
      mouseDragTask = null;
      if (task) moveTask(task, zone.dataset.status);
    });
  });
  $$('[data-add-status]').forEach(button => button.addEventListener('click', () => openTask(null, button.dataset.addStatus)));
}

document.addEventListener('click', event => {
  if (event.target.closest('.board-actions')) return;
  $$('.kanban-card.menu-open').forEach(card => {
    card.classList.remove('menu-open');
    $('.board-card-menu', card).hidden = true;
    $('.board-menu-toggle', card).setAttribute('aria-expanded', 'false');
  });
});

function moveTask(task, status) {
  if (task.status === status) return;
  patchTask(task, { status }, `Задача перемещена в «${BOARD_COLUMNS.find(column => column.status === status).title}»`);
}

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
    <button class="checklist-toggle" type="button" aria-label="${item.is_done ? 'Вернуть подзадачу' : 'Выполнить подзадачу'}" aria-pressed="${item.is_done}">${item.is_done ? '✓' : ''}</button>
    <input class="checklist-title-input" value="${escapeAttribute(item.title)}" maxlength="240" aria-label="Название подзадачи">
    <button class="icon-button checklist-delete" type="button" aria-label="Удалить подзадачу">×</button>
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

$$('.nav-item, .mobile-nav-item').forEach(button => button.addEventListener('click', () => {
  state.view = button.dataset.view || 'list';
  if (button.dataset.view === 'board') state.filter = 'all';
  else state.filter = button.dataset.filter;
  render();
}));
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
  state.taskFilters.query = event.currentTarget.value;
  render();
});
$$('[data-task-filter]').forEach(control => control.addEventListener('change', event => {
  state.taskFilters[event.currentTarget.dataset.taskFilter] = event.currentTarget.value;
  render();
}));
$('#clearFilters').addEventListener('click', () => {
  Object.assign(state.taskFilters, { query: '', project: 'all', status: 'all', priority: 'all', date: 'all', dateFrom: '', dateTo: '' });
  $('#taskSearch').value = '';
  render();
  $('#taskSearch').focus();
});
$$('.segmented button').forEach(button => button.addEventListener('click', () => {
  state.sort = button.dataset.sort;
  $$('.segmented button').forEach(item => item.classList.toggle('active', item === button));
  render();
}));

function openTask(task = null, initialStatus = null) {
  const form = $('#taskForm');
  form.reset();
  $('#taskError').textContent = '';
  $('#dialogTitle').textContent = task ? 'Изменить задачу' : 'Новая задача';
  const plannedToday = state.filter === 'today';
  const values = task ? { ...task, due_at: toDateTimeLocal(task.due_at) } : { scheduled_date: plannedToday ? today() : '', due_at: '', project_id: state.filter.startsWith('project:') ? state.filter.split(':')[1] : '', priority: 'normal', status: initialStatus || (plannedToday ? 'todo' : 'inbox') };
  ['id', 'title', 'description', 'scheduled_date', 'due_at', 'priority', 'status', 'project_id', 'estimated_minutes'].forEach(key => { if (form.elements[key]) form.elements[key].value = values[key] ?? ''; });
  $('#taskDialog').showModal();
  requestAnimationFrame(() => form.elements.title.focus());
}
$('#addTask').addEventListener('click', () => openTask());
$$('[data-close]').forEach(button => button.addEventListener('click', () => $('#taskDialog').close()));

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
  if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !typing && state.token && !$('#appView').hidden && !document.querySelector('dialog[open]')) {
    event.preventDefault();
    $('#taskSearch').focus();
    return;
  }
  if (event.key.toLowerCase() !== 'n' || event.ctrlKey || event.metaKey || event.altKey || event.repeat || typing) return;
  if (!state.token || $('#appView').hidden || document.querySelector('dialog[open]')) return;
  event.preventDefault();
  openTask();
});

$('#taskForm').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  const raw = Object.fromEntries(new FormData(event.currentTarget));
  const id = raw.id;
  delete raw.id;
  Object.keys(raw).forEach(key => { if (raw[key] === '') raw[key] = null; });
  if (raw.estimated_minutes) raw.estimated_minutes = Number(raw.estimated_minutes);
  if (raw.due_at) raw.due_at = new Date(raw.due_at).toISOString();
  try {
    if (id) {
      const old = state.tasks.find(task => task.id === id);
      const { task } = await api(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify({ ...raw, expected_version: old.version }) });
      state.tasks = state.tasks.map(item => item.id === id ? task : item);
    } else {
      const { task } = await api('/tasks', { method: 'POST', body: JSON.stringify(raw) });
      state.tasks.push(task);
    }
    $('#taskDialog').close();
    render();
    toast('Задача сохранена');
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
    const { task } = await api(`/tasks/${old.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: old.version }) });
    state.tasks = state.tasks.map(item => item.id === old.id ? task : item);
    render();
    if (successMessage) toast(successMessage);
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
  if (!await confirmAction({ title: 'Импортировать данные?', message: `Из файла «${file.name}» будут добавлены новые копии записей. Существующие данные не изменятся.`, confirmLabel: 'Импортировать' })) {
    input.value = '';
    return;
  }
  input.disabled = true;
  try {
    const payload = JSON.parse(await file.text());
    const { imported } = await api('/data/import', { method: 'POST', body: JSON.stringify(payload) });
    $('#dataDialog').close();
    await bootstrap();
    toast(`Импортировано: ${imported.projects} проектов, ${imported.tasks} задач, ${imported.checklist_items} подзадач`);
  } catch (error) {
    $('#dataError').textContent = error instanceof SyntaxError ? 'Файл содержит некорректный JSON.' : error.message;
  } finally {
    input.disabled = false;
    input.value = '';
  }
});

async function startApplication() {
  const verificationToken = location.hash.startsWith('#verify=') ? location.hash.slice('#verify='.length) : '';
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
