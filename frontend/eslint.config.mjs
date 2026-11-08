import { FlatCompat } from '@eslint/eslintrc';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });
const rules = [{ ignores: ['.next/**', '.next-dev/**', '.next-flower-qa/**', 'node_modules/**', 'next-env.d.ts'] }, ...compat.extends('next/core-web-vitals', 'next/typescript')];
export default rules;
