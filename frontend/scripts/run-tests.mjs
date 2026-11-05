import {spawnSync} from 'node:child_process';
import {readdirSync} from 'node:fs';
const files=readdirSync(new URL('../tests/',import.meta.url)).filter(name=>name.endsWith('.test.mjs')).map(name=>'tests/'+name);
const result=spawnSync(process.execPath,['--experimental-strip-types','--test',...files],{stdio:'inherit'});
process.exit(result.status??1);
