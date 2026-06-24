const state = { token: localStorage.getItem('crm_token') || '', user: JSON.parse(localStorage.getItem('crm_user') || 'null'), users: [], influencers: [], selectedId: null, dashboard: null };
const $ = (id) => document.getElementById(id);
const today = new Date().toISOString().slice(0, 10);
$('statDate').value = today;

function toast(text) {
  const node = $('toast');
  node.textContent = text;
  node.classList.add('show');
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => node.classList.remove('show'), 2400);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 ${response.status}`);
  return data;
}

function setLoggedIn(on) {
  $('loginView').classList.toggle('hidden', on);
  $('appView').classList.toggle('hidden', !on);
  if (on && state.user) $('userBadge').textContent = `${state.user.display_name} · ${roleName(state.user.role)}`;
}

function roleName(role) {
  return ({ admin: '管理员', manager: '主管', bd: 'BD', operator: '运营' })[role] || role;
}

$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const result = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: $('loginUser').value, password: $('loginPass').value }) });
    state.token = result.token;
    state.user = result.user;
    localStorage.setItem('crm_token', state.token);
    localStorage.setItem('crm_user', JSON.stringify(state.user));
    setLoggedIn(true);
    await bootstrap();
  } catch (error) { toast(error.message); }
});

$('logoutBtn').addEventListener('click', () => {
  localStorage.removeItem('crm_token');
  localStorage.removeItem('crm_user');
  state.token = '';
  state.user = null;
  setLoggedIn(false);
});

document.querySelectorAll('.nav').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.nav').forEach((item) => item.classList.toggle('active', item === button));
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.id === `${button.dataset.view}View`));
  $('pageTitle').textContent = button.textContent;
}));

$('refreshBtn').addEventListener('click', bootstrap);
$('searchInput').addEventListener('input', debounce(loadInfluencers, 250));
$('statusFilter').addEventListener('change', loadInfluencers);
$('ownerFilter').addEventListener('change', loadInfluencers);
$('newBtn').addEventListener('click', () => $('modal').classList.remove('hidden'));
$('closeModal').addEventListener('click', () => $('modal').classList.add('hidden'));
$('exportBtn').addEventListener('click', () => { window.location.href = '/api/crm/export-csv'; });

function debounce(fn, wait) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

async function bootstrap() {
  try {
    const [users] = await Promise.all([api('/api/crm/users')]);
    state.users = users;
    renderUserOptions();
    await Promise.all([loadDashboard(), loadInfluencers(), loadTasks()]);
  } catch (error) {
    toast(error.message);
    if (String(error.message).includes('Invalid session')) setLoggedIn(false);
  }
}

function renderUserOptions() {
  const options = ['<option value="">默认当前用户</option>', ...state.users.map((user) => `<option value="${user.id}">${user.display_name}（${roleName(user.role)}）</option>`)].join('');
  $('ownerSelect').innerHTML = options;
  $('ownerFilter').innerHTML = '<option value="">全部负责人</option>' + state.users.map((user) => `<option value="${user.id}">${user.display_name}</option>`).join('');
}

async function loadDashboard() {
  const data = await api(`/api/crm/dashboard?date=${$('statDate').value || today}`);
  state.dashboard = data;
  const metrics = [
    ['新增达人', data.daily.new_accounts, '今日新录入账号'],
    ['已建联', data.daily.connected, `建联率 ${data.rates.connect_rate}%`],
    ['寄样', data.daily.sampled, `寄样率 ${data.rates.sample_rate}%`],
    ['已出单', data.daily.dealed, `金额 ${Number(data.daily.deal_amount || 0).toFixed(0)}`],
    ['待处理', data.daily.overdue, '逾期或待跟进'],
  ];
  $('metricGrid').innerHTML = metrics.map(([label, value, hint]) => `<article class="metric"><span>${label}</span><b>${value}</b><small>${hint}</small></article>`).join('');
  renderFunnel(data);
  renderStatusBars(data.status_counts || {});
  renderOwnerTable(data.owners || []);
}

function renderFunnel(data) {
  const steps = [
    ['新增/触达', data.funnel.invited || data.daily.new_accounts],
    ['已建联', data.funnel.connected],
    ['寄样', data.funnel.sampled],
    ['已出单', data.funnel.dealed],
  ];
  $('funnel').innerHTML = steps.map(([label, value]) => `<div class="funnel-step"><span>${label}</span><b>${value || 0}</b></div>`).join('');
  $('rateText').textContent = `建联率 ${data.rates.connect_rate}% · 寄样率 ${data.rates.sample_rate}% · 出单率 ${data.rates.deal_rate}%`;
}

function renderStatusBars(counts) {
  const max = Math.max(1, ...Object.values(counts));
  const statuses = ['未建联', '已建联', '寄样', '已出单', '定期维护'];
  $('statusBars').innerHTML = statuses.map((status) => `<div class="bar-row"><span>${status}</span><div class="bar"><i style="width:${((counts[status] || 0) / max) * 100}%"></i></div><b>${counts[status] || 0}</b></div>`).join('');
}

function renderOwnerTable(rows) {
  $('ownerTable').innerHTML = `<thead><tr><th>负责人</th><th>达人总数</th><th>未建联</th><th>已建联</th><th>寄样</th><th>已出单</th><th>出单率</th></tr></thead><tbody>${rows.map((row) => {
    const rate = row.total ? ((row.dealed || 0) / row.total * 100).toFixed(1) : '0.0';
    return `<tr><td><b>${row.owner_name}</b></td><td>${row.total || 0}</td><td>${row.invited || 0}</td><td>${row.connected || 0}</td><td>${row.sampled || 0}</td><td>${row.dealed || 0}</td><td>${rate}%</td></tr>`;
  }).join('')}</tbody>`;
}

async function loadInfluencers() {
  const query = new URLSearchParams();
  if ($('searchInput').value) query.set('search', $('searchInput').value);
  if ($('statusFilter').value) query.set('status', $('statusFilter').value);
  if ($('ownerFilter').value) query.set('owner_id', $('ownerFilter').value);
  const data = await api(`/api/crm/influencers?${query.toString()}`);
  state.influencers = data.items;
  $('statusFilter').innerHTML = '<option value="">全部状态</option>' + data.statuses.map((status) => `<option ${status === $('statusFilter').value ? 'selected' : ''}>${status}</option>`).join('');
  renderInfluencerTable();
  if (state.selectedId) loadDetail(state.selectedId).catch(() => { state.selectedId = null; renderDetail(null); });
}

function statusClass(status) {
  if (status === '已出单') return 'done';
  if (status === '未建联') return 'fail';
  return '';
}

function renderInfluencerTable() {
  $('influencerTable').innerHTML = `<thead><tr><th>跟进时间</th><th>达人</th><th>平台</th><th>类目</th><th>粉丝</th><th>联系方式</th><th>负责人</th><th>状态</th></tr></thead><tbody>${state.influencers.map((item) => `<tr data-id="${item.id}"><td><b>${formatDateTime(item.follow_time || item.updated_at || item.created_at)}</b><small>${item.follow_time ? '最近跟进' : '新增'}</small></td><td><b>${item.nickname || item.account}</b><small>${item.account}</small></td><td>${item.platform}</td><td>${item.category || '-'}</td><td>${formatFollowers(item.followers)}</td><td>${item.contact || '-'}</td><td>${item.owner_name || '-'}</td><td><span class="status-pill ${statusClass(item.status)}">${item.status}</span></td></tr>`).join('')}</tbody>`;
  document.querySelectorAll('#influencerTable tbody tr').forEach((row) => row.addEventListener('click', () => loadDetail(row.dataset.id)));
}

async function loadDetail(id) {
  state.selectedId = id;
  const detail = await api(`/api/crm/influencers/${id}`);
  renderDetail(detail);
}

function renderDetail(detail) {
  if (!detail) {
    $('detailPane').innerHTML = '<div class="empty detail-empty"><b>选择达人</b><span>查看跟进记录和当前状态</span></div>';
    return;
  }
  const item = detail.influencer;
  const initials = (item.nickname || item.account || '达').slice(0, 1).toUpperCase();
  const followups = detail.followups.map((log) => `
    <div class="timeline-item">
      <div><b>${log.action}</b><small>${log.user_name || '-'} · ${formatTime(log.created_at)}</small></div>
      <p>${log.note || '无备注'}</p>
    </div>`).join('') || '<div class="empty timeline-empty"><b>暂无跟进记录</b><span>保存一次跟进后会显示在这里</span></div>';
  $('detailPane').innerHTML = `
    <div class="detail-hero">
      <div class="creator-avatar">${initials}</div>
      <div class="creator-title">
        <h2>${item.nickname || item.account}</h2>
        <span class="status-pill ${statusClass(item.status)}">${item.status}</span>
      </div>
    </div>
    <div class="detail-meta">
      <div><span>平台账号</span><b>${item.platform} · ${item.account}</b></div>
      <div><span>负责人</span><b>${item.owner_name || '-'}</b></div>
      <div><span>粉丝量</span><b>${formatFollowers(item.followers)}</b></div>
      <div><span>类目</span><b>${item.category || '-'}</b></div>
    </div>
    <div class="detail-note"><span>备注</span><p>${item.notes || '暂无备注'}</p></div>
    ${quickActionHtml(item.id)}
    <div class="timeline-head"><h3>跟进记录</h3><span>${detail.followups.length} 条</span></div>
    <div class="timeline">${followups}</div>`;
  bindQuickActions(item.id);
}

function formatFollowers(value) {
  const text = String(value || '').trim();
  if (!text) return '-';
  const numeric = Number(text);
  return Number.isFinite(numeric) && text !== '' ? numeric.toLocaleString() : text;
}

function formatDateTime(value) {
  return formatTime(value).slice(5) || '-';
}

function formatTime(value) {
  if (!value) return '-';
  return value.replace('T', ' ').replace('+00:00', '').slice(0, 16);
}

function quickActionHtml(id) {
  return `<div class="quick-card">
    <div class="quick-head"><h3>新增跟进</h3><span>记录本次动作</span></div>
    <div class="quick-actions simple">
      <label><span>跟进动作</span><select id="actionType"><option>未建联</option><option>已建联</option><option>寄样</option><option>已出单</option><option>定期维护</option></select></label>
      <label class="wide"><span>跟进备注</span><textarea id="actionNote" placeholder="记录沟通进展、寄样反馈或已出单信息"></textarea></label>
      <button class="btn primary wide" id="followBtn">保存跟进</button>
    </div>
  </div>`;
}

function bindQuickActions(id) {
  $('followBtn').addEventListener('click', async () => {
    await api(`/api/crm/influencers/${id}/followups`, { method: 'POST', body: JSON.stringify({ action: $('actionType').value, note: $('actionNote').value }) });
    toast('跟进已保存');
    await Promise.all([loadDashboard(), loadInfluencers(), loadDetail(id), loadTasks()]);
  });
}

$('modalForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget).entries());
  if (!data.owner_id) delete data.owner_id;
  try {
    await api('/api/crm/influencers', { method: 'POST', body: JSON.stringify(data) });
    $('modal').classList.add('hidden');
    event.currentTarget.reset();
    toast('达人已新增');
    await Promise.all([loadDashboard(), loadInfluencers()]);
  } catch (error) { toast(error.message); }
});

async function loadTasks() {
  const data = await api('/api/crm/tasks');
  const groups = [['今日待跟进', data.today], ['逾期未跟进', data.overdue], ['寄样未成交', data.sampled_not_dealed]];
  $('taskGrid').innerHTML = groups.map(([title, rows]) => `<article class="panel"><div class="panel-head"><h2>${title}</h2><span>${rows.length}</span></div><div class="task-list">${rows.map((row) => `<div class="task-card"><b>${row.nickname || row.account}</b><small>${row.status} · ${row.next_follow_at || '-'}</small></div>`).join('') || '<div class="empty">暂无任务</div>'}</div></article>`).join('');
}

$('csvFile').addEventListener('change', () => { $('fileName').textContent = $('csvFile').files[0]?.name || '未选择文件'; });
$('importBtn').addEventListener('click', async () => {
  const file = $('csvFile').files[0];
  if (!file) { toast('请先选择 CSV 文件'); return; }
  const csvText = await file.text();
  try {
    const result = await api('/api/crm/import-csv', { method: 'POST', body: JSON.stringify({ csv_text: csvText }) });
    toast(`导入完成：新增 ${result.created}，跳过 ${result.skipped}`);
    await Promise.all([loadDashboard(), loadInfluencers()]);
  } catch (error) { toast(error.message); }
});

setLoggedIn(Boolean(state.token && state.user));
if (state.token && state.user) bootstrap();
