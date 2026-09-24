// Copies ../data/snapshot into public/data so both `vite dev` and `vite build` serve it at ./data/.
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs';

const src = new URL('../../data/snapshot/', import.meta.url);
const dst = new URL('../public/data/', import.meta.url);
rmSync(dst, { recursive: true, force: true });
mkdirSync(dst, { recursive: true });
if (existsSync(src)) {
  cpSync(src, dst, { recursive: true });
  console.log('copied data/snapshot -> web/public/data');
} else {
  console.warn('no data/snapshot yet — the dashboard will show NO SNAPSHOT DATA');
}
