/**
 * Fails when the built bundle has an absolute origin welded into it.
 *
 * The backend serves this bundle, so the dashboard must talk to whatever origin
 * the user actually reached it on. A build that hard-codes one is broken for
 * everybody whose origin differs — a different port, `127.0.0.1` instead of
 * `localhost`, a hostname, a reverse proxy.
 *
 * **This shipped once, so it is checked rather than remembered.** `client.ts`
 * had `VITE_API_URL || window.location.origin`, which reads like a fallback and
 * is not one: Vite inlines `import.meta.env.VITE_API_URL` as a string literal at
 * build time, so a value in the build machine's `.env` makes the literal
 * non-empty, the `||` dead, and the fallback unreachable. A bundle built with
 * `VITE_API_URL=http://localhost:8000` was served from port 7842. Its documents
 * and assets loaded — same origin, no problem — and every XHR went to a port
 * with nothing listening on it. The dashboard said "Cannot reach the backend"
 * while the backend answered `curl` on the very origin serving the page.
 *
 * What makes that failure nasty is that nothing about it looks like a build
 * problem: it is invisible on the machine that built it, because there the
 * baked-in origin happens to be right.
 *
 *   node scripts/check-bundle-origin.mjs
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const DIST = join(here, '..', 'dist')

/**
 * Origins that must never appear in a build.
 *
 * Deliberately not "any absolute URL": the bundle legitimately contains links
 * to documentation and to upstream projects, and banning those would make this
 * check something people work around rather than fix. What is banned is an
 * origin that could be mistaken for *this backend* — a loopback address or a
 * bare hostname with a port, which is only ever a development leak.
 */
const BANNED = [
  {
    re: /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?/gi,
    why: 'a loopback origin — the dashboard must use window.location.origin',
  },
  {
    re: /\bVITE_API_URL\b/g,
    why: 'VITE_API_URL reached the build; it must be dev-only (import.meta.env.DEV)',
  },
]

/**
 * Occurrences that belong to somebody else's code, with the reason each is
 * allowed.
 *
 * A check that cries wolf gets deleted, so the exemptions are specific matches
 * with stated reasons rather than a loosened pattern. Each is tested against
 * the surrounding *context* rather than the bare origin, so an exemption cannot
 * quietly cover one of ours that happens to sit nearby in a minified file.
 */
const ALLOW = [
  {
    re: /window\.location\.href\s*\|\|\s*[`'"]https?:\/\/localhost[`'"]/,
    why: "axios's own fallback base for non-browser environments; unreachable in a browser and not ours to change",
  },
]

function walk(dir) {
  const out = []
  let entries
  try {
    entries = readdirSync(dir)
  } catch {
    return out
  }
  for (const entry of entries) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...walk(full))
    else if (/\.(?:js|css|html)$/.test(entry)) out.push(full)
  }
  return out
}

const files = walk(DIST)

if (files.length === 0) {
  // A check that silently passes when there is nothing to check is worse than
  // no check: it would report green on every CI run that forgot to build.
  console.error(
    `No built files under ${DIST}.\n` +
      `Run \`npm run build\` first — this check is meaningless without a bundle.`,
  )
  process.exit(1)
}

const offences = []
for (const file of files) {
  const text = readFileSync(file, 'utf8')
  for (const { re, why } of BANNED) {
    for (const match of text.matchAll(re)) {
      // A little of the surrounding code, so the report names which of several
      // possible sites it is without needing to open a minified file — and so
      // an exemption is matched against context rather than a bare origin.
      const from = Math.max(0, match.index - 45)
      const context = text
        .slice(from, match.index + match[0].length + 35)
        .replace(/\s+/g, ' ')
      if (ALLOW.some((entry) => entry.re.test(context))) continue
      offences.push({
        file: relative(DIST, file).replace(/\\/g, '/'),
        found: match[0],
        why,
        context,
      })
    }
  }
}

if (offences.length === 0) {
  console.log(`No hard-coded origin in ${files.length} built file(s).`)
  process.exit(0)
}

console.error(`${offences.length} hard-coded origin(s) in the build:\n`)
for (const o of offences) {
  console.error(`  ${o.file}: ${o.found}`)
  console.error(`      ${o.why}`)
  console.error(`      …${o.context}…\n`)
}
console.error(
  'The backend serves this bundle, so it must call window.location.origin. ' +
    'VITE_API_URL applies in development only — see API_ORIGIN in ' +
    'src/api/client.ts, which branches on import.meta.env.DEV so the dev path ' +
    'is eliminated from the build entirely.',
)
process.exit(1)
