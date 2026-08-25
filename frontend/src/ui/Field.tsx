import { useId } from 'react'
import type {
  InputHTMLAttributes,
  ReactNode,
  Ref,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'

/**
 * The form set.
 *
 * One visual treatment, one focus ring, and — the part that was missing — one
 * place where a label, its control, its helper text and its error are wired
 * together. `Field` generates the id and the `aria-describedby`, so an input
 * cannot end up with a label that does not point at it, which is the accessible
 * failure nobody notices until they try the keyboard.
 *
 * The focus ring is the accent, and it is on `:focus-visible` globally (see
 * index.css) as well as an explicit border change here — the border says
 * "this one" while the ring says "you are keyboard-navigating".
 */
const CONTROL = [
  'w-full rounded border bg-surface text-text placeholder:text-text-subtle',
  'border-border-strong transition-colors',
  'focus:border-accent focus:outline-none',
  'disabled:cursor-not-allowed disabled:opacity-50',
].join(' ')

const CONTROL_HEIGHT = 'h-[var(--control-height-lg)] px-2.5 text-base'

export function Field({
  label,
  help,
  error,
  children,
  className = '',
}: {
  label?: ReactNode
  /** Explanatory copy under the control. This product's help text is one of
   *  its better assets; it gets a permanent home rather than a tooltip. */
  help?: ReactNode
  error?: ReactNode
  children: (props: { id: string; describedBy: string | undefined }) => ReactNode
  className?: string
}) {
  const id = useId()
  const helpId = help ? `${id}-help` : undefined
  const errorId = error ? `${id}-error` : undefined
  const describedBy = [errorId, helpId].filter(Boolean).join(' ') || undefined

  return (
    <div className={className}>
      {label && (
        <label htmlFor={id} className="mb-1.5 block text-xs font-medium text-text">
          {label}
        </label>
      )}
      {children({ id, describedBy })}
      {error && (
        <p id={errorId} className="mt-1.5 text-xs text-danger">
          {error}
        </p>
      )}
      {help && (
        <p id={helpId} className="mt-1.5 text-xs leading-relaxed text-text-muted">
          {help}
        </p>
      )}
    </div>
  )
}

export function Input({
  className = '',
  data = false,
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & {
  data?: boolean
  /** React 19 passes `ref` through as an ordinary prop; it only has to be
   *  declared. Callers that need to move focus — the login form putting the
   *  cursor back in the password box after a rejection — would otherwise have
   *  to drop out of the design system to get it. */
  ref?: Ref<HTMLInputElement>
}) {
  return (
    <input
      className={[CONTROL, CONTROL_HEIGHT, data ? 'data' : '', className].join(' ')}
      {...rest}
    />
  )
}

export function Select({
  className = '',
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={[CONTROL, CONTROL_HEIGHT, className].join(' ')} {...rest}>
      {children}
    </select>
  )
}

export function Textarea({
  className = '',
  data = false,
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { data?: boolean }) {
  return (
    <textarea
      className={[CONTROL, 'px-2.5 py-2 text-sm', data ? 'data' : '', className].join(' ')}
      {...rest}
    />
  )
}

export function Checkbox({
  label,
  className = '',
  ...rest
}: InputHTMLAttributes<HTMLInputElement> & { label: ReactNode }) {
  return (
    <label
      className={`flex cursor-pointer select-none items-center gap-2 text-sm text-text-muted ${className}`}
    >
      <input
        type="checkbox"
        className="h-3.5 w-3.5 rounded-sm border-border-strong bg-surface accent-[var(--accent)]"
        {...rest}
      />
      {label}
    </label>
  )
}

/** A group of radio-like options rendered as segmented buttons. */
export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  label,
}: {
  value: T
  onChange: (next: T) => void
  options: { value: T; label: ReactNode; title?: string }[]
  label: string
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex rounded border border-border bg-surface p-0.5"
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={option.title}
            onClick={() => onChange(option.value)}
            className={[
              'rounded-sm px-2.5 py-1 text-xs font-medium transition-colors',
              active
                ? 'bg-accent text-accent-fg'
                : 'text-text-muted hover:bg-surface-overlay hover:text-text',
            ].join(' ')}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
