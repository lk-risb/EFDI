interface BrandConfig {
  orgName: string
  accentFill: string
  accentFillHover: string
  accentText: string
  accentRing: string
}

const BRANDS: Record<string, BrandConfig> = {
  default: {
    orgName: 'Zenoh Admin',
    accentFill: '#d4d4d8',
    accentFillHover: '#e4e4e7',
    accentText: '#18181b',
    accentRing: '#a1a1aa',
  },
}

export const brand: BrandConfig = BRANDS[import.meta.env.VITE_BRAND ?? 'default'] ?? BRANDS.default

export function applyBrand() {
  const root = document.documentElement.style
  root.setProperty('--brand-accent-fill', brand.accentFill)
  root.setProperty('--brand-accent-fill-hover', brand.accentFillHover)
  root.setProperty('--brand-accent-text', brand.accentText)
  root.setProperty('--brand-accent-ring', brand.accentRing)
}
