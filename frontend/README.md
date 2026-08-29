# Frontend — dev notes

The dashboard. Setup and the first-launch walkthrough live in the
[root README](../README.md); this file is frontend-specific notes only.

React 19 + TypeScript + Vite, styled with Tailwind v4, server state via
TanStack Query. Oxlint rather than ESLint.

## Run

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # typecheck (tsc -b) + production build
npm run lint     # oxlint
```

There is no test runner here. The frontend's logic is kept thin — the backend
suite plus live verification covers behaviour. See
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Talking to the backend

`VITE_API_URL` (see `.env.example`) sets the API base, default
`http://localhost:7842`. All HTTP access goes through `src/api/client.ts`;
components never import axios directly.

The console WebSocket is the exception. In dev it is opened **same-origin** and
proxied by Vite to the backend — see the `server.proxy` entry in
`vite.config.ts`, scoped by regex to `/instances/<id>/console` so it catches the
upgrade and nothing else. `consoleWsUrl()` in `client.ts` picks same-origin in
dev and the configured API base in a build.

## Layout

```
src/
  api/client.ts        every HTTP call + shared domain types
  hooks/queries.ts     TanStack Query hooks; the only place touching the cache
  components/
    InstancesTable     the instance list and its row actions
    LaunchModal        intent-framed launch flow (modes, sizing, Advanced)
    ConsoleModal       noVNC console; loads @novnc/novnc via dynamic import
    ImagesTable        image catalog
    ImportImageModal   register a local disk image
    StatusBadge        instance status, including derived "Degraded"
    EngineBadge        driver label, including retired engines
  lib/
    ssh.ts             the SSH command and displayed address (single source)
    format.ts          time and byte/memory formatting
  types/novnc.d.ts     hand-written typings; @novnc/novnc ships none
```

Two things worth knowing before changing them:

- **`lib/ssh.ts` is the single source** for both the address shown in the table
  and the command behind Copy SSH, so the two cannot disagree. The command must
  include `-i <key>` — VMs trust only the orchestrator's key, and without it ssh
  fails with "Permission denied (publickey)".
- **noVNC is code-split.** It is ~187 kB and only the console needs it, so it is
  loaded with a dynamic `import()` inside the connect effect rather than being
  shipped in the main bundle.
