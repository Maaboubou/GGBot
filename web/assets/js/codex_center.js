/** First-class Codex setup and operations surface. */
const CodexCenter = {
    loading: false,

    escape(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
    },

    async load({ quiet = false } = {}) {
        if (this.loading) return;
        this.loading = true;
        try {
            const [jobsResponse, profiles, assistant] = await Promise.all([
                API.codexJobs.list(),
                API.codexProfiles.list(),
                API.assistant.getOverview().catch(() => ({ chats: [] }))
            ]);
            const jobs = jobsResponse.data || {};
            this.renderReadiness(jobs, profiles, assistant);
            this.renderProfiles(profiles);
            LLMManager.renderCodexJobs(
                jobs.active || [], jobs.recent || [], jobs.stats || {},
                jobs.sessions || [], jobs.session_stats || {}, jobs.runtime || {},
                jobs.upgrade || {}, jobs.file_tools || {}
            );
            LLMManager.scheduleCodexJobsPoll(
                (jobs.active || []).length,
                Boolean(jobs.upgrade?.operation_running)
            );
        } catch (error) {
            if (!quiet) UI.showError(`加载 Codex 运行中心失败：${error.message}`);
            const readiness = document.getElementById('codexReadiness');
            if (readiness) readiness.innerHTML = `<div class="alert alert-danger mb-0">${this.escape(error.message)}</div>`;
        } finally {
            this.loading = false;
        }
    },

    renderReadiness(jobs, profiles, assistant) {
        const runtime = jobs.runtime || {};
        const fileTools = jobs.file_tools || {};
        const codexReady = Boolean(runtime.running || fileTools.codex?.available);
        const profileItems = profiles.profiles || [];
        const selectedProfile = profileItems.find(item => item.name === profiles.default_profile_id);
        const assistantChats = (assistant.chats || []).filter(chat => chat.enabled);
        const chatReady = assistantChats.length > 0;
        const replyReady = codexReady && chatReady && (!profiles.default_profile_id || selectedProfile?.available);
        const steps = [
            {
                ready: codexReady,
                icon: 'bi-terminal',
                title: 'Codex 运行环境',
                detail: codexReady ? `${runtime.active_version || fileTools.codex?.version || '已检测到 Codex'}` : '先在下方运行环境中配置 WSL Codex 路径'
            },
            {
                ready: !profiles.default_profile_id || Boolean(selectedProfile?.available),
                icon: 'bi-person-badge',
                title: '回复 Profile',
                detail: profiles.default_profile_id ? `默认使用 ${profiles.default_profile_id}` : '使用 Codex 当前登录；需要其他模型时再创建 Profile'
            },
            {
                ready: chatReady,
                icon: 'bi-shield-check',
                title: '聊天权限',
                detail: chatReady ? `已为 ${assistantChats.length} 个聊天启用 Assistant` : '选择聊天、开启监听并授予 Assistant 回复权限',
                action: chatReady ? '' : '<a href="/chats" class="btn btn-sm btn-primary" onclick="event.preventDefault(); UI.switchTab(\'users\')">去配置聊天</a>'
            }
        ];
        const container = document.getElementById('codexReadiness');
        if (!container) return;
        container.innerHTML = `
            <div class="codex-readiness-summary ${replyReady ? 'ready' : 'pending'}">
                <span><i class="bi ${replyReady ? 'bi-check-circle-fill' : 'bi-hourglass-split'}"></i></span>
                <div><strong>${replyReady ? 'Assistant 已具备回复条件' : '还差少量配置即可开始回复'}</strong><small>${replyReady ? '最终回复将只经过 Codex。辅助模型配置不会改变这个边界。' : '完成 Codex 与聊天权限后即可工作，插件可以独立启用。'}</small></div>
            </div>
            <div class="codex-readiness-steps">${steps.map((step, index) => `
                <article class="${step.ready ? 'done' : ''}">
                    <span class="codex-step-index">${step.ready ? '<i class="bi bi-check-lg"></i>' : index + 1}</span>
                    <i class="bi ${step.icon}"></i>
                    <div><strong>${step.title}</strong><small>${this.escape(step.detail)}</small></div>
                    ${step.action || ''}
                </article>`).join('')}
            </div>`;
    },

    renderProfiles(data) {
        const profiles = data.profiles || [];
        const container = document.getElementById('codexProfilesGrid');
        if (!container) return;
        const systemCard = `
            <article class="codex-profile-card ${data.default_profile_id ? '' : 'selected'}">
                <div class="codex-profile-card-icon"><i class="bi bi-terminal"></i></div>
                <div class="codex-profile-card-copy"><div><strong>Codex 当前配置</strong>${data.default_profile_id ? '' : '<span>默认</span>'}</div><p>使用当前 Codex 安装自身的登录、模型与配置。</p><small>system runtime</small></div>
                ${data.default_profile_id ? '<button class="btn btn-sm btn-outline-primary" data-default-profile="">设为默认</button>' : '<i class="bi bi-check-circle-fill text-success"></i>'}
            </article>`;
        const cards = profiles.map(profile => {
            const selected = profile.name === data.default_profile_id;
            const ready = Boolean(profile.available);
            return `
                <article class="codex-profile-card ${selected ? 'selected' : ''}">
                    <div class="codex-profile-card-icon ${ready ? '' : 'warning'}"><i class="bi ${ready ? 'bi-person-badge' : 'bi-exclamation-triangle'}"></i></div>
                    <div class="codex-profile-card-copy">
                        <div><strong>${this.escape(profile.name)}</strong>${selected ? '<span>默认</span>' : ''}</div>
                        <p>${this.escape(profile.model)} · ${this.escape(profile.provider_name || profile.auth_type || '')}</p>
                        <small>${ready ? `${this.escape(profile.reasoning_effort || 'inherit')} 推理 · ${Number(profile.context_window || 0).toLocaleString()} context` : '尚未完成凭据配置'}</small>
                    </div>
                    ${selected ? '<i class="bi bi-check-circle-fill text-success"></i>' : `<button class="btn btn-sm btn-outline-primary" data-default-profile="${this.escape(profile.name)}" ${ready ? '' : 'disabled'}>设为默认</button>`}
                </article>`;
        }).join('');
        container.innerHTML = systemCard + cards + `
            <button class="codex-profile-add" type="button" onclick="CodexCenter.openProfileModal()"><i class="bi bi-plus-lg"></i><span>连接其他模型</span><small>创建隔离的 Codex Profile</small></button>`;
        container.querySelectorAll('[data-default-profile]').forEach(button => {
            button.addEventListener('click', () => this.setDefault(button.dataset.defaultProfile || ''));
        });
    },

    openProfileModal() {
        const form = document.getElementById('codexProfileForm');
        form?.reset();
        bootstrap.Modal.getOrCreateInstance(document.getElementById('codexProfileModal')).show();
    },

    async createProfile() {
        const form = document.getElementById('codexProfileForm');
        if (!form?.reportValidity()) return;
        const values = new FormData(form);
        const payload = {
            name: String(values.get('name') || '').trim(),
            auth_type: 'api_key',
            model: String(values.get('model') || '').trim(),
            provider_name: String(values.get('provider_name') || '').trim(),
            base_url: String(values.get('base_url') || '').trim(),
            api_key: String(values.get('api_key') || ''),
            reasoning_effort: String(values.get('reasoning_effort') || 'high'),
            model_verbosity: String(values.get('model_verbosity') || 'inherit'),
            context_window: Number(values.get('context_window') || 128000),
            make_default: values.get('make_default') === 'on'
        };
        const button = document.getElementById('saveCodexProfileButton');
        if (button) button.disabled = true;
        try {
            await API.codexProfiles.create(payload);
            bootstrap.Modal.getInstance(document.getElementById('codexProfileModal'))?.hide();
            UI.showSuccess('Codex Profile 已创建');
            await this.load();
        } catch (error) {
            UI.showError(`创建 Profile 失败：${error.message}`);
        } finally {
            if (button) button.disabled = false;
            const secret = form.elements.api_key;
            if (secret) secret.value = '';
        }
    },

    async setDefault(profileId) {
        try {
            await API.codexProfiles.setDefault(profileId);
            UI.showSuccess(profileId ? `默认 Profile 已切换为 ${profileId}` : '已恢复 Codex 当前配置');
            await this.load();
        } catch (error) {
            UI.showError(`切换失败：${error.message}`);
        }
    }
};
