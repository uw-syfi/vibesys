/** Generate an uncommitted import list; browsers cannot enumerate modules. */
import {readdirSync, writeFileSync} from 'node:fs';

const generated = new URL('./src/generated/vibesys/', import.meta.url);
const imports = [];
const names = [];
for (const pkg of ['api', 'store']) {
  const directory = new URL(`${pkg}/v1/`, generated);
  for (const file of readdirSync(directory)
    .filter(name => name.endsWith('_pb.ts'))
    .sort()) {
    const stem = file.slice(0, -6);
    const name = `file_vibesys_${pkg}_v1_${stem}`;
    imports.push(`import {${name}} from './vibesys/${pkg}/v1/${stem}_pb.js';`);
    names.push(name);
  }
}
writeFileSync(
  new URL('./src/generated/schema-files.ts', import.meta.url),
  `// Generated locally by generate-schema-files.mjs; do not commit.
${imports.join('\n')}
export const schemaFiles = [${names.join(', ')}];
`,
);
