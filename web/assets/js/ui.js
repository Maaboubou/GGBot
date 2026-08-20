/**
 * UI Module
 * Handles DOM manipulation and rendering
 */

const UI = {
    sidebarStorageKey: 'wxauto.sidebarCollapsed',
    themeStorageKey: 'wxauto.colorTheme',
    routes: {
        dashboard: '/',
        users: '/chats',
        roles: '/assistant',
        plugins: '/plugins',
        llm: '/ai',
        logs: '/operations/logs',
        settings: '/system'
    },
    routeAliases: {
        '/dashboard': 'dashboard',
        '/users': 'users',
        '/roles': 'roles',
        '/automations': 'plugins',
        '/llm': 'llm',
        '/logs': 'logs',
        '/settings': 'settings',
        '/system/providers': 'settings',
        '/system/integrations': 'settings',
        '/system/runtime': 'settings',
        '/system/developer': 'settings',
        '/system/operations': 'settings',
        '/system/backups': 'settings',
        '/assistant/roles': 'roles',
        '/assistant/chats': 'roles',
        '/assistant/memory': 'roles',
        '/ai/models': 'llm',
        '/ai/mappings': 'llm',
        '/ai/usage': 'llm',
        '/ai/sessions': 'llm',
        '/ai/calls': 'llm',
        '/ai/network': 'llm',
        '/operations': 'logs'
    },

    // Icons mapping
    icons: {
        cpu: 'bi-cpu',
        memory: 'bi-memory',
        disk: 'bi-hdd',
        time: 'bi-clock',
        check: 'bi-check-circle-fill',
        error: 'bi-x-circle-fill',
        plugin: 'bi-puzzle',
        user: 'bi-person'
    },

    // Initialize UI components
    init() {
        this.setupTheme();

        // Mobile Sidebar
        const toggleBtn = document.querySelector('.mobile-toggle');
        const overlay = document.querySelector('.mobile-overlay');
        const collapseBtn = document.getElementById('sidebarCollapseToggle');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', () => UI.toggleSidebar(true));
        }
        if (overlay) {
            overlay.addEventListener('click', () => UI.toggleSidebar(false));
        }
        if (collapseBtn) {
            collapseBtn.addEventListener('click', () => UI.toggleDesktopSidebar());

            let shouldCollapse = false;
            try {
                shouldCollapse = localStorage.getItem(this.sidebarStorageKey) === 'true';
            } catch (e) {
                console.warn('Could not restore sidebar state:', e);
            }
            this.toggleDesktopSidebar(shouldCollapse, false);
        }

        // Tab Navigation
        document.querySelectorAll('.nav-link[data-tab]').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                UI.switchTab(link.dataset.tab);
            });
        });

        window.addEventListener('popstate', () => {
            UI.switchTab(UI.getInitialTab(), { history: false });
        });

        // Initialize Tooltips if Bootstrap is available
        if (typeof bootstrap !== 'undefined' && bootstrap.Tooltip) {
            const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
            tooltipTriggerList.map(function (tooltipTriggerEl) {
                return new bootstrap.Tooltip(tooltipTriggerEl);
            });
        }
    },

    getSystemTheme() {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    },

    getSavedTheme() {
        try {
            const value = localStorage.getItem(this.themeStorageKey);
            return ['light', 'dark'].includes(value) ? value : null;
        } catch (e) {
            console.warn('Could not restore color theme:', e);
            return null;
        }
    },

    applyTheme(theme, persist = false) {
        const nextTheme = theme === 'dark' ? 'dark' : 'light';
        document.documentElement.dataset.theme = nextTheme;
        document.documentElement.setAttribute('data-bs-theme', nextTheme);
        document.querySelector('meta[name="theme-color"]')
            ?.setAttribute('content', getComputedStyle(document.documentElement).getPropertyValue('--bg-body').trim());

        if (persist) {
            try {
                localStorage.setItem(this.themeStorageKey, nextTheme);
            } catch (e) {
                console.warn('Could not save color theme:', e);
            }
        }

        const button = document.getElementById('themeToggle');
        if (!button) return;
        const isDark = nextTheme === 'dark';
        const actionLabel = isDark ? '切换到亮色模式' : '切换到暗色模式';
        button.setAttribute('aria-label', actionLabel);
        button.setAttribute('aria-pressed', String(isDark));
        button.title = actionLabel;
        const icon = button.querySelector('i');
        if (icon) icon.className = `bi ${isDark ? 'bi-sun' : 'bi-moon-stars'}`;
        const label = button.querySelector('[data-theme-label]');
        if (label) label.textContent = isDark ? '亮色' : '暗色';
    },

    setupTheme() {
        this.applyTheme(this.getSavedTheme() || this.getSystemTheme());
        document.getElementById('themeToggle')?.addEventListener('click', () => {
            const currentTheme = document.documentElement.dataset.theme || 'light';
            this.applyTheme(currentTheme === 'dark' ? 'light' : 'dark', true);
        });

        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', event => {
            if (!this.getSavedTheme()) this.applyTheme(event.matches ? 'dark' : 'light');
        });
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    },

    debounce(fn, delay = 300) {
        let timer = null;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), delay);
        };
    },

    toggleSidebar(show) {
        const sidebar = document.querySelector('.sidebar');
        const overlay = document.querySelector('.mobile-overlay');
        if (show) {
            sidebar.classList.add('show');
            overlay.classList.add('show');
        } else {
            sidebar.classList.remove('show');
            overlay.classList.remove('show');
        }
    },

    toggleDesktopSidebar(collapsed, persist = true) {
        const sidebar = document.querySelector('.sidebar');
        const toggleBtn = document.getElementById('sidebarCollapseToggle');
        if (!sidebar || !toggleBtn) return;

        const shouldCollapse = typeof collapsed === 'boolean'
            ? collapsed
            : !sidebar.classList.contains('is-collapsed');
        sidebar.classList.toggle('is-collapsed', shouldCollapse);

        const actionLabel = shouldCollapse ? '展开侧边栏' : '折叠侧边栏';
        toggleBtn.setAttribute('aria-label', actionLabel);
        toggleBtn.setAttribute('aria-expanded', String(!shouldCollapse));
        toggleBtn.title = actionLabel;

        document.querySelectorAll('.sidebar .nav-link[data-tab]').forEach(link => {
            const label = link.querySelector('span')?.textContent?.trim();
            if (!label) return;
            link.setAttribute('aria-label', label);
            if (shouldCollapse) {
                link.title = label;
            } else {
                link.removeAttribute('title');
            }
        });

        if (persist) {
            try {
                localStorage.setItem(this.sidebarStorageKey, String(shouldCollapse));
            } catch (e) {
                console.warn('Could not save sidebar state:', e);
            }
        }
    },

    normalizePath(pathname) {
        if (!pathname || pathname === '/index.html') return '/';
        const normalized = pathname.replace(/\/+$/, '');
        return normalized || '/';
    },

    getInitialTab() {
        const pathname = this.normalizePath(window.location.pathname);
        const direct = Object.entries(this.routes).find(([, path]) => path === pathname);
        if (direct) return direct[0];
        return this.routeAliases[pathname] || 'dashboard';
    },

    switchTab(tabId, options = {}) {
        if (!this.routes[tabId]) tabId = 'dashboard';
        // Update Sidebar
        document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
        const activeLink = document.querySelector(`.nav-link[data-tab="${tabId}"]`);
        if (activeLink) activeLink.classList.add('active');

        // Update Content
        // specific selector to avoid hiding nested tab-content (like in LLM manager)
        document.querySelectorAll('.main-content > .tab-content').forEach(el => el.classList.add('d-none'));
        const targetTab = document.getElementById(tabId);
        if (targetTab) {
            targetTab.classList.remove('d-none');
            // Trigger load data for this tab
            window.App.loadTab(tabId);
        }

        if (options.history !== false) {
            const targetPath = this.routes[tabId];
            if (this.normalizePath(window.location.pathname) !== targetPath) {
                window.history.pushState({ tab: tabId }, '', targetPath);
            }
        }

        // Close sidebar on mobile
        if (window.innerWidth < 768) UI.toggleSidebar(false);

        // Update Title
        const titleMap = {
            'dashboard': '概览',
            'plugins': '插件',
            'users': '聊天',
            'roles': 'AI 助手',
            'settings': '系统',
            'wechat': 'WeChat 状态',
            'logs': '运行与日志',
            'llm': '模型与调用'
        };
        document.getElementById('pageTitle').textContent = titleMap[tabId] || '概览';
    },

    showLoading(elementId) {
        const el = document.getElementById(elementId);
        if (el) {
            el.innerHTML = `
                <div class="loading-wrapper">
                    <div class="spinner-custom"></div>
                    <p>正在加载数据…</p>
                </div>
            `;
        }
    },

    showLoadingOverlay(message) {
        let overlay = document.querySelector('.mobile-overlay');
        // Create full screen overlay if it's just the sidebar one or doesn't exist
        // or just reuse a new specific one

        // Remove existing if any (to avoid duplicates/conflicts)
        const old = document.getElementById('system-loading-overlay');
        if (old) old.remove();

        const div = document.createElement('div');
        div.id = 'system-loading-overlay';
        div.style.position = 'fixed';
        div.style.top = '0';
        div.style.left = '0';
        div.style.width = '100vw';
        div.style.height = '100vh';
        div.style.backgroundColor = 'rgba(var(--ink-rgb), 0.85)';
        div.style.zIndex = '9999';
        div.style.display = 'flex';
        div.style.flexDirection = 'column';
        div.style.alignItems = 'center';
        div.style.justifyContent = 'center';
        div.style.color = 'var(--text-on-dark)';
        div.style.backdropFilter = 'blur(5px)';

        div.innerHTML = `
            <div class="spinner-border text-light mb-4" style="width: 3rem; height: 3rem;" role="status"></div>
            <h4 class="fw-light">${this.escapeHtml(message || '正在加载…')}</h4>
            <div class="small text-white-50 mt-2">页面会自动重新加载。</div>
        `;

        document.body.appendChild(div);
    },

    showRestartOverlay(title = '系统重启中', statusMessage = '正在重启服务…') {
        const old = document.getElementById('system-restart-overlay');
        if (old) old.remove();

        const div = document.createElement('div');
        div.id = 'system-restart-overlay';
        div.style.cssText = `
            position:fixed; top:0; left:0; width:100vw; height:100vh;
            background:rgba(var(--ink-rgb),0.96); z-index:10000;
            display:flex; flex-direction:column; align-items:center; justify-content:center;
            color:var(--text-on-dark); backdrop-filter:blur(8px); font-family:inherit;
        `;

        div.innerHTML = `
            <style>
                @keyframes rx-spin { to { transform: rotate(360deg); } }
                @keyframes rx-pulse { 0%,100%{opacity:.3} 50%{opacity:1} }
                @keyframes rx-dot { 0%,80%,100%{transform:scale(0);opacity:0} 40%{transform:scale(1);opacity:1} }
                .rx-ring {
                    width:80px; height:80px; border-radius:50%;
                    border:3px solid rgba(var(--primary-rgb),0.2);
                    border-top-color:var(--primary);
                    animation: rx-spin 1s linear infinite;
                    margin-bottom:32px;
                }
                .rx-dots span {
                    display:inline-block; width:8px; height:8px; border-radius:50%;
                    background:var(--primary); margin:0 4px;
                }
                .rx-dots span:nth-child(1){animation:rx-dot 1.4s ease-in-out 0s infinite}
                .rx-dots span:nth-child(2){animation:rx-dot 1.4s ease-in-out .2s infinite}
                .rx-dots span:nth-child(3){animation:rx-dot 1.4s ease-in-out .4s infinite}
                .rx-status { animation: rx-pulse 2s ease-in-out infinite; }
            </style>
            <div class="rx-ring"></div>
            <h4 style="font-weight:300;letter-spacing:.05em;margin-bottom:8px;">${this.escapeHtml(title)}</h4>
            <div class="rx-status" style="color:var(--text-muted-on-dark);font-size:.9rem;margin-bottom:24px;" id="rx-status-text">${this.escapeHtml(statusMessage)}</div>
            <div class="rx-dots"><span></span><span></span><span></span></div>
            <div style="margin-top:28px;color:var(--text-muted-on-dark);font-size:.8rem;">
                已等待 <span id="rx-elapsed">0</span> 秒 &nbsp;·&nbsp; 连接恢复后将自动刷新
            </div>
        `;

        document.body.appendChild(div);

        // Elapsed timer
        let elapsed = 0;
        this._rxTimer = setInterval(() => {
            elapsed++;
            const el = document.getElementById('rx-elapsed');
            if (el) el.textContent = elapsed;
        }, 1000);

        // Status messages cycle
        const statuses = [
            statusMessage,
            '等待服务恢复…',
            '正在重新连接…',
            '即将完成，请稍候…',
        ];
        let si = 0;
        this._rxStatusTimer = setInterval(() => {
            si = (si + 1) % statuses.length;
            const el = document.getElementById('rx-status-text');
            if (el) el.textContent = statuses[si];
        }, 4000);
    },

    hideRestartOverlay() {
        const div = document.getElementById('system-restart-overlay');
        if (div) div.remove();
        if (this._rxTimer) { clearInterval(this._rxTimer); this._rxTimer = null; }
        if (this._rxStatusTimer) { clearInterval(this._rxStatusTimer); this._rxStatusTimer = null; }
    },

    showError(message, type = 'toast') {
        if (type === 'toast') {
            this.showToast(message, 'danger');
        } else {
            const container = document.querySelector('main');
            const alert = document.createElement('div');
            alert.className = 'alert alert-danger alert-dismissible fade show mb-4';
            alert.innerHTML = `
                <strong>错误：</strong>${this.escapeHtml(message)}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            container.prepend(alert);
        }
    },

    showSuccess(message) {
        this.showToast(message, 'success');
    },

    showInfo(message) {
        this.showToast(message, 'info');
    },

    showToast(message, variant = 'secondary', delay = 3500) {
        if (typeof bootstrap === 'undefined' || !bootstrap.Toast) {
            console.log(`[${variant}] ${message}`);
            return;
        }

        let container = document.getElementById('toastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toastContainer';
            container.className = 'toast-container position-fixed top-0 end-0 p-3';
            container.style.zIndex = '10800';
            document.body.appendChild(container);
        }

        const iconMap = {
            success: 'bi-check-circle',
            danger: 'bi-exclamation-triangle',
            warning: 'bi-exclamation-circle',
            info: 'bi-info-circle',
            secondary: 'bi-bell'
        };
        const titleMap = {
            success: '成功',
            danger: '错误',
            warning: '提醒',
            info: '信息',
            secondary: '通知'
        };

        const toastEl = document.createElement('div');
        toastEl.className = 'toast shadow border-0';
        toastEl.setAttribute('role', 'status');
        toastEl.setAttribute('aria-live', 'polite');
        toastEl.setAttribute('aria-atomic', 'true');
        toastEl.innerHTML = `
            <div class="toast-header">
                <i class="bi ${iconMap[variant] || iconMap.secondary} text-${variant} me-2"></i>
                <strong class="me-auto">${titleMap[variant] || titleMap.secondary}</strong>
                <button type="button" class="btn-close" data-bs-dismiss="toast" aria-label="关闭"></button>
            </div>
            <div class="toast-body">${this.escapeHtml(message)}</div>
        `;

        container.appendChild(toastEl);
        const toast = new bootstrap.Toast(toastEl, { delay });
        toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
        toast.show();
    },

    confirm(message, options = {}) {
        if (typeof bootstrap === 'undefined' || !bootstrap.Modal) {
            return Promise.resolve(window.confirm(message));
        }

        const old = document.getElementById('uiConfirmModal');
        if (old) old.remove();

        const confirmText = options.confirmText || '确认';
        const cancelText = options.cancelText || '取消';
        const title = options.title || '请确认';
        const requestedVariant = options.variant || 'primary';
        const variant = ['primary', 'danger', 'warning', 'success', 'secondary'].includes(requestedVariant)
            ? requestedVariant
            : 'primary';

        const modalEl = document.createElement('div');
        modalEl.className = 'modal fade';
        modalEl.id = 'uiConfirmModal';
        modalEl.tabIndex = -1;
        modalEl.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">${this.escapeHtml(title)}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
                    </div>
                    <div class="modal-body">
                        <div style="white-space:pre-wrap;">${this.escapeHtml(message)}</div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">${this.escapeHtml(cancelText)}</button>
                        <button type="button" class="btn btn-${variant}" id="uiConfirmAccept">${this.escapeHtml(confirmText)}</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modalEl);

        return new Promise(resolve => {
            let accepted = false;
            const modal = new bootstrap.Modal(modalEl);
            modalEl.querySelector('#uiConfirmAccept').addEventListener('click', () => {
                accepted = true;
                modal.hide();
            });
            modalEl.addEventListener('hidden.bs.modal', () => {
                modalEl.remove();
                resolve(accepted);
            }, { once: true });
            modal.show();
        });
    },

    // Components Renderers
    renderDashboard(status, components, stats, wxStatus, wxInfo) {
        // 0. Render Bot Status (WeChat)
        const botStatusEl = document.getElementById('dashboardWechatStatus');
        if (botStatusEl && wxStatus) {
            const isConnected = wxStatus.status === 'connected';
            const info = wxInfo || {};

            // Try to resolve user info keys
            const name = info.display_name || info.name || info.nickname || info.nickName || info.self_nickname || 'WeChat Bot';
            const avatar = info.bm || info.avatar || info.headImgUrl || info.mediumHeadImgUrl || '';
            const id = info.id || info.wxid || info.username || '';
            const safeAvatar = this.escapeHtml(avatar);

            const statusBadge = isConnected ?
                '<span class="badge bg-success-subtle text-success rounded-pill"><i class="bi bi-circle-fill me-1" style="font-size: 6px; vertical-align: middle;"></i>在线</span>' :
                '<span class="badge bg-danger-subtle text-danger rounded-pill"><i class="bi bi-circle-fill me-1" style="font-size: 6px; vertical-align: middle;"></i>离线</span>';

            botStatusEl.innerHTML = `
                <div class="mb-3">
                    ${avatar ?
                    `<img src="${safeAvatar}" class="rounded-circle shadow-sm border border-2 border-white" style="width: 80px; height: 80px; object-fit: cover;" alt="WeChat 头像">` :
                    `<div class="rounded-circle bg-light d-inline-flex align-items-center justify-content-center shadow-sm" style="width: 80px; height: 80px;"><i class="bi bi-robot fs-1 text-secondary"></i></div>`
                }
                </div>
                <h5 class="fw-bold mb-1">${isConnected ? this.escapeHtml(name) : 'WeChat 客户端'}</h5>
                ${isConnected && id ? `<div class="text-muted small font-monospace mb-2">${this.escapeHtml(id)}</div>` : ''}
                <div class="mt-2">${statusBadge}</div>
            `;
        }

        // 1. Render Compact System Status
        const statusList = document.getElementById('systemStatusList');
        if (statusList && status && status.system) {
            const cpu = Math.max(0, Math.min(100, Number(status.system.cpu?.usage_percent || 0)));
            const mem = Math.max(0, Math.min(100, Number(status.system.memory?.percent || 0)));
            const disk = Math.max(0, Math.min(100, Number(status.system.disk?.percent || 0)));
            const uptime = Math.max(0, Number(status.system.uptime || 0) / 3600).toFixed(1);

            statusList.innerHTML = `
                <div class="mb-4">
                    <div class="d-flex justify-content-between mb-1">
                        <span class="small fw-bold text-muted">CPU</span>
                        <span class="small text-primary">${cpu.toFixed(1)}%</span>
                    </div>
                    <div class="progress" style="height: 6px;">
                        <div class="progress-bar bg-primary" style="width: ${cpu}%"></div>
                    </div>
                </div>
                <div class="mb-4">
                    <div class="d-flex justify-content-between mb-1">
                        <span class="small fw-bold text-muted">内存</span>
                        <span class="small text-info">${mem.toFixed(1)}%</span>
                    </div>
                    <div class="progress" style="height: 6px;">
                        <div class="progress-bar bg-info" style="width: ${mem}%"></div>
                    </div>
                </div>
                <div class="mb-4">
                     <div class="d-flex justify-content-between mb-1">
                        <span class="small fw-bold text-muted">磁盘</span>
                        <span class="small text-warning">${disk.toFixed(1)}%</span>
                    </div>
                    <div class="progress" style="height: 6px;">
                        <div class="progress-bar bg-warning" style="width: ${disk}%"></div>
                    </div>
                </div>
                 <div class="d-flex align-items-center pt-2 border-top">
                    <i class="bi bi-clock me-2 text-muted"></i>
                    <div>
                        <div class="small text-muted fw-bold">运行时间</div>
                        <div class="h6 mb-0">${uptime} 小时</div>
                    </div>
                </div>
            `;
        }

        // 2. Render Listeners Summary (User-Centric View)
        const listenersContainer = document.getElementById('listenersSummary');
        const totalCountEl = document.getElementById('totalListenersCount');

        // Normalize stats object
        const s = stats && stats.stats ? stats.stats : (stats || {});

        if (listenersContainer && s.user_listeners_by_type) {
            const groups = s.user_listeners_by_type;
            const totalListeners = s.total_listeners || 0;
            if (totalCountEl) totalCountEl.textContent = `${totalListeners} 个活跃监听`;

            if (Object.keys(groups).length === 0) {
                listenersContainer.innerHTML = `
                    <div class="text-center py-5 text-muted">
                        <i class="bi bi-broadcast opacity-25" style="font-size: 3rem;"></i>
                        <p class="mt-3">未找到活跃的消息监听。</p>
                    </div>
                 `;
            } else {
                const typeLabels = {
                    'text_message_received': { icon: 'bi-chat-text', label: '文本消息', color: 'success' },
                    'image_message_received': { icon: 'bi-image', label: '图片', color: 'warning' },
                    'file_message_received': { icon: 'bi-file-earmark', label: '文件', color: 'primary' },
                    'quote_message_received': { icon: 'bi-chat-quote', label: '引用消息', color: 'info' },
                    'quote_text_message_received': { icon: 'bi-blockquote-left', label: '文本引用', color: 'info' },
                    'quote_image_message_received': { icon: 'bi-card-image', label: '图片引用', color: 'warning' },
                    'friend_request_received': { icon: 'bi-person-plus', label: '好友请求', color: 'danger' },
                    'system_startup': { icon: 'bi-power', label: '系统启动', color: 'secondary' },
                    'system_shutdown': { icon: 'bi-power', label: '系统关闭', color: 'dark' },
                    'plugin_loaded': { icon: 'bi-plugin', label: '插件事件', color: 'secondary' },
                    'emotion_message_received': { icon: 'bi-emoji-smile', label: '表情消息', color: 'warning' }
                };

                const html = Object.entries(groups).map(([type, usersMap]) => {
                    const meta = typeLabels[type] || { icon: 'bi-lightning', label: type.replace(/_/g, ' '), color: 'secondary' };
                    // usersMap is { 'UserA': ['plugin1', 'plugin2'] }

                    const userItems = Object.entries(usersMap).map(([user, plugins], idx) => {
                        const collapseId = `collapse-listener-${idx}-${Math.abs(String(type).split('').reduce((sum, char) => sum + char.charCodeAt(0), 0))}`;
                        const pluginList = plugins.map(p => `<span class="badge bg-light text-secondary border me-1">${this.escapeHtml(p)}</span>`).join('');

                        return `
                            <div class="me-3 mb-3 d-inline-block text-start">
                                <button class="btn btn-outline-${meta.color} btn-sm rounded-pill px-3 position-relative"
                                        type="button"
                                        data-bs-toggle="collapse"
                                        data-bs-target="#${collapseId}"
                                        aria-expanded="false"
                                        style="border-style: dashed;">
                                    <i class="bi bi-person me-1"></i> ${this.escapeHtml(user)}
                                    <span class="position-absolute top-0 start-100 translate-middle badge rounded-pill bg-secondary" style="font-size: 0.6em;">
                                        ${plugins.length}
                                    </span>
                                </button>
                                <div class="collapse mt-2" id="${collapseId}">
                                    <div class="card card-body p-2 bg-light border-0 shadow-sm" style="min-width: 200px;">
                                        <small class="text-muted d-block mb-1">活跃插件：</small>
                                        <div>${pluginList}</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    }).join('');

                    if (!userItems) return ''; // Skip empty groups if any

                    return `
                        <div class="mb-4 pb-3 border-bottom">
                             <div class="d-flex align-items-center mb-3">
                                <div class="bg-${meta.color}-subtle text-${meta.color} rounded p-2 me-3 d-flex align-items-center justify-content-center">
                                    <i class="bi ${meta.icon} fs-5"></i>
                                </div>
                                <h6 class="mb-0 fw-bold text-dark">${this.escapeHtml(meta.label)}</h6>
                             </div>
                             <div class="d-flex flex-wrap ps-5">
                                ${userItems}
                             </div>
                        </div>
                    `;
                }).join('');

                listenersContainer.innerHTML = html || '<div class="text-muted ps-4">没有活跃的用户监听。</div>';
            }
        }
    },

    updateMetric(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    },

    renderPluginsList(plugins) {
        const capabilities = Array.isArray(plugins)
            ? plugins
            : Object.entries(plugins || {}).map(([id, info]) => ({ id, ...info }));
        return this.renderCapabilitiesList(capabilities);
    },

    renderAutomationWorkbench(capabilities, routing, state = {}) {
        const container = document.getElementById('pluginsList');
        if (!container) return;

        const view = state.view === 'routes' ? 'routes' : 'library';
        const summary = routing?.summary || {};
        const eventTypes = routing?.event_types || [];
        const selectedEvent = eventTypes.some(item => item.id === state.selectedEvent)
            ? state.selectedEvent
            : (eventTypes[0]?.id || '');
        const selectedMeta = eventTypes.find(item => item.id === selectedEvent) || {};
        const routeMode = state.routeMode === 'detail' ? 'detail' : 'sort';
        const chats = routing?.chats || [];
        const context = routing?.context || {};
        const selectedChatId = context.chat_id ?? state.selectedChatId ?? null;
        const isChatPreview = selectedChatId !== null && selectedChatId !== undefined;
        const chatOptions = chats.map(chat => `
            <option value="${Number(chat.id)}" ${Number(chat.id) === Number(selectedChatId) ? 'selected' : ''}>
                ${this.escapeHtml(chat.display_name || chat.chat_name)}${chat.is_group ? ' · 群聊' : ' · 私聊'}
            </option>
        `).join('');
        const workbenchHead = `
            <section class="automation-command fade-in">
                <div class="automation-view-switch" role="tablist" aria-label="插件视图">
                    <button type="button" class="automation-view-button ${view === 'library' ? 'active' : ''}" data-automation-view="library" role="tab" aria-selected="${view === 'library'}">
                        <i class="bi bi-grid"></i><span>插件库</span>
                    </button>
                    <button type="button" class="automation-view-button ${view === 'routes' ? 'active' : ''}" data-automation-view="routes" role="tab" aria-selected="${view === 'routes'}">
                        <i class="bi bi-diagram-3"></i><span>执行顺序</span>
                    </button>
                </div>
                ${view === 'routes' ? `
                    <div class="automation-command-title">
                        <span><i class="bi bi-globe2"></i>全局规则 · 各消息类型独立排序</span>
                        <span class="is-ok"><i class="bi bi-check2"></i>${Number(summary.listener_count || 0)} 个节点已纳入中央顺序表</span>
                    </div>
                    <div class="automation-command-context">
                        <div class="automation-route-mode-switch" role="tablist" aria-label="执行顺序显示模式">
                            <button type="button" class="automation-route-mode-button ${routeMode === 'sort' ? 'active' : ''}" data-route-mode="sort" role="tab" aria-selected="${routeMode === 'sort'}">
                                <i class="bi bi-list-ol"></i><span>紧凑排序</span>
                            </button>
                            <button type="button" class="automation-route-mode-button ${routeMode === 'detail' ? 'active' : ''}" data-route-mode="detail" role="tab" aria-selected="${routeMode === 'detail'}">
                                <i class="bi bi-card-text"></i><span>查看详情</span>
                            </button>
                        </div>
                        <label class="automation-chat-control" title="只读查看该聊天会经过哪些全局节点">
                            <i class="bi bi-eye"></i>
                            <select class="form-select form-select-sm" id="automationChatPreview" aria-label="聊天执行顺序只读预览">
                                <option value="">全部聊天 · 全局结构</option>
                                ${chatOptions}
                            </select>
                        </label>
                    </div>
                ` : ''}
            </section>
        `;

        if (view === 'library') {
            container.innerHTML = `${workbenchHead}<div id="automationCapabilityLibrary"></div>`;
            container.querySelectorAll('[data-automation-view]').forEach(button => {
                button.addEventListener('click', () => App.setAutomationView(button.dataset.automationView));
            });
            this.renderCapabilitiesList(capabilities, 'automationCapabilityLibrary');
            return;
        }

        const eventButtons = eventTypes.map(event => `
            <button type="button" class="automation-event-button ${event.id === selectedEvent ? 'active' : ''}" data-event-type="${this.escapeHtml(event.id)}">
                <span class="automation-event-icon"><i class="bi ${this.escapeHtml(event.icon)}"></i></span>
                <span class="automation-event-copy"><strong>${this.escapeHtml(event.label)}</strong><small>${isChatPreview ? `${Number(event.eligible_count)} / ${Number(event.listener_count)} 个会经过` : `${Number(event.listener_count)} 个执行节点`}</small></span>
                ${event.blocker_count ? `<span class="automation-event-count is-blocking" title="${Number(event.blocker_count)} 个可截断节点"><i class="bi bi-sign-stop"></i>${Number(event.blocker_count)}</span>` : `<span class="automation-event-count">${Number(event.listener_count)}</span>`}
            </button>
        `).join('');

        const liveItems = routing?.routes?.[selectedEvent] || [];
        const liveKeys = liveItems.map(item => item.listener_key);
        const draftKeys = state.draftEvent === selectedEvent && Array.isArray(state.draftKeys)
            ? state.draftKeys.filter(key => liveKeys.includes(key))
            : liveKeys;
        const missingKeys = liveKeys.filter(key => !draftKeys.includes(key));
        const orderedItems = [...draftKeys, ...missingKeys]
            .map(key => liveItems.find(item => item.listener_key === key))
            .filter(Boolean);

        const detailRouteSteps = orderedItems.map((item, index) => {
            const eligible = Boolean(item.eligible);
            const canBlock = eligible && Boolean(item.can_block);
            const statusLabel = item.status === 'disabled' ? '已停用' : '需检查';
            const exceptionalStatus = item.status !== 'running';
            const stateClass = !eligible ? 'is-skipped' : '';
            const trigger = item.trigger || {};
            const editable = Array.isArray(trigger.editable) ? trigger.editable : [];
            const triggerValues = editable.map(field => {
                const rawValue = Array.isArray(field.value) ? field.value.join('、') : String(field.value ?? '');
                return `<span class="automation-trigger-value"><strong>${this.escapeHtml(field.title)}</strong>${this.escapeHtml(rawValue || '未设置')}</span>`;
            }).join('');
            const conditions = (trigger.conditions || []).map(condition => `<li>${this.escapeHtml(condition)}</li>`).join('');
            const previewLabel = isChatPreview
                ? (eligible ? '该聊天已启用' : (item.reason || '该聊天不会经过'))
                : (item.scope_summary || '全局');
            const propagationLabel = item.propagation === 'observe'
                ? '<span><i class="bi bi-eye"></i>只观察，不拦截</span>'
                : canBlock
                    ? '<span class="is-blocker"><i class="bi bi-sign-stop"></i>命中并消费后结束</span>'
                    : '<span><i class="bi bi-arrow-down"></i>命中后继续</span>';
            const step = `
                <div class="automation-route-step ${stateClass}" data-listener-key="${this.escapeHtml(item.listener_key)}">
                    <article class="automation-route-card is-detail ${canBlock ? 'can-block' : ''}">
                        <div class="automation-detail-marker" aria-hidden="true"><i class="bi bi-card-text"></i></div>
                        <div class="automation-route-rank">${String(index + 1).padStart(2, '0')}</div>
                        <div class="automation-route-icon"><i class="bi ${this.escapeHtml(item.icon || 'bi-puzzle')}"></i></div>
                        <div class="automation-route-main">
                            <div class="automation-route-title-row">
                                <h3>${this.escapeHtml(item.display_name || item.plugin_name)}</h3>
                                ${exceptionalStatus ? `<span class="automation-route-status is-${this.escapeHtml(item.status || 'unknown')}">${statusLabel}</span>` : ''}
                            </div>
                            <div class="automation-route-id">${this.escapeHtml(item.listener_title || item.plugin_name)} · ${this.escapeHtml(item.handler_name || item.listener_key)}</div>
                            <div class="automation-route-trigger">
                                <div class="automation-trigger-head">
                                    <span><i class="bi bi-lightning-charge"></i>触发条件</span>
                                    ${editable.length ? `<button type="button" class="automation-trigger-button"><i class="bi bi-pencil-square"></i>编辑全局触发</button>` : '<small><i class="bi bi-lock"></i>插件固定</small>'}
                                </div>
                                <p>${this.escapeHtml(trigger.summary || '由插件规则判断')}</p>
                                ${triggerValues ? `<div class="automation-trigger-values">${triggerValues}</div>` : ''}
                                ${conditions ? `<ul>${conditions}</ul>` : ''}
                            </div>
                            <div class="automation-route-tags">
                                <span class="${eligible ? 'is-eligible' : 'is-muted'}"><i class="bi ${eligible ? 'bi-people' : 'bi-slash-circle'}"></i>${this.escapeHtml(previewLabel)}</span>
                                ${propagationLabel}
                            </div>
                        </div>
                        <div class="automation-route-actions">
                            <button type="button" class="automation-config-button" title="打开全部插件设置"><i class="bi bi-sliders"></i></button>
                        </div>
                    </article>
                    ${index < orderedItems.length - 1 ? (canBlock ? `
                        <div class="automation-route-branch">
                            <span class="continues"><i class="bi bi-arrow-down"></i>未命中 / 未消费 → 下一插件</span>
                            <span class="stops"><i class="bi bi-sign-stop"></i>命中并消费 → 结束</span>
                        </div>
                    ` : '<div class="automation-route-connector"><i class="bi bi-arrow-down"></i></div>') : ''}
                </div>
            `;
            return step;
        }).join('');

        const compactRouteSteps = orderedItems.map((item, index) => {
            const eligible = Boolean(item.eligible);
            const stateClass = !eligible ? 'is-skipped' : '';
            const trigger = item.trigger || {};
            const triggerSummary = trigger.summary || '由插件规则判断';
            const previewLabel = isChatPreview
                ? (eligible ? '该聊天已启用' : (item.reason || '该聊天不会经过'))
                : (item.status === 'running' ? '运行中' : '需检查');
            const outcome = item.propagation === 'observe'
                ? '<span class="automation-compact-outcome is-observe"><i class="bi bi-eye"></i>只观察</span>'
                : item.propagation === 'stop_on_consumed'
                    ? '<span class="automation-compact-outcome is-stop"><i class="bi bi-sign-stop"></i>消费后结束</span>'
                    : '<span class="automation-compact-outcome is-continue"><i class="bi bi-arrow-down"></i>继续传递</span>';
            return `
                <div class="automation-route-step automation-compact-step ${stateClass}" data-listener-key="${this.escapeHtml(item.listener_key)}">
                    <article class="automation-compact-row ${item.can_block ? 'can-block' : ''}">
                        <div class="automation-drag-handle" title="按住拖动调整顺序" aria-label="按住拖动调整顺序"><i class="bi bi-grip-vertical"></i></div>
                        <div class="automation-compact-rank" aria-label="当前顺序第 ${index + 1} 位">${String(index + 1).padStart(2, '0')}</div>
                        <div class="automation-compact-icon"><i class="bi ${this.escapeHtml(item.icon || 'bi-puzzle')}"></i></div>
                        <div class="automation-compact-identity">
                            <strong>${this.escapeHtml(item.display_name || item.plugin_name)}</strong>
                            <small>${this.escapeHtml(item.listener_title || item.handler_name || item.plugin_name)}</small>
                        </div>
                        <div class="automation-compact-trigger" title="${this.escapeHtml(triggerSummary)}">
                            <i class="bi bi-lightning-charge"></i><span>${this.escapeHtml(triggerSummary)}</span>
                        </div>
                        <span class="automation-compact-preview ${eligible ? 'is-enabled' : 'is-skipped'}" title="${this.escapeHtml(previewLabel)}">
                            <i class="bi ${eligible ? 'bi-check-circle' : 'bi-slash-circle'}"></i>${this.escapeHtml(previewLabel)}
                        </span>
                        ${outcome}
                    </article>
                </div>
            `;
        }).join('');
        const routeSteps = routeMode === 'sort' ? compactRouteSteps : detailRouteSteps;
        const dirtyBar = state.dirty ? `
            <div class="automation-save-bar" role="status">
                <div><span class="automation-unsaved-dot"></span><strong>顺序尚未应用</strong><small>调整结果确认后才会写入全局顺序</small></div>
                <div class="automation-save-actions">
                    <button type="button" class="btn btn-surface btn-sm" id="automationUndoOrder"><i class="bi bi-arrow-counterclockwise me-1"></i>撤销</button>
                    <button type="button" class="btn btn-primary btn-sm" id="automationSaveOrder" ${state.saving ? 'disabled' : ''}>
                        ${state.saving ? '<span class="spinner-border spinner-border-sm me-1"></span>' : '<i class="bi bi-check2 me-1"></i>'}应用新顺序
                    </button>
                </div>
            </div>
        ` : '';

        container.innerHTML = `
            ${workbenchHead}
            <div class="automation-workbench-grid">
                <aside class="automation-event-rail">
                    <div class="automation-rail-heading"><span>消息通道</span><small>各自独立排序</small></div>
                    <div class="automation-event-list">${eventButtons || '<div class="text-muted small p-3">暂无消息监听器</div>'}</div>
                </aside>
                <main class="automation-route-panel">
                    <div class="automation-route-panel-head">
                        <div>
                            <h2><i class="bi ${this.escapeHtml(selectedMeta.icon || 'bi-diagram-3')}"></i>${this.escapeHtml(selectedMeta.label || '消息')}处理顺序</h2>
                            <span class="automation-route-count">该通道共 ${Number(selectedMeta.listener_count || 0)} 个节点 · 顺序对所有聊天生效</span>
                        </div>
                        <div class="automation-route-panel-actions">
                            ${routeMode === 'sort' ? `
                                <span class="automation-sort-hint"><i class="bi bi-grip-vertical"></i>拖动左侧把手调整顺序</span>
                            ` : `
                                <div class="automation-route-legend">
                                    <span><i class="bi bi-arrow-down is-pass"></i>放行</span>
                                    <span><i class="bi bi-sign-stop is-block"></i>截断</span>
                                    <span><i class="bi bi-slash-circle is-skip"></i>跳过</span>
                                </div>
                            `}
                        </div>
                    </div>
                    <div class="automation-preview-banner ${isChatPreview ? 'is-chat' : ''}">
                        <i class="bi ${isChatPreview ? 'bi-eye' : 'bi-globe2'}"></i>
                        ${isChatPreview
                            ? `<span><strong>只读预览：${this.escapeHtml(context.chat_display_name || context.chat_name || '当前聊天')}</strong> · ${Number(selectedMeta.eligible_count || 0)} / ${Number(selectedMeta.listener_count || 0)} 个节点会经过；灰色行表示该聊天未启用或不适用。调整仍会修改全局顺序。</span>`
                            : '<span><strong>全局结构</strong> · 选择顶部聊天可查看该聊天的插件开关和实际经过顺序。</span>'}
                    </div>
                    <div class="automation-route-list" id="automationRouteList">
                        ${routeSteps || '<div class="empty-state-panel"><i class="bi bi-diagram-3"></i><h3>暂无执行节点</h3><p>该消息类型目前没有插件监听。</p></div>'}
                    </div>
                </main>
            </div>
            ${dirtyBar}
        `;

        container.querySelectorAll('[data-automation-view]').forEach(button => {
            button.addEventListener('click', () => App.setAutomationView(button.dataset.automationView));
        });
        container.querySelectorAll('[data-route-mode]').forEach(button => {
            button.addEventListener('click', () => App.setAutomationRouteMode(button.dataset.routeMode));
        });
        document.getElementById('automationChatPreview')?.addEventListener('change', event => {
            App.selectAutomationChat(event.target.value);
        });
        container.querySelectorAll('.automation-event-button').forEach(button => {
            button.addEventListener('click', () => App.selectAutomationEvent(button.dataset.eventType));
        });
        document.getElementById('automationUndoOrder')?.addEventListener('click', () => App.undoAutomationOrder());
        document.getElementById('automationSaveOrder')?.addEventListener('click', () => App.saveAutomationOrder());

        const routeList = document.getElementById('automationRouteList');
        if (!routeList) return;
        routeList.querySelectorAll('.automation-route-step').forEach(step => {
            const key = step.dataset.listenerKey;
            step.querySelector('.automation-config-button')?.addEventListener('click', () => {
                const item = liveItems.find(candidate => candidate.listener_key === key);
                if (item) App.showPluginSettings(item.plugin_name);
            });
            step.querySelector('.automation-trigger-button')?.addEventListener('click', () => {
                const item = liveItems.find(candidate => candidate.listener_key === key);
                if (item) App.showPluginSettings(item.plugin_name, { focusGroup: 'trigger' });
            });
            if (routeMode !== 'sort') return;
            const handle = step.querySelector('.automation-drag-handle');
            handle?.addEventListener('pointerdown', event => {
                if (event.button !== 0 || event.isPrimary === false) return;
                event.preventDefault();

                const pointerId = event.pointerId;
                const sourceRect = step.getBoundingClientRect();
                const pointerOffsetY = event.clientY - sourceRect.top;
                const originalKeys = [...routeList.querySelectorAll('.automation-route-step')]
                    .map(item => item.dataset.listenerKey);
                const ghost = step.querySelector('.automation-compact-row')?.cloneNode(true);
                if (!ghost) return;

                ghost.classList.add('automation-drag-ghost');
                ghost.style.width = `${sourceRect.width}px`;
                ghost.style.left = `${sourceRect.left}px`;
                ghost.style.top = `${sourceRect.top}px`;
                document.body.appendChild(ghost);

                step.classList.add('is-dragging');
                routeList.classList.add('is-sorting');
                document.body.classList.add('automation-pointer-sorting');
                try {
                    handle.setPointerCapture(pointerId);
                } catch (_) {
                    // Pointer capture is an enhancement; document listeners keep sorting functional.
                }

                let lastPointerY = event.clientY;
                let scrollFrame = null;

                const placeAtPointer = clientY => {
                    const siblings = [...routeList.querySelectorAll('.automation-route-step:not(.is-dragging)')];
                    const before = siblings.find(item => {
                        const rect = item.getBoundingClientRect();
                        return clientY < rect.top + rect.height / 2;
                    });
                    if (before) {
                        routeList.insertBefore(step, before);
                    } else {
                        routeList.appendChild(step);
                    }
                };

                const updateGhost = clientY => {
                    const top = Math.max(
                        8,
                        Math.min(window.innerHeight - sourceRect.height - 8, clientY - pointerOffsetY)
                    );
                    ghost.style.top = `${top}px`;
                };

                const autoScroll = () => {
                    const edge = Math.min(120, window.innerHeight * 0.16);
                    const distanceTop = lastPointerY;
                    const distanceBottom = window.innerHeight - lastPointerY;
                    let speed = 0;
                    if (distanceTop < edge) {
                        speed = -Math.max(5, Math.round((edge - distanceTop) * 0.24));
                    } else if (distanceBottom < edge) {
                        speed = Math.max(5, Math.round((edge - distanceBottom) * 0.24));
                    }
                    if (speed) {
                        window.scrollBy(0, speed);
                        placeAtPointer(lastPointerY);
                    }
                    scrollFrame = window.requestAnimationFrame(autoScroll);
                };

                const onPointerMove = moveEvent => {
                    if (moveEvent.pointerId !== pointerId) return;
                    moveEvent.preventDefault();
                    lastPointerY = moveEvent.clientY;
                    updateGhost(lastPointerY);
                    placeAtPointer(lastPointerY);
                };

                const restoreOriginalOrder = () => {
                    const byKey = new Map(
                        [...routeList.querySelectorAll('.automation-route-step')]
                            .map(item => [item.dataset.listenerKey, item])
                    );
                    originalKeys.forEach(originalKey => {
                        const item = byKey.get(originalKey);
                        if (item) routeList.appendChild(item);
                    });
                };

                const finish = commit => {
                    document.removeEventListener('pointermove', onPointerMove);
                    document.removeEventListener('pointerup', onPointerUp);
                    document.removeEventListener('pointercancel', onPointerCancel);
                    document.removeEventListener('keydown', onKeyDown);
                    if (scrollFrame !== null) window.cancelAnimationFrame(scrollFrame);
                    try {
                        if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
                    } catch (_) {
                        // The browser may already have released capture.
                    }
                    if (!commit) restoreOriginalOrder();
                    ghost.remove();
                    step.classList.remove('is-dragging');
                    routeList.classList.remove('is-sorting');
                    document.body.classList.remove('automation-pointer-sorting');
                    App.captureAutomationOrder();
                };

                const onPointerUp = upEvent => {
                    if (upEvent.pointerId === pointerId) finish(true);
                };
                const onPointerCancel = cancelEvent => {
                    if (cancelEvent.pointerId === pointerId) finish(false);
                };
                const onKeyDown = keyEvent => {
                    if (keyEvent.key !== 'Escape') return;
                    keyEvent.preventDefault();
                    finish(false);
                };

                document.addEventListener('pointermove', onPointerMove, { passive: false });
                document.addEventListener('pointerup', onPointerUp);
                document.addEventListener('pointercancel', onPointerCancel);
                document.addEventListener('keydown', onKeyDown);
                scrollFrame = window.requestAnimationFrame(autoScroll);
            });
        });
    },

    renderCapabilitiesList(capabilities, containerId = 'pluginsList') {
        const container = document.getElementById(containerId);
        if (!container) return;

        const automations = (capabilities || []).filter(item => !item.featured);
        if (automations.length === 0) {
            container.innerHTML = '<div class="empty-state-panel"><i class="bi bi-puzzle"></i><h3>暂无插件能力</h3><p>发现的插件会显示在这里。</p></div>';
            return;
        }

        const categoryFilters = [...new Map(automations.map(item => [item.category, item.category_label])).entries()];
        const cards = automations.map(info => {
            const name = info.id;
            const isEnabled = info.enabled;
            const statusLabel = info.status === 'running' ? '运行中' : info.status === 'disabled' ? '已停用' : '需检查';
            const statusClass = info.status === 'running' ? 'is-running' : info.status === 'disabled' ? 'is-disabled' : 'is-error';

            return `
                <article class="capability-card" data-capability-id="${this.escapeHtml(name)}" data-category="${this.escapeHtml(info.category)}" data-search="${this.escapeHtml(`${info.display_name} ${info.internal_name} ${info.description}`.toLowerCase())}">
                    <div class="capability-card-head">
                        <div class="capability-icon"><i class="bi ${this.escapeHtml(info.icon || 'bi-lightning-charge')}"></i></div>
                        <div class="capability-heading">
                            <div class="d-flex align-items-center gap-2 flex-wrap">
                                <h3>${this.escapeHtml(info.display_name || name)}</h3>
                                <span class="capability-status ${statusClass}"><span></span>${statusLabel}</span>
                            </div>
                            <div class="capability-internal-id">${this.escapeHtml(name)} · v${this.escapeHtml(info.version || '1.0')}</div>
                        </div>
                        <div class="form-check form-switch capability-enable">
                            <input class="form-check-input capability-toggle" type="checkbox" role="switch"
                                ${isEnabled ? 'checked' : ''} aria-label="启用 ${this.escapeHtml(info.display_name || name)}">
                        </div>
                    </div>
                    <p class="capability-description">${this.escapeHtml(info.description || '暂无功能说明')}</p>
                    <div class="capability-metrics">
                        <span><strong>${Number(info.assigned_chat_count || 0)}</strong> 个聊天</span>
                        <span><strong>${Number(info.settings_count || 0)}</strong> 项设置</span>
                        <span>${this.escapeHtml(info.category_label || '其他')}</span>
                    </div>
                    <div class="capability-actions">
                        <button class="btn btn-primary btn-sm capability-configure" ${info.configurable ? '' : 'disabled'}>
                            <i class="bi bi-sliders me-1"></i>配置
                        </button>
                        <button class="btn btn-light border btn-sm capability-assign">
                            <i class="bi bi-chat-square-text me-1"></i>分配聊天
                        </button>
                        <div class="dropdown ms-auto">
                            <button class="btn btn-light border btn-sm" data-bs-toggle="dropdown" aria-label="更多操作"><i class="bi bi-three-dots"></i></button>
                            <ul class="dropdown-menu dropdown-menu-end">
                                <li><button class="dropdown-item capability-reload"><i class="bi bi-arrow-clockwise me-2"></i>重新加载</button></li>
                                <li><button class="dropdown-item capability-details"><i class="bi bi-code-square me-2"></i>开发者详情</button></li>
                            </ul>
                        </div>
                    </div>
                </article>
            `;
        }).join('');

        const filters = categoryFilters.map(([id, label]) => `
            <button type="button" class="capability-filter" data-capability-filter="${this.escapeHtml(id)}">${this.escapeHtml(label)}</button>
        `).join('');

        const newHtml = `
            <div class="capability-toolbar">
                <div class="capability-search"><i class="bi bi-search"></i><input id="capabilitySearch" type="search" placeholder="搜索插件"></div>
                <div class="capability-filters"><button type="button" class="capability-filter active" data-capability-filter="all">全部</button>${filters}</div>
            </div>
            <div class="capability-grid" id="capabilityGrid">${cards}</div>
        `;

        container.innerHTML = newHtml;

        container.querySelectorAll('.capability-card').forEach(card => {
            const capabilityId = card.dataset.capabilityId;
            card.querySelector('.capability-toggle')?.addEventListener('change', event => {
                App.togglePlugin(capabilityId, event.target.checked);
            });
            card.querySelector('.capability-configure')?.addEventListener('click', () => App.showPluginSettings(capabilityId));
            card.querySelector('.capability-assign')?.addEventListener('click', () => UI.switchTab('users'));
            card.querySelector('.capability-reload')?.addEventListener('click', () => App.reloadPlugin(capabilityId));
            card.querySelector('.capability-details')?.addEventListener('click', () => App.showPluginDetails(capabilityId));
        });

        const applyFilters = () => {
            const query = (document.getElementById('capabilitySearch')?.value || '').trim().toLowerCase();
            const active = container.querySelector('.capability-filter.active')?.dataset.capabilityFilter || 'all';
            container.querySelectorAll('.capability-card').forEach(card => {
                const categoryMatch = active === 'all' || card.dataset.category === active;
                const searchMatch = !query || (card.dataset.search || '').includes(query);
                card.classList.toggle('d-none', !categoryMatch || !searchMatch);
            });
        };
        document.getElementById('capabilitySearch')?.addEventListener('input', this.debounce(applyFilters, 120));
        container.querySelectorAll('.capability-filter').forEach(button => {
            button.addEventListener('click', () => {
                container.querySelectorAll('.capability-filter').forEach(item => item.classList.remove('active'));
                button.classList.add('active');
                applyFilters();
            });
        });
    },

    renderUsersList(users) {
        const list = document.getElementById('usersList');
        if (!list) return;

        if (!users || users.length === 0) {
            list.innerHTML = `
                <div class="text-center py-5 text-muted">
                    <i class="bi bi-people opacity-25" style="font-size: 2rem;"></i>
                    <p class="mt-2 small">暂无监听中的聊天或用户。<br>请点击上方按钮添加。</p>
                </div>
            `;
            return;
        }

        list.innerHTML = users.map(u => {
            const userId = Number(u.id) || null;
            const isActive = u.is_listening;
            const listeningEnabled = u.listening_enabled !== false;
            const isConfigured = u.has_permission_config;
            const isSelected = window.App.currentThreadName === u.chat_name;
            const activeClass = isSelected ? 'active' : '';
            const chatTypeIcon = u.is_group ? '👥' : '👤';
            const chatTypeLabel = u.is_group ? '群聊' : '私聊';
            const blacklistCount = (() => {
                if (!u.sender_blacklist) return 0;
                try {
                    const parsed = JSON.parse(u.sender_blacklist);
                    return Array.isArray(parsed) ? parsed.map(item => String(item || '').trim()).filter(Boolean).length : 0;
                } catch (e) {
                    return String(u.sender_blacklist).split(/\r?\n/).map(item => item.trim()).filter(Boolean).length;
                }
            })();

            // Safe encoding for attribute
            const safeChatName = this.escapeHtml(u.chat_name);
            const displayName = this.escapeHtml(u.remark || u.chat_name);

            return `
                <div class="list-group-item list-group-item-action py-3 ${activeClass}"
                   role="button" tabindex="0" data-chat-select
                   aria-current="${isSelected ? 'true' : 'false'}"
                   data-user-id="${userId || ''}" data-chatname="${safeChatName}">
                    <div class="d-flex justify-content-between align-items-center">
                        <div class="d-flex align-items-start gap-2 flex-grow-1">
                            <span class="fs-5" title="${chatTypeLabel}">${chatTypeIcon}</span>
                            <div class="flex-grow-1">
                                <div class="fw-bold text-break">${displayName}</div>
                                ${u.remark ? `<div class="small text-muted text-break">${safeChatName}</div>` : ''}
                                <div class="mt-1">
                                    ${isActive
                                        ? '<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill" style="font-size: 0.7em;">监听中</span>'
                                        : listeningEnabled
                                            ? '<span class="badge bg-warning-subtle text-warning border border-warning-subtle rounded-pill" style="font-size: 0.7em;">等待恢复</span>'
                                            : '<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle rounded-pill" style="font-size: 0.7em;">已暂停</span>'}
                                    ${isConfigured ? '<span class="badge bg-primary-subtle text-primary border border-primary-subtle rounded-pill" style="font-size: 0.7em;">已管理</span>' : ''}
                                    ${blacklistCount > 0 ? `<span class="badge bg-danger-subtle text-danger border border-danger-subtle rounded-pill" style="font-size: 0.7em;">已屏蔽 ${blacklistCount} 人</span>` : ''}
                                </div>
                            </div>
                        </div>
                        <div class="d-flex gap-1 ms-2">
                            ${userId ? `
                                <button type="button" class="btn btn-link text-primary p-0"
                                    onclick="event.preventDefault(); event.stopPropagation(); App.openChatMemoryLibrary(${userId})"
                                    title="打开记忆库" aria-label="打开记忆库">
                                    <i class="bi bi-database-gear"></i>
                                </button>
                                <button type="button" class="btn btn-link text-secondary p-0"
                                    onclick="event.preventDefault(); event.stopPropagation(); App.showEditUserModal(${userId}, this.getAttribute('data-chatname'), ${!!u.is_group})"
                                    data-chatname="${safeChatName}"
                                    title="编辑用户信息">
                                    <i class="bi bi-pencil"></i>
                                </button>
                            ` : ''}
                            ${isActive ? `
                                <button class="btn btn-link text-warning p-0"
                                    onclick="event.stopPropagation(); App.removeListener(this.getAttribute('data-chatname'))"
                                    data-chatname="${safeChatName}"
                                    title="停止监听">
                                    <i class="bi bi-stop-circle"></i>
                                </button>
                            ` : userId ? `
                                <button class="btn btn-link text-success p-0"
                                    onclick="event.stopPropagation(); App.addListener(this.getAttribute('data-chatname'))"
                                    data-chatname="${safeChatName}"
                                    title="恢复监听">
                                    <i class="bi bi-play-circle"></i>
                                </button>
                            ` : ''}
                            ${userId ? `
                                <button class="btn btn-link text-danger p-0"
                                    onclick="event.stopPropagation(); App.deleteUser(${userId}, this.getAttribute('data-chatname'))"
                                    data-chatname="${safeChatName}"
                                    title="删除用户">
                                    <i class="bi bi-trash"></i>
                                </button>
                            ` : ''}
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        const selectChat = item => {
            const userId = Number(item.dataset.userId) || null;
            window.App.selectUser(item.dataset.chatname, userId);
        };
        list.querySelectorAll('[data-chat-select]').forEach(item => {
            item.addEventListener('click', event => {
                if (event.target.closest('button')) return;
                selectChat(item);
            });
            item.addEventListener('keydown', event => {
                if (event.target !== item || !['Enter', ' '].includes(event.key)) return;
                event.preventDefault();
                selectChat(item);
            });
        });
    },

    setActiveManagedChat(chatName) {
        const selectedName = String(chatName || '');
        document.querySelectorAll('#usersList [data-chat-select]').forEach(item => {
            const isSelected = item.dataset.chatname === selectedName;
            item.classList.toggle('active', isSelected);
            item.setAttribute('aria-current', isSelected ? 'true' : 'false');
        });
    },

    renderManagedChatPending(chatName) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.setAttribute('aria-busy', 'true');
        container.innerHTML = `
            <div class="card-body d-flex flex-column align-items-center justify-content-center text-muted">
                <span class="spinner-border spinner-border-sm mb-3" aria-hidden="true"></span>
                <h6 class="mb-1">${this.escapeHtml(chatName || '聊天')}</h6>
                <p class="small mb-0">正在加载聊天配置…</p>
            </div>
        `;
    },

    renderManagedChatError(chatName) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.removeAttribute('aria-busy');
        container.innerHTML = `
            <div class="card-body d-flex flex-column align-items-center justify-content-center text-muted">
                <i class="bi bi-exclamation-circle text-danger fs-3 mb-3" aria-hidden="true"></i>
                <h6 class="mb-1">${this.escapeHtml(chatName || '聊天')}</h6>
                <p class="small mb-0">聊天配置加载失败，请重试。</p>
            </div>
        `;
    },

    getSystemSettingsGroupFromPath() {
        const path = this.normalizePath(window.location.pathname);
        return {
            '/system/providers': 'integrations',
            '/system/integrations': 'integrations',
            '/system/runtime': 'runtime',
            '/system/developer': 'developer',
            '/system/operations': 'operations',
            '/system/backups': 'backups'
        }[path] || 'identity';
    },

    renderSystemSettings(consoleData) {
        const container = document.getElementById('settings');
        if (!container) return;
        const groups = consoleData.groups || [];
        const primaryGroups = groups.filter(group => group.id !== 'developer');
        const extensionGroups = groups.filter(group => group.id === 'developer');
        const platformGroups = [
            ...primaryGroups,
            {
                id: 'operations', title: '运行状态', icon: 'bi-heart-pulse',
                description: '统一任务、插件状态和组件健康'
            },
            {
                id: 'backups', title: '备份与迁移', icon: 'bi-shield-check',
                description: '状态备份、完整迁移和安全恢复'
            },
            ...extensionGroups
        ];
        const identity = consoleData.identity || {};
        const requested = this.getSystemSettingsGroupFromPath();
        const activeId = platformGroups.some(group => group.id === requested) ? requested : groups[0]?.id;
        const navigation = platformGroups.map(group => `
            <button type="button" class="system-settings-nav-item ${group.id === activeId ? 'active' : ''}" data-system-group="${this.escapeHtml(group.id)}">
                <i class="bi ${this.escapeHtml(group.icon)}"></i>
                <span><strong>${this.escapeHtml(group.title)}</strong></span>
            </button>
        `).join('');
        const renderField = (field, group) => {
                const inputId = `system-setting-${field.key}`;
                const restart = field.requires_restart ? '<span class="system-restart-pill">需重启</span>' : '';
                let control = '';
                if (!field.editable) {
                    control = `<div class="system-setting-readonly"><i class="bi bi-info-circle"></i><span>${this.escapeHtml(field.readonly_text || '当前页面只展示状态')}</span></div>`;
                } else if (field.control === 'select') {
                    control = `
                        <select class="form-select system-setting-input" id="${inputId}" name="${this.escapeHtml(field.key)}" data-original="${this.escapeHtml(field.value || '')}" data-sensitive="false">
                            ${(field.options || []).map(option => `<option value="${this.escapeHtml(option.value)}" ${String(field.value) === String(option.value) ? 'selected' : ''}>${this.escapeHtml(option.label)}</option>`).join('')}
                        </select>`;
                } else {
                    const isSecret = field.sensitive;
                    const value = isSecret ? '' : (field.value ?? '');
                    const placeholder = isSecret
                        ? (field.configured ? '已配置—输入新值以替换' : '未配置')
                        : '';
                    control = `
                        <div class="system-setting-control">
                            <input type="${field.control === 'number' ? 'number' : isSecret ? 'password' : 'text'}" class="form-control system-setting-input"
                                id="${inputId}" name="${this.escapeHtml(field.key)}" value="${this.escapeHtml(String(value))}"
                                placeholder="${this.escapeHtml(placeholder)}" data-original="${this.escapeHtml(String(value))}" data-sensitive="${isSecret}">
                            ${isSecret ? `<button type="button" class="btn btn-light border" data-reveal-setting="${inputId}" aria-label="显示或隐藏 ${this.escapeHtml(field.title)}"><i class="bi bi-eye"></i></button>` : ''}
                        </div>`;
                }
                return `
                    <div class="system-setting-row">
                        <div class="system-setting-copy">
                            <div><label for="${inputId}">${this.escapeHtml(field.title)}</label>${restart}</div>
                            <p>${this.escapeHtml(field.description || '')}</p>
                            ${group.id === 'developer' ? `<div class="system-setting-meta"><code>${this.escapeHtml(field.key)}</code></div>` : ''}
                        </div>
                        <div>${control}</div>
                    </div>`;
        };
        const sections = groups.map(group => {
            const sectionNames = [];
            const fieldsBySection = {};
            (group.fields || []).forEach(field => {
                const name = field.section || '其他';
                if (!fieldsBySection[name]) {
                    sectionNames.push(name);
                    fieldsBySection[name] = [];
                }
                fieldsBySection[name].push(field);
            });
            const fields = sectionNames.map(name => `
                <div class="system-setting-subsection">
                    ${sectionNames.length > 1 ? `<div class="system-setting-subsection-title"><span>${this.escapeHtml(name)}</span></div>` : ''}
                    ${fieldsBySection[name].map(field => renderField(field, group)).join('')}
                </div>`).join('') || '<div class="system-settings-empty">当前没有此类设置。</div>';
            const editable = (group.fields || []).some(field => field.editable);
            const developerActions = group.id === 'developer' ? `
                <div class="system-developer-actions">
                    <div><strong>自定义配置</strong><small>仅在扩展文档明确要求时添加或导入。</small></div>
                    <button class="btn btn-sm btn-light border" onclick="UI.showAddSettingModal()"><i class="bi bi-plus-lg me-1"></i>添加自定义键</button>
                    <button class="btn btn-sm btn-light border" onclick="App.reloadSettingsFromEnv()"><i class="bi bi-arrow-clockwise me-1"></i>导入 .env 到数据库</button>
                </div>` : '';
            const description = group.id === 'identity'
                ? `已识别 ${Number(identity.detected_count || 0)} / ${Number(identity.group_count || 0)} 个群聊昵称；全局名称仅在没有聊天级名称时使用。`
                : group.description;
            const contextAction = group.id === 'identity'
                ? '<a class="btn btn-sm btn-light" href="/assistant/chats">各聊天身份<i class="bi bi-arrow-right ms-1"></i></a>'
                : '';
            return `
                <section class="system-settings-section ${group.id === activeId ? '' : 'd-none'}" data-system-section="${this.escapeHtml(group.id)}">
                    <div class="system-settings-section-head"><div><h3>${this.escapeHtml(group.title)}</h3><p>${this.escapeHtml(description)}</p></div><div class="system-settings-section-actions">${contextAction}${editable ? '<button class="btn btn-primary btn-sm" onclick="App.saveSettings()"><i class="bi bi-check-lg me-1"></i>保存</button>' : ''}</div></div>
                    <div class="system-settings-fields">${fields}</div>
                    ${developerActions}
                </section>`;
        }).join('');
        const platformSections = `
            <section class="system-settings-section ${activeId === 'operations' ? '' : 'd-none'}" data-system-section="operations">
                <div id="systemOperationsConsole" class="system-platform-console"><div class="loading-wrapper">正在读取运行状态…</div></div>
            </section>
            <section class="system-settings-section ${activeId === 'backups' ? '' : 'd-none'}" data-system-section="backups">
                <div id="systemBackupsConsole" class="system-platform-console"><div class="loading-wrapper">正在读取备份…</div></div>
            </section>`;

        container.innerHTML = `
            <div class="system-settings-shell">
                <aside class="system-settings-nav">${navigation}</aside>
                <main class="system-settings-main">${sections}${platformSections}</main>
            </div>`;
        container.dataset.activeSystemGroup = activeId || '';
        container.querySelectorAll('[data-system-group]').forEach(button => button.addEventListener('click', () => {
            this.switchSystemSettingsGroup(button.dataset.systemGroup);
        }));
        container.querySelectorAll('[data-reveal-setting]').forEach(button => button.addEventListener('click', () => {
            const input = document.getElementById(button.dataset.revealSetting);
            if (!input) return;
            input.type = input.type === 'password' ? 'text' : 'password';
            button.querySelector('i')?.classList.toggle('bi-eye');
            button.querySelector('i')?.classList.toggle('bi-eye-slash');
        }));
    },

    switchSystemSettingsGroup(groupId, options = {}) {
        const container = document.getElementById('settings');
        if (!container) return;
        container.dataset.activeSystemGroup = groupId;
        container.querySelectorAll('[data-system-group]').forEach(button => {
            button.classList.toggle('active', button.dataset.systemGroup === groupId);
        });
        container.querySelectorAll('[data-system-section]').forEach(section => {
            section.classList.toggle('d-none', section.dataset.systemSection !== groupId);
        });
        if (options.history !== false) {
            const paths = {
                identity: '/system', integrations: '/system/integrations',
                runtime: '/system/runtime', developer: '/system/developer',
                operations: '/system/operations', backups: '/system/backups'
            };
            const path = paths[groupId] || '/system';
            if (this.normalizePath(window.location.pathname) !== path) {
                window.history.pushState({ tab: 'settings', section: groupId }, '', path);
            }
        }
        if (groupId === 'operations') window.SystemOperations?.loadRuntime();
        if (groupId === 'backups') window.SystemOperations?.loadBackups();
    },

    togglePassword(id) {
        const input = document.getElementById(id);
        if (input) {
            input.type = input.type === 'password' ? 'text' : 'password';
        }
    },

    renderChatCapabilities(user, capabilities) {
        const container = document.getElementById('userPermissionsPanelContainer');
        if (!container) return;
        container.removeAttribute('aria-busy');

        const permissions = user.permissions || [];
        const current = new Set(permissions.map(item => item.plugin_name));
        const permissionByName = Object.fromEntries(
            permissions.filter(item => !item.plugin_name.endsWith('#push')).map(item => [item.plugin_name, item])
        );
        const chatMeta = (window.App._managedChats || []).find(item => item.chat_name === user.chat_name) || {};
        const enabledCount = (capabilities || []).filter(item => current.has(item.id)).length;
        const safeChatName = this.escapeHtml(user.chat_name || '');
        const userId = Number(user.id) || null;
        const formatList = (raw) => {
            if (!raw) return '';
            try {
                const parsed = JSON.parse(raw);
                return Array.isArray(parsed) ? parsed.join('\n') : String(raw);
            } catch (error) {
                return String(raw);
            }
        };
        const cards = [...(capabilities || [])]
            .sort((a, b) => Number(b.featured) - Number(a.featured) || a.category_order - b.category_order || a.display_name.localeCompare(b.display_name))
            .map(capability => {
                const id = capability.id;
                const permission = permissionByName[id] || {};
                const enabledForChat = current.has(id);
                const supportsPush = (capability.features || []).includes('push');
                const supportsMentionOption = id !== 'builtin_chatbot';
                const pushEnabled = current.has(`${id}#push`);
                const globallyAvailable = capability.enabled && capability.loaded;
                const icon = capability.icon || 'bi-lightning-charge';
                let specialContent = '';

                if (id === 'builtin_chatbot') {
                    const ignored = formatList(permission.ignored_senders);
                    const proactiveEnabled = Boolean(user.is_group && permission.proactive_enabled);
                    let memoryEnabled = false;
                    try { memoryEnabled = !!JSON.parse(permission.memory_profile || '{}').enabled; } catch (error) { /* legacy value */ }
                    const summary = [
                        proactiveEnabled ? '主动回复' : '',
                        permission.followup_enabled ? `连续对话 ${permission.followup_window_seconds || 60}s` : '',
                        memoryEnabled ? '独立记忆配置' : '继承全局记忆'
                    ].filter(Boolean).join(' · ');
                    specialContent = `
                        <input class="proactive-check d-none" type="checkbox" id="proactive-${id}" ${proactiveEnabled ? 'checked' : ''}>
                        <input class="followup-check d-none" type="checkbox" id="followup-enabled-${id}" ${permission.followup_enabled ? 'checked' : ''}>
                        <input type="hidden" id="followup-window-${id}" value="${Number(permission.followup_window_seconds || 60)}">
                        <input type="hidden" id="followup-merge-${id}" value="${Number(permission.followup_merge_seconds || 3)}">
                        <input type="hidden" id="followup-max-turns-${id}" value="${Number(permission.followup_max_turns || 3)}">
                        <textarea class="d-none memory-profile-input" id="memory-profile-${id}">${this.escapeHtml(permission.memory_profile || '')}</textarea>
                        <textarea class="d-none ignored-senders-input" id="ignored-senders-${id}">${this.escapeHtml(ignored)}</textarea>
                        <div class="chat-capability-special">
                            <div><strong>AI 助手会话配置</strong><small id="chatbot-settings-summary-${id}">${this.escapeHtml(summary || '继承全局行为')}</small></div>
                            <button type="button" class="btn btn-sm btn-primary chatbot-chat-configure" id="chatbot-settings-${id}"
                                ${enabledForChat && userId ? '' : 'disabled'}>配置</button>
                        </div>
                        <button type="button" class="chat-memory-override-link" data-plugin-id="${this.escapeHtml(id)}" ${enabledForChat ? '' : 'disabled'}>
                            <i class="bi bi-database-gear me-1"></i>高级记忆覆盖
                        </button>`;
                }

                return `
                    <article class="chat-capability-card ${enabledForChat ? 'selected' : ''} ${globallyAvailable ? '' : 'unavailable'}" data-capability-id="${this.escapeHtml(id)}">
                        <div class="chat-capability-main">
                            <div class="chat-capability-icon"><i class="bi ${this.escapeHtml(icon)}"></i></div>
                            <div class="chat-capability-copy">
                                <div class="chat-capability-title-row">
                                    <strong>${this.escapeHtml(capability.display_name || id)}</strong>
                                    ${capability.featured ? '<span class="chat-featured-pill">AI 助手</span>' : ''}
                                    ${!globallyAvailable ? '<span class="chat-unavailable-pill">全局未运行</span>' : ''}
                                </div>
                                <p>${this.escapeHtml(capability.description || '暂无功能说明')}</p>
                                <small>${this.escapeHtml(capability.category_label || '其他能力')}</small>
                            </div>
                            <div class="form-check form-switch chat-capability-toggle">
                                <input class="form-check-input permission-check" type="checkbox" value="${this.escapeHtml(id)}" id="perm-${this.escapeHtml(id)}"
                                    ${enabledForChat ? 'checked' : ''} ${globallyAvailable ? '' : 'disabled'} aria-label="在此聊天启用 ${this.escapeHtml(capability.display_name || id)}">
                            </div>
                        </div>
                        ${specialContent}
                        ${supportsMentionOption || supportsPush ? `<details class="chat-capability-advanced" ${permission.require_mention || pushEnabled ? 'open' : ''}>
                            <summary>触发与执行选项</summary>
                            <div class="chat-capability-options">
                                ${supportsMentionOption ? `<label><span><strong>需要 @Bot</strong><small>只有明确提及机器人时触发</small></span><input class="form-check-input mention-check" type="checkbox" id="mention-${this.escapeHtml(id)}" ${permission.require_mention ? 'checked' : ''} ${enabledForChat ? '' : 'disabled'}></label>` : ''}
                                ${supportsPush ? `<label><span><strong>允许后台推送</strong><small>计划任务可向此聊天发送结果</small></span><input class="form-check-input push-check" type="checkbox" id="push-${this.escapeHtml(id)}" ${pushEnabled ? 'checked' : ''} ${enabledForChat ? '' : 'disabled'}></label>` : `<input class="push-check d-none" type="checkbox" id="push-${this.escapeHtml(id)}" data-unsupported="true" disabled>`}
                            </div>
                        </details>` : ''}
                    </article>`;
            }).join('');

        container.innerHTML = `
            <div class="chat-detail-shell">
                <div class="chat-detail-header">
                    <div>
                        <div class="d-flex align-items-center gap-2 flex-wrap">
                            <h3>${safeChatName}</h3>
                            <span class="assistant-state-pill ${chatMeta.is_listening ? 'on' : 'off'}">${chatMeta.is_listening ? '正在监听' : '未监听'}</span>
                        </div>
                        <p>${user.is_group ? '群聊' : '私聊'} · 已启用 ${enabledCount} 项能力</p>
                    </div>
                    <div class="chat-detail-actions">
                        ${userId ? `<button class="btn btn-sm btn-light border" onclick="App.showEditUserModal(${userId}, this.dataset.chatname, ${!!user.is_group})" data-chatname="${safeChatName}"><i class="bi bi-pencil me-1"></i>基本信息</button>` : ''}
                        <button class="btn btn-sm btn-primary" onclick="App.saveUserPermissions(${userId || 'null'}, this.dataset.chatname)" data-chatname="${safeChatName}"><i class="bi bi-check-lg me-1"></i>保存能力</button>
                    </div>
                </div>
                <div class="chat-detail-notice"><i class="bi bi-info-circle"></i><span>这里只决定“此聊天能用什么”。各能力的全局行为在“AI 助手”或“插件”页管理。</span></div>
                <div class="chat-capability-grid">${cards || '<div class="assistant-empty-inline">尚未发现可用能力。</div>'}</div>
            </div>`;

        container.querySelectorAll('.permission-check').forEach(toggle => {
            const update = () => {
                const card = toggle.closest('.chat-capability-card');
                const enabled = toggle.checked;
                const available = !card.classList.contains('unavailable');
                card.classList.toggle('selected', enabled);
                card.querySelectorAll('.mention-check, .push-check').forEach(input => {
                    input.disabled = !enabled || !available || input.dataset.unsupported === 'true';
                    if (!enabled) input.checked = false;
                });
                card.querySelectorAll('.chat-capability-special button').forEach(button => button.disabled = !enabled || !available);
                const chatbotButton = card.querySelector('.chat-memory-override-link');
                if (chatbotButton) chatbotButton.disabled = !enabled || !available;
            };
            toggle.addEventListener('change', update);
            update();
        });
        container.querySelector('.chatbot-chat-configure')?.addEventListener('click', () => {
            if (userId) App.openAssistantChatEditorFromChats(userId);
        });
        container.querySelector('.chat-memory-override-link')?.addEventListener('click', event => {
            if (userId) App.showChatbotPermissionModal(event.currentTarget.dataset.pluginId, userId);
        });
    },

    renderLogs(content, searchKeyword) {
        const container = document.getElementById('logContent');
        if (!container) return Promise.resolve({ matchCount: 0 });

        if (!content) {
            container.innerHTML = '<div class="logs-placeholder"><i class="bi bi-inbox"></i><span>无日志内容</span></div>';
            return Promise.resolve({ matchCount: 0 });
        }

        const lines = content.split('\n');
        const keyword = String(searchKeyword || '').trim();
        const literalPattern = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        let matchCount = 0;

        const renderSyntax = segment => {
            let escaped = this.escapeHtml(segment);
            escaped = escaped.replace(
                /\[(ERROR|WARNING|INFO|DEBUG|CRITICAL|FATAL)\]/gi,
                '<span class="log-level log-level-$1">[$1]</span>'
            );
            escaped = escaped.replace(
                /(\[([a-zA-Z_][a-zA-Z0-9_.]*)\])/g,
                '<span class="log-module">$1</span>'
            );
            return escaped.replace(
                /(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{3})?)/g,
                '<span class="log-time">$1</span>'
            );
        };

        const renderHighlightedLine = line => {
            if (!literalPattern) return renderSyntax(line);
            const regex = new RegExp(literalPattern, 'gi');
            let cursor = 0;
            let html = '';
            let match;
            while ((match = regex.exec(line)) !== null) {
                html += renderSyntax(line.slice(cursor, match.index));
                const matchIndex = matchCount++;
                html += `<mark class="log-search-match" data-log-match-index="${matchIndex}">${renderSyntax(match[0])}</mark>`;
                cursor = regex.lastIndex;
            }
            return html + renderSyntax(line.slice(cursor));
        };

        const renderLine = (line, lineNo) => {
            let lineClass = '';

            // Detect log level for line coloring
            if (/\[ERROR\]|Traceback|Error:|exception/i.test(line)) {
                lineClass = 'log-error';
            } else if (/\[WARNING\]|WARNING:/i.test(line)) {
                lineClass = 'log-warn';
            } else if (/\[(CRITICAL|FATAL)\]/i.test(line)) {
                lineClass = 'log-critical';
            }

            const escaped = renderHighlightedLine(line);
            return `<div class="log-line ${lineClass}" data-line="${lineNo}">` +
                `<span class="log-ln">${lineNo}</span>` +
                `<span class="log-text">${escaped}</span>` +
                `</div>`;
        };

        // Save current scroll position to prevent jumping
        const prevScrollTop = container.scrollTop;
        const prevScrollHeight = container.scrollHeight;
        const isAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 10;

        let html = '';
        for (let index = 0; index < lines.length; index++) {
            html += renderLine(lines[index], index + 1);
        }

        container.innerHTML = html;

        // Restore scroll position if not following
        if (!isAtBottom) {
            // Adjust scroll position based on height difference if lines were added
            const heightDiff = container.scrollHeight - prevScrollHeight;
            if (heightDiff > 0) {
                 container.scrollTop = prevScrollTop + heightDiff;
            } else {
                 container.scrollTop = prevScrollTop;
            }
        }

        return Promise.resolve({ matchCount });
    },

    renderCapabilitySettingsForm(settings, capability = {}) {
        const groups = settings.groups || [];
        const editableFields = groups.flatMap(group => group.fields || []).filter(field => !field.deprecated);
        const hasBasicFields = editableFields.some(field => field.level === 'basic');
        const levelFilter = hasBasicFields ? 'basic' : 'all';
        const globalMemory = editableFields.find(field => field.key === 'memory_enabled');
        const inheritanceSummary = settings.capability_id === 'builtin_chatbot' && globalMemory ? `
            <div class="cap-settings-inheritance">
                <span class="cap-settings-inheritance-icon"><i class="bi bi-database-check"></i></span>
                <div><strong data-global-memory-summary>长期记忆全局默认：${globalMemory.value ? '开启' : '关闭'}</strong>
                    <small data-global-memory-help>聊天卡片上的“继承全局 · ${globalMemory.value ? '开启' : '关闭'}”就是来自这个设置。</small></div>
                <div class="cap-settings-inheritance-actions">
                    <label class="cap-settings-primary-toggle">
                        <span data-global-memory-toggle-label>${globalMemory.value ? '已开启' : '已关闭'}</span>
                        <input class="form-check-input" type="checkbox" data-global-memory-toggle ${globalMemory.value ? 'checked' : ''} aria-label="长期记忆全局总开关">
                    </label>
                    <button type="button" class="btn btn-sm btn-outline-primary" data-settings-jump="memory">详细设置</button>
                </div>
            </div>` : '';

        const nav = groups.map(group => {
            const visibleCount = (group.fields || []).filter(field => !field.deprecated).length;
            if (!visibleCount) return '';
            return `<button type="button" class="cap-settings-nav-item" data-settings-anchor="cap-settings-${this.escapeHtml(group.id)}">
                <span>${this.escapeHtml(group.title)}</span><small data-settings-visible-count>${visibleCount}</small>
            </button>`;
        }).join('');

        const sections = groups.map(group => {
            const fields = (group.fields || []).filter(field => !field.deprecated);
            if (!fields.length) return '';
            const fieldHtml = fields.map(field => this.renderCapabilitySettingsField(field)).join('');
            return `<section class="cap-settings-section" id="cap-settings-${this.escapeHtml(group.id)}">
                <header><div><h3>${this.escapeHtml(group.title)}</h3><p>${this.escapeHtml(group.description || '')}</p></div></header>
                <div class="cap-settings-fields">${fieldHtml}</div>
            </section>`;
        }).join('');

        return `
            <div class="cap-settings-shell" data-level-filter="${levelFilter}" data-capability-id="${this.escapeHtml(settings.capability_id || '')}">
                <aside class="cap-settings-aside">
                    <div class="cap-settings-capability">
                        <span class="capability-icon"><i class="bi ${this.escapeHtml(capability.icon || 'bi-sliders')}"></i></span>
                        <div><strong>${this.escapeHtml(capability.display_name || settings.capability_id)}</strong><small>${Number(settings.field_count || 0)} 项全局设置</small></div>
                    </div>
                    <nav class="cap-settings-nav">${nav}</nav>
                </aside>
                <main class="cap-settings-main">
                    <div class="cap-settings-toolbar">
                        <div class="cap-settings-search"><i class="bi bi-search"></i><input type="search" id="capabilitySettingsSearch" placeholder="搜索设置"></div>
                        <div class="btn-group btn-group-sm" role="group" aria-label="设置级别">
                            <button type="button" class="btn btn-outline-secondary ${levelFilter === 'basic' ? 'active' : ''}" data-settings-level="basic">常用</button>
                            <button type="button" class="btn btn-outline-secondary ${levelFilter === 'all' ? 'active' : ''}" data-settings-level="all">全部</button>
                        </div>
                    </div>
                    <div class="cap-settings-notice"><i class="bi bi-globe2"></i><span>${this.escapeHtml(settings.notice || '这里设置该能力对所有聊天的默认行为。')}</span></div>
                    ${inheritanceSummary}
                    <form id="capabilitySettingsForm">${sections}</form>
                    <div class="cap-settings-empty d-none" id="capabilitySettingsEmpty">没有匹配的设置</div>
                </main>
            </div>`;
    },

    renderCapabilitySettingsField(field) {
        const id = `cap-cfg-${field.key}`;
        const value = field.value ?? field.default ?? '';
        const common = `data-config-key="${this.escapeHtml(field.key)}" data-config-type="${this.escapeHtml(field.type)}" data-sensitive="${field.sensitive ? 'true' : 'false'}"`;
        const minimum = field.minimum !== null && field.minimum !== undefined ? ` min="${this.escapeHtml(String(field.minimum))}"` : '';
        const maximum = field.maximum !== null && field.maximum !== undefined ? ` max="${this.escapeHtml(String(field.maximum))}"` : '';
        const step = field.step !== null && field.step !== undefined ? ` step="${this.escapeHtml(String(field.step))}"` : (field.type === 'number' ? ' step="any"' : '');
        let control = '';

        if (!field.editable) {
            control = `<div class="cap-settings-readonly"><i class="bi bi-braces"></i><span>结构化配置将在专用编辑器中管理，当前已保护原值。</span></div>`;
        } else if (field.type === 'boolean') {
            control = `<div class="form-check form-switch modern-toggle mb-0"><input class="form-check-input" type="checkbox" id="${id}" ${common} ${value ? 'checked' : ''}></div>`;
        } else if ((field.options || []).length) {
            const options = field.options.map(option => `<option value="${this.escapeHtml(String(option.value))}" ${String(value) === String(option.value) ? 'selected' : ''}>${this.escapeHtml(String(option.label))}</option>`).join('');
            control = `<select class="form-select" id="${id}" ${common}>${options}</select>`;
        } else if (field.type === 'array') {
            const text = Array.isArray(value) ? value.join('\n') : String(value || '');
            control = `<textarea class="form-control" id="${id}" rows="4" ${common}>${this.escapeHtml(text)}</textarea><small>每行一项</small>`;
        } else if (field.type === 'integer' || field.type === 'number') {
            control = `<input class="form-control" type="number" id="${id}" value="${this.escapeHtml(String(value))}" ${common}${minimum}${maximum}${step}>`;
        } else if (field.control === 'textarea') {
            control = `<textarea class="form-control font-monospace cap-settings-prompt-editor" id="${id}" rows="12" ${common}>${this.escapeHtml(String(value))}</textarea>`;
        } else {
            const inputType = field.sensitive ? 'password' : 'text';
            const placeholder = field.sensitive && field.configured ? '已配置；留空保持不变' : (field.placeholder || '');
            control = `<input class="form-control" type="${inputType}" id="${id}" value="${field.sensitive ? '' : this.escapeHtml(String(value))}" placeholder="${this.escapeHtml(placeholder)}" ${common}>`;
        }

        return `<div class="cap-settings-field" data-settings-level-value="${this.escapeHtml(field.level || 'basic')}" data-settings-search-value="${this.escapeHtml(`${field.title} ${field.description} ${field.key}`.toLowerCase())}">
            <div class="cap-settings-label"><label for="${id}">${this.escapeHtml(field.title)}</label><p>${this.escapeHtml(field.description || '')}</p>${field.level === 'developer' ? `<code>${this.escapeHtml(field.key)}</code>` : ''}</div>
            <div class="cap-settings-control">${control}</div>
        </div>`;
    },

    bindCapabilitySettingsControls(modalElement) {
        const shell = modalElement.querySelector('.cap-settings-shell');
        if (!shell) return;
        const search = shell.querySelector('#capabilitySettingsSearch');
        const filterButtons = shell.querySelectorAll('[data-settings-level]');

        const apply = () => {
            const query = (search?.value || '').trim().toLowerCase();
            const level = shell.dataset.levelFilter || 'all';
            let visibleFields = 0;
            shell.querySelectorAll('.cap-settings-field').forEach(field => {
                const levelMatch = level === 'all' || field.dataset.settingsLevelValue === 'basic';
                const searchMatch = !query || (field.dataset.settingsSearchValue || '').includes(query);
                const visible = levelMatch && searchMatch;
                field.classList.toggle('d-none', !visible);
                if (visible) visibleFields += 1;
            });
            shell.querySelectorAll('.cap-settings-section').forEach(section => {
                section.classList.toggle('d-none', !section.querySelector('.cap-settings-field:not(.d-none)'));
            });
            shell.querySelectorAll('[data-settings-anchor]').forEach(button => {
                const section = shell.querySelector(`#${button.dataset.settingsAnchor}`);
                const count = section?.querySelectorAll('.cap-settings-field:not(.d-none)').length || 0;
                button.classList.toggle('d-none', count === 0);
                const badge = button.querySelector('[data-settings-visible-count]');
                if (badge) badge.textContent = String(count);
            });
            shell.querySelector('#capabilitySettingsEmpty')?.classList.toggle('d-none', visibleFields > 0);
        };

        const syncDependencies = () => {
            if (shell.dataset.capabilityId !== 'builtin_chatbot') return;
            const memoryToggle = shell.querySelector('[data-config-key="memory_enabled"]');
            if (!memoryToggle) return;
            const enabled = memoryToggle.checked;
            const summary = shell.querySelector('[data-global-memory-summary]');
            const help = shell.querySelector('[data-global-memory-help]');
            const primaryToggle = shell.querySelector('[data-global-memory-toggle]');
            const primaryToggleLabel = shell.querySelector('[data-global-memory-toggle-label]');
            if (primaryToggle) primaryToggle.checked = enabled;
            if (primaryToggleLabel) primaryToggleLabel.textContent = enabled ? '已开启' : '已关闭';
            if (summary) summary.textContent = `长期记忆全局默认：${enabled ? '开启' : '关闭'}`;
            if (help) help.textContent = `保存后，所有“继承全局”的聊天都会显示并使用“${enabled ? '开启' : '关闭'}”状态。`;
            shell.querySelectorAll('[data-config-key^="memory_"]').forEach(control => {
                if (control === memoryToggle) return;
                control.disabled = !enabled;
                control.closest('.cap-settings-field')?.classList.toggle('is-dependency-disabled', !enabled);
            });
        };

        search?.addEventListener('input', this.debounce(apply, 100));
        filterButtons.forEach(button => button.addEventListener('click', () => {
            shell.dataset.levelFilter = button.dataset.settingsLevel;
            filterButtons.forEach(item => item.classList.toggle('active', item === button));
            apply();
        }));
        shell.querySelectorAll('[data-settings-anchor]').forEach(button => button.addEventListener('click', () => {
            const section = shell.querySelector(`#${button.dataset.settingsAnchor}`);
            section?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }));
        shell.querySelectorAll('[data-settings-jump]').forEach(button => button.addEventListener('click', () => {
            const targetId = `cap-settings-${button.dataset.settingsJump}`;
            shell.querySelector(`#${targetId}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }));
        shell.querySelector('[data-config-key="memory_enabled"]')?.addEventListener('change', syncDependencies);
        shell.querySelector('[data-global-memory-toggle]')?.addEventListener('change', event => {
            const memoryToggle = shell.querySelector('[data-config-key="memory_enabled"]');
            if (!memoryToggle) return;
            memoryToggle.checked = event.currentTarget.checked;
            memoryToggle.dispatchEvent(new Event('change', { bubbles: true }));
        });
        syncDependencies();
        apply();
    },

    focusCapabilitySettingsGroup(modalElement, groupId) {
        const shell = modalElement?.querySelector('.cap-settings-shell');
        if (!shell || !groupId) return;
        const targetId = `cap-settings-${groupId}`;
        const target = shell.querySelector(`#${targetId}`);
        if (!target) return;
        if (!target.querySelector('.cap-settings-field:not(.d-none)')) {
            shell.querySelector('[data-settings-level="all"]')?.click();
        }
        shell.querySelectorAll('[data-settings-anchor]').forEach(button => {
            button.classList.toggle('active', button.dataset.settingsAnchor === targetId);
        });
        target.classList.add('is-focused');
        window.setTimeout(() => {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            window.setTimeout(() => target.classList.remove('is-focused'), 1400);
        }, 180);
    },

    showAddSettingModal() {
        const form = document.getElementById('addSettingForm');
        if (form) form.reset();

        const modalEl = document.getElementById('addSettingModal');
        if (modalEl) {
            let mInst = bootstrap.Modal.getInstance(modalEl);
            if (!mInst) {
                mInst = new bootstrap.Modal(modalEl);
            }
            mInst.show();
        }
    },

    async submitNewSetting() {
        const key = document.getElementById('newSettingKey').value.trim();
        const value = document.getElementById('newSettingValue').value.trim();
        if (!key || !value) {
            UI.showError('键名和值不能为空');
            return;
        }

        const data = {
            key: key,
            value: value,
            category: document.getElementById('newSettingCategory').value.trim() || 'default',
            description: document.getElementById('newSettingDescription').value.trim()
        };

        const btn = document.querySelector('#addSettingModal .btn-primary');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>正在保存…';
        btn.disabled = true;

        try {
            await API.settings.create(data);

            // Close modal safely
            const modalEl = document.getElementById('addSettingModal');
            let mInst = bootstrap.Modal.getInstance(modalEl);
            if (mInst) {
                mInst.hide();
            }

            // Refresh settings view
            if (window.App && App.loadSettings) {
                await App.loadSettings();
            }

        } catch (error) {
            UI.showError('添加设置失败：' + error.message);
        } finally {
            if (btn) {
                btn.innerHTML = originalText;
                btn.disabled = false;
            }
        }
    },

    async deleteSetting(key) {
        if (!await UI.confirm(`确定要永久删除设置键 ${key} 吗？\n此操作无法撤销。`, {
            title: '删除设置',
            confirmText: '删除',
            variant: 'danger'
        })) {
            return;
        }

        try {
            const resp = await API.settings.delete(key);
            if (resp && resp.success) {
                // Remove it from the DOM immediately or just reload
                if (window.App && App.loadSettings) {
                    await App.loadSettings();
                }
            }
        } catch (error) {
            UI.showError(`删除设置 ${key} 失败：` + error.message);
        }
    }
};

window.UI = UI;
