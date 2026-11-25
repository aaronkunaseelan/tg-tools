// bin/deploy: an archived tree of an exact commit, the token read inside the script, and
// nothing printed that could be a secret. The fake worker-deps.py lives under a fake HOME,
// so `--project <tmpdir>` and the minimal child environment are observed directly.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const FAKE_TOKEN = 'fake-cloudflare-token-0123456789';

// Stands in for the host-owned worker-deps.py: records what it was called with, exits 0.
const STUB = `#!/usr/bin/env python3
import os, sys
print('stub: argv=' + ' '.join(sys.argv[1:]))
print('stub: cwd=' + os.getcwd())
# LC_CTYPE is set by the interpreter's own locale coercion (PEP 538), not by the caller.
print('stub: env=' + ','.join(sorted(k for k in os.environ if k != 'LC_CTYPE')))
sys.exit(0)
`;

function git(cwd, ...args) {
  return execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' });
}

const roots = [];
after(() => { for (const root of roots) rmSync(root, { recursive: true, force: true }); });

function sandbox() {
  const root = mkdtempSync(path.join(tmpdir(), 'rss-edge-deploy-test-'));
  roots.push(root);
  const clone = path.join(root, 'clone');
  execFileSync('git', ['clone', '--quiet', repo, clone], { encoding: 'utf8' });

  const home = path.join(root, 'home');
  const scripts = path.join(home, 'workbench', 'scripts');
  mkdirSync(scripts, { recursive: true });
  const stub = path.join(scripts, 'worker-deps.py');
  writeFileSync(stub, STUB);
  chmodSync(stub, 0o755);

  return {
    clone,
    home,
    credentials: path.join(root, 'credentials.env'),
    secrets: path.join(root, 'secrets.env'),
    deploy: path.join(clone, 'bin', 'deploy'),
  };
}

// No TMPDIR or other inherited variable: the child environment is the script's own.
function run(box, args) {
  return spawnSync('python3', [box.deploy, ...args], {
    cwd: box.clone,
    encoding: 'utf8',
    env: {
      PATH: process.env.PATH,
      HOME: box.home,
      RSS_EDGE_CREDENTIALS: box.credentials,
      RSS_EDGE_SECRETS: box.secrets,
    },
  });
}

const output = (result) => result.stdout + result.stderr;

test('bin/deploy --dry-run HEAD builds the archived tree of the checked-out commit', () => {
  const box = sandbox();
  writeFileSync(box.credentials, `CLOUDFLARE_API_TOKEN=${FAKE_TOKEN}\n`);
  const head = git(box.clone, 'rev-parse', 'HEAD').trim();

  const result = run(box, ['--dry-run', 'HEAD']);
  assert.equal(result.status, 0, output(result));
  assert.match(result.stderr, new RegExp(`build of ${head}`));
  // worker-deps gets the temp tree as --project, build (not deploy), and no secrets file.
  assert.match(result.stdout, /stub: argv=--project \S+ build\n/);
  assert.doesNotMatch(result.stdout, /--secrets-file| deploy/);
  // A dry run does not read the token, and the child environment is PATH and HOME only.
  assert.match(result.stdout, /stub: env=HOME,PATH\n/);
  // The extraction is in the --project directory, and gone once the script exits.
  const tree = result.stdout.match(/stub: argv=--project (\S+) build/)[1];
  assert.match(result.stdout, new RegExp(`stub: cwd=${tree}\n`));
  assert.equal(existsSync(tree), false, `${tree} was not removed`);
  assert.ok(!output(result).includes(FAKE_TOKEN));
});

