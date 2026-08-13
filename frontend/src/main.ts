import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'

import App from '@/App.vue'
import { setUsername } from '@/api/client'
import router from '@/router'

// Placeholder identity until real login exists; overridable for local testing.
setUsername(localStorage.getItem('username') ?? 'admin')

createApp(App).use(createPinia()).use(router).use(ElementPlus).mount('#app')