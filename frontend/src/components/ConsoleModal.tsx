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
import { CONSOLE_CLOSE, consoleWsUrl, type Instance } from '../api/client'
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
export function ConsoleModal({ instance, onClose }: ConsoleModalProps) {
  const screenRef = useRef<HTMLDivElement>(null)
  const rfbRef = useRef<RFB | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  // Written by the socket's close handler, read by the RFB disconnect handler —
  // a ref, not state, because the two fire in the same tick and the reason must
  // already be there when we render the disconnected panel.
  const closeInfoRef = useRef<{ code: number; reason: string } | null>(null)

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
    const onConnect = () => {
      setPhase('connected')
      setDetail(null)
      rfb?.focus() // otherwise keystrokes go to the page, not the guest
    }
    const onDisconnect = () => {
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
      let attempt
      try {
        attempt = await connectConsole({
          loadViewer: async () => (await import('@novnc/novnc')).default,
          openSocket: () => {
            socket = new WebSocket(consoleWsUrl(instanceId))
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
                  <span className="text-sm">Connecting to console…</span>
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
