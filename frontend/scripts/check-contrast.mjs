/**
 * WCAG contrast check over the real token file.
 *
 * This parses src/styles/tokens.css rather than restating the palette, so it
 * cannot pass while the application renders something else. Every pair listed
 * in PAIRS is one the UI actually puts on screen; adding a colour combination
 * to a component without adding it here is the mistake this guards against.
 *
 *   node scripts/check-contrast.mjs          # table + exit code
 *   node scripts/check-contrast.mjs --md     # markdown, for the report
 *
 * Thresholds are WCAG 2.1 AA: 4.5:1 for body text, 3:1 for large text and for
 * meaningful non-text (status dots, focus rings, borders that carry meaning).
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const CSS = readFileSync(join(here, '..', 'src', 'styles', 'tokens.css'), 'utf8')

/* ------------------------------------------------------------------ */
/* Parsing                                                             */
/* ------------------------------------------------------------------ */

/** Custom properties declared inside the block whose selector matches. */
function block(selectorPattern) {
  const out = {}
  // Each `selector { ... }` group; selectors may be comma-separated lists.
  const re = /([^{}]+)\{([^}]*)\}/g
  let m
  while ((m = re.exec(CSS))) {
    const selector = m[1].replace(/\/\*[\s\S]*?\*\//g, '').trim()
    if (!selectorPattern.test(selector)) continue
    // Comments are stripped from the body too, not just the selector: the
    // token file explains itself, and prose like "cannot be --text: in light
    // mode …" otherwise parses as a declaration whose value is the rest of
    // the sentence. That produced "Not a hex colour: in light mode …".
    const body = m[2].replace(/\/\*[\s\S]*?\*\//g, '')
    for (const [, name, value] of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
      out[name] = value.trim()
    }
  }
  return out
}

const palette = block(/^:root$/m)
const darkVars = { ...palette, ...block(/:root\[data-theme='dark'\]|^:root,\s*$/m) }
const lightVars = { ...palette, ...block(/:root\[data-theme='light'\]/) }

// The dark block's selector is `:root, :root[data-theme='dark']`; pick it up
// explicitly so a stray match order cannot silently drop it.
Object.assign(darkVars, block(/:root\[data-theme='dark'\]/))

/** Resolve `var(--x)` chains down to a literal colour. */
function resolve(value, vars, depth = 0) {
  if (depth > 12) throw new Error(`Cyclic token: ${value}`)
  const v = value.trim()
  const varMatch = v.match(/^var\((--[\w-]+)\)$/)
  if (varMatch) {
    const next = vars[varMatch[1]]
    if (!next) throw new Error(`Undefined token ${varMatch[1]}`)
    return resolve(next, vars, depth + 1)
  }
  return v
}

/* ------------------------------------------------------------------ */
/* Contrast                                                            */
/* ------------------------------------------------------------------ */
function rgb(hex) {
  const h = hex.replace('#', '').trim()
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  if (!/^[0-9a-f]{6}$/i.test(full)) throw new Error(`Not a hex colour: ${hex}`)
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16))
}

