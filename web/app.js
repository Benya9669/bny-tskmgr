const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { token: localStorage.getItem('taskflow_token'), user: null, tasks: [], projects: [], filter: 'today', sort: 'priority', view: 'list' };
const today = () => new Date().toLocaleDateString('sv-SE');
const BOARD_COLUMNS = [
  { status: 'inbox', title: 'Входящие', hint: 'Новые идеи и задачи' },
  { status: 'todo', title: 'Запланировано', hint: 'Готово к началу' },
  { status: 'in_progress', title: 'В работе', hint: 'Текущий фокус' },
  { status: 'done', title: 'Выполнено', hint: 'Готово' },
];

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
  setTimeout(() => element.classList.remove('show'), 2200);
}

function setAuthenticated(yes) {
  $('#authView').hidden = yes;
  $('#appView').hidden = !yes;
}

async function bootstrap() {
  if (!state.token) return setAuthenticated(false);
  try {
    const [{ user }, { tasks }, { projects }, health] = await Promise.all([api('/me'), api('/tasks'), api('/projects'), fetch('/api/health').then(response => response.json())]);
    Object.assign(state, { user, tasks, projects, version: health.version });
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
  setAuthenticated(false);
}
$('#logout').addEventListener('click', logout);
$('#mobileLogout').addEventListener('click', logout);

function filteredTasks() {
  if (state.view === 'board') {
    if (state.filter.startsWith('project:')) return state.tasks.filter(task => task.project_id === state.filter.split(':')[1]);
    return [...state.tasks];
  }
  let tasks = state.tasks.filter(task => {
    if (state.filter === 'today') return task.scheduled_date && task.scheduled_date <= today() && task.status !== 'done';
    if (state.filter === 'inbox') return task.status === 'inbox';
    if (state.filter === 'done') return task.status === 'done';
    if (state.filter.startsWith('project:')) return task.project_id === state.filter.split(':')[1];
    return task.status !== 'done';
  });
  const weight = { urgent: 0, high: 1, normal: 2, low: 3 };
  return tasks.sort((a, b) => state.sort === 'priority' ? weight[a.priority] - weight[b.priority] : b.created_at.localeCompare(a.created_at));
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
  $$('[data-task-view]').forEach(button => button.classList.toggle('active', button.dataset.taskView === state.view));
  $('.progress-card').hidden = state.view === 'board';
  $('.task-section').hidden = state.view === 'board';
  $('#boardSection').hidden = state.view !== 'board';
  renderProjects();
  if (state.view === 'board') renderBoard(tasks);
  else renderTasks(tasks);
  const relevant = state.tasks.filter(task => task.scheduled_date === today());
  const done = relevant.filter(task => task.status === 'done').length;
  const percent = relevant.length ? Math.round(done / relevant.length * 100) : 0;
  $('#progressText').textContent = `${done} из ${relevant.length} задач`;
  $('#progressPercent').textContent = `${percent}%`;
  $('#progressBar').style.width = `${percent}%`;
}

function renderProjects() {
  $('#projectList').innerHTML = state.projects.map(project => `<div class="project-row"><button class="project-item" data-project="${project.id}"><i style="background:${escapeHtml(project.color)}"></i><span>${escapeHtml(project.name)}</span></button><button class="project-edit" data-edit-project="${project.id}" aria-label="Настроить проект">•••</button></div>`).join('');
  $('#mobileProjectList').innerHTML = state.projects.map(project => `<button class="mobile-project-item ${state.filter === `project:${project.id}` ? 'active' : ''}" data-mobile-project="${project.id}"><i style="background:${escapeHtml(project.color)}"></i>${escapeHtml(project.name)}</button>`).join('');
  $('#projectSelect').innerHTML = '<option value="">Без проекта</option>' + state.projects.map(project => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join('');
  $$('.project-item').forEach(button => button.addEventListener('click', () => { state.filter = `project:${button.dataset.project}`; render(); }));
  $$('.mobile-project-item').forEach(button => button.addEventListener('click', () => { state.filter = `project:${button.dataset.mobileProject}`; render(); }));
  $$('.project-edit').forEach(button => button.addEventListener('click', () => openProject(state.projects.find(project => project.id === button.dataset.editProject))));
}

function renderTasks(tasks) {
  $('#taskList').innerHTML = tasks.map(task => {
    const project = state.projects.find(item => item.id === task.project_id);
    const priority = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' }[task.priority];
    const status = { inbox: 'Входящие', todo: 'Запланировано', in_progress: 'В работе', done: 'Выполнено' }[task.status];
    const estimate = task.estimated_minutes ? `<span>◷ ${task.estimated_minutes} мин</span>` : '';
    const overdue = task.scheduled_date && task.scheduled_date < today() && task.status !== 'done' ? '<span class="overdue">Просрочено</span>' : '';
    return `<article class="task ${task.status === 'done' ? 'done' : ''}" data-id="${task.id}">
      <button class="check" aria-label="${task.status === 'done' ? 'Вернуть задачу' : 'Выполнить задачу'}">✓</button>
      <div class="task-main"><div class="task-title">${escapeHtml(task.title)}</div><div class="task-meta">
        <span class="priority-${task.priority}"><i class="dot" style="background:${task.priority === 'urgent' || task.priority === 'high' ? '#df695f' : '#aaa'}"></i>${priority}</span>
        <span>${status}</span>${project ? `<span>${escapeHtml(project.name)}</span>` : ''}${estimate}${overdue}</div></div>
      <div class="task-actions"><button class="icon-button edit-task" aria-label="Изменить">✎</button><button class="icon-button delete-task" aria-label="Удалить">×</button></div>
    </article>`;
  }).join('');
  $('#emptyState').hidden = tasks.length > 0;
  $$('.task').forEach(element => {
    const task = state.tasks.find(item => item.id === element.dataset.id);
    $('.check', element).addEventListener('click', () => patchTask(task, { status: task.status === 'done' ? 'todo' : 'done' }));
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
      const project = state.projects.find(item => item.id === task.project_id);
      const date = task.scheduled_date ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' }).format(new Date(`${task.scheduled_date}T12:00:00`)) : '';
      const overdue = task.scheduled_date && task.scheduled_date < today() && task.status !== 'done';
      return `<article class="kanban-card" draggable="true" data-id="${task.id}" data-priority="${task.priority}">
        <div class="kanban-card-head"><span class="kanban-priority priority-${task.priority}"><i class="dot"></i>${priorityNames[task.priority]}</span><button class="icon-button board-edit" type="button" aria-label="Изменить задачу">•••</button></div>
        <h3>${escapeHtml(task.title)}</h3>
        ${task.description ? `<p>${escapeHtml(task.description)}</p>` : ''}
        <div class="kanban-card-meta">${project ? `<span><i class="project-dot" style="background:${escapeHtml(project.color)}"></i>${escapeHtml(project.name)}</span>` : '<span>Без проекта</span>'}${date ? `<span class="${overdue ? 'overdue' : ''}">◷ ${date}</span>` : ''}${task.estimated_minutes ? `<span>${task.estimated_minutes} мин</span>` : ''}</div>
        <label class="kanban-status-label">Переместить<select class="kanban-status" aria-label="Статус задачи">${BOARD_COLUMNS.map(option => `<option value="${option.status}" ${option.status === task.status ? 'selected' : ''}>${option.title}</option>`).join('')}</select></label>
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
    $('.board-edit', card).addEventListener('click', () => openTask(task));
    $('.kanban-status', card).addEventListener('change', event => moveTask(task, event.currentTarget.value));
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

function moveTask(task, status) {
  if (task.status === status) return;
  patchTask(task, { status }, `Задача перемещена в «${BOARD_COLUMNS.find(column => column.status === status).title}»`);
}

function escapeHtml(value = '') {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
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
  const values = task || { scheduled_date: plannedToday ? today() : '', project_id: state.filter.startsWith('project:') ? state.filter.split(':')[1] : '', priority: 'normal', status: initialStatus || (plannedToday ? 'todo' : 'inbox') };
  ['id', 'title', 'description', 'scheduled_date', 'priority', 'status', 'project_id', 'estimated_minutes'].forEach(key => { if (form.elements[key]) form.elements[key].value = values[key] ?? ''; });
  $('#taskDialog').showModal();
}
$('#addTask').addEventListener('click', () => openTask());
$$('[data-close]').forEach(button => button.addEventListener('click', () => $('#taskDialog').close()));

$('#taskForm').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  const raw = Object.fromEntries(new FormData(event.currentTarget));
  const id = raw.id;
  delete raw.id;
  Object.keys(raw).forEach(key => { if (raw[key] === '') raw[key] = null; });
  if (raw.estimated_minutes) raw.estimated_minutes = Number(raw.estimated_minutes);
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
  if (!confirm(`Удалить задачу «${task.title}»?`)) return;
  try {
    await api(`/tasks/${task.id}`, { method: 'DELETE' });
    state.tasks = state.tasks.filter(item => item.id !== task.id);
    render();
    toast('Задача удалена');
  } catch (error) { toast(error.message); }
}

function openProject(project = null) {
  const form = $('#projectForm');
  form.reset();
  form.elements.id.value = project?.id || '';
  form.elements.name.value = project?.name || '';
  form.elements.color.value = project?.color || '#6d5dfc';
  $('#projectDialogTitle').textContent = project ? 'Настроить проект' : 'Новый проект';
  $('#projectError').textContent = '';
  $('#deleteProject').hidden = !project;
  $('#projectDialog').showModal();
}

$('#newProject').addEventListener('click', () => openProject());
$$('[data-project-close]').forEach(button => button.addEventListener('click', () => $('#projectDialog').close()));

$('#projectForm').addEventListener('submit', async event => {
  event.preventDefault();
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
  if (!project || !confirm(`Удалить проект «${project.name}»? Задачи останутся без проекта.`)) return;
  try {
    await api(`/projects/${id}`, { method: 'DELETE' });
    state.projects = state.projects.filter(item => item.id !== id);
    state.tasks = state.tasks.map(task => task.project_id === id ? { ...task, project_id: null, version: task.version + 1 } : task);
    if (state.filter === `project:${id}`) state.filter = 'all';
    $('#projectDialog').close();
    render();
    toast('Проект удалён, задачи сохранены');
  } catch (error) { $('#projectError').textContent = error.message; }
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
