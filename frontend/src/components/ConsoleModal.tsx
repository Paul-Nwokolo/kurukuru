import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Keyboard,
  Loader2,
  Maximize2,
  Minimize2,
  Monitor,
  RefreshCw,
  X,
} from 'lucide-react'
import type RFB from '@novnc/novnc'
import {
  CONSOLE_CLOSE,
  apiErrorMessage,
  consoleWsUrl,
  createConsoleTicket,
  type Instance,
} from '../api/client'
import { connectConsole } from '../lib/console'
import { Button } from '../ui/Button'
import { StatusBadge } from '../ui/Status'

type Phase = 'connecting' | 'connected' | 'disconnected'

interface ConsoleModalProps {
  instance: Instance | null
  onClose: () => void
}

/**
 * Full-screen VNC console for a QEMU instance.
 *
 * The WebSocket is created here rather than letting noVNC open it from a URL,
 * because the backend explains refusals through close codes — and a socket
 * noVNC owns internally gives us no way to read them. noVNC accepts an
 * already-created socket and attaches to it, so we keep the reference, listen
 * for `close`, and can tell the user *why* the console went away instead of
 * showing a bare "disconnected".
 */
/** How many *automatic* reconnects a dropped-but-still-Running console gets
 *  before falling back to the manual "Reconnect" button. Covers the reboot
 *  watchdog's auto-restart (kurukuru/reboot_watchdog.py) and an ordinary
 *  manual restart alike — either way, the backend says the instance is
 *  Running again and a fresh VNC socket is worth trying without the user
 *  having to notice the drop and click something. */
const AUTO_RECONNECT_LIMIT = 3
const AUTO_RECONNECT_DELAY_MS = 1500

