/**
 * Fails when the console socket is opened before the viewer is loaded.
 *
 *   node scripts/check-console-handshake.mjs
 *
 * ## What broke
 *
 * The console panel sat on "Connecting…" forever. The WebSocket was open, the
 * backend had bridged it, QEMU had already written `RFB 003.008\n` — and
 * nothing in the browser console said a word. It survived typecheck, lint,
 * every backend console test (all of which pass: the *bridge* was fine), and a
 * clean build. There was nothing to see, because the failure was an event
 * dispatched to no listener.
 *
 * noVNC's `Websock.attach()` assigns `onmessage` when it is called. A socket
 * opened earlier has already had its `message` events dispatched, and DOM
 * events are not buffered or replayed — the greeting went to an empty room and
 * the RFB state machine waited for it forever. Opening the socket before
 * awaiting the ~100 kB viewer chunk lost that race almost every time.
 *
 * ## What this asserts
 *
 * The real `connectConsole` from `src/lib/console.ts`, driven against a fake
 * channel that reproduces the two behaviours that matter:
 *
 *   - `attach()` assigns handlers with no replay of anything already
 *     dispatched — modelled on noVNC 1.7's core/websock.js;
 *   - the server greets the instant the socket opens, exactly as QEMU does.
 *
 * The handshake completing is the assertion. If the socket is created before
 * the viewer is awaited, the greeting is dispatched while nothing is
 * listening, no handshake starts, and this exits 1.
 *
 * ## Why the clock is not involved
 *
 * The bug is a race, and a test that reproduces a race by racing is a test
 * that passes on a fast machine. Time here is counted in **event-loop turns**,
 * not milliseconds: the viewer loads on turn 5, the socket opens one turn
 * after it is created and greets the turn after that. Correct ordering passes
 * every run; the reverted ordering fails every run.
 *
 * ## Watched to fail
 *
 * Per CONTRIBUTING's verification-integrity rule — a check nobody has watched
 * fire is not evidence — the fix in `src/lib/console.ts` was reverted (the
 * `openSocket()` call moved back above the `await loadViewer()`) and this was
 * run five times: exit 1 on all five, reporting both dropped events and the
 * handshake stalled at "awaiting-version". Restored, five more runs, exit 0 on
 * all five. So it fails for the reason it claims to, and deterministically —
 * which is the point of counting turns instead of milliseconds.
 */

import { connectConsole } from '../src/lib/console.ts'

/** Resolve after `n` macrotasks — a clock with no wall time in it. */
function afterTurns(n) {
  return new Promise((resolve) => {
    let remaining = n
    const tick = () => (remaining-- > 0 ? setTimeout(tick, 0) : resolve())
    tick()
  })
}

const VIEWER_LOAD_TURNS = 5
const GREETING = 'RFB 003.008\n'

/**
 * A WebSocket that behaves like the browser's where it matters.
 *
 * Handlers are plain properties, assigned by whoever attaches. An event that
 * fires while the matching property is unset is *gone* — no queue, no replay.
 * That single line is the entire bug.
 */
class FakeChannel {
  constructor() {
    this.readyState = 0 // CONNECTING
    this.binaryType = ''
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    this.protocol = ''
    /** Events that fired with nothing listening. The evidence, when it fails. */
    this.dropped = []

    // QEMU accepts and greets immediately; the socket opening and the first
    // bytes are one turn apart, not one second.
    setTimeout(() => {
      this.readyState = 1 // OPEN
      if (this.onopen) this.onopen()
      else this.dropped.push('open')

      setTimeout(() => {
        if (this.onmessage) this.onmessage({ data: GREETING })
        else this.dropped.push('message: the RFB version greeting')
      }, 0)
    }, 0)
  }

  send() {}
  close() {
    this.readyState = 3
  }
  addEventListener(type, listener) {
    if (type === 'close') this.onclose = listener
  }
}

/**
 * noVNC, reduced to the part that has a bug in it.
 *
 * `attach` mirrors core/websock.js: assign the handlers, then — as RFB._connect
 * does in 1.7 — start the handshake if the channel is already open. Both are
 * modelled, because the readyState check is what makes the *lost greeting*,
 * rather than the lost open event, the failure that actually reaches the user.
 */
class FakeViewer {
  constructor(channel) {
    this.channel = channel
    this.state = 'idle'
    this.handshakeComplete = false

    channel.onopen = () => this.#socketOpen()
    channel.onmessage = (event) => this.#receive(event.data)
    if (channel.readyState === 1) this.#socketOpen()
  }

  #socketOpen() {
    if (this.state === 'idle') this.state = 'awaiting-version'
  }

  #receive(data) {
    // A greeting that arrives before the socket-open path has run is not a
    // handshake either; both halves have to land, in order.
    if (this.state === 'awaiting-version' && data === GREETING) {
      this.state = 'connected'
      this.handshakeComplete = true
    }
  }
}

const channels = []

const attempt = await connectConsole({
  loadViewer: async () => {
    await afterTurns(VIEWER_LOAD_TURNS) // the ~100 kB dynamic import
    return FakeViewer
  },
  openSocket: () => {
    const channel = new FakeChannel()
    channels.push(channel)
    return channel
  },
  attach: (Viewer, channel) => new Viewer(channel),
  onClose: () => {},
  cancelled: () => false,
})

// Long enough for every scheduled turn to have run, so a failure is "it never
// happened" rather than "it had not happened yet".
await afterTurns(VIEWER_LOAD_TURNS + 10)

const problems = []

if (attempt === null) {
  problems.push('connectConsole returned null — no socket was ever opened.')
} else {
  if (channels.length !== 1) {
    problems.push(`expected exactly one socket, got ${channels.length}.`)
  }
  const channel = channels[0]
  if (channel && channel.dropped.length > 0) {
    problems.push(
      `the socket was open before the viewer attached, so these were ` +
        `dispatched to nobody: ${channel.dropped.join(', ')}.`,
    )
  }
  if (!attempt.viewer.handshakeComplete) {
    problems.push(
      `the RFB handshake never completed (viewer stopped at ` +
        `"${attempt.viewer.state}"). This is the panel sitting on ` +
        `"Connecting…" forever.`,
    )
  }
  if (channel && channel.binaryType !== 'arraybuffer') {
    problems.push(
      `binaryType is "${channel.binaryType}", not "arraybuffer" — framebuffer ` +
        `data would arrive as Blobs that noVNC cannot read.`,
    )
  }
}

if (problems.length === 0) {
  console.log('Console handshake: the socket is opened after the viewer loads.')
  process.exit(0)
}

console.error('Console handshake regression:\n')
for (const problem of problems) console.error(`  - ${problem}`)
console.error(
  '\nThe WebSocket must be created *after* the viewer chunk resolves and ' +
    'handed over in the same tick. See src/lib/console.ts.',
)
process.exit(1)
