import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/nyxboard/static/js/tests/setup.js'],
  },
  resolve: {
    alias: {
      '@components': resolve(__dirname, './src/nyxboard/static/js/components'),
      '@tests': resolve(__dirname, './src/nyxboard/static/js/tests')
    }
  }
});
