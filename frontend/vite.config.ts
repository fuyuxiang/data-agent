import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [ElementPlusResolver()],
      dts: 'src/components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 50803,
    proxy: {
      '/api': {
        target: 'http://localhost:50805',
        changeOrigin: true,
        logLevel: 'debug',
        onProxyReq: (proxyReq, req, res) => {
          console.log('[Proxy] 请求:', req.method, req.url)
        },
        onProxyRes: (proxyRes, req, res) => {
          console.log('[Proxy] 响应:', req.method, req.url, proxyRes.statusCode)
        },
        onError: (err, req, res) => {
          console.error('[Proxy] 错误:', err)
        }
      },
    },
  },
})
