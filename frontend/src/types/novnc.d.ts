/**
 * Minimal typings for @novnc/novnc 1.7, which ships plain ESM with no types.
 *
 * Only the surface this app actually uses is declared — a hand-written stub
 * that claims more than we exercise would be a liability, not a safety net.
 */
declare module '@novnc/novnc' {
  export interface RFBOptions {
    /** Credentials for password-protected RFB servers. QEMU's -vnc has none here. */
    credentials?: { username?: string; password?: string; target?: string }
    shared?: boolean
    repeaterID?: string
    wsProtocols?: string[]
  }

  /** `detail.clean` distinguishes an orderly server close from a dropped socket. */
  export interface RFBDisconnectEvent extends CustomEvent<{ clean: boolean }> {}

  export default class RFB extends EventTarget {
    constructor(
      target: HTMLElement,
      /** A URL, or an already-created WebSocket to attach to. */
      urlOrChannel: string | WebSocket,
      options?: RFBOptions,
    )

    /** Scale the framebuffer to fit its container instead of cropping. */
    scaleViewport: boolean
    /** Clip (and allow dragging) when the framebuffer exceeds the container. */
    clipViewport: boolean
    resizeSession: boolean
    viewOnly: boolean
    focusOnClick: boolean

    focus(): void
    blur(): void
    disconnect(): void
    sendCtrlAltDel(): void
    sendKey(keysym: number, code: string | null, down?: boolean): void
    machineReboot(): void
    machineShutdown(): void
  }
}
