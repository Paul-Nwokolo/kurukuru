# PHASE 12 BRIEF — Design System, Themes & Structure

No new features. This phase gives the dashboard a coherent visual system,
a real light mode, and an information architecture that reflects the
product rather than the order features were built in.

Read this whole brief before starting. Work in the order given — tokens
first, because everything after depends on them.

## Design direction
Dense but calm. The utility of a hyperscaler console (nothing hidden to
look clean, everything one click away) with the restraint of Nothing's
hardware aesthetic and the legibility of Google's data presentation.

The governing rule, from which most decisions follow:

  **Monochrome carries structure and actions. Colour carries state.**

Today green is the logo, the primary button, AND the healthy status — one
signal doing three jobs. After this phase a green dot means exactly one
thing.

## Palette
Structure — layered greys:
  White #FFFFFF · Rainbow Grey #C5C5C5 · Quicksilver #A7A7A7 ·
  Battleship Gray #848484 · plus near-blacks for dark-mode surfaces.
Brand/interaction — Mystic Heather #9F8CAE, Lilac Haze #B3A0BE:
  active nav, selected states, focus rings, primary buttons.
Status — green healthy/running, red danger/error, amber transitional
  (Provisioning, Creating, Degraded). Nothing else may use these.

IMPORTANT: #9F8CAE and #B3A0BE are mid-tones. White text on them fails
WCAG AA, and they fail as text on white. Build a full ramp (roughly
50–900) from that hue and pick per theme: darker steps for text and fills
in light mode, lighter steps in dark mode. Same for green/red/amber —
status colours that read on near-black often fail on white. Every
foreground/background pair used in the UI must meet 4.5:1 for body text
and 3:1 for large text and meaningful non-text (status dots, focus rings).
Verify programmatically, not by eye, and report any pair you had to
adjust.

## Part A — Tokens and themes (do this first)
- One source of truth: CSS custom properties for colour, spacing, radius,
  border, shadow, and typography, consumed through Tailwind theme config.
  No hard-coded hex anywhere in components after this phase — add a lint
  rule or a test that fails on raw hex in .tsx if practical.
- Semantic names, not literal ones: `--surface`, `--surface-raised`,
  `--border`, `--text`, `--text-muted`, `--accent`, `--accent-fg`,
  `--status-healthy`, `--status-danger`, `--status-transitional`.
  Components must never reference a grey by number.
- Two themes, both first-class: dark (default) and light. Toggle in the
  header, persisted to localStorage, honouring `prefers-color-scheme` on
  first visit. No flash of wrong theme on load.
- Typography: a technical/geometric sans for chrome and prose, and a mono
  for DATA — identifiers, ports, paths, fingerprints, sizes, IPs, device
  names, status codes. Mono is used sporadically today (`qcow2`, the ISO
  path, fingerprints); make it systematic. Self-host the fonts; no
  external CDN. Define a type scale and use it.

## Part B — Component pass
Bring these to one standard, in both themes:
- Buttons: primary (accent), secondary (outline), ghost, danger. One
  height scale. Loading and disabled states. `Launch Instance`,
  `Import image`, `Create volume`, `Create` are all primary today and all
  green — they become accent.
- Status: one `StatusBadge` used everywhere — instances, images, volumes,
  snapshots, networks. Dot + label, colour by state only.
- Tables: one component. Consistent header treatment, row height,
  hover, zebra-or-not, empty state, loading skeleton, and a standard
  Actions column. Numeric and size columns right-aligned in mono.
- Forms: one input/select/checkbox/textarea set with proper focus rings
  (accent, visible in both themes), inline validation, and helper text.
- Modals: one shell. Header, scrollable body, pinned footer. The Launch
  modal currently scrolls its own footer out of reach — fix that.
- Badges/tags: `BUILTIN`, `CLOUD-INIT`, `DEFAULT`, `ORCHESTRATOR`,
  `derived`, engine labels — one component, monochrome by default.
- Empty states: one pattern. The Instances empty state is good; match it.
- Toasts/inline errors: one pattern, using status colours only.

## Part C — Structure
- **Sidebar**: nine flat items is a list of features, not a model. Group
  with quiet section labels:
    Compute — Instances
    Resources — Images, ISOs, Volumes, Key pairs
    Configuration — Networks, Projects
    System — Activity, Settings
  Keep every destination reachable; this is grouping, not nesting.
- **Header**: project selector, theme toggle, refresh, backend health.
  `Backend online` currently looks like a button but isn't — make it
  unmistakably an indicator. Consider collapsing Refresh into a quieter
  icon action given everything polls.
- **Footer nav** (`Volumes · Snapshots · Events · Projects`) is vestigial
  now that all four are in the sidebar. Remove it.
- **One creation pattern.** Volumes and Projects use inline forms above
  the table; Images, Key pairs and Instances use a modal. Standardise on
  the modal — an occasional action shouldn't hold vertical space
  permanently.
- **Instance detail view**: six sections currently appended in build
  order. Re-order by what people come to the page for: identity/status and
  primary actions at top; then Access (SSH command, keys, console); then
  Network (forwards); then Storage (volumes, snapshots); then Config
  (user-data, image, accel, display); then Activity. Consider two columns
  at wide widths — the page is long and mostly left-aligned today.
- **Launch modal**: long enough to hide its own scroll. Either steps
  (Source → Size → Access → Advanced) or clearer sectioning with a
  visible summary; recommend which, then implement.
- **Activity icons**: five colours where the text already says what
  happened. Monochrome icons; colour only where the event is an error.

## Part D — Polish
- Focus-visible on every interactive element; full keyboard traversal;
  Escape closes modals; Enter submits forms.
- Loading skeletons rather than spinners for tables and the detail view.
- Consistent relative time with absolute on hover, everywhere.
- Copy-to-clipboard: one component, one confirmation pattern.
- Truncation with tooltip for long paths/fingerprints, consistently.
- Respect `prefers-reduced-motion`.
- Console panel: it is the one screen that should feel like hardware —
  give it the darkest surface, minimal chrome, and a quiet toolbar.
- A generic wordmark placeholder. The product name is NOT decided; do not
  invent one. Keep "Local IaaS Orchestrator" and make the lockup easy to
  replace later — one component, one place.

## Non-negotiables
- **Do not touch the copy.** The explanatory writing (Networks
  availability, Projects "grants and withholds nothing", Volumes UUID
  guidance, the Graphics/Performance help text) is the product's best
  asset. Restyle it; do not rewrite it. If a layout change would truncate
  or hide it, change the layout.
- No behaviour changes. No new endpoints. If you find a bug, report it —
  don't fix it inside this phase unless it blocks the work.
- Bundle size: report before/after. Fonts and a token layer should be
  modest; if the delta is large, explain it.

## Verification
- Every view in BOTH themes: Instances (populated, empty, loading, error),
  detail view, all nine tabs, Launch modal (all three modes, Advanced
  open), console, every modal and confirm dialog. Screenshot each.
- Contrast: programmatic check of every token pair actually used, with
  the results table in the report.
- Keyboard-only pass through the main flows.
- 571 backend tests still green; tsc, lint, build clean, exit codes read
  directly.
- Report: what changed, the contrast table, before/after bundle size,
  anything you found that you left alone, and screenshots.

## Out of scope
Product name and real identity, marketing site, mobile/responsive layout
beyond not breaking, animation systems, packaging.
