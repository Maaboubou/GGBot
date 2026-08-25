/**
 * API Module
 * Handles all server communication
 */

const API = {
    // Base Utils
    async request(url, options = {}) {
        try {
            const response = await fetch(url, options);
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || error.message || `HTTP ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            if (error.name !== 'AbortError') {
                console.error(`API Error (${url}):`, error);
            }
            throw error;
        }
    },

    async get(url, options = {}) {
        return this.request(url, options);
    },

    async post(url, data) {
        return this.request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
    },

    async put(url, data) {
        return this.request(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
    },

    async delete(url) {
        return this.request(url, { method: 'DELETE' });
    },

    // System
    system: {
        getInfo: () => API.get('/api/system/info'),
        getStatus: () => API.get('/api/system/status'),
        getHealthDetails: () => API.get('/api/system/health/details'),
        getLogs: (type, lines, search, plugin, options = {}) => {
            const params = new URLSearchParams({
                lines: lines || 100,
                type: type || 'app'
            });
            if (search) params.append('search', search);
            if (plugin) params.append('plugin_name', plugin);
            return API.get(`/api/system/logs/${type}?${params.toString()}`, options);
        },
        restart: (serviceName) => API.post(`/api/system/restart/${serviceName}`),
        getRestartCapabilities: () => API.get('/api/system/restart-capabilities'),
        checkHealth: () => API.get('/health')
    },

    litellm: {
        getStatus: () => API.get('/api/llm/litellm/status'),
        checkUpdate: () => API.post('/api/llm/litellm/check'),
        startUpdate: () => API.post('/api/llm/litellm/update')
    },

    backups: {
        getOverview: () => API.get('/api/backups/'),
        create: options => API.post('/api/backups/', options),
        validate: name => API.post(`/api/backups/${encodeURIComponent(name)}/validate`, {}),
        prepareRestore: (name, confirmation) => API.post(
            `/api/backups/${encodeURIComponent(name)}/prepare-restore`,
            { confirmation }
        ),
        delete: (name, confirmation) => API.request(
            `/api/backups/${encodeURIComponent(name)}`,
            {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ confirmation })
            }
        ),
        downloadUrl: name => `/api/backups/${encodeURIComponent(name)}/download`,
        importFile: async file => {
            const response = await fetch(
                `/api/backups/import?filename=${encodeURIComponent(file.name)}`,
                { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }
            );
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || error.message || `HTTP ${response.status}`);
            }
            return response.json();
        }
    },

    operations: {
        getAll: (limit = 100, owner = '') => API.get(
            `/api/operations/?limit=${encodeURIComponent(limit)}${owner ? `&owner=${encodeURIComponent(owner)}` : ''}`
        ),
        get: id => API.get(`/api/operations/${encodeURIComponent(id)}`),
        cancel: id => API.post(`/api/operations/${encodeURIComponent(id)}/cancel`, {}),
        getRuntime: () => API.get('/api/operations/runtime'),
        getIncidents: (limit = 40) => API.get(`/api/operations/incidents?limit=${encodeURIComponent(limit)}`),
        getAudit: (limit = 50) => API.get(`/api/operations/audit?limit=${encodeURIComponent(limit)}`),
        getStorage: () => API.get('/api/operations/storage'),
        scanStorage: () => API.post('/api/operations/storage/scan', {}),
        getCleanupPreview: (days = 7) => API.get(`/api/operations/storage/cleanup-preview?retention_days=${encodeURIComponent(days)}`),
        cleanupStorage: (days, confirmation) => API.post('/api/operations/storage/cleanup', { retention_days: days, confirmation })
    },

    // Plugins
    plugins: {
        getAll: () => API.get('/api/plugins/'),
        getStats: () => API.get('/api/plugins/stats'),
        toggle: (name, enabled) => API.post(`/api/plugins/${name}/toggle`, { enabled }),
        reload: (name) => API.post(`/api/plugins/${name}/reload`),
        // Configuration reads and writes use API.capabilities so values are
        // normalized, validated and secrets are never echoed back.
    },

    // Product-facing capabilities. Unlike the legacy plugin endpoints these
    // responses contain normalized metadata and redacted settings only.
    capabilities: {
        getAll: () => API.get('/api/capabilities/'),
        getDetail: (id) => API.get(`/api/capabilities/${encodeURIComponent(id)}`),
        getSettings: (id) => API.get(`/api/capabilities/settings/${encodeURIComponent(id)}`),
        updateSettings: (id, values) => API.put(
            `/api/capabilities/settings/${encodeURIComponent(id)}`,
            { values }
        )
    },

    // Runtime message routing and per-event execution order.
    automation: {
        getOverview: ({ chatId = null, mentioned = true } = {}) => {
            const params = new URLSearchParams({ mentioned: mentioned ? 'true' : 'false' });
            if (chatId !== null && chatId !== undefined && chatId !== '') {
                params.set('chat_id', String(chatId));
            }
            return API.get(`/api/automation/overview?${params.toString()}`);
        },
        updateOrder: (eventType, listenerKeys, expectedSignature = null) => API.put(
            `/api/automation/events/${encodeURIComponent(eventType)}/order`,
            { listener_keys: listenerKeys, expected_signature: expectedSignature }
        )
    },

    // First-class AI assistant console. This endpoint aggregates roles,
    // judges, chat overrides and the effective global state in one request.
    assistant: {
        getOverview: () => API.get('/api/assistant/overview'),
        updateChat: (userId, changes) => API.request(`/api/assistant/chats/${userId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(changes)
        })
    },

    memory: {
        getOverview: userId => API.get(`/api/assistant/memory/chats/${userId}/overview`),
        getEvents: (userId, params = {}) => {
            const query = new URLSearchParams();
            Object.entries(params).forEach(([key, value]) => {
                if (value !== undefined && value !== null && value !== '') {
                    query.set(key, String(value));
                }
            });
            return API.get(`/api/assistant/memory/chats/${userId}/events?${query}`);
        },
        getEvent: (userId, eventId) => (
            API.get(`/api/assistant/memory/chats/${userId}/events/${eventId}`)
        ),
        correctEvent: (userId, eventId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/events/${eventId}/corrections`, payload)
        ),
        deleteEvent: (userId, eventId, reason) => (
            API.post(`/api/assistant/memory/chats/${userId}/events/${eventId}/delete`, { reason })
        ),
        reviewEvent: (userId, eventId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/events/${eventId}/review`, payload)
        ),
        updateStage: (userId, payload) => (
            API.put(`/api/assistant/memory/chats/${userId}/stage`, payload)
        ),
        getPeople: (userId, params = {}) => {
            const query = new URLSearchParams();
            Object.entries(params).forEach(([key, value]) => {
                if (value !== undefined && value !== null && value !== '') {
                    query.set(key, String(value));
                }
            });
            return API.get(`/api/assistant/memory/chats/${userId}/people?${query}`);
        },
        getPerson: (userId, personId) => (
            API.get(`/api/assistant/memory/chats/${userId}/people/${personId}`)
        ),
        reviewObservation: (userId, personId, observationId, payload) => (
            API.post(
                `/api/assistant/memory/chats/${userId}/people/${personId}/observations/${observationId}/review`,
                payload
            )
        ),
        addPersonFact: (userId, personId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/people/${personId}/facts`, payload)
        ),
        deletePersonFact: (userId, personId, factId, reason) => (
            API.post(
                `/api/assistant/memory/chats/${userId}/people/${personId}/facts/${factId}/delete`,
                { reason }
            )
        ),
        addPersonAlias: (userId, personId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/people/${personId}/aliases`, payload)
        ),
        mergePerson: (userId, personId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/people/${personId}/merge`, payload)
        ),
        getReviews: (userId, params = {}) => {
            const query = new URLSearchParams(params);
            return API.get(`/api/assistant/memory/chats/${userId}/reviews?${query}`);
        },
        getChanges: (userId, params = {}) => {
            const query = new URLSearchParams(params);
            return API.get(`/api/assistant/memory/chats/${userId}/changes?${query}`);
        },
        revertChange: (userId, changeId, category) => (
            API.post(`/api/assistant/memory/chats/${userId}/changes/${changeId}/revert`, { category })
        ),
        getMaintenance: (userId, retentionDays = 90) => (
            API.get(`/api/assistant/memory/chats/${userId}/maintenance?retention_days=${retentionDays}`)
        ),
        cleanupCandidates: (userId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/maintenance/cleanup-candidates`, payload)
        ),
        backup: (userId, confirmation) => (
            API.post(`/api/assistant/memory/chats/${userId}/maintenance/backup`, { confirmation })
        ),
        clear: (userId, payload) => (
            API.post(`/api/assistant/memory/chats/${userId}/maintenance/clear`, payload)
        )
    },

    // WeChat
    wechat: {
        getStatus: () => API.get('/api/wechat/status'),
        getMyInfo: () => API.get('/api/wechat/my-info'),
        getListeners: () => API.get('/api/wechat/listened-chats'),
        addListener: (chatName) => API.post('/api/wechat/add-listen-chat', { chat_name: chatName }),
        removeListener: (chatName) => API.post(`/api/wechat/remove-listen-chat/${chatName}`),
    },

    // Users & Permissions
    users: {
        getAll: () => API.get('/api/permissions/users'),
        delete: (id) => API.request(`/api/permissions/users/${id}`, { method: 'DELETE' }),
        updatePermissions: (userId, permissions) => API.put(`/api/permissions/users/${userId}/permissions`, permissions),
        getMemory: (userId) => API.get(`/api/permissions/users/${userId}/memory`),
        getMemoryEvents: (userId, params = {}) => {
            const query = new URLSearchParams();
            Object.entries(params).forEach(([key, value]) => {
                if (value !== undefined && value !== null && value !== '') {
                    query.set(key, String(value));
                }
            });
            return API.get(`/api/permissions/users/${userId}/memory/events?${query.toString()}`);
        },
        getMemoryPeople: (userId) => API.get(`/api/permissions/users/${userId}/memory/people`),
        getMemoryPerson: (userId, personId) => (
            API.get(`/api/permissions/users/${userId}/memory/people/${personId}`)
        ),
        getMemoryPersonAudits: (userId) => (
            API.get(`/api/permissions/users/${userId}/memory/people/audits`)
        ),
        reviewMemoryPersonObservation: (
            userId,
            personId,
            observationId,
            payload
        ) => (
            API.post(
                `/api/permissions/users/${userId}/memory/people/${personId}/observations/${observationId}/review`,
                payload
            )
        ),
        addMemoryPersonFact: (userId, personId, payload) => (
            API.post(
                `/api/permissions/users/${userId}/memory/people/${personId}/facts`,
                payload
            )
        ),
        deleteMemoryPersonFact: (userId, personId, factId, reason) => (
            API.request(
                `/api/permissions/users/${userId}/memory/people/${personId}/facts/${factId}?reason=${encodeURIComponent(reason)}`,
                { method: 'DELETE' }
            )
        ),
        refreshMemoryPerson: (userId, personId) => (
            API.post(
                `/api/permissions/users/${userId}/memory/people/${personId}/refresh`,
                {}
            )
        ),
        getMemoryPersonIdentityAudits: (userId) => (
            API.get(`/api/permissions/users/${userId}/memory/people/identity-audits`)
        ),
        addMemoryPersonAlias: (userId, personId, payload) => (
            API.post(`/api/permissions/users/${userId}/memory/people/${personId}/aliases`, payload)
        ),
        mergeMemoryPerson: (userId, personId, payload) => (
            API.post(`/api/permissions/users/${userId}/memory/people/${personId}/merge`, payload)
        ),
        revertMemoryPersonIdentityAudit: (userId, auditId) => (
            API.post(`/api/permissions/users/${userId}/memory/people/identity-audits/${auditId}/revert`, {})
        ),
        getMemoryCorrections: (userId, activeOnly = false) => (
            API.get(`/api/permissions/users/${userId}/memory/corrections?active_only=${activeOnly ? 'true' : 'false'}`)
        ),
        getMemoryEventSource: (userId, eventId) => (
            API.get(`/api/permissions/users/${userId}/memory/events/${eventId}/source`)
        ),
        correctMemoryEvent: (userId, eventId, payload) => (
            API.post(`/api/permissions/users/${userId}/memory/events/${eventId}/corrections`, payload)
        ),
        deleteMemoryEvent: (userId, eventId, reason = '') => (
            API.request(
                `/api/permissions/users/${userId}/memory/events/${eventId}?reason=${encodeURIComponent(reason || '管理员从记忆库浏览器删除事件卡')}`,
                { method: 'DELETE' }
            )
        ),
        revertMemoryCorrection: (userId, correctionId) => (
            API.post(`/api/permissions/users/${userId}/memory/corrections/${correctionId}/revert`, {})
        ),
        updateMemoryPerson: (userId, personName, payload) => (
            API.put(`/api/permissions/users/${userId}/memory/people/${encodeURIComponent(personName)}`, payload)
        ),
        updateMemory: (userId, payload) => API.put(`/api/permissions/users/${userId}/memory`, payload),
        clearMemory: (userId, scope = 'all') => API.delete(`/api/permissions/users/${userId}/memory?scope=${encodeURIComponent(scope)}`),
        addUser: (chatName, remark, isGroup, senderBlacklist = null) => API.post('/api/permissions/users', {
            chat_name: chatName,
            remark,
            is_group: isGroup,
            sender_blacklist: senderBlacklist
        }),
        updateUser: (userId, data) => API.put(`/api/permissions/users/${userId}`, data)
    },

    // Roles
    roles: {
        getAll: () => API.get('/api/chatbot/roles/'),
        getDetail: (id) => API.get(`/api/chatbot/roles/${id}`),
        create: (data) => API.post('/api/chatbot/roles/', data),
        update: (id, data) => API.request(`/api/chatbot/roles/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        }),
        delete: (id) => API.request(`/api/chatbot/roles/${id}`, { method: 'DELETE' })
    },

    // Judges
    judges: {
        getAll: () => API.get('/api/chatbot/judges/'),
        getDetail: (id) => API.get(`/api/chatbot/judges/${id}`),
        create: (data) => API.post('/api/chatbot/judges/', data),
        update: (id, data) => API.request(`/api/chatbot/judges/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        }),
        delete: (id) => API.request(`/api/chatbot/judges/${id}`, { method: 'DELETE' })
    },

    codexJobs: {
        list: () => API.get('/api/codex/jobs'),
        refreshRuntime: () => API.post('/api/codex/jobs/runtime/refresh', {}),
        cancel: (requestId) => API.post(`/api/codex/jobs/${encodeURIComponent(requestId)}/cancel`, {}),
        events: (requestId) => API.get(`/api/codex/jobs/events/${encodeURIComponent(requestId)}`),
        checkUpdate: () => API.post('/api/codex/jobs/upgrade/check', {}),
        startUpdate: () => API.post('/api/codex/jobs/upgrade/start', {}),
        rollback: () => API.post('/api/codex/jobs/upgrade/rollback', {}),
        resetSession: (chatId) => API.post('/api/codex/jobs/sessions/reset', { chat_id: chatId }),
        interruptSession: (chatId) => API.post('/api/codex/jobs/sessions/interrupt', { chat_id: chatId })
    },

    // Settings
    settings: {
        getConsole: () => API.get('/api/settings/console'),
        updateConsole: (values) => API.put('/api/settings/console', { values }),
        create: (data) => API.post('/api/settings/', data),
        delete: (key) => API.delete(`/api/settings/${key}`),
        reloadFromEnv: () => API.post('/api/settings/reload-env')
    }
};

// Export for module usage (if we switched to modules) or global
window.API = API;
