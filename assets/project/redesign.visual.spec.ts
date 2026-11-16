/**
 * COPY AND ADAPT into the actual app's existing Playwright test tree.
 * Requires existing @playwright/test and a running, seeded app. Do not install tooling silently.
 * This refuses to create/update a golden baseline. Approved prototype PNGs remain immutable.
 * Comparison is strict decoded-pixel equality: different PNG compression/metadata
 * do not cause false mismatches. Human visual review is still required.
 */
import { test, expect, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve } from 'node:path';

type VisualCase = {
  case_id: string;
  reference_file: string;
  route: string;
  viewport: { width: number; height: number };
  fixture: string;
  state_setup: string;
};
type SliceContract = {
  slice_id: string;
  prototype_version: string;
  approved_artifact_hashes: Record<string, string>;
  visual_cases: VisualCase[];
};
type ApprovedManifest = {
  active_approval_id: string;
  approved_version: string;
  approvals: Record<string, { visual_cases: VisualCase[] }>;
  slices: Record<string, { status: string }>;
};

const projectRoot = resolve(process.env.REDESIGN_PROJECT_ROOT ?? process.cwd());
const initiativeId = process.env.REDESIGN_INITIATIVE_ID;
const sliceId = process.env.REDESIGN_SLICE_ID;
const appBaseUrl = process.env.REDESIGN_APP_BASE_URL;
if (!initiativeId || !sliceId || !appBaseUrl ||
    !/^[a-z][a-z0-9-]{2,60}$/.test(initiativeId) ||
    !/^[a-z][a-z0-9-]{2,60}$/.test(sliceId)) {
  throw new Error('BLOCKED: REDESIGN_PROJECT_ROOT, REDESIGN_INITIATIVE_ID, REDESIGN_SLICE_ID and REDESIGN_APP_BASE_URL are required');
}
const candidates = [
  resolve(projectRoot, '.artifacts', 'prototype-to-production', initiativeId),
  resolve(projectRoot, 'design', 'redesigns', initiativeId),
].filter(path => existsSync(resolve(path, 'manifest.json')));
if (candidates.length !== 1) {
  throw new Error('BLOCKED: missing or ambiguous redesign state in hidden/legacy initiative directories');
}
const initiative = candidates[0];

function artifact(relativePath: string): string {
  if (!relativePath || isAbsolute(relativePath) || relativePath.split(/[\\/]/).includes('..')) {
    throw new Error(`BLOCKED: invalid artifact path ${relativePath}`);
  }
  const absolute = resolve(initiative, relativePath);
  if (relative(initiative, absolute).startsWith('..')) {
    throw new Error(`BLOCKED: artifact outside initiative: ${relativePath}`);
  }
  return absolute;
}
function sha256(bytes: Buffer): string {
  return createHash('sha256').update(bytes).digest('hex');
}
function lockedBytes(relativePath: string, hashes: Record<string, string>): Buffer {
  const path = artifact(relativePath);
  if (!existsSync(path)) throw new Error(`BLOCKED: missing locked visual/source artifact ${relativePath}`);
  const bytes = readFileSync(path);
  if (!hashes[relativePath] || sha256(bytes) !== hashes[relativePath]) {
    throw new Error(`BLOCKED: approved artifact sha256 changed: ${relativePath}`);
  }
  return bytes;
}

const manifest = JSON.parse(readFileSync(artifact('manifest.json'), 'utf-8')) as ApprovedManifest;
const packet: SliceContract = JSON.parse(readFileSync(artifact(`slices/${sliceId}/contract.json`), 'utf-8'));
if (manifest.approved_version !== packet.prototype_version ||
    !manifest.active_approval_id || manifest.slices?.[sliceId]?.status === 'cancelled') {
  throw new Error('BLOCKED: slice has no matching current approved prototype');
}
for (const path of Object.keys(packet.approved_artifact_hashes)) {
  lockedBytes(path, packet.approved_artifact_hashes);
}
// A screenshot from an unrelated page/state is not a reference even if its dimensions match.
// The approved prototype B case is the ONLY pixel baseline for that implementation case.
const approvedCases = manifest.approvals?.[manifest.active_approval_id]?.visual_cases ?? [];
for (const visualCase of packet.visual_cases) {
  const approved = approvedCases.find(item => item.case_id === visualCase.case_id);
  if (!approved ||
      approved.reference_file !== visualCase.reference_file ||
      approved.route !== visualCase.route ||
      approved.fixture !== visualCase.fixture ||
      approved.state_setup !== visualCase.state_setup ||
      approved.viewport.width !== visualCase.viewport.width ||
      approved.viewport.height !== visualCase.viewport.height) {
    throw new Error(`BLOCKED: ${visualCase.case_id} does not match the exact approved B route/reference/fixture/state/viewport`);
  }
}

