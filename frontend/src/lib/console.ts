/**
 * Opening a VNC console, in the one order that works.
 *
 * This is four lines of logic and it lives in its own module for one reason:
 * the order of those lines is a *behaviour*, it has been wrong once, and
 * nothing in a type check or a build can tell you which order you wrote. It is
 * exercised by `scripts/check-console-handshake.mjs`, which drives it against
 * a fake channel that reproduces noVNC's attach semantics and fails if the
 * socket is opened too early.
 *
 * ## The order, and why
 *
 * The socket must be created **after** the viewer chunk has loaded, and handed
 * to the viewer in the same tick. Never before.
 *
 * noVNC's `Websock.attach()` (core/websock.js) assigns `onopen` and
 * `onmessage` unconditionally, with no replay of what has already happened —
 * which is correct for a socket it opened itself and wrong for one handed to
 * it late. Two things are lost when the socket has been open for a while:
 *
 *   1. **The `open` event**, which has been and gone. noVNC 1.7 covers this
 *      case in `RFB._connect` by checking `readyState` after attaching, so on
 *      its own it is survivable — but the check is version-dependent and the
 *      handshake still starts late.
 *   2. **The server's greeting**, which is not survivable and is the one that
 *      actually bit. QEMU writes `RFB 003.008\n` the instant it accepts, and a
 *      `message` event dispatched before `onmessage` exists is dispatched to
 *      nobody. DOM events are not buffered; there is no replay. The RFB state
 *      machine then waits forever for a version string that was delivered to
 *      an empty room.
 *
 * The symptom is the panel sitting on "Connecting…" indefinitely with a live
 * socket, a server that has already spoken, and nothing in the console log.
 *
 * Opening the socket before awaiting the ~100 kB viewer chunk lost this race
 * almost every time: a loopback socket opens in ~10 ms and the import resolves
 * in ~90 ms — and it is slower still in production, where the chunk is a real
 * network fetch rather than a warm dev-server module.
 */

/** The parts of a WebSocket this module touches. */
export interface ConsoleSocket {
  binaryType: string
  addEventListener(
    type: 'close',
    listener: (event: { code: number; reason: string }) => void,
  ): void
}

export interface ConsoleAttempt<TViewer> {
  viewer: TViewer
  socket: ConsoleSocket
}

/**
 * Load the viewer, then open the socket, then attach — in that order.
 *
 * Returns null if `cancelled()` reports that the caller lost interest while
 * the chunk was in flight, in which case no socket is opened at all.
 *
 * Every collaborator is injected. That is what lets the regression test drive
 * the real ordering against a channel it controls, rather than asserting
 * against a mock of the thing under test.
 */
export async function connectConsole<TLoaded, TViewer>(options: {
  /** Dynamic import of the viewer. The slow step, and the whole problem. */
  loadViewer: () => Promise<TLoaded>
  /** Create the WebSocket. Must not be called before the viewer is loaded. */
  openSocket: () => ConsoleSocket
  /** Hand the socket to the viewer. Must happen in the same tick. */
  attach: (loaded: TLoaded, socket: ConsoleSocket) => TViewer
  /** Close code and reason, kept so a refusal can be explained to the user. */
  onClose: (info: { code: number; reason: string }) => void
  /** Whether the caller has since given up. Checked after every await. */
  cancelled: () => boolean
}): Promise<ConsoleAttempt<TViewer> | null> {
  const loaded = await options.loadViewer()
  if (options.cancelled()) return null

  // Everything below is synchronous, deliberately. An await anywhere in here
  // reintroduces exactly the gap this function exists to close.
  const socket = options.openSocket()
  socket.binaryType = 'arraybuffer'
  socket.addEventListener('close', (event) =>
    options.onClose({ code: event.code, reason: event.reason }),
  )
  return { viewer: options.attach(loaded, socket), socket }
}
