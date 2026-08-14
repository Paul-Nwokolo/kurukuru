# PHASE 3 BRIEF — React Dashboard

## Context
Phases 1-2 are DONE. The FastAPI backend runs at http://localhost:8000 with
CORS already configured for http://localhost:5173 (see backend/app/config.py).
Working API surface (see backend/app/routers/instances.py and /docs):

- GET    /health, GET /flavors  (flavors returns {name: {cpus, memory, disk}})
- POST   /instances             body {name, flavor} -> 202, provisions async
- GET    /instances             (?include_terminated=true)
- GET    /instances/{id}
- POST   /instances/{id}/start | /stop     (409 on invalid state transition)
- DELETE /instances/{id}
- POST   /instances/refresh     (forces reconciliation with Multipass)

Instance shape: {id, name, flavor, status, ip_address, created_at,
updated_at, error_message}
Statuses: Pending, Provisioning, Running, Stopped, Terminated, Error.

## Task
Create a `frontend/` directory at the repo root (sibling of `backend/`) with
a Vite + React + Tailwind CSS app. Use JavaScript or TypeScript (prefer TS).

### Stack
- Vite + React 18+, Tailwind CSS, lucide-react icons
- axios for HTTP, @tanstack/react-query for data fetching/polling
- API base URL from `import.meta.env.VITE_API_URL` defaulting to
  http://localhost:8000 (put a .env.example in frontend/)

### Layout & views
1. Dark-mode-first dashboard: fixed left sidebar (app name + nav items:
   "Instances" active, "Images" and "Settings" as disabled placeholders),
   main content area.
2. Instances view:
   - Data table: Name, Status, IP Address, Flavor, Created, Actions.
   - Status rendered as a colored dot + label:
     Running=green, Stopped=gray, Pending/Provisioning=amber (pulsing),
     Error=red, Terminated=zinc/dim.
   - If status is Error, show error_message in a tooltip or expandable row.
   - Row actions (context-sensitive to the state machine):
     Start (only when Stopped), Stop (only when Running),
     Terminate (always, with a confirm dialog).
   - Toggle: "Show terminated" -> adds ?include_terminated=true.
   - Empty state with a "Launch your first instance" call to action.
3. "Launch Instance" modal:
   - Fields: Name (validate: ^[a-z][a-z0-9-]{1,30}$, show inline error),
     Flavor (radio cards built from GET /flavors — show cpus/memory/disk,
     never hardcode the catalog).
   - Submit -> POST /instances -> close modal, show the new Pending row
     immediately. Surface 409 duplicate-name errors inline in the form.
4. Header bar: app title, a "Refresh" button (POST /instances/refresh then
   refetch), and a backend health indicator (green/red dot from GET /health,
   polled every 15s).

### Data behavior (important)
- Poll GET /instances every 3s via react-query `refetchInterval` — VMs change
  state server-side (background provisioning), the UI must follow without
  manual refresh.
- Optimistic-ish UX: after start/stop/terminate, immediately refetch.
- Handle backend-down gracefully: if /health fails, show a non-blocking
  banner ("Backend unreachable") instead of a blank page.
- All API calls in one module: src/api/client.ts. Components never call
  axios directly.

### Quality bar
- Componentize: Sidebar, InstancesTable, StatusBadge, LaunchModal,
  ConfirmDialog, HealthIndicator.
- No UI component library (no MUI/shadcn) — plain Tailwind, clean and dense,
  the aesthetic of a cloud console (think EC2/DigitalOcean, dark).
- Loading skeletons for the table; disable action buttons while a mutation
  is in flight.

### Testing (do this yourself before finishing)
1. `npm run build` must pass with zero errors.
2. Live test with the backend running on 8000: launch a VM named
   "ui-test" (small) from the modal, watch it reach Running in the table
   without any manual refresh, stop it, start it, terminate it with the
   confirm dialog, verify the terminated toggle shows/hides it.
   Clean up: confirm `multipass list` is empty afterward.
3. Show me a summary of the component tree and the live test results.

## Out of scope
No auth, no routing library needed (single view + placeholders is fine),
no cloud-init UI (Phase 4).
