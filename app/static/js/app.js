/* ==========================================================
   每日 AI 新闻助手 · 前端逻辑（第四阶段）
   技术栈：原生 JS（无框架、无构建），fetch 调 RESTful 接口

   页面结构（hash 路由，四个视图共用一份 HTML）：
     #/                    概览：用户信息 / 订阅偏好 / 最近简报 / 最近推送 / 立即生成
     #/briefings           历史简报列表（日期 / 标题 / 条数 / 生成状态 / 推送状态）
     #/briefing/:id        简报详情（Markdown 渲染 + 推送 / 重新生成）
     #/settings            订阅配置（第一阶段的表单）

   三个关键交互：
   1. 手动生成是**异步**的：接口立刻返回任务，前端轮询
      `GET /tasks/{id}` 显示「等待执行 → 正在生成 → 成功 / 失败」。
      任务 ID 写进 localStorage，刷新页面后仍能继续轮询。
   2. 防重复点击：按钮在请求期间与轮询期间都禁用；后端还有数据库唯一键兜底。
   3. Markdown 渲染一律「先转义再插标签」，因为简报正文来自大模型。
   ========================================================== */

(function () {
  'use strict';

  // ---------------------------------------------------------- 常量
  const API_BASE = '/api/v1';
  const AGENT_API = '/api/agent';
  const STORAGE_KEY = 'news_agent_user_id';
  const ACTIVE_TASK_KEY = 'news_agent_active_task';
  const PAGE_SIZE = 10;
  const POLL_INTERVAL = 2000;        // 轮询间隔（毫秒）
  const POLL_TIMEOUT = 15 * 60 * 1000; // 超过 15 分钟视为异常，停止轮询

  const LIMITS = { topics: 20, keywords: 50, exclude_keywords: 50, termLength: 30 };

  const PRESET_TOPICS = [
    '大模型 / LLM', 'AI Agent', '多模态', '具身智能', 'AI 芯片与算力',
    '开源模型', 'RAG / 知识库', 'AI 编程工具', 'AI 合规与监管', '模型评测',
  ];

  // 状态 → 展示文案与配色
  const TASK_STATUS = {
    pending: { text: '等待执行', cls: 'is-wait' },
    running: { text: '正在生成', cls: 'is-run' },
    success: { text: '生成成功', cls: 'is-ok' },
    failed: { text: '生成失败', cls: 'is-err' },
    skipped: { text: '已跳过', cls: 'is-skip' },
  };
  const PUSH_STATUS = {
    pending: { text: '待推送', cls: 'is-wait' },
    success: { text: '已发送', cls: 'is-ok' },
    failed: { text: '推送失败', cls: 'is-err' },
    skipped: { text: '未推送', cls: 'is-skip' },
    disabled: { text: '已关闭', cls: 'is-skip' },
  };
  const TRIGGER_LABEL = { manual: '手动', scheduled: '定时', catch_up: '补跑', retry: '重试' };

  // ---------------------------------------------------------- DOM
  const $ = (id) => document.getElementById(id);

  const views = {
    dashboard: $('view-dashboard'),
    history: $('view-history'),
    detail: $('view-detail'),
    settings: $('view-settings'),
  };

  const toastEl = $('toast');
  const userChip = $('userChip');
  const userChipText = $('userChipText');

  // ---------------------------------------------------------- 状态
  const state = {
    userId: null,
    dirty: false,
    saving: false,
    route: 'dashboard',
    dashboard: null,
    history: { page: 1, total: 0, items: [] },
    detailBriefingId: null,
    pollTaskId: null,
    pollTimer: null,
    pollStartedAt: 0,
    generating: false,
  };

  // ==========================================================
  //  基础工具
  // ==========================================================

  /** 统一请求封装：解析 {code, message, data} 信封，失败抛可读错误 */
  async function request(path, options = {}) {
    const { method = 'GET', body, base = API_BASE } = options;
    let response;
    try {
      response = await fetch(base + path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (error) {
      throw new Error('无法连接后端服务，请确认服务已启动');
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    if (!response.ok || (payload && payload.code !== 0)) {
      const error = new Error((payload && payload.message) || `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return payload ? payload.data : null;
  }

  let toastTimer = null;
  function toast(message, type = 'info') {
    toastEl.textContent = message;
    toastEl.className =
      'toast is-show' + (type === 'error' ? ' is-error' : type === 'success' ? ' is-success' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.className = 'toast'; }, 2600);
  }

  function escapeHtml(text) {
    return String(text).replace(
      /[&<>"']/g,
      (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])
    );
  }

  /** ISO / datetime -> "YYYY-MM-DD HH:mm"（不做时区换算，后端返回的已是本地时间） */
  function formatTime(value) {
    if (!value) return '—';
    const text = String(value).replace('T', ' ').replace(/\.\d+/, '');
    return text.slice(0, 16);
  }

  function formatDate(value) {
    if (!value) return '—';
    return String(value).slice(0, 10);
  }

  function formatDuration(ms) {
    if (!ms || ms < 0) return '—';
    const seconds = Math.round(ms / 1000);
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
  }

  function badge(status, table) {
    const meta = table[status] || { text: status || '未知', cls: 'is-skip' };
    return `<span class="badge ${meta.cls}">${escapeHtml(meta.text)}</span>`;
  }

  function todayISO() {
    // 不能用 toISOString()（UTC），晚上 8 点后会把「今天」算成第二天
    const now = new Date();
    const pad = (value) => String(value).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }

  /** 从 Markdown 中取一行 summar 用作列表预览（不渲染 HTML） */
  function firstParagraph(markdown, limit = 90) {
    const text = String(markdown || '')
      .replace(/^#.*$/gm, '')
      .replace(/^[-*]\s+/gm, '')
      .replace(/[*`#>]/g, '')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .join(' ');
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  }

  // ==========================================================
  //  Markdown 渲染（只覆盖简报用到的语法）
  // ==========================================================

  function renderInline(text) {
    const links = [];
    let out = escapeHtml(text);

    // Markdown 链接先换成占位符，避免裸链接规则二次包裹
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (match, label, url) => {
      links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      return `\u0001${links.length - 1}\u0001`;
    });

    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(
      /(^|[\s\u3000（(【\[：:，,、；;])(https?:\/\/[^\s<>"'）)】\]，。；、]+)/g,
      (match, lead, url) => `${lead}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
    );

    return out.replace(/\u0001(\d+)\u0001/g, (match, index) => links[Number(index)] || '');
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let listTag = null;
    let quoteLines = [];

    const closeList = () => {
      if (listTag) { html.push(`</${listTag}>`); listTag = null; }
    };
    const flushQuote = () => {
      if (quoteLines.length) {
        html.push(`<blockquote><p>${quoteLines.map(renderInline).join('<br />')}</p></blockquote>`);
        quoteLines = [];
      }
    };

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      if (!line.trim()) { closeList(); flushQuote(); continue; }

      if (line.startsWith('> ')) { closeList(); quoteLines.push(line.slice(2)); continue; }
      flushQuote();

      const heading = /^(#{1,3})\s+(.*)$/.exec(line);
      if (heading) {
        closeList();
        const level = heading[1].length;
        html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        continue;
      }

      if (/^(-{3,}|\*{3,})$/.test(line.trim())) { closeList(); html.push('<hr />'); continue; }

      const ordered = /^(\d+)\.\s+(.*)$/.exec(line);
      const unordered = /^[-*+]\s+(.*)$/.exec(line);
      if (ordered || unordered) {
        const tag = ordered ? 'ol' : 'ul';
        if (listTag !== tag) { closeList(); html.push(`<${tag}>`); listTag = tag; }
        html.push(`<li>${renderInline(ordered ? ordered[2] : unordered[1])}</li>`);
        continue;
      }

      closeList();
      html.push(`<p>${renderInline(line)}</p>`);
    }

    closeList();
    flushQuote();
    return html.join('\n');
  }

  // ==========================================================
  //  路由
  // ==========================================================

  function parseRoute() {
    const raw = (window.location.hash || '#/').replace(/^#/, '');
    const parts = raw.split('/').filter(Boolean);
    if (!parts.length) return { name: 'dashboard' };
    if (parts[0] === 'briefings') return { name: 'history' };
    if (parts[0] === 'briefing' && parts[1]) return { name: 'detail', id: Number(parts[1]) };
    if (parts[0] === 'settings') return { name: 'settings' };
    return { name: 'dashboard' };
  }

  function setActiveTab(name) {
    document.querySelectorAll('#mainNav .tab').forEach((tab) => {
      tab.classList.toggle('is-active', tab.dataset.route === name);
    });
  }

  async function handleRoute() {
    const route = parseRoute();
    state.route = route.name;

    Object.entries(views).forEach(([key, el]) => {
      el.hidden = key !== route.name;
    });
    setActiveTab(route.name);

    if (route.name === 'dashboard') await loadDashboard();
    else if (route.name === 'history') await loadHistory(1);
    else if (route.name === 'detail') await loadDetail(route.id);
    else if (route.name === 'settings') await loadSettings();
  }

  // ==========================================================
  //  用户与状态展示
  // ==========================================================

  function updateChip() {
    if (state.userId) {
      userChip.classList.add('is-active');
      userChipText.textContent = `已关联用户 #${state.userId}`;
    } else {
      userChip.classList.remove('is-active');
      userChipText.textContent = '尚未创建配置';
    }
  }

  function requireUser() {
    if (state.userId) return true;
    toast('请先在「订阅配置」中保存用户信息', 'error');
    window.location.hash = '#/settings';
    return false;
  }

  // ==========================================================
  //  视图一：概览
  // ==========================================================

  async function loadDashboard() {
    if (!state.userId) {
      renderEmptyDashboard();
      return;
    }
    try {
      const data = await request(`/users/${state.userId}/dashboard`);
      state.dashboard = data;
      renderUser(data.user);
      renderPreferences(data.preferences);
      renderLatestBriefing(data.latest_briefing);
      renderLatestPush(data.latest_push);
      renderPushSwitch(data.user);
      renderScheduler(data.scheduler);
      renderGenState(data);
    } catch (error) {
      if (error.status === 404) {
        // 用户被删除 / 换了数据库：解除本地关联
        localStorage.removeItem(STORAGE_KEY);
        localStorage.removeItem(ACTIVE_TASK_KEY);
        state.userId = null;
        updateChip();
        renderEmptyDashboard();
        toast('本地关联的用户已不存在，请重新保存配置', 'error');
        window.location.hash = '#/settings';
        return;
      }
      toast(error.message, 'error');
    }
  }

  function renderEmptyDashboard() {
    $('dashNickname').textContent = '—';
    $('dashIdentity').textContent = '—';
    $('dashEmail').textContent = '—';
    $('dashUserId').textContent = '—';
    $('prefSummary').innerHTML = '<p class="empty">还没有保存订阅偏好</p>';
    $('prefUpdatedAt').textContent = '—';
    $('latestBriefing').innerHTML = '<p class="empty">还没有生成过简报</p>';
    $('latestPush').innerHTML = '<p class="empty">还没有推送记录</p>';
    $('schedStats').innerHTML = '';
    $('genState').textContent = '尚未创建用户';
    $('genSub').textContent = '请先保存订阅配置';
    $('genBtn').disabled = true;
    $('genBtn').querySelector('.btn-label').textContent = '立即生成简报';
    $('phaseSteps').hidden = true;
    $('genProgress').hidden = true;
    $('genResult').hidden = true;
  }

  function renderUser(user) {
    $('dashNickname').textContent = user.nickname || '—';
    $('dashIdentity').textContent = user.identity || '—';
    $('dashEmail').textContent = user.email || '未填写';
    $('dashUserId').textContent = `#${user.id}`;
  }

  function renderPreferences(pref) {
    if (!pref) {
      $('prefSummary').innerHTML =
        '<p class="empty">还没有设置订阅偏好。<a href="#/settings">去设置 →</a></p>';
      $('prefUpdatedAt').textContent = '—';
      return;
    }
    $('prefUpdatedAt').textContent = `最后更新 ${formatTime(pref.updated_at)}`;

    const group = (label, values, kind) => {
      if (!values || !values.length) {
        return `<div class="pref-group"><span class="pref-label">${label}</span>
          <span class="pref-empty">未设置</span></div>`;
      }
      const chips = values
        .map((value) => `<span class="chip chip--${kind}">${escapeHtml(value)}</span>`)
        .join('');
      return `<div class="pref-group"><span class="pref-label">${label}</span>
        <span class="chips">${chips}</span></div>`;
    };

    $('prefSummary').innerHTML = [
      group('关注领域', pref.topics, 'topic'),
      group('关键词', pref.keywords, 'keyword'),
      group('排除词', pref.exclude_keywords, 'exclude'),
    ].join('');
  }

  function renderLatestBriefing(briefing) {
    if (!briefing) {
      $('latestBriefing').innerHTML = '<p class="empty">还没有生成过简报</p>';
      return;
    }
    $('latestBriefing').innerHTML = `
      <div class="latest-title">${escapeHtml(briefing.title)}</div>
      <dl class="kv kv--tight">
        <dt>简报日期</dt><dd>${formatDate(briefing.briefing_date)}</dd>
        <dt>生成时间</dt><dd>${formatTime(briefing.generated_at)}</dd>
        <dt>新闻条数</dt><dd>${briefing.news_count} 条</dd>
      </dl>
      <div class="card-actions">
        <a class="btn btn-ghost" href="#/briefing/${briefing.id}">查看完整简报 →</a>
      </div>`;
  }

  function renderLatestPush(push) {
    if (!push) {
      $('latestPush').innerHTML = '<p class="empty">还没有推送记录</p>';
      return;
    }
    const rows = [
      ['状态', badge(push.status, PUSH_STATUS)],
      ['收件人', escapeHtml(push.target || '—')],
      ['发送时间', formatTime(push.sent_at)],
      ['尝试次数', `${push.attempts} 次`],
    ];
    if (push.status === 'failed' && push.last_error) {
      rows.push(['失败原因', `<span class="text-err">${escapeHtml(push.last_error)}</span>`]);
    }
    if (push.next_retry_at && push.status === 'failed') {
      rows.push(['下次重试', formatTime(push.next_retry_at)]);
    }

    const list = rows
      .map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`)
      .join('');
    const retryBtn = push.status === 'failed'
      ? `<div class="card-actions"><button type="button" class="btn btn-ghost" data-retry-push="${push.id}">重试推送</button></div>`
      : '';
    $('latestPush').innerHTML = `<dl class="kv kv--tight">${list}</dl>${retryBtn}`;

    const btn = $('latestPush').querySelector('[data-retry-push]');
    if (btn) btn.addEventListener('click', () => retryPush(push.id, btn));
  }

  function renderPushSwitch(user) {
    const toggle = $('pushToggle');
    toggle.checked = Boolean(user.push_enabled);
    $('pushToggleLabel').textContent = user.push_enabled ? '接收邮件推送' : '已关闭推送';

    const sched = state.dashboard && state.dashboard.scheduler;
    let hint = '';
    if (!user.email) hint = '未填写接收邮箱，打开开关也不会发信';
    else if (sched && sched.enabled === false) hint = '服务端已关闭定时任务，仅支持手动生成';
    else hint = `简报生成后发送到 ${user.email}`;
    $('pushHint').textContent = hint;
  }

  function renderScheduler(scheduler) {
    if (!scheduler) {
      $('schedStats').innerHTML = '<li>调度器状态不可用</li>';
      $('footerSched').textContent = '—';
      return;
    }
    const items = [
      ['状态', scheduler.running ? '运行中' : (scheduler.enabled ? '未启动' : '已关闭')],
      ['每天执行', `${scheduler.time}（${scheduler.timezone}）`],
      ['下次执行', scheduler.next_run_at ? formatTime(scheduler.next_run_at) : '—'],
      ['启动补跑', scheduler.catch_up ? '已开启' : '已关闭'],
      ['并发上限', `${scheduler.max_concurrency} 个用户`],
    ];
    $('schedStats').innerHTML = items
      .map(([label, value]) => `<li>${label} <b>${escapeHtml(value)}</b></li>`)
      .join('');
    $('footerSched').textContent = `每日 ${scheduler.time} 自动生成 · ${scheduler.timezone}`;
  }

  /** 根据概览数据决定「生成」区域展示什么状态 */
  function renderGenState(data) {
    const task = data.running_task || data.latest_task;
    const btn = $('genBtn');

    if (data.running_task) {
      // 服务端还有任务在跑（可能是刷新页面 / 换了设备）：直接恢复轮询
      setGenerating(true);
      showPhase(data.running_task.status);
      $('genState').textContent = '正在生成…';
      $('genSub').textContent = `任务 #${data.running_task.id} · ${TRIGGER_LABEL[data.running_task.trigger_type] || ''}触发`;
      $('genProgress').hidden = false;
      startPolling(data.running_task.id);
      return;
    }

    if (!task) {
      $('genState').textContent = '今日尚未生成';
      $('genSub').textContent = '点击右侧按钮，Agent 会现场检索并撰写';
      btn.disabled = !state.userId;
      btn.querySelector('.btn-label').textContent = '立即生成简报';
      $('phaseSteps').hidden = true;
      $('genProgress').hidden = true;
      $('genResult').hidden = true;
      return;
    }

    const isToday = task.briefing_date === todayISO();
    const statusMeta = TASK_STATUS[task.status] || { text: task.status };
    $('genState').textContent = isToday
      ? `今日简报：${statusMeta.text}`
      : `${task.briefing_date}：${statusMeta.text}`;
    $('genSub').textContent = buildTaskSub(task);
    showPhase(task.status);
    $('genProgress').hidden = true;
    renderTaskResult(task);

    btn.disabled = !state.userId;
    btn.querySelector('.btn-label').textContent =
      task.status === 'success' ? '重新生成' : '立即生成简报';
  }

  function buildTaskSub(task) {
    const parts = [`任务 #${task.id}`];
    if (task.trigger_type) parts.push(`${TRIGGER_LABEL[task.trigger_type] || task.trigger_type}触发`);
    if (task.status === 'success' && task.briefing) parts.push(`${task.briefing.news_count} 条新闻`);
    if (task.finished_at) parts.push(`完成于 ${formatTime(task.finished_at)}`);
    return parts.join(' · ');
  }

  function showPhase(status) {
    const order = { pending: 1, running: 2, success: 3, failed: 3, skipped: 3 };
    const current = order[status] || 0;
    $('phaseSteps').hidden = false;
    $('phaseSteps').querySelectorAll('.phase').forEach((li) => {
      const phase = li.dataset.phase;
      const index = order[phase] || 0;
      li.classList.toggle('is-active', phase === 'running' ? status === 'running' : false);
      li.classList.toggle('is-done', index < current || (phase === 'done' && current === 3));
      li.classList.toggle('is-fail', phase === 'done' && (status === 'failed' || status === 'skipped'));
    });
  }

  function renderTaskResult(task) {
    const box = $('genResult');
    if (task.status === 'success' && task.briefing) {
      box.className = 'gen-result is-ok';
      box.innerHTML = `
        <strong>生成成功</strong>：${escapeHtml(task.briefing.title)}（${task.briefing.news_count} 条）
        <a href="#/briefing/${task.briefing.id}">查看简报 →</a>`;
      box.hidden = false;
      return;
    }
    if (task.status === 'failed' || task.status === 'skipped') {
      box.className = 'gen-result is-err';
      const reason = task.error_message || (task.warnings || []).join('；') || '未知原因';
      const retry = task.status === 'failed'
        ? ` <button type="button" class="link-btn" data-retry-task="${task.id}">重试</button>`
        : '';
      box.innerHTML = `<strong>${task.status === 'failed' ? '生成失败' : '已跳过'}</strong>：${escapeHtml(reason)}${retry}`;
      box.hidden = false;
      const btn = box.querySelector('[data-retry-task]');
      if (btn) btn.addEventListener('click', () => retryTask(task.id, btn));
      return;
    }
    box.hidden = true;
  }

  // ==========================================================
  //  手动生成：触发 + 轮询
  // ==========================================================

  function setGenerating(generating) {
    state.generating = generating;
    const btn = $('genBtn');
    btn.classList.toggle('is-loading', generating);
    if (generating) {
      btn.disabled = true;
      btn.querySelector('.btn-label').textContent = '生成中…';
    }
  }

  async function generateBriefing(force = false) {
    if (!requireUser()) return;
    if (state.generating) { toast('任务正在执行，请稍候'); return; }

    setGenerating(true);
    $('genResult').hidden = true;
    $('genState').textContent = '正在提交…';
    $('genSub').textContent = '';

    try {
      const data = await request(`/users/${state.userId}/briefings/trigger`, {
        method: 'POST',
        body: { force },
      });
      const task = data.task;

      if (data.action === 'already_generated') {
        setGenerating(false);
        toast('今日简报已生成，如需覆盖请用「重新生成」', 'info');
        await loadDashboard();
        return;
      }

      // created / resumed / regenerated / already_running 都要进入轮询
      $('genState').textContent = data.action === 'already_running' ? '已有任务在生成中' : '任务已提交';
      $('genSub').textContent = `任务 #${task.id}`;
      showPhase(task.status);
      startPolling(task.id);
    } catch (error) {
      setGenerating(false);
      $('genState').textContent = '提交失败';
      $('genSub').textContent = error.message;
      toast(error.message, 'error');
    }
  }

  function startPolling(taskId) {
    if (state.pollTaskId === taskId && state.pollTimer) return;
    stopPolling();

    state.pollTaskId = taskId;
    state.pollStartedAt = Date.now();
    localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify({ taskId, startedAt: state.pollStartedAt }));
    $('genProgress').hidden = false;

    state.pollTimer = setInterval(pollTick, POLL_INTERVAL);
    pollTick();
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
    state.pollTaskId = null;
    localStorage.removeItem(ACTIVE_TASK_KEY);
    $('genProgress').hidden = true;
  }

  async function pollTick() {
    if (!state.pollTaskId) return;

    if (Date.now() - state.pollStartedAt > POLL_TIMEOUT) {
      stopPolling();
      setGenerating(false);
      $('genState').textContent = '任务执行超时';
      $('genSub').textContent = '请稍后刷新查看结果（任务可能仍在后台执行）';
      return;
    }

    try {
      const data = await request(`/tasks/${state.pollTaskId}`);
      const task = data.task;
      const elapsed = formatDuration(Date.now() - state.pollStartedAt);

      if (task.status === 'pending') {
        $('genState').textContent = '等待执行…';
        $('progressText').textContent = `已排队 ${elapsed}，worker 空闲后立即开始`;
      } else if (task.status === 'running') {
        $('genState').textContent = '正在生成…';
        $('progressText').textContent =
          `Agent 正在自主检索与撰写（搜索 → 读原文 → 写作），已用 ${elapsed}`;
      }

      showPhase(task.status);

      if (['success', 'failed', 'skipped'].includes(task.status)) {
        stopPolling();
        setGenerating(false);
        $('genState').textContent = task.status === 'success' ? '生成成功' : '生成结束';
        $('genSub').textContent = buildTaskSub(task);
        renderTaskResult(task);
        if (task.status === 'success') {
          toast(`简报生成成功（${task.briefing ? task.briefing.news_count : 0} 条新闻）`, 'success');
        } else {
          toast(task.error_message || '本次未生成简报', 'error');
        }
        await loadDashboard();
      }
    } catch (error) {
      stopPolling();
      setGenerating(false);
      $('genState').textContent = '查询状态失败';
      $('genSub').textContent = error.message;
      toast(error.message, 'error');
    }
  }

  /** 页面加载时恢复未完成的轮询（刷新 / 换标签页都不丢进度） */
  async function restoreActiveTask() {
    const raw = localStorage.getItem(ACTIVE_TASK_KEY);
    if (!raw) return false;
    let payload = null;
    try { payload = JSON.parse(raw); } catch (error) { payload = null; }
    if (!payload || !payload.taskId) { localStorage.removeItem(ACTIVE_TASK_KEY); return false; }

    try {
      const data = await request(`/tasks/${payload.taskId}`);
      if (['pending', 'running'].includes(data.task.status)) {
        state.pollStartedAt = payload.startedAt || Date.now();
        setGenerating(true);
        showPhase(data.task.status);
        startPolling(payload.taskId);
        return true;
      }
    } catch (error) {
      // 任务不存在（换了数据库）→ 忽略
    }
    localStorage.removeItem(ACTIVE_TASK_KEY);
    return false;
  }

  async function retryTask(taskId, button) {
    if (button) button.disabled = true;
    try {
      const data = await request(`/tasks/${taskId}/retry`, { method: 'POST' });
      toast(data.action === 'already_generated' ? '该任务已成功，无需重试' : '已重新提交执行', 'success');
      setGenerating(true);
      showPhase('pending');
      startPolling(data.task.id);
    } catch (error) {
      toast(error.message, 'error');
      if (button) button.disabled = false;
    }
  }

  async function retryPush(pushId, button) {
    if (button) button.disabled = true;
    button && (button.textContent = '重试中…');
    try {
      const record = await request(`/push-records/${pushId}/retry`, { method: 'POST' });
      toast(
        record.status === 'success' ? '邮件已重新发送' : (record.last_error || '推送仍未成功'),
        record.status === 'success' ? 'success' : 'error'
      );
      await loadDashboard();
    } catch (error) {
      toast(error.message, 'error');
      if (button) { button.disabled = false; button.textContent = '重试推送'; }
    }
  }

  // ==========================================================
  //  视图二：历史简报列表
  // ==========================================================

  async function loadHistory(page = 1) {
    if (!requireUser()) return;
    state.history.page = page;

    try {
      const data = await request(
        `/tasks?user_id=${state.userId}&page=${page}&page_size=${PAGE_SIZE}`
      );
      state.history.total = data.total;
      state.history.items = data.items || [];
      renderHistory();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function renderHistory() {
    const body = $('historyBody');
    const items = state.history.items;
    const empty = $('historyEmpty');
    const table = $('historyTable');

    if (!items.length) {
      body.innerHTML = '';
      table.hidden = true;
      empty.hidden = false;
      $('historyPager').hidden = true;
      return;
    }

    table.hidden = false;
    empty.hidden = true;

    body.innerHTML = items
      .map((item) => {
        const briefing = item.briefing;
        const title = briefing ? escapeHtml(briefing.title) : '<span class="text-mute">—</span>';
        const count = briefing ? `${briefing.news_count} 条` : '—';
        const canView = Boolean(briefing);
        const reason = item.error_message
          ? `<div class="row-note" title="${escapeHtml(item.error_message)}">${escapeHtml(
              item.error_message.slice(0, 46)
            )}${item.error_message.length > 46 ? '…' : ''}</div>`
          : '';

        const actions = [];
        if (canView) actions.push(`<a class="link-btn" href="#/briefing/${briefing.id}">查看</a>`);
        if (item.status === 'failed') {
          actions.push(`<button type="button" class="link-btn" data-retry-task="${item.id}">重试</button>`);
        }

        return `<div class="table-row">
          <span class="col-date">${formatDate(item.briefing_date)}</span>
          <span class="col-title">${title}${reason}</span>
          <span class="col-count ta-c">${count}</span>
          <span class="col-status ta-c">${badge(item.status, TASK_STATUS)}</span>
          <span class="col-push ta-c">${item.push ? badge(item.push.status, PUSH_STATUS) : '<span class="text-mute">—</span>'}</span>
          <span class="col-action ta-r">${actions.join('')}</span>
        </div>`;
      })
      .join('');

    body.querySelectorAll('[data-retry-task]').forEach((btn) => {
      btn.addEventListener('click', () => retryTask(Number(btn.dataset.retryTask), btn));
    });

    // 分页
    const totalPages = Math.max(1, Math.ceil(state.history.total / PAGE_SIZE));
    $('historyPager').hidden = totalPages <= 1;
    $('pagerText').textContent = `第 ${state.history.page} / ${totalPages} 页 · 共 ${state.history.total} 条`;
    $('prevPage').disabled = state.history.page <= 1;
    $('nextPage').disabled = state.history.page >= totalPages;
  }

  // ==========================================================
  //  视图三：简报详情
  // ==========================================================

  async function loadDetail(briefingId) {
    state.detailBriefingId = briefingId;
    $('detailBody').innerHTML = '<p class="empty">加载中…</p>';
    $('detailMeta').innerHTML = '';

    try {
      const briefing = await request(`/briefings/${briefingId}`);
      $('detailMeta').innerHTML = `
        <h2 class="detail-title">${escapeHtml(briefing.title)}</h2>
        <ul class="stat-row">
          <li>简报日期 <b>${formatDate(briefing.briefing_date)}</b></li>
          <li>新闻条数 <b>${briefing.news_count} 条</b></li>
          <li>生成时间 <b>${formatTime(briefing.generated_at)}</b></li>
          <li>关注方向 <b>${escapeHtml((briefing.focus_topics || []).join('、') || '—')}</b></li>
          ${briefing.run_id ? `<li>运行 ID <b>${escapeHtml(briefing.run_id)}</b></li>` : ''}
        </ul>
        <div id="detailPushRow" class="detail-push">推送状态：<span class="text-mute">读取中…</span></div>`;

      $('detailBody').innerHTML = renderMarkdown(briefing.content || '');
      $('detailBody').hidden = false;

      // 推送状态与「重新推送」按钮（失败时才出现）
      renderDetailPush(briefing);
    } catch (error) {
      $('detailBody').innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
      $('pushBtn').hidden = true;
    }
  }

  async function renderDetailPush(briefing) {
    const row = $('detailPushRow');
    if (!row) return;
    if (!state.userId) return;

    try {
      const data = await request(`/users/${state.userId}/push-records?page=1&page_size=50`);
      const record = (data.items || []).find((item) => item.briefing_id === briefing.id);
      if (!record) {
        row.innerHTML = '推送状态：<span class="badge is-skip">无推送记录</span>';
        $('pushBtn').hidden = true;
        return;
      }
      const extra = record.status === 'failed' && record.last_error
        ? `<span class="text-err">（${escapeHtml(record.last_error)}）</span>`
        : '';
      row.innerHTML = `推送状态：${badge(record.status, PUSH_STATUS)}
        <span class="text-mute">尝试 ${record.attempts} 次${record.sent_at ? ` · 发送于 ${formatTime(record.sent_at)}` : ''}</span>${extra}`;

      if (record.status === 'failed') {
        $('pushBtn').hidden = false;
        $('pushBtn').dataset.pushId = String(record.id);
      } else {
        $('pushBtn').hidden = true;
      }
    } catch (error) {
      row.innerHTML = `推送状态：<span class="text-mute">读取失败</span>`;
    }
  }

  // ==========================================================
  //  视图四：订阅配置
  // ==========================================================

  class TagField {
    constructor(key, limit, onChange) {
      this.key = key;
      this.limit = limit;
      this.onChange = onChange || function () {};
      this.wrapper = document.querySelector(`.tag-field[data-field="${key}"]`);
      this.list = this.wrapper.querySelector('.tag-list');
      this.input = this.wrapper.querySelector('input[type="text"]');
      this.values = [];
      this._bind();
    }

    _bind() {
      this.wrapper.addEventListener('click', (event) => {
        if (event.target === this.wrapper || event.target === this.list) this.input.focus();
      });

      this.input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
          event.preventDefault();
          this.commitInput();
        } else if (event.key === 'Backspace' && this.input.value === '' && this.values.length) {
          this.remove(this.values.length - 1);
        }
      });

      this.input.addEventListener('blur', () => this.commitInput());
    }

    commitInput() {
      const raw = this.input.value;
      if (!raw || !raw.trim()) return;
      this.addMany(raw);
      this.input.value = '';
    }

    addMany(raw) {
      const parts = String(raw)
        .split(/[,，;；、\n|]/)
        .map((item) => item.trim())
        .filter(Boolean);

      let added = false;
      for (const part of parts) {
        if (this.values.length >= this.limit) {
          toast(`${this.label}最多添加 ${this.limit} 个`, 'error');
          break;
        }
        const value = part.slice(0, LIMITS.termLength);
        if (this.values.some((item) => item.toLowerCase() === value.toLowerCase())) continue;
        this.values.push(value);
        added = true;
      }
      if (added) { this.render(); this.onChange(); }
    }

    remove(index) {
      if (index < 0 || index >= this.values.length) return;
      this.values.splice(index, 1);
      this.render();
      this.onChange();
    }

    set(values) {
      this.values = Array.isArray(values)
        ? values.filter((v) => typeof v === 'string' && v.trim()).map((v) => v.trim())
        : [];
      this.render();
    }

    get() { return this.values.slice(); }

    get label() {
      return { topics: '关注领域', keywords: '关键词', exclude_keywords: '排除关键词' }[this.key] || '标签';
    }

    render() {
      this.list.innerHTML = '';
      this.values.forEach((value, index) => {
        const li = document.createElement('li');
        li.className = 'tag';

        const text = document.createElement('span');
        text.className = 'tag-text';
        text.textContent = value; // textContent 天然防 XSS
        text.title = value;

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'tag-remove';
        button.textContent = '×';
        button.setAttribute('aria-label', `删除 ${value}`);
        button.addEventListener('click', (event) => {
          event.stopPropagation();
          this.remove(index);
        });

        li.append(text, button);
        this.list.appendChild(li);
      });
      this.syncPresets();
    }

    syncPresets() {
      if (!this.presets) return;
      const lower = this.values.map((v) => v.toLowerCase());
      this.presets.forEach(({ el, value }) => {
        el.disabled = lower.includes(value.toLowerCase()) || this.values.length >= this.limit;
      });
    }
  }

  const markDirty = () => {
    if (!state.dirty) {
      state.dirty = true;
      setStatus('有未保存的修改', 'is-dirty');
    }
  };

  const topicsField = new TagField('topics', LIMITS.topics, markDirty);
  const keywordsField = new TagField('keywords', LIMITS.keywords, markDirty);
  const excludeField = new TagField('exclude_keywords', LIMITS.exclude_keywords, markDirty);

  function renderPresets() {
    const row = $('presetRow');
    PRESET_TOPICS.forEach((value) => {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'preset';
      el.textContent = value;
      el.addEventListener('click', () => {
        topicsField.addMany(value);
        topicsField.input.focus();
      });
      row.appendChild(el);
    });
    topicsField.presets = Array.from(row.querySelectorAll('.preset')).map((el) => ({
      el,
      value: el.textContent,
    }));
    topicsField.syncPresets();
  }

  function setStatus(text, className) {
    const el = $('saveStatus');
    el.textContent = text;
    el.className = 'status' + (className ? ` ${className}` : '');
  }

  function setSaving(saving) {
    state.saving = saving;
    const btn = $('saveBtn');
    btn.disabled = saving;
    btn.classList.toggle('is-loading', saving);
    btn.querySelector('.btn-label').textContent = saving ? '保存中…' : '保存订阅';
  }

  function setFieldError(input, message) {
    input.classList.add('is-invalid');
    const hint = input.parentElement.querySelector('.hint');
    if (hint) {
      if (hint.dataset.original === undefined) hint.dataset.original = hint.textContent;
      hint.textContent = message;
      hint.classList.add('is-error');
    }
  }

  function clearFieldError(input) {
    input.classList.remove('is-invalid');
    const hint = input.parentElement.querySelector('.hint');
    if (hint && hint.dataset.original !== undefined) {
      hint.textContent = hint.dataset.original;
      hint.classList.remove('is-error');
    }
  }

  function isValidEmail(value) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value);
  }

  function fillProfile(user) {
    $('nickname').value = user.nickname || '';
    $('identity').value = user.identity || '';
    $('email').value = user.email || '';
  }

  function fillPreferences(pref) {
    topicsField.set(pref ? pref.topics || [] : []);
    keywordsField.set(pref ? pref.keywords || [] : []);
    excludeField.set(pref ? pref.exclude_keywords || [] : []);
  }

  async function loadSettings() {
    if (!state.userId) {
      setStatus('信息尚未保存', '');
      return;
    }
    try {
      const [user, preference] = await Promise.all([
        request(`/users/${state.userId}`),
        request(`/users/${state.userId}/preferences`),
      ]);
      fillProfile(user);
      fillPreferences(preference);
      state.dirty = false;
      setStatus(
        preference ? `已保存 · 最后更新 ${formatTime(preference.updated_at)}` : '已加载用户，尚未设置偏好',
        preference ? 'is-saved' : ''
      );
    } catch (error) {
      if (error.status === 404) {
        localStorage.removeItem(STORAGE_KEY);
        state.userId = null;
        updateChip();
        setStatus('本地关联的用户已不存在，请重新保存', '');
        return;
      }
      toast(error.message, 'error');
    }
  }

  function readProfile() {
    topicsField.commitInput();
    keywordsField.commitInput();
    excludeField.commitInput();

    let firstInvalid = null;

    const nickname = $('nickname').value.trim();
    if (!nickname) { setFieldError($('nickname'), '请填写昵称'); firstInvalid = firstInvalid || $('nickname'); }
    else if (nickname.length > 50) { setFieldError($('nickname'), '昵称不能超过 50 个字符'); firstInvalid = firstInvalid || $('nickname'); }
    else clearFieldError($('nickname'));

    const identity = $('identity').value.trim();
    if (!identity) { setFieldError($('identity'), '请填写身份或职业描述'); firstInvalid = firstInvalid || $('identity'); }
    else if (identity.length > 200) { setFieldError($('identity'), '描述不能超过 200 个字符'); firstInvalid = firstInvalid || $('identity'); }
    else clearFieldError($('identity'));

    const email = $('email').value.trim();
    if (email && !isValidEmail(email)) { setFieldError($('email'), '邮箱格式不正确'); firstInvalid = firstInvalid || $('email'); }
    else clearFieldError($('email'));

    if (firstInvalid) {
      firstInvalid.focus();
      toast('请检查表单中标红的必填项', 'error');
      return null;
    }

    return {
      nickname,
      identity,
      email: email || null,
      topics: topicsField.get(),
      keywords: keywordsField.get(),
      exclude_keywords: excludeField.get(),
    };
  }

  async function saveProfile(profile) {
    const body = { nickname: profile.nickname, identity: profile.identity, email: profile.email };
    if (state.userId) {
      try {
        return await request(`/users/${state.userId}`, { method: 'PUT', body });
      } catch (error) {
        if (error.status === 404) return request('/users', { method: 'POST', body });
        throw error;
      }
    }
    return request('/users', { method: 'POST', body });
  }

  async function handleSave() {
    if (state.saving) return;
    const profile = readProfile();
    if (!profile) return;

    setSaving(true);
    try {
      const user = await saveProfile(profile);
      const preference = await request(`/users/${user.id}/preferences`, {
        method: 'PUT',
        body: {
          topics: profile.topics,
          keywords: profile.keywords,
          exclude_keywords: profile.exclude_keywords,
        },
      });

      state.userId = user.id;
      state.dirty = false;
      localStorage.setItem(STORAGE_KEY, String(user.id));
      updateChip();
      setStatus(`已保存 · 最后更新 ${formatTime(preference.updated_at)}`, 'is-saved');
      toast('订阅保存成功', 'success');
    } catch (error) {
      setStatus('保存失败，请重试', '');
      toast(error.message || '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  }

  function handleReset() {
    if (state.dirty && !window.confirm('当前有未保存的修改，确定要新建一份配置吗？')) return;

    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(ACTIVE_TASK_KEY);
    state.userId = null;
    state.dirty = false;
    stopPolling();

    $('nickname').value = '';
    $('identity').value = '';
    $('email').value = '';
    [$('nickname'), $('identity'), $('email')].forEach(clearFieldError);

    topicsField.set([]);
    keywordsField.set([]);
    excludeField.set([]);

    updateChip();
    setStatus('信息尚未保存', '');
    $('nickname').focus();
    toast('已解除当前用户关联，可创建新配置');
  }

  // ==========================================================
  //  事件绑定与启动
  // ==========================================================

  function bindEvents() {
    $('genBtn').addEventListener('click', () => {
      const hasBriefing =
        state.dashboard &&
        state.dashboard.latest_briefing &&
        state.dashboard.latest_task &&
        state.dashboard.latest_task.status === 'success';
      generateBriefing(Boolean(hasBriefing));
    });

    $('refreshBtn').addEventListener('click', async () => {
      await loadDashboard();
      toast('已刷新');
    });

    $('pushToggle').addEventListener('change', async (event) => {
      const enabled = event.target.checked;
      if (!requireUser()) { event.target.checked = !enabled; return; }
      try {
        const data = await request(`/users/${state.userId}/push-settings`, {
          method: 'PUT',
          body: { push_enabled: enabled },
        });
        toast(enabled ? '已开启邮件推送' : '已关闭邮件推送', 'success');
        if (state.dashboard) {
          state.dashboard.user.push_enabled = data.push_enabled;
          renderPushSwitch(state.dashboard.user);
        }
      } catch (error) {
        event.target.checked = !enabled;
        toast(error.message, 'error');
      }
    });

    $('saveBtn').addEventListener('click', handleSave);
    $('resetBtn').addEventListener('click', handleReset);

    [$('nickname'), $('identity'), $('email')].forEach((el) => {
      el.addEventListener('input', () => { clearFieldError(el); markDirty(); });
    });

    $('prevPage').addEventListener('click', () => loadHistory(Math.max(1, state.history.page - 1)));
    $('nextPage').addEventListener('click', () => loadHistory(state.history.page + 1));

    $('regenBtn').addEventListener('click', async () => {
      if (!state.detailBriefingId) return;
      if (!requireUser()) return;
      if (!window.confirm('重新生成会覆盖当天已有的简报，确定继续吗？')) return;

      try {
        // 详情页只知道简报 ID，用它的日期触发「强制重跑」
        const briefing = await request(`/briefings/${state.detailBriefingId}`);
        const data = await request(`/users/${state.userId}/briefings/trigger`, {
          method: 'POST',
          body: { briefing_date: briefing.briefing_date, force: true },
        });
        toast('已提交重新生成，可在概览页查看进度', 'success');
        window.location.hash = '#/';
        setGenerating(true);
        showPhase('pending');
        startPolling(data.task.id);
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    $('pushBtn').addEventListener('click', () => {
      const pushId = $('pushBtn').dataset.pushId;
      if (pushId) retryPush(Number(pushId), $('pushBtn'));
    });

    userChip.addEventListener('click', () => { window.location.hash = '#/settings'; });

    document.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (state.route === 'settings') handleSave();
      }
    });

    window.addEventListener('beforeunload', (event) => {
      if (state.dirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  }

  /** 初始用户：URL 参数优先（?user_id=3），其次 localStorage */
  function resolveInitialUserId() {
    const fromUrl = Number(new URLSearchParams(window.location.search).get('user_id'));
    if (Number.isInteger(fromUrl) && fromUrl > 0) {
      localStorage.setItem(STORAGE_KEY, String(fromUrl));
      return fromUrl;
    }
    const stored = localStorage.getItem(STORAGE_KEY);
    const fromStore = stored ? Number(stored) : NaN;
    return Number.isInteger(fromStore) && fromStore > 0 ? fromStore : null;
  }

  async function bootstrap() {
    renderPresets();
    updateChip();
    bindEvents();

    state.userId = resolveInitialUserId();
    if (state.userId) updateChip();

    window.addEventListener('hashchange', handleRoute);

    // 页脚的「每日 X 自动生成」与路由无关，先取一次，避免只在概览页显示
    request('/scheduler')
      .then((info) => {
        $('footerSched').textContent = info.enabled
          ? `每日 ${info.time} 自动生成 · ${info.timezone}`
          : '定时任务已关闭';
      })
      .catch(() => { $('footerSched').textContent = ''; });

    await handleRoute();

    // 刷新页面后若仍有任务在跑，恢复轮询并回到概览
    if (state.userId) {
      const resumed = await restoreActiveTask();
      if (resumed && state.route !== 'dashboard') window.location.hash = '#/';
    }
  }

  document.addEventListener('DOMContentLoaded', bootstrap);
})();
