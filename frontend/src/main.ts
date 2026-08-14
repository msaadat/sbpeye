import { createApp } from 'vue'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import Aura from '@primeuix/themes/aura'
import { definePreset } from '@primeuix/themes'
import router from './router'
import App from './App.vue'
import 'primeicons/primeicons.css'
import './styles.css'
import './premium.css'

/*
 * PrimeVue emits its design tokens into a `:root, :host` block that it injects
 * at app-init — after our stylesheets. A plain `:root { --p-primary-600: … }`
 * in styles.css therefore loses on source order and silently does nothing,
 * which is how the app ended up running on Aura's stock emerald while every
 * hand-written rule used the SBP green. Overriding through the preset is the
 * only place these actually take.
 *
 * The values point at the --sbp-* custom properties so the palette, radius and
 * type scale have a single definition (styles.css) and PrimeVue follows it —
 * including the light/dark swap, which var() resolves at computed-value time.
 */
const SBPEyePreset = definePreset(Aura, {
  primitive: {
    borderRadius: {
      none: '0',
      xs: 'var(--sbp-radius-sm)',
      sm: 'var(--sbp-radius-sm)',
      md: 'var(--sbp-radius)',
      lg: 'var(--sbp-radius)',
      xl: 'var(--sbp-radius-lg)',
    },
    // The severity ramps. Aura's stock red/green/amber are a different family
    // from the --sbp-success/-warning/-danger tokens the hand-written rules use,
    // so a PrimeVue danger button sat next to a .status-chip.status-danger in
    // two different reds. Each ramp is anchored so that step 600 is the light
    // token and step 400 is the dark one — the steps Aura picks per scheme.
    red: {
      50: '#fdf2f1', 100: '#fbe1df', 200: '#f7c8c3', 300: '#f4a9a1',
      400: '#f08a80', 500: '#cf5348', 600: '#b3261e', 700: '#941d16',
      800: '#781813', 900: '#631714', 950: '#360a07',
    },
    green: {
      50: '#f0faf4', 100: '#d8f2e3', 200: '#b1e5c8', 300: '#84d3a8',
      400: '#56c48c', 500: '#2c9d67', 600: '#167a4a', 700: '#12603b',
      800: '#104d31', 900: '#0e3f29', 950: '#052316',
    },
    amber: {
      50: '#fdf7ec', 100: '#faecce', 200: '#f4d79c', 300: '#ebbf63',
      400: '#e0a336', 500: '#c4851a', 600: '#a76a09', 700: '#855107',
      800: '#6c4108', 900: '#5a370b', 950: '#331d03',
    },
  },
  semantic: {
    primary: {
      50: '#edf8f3',
      100: '#d4efe4',
      200: '#aee0cc',
      300: '#80cbb0',
      400: '#54ae91',
      500: '#2f8e70',
      600: '#156f52',
      700: '#105941',
      800: '#0d4735',
      900: '#0a382a',
      950: '#051f18',
    },
    formField: {
      borderRadius: 'var(--sbp-radius)',
      sm: { fontSize: 'var(--sbp-fs-sm)' },
    },
    colorScheme: {
      light: {
        primary: {
          color: '{primary.600}',
          contrastColor: '#ffffff',
          hoverColor: '{primary.700}',
          activeColor: '{primary.800}',
        },
      },
      dark: {
        primary: {
          // The 600 step is too dark to read on the dark surfaces; 400 is the
          // same hue at the lightness --sbp-green-text uses.
          color: '{primary.400}',
          contrastColor: '#051f18',
          hoverColor: '{primary.300}',
          activeColor: '{primary.200}',
        },
      },
    },
  },
  components: {
    button: {
      borderRadius: 'var(--sbp-radius)',
      sm: { fontSize: 'var(--sbp-fs-sm)' },
    },
    tag: {
      borderRadius: 'var(--sbp-radius-pill)',
      fontSize: 'var(--sbp-fs-eyebrow)',
    },
    toast: {
      summary: { fontSize: 'var(--sbp-fs-body)' },
      detail: { fontSize: 'var(--sbp-fs-sm)' },
    },
  },
})

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ToastService)
app.use(ConfirmationService)
app.use(PrimeVue, {
  theme: {
    preset: SBPEyePreset,
    options: {
      darkModeSelector: '.sbpeye-dark',
    },
  },
})

app.mount('#app')
