import type { NextConfig } from 'next';
const backend = process.env.BLOOMATE_BACKEND_URL || 'http://127.0.0.1:8000';
const config: NextConfig = { distDir: process.env.NODE_ENV === 'development' ? (process.env.BLOOMATE_DEV_DIST_DIR || '.next-dev') : '.next', async rewrites() { return [{ source: '/api/:path*', destination: `${backend}/api/:path*` }]; } };
export default config;
