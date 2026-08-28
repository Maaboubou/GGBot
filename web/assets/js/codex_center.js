/** First-class Codex setup and operations surface. */
const CodexCenter = {
    loading: false,
    profileEditorBound: false,
    profiles: [],
    editingProfile: null,
    catalogProviders: [],
    profileCatalogModels: [],
    catalogVersion: '',
    profileCatalogRequestId: 0,
    oauthProfile: '',
    oauthMakeDefault: false,
    oauthPollTimer: null,
    oauthModels: [],
    profileProviderBases: {
        openai: 'https://api.openai.com/v1',
        deepseek: 'https://api.deepseek.com',
        openrouter: 'https://openrouter.ai/api/v1',
        xai: 'https://api.x.ai/v1',
        groq: 'https://api.groq.com/openai/v1',
        mistral: 'https://api.mistral.ai/v1',
        together_ai: 'https://api.together.xyz/v1',
        fireworks_ai: 'https://api.fireworks.ai/inference/v1',
        perplexity: 'https://api.perplexity.ai',
        cerebras: 'https://api.cerebras.ai/v1'
    },

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
            if (profiles) {
                this.renderProfiles(profiles);
                window.App?.updateManagedChatProfiles(profiles);
            }
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
        this.profiles = profiles;
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
                        <small>${ready ? `${this.escape(profile.reasoning_effort || 'inherit')} 推理 · ${Number(profile.context_window || 0).toLocaleString()} context${profile.supports_vision ? ' · 图片' : ''}${profile.supports_web_search ? ' · 搜索' : ''}` : '尚未完成凭据配置'}</small>
                    </div>
                    <div class="codex-profile-card-actions">
                        <button class="btn btn-sm btn-outline-secondary" type="button" data-edit-profile="${this.escape(profile.name)}"><i class="bi bi-pencil"></i> 编辑</button>
                        ${profile.auth_type === 'chatgpt' && !ready
                            ? `<button class="btn btn-sm btn-outline-primary" type="button" data-profile-login="${this.escape(profile.name)}">完成登录</button>`
                            : (selected ? '' : `<button class="btn btn-sm btn-outline-primary" type="button" data-default-profile="${this.escape(profile.name)}" ${ready ? '' : 'disabled'}>设为默认</button>`)}
                    </div>
                </article>`;
        }).join('');
        container.innerHTML = systemCard + cards + `
            <button class="codex-profile-add" type="button" onclick="CodexCenter.openProfileModal()"><i class="bi bi-plus-lg"></i><span>新建 Profile</span><small>连接兼容 Responses API 的模型</small></button>`;
        container.querySelectorAll('[data-default-profile]').forEach(button => {
            button.addEventListener('click', () => this.setDefault(button.dataset.defaultProfile || ''));
        });
        container.querySelectorAll('[data-profile-login]').forEach(button => {
            button.addEventListener('click', () => this.startOAuth(button.dataset.profileLogin || '', false));
        });
        container.querySelectorAll('[data-edit-profile]').forEach(button => {
            button.addEventListener('click', () => this.openProfileModal(button.dataset.editProfile || ''));
        });
    },

    setupProfileEditor() {
        if (this.profileEditorBound) return;
        const form = document.getElementById('codexProfileForm');
        if (!form) return;
        this.profileEditorBound = true;
        form.querySelectorAll('input[name="profile_source"]').forEach(input => {
            input.addEventListener('change', () => this.setProfileSource(input.value));
        });
        document.getElementById('codexProfileCatalogProvider')?.addEventListener('change', event => {
            this.selectProfileProvider(event.target.value, { forceProvider: true });
        });
        const modelInput = document.getElementById('codexProfileModel');
        const capabilityInputs = [
            document.getElementById('codexProfileSupportsVision'),
            document.getElementById('codexProfileSupportsWebSearch')
        ].filter(Boolean);
        modelInput?.addEventListener('focus', () => {
            if (this.getProfileSource() === 'catalog') this.openProfileCatalog();
        });
        modelInput?.addEventListener('input', () => {
            capabilityInputs.forEach(input => {
                if (input.dataset.autoModel && input.dataset.autoModel !== modelInput.value) {
                    input.checked = false;
                    delete input.dataset.autoModel;
                }
            });
            if (this.getProfileSource() === 'catalog') this.renderProfileCatalog(modelInput.value);
        });
        modelInput?.addEventListener('keydown', event => {
            if (event.key === 'Escape') this.closeProfileCatalog();
        });
        form.addEventListener('submit', event => {
            event.preventDefault();
            this.saveProfile();
        });
        capabilityInputs.forEach(input => input.addEventListener('change', event => {
            if (event.isTrusted) delete event.target.dataset.autoModel;
        }));
        document.addEventListener('click', event => {
            if (!event.target.closest('.codex-profile-model-picker')) this.closeProfileCatalog();
        });
        document.getElementById('codexProfileModal')?.addEventListener('hidden.bs.modal', () => {
            const secret = document.getElementById('codexProfileApiKey');
            if (secret) {
                secret.value = '';
                secret.type = 'password';
            }
            this.editingProfile = null;
            this.closeProfileCatalog();
        });
    },

    getProfileSource() {
        return document.querySelector('#codexProfileForm input[name="profile_source"]:checked')?.value || 'oauth';
    },

    async setProfileSource(source, { load = true } = {}) {
        const form = document.getElementById('codexProfileForm');
        if (!form) return;
        const oauthMode = source === 'oauth';
        const catalogMode = source === 'catalog';
        form.classList.toggle('codex-profile-mode-manual', !catalogMode && !oauthMode);
        form.classList.toggle('codex-profile-mode-oauth', oauthMode);
        form.querySelectorAll('.codex-profile-api-field').forEach(element => {
            element.classList.toggle('d-none', oauthMode);
        });
        form.querySelectorAll('.codex-profile-oauth-field').forEach(element => {
            element.classList.toggle('d-none', !oauthMode);
        });
        form.querySelectorAll('.codex-profile-catalog-field').forEach(element => {
            element.classList.toggle('d-none', !catalogMode);
        });
        const modelInput = document.getElementById('codexProfileModel');
        const baseUrl = document.getElementById('codexProfileBaseUrl');
        const apiKey = document.getElementById('codexProfileApiKey');
        const nameInput = document.getElementById('codexProfileName');
        const hint = document.getElementById('codexProfileCatalogHint');
        if (modelInput) modelInput.required = !oauthMode;
        if (baseUrl) baseUrl.required = !oauthMode;
        if (apiKey) apiKey.required = !oauthMode && !this.editingProfile;
        if (nameInput) nameInput.placeholder = oauthMode ? '例如 chatgpt-main' : '例如 deepseek-main';
        if (oauthMode) {
            this.closeProfileCatalog();
            return;
        }
        if (modelInput) {
            modelInput.placeholder = catalogMode
                ? '输入或从 LiteLLM 目录选择'
                : '例如 deepseek-chat';
        }
        if (!catalogMode) {
            this.closeProfileCatalog();
            if (hint) hint.textContent = '直接填写 Responses API 接受的模型 ID。';
            return;
        }
        if (!load) return;
        if (!this.catalogProviders.length) await this.loadProfileCatalogProviders();
        const providerSelect = document.getElementById('codexProfileCatalogProvider');
        const providerKey = providerSelect?.value
            || (this.catalogProviders.some(provider => provider.id === 'openai') ? 'openai' : this.catalogProviders[0]?.id || '');
        if (providerSelect && providerKey) providerSelect.value = providerKey;
        if (providerKey) await this.selectProfileProvider(providerKey, { resetModel: false });
    },

    async loadProfileCatalogProviders() {
        const select = document.getElementById('codexProfileCatalogProvider');
        const hint = document.getElementById('codexProfileProviderHint');
        if (select) {
            select.disabled = true;
            select.innerHTML = '<option value="">正在读取目录…</option>';
        }
        try {
            const response = await fetch('/api/llm/models/catalog');
            const result = await response.json();
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '目录加载失败');
            }
            this.catalogProviders = Array.isArray(result.data?.providers) ? result.data.providers : [];
            if (!this.catalogProviders.length) throw new Error('LiteLLM 目录没有可用供应商');
            this.catalogVersion = result.data?.version || '';
            if (hint) hint.textContent = `本机 LiteLLM ${this.catalogVersion || '目录'} · ${this.catalogProviders.length} 个供应商`;
        } catch (error) {
            console.warn('Codex Profile LiteLLM providers unavailable:', error);
            this.catalogProviders = [
                { id: 'openai', label: 'OpenAI', model_count: 0 },
                { id: 'deepseek', label: 'DeepSeek', model_count: 0 },
                { id: 'openrouter', label: 'OpenRouter', model_count: 0 }
            ];
            if (hint) hint.textContent = '目录暂时不可用；可选择供应商后手动输入模型。';
        } finally {
            if (select) {
                const selected = select.value;
                select.innerHTML = this.catalogProviders.map(provider => `
                    <option value="${this.escape(provider.id)}">${this.escape(provider.label)}${provider.model_count ? ` · ${Number(provider.model_count).toLocaleString()} 个模型` : ''}</option>`).join('');
                const next = this.catalogProviders.some(provider => provider.id === selected)
                    ? selected
                    : (this.catalogProviders.some(provider => provider.id === 'openai') ? 'openai' : this.catalogProviders[0]?.id || '');
                select.value = next;
                select.disabled = false;
            }
        }
    },

    getProfileProviderLabel(providerKey) {
        return this.catalogProviders.find(provider => provider.id === providerKey)?.label
            || String(providerKey || '').replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase());
    },

    async selectProfileProvider(providerKey, { resetModel = true, forceProvider = false } = {}) {
        if (!providerKey) return;
        const providerName = document.getElementById('codexProfileProviderName');
        const baseUrl = document.getElementById('codexProfileBaseUrl');
        const modelInput = document.getElementById('codexProfileModel');
        const label = this.getProfileProviderLabel(providerKey);
        const suggestedBase = this.profileProviderBases[providerKey] || '';
        if (providerName) {
            const previousAuto = providerName.dataset.autoValue || '';
            if (forceProvider || !providerName.value || providerName.value === previousAuto) providerName.value = label;
            providerName.dataset.autoValue = label;
        }
        if (baseUrl) {
            const previousAuto = baseUrl.dataset.autoValue || '';
            if (forceProvider || !baseUrl.value || baseUrl.value === previousAuto) baseUrl.value = suggestedBase;
            baseUrl.dataset.autoValue = suggestedBase;
        }
        if (resetModel && modelInput) modelInput.value = '';
        if (resetModel) {
            [
                document.getElementById('codexProfileSupportsVision'),
                document.getElementById('codexProfileSupportsWebSearch')
            ].filter(Boolean).forEach(input => {
                input.checked = false;
                delete input.dataset.autoModel;
            });
        }
        await this.loadProfileCatalog(providerKey);
    },

    async loadProfileCatalog(providerKey) {
        const requestId = ++this.profileCatalogRequestId;
        const hint = document.getElementById('codexProfileCatalogHint');
        if (hint && this.getProfileSource() === 'catalog') {
            hint.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>正在读取本机 LiteLLM 模型目录…';
        }
        try {
            const response = await fetch(`/api/llm/models/catalog?provider=${encodeURIComponent(providerKey)}&limit=300`);
            const result = await response.json();
            if (requestId !== this.profileCatalogRequestId) return;
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '模型目录加载失败');
            }
            this.profileCatalogModels = Array.isArray(result.data?.models) ? result.data.models : [];
            this.catalogVersion = result.data?.version || this.catalogVersion;
            const version = this.catalogVersion ? ` · LiteLLM ${this.catalogVersion}` : '';
            if (hint && this.getProfileSource() === 'catalog') {
                hint.textContent = `可选择 ${this.profileCatalogModels.length} 个模型${version}；目录外模型仍可直接输入。`;
            }
        } catch (error) {
            if (requestId !== this.profileCatalogRequestId) return;
            console.warn(`Codex Profile catalog unavailable for ${providerKey}:`, error);
            this.profileCatalogModels = [];
            if (hint && this.getProfileSource() === 'catalog') hint.textContent = '目录暂时不可用，仍可手动输入模型 ID。';
        }
        this.renderProfileCatalog(document.getElementById('codexProfileModel')?.value || '');
    },

    getNativeProfileModelId(item) {
        const modelId = String(item?.provider_model_id || item?.id || '');
        const providerKey = document.getElementById('codexProfileCatalogProvider')?.value || '';
        const prefix = `${providerKey}/`;
        return providerKey && modelId.startsWith(prefix) ? modelId.slice(prefix.length) : modelId;
    },

    formatTokenCount(value) {
        const count = Number(value || 0);
        if (!count) return '';
        if (count >= 1000000) return `${(count / 1000000).toFixed(count % 1000000 ? 1 : 0)}M`;
        if (count >= 1000) return `${Math.round(count / 1000)}K`;
        return count.toLocaleString();
    },

    renderProfileCatalog(query = '') {
        const menu = document.getElementById('codexProfileCatalogMenu');
        if (!menu) return;
        const needle = String(query || '').trim().toLowerCase();
        const matches = this.profileCatalogModels.filter(item => {
            const nativeId = this.getNativeProfileModelId(item);
            return !needle || nativeId.toLowerCase().includes(needle) || String(item.id || '').toLowerCase().includes(needle);
        }).slice(0, 40);
        if (!matches.length) {
            menu.innerHTML = `
                <div class="model-catalog-empty">
                    <i class="bi bi-pencil-square"></i>
                    <strong>${this.profileCatalogModels.length ? '目录中没有匹配项' : '当前没有可用目录'}</strong>
                    <span>可以继续手动输入 Responses API 接受的模型 ID。</span>
                </div>`;
            return;
        }
        menu.innerHTML = matches.map(item => {
            const modelId = this.getNativeProfileModelId(item);
            const capabilities = [];
            if (item.supports_reasoning) capabilities.push('推理');
            if (item.supports_vision) capabilities.push('图片');
            if (item.supports_web_search) capabilities.push('搜索');
            const context = item.max_input_tokens ? `上下文 ${this.formatTokenCount(item.max_input_tokens)}` : '';
            return `
                <button type="button" class="model-catalog-item" role="option" data-codex-profile-model="${this.escape(modelId)}">
                    <span class="model-catalog-item-main"><strong>${this.escape(modelId)}</strong><small>${item.recommended ? '常用名称' : '版本/预览模型'}${context ? ` · ${context}` : ''}</small></span>
                    <span class="model-catalog-capabilities">${capabilities.map(value => `<em>${value}</em>`).join('')}</span>
                </button>`;
        }).join('');
        menu.querySelectorAll('[data-codex-profile-model]').forEach(button => {
            button.addEventListener('click', () => this.selectProfileCatalogModel(button.dataset.codexProfileModel));
        });
    },

    selectProfileCatalogModel(modelId) {
        const item = this.profileCatalogModels.find(candidate => this.getNativeProfileModelId(candidate) === modelId);
        const modelInput = document.getElementById('codexProfileModel');
        const contextInput = document.querySelector('#codexProfileForm [name="context_window"]');
        const nameInput = document.getElementById('codexProfileName');
        const visionInput = document.getElementById('codexProfileSupportsVision');
        const webSearchInput = document.getElementById('codexProfileSupportsWebSearch');
        if (modelInput) modelInput.value = modelId;
        if (contextInput && item?.max_input_tokens) contextInput.value = item.max_input_tokens;
        [
            [visionInput, item?.supports_vision],
            [webSearchInput, item?.supports_web_search]
        ].forEach(([input, supported]) => {
            if (!input) return;
            input.checked = Boolean(supported);
            input.dataset.autoModel = modelId;
        });
        if (nameInput && !nameInput.value) {
            nameInput.value = String(modelId).toLowerCase()
                .replace(/[^a-z0-9_-]+/g, '-')
                .replace(/^-+|-+$/g, '')
                .slice(0, 48);
        }
        this.closeProfileCatalog();
    },

    openProfileCatalog() {
        if (this.getProfileSource() !== 'catalog') return;
        const menu = document.getElementById('codexProfileCatalogMenu');
        const input = document.getElementById('codexProfileModel');
        if (!menu || !input) return;
        this.renderProfileCatalog(input.value);
        menu.classList.remove('d-none');
        input.setAttribute('aria-expanded', 'true');
    },

    closeProfileCatalog() {
        document.getElementById('codexProfileCatalogMenu')?.classList.add('d-none');
        document.getElementById('codexProfileModel')?.setAttribute('aria-expanded', 'false');
    },

    toggleProfileCatalog() {
        const menu = document.getElementById('codexProfileCatalogMenu');
        if (!menu) return;
        if (menu.classList.contains('d-none')) {
            this.openProfileCatalog();
            document.getElementById('codexProfileModel')?.focus();
        } else {
            this.closeProfileCatalog();
        }
    },

    async openProfileModal(profileName = '') {
        const form = document.getElementById('codexProfileForm');
        if (!form) return;
        this.setupProfileEditor();
        const profile = profileName
            ? this.profiles.find(item => item.name === profileName)
            : null;
        if (profileName && !profile) {
            UI.showError('Codex Profile 不存在，请刷新后重试');
            return;
        }
        this.editingProfile = profile;
        form.reset();
        this.profileCatalogModels = [];
        form.querySelectorAll('input[name="profile_source"]').forEach(input => {
            input.disabled = false;
        });
        const providerName = document.getElementById('codexProfileProviderName');
        const baseUrl = document.getElementById('codexProfileBaseUrl');
        const nameInput = document.getElementById('codexProfileName');
        const modelInput = document.getElementById('codexProfileModel');
        const apiKey = document.getElementById('codexProfileApiKey');
        const apiKeyHint = document.getElementById('codexProfileApiKeyHint');
        const visionInput = document.getElementById('codexProfileSupportsVision');
        const webSearchInput = document.getElementById('codexProfileSupportsWebSearch');
        const modalTitle = document.getElementById('codexProfileModalTitle');
        const modalSubtitle = document.getElementById('codexProfileModalSubtitle');
        const saveButton = document.getElementById('saveCodexProfileButton');
        if (providerName) delete providerName.dataset.autoValue;
        if (baseUrl) delete baseUrl.dataset.autoValue;
        if (visionInput) delete visionInput.dataset.autoModel;
        if (webSearchInput) delete webSearchInput.dataset.autoModel;
        if (nameInput) nameInput.readOnly = Boolean(profile);
        if (apiKey) apiKey.placeholder = '';

        let source = 'oauth';
        if (profile) {
            source = profile.auth_type === 'chatgpt' ? 'oauth' : 'manual';
            if (nameInput) nameInput.value = profile.name || '';
            if (modelInput) modelInput.value = profile.model || '';
            if (providerName) providerName.value = profile.provider_name || '';
            if (baseUrl) baseUrl.value = profile.base_url || '';
            if (form.elements.reasoning_effort) form.elements.reasoning_effort.value = profile.reasoning_effort || 'high';
            if (form.elements.model_verbosity) form.elements.model_verbosity.value = profile.model_verbosity || 'inherit';
            if (form.elements.context_window) form.elements.context_window.value = Number(profile.context_window || 128000);
            if (form.elements.make_default) {
                form.elements.make_default.checked = Boolean(profile.is_default);
                form.elements.make_default.disabled = Boolean(profile.is_default);
            }
            if (visionInput) visionInput.checked = Boolean(profile.supports_vision);
            if (webSearchInput) webSearchInput.checked = Boolean(profile.supports_web_search);
            form.querySelectorAll('input[name="profile_source"]').forEach(input => {
                input.checked = input.value === source;
                input.disabled = profile.auth_type === 'chatgpt' || input.value === 'oauth';
            });
            if (apiKey) apiKey.placeholder = '留空则继续使用当前 API Key';
            if (apiKeyHint) apiKeyHint.textContent = '留空不会修改现有密钥；输入新 Key 后保存即可完成轮换。密钥仍只存入权限为 0600 的独立文件。';
            if (modalTitle) modalTitle.textContent = `编辑 Codex Profile · ${profile.name}`;
            if (modalSubtitle) modalSubtitle.textContent = profile.auth_type === 'chatgpt'
                ? '调整模型参数；如需更换账号，请重新完成 ChatGPT 授权。'
                : '可修改接口、模型参数或轮换 API Key。';
            if (saveButton) saveButton.textContent = '保存更改';
        } else {
            const oauthSource = form.querySelector('input[name="profile_source"][value="oauth"]');
            if (oauthSource) oauthSource.checked = true;
            if (form.elements.make_default) {
                form.elements.make_default.checked = true;
                form.elements.make_default.disabled = false;
            }
            if (apiKeyHint) apiKeyHint.textContent = '密钥通过标准输入交给 WSL，并存入权限为 0600 的独立文件；不会写入 Profile 清单或返回网页。';
            if (modalTitle) modalTitle.textContent = '新建 Codex Profile';
            if (modalSubtitle) modalSubtitle.textContent = '使用 ChatGPT 官方登录或连接 Responses 兼容接口。';
            if (saveButton) saveButton.textContent = '创建 Profile';
        }
        await this.setProfileSource(source, { load: false });
        bootstrap.Modal.getOrCreateInstance(document.getElementById('codexProfileModal')).show();
    },

    async saveProfile() {
        if (this.editingProfile) {
            await this.updateProfile(this.editingProfile);
            return;
        }
        await this.createProfile();
    },

    async updateProfile(profile) {
        const form = document.getElementById('codexProfileForm');
        if (!profile || !form?.reportValidity()) return;
        const values = new FormData(form);
        const payload = {
            reasoning_effort: String(values.get('reasoning_effort') || 'high'),
            model_verbosity: String(values.get('model_verbosity') || 'inherit'),
            context_window: Number(values.get('context_window') || 128000),
            supports_vision: values.get('supports_vision') === 'on',
            supports_web_search: values.get('supports_web_search') === 'on'
        };
        const secret = String(values.get('api_key') || '');
        if (profile.auth_type === 'api_key') {
            payload.model = String(values.get('model') || '').trim();
            payload.provider_name = String(values.get('provider_name') || '').trim();
            payload.base_url = String(values.get('base_url') || '').trim();
            if (secret) payload.api_key = secret;
        }
        const button = document.getElementById('saveCodexProfileButton');
        if (button) button.disabled = true;
        try {
            await API.codexProfiles.update(profile.name, payload);
            if (form.elements.make_default?.checked && !profile.is_default) {
                await API.codexProfiles.setDefault(profile.name);
            }
            const modalElement = document.getElementById('codexProfileModal');
            const modal = bootstrap.Modal.getInstance(modalElement);
            const hidden = modalElement && modalElement.classList.contains('show')
                ? new Promise(resolve => modalElement.addEventListener('hidden.bs.modal', resolve, { once: true }))
                : Promise.resolve();
            modal?.hide();
            await hidden;
            await this.load();
            UI.showSuccess(secret ? 'Codex Profile 已更新，新的 API Key 已保存' : 'Codex Profile 已更新');
        } catch (error) {
            UI.showError(`更新 Profile 失败：${error.message}`);
        } finally {
            if (button) button.disabled = false;
            const apiKey = form.elements.api_key;
            if (apiKey) apiKey.value = '';
        }
    },

    async createProfile() {
        const form = document.getElementById('codexProfileForm');
        if (!form?.reportValidity()) return;
        const values = new FormData(form);
        const source = this.getProfileSource();
        const oauthMode = source === 'oauth';
        const payload = {
            name: String(values.get('name') || '').trim(),
            auth_type: oauthMode ? 'chatgpt' : 'api_key',
            model: oauthMode ? 'gpt-5.6-sol' : String(values.get('model') || '').trim(),
            provider_name: oauthMode ? 'ChatGPT 官方登录' : String(values.get('provider_name') || '').trim(),
            base_url: oauthMode ? '' : String(values.get('base_url') || '').trim(),
            api_key: oauthMode ? '' : String(values.get('api_key') || ''),
            reasoning_effort: String(values.get('reasoning_effort') || 'high'),
            model_verbosity: String(values.get('model_verbosity') || 'inherit'),
            context_window: Number(values.get('context_window') || 128000),
            supports_vision: oauthMode || values.get('supports_vision') === 'on',
            supports_web_search: oauthMode || values.get('supports_web_search') === 'on',
            make_default: values.get('make_default') === 'on'
        };
        const button = document.getElementById('saveCodexProfileButton');
        if (button) button.disabled = true;
        try {
            const result = await API.codexProfiles.create(payload);
            const profileModalElement = document.getElementById('codexProfileModal');
            const profileModal = bootstrap.Modal.getInstance(profileModalElement);
            const hidden = profileModalElement && profileModalElement.classList.contains('show')
                ? new Promise(resolve => profileModalElement.addEventListener('hidden.bs.modal', resolve, { once: true }))
                : Promise.resolve();
            profileModal?.hide();
            await hidden;
            await this.load();
            if (result.requires_login) {
                await this.startOAuth(result.profile?.name || payload.name, payload.make_default);
            } else {
                UI.showSuccess('Codex Profile 已创建');
            }
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
    },

    resetOAuthView() {
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        this.oauthModels = [];
        document.getElementById('codexOAuthAlert')?.classList.add('d-none');
        document.getElementById('codexOAuthPending')?.classList.remove('d-none');
        document.getElementById('codexOAuthConnected')?.classList.add('d-none');
        document.getElementById('codexOAuthFinish')?.classList.add('d-none');
        const cancel = document.getElementById('codexOAuthCancel');
        if (cancel) cancel.textContent = '取消授权';
    },

    async startOAuth(profileName, makeDefault = false, force = false) {
        if (!profileName) return;
        this.oauthProfile = profileName;
        this.oauthMakeDefault = Boolean(makeDefault);
        this.resetOAuthView();
        const subtitle = document.getElementById('codexOAuthSubtitle');
        if (subtitle) subtitle.textContent = `${profileName} · 扫码完成 Codex 官方授权`;
        bootstrap.Modal.getOrCreateInstance(document.getElementById('codexOAuthModal')).show();
        try {
            const status = await API.codexProfiles.startOAuth(profileName, force);
            this.renderOAuthStatus(status);
        } catch (error) {
            this.showOAuthError(error.message);
        }
    },

    renderOAuthStatus(status) {
        const state = String(status?.status || 'idle');
        if (state === 'pending') {
            const loginId = String(status.login_id || '');
            const link = document.getElementById('codexOAuthLink');
            if (link) link.href = status.verification_url || '#';
            const code = document.getElementById('codexOAuthCode');
            if (code) code.textContent = status.user_code || '';
            const qr = document.getElementById('codexOAuthQr');
            if (qr && loginId) {
                qr.src = `/api/codex/profiles/${encodeURIComponent(this.oauthProfile)}/oauth/qr?login_id=${encodeURIComponent(loginId)}`;
            }
            const seconds = Math.max(0, Number(status.expires_in || 0));
            const countdown = document.getElementById('codexOAuthCountdown');
            if (countdown) countdown.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')} 后过期`;
            this.oauthPollTimer = setTimeout(() => this.pollOAuth(), 1200);
            return;
        }
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        if (state === 'connected') {
            document.getElementById('codexOAuthPending')?.classList.add('d-none');
            document.getElementById('codexOAuthConnected')?.classList.remove('d-none');
            const account = status.account || {};
            const accountLabel = [account.email, account.plan_type].filter(Boolean).join(' · ') || 'ChatGPT 账号';
            const accountElement = document.getElementById('codexOAuthAccount');
            if (accountElement) accountElement.textContent = accountLabel;
            this.oauthModels = Array.isArray(status.models) ? status.models : [];
            const select = document.getElementById('codexOAuthModel');
            if (select) {
                select.innerHTML = this.oauthModels.map(model => `<option value="${this.escape(model.id)}" ${model.is_default ? 'selected' : ''}>${this.escape(model.display_name || model.id)} · ${this.escape(model.id)}</option>`).join('');
                select.disabled = !this.oauthModels.length;
            }
            const finish = document.getElementById('codexOAuthFinish');
            finish?.classList.remove('d-none');
            if (finish) finish.disabled = !this.oauthModels.length;
            const cancel = document.getElementById('codexOAuthCancel');
            if (cancel) cancel.textContent = '稍后使用';
            return;
        }
        this.showOAuthError(status?.error || (state === 'expired' ? '授权已过期，请重新发起登录。' : 'ChatGPT 授权未完成。'));
    },

    async pollOAuth() {
        if (!this.oauthProfile) return;
        try {
            this.renderOAuthStatus(await API.codexProfiles.getOAuth(this.oauthProfile));
        } catch (error) {
            this.showOAuthError(error.message);
        }
    },

    showOAuthError(message) {
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        const alert = document.getElementById('codexOAuthAlert');
        if (alert) {
            alert.textContent = message || 'ChatGPT 授权失败';
            alert.classList.remove('d-none');
        }
        const cancel = document.getElementById('codexOAuthCancel');
        if (cancel) cancel.textContent = '关闭';
    },

    async copyOAuthCode() {
        const code = document.getElementById('codexOAuthCode')?.textContent || '';
        if (!code) return;
        try {
            await navigator.clipboard.writeText(code);
            UI.showSuccess('验证码已复制');
        } catch (error) {
            UI.showError('复制失败，请手动输入验证码');
        }
    },

    async cancelOAuth() {
        clearTimeout(this.oauthPollTimer);
        this.oauthPollTimer = null;
        const pending = !document.getElementById('codexOAuthPending')?.classList.contains('d-none');
        if (pending && this.oauthProfile) {
            try {
                await API.codexProfiles.cancelOAuth(this.oauthProfile);
            } catch (error) {
                // The app-server may already have completed or expired the login.
            }
        }
        bootstrap.Modal.getInstance(document.getElementById('codexOAuthModal'))?.hide();
        await this.load();
    },

    async closeOAuth() {
        await this.cancelOAuth();
    },

    async finishOAuth() {
        const profileName = this.oauthProfile;
        const modelId = document.getElementById('codexOAuthModel')?.value || '';
        const button = document.getElementById('codexOAuthFinish');
        if (!profileName || !modelId) {
            this.showOAuthError('当前账号没有返回可用的 Codex 模型。');
            return;
        }
        const model = this.oauthModels.find(item => item.id === modelId) || {};
        const payload = { model: modelId };
        if (Number(model.context_window || 0) >= 4096) payload.context_window = Number(model.context_window);
        if ((model.supported_reasoning_efforts || []).includes(model.default_reasoning_effort)) {
            payload.reasoning_effort = model.default_reasoning_effort;
        }
        payload.supports_vision = (model.input_modalities || []).includes('image');
        if (button) button.disabled = true;
        try {
            await API.codexProfiles.update(profileName, payload);
            if (this.oauthMakeDefault) await API.codexProfiles.setDefault(profileName);
            bootstrap.Modal.getInstance(document.getElementById('codexOAuthModal'))?.hide();
            UI.showSuccess(this.oauthMakeDefault ? 'ChatGPT 已连接并设为默认 Profile' : 'ChatGPT Profile 已连接');
            await this.load();
        } catch (error) {
            this.showOAuthError(error.message);
        } finally {
            if (button) button.disabled = false;
        }
    }
};
