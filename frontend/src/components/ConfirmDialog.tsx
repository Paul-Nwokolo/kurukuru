import { useId } from 'react'
import type { ReactNode } from 'react'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'

/**
 * The destructive-confirmation dialog.
 *
 * Built on the shared Modal so Escape, focus trapping and the pinned footer
 * come for free. The confirm button is always `danger` — every use of this
 * component is something irreversible, and if one ever is not, it should be a
 * plain form rather than a confirmation.
 *
 * **The confirm button submits a real form**, wired back across the pinned
 * footer with the `form` attribute the same way LaunchModal does it. Two
 * reasons, neither of them tidiness: a dialog whose primary action is a bare
 * `onClick` cannot be completed by Enter from anything the body might grow
 * (a "type the name to confirm" input is the obvious one), and `type="submit"`
 * is what tells the browser which control is the default action.
 *
 * **Focus opens on Cancel, not on the confirm button.** This is deliberate and
 * it is the one place this component declines to make Enter do the dangerous
 * thing: every use is irreversible, dialogs appear in response to a click that
 * may still have Enter held down after it, and the cost of guessing wrong is a
 * terminated instance. Tab once and Enter confirms — the dialog is fully
 * keyboard-operable either way, so the only thing being traded is one
 * keystroke against an unrecoverable misfire.
 */
export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  message: ReactNode
  confirmLabel?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  // Scoped per instance rather than a module constant: two of these can be
  // mounted at once (a table row's dialog and a detail page's), and duplicate
  // ids would point both footers at whichever form the document reached first.
  const formId = useId()

  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      size="sm"
      footer={
        <>
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button intent="danger" type="submit" form={formId} loading={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault()
          onConfirm()
        }}
        className="text-sm leading-relaxed text-text-muted"
      >
        {message}
      </form>
    </Modal>
  )
}
