import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Loader2 } from 'lucide-react'

/**
 * The one button.
 *
 * Four intents, and the split is about consequence rather than prominence:
 *
 *   primary   — the accent. The thing this screen is for.
 *   secondary — outlined. Everything else that is a real action.
 *   ghost     — no chrome until hovered. Icon actions in table rows.
 *   danger    — destructive, and only destructive. Terminate, Delete.
 *
 * Note what is *not* here: green. `Launch Instance`, `Add image` and
 * `Create volume` were all green, which is also what a healthy instance is.
 * Primary actions are the accent now, and green means one thing.
 */
type Intent = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg'

interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  intent?: Intent
  size?: Size
  /** Shows a spinner and disables the button. The label stays, so the control
   *  does not change width mid-action and move whatever is next to it. */
  loading?: boolean
  /** Rendered before the label at the right size for the button. */
  icon?: typeof Loader2
  children?: ReactNode
  className?: string
}

const INTENTS: Record<Intent, string> = {
  primary:
    'bg-accent text-accent-fg hover:bg-accent-hover ' +
    'disabled:bg-accent disabled:text-accent-fg',
  secondary:
    'border border-border-strong bg-surface-raised text-text ' +
    'hover:border-text-subtle hover:bg-surface-overlay',
  ghost: 'text-text-muted hover:bg-surface-overlay hover:text-text',
  danger:
    'border border-danger/40 bg-danger-quiet text-danger ' +
    'hover:border-danger hover:bg-danger/15',
}

const SIZES: Record<Size, string> = {
  sm: 'h-[var(--control-height-sm)] px-2.5 text-2xs gap-1.5',
  md: 'h-[var(--control-height)] px-3 text-xs gap-2',
  lg: 'h-[var(--control-height-lg)] px-4 text-base gap-2',
}

const ICON_SIZES: Record<Size, string> = {
  sm: 'h-3 w-3',
  md: 'h-3.5 w-3.5',
  lg: 'h-4 w-4',
}

export function Button({
  intent = 'secondary',
  size = 'md',
  loading = false,
  icon: Icon,
  children,
  className = '',
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  const Glyph = loading ? Loader2 : Icon
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={[
        'inline-flex shrink-0 items-center justify-center rounded font-medium',
        'transition-colors disabled:cursor-not-allowed disabled:opacity-50',
        INTENTS[intent],
        SIZES[size],
        className,
      ].join(' ')}
      {...rest}
    >
      {Glyph && (
        <Glyph className={`${ICON_SIZES[size]} shrink-0 ${loading ? 'animate-spin' : ''}`} />
      )}
      {children}
    </button>
  )
}

/**
 * A square, label-less button for table rows and toolbars.
 *
 * `title` is required rather than optional: an icon with no accessible name is
 * a control only its author can use, and this is the component where that
 * mistake would otherwise be easiest to make.
 */
export function IconButton({
  icon: Icon,
  title,
  intent = 'ghost',
  size = 'md',
  className = '',
  ...rest
}: Omit<ButtonProps, 'children' | 'icon'> & { icon: typeof Loader2; title: string }) {
  const box = size === 'sm' ? 'h-7 w-7' : 'h-8 w-8'
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      className={[
        'inline-flex items-center justify-center rounded transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-40',
        intent === 'danger'
          ? 'text-danger hover:bg-danger-quiet'
          : 'text-text-subtle hover:bg-surface-overlay hover:text-text',
        box,
        className,
      ].join(' ')}
      {...rest}
    >
      <Icon className={size === 'sm' ? 'h-3.5 w-3.5' : 'h-4 w-4'} />
    </button>
  )
}
