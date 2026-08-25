import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { LogOut, SlidersHorizontal, User as UserIcon } from 'lucide-react'
import { logout, whoami } from '../api/client'
import { VIEWS, navigate } from '../lib/router'

/**
 * Who is signed in, and the way out.
 *
 * Sign-out used to live only in Settings, which made it something you had to
 * know was there, and left the dashboard with nowhere at all showing whose
 * session it was. Identity belongs in the header, next to the other things that
 * say what this window is scoped to.
 *
 * The split with Settings is deliberate and not a duplication: this is identity
 * and the exit. Changing a password and managing API tokens are administrative
 * tasks with consequences worth reading about first, and they stay on the page
 * that has room to explain them — the menu links there rather than reproducing
 * them in a popover.
 */
export function AccountMenu() {
  const { data: user } = useQuery({ queryKey: ['whoami'], queryFn: whoami })
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  // Escape closes, and a click anywhere else closes. Both are what a menu is
  // expected to do, and neither comes for free without a dialog element —
  // which this should not be, since it is not modal.
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus() // Escape must not strand focus on nothing
    }
    const onPointer = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onPointer)
    }
  }, [open])

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((wasOpen) => !wasOpen)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={user ? `Signed in as ${user.username}` : 'Account'}
        className={[
          'flex h-[var(--control-height)] items-center gap-1.5 rounded px-2 text-xs',
          'text-text-muted transition-colors',
          'hover:bg-surface-overlay hover:text-text',
          open ? 'bg-surface-overlay text-text' : '',
        ].join(' ')}
      >
        <UserIcon className="h-3.5 w-3.5 shrink-0" aria-hidden />
        {/* The username, not an avatar or initials: one account, a name that
            is already short, and nothing to gain from making it a puzzle. */}
        <span className="max-w-[10rem] truncate">{user?.username ?? '…'}</span>
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Account"
          className="absolute right-0 top-[calc(100%+0.375rem)] z-50 w-52 overflow-hidden rounded-md border border-border bg-surface-raised shadow-lg"
        >
          <div className="border-b border-border px-3 py-2">
            <div className="truncate text-xs font-medium text-text">
              {user?.username ?? '…'}
            </div>
            {/* Said here because it is the one place someone forms a mental
                model of what their account is: it is not a limited login. */}
            <div className="mt-0.5 text-2xs text-text-subtle">
              {user?.is_owner ? 'Owner · full control' : 'Full control'}
            </div>
          </div>

          <MenuItem
            icon={SlidersHorizontal}
            onClick={() => {
              setOpen(false)
              navigate(VIEWS.settings.path)
            }}
          >
            Password and tokens
          </MenuItem>
          <MenuItem
            icon={LogOut}
            onClick={() => {
              setOpen(false)
              void logout()
            }}
          >
            Sign out
          </MenuItem>
        </div>
      )}
    </div>
  )
}

function MenuItem({
  icon: Icon,
  onClick,
  children,
}: {
  icon: typeof LogOut
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-text-muted transition-colors hover:bg-surface-overlay hover:text-text"
    >
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      {children}
    </button>
  )
}