function luminance(hex) {
  const [r, g, b] = rgb(hex).map((channel) => {
    const c = channel / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(fg, bg) {
  const a = luminance(fg)
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

/* ------------------------------------------------------------------ */
/* The pairs the UI actually renders                                   */
/* ------------------------------------------------------------------ */
const BODY = 4.5
const LARGE = 3

/** [foreground, background, minimum, description] */
const PAIRS = [
  ['--text', '--surface', BODY, 'Body text on the app canvas'],
  ['--text', '--surface-raised', BODY, 'Body text on cards and tables'],
  ['--text', '--surface-overlay', BODY, 'Body text in modals'],
  ['--text-muted', '--surface', BODY, 'Muted text on the canvas'],
  ['--text-muted', '--surface-raised', BODY, 'Muted text on cards'],
  ['--text-muted', '--surface-overlay', BODY, 'Muted text in modals'],
  ['--text-subtle', '--surface', LARGE, 'Subtle text (labels, hints)'],
  ['--text-subtle', '--surface-raised', LARGE, 'Subtle text on cards'],

  ['--accent-fg', '--accent', BODY, 'Primary button label'],
  ['--accent-fg', '--accent-hover', BODY, 'Primary button label, hovered'],
  ['--accent-text', '--surface', BODY, 'Accent text / links on the canvas'],
  ['--accent-text', '--surface-raised', BODY, 'Accent text on cards'],
  ['--accent', '--surface', LARGE, 'Accent as a non-text mark (active nav)'],
  ['--focus', '--surface', LARGE, 'Focus ring on the canvas'],
  ['--focus', '--surface-raised', LARGE, 'Focus ring on cards'],
  ['--focus', '--surface-overlay', LARGE, 'Focus ring in modals'],

  ['--status-healthy', '--surface', LARGE, 'Healthy dot on the canvas'],
  ['--status-healthy', '--surface-raised', LARGE, 'Healthy dot on cards'],
  ['--status-healthy', '--status-healthy-quiet', LARGE, 'Healthy badge text on its tint'],
  ['--status-danger', '--surface', LARGE, 'Danger dot on the canvas'],
  ['--status-danger', '--surface-raised', LARGE, 'Danger dot on cards'],
  ['--status-danger', '--status-danger-quiet', BODY, 'Error text on its tint'],
  ['--status-transitional', '--surface', LARGE, 'Transitional dot on the canvas'],
  ['--status-transitional', '--surface-raised', LARGE, 'Transitional dot on cards'],
  ['--status-transitional', '--status-transitional-quiet', LARGE, 'Transitional badge on tint'],

  ['--border-strong', '--surface', LARGE, 'Meaningful border (input outline)'],
  ['--text-on-sunken', '--surface-sunken', BODY, 'Console toolbar text'],
]

/* ------------------------------------------------------------------ */
/* Run                                                                 */
/* ------------------------------------------------------------------ */
const themes = [
  ['dark', darkVars],
  ['light', lightVars],
]

const rows = []
let failures = 0

for (const [themeName, vars] of themes) {
  for (const [fgToken, bgToken, min, description] of PAIRS) {
    if (!vars[fgToken] || !vars[bgToken]) {
      throw new Error(`${themeName}: token missing (${fgToken} on ${bgToken})`)
    }
    const fg = resolve(vars[fgToken], vars)
    const bg = resolve(vars[bgToken], vars)
    const ratio = contrast(fg, bg)
    const pass = ratio >= min
    if (!pass) failures += 1
    rows.push({
      theme: themeName,
      description,
      pair: `${fgToken} on ${bgToken}`,
      fg,
      bg,
      ratio: ratio.toFixed(2),
      min: min.toFixed(1),
      pass,
    })
  }
}

const markdown = process.argv.includes('--md')

if (markdown) {
  console.log('| Theme | What | Tokens | Colours | Ratio | Min | |')
  console.log('|---|---|---|---|---|---|---|')
  for (const r of rows) {
    console.log(
      `| ${r.theme} | ${r.description} | \`${r.pair}\` | ${r.fg} on ${r.bg} | ` +
        `**${r.ratio}** | ${r.min} | ${r.pass ? 'PASS' : 'FAIL'} |`,
    )
  }
} else {
  const width = Math.max(...rows.map((r) => r.description.length))
  let current = ''
  for (const r of rows) {
    if (r.theme !== current) {
      current = r.theme
      console.log(`\n${r.theme.toUpperCase()}`)
    }
    console.log(
      `  ${r.pass ? 'PASS' : 'FAIL'}  ${r.description.padEnd(width)}  ` +
        `${r.ratio.padStart(6)} : 1  (min ${r.min})  ${r.fg} on ${r.bg}`,
    )
  }
}

console.log(
  `\n${rows.length} pairs checked across ${themes.length} themes — ` +
    `${failures === 0 ? 'all pass' : `${failures} FAILING`}`,
)
process.exit(failures === 0 ? 0 : 1)
