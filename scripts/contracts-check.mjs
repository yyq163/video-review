import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = process.cwd();
const generate = process.argv.includes('--generate');
const python = existsSync(join(root, 'backend/.venv/bin/python')) ? join(root, 'backend/.venv/bin/python') : 'python3';
const generatorArgs = ['backend/scripts/generate_contracts.py', ...(generate ? [] : ['--check'])];
const generator = spawnSync(python, generatorArgs, { cwd: root, encoding: 'utf8' });
if (generator.stdout) process.stdout.write(generator.stdout);
if (generator.stderr) process.stderr.write(generator.stderr);
if (generator.status !== 0) process.exit(generator.status ?? 1);

const generatedContractDirectory = 'src/modules/final-cut-review/contracts-generated';
const generatedContractFiles = readdirSync(join(root, generatedContractDirectory))
  .filter((file) => file.startsWith('backend-contract') && file.endsWith('.ts'))
  .sort()
  .map((file) => join(generatedContractDirectory, file));

const files = [
  'src/modules/final-cut-review/contracts/types.ts',
  ...generatedContractFiles,
  'src/modules/final-cut-review/entry/capabilities.ts',
  'src/modules/final-cut-review/ports/index.ts',
];

const legacyCapabilities = [
  'project:create',
  'project:update',
  'item:create',
  'item:update',
  'version:append',
  'issue:create',
  'issue:reply',
  'issue:resolve',
  'issue:reopen',
  'review:request_changes',
  'review:finalize',
  'download:finalized_original',
  'package:create',
];

const requiredSymbols = [
  'ReviewPlaybackTarget',
  'ReviewIssueRevision',
  'ReviewAnnotationSet',
  'OriginalMediaSnapshot',
  'FinalCutPackageSnapshot',
  'ExecutionContext',
];

const requiredBackendSymbols = [
  'FinalCutReviewClient',
  'EDIT_ENTRY_PROFILE',
  'REVIEW_ENTRY_PROFILE',
  'DomainEventEnvelope',
  'ReviewIssueDTO',
  'PackageSnapshotDTO',
  'UploadInitRequest',
];

let failed = false;
if (existsSync(join(root, 'src/modules/final-cut-review/contracts-generated/types.ts'))) {
  process.stderr.write('frontend domain types must not live under contracts-generated/types.ts\n');
  failed = true;
}

for (const file of files) {
  const text = readFileSync(join(root, file), 'utf8');
  for (const legacy of legacyCapabilities) {
    if (text.includes(`'${legacy}'`) || text.includes(`"${legacy}"`)) {
      process.stderr.write(`legacy capability found in ${file}: ${legacy}\n`);
      failed = true;
    }
  }
}

const typesText = readFileSync(join(root, 'src/modules/final-cut-review/contracts/types.ts'), 'utf8');
for (const symbol of requiredSymbols) {
  if (!typesText.includes(symbol)) {
    process.stderr.write(`required contract symbol missing: ${symbol}\n`);
    failed = true;
  }
}
const backendContractText = generatedContractFiles
  .map((file) => readFileSync(join(root, file), 'utf8'))
  .join('\n');
for (const symbol of requiredBackendSymbols) {
  if (!backendContractText.includes(symbol)) {
    process.stderr.write(`required backend contract symbol missing: ${symbol}\n`);
    failed = true;
  }
}

if (failed) {
  process.exit(1);
}

process.stdout.write(generate ? 'contracts:generate passed\n' : 'contracts:check passed\n');