export function ConsoleModal({ instance, onClose }: ConsoleModalProps) {
  const screenRef = useRef<HTMLDivElement>(null)
  const rfbRef = useRef<RFB | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  // Written by the socket's close handler, read by the RFB disconnect handler —
  // a ref, not state, because the two fire in the same tick and the reason must
  // already be there when we render the disconnected panel.
  const closeInfoRef = useRef<{ code: number; reason: string } | null>(null)
  // The effect below only re-runs on `instanceId`/`attempt` changing, so a
  // `disconnect` handler closed over `instance` could read a stale status —
  // one fetched minutes before the drop it is now reacting to. This ref is
  // written on every render instead, so onDisconnect always reads the latest
  // poll rather than whatever was current when the connection was opened.
  const instanceRef = useRef(instance)
  instanceRef.current = instance
  const autoReconnectCountRef = useRef(0)

  const [phase, setPhase] = useState<Phase>('connecting')
  const [detail, setDetail] = useState<string | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  // Bumping this tears down the old connection and builds a new one.
  const [attempt, setAttempt] = useState(0)

  const instanceId = instance?.id ?? null

  useEffect(() => {
    if (!instanceId || !screenRef.current) return

    setPhase('connecting')
    setDetail(null)
    closeInfoRef.current = null

    // noVNC is ~100 kB and only this panel needs it, so it is loaded on demand
    // rather than shipped in the main bundle. `cancelled` guards the case where
    // the modal closes while the chunk is still in flight.
    let cancelled = false
    let rfb: RFB | null = null
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof window.setTimeout> | null = null
    const onConnect = () => {
      setPhase('connected')
      setDetail(null)
      autoReconnectCountRef.current = 0 // a real connection earns a fresh budget
      rfb?.focus() // otherwise keystrokes go to the page, not the guest
    }
    const onDisconnect = () => {
      // The backend agreeing the instance is still Running is what makes this
      // worth retrying automatically rather than dumping the user on a manual
      // button: a genuine stop/terminate/error already shows up as a status
      // other than Running, and those should not auto-retry into a wall.
      const stillRunning = instanceRef.current?.status === 'Running'
      if (stillRunning && autoReconnectCountRef.current < AUTO_RECONNECT_LIMIT) {
        autoReconnectCountRef.current += 1
        setPhase('connecting')
        setDetail('Reconnecting…')
        reconnectTimer = window.setTimeout(() => {
          if (!cancelled) setAttempt((n) => n + 1)
        }, AUTO_RECONNECT_DELAY_MS)
        return
      }
      setPhase('disconnected')
      setDetail(explainClose(closeInfoRef.current))
    }
    const onSecurityFailure = (event: Event) => {
      const reason = (event as CustomEvent<{ reason?: string }>).detail?.reason
      setPhase('disconnected')
      setDetail(reason ?? 'The VNC server rejected the connection')
    }

    // The socket is opened by connectConsole, *after* the viewer chunk has
    // loaded and in the same tick as the attach — never before. See
    // src/lib/console.ts for what goes wrong otherwise, and
    // scripts/check-console-handshake.mjs for the test that holds the order
    // in place.
    void (async () => {
      // Minted before connectConsole, because `openSocket` must stay
      // synchronous — awaiting inside it would reopen the race the ordering in
      // src/lib/console.ts exists to close. A ticket lives 30 seconds and the
      // viewer chunk takes ~100 ms, so spending one round trip up front costs
      // nothing and keeps that invariant intact.
      //
      // Its own try, because this failure is an HTTP one and reads nothing like
      // the two below: a 401 here is a session that expired between opening the
      // modal and clicking connect.
      let ticket: string
      try {
        ticket = await createConsoleTicket(instanceId)
      } catch (err) {
        if (cancelled) return
        setPhase('disconnected')
        setDetail(apiErrorMessage(err))
        return
      }
      if (cancelled) return

      let attempt
      try {
        attempt = await connectConsole({
          loadViewer: async () => (await import('@novnc/novnc')).default,
          openSocket: () => {
            socket = new WebSocket(consoleWsUrl(instanceId, ticket))
            socketRef.current = socket
            return socket
          },
          attach: (RFBClass: typeof RFB, channel) =>
            new RFBClass(screenRef.current as HTMLDivElement, channel as WebSocket),
          onClose: (info) => {
            closeInfoRef.current = info
          },
          cancelled: () => cancelled || !screenRef.current,
        })
      } catch (err) {
        if (cancelled) return
        setPhase('disconnected')
        setDetail(
          socket
            ? err instanceof Error
              ? err.message
              : 'Could not start the console'
            : 'Could not load the console viewer.',
        )
        return
      }
      if (attempt === null) return

      rfb = attempt.viewer
      rfbRef.current = rfb
      rfb.scaleViewport = true
      rfb.clipViewport = true
      rfb.focusOnClick = true
      rfb.addEventListener('connect', onConnect)
      rfb.addEventListener('disconnect', onDisconnect)
      rfb.addEventListener('securityfailure', onSecurityFailure)
    })()

    return () => {
      cancelled = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      rfb?.removeEventListener('connect', onConnect)
      rfb?.removeEventListener('disconnect', onDisconnect)
      rfb?.removeEventListener('securityfailure', onSecurityFailure)
      try {
        rfb?.disconnect()
      } catch {
        // Already torn down — nothing to do.
      }
      try {
        socket?.close()
      } catch {
        // Already closed.
      }
      rfbRef.current = null
      socketRef.current = null
    }
  }, [instanceId, attempt])

  // A manual close/reopen of the modal should not carry a stale auto-retry
  // budget into the next session's first disconnect.
  useEffect(() => {
    if (!instanceId) autoReconnectCountRef.current = 0
  }, [instanceId])

  // Esc closes the console — but only when not fullscreen, where the browser
  // uses Esc to leave fullscreen first.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.fullscreenElement) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    const onChange = () => setFullscreen(Boolean(document.fullscreenElement))
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  const panelRef = useRef<HTMLDivElement>(null)
  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen()
    else void panelRef.current?.requestFullscreen()
  }, [])

  if (!instance) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-surface/90 p-4 backdrop-blur-sm">
      {/*
        The one screen that should feel like hardware.

        It gets --surface-sunken (pure black in dark, near-black in light) and
        the quietest chrome in the app: a single-height toolbar, no card
        padding around the framebuffer, and nothing coloured except the
        connection state. Everywhere else is a document; this is a monitor.
      */}
      <div
        ref={panelRef}
        className="relative flex h-full w-full max-w-6xl flex-col overflow-hidden rounded-lg border border-border bg-surface-sunken shadow-[var(--shadow-overlay)]"
      >
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-3 py-2">
          <div className="flex items-center gap-2.5">
            <Monitor className="h-3.5 w-3.5 text-text-on-sunken/60" />
            <span className="text-sm font-semibold text-text-on-sunken">{instance.name}</span>
            <StatusBadge status={PHASE_STATUS[phase]} label={PHASE_LABEL[phase]} />
          </div>
          <div className="flex items-center gap-1">
            <ToolbarButton
              icon={Keyboard}
              label="Ctrl+Alt+Del"
              disabled={phase !== 'connected'}
              onClick={() => rfbRef.current?.sendCtrlAltDel()}
            />
            <ToolbarButton
              icon={fullscreen ? Minimize2 : Maximize2}
              label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
              onClick={toggleFullscreen}
            />
            <button
              type="button"
              onClick={onClose}
              aria-label="Close console"
              className="ml-1 rounded p-1.5 text-text-on-sunken/50 transition-colors hover:bg-text-on-sunken/10 hover:text-text-on-sunken"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="relative min-h-0 flex-1">
          <div ref={screenRef} className="h-full w-full" />

          {phase !== 'connected' && (
            <div className="absolute inset-0 flex items-center justify-center bg-surface-sunken/90">
              {phase === 'connecting' ? (
                <div className="flex flex-col items-center gap-3 text-text-on-sunken/70">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  <span className="text-sm">{detail ?? 'Connecting to console…'}</span>
                </div>
              ) : (
                <div className="flex max-w-md flex-col items-center gap-3 px-6 text-center">
                  <span className="text-base font-semibold text-text-on-sunken">
                    Disconnected
                  </span>
                  {detail && <p className="text-sm text-text-on-sunken/70">{detail}</p>}
                  <Button
                    intent="primary"
                    icon={RefreshCw}
                    className="mt-1"
                    onClick={() => setAttempt((n) => n + 1)}
                  >
                    Reconnect
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* The caveat belongs here, not only in a tooltip: this is where a
            black screen is actually experienced, and it names the fix. */}
        {instance.console_caveat && phase === 'connected' && (
          <div className="shrink-0 border-t border-transitional/30 bg-transitional-quiet px-3 py-1.5 text-xs text-transitional">
            {instance.console_caveat}
          </div>
        )}

        <div className="shrink-0 border-t border-border px-3 py-1.5 text-xs text-text-on-sunken/50">
          Click the screen to type into the guest · Esc closes the console
        </div>
      </div>
    </div>
  )
}

/** Turn a WebSocket close code from the backend into something worth reading. */
function explainClose(info: { code: number; reason: string } | null): string {
  if (!info) return 'The console connection ended.'
  if (info.reason) return info.reason // backend sent a specific explanation
  switch (info.code) {
    case CONSOLE_CLOSE.UNAUTHENTICATED:
      return 'The console ticket was refused. Close this and open it again.'
    case CONSOLE_CLOSE.NOT_FOUND:
      return 'This instance no longer exists.'
    case CONSOLE_CLOSE.CONFLICT:
      return 'The console is unavailable for this instance right now.'
    case CONSOLE_CLOSE.VNC_UNAVAILABLE:
      return 'Could not reach the VM’s display — it may have stopped.'
    default:
      return 'The console connection closed.'
  }
}

/** Toolbar controls live on the sunken surface, so they cannot use the
 *  standard Button intents — those are painted for --surface. */
function ToolbarButton({
  icon: Icon,
  label,
  onClick,
  disabled = false,
}: {
  icon: typeof Keyboard
  label: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={label}
      className="inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs font-medium text-text-on-sunken/70 transition-colors hover:bg-text-on-sunken/10 hover:text-text-on-sunken disabled:cursor-not-allowed disabled:opacity-40"
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  )
}

/** The console's connection state, spoken in the app's status vocabulary. */
const PHASE_STATUS: Record<Phase, string> = {
  connecting: 'Provisioning',
  connected: 'Running',
  disconnected: 'Error',
}
const PHASE_LABEL: Record<Phase, string> = {
  connecting: 'Connecting',
  connected: 'Connected',
  disconnected: 'Disconnected',
}
