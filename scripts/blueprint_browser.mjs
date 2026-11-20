// Capture evidence through the browser controlled by the coordinator.
import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';

async function waitFor(probe, message) {
    for (let attempt = 0; attempt < 100; attempt++) {
        const value = await probe();
        if (value) return value;
        await delay(100);
    }
    throw new Error(message);
}

async function markerPaints(evaluate, screenshot, captureScreenshot, selector, rect) {
    // Chrome's visibility observer is conservative for translucency and partial covering.
    // For those cases, check whether the marker contributes any stable screenshot pixels.
    await evaluate(`(() => {
        const marker = document.querySelector(${JSON.stringify(selector)});
        const original = marker.getAttribute('style');
        globalThis.restoreMarkerStyle = () => {
            if (original === null) marker.removeAttribute('style');
            else marker.setAttribute('style', original);
        };
        marker.style.setProperty('transition', 'none', 'important');
        marker.style.setProperty('opacity', '0', 'important');
    })()`);
    let hidden;
    try {
        hidden = await captureScreenshot();
    } finally {
        await evaluate('restoreMarkerStyle()');
    }
    const restored = await captureScreenshot();
    return evaluate(`(async () => {
        const rect = ${JSON.stringify(rect)};
        const x = Math.floor(rect.left);
        const y = Math.floor(rect.top);
        const width = Math.ceil(rect.right) - x;
        const height = Math.ceil(rect.bottom) - y;
        const canvas = new OffscreenCanvas(width, height);
        const context = canvas.getContext('2d', { willReadFrequently: true });
        async function pixels(png) {
            const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
            const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
            context.drawImage(bitmap, x, y, width, height, 0, 0, width, height);
            bitmap.close();
            return context.getImageData(0, 0, width, height).data;
        }
        const before = await pixels(${JSON.stringify(screenshot)});
        const without = await pixels(${JSON.stringify(hidden)});
        const after = await pixels(${JSON.stringify(restored)});
        for (let offset = 0; offset < before.length; offset += 4) {
            const stable = before[offset] === after[offset] && before[offset + 1] === after[offset + 1] &&
                before[offset + 2] === after[offset + 2];
            const changed = before[offset] !== without[offset] || before[offset + 1] !== without[offset + 1] ||
                before[offset + 2] !== without[offset + 2];
            if (stable && changed) return true;
        }
        return false;
    })()`);
}

