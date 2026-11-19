// ==UserScript==
// @name         Stacks - Public-domain Catalogues
// @namespace    http://tampermonkey.net/
// @version      1.0.0
// @description  Add a Stacks Download button to Project Gutenberg, Standard Ebooks and LibriVox book pages
// @author       josh1475 (Stacks fork)
// @match        https://www.gutenberg.org/ebooks/*
// @match        https://gutenberg.org/ebooks/*
// @match        https://standardebooks.org/ebooks/*
// @match        https://librivox.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_addStyle
// @connect      localhost
// @connect      127.0.0.1
// @connect      *
// ==/UserScript==

// The button sends only the catalogue's name and the book's id there (POST /api/catalogue/queue); Stacks looks the
// book up in that catalogue itself and downloads it from the catalogue's own hosts. Use the Downloader key.

(function () {
    'use strict';

    const DEFAULT_SERVER_URL = 'http://localhost:7788';
    const LABELS = { gutenberg: 'Project Gutenberg', standardebooks: 'Standard Ebooks', librivox: 'LibriVox' };

    const CONFIG = { serverUrl: DEFAULT_SERVER_URL, apiKey: '', showNotifications: true };

    function reloadConfig() {
        CONFIG.serverUrl = GM_getValue('stacksServerUrl', DEFAULT_SERVER_URL);
        CONFIG.apiKey = GM_getValue('stacksApiKey', '');
        CONFIG.showNotifications = GM_getValue('stacksShowNotifications', true);
    }
    reloadConfig();

    // ============================================================
    // STYLES (the same Dracula look as the Anna's Archive script)
    // ============================================================

    GM_addStyle(`
        #stacks-settings-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 10000; }
        #stacks-settings-dialog {
            position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
            background: #282a36 !important; padding: 30px; border-radius: 8px; box-shadow: 0 10px 40px rgba(0,0,0,0.5);
            z-index: 10001; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 500px; width: 90%; border: 4px solid #bd93f9 !important;
        }
        #stacks-settings-dialog h2 { margin: 0 0 20px 0; color: #f8f8f2 !important; font-size: 20px; }
        #stacks-settings-dialog label { display: block; margin-bottom: 8px; font-weight: 600; color: #8be9fd !important; }
        #stacks-settings-dialog input[type="text"], #stacks-settings-dialog input[type="password"] {
            width: 100%; padding: 10px; border: 2px solid #44475a !important; border-radius: 4px; font-size: 14px;
            font-family: monospace; background: #181a26 !important; color: #f8f8f2 !important; box-sizing: border-box;
        }
        #stacks-settings-dialog .row { margin-bottom: 20px; }
        #stacks-settings-dialog .hint { margin-top: 8px; font-size: 12px; color: #6272a4 !important; }
        #stacks-settings-dialog .footer { display: flex; gap: 10px; justify-content: flex-end; margin-top: 10px; }
        #stacks-settings-dialog button { padding: 10px 20px; border-radius: 4px; border: none; cursor: pointer; font-size: 14px; }
        #stacks-settings-dialog button:hover { opacity: 0.8; }
        #stacks-settings-dialog #cancelSettings { background: #6272a4 !important; color: #f8f8f2 !important; }
        #stacks-settings-dialog #saveSettings { background: #50fa7b !important; color: #282a36 !important; }
        #stacks-settings-dialog #testConnection { padding: 6px 12px; font-size: 13px; background: #8be9fd !important; color: #282a36 !important; }
        #stacks-toast-container { position: fixed; top: 20px; right: 20px; z-index: 10000; display: flex; flex-direction: column; gap: 10px; pointer-events: none; }
        .stacks-toast {
            display: flex; align-items: center; gap: 12px; min-width: 320px; max-width: 420px; padding: 16px;
            background: #282a36 !important; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); pointer-events: all;
            border: 1px solid #44475a !important; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        .stacks-toast-success { border-left: 7px solid #50fa7b !important; }
        .stacks-toast-error { border-left: 7px solid #ff5555 !important; }
        .stacks-toast-info { border-left: 7px solid #8be9fd !important; }
        .stacks-toast-title { font-weight: 600; font-size: 14px; color: #f8f8f2 !important; }
        .stacks-toast-message { font-size: 13px; color: #f8f8f2 !important; }
        #stacks-catalogue-btn {
            position: fixed; right: 20px; bottom: 20px; z-index: 9999; padding: 12px 18px; border-radius: 8px;
            border: 2px solid #bd93f9 !important; background: #282a36 !important; color: #f8f8f2 !important; cursor: pointer;
            font: 600 14px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            transition: opacity 0.2s, transform 0.1s;
        }
        #stacks-catalogue-btn:hover { opacity: 0.85; transform: scale(1.05); }
        #stacks-catalogue-btn:active { transform: scale(0.95); }
        #stacks-catalogue-btn small { display: block; font-weight: 400; color: #8be9fd !important; font-size: 11px; }
    `);

    // ============================================================
    // TOASTS
    // ============================================================

    function showToast({ title = '', message = '', type = 'info', timeout = 4000 }) {
        if (!CONFIG.showNotifications) return;
        let container = document.getElementById('stacks-toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'stacks-toast-container';
            document.body.appendChild(container);
        }
        const toast = document.createElement('div');
        toast.className = `stacks-toast stacks-toast-${type}`;
        const content = document.createElement('div');
        const t = document.createElement('div');
        t.className = 'stacks-toast-title';
        t.textContent = title;
        const m = document.createElement('div');
        m.className = 'stacks-toast-message';
        m.textContent = message;
        content.append(t, m);
        toast.appendChild(content);
        container.appendChild(toast);
        setTimeout(() => toast.remove(), timeout);
    }

    // ============================================================
    // API
    // ============================================================

    function apiRequest({ method = 'GET', path = '/', body = null, baseUrl, apiKey, timeout = 30000 }) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method,
                url: (baseUrl || CONFIG.serverUrl).replace(/\/+$/, '') + path,
                headers: { 'X-API-Key': apiKey || CONFIG.apiKey, 'Content-Type': 'application/json' },
                data: body ? JSON.stringify(body) : undefined,
                timeout,
                onload: (response) => {
                    let data = null;
                    try { data = response.responseText ? JSON.parse(response.responseText) : null; } catch (e) { /* ignore */ }
                    resolve({ status: response.status, data });
                },
                onerror: () => reject(new Error('Failed to connect to Stacks server')),
                ontimeout: () => reject(new Error('Request timed out')),
            });
        });
    }

    // ============================================================
    // WHICH BOOK THIS PAGE IS
    // ============================================================

    /** {source, id} for this page, or null when it isn't one book's page. */
    function pageItem() {
        const host = location.hostname;
        const path = location.pathname;
        if (host === 'www.gutenberg.org' || host === 'gutenberg.org') {
            const m = path.match(/^\/ebooks\/(\d{1,7})\/?$/);
            return m ? { source: 'gutenberg', id: m[1] } : null;
        }
        if (host === 'standardebooks.org') {
            const parts = path.replace(/^\/ebooks\//, '').replace(/\/+$/, '').split('/');
            if (!path.startsWith('/ebooks/') || parts.length < 2 || parts.length > 5) return null;
            if (parts.includes('downloads') || parts.includes('text')) return null;
            if (!parts.every((p) => /^[a-z0-9_-]{1,120}$/.test(p))) return null;
            return { source: 'standardebooks', id: parts.join('/') };
        }
        if (host === 'librivox.org') {
            // An audiobook's page links its podcast feed, https://librivox.org/rss/<id>
            const rss = document.querySelector('a[href*="librivox.org/rss/"], a[href^="/rss/"]');
            const m = rss && rss.getAttribute('href').match(/\/rss\/(\d{1,8})\/?$/);
            return m ? { source: 'librivox', id: m[1] } : null;
        }
        return null;
    }

    async function queue(item) {
        if (!CONFIG.apiKey) {
            throw new Error('API key not configured. Tampermonkey icon → Stacks Settings (use the Downloader key).');
        }
        const result = await apiRequest({ method: 'POST', path: '/api/catalogue/queue', body: { source: item.source, id: item.id, added_by: 'browser' } });
        if (result.status === 401) throw new Error('Invalid API key. Get the Downloader key from Stacks → Settings.');
        return result;
    }

    // ============================================================
    // THE BUTTON
    // ============================================================

    function addButton(item) {
        if (document.getElementById('stacks-catalogue-btn')) return;
        const btn = document.createElement('button');
        btn.id = 'stacks-catalogue-btn';
        btn.type = 'button';
        btn.textContent = item.source === 'librivox' ? 'Download audiobook' : 'Download';
        const small = document.createElement('small');
        small.textContent = `with Stacks · ${LABELS[item.source]}`;
        btn.appendChild(small);
        btn.title = 'Add to Stacks queue';
        btn.addEventListener('click', async () => {
            const label = btn.firstChild.textContent;
            btn.firstChild.textContent = 'Adding...';
            btn.disabled = true;
            try {
                const r = await queue(item);
                if (r.data && r.data.success) {
                    showToast({ title: 'Success', message: `Added ${r.data.title || 'it'} to queue`, type: 'success' });
                    btn.firstChild.textContent = 'Queued';
                } else {
                    const msg = (r.data && (r.data.message || r.data.error)) || `Failed with status ${r.status}`;
                    showToast({ title: r.status === 409 ? 'Info' : 'Error', message: msg, type: r.status === 409 ? 'info' : 'error', timeout: 6000 });
                    btn.firstChild.textContent = label;
                }
            } catch (error) {
                showToast({ title: 'Error', message: error.message, type: 'error', timeout: 6000 });
                btn.firstChild.textContent = label;
            }
            btn.disabled = false;
        });
        document.body.appendChild(btn);
    }

    // ============================================================
    // SETTINGS
    // ============================================================

    function field(id, label, type, value, placeholder, hint) {
        const row = document.createElement('div');
        row.className = 'row';
        const l = document.createElement('label');
        l.htmlFor = id;
        l.textContent = label;
        const input = document.createElement('input');
        input.type = type;
        input.id = id;
        input.value = value;
        input.placeholder = placeholder;
        const h = document.createElement('div');
        h.className = 'hint';
        h.textContent = hint;
        row.append(l, input, h);
        return row;
    }

    function showSettingsDialog() {
        const overlay = document.createElement('div');
        overlay.id = 'stacks-settings-overlay';
        const dialog = document.createElement('div');
        dialog.id = 'stacks-settings-dialog';
        const h2 = document.createElement('h2');
        h2.textContent = '⚙️ Stacks Settings';
        dialog.appendChild(h2);
        dialog.appendChild(field('stacksServerUrl', 'Server URL:', 'text', CONFIG.serverUrl, 'http://your-server:7788',
            'For example http://localhost:7788 or http://192.168.1.100:7788'));
        dialog.appendChild(field('stacksApiKey', 'API Key:', 'password', CONFIG.apiKey, 'Your 32-character Downloader key',
            'Stacks web interface → Settings tab → Downloader API Key (click Copy).'));

        const notes = document.createElement('div');
        notes.className = 'row';
        const nl = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.id = 'stacksShowNotifications';
        cb.checked = CONFIG.showNotifications;
        nl.append(cb, document.createTextNode(' Show notifications'));
        notes.appendChild(nl);
        dialog.appendChild(notes);

        const status = document.createElement('div');
        status.className = 'hint';
        status.textContent = 'Click "Test Connection" to verify';
        const test = document.createElement('button');
        test.id = 'testConnection';
        test.textContent = 'Test Connection';
        dialog.append(status, test);

        const footer = document.createElement('div');
        footer.className = 'footer';
        const cancel = document.createElement('button');
        cancel.id = 'cancelSettings';
        cancel.textContent = 'Cancel';
        const save = document.createElement('button');
        save.id = 'saveSettings';
        save.textContent = 'Save Settings';
        footer.append(cancel, save);
        dialog.appendChild(footer);

        document.body.append(overlay, dialog);
        const close = () => { overlay.remove(); dialog.remove(); };
        overlay.addEventListener('click', close);
        cancel.addEventListener('click', close);

        test.addEventListener('click', async () => {
            status.textContent = 'Testing...';
            try {
                const r = await apiRequest({ path: '/api/subdirs', baseUrl: dialog.querySelector('#stacksServerUrl').value.trim(),
                    apiKey: dialog.querySelector('#stacksApiKey').value.trim(), timeout: 10000 });
                status.textContent = r.status === 200 ? '✓ Connected' : r.status === 401 ? '✕ Invalid API key' : `✕ Status ${r.status}`;
            } catch (e) {
                status.textContent = '✕ ' + e.message;
            }
        });

        save.addEventListener('click', () => {
            const url = dialog.querySelector('#stacksServerUrl').value.trim();
            const key = dialog.querySelector('#stacksApiKey').value.trim();
            if (!url || !/^https?:\/\//.test(url)) { alert('Please enter a server URL starting with http:// or https://'); return; }
            if (!key) { alert('Please enter an API key'); return; }
            GM_setValue('stacksServerUrl', url);
            GM_setValue('stacksApiKey', key);
            GM_setValue('stacksShowNotifications', dialog.querySelector('#stacksShowNotifications').checked);
            reloadConfig();
            close();
            showToast({ title: 'Settings Saved', message: 'Ready to download.', type: 'success' });
        });
    }

    GM_registerMenuCommand('⚙️ Stacks Settings', showSettingsDialog);

    // ============================================================
    // INIT
    // ============================================================

    function init() {
        const item = pageItem();
        if (item) addButton(item);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
