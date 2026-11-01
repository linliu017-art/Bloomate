import type { Metadata } from 'next';
import './style.css';
export const metadata: Metadata = { title: 'Bloomate', description: '把每一步种进花园' };
export default function RootLayout({children}: Readonly<{children: React.ReactNode}>) { return <html lang="zh-CN"><body>{children}</body></html>; }
