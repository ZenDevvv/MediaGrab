# MediaGrab Design System

A small, flat, friendly system for a self-hosted media utility that ships as an installable web app (PWA) first. Light and dark are equal citizens. One accent, no gradients, one hard shadow.

## 1. Principles

1. **One job per screen.** Paste, choose, wait, save. Everything else is a step that opens from a row.
2. **One bold thing.** The marigold accent and the binder-clip mark carry the personality. Everything around them stays quiet.
3. **Flat, with one exception.** No soft drop shadows, no glass, no glow. The primary button alone has a hard offset shadow.
4. **Show the real numbers.** Sizes, speeds, bitrates, timestamps and file names are content. Set them in the mono face.
5. **State is visible in form.** Selection, progress and errors change border, fill or shape, not only color.
6. **Phone first, wide second.** One column is the base. Wide screens re-compose the same components into two columns.

### Anti-slop checklist

Do not use: purple or indigo gradients, glassmorphism, glowing badges, Inter as the main face, emoji as icons, a centered hero over three feature cards, the same radius and shadow on everything, accent bars on rounded cards.

## 2. Color

All colors are tokens. Components never use literal hex values.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--board` | `#DCE2E7` | `#080C12` | Page behind the app (design board only) |
| `--bg` | `#F5F7F8` | `#101722` | App background |
| `--card` | `#FFFFFF` | `#172131` | Inputs, cards, panels |
| `--ink` | `#13171B` | `#EAF0F6` | Primary text |
| `--muted` | `#566069` | `#9AA8B8` | Secondary text, labels |
| `--line` | `#CBD3DA` | `#2A384C` | Hairlines, resting borders |
| `--edge` | `#13171B` | `#5A6B82` | Strong borders (inputs, secondary buttons) |
| `--soft` | `#E8ECEF` | `#1E2B3F` | Chip and tag fill, code block fill |
| `--accent` | `#FFC93C` | `#FFC93C` | The one accent: primary button, progress, selection fill |
| `--accent-ink` | `#13171B` | `#13171B` | Text on accent |
| `--accent-soft` | `#FFF0BF` | `#3A3218` | Tag fill, focus halo, install strip |
| `--pop` | `#13171B` | `#05080C` | Border and hard shadow on the primary button |
| `--sel` | `#13171B` | `#FFC93C` | Selected border, focus ring |
| `--ok` | `#1B7F54` | `#5FD3A0` | Success text and status dot |
| `--bad` | `#B8341F` | `#FF8F7C` | Error text and borders |

Rules:

- Accent is for action and selection only. Never use it for body text.
- Semantic colors (`--ok`, `--bad`) are separate from the accent.
- Neutrals lean slightly blue. Do not substitute pure gray.
- Dark mode is a redesign of the same tokens, not an inversion. The accent stays the same hue in both.
- Check text and control contrast yourself before shipping new color pairs.

### Theme switching

Three states: system (default), light, dark.

```css
:root { /* light values, every token */ }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { /* dark values */ color-scheme: dark; }
}
:root[data-theme="dark"] { /* same dark values */ color-scheme: dark; }
body { background: var(--bg); color: var(--ink); }
```

Persist the user's explicit choice (`light` or `dark`) in `localStorage`. "System" removes the attribute.

## 3. Typography

| Role | Family | Weights | Used for |
|---|---|---|---|
| Display | Gabarito | 500, 600, 700, 800 | Wordmark, headings, big numbers, quality labels |
| Body | Onest | 400, 500, 600 | Paragraphs, buttons, labels |
| Mono | DM Mono | 400, 500 | Sizes, speeds, times, bitrates, file names, small caps labels |

Fallbacks: display `ui-rounded, system-ui, sans-serif`; body `system-ui, sans-serif`; mono `ui-monospace, Menlo, monospace`.

Google Fonts link:

```
https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Gabarito:wght@500;600;700;800&family=Onest:wght@400;500;600&display=swap
```

### Scale

| Style | Size / line | Face | Weight |
|---|---|---|---|
| Hero (desktop) | 68 / 0.98 | Display | 800 |
| Hero (phone) | 40 / 1.02 | Display | 800 |
| Big number (progress) | 64 phone, 104 desktop | Display | 800 |
| Screen title | 34 to 52 / 1.05 | Display | 800 |
| Card title | 17 to 30 / 1.12 to 1.2 | Display | 700 to 800 |
| Option name | 15 to 17 | Display | 700 |
| Body | 15 / 1.5 | Body | 400 |
| Small | 13 / 1.5 | Body | 400 |
| Mono value | 12 to 14 | Mono | 400 to 500 |
| Eyebrow label | 11 to 12, uppercase, letter-spacing 0.06 to 0.08em | Mono | 500 |

Rules: headings use `text-wrap: balance`. Running text stays near 65 characters wide. Digits that line up use `font-variant-numeric: tabular-nums`.

## 4. Space, shape, borders

