# MediaGrab logo

Two trim brackets pinch a play triangle. Gabarito ExtraBold wordmark, converted to outlines.

| File | Use |
|---|---|
| `mediagrab-lockup.svg`, `mediagrab-lockup-on-dark.svg` | Mark plus wordmark. Light and dark grounds. |
| `mediagrab-lockup@2x.png`, `mediagrab-lockup-on-dark@2x.png` | PNG exports of the lockups (1280 px wide). |
| `mediagrab-mark.svg`, `mediagrab-mark-on-dark.svg` | Mark only, full color. |
| `mediagrab-mark-mono-black.svg`, `mediagrab-mark-mono-white.svg` | One-color versions. |
| `mediagrab-wordmark.svg` | Wordmark only, outlined. |
| `mediagrab-icon.svg`, `icon-192.png`, `icon-512.png` | App icon (purpose: any). |
| `mediagrab-icon-maskable.svg`, `icon-maskable-512.png` | App icon (purpose: maskable). |
| `apple-touch-icon-180.png` | iOS home screen icon. |
| `favicon.svg`, `favicon-16.png`, `favicon-32.png`, `favicon-48.png`, `favicon.ico` | Browser tab. |

## Colors

- Ink on light: `#13171B`. Ink on dark: `#EAF0F6`.
- Marigold: `#FFC93C`.

## Clear space and size

Keep the height of the play triangle (about 30% of the mark height) free on every side. Minimum: lockup 120 px wide, mark 24 px. Below 24 px use the favicon version.

## PWA

```json
{
  "name": "MediaGrab",
  "short_name": "MediaGrab",
  "display": "standalone",
  "background_color": "#F5F7F8",
  "theme_color": "#F5F7F8",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
```

```html
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon-180.png">
<meta name="theme-color" content="#F5F7F8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#101722" media="(prefers-color-scheme: dark)">
```
