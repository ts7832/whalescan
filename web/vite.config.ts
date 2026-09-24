/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// base './' makes the build work under https://<user>.github.io/whalescan/
export default defineConfig({ base: './', plugins: [react()], test: { environment: 'node' } });
