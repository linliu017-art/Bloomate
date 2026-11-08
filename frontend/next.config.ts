import type { NextConfig } from 'next';
const config: NextConfig = {
  output: 'standalone',
  distDir: process.env.NODE_ENV === 'development' ? (process.env.BLOOMATE_DEV_DIST_DIR || '.next-dev') : '.next',
};
export default config;
