import { create } from 'zustand'
import { apiJson } from '@/lib/api'

interface BrandingState {
  orgName: string
  accentFill: string
  accentFillHover: string
  accentText: string
  accentRing: string
  logoUrl: string | null
  loaded: boolean
  fetchBranding: () => Promise<void>
}

const DEFAULTS = {
  orgName: 'EFDI Zenoh Console',
  accentFill: '#d4d4d8',
  accentFillHover: '#e4e4e7',
  accentText: '#18181b',
  accentRing: '#a1a1aa',
  logoUrl: null as string | null,
}

function applyAccentVars(accentFill: string, accentFillHover: string, accentText: string, accentRing: string) {
  document.documentElement.style.setProperty('--brand-accent-fill', accentFill)
  document.documentElement.style.setProperty('--brand-accent-fill-hover', accentFillHover)
  document.documentElement.style.setProperty('--brand-accent-text', accentText)
  document.documentElement.style.setProperty('--brand-accent-ring', accentRing)
}

export const useBranding = create<BrandingState>()((set) => ({
  ...DEFAULTS,
  loaded: false,
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