async function checkBuild(send, responses, build, url) {
    const relevant = responses.filter(response =>
        new URL(response.url).origin === url.origin &&
        ['Document', 'Script', 'Stylesheet', 'Fetch', 'XHR'].includes(response.type));
    if (!relevant.some(response => response.type === 'Document' && response.url === url.href)) {
        throw new Error('Could not identify the served build document');
    }
    const evidence = [];
    for (const response of relevant) {
        const headers = Object.fromEntries(Object.entries(response.headers).map(
            ([name, value]) => [name.toLowerCase(), value]));
        const fingerprint = headers['x-p2p-build-fingerprint'];
        if (fingerprint !== undefined) {
            if (fingerprint !== build.fingerprint) throw new Error('The served build fingerprint is stale');
            evidence.push({ url: response.url, fingerprint });
            continue;
        }
        const path = decodeURIComponent(new URL(response.url).pathname).replace(/^\//, '');
        const candidates = new Set([path]);
        if (response.type === 'Document') {
            if (!path || path.endsWith('/')) candidates.add(path + 'index.html');
            else if (!path.endsWith('.html')) {
                candidates.add(path + '.html');
                candidates.add(path + '/index.html');
            }
        }
        const matches = Object.entries(build.sources).filter(([file]) =>
            [...candidates].some(candidate => candidate && (file === candidate || file.endsWith('/' + candidate))));
        const { body, base64Encoded } = await send('Network.getResponseBody', { requestId: response.requestId });
        const digest = createHash('sha256').update(Buffer.from(body, base64Encoded ? 'base64' : 'utf8')).digest('hex');
        if (matches.length !== 1 || matches[0][1].split(':').at(-1) !== digest) {
            throw new Error('The served build does not match current source for /' + path +
                '. Serve current source bytes or a build-time X-P2P-Build-Fingerprint.');
        }
        evidence.push({ url: response.url, source: matches[0][0], sha256: digest });
    }
    return evidence;
}

async function capture({ browser, appUrl, renderCase, checks, build }) {
    const url = new URL(renderCase.route, appUrl);
    if (!['http:', 'https:'].includes(url.protocol) || url.origin !== new URL(appUrl).origin) {
        throw new Error('The approved route must be on the configured HTTP(S) application');
    }
    const profile = await mkdtemp(join(tmpdir(), 'p2p-browser-'));
    const args = ['--headless', '--disable-gpu', '--disable-background-networking',
        '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
        '--user-data-dir=' + profile];
    if (process.getuid?.() === 0) args.push('--no-sandbox');
    const chrome = spawn(browser, [...args, 'about:blank'], { stdio: 'ignore' });
    let socket;
    try {
        await once(chrome, 'spawn');
        const port = await waitFor(async () => {
            try {
                return (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0];
            } catch (error) {
                if (error.code !== 'ENOENT') throw error;
                return null;
            }
        }, 'Chromium did not open a debugging port');
        const endpoint = `http://127.0.0.1:${port}`;
        const tabs = await (await fetch(endpoint + '/json/list', { signal: AbortSignal.timeout(5000) })).json();
        const page = tabs.find(tab => tab.type === 'page' && tab.url === 'about:blank');
        if (!page) throw new Error('Chromium did not open a fresh page');
        socket = new WebSocket(page.webSocketDebuggerUrl);
        await once(socket, 'open', { signal: AbortSignal.timeout(5000) });
        let sequence = 0;
        function send(method, params = {}) {
            return new Promise((resolve, reject) => {
                const id = ++sequence;
                const timeout = setTimeout(() => {
                    socket.removeEventListener('message', receive);
                    reject(new Error(method + ' timed out'));
                }, 5000);
                function receive(event) {
                    const reply = JSON.parse(event.data);
                    if (reply.id !== id) return;
                    clearTimeout(timeout);
                    socket.removeEventListener('message', receive);
                    if (reply.error) reject(new Error(reply.error.message));
                    else resolve(reply.result);
                }
                socket.addEventListener('message', receive);
                socket.send(JSON.stringify({ id, method, params }));
            });
        }

        const responses = [];
        if (build) {
            socket.addEventListener('message', event => {
                const message = JSON.parse(event.data);
                if (message.method === 'Network.responseReceived') {
                    const { requestId, type, response } = message.params;
                    responses.push({ requestId, type, url: response.url, headers: response.headers });
                }
            });
            await send('Network.enable');
            await send('Network.setCacheDisabled', { cacheDisabled: true });
            await send('Network.setBypassServiceWorker', { bypass: true });
        }
        const { width, height } = renderCase.viewport;
        await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
        async function navigate(destination) {
            if (destination.origin !== url.origin) throw new Error('Discovery navigation must stay on the application origin');
            const navigation = await send('Page.navigate', { url: destination.href });
            if (navigation.errorText) throw new Error(navigation.errorText);
            await waitFor(async () => {
                const { result } = await send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true });
                return result.value === 'complete';
            }, 'The application did not finish loading');
        }
        await navigate(url);

        if (renderCase.command) {
            const [command, ...commandArgs] = renderCase.command;
            await promisify(execFile)(command, commandArgs, {
                env: { ...process.env, P2P_BROWSER_ENDPOINT: endpoint, P2P_BROWSER_PAGE: page.webSocketDebuggerUrl },
                timeout: 45000,
            });
        }
        for (const step of renderCase.setup ?? []) {
            if (step.action === 'navigate') {
                await navigate(new URL(step.route, url.origin));
                continue;
            }
            await waitFor(async () => {
                const expression = `(() => {
                    const step = ${JSON.stringify(step)};
                    if (location.origin !== ${JSON.stringify(url.origin)}) throw new Error('Outside the application origin');
                    const target = document.querySelector(step.selector);
                    if (!target) return false;
                    if (step.action === 'click') target.click();
                    if (step.action === 'fill') {
                        const proto = target instanceof HTMLInputElement ? HTMLInputElement.prototype :
                            target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : null;
                        if (!proto) throw new Error('Expected a text input or textarea');
                        Object.getOwnPropertyDescriptor(proto, 'value').set.call(target, step.value);
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    return true;
                })()`;
                const { result, exceptionDetails } = await send('Runtime.evaluate', { expression, returnByValue: true });
                if (exceptionDetails) throw new Error(`Could not perform browser setup action ${step.action}: ${step.selector}`);
                return result.value;
            }, `Browser setup selector did not appear: ${step.selector}`);
        }

        const { frameTree } = await send('Page.getFrameTree');
        const { executionContextId } = await send('Page.createIsolatedWorld', { frameId: frameTree.frame.id });
        async function evaluate(expression) {
            const evaluated = await send('Runtime.evaluate', {
                expression, contextId: executionContextId, returnByValue: true, awaitPromise: true,
            });
            if (evaluated.exceptionDetails) throw new Error('Could not measure the approved browser state');
            return evaluated.result.value;
        }
        async function captureScreenshot() {
            const { data } = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
            return data;
        }
        const identityRules = renderCase.observations;
        const observation = await evaluate(`(async () => {
            const measure = ({ selector, property }) => {
                const element = document.querySelector(selector);
                return element ? getComputedStyle(element).getPropertyValue(property).trim() : null;
            };
            const ready = document.querySelector(${JSON.stringify(renderCase.ready_selector)});
            const requestedIdentities = ${JSON.stringify(identityRules)};
            const identities = requestedIdentities && Object.fromEntries(Object.entries(requestedIdentities)
                .map(([name, rule]) => {
                    const node = document.querySelector(rule.selector);
                    const actual = rule.attribute ? node?.getAttribute(rule.attribute) : node?.textContent;
                    return [name, actual?.trim() ?? null];
                }));
            const visible = element => element && element.checkVisibility({
                checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true
            }) && new Promise(resolve => {
                const observer = new IntersectionObserver(([entry]) => {
                    observer.disconnect();
                    const rect = entry.intersectionRect;
                    resolve(entry.isIntersecting && rect.width > 0 && rect.height > 0 && {
                        rect: rect.toJSON(), visiblyUnobstructed: entry.isVisible === true,
                    });
                }, { trackVisibility: true, delay: 100 });
                observer.observe(element);
            });
            const criteria = ${JSON.stringify(checks)};
            const [state, ...targets] = await Promise.all([visible(ready), ...criteria.map(rule =>
                rule.visibility === 'hidden' ? null : visible(document.querySelector(rule.selector))
            )]);
            return {
                url: location.href,
                viewport: { width: innerWidth, height: innerHeight },
                state,
                targets,
                actual: criteria.map(rule => {
                    const element = document.querySelector(rule.selector);
                    if (rule.visibility === 'hidden' && (!element || element.checkVisibility({
                        checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true
                    }))) return null;
                    return measure(rule);
                }),
                ...(identities ? { identities } : {}),
            };
        })()`);
        const { state, targets, ...measurements } = observation;
        if (observation.url !== url.href || observation.viewport.width !== width ||
            observation.viewport.height !== height || !state) {
            throw new Error('The browser did not show the approved route, viewport and visible state');
        }
        const screenshot = await captureScreenshot();
        if (!state.visiblyUnobstructed &&
            !await markerPaints(evaluate, screenshot, captureScreenshot, renderCase.ready_selector, state.rect)) {
            throw new Error('The browser did not show the approved visible state in the screenshot');
        }
        for (const [index, rule] of checks.entries()) {
            if (rule.visibility === 'hidden') continue;
            const target = targets[index];
            if (!target || (!target.visiblyUnobstructed &&
                !await markerPaints(evaluate, screenshot, captureScreenshot, rule.selector, target.rect))) {
                throw new Error('The browser did not show the approved visible measurement target: ' + rule.selector);
            }
        }
        const buildEvidence = build && await checkBuild(send, responses, build, url);
        return { ...measurements, stateVisible: true, ...(build ? { buildEvidence } : {}), screenshot };
    } finally {
        socket?.close();
        if (chrome.pid && chrome.exitCode === null && chrome.signalCode === null) {
            const exited = once(chrome, 'exit');
            chrome.kill();
            await exited;
        }
        await rm(profile, { recursive: true, force: true, maxRetries: 5 }).catch(() => {});
    }
}

try {
    if (Number(process.versions.node.split('.')[0]) < 22 || typeof WebSocket !== 'function') {
        throw new Error('Blueprint browser verification requires Node.js 22 or later with WebSocket support');
    }
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    console.log(JSON.stringify(await capture(JSON.parse(input))));
} catch (error) {
    console.error(error.message);
    process.exitCode = 1;
}
