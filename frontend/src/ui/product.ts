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
 * has a cost and no upside. The mark is shown with its descriptor rather than
 * bare, and the visual language stays deliberately plain: no pixel art, no
 * retro-game styling, no spinning characters.
 */
export const PRODUCT_NAME = 'Kurukuru'

/** What follows the name wherever it is introduced. */
export const PRODUCT_TAGLINE = 'local cloud infrastructure'
