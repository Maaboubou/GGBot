/**
 * LLM Manager - Frontend Logic
 * Handles model configuration, plugin mappings, prompts, and statistics
 */

const LLMManager = {
    currentModels: {},
    currentMappings: {},
    currentCapabilities: {},
    currentStats: {},
    catalogProviders: [],
    catalogModels: [],
    catalogVersion: '',
    catalogRequestId: 0,
    credentialStatusRequestId: 0,
    sharedCredentialStatus: { name: '', configured: false, source: 'unknown' },
    modelSortable: null,
    modelIdAutofill: true,
    providerPresets: {
        openai: {
            label: 'OpenAI', catalogProvider: 'openai', providerValue: 'openai',
            envVar: 'OPENAI_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 gpt-5.5', apiBase: '', apiBaseRequired: false,
        },
        anthropic: {
            label: 'Anthropic', catalogProvider: 'anthropic', providerValue: 'anthropic',
            envVar: 'ANTHROPIC_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 anthropic/claude-sonnet-4-6', apiBase: '', apiBaseRequired: false,
        },
        gemini: {
            label: 'Google Gemini', catalogProvider: 'gemini', providerValue: 'gemini',
            envVar: 'GEMINI_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 gemini/gemini-3.5-flash', apiBase: '', apiBaseRequired: false,
        },
        deepseek: {
            label: 'DeepSeek', catalogProvider: 'deepseek', providerValue: 'deepseek',
            envVar: 'DEEPSEEK_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 deepseek/deepseek-chat', apiBase: '', apiBaseRequired: false,
        },
        openrouter: {
            label: 'OpenRouter', catalogProvider: 'openrouter', providerValue: 'openrouter',
            envVar: 'OPENROUTER_API_KEY', requiresCredential: true,
            modelPlaceholder: '例如 openrouter/anthropic/claude-sonnet-4',
            apiBase: 'https://openrouter.ai/api/v1', apiBaseRequired: false,
        },
        local_codex: {
            label: '本地 Codex', catalogProvider: 'local_codex', providerValue: 'local_codex',
            envVar: '', requiresCredential: false,
            modelPlaceholder: '例如 gpt-5.6-sol', apiBase: '', apiBaseRequired: false,
        },
        compatible: {
            label: '自定义 OpenAI 兼容接口', catalogProvider: '', providerValue: 'custom_openai',
            envVar: 'CUSTOM_LLM_API_KEY', requiresCredential: false,
            modelPlaceholder: '输入接口暴露的模型名称', apiBase: '', apiBaseRequired: true,
        },
        other: {
            label: '其他 LiteLLM 供应商', catalogProvider: '', providerValue: '',
            envVar: 'LLM_API_KEY', requiresCredential: true,
            modelPlaceholder: '先选择 LiteLLM 供应商', apiBase: '', apiBaseRequired: false,
        },
    },
    activeStatsType: 'today',
    statsSort: {
        today: { field: 'calls', direction: 'desc' },
        session: { field: 'calls', direction: 'desc' },
        total: { field: 'calls', direction: 'desc' },
    },

    escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    normalizePastedText(value) {
        let text = String(value ?? '');
        if (typeof text.normalize === 'function') text = text.normalize('NFKC');
        return text.replace(/[\u200B-\u200D\u2060\uFEFF]/g, '');
    },

    sanitizeModelId(value) {
        return this.normalizePastedText(value)
            .trim()
            .replace(/\s+/g, '-')
            .replace(/[\\/?#\x00-\x1f\x7f]+/g, '-')
            .replace(/-+/g, '-')
            .replace(/^[.-]+|[.-]+$/g, '')
            .slice(0, 80);
    },

    sanitizeCredentialName(value) {
        let name = this.normalizePastedText(value)
            .trim()
            .replace(/[^A-Za-z0-9_]+/g, '_')
            .replace(/_+/g, '_')
            .replace(/^_+|_+$/g, '')
            .slice(0, 120);
        if (/^[0-9]/.test(name)) name = `_${name}`;
        return name;
    },

    buildModelCredentialName(modelId) {
        const normalized = this.sanitizeModelId(modelId) || 'model';
        let hash = 2166136261;
        for (let index = 0; index < normalized.length; index += 1) {
            hash = Math.imul(hash ^ normalized.charCodeAt(index), 16777619) >>> 0;
        }
        const stem = normalized
            .toUpperCase()
            .replace(/[^A-Z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '')
            .slice(0, 50) || 'MODEL';
        return `GGBOT_MODEL_${stem}_${hash.toString(16).padStart(8, '0').toUpperCase()}_API_KEY`;
    },

    mergeDeclaredTaskMappings(rawMappings, capabilities) {
        const merged = {};
        Object.entries(rawMappings || {}).forEach(([pluginName, mappings]) => {
            if (!mappings || typeof mappings !== 'object' || Array.isArray(mappings)) return;
            merged[pluginName] = {};
            Object.entries(mappings).forEach(([callType, mapping]) => {
                merged[pluginName][callType] = mapping && typeof mapping === 'object'
                    ? {...mapping}
                    : { primary: '', fallback: [], override_params: {} };
            });
        });
        Object.entries(capabilities || {}).forEach(([pluginName, capability]) => {
            const tasks = capability?.llm_tasks;
            if (!tasks || typeof tasks !== 'object' || Array.isArray(tasks) || !Object.keys(tasks).length) return;
            const pluginMappings = merged[pluginName] ||= {};
            Object.keys(tasks).forEach(callType => {
                if (!pluginMappings[callType]) {
                    pluginMappings[callType] = { primary: '', fallback: [], override_params: {} };
                }
            });
        });
        return merged;
    },

    summarizeBase64ForDisplay(value) {
        const text = String(value ?? '');
        const dataUrlMatch = text.match(/^(data:[^;,]+(?:;[^,]*)?;base64,)([\s\S]+)$/i);
        const prefix = dataUrlMatch ? dataUrlMatch[1] : '';
        const payload = dataUrlMatch ? dataUrlMatch[2] : text;
        const head = payload.slice(0, 80);
        const tail = payload.length > 112 ? payload.slice(-32) : '';
        const omitted = Math.max(payload.length - head.length - tail.length, 0);
        const suffix = tail ? `...${tail}` : '';
        return `${prefix}${head}${suffix} [base64 omitted: ${omitted} chars, total: ${payload.length} chars]`;
    },

    isLikelyBase64String(value, key = '') {
        if (typeof value !== 'string') return false;
        if (/^data:[^;,]+(?:;[^,]*)?;base64,/i.test(value)) return true;
        if (value.length < 512) return false;

        const normalized = value.replace(/\s+/g, '');
        if (normalized.length < 512 || normalized.length % 4 === 1) return false;

        const keyHint = /base64|image|audio|video|file|bytes|payload|data/i.test(String(key));
        if (!keyHint) return false;

        return /^[A-Za-z0-9+/]+={0,2}$/.test(normalized);
    },

    sanitizeCallHistoryPayload(value, key = '') {
        if (this.isLikelyBase64String(value, key)) {
            return this.summarizeBase64ForDisplay(value);
        }

        if (Array.isArray(value)) {
            return value.map(item => this.sanitizeCallHistoryPayload(item, key));
        }

        if (value && typeof value === 'object') {
            const sanitized = {};
            Object.entries(value).forEach(([childKey, childValue]) => {
                sanitized[childKey] = this.sanitizeCallHistoryPayload(childValue, childKey);
            });
            return sanitized;
        }

        return value;
    },

    renderTokenUsage(entry) {
        const formatK = (num) => {
            if (num === null || num === undefined) return '-';
            const val = Number(num);
            if (val >= 1000000) return (val / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
            if (val >= 1000) return (val / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
            return val.toLocaleString();
        };

        const usage = entry.token_usage || {};
        const prompt = usage.prompt_tokens || entry.prompt_tokens || 0;
        const completion = usage.completion_tokens || entry.completion_tokens || 0;
        const total = usage.total_tokens || entry.tokens || 0;
        const cached = usage.cached_tokens || 0;
        const cacheMiss = usage.cache_miss_tokens || 0;
        const cacheRate = usage.cache_hit_rate;
        const reasoning = usage.reasoning_tokens || 0;
        const estimated = usage.estimated || entry.estimated;

        let html = '';
        const badgeClass = "badge bg-light text-secondary border fw-normal";
        const valClass = "text-dark font-monospace ms-1";

        if (estimated) {
            html += `<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle fw-normal">估算</span> `;
        }

        if (prompt || completion) {
            html += `<span class="${badgeClass}">输入<span class="${valClass}">${formatK(prompt)}</span></span>`;
            html += `<span class="${badgeClass}">输出<span class="${valClass}">${formatK(completion)}</span></span>`;
            if (total) html += `<span class="${badgeClass}">总计<span class="${valClass}">${formatK(total)}</span></span>`;
            if (reasoning) html += `<span class="${badgeClass}">推理<span class="${valClass}">${formatK(reasoning)}</span></span>`;
            if (cached) {
                let cacheStr = `<span class="${badgeClass}">缓存<span class="text-success font-monospace ms-1">${formatK(cached)}</span>`;
                if (cacheRate !== undefined) {
                    cacheStr += `<span class="ms-1 opacity-75">(${(Number(cacheRate) * 100).toFixed(1)}%)</span>`;
                }
                cacheStr += `</span>`;
                html += cacheStr;
            }
            if (cacheMiss) html += `<span class="${badgeClass}">未命中<span class="${valClass}">${formatK(cacheMiss)}</span></span>`;
            return html;
        }

        if (total) return `${html}<span class="${badgeClass}">总计<span class="${valClass}">${formatK(total)}</span></span>`;
        return html;
    },

    getModelDomId(modelId) {
        return btoa(unescape(encodeURIComponent(String(modelId))))
            .replace(/[+/=]/g, '_');
    },

    formatTemperature(value, fallback = 'N/A') {
        if (value === undefined || value === null || value === '') return fallback;
        const num = Number(value);
        if (!Number.isFinite(num)) return String(value);
        return Number.isInteger(num) ? num.toFixed(1) : String(value);
    },

    getTaskRouteMeta(pluginName, callType) {
        const capability = this.currentCapabilities[pluginName] || {};
        const declared = capability.llm_tasks?.[callType];
        if (declared && typeof declared === 'object') {
            const declaredOrder = Number(declared.order);
            return {
                label: declared.label || callType,
                description: declared.description || '该插件未补充任务用途说明。',
                category: declared.category || '模型任务',
                order: Number.isFinite(declaredOrder) ? declaredOrder : 500,
                declared: true,
            };
        }
        const readable = String(callType || '')
            .replace(/[_-]+/g, ' ')
            .trim()
            .replace(/\b\w/g, value => value.toUpperCase());
        return {
            label: readable ? `自定义任务 · ${readable}` : '未命名任务',
            description: '插件尚未声明这项模型任务的用途。',
            category: '自定义',
            order: 900,
            declared: false,
        };
    },

    getRouteCapabilityMeta(pluginName) {
        const capability = this.currentCapabilities[pluginName] || {};
        const readable = String(pluginName || '')
            .replace(/[_-]+/g, ' ')
            .trim()
            .replace(/\b\w/g, value => value.toUpperCase());
        const icon = /^bi-[a-z0-9-]+$/.test(String(capability.icon || ''))
            ? capability.icon
            : 'bi-puzzle';
        return {
            displayName: capability.display_name || readable || '未命名能力',
            description: capability.description || '该插件尚未声明能力说明。',
            icon,
        };
    },

    getRouteModelInfo(modelId) {
        const id = String(modelId || '').trim();
        const config = this.currentModels[id] || {};
        const configuredModel = String(config.model || '').trim();
        return {
            id,
            detail: configuredModel && configuredModel !== id ? configuredModel : '',
        };
    },

    formatRouteModelOption(modelId) {
        const info = this.getRouteModelInfo(modelId);
        return info.detail ? `${info.id} · ${info.detail}` : info.id;
    },

    renderRouteModelCell(modelId, emptyLabel = '未配置') {
        const info = this.getRouteModelInfo(modelId);
        if (!info.id) return `<span class="llm-route-model-empty">${this.escapeHtml(emptyLabel)}</span>`;
        return `<div class="llm-route-model-cell"><strong title="${this.escapeHtml(info.id)}">${this.escapeHtml(info.id)}</strong>${info.detail ? `<span title="${this.escapeHtml(info.detail)}">${this.escapeHtml(info.detail)}</span>` : ''}</div>`;
    },

    /**
     * Initialize LLM Manager when tab is loaded
     */
    async init() {
        this.setupSubTabHandlers();
        this.setupModelEditor();
        this.setupMappingEditor();
        this.loadModelCatalogProviders();

        // Add event listener to clean up modal when closed
        const modalEl = document.getElementById('addModelModal');
        if (modalEl && !modalEl.dataset.cleanupBound) {
            modalEl.dataset.cleanupBound = 'true';
            modalEl.addEventListener('hidden.bs.modal', () => {
                // Reset form to prevent data leakage between edits
                document.getElementById('addModelForm').reset();
                document.getElementById('modelExtraBody').value = '';
                document.getElementById('modelEditMode').value = 'false';
                this.closeModelCatalog();
            });
        }

        await this.activateSubTab(this.getSubTabFromPath(), { history: false });
    },

    setupSubTabHandlers() {
        if (this.subTabHandlersReady) return;
        this.subTabHandlersReady = true;

        document.querySelectorAll('#llm > ul.nav [data-bs-target^="#llm-"]').forEach(button => {
            button.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                const target = button.getAttribute('data-bs-target') || '';
                this.activateSubTab(target.replace(/^#/, ''));
            });
        });
    },

    getSubTabFromPath() {
        const path = UI.normalizePath(window.location.pathname);
        return {
            '/ai/mappings': 'llm-mappings',
            '/ai/usage': 'llm-stats',
            '/ai/sessions': 'llm-codex-jobs',
            '/ai/calls': 'llm-history',
            '/ai/network': 'llm-proxy'
        }[path] || 'llm-models';
    },

    async activateSubTab(targetId, options = {}) {
        const panes = ['llm-history', 'llm-models', 'llm-mappings', 'llm-stats', 'llm-codex-jobs', 'llm-proxy'];
        if (!panes.includes(targetId)) targetId = 'llm-models';
        panes.forEach(id => {
            const pane = document.getElementById(id);
            const button = document.querySelector(`#llm > ul.nav [data-bs-target="#${id}"]`);

            const active = id === targetId;
            if (pane) {
                pane.classList.toggle('show', active);
                pane.classList.toggle('active', active);
                pane.classList.toggle('d-none', !active);
            }
            if (button) {
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
            }
        });

        if (options.history !== false) {
            const paths = {
                'llm-models': '/ai/models',
                'llm-mappings': '/ai/mappings',
                'llm-stats': '/ai/usage',
                'llm-codex-jobs': '/ai/sessions',
                'llm-history': '/ai/calls',
                'llm-proxy': '/ai/network'
            };
            const path = paths[targetId];
            if (UI.normalizePath(window.location.pathname) !== path) {
                window.history.pushState({ tab: 'llm', section: targetId }, '', path);
            }
        }

        if (options.load === false) return;
        await this.loadSubTab(targetId);
    },

    async loadSubTab(targetId) {
        if (targetId === 'llm-models') return this.loadModels();
        if (targetId === 'llm-mappings') {
            if (!Object.keys(this.currentModels || {}).length) {
                await this.loadModels();
            }
            return this.loadMappings();
        }
        if (targetId === 'llm-stats') return this.loadStats();
        if (targetId === 'llm-codex-jobs') return this.loadCodexJobs();
        if (targetId === 'llm-proxy') return this.loadProxy();
        return this.loadCallHistory();
    },

    setupModelEditor() {
        const form = document.getElementById('addModelForm');
        if (!form || form.dataset.editorBound) return;
        form.dataset.editorBound = 'true';

        const modelName = document.getElementById('modelName');
        const modelId = document.getElementById('modelId');
        const template = document.getElementById('modelTemplateSelect');
        modelName?.addEventListener('focus', () => this.openModelCatalog());
        modelName?.addEventListener('input', () => {
            this.renderModelCatalogMenu(modelName.value);
            if (this.modelIdAutofill) this.suggestModelId(modelName.value);
            this.updateModelFormReview();
        });
        modelName?.addEventListener('keydown', event => {
            if (event.key === 'Escape') this.closeModelCatalog();
        });
        modelId?.addEventListener('input', event => {
            if (event.isTrusted) this.modelIdAutofill = false;
            this.updateModelFormReview();
        });
        template?.addEventListener('change', event => this.cloneModelConfig(event.target.value));
        form.addEventListener('input', event => {
            if (event.target !== modelName && event.target !== modelId) this.updateModelFormReview();
        });
        form.addEventListener('change', () => this.updateModelFormReview());
        document.addEventListener('click', event => {
            if (!event.target.closest('.model-catalog-picker')) this.closeModelCatalog();
        });
    },

    setupMappingEditor() {
        const primary = document.getElementById('mappingPrimary');
        const fallback = document.getElementById('mappingFallback');
        if (!primary || primary.dataset.samplingBound) return;
        primary.dataset.samplingBound = 'true';
        primary.addEventListener('change', () => this.updateMappingSamplingControls());
        fallback?.addEventListener('change', () => this.updateMappingSamplingControls());
    },

    async loadModelCatalogProviders() {
        try {
            const response = await fetch('/api/llm/models/catalog');
            const result = await response.json();
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '目录加载失败');
            }
            this.catalogProviders = result.data?.providers || [];
            this.catalogVersion = result.data?.version || '';
            const select = document.getElementById('modelOtherProvider');
            if (select) {
                const selected = select.value;
                select.innerHTML = '<option value="">选择供应商…</option>' + this.catalogProviders
                    .map(provider => `<option value="${this.escapeHtml(provider.id)}">${this.escapeHtml(provider.label)} · ${Number(provider.model_count || 0).toLocaleString()} 个模型</option>`)
                    .join('');
                if (selected && this.catalogProviders.some(provider => provider.id === selected)) {
                    select.value = selected;
                }
            }
            this.updateModelFormReview();
        } catch (error) {
            console.warn('LiteLLM catalog providers unavailable:', error);
            this.catalogProviders = [];
        }
    },

    getModelManagementMeta(config = {}) {
        if (config._management && typeof config._management === 'object') return config._management;
        const provider = this.inferProviderKey(config);
        const hasCredential = Boolean(config.api_key);
        return {
            provider,
            credential: {
                mode: provider === 'local_codex' ? 'none' : (hasCredential ? 'direct' : 'missing'),
                configured: hasCredential,
                environment_variable: '',
            },
            share_ready: provider === 'local_codex',
            mapping_count: 0,
            mapped_by: [],
        };
    },

    inferProviderKey(config = {}) {
        const explicit = String(config.custom_llm_provider || config.provider || '').toLowerCase();
        const model = String(config.model || '').toLowerCase();
        const apiBase = String(config.api_base || '').toLowerCase();
        if (['local_codex', 'local_codex_cli'].includes(explicit) || apiBase.includes('/api/codex/')) return 'local_codex';
        if (explicit === 'custom_openai') return 'compatible';
        if (explicit) return explicit;
        if (model.startsWith('openrouter/') || apiBase.includes('openrouter.ai')) return 'openrouter';
        for (const provider of ['anthropic', 'gemini', 'deepseek', 'azure', 'bedrock', 'vertex_ai', 'mistral', 'groq', 'xai']) {
            if (model.startsWith(`${provider}/`)) return provider;
        }
        return apiBase ? 'compatible' : 'openai';
    },

    isGemini3ModelConfig(config = {}) {
        const disabled = this.getModelManagementMeta(config).disabled_parameters || [];
        if (disabled.includes('temperature')) return true;
        const provider = this.inferProviderKey(config);
        const model = String(config.model || '').toLowerCase();
        return ['gemini', 'vertex_ai'].includes(provider) && model.includes('gemini-3');
    },

    isGemini3FormSelection(provider, modelName) {
        const providerId = String(
            provider?.key === 'other'
                ? provider.catalogProvider
                : (provider?.providerValue || provider?.key || '')
        ).toLowerCase();
        return ['gemini', 'vertex_ai'].includes(providerId)
            && String(modelName || '').toLowerCase().includes('gemini-3');
    },

    stripGeminiSamplingParameters(value) {
        if (!value || typeof value !== 'object') return value;
        const cleaned = JSON.parse(JSON.stringify(value));
        const stripKnownContainers = object => {
            if (!object || typeof object !== 'object' || Array.isArray(object)) return;
            for (const key of ['temperature', 'top_p', 'top_k', 'topP', 'topK']) delete object[key];
            for (const key of ['extra_body', 'generation_config', 'generationConfig']) {
                stripKnownContainers(object[key]);
                if (object[key] && typeof object[key] === 'object' && !Object.keys(object[key]).length) {
                    delete object[key];
                }
            }
        };
        stripKnownContainers(cleaned);
        return cleaned;
    },

    updateModelSamplingControls(provider = null, modelName = null) {
        provider ||= this.getActiveProviderPreset();
        modelName ??= this.normalizePastedText(document.getElementById('modelName')?.value || '').trim();
        const disabled = this.isGemini3FormSelection(provider, modelName);
        const group = document.getElementById('modelTemperatureGroup');
        const input = document.getElementById('modelTemp');
        const notice = document.getElementById('modelSamplingNotice');
        group?.classList.toggle('d-none', disabled);
        notice?.classList.toggle('d-none', !disabled);
        if (input) {
            input.disabled = disabled;
            if (disabled) input.value = '';
        }
        return disabled;
    },

    updateMappingSamplingControls() {
        const primaryId = document.getElementById('mappingPrimary')?.value || '';
        const fallbackId = document.getElementById('mappingFallback')?.value || '';
        const primaryIsGemini3 = this.isGemini3ModelConfig(this.currentModels[primaryId] || {});
        const fallbackIsGemini3 = this.isGemini3ModelConfig(this.currentModels[fallbackId] || {});
        const group = document.getElementById('mappingOverrideTemperatureGroup');
        const input = document.getElementById('mappingOverrideTemp');
        const notice = document.getElementById('mappingSamplingNotice');
        group?.classList.toggle('d-none', primaryIsGemini3);
        if (input) {
            input.disabled = primaryIsGemini3;
            if (primaryIsGemini3) input.value = '';
        }
        if (notice) {
            notice.classList.toggle('d-none', !(primaryIsGemini3 || fallbackIsGemini3));
            notice.textContent = primaryIsGemini3
                ? 'Gemini 3+ 使用模型默认采样设置；路由中的 temperature、top_p 和 top_k 不会保存或发送。'
                : 'Gemini 3+ 备用模型使用默认采样设置；此处参数只会应用到支持它们的模型。';
        }
        return primaryIsGemini3;
    },

    getProviderLabel(providerKey) {
        if (this.providerPresets[providerKey]) return this.providerPresets[providerKey].label;
        return this.catalogProviders.find(provider => provider.id === providerKey)?.label
            || String(providerKey || '未知供应商').replace(/_/g, ' ');
    },

    getActiveProviderPreset() {
        const presetKey = document.getElementById('modelProviderPreset')?.value || 'openai';
        if (presetKey !== 'other') return { key: presetKey, ...this.providerPresets[presetKey] };
        const providerKey = document.getElementById('modelOtherProvider')?.value || '';
        const providerInfo = this.catalogProviders.find(provider => provider.id === providerKey);
        return {
            key: 'other',
            label: providerInfo?.label || '其他 LiteLLM 供应商',
            catalogProvider: providerKey,
            providerValue: providerKey,
            envVar: providerInfo?.environment_variable || (providerKey ? `${providerKey.toUpperCase().replace(/[^A-Z0-9]+/g, '_')}_API_KEY` : 'LLM_API_KEY'),
            requiresCredential: true,
            modelPlaceholder: providerKey ? `输入或选择 ${providerInfo?.label || providerKey} 模型` : '先选择 LiteLLM 供应商',
            apiBase: '',
            apiBaseRequired: false,
        };
    },

    async applyProviderPreset(presetKey, options = {}) {
        const preset = this.providerPresets[presetKey];
        if (!preset) return;
        const preserveValues = options.preserveValues === true;
        document.getElementById('modelProviderPreset').value = presetKey;
        document.querySelectorAll('.model-provider-option').forEach(button => {
            const active = button.dataset.modelProvider === presetKey;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        document.getElementById('modelOtherProviderGroup')?.classList.toggle('d-none', presetKey !== 'other');
        document.getElementById('modelProviderOverrideGroup')?.classList.toggle('d-none', !['compatible', 'other'].includes(presetKey));
        const isCodex = presetKey === 'local_codex';
        document.getElementById('modelCodexReasoningGroup')?.classList.toggle('d-none', !isCodex);
        document.getElementById('modelCodexIsolatedGroup')?.classList.toggle('d-none', !isCodex);
        document.getElementById('modelCredentialSection')?.classList.toggle('d-none', isCodex);
        document.getElementById('modelApiBaseRequired')?.classList.toggle('d-none', !preset.apiBaseRequired);
        const apiBaseHelp = document.getElementById('modelApiBaseHelp');
        if (apiBaseHelp) {
            apiBaseHelp.textContent = preset.apiBaseRequired
                ? '填写兼容 OpenAI Chat Completions 的完整基础地址，通常以 /v1 结尾。'
                : '官方供应商通常无需填写；代理、私有部署或网关场景可覆盖。';
        }
        const webSearchLabel = document.getElementById('modelWebSearchLabel');
        if (webSearchLabel) webSearchLabel.textContent = isCodex ? '允许 Codex 使用 Web 搜索' : '启用供应商 Web 搜索';
        const modelName = document.getElementById('modelName');
        if (modelName) modelName.placeholder = preset.modelPlaceholder;

        if (!preserveValues) {
            if (modelName) modelName.value = '';
            document.getElementById('modelApiBase').value = preset.apiBase || '';
            document.getElementById('modelProvider').value = preset.providerValue || '';
            document.getElementById('modelApiKeyEnv').value = preset.envVar || '';
            document.getElementById('modelApiKeyEnvValue').value = '';
            const credentialMode = document.getElementById('modelCredentialMode');
            credentialMode.value = isCodex ? 'none' : 'environment';
            credentialMode.dataset.canPreserve = 'false';
            credentialMode.dataset.originalMode = '';
            document.getElementById('modelMaxTokens').value = '';
            document.getElementById('modelContextWindow').value = '';
            document.getElementById('modelTimeout').value = isCodex ? '600' : '';
            document.getElementById('modelMaxRetries').value = isCodex ? '0' : '';
            document.getElementById('modelVision').checked = false;
            document.getElementById('modelWebSearch').checked = false;
            document.getElementById('modelExtraBody').value = '';
        }
        this.updateCredentialFields();
        if (presetKey === 'other') {
            if (!this.catalogProviders.length) await this.loadModelCatalogProviders();
            const selectedProvider = document.getElementById('modelOtherProvider')?.value;
            if (selectedProvider) await this.selectOtherProvider(selectedProvider, { preserveValues });
            else this.setCatalogState([], '请先从上方选择一个 LiteLLM 供应商。');
        } else if (preset.catalogProvider) {
            await this.loadModelCatalog(preset.catalogProvider);
        } else {
            this.setCatalogState([], '自定义接口的模型由服务端决定，请直接输入模型名称。');
        }
        this.updateModelFormReview();
    },

    async selectOtherProvider(providerKey, options = {}) {
        if (!providerKey) {
            this.setCatalogState([], '请先选择一个 LiteLLM 供应商。');
            this.updateModelFormReview();
            return;
        }
        const preset = this.getActiveProviderPreset();
        document.getElementById('modelProvider').value = providerKey;
        document.getElementById('modelName').placeholder = preset.modelPlaceholder;
        if (!options.preserveValues) {
            document.getElementById('modelApiKeyEnv').value = preset.envVar;
            document.getElementById('modelApiKeyEnvValue').value = '';
            document.getElementById('modelName').value = '';
        }
        await this.loadModelCatalog(providerKey);
        this.updateCredentialFields();
        this.updateModelFormReview();
    },

    async loadModelCatalog(providerKey) {
        const requestId = ++this.catalogRequestId;
        const hint = document.getElementById('modelCatalogHint');
        if (hint) hint.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>正在读取本机 LiteLLM 模型目录…';
        try {
            const response = await fetch(`/api/llm/models/catalog?provider=${encodeURIComponent(providerKey)}&limit=300`);
            const result = await response.json();
            if (requestId !== this.catalogRequestId) return;
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || result.message || '模型目录加载失败');
            }
            this.catalogVersion = result.data?.version || this.catalogVersion;
            this.setCatalogState(result.data?.models || []);
        } catch (error) {
            if (requestId !== this.catalogRequestId) return;
            console.warn(`Catalog unavailable for ${providerKey}:`, error);
            this.setCatalogState([], '目录暂时不可用，仍可手动输入模型名称。');
        }
    },

    setCatalogState(models, message = '') {
        this.catalogModels = Array.isArray(models) ? models : [];
        const hint = document.getElementById('modelCatalogHint');
        if (hint) {
            const version = this.catalogVersion ? ` · LiteLLM ${this.catalogVersion}` : '';
            hint.textContent = message || `可选择 ${this.catalogModels.length} 个聊天模型${version}；目录外的新模型也可直接输入。`;
        }
        this.renderModelCatalogMenu(document.getElementById('modelName')?.value || '');
    },

    openModelCatalog() {
        const menu = document.getElementById('modelCatalogMenu');
        const input = document.getElementById('modelName');
        if (!menu || !input) return;
        this.renderModelCatalogMenu(input.value);
        menu.classList.remove('d-none');
        input.setAttribute('aria-expanded', 'true');
    },

    closeModelCatalog() {
        document.getElementById('modelCatalogMenu')?.classList.add('d-none');
        document.getElementById('modelName')?.setAttribute('aria-expanded', 'false');
    },

    toggleModelCatalog() {
        const menu = document.getElementById('modelCatalogMenu');
        if (!menu) return;
        if (menu.classList.contains('d-none')) {
            this.openModelCatalog();
            document.getElementById('modelName')?.focus();
        } else {
            this.closeModelCatalog();
        }
    },

    renderModelCatalogMenu(query = '') {
        const menu = document.getElementById('modelCatalogMenu');
        if (!menu) return;
        const needle = String(query || '').trim().toLowerCase();
        const matches = this.catalogModels
            .filter(item => !needle || String(item.id).toLowerCase().includes(needle))
            .slice(0, 40);
        if (!matches.length) {
            menu.innerHTML = `
                <div class="model-catalog-empty">
                    <i class="bi bi-pencil-square"></i>
                    <strong>${this.catalogModels.length ? '目录中没有匹配项' : '当前没有可用目录'}</strong>
                    <span>可以继续手动输入，保存时会使用所选供应商适配器。</span>
                </div>`;
            return;
        }
        menu.innerHTML = matches.map(item => {
            const capabilities = [];
            if (item.supports_vision) capabilities.push('图片');
            if (item.supports_reasoning) capabilities.push('推理');
            if (item.supports_web_search) capabilities.push('搜索');
            const context = item.max_input_tokens ? this.formatTokenCount(item.max_input_tokens) : '';
            return `
                <button type="button" class="model-catalog-item" role="option"
                    data-model-catalog-id="${this.escapeHtml(item.id)}">
                    <span class="model-catalog-item-main">
                        <strong>${this.escapeHtml(item.id)}</strong>
                        <small>${item.recommended ? '常用名称' : '版本/预览模型'}${context ? ` · 上下文 ${context}` : ''}</small>
                    </span>
                    <span class="model-catalog-capabilities">${capabilities.map(value => `<em>${value}</em>`).join('')}</span>
                </button>`;
        }).join('');
        menu.querySelectorAll('[data-model-catalog-id]').forEach(button => {
            button.addEventListener('click', () => this.selectCatalogModel(button.dataset.modelCatalogId));
        });
    },

    selectCatalogModel(modelId) {
        const item = this.catalogModels.find(model => model.id === modelId);
        const input = document.getElementById('modelName');
        if (!input) return;
        input.value = modelId;
        if (this.modelIdAutofill) this.suggestModelId(modelId);
        if (item) {
            const contextInput = document.getElementById('modelContextWindow');
            const maxTokensInput = document.getElementById('modelMaxTokens');
            if (contextInput && !contextInput.value && item.max_input_tokens) contextInput.value = item.max_input_tokens;
            if (maxTokensInput && !maxTokensInput.value && item.max_output_tokens) maxTokensInput.value = item.max_output_tokens;
            if (item.supports_vision) document.getElementById('modelVision').checked = true;
        }
        this.closeModelCatalog();
        this.updateModelFormReview();
    },

    suggestModelId(modelName) {
        const input = document.getElementById('modelId');
        if (!input) return;
        const parts = String(modelName || '').split('/').filter(Boolean);
        const source = parts.at(-1) || this.getActiveProviderPreset().catalogProvider || 'model';
        let suggestion = source
            .toLowerCase()
            .replace(/[^a-z0-9._-]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 60) || 'model';
        const currentId = document.getElementById('modelEditMode')?.value === 'true'
            ? input.dataset.oldId
            : '';
        if (this.currentModels[suggestion] && suggestion !== currentId) {
            let suffix = 2;
            while (this.currentModels[`${suggestion}-${suffix}`]) suffix += 1;
            suggestion = `${suggestion}-${suffix}`;
        }
        input.value = suggestion;
    },

    formatTokenCount(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '-';
        if (number >= 1000000) return `${(number / 1000000).toFixed(number % 1000000 ? 1 : 0)}M`;
        if (number >= 1000) return `${(number / 1000).toFixed(number % 1000 ? 1 : 0)}K`;
        return number.toLocaleString();
    },

    renderSharedCredentialStatus() {
        const target = document.getElementById('modelSharedCredentialStatus');
        if (!target) return;
        const status = this.sharedCredentialStatus || {};
        const content = {
            loading: ['bi-arrow-repeat', '正在检查…'],
            pending: ['bi-shield-plus', '保存时写入本机'],
            preserved: ['bi-shield-check', '使用已保存的 API Key'],
            database: ['bi-shield-check', 'API Key 已保存在本机'],
            environment: ['bi-shield-check', 'API Key 已由环境变量提供'],
            missing: ['bi-exclamation-circle', '请填写 API Key'],
            invalid: ['bi-exclamation-circle', '凭据配置无效'],
            optional: ['bi-info-circle', '免认证接口可留空'],
        }[status.source] || ['bi-info-circle', '尚未配置 API Key'];
        target.className = `model-credential-status ${status.configured ? 'ready' : status.source === 'missing' || status.source === 'invalid' ? 'missing' : ''}`;
        target.innerHTML = `<i class="bi ${content[0]}"></i><span>${content[1]}</span>`;
    },

    async loadSharedCredentialStatus(rawName, options = {}) {
        const name = String(rawName || '').trim();
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
            this.sharedCredentialStatus = { name, configured: false, source: name ? 'invalid' : 'unknown' };
            this.renderSharedCredentialStatus();
            this.updateModelFormReview();
            return this.sharedCredentialStatus;
        }
        const requestId = ++this.credentialStatusRequestId;
        this.sharedCredentialStatus = { name, configured: false, source: 'loading' };
        this.renderSharedCredentialStatus();
        try {
            const response = await fetch(`/api/llm/credentials/${encodeURIComponent(name)}`);
            const result = await response.json().catch(() => ({}));
            if (requestId !== this.credentialStatusRequestId) return this.sharedCredentialStatus;
            if (!response.ok || result.status !== 'success') throw new Error(result.detail || '无法检查凭据');
            this.sharedCredentialStatus = result.data || { name, configured: false, source: 'missing' };
            if (options.optional && !this.sharedCredentialStatus.configured) {
                this.sharedCredentialStatus.source = 'optional';
            }
        } catch (error) {
            if (requestId !== this.credentialStatusRequestId) return this.sharedCredentialStatus;
            this.sharedCredentialStatus = { name, configured: false, source: 'unknown' };
        }
        this.renderSharedCredentialStatus();
        this.updateModelFormReview();
        return this.sharedCredentialStatus;
    },

    async ensureSharedCredential(name, value) {
        if (value) {
            const response = await fetch(`/api/llm/credentials/${encodeURIComponent(name)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value }),
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok || result.status !== 'success') {
                throw new Error(result.detail || '本机凭据保存失败');
            }
            this.sharedCredentialStatus = result.data;
            this.renderSharedCredentialStatus();
            return;
        }
        const status = await this.loadSharedCredentialStatus(name);
        if (!status?.configured) {
            throw new Error(`凭据 ${name} 尚未配置，请填写密钥值后再保存。`);
        }
    },

    onUnifiedCredentialInput() {
        const value = document.getElementById('modelApiKeyEnvValue')?.value.trim() || '';
        const modeInput = document.getElementById('modelCredentialMode');
        if (!modeInput) return;
        if (value) {
            modeInput.value = 'environment';
            this.sharedCredentialStatus = {
                name: document.getElementById('modelApiKeyEnv')?.value || '',
                configured: true,
                source: 'pending',
            };
            this.renderSharedCredentialStatus();
            this.updateCredentialFields({ checkStatus: false });
            return;
        }
        modeInput.value = modeInput.dataset.canPreserve === 'true'
            ? 'preserve'
            : (this.getActiveProviderPreset().requiresCredential ? 'environment' : 'none');
        this.updateCredentialFields();
    },

    getEffectiveCredentialMode(provider, isEdit, envVar, envValue) {
        if (provider.key === 'local_codex') return 'none';
        const requestedMode = document.getElementById('modelCredentialMode')?.value || 'environment';
        if (envValue) return 'environment';
        if (requestedMode === 'preserve' && isEdit) return 'preserve';
        if (provider.requiresCredential) return 'environment';
        const credentialReady = this.sharedCredentialStatus?.name === envVar
            && this.sharedCredentialStatus?.configured;
        return credentialReady ? 'environment' : 'none';
    },

    updateCredentialFields(options = {}) {
        const mode = document.getElementById('modelCredentialMode')?.value || 'environment';
        const provider = this.getActiveProviderPreset();
        const envVar = document.getElementById('modelApiKeyEnv')?.value || '';
        const keyValue = document.getElementById('modelApiKeyEnvValue')?.value.trim() || '';
        if (keyValue) {
            this.credentialStatusRequestId += 1;
            this.sharedCredentialStatus = { name: envVar, configured: true, source: 'pending' };
            this.renderSharedCredentialStatus();
        } else if (mode === 'preserve') {
            this.credentialStatusRequestId += 1;
            this.sharedCredentialStatus = { name: envVar, configured: true, source: 'preserved' };
            this.renderSharedCredentialStatus();
        } else if (mode === 'none' && options.checkStatus !== true) {
            this.credentialStatusRequestId += 1;
            this.sharedCredentialStatus = { name: envVar, configured: false, source: 'optional' };
            this.renderSharedCredentialStatus();
        } else if (options.checkStatus !== false) {
            this.loadSharedCredentialStatus(envVar, { optional: !provider.requiresCredential });
        }
        this.updateModelFormReview();
    },

    updateModelFormReview() {
        const container = document.getElementById('modelFormReviewContent');
        if (!container) return;
        const provider = this.getActiveProviderPreset();
        const modelId = this.sanitizeModelId(document.getElementById('modelId')?.value || '');
        const modelName = this.normalizePastedText(document.getElementById('modelName')?.value || '').trim();
        const apiBase = this.normalizePastedText(document.getElementById('modelApiBase')?.value || '').trim();
        const envVar = this.sanitizeCredentialName(document.getElementById('modelApiKeyEnv')?.value || '');
        const envValue = document.getElementById('modelApiKeyEnvValue')?.value.trim() || '';
        const isEdit = document.getElementById('modelEditMode')?.value === 'true';
        this.updateModelSamplingControls(provider, modelName);
        const mode = this.getEffectiveCredentialMode(provider, isEdit, envVar, envValue);
        const checks = [
            { ok: provider.key !== 'other' || Boolean(provider.catalogProvider), label: provider.label || '选择供应商' },
            { ok: Boolean(modelName), label: modelName || '选择或输入服务端模型' },
            { ok: Boolean(modelId) && !/[\\/?#]/.test(modelId), label: modelId ? `配置名：${modelId}` : '填写配置名称' },
            { ok: !provider.apiBaseRequired || /^https?:\/\//i.test(apiBase), label: provider.apiBaseRequired ? (apiBase ? 'API 地址已填写' : '填写 API 地址') : '连接地址可用默认值' },
            {
                ok: provider.key === 'local_codex'
                    || mode === 'none'
                    || (mode === 'preserve' && isEdit)
                    || (mode === 'environment' && /^[A-Za-z_][A-Za-z0-9_]*$/.test(envVar)
                        && (Boolean(envValue) || (this.sharedCredentialStatus?.name === envVar && this.sharedCredentialStatus?.configured))),
                label: mode === 'environment' ? (envValue ? '新的 API Key 将保存到本机'
                    : this.sharedCredentialStatus?.name === envVar && this.sharedCredentialStatus?.configured
                        ? '本机 API Key 已就绪' : '填写 API Key')
                    : mode === 'preserve' ? '已有 API Key 将保持不变' : '此接口不需要 API Key',
            },
        ];
        container.innerHTML = `
            <div class="model-review-provider">
                <span>接入方式</span><strong>${this.escapeHtml(provider.label || '待选择')}</strong>
            </div>
            <div class="model-review-checks">
                ${checks.map(check => `
                    <div class="${check.ok ? (check.warning ? 'warning' : 'ready') : 'pending'}">
                        <i class="bi ${check.ok ? (check.warning ? 'bi-exclamation-circle' : 'bi-check-circle') : 'bi-circle'}"></i>
                        <span>${this.escapeHtml(check.label)}</span>
                    </div>`).join('')}
            </div>
            <div class="model-review-result ${checks.every(check => check.ok) ? 'ready' : ''}">
                ${checks.every(check => check.ok) ? '可以保存配置' : '完成左侧必填项后即可保存'}
            </div>`;
    },

    filterModels() {
        this.renderModels();
    },

    /**
     * Load all models
     */
    async loadModels() {
        try {
            const response = await fetch('/api/llm/models');
            const result = await response.json();

            if (result.status === 'success') {
                this.currentModels = result.data || {};
                this.renderModels();
            } else {
                throw new Error(result.message || '未知错误');
            }
        } catch (error) {
            console.error('Failed to load models:', error);
            const container = document.getElementById('llmModelsList');
            if (container) {
                container.innerHTML = `
                    <div class="col-12">
                        <div class="alert alert-danger">
                            <i class="bi bi-exclamation-triangle me-2"></i>
                            加载模型失败：${this.escapeHtml(error.message)}
                        </div>
                    </div>
                `;
            }
        }
    },

    /**
     * Render models list
     */
    renderModels() {
        const container = document.getElementById('llmModelsList');
        if (!container) return;
        if (this.modelSortable) {
            this.modelSortable.destroy();
            this.modelSortable = null;
        }
        try {
            const allEntries = Object.entries(this.currentModels || {}).filter(([, config]) => config);
            const search = document.getElementById('llmModelSearch')?.value.trim().toLowerCase() || '';
            const providerFilter = document.getElementById('llmProviderFilter');
            const selectedProvider = providerFilter?.value || '';
            const providers = [...new Set(allEntries.map(([, config]) => this.getModelManagementMeta(config).provider))]
                .filter(Boolean)
                .sort((a, b) => this.getProviderLabel(a).localeCompare(this.getProviderLabel(b), 'zh-CN'));
            if (providerFilter) {
                providerFilter.innerHTML = '<option value="">全部供应商</option>' + providers
                    .map(provider => `<option value="${this.escapeHtml(provider)}">${this.escapeHtml(this.getProviderLabel(provider))}</option>`)
                    .join('');
                providerFilter.value = providers.includes(selectedProvider) ? selectedProvider : '';
            }
            const activeProvider = providerFilter?.value || '';
            const entries = allEntries.filter(([modelId, config]) => {
                const meta = this.getModelManagementMeta(config);
                const matchesProvider = !activeProvider || meta.provider === activeProvider;
                const haystack = `${modelId} ${config.model || ''} ${this.getProviderLabel(meta.provider)} ${config.api_base || ''}`.toLowerCase();
                return matchesProvider && (!search || haystack.includes(search));
            });

            const envCount = allEntries.filter(([, config]) => this.getModelManagementMeta(config).credential?.mode === 'environment').length;
            const directCount = allEntries.filter(([, config]) => this.getModelManagementMeta(config).credential?.mode === 'direct').length;
            const missingCount = allEntries.filter(([, config]) => {
                const credential = this.getModelManagementMeta(config).credential || {};
                return credential.mode === 'missing' || (credential.mode === 'environment' && !credential.configured);
            }).length;
            const summary = document.getElementById('llmModelSummary');
            if (summary) {
                summary.innerHTML = `
                    <span><strong>${allEntries.length}</strong> 个模型连接</span>
                    <span class="ready"><i class="bi bi-shield-check"></i>${envCount} 个 API Key 已统一管理</span>
                    ${directCount ? `<span class="warning"><i class="bi bi-exclamation-circle"></i>${directCount} 个使用旧版凭据</span>` : ''}
                    ${missingCount ? `<span class="muted"><i class="bi bi-key"></i>${missingCount} 个未配置密钥</span>` : ''}
                    ${entries.length !== allEntries.length ? `<span class="filter-result">当前显示 ${entries.length} 个</span>` : ''}`;
            }

            if (!allEntries.length) {
                container.innerHTML = `
                    <div class="llm-model-empty">
                        <i class="bi bi-boxes"></i>
                        <strong>还没有模型连接</strong>
                        <span>从常用供应商开始，通常一分钟内即可完成。</span>
                        <button class="btn btn-primary btn-sm" onclick="LLMManager.showAddModelModal()">添加第一个模型</button>
                    </div>`;
                return;
            }
            if (!entries.length) {
                container.innerHTML = `
                    <div class="llm-model-empty compact">
                        <i class="bi bi-search"></i>
                        <strong>没有匹配的模型</strong>
                        <span>调整搜索词或供应商筛选。</span>
                    </div>`;
                return;
            }

            let html = '';
            for (const [modelId, config] of entries) {
                const meta = this.getModelManagementMeta(config);
                const credential = meta.credential || {};
                const safeModelId = this.escapeHtml(modelId);
                const safeConfiguredModel = this.escapeHtml(config.model || '未知');
                const domModelId = this.getModelDomId(modelId);
                const providerLabel = this.escapeHtml(this.getProviderLabel(meta.provider));
                const credentialState = credential.mode === 'environment' && credential.configured
                    ? { className: 'ready', icon: 'bi-shield-check', label: 'API Key 已就绪' }
                    : credential.mode === 'environment'
                        ? { className: 'missing', icon: 'bi-key', label: 'API Key 未配置' }
                    : credential.mode === 'direct'
                        ? { className: 'warning', icon: 'bi-exclamation-circle', label: 'API Key 已配置（旧版）' }
                        : credential.mode === 'none'
                            ? { className: 'local', icon: 'bi-pc-display', label: '本地调用' }
                            : { className: 'missing', icon: 'bi-key', label: '未配置密钥' };
                const tokenPills = [];
                if (config.context_window_tokens || config.max_input_tokens) {
                    tokenPills.push(`<span title="上下文窗口"><i class="bi bi-arrows-expand"></i>${this.formatTokenCount(config.context_window_tokens || config.max_input_tokens)}</span>`);
                }
                if (config.max_tokens) tokenPills.push(`<span title="最大输出"><i class="bi bi-box-arrow-up"></i>${this.formatTokenCount(config.max_tokens)}</span>`);
                if (config.supports_vision) tokenPills.push('<span title="支持图片"><i class="bi bi-image"></i>图片</span>');
                if (config.enable_web_search || config.codex_web_search) tokenPills.push('<span title="启用 Web 搜索"><i class="bi bi-globe"></i>搜索</span>');
                html += `
                    <div class="col-md-6 col-lg-4" data-id="${safeModelId}" data-provider="${this.escapeHtml(meta.provider)}">
                        <div class="card h-100 llm-model-card">
                            <div class="card-body">
                                <div class="llm-model-card-topline">
                                    <span class="llm-provider-badge"><i class="bi bi-cpu"></i>${providerLabel}</span>
                                    <i class="bi bi-grip-vertical drag-handle" title="拖动排序"></i>
                                </div>
                                <div class="llm-model-identity">
                                    <h6>${safeModelId}</h6>
                                    <code title="${safeConfiguredModel}">${safeConfiguredModel}</code>
                                </div>
                                <div class="llm-model-status ${credentialState.className}">
                                    <i class="bi ${credentialState.icon}"></i>
                                    <span>${this.escapeHtml(credentialState.label)}</span>
                                    ${meta.mapping_count ? `<em>${Number(meta.mapping_count)} 个任务在用</em>` : '<em>尚未分配任务</em>'}
                                </div>
                                ${config.api_base ? `<div class="llm-model-endpoint" title="${this.escapeHtml(config.api_base)}"><i class="bi bi-link-45deg"></i>${this.escapeHtml(config.api_base)}</div>` : ''}
                                <div class="llm-model-capabilities">
                                    ${this.isGemini3ModelConfig(config)
                                        ? '<span title="Gemini 3+ 使用模型默认采样设置"><i class="bi bi-stars"></i>默认采样</span>'
                                        : `<span title="温度"><i class="bi bi-thermometer-half"></i>${this.formatTemperature(config.temperature, '默认')}</span>`}
                                    ${tokenPills.join('')}
                                </div>
                                <div id="modelTestResult_${domModelId}" class="llm-model-test-result" style="display:none;"></div>
                                <div class="llm-model-actions">
                                    <button class="btn btn-sm btn-outline-primary llm-model-test" id="testModelBtn_${domModelId}" data-model-id="${safeModelId}">
                                        <i class="bi bi-lightning-charge me-1"></i>测试
                                    </button>
                                    <button class="btn btn-sm btn-outline-secondary llm-model-edit" data-model-id="${safeModelId}">
                                        <i class="bi bi-pencil me-1"></i>编辑
                                    </button>
                                    <button class="btn btn-sm btn-link text-danger llm-model-delete" data-model-id="${safeModelId}" title="删除">
                                        <i class="bi bi-trash"></i>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
            container.querySelectorAll('.llm-model-test').forEach(button => {
                button.addEventListener('click', () => this.testModelConnectivity(button.dataset.modelId));
            });
            container.querySelectorAll('.llm-model-edit').forEach(button => {
                button.addEventListener('click', event => {
                    event.preventDefault();
                    this.editModel(button.dataset.modelId);
                });
            });
            container.querySelectorAll('.llm-model-delete').forEach(button => {
                button.addEventListener('click', event => {
                    event.preventDefault();
                    this.deleteModel(button.dataset.modelId);
                });
            });

            if (window.Sortable && !search && !activeProvider) {
                this.modelSortable = new Sortable(container, {
                    animation: 150,
                    handle: '.drag-handle',
                    ghostClass: 'bg-light',
                    onEnd: async () => {
                        const items = container.querySelectorAll('[data-id]');
                        const order = Array.from(items).map(el => el.getAttribute('data-id'));
                        try {
                            const res = await fetch('/api/llm/models/reorder', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ order })
                            });
                            const result = await res.json().catch(() => ({}));
                            if (!res.ok || result.status !== 'success') {
                                throw new Error(result.detail || result.message || '调整顺序失败');
                            }
                            UI.showSuccess('模型顺序已保存');
                        } catch (e) {
                            console.error('Failed to save new order', e);
                            UI.showError(`保存模型顺序失败：${e.message}`);
                        }
                    }
                });
            }
        } catch (e) {
            console.error('Error rendering models:', e);
                container.innerHTML = `<div class="alert alert-danger">渲染模型失败：${this.escapeHtml(e.message)}</div>`;
        }
    },

    /**
     * Load plugin mappings
     */
    async loadMappings() {
        try {
            const [response, capabilityPayload] = await Promise.all([
                fetch('/api/llm/mappings'),
                fetch('/api/capabilities/')
                    .then(capabilityResponse => capabilityResponse.ok
                        ? capabilityResponse.json()
                        : { capabilities: [] })
                    .catch(error => {
                        console.warn('Capability metadata is unavailable:', error);
                        return { capabilities: [] };
                    }),
            ]);
            if (!response.ok) {
                throw new Error(`加载映射失败（HTTP ${response.status}）`);
            }
            const result = await response.json();

            if (result.status === 'success') {
                this.currentCapabilities = Object.fromEntries(
                    (capabilityPayload.capabilities || [])
                        .filter(capability => capability?.id)
                        .map(capability => [capability.id, capability])
                );
                this.currentMappings = this.mergeDeclaredTaskMappings(
                    result.data || {},
                    this.currentCapabilities,
                );
                this.renderMappings();
            } else {
                throw new Error(result.message || '加载映射时发生未知错误');
            }
        } catch (error) {
            console.error('Failed to load mappings:', error);
            const container = document.getElementById('llmMappingsList');
            if (container) {
                container.innerHTML = `<div class="alert alert-danger">加载映射失败：${this.escapeHtml(error.message)}</div>`;
            }
        }
    },

    /**
     * Render plugin mappings
     */
    renderMappings() {
        const container = document.getElementById('llmMappingsList');
        if (!container) return;

        if (!this.currentMappings || Object.keys(this.currentMappings).length === 0) {
            container.innerHTML = `
                <div class="text-center py-5 text-muted">
                    <i class="bi bi-diagram-3 fs-1 mb-3 d-block"></i>
                    <p>尚未配置插件映射。</p>
                </div>
            `;
            return;
        }

        const pluginEntries = Object.entries(this.currentMappings).sort(([leftName], [rightName]) => {
            const left = this.currentCapabilities[leftName] || {};
            const right = this.currentCapabilities[rightName] || {};
            if (Boolean(left.featured) !== Boolean(right.featured)) return left.featured ? -1 : 1;
            const categoryDelta = Number(left.category_order || 500) - Number(right.category_order || 500);
            if (categoryDelta !== 0) return categoryDelta;
            return this.getRouteCapabilityMeta(leftName).displayName.localeCompare(
                this.getRouteCapabilityMeta(rightName).displayName,
                'zh-CN'
            );
        });

        let html = '<div class="llm-route-groups">';
        for (const [pluginName, mappings] of pluginEntries) {
            const safePluginName = this.escapeHtml(pluginName);
            const capability = this.getRouteCapabilityMeta(pluginName);
            const taskEntries = Object.entries(mappings || {}).sort(([leftType], [rightType]) => {
                const left = this.getTaskRouteMeta(pluginName, leftType);
                const right = this.getTaskRouteMeta(pluginName, rightType);
                return left.order - right.order || left.label.localeCompare(right.label, 'zh-CN');
            });
            html += `
                <section class="llm-route-group">
                    <header class="llm-route-group-header">
                        <div class="llm-route-capability">
                            <span class="llm-route-capability-icon"><i class="bi ${this.escapeHtml(capability.icon)}"></i></span>
                            <div>
                                <div class="llm-route-capability-title">
                                    <h6>${this.escapeHtml(capability.displayName)}</h6>
                                    <span>${taskEntries.length} 项任务</span>
                                </div>
                                <p>${this.escapeHtml(capability.description)}</p>
                                <small>内部标识：<code>${safePluginName}</code></small>
                            </div>
                        </div>
                        <button class="btn btn-sm btn-outline-secondary llm-prompts-button" data-plugin-name="${safePluginName}">
                            <i class="bi bi-chat-quote me-1"></i>管理提示词
                        </button>
                    </header>
                    <div class="llm-route-table-wrap">
                        <table class="llm-route-table">
                            <thead>
                                <tr>
                                    <th scope="col">任务</th>
                                    <th scope="col">主模型</th>
                                    <th scope="col">失败后备用</th>
                                    <th scope="col">参数</th>
                                    <th scope="col" class="llm-route-action-column">操作</th>
                                </tr>
                            </thead>
                            <tbody>
            `;

            for (const [callType, mapping] of taskEntries) {
                const task = this.getTaskRouteMeta(pluginName, callType);
                const fallbackModels = Array.isArray(mapping.fallback) ? mapping.fallback : [];
                const fallback = fallbackModels[0] || '';
                const overrideParams = mapping.override_params && typeof mapping.override_params === 'object'
                    ? mapping.override_params
                    : {};
                const overrideKeys = Object.keys(overrideParams);
                const safeCallType = this.escapeHtml(callType);
                const routeKey = `${pluginName}.${callType}`;

                html += `
                    <tr class="llm-route-row${task.declared ? '' : ' is-undeclared'}">
                        <td class="llm-route-task-cell">
                            <div class="llm-route-task-title">
                                <strong>${this.escapeHtml(task.label)}</strong>
                                <span class="llm-route-category">${this.escapeHtml(task.category)}</span>
                                ${task.declared ? '' : '<span class="llm-route-declaration-warning">待插件补充声明</span>'}
                            </div>
                            <p>${this.escapeHtml(task.description)}</p>
                            <code class="llm-route-key" title="${this.escapeHtml(routeKey)}">${safeCallType}</code>
                        </td>
                        <td>${this.renderRouteModelCell(mapping.primary)}</td>
                        <td>
                            ${this.renderRouteModelCell(fallback, '未设置')}
                            ${fallbackModels.length > 1 ? `<span class="llm-route-more">另有 ${fallbackModels.length - 1} 个</span>` : ''}
                        </td>
                        <td>
                            ${overrideKeys.length > 0
                                ? `<span class="llm-route-overrides" title="${this.escapeHtml(JSON.stringify(overrideParams))}"><i class="bi bi-sliders me-1"></i>${overrideKeys.length} 项覆盖</span>`
                                : '<span class="llm-route-overrides is-empty">模型默认</span>'}
                        </td>
                        <td class="llm-route-row-action">
                            <button class="btn btn-sm btn-outline-primary llm-mapping-edit" data-plugin-name="${safePluginName}" data-call-type="${safeCallType}">
                                <i class="bi bi-pencil-square me-1"></i>配置
                            </button>
                        </td>
                    </tr>
                `;
            }

            html += `
                            </tbody>
                        </table>
                    </div>
                </section>
            `;
        }
        html += '</div>';

        container.innerHTML = html;
        container.querySelectorAll('.llm-prompts-button').forEach(button => {
            button.addEventListener('click', () => this.showPromptsModal(button.dataset.pluginName));
        });
        container.querySelectorAll('.llm-mapping-edit').forEach(button => {
            button.addEventListener('click', () => this.editMapping(button.dataset.pluginName, button.dataset.callType));
        });
    },

    /**
     * Show Prompts Modal for a plugin
     */
    async showPromptsModal(pluginName) {
        const modal = new bootstrap.Modal(document.getElementById('pluginPromptsModal'));
        const container = document.getElementById('pluginPromptsModalBody');
        const title = document.getElementById('pluginPromptsModalTitle');

        title.textContent = `提示词 · ${this.getRouteCapabilityMeta(pluginName).displayName}`;
        container.innerHTML = `
            <div class="text-center py-5">
                <div class="spinner-border text-primary" role="status"></div>
                <p class="mt-2 text-muted">正在加载提示词…</p>
            </div>
        `;

        modal.show();

        try {
            const settings = await API.capabilities.getSettings(pluginName);
            const prompts = Object.fromEntries(
                (settings.groups || [])
                    .flatMap(group => group.fields || [])
                    .filter(field => field.key.toLowerCase().includes('prompt') && typeof field.value === 'string')
                    .map(field => [field.key, field.value])
            );

            if (Object.keys(prompts).length === 0) {
                container.innerHTML = `
                    <div class="text-center py-5 text-muted">
                        <i class="bi bi-chat-quote fs-1 mb-3 d-block"></i>
                        <p>未找到此插件的提示词。</p>
                    </div>
                `;
                return;
            }

            // Render Prompt Editors
            let html = '';
            for (const [key, value] of Object.entries(prompts)) {
                html += `
                    <div class="mb-4">
                        <label class="form-label fw-bold">${UI.escapeHtml(key)}</label>
                        <textarea class="form-control font-monospace mb-2" rows="6" id="prompt_${UI.escapeHtml(key)}">${UI.escapeHtml(value)}</textarea>
                        <div class="d-flex justify-content-end gap-2">
                             <button class="btn btn-sm btn-outline-secondary" type="button" data-prompt-reset="${UI.escapeHtml(key)}">重置</button>
                             <button class="btn btn-sm btn-primary llm-prompt-save" type="button" data-plugin-name="${UI.escapeHtml(pluginName)}" data-prompt-key="${UI.escapeHtml(key)}">保存</button>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = html;
            container.querySelectorAll('[data-prompt-reset]').forEach(button => {
                const key = button.dataset.promptReset;
                button.addEventListener('click', () => {
                    const input = document.getElementById(`prompt_${key}`);
                    if (input) input.value = prompts[key] || '';
                });
            });
            container.querySelectorAll('.llm-prompt-save').forEach(button => {
                button.addEventListener('click', () => this.savePrompt(button.dataset.pluginName, button.dataset.promptKey));
            });

        } catch (error) {
            console.error('Failed to load prompts:', error);
            container.innerHTML = `
                <div class="alert alert-danger">
                    加载提示词失败：${this.escapeHtml(error.message)}
                </div>
            `;
        }
    },

    /**
     * Save specific prompt
     */
    async savePrompt(capabilityId, key) {
        try {
            const textarea = document.getElementById(`prompt_${key}`);
            const newValue = textarea.value;

            await API.capabilities.updateSettings(capabilityId, { [key]: newValue });
            UI.showSuccess('提示词已保存并应用');

        } catch (error) {
            console.error('Save prompt error:', error);
            UI.showError(`保存失败：${error.message}`);
        }
    },

    /**
     * Load statistics
     */
    async loadStats() {
        try {
            const response = await fetch('/api/llm/stats');
            const result = await response.json();

            if (result.status === 'success') {
                this.currentStats = result.data;
                this.renderStats();
            }
        } catch (error) {
            console.error('Failed to load stats:', error);
        }
    },

    /**
     * Render statistics
     */
    renderStats() {
        const container = document.getElementById('llmStatsList');

        // Check if we have stats data in the new format
        const hasTodayStats = this.currentStats.today && Object.keys(this.currentStats.today).length > 0;
        const hasSessionStats = this.currentStats.session && Object.keys(this.currentStats.session).length > 0;
        const hasTotalStats = this.currentStats.total && Object.keys(this.currentStats.total).length > 0;

        if (!hasTodayStats && !hasSessionStats && !hasTotalStats) {
            container.innerHTML = `
                <div class="text-center py-5 text-muted">
                    <i class="bi bi-graph-up fs-1 mb-3 d-block"></i>
                    <p>暂无用量统计。</p>
                    <small>发起 LLM 调用后，统计数据会显示在这里。</small>
                </div>
            `;
            return;
        }

        const statsTypes = ['today', 'session', 'total'];
        const activeType = statsTypes.includes(this.activeStatsType)
            ? this.activeStatsType
            : 'today';
        this.activeStatsType = activeType;
        const tabClass = type => `nav-link${type === activeType ? ' active' : ''}`;
        const paneClass = type => `tab-pane fade${type === activeType ? ' show active' : ''}`;

        // Add tabs for today vs session vs total and preserve the active view.
        let html = `
            <ul class="nav nav-tabs mb-4" role="tablist">
                <li class="nav-item" role="presentation">
                    <button class="${tabClass('today')}" id="today-stats-tab" data-bs-toggle="tab"
                            data-bs-target="#today-stats" data-stats-type="today" type="button" role="tab"
                            aria-selected="${activeType === 'today'}">
                        <i class="bi bi-calendar-day me-2"></i>今日统计
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="${tabClass('session')}" id="session-stats-tab" data-bs-toggle="tab"
                            data-bs-target="#session-stats" data-stats-type="session" type="button" role="tab"
                            aria-selected="${activeType === 'session'}">
                        <i class="bi bi-clock-history me-2"></i>本次运行统计
                    </button>
                </li>
                <li class="nav-item" role="presentation">
                    <button class="${tabClass('total')}" id="total-stats-tab" data-bs-toggle="tab"
                            data-bs-target="#total-stats" data-stats-type="total" type="button" role="tab"
                            aria-selected="${activeType === 'total'}">
                        <i class="bi bi-database me-2"></i>历史总统计
                    </button>
                </li>
            </ul>

            <div class="tab-content">
                <div class="${paneClass('today')}" id="today-stats" role="tabpanel">
                    ${this.renderStatsContent(this.currentStats.today || {}, 'today')}
                </div>
                <div class="${paneClass('session')}" id="session-stats" role="tabpanel">
                    ${this.renderStatsContent(this.currentStats.session || {}, 'session')}
                </div>
                <div class="${paneClass('total')}" id="total-stats" role="tabpanel">
                    ${this.renderStatsContent(this.currentStats.total || {}, 'total')}
                </div>
            </div>
        `;

        container.innerHTML = html;
        container.querySelectorAll('[data-stats-type]').forEach(tab => {
            tab.addEventListener('shown.bs.tab', () => {
                this.activeStatsType = tab.dataset.statsType || 'today';
            });
        });
    },

    getStatsAverageTime(stat) {
        const times = Array.isArray(stat?.response_times)
            ? stat.response_times.map(Number).filter(Number.isFinite)
            : [];
        if (times.length === 0) return null;
        return times.reduce((sum, value) => sum + value, 0) / times.length;
    },

    getStatsCacheRate(stat) {
        const hit = Number(stat?.cache_hit_tokens) || 0;
        const miss = Number(stat?.cache_miss_tokens) || 0;
        const total = hit + miss;
        return total > 0 ? hit / total : null;
    },

    getStatsLastCallTimestamp(stat) {
        if (!stat?.last_call) return null;
        const timestamp = Date.parse(stat.last_call);
        return Number.isFinite(timestamp) ? timestamp : null;
    },

    getStatsSortValue(key, stat, field) {
        switch (field) {
            case 'plugin':
                return String(key).toLocaleLowerCase();
            case 'calls':
                return Number(stat?.count) || 0;
            case 'tokens':
                return Number(stat?.total_tokens) || 0;
            case 'cost':
                return Number(stat?.total_cost) || 0;
            case 'cache':
                return this.getStatsCacheRate(stat);
            case 'avg_time':
                return this.getStatsAverageTime(stat);
            case 'models': {
                const models = Object.keys(stat?.model_usage || {}).join(', ').toLocaleLowerCase();
                return models || null;
            }
            case 'last_call':
                return this.getStatsLastCallTimestamp(stat);
            default:
                return null;
        }
    },

    getSortedStatsEntries(stats, type) {
        const sortableFields = ['plugin', 'calls', 'tokens', 'cost', 'cache', 'avg_time', 'models', 'last_call'];
        const fallback = { field: 'calls', direction: 'desc' };
        const requested = this.statsSort[type] || fallback;
        const config = {
            field: sortableFields.includes(requested.field) ? requested.field : fallback.field,
            direction: requested.direction === 'asc' ? 'asc' : 'desc',
        };

        return Object.entries(stats).sort((leftEntry, rightEntry) => {
            const leftValue = this.getStatsSortValue(leftEntry[0], leftEntry[1], config.field);
            const rightValue = this.getStatsSortValue(rightEntry[0], rightEntry[1], config.field);

            // Missing values stay at the bottom for both ascending and descending sorts.
            if (leftValue === null && rightValue !== null) return 1;
            if (leftValue !== null && rightValue === null) return -1;

            let comparison = 0;
            if (typeof leftValue === 'string' || typeof rightValue === 'string') {
                comparison = String(leftValue ?? '').localeCompare(
                    String(rightValue ?? ''),
                    undefined,
                    { numeric: true, sensitivity: 'base' },
                );
            } else if (leftValue !== null && rightValue !== null) {
                comparison = leftValue - rightValue;
            }

            if (comparison === 0) {
                return leftEntry[0].localeCompare(rightEntry[0], undefined, {
                    numeric: true,
                    sensitivity: 'base',
                });
            }
            return config.direction === 'asc' ? comparison : -comparison;
        });
    },

    renderStatsSortHeader(type, field, label, alignment = 'text-start') {
        const config = this.statsSort[type] || { field: 'calls', direction: 'desc' };
        const isActive = config.field === field;
        const direction = isActive ? config.direction : null;
        const icon = direction === 'asc'
            ? 'bi-arrow-up'
            : direction === 'desc'
                ? 'bi-arrow-down'
                : 'bi-arrow-down-up opacity-50';
        const ariaSort = direction === 'asc'
            ? 'ascending'
            : direction === 'desc'
                ? 'descending'
                : 'none';
        const justify = alignment === 'text-center'
            ? 'justify-content-center'
            : alignment === 'text-end'
                ? 'justify-content-end'
                : 'justify-content-start';
        const textField = field === 'plugin' || field === 'models';
        const nextDirection = isActive
            ? (direction === 'desc' ? '升序' : '降序')
            : (textField ? '升序' : '降序');

        return `
            <th class="${alignment} p-0" aria-sort="${ariaSort}">
                <button type="button"
                        class="btn btn-link rounded-0 border-0 text-decoration-none text-body fw-semibold d-inline-flex align-items-center gap-1 w-100 px-2 py-2 ${justify}"
                        onclick="LLMManager.sortStats('${type}', '${field}')"
                        title="按 ${this.escapeHtml(label)} ${nextDirection}排列">
                    <span>${this.escapeHtml(label)}</span>
                    <i class="bi ${icon} small" aria-hidden="true"></i>
                </button>
            </th>
        `;
    },

    sortStats(type, field) {
        const sortableFields = ['plugin', 'calls', 'tokens', 'cost', 'cache', 'avg_time', 'models', 'last_call'];
        if (!['today', 'session', 'total'].includes(type) || !sortableFields.includes(field)) return;

        const current = this.statsSort[type] || { field: 'calls', direction: 'desc' };
        const textField = field === 'plugin' || field === 'models';
        this.statsSort[type] = {
            field,
            direction: current.field === field
                ? (current.direction === 'asc' ? 'desc' : 'asc')
                : (textField ? 'asc' : 'desc'),
        };
        this.activeStatsType = type;

        const pane = document.getElementById(`${type}-stats`);
        if (pane) {
            pane.innerHTML = this.renderStatsContent(this.currentStats[type] || {}, type);
        }
    },

    /**
     * Render stats content for a given dataset
     */
    renderStatsContent(stats, type) {
        if (Object.keys(stats).length === 0) {
            const label = type === 'today' ? '今日' : type === 'session' ? '本次运行' : '历史';
            return `
                <div class="text-center py-5 text-muted">
                    <i class="bi bi-info-circle fs-1 mb-3 d-block"></i>
                    <p>暂无${label}统计。</p>
                </div>
            `;
        }

        // Calculate totals
        let totalCalls = 0;
        let totalTokens = 0;
        let totalCost = 0;
        let totalCacheHit = 0;
        let totalCacheMiss = 0;

        for (const stat of Object.values(stats)) {
            totalCalls += Number(stat.count) || 0;
            totalTokens += Number(stat.total_tokens) || 0;
            totalCost += Number(stat.total_cost) || 0;
            totalCacheHit += Number(stat.cache_hit_tokens) || 0;
            totalCacheMiss += Number(stat.cache_miss_tokens) || 0;
        }
        const totalCacheRate = (totalCacheHit + totalCacheMiss) > 0
            ? (totalCacheHit / (totalCacheHit + totalCacheMiss) * 100).toFixed(1)
            : 'N/A';

        let html = `
            <div class="row g-3 mb-4">
                <div class="col-md-3">
                    <div class="card border-0 shadow-sm">
                        <div class="card-body text-center">
                            <h3 class="fw-bold text-primary mb-0">${totalCalls}</h3>
                            <small class="text-muted">总调用次数</small>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card border-0 shadow-sm">
                        <div class="card-body text-center">
                            <h3 class="fw-bold text-success mb-0">${totalTokens.toLocaleString()}</h3>
                            <small class="text-muted">总 Token 数</small>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card border-0 shadow-sm">
                        <div class="card-body text-center">
                            <h3 class="fw-bold text-warning mb-0">${totalCost.toFixed(4)}</h3>
                            <small class="text-muted">总费用</small>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card border-0 shadow-sm">
                        <div class="card-body text-center">
                            <h3 class="fw-bold text-info mb-0">${totalCacheRate}</h3>
                            <small class="text-muted">缓存命中率</small>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Table of individual plugin stats
        html += `
            <div class="table-responsive">
                <table class="table table-hover align-middle">
                    <thead class="table-light">
                        <tr>
                            ${this.renderStatsSortHeader(type, 'plugin', '插件.调用类型')}
                            ${this.renderStatsSortHeader(type, 'calls', '调用次数', 'text-center')}
                            ${this.renderStatsSortHeader(type, 'tokens', 'Token 数', 'text-center')}
                            ${this.renderStatsSortHeader(type, 'cost', '费用', 'text-center')}
                            ${this.renderStatsSortHeader(type, 'cache', '缓存', 'text-center')}
                            ${this.renderStatsSortHeader(type, 'avg_time', '平均耗时', 'text-center')}
                            ${this.renderStatsSortHeader(type, 'models', '使用的模型')}
                            ${this.renderStatsSortHeader(type, 'last_call', '最近调用', 'text-end')}
                        </tr>
                    </thead>
                    <tbody>
        `;

        const sortedEntries = this.getSortedStatsEntries(stats, type);

        for (const [key, stat] of sortedEntries) {
            const models = Object.keys(stat.model_usage || {}).join(', ');
            const lastCallTimestamp = this.getStatsLastCallTimestamp(stat);
            const lastCall = lastCallTimestamp !== null
                ? new Date(lastCallTimestamp).toLocaleString()
                : 'N/A';

            // Calculate Average Response Time
            const avgTime = this.getStatsAverageTime(stat);
            const avgTimeStr = avgTime !== null ? `${avgTime.toFixed(2)}s` : 'N/A';
            const calls = Number(stat.count) || 0;
            const tokens = Number(stat.total_tokens) || 0;
            const cost = Number(stat.total_cost) || 0;
            const cacheHit = Number(stat.cache_hit_tokens) || 0;
            const cacheMiss = Number(stat.cache_miss_tokens) || 0;
            const cacheRateValue = this.getStatsCacheRate(stat);
            const cacheRate = cacheRateValue !== null
                ? `${(cacheRateValue * 100).toFixed(1)}%`
                : 'N/A';
            const cacheDetail = (cacheHit || cacheMiss)
                ? `<div>${cacheRate}</div><small class="text-muted">命中 ${cacheHit.toLocaleString()} / 未命中 ${cacheMiss.toLocaleString()}</small>`
                : '<span class="text-muted">N/A</span>';

            html += `
                <tr>
                    <td><code>${this.escapeHtml(key)}</code></td>
                    <td class="text-center"><span class="badge bg-primary">${calls}</span></td>
                    <td class="text-center">${tokens.toLocaleString()}</td>
                    <td class="text-center">${cost.toFixed(4)}</td>
                    <td class="text-center">${cacheDetail}</td>
                    <td class="text-center">${avgTimeStr}</td>
                    <td><small class="text-muted">${this.escapeHtml(models || 'N/A')}</small></td>
                    <td class="text-end"><small class="text-muted">${lastCall}</small></td>
                </tr>
            `;
        }

        html += `
                    </tbody>
                </table>
            </div>
        `;

        return html;
    },

    /**
     * Refresh methods
     */
    async refreshModels() {
        await this.loadModels();
    },

    async refreshMappings() {
        await this.loadMappings();
    },

    async refreshStats() {
        await this.loadStats();
    },

    /**
     * Show add model modal
     */
    async showAddModelModal() {
        const form = document.getElementById('addModelForm');
        form.reset();
        document.getElementById('modelEditMode').value = 'false';
        document.getElementById('modelId').dataset.oldId = '';
        document.getElementById('addModelModalTitle').textContent = '添加模型';
        document.getElementById('addModelModalSubtitle').textContent = '选择供应商和模型，填写 API Key 即可。';
        document.getElementById('modelTemplateGroup').classList.remove('d-none');
        document.getElementById('modelAdvancedOptions').open = false;
        document.getElementById('modelFormAlert').classList.add('d-none');
        const credentialMode = document.getElementById('modelCredentialMode');
        credentialMode.dataset.canPreserve = 'false';
        credentialMode.dataset.originalMode = '';
        this.populateModelTemplates();
        this.modelIdAutofill = true;
        const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('addModelModal'));
        modal.show();
        await this.applyProviderPreset('openai');
        this.updateModelFormReview();
    },

    populateModelTemplates() {
        const select = document.getElementById('modelTemplateSelect');
        if (!select) return;
        select.innerHTML = '<option value="">选择已有配置…</option>' + Object.keys(this.currentModels || {})
            .map(modelId => `<option value="${this.escapeHtml(modelId)}">${this.escapeHtml(modelId)} · ${this.escapeHtml(this.currentModels[modelId]?.model || '')}</option>`)
            .join('');
    },

    getPresetKeyForConfig(config) {
        const provider = this.getModelManagementMeta(config).provider;
        if (['openai', 'anthropic', 'gemini', 'deepseek', 'openrouter', 'local_codex', 'compatible'].includes(provider)) {
            return provider;
        }
        return 'other';
    },

    async cloneModelConfig(modelId) {
        if (!modelId || !this.currentModels[modelId]) return;
        const config = this.currentModels[modelId];
        await this.applyConfigToModelForm(modelId, config, { clone: true });
        let copyId = `${modelId}-copy`;
        let suffix = 2;
        while (this.currentModels[copyId]) copyId = `${modelId}-copy-${suffix++}`;
        document.getElementById('modelId').value = copyId;
        document.getElementById('modelId').dataset.oldId = '';
        this.modelIdAutofill = false;
        this.updateModelFormReview();
    },

    /**
     * Show edit model modal
     */
    async editModel(modelId) {
        const config = this.currentModels[modelId];
        if (!config) return;
        document.getElementById('modelEditMode').value = 'true';
        document.getElementById('modelTemplateGroup').classList.add('d-none');
        document.getElementById('modelFormAlert').classList.add('d-none');
        document.getElementById('addModelModalTitle').textContent = `编辑“${modelId}”`;
        document.getElementById('addModelModalSubtitle').textContent = 'API Key 留空会保持不变。';
        const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('addModelModal'));
        modal.show();
        await this.applyConfigToModelForm(modelId, config, { edit: true });
    },

    async applyConfigToModelForm(modelId, config, options = {}) {
        const meta = this.getModelManagementMeta(config);
        const presetKey = this.getPresetKeyForConfig(config);
        if (presetKey === 'other' && !this.catalogProviders.length) await this.loadModelCatalogProviders();
        if (presetKey === 'other') {
            document.getElementById('modelOtherProvider').value = meta.provider;
        }
        await this.applyProviderPreset(presetKey, { preserveValues: true });
        if (presetKey === 'other') {
            document.getElementById('modelOtherProvider').value = meta.provider;
            await this.selectOtherProvider(meta.provider, { preserveValues: true });
        }

        document.getElementById('modelId').value = modelId;
        document.getElementById('modelId').dataset.oldId = options.edit ? modelId : '';
        document.getElementById('modelName').value = config.model || '';
        document.getElementById('modelProvider').value = config.custom_llm_provider || config.provider || this.getActiveProviderPreset().providerValue || '';
        document.getElementById('modelApiBase').value = config.api_base || '';
        document.getElementById('modelTemp').value = config.temperature ?? 0.7;
        document.getElementById('modelMaxTokens').value = config.max_tokens ?? '';
        document.getElementById('modelContextWindow').value = config.context_window_tokens ?? config.max_input_tokens ?? '';
        document.getElementById('modelTimeout').value = config.timeout ?? '';
        document.getElementById('modelMaxRetries').value = config.max_retries ?? '';
        document.getElementById('modelVision').checked = Boolean(config.supports_vision || config.vision || config.image_input);
        document.getElementById('modelWebSearch').checked = Boolean(config.enable_web_search || config.codex_web_search);
        document.getElementById('modelCodexReasoning').value = config.codex_reasoning_effort || 'medium';
        document.getElementById('modelCodexIsolated').checked = config.codex_isolated_workdir !== false;
        document.getElementById('modelExtraBody').value = config.extra_body ? JSON.stringify(config.extra_body, null, 2) : '';
        document.getElementById('modelApiKeyEnv').value = meta.credential?.environment_variable || this.getActiveProviderPreset().envVar || '';
        document.getElementById('modelApiKeyEnvValue').value = '';

        const credentialMode = document.getElementById('modelCredentialMode');
        const canPreserve = Boolean(options.edit && meta.credential?.configured);
        credentialMode.dataset.canPreserve = canPreserve ? 'true' : 'false';
        credentialMode.dataset.originalMode = meta.credential?.mode || '';
        if (canPreserve) {
            credentialMode.value = 'preserve';
        } else if (meta.credential?.mode === 'environment') {
            credentialMode.value = 'environment';
        } else if (meta.credential?.mode === 'none' || presetKey === 'local_codex') {
            credentialMode.value = 'none';
        } else {
            credentialMode.value = this.getActiveProviderPreset().requiresCredential ? 'environment' : 'none';
        }
        this.modelIdAutofill = !options.edit && !options.clone;
        this.updateCredentialFields();
        this.updateModelFormReview();
    },

    parseOptionalNumber(elementId, label, minimum, maximum = null) {
        const raw = document.getElementById(elementId)?.value.trim() || '';
        if (!raw) return { value: null, empty: true };
        const value = Number(raw);
        if (!Number.isFinite(value) || value < minimum || (maximum !== null && value > maximum)) {
            const range = maximum === null ? `不能小于 ${minimum}` : `必须在 ${minimum}–${maximum} 之间`;
            throw new Error(`${label}${range}`);
        }
        return { value, empty: false };
    },

    validateModelForm(options = {}) {
        const showErrors = options.showErrors !== false;
        const provider = this.getActiveProviderPreset();
        const modelIdInput = document.getElementById('modelId');
        const modelNameInput = document.getElementById('modelName');
        const apiBaseInput = document.getElementById('modelApiBase');
        const envVarInput = document.getElementById('modelApiKeyEnv');
        const isEdit = document.getElementById('modelEditMode').value === 'true';
        const storedEnvVar = this.sanitizeCredentialName(envVarInput.value);
        const envValue = document.getElementById('modelApiKeyEnvValue').value.trim();
        const modelId = this.sanitizeModelId(modelIdInput.value);
        const envVar = envValue ? this.buildModelCredentialName(modelId) : storedEnvVar;
        const values = {
            provider,
            isEdit,
            modelId,
            modelName: this.normalizePastedText(modelNameInput.value).trim(),
            apiBase: this.normalizePastedText(apiBaseInput.value).trim(),
            credentialMode: this.getEffectiveCredentialMode(provider, isEdit, envVar, envValue),
            envVar,
            envValue,
            providerValue: document.getElementById('modelProvider').value.trim() || provider.providerValue || '',
        };
        modelIdInput.value = values.modelId;
        modelNameInput.value = values.modelName;
        apiBaseInput.value = values.apiBase;
        envVarInput.value = values.envVar;
        try {
            if (provider.key === 'other' && !provider.catalogProvider) throw new Error('请选择一个 LiteLLM 供应商。');
            if (!values.modelId) throw new Error('请填写配置名称。');
            if (values.modelId.length > 80 || /[\\/?#\x00-\x1f\x7f]/.test(values.modelId)) {
                throw new Error('配置名称不能包含 /、\\、?、# 或控制字符，且不能超过 80 个字符。');
            }
            if (!values.modelName) throw new Error('请选择或输入服务端模型。');
            if (values.apiBase && !/^(https?:\/\/|env::[A-Za-z_][A-Za-z0-9_]*$)/i.test(values.apiBase)) {
                throw new Error('API 地址必须以 http://、https:// 开头，或使用 env::VAR_NAME。');
            }
            if (provider.apiBaseRequired && !values.apiBase) throw new Error('自定义 OpenAI 兼容接口必须填写 API 地址。');
            if (values.credentialMode === 'environment' && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(values.envVar)) {
                throw new Error('当前供应商缺少有效的 API Key 保存位置，请重新选择供应商。');
            }
            if (values.credentialMode === 'preserve' && !values.isEdit) throw new Error('新增模型时不能保留旧凭据。');
            if (values.credentialMode === 'none' && provider.requiresCredential) {
                throw new Error(`${provider.label} 需要 API Key，请填写后再保存。`);
            }
            values.samplingDisabled = this.isGemini3FormSelection(provider, values.modelName);
            values.temperature = values.samplingDisabled
                ? { value: null, empty: true }
                : this.parseOptionalNumber('modelTemp', '温度', 0, 2);
            values.maxTokens = this.parseOptionalNumber('modelMaxTokens', '最大输出 Token', 1);
            values.contextWindow = this.parseOptionalNumber('modelContextWindow', '上下文窗口', 1);
            values.timeout = this.parseOptionalNumber('modelTimeout', '超时', 1, 3600);
            values.maxRetries = this.parseOptionalNumber('modelMaxRetries', 'SDK 重试次数', 0, 20);
            const extraBody = document.getElementById('modelExtraBody').value.trim();
            values.extraBody = extraBody ? JSON.parse(extraBody) : {};
            if (!values.extraBody || Array.isArray(values.extraBody) || typeof values.extraBody !== 'object') {
                throw new Error('附加请求体必须是 JSON 对象。');
            }
            if (values.samplingDisabled) {
                values.extraBody = this.stripGeminiSamplingParameters(values.extraBody);
            }
            this.showModelFormError('');
            return values;
        } catch (error) {
            if (error instanceof SyntaxError) error = new Error(`附加请求体不是有效 JSON：${error.message}`);
            if (showErrors) this.showModelFormError(error.message);
            return null;
        }
    },

    showModelFormError(message) {
        const alert = document.getElementById('modelFormAlert');
        if (!alert) return;
        alert.textContent = message || '';
        alert.classList.toggle('d-none', !message);
        if (message) alert.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    setModelSaveBusy(busy) {
        for (const id of ['saveModelButton', 'saveAndTestModelButton']) {
            const button = document.getElementById(id);
            if (!button) continue;
            button.disabled = busy;
            if (!button.dataset.label) button.dataset.label = button.innerHTML;
            button.innerHTML = busy ? '<span class="spinner-border spinner-border-sm me-1"></span>正在保存' : button.dataset.label;
        }
    },

    /**
     * Save Model (Add or Edit)
     */
    async saveModel(options = {}) {
        const values = this.validateModelForm();
        if (!values) return;
        const { isEdit } = values;
        const modelIdInput = document.getElementById('modelId');
        const newModelId = values.modelId;
        const oldModelId = modelIdInput.dataset.oldId || newModelId;
        const targetModelId = isEdit ? oldModelId : newModelId;
        const payload = {
            model: values.modelName,
            api_base: values.apiBase,
            custom_llm_provider: values.providerValue,
            supports_vision: document.getElementById('modelVision').checked,
            extra_body: values.extraBody,
        };
        const numericFields = [
            ['temperature', values.temperature],
            ['max_tokens', values.maxTokens],
            ['context_window_tokens', values.contextWindow],
            ['timeout', values.timeout],
            ['max_retries', values.maxRetries],
        ];
        payload.clear_fields = [];
        for (const [key, parsed] of numericFields) {
            if (!parsed.empty) payload[key] = parsed.value;
            else if (isEdit && this.currentModels[targetModelId]?.[key] !== undefined) payload.clear_fields.push(key);
        }
        if (values.contextWindow.empty && isEdit && this.currentModels[targetModelId]?.max_input_tokens !== undefined) {
            payload.clear_fields.push('max_input_tokens');
        }
        if (!Object.keys(values.extraBody).length && isEdit) payload.clear_fields.push('extra_body');
        if (values.credentialMode === 'environment') {
            payload.api_key = `env::${values.envVar}`;
            payload.credential_mode = 'environment';
        }
        if (values.credentialMode === 'none') {
            payload.api_key = '';
            payload.credential_mode = 'none';
        }
        const webSearch = document.getElementById('modelWebSearch').checked;
        if (values.provider.key === 'local_codex') {
            payload.enable_web_search = false;
            payload.codex_web_search = webSearch;
            payload.codex_reasoning_effort = document.getElementById('modelCodexReasoning').value;
            payload.codex_isolated_workdir = document.getElementById('modelCodexIsolated').checked;
        } else {
            payload.enable_web_search = webSearch;
        }

        try {
            this.setModelSaveBusy(true);
            if (values.credentialMode === 'environment') {
                await this.ensureSharedCredential(values.envVar, values.envValue);
            }
            let url = `/api/llm/models/${encodeURIComponent(targetModelId)}`;
            if (isEdit && oldModelId !== newModelId) {
                url += `?new_id=${encodeURIComponent(newModelId)}`;
            }
            const method = isEdit ? 'PUT' : 'POST';

            const response = await fetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            const result = await response.json().catch(() => ({}));

            if (response.ok && result.status === 'success') {
                const modalEl = document.getElementById('addModelModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();

                UI.showSuccess(`模型“${newModelId}”已保存`);
                document.getElementById('modelApiKeyEnvValue').value = '';
                await this.loadModels();
                await this.loadMappings();
                if (options.testAfterSave) {
                    window.setTimeout(() => this.testModelConnectivity(newModelId), 150);
                }
            } else {
                const detail = Array.isArray(result.detail)
                    ? result.detail.map(item => item.msg || String(item)).join('；')
                    : result.detail;
                throw new Error(detail || result.message || `保存模型失败（HTTP ${response.status}）`);
            }
        } catch (error) {
            console.error('Save model error:', error);
            this.showModelFormError(`保存失败：${error.message}`);
        } finally {
            this.setModelSaveBusy(false);
        }
    },

    /**
     * Delete model
     */
    async deleteModel(modelId) {
        const mappingCount = this.getModelManagementMeta(this.currentModels[modelId] || {}).mapping_count || 0;
        const usageWarning = mappingCount ? `\n该模型仍被 ${mappingCount} 个任务引用，删除后这些任务将无法正常调用。` : '';
        if (!await UI.confirm(`确定要删除模型“${modelId}”吗？${usageWarning}`, {
            title: '删除模型',
            confirmText: '删除',
            variant: 'danger'
        })) {
            return;
        }

        try {
            const response = await fetch(`/api/llm/models/${encodeURIComponent(modelId)}`, {
                method: 'DELETE'
            });
            const result = await response.json();

            if (result.status === 'success') {
                UI.showSuccess(`模型“${modelId}”已删除`);
                await this.loadModels();
            } else {
                throw new Error(result.message || '删除失败');
            }
        } catch (error) {
            console.error('Failed to delete model:', error);
            UI.showError(`删除模型失败：${error.message}`);
        }
    },

    renderModelTestResult(modelId, data) {
        const domModelId = this.getModelDomId(modelId);
        const resultDiv = document.getElementById(`modelTestResult_${domModelId}`);
        if (!resultDiv) return;

        const statusClass = data.ok ? 'alert-success' : 'alert-danger';
        const statusIcon = data.ok ? 'bi-check-circle' : 'bi-exclamation-triangle';
        const latencyText = data.latency_ms !== undefined && data.latency_ms !== null
            ? `${data.latency_ms} 毫秒`
            : 'N/A';
        const configuredModel = this.escapeHtml(data.configured_model || '');
        const actualModel = data.actual_model
            ? `<div><strong>实际模型：</strong><code>${this.escapeHtml(data.actual_model)}</code></div>`
            : '';
        const message = this.escapeHtml(data.message || '');

        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `
            <div class="alert ${statusClass} py-2 px-3 mb-0 small">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span><i class="bi ${statusIcon} me-1"></i>${data.ok ? '连通成功' : '连通失败'}</span>
                    <span class="badge ${data.ok ? 'bg-success' : 'bg-danger'}">${latencyText}</span>
                </div>
                <div><strong>配置模型：</strong><code>${configuredModel}</code></div>
                ${actualModel}
                <div class="mt-1 text-break">${message}</div>
            </div>
        `;
    },

    async testModelConnectivity(modelId) {
        const domModelId = this.getModelDomId(modelId);
        const btn = document.getElementById(`testModelBtn_${domModelId}`);
        const resultDiv = document.getElementById(`modelTestResult_${domModelId}`);

        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
        }
        if (resultDiv) {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>正在测试…</div>';
        }

        try {
            const response = await fetch(`/api/llm/models/${encodeURIComponent(modelId)}/test`, {
                method: 'POST'
            });
            const result = await response.json();

            if (!response.ok) {
                throw new Error(result.detail || result.message || '服务器错误');
            }

            if (result.status !== 'success' || !result.data) {
                throw new Error(result.message || '测试失败');
            }

            this.renderModelTestResult(modelId, result.data);
        } catch (error) {
            console.error(`Failed to test model ${modelId}:`, error);
            this.renderModelTestResult(modelId, {
                ok: false,
                latency_ms: null,
                configured_model: this.currentModels[modelId]?.model || modelId,
                actual_model: null,
                message: error.message || '未知错误'
            });
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-lightning-charge me-1"></i>测试';
            }
        }
    },

    /**
     * Edit mapping - opens modal with current mapping data
     */
    async editMapping(pluginName, callType) {
        try {
            const mapping = this.currentMappings[pluginName]?.[callType];
            if (!mapping) {
                UI.showError('未找到映射');
                return;
            }
            const capability = this.getRouteCapabilityMeta(pluginName);
            const task = this.getTaskRouteMeta(pluginName, callType);

            document.getElementById('mappingPluginName').value = pluginName;
            document.getElementById('mappingCallType').value = callType;
            document.getElementById('mappingFullName').value = `${capability.displayName} · ${task.label}`;
            document.getElementById('mappingInternalName').textContent = `${pluginName}.${callType}`;
            document.getElementById('editMappingModalTitle').textContent = `配置模型路由 · ${task.label}`;

            const availableModelIds = Object.keys(this.currentModels);
            [mapping.primary, ...(mapping.fallback || [])].forEach(modelId => {
                if (modelId && !availableModelIds.includes(modelId)) availableModelIds.push(modelId);
            });
            const primarySelect = document.getElementById('mappingPrimary');
            primarySelect.innerHTML = '<option value="">选择模型…</option>';
            for (const modelId of availableModelIds) {
                const option = document.createElement('option');
                option.value = modelId;
                option.textContent = this.currentModels[modelId]
                    ? this.formatRouteModelOption(modelId)
                    : `${modelId} · 当前模型连接中不存在`;
                if (modelId === mapping.primary) {
                    option.selected = true;
                }
                primarySelect.appendChild(option);
            }

            const fallbackSelect = document.getElementById('mappingFallback');
            fallbackSelect.innerHTML = '<option value="">无（不使用备用模型）</option>';
            for (const modelId of availableModelIds) {
                const option = document.createElement('option');
                option.value = modelId;
                option.textContent = this.currentModels[modelId]
                    ? this.formatRouteModelOption(modelId)
                    : `${modelId} · 当前模型连接中不存在`;
                if (mapping.fallback && mapping.fallback.length > 0 && modelId === mapping.fallback[0]) {
                    option.selected = true;
                }
                fallbackSelect.appendChild(option);
            }

            // Populate override parameters
            const overrideParams = {...(mapping.override_params || {})};
            document.getElementById('mappingOverrideTemp').value = overrideParams.temperature !== undefined ? overrideParams.temperature : '';
            document.getElementById('mappingOverrideMaxTokens').value = overrideParams.max_tokens !== undefined ? overrideParams.max_tokens : '';
            document.getElementById('mappingOverrideTimeout').value = overrideParams.timeout !== undefined ? overrideParams.timeout : '';

            delete overrideParams.temperature;
            delete overrideParams.max_tokens;
            delete overrideParams.timeout;
            document.getElementById('mappingOverrideExtra').value = Object.keys(overrideParams).length > 0 ? JSON.stringify(overrideParams, null, 2) : '';
            this.updateMappingSamplingControls();

            bootstrap.Modal.getOrCreateInstance(document.getElementById('editMappingModal')).show();
        } catch (error) {
            console.error('Failed to open edit mapping modal:', error);
            UI.showError(`打开编辑弹窗失败：${error.message}`);
        }
    },

    /**
     * Save mapping edit
     */
    async saveMappingEdit() {
        try {
            const pluginName = document.getElementById('mappingPluginName').value;
            const callType = document.getElementById('mappingCallType').value;
            const primary = document.getElementById('mappingPrimary').value;
            const fallbackModel = document.getElementById('mappingFallback').value.trim();
            const overrideTemp = document.getElementById('mappingOverrideTemp').value;
            const overrideMaxTokens = document.getElementById('mappingOverrideMaxTokens').value;
            const overrideTimeout = document.getElementById('mappingOverrideTimeout').value;
            const overrideExtraStr = document.getElementById('mappingOverrideExtra').value.trim();

            if (!primary) {
                UI.showError('请选择主模型');
                return;
            }

            // Build mapping object
            const mapping = {
                primary: primary,
                fallback: fallbackModel ? [fallbackModel] : [],
                override_params: {}
            };

            // Add override params if specified by the user
            if (overrideTemp !== '') {
                mapping.override_params.temperature = parseFloat(overrideTemp);
            }
            if (overrideMaxTokens !== '') {
                mapping.override_params.max_tokens = parseInt(overrideMaxTokens);
            }
            if (overrideTimeout !== '') {
                mapping.override_params.timeout = parseInt(overrideTimeout);
            }

            // Parse and merge extra JSON parameters
            if (overrideExtraStr) {
                try {
                    const extraData = JSON.parse(overrideExtraStr);
                    Object.assign(mapping.override_params, extraData);
                } catch (e) {
                    UI.showError('附加参数中的 JSON 无效：' + e.message);
                    return;
                }
            }
            if (this.isGemini3ModelConfig(this.currentModels[primary] || {})) {
                mapping.override_params = this.stripGeminiSamplingParameters(mapping.override_params);
            }

            // Save via API
            const response = await fetch(`/api/llm/mappings/${pluginName}/${callType}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(mapping)
            });

            const result = await response.json();

            if (result.status === 'success') {
                // Close modal
                const modalEl = document.getElementById('editMappingModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();

                const task = this.getTaskRouteMeta(pluginName, callType);
                UI.showSuccess(`“${task.label}”的模型路由已更新`);
                await this.loadMappings();
            } else {
                throw new Error(result.message || '保存映射失败');
            }
        } catch (error) {
            console.error('Failed to save mapping:', error);
            UI.showError(`保存映射失败：${error.message}`);
        }
    },

    /**
     * Load proxy settings from server
     */
    async loadProxy() {
        try {
            const response = await fetch('/api/llm/proxy');
            const result = await response.json();
            if (result.status === 'success') {
                const { proxy_url, display_url, enabled, sensitive } = result.data;
                const input = document.getElementById('proxyUrlInput');
                const badge = document.getElementById('proxyStatusBadge');
                if (input) {
                    input.value = proxy_url || '';
                    input.placeholder = sensitive ? '代理已配置（含凭据）；留空可直接测试，保存空值会禁用' : 'http://host:port';
                }
                if (badge) {
                    badge.textContent = enabled ? `已启用：${display_url || '已配置'}` : '已禁用';
                    badge.className = `badge rounded-pill fs-6 ${enabled ? 'bg-success' : 'bg-secondary'}`;
                }
            }
        } catch (error) {
            console.error('Failed to load proxy settings:', error);
        }
    },

    /**
     * Save proxy settings to server
     */
    async saveProxy() {
        const input = document.getElementById('proxyUrlInput');
        const badge = document.getElementById('proxyStatusBadge');
        const proxy_url = (input ? input.value : '').trim();

        // Basic URL validation
        if (proxy_url && !proxy_url.match(/^https?:\/\/.+:\d+\/?/)) {
            UI.showError('格式错误，请输入合法的代理地址，例如：http://100.x.x.x:6688');
            return;
        }

        try {
            const response = await fetch('/api/llm/proxy', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proxy_url })
            });
            const result = await response.json();

            if (result.status === 'success') {
                const { enabled, display_url, proxy_url: publicUrl, sensitive } = result.data;
                if (input) {
                    input.value = publicUrl || '';
                    input.placeholder = sensitive ? '代理已配置（含凭据）；留空可直接测试，保存空值会禁用' : 'http://host:port';
                }
                if (badge) {
                    badge.textContent = enabled ? `已启用：${display_url || '已配置'}` : '已禁用';
                    badge.className = `badge rounded-pill fs-6 ${enabled ? 'bg-success' : 'bg-secondary'}`;
                }
                UI.showSuccess(enabled
                    ? `代理已保存：${display_url || '已配置'}`
                    : '代理已禁用（直连模式）');
            } else {
                throw new Error(result.message || '保存失败');
            }
        } catch (error) {
            console.error('Failed to save proxy settings:', error);
            UI.showError(`保存失败：${error.message}`);
        }
    },

    /**
     * Test proxy and direct connectivity
     */
    async testProxy() {
        const proxyInput = document.getElementById('proxyUrlInput');
        const testUrlInput = document.getElementById('proxyTestUrlInput');
        const resultDiv = document.getElementById('proxyTestResult');
        const btn = document.getElementById('proxyTestBtn');

        const proxy_url = proxyInput ? proxyInput.value.trim() : '';
        const test_url = testUrlInput ? testUrlInput.value.trim() : '';

        // Show loading state
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> 正在测试…'; }
        if (resultDiv) { resultDiv.style.display = 'block'; resultDiv.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>正在测试连通性，请稍候…</div>'; }

        try {
            const resp = await fetch('/api/llm/proxy/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proxy_url, test_url })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || '服务器错误');

            const { results, test_url: tested_url, proxy_url: used_proxy } = data;

            const renderRow = (label, icon, r) => {
                const color = r.ok ? 'success' : 'danger';
                const badgeText = r.ok ? `${r.latency_ms} 毫秒` : '失败';
                const badgeColor = r.ok ? 'bg-success' : 'bg-danger';
                const detail = r.ok && r.status_code
                    ? `HTTP ${r.status_code}（${r.latency_ms} 毫秒）`
                    : r.message;
                return `
                    <tr>
                        <td><i class="bi bi-${icon} me-1 text-secondary"></i>${this.escapeHtml(label)}</td>
                        <td><span class="badge ${badgeColor}">${badgeText}</span></td>
                        <td class="text-muted small">${this.escapeHtml(detail)}</td>
                    </tr>`;
            };

            let rows = renderRow('直连', 'arrow-right-circle', results.direct);
            if (results.proxy) {
                rows += renderRow(`代理 (${used_proxy || '已配置'})`, 'hdd-network', results.proxy);
            } else if (!used_proxy) {
                rows += `<tr><td colspan="3" class="text-muted small"><i class="bi bi-info-circle me-1"></i>未配置代理，仅测试直连</td></tr>`;
            }

            resultDiv.innerHTML = `
                <div class="card border-0 bg-light">
                    <div class="card-body p-3">
                        <div class="small text-muted mb-2">
                            <i class="bi bi-link-45deg me-1"></i>
                            测试目标：<code>${this.escapeHtml(tested_url)}</code>
                        </div>
                        <table class="table table-sm mb-0">
                            <thead><tr>
                                <th>通道</th><th>延迟</th><th>详情</th>
                            </tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                </div>`;
        } catch (err) {
            console.error('Proxy test failed:', err);
            if (resultDiv) resultDiv.innerHTML = `<div class="alert alert-danger py-2 small"><i class="bi bi-exclamation-triangle me-1"></i>${this.escapeHtml(err.message)}</div>`;
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-wifi me-1"></i> 测试连接'; }
        }
    },

    // ========== Call History ==========

    currentHistorySource: null,
    currentHistoryEntries: [],
    currentHistoryPaging: null,
    callHistoryAbortController: null,

    async loadCallHistory() {
        const sourceList = document.getElementById('callHistorySourceList');
        if (!sourceList) return;

        sourceList.innerHTML = '<div class="text-center text-muted py-4 small"><div class="spinner-border spinner-border-sm me-2"></div>加载中…</div>';

        try {
            const resp = await fetch('/api/llm/call-history/summary');
            const data = await resp.json();
            if (data.status !== 'success' || !data.data) {
                sourceList.innerHTML = '<div class="text-center text-muted py-4 small">暂无调用记录</div>';
                return;
            }

            const summaries = data.data;
            if (!summaries.length) {
                sourceList.innerHTML = '<div class="text-center text-muted py-4 small">暂无调用记录</div>';
                return;
            }

            // Group by plugin_name
            const grouped = {};
            summaries.forEach(s => {
                if (!grouped[s.plugin_name]) grouped[s.plugin_name] = [];
                grouped[s.plugin_name].push(s);
            });

            let html = '';
            for (const [plugin, items] of Object.entries(grouped)) {
                html += `<div class="list-group-item bg-light border-0 py-1 px-3">
                    <small class="text-muted fw-semibold" style="font-size:0.7rem;">${this.escapeHtml(plugin)}</small>
                </div>`;
                items.forEach(item => {
                    const isActive = (this.currentHistorySource === item.key) ? 'active' : '';
                    const statusIcon = item.last_success
                        ? '<i class="bi bi-check-circle-fill text-success"></i>'
                        : '<i class="bi bi-x-circle-fill text-danger"></i>';
                    const timeDisplay = item.last_call ? this.escapeHtml(String(item.last_call).substring(11, 19)) : '';
                    html += `<a href="#" class="list-group-item list-group-item-action border-0 py-2 px-3 ${isActive}"
                        data-history-key="${this.escapeHtml(item.key)}">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <span class="me-1">${statusIcon}</span>
                                <span class="small fw-medium">${this.escapeHtml(item.call_type)}</span>
                                <span class="badge bg-secondary-subtle text-secondary ms-1" style="font-size:0.6rem;">${Number(item.count || 0)}条</span>
                            </div>
                            <small class="text-muted" style="font-size:0.65rem;">${timeDisplay}</small>
                        </div>
                        <small class="text-muted d-block" style="font-size:0.65rem;">${this.escapeHtml(item.last_model || '')}</small>
                    </a>`;
                });
            }
            sourceList.innerHTML = html;
            sourceList.querySelectorAll('[data-history-key]').forEach(link => {
                link.addEventListener('click', event => {
                    event.preventDefault();
                    this.showCallHistoryDetail(link.dataset.historyKey);
                });
            });

            // If current source still exists, refresh detail
            if (this.currentHistorySource) {
                this.showCallHistoryDetail(this.currentHistorySource);
            }
        } catch (e) {
            console.error('Failed to load call history:', e);
            sourceList.innerHTML = '<div class="text-center text-danger py-4 small">加载失败</div>';
        }
    },

    async showCallHistoryDetail(key) {
        this.currentHistorySource = key;
        const detail = document.getElementById('callHistoryDetail');
        if (!detail) return;

        // Highlight active in list
        document.querySelectorAll('#callHistorySourceList .list-group-item-action').forEach(el => {
            el.classList.remove('active');
        });
        const activeLink = Array.from(document.querySelectorAll('#callHistorySourceList [data-history-key]'))
            .find(link => link.dataset.historyKey === key);
        if (activeLink) activeLink.classList.add('active');

        detail.innerHTML = '<div class="text-center py-4"><div class="spinner-border spinner-border-sm text-primary"></div></div>';

        try {
            const parts = key.split('.');
            const plugin = parts[0];
            const callType = parts.slice(1).join('.');
            if (this.callHistoryAbortController) this.callHistoryAbortController.abort();
            const controller = new AbortController();
            this.callHistoryAbortController = controller;
            const resp = await fetch(`/api/llm/call-history?plugin_name=${encodeURIComponent(plugin)}&call_type=${encodeURIComponent(callType)}&limit=10&offset=0`, {
                signal: controller.signal
            });
            if (this.callHistoryAbortController !== controller) return;
            this.callHistoryAbortController = null;
            const data = await resp.json();

            if (data.status !== 'success' || !data.data || !Array.isArray(data.data.entries)) {
                detail.innerHTML = '<div class="text-center text-muted py-4">暂无记录</div>';
                return;
            }

            const entries = data.data.entries;
            this.currentHistoryEntries = entries;
            this.currentHistoryPaging = {
                plugin,
                callType,
                key,
                total: data.data.total || entries.length,
                limit: data.data.limit || 10,
                offset: data.data.offset || 0,
            };

            let html = '';
            entries.forEach((entry, idx) => {
                const time = entry.timestamp ? this.escapeHtml(String(entry.timestamp).replace('T', ' ').substring(0, 19)) : '';
                const statusBadge = entry.success
                    ? '<span class="badge bg-success-subtle text-success">成功</span>'
                    : '<span class="badge bg-danger-subtle text-danger">失败</span>';
                const modelBadge = `<span class="badge bg-secondary-subtle text-secondary ms-1">${this.escapeHtml(entry.actual_model || entry.model_id || '未知')}</span>`;

                const hasReasoning = !!entry.has_reasoning;
                const messageCount = entry.message_count || 0;
                const responseSize = entry.response_size || 0;
                const reasoningSize = entry.reasoning_size || 0;
                const tokenUsageHtml = this.renderTokenUsage(entry);
                const memoryBadge = entry.has_memory_trace
                    ? `<span class="badge bg-primary-subtle text-primary border fw-normal">
                        <i class="bi bi-database-check me-1"></i>阶段 ${entry.memory_has_stage ? 1 : 0}
                        · 事件 ${Number(entry.memory_event_count || 0)}
                        · 人物 ${Number(entry.memory_people_count || 0)}
                       </span>`
                    : '';
                const chatContext = entry.chat_name
                    ? `<span class="badge bg-light text-dark border fw-normal">
                        <i class="bi bi-chat-dots me-1"></i>${this.escapeHtml(entry.chat_name)}
                        ${entry.role_name ? ` · ${this.escapeHtml(entry.role_name)}` : ''}
                       </span>`
                    : '';

                html += `<div class="card border-0 shadow-sm mb-3">
                    <div class="card-body p-3">
                        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-2">
                            <div class="d-flex align-items-center gap-2 flex-wrap">
                                ${statusBadge}
                                ${modelBadge}
                                <span class="text-muted small"><i class="bi bi-calendar2-event me-1"></i>${time}</span>
                            </div>
                            <div class="d-flex flex-wrap justify-content-end gap-2">
                                <button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'req')">
                                    <i class="bi bi-box-arrow-in-down me-1"></i>请求
                                </button>
                                ${hasReasoning ? `<button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'reasoning')">
                                    <i class="bi bi-lightbulb me-1"></i>思维
                                </button>` : ''}
                                <button class="btn btn-sm btn-outline-secondary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'resp')">
                                    <i class="bi bi-box-arrow-up me-1"></i>响应
                                </button>
                                ${entry.has_memory_trace ? `<button class="btn btn-sm btn-primary py-0 px-2" style="font-size:0.75rem;"
                                    onclick="LLMManager.toggleCallHistoryBody(${idx}, 'memory')">
                                    <i class="bi bi-database-check me-1"></i>本轮记忆
                                </button>` : ''}
                            </div>
                        </div>
                        <div class="d-flex flex-wrap align-items-center gap-2 mt-2 pt-2 border-top">
                            <span class="badge bg-light text-secondary border fw-normal"><i class="bi bi-stopwatch me-1"></i>${Number(entry.response_time || 0).toFixed(1)} 秒</span>
                            ${tokenUsageHtml}
                            ${chatContext}
                            ${memoryBadge}
                        </div>
                    </div>
                    <div class="history-req-body d-none" data-history-index="${idx}" data-history-kind="req">
                        <div class="px-3 pt-2">
                            <small class="text-muted fw-semibold">📥 请求（${messageCount} 条消息）</small>
                        </div>
                        <pre class="m-2 p-2 bg-light rounded small" style="max-height:250px; font-size:0.75rem; overflow:auto;"></pre>
                    </div>
                    ${hasReasoning ? `<div class="history-reasoning-body d-none" data-history-index="${idx}" data-history-kind="reasoning">
                        <div class="px-3 pt-2">
                            <small class="text-muted fw-semibold"><i class="bi bi-lightbulb me-1"></i>思维链（${reasoningSize} 个字符）</small>
                        </div>
                        <div class="m-2 p-2 bg-warning-subtle rounded small" style="max-height:250px; font-size:0.8rem; overflow:auto; white-space:pre-wrap;"></div>
                    </div>` : ''}
                    <div class="history-resp-body d-none" data-history-index="${idx}" data-history-kind="resp">
                        <div class="px-3 pt-2">
                            <small class="text-muted fw-semibold">📤 响应（${responseSize} 个字符）</small>
                        </div>
                        <div class="m-2 p-2 bg-light rounded small" style="max-height:250px; font-size:0.8rem; overflow:auto; white-space:pre-wrap;"></div>
                    </div>
                    ${entry.has_memory_trace ? `<div class="history-memory-body d-none border-top" data-history-index="${idx}" data-history-kind="memory">
                        <div class="px-3 pt-3 d-flex flex-wrap justify-content-between gap-2">
                            <small class="text-muted fw-semibold"><i class="bi bi-database-check me-1"></i>本轮已注入记忆</small>
                            ${entry.trace_id ? `<code class="small">${this.escapeHtml(entry.trace_id)}</code>` : ''}
                        </div>
                        <div class="history-memory-content m-3"></div>
                    </div>` : ''}
                    ${!entry.success ? `<div class="history-error-body" data-history-index="${idx}" data-history-kind="error">
                        <div class="px-3 pt-2"><small class="text-muted fw-semibold text-danger">❌ 错误</small></div>
                        <div class="m-2 p-2 bg-danger-subtle rounded small text-danger" style="font-size:0.8rem; white-space:pre-wrap;"></div>
                    </div>` : ''}
                </div>`;
            });

            if (!html) html = '<div class="text-center text-muted py-4">暂无记录</div>';
            html += `<div class="text-center text-muted small pb-2">显示最近 ${entries.length} / ${Number(this.currentHistoryPaging.total || 0)} 条记录</div>`;
            detail.innerHTML = html;
            entries.forEach((entry, idx) => {
                if (!entry.success) this.renderCallHistoryPayload(idx, 'error');
            });
        } catch (e) {
            if (e.name === 'AbortError') return;
            console.error('Failed to load call history detail:', e);
            detail.innerHTML = '<div class="text-center text-danger py-4">加载失败</div>';
        }
    },

    toggleCallHistoryBody(index, kind) {
        const selectorMap = {
            req: '.history-req-body',
            resp: '.history-resp-body',
            reasoning: '.history-reasoning-body',
            memory: '.history-memory-body',
            error: '.history-error-body'
        };
        const selector = selectorMap[kind];
        const body = document.querySelector(`${selector}[data-history-index="${index}"]`);
        if (!body) return;

        body.classList.toggle('d-none');
        this.renderCallHistoryPayload(index, kind);
    },

    renderCallHistoryPayload(index, kind) {
        const selectorMap = {
            req: '.history-req-body',
            resp: '.history-resp-body',
            reasoning: '.history-reasoning-body',
            memory: '.history-memory-body',
            error: '.history-error-body'
        };
        const selector = selectorMap[kind];
        const body = document.querySelector(`${selector}[data-history-index="${index}"]`);
        if (!body) return;
        if (body.dataset.rendered === 'true') return;

        const entry = this.currentHistoryEntries[index] || {};
        let text = '';
        const target = kind === 'memory'
            ? body.querySelector('.history-memory-content')
            : (body.querySelector('pre') || body.querySelector('.rounded'));
        if (!target) return;

        if (kind === 'error') {
            target.textContent = entry.error_preview || '';
            body.dataset.rendered = 'true';
            return;
        }

        target.textContent = '加载中…';
        this.fetchCallHistoryEntry(entry)
            .then(fullEntry => {
                if (kind === 'req') text = JSON.stringify(this.sanitizeCallHistoryPayload(fullEntry.messages || []), null, 2);
                if (kind === 'resp') text = fullEntry.response_text || '';
                if (kind === 'reasoning') text = fullEntry.reasoning_text || '';
                if (kind === 'memory') {
                    target.innerHTML = (
                        fullEntry.memory_trace
                            && typeof App !== 'undefined'
                            && typeof App.renderMemoryTrace === 'function'
                            ? App.renderMemoryTrace(fullEntry.memory_trace, `history-${fullEntry.trace_id || index}-${index}`)
                            : '<div class="text-muted text-center py-3">此记录没有本轮记忆审计数据。</div>'
                    );
                } else {
                    target.textContent = text;
                }
                body.dataset.rendered = 'true';
            })
            .catch(e => {
                console.error('Failed to load call history entry:', e);
                target.textContent = '加载失败';
            });
    },

    formatJobTime(value) {
        if (!value) return '-';
        const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString('zh-CN');
    },

    formatJobDuration(job) {
        const seconds = Number(job.elapsed_seconds ?? job.duration_seconds ?? 0);
        if (!Number.isFinite(seconds) || seconds <= 0) return '-';
        if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
        const mins = Math.floor(seconds / 60);
        const rest = Math.round(seconds % 60);
        return `${mins} 分 ${rest} 秒`;
    },

    reasoningEffortLabel(value) {
        const normalized = String(value || '');
        return {
            none: '无',
            minimal: '最低',
            low: '低',
            medium: '中',
            high: '高',
            xhigh: '超高',
            inherit: '继承默认值'
        }[normalized] || normalized || '-';
    },

    jobStatusBadge(status) {
        const normalized = String(status || 'unknown');
        const cls = {
            starting: 'bg-info',
            running: 'bg-primary',
            completed: 'bg-success',
            failed: 'bg-danger',
            timeout: 'bg-warning text-dark',
            cancelling: 'bg-warning text-dark',
            cancelled: 'bg-secondary',
            terminating: 'bg-warning text-dark',
            idle: 'bg-secondary',
            pending: 'bg-info',
            ready: 'bg-success',
        }[normalized] || 'bg-secondary';
        const label = {
            starting: '正在启动',
            running: '运行中',
            completed: '已完成',
            failed: '失败',
            timeout: '已超时',
            cancelling: '正在取消',
            cancelled: '已取消',
            terminating: '正在终止',
            idle: '空闲',
            pending: '等待中',
            ready: '就绪',
            unknown: '未知',
        }[normalized] || normalized;
        return `<span class="badge ${cls}">${this.escapeHtml(label)}</span>`;
    },

    async loadCodexJobs(options = {}) {
        const container = document.getElementById('codexJobsList');
        if (!container) return;
        const quiet = Boolean(options.quiet);
        if (quiet && this.codexJobsLoading) return;
        this.codexJobsLoading = true;
        try {
            if (!quiet) {
                container.innerHTML = `<div class="text-center text-muted py-4"><div class="spinner-border spinner-border-sm me-2"></div>正在加载 Codex 运行状态…</div>`;
            }
            const result = await API.codexJobs.list();
            const data = result.data || {};
            this.renderCodexJobs(
                data.active || [],
                data.recent || [],
                data.stats || {},
                data.sessions || [],
                data.session_stats || {},
                data.runtime || {},
                data.upgrade || {},
            );
            this.scheduleCodexJobsPoll(
                (data.active || []).length,
                Boolean(data.upgrade?.operation_running),
            );
        } catch (e) {
            console.error('Failed to load Codex jobs:', e);
            if (!quiet) {
                container.innerHTML = `<div class="alert alert-danger">加载 Codex 运行状态失败：${this.escapeHtml(e.message)}</div>`;
            }
        } finally {
            this.codexJobsLoading = false;
        }
    },

    scheduleCodexJobsPoll(activeCount = 0, operationRunning = false) {
        clearTimeout(this.codexJobsPollTimer);
        const pane = document.getElementById('llm-codex-jobs');
        if (!pane?.classList.contains('active')) return;
        const delay = operationRunning ? 1000 : activeCount > 0 ? 2000 : 10000;
        this.codexJobsPollTimer = setTimeout(
            () => this.loadCodexJobs({ quiet: true }),
            delay,
        );
    },

    renderCodexJobs(active, recent, stats, sessions = [], sessionStats = {}, runtime = {}, upgrade = {}) {
        const container = document.getElementById('codexJobsList');
        if (!container) return;
        this.codexExpandedSessions ||= new Set();

        const formatK = value => {
            const number = Number(value || 0);
            if (number >= 1000000) return `${(number / 1000000).toFixed(1).replace(/\.0$/, '')}M`;
            if (number >= 1000) return `${(number / 1000).toFixed(1).replace(/\.0$/, '')}k`;
            return number.toLocaleString();
        };
        const statusMeta = status => {
            const key = String(status || 'idle');
            const labels = {
                queued: '等待中', starting: '准备中', running: '运行中', completed: '已完成', failed: '失败',
                timeout: '已超时', cancelling: '正在中断', cancelled: '已取消', idle: '空闲',
            };
            const classes = {
                queued: 'busy', starting: 'busy', running: 'busy', completed: 'ready', failed: 'failed',
                timeout: 'warning', cancelling: 'warning', cancelled: '', idle: '',
            };
            return { label: labels[key] || key, className: classes[key] || '' };
        };
        const activityLabel = job => {
            const item = String(job?.current_item_type || '');
            const event = String(job?.progress_event || '');
            const labels = {
                reasoning: '分析中', agentMessage: '生成回复', commandExecution: '执行命令',
                webSearch: '搜索中', mcpToolCall: '调用工具', fileChange: '处理文件',
                imageGeneration: '生成图片', dynamicToolCall: '调用工具',
            };
            if (labels[item]) return labels[item];
            return {
                queued: '等待执行', turn_started: '开始处理', item_started: '处理中',
                token_usage: '整理结果', process_started: '进程运行中', process_completed: '读取结果',
                response_ready: '结果就绪', error: '执行失败',
            }[event] || statusMeta(job?.status).label;
        };

        const pools = runtime.pools || {};
        const poolValues = Object.values(pools);
        const workerCount = poolValues.reduce((sum, pool) => sum + Number(pool.size || 0), 0);
        const queueCount = poolValues.reduce((sum, pool) => sum + Number(pool.waiting || 0), 0);
        const activeCount = Number(stats.active_count ?? active.length ?? 0);
        const sessionCount = Number(sessionStats.session_count ?? sessions.length ?? 0);
        const operation = upgrade.operation || null;
        const operationRunning = Boolean(upgrade.operation_running);
        const serverRunning = Boolean(runtime.running);
        const maintenance = Boolean(runtime.maintenance || operationRunning);
        const dotClass = maintenance ? 'busy' : serverRunning ? 'ready' : 'failed';
        const installedVersion = String(upgrade.installed_version || runtime.active_version || '-');
        const schemaShort = String(runtime.schema_hash || '').slice(0, 10) || '-';
        const checkedCurrent = Boolean(upgrade.available_version && !upgrade.update_available);

        const updateButton = operationRunning
            ? `<button class="btn btn-outline-secondary" disabled><span class="spinner-border spinner-border-sm me-1"></span>处理中</button>`
            : checkedCurrent
                ? `<button class="btn btn-outline-secondary" disabled><i class="bi bi-check2 me-1"></i>已是最新</button>`
                : `<button class="btn btn-primary codex-update-start"><i class="bi bi-arrow-up-circle me-1"></i>更新</button>`;
        const rollbackButton = upgrade.rollback_available && !operationRunning
            ? `<button class="btn btn-outline-secondary codex-update-rollback" title="恢复 ${this.escapeHtml(upgrade.rollback_version || '')}"><i class="bi bi-arrow-counterclockwise"></i></button>`
            : '';

        const operationFinishedAt = operation?.finished_at ? Date.parse(operation.finished_at) : NaN;
        const showOperation = Boolean(operation && (
            operationRunning
            || operation.status === 'failed'
            || !Number.isFinite(operationFinishedAt)
            || Date.now() - operationFinishedAt < 10 * 60 * 1000
        ));
        const operationHtml = showOperation ? (() => {
            const opStatus = statusMeta(operation.status === 'succeeded' || operation.status === 'rolled_back' ? 'completed' : operation.status);
            const logs = (operation.logs || []).map(log => `
                <div>${this.escapeHtml(log.at || '')} · ${this.escapeHtml(log.message || '')}</div>`).join('');
            return `
                <div class="codex-operation">
                    <span class="codex-inline-badge ${opStatus.className}">${this.escapeHtml(opStatus.label)}</span>
                    <div>
                        <div class="codex-operation-message" title="${this.escapeHtml(operation.message || '')}">${this.escapeHtml(operation.message || '')}</div>
                        <div class="progress mt-1"><div class="progress-bar" style="width:${Math.max(0, Math.min(Number(operation.progress || 0), 100))}%"></div></div>
                    </div>
                    ${logs ? `<details><summary>过程</summary><div class="codex-operation-log">${logs}</div></details>` : '<span></span>'}
                </div>`;
        })() : '';

        const activeByChat = new Map(
            active.filter(job => job.chat_id).map(job => [String(job.chat_id), job]),
        );

        const renderSessions = () => {
            if (!sessions.length) return '<div class="codex-empty">暂无持久会话</div>';
            const rows = sessions.map(session => {
                const chatId = String(session.chat_id || '');
                const activeJob = activeByChat.get(chatId);
                const status = statusMeta(activeJob?.status || session.status);
                const input = Number(session.last_input_tokens || 0);
                const cached = Number(session.last_cached_input_tokens || 0);
                const completion = Number(session.last_completion_tokens || 0);
                const total = Number(session.last_total_tokens || 0);
                const contextWindow = Number(session.model_context_window || 0);
                const contextPercent = session.context_usage_percent === null || session.context_usage_percent === undefined
                    ? null : Number(session.context_usage_percent);
                const safePercent = Math.max(0, Math.min(contextPercent || 0, 100));
                const cacheRate = input > 0 ? cached / input * 100 : null;
                const roleName = !session.role_name || session.role_name === 'default' ? '默认' : session.role_name;
                const expanded = this.codexExpandedSessions.has(chatId);
                const sessionTotal = session.session_total || {};
                const contextClass = safePercent >= 90 ? 'danger' : safePercent >= 75 ? 'warning' : '';
                const contextHtml = contextWindow ? `
                    <div class="codex-context">
                        <div class="codex-context-label"><span>${formatK(input)}</span><span>${contextPercent.toFixed(1)}%</span></div>
                        <div class="codex-context-track ${contextClass}"><i style="width:${safePercent}%"></i></div>
                    </div>` : '<span class="codex-cell-sub">尚无数据</span>';
                const detailHtml = expanded ? `
                    <tr class="codex-detail-row"><td colspan="7">
                        <div class="codex-detail-grid">
                            <div><small>Thread ID</small><span title="${this.escapeHtml(session.thread_id || '')}">${this.escapeHtml(session.thread_id || '-')}</span></div>
                            <div><small>最近 Turn</small><span title="${this.escapeHtml(session.turn_id || '')}">${this.escapeHtml(session.turn_id || '-')}</span></div>
                            <div><small>最近一轮</small><span>输入 ${formatK(input)} · 输出 ${formatK(completion)} · 缓存 ${formatK(cached)}</span></div>
                            <div><small>线程累计</small><span>${formatK(sessionTotal.total_tokens || 0)} Token</span></div>
                            <div><small>推理 / 搜索</small><span>${this.escapeHtml(this.reasoningEffortLabel(session.reasoning_effort))} · ${session.web_search_mode ? this.escapeHtml(session.web_search_mode) : '关闭'}</span></div>
                            <div><small>统计来源</small><span>${this.escapeHtml(session.usage_source || '-')}</span></div>
                            <div><small>Schema</small><span>${this.escapeHtml(session.schema_hash || runtime.schema_hash || '-')}</span></div>
                            <div><small>最近活动</small><span>${this.formatJobTime(session.updated_at)}</span></div>
                        </div>
                    </td></tr>` : '';
                return `
                    <tr>
                        <td>
                            <div class="codex-cell-main" title="${this.escapeHtml(chatId)}">${this.escapeHtml(chatId || '-')}</div>
                            <div class="codex-cell-sub">${this.escapeHtml(roleName)}</div>
                        </td>
                        <td>
                            <span class="codex-inline-badge ${status.className}">${this.escapeHtml(status.label)}</span>
                            <div class="codex-cell-sub">${activeJob ? this.escapeHtml(activityLabel(activeJob)) : '等待消息'}</div>
                        </td>
                        <td>
                            <div class="codex-cell-main">${this.escapeHtml(session.model || '-')}</div>
                            <div class="codex-cell-sub">${Number(session.turn_count || 0).toLocaleString()} 轮</div>
                        </td>
                        <td>${contextHtml}</td>
                        <td>
                            <div class="codex-cell-main codex-mono">${formatK(total)} Token</div>
                            <div class="codex-cell-sub">缓存 ${cacheRate === null ? '-' : `${cacheRate.toFixed(1)}%`}</div>
                        </td>
                        <td><span class="codex-mono">${this.formatJobTime(session.updated_at)}</span></td>
                        <td>
                            <div class="codex-row-actions">
                                <button class="btn codex-session-toggle" data-chat-id="${this.escapeHtml(chatId)}" title="${expanded ? '收起详情' : '查看详情'}"><i class="bi bi-${expanded ? 'chevron-up' : 'chevron-down'}"></i></button>
                                ${activeJob ? `<button class="btn text-danger codex-session-interrupt" data-chat-id="${this.escapeHtml(chatId)}" title="中断当前任务"><i class="bi bi-stop-circle"></i></button>` : ''}
                                <button class="btn codex-session-reset" data-chat-id="${this.escapeHtml(chatId)}" title="开启新上下文"><i class="bi bi-arrow-repeat"></i></button>
                            </div>
                        </td>
                    </tr>${detailHtml}`;
            }).join('');
            return `
                <div class="table-responsive">
                    <table class="codex-compact-table" style="min-width:860px">
                        <thead><tr><th>会话</th><th>状态</th><th>模型</th><th>上下文</th><th>最近 Token</th><th>活动时间</th><th></th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>`;
        };

        const renderJobs = (jobs, running = false) => {
            if (!jobs.length) return `<div class="codex-empty">${running ? '当前没有运行中的任务' : '暂无任务历史'}</div>`;
            const rows = jobs.map(job => {
                const requestId = String(job.request_id || '');
                const status = statusMeta(job.status);
                const identity = job.chat_id || job.profile || job.backend || '-';
                const error = job.error ? `<div class="codex-cell-sub text-danger" title="${this.escapeHtml(job.error)}">${this.escapeHtml(job.error)}</div>` : '';
                return `
                    <tr>
                        <td><div class="codex-cell-main">${this.escapeHtml(identity)}</div><div class="codex-cell-sub codex-mono" title="${this.escapeHtml(requestId)}">${this.escapeHtml(requestId.slice(0, 12))}</div></td>
                        <td><span class="codex-inline-badge ${status.className}">${this.escapeHtml(status.label)}</span><div class="codex-cell-sub">${this.escapeHtml(activityLabel(job))}</div></td>
                        <td><div class="codex-cell-main">${this.escapeHtml(job.model || '-')}</div><div class="codex-cell-sub">${this.escapeHtml(job.pool_worker || job.backend || '')}</div></td>
                        <td><span class="codex-mono">${formatK(job.total_tokens || 0)}</span></td>
                        <td><span class="codex-mono">${this.formatJobDuration(job)}</span>${error}</td>
                        <td><span class="codex-mono">${this.formatJobTime(job.started_at)}</span></td>
                        <td><div class="codex-row-actions"><button class="btn codex-job-details" data-request-id="${this.escapeHtml(requestId)}" title="查看过程"><i class="bi bi-list-ul"></i></button>${running ? `<button class="btn text-danger codex-job-cancel" data-request-id="${this.escapeHtml(requestId)}" title="取消任务"><i class="bi bi-stop-circle"></i></button>` : ''}</div></td>
                    </tr>`;
            }).join('');
            return `
                <div class="table-responsive"><table class="codex-compact-table" style="min-width:760px">
                    <thead><tr><th>来源</th><th>进度</th><th>模型 / Worker</th><th>Token</th><th>耗时</th><th>开始时间</th><th></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>`;
        };

        container.innerHTML = `
            <div class="codex-center">
                <div class="codex-runtime-strip">
                    <div class="codex-runtime-identity">
                        <span class="codex-runtime-dot ${dotClass}"></span>
                        <div><strong>${maintenance ? '运行环境维护中' : serverRunning ? 'Codex 正常运行' : 'Codex 未就绪'}</strong><small>${this.escapeHtml(installedVersion)}</small></div>
                    </div>
                    <div class="codex-runtime-facts">
                        <span>进程<strong>${workerCount}</strong></span><span>队列<strong>${queueCount}</strong></span><span>会话<strong>${sessionCount}</strong></span><span>任务<strong>${activeCount}</strong></span><span>线程 Token<strong>${formatK(sessionStats.current_thread_total_tokens || 0)}</strong></span><span>Schema<strong class="codex-mono">${this.escapeHtml(schemaShort)}</strong></span><span>安装<strong>${this.escapeHtml(upgrade.installation?.method_label || '-')}</strong></span>
                        ${upgrade.update_available ? `<span class="codex-inline-badge warning">可用 ${this.escapeHtml(upgrade.available_version || '')}</span>` : ''}
                    </div>
                    <div class="codex-runtime-actions">
                        <button class="btn btn-outline-secondary codex-update-check" ${operationRunning ? 'disabled' : ''} title="检查最新版本"><i class="bi bi-arrow-clockwise"></i></button>
                        ${updateButton}${rollbackButton}
                    </div>
                </div>
                ${operationHtml}
                <section class="codex-section">
                    <div class="codex-section-head"><h6>会话</h6><small>${sessionCount} 个持久上下文 · ${Number(sessionStats.total_turn_count || 0).toLocaleString()} 轮</small></div>
                    ${renderSessions()}
                </section>
                ${active.length ? `<section class="codex-section"><div class="codex-section-head"><h6>正在执行</h6><small>${active.length} 个任务</small></div>${renderJobs(active, true)}</section>` : ''}
                <details class="codex-section codex-history" ${this.codexHistoryOpen ? 'open' : ''}>
                    <summary>任务历史 <span>${recent.length} 条 · 点击展开</span></summary>
                    ${renderJobs(recent, false)}
                </details>
            </div>`;

        container.querySelectorAll('.codex-session-toggle').forEach(button => {
            button.addEventListener('click', () => {
                const chatId = button.dataset.chatId;
                if (this.codexExpandedSessions.has(chatId)) this.codexExpandedSessions.delete(chatId);
                else this.codexExpandedSessions.add(chatId);
                this.renderCodexJobs(active, recent, stats, sessions, sessionStats, runtime, upgrade);
            });
        });
        container.querySelector('.codex-history')?.addEventListener('toggle', event => {
            this.codexHistoryOpen = event.currentTarget.open;
        });
        container.querySelectorAll('.codex-job-cancel').forEach(button => {
            button.addEventListener('click', () => this.cancelCodexJob(button.dataset.requestId));
        });
        container.querySelectorAll('.codex-job-details').forEach(button => {
            button.addEventListener('click', () => this.showCodexJobDetails(button.dataset.requestId));
        });
        container.querySelectorAll('.codex-session-reset').forEach(button => {
            button.addEventListener('click', () => this.resetCodexSession(button.dataset.chatId));
        });
        container.querySelectorAll('.codex-session-interrupt').forEach(button => {
            button.addEventListener('click', () => this.interruptCodexSession(button.dataset.chatId));
        });
        container.querySelector('.codex-update-check')?.addEventListener('click', () => this.checkCodexUpdate());
        container.querySelector('.codex-update-start')?.addEventListener('click', () => this.startCodexUpdate(upgrade));
        container.querySelector('.codex-update-rollback')?.addEventListener('click', () => this.rollbackCodex(upgrade));
    },

    async cancelCodexJob(requestId) {
        if (!requestId) return;
        if (!await UI.confirm(`确定要取消 Codex 任务 ${requestId.slice(0, 8)}… 吗？`, {
            title: '取消 Codex 任务',
            confirmText: '取消任务',
            variant: 'danger'
        })) return;
        try {
            await API.codexJobs.cancel(requestId);
            UI.showSuccess('已发送取消请求');
            await this.loadCodexJobs();
        } catch (e) {
            UI.showError('取消失败：' + e.message);
            await this.loadCodexJobs();
        }
    },

    async showCodexJobDetails(requestId) {
        if (!requestId) return;
        try {
            const response = await API.codexJobs.events(requestId);
            const job = response.data?.job || {};
            const events = response.data?.events || [];
            const labels = {
                queued: '进入队列', turn_started: '开始处理', item_started: '开始步骤',
                item_completed: '完成步骤', token_usage: '更新 Token', process_started: '进程启动',
                process_completed: '进程结束', response_ready: '结果就绪', error: '发生错误',
                cancel_requested: '请求中断', turn_completed: 'Turn 完成', job_finished: '任务结束',
            };
            const itemLabels = {
                userMessage: '提交输入', reasoning: '分析', agentMessage: '生成回复', commandExecution: '执行命令',
                webSearch: '网页搜索', mcpToolCall: '调用工具', fileChange: '处理文件',
                imageGeneration: '生成图片', dynamicToolCall: '调用工具',
            };
            const eventRows = events.length ? events.map(event => {
                const item = itemLabels[event.item_type] || event.item_type || '';
                const detail = event.message || item || event.status || '';
                return `<div class="codex-event"><strong>${this.escapeHtml(labels[event.type] || event.type || '-')}</strong><small>${this.formatJobTime(event.created_at)}${detail ? ` · ${this.escapeHtml(detail)}` : ''}</small></div>`;
            }).join('') : '<div class="text-muted small">暂无过程记录</div>';

            let modal = document.getElementById('codexJobEventModal');
            if (!modal) {
                modal = document.createElement('div');
                modal.id = 'codexJobEventModal';
                modal.className = 'modal fade';
                modal.tabIndex = -1;
                modal.innerHTML = `
                    <div class="modal-dialog modal-dialog-scrollable">
                        <div class="modal-content">
                            <div class="modal-header py-2"><h6 class="modal-title">任务过程</h6><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                            <div class="modal-body" id="codexJobEventBody"></div>
                        </div>
                    </div>`;
                document.body.appendChild(modal);
            }
            modal.querySelector('#codexJobEventBody').innerHTML = `
                <div class="d-flex flex-wrap gap-3 pb-3 mb-3 border-bottom small text-muted">
                    <span>状态 <strong class="text-body">${this.escapeHtml(job.status || '-')}</strong></span>
                    <span>模型 <strong class="text-body">${this.escapeHtml(job.model || '-')}</strong></span>
                    <span>耗时 <strong class="text-body">${this.formatJobDuration(job)}</strong></span>
                    <span>Token <strong class="text-body">${Number(job.total_tokens || 0).toLocaleString()}</strong></span>
                </div>
                ${job.error ? `<div class="alert alert-danger py-2 small">${this.escapeHtml(job.error)}</div>` : ''}
                <div class="codex-event-list">${eventRows}</div>`;
            bootstrap.Modal.getOrCreateInstance(modal).show();
        } catch (error) {
            UI.showError(`读取任务过程失败：${error.message}`);
        }
    },

    async checkCodexUpdate() {
        try {
            const response = await API.codexJobs.checkUpdate();
            const data = response.data || {};
            if (data.update_available) {
                UI.showSuccess(`可更新到 Codex ${data.available_version}`);
            } else {
                UI.showSuccess('当前已是最新版本');
            }
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`检查版本失败：${error.message}`);
        }
    },

    async startCodexUpdate(upgrade = {}) {
        const target = upgrade.available_version ? ` ${upgrade.available_version}` : '最新版';
        if (!await UI.confirm(`确定将 Codex 更新到${target}吗？现有任务会继续运行，验证通过后自动切换。`, {
            title: '更新 Codex',
            confirmText: '开始更新',
            variant: 'primary',
        })) return;
        try {
            await API.codexJobs.startUpdate();
            UI.showSuccess('Codex 更新任务已开始');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`启动更新失败：${error.message}`);
        }
    },

    async rollbackCodex(upgrade = {}) {
        const version = upgrade.rollback_version || '上一版本';
        if (!await UI.confirm(`确定恢复 Codex ${version} 吗？运行时会在验证通过后自动切换。`, {
            title: '恢复 Codex',
            confirmText: '开始恢复',
            variant: 'warning',
        })) return;
        try {
            await API.codexJobs.rollback();
            UI.showSuccess('Codex 恢复任务已开始');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`启动恢复失败：${error.message}`);
        }
    },

    async resetCodexSession(chatId) {
        if (!chatId) return;
        if (!await UI.confirm(`确定让“${chatId}”在下一条消息时开启新上下文吗？`, {
            title: '开启新上下文',
            confirmText: '确认',
            variant: 'warning',
        })) return;
        try {
            await API.codexJobs.resetSession(chatId);
            this.codexExpandedSessions?.delete(chatId);
            UI.showSuccess('下一条消息将使用新上下文');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`操作失败：${error.message}`);
        }
    },

    async interruptCodexSession(chatId) {
        if (!chatId) return;
        if (!await UI.confirm(`确定中断“${chatId}”当前正在运行的任务吗？`, {
            title: '中断任务',
            confirmText: '中断',
            variant: 'danger',
        })) return;
        try {
            await API.codexJobs.interruptSession(chatId);
            UI.showSuccess('已发送中断请求');
            await this.loadCodexJobs({ quiet: true });
        } catch (error) {
            UI.showError(`中断失败：${error.message}`);
        }
    },

    async fetchCallHistoryEntry(entry) {
        if (entry._fullEntry) return entry._fullEntry;
        const paging = this.currentHistoryPaging || {};
        const plugin = paging.plugin || entry.plugin_name || '';
        const callType = paging.callType || entry.call_type || '';
        const index = entry.index;
        const resp = await fetch(`/api/llm/call-history/entry?plugin_name=${encodeURIComponent(plugin)}&call_type=${encodeURIComponent(callType)}&index=${encodeURIComponent(index)}`);
        const data = await resp.json();
        if (data.status !== 'success' || !data.data) {
            throw new Error(data.detail || data.message || '调用记录不存在');
        }
        entry._fullEntry = data.data;
        return entry._fullEntry;
    },
};

// Export for use in main.js
window.LLMManager = LLMManager;
