/**
 * How to reach an instance over SSH.
 *
 * A QEMU VM sits behind a loopback port forward, so it is reachable as
 * 127.0.0.1:<ssh_port> and never at an address of its own. Both the address
 * shown in the table and the command behind Copy SSH are derived here so they
 * can never disagree.
 *
 * Every VM trusts exactly one key — the orchestrator's — so the command must
 * carry `-i <private key>`. Without it ssh offers whatever is in the user's
 * agent and the guest rejects it with "Permission denied (publickey)".
 */
import type { Instance } from '../api/client'

/**
 * Display form of the endpoint, e.g. "127.0.0.1:2200".
 *
 * The port is still treated as optional: retired Multipass rows recorded a
 * routable guest IP and no port, and they must render as the plain address
 * rather than "10.1.2.3:null".
 */
export function displayAddress(inst: Instance): string | null {
  if (!inst.ip_address) return null
  return inst.ssh_port ? `${inst.ip_address}:${inst.ssh_port}` : inst.ip_address
}

/**
 * Quote a filesystem path for a shell.
 *
 * Double quotes rather than single: they are the one form both POSIX shells and
 * cmd.exe/PowerShell understand, and a Windows key path (`C:\Users\...`) stays
 * literal inside them — backslash is only an escape before `$`, a backtick, `"`
 * or another backslash, none of which appear in a path. Always quoted, not just
 * when spaces are present, so the command is copy-safe on any machine.
 */
function quotePath(path: string): string {
  return `"${path.replace(/(["`$\\](?=[["`$\\]))/g, '\\$1')}"`
}

/**
 * The exact command to paste into a terminal, or null if there's nowhere to go.
 *
 * `keyPath` comes from GET /ssh-key. It is omitted only if that request hasn't
 * resolved yet — a command missing `-p` or the key is worse than no command, so
 * callers should treat a null key as "not ready".
 */
export function sshCommand(inst: Instance, keyPath: string | null | undefined): string | null {
  // Both halves are required: QEMU publishes an address only for guests it
  // injected a key into, and always with the forwarded port alongside it. A
  // row carrying one without the other cannot be reached, and offering a
  // half-formed command would be worse than offering none.
  if (!inst.ip_address || !inst.ssh_port) return null
  const parts = ['ssh']
  if (keyPath) parts.push('-i', quotePath(keyPath))
  parts.push('-p', String(inst.ssh_port)) // -p precedes the destination
  parts.push(`${inst.ssh_user}@${inst.ip_address}`)
  return parts.join(' ')
}
