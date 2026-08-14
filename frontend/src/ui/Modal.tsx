import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import { IconButton } from './Button'

/**
 * One modal shell: fixed header, scrollable body, pinned footer.
 *
 * The pinned footer is the whole reason this exists. The Launch modal grew
 * long enough that its own Cancel/Launch buttons scrolled out of the viewport
 * — the dialog looked like it had no way to submit unless you knew to scroll
 * past the fold. Here the body is the only thing that scrolls, so the actions
 * are always where you left them.
 *
 * Also handled once, rather than in each of the six dialogs that needed it:
 * Escape closes, focus moves into the dialog on open and returns to whatever
 * opened it on close, and a click on the backdrop dismisses.
 */
interface ModalProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  icon?: typeof X
  children: ReactNode
  /** Pinned to the bottom, never scrolled. Buttons go here. */
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
}

const SIZES = { sm: 'max-w-md', md: 'max-w-lg', lg: 'max-w-2xl' }

export function Modal({
  open,
  onClose,
  title,
  icon: Icon,
  children,
  footer,
  size = 'md',
}: ModalProps) {
  const panel = useRef<HTMLDivElement>(null)
  const restoreFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return

    restoreFocus.current = document.activeElement as HTMLElement | null

    // Focus the first control rather than the panel itself, so a keyboard user
    // lands on something they can act on instead of having to tab past the
    // heading every time.
    const focusable = panel.current?.querySelector<HTMLElement>(
      'input:not([type="hidden"]), select, textarea, button, [href], [tabindex]:not([tabindex="-1"])',
    )
    focusable?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
      if (event.key !== 'Tab' || !panel.current) return
      // Keep Tab inside the dialog: a focus ring that wanders onto the page
      // behind a modal is a keyboard user losing their place entirely.
      const items = Array.from(
        panel.current.querySelectorAll<HTMLElement>(
          'input:not([type="hidden"]):not([disabled]), select:not([disabled]), ' +
            'textarea:not([disabled]), button:not([disabled]), [href], ' +
            '[tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.offsetParent !== null)
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.body.style.overflow = previousOverflow
      restoreFocus.current?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="animate-backdrop fixed inset-0 z-50 flex items-center justify-center bg-surface/80 p-4 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        className={[
          'animate-modal flex max-h-[min(90vh,44rem)] w-full flex-col overflow-hidden rounded-lg',
          'border border-border bg-surface-overlay shadow-[var(--shadow-overlay)]',
          SIZES[size],
        ].join(' ')}
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h2 className="flex items-center gap-2 text-base font-semibold text-text">
            {Icon && <Icon className="h-4 w-4 text-text-muted" />}
            {title}
          </h2>
          <IconButton icon={X} title="Close" onClick={onClose} size="sm" />
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">{children}</div>

        {footer && (
          <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border bg-surface/40 px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  )
}