test('a commit that is not on the checked-out line is refused with exit 2', () => {
  const box = sandbox();
  // An orphan commit (same tree, no parent) is never an ancestor of HEAD.
  const tree = git(box.clone, 'rev-parse', 'HEAD^{tree}').trim();
  const orphan = execFileSync('git', ['-C', box.clone, 'commit-tree', tree, '-m', 'other line'], {
    encoding: 'utf8',
    env: {
      ...process.env,
      GIT_AUTHOR_NAME: 'test', GIT_AUTHOR_EMAIL: 'test@example.invalid',
      GIT_COMMITTER_NAME: 'test', GIT_COMMITTER_EMAIL: 'test@example.invalid',
    },
  }).trim();
  assert.notEqual(orphan, git(box.clone, 'rev-parse', 'HEAD').trim());

  for (const args of [[orphan], ['--dry-run', orphan], ['0000000000000000000000000000000000000000'], ['nope']]) {
    const result = run(box, args);
    assert.equal(result.status, 2, `${args.join(' ')}: ${output(result)}`);
    assert.match(result.stderr, /bin\/deploy: /);
    assert.doesNotMatch(result.stdout, /stub:/, 'worker-deps must not run');
  }
});

test('a missing argument or an unknown flag exits 2', () => {
  const box = sandbox();
  for (const args of [[], ['--strict'], ['--dry-run'], ['HEAD', 'extra']]) {
    const result = run(box, args);
    assert.equal(result.status, 2, `${JSON.stringify(args)}: ${output(result)}`);
    assert.match(result.stderr, /usage: bin\/deploy \[--dry-run\] <sha>/);
    assert.doesNotMatch(result.stdout, /stub:/);
  }
});

test('the real token is passed to worker-deps but never printed', () => {
  const box = sandbox();
  writeFileSync(box.credentials, `# agent secrets\nSOME_OTHER_KEY=unrelated-secret-value\nCLOUDFLARE_API_TOKEN=${FAKE_TOKEN}\n`);
  writeFileSync(box.secrets, 'FEED_KEYS=episodes=aaaaaaaaaaaaaaaaaaaaaa\n');

  const result = run(box, ['HEAD']);
  assert.equal(result.status, 0, output(result));
  assert.match(result.stdout, new RegExp(`stub: argv=--project \\S+ deploy --secrets-file ${box.secrets}\n`));
  // PATH, HOME and the token: the token and nothing else from the credentials file.
  assert.match(result.stdout, /stub: env=CLOUDFLARE_API_TOKEN,HOME,PATH\n/);
  assert.ok(!output(result).includes(FAKE_TOKEN), 'the token was printed');
  assert.ok(!output(result).includes('unrelated-secret-value'), 'an unrelated credential was read');
});

test('a missing key or file is reported by name, with nothing of its content', () => {
  const box = sandbox();
  writeFileSync(box.secrets, 'FEED_KEYS=episodes=aaaaaaaaaaaaaaaaaaaaaa\n');

  const absentKey = sandbox();
  writeFileSync(absentKey.credentials, 'SOME_OTHER_KEY=unrelated-secret-value\n');
  writeFileSync(absentKey.secrets, 'FEED_KEYS=episodes=aaaaaaaaaaaaaaaaaaaaaa\n');
  const noKey = run(absentKey, ['HEAD']);
  assert.equal(noKey.status, 1, output(noKey));
  assert.match(noKey.stderr, /CLOUDFLARE_API_TOKEN is not set in .*credentials\.env/);
  assert.ok(!output(noKey).includes('unrelated-secret-value'));
  assert.doesNotMatch(noKey.stdout, /stub:/);

  writeFileSync(box.credentials, `CLOUDFLARE_API_TOKEN=${FAKE_TOKEN}\n`);
  const noCredentials = run({ ...box, credentials: path.join(box.credentials, 'absent.env') }, ['HEAD']);
  assert.equal(noCredentials.status, 1, output(noCredentials));
  assert.match(noCredentials.stderr, /cannot read .*absent\.env/);
  assert.ok(!output(noCredentials).includes(FAKE_TOKEN));
  assert.doesNotMatch(noCredentials.stdout, /stub:/);

  const noSecrets = run({ ...box, secrets: path.join(box.secrets, 'absent.env') }, ['HEAD']);
  assert.equal(noSecrets.status, 1, output(noSecrets));
  assert.match(noSecrets.stderr, /absent\.env is missing/);
  assert.ok(!output(noSecrets).includes(FAKE_TOKEN));
  assert.doesNotMatch(noSecrets.stdout, /stub:/);
});
