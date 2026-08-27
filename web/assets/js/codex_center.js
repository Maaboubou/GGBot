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
            const [jobsResponse, profiles] = await Promise.all([
                API.codexJobs.list(),
                quiet ? Promise.resolve(null) : API.codexProfiles.list()
            ]);
            const jobs = jobsResponse.data || {};
            if (profiles) this.renderProfiles(profiles);
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
            if (!quiet) {
                const jobs = document.getElementById('codexJobsList');
                const profiles = document.getElementById('codexProfilesGrid');
                const message = `<div class="alert alert-danger mb-0">${this.escape(error.message)}</div>`;
                if (jobs) jobs.innerHTML = message;
                if (profiles) profiles.innerHTML = message;
            }
        } finally {
            this.loading = false;
        }
    },

    renderProfiles(data) {
        const profiles = data.profiles || [];
        const container = document.getElementById('codexProfilesGrid');
        if (!container) return;
        const systemCard = `
            <article class="codex-profile-card ${data.default_profile_id ? '' : 'selected'}" ${data.default_profile_id ? '' : 'aria-current="true"'}>
                <div class="codex-profile-card-icon"><i class="bi bi-terminal"></i></div>
                <div class="codex-profile-card-copy"><div><strong>当前配置</strong>${data.default_profile_id ? '' : '<span>默认</span>'}</div><p>沿用本机 Codex 的登录、模型与设置。</p><small>系统配置</small></div>
                ${data.default_profile_id ? '<button class="btn btn-sm btn-outline-primary" data-default-profile="">设为默认</button>' : ''}
            </article>`;
        const cards = profiles.map(profile => {
            const selected = profile.name === data.default_profile_id;
            const ready = Boolean(profile.available);
            return `
                <article class="codex-profile-card ${selected ? 'selected' : ''}" ${selected ? 'aria-current="true"' : ''}>
                    <div class="codex-profile-card-icon ${ready ? '' : 'warning'}"><i class="bi ${ready ? 'bi-person-badge' : 'bi-exclamation-triangle'}"></i></div>
                    <div class="codex-profile-card-copy">
                        <div><strong>${this.escape(profile.name)}</strong>${selected ? '<span>默认</span>' : ''}</div>
                        <p>${this.escape(profile.model)} · ${this.escape(profile.provider_name || profile.auth_type || '')}</p>
                        <small>${ready ? `${this.escape(profile.reasoning_effort || 'inherit')} 推理 · ${Number(profile.context_window || 0).toLocaleString()} context` : '尚未完成凭据配置'}</small>
                    </div>
                    ${selected ? '' : `<button class="btn btn-sm btn-outline-primary" data-default-profile="${this.escape(profile.name)}" ${ready ? '' : 'disabled'}>设为默认</button>`}
                </article>`;
        }).join('');
        container.innerHTML = systemCard + cards + `
            <button class="codex-profile-add" type="button" onclick="CodexCenter.openProfileModal()"><i class="bi bi-plus-lg"></i><span>新建 Profile</span><small>连接兼容 Responses API 的模型</small></button>`;
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