- **Base unit:** 4px. Common steps: 4, 6, 8, 10, 12, 14, 16, 20, 24, 32, 44.
- **Screen padding:** 16px at phone width, 20px in the app frame, 40px on desktop.
- **Gaps:** use flex or grid `gap`, not margins, between siblings.
- **Radii:** pill (`999px`) for buttons, inputs, segmented controls, tags. 20 to 22px for app frames and side panels. 14 to 16px for cards, chips, rows. 12px for thumbnails. 7px for the trim handles.
- **Borders:** 1.5px everywhere. `--line` at rest, `--edge` on inputs and secondary buttons, `--sel` when selected.
- **Shadows:** none, except the primary button (below).

## 5. Components

### Primary button
Accent fill, `--accent-ink` text, 1.5px `--pop` border, hard shadow `3px 3px 0 var(--pop)`. Height 48px (38px small), pill, 600 weight.
- Hover: move `1px 1px`, shadow `2px 2px`.
- Active: move `3px 3px`, shadow removed.
- Use once per screen.

### Secondary and quiet buttons
Secondary: `--card` fill, 1.5px `--edge` border. Quiet: no border, `--muted` text, `--ink` on hover. Used for Cancel, Not now, Grab another link.

### Link field
Pill, `--card` fill, 1.5px `--edge` border, the Grab button sits inside on the right. Focus: border `--sel` plus a 3px `--accent-soft` halo. Error: border `--bad`, wavy underline under the bad text, message below in `--bad`. Large variant on desktop (52px controls).

### Segmented format switch
Two options in a `--soft` pill. Selected option gets `--card` fill and a 1.5px `--sel` ring. Labels: "Video MP4" and "Audio MP3".

### Quality chip (radio)
Three-column grid. Shows resolution (display face), size (mono) and a tag. Selected: border and 1.5px ring in `--sel`.
- Tag `remux` (accent-soft): native MP4 stream, copied in seconds. Used for 1080p and below.
- Tag `encode` (soft): converted first, takes longer. Used for 2160p and 1440p.

### Option row (radio)
Dot, name with mono detail, description line, mono size on the right. Selected dot fills with accent and an inner ring. Used for MP3 tiers and subtitle language. The recommended tier carries an accent "Recommended" pill.

### Source bitrate scale
A thin line from 0 to 320 kbps with ticks at 128, 192 and 320. An accent dot marks the detected source (about 129 kbps) with a "source" label.

### Setting row
36px soft icon tile, bold name, mono current value, right chevron. Opens a step. Examples: Trim "00:15 to 01:30 · 1:15", Subtitles "English · embedded in the MP4".

### Trim timeline
A strip of vertical bars in a card. Bars inside the selection use `--ink`, outside use `--line`. Two accent handles (14px wide, full height, pill ends) mark start and end. Below: mm:ss Start and End fields, a live duration (display face, large) and "of 4:12", plus a plain-language saving ("about 70% less").
- Handles never cross. Minimum clip is 1 second.
- Invalid typed times revert and show an inline error: "Use mm:ss. The end has to come after the start and fit inside 4:12."
- Sizes everywhere recalculate from the clip length.

### Switch
48 x 28 pill. On: accent fill, `--pop` border. Used for "Embed in the MP4". Always labelled; the label states the result.

### Progress
Large percentage (display face), a 16px pill bar with accent fill and a hard right edge, the clip mark riding the head of the bar, three stat tiles (Speed, Left, Got) in mono, then a stage list. Stage states: done (accent circle with check), now (ringed dot), todo (muted, hollow). Stages follow the real pipeline: Read the link, Downloading, Joining into an MP4, Embedding captions, Ready.

### File card
Icon, file name in mono (wraps anywhere), then a two-column spec list (Quality, Length, Size, Captions). Compact variant for extra files like the `.srt`. A countdown line shows when the file is deleted ("Deleted from this machine in 29:41").

### Status blocks
Round 56px badge plus a heading and one plain sentence.
- Success: accent fill with a check.
- Bad link: `--bad` outline with "!".
- Offline or server unreachable: soft fill with a slashed wifi icon.

### Install card (PWA)
App icon, "Install MediaGrab", one line of copy, a primary small button and a quiet "Not now". On desktop it becomes a thin accent-soft strip above the nav.

### Browser frame (design board only)
Soft title bar with three muted dots and a mono address pill. Not part of the product.

## 6. Logo and app icon

- **Mark:** a binder clip. Wire handle drawn as a 2px ink stroke with round caps. Body a trapezoid filled with `--accent`, 2px ink stroke, round joins. Designed on a 32px grid.
- **Wordmark:** "MediaGrab" in Gabarito 800, 20px, letter-spacing -0.01em, next to the mark at 28px.
- **App icon:** the clip on a marigold rounded square (corner radius about 27% of the side), 1.5px dark border. Clip body is filled with the card color so it reads on the accent. Ship 192 and 512 PNGs plus a maskable version with safe padding.
- **Theme color:** set `<meta name="theme-color">` once for light and once for dark, matching `--bg` in each theme.

## 7. Layout

