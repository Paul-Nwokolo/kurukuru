/**
 * Fails when the dashboard spells a name instead of asking for it.
 *
 * There are **two** names, with two different single sources, and the whole
 * point of this check is that they do not get confused for each other.
 *
 * 1. **The command** — `kurukuru` — has one definition, `CLI_NAME` in
 *    `backend/app/product.py`. It reaches this app as `cli_name` on
 *    `GET /auth/first-run`, and copy renders it via `cliName()` / `cliCommand()`
 *    from `src/api/client.ts`. It comes from the backend because it is a
 *    property of the *install*: it can be aliased, and it has been renamed
 *    once already. The strings that matter most are the ones shown to someone
 *    who is *locked out* — "run `kurukuru auth init`" on the login screen — and
 *    those are followed literally by a user with no other way in. Copy naming a
 *    command that does not exist is worse than no copy at all.
 *
 * 2. **The brand** — `Kurukuru` — has one definition in `src/ui/product.ts`.
 *    It does not vary by install and is needed before the first request
 *    resolves, so it is a frontend constant rather than an API field. Only that
 *    file may spell it.
 *
 * **The old names are checked too, and that is deliberate.** A rename is not
 * finished when the new name is everywhere; it is finished when the old one
 * cannot come back. Phase 16 found `Local IaaS` living in *two* files while
 * this script's own comment claimed it had exactly one home — because the check
 * only knew about the lower-case command name. Both spellings are now the
 * script's business.
 *
 *   node scripts/check-product-name.mjs
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = join(here, '..', 'src')

/**
 * What may not appear in source, and where the name is allowed to live instead.
 *
 * `only` names the one file exempt from a rule — the file that *is* the single
 * source. A rule with no `only` may not be spelled anywhere.
 */
const RULES = [
  {
    what: 'the command name',
    // Case-sensitive and word-bounded: `kurukuru` is what a user types.
    re: /\bkurukuru\b/,
    fix: "render it with cliName() or cliCommand('auth login') from src/api/client.ts",
  },
  {
    what: 'the previous command name',
    re: /\biaas\b/,
    fix: "the command is no longer called this; render it with cliName() from src/api/client.ts",
  },
  {
    what: 'the product name',
    re: /\bKurukuru\b/,
    only: 'ui/product.ts',
    fix: "import PRODUCT_NAME from src/ui/product.ts",
  },
  {
    what: 'the previous product name',
    re: /\bLocal IaaS\b/,
    fix: "the product is no longer called this; import PRODUCT_NAME from src/ui/product.ts",
  },
  {
    // Added after `IAAS_CORS_ORIGINS` survived the Phase 16 rename in this
    // app's own copy and shipped. It slipped through because every rule above
    // is case-sensitive on the *lower*-case command name, and an environment
    // variable is upper-case — so nothing here was ever looking at it.
    what: 'the previous environment-variable prefix',
    re: /\bIAAS_[A-Z0-9_]*/,
    fix: 'environment variables are KURUKURU_-prefixed; check backend/kurukuru/config.py for the current name',
  },
  {
    // The old product name in the casings the rules above miss — `IaasNetwork`,
    // `IaaSClient`. Identifiers are not copy, but they are still the old name,
    // and leaving them teaches the next reader a name that no longer exists.
    what: 'the previous product name in an identifier',
    re: /\bIaa[Ss]\b|\bIaa[Ss][A-Z]/,
    fix: 'name it for what it is rather than for what the product used to be called',
  },
]

/**
 * Occurrences that are not copy, with the reason each is allowed.
 *
 * The distinction throughout is **is this shown to a user, or is it a wire
 * identifier?** A rename of the command does not automatically rename an HTTP
 * header or a storage key — those are contracts with the backend and with the
 * browser. Phase 16 did rename them, coherently on both sides and with the old
 * values migrated, but they are still not copy: they are spelled once each, at
 * the boundary that owns them, and never in a sentence.
 */
const ALLOW = [
  {
    re: /CSRF_HEADER = 'X-Kurukuru-CSRF'/,
    why: 'the one definition of the HTTP header name; wire contract, not copy',
  },
  {
    re: /'(?:kurukuru|iaas)\.(?:project|theme)'/,
    why: 'localStorage keys, old and new; the pair is what carries a saved preference across the rename',
  },
  {
    re: /kurukuru_session|iaas_session/,
    why: 'cookie names, old and new; wire contract, not copy',
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
    if (ALLOW.some((entry) => entry.re.test(line))) continue

    for (const rule of RULES) {
      if (rule.only === rel) continue
      if (!rule.re.test(line)) continue
      offences.push({ file: rel, line: index + 1, text: trimmed, rule })
    }
  }
}

if (offences.length === 0) {
  console.log('No hard-coded product or command name in user-visible text.')
  process.exit(0)
}

console.error(`${offences.length} hard-coded name(s) found:\n`)
for (const o of offences) {
  console.error(`  ${o.file}:${o.line}  [${o.rule.what}]  ${o.text.slice(0, 90)}`)
  console.error(`      ${o.rule.fix}`)
}
console.error(
  '\nThe command name has one definition (backend/app/product.py) and reaches ' +
    'the app as cli_name on GET /auth/first-run; omit the sentence when it ' +
    'returns null rather than falling back to a literal. The product name has ' +
    'one definition (src/ui/product.ts).',
)
process.exit(1)
