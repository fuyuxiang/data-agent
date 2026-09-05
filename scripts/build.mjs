import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const frontend = resolve(root, 'frontend');
const output = resolve(frontend, 'dist');

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(resolve(frontend, 'src'), resolve(output, 'src'), { recursive: true });
await cp(resolve(frontend, 'vendor'), resolve(output, 'vendor'), { recursive: true });
let html = await readFile(resolve(frontend, 'index.html'), 'utf8');
html = html.replaceAll('href="/src/', 'href="./src/').replaceAll('src="/src/', 'src="./src/').replaceAll('src="/vendor/', 'src="./vendor/');
await writeFile(resolve(output, 'index.html'), html);
console.log(`Built static frontend at ${output}`);