### Phone (base)
Single column inside 16 to 20px padding. Header: wordmark left, theme button right. Primary action sits at the bottom of the content (pinned with `margin-top: auto`). Trim and Subtitles open as steps with a back arrow, and can be presented as bottom sheets in the real app.

### Desktop (900px and up)
Content is a browser-width page with 40px padding, a nav row (wordmark, "Your files" with count, theme button) and a two-column split.
- Paste: hero and link field left, "Your files" list right.
- Choose: preview, title and trim timeline left. Format, quality, subtitles and Grab right.
- Working: big percentage, stats and stages left. Preview and recap right.
- Ready: success, files and Save buttons left. Preview, recap and countdown right.

Columns: roughly 1.25fr / 1fr for choose and paste, 1.1fr / 1fr for working and ready, 44px gap. Below 900px they stack to one column.

### Safe areas
Keep the root padded with `env(safe-area-inset-*)`. A bottom bar adds `env(safe-area-inset-bottom)` to its own padding.

## 8. Motion

Minimal. Button press (100ms transform and shadow), switch thumb (150ms), smooth scroll between steps. All motion is disabled under `prefers-reduced-motion`. Progress updates from server-sent events change width without easing.

## 9. Copy

- Name things by what the person recognizes: "Your files", "Trim", "Subtitles", not "jobs" or "streams".
- Buttons say what happens: "Grab MP4 · 29 MB", "Save .srt", "Use this clip".
- Errors say what is wrong and what to do: "That link didn't work. There's no video or audio at this address."
- Use short, direct sentences. No exclamation marks, no emoji.
- Show privacy plainly: "No ads, no tracking. Files delete themselves after 30 minutes."

## 10. Accessibility

- Every control has a visible focus state: 2px `--sel` outline, 3px offset.
- Radios and switches are real inputs, visually styled. Selection is also shown by border weight, not color alone.
- The progress bar uses `role="progressbar"` with value and label. The percentage is an `aria-live` region.
- Error text is linked to its field with `aria-describedby`, and the field has `aria-invalid`.
- Touch targets are at least 36px, primary actions 48px.
- Support text zoom to 200% without horizontal scrolling.

## 11. Implementation

### CSS variables

```css
:root {
  --font-display: 'Gabarito', ui-rounded, system-ui, sans-serif;
  --font-body: 'Onest', system-ui, sans-serif;
  --font-mono: 'DM Mono', ui-monospace, Menlo, monospace;

  --bg: #F5F7F8;  --card: #FFFFFF;  --ink: #13171B;  --muted: #566069;
  --line: #CBD3DA;  --edge: #13171B;  --soft: #E8ECEF;
  --accent: #FFC93C;  --accent-ink: #13171B;  --accent-soft: #FFF0BF;
  --pop: #13171B;  --sel: #13171B;  --ok: #1B7F54;  --bad: #B8341F;
}
```

### Tailwind (v3) mapping

```js
// tailwind.config.js
module.exports = {
  darkMode: ['class', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)', card: 'var(--card)', ink: 'var(--ink)', muted: 'var(--muted)',
        line: 'var(--line)', edge: 'var(--edge)', soft: 'var(--soft)',
        accent: { DEFAULT: 'var(--accent)', ink: 'var(--accent-ink)', soft: 'var(--accent-soft)' },
        pop: 'var(--pop)', sel: 'var(--sel)', ok: 'var(--ok)', bad: 'var(--bad)',
      },
      fontFamily: {
        display: ['Gabarito', 'ui-rounded', 'system-ui', 'sans-serif'],
        body: ['Onest', 'system-ui', 'sans-serif'],
        mono: ['"DM Mono"', 'ui-monospace', 'monospace'],
      },
      borderWidth: { DEFAULT: '1.5px' },
      boxShadow: { pop: '3px 3px 0 var(--pop)', 'pop-sm': '2px 2px 0 var(--pop)' },
    },
  },
};
```

Because colors point at CSS variables, the `[data-theme]` and `prefers-color-scheme` blocks from section 2 do the theming. You do not need `dark:` variants for color.

### PWA checklist

- `manifest.webmanifest` with `name`, `short_name` "MediaGrab", `display: standalone`, `start_url`, 192 and 512 icons, one maskable icon.
- Two `theme-color` meta tags with `media="(prefers-color-scheme: ...)"`.
- Service worker that caches the app shell so the offline screen can render, and never caches media downloads.
- Offline and server-unreachable screens use the same status block pattern.
- Install card: listen for `beforeinstallprompt`, show the card, and hide it after install or dismissal.

## 12. Screen inventory

| Screen | Phone | Desktop |
|---|---|---|
| Paste | yes | yes |
| Choose · video | yes | yes (with trim and subtitles inline) |
| Choose · audio | yes | via the format switch |
| Trim | yes (step) | inline on Choose |
| Subtitles | yes (step) | inline on Choose |
| Working | yes | yes |
| Ready | yes | yes |
| Error · bad link | yes | reuse phone pattern, centered column |
| Error · offline | yes | reuse phone pattern, centered column |
