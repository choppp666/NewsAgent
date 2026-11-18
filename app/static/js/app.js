/* ==========================================================
   每日 AI 新闻助手 · 前端逻辑
   技术栈：原生 JS（无框架），通过 fetch 调用 RESTful 接口
   ----------------------------------------------------------
   交互流程：
   1. 页面加载时读取 localStorage 中的 user_id，若有则回填用户信息与偏好；
   2. 点击「保存订阅」：无 user_id 先创建用户，有则更新用户；
   3. 随后调用「创建或更新订阅偏好」接口整体覆盖保存；
   4. 保存成功后把 user_id 写回 localStorage，刷新页面即可恢复。
   ========================================================== */

(function () {
  'use strict';

  // ---------------------------------------------------------- 常量
  const API_BASE = '/api/v1';
  // 简报表现在 /api/agent 下（生成接口会跑完整 Agent 循环，耗时长）
  const AGENT_API = '/api/agent';
  const STORAGE_KEY = 'news_agent_user_id';

  const LIMITS = {
    topics: 20,
    keywords: 50,
    exclude_keywords: 50,
    termLength: 30,
  };

  // 预设的关注领域，点击即可加入
  const PRESET_TOPICS = [
    '大模型 / LLM',
    'AI Agent',
    '多模态',
    '具身智能',
    'AI 芯片与算力',
    '开源模型',
    'RAG / 知识库',
    'AI 编程工具',
    'AI 合规与监管',
    '模型评测',
  ];

  // ---------------------------------------------------------- DOM 引用
  const $ = (id) => document.getElementById(id);

  const nicknameEl = $('nickname');
  const identityEl = $('identity');
  const emailEl = $('email');
  const saveBtn = $('saveBtn');
  const resetBtn = $('resetBtn');
  const saveStatus = $('saveStatus');
  const userChip = $('userChip');
  const userChipText = $('userChipText');
  const toastEl = $('toast');
  const presetRow = $('presetRow');
  const stepEls = Array.from(document.querySelectorAll('.step'));

  const genBtn = $('genBtn');
  const briefingState = $('briefingState');
  const briefingSub = $('briefingSub');
  const briefingProgress = $('briefingProgress');
  const progressText = $('progressText');
  const briefingStats = $('briefingStats');
  const briefingBody = $('briefingBody');

  // 运行时状态
  const state = {
    userId: null,
    dirty: false,
    loading: false,
    briefingLoading: false,
    briefing: null,
  };

  // ==========================================================
  //  工具函数
  // ==========================================================

  /** 统一的请求封装：统一解析「{code, message, data}」信封并抛出可读错误 */
  async function request(path, options = {}) {
    const { method = 'GET', body, base = API_BASE } = options;
    let response;
    try {
      response = await fetch(base + path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (networkError) {
      throw new Error('无法连接后端服务，请确认服务已启动');
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (e) {
      payload = null;
    }

    if (!response.ok || (payload && payload.code !== 0)) {
      const error = new Error(
        (payload && payload.message) || `请求失败（HTTP ${response.status}）`
      );
      error.status = response.status;
      throw error;
    }
    return payload ? payload.data : null;
  }

  /** 轻提示，2.4 秒后自动消失 */
  let toastTimer = null;
  function toast(message, type = 'info') {
    toastEl.textContent = message;
    toastEl.className = 'toast is-show' + (type === 'error' ? ' is-error' : type === 'success' ? ' is-success' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastEl.className = 'toast';
    }, 2400);
  }

  /** ISO 时间字符串 -> "YYYY-MM-DD HH:mm:ss" */
  function formatTime(isoString) {
    if (!isoString) return '';
    const text = String(isoString).replace('T', ' ').replace(/\.\d+/, '');
    return text.slice(0, 19);
  }

  /** 字段错误提示（红色边框 + 文案替换） */
  function setFieldError(input, message) {
    input.classList.add('is-invalid');
    const hint = input.parentElement.querySelector('.hint');
    if (hint) {
      if (hint.dataset.original === undefined) {
        hint.dataset.original = hint.textContent;
      }
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
    // 与后端 EmailStr 保持同样宽松度即可，前端只做基础格式拦截
    return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value);
  }

  // ==========================================================
  //  标签输入组件
  // ==========================================================

  class TagField {
    /**
     * @param {string} key        字段名（topics / keywords / exclude_keywords）
     * @param {number} limit      最多允许的标签数
     * @param {Function} onChange 变更回调（用于标记未保存状态）
     */
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
      // 点击空白区域聚焦输入框（模拟真实输入框体验）
      this.wrapper.addEventListener('click', (event) => {
        if (event.target === this.wrapper || event.target === this.list) {
          this.input.focus();
        }
      });

      this.input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
          event.preventDefault();
          this.commitInput();
        } else if (event.key === 'Backspace' && this.input.value === '' && this.values.length) {
          // 输入框为空时退格删除最后一个标签
          this.remove(this.values.length - 1);
        }
      });

      // 失焦时把未确认的输入也保存下来，避免用户输完直接点保存丢内容
      this.input.addEventListener('blur', () => this.commitInput());
    }

    /** 把输入框当前内容解析为标签 */
    commitInput() {
      const raw = this.input.value;
      if (!raw || !raw.trim()) return;
      this.addMany(raw);
      this.input.value = '';
    }

    /** 支持一次粘贴多个（逗号/顿号/分号分隔） */
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
        const exists = this.values.some(
          (item) => item.toLowerCase() === value.toLowerCase()
        );
        if (exists) continue;
        this.values.push(value);
        added = true;
      }

      if (added) {
        this.render();
        this.onChange();
      }
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

    get() {
      return this.values.slice();
    }

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
        text.textContent = value; // 使用 textContent，天然避免 XSS
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

    /** 已添加的预设标签置灰，避免重复点击 */
    syncPresets() {
      if (!this.presets) return;
      const lower = this.values.map((v) => v.toLowerCase());
      this.presets.forEach(({ el, value }) => {
        const used = lower.includes(value.toLowerCase());
        el.disabled = used || this.values.length >= this.limit;
      });
    }
  }

  // ==========================================================
  //  初始化标签组件与预设
  // ==========================================================

  const markDirty = () => {
    if (!state.dirty) {
      state.dirty = true;
      setStatus('有未保存的修改', 'is-dirty');
      updateSteps(2);
    }
  };

  const topicsField = new TagField('topics', LIMITS.topics, markDirty);
  const keywordsField = new TagField('keywords', LIMITS.keywords, markDirty);
  const excludeField = new TagField('exclude_keywords', LIMITS.exclude_keywords, markDirty);

  function renderPresets() {
    PRESET_TOPICS.forEach((value) => {
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'preset';
      el.textContent = value;
      el.addEventListener('click', () => {
        topicsField.addMany(value);
        topicsField.input.focus();
      });
      presetRow.appendChild(el);
    });
    topicsField.presets = Array.from(presetRow.querySelectorAll('.preset')).map((el) => ({
      el,
      value: el.textContent,
    }));
    topicsField.syncPresets();
  }

  // ==========================================================
  //  状态展示
  // ==========================================================

  function setStatus(text, className) {
    saveStatus.textContent = text;
    saveStatus.className = 'status' + (className ? ' ' + className : '');
  }

  function updateChip() {
    if (state.userId) {
      userChip.classList.add('is-active');
      userChipText.textContent = `已关联用户 #${state.userId}`;
    } else {
      userChip.classList.remove('is-active');
      userChipText.textContent = '尚未创建配置';
    }
  }

  /** 步骤条：1=填信息 2=设偏好 3=已保存 */
  function updateSteps(activeCount) {
    stepEls.forEach((el, index) => {
      el.classList.toggle('is-active', index < activeCount);
    });
  }

  function setLoading(loading) {
    state.loading = loading;
    saveBtn.disabled = loading;
    saveBtn.classList.toggle('is-loading', loading);
    saveBtn.querySelector('.btn-label').textContent = loading ? '保存中…' : '保存订阅';
  }

  // ==========================================================
  //  表单读写
  // ==========================================================

  function fillProfile(user) {
    nicknameEl.value = user.nickname || '';
    identityEl.value = user.identity || '';
    emailEl.value = user.email || '';
  }

  function fillPreferences(pref) {
    if (!pref) {
      topicsField.set([]);
      keywordsField.set([]);
      excludeField.set([]);
      return;
    }
    topicsField.set(pref.topics || []);
    keywordsField.set(pref.keywords || []);
    excludeField.set(pref.exclude_keywords || []);
  }

  /** 读取并校验表单，返回 null 表示校验未通过 */
  function readProfile() {
    // 先把各标签输入框里未确认的内容收进来
    topicsField.commitInput();
    keywordsField.commitInput();
    excludeField.commitInput();

    let firstInvalid = null;

    const nickname = nicknameEl.value.trim();
    if (!nickname) {
      setFieldError(nicknameEl, '请填写昵称');
      firstInvalid = firstInvalid || nicknameEl;
    } else if (nickname.length > 50) {
      setFieldError(nicknameEl, '昵称不能超过 50 个字符');
      firstInvalid = firstInvalid || nicknameEl;
    } else {
      clearFieldError(nicknameEl);
    }

    const identity = identityEl.value.trim();
    if (!identity) {
      setFieldError(identityEl, '请填写身份或职业描述');
      firstInvalid = firstInvalid || identityEl;
    } else if (identity.length > 200) {
      setFieldError(identityEl, '描述不能超过 200 个字符');
      firstInvalid = firstInvalid || identityEl;
    } else {
      clearFieldError(identityEl);
    }

    const email = emailEl.value.trim();
    if (email && !isValidEmail(email)) {
      setFieldError(emailEl, '邮箱格式不正确');
      firstInvalid = firstInvalid || emailEl;
    } else {
      clearFieldError(emailEl);
    }

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

  // ==========================================================
  //  核心交互：加载与保存
  // ==========================================================

  async function loadSavedData(userId) {
    try {
      const [user, preference] = await Promise.all([
        request(`/users/${userId}`),
        request(`/users/${userId}/preferences`),
      ]);

      fillProfile(user);
      fillPreferences(preference);

      state.userId = user.id;
      state.dirty = false;
      updateChip();
      updateSteps(3);

      if (preference) {
        setStatus(`已保存 · 最后更新 ${formatTime(preference.updated_at)}`, 'is-saved');
      } else {
        setStatus('已加载用户信息，但尚未设置订阅偏好', '');
      }
    } catch (error) {
      // 用户被删除或数据库被清空时，自动解除本地关联，回到「新建」状态
      if (error.status === 404) {
        localStorage.removeItem(STORAGE_KEY);
        state.userId = null;
        updateChip();
        updateSteps(1);
        setStatus('本地关联的用户已不存在，请重新保存配置', '');
        loadBriefing();
      } else {
        toast(error.message, 'error');
      }
    }
  }

  async function saveProfile(profile, userId) {
    const body = {
      nickname: profile.nickname,
      identity: profile.identity,
      email: profile.email,
    };

    if (userId) {
      try {
        return await request(`/users/${userId}`, { method: 'PUT', body });
      } catch (error) {
        // 用户已被删除：自动降级为新建，避免用户卡在报错里
        if (error.status === 404) {
          return request('/users', { method: 'POST', body });
        }
        throw error;
      }
    }
    return request('/users', { method: 'POST', body });
  }

  async function handleSave() {
    if (state.loading) return;

    const profile = readProfile();
    if (!profile) return;

    setLoading(true);
    try {
      // 1) 用户信息：有 ID 走更新，无 ID 走创建
      const user = await saveProfile(profile, state.userId);

      // 2) 订阅偏好：整体覆盖保存
      const preference = await request(`/users/${user.id}/preferences`, {
        method: 'PUT',
        body: {
          topics: profile.topics,
          keywords: profile.keywords,
          exclude_keywords: profile.exclude_keywords,
        },
      });

      // 3) 记住用户 ID，刷新后可恢复
      state.userId = user.id;
      state.dirty = false;
      localStorage.setItem(STORAGE_KEY, String(user.id));

      updateChip();
      updateSteps(3);
      setStatus(`已保存 · 最后更新 ${formatTime(preference.updated_at)}`, 'is-saved');
      toast('订阅保存成功', 'success');

      // 偏好变了，今天的简报状态也要跟着刷新（可能已经有旧简报）
      loadBriefing();
    } catch (error) {
      setStatus('保存失败，请重试', '');
      toast(error.message || '保存失败', 'error');
    } finally {
      setLoading(false);
    }
  }

  function handleReset() {
    if (state.dirty && !window.confirm('当前有未保存的修改，确定要新建一份配置吗？')) {
      return;
    }
    localStorage.removeItem(STORAGE_KEY);
    state.userId = null;
    state.dirty = false;

    nicknameEl.value = '';
    identityEl.value = '';
    emailEl.value = '';
    [nicknameEl, identityEl, emailEl].forEach(clearFieldError);

    topicsField.set([]);
    keywordsField.set([]);
    excludeField.set([]);

    updateChip();
    updateSteps(1);
    setStatus('信息尚未保存', '');
    nicknameEl.focus();
    loadBriefing();
  }

  // ==========================================================
  //  今日简报
  // ==========================================================

  /** 本地时区的 YYYY-MM-DD。不能用 toISOString()，那是 UTC，晚上 8 点后会算成第二天 */
  function todayISO() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }

  function escapeHtml(text) {
    return String(text).replace(
      /[&<>"']/g,
      (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])
    );
  }

  /**
   * 行内语法：**粗体**、`代码`、[文字](链接)、裸链接。
   * 必须「先转义再插标签」——简报正文来自大模型，直接塞进 innerHTML 会有 XSS 风险。
   */
  function renderInline(text) {
    const links = [];
    let out = escapeHtml(text);

    // Markdown 链接先换成占位符，否则后面的裸链接规则会把它二次包裹
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (match, label, url) => {
      links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      return `\u0001${links.length - 1}\u0001`;
    });

    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // 裸链接：前面可能是空格，也可能是中文冒号/顿号/括号（简报里写作「原文链接：https://…」）
    out = out.replace(
      /(^|[\s\u3000（(【\[：:，,、；;])(https?:\/\/[^\s<>"'）)】\]，。；、]+)/g,
      (match, lead, url) => {
        return `${lead}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`;
      }
    );

    return out.replace(/\u0001(\d+)\u0001/g, (match, index) => links[Number(index)] || '');
  }

  /**
   * 轻量 Markdown 渲染：只覆盖简报实际用到的语法
   * （# ~ ### 标题、有序/无序列表、引用、粗体、行内代码、链接）。
   * 不引第三方库：页面要求零依赖，内网离线也能打开。
   */
  function renderMarkdown(markdown) {
    const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
    const html = [];
    let listTag = null;
    let quoteLines = [];

    const closeList = () => {
      if (listTag) {
        html.push(`</${listTag}>`);
        listTag = null;
      }
    };
    const flushQuote = () => {
      if (quoteLines.length) {
        html.push(`<blockquote><p>${quoteLines.map(renderInline).join('<br />')}</p></blockquote>`);
        quoteLines = [];
      }
    };

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();

      if (!line.trim()) {
        closeList();
        flushQuote();
        continue;
      }

      if (line.startsWith('> ')) {
        closeList();
        quoteLines.push(line.slice(2));
        continue;
      }
      flushQuote();

      const heading = /^(#{1,3})\s+(.*)$/.exec(line);
      if (heading) {
        closeList();
        const level = heading[1].length;
        html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        continue;
      }

      if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
        closeList();
        html.push('<hr />');
        continue;
      }

      const ordered = /^(\d+)\.\s+(.*)$/.exec(line);
      const unordered = /^[-*+]\s+(.*)$/.exec(line);
      if (ordered || unordered) {
        const tag = ordered ? 'ol' : 'ul';
        if (listTag !== tag) {
          closeList();
          html.push(`<${tag}>`);
          listTag = tag;
        }
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

  function updateBriefingButton() {
    genBtn.disabled = state.briefingLoading || !state.userId;
    genBtn.querySelector('.btn-label').textContent = state.briefing ? '重新生成' : '生成今日简报';
  }

  function setBriefingLoading(loading) {
    state.briefingLoading = loading;
    genBtn.classList.toggle('is-loading', loading);
    briefingProgress.hidden = !loading;

    if (loading) {
      genBtn.disabled = true;
      genBtn.querySelector('.btn-label').textContent = '生成中…';
    } else {
      updateBriefingButton();
    }
  }

  function resetBriefingView() {
    state.briefing = null;
    briefingBody.hidden = true;
    briefingBody.innerHTML = '';
    briefingStats.hidden = true;
    briefingStats.innerHTML = '';
  }

  /** 读取今天的简报（同一用户同一天只会有一条） */
  async function loadBriefing() {
    if (!state.userId) {
      resetBriefingView();
      briefingState.textContent = '尚未生成';
      briefingSub.textContent = '保存订阅偏好后即可生成';
      updateBriefingButton();
      return;
    }

    briefingState.textContent = '读取中…';
    briefingSub.textContent = '';
    try {
      const data = await request(`/briefings?user_id=${state.userId}&page=1&page_size=30`);
      const today = todayISO();
      const found = (data.items || []).find((item) => item.briefing_date === today);

      if (!found) {
        resetBriefingView();
        briefingState.textContent = '今日尚未生成';
        briefingSub.textContent = '点右侧按钮，Agent 会现场检索并撰写';
      } else {
        const detail = await request(`/briefings/${found.id}`);
        renderBriefing(detail);
      }
    } catch (error) {
      briefingState.textContent = '读取失败';
      briefingSub.textContent = error.message;
    } finally {
      updateBriefingButton();
    }
  }

  function renderBriefing(briefing) {
    state.briefing = briefing;
    briefingState.textContent = `已生成 · ${briefing.briefing_date}`;
    briefingSub.textContent = `${briefing.news_count} 条 · ${formatTime(briefing.generated_at)} 生成`;
    briefingBody.innerHTML = renderMarkdown(briefing.content || '');
    briefingBody.hidden = false;
    briefingStats.hidden = true;
    briefingStats.innerHTML = '';
    updateBriefingButton();
  }

  /** 把本次运行的轨迹摘要做成小标签，让人看得出「模型真的自己跑了一轮」 */
  function renderRunStats(result) {
    const run = result.run || {};
    const pairs = [
      ['搜索', `${(result.searches || []).length} 次`],
      ['候选', `${result.news_candidates || 0} 条`],
      ['读原文', `${result.fetched_articles || 0} 篇`],
      ['轮数', String(run.steps || 0)],
      ['工具调用', `${run.tool_call_count || 0} 次`],
      ['耗时', `${((run.duration_ms || 0) / 1000).toFixed(1)}s`],
      ['模型', `${run.llm_mode || ''}/${run.model || ''}`],
    ];
    briefingStats.innerHTML = pairs
      .map(([label, value]) => `<li>${label} <b>${escapeHtml(value)}</b></li>`)
      .join('');
    briefingStats.hidden = false;
  }

  async function generateBriefing() {
    if (state.briefingLoading) return;
    if (!state.userId) {
      toast('请先保存订阅偏好', 'error');
      return;
    }

    setBriefingLoading(true);
    briefingState.textContent = '正在生成…';
    briefingSub.textContent = '模型正在自主检索';

    const startedAt = Date.now();
    const tick = () => {
      const seconds = Math.round((Date.now() - startedAt) / 1000);
      progressText.textContent = `模型正在自主执行（搜索 → 读原文 → 撰写），已用 ${seconds} 秒`;
    };
    tick();
    const timer = setInterval(tick, 500);

    try {
      const result = await request('/briefings', {
        method: 'POST',
        base: AGENT_API,
        body: { user_id: state.userId },
      });

      if (result && result.briefing) {
        renderBriefing(result.briefing);
        renderRunStats(result);
        toast('简报已生成并入库', 'success');
      } else {
        const reason = (result && (result.warnings || []).join('；')) || '本次未生成简报';
        briefingState.textContent = '生成未完成';
        briefingSub.textContent = reason;
        toast(reason, 'error');
      }
    } catch (error) {
      briefingState.textContent = '生成失败';
      briefingSub.textContent = error.message || '请稍后重试';
      toast(error.message || '生成失败', 'error');
    } finally {
      clearInterval(timer);
      setBriefingLoading(false);
    }
  }

  // ==========================================================
  //  事件绑定与启动
  // ==========================================================

  [nicknameEl, identityEl, emailEl].forEach((el) => {
    el.addEventListener('input', () => {
      clearFieldError(el);
      markDirty();
    });
  });

  saveBtn.addEventListener('click', handleSave);
  resetBtn.addEventListener('click', handleReset);
  genBtn.addEventListener('click', generateBriefing);

  // Ctrl / Cmd + S 快捷保存
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      handleSave();
    }
  });

  // 页面卸载前提醒未保存内容
  window.addEventListener('beforeunload', (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  /**
   * 初始用户来源：URL 参数优先，其次 localStorage。
   * 支持 `?user_id=3` 直接打开某个用户的简报，验收和排障时不用手动改本地存储。
   */
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

  function bootstrap() {
    renderPresets();
    updateChip();
    updateSteps(1);

    const userId = resolveInitialUserId();

    if (userId) {
      state.userId = userId;
      updateChip();
      loadSavedData(userId);
      loadBriefing();
    } else {
      nicknameEl.focus();
    }
  }

  document.addEventListener('DOMContentLoaded', bootstrap);
})();
