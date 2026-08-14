/**
 * Fails when a component hard-codes a colour.
 *
 * Two ways to break the token layer, and this catches both:
 *
 *   1. A raw hex — `#09090b`, `bg-[#1a1a1a]`.
 *   2. A Tailwind palette colour — `bg-zinc-900`, `text-green-400`. These are
 *      the more insidious ones: they look like they belong, and they render
 *      perfectly well, but they do not move when the theme changes. Every
 *      light-mode bug in this codebase would have started as one of these.
 *
 * The allowlist is deliberately short and each entry says why. Growing it is a
 * decision someone should have to defend in review.
 *
 *   node scripts/check-raw-colour.mjs
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = join(here, '..', 'src')

/** Tailwind's built-in colour families. Ours are named semantically instead. */
const PALETTE =
  '(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|' +
  'teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)'

const RULES = [
  {
    name: 'raw hex colour',
    // #abc / #aabbcc / #aabbccdd, but not a CSS id selector or a fragment.
    re: /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\b/g,
  },
  {
    name: 'Tailwind palette colour',
    re: new RegExp(
      `\\b(?:bg|text|border|ring|fill|stroke|from|to|via|divide|outline|shadow|` +
        `accent|caret|decoration|placeholder)-${PALETTE}-\\d{2,3}\\b`,
      'g',
    ),
  },
  {
    name: 'arbitrary colour value',
    re: /\b(?:bg|text|border|ring|fill|stroke)-\[#[0-9a-fA-F]{3,8}\]/g,
  },
]

/**
 * Files that may contain colours, with the reason.
 *
 * `src/styles/tokens.css` is the point of the whole system — it is where the
 * literals live. Nothing else has an excuse.
 */
const ALLOW = new Map([
  ['styles/tokens.css', 'the palette itself; every literal in the app lives here'],
])

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === 'assets') continue // font binaries
      out.push(...walk(full))
    } else if (/\.(tsx?|css)$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

const offences = []

for (const file of walk(SRC)) {
  const rel = relative(SRC, file).replace(/\\/g, '/')
  if (ALLOW.has(rel)) continue
  const source = readFileSync(file, 'utf8')
  const lines = source.split('\n')

  for (const rule of RULES) {
    for (const [index, line] of lines.entries()) {
      // Skip lines that are purely a comment: prose may legitimately mention
      // a colour ("was grey-700, measured 2.27:1") without rendering one.
      const trimmed = line.trim()
      if (trimmed.startsWith('*') || trimmed.startsWith('//') || trimmed.startsWith('/*')) {
        continue
      }
      rule.re.lastIndex = 0
      const found = line.match(rule.re)
      if (found) {
        offences.push({ file: rel, line: index + 1, rule: rule.name, text: found.join(', ') })
      }
    }
  }
}

if (offences.length === 0) {
  console.log('No hard-coded colours outside the token layer.')
  process.exit(0)
}

console.error(`${offences.length} hard-coded colour(s) found:\n`)
for (const o of offences) {
  console.error(`  ${o.file}:${o.line}  ${o.rule}: ${o.text}`)
}
console.error(
  '\nUse a semantic token instead (bg-surface, text-text-muted, ' +
    'text-healthy, …). See src/styles/tokens.css.',
)
process.exit(1)
