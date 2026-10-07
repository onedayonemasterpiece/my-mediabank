import { readFile, mkdir, cp } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { installBrowserAssets } from '@onedayonemasterpiece/live-interaction/assets';

const root = resolve(import.meta.dirname, '..');
const manifest = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
const framework = JSON.parse(await readFile(resolve(root, 'node_modules/@onedayonemasterpiece/live-interaction/package.json'), 'utf8'));
const receipt = JSON.parse(await readFile(resolve(root, 'vendor/live-interaction-0.3.27.json'), 'utf8'));
const archive = await readFile(resolve(root, manifest.liveFramework.archive));
if (framework.version !== manifest.liveFramework.version ||
    createHash('sha256').update(archive).digest('hex') !== receipt.sha256 ||
    receipt.source_commit !== manifest.liveFramework.sourceCommit) {
  throw new Error('Live framework version/archive integrity does not match the pinned receipt');
}
await mkdir(resolve(root, 'dist'), {recursive: true});
await cp(resolve(root, 'web'), resolve(root, 'dist'), {recursive: true});
await installBrowserAssets(resolve(root, 'dist/live'));
console.log(JSON.stringify({web: 'built', framework: framework.version, prompt: 'postcard-2026-10-07-v1'}));
