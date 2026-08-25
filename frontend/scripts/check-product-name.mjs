/**
 * Fails when the dashboard spells the product's name instead of asking for it.
 *
 * The CLI is called `iaas` today and will not be forever. The strings that
 * matter most are the ones shown to someone who is *locked out* — "run `iaas
 * auth init`" on the login screen — and those are followed literally by a user
 * with no other way in. A rename that misses one of them produces instructions
 * naming a command that does not exist, on the one screen where the user cannot
 * work around it.
 *
 * So the name has exactly one definition, `CLI_NAME` in `backend/app/product.py`.
 * It reaches this app as `cli_name` on `GET /auth/first-run`, and copy renders
 * it via `cliName()` / `cliCommand()` from `src/api/client.ts`.
 *
 * What this catches is the literal reappearing in a string or in JSX text.
 * Comments are exempt: prose explaining the mechanism has to be able to name it.
 *
 *   node scripts/check-product-name.mjs
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = join(here, '..', 'src')

/** The name, as the backend currently defines it. */
const NAME = 'iaas'

/**
 * Occurrences that are not copy, with the reason each is allowed.
 *
 * The distinction throughout is **is this shown to a user, or is it a wire
 * identifier?** A rename of the command does not rename an HTTP header or a
 * storage key — those are contracts with the backend and with the browser, and
 * changing them would break a running install rather than fix one.
 */
const ALLOW = [
  {
    re: /x-iaas-csrf|X-IAAS-CSRF/,
    why: 'HTTP header name; part of the wire contract, not copy',
  },
  {
    re: /'iaas\.(?:project|theme)'/,
    why: 'localStorage key; renaming it would silently discard saved preferences',
  },
  {
    re: /iaas_session/,
    why: 'cookie name; part of the wire contract, not copy',
  },
]

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === 'assets') continue
      out.push(...walk(full))
    } else if (/\.tsx?$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

// Case-sensitive, and that is the whole distinction: `iaas` is the command a
// user types, which this app must never spell. "Local IaaS" is the product's
// display name, which already has exactly one home — `src/ui/Wordmark.tsx`,
// documented as the file you edit when the name is settled. Two different
// names, two different single sources; only the first one belongs in copy.
const pattern = new RegExp('\\b' + NAME + '\\b')
const offences = []

for (const file of walk(SRC)) {
  const rel = relative(SRC, file).replace(/\\/g, '/')
  const lines = readFileSync(file, 'utf8').split('\n')

  for (const [index, line] of lines.entries()) {
    // Comment-only lines are prose about the mechanism, and must be able to
    // name it. This is the same carve-out check-raw-colour.mjs makes.
    const trimmed = line.trim()
    if (trimmed.startsWith('*') || trimmed.startsWith('//') || trimmed.startsWith('/*')) {
      continue
    }
    if (!pattern.test(line)) continue
    if (ALLOW.some((entry) => entry.re.test(line))) continue

    offences.push({ file: rel, line: index + 1, text: trimmed })
  }
}

if (offences.length === 0) {
  console.log(`No hard-coded "${NAME}" in user-visible text.`)
  process.exit(0)
}

console.error(`${offences.length} hard-coded product name(s) found:\n`)
for (const o of offences) {
  console.error(`  ${o.file}:${o.line}  ${o.text.slice(0, 100)}`)
}
console.error(
  `\nThe name has one definition (backend/app/product.py) and reaches the app ` +
    `as cli_name on GET /auth/first-run. Render it with cliName() or ` +
    `cliCommand('auth login') from src/api/client.ts, and omit the sentence ` +
    `when they return null rather than falling back to a literal.`,
)
process.exit(1)
