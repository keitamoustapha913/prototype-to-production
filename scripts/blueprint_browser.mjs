// Capture evidence through the browser controlled by the coordinator.
import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';

const run = promisify(execFile);
const keyboardKeys = new Map([
    ['Tab', 9], ['Enter', 13], ['Escape', 27], ['Space', 32], ['End', 35],
    ['Home', 36], ['ArrowLeft', 37], ['ArrowUp', 38], ['ArrowRight', 39], ['ArrowDown', 40],
]);

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

async function capture({ browser, appUrl, renderCase, checks = [], build, interaction_assertions = [] }, evidenceDirectory) {
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
        const browserVersion = evidenceDirectory ? await send('Browser.getVersion') : undefined;
        if (renderCase.color_scheme !== undefined) {
            if (!['light', 'dark'].includes(renderCase.color_scheme)) throw new Error('Unknown color scheme');
            await send('Emulation.setEmulatedMedia', {
                features: [{name: 'prefers-color-scheme', value: renderCase.color_scheme}],
            });
        }
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
            await run(command, commandArgs, {
                env: { ...process.env, P2P_BROWSER_ENDPOINT: endpoint, P2P_BROWSER_PAGE: page.webSocketDebuggerUrl },
                timeout: 45000,
            });
        }
        for (const step of renderCase.setup ?? []) {
            if (step.action === 'navigate') {
                await navigate(new URL(step.route, url.origin));
                continue;
            }
            if (step.action === 'press') {
                const { result } = await send('Runtime.evaluate', { expression: 'location.origin', returnByValue: true });
                const windowsVirtualKeyCode = keyboardKeys.get(step.key);
                if (result.value !== url.origin || !windowsVirtualKeyCode) {
                    throw new Error('Browser setup requires a supported key on the application origin');
                }
                const event = { key: step.key === 'Space' ? ' ' : step.key,
                    code: step.key, windowsVirtualKeyCode, modifiers: step.shift ? 8 : 0 };
                await send('Input.dispatchKeyEvent', { ...event, type: 'keyDown',
                    ...(step.key === 'Enter' ? {text: '\r'} : step.key === 'Space' ? {text: ' '} : {}) });
                await send('Input.dispatchKeyEvent', { ...event, type: 'keyUp' });
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
            const assertions = ${JSON.stringify(interaction_assertions)};
            async function observeAssertion(rule) {
                const node = document.querySelector(rule.selector);
                switch (rule.probe) {
                    case 'visible': return { target: await visible(node) };
                    case 'focused': return { actual: Boolean(node && document.activeElement === node),
                        insideComponent: Boolean(ready && node && ready.contains(node)) };
                    case 'accessibility': return {};
                    case 'text': return { missing: !node, actual: node?.textContent.trim() ?? null };
                    case 'attribute': return { missing: !node, actual: node?.getAttribute(rule.attribute) ?? null };
                    default: throw new Error('Unknown interaction assertion');
                }
            }
            const visualEvidence = ${Boolean(evidenceDirectory)};
            const describe = node => node && ({
                tag: node.localName, id: node.id, role: node.getAttribute('role'),
                testId: node.getAttribute('data-testid'), name: node.getAttribute('name'),
            });
            const geometry = node => node && ({
                bounds: node.getBoundingClientRect().toJSON(),
                clientWidth: node.clientWidth, clientHeight: node.clientHeight,
                scrollWidth: node.scrollWidth, scrollHeight: node.scrollHeight,
            });
            const tokenNames = ${JSON.stringify(renderCase.design_tokens || [])};
            const styles = (node, properties) => {
                const computed = getComputedStyle(node);
                return Object.fromEntries(properties.map(name => [name, computed.getPropertyValue(name).trim()]));
            };
            const controls = 'button, a[href], input:not([type="hidden"]), select, textarea, summary, audio[controls], video[controls], [onclick], [aria-controls], [tabindex], [contenteditable="true"], [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="menuitem"], [role="option"], [role="switch"], [role="slider"], [role="tab"], [role="textbox"]';
            const descendants = visualEvidence ? ready?.getElementsByTagName('*') : null;
            const [state, targets, probes] = await Promise.all([
                visible(ready),
                Promise.all(criteria.map(rule =>
                    rule.visibility === 'hidden' ? null : visible(document.querySelector(rule.selector)))),
                Promise.all(assertions.map(observeAssertion)),
            ]);
            return {
                url: location.href,
                viewport: { width: innerWidth, height: innerHeight },
                state,
                targets,
                probes,
                actual: criteria.map(rule => {
                    const element = document.querySelector(rule.selector);
                    if (rule.visibility === 'hidden' && (!element || element.checkVisibility({
                        checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true
                    }))) return null;
                    return measure(rule);
                }),
                ...(identities ? { identities } : {}),
                ...(visualEvidence ? {
                    browserState: {
                        activeControl: describe(document.activeElement),
                        colorScheme: getComputedStyle(document.documentElement).colorScheme,
                        prefersDark: matchMedia('(prefers-color-scheme: dark)').matches,
                        expandedControls: [...document.querySelectorAll('[aria-expanded="true"], details[open]')]
                            .filter(node => node.getClientRects().length > 0).map(describe),
                    },
                    structure: {
                        component: describe(ready),
                        interactive_controls: ready && (Number(ready.matches(controls)) + ready.querySelectorAll(controls).length),
                        descendants: Array.from({length: Math.min(descendants?.length || 0, 200)}, (_, index) => {
                            const node = descendants[index];
                            let depth = 1;
                            for (let parent = node.parentElement; parent !== ready; parent = parent.parentElement) depth++;
                            return {...describe(node), depth};
                        }),
                        truncated: (descendants?.length || 0) > 200,
                    },
                    designTokens: ready && {
                        component: styles(ready, ['color', 'background-color', 'font-family', 'font-size', 'font-weight',
                            'line-height', 'display', 'gap', 'padding', 'margin', 'border-radius', 'position', ...tokenNames]),
                        root: styles(document.documentElement, tokenNames),
                    },
                    geometry: {
                        component: geometry(ready), page: geometry(document.documentElement),
                        targets: criteria.map(rule => geometry(document.querySelector(rule.selector))),
                    },
                } : {}),
            };
        })()`);
        const { state, targets, probes, ...measurements } = observation;
        if (observation.url !== url.href || observation.viewport.width !== width ||
            observation.viewport.height !== height || !state) {
            throw new Error('The browser did not show the approved route, viewport and visible state');
        }
        const screenshot = await captureScreenshot();
        async function showsMarker(selector, target) {
            return Boolean(target && (target.visiblyUnobstructed ||
                await markerPaints(evaluate, screenshot, captureScreenshot, selector, target.rect)));
        }
        if (!await showsMarker(renderCase.ready_selector, state)) {
            throw new Error('The browser did not show the approved visible state in the screenshot');
        }
        for (const [index, rule] of checks.entries()) {
            if (rule.visibility === 'hidden') continue;
            if (!await showsMarker(rule.selector, targets[index])) {
                if (!evidenceDirectory) {
                    throw new Error('The browser did not show the approved visible measurement target: ' + rule.selector);
                }
                measurements.actual[index] = null;
            }
        }
        if (evidenceDirectory || interaction_assertions.length) {
            let documentNode;
            const semantics = new Map();
            async function accessibilityTarget(selector) {
                if (semantics.has(selector)) return semantics.get(selector);
                documentNode ??= (await send('DOM.getDocument', { depth: 0 })).root.nodeId;
                const { nodeId } = await send('DOM.querySelector', { nodeId: documentNode, selector });
                let target = null;
                if (nodeId) {
                    const { nodes } = await send('Accessibility.getPartialAXTree', { nodeId, fetchRelatives: false });
                    const node = nodes[0];
                    if (node && !node.ignored) {
                        target = { name: node.name?.value ?? null, role: node.role?.value ?? null, disabled: false };
                        for (const property of node.properties || []) {
                            if (['expanded', 'disabled', 'checked', 'selected', 'pressed'].includes(property.name)) {
                                const value = property.value.value;
                                target[property.name] = value === 'true' ? true : value === 'false' ? false : value;
                            }
                        }
                    }
                }
                semantics.set(selector, target);
                return target;
            }
            measurements.interactionAssertions = [];
            const accessibilityTargets = [];
            for (const [index, rule] of interaction_assertions.entries()) {
                const probe = probes[index];
                let actual = probe.actual;
                let matches;
                if (rule.probe === 'accessibility') {
                    const target = await accessibilityTarget(rule.selector);
                    actual = target && Object.fromEntries(Object.keys(rule.expected).map(key => [key, target[key] ?? null]));
                    matches = Boolean(actual && Object.entries(rule.expected).every(([key, value]) => actual[key] === value));
                } else {
                    if (rule.probe === 'visible') actual = await showsMarker(rule.selector, probe.target);
                    matches = !probe.missing && actual === rule.expected;
                }
                if (evidenceDirectory && rule.probe === 'focused' && actual === true) {
                    const target = await accessibilityTarget(rule.selector);
                    accessibilityTargets.push([rule.selector, { ...target, inside_component: probe.insideComponent }]);
                }
                measurements.interactionAssertions.push({ ...rule, actual, result: matches ? 'pass' : 'fail' });
            }
            if (evidenceDirectory) measurements.accessibilityTargets = Object.fromEntries(accessibilityTargets);
        }
        const buildEvidence = build && await checkBuild(send, responses, build, url);
        if (evidenceDirectory) {
            // Crop the visible portion from the full page so animation cannot change between images.
            const { cssContentSize, cssLayoutViewport } = await send('Page.getLayoutMetrics');
            const fullPage = {
                x: 0, y: 0, width: Math.ceil(Math.max(width, cssContentSize.width)),
                height: Math.ceil(Math.max(height, cssContentSize.height)), scale: 1,
            };
            const x = Math.max(0, Math.floor(state.rect.left + cssLayoutViewport.pageX));
            const y = Math.max(0, Math.floor(state.rect.top + cssLayoutViewport.pageY));
            const right = Math.min(fullPage.width, Math.ceil(state.rect.right + cssLayoutViewport.pageX));
            const bottom = Math.min(fullPage.height, Math.ceil(state.rect.bottom + cssLayoutViewport.pageY));
            const closeup = {x, y, width: right - x, height: bottom - y, scale: 1};
            const { data } = await send('Page.captureScreenshot', {
                format: 'png', clip: fullPage, captureBeyondViewport: true,
            });
            const full = join(evidenceDirectory, 'full.png');
            const detail = join(evidenceDirectory, 'closeup.png');
            await mkdir(evidenceDirectory); // An existing attempt must never be overwritten.
            try {
                await writeFile(full, Buffer.from(data, 'base64'));
                await run(process.env.P2P_CAPTURE_PYTHON || 'python3', ['-c', `
import sys
from PIL import Image

with Image.open(sys.argv[1]) as page:
    page.crop(tuple(map(int, sys.argv[3:]))).save(sys.argv[2])
`, full, detail, ...[x, y, right, bottom].map(String)], { timeout: 30000 });
            } catch (error) {
                await rm(evidenceDirectory, { recursive: true, force: true });
                throw error;
            }
            return { ...measurements, buildEvidence, component: renderCase.ready_selector, browserVersion, closeup, full, detail };
        }
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
    const args = process.argv.slice(2);
    if (args.length && (args.length !== 2 || args[0] !== '--visual-evidence' || !args[1])) {
        throw new Error('Usage: blueprint_browser.mjs [--visual-evidence NEW_DIRECTORY] < capture.json');
    }
    console.log(JSON.stringify(await capture(JSON.parse(input), args[1])));
} catch (error) {
    console.error(error.message);
    process.exitCode = 1;
}