/**
 * REQUIRED PROJECT INTEGRATION: implement this before running.
 * Configure stable fixture records, authenticated identity, server responses, clock,
 * feature flags, dialog/filter state, and font loading as required by state_setup.
 * Register route intercepts and storage state BEFORE page.goto. Make it deterministic.
 */
async function prepareCase(page: Page, visualCase: VisualCase, fixtureJson: unknown): Promise<void> {
  void page;
  void fixtureJson;
  throw new Error(`BLOCKED: implement app-specific prepareCase for ${visualCase.case_id} / ${visualCase.state_setup}`);
}

for (const visualCase of packet.visual_cases) {
  test(`${sliceId}: ${visualCase.case_id}`, async ({ page }) => {
    if (!/^[a-z][a-z0-9-]*$/.test(visualCase.case_id)) {
      throw new Error(`BLOCKED: unsafe case ID ${visualCase.case_id}`);
    }
    const reference = lockedBytes(visualCase.reference_file, packet.approved_artifact_hashes);
    const fixture = JSON.parse(lockedBytes(visualCase.fixture, packet.approved_artifact_hashes).toString('utf-8'));
    await page.setViewportSize(visualCase.viewport);
    await prepareCase(page, visualCase, fixture);
    await page.goto(new URL(visualCase.route, appBaseUrl).toString());
    await page.evaluate(() => document.fonts.ready);
    await page.mouse.move(-1, -1);

    // Layout guard; add app-specific overlap checks for the approved component hierarchy.
    const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    expect(horizontalOverflow, 'Unexpected horizontal page overflow').toBe(false);
    const visibleQaControls = await page.locator('[data-prototype-only]:visible, [data-qa-only]:visible').count();
    expect(visibleQaControls, 'Prototype-only controls must not appear in approved product UI').toBe(0);

    const screenshotFile = artifact(`slices/${sliceId}/evidence/${visualCase.case_id}.png`);
    mkdirSync(dirname(screenshotFile), { recursive: true });
    const actual = await page.screenshot({
      path: screenshotFile, type: 'png', animations: 'disabled', caret: 'hide',
      scale: 'css', fullPage: false,
    });
    expect(reference.length >= 24, 'Approved reference is not a valid-sized PNG').toBe(true);
    expect(reference.readUInt32BE(16), 'Approved reference viewport width mismatch').toBe(visualCase.viewport.width);
    expect(reference.readUInt32BE(20), 'Approved reference viewport height mismatch').toBe(visualCase.viewport.height);
    const comparison = await page.evaluate(async ({ referenceBase64, actualBase64 }) => {
      async function pixels(base64: string) {
        const bytes = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
        const canvas = document.createElement('canvas');
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        const context = canvas.getContext('2d', { willReadFrequently: true });
        if (!context) throw new Error('BLOCKED: cannot decode PNG for pixel comparison');
        context.drawImage(bitmap, 0, 0);
        return { width: bitmap.width, height: bitmap.height, data: context.getImageData(0, 0, bitmap.width, bitmap.height).data };
      }
      const expected = await pixels(referenceBase64);
      const observed = await pixels(actualBase64);
      if (expected.width !== observed.width || expected.height !== observed.height) {
        return { differentPixels: -1, width: observed.width, height: observed.height };
      }
      let differentPixels = 0;
      for (let i = 0; i < expected.data.length; i += 4) {
        if (expected.data[i] !== observed.data[i] ||
            expected.data[i + 1] !== observed.data[i + 1] ||
            expected.data[i + 2] !== observed.data[i + 2] ||
            expected.data[i + 3] !== observed.data[i + 3]) differentPixels++;
      }
      return { differentPixels, width: observed.width, height: observed.height };
    }, { referenceBase64: reference.toString('base64'), actualBase64: actual.toString('base64') });
    // If the count is nonzero, retain the original failed screenshot, diagnose the
    // correct comparison pair and capture three additional clean browser contexts.
    // Do not blindly use Playwright's `retries` setting: independent fresh contexts,
    // saved retry PNGs, changed-pixel counts and a human review are mandatory before
    // the coordinator may classify an isolated capture-only WARNING. See visual-qa.md.
    expect(comparison.differentPixels,
      `Visual mismatch (${comparison.differentPixels} pixels) in ${visualCase.case_id}; inspect ${screenshotFile} against ${visualCase.reference_file}`
    ).toBe(0);
  });
}
