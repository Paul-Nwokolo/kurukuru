/**
 * What the product is called, in the dashboard, in one place.
 *
 * The **command** is not here: it arrives from the backend as `cli_name` on
 * `GET /auth/first-run`, because it is a property of the install (it can be
 * aliased, it could be renamed again) and copy that names a command the user
 * does not have is worse than no copy at all. See `check-product-name.mjs`.
 *
 * The **brand** is different. It does not vary by install, it is needed before
 * the first request resolves — the login screen renders it while unauthenticated
 * — and round-tripping a constant through HTTP to render a heading would make
 * the one screen a locked-out user sees depend on a fetch. So it lives here as
 * a literal, and the name guard enforces that this is the only file allowed to
 * spell it.
 *
 * `PRODUCT_TAGLINE` is not decoration. There is a live Nintendo registration for
 * "KURUKURU KURURIN" in Class 009 — video game programs — and while a
 * single-host hypervisor control plane is not in that category, resembling one
 * has a cost and no upside. So the name is presented with its descriptor, and
 * the visual language stays deliberately plain: no pixel art, no retro-game
 * styling, no spinning characters.
 *
 * **Where the descriptor is owed, precisely.** The concern is *public
 * presentation* — the places the product introduces itself to someone who has
 * not met it, and where a passing resemblance to a game could be formed:
 *
 *   - the website and the README
 *   - the installer, and what it registers in Apps & Features
 *   - the login screen, which is what an unauthenticated visitor sees
 *
 * In each of those the name and descriptor appear together, at a size the
 * descriptor can actually be read at.
 *
 * It does **not** extend to chrome inside the running application. The sidebar
 * lockup is 40px, where the design spec's own ratio puts the descriptor at
 * 5.5px: present, unreadable, and therefore doing none of the work this rule
 * exists to do — noise shaped like information, in front of a user who has
 * already installed the thing and signed into it. `Wordmark` takes
 * `descriptor={false}` there for that reason, and this paragraph is here so
 * that it is not quietly reinstated as a fix.
 */
export const PRODUCT_NAME = 'Kurukuru'

/** What follows the name wherever it is introduced. */
export const PRODUCT_TAGLINE = 'local cloud infrastructure'
