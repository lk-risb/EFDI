import {create} from 'zustand'
import {apiFetch, apiJson} from '@/lib/api'

export interface BrandingFields {
  org_name: string
  accent_fill: string
  accent_fill_hover: string
  accent_text: string
  accent_ring: string
}

interface BrandingState {
  orgName: string
  accentFill: string
  accentFillHover: string
  accentText: string
  accentRing: string
  logoUrl: string | null
  loaded: boolean
  fetchBranding: () => Promise<void>
  updateBranding: (fields: Partial<BrandingFields>) => Promise<void>
  uploadLogo: (file: File) => Promise<void>
}

// Same signal-blue accent TAK's branding.ts already ships (its own port of
// Scout's blue-600/500) — matched exactly rather than independently
// converting oklch, so the two consoles' unbranded defaults agree. This is
// what renders before /api/branding resolves, and what it falls back to if
// that request fails, so it must match the backend default (branding.py) or
// an unbranded/offline load shows stale gray.
const DEFAULTS = {
  orgName: 'EFDI Zenoh Console',
  accentFill: '#2563eb',
  accentFillHover: '#3b82f6',
  accentText: '#ffffff',
  accentRing: '#2563eb',
  logoUrl: null as string | null,
}

function applyAccentVars(accentFill: string, accentFillHover: string, accentText: string, accentRing: string) {
  document.documentElement.style.setProperty('--brand-accent-fill', accentFill)
  document.documentElement.style.setProperty('--brand-accent-fill-hover', accentFillHover)
  document.documentElement.style.setProperty('--brand-accent-text', accentText)
  document.documentElement.style.setProperty('--brand-accent-ring', accentRing)
}

export const useBranding = create<BrandingState>()((set, get) => ({
  ...DEFAULTS,
  loaded: false,
  updateBranding: async (fields) => {
    await apiJson('/api/branding', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(fields),
    })
    await get().fetchBranding()
  },
  uploadLogo: async (file) => {
    const form = new FormData()
    form.append('file', file)
    await apiFetch('/api/branding/logo', { method: 'POST', body: form })
    await get().fetchBranding()
  },
  fetchBranding: async () => {
    try {
      const data = await apiJson<{
        org_name: string
        accent_fill: string
        accent_fill_hover: string
        accent_text: string
        accent_ring: string
        logo_url: string | null
      }>('/api/branding')
      applyAccentVars(data.accent_fill, data.accent_fill_hover, data.accent_text, data.accent_ring)
      set({
        orgName: data.org_name,
        accentFill: data.accent_fill,
        accentFillHover: data.accent_fill_hover,
        accentText: data.accent_text,
        accentRing: data.accent_ring,
        logoUrl: data.logo_url,
        loaded: true,
      })
    } catch {
      applyAccentVars(DEFAULTS.accentFill, DEFAULTS.accentFillHover, DEFAULTS.accentText, DEFAULTS.accentRing)
      set({ loaded: true })
    }
  },
}))
