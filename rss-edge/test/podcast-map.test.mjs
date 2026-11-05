// tg-tools builds the Telegram links for its podcasts from its own half of the mapping
// (shared/tg_tools_common/podcast_urls.py). A host or podcast changed here only would give links that 404:
// this fails instead. tg-tools runs the same check against src/podcasts.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PODCASTS, STORED_HOSTS } from '../src/podcasts.js';

// On OCI this repo is ~/workbench/project-folder/rss-edge and tg-tools ~/tools/tg-tools; in the public tg-tools
// mirror this repo is its rss-edge/ folder. Skipped only where neither exists.
const candidates = [process.env.TG_TOOLS_REPO, fileURLToPath(new URL('../../../../tools/tg-tools', import.meta.url)),
  fileURLToPath(new URL('../..', import.meta.url))].filter(Boolean);
const repo = candidates.find((path) => existsSync(join(path, 'shared/tg_tools_common/podcast_urls.py')));

test('tg-tools maps the same hosts and podcast names as the Worker', { skip: repo ? false : `no tg-tools checkout (tried ${candidates.join(', ')}; set TG_TOOLS_REPO)` }, () => {
  const out = execFileSync('python3', ['-I', '-c', [
    'import json, sys', `sys.path.insert(0, ${JSON.stringify(join(repo, 'shared'))})`,
    'from tg_tools_common.podcast_urls import STORAGE_HOSTS, PODCASTS',
    'print(json.dumps({"hosts": sorted(STORAGE_HOSTS), "podcasts": PODCASTS}))'].join('\n')], { encoding: 'utf8', timeout: 30000 });
  const tg = JSON.parse(out);
  assert.deepEqual(tg.hosts, [...STORED_HOSTS].sort());
  for (const [prefix, name] of Object.entries(tg.podcasts)) {
    assert.equal(PODCASTS.find((podcast) => podcast.prefix === prefix)?.name, name, `tg-tools ${prefix} -> ${name}`);
  }
});
