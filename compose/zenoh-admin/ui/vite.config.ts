import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { TanStackRouterVite } from '@tanstack/router-plugin/vite'
import path from 'path'
import { execSync } from 'child_process'

function gitVersion(): string {
  if (process.env.APP_VERSION) return process.env.APP_VERSION
  try {
    return execSync('git describe --tags --always', { cwd: __dirname, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return 'unknown'
  }
}

const devApiPort = Number(process.env.ZENOH_ADMIN_DEV_API_PORT || 8895)

export default defineConfig({
  plugins: [
    TanStackRouterVite({ routesDirectory: './src/routes', generatedRouteTree: './src/routeTree.gen.ts' }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  define: {
    __APP_VERSION__: JSON.stringify(gitVersion()),
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': `http://127.0.0.1:${devApiPort}`,
      '/auth': `http://127.0.0.1:${devApiPort}`,
    },
  },
  build: {
    outDir: 'dist',
  },
})
